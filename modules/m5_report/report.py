# -*- coding: utf-8 -*-
"""
M5 网页日报生成模块 — 把 M1~M4 产物整合成一份自包含 HTML 日报

输入:
  data/raw_news.json          （M1 原始新闻，用于统计）
  data/structured_news.json   （M2 结构化新闻，板块/个股/情绪/验证标记）
  data/analysis.md            （M3 DeepSeek 完整分析）
  data/quotes.json            （M4 个股行情）

输出:
  reports/YYYY-MM-DD-am.html  （盘前版，当日自包含，移动端优先、暗色主题）
  reports/YYYY-MM-DD-pm.html  （盘后版，与盘前版并存互不覆盖）
  reports/latest.html         （最近一次运行的那份，固定入口）
  reports/index.html          （索引页，按日期倒序、每天列出盘前/盘后）

要点：
1. 纯单文件 HTML，CSS/JS 全部内联，无任何外部依赖
2. 移动端优先：单栏、大字号、夜间友好，无横向滚动
3. 所有动态文本先 HTML 转义再拼接，防注入
4. 时段由北京时间判定：12:00 前为盘前(am)，之后为盘后(pm)；
   同日两次运行产出不同文件名，两份都保留
5. **定时延迟标注**：GitHub Actions 的 schedule 事件会被延迟投递（实测 2026-09-17
   延迟 4~5 小时），此时"盘前"标签与实际内容严重不符——12:46 才跑的任务抓的是
   当天上午的盘中新闻。故由 workflow 注入计划时点与延迟分钟数，本报在标题与
   页顶标出，避免读者把延迟版误读成真正的盘前快照。

用法:
    python modules/m5_report/report.py              # 按时段自动命名
    python modules/m5_report/report.py --slot am    # 手动指定时段

环境变量（可选，由 workflow 注入；本地运行不设即不标注）:
    REPORT_SLOT        时段 am/pm，优先级低于命令行 --slot
    REPORT_PLAN_TIME   定时任务计划时点，如 "08:10"（手动触发时为空）
    REPORT_DELAY_MIN   实际开始相对计划时点的延迟分钟数
"""
import json
import os
import re
import sys
import html
from pathlib import Path
from datetime import datetime, timezone, timedelta

BASE = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE / "data"
REPORTS_DIR = BASE / "reports"

CST = timezone(timedelta(hours=8))          # 北京时间
SLOT_CN = {"am": "盘前", "pm": "盘后"}

# 分类中文名
CAT_CN = {
    "stock": "个股公告",
    "policy": "宏观政策",
    "industry": "行业动态",
    "international": "国际市场",
    "other": "其他",
}
SENTI_CN = {"bullish": "利好", "bearish": "利空", "neutral": "中性"}
SENTI_CLS = {"bullish": "up", "bearish": "down", "neutral": "flat"}

# 延迟低于此分钟数不值得标注（GitHub cron 偶有数分钟抖动，属正常）
DELAY_THRESHOLD_MIN = 15


