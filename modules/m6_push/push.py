# -*- coding: utf-8 -*-
"""
M6 微信推送模块 — 通过 Server酱 把日报摘要推送到微信

输入:
  data/structured_news.json   （M2 结构化新闻，选 3 条重点）
  data/analysis.md            （M3 分析，取情绪一句话）

配置（环境变量）:
  SERVERCHAN_SENDKEY    Server酱 SendKey（SCT 开头 → Turbo版 API）
  REPORT_BASE_URL       日报根 URL，用于拼接当天日报链接（M7 起为 GitHub Pages）

要点：
1. 推送失败只记录日志，不影响日报已生成的事实（不抛异常退出）
2. 免费版限制 5 次/天，故只推情绪 + 3 条重点新闻标题，正文放链接
3. 重点新闻排序：confirmed 优先 > 政策/个股权重 > 利好/利空优先
"""
import os
import re
import json
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime

BASE = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE / "data"

SC_API = "https://sctapi.ftqq.com/{sendkey}.send"

CAT_WEIGHT = {"policy": 5, "stock": 4, "industry": 3, "international": 2, "other": 1}
SENTI_WEIGHT = {"bullish": 2, "bearish": 2, "neutral": 1}
SENTI_CN = {"bullish": "利好", "bearish": "利空", "neutral": "中性"}
CAT_CN = {"policy": "政策", "stock": "个股", "industry": "行业", "international": "国际", "other": "其他"}


def extract_sentiment(analysis_md):
    """从 M3 分析取"结论：…"情绪一句话"""
    for line in analysis_md.splitlines():
        m = re.match(r"^\s*结论[：:]\s*(.+)$", line)
        if m:
            core = m.group(1).strip()
            return re.sub(r"\*\*(.+?)\*\*", r"\1", core)
    return "暂无市场情绪判断"


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


def send(title, desp, sendkey):
    """调用 Server酱 推送。返回 (ok, message)。失败不抛异常。"""
    url = SC_API.format(sendkey=sendkey)
    payload = urllib.parse.urlencode({"title": title, "desp": desp}).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = json.loads(r.read().decode("utf-8"))
        code = resp.get("code")
        if code == 0:
            pushid = (resp.get("data") or {}).get("pushid", "")
            return True, f"pushid={pushid}"
        return False, f"code={code} message={resp.get('message', '')}"
    except Exception as e:
        return False, str(e)


def main():
    sendkey = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
    if not sendkey:
        print("[warn] SERVERCHAN_SENDKEY 未设置，跳过推送（不影响日报）")
        return

    structured = json.loads((DATA_DIR / "structured_news.json").read_text(encoding="utf-8"))
    analysis_md = (DATA_DIR / "analysis.md").read_text(encoding="utf-8")

    date_str = datetime.now().strftime("%Y-%m-%d")
    sentiment = extract_sentiment(analysis_md)

    # 3 条重点新闻
    top = select_top_news(structured.get("news", []), k=3)
    lines = []
    for i, n in enumerate(top, 1):
        title = extract_title(n.get("text", ""))
        cat = CAT_CN.get(n.get("category"), n.get("category"))
        senti = SENTI_CN.get(n.get("sentiment"), "")
        verified = "已确认" if n.get("verified") == "confirmed" else "待核实"
        lines.append(f"{i}. [{cat}] {title}（{senti}·{verified}）")

    # 日报链接（gh-pages 根目录即 reports 内容，故无需 /reports/ 前缀）
    base_url = os.environ.get("REPORT_BASE_URL", "").strip().rstrip("/")
    if base_url:
        report_url = f"{base_url}/{date_str}.html"
        link_line = f"\n[查看完整日报 →]({report_url})"
    else:
        link_line = "\n（REPORT_BASE_URL 未设置，暂不附链接）"

    title = f"📊 股市情报日报 {date_str}"
    desp = (
        f"### 今日市场\n{sentiment}\n\n"
        f"### 重点新闻\n" + "\n".join(lines) + link_line
    )

    ok, msg = send(title, desp, sendkey)
    if ok:
        print(f"[OK] 已推送到微信（{msg}）")
        print(f"     标题: {title}")
        print(f"     正文:\n{desp}")
    else:
        print(f"[warn] 推送失败（{msg}），日报已生成不受影响")
        # 失败时仍打印内容，便于核对
        print(f"     标题: {title}")
        print(f"     正文:\n{desp}")


if __name__ == "__main__":
    main()
