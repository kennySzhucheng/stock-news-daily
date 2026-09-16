# -*- coding: utf-8 -*-
"""
M6 微信推送模块 — 通过 Server酱 把日报摘要推送到微信

输入:
  data/structured_news.json   （M2 结构化新闻，选 3 条重点）
  data/analysis.md            （M3 分析，取情绪结论与关注板块）
  reports/                    （M5 产出，用于定位当天日报链接）

配置（环境变量）:
  SERVERCHAN_SENDKEY    Server酱 SendKey（SCT 开头 → Turbo版 API）
  REPORT_BASE_URL       日报根 URL，用于拼接当天日报链接（M7 起为 GitHub Pages）
  REPORT_SLOT           手动指定时段 am/pm，不设则按北京时间判定

要点：
1. 推送失败只记录日志，不影响日报已生成的事实（不抛异常退出）
2. 免费版限制 5 次/天，故只推市场分析 + 3 条重点新闻标题，正文放链接
3. **市场分析置于最前**：情绪结论 + 关注板块逻辑链，新闻标题排在其后
4. 重点新闻排序：confirmed 优先 > 政策/个股权重 > 利好/利空优先
"""
import os
import re
import json
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone, timedelta

BASE = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE / "data"
REPORTS_DIR = BASE / "reports"

CST = timezone(timedelta(hours=8))
SLOT_CN = {"am": "盘前", "pm": "盘后"}

SC_API = "https://sctapi.ftqq.com/{sendkey}.send"

CAT_WEIGHT = {"policy": 5, "stock": 4, "industry": 3, "international": 2, "other": 1}
SENTI_WEIGHT = {"bullish": 2, "bearish": 2, "neutral": 1}
SENTI_CN = {"bullish": "利好", "bearish": "利空", "neutral": "中性"}
CAT_CN = {"policy": "政策", "stock": "个股", "industry": "行业", "international": "国际", "other": "其他"}


def detect_slot(now=None):
    """与 M5 保持一致：北京时间 12:00 前为盘前(am)，之后为盘后(pm)"""
    now = now or datetime.now(CST)
    return "am" if now.hour < 12 else "pm"


def extract_sentiment(analysis_md):
    """从 M3 分析取"结论：…"情绪一句话。

    M3 会不定期把整行写成 `**结论：中性偏谨慎**——…`，加粗标记落在行首，
    直接匹配行首会漏掉，推送就变成"暂无市场情绪判断"。故先剥掉加粗。
    """
    for line in analysis_md.splitlines():
        s = re.sub(r"\*\*(.+?)\*\*", r"\1", line).strip()
        m = re.match(r"^结论[：:]\s*(.+)$", s)
        if m:
            return m.group(1).strip()
    return "暂无市场情绪判断"


def extract_market_view(analysis_md, max_sections=3, max_len=80):
    """从 M3 分析的「值得关注的板块」抽取 (板块名, 逻辑链摘要, 置信度)。

    推送里这块放在最前面，让用户先看到市场判断再看新闻素材。
    """
    sections = []
    cur = None
    for line in analysis_md.splitlines():
        s = line.strip()
        m = re.match(r"^###\s+(.+?)\s*$", s)
        if m:
            cur = {"name": m.group(1).strip(), "logic": "", "conf": ""}
            sections.append(cur)
            continue
        if cur is None:
            continue
        # 遇到二级（或更高级）标题说明本节已结束
        if re.match(r"^#{1,2}\s", s):
            cur = None
            continue
        m = re.match(r"^[-*]\s*逻辑链[：:]\s*(.*)$", s)
        if m and not cur["logic"]:
            cur["logic"] = re.sub(r"\*\*(.+?)\*\*", r"\1", m.group(1)).strip()
            continue
        m = re.match(r"^[-*]\s*[（(]置信度[：:]\s*([^）)]+)[）)]\s*$", s)
        if m and not cur["conf"]:
            cur["conf"] = m.group(1).strip()

    out = []
    for sec in sections[:max_sections]:
        if not sec["logic"]:
            continue
        # 推送里没有新闻编号表，去掉 [12] 这类引用，避免读者无从对照
        logic = re.sub(r"\[\d+\]", "", sec["logic"])
        logic = re.sub(r"[（(]\s*[）)]", "", logic)
        logic = re.sub(r"\s+", " ", logic).strip(" ；;，,、")
        if len(logic) > max_len:
            logic = logic[:max_len - 1] + "…"
        if logic:
            out.append((sec["name"], logic, sec["conf"]))
    return out


def extract_title(text, max_len=40):
    """从新闻正文抽取标题：优先【…】，否则截断；统一上限 max_len 字"""
    m = re.match(r"^【(.+?)】", text)
    t = m.group(1).strip() if m else text.strip()
    if len(t) > max_len:
        t = t[:max_len] + "…"
    return t


def _stem(s):
    """归一化主干：去标点空白，取前 12 字，用于标题去重"""
    s = re.sub(r"[^一-鿿A-Za-z0-9]", "", s or "")
    return s[:12]