def detect_slot(now=None):
    """按北京时间判定运行时段：12:00 前为盘前(am)，之后为盘后(pm)"""
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
    """读取 workflow 注入的延迟信息，判断是否需要在日报里标注。

    定时任务被延迟投递时，"盘前/盘后"标签会与实际内容脱节：12:46 才开跑的
    任务抓的是当天上午的盘中新闻，却仍顶着"盘前"标题。标注后读者至少知道
    自己看的是哪个时点的快照。

    本地运行或手动触发时两个环境变量缺失，返回未标注状态。
    （本函数与 m6_push/push.py 中的同名函数保持一致。）
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
            f"计划 {plan} 生成，实际 {now.strftime('%H:%M')} 才开始运行"
            f"（延迟{_fmt_delay(delay_min)}）。因此本报告采集的是实际运行时刻前 24 小时的"
            f"新闻，并非严格意义的{slot_cn}快照，阅读时请注意每条新闻自身的发布时间。"
        ),
    }


def esc(s):
    """HTML 转义，动态文本拼接前必须调用"""
    if s is None:
        return ""
    return html.escape(str(s), quote=True)


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------
def load_data():
    out = {}
    out["raw"] = json.loads((DATA_DIR / "raw_news.json").read_text(encoding="utf-8"))
    out["structured"] = json.loads((DATA_DIR / "structured_news.json").read_text(encoding="utf-8"))
    out["quotes"] = json.loads((DATA_DIR / "quotes.json").read_text(encoding="utf-8"))
    out["analysis_md"] = (DATA_DIR / "analysis.md").read_text(encoding="utf-8")
    out["picks"] = load_picks()      # M10 可选产出，缺失即空列表
    return out


# ---------------------------------------------------------------------------
# 推送摘要（≤200 字）
# ---------------------------------------------------------------------------
def extract_summary(analysis_md):
    """从 M3 分析里取市场情绪结论句，压缩到 ≤200 字

    先走 extract_conclusion 的三级兜底；仍取不到时保留原有兜底（取第一段正文），
    连正文都没有（空文件/纯标题）则用显式标记收尾 —— **不留「今日市场：」后面
    空一串**：那会让读者以为"今天没有判断"，而实际是解析失败。
    """
    core = extract_conclusion(analysis_md)
    if not core:
        # 兜底：取第一段正文。必须跳过标题行，否则会抓到 "## 一、市场情绪概览"
        for line in analysis_md.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                core = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
                break
    if not core:
        core = "未能解析出结论（见下方市场分析）"
    summary = f"今日市场：{core}"
    if len(summary) > 200:
        summary = summary[:199] + "…"
    return summary


def extract_conclusion(analysis_md):
    """取 M3 的市场情绪结论（三级兜底），取不到返回 ""。

    **同一判据在另外两处各有一份副本，改动必须三处同步、逐字同逻辑**：
      - modules/m6_push/push.py    :: extract_sentiment
      - modules/m9_web/aggregate.py:: Bundle.conclusion
    （本函数被摘要框与索引页复用，故签名固定为「返回 str、取不到返回 ""」，
    调用方自己决定怎么标记「没解析到」。）

    级1  逐行剥掉成对 ** 后匹配 `^结论[：:]` + 正文（M3 有时写成
         `**结论：中性偏谨慎**——…`，加粗标记在行首，直接按行首匹配会漏，
         摘要框会退化成抓正文第一行）；
    级2  首个含「结论/情绪」或带情绪词、且剥 ** 后 >= 6 字的正文行（跳过标题行），
         取「结论/情绪」之后的文本，再砍到最后一个「为/是/：/，」之后、
         含 乐观|中性|谨慎|悲观|积极|偏 的短句；取不到则整行截断 40 字；
    级3  「## 一、市场情绪概览」小节内的第一段正文（跳过标题行），截断 60 字。
    """
    text = analysis_md or ""

    def strip_bold(line):
        return re.sub(r"\*\*(.+?)\*\*", r"\1", line).strip()

    def after_marker(s):
        """取「结论/情绪」之后、最后一个「为/是/：/，」之后的情绪短句"""
        pos = max(s.rfind("结论"), s.rfind("情绪"))
        body = s[pos + 2:] if pos >= 0 else s
        cut = -1
        for ch in ("为", "是", "：", ":", "，", ","):
            cut = max(cut, body.rfind(ch))
        cand = (body[cut + 1:] if cut >= 0 else body).strip(
            " 　*#-—…。；;、!！?？\"'“”‘’()（）[]【】")
        if cand and any(w in cand for w in ("乐观", "中性", "谨慎", "悲观", "积极", "偏")):
            return cand[:40]
        return ""

    # 级 1：行首「结论：…」
    for line in text.splitlines():
        s = strip_bold(line)
        m = re.match(r"^结论[：:]\s*(.+)$", s)
        if m and m.group(1).strip():
            return m.group(1).strip()

    # 级 2：没有独立成行的「结论：」，就从含结论/情绪词的正文行里抠
    for line in text.splitlines():
        s = strip_bold(line)
        if not s or s.startswith("#") or len(s) < 6:
            continue
        if not ("结论" in s or "情绪" in s
                or any(w in s for w in ("乐观", "中性", "谨慎", "悲观", "积极", "偏"))):
            continue
        frag = after_marker(s)
        return frag if frag else s[:40]

    # 级 3：「市场情绪概览」小节的第一段正文
    inside = False
    for line in text.splitlines():
        s = line.strip()
        if not inside:
            if re.match(r"^#{1,6}\s", s) and "市场情绪概览" in s:
                inside = True
            continue
        if re.match(r"^#{1,6}\s", s):
            break
        if not s or re.match(r"^-{3,}$", s):
            continue
        para = re.sub(r"^(?:[-*]|\d+[.、)])\s*", "", strip_bold(s)).strip()
        if para:
            return para[:60]
    return ""


# ---------------------------------------------------------------------------
# Markdown → HTML（针对 M3 分析的固定结构做最小实现）
# ---------------------------------------------------------------------------
def md_to_html(md_text):
    """把 analysis.md 转成 HTML。支持 #/##/###、**加粗**、有序/无序列表、表格、---"""
    lines = md_text.splitlines()
    out = []
    i = 0
    in_table = False
    in_ul = False
    in_ol = False

    def close_lists():
        nonlocal in_ul, in_ol
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_ol:
            out.append("</ol>")
            in_ol = False

    def inline(t):
        t = esc(t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
        t = re.sub(r"\*(.+?)\*", r"<em>\1</em>", t)
        # 引用编号 [12] → 上标弱化（财经媒体风：引用退居正文之后）
        t = re.sub(r"\[(\d+)\]", r'<sup class="cite-n">\1</sup>', t)
        return t

    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()

        # 空行 / 分隔线
        if not stripped:
            close_lists()
            if in_table:
                out.append("</table>")
                in_table = False
            i += 1
            continue
        if re.match(r"^-{3,}$", stripped):
            close_lists()
            if in_table:
                out.append("</table>")
                in_table = False
            out.append("<hr>")
            i += 1
            continue

        # 表格
        if stripped.startswith("|"):
            close_lists()
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if not in_table:
                out.append('<table class="md-table">')
                in_table = True
            # 分隔行 |---|---|
            if all(re.match(r"^:?-{2,}:?$", c) for c in cells):
                i += 1
                continue
            is_header = all(c and (c.startswith("**") or True) for c in cells)
            # 表头判定：下一行是分隔行
            next_is_sep = (i + 1 < len(lines)
                           and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i + 1])
                           and "---" in lines[i + 1])
            tag = "th" if next_is_sep else "td"
            out.append("<tr>")
            for c in cells:
                out.append(f"<{tag}>{inline(c)}</{tag}>")
            out.append("</tr>")
            i += 1
            continue

        # 标题
        m = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if m:
            close_lists()
            if in_table:
                out.append("</table>")
                in_table = False
            level = len(m.group(1))
            out.append(f"<h{level} class='md-h{level}'>{inline(m.group(2))}</h{level}>")
            i += 1
            continue

        # 无序列表
        m = re.match(r"^[-*]\s+(.*)$", stripped)
        if m:
            close_lists()
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{inline(m.group(1))}</li>")
            i += 1
            continue

        # 有序列表
        m = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if m:
            close_lists()
            if not in_ol:
                out.append("<ol>")
                in_ol = True
            out.append(f"<li>{inline(m.group(1))}</li>")
            i += 1
            continue

        # 普通段落
        close_lists()
        if in_table:
            out.append("</table>")
            in_table = False
        out.append(f"<p>{inline(stripped)}</p>")
        i += 1

    close_lists()
    if in_table:
        out.append("</table>")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 各版块渲染
# ---------------------------------------------------------------------------
def render_quotes(quotes):
    """个股行情一览：按涨跌幅绝对值倒序，红涨绿跌"""
    qs = quotes.get("quotes", [])
    if not qs:
        return '<p class="empty">今日无行情数据</p>'

    rows = []
    for q in sorted(qs, key=lambda x: abs(x.get("change_pct") or 0), reverse=True):
        pct = q.get("change_pct")
        pct_str = f"{pct:+.2f}%" if isinstance(pct, (int, float)) else "—"
        cls = "up" if (pct or 0) > 0 else ("down" if (pct or 0) < 0 else "flat")
        price = q.get("price")
        price_str = f"{price:.2f}" if isinstance(price, (int, float)) else "—"
        market = q.get("market") or ""
        rows.append(
            f"<tr>"
            f"<td class='q-name'>{esc(q.get('name'))}"
            f"<span class='q-code'>{esc(q.get('code'))}</span></td>"
            f"<td class='q-mkt'>{esc(market)}</td>"
            f"<td class='q-price'>{price_str}</td>"
            f"<td class='{cls}'>{pct_str}</td>"
            f"</tr>"
        )
    failed = quotes.get("failed", [])
    fail_note = ""
    if failed:
        names = "、".join(esc(f["name"]) for f in failed)
        fail_note = f'<p class="muted small">未取到行情：{names}</p>'

    return (
        '<div class="table-wrap"><table class="quote-table">'
        "<thead><tr><th>个股</th><th>市场</th><th>现价</th><th>涨跌</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>{fail_note}"
    )


