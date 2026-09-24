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
  REPORT_PLAN_TIME      定时任务计划时点，如 "08:10"（手动触发时为空）
  REPORT_DELAY_MIN      实际开始相对计划时点的延迟分钟数

要点：
1. 推送失败只记录日志，不影响日报已生成的事实（不抛异常退出）
2. 免费版限制 5 次/天，故只推市场分析 + 3 条重点新闻标题，正文放链接
3. **市场分析置于最前**：情绪结论 + 关注板块逻辑链，新闻标题排在其后
4. 重点新闻排序：confirmed 优先 > 政策/个股权重 > 利好/利空优先
5. **定时延迟标注**：cron 被延迟投递时在标题与正文首行标出实际生成时点，
   避免用户把延迟版"盘前"推送当成真正的盘前快照（与 M5 日报一致）
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

# 延迟低于此分钟数不值得标注（与 M5 保持一致）
DELAY_THRESHOLD_MIN = 15

SC_API = "https://sctapi.ftqq.com/{sendkey}.send"

CAT_WEIGHT = {"policy": 5, "stock": 4, "industry": 3, "international": 2, "other": 1}
SENTI_WEIGHT = {"bullish": 2, "bearish": 2, "neutral": 1}
SENTI_CN = {"bullish": "利好", "bearish": "利空", "neutral": "中性"}
CAT_CN = {"policy": "政策", "stock": "个股", "industry": "行业", "international": "国际", "other": "其他"}


def detect_slot(now=None):
    """与 M5 保持一致：北京时间 12:00 前为盘前(am)，之后为盘后(pm)"""
    now = now or datetime.now(CST)
    return "am" if now.hour < 12 else "pm"


def _fmt_delay(minutes, short=False):
    """把延迟分钟数写成中文（"4小时36分"）或短式（"4h36m"）"""
    h, m = divmod(int(minutes), 60)
    if short:
        return f"{h}h{m:02d}m" if h else f"{m}m"
    if h and m:
        return f"{h}小时{m:02d}分"
    if h:
        return f"{h}小时"
    return f"{m}分钟"


def delay_context(now=None):
    """读取 workflow 注入的延迟信息（与 m5_report/report.py 中的同名函数一致）。

    定时任务被延迟投递时，用户收到的"盘前"推送里装的其实是当天上午的盘中新闻。
    微信推送只有标题和摘要两处可用于提示，故两者都标。
    """
    plan = (os.environ.get("REPORT_PLAN_TIME") or "").strip()
    raw = (os.environ.get("REPORT_DELAY_MIN") or "").strip()
    if not plan or not raw:
        return {"late": False}
    try:
        delay_min = int(float(raw))
    except ValueError:
        return {"late": False}
    if delay_min < DELAY_THRESHOLD_MIN:
        return {"late": False}

    now = now or datetime.now(CST)
    env_slot = (os.environ.get("REPORT_SLOT") or "").strip().lower()
    slot_cn = SLOT_CN.get(env_slot if env_slot in ("am", "pm") else detect_slot(now), "")
    return {
        "late": True,
        "plan": plan,
        "actual": now.strftime("%H:%M"),
        "short": f"延迟{_fmt_delay(delay_min, short=True)}",
        "text": (
            f"定时任务延迟：计划 {plan} 生成，实际 {now.strftime('%H:%M')} 才运行"
            f"（{_fmt_delay(delay_min)}）。内容采集自实际运行时刻前 24 小时，"
            f"并非严格意义的{slot_cn}快照。"
        ),
    }


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


def load_picks(date_str):
    """reports/picks/ledger.jsonl → {"today": [...], "stats": "..."}，读不到返回 None。

    **故意不 import m10_picks**：那个模块在 import 期就会加载 M4/M3 并连网，
    为了读一个文件把整条依赖链拖进推送模块不值得（推送是最后一步，最该轻）。
    这里自己解析 JSON Lines，坏行跳过，任何异常都退化成「没有候选块」。
    """
    try:
        path = REPORTS_DIR / "picks" / "ledger.jsonl"
        if not path.exists():
            return None
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if not rows:
            return None
        today = [r for r in rows if r.get("date") == date_str]

        # 汇总：**均值必须与样本数一起给**，且不叫「胜率」—— 样本量小时
        # 百分比没有意义，二值的对/错也会抹掉「跑赢 0.1%」与「跑输 0.1%」的区别
        bits = []
        for k in (1, 3, 5):
            vals = [(r.get("reviews") or {}).get(str(k)) for r in rows]
            vals = [v for v in vals
                    if v and v.get("status") == "ok" and v.get("alpha") is not None]
            if not vals:
                continue
            if len(vals) < 3:
                bits.append(f"T+{k} 样本 {len(vals)} 条（不足 3 条不给均值）")
            else:
                avg = sum(v["alpha"] for v in vals) / len(vals)
                bits.append(f"T+{k} 均超额 {avg:+.2%}（样本 {len(vals)} 条）")
        return {"today": today, "stats": "　".join(bits)}
    except Exception as e:
        print(f"[warn] 候选账本读取失败（{str(e)[:50]}），推送不含候选块")
        return None


def _clip(s, n):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def build_picks_block(picks):
    """候选观察清单 → 推送正文的一段。picks 为 None（账本缺失）时返回 []。"""
    if not picks:
        return []
    parts = ["", "### 🎯 候选观察清单（不是买入建议）"]
    today = picks.get("today") or []
    if today:
        for i, r in enumerate(today, 1):
            tag = "〔板块〕" if r.get("kind") == "board" else ""
            conf = f"（{r['confidence']}）" if r.get("confidence") else ""
            parts.append(f"{i}. **{r.get('name')}**{tag}："
                         f"{_clip(r.get('logic'), 60)}{conf}")
            parts.append(f"   推翻信号：{_clip(r.get('invalidation'), 40)}")
    else:
        # 盘前那半数运行永远没有当日候选，但**跟踪中的候选往往正好到期**，
        # 那段表现是盘前推送唯一的价值增量，不能只留一块空白
        parts.append("盘前不记录新候选（新候选只在收盘后记录）；"
                     "以下是跟踪中候选的表现。")
    if picks.get("stats"):
        parts.append(f"📊 已回填：{picks['stats']}")
    parts.append("（候选为观察清单，非买入指令；历史表现不代表未来）")
    return parts