def select_top_news(news, k=3):
    """按 确认优先 > 类别权重 > 情绪显著性 排序，去重后取前 k 条"""
    def score(n):
        verified = 1 if n.get("verified") == "confirmed" else 0
        cat = CAT_WEIGHT.get(n.get("category"), 1)
        senti = SENTI_WEIGHT.get(n.get("sentiment"), 1)
        return (verified, cat, senti)
    ranked = sorted(news, key=score, reverse=True)
    picked, seen = [], []
    for n in ranked:
        t = extract_title(n.get("text", ""))
        if _stem(t) in seen:
            continue  # 同一事件的多源报道，只推一条
        picked.append(n)
        seen.append(_stem(t))
        if len(picked) >= k:
            break
    return picked


def _urlopen(req, timeout=20):
    """优先直连，失败回退系统代理。

    Windows 上 urllib 会自动读取系统代理设置；若梯子开着但节点不通，
    所有请求都会失败。
    """
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(req, timeout=timeout)
    except Exception:
        return urllib.request.urlopen(req, timeout=timeout)


def send(title, desp, sendkey):
    """调用 Server酱 推送。返回 (ok, message)。失败不抛异常。"""
    url = SC_API.format(sendkey=sendkey)
    payload = urllib.parse.urlencode({"title": title, "desp": desp}).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=payload, method="POST")
        with _urlopen(req, 20) as r:
            resp = json.loads(r.read().decode("utf-8"))
        code = resp.get("code")
        if code == 0:
            pushid = (resp.get("data") or {}).get("pushid", "")
            return True, f"pushid={pushid}"
        return False, f"code={code} message={resp.get('message', '')}"
    except Exception as e:
        return False, str(e)


def resolve_report_name(date_str, slot):
    """定位 M5 产出的当天日报文件名。

    优先按实际文件判断（同日盘前/盘后各一份，取较晚的那份），
    文件不存在时回退到「日期-时段」的约定命名。
    """
    cands = sorted(REPORTS_DIR.glob(f"{date_str}-*.html"))
    if cands:
        return cands[-1].name
    return f"{date_str}-{slot}.html"


def build_digest(date_str, slot, sentiment, market_view, top_news):
    """拼推送正文。顺序：市场分析（情绪 + 关注板块）→ 重点新闻 → 日报链接。

    市场判断放最前，让用户不点开链接也能先拿到结论；新闻标题退居其次。
    """
    parts = [f"### 📈 市场分析（{SLOT_CN.get(slot, '')}）", sentiment]

    if market_view:
        parts.append("")
        parts.append("**关注板块**")
        for i, (name, logic, conf) in enumerate(market_view, 1):
            tail = f"（{conf}）" if conf else ""
            parts.append(f"{i}. **{name}**：{logic}{tail}")

    parts.append("")
    parts.append("### 📌 重点新闻")
    for i, n in enumerate(top_news, 1):
        title = extract_title(n.get("text", ""))
        cat = CAT_CN.get(n.get("category"), n.get("category"))
        senti = SENTI_CN.get(n.get("sentiment"), "")
        verified = "已确认" if n.get("verified") == "confirmed" else "待核实"
        parts.append(f"{i}. [{cat}] {title}（{senti}·{verified}）")

    return "\n".join(parts)


def main():
    sendkey = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
    if not sendkey:
        print("[warn] SERVERCHAN_SENDKEY 未设置，跳过推送（不影响日报）")
        return

    structured = json.loads((DATA_DIR / "structured_news.json").read_text(encoding="utf-8"))
    analysis_md = (DATA_DIR / "analysis.md").read_text(encoding="utf-8")

    now = datetime.now(CST)
    date_str = now.strftime("%Y-%m-%d")
    slot = os.environ.get("REPORT_SLOT", "").strip().lower()
    if slot not in ("am", "pm"):
        slot = detect_slot(now)

    sentiment = extract_sentiment(analysis_md)
    market_view = extract_market_view(analysis_md)
    top = select_top_news(structured.get("news", []), k=3)

    # 日报链接（gh-pages 根目录即 reports 内容，故无需 /reports/ 前缀）
    base_url = os.environ.get("REPORT_BASE_URL", "").strip().rstrip("/")
    if base_url:
        report_name = resolve_report_name(date_str, slot)
        link_line = f"\n\n[查看完整日报 →]({base_url}/{report_name})"
    else:
        link_line = "\n\n（REPORT_BASE_URL 未设置，暂不附链接）"

    title = f"📊 股市情报日报 {date_str} · {SLOT_CN.get(slot, '')}"
    desp = build_digest(date_str, slot, sentiment, market_view, top) + link_line

    ok, msg = send(title, desp, sendkey)
    if ok:
        print(f"[OK] 已推送到微信（{msg}）")
    else:
        print(f"[warn] 推送失败（{msg}），日报已生成不受影响")
    # 成功与否都打印内容，便于核对
    print(f"     时段: {slot} / 关注板块 {len(market_view)} 个")
    print(f"     标题: {title}")
    print(f"     正文:\n{desp}")


if __name__ == "__main__":
    main()