# ---------------------------------------------------------------------------
# 来源清单：confirmed 的条目要说清"是谁刊发的"
#
# 背景（2026-10-04）：M2 的 confirmed 判据是「至少两个**独立出版方**刊发过同一
# 事件」，而 M1 的跨源去重已把所有报道过该事件的来源聚合进 `sources`（已排序，
# 至少含自身 source）。所以这里能列出来源清单 —— 只给一个「已确认」徽章，等于
# 把印证强度替读者判断完了；列出是谁报的，印证有多硬由读者自己看。
#
# 规则只有这一份：去重、保序、最多 SOURCES_IN_BADGE 家，超出用「等 N 家」
# （N 是来源**总数**，不是显示条数）。
# ---------------------------------------------------------------------------
SOURCES_IN_BADGE = 4


def news_sources(n):
    """该新闻的全部报道来源：优先 sources，缺字段时退回自身 source。

    去重、保持原顺序（M1 已按名称排好）。旧数据没有 sources 字段，退化成
    只显示自己那一家；两者都取不到时返回 []，由调用方决定不渲染。
    """
    raw = n.get("sources") or []
    if not raw:
        raw = [n.get("source")]
    out = []
    for s in raw:
        s = s.strip() if isinstance(s, str) else ""
        if s and s not in out:
            out.append(s)
    return out


def sources_label(n, max_sources=SOURCES_IN_BADGE):
    """「来源：新浪财经、东方财富」；来源过多时「来源：A、B、C、D 等 6 家」。

    没有可取来源时返回 ""（调用方据此不渲染空标签）。
    """
    srcs = news_sources(n)
    if not srcs:
        return ""
    if max_sources and len(srcs) > max_sources:
        return "来源：" + "、".join(srcs[:max_sources]) + f" 等 {len(srcs)} 家"
    return "来源：" + "、".join(srcs)


def render_news(structured):
    """分板块新闻列表，带验证标记、情绪、板块/个股标签、原文链接"""
    news = structured.get("news", [])
    by_cat = {}
    for n in news:
        cat = n.get("category") or "other"
        by_cat.setdefault(cat, []).append(n)

    order = ["policy", "stock", "industry", "international", "other"]
    sections = []
    for cat in order:
        if cat not in by_cat:
            continue
        items = by_cat[cat]
        cards = []
        for n in items:
            verified = n.get("verified") == "confirmed"
            v_badge = (
                '<span class="badge confirmed">已确认</span>'
                if verified
                else '<span class="badge unverified">待核实</span>'
            )
            # 只有 confirmed 附来源清单：单源条目的来源就是它自己那一家，左边
            # <span class="src"> 已经显示过一次，重复列是画蛇添足（也避免读者
            # 误以为"列出来了 = 有印证"）。来源取不到时整段不渲染。
            src_text = sources_label(n) if verified else ""
            src_html = (
                f'<span class="src-list">· {esc(src_text)}</span>' if src_text else ""
            )
            senti = n.get("sentiment") or "neutral"
            s_badge = (
                f'<span class="badge senti {SENTI_CLS.get(senti, "flat")}">'
                f"{SENTI_CN.get(senti, senti)}</span>"
            )
            boards = "".join(
                f'<span class="tag">{esc(b)}</span>' for b in (n.get("board") or []) if b and b != "其他"
            )
            stocks = "".join(
                f'<span class="tag stock">{esc(s)}</span>' for s in (n.get("stocks") or [])
            )
            url = n.get("url") or ""
            link = f'<a class="src-link" href="{esc(url)}" target="_blank" rel="noopener">原文↗</a>' if url else ""
            time_str = esc((n.get("time") or "")[:16])
            cards.append(
                '<div class="news-card">'
                f'<div class="news-text">{esc(n.get("text"))}</div>'
                '<div class="news-meta">'
                f'<span class="time">{time_str}</span>'
                f'<span class="src">{esc(n.get("source"))}</span>'
                f"{v_badge}{src_html}{s_badge}"
                f'<span class="tags">{boards}{stocks}</span>'
                f"{link}"
                "</div></div>"
            )
        sections.append(
            f'<section class="news-group">'
            f'<h3>{CAT_CN.get(cat, cat)}<span class="count">{len(items)}</span></h3>'
            f"{''.join(cards)}</section>"
        )
    if not sections:
        return '<p class="empty">今日无结构化新闻</p>'
    return "".join(sections)


# ---------------------------------------------------------------------------
# 版块 候选观察清单（M10）
#
# 这一块与其余版块的性质不同：前四块是「今天的判断」，这一块是「过往判断后来
# 怎么样了」。所以跑输的条目和跑赢的用同一套版式、同等字号 —— 没有把失败藏起来
# 的开关，也不给候选打对/错二值标签（跑赢 0.1% 与跑输 0.1% 不该渲染成两档）。
# ---------------------------------------------------------------------------
PICK_HISTORY_DAYS = 14        # 日报里回填记录展示多久
PICK_MIN_SAMPLE = 3           # 样本少于此数不给均值（百分比在小样本上没有意义）

_STATUS_CN = {"no_quote": "未匹配到行情", "no_bench": "基准缺失", "expired": "未取到行情"}


def load_picks():
    """reports/picks/ledger.jsonl → list[dict]。

    M10 是可选产出：账本缺失或含坏行都不能阻塞日报，最多是少一个版块。
    """
    path = REPORTS_DIR / "picks" / "ledger.jsonl"
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _pct(v):
    return "—" if v is None else f"{v:+.2%}"