def build_digest(date_str, slot, sentiment, market_view, top_news, delay=None, picks=None):
    """拼推送正文。顺序：延迟提示（若有）→ 市场分析（情绪 + 关注板块）
    → 候选观察清单 → 重点新闻 → 日报链接。

    市场判断放最前，让用户不点开链接也能先拿到结论；新闻标题退居其次。
    候选块紧跟市场分析 —— 它是从那份分析派生出来的、可事后检验的一层。
    """
    delay = delay or {"late": False}
    parts = []
    if delay.get("late"):
        parts.append(f"> ⚠️ {delay['text']}")
        parts.append("")

    parts.append(f"### 📈 市场分析（{SLOT_CN.get(slot, '')}）")
    parts.append(sentiment)

    if market_view:
        parts.append("")
        parts.append("**关注板块**")
        for i, (name, logic, conf) in enumerate(market_view, 1):
            tail = f"（{conf}）" if conf else ""
            parts.append(f"{i}. **{name}**：{logic}{tail}")

    parts += build_picks_block(picks)

    parts.append("")
    parts.append("### 📌 重点新闻")
    for i, n in enumerate(top_news, 1):
        title = extract_title(n.get("text", ""))
        cat = CAT_CN.get(n.get("category"), n.get("category"))
        senti = SENTI_CN.get(n.get("sentiment"), "")
        verified = "已确认" if n.get("verified") == "confirmed" else "待核实"
        parts.append(f"{i}. [{cat}] {title}（{senti}·{verified}）")

    return "\n".join(parts)


def write_push_status(date_str, slot, ok, detail):
    """把推送结果落成 reports/status-<date>-<slot>.json，随日报一起上线。

    为什么需要：**推送失败是静默的** —— 日报照常生成、网页照常上线，只有微信
    收不到。Server酱免费额度只有 5 条/天，额度耗尽正是这种表现（长期笔记里记过）。
    M8 的送达体检（modules/m8_e2e/healthcheck.py）读这个文件，判断「该到的推送
    到底到了没有」—— 否则这种失败只能靠人碰巧发现。

    文件名以 status- 开头，不会被 M5 的索引页（glob *.html）或 M6 自己的
    resolve_report_name（glob <日期>-*.html）误取。写失败不影响推送与日报。
    """
    try:
        REPORTS_DIR.mkdir(exist_ok=True)
        payload = {
            "date": date_str,
            "slot": slot,
            "push_ok": bool(ok),
            "push_detail": detail,
            "run_number": os.environ.get("GITHUB_RUN_NUMBER", ""),
            "event": os.environ.get("GITHUB_EVENT_NAME", ""),
            "generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        }
        (REPORTS_DIR / f"status-{date_str}-{slot}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        print(f"[OK] 推送状态已记录: status-{date_str}-{slot}.json")
    except Exception as e:
        print(f"[warn] 推送状态写入失败（{e}），不影响推送与日报")


def main():
    sendkey = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
    if not sendkey:
        print("[warn] SERVERCHAN_SENDKEY 未设置，跳过推送（不影响日报）")
        # 也要落状态：密钥缺失同样是「该到的推送没到」，体检得看得见
        _now = datetime.now(CST)
        _slot = os.environ.get("REPORT_SLOT", "").strip().lower()
        if _slot not in ("am", "pm"):
            _slot = detect_slot(_now)
        write_push_status(_now.strftime("%Y-%m-%d"), _slot, False,
                          "SERVERCHAN_SENDKEY 未设置")
        return

    structured = json.loads((DATA_DIR / "structured_news.json").read_text(encoding="utf-8"))
    analysis_md = (DATA_DIR / "analysis.md").read_text(encoding="utf-8")

    now = datetime.now(CST)
    date_str = now.strftime("%Y-%m-%d")
    slot = os.environ.get("REPORT_SLOT", "").strip().lower()
    if slot not in ("am", "pm"):
        slot = detect_slot(now)
    os.environ["REPORT_SLOT"] = slot          # 让 delay_context 拿到最终时段
    delay = delay_context(now)

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

    # Server酱 Turbo 标题上限 32 字符，有延迟时改用短日期腾出空间
    if delay.get("late"):
        title = (f"📊 股市情报日报 {now.strftime('%m-%d')} "
                 f"{SLOT_CN.get(slot, '')}·{delay['short']}")
    else:
        title = f"📊 股市情报日报 {date_str} · {SLOT_CN.get(slot, '')}"
    picks = load_picks(date_str)
    desp = build_digest(date_str, slot, sentiment, market_view, top, delay,
                        picks=picks) + link_line

    ok, msg = send(title, desp, sendkey)
    if ok:
        print(f"[OK] 已推送到微信（{msg}）")
    else:
        print(f"[warn] 推送失败（{msg}），日报已生成不受影响")
    write_push_status(date_str, slot, ok, msg)
    # 成功与否都打印内容，便于核对
    print(f"     时段: {slot} / 关注板块 {len(market_view)} 个 / "
          f"候选 {len((picks or {}).get('today') or [])} 条")
    print(f"     标题: {title}")
    print(f"     正文:\n{desp}")


if __name__ == "__main__":
    main()