def _tier_cell(rev, k):
    """一档复盘 → 单元格。未到期显示 —；补记的标注实际跨度（due 与 done 可能差几天）。"""
    if not rev:
        return '<span class="pick-pending">T+%d 未到期</span>' % k
    if rev.get("status") != "ok":
        return f'<span class="pick-pending">{esc(_STATUS_CN.get(rev.get("status"), rev.get("status")))}</span>'
    lag = ""
    if rev.get("done") and rev.get("due") and rev["done"] != rev["due"]:
        lag = f'<span class="pick-lag">{esc(rev["done"][5:])}补</span>'
    return (f'<b>{_pct(rev.get("ret"))}</b>'
            f'<span class="pick-alpha">超额 {_pct(rev.get("alpha"))}</span>{lag}')


def picks_stats(rows):
    """各档平均收益与平均超额 → {k: {"n", "ret", "alpha"}}。

    只统计 status == "ok" 的档位，n 就是参与平均的样本数 —— **n 必须与均值
    一起显示**，本函数因此把 n 一并返回而不是只给均值。
    不产出胜率/命中率这类字段：样本量小时百分比没有意义，且它会把连续的超额
    压成二值的对/错。
    """
    out = {}
    for k in (1, 3, 5):
        vals_r, vals_a = [], []
        for r in rows:
            rev = (r.get("reviews") or {}).get(str(k))
            if not rev or rev.get("status") != "ok":
                continue
            if rev.get("ret") is not None:
                vals_r.append(rev["ret"])
            if rev.get("alpha") is not None:
                vals_a.append(rev["alpha"])
        out[k] = {
            "n": len(vals_r),
            "ret": (sum(vals_r) / len(vals_r)) if vals_r else None,
            "alpha": (sum(vals_a) / len(vals_a)) if vals_a else None,
        }
    return out


def render_picks(picks, date_str):
    """候选观察清单版块。没有任何账本数据时返回空串（不显示空版块）。"""
    if not picks:
        return ""
    today_rows = [r for r in picks if r.get("date") == date_str]
    cutoff = (datetime.strptime(date_str, "%Y-%m-%d")
              - timedelta(days=PICK_HISTORY_DAYS)).strftime("%Y-%m-%d")
    hist = sorted([r for r in picks if r.get("date") != date_str
                   and r.get("date", "") >= cutoff],
                  key=lambda r: (r.get("date", ""), r.get("id", "")), reverse=True)

    # 今日候选卡：逻辑链 + 推翻条件是主体，价格只是脚注
    cards = []
    for r in today_rows:
        tag = '<span class="pick-tag">板块</span>' if r.get("kind") == "board" else ""
        refs = "".join(f'<span class="pick-ref">[{n}]</span>'
                       for n in (r.get("basis_refs") or [])[:8])
        cards.append(f"""<div class="pick-card">
  <div class="pick-head"><b>{esc(r.get("name"))}</b>{tag}
    <span class="pick-conf">置信度 {esc(r.get("confidence") or "—")}</span></div>
  <p class="pick-logic">{esc(r.get("logic"))}</p>
  <p class="pick-inval"><b>推翻条件</b>：{esc(r.get("invalidation"))}</p>
  <div class="pick-foot">基准 {esc(r.get("base_price") or "未匹配到行情")}
    {("· 所属板块 " + esc(r["board"])) if r.get("board") and r.get("kind") == "stock" else ""}
    {("· 新闻依据 " + refs) if refs else ""}</div>
</div>""")

    if today_rows:
        today_html = "".join(cards)
    else:
        today_html = ('<p class="pick-empty">本时段不记录新候选（新候选只在收盘后记录，'
                      '以保证基准价就是当日收盘价）。以下是跟踪中候选的表现。</p>')

    # 历史回填：逐条明细，每条自带 T+1/T+3/T+5 与同期基准
    rows_html = []
    for r in hist:
        tag = '<span class="pick-tag">板块</span>' if r.get("kind") == "board" else ""
        tiers = "".join(f'<div class="pick-tier"><span class="pick-tier-k">T+{k}</span>'
                        f'{_tier_cell((r.get("reviews") or {}).get(str(k)), k)}</div>'
                        for k in (1, 3, 5))
        rows_html.append(f"""<div class="pick-row">
  <div class="pick-row-head">{esc(r.get("date"))} · <b>{esc(r.get("name"))}</b>{tag}
    <span class="pick-base">基准 {esc(r.get("base_price") if r.get("base_price") else "—")}</span></div>
  <div class="pick-tiers">{tiers}</div>
</div>""")

    # 汇总行：均值与样本数**必须同时出现**
    st = picks_stats(hist)
    parts = []
    for k in (1, 3, 5):
        d = st[k]
        if d["n"] == 0:
            continue
        if d["n"] < PICK_MIN_SAMPLE:
            parts.append(f"T+{k} 样本 {d['n']} 条（少于 {PICK_MIN_SAMPLE} 条不给均值）")
        else:
            parts.append(f"T+{k} 均超额 {_pct(d['alpha'])}（样本 {d['n']} 条）")
    stats_html = ("<p class=\"pick-stats\">" + "　".join(parts) + "</p>") if parts else ""

    hist_html = ""
    if rows_html:
        hist_html = f"""<details class="fold">
  <summary>
    <h3>往期候选回填<span class="h2-count">{len(rows_html)} 条</span></h3>
    <span class="fold-hint"><span class="fold-label"></span><span class="fold-arrow"></span></span>
  </summary>
  {''.join(rows_html)}
  <p class="pick-note">收益率为<b>未复权</b>口径；区间内若发生除权除息，该档会被低估。
  「补」表示实际打分日与到期日不同（周末/停牌/限流所致）。</p>
</details>"""

    n_today = len(today_rows)
    return f"""<section class="block" id="picks">
  <h2>候选观察清单<span class="h2-count">今日 {n_today} 条</span></h2>
  <p class="pick-note">每条候选都带<b>推翻条件</b>与新闻依据，其后续表现按 T+1/T+3/T+5
  原样回填，<b>含跑输的</b>。超额相对沪深300。<b>不是买入指令</b>，决策权归您本人。</p>
  {today_html}
  {stats_html}
  {hist_html}
</section>"""


# ---------------------------------------------------------------------------
# 页面骨架
# ---------------------------------------------------------------------------
CSS = """
:root{
  /* 财经媒体风 · 浅色纸面主调：暖白底 / 墨色字 / 细线分割，
     红绿只上数字，强调色用墨蓝（红绿已被涨跌独占） */
  --bg:#f7f4ee; --bg2:#efeae1; --card:#ffffff; --border:#ddd6c9;
  --ink:#1f1d1a; --text:#2b2822; --muted:#7a746a;
  --accent:#1e4d7a; --warn:#9a6b12;
  --up:#c4362b; --down:#12734f; --flat:#7a746a;
  --confirmed:#1e4d7a; --unverified:#9a6b12;
  --serif:Georgia,"Times New Roman","Songti SC","SimSun","Noto Serif CJK SC",
    "Source Han Serif SC",serif;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
    "Hiragino Sans GB","Microsoft YaHei","Noto Sans CJK SC",sans-serif;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#15161a; --bg2:#1c1d22; --card:#1c1d22; --border:#2e2f36;
    --ink:#f0ede6; --text:#ddd9d1; --muted:#97918a;
    --accent:#7fb0e0; --warn:#e0a94e;
    --up:#ff6b5e; --down:#3fbd8a; --flat:#97918a;
    --confirmed:#7fb0e0; --unverified:#e0a94e;
  }
}
*{box-sizing:border-box; margin:0; padding:0;}
html{-webkit-text-size-adjust:100%;}
body{
  background:var(--bg); color:var(--text);
  font-family:var(--sans);
  line-height:1.78; font-size:16px; padding:0 0 48px;
  max-width:760px; margin:0 auto;
}
a{color:var(--accent); text-decoration:none; word-break:break-all;}
/* 报头：报纸双线（粗上细下），无色块无 emoji */
header.hero{
  padding:0 16px 8px; background:var(--bg);
  border-top:4px solid var(--ink); border-bottom:1px solid var(--ink);
}
header.hero .kicker{
  font:12px/1.6 var(--sans); letter-spacing:.18em; color:var(--muted);
  padding-top:14px;
}
header.hero h1{
  font-family:var(--serif); font-size:26px; font-weight:700;
  letter-spacing:.02em; line-height:1.25;
}
header.hero .date{
  color:var(--muted); font-size:12.5px; letter-spacing:.06em;
  margin-top:8px; padding-top:6px; border-top:1px solid var(--border);
}
@media (min-width:600px){
  header.hero h1{font-size:32px;}
}
.summary-box{
  margin:16px 16px 0; padding:14px 16px; background:var(--bg2);
  border-top:1px solid var(--border); border-bottom:1px solid var(--border);
  font-size:16px;
}
main{padding:0 12px;}
section.block{margin:24px 8px;}
section.block > h2,
section.block > details > summary > h2{
  font-family:var(--serif); font-size:18px; font-weight:700;
  margin-bottom:12px; padding-bottom:8px;
  border-bottom:1px solid var(--border);
  counter-increment:sec;
}
/* 版块序号：纯 CSS 计数（01 02 03），不动模板结构 */
section.block > h2::before,
section.block > details > summary > h2::before{
  content:counter(sec,decimal-leading-zero) " ";
  font:400 12px/1 var(--sans); color:var(--muted);
  letter-spacing:.1em; margin-right:8px; vertical-align:2px;
}
@media (min-width:600px){
  section.block > h2,
  section.block > details > summary > h2{font-size:20px;}
}
/* 分板块新闻默认折叠：它占全文八成篇幅（实测 09-24 盘后 24888/31592 字），
   展开着会把真正有判断价值的前四块内容淹在几十屏新闻下面 */
section.block > details > summary{
  cursor:pointer; list-style:none; display:flex; align-items:center;
  justify-content:space-between; gap:10px; padding:4px 0;
  user-select:none; -webkit-tap-highlight-color:transparent;
}
section.block > details > summary::-webkit-details-marker{display:none;}
section.block > details > summary::marker{content:"";}
section.block > details > summary > h2{margin-bottom:0;}
section.block > details > summary:focus-visible{outline:2px solid var(--accent); outline-offset:2px;}
.fold-hint{font-size:13px; color:var(--muted); white-space:nowrap;}
.fold-label::after{content:"展开";}
details[open] > summary .fold-label::after{content:"收起";}
/* 折叠箭头：CSS 三角，替代 ▸ 字符 */
.fold-arrow{
  display:inline-block; width:0; height:0; margin-left:6px;
  border-left:5px solid currentColor; border-top:4px solid transparent;
  border-bottom:4px solid transparent; transition:transform .15s ease;
}
details[open] > summary .fold-arrow{transform:rotate(90deg);}
.news-group{margin-bottom:18px;}
.news-group h3{
  font-family:var(--serif); font-size:15.5px; color:var(--ink); font-weight:700;
  margin:14px 0 8px; display:flex; align-items:center; gap:8px;
}
.news-group h3 .count{
  background:none; color:var(--muted); font:400 12px var(--sans);
  padding:0; border:0;
}
/* 新闻条目流：细线分隔，非卡片 */
.news-card{
  background:transparent; border:0; border-bottom:1px solid var(--border);
  padding:14px 0; margin-bottom:0;
}
.news-card:last-of-type{border-bottom:0;}
.news-text{font-size:15px; word-break:break-word;}
.news-meta{
  display:flex; flex-wrap:wrap; align-items:center; gap:6px;
  margin-top:8px; font-size:13px; color:var(--muted);
}
.badge{
  display:inline-block; font-size:12px; padding:1px 7px;
  border-radius:2px; line-height:1.5; white-space:nowrap;
}
.badge.confirmed{background:none; color:var(--confirmed); border:1px solid var(--confirmed);}
.badge.unverified{background:none; color:var(--unverified); border:1px solid var(--unverified);}
/* confirmed 徽章后的来源清单：沿用页面 token（--muted），不引入新颜色。
   .news-meta 已是 flex-wrap，手机上整段会折到下一行，不会撑破版心。 */
.src-list{font-size:12px; color:var(--muted); line-height:1.5; min-width:0; overflow-wrap:anywhere;}
.badge.senti{border:1px solid var(--border);}
.badge.senti.up{color:var(--up); border-color:var(--up); background:none;}
.badge.senti.down{color:var(--down); border-color:var(--down); background:none;}
.badge.senti.flat{color:var(--flat);}
.tag{
  font-size:12px; padding:1px 6px; border-radius:2px;
  background:var(--bg2); border:0; color:var(--muted);
}
.tag.stock{color:var(--accent); border:1px solid var(--accent); background:none;}
.src-link{font-size:13px;}
.table-wrap{overflow-x:auto; -webkit-overflow-scrolling:touch; border-radius:0; border:1px solid var(--border);}
table.quote-table{width:100%; border-collapse:collapse; background:var(--bg); font-size:14px; min-width:420px; font-variant-numeric:tabular-nums;}
table.quote-table th, table.quote-table td{
  padding:10px 12px; text-align:left; border-bottom:1px solid var(--border);
  white-space:nowrap;
}
table.quote-table thead th{
  background:var(--bg2); color:var(--muted); font-weight:600; font-size:12px;
  letter-spacing:.06em;
}
.q-name{font-weight:600;}
.q-code{display:block; font-size:12px; color:var(--muted); font-weight:400;}
.q-mkt{color:var(--muted); font-size:13px;}
.up{color:var(--up);} .down{color:var(--down);} .flat{color:var(--flat);}
/* 市场分析正文：去卡片化，靠排版层次立结构 */
.analysis{
  background:transparent; border:0;
  padding:0; font-size:15px;
}
.analysis h1,.analysis h2,.analysis h3{margin:16px 0 8px; line-height:1.4; font-family:var(--serif);}
.analysis h1{font-size:20px;} .analysis h2{font-size:18px;} .analysis h3{font-size:16px;}
.analysis p{margin:8px 0;}
.analysis ul,.analysis ol{margin:8px 0 8px 22px;}
.analysis li{margin:4px 0;}
.analysis hr{border:none; border-top:1px solid var(--border); margin:16px 0;}
.analysis .md-table{border-collapse:collapse; margin:12px 0; font-size:14px; display:block; overflow-x:auto; max-width:100%;}
.analysis .md-table th,.analysis .md-table td{
  border:1px solid var(--border); padding:8px 10px; text-align:left; vertical-align:top;
}
.analysis .md-table th{background:var(--bg2); white-space:nowrap;}
.analysis strong{color:var(--ink);}
/* 行内引用编号 [n] → 上标弱化；连排时补逗号避免数字粘连误读 */
.cite-n{font-size:.72em; color:var(--muted); letter-spacing:.02em;
  vertical-align:super; line-height:1;}
.cite-n + .cite-n::before{content:",";}
/* 风险声明：双线规线页脚 */
footer.risk{
  margin:28px 8px 8px; padding:16px 0 8px; background:transparent;
  border:0; border-top:3px double var(--ink); border-radius:0;
  font-size:13px; color:var(--muted); line-height:1.6;
}
footer.risk h3{font-family:var(--serif); font-size:14px; color:var(--ink); margin-bottom:6px;}
.muted{color:var(--muted);} .small{font-size:13px;} .empty{color:var(--muted); padding:12px 0;}
.index-item{
  display:block; background:transparent; border:1px solid var(--border);
  border-radius:0; padding:14px 16px; margin-bottom:10px; color:var(--text);
}
.index-item .d{font-weight:600; font-family:var(--serif); font-size:16px;}
.index-item .s{color:var(--muted); font-size:13px;}
.latest-item{border-top:1px solid var(--ink); border-bottom:1px solid var(--ink); margin-bottom:18px;}
/* 索引页底部：网页版门户入口 */
.web-entry{
  display:block; color:var(--text); text-decoration:none;
  border:1px solid var(--border); border-left:3px solid var(--accent);
  border-radius:0; padding:14px 16px; margin-top:22px;
}
.web-entry .d{font-weight:600; font-family:var(--serif); font-size:16px; color:var(--ink);}
.web-entry .s{color:var(--muted); font-size:13px; margin-top:3px;}
.web-entry .go{color:var(--accent); font-size:13px; margin-top:8px;}
.web-entry:hover .d{color:var(--accent);}

/* 时段徽章：盘前 / 盘后 */
.slot-badge{
  display:inline-block; font-size:12px; padding:1px 7px; border-radius:2px;
  margin-right:6px; vertical-align:1px; white-space:nowrap;
}
.slot-badge.am{background:none; color:var(--warn); border:1px solid var(--warn);}
.slot-badge.pm{background:none; color:var(--accent); border:1px solid var(--accent);}

/* 定时延迟提示条 */
.delay-banner{
  margin:12px 16px 0; padding:10px 14px; border-radius:0;
  background:var(--bg2); border:0; border-left:3px solid var(--warn);
  font-size:13px; line-height:1.6; color:var(--text);
}
.delay-banner b{color:var(--warn);}

/* 顶部版块跳转：竖线分隔的文字索引（报刊目录式），非胶囊 */
nav.toc{
  display:flex; gap:0; padding:10px 16px; background:var(--bg);
  border-bottom:1px solid var(--border); overflow-x:auto;
  -webkit-overflow-scrolling:touch;
}
nav.toc a{
  font-size:13px; padding:4px 10px; white-space:nowrap;
  background:none; border:0; color:var(--text);
}
nav.toc a:first-child{padding-left:0;}
nav.toc a + a{border-left:1px solid var(--border);}
nav.toc a:hover{color:var(--accent);}
section.block{scroll-margin-top:8px;}
section.block > h2{display:flex; align-items:center; gap:8px;}
.h2-count{
  font-size:12.5px; font-weight:400; color:var(--muted);
  background:none; border:0; padding:0;
}

/* 候选观察清单（M10）。折叠的 h3 需要自己一份标题样式 —— 上面那条
   `section.block > details > summary > h2` 只管 h2，h3 拿不到。 */
section.block > details > summary > h3{
  font-family:var(--serif); font-size:16px; font-weight:700; margin-bottom:0;
  padding-bottom:6px; border-bottom:1px solid var(--border);
  display:flex; align-items:center; gap:8px;
}
.pick-note{font-size:12.5px; color:var(--muted); line-height:1.6; margin:0 0 12px;}
.pick-empty{font-size:13.5px; color:var(--muted); background:var(--bg2);
  border:0; border-radius:0; padding:12px 14px;}
.pick-card{background:transparent; border:0; border-bottom:1px solid var(--border);
  border-radius:0; padding:14px 0; margin-bottom:0;}
.pick-card:last-of-type{border-bottom:0;}
.pick-head{display:flex; align-items:center; gap:8px; flex-wrap:wrap; font-size:15px;}
.pick-conf{font-size:11.5px; color:var(--muted); border:1px solid var(--border);
  border-radius:2px; padding:1px 7px;}
.pick-tag{font-size:11px; color:var(--accent); border:1px solid var(--accent);
  border-radius:2px; padding:1px 6px;}
.pick-logic{font-size:14px; line-height:1.65; margin:8px 0 6px;}
.pick-inval{font-size:13px; line-height:1.6; margin:0; color:var(--muted);}
.pick-inval b{color:var(--text);}
.pick-foot{font-size:12px; color:var(--muted); margin-top:8px;
  display:flex; flex-wrap:wrap; gap:6px; align-items:center;}
.pick-ref{background:var(--bg2); border:0;
  border-radius:2px; padding:0 5px;}
/* 回填明细：逐条列出，跑输的与跑赢的同版式同字号 */
.pick-row{background:transparent; border:0; border-bottom:1px solid var(--border);
  border-radius:0; padding:12px 0; margin-bottom:0;}
.pick-row:last-of-type{border-bottom:0;}
.pick-row-head{font-size:13px; display:flex; flex-wrap:wrap; gap:6px; align-items:center;}
.pick-base{font-size:12px; color:var(--muted);}
.pick-tiers{display:flex; flex-wrap:wrap; gap:8px; margin-top:8px;}
.pick-tier{flex:1 1 84px; background:var(--bg2); border:1px solid var(--border);
  border-radius:0; padding:6px 8px; font-size:13px; display:flex;
  flex-direction:column; gap:2px; font-variant-numeric:tabular-nums;}
.pick-tier-k{font-size:11px; color:var(--muted);}
.pick-alpha{font-size:11.5px; color:var(--muted);}
.pick-pending{font-size:12px; color:var(--muted);}
.pick-lag{font-size:10.5px; color:var(--muted); border:1px solid var(--border);
  border-radius:2px; padding:0 5px; margin-left:4px;}
.pick-stats{font-size:13px; background:var(--bg2); border:0;
  border-radius:0; padding:8px 12px; margin:0 0 10px; line-height:1.7;}

/* 索引页：按日期分组 */
.day-group{
  background:transparent; border:0; border-bottom:1px solid var(--border);
  border-radius:0; padding:14px 0; margin-bottom:0;
}
.day-group:last-of-type{border-bottom:0;}
.day-date{font-family:var(--serif); font-weight:700; font-size:17px; margin-bottom:8px;}
.day-links{display:flex; flex-wrap:wrap; gap:8px;}
.slot-link{
  display:flex; align-items:center; gap:8px; flex:1 1 120px;
  padding:8px 12px; border-radius:0; background:var(--bg2);
  border:1px solid var(--border); color:var(--text); font-size:14px;
}
.slot-link.am{border-left:3px solid var(--unverified);}
.slot-link.pm{border-left:3px solid var(--accent);}
.slot-link.na{border-left:3px solid var(--border);}
.slot-link.na .slot-name{color:var(--muted);}
.slot-link .slot-go{margin-left:auto; color:var(--muted); font-size:12px;}
.day-latest{margin-top:8px; font-size:13px; text-align:right;}
"""


def build_page(data, date_str, slot, delay=None):
    """版块顺序：摘要 → 市场分析 → 候选观察清单 → 个股行情 → 分板块新闻。
    市场分析（M3 全文）置于新闻列表之前，先给判断再给素材；候选清单紧跟在
    市场分析之后，因为它是**由那份分析派生出来的、可事后检验的一层**，
    而个股行情与新闻是参考素材。

    delay 为 delay_context() 的结果；late=True 时在标题与页顶标出延迟，
    避免读者把被延迟的定时任务误当成真正的盘前/盘后快照。
    """
    summary = extract_summary(data["analysis_md"])
    analysis_html = md_to_html(data["analysis_md"])
    quotes_html = render_quotes(data["quotes"])
    news_html = render_news(data["structured"])
    slot_cn = SLOT_CN.get(slot, "")
    news_count = len(data["structured"].get("news", []))
    quote_count = data["quotes"].get("count", 0)

    # M10 是可选产出：账本不存在（首次运行）就整个版块不出现，导航也不加锚点
    picks_html = render_picks(data.get("picks") or [], date_str)
    picks_nav = '<a href="#picks">候选清单</a>\n  ' if picks_html else ""

    delay = delay or {"late": False}
    title_suffix = f"（{delay['short']}）" if delay.get("late") else ""
    delay_banner = (
        f'<div class="delay-banner"><b>提示 · 定时任务延迟</b><br>{esc(delay["text"])}</div>'
        if delay.get("late") else ""
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>股市情报日报 · {esc(date_str)} {esc(slot_cn)}{esc(title_suffix)}</title>
<style>{CSS}</style>
</head>
<body>
<header class="hero">
  <div class="kicker">STOCK NEWS DAILY</div>
  <h1>股市情报日报</h1>
  <div class="date">
    <span class="slot-badge {esc(slot)}">{esc(slot_cn)}</span>
    {esc(date_str)} · AI 自动生成 · 仅供参考
  </div>
</header>

<nav class="toc">
  <a href="#market">市场分析</a>
  {picks_nav}<a href="#quotes">个股行情</a>
  <a href="#news">分板块新闻</a>
</nav>

{delay_banner}

<div class="summary-box">{esc(summary)}</div>

<main>
  <section class="block" id="market">
    <h2>市场分析</h2>
    <div class="analysis">{analysis_html}</div>
  </section>

  {picks_html}
  <section class="block" id="quotes">
    <h2>个股行情一览<span class="h2-count">{quote_count} 只</span></h2>
    {quotes_html}
  </section>

  <section class="block" id="news">
    <details class="fold">
      <summary>
        <h2>分板块新闻<span class="h2-count">{news_count} 条</span></h2>
        <span class="fold-hint"><span class="fold-label"></span><span class="fold-arrow"></span></span>
      </summary>
      {news_html}
    </details>
  </section>
</main>

<footer class="risk">
  <h3>风险声明</h3>
  <p>本报告由 AI 自动生成，仅供参考，不构成任何投资建议。新闻解读可能存在偏差，
  市场有风险，投资需谨慎，请独立判断。所有 AI 结论均基于文中列出的新闻原文，
  未经过人工核实，单一来源信息标注"待核实"。决策权归您本人。</p>
</footer>
</body>
</html>"""


def _parse_report_name(stem):
    """从文件名解析 (日期, 时段)。非日报文件（如 latest / index）返回 None"""
    m = re.match(r"^(\d{4}-\d{2}-\d{2})(?:-(am|pm))?$", stem)
    if not m:
        return None
    return m.group(1), (m.group(2) or "")


def build_index(report_files):
    """索引页：按日期倒序分组，每天列出盘前/盘后各一份"""
    by_date = {}
    for f in report_files:
        parsed = _parse_report_name(f.stem)
        if not parsed:
            continue
        date_str, slot = parsed
        by_date.setdefault(date_str, []).append((slot, f))

    blocks = []
    for date_str in sorted(by_date, reverse=True):
        entries = sorted(by_date[date_str], key=lambda x: x[0])
        links = []
        for slot, f in entries:
            # 没有时段后缀的是"加时段命名"之前那次运行留下的文件（只会出现在
            # 2026-09-16 当天），标成"早期"以免与"盘前/盘后"并列时让人困惑
            label = SLOT_CN.get(slot, "早期")
            cls = slot or "na"
            links.append(
                f'<a class="slot-link {esc(cls)}" href="{esc(f.name)}"'
                f' title="{esc(f.name)}">'
                f'<span class="slot-name">{esc(label)}</span>'
                f'<span class="slot-go">查看 →</span></a>'
            )
        # 同日两份时，右侧再给一个"最新"直达
        latest = entries[-1][1]
        blocks.append(
            f'<div class="day-group">'
            f'<div class="day-date">{esc(date_str)}</div>'
            f'<div class="day-links">{"".join(links)}</div>'
            f'<div class="day-latest"><a href="{esc(latest.name)}">当日最新</a></div>'
            f"</div>"
        )

    body = "".join(blocks) if blocks else '<p class="empty">暂无日报</p>'
    has_latest = (REPORTS_DIR / "latest.html").exists()
    latest_link = (
        '<a class="index-item latest-item" href="latest.html">'
        '<div class="d">最新一期</div>'
        '<div class="s">直接打开最近一次运行生成的日报</div></a>'
        if has_latest else ""
    )
    # 页面底部给网页版留一个入口：索引页只列日报，交互查询（搜索/筛选/持仓/候选）
    # 都在 M9 网页版里，放个直达链接省得记地址。相对路径在线上（/ → /web/）
    # 与本地双击打开（reports/ → reports/web/）都成立。
    web_entry = (
        '<a class="web-entry" href="web/">'
        '<div class="d">网页版门户</div>'
        '<div class="s">交互式查阅：新闻搜索与筛选 · 个股行情 · 板块 · 深度分析 · 候选清单 · 持仓盈亏</div>'
        '<div class="go">进入网页版 →</div>'
        "</a>"
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>股市情报日报 · 索引</title>
<style>{CSS}</style>
</head>
<body>
<header class="hero">
  <div class="kicker">STOCK NEWS DAILY</div>
  <h1>股市情报日报</h1>
  <div class="date">全部日报 · 按日期倒序 · 每天盘前/盘后各一份</div>
</header>
<main>
  <section class="block">
    {latest_link}
    {body}
    {web_entry}
  </section>
</main>
<footer class="risk">
  <p>本页由 AI 自动生成，仅供参考，不构成任何投资建议。市场有风险，投资需谨慎。</p>
</footer>
</body>
</html>"""


def main():
    # 时段优先级：命令行 --slot > 环境变量 REPORT_SLOT > 按北京时间自动判定
    slot = ""
    if "--slot" in sys.argv:
        i = sys.argv.index("--slot")
        if i + 1 < len(sys.argv):
            slot = sys.argv[i + 1].strip().lower()
    if slot not in ("am", "pm"):
        slot = (os.environ.get("REPORT_SLOT") or "").strip().lower()
    if slot not in ("am", "pm"):
        slot = detect_slot()
    # 让 delay_context 拿到最终时段（命令行 --slot 可能覆盖了环境变量）
    os.environ["REPORT_SLOT"] = slot

    now = datetime.now(CST)
    date_str = now.strftime("%Y-%m-%d")
    data = load_data()
    delay = delay_context(now)

    REPORTS_DIR.mkdir(exist_ok=True)

    page = build_page(data, date_str, slot, delay)
    page_path = REPORTS_DIR / f"{date_str}-{slot}.html"
    page_path.write_text(page, encoding="utf-8")
    print(f"[OK] report -> {page_path} ({len(page)} bytes, {SLOT_CN[slot]})")
    if delay.get("late"):
        print(f"[warn] 定时任务延迟 {delay['short']}：计划 {delay['plan']}，"
              f"实际 {delay['actual']} 开始，已在日报中标注")

    # latest.html：固定入口，内容 = 最近一次运行产出（同日盘后版会覆盖盘前版）
    latest_path = REPORTS_DIR / "latest.html"
    latest_path.write_text(page, encoding="utf-8")
    print(f"[OK] latest -> {latest_path}")

    # 更新索引（latest.html 与 index.html 自身不计入日报列表）
    report_files = sorted(
        [p for p in REPORTS_DIR.glob("*.html")
         if p.name not in ("index.html", "latest.html")],
        key=lambda p: p.stem, reverse=True,
    )
    idx = build_index(report_files)
    idx_path = REPORTS_DIR / "index.html"
    idx_path.write_text(idx, encoding="utf-8")
    print(f"[OK] index  -> {idx_path} ({len(report_files)} reports)")

    # 摘要统计
    print(f"    news={len(data['structured']['news'])} quotes={data['quotes']['count']} "
          f"summary_len={len(extract_summary(data['analysis_md']))}")


if __name__ == "__main__":
    main()
