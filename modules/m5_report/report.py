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
def _is_a_share_market(market):
    """判断 M4 的 market 标签是不是沪深 A 股（可直接在 A 股账户交易）。

    实测 M4 的取值有 `沪A` / `深A` / `科创板` / `创业板` / `美股` / `港股` 等；
    这里刻意用"排除法 + 白名单"双重判据，避免新增标签被误判成 A 股。
    """
    m = market or ""
    if not m or "港" in m or "美" in m or "B股" in m:
        return False
    return ("A" in m) or ("科创" in m) or ("创业" in m) or ("北交" in m)


def _quote_rows(qs):
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
    return "".join(rows)


_QUOTE_THEAD = ("<thead><tr><th>个股</th><th>市场</th><th>现价</th>"
                "<th>涨跌</th></tr></thead>")


def render_quotes(quotes):
    """个股行情一览：**沪深 A 股放主表，其它市场单独一栏并标注**。

    为什么分开（2026-10-04）：新闻里提到的标的有相当比例是美股/港股 ——
    实测 10-04 那天的 6 只行情里 **5 只是美股**。这份日报是给做 A 股的人看的，
    混在一张表里既容易误读成"今天只有这些票"，也会把真正的 A 股线索淹掉。
    分开后主表就是"今天新闻里出现的、你能买的票"，海外行情降级为参考。
    """
    qs = quotes.get("quotes", [])
    if not qs:
        return '<p class="empty">今日无行情数据</p>'

    a_share = [q for q in qs if _is_a_share_market(q.get("market"))]
    others = [q for q in qs if not _is_a_share_market(q.get("market"))]

    parts = []
    # ① 主表：沪深 A 股
    if a_share:
        parts.append(
            '<div class="table-wrap"><table class="quote-table">'
            f"{_QUOTE_THEAD}<tbody>{_quote_rows(a_share)}</tbody></table></div>"
        )
    else:
        parts.append('<p class="empty">今日新闻涉及的个股中没有沪深 A 股'
                     '（提到的都是海外/港股标的，见下）。</p>')

    # ② 其它市场：单独一栏 + 明确说明不能在本账户交易
    if others:
        parts.append(
            '<p class="q-sub-title">其它市场（'
            f'{len(others)} 只）'
            '<span class="q-sub-note">非沪深 A 股，一般不能在 A 股账户交易，'
            '仅作外围参考</span></p>'
        )
        parts.append(
            '<div class="table-wrap"><table class="quote-table muted-table">'
            f"{_QUOTE_THEAD}<tbody>{_quote_rows(others)}</tbody></table></div>"
        )

    failed = quotes.get("failed", [])
    if failed:
        names = "、".join(esc(f["name"]) for f in failed)
        parts.append(f'<p class="muted small">未取到行情：{names}</p>')
    return "".join(parts)


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


# ---------------------------------------------------------------------------
# 跨天事件追踪（M2 的 continuing 字段）与「报道家数」
#
# continuing 是 M2 的**跨天事件追踪**结果，新事件**没有这个键**（不是 null）：
#   {"days": 3, "first_date": "2026-10-08", "prev_date": "2026-10-09",
#    "families": ["新浪财经"], "confidence": "high"}
# 两条铁律（冻结契约）：
#   ① confidence 只有 high / low。**low 绝不许写成「持续关注第 N 天」** ——
#      那会让人以为"同一事件"已经确认；low 只能写「疑似同一事件」。
#   ② `families` 是**历史记录**的来源家族，**不含今天这条自己的来源**。要写
#      「N 家媒体」必须自己把今天的 sources 并进去，否则系统性少数一家。
# ---------------------------------------------------------------------------
CONT_FIRST_PREFIX = "首次 "


def _mmdd(s):
    """2026-10-08 / 2026-10-08 10:00:00 → "10-08"；取不到返回 ""（不编日期）。"""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(s or ""))
    return f"{m.group(2)}-{m.group(3)}" if m else ""


def continuing_info(n):
    """M2 的 continuing → {"days", "confidence", "first_date", "first_mmdd"}；无则 None。

    `days` 取不到（缺键/写坏/非数字/小于 1）就返回 None：宁可**什么都不显示**，
    也不能编一个"N 天"出来（空标签比没标签更糟）。confidence 不是 high/low 时
    同样按"没有"处理。
    """
    c = n.get("continuing")
    if not isinstance(c, dict):
        return None
    try:
        days = int(c.get("days"))
    except (TypeError, ValueError):
        return None
    if days < 1:
        return None
    conf = str(c.get("confidence") or "").strip().lower()
    if conf not in ("high", "low"):
        return None
    return {"days": days, "confidence": conf,
            "first_date": str(c.get("first_date") or ""),
            "first_mmdd": _mmdd(c.get("first_date"))}


def continuing_badge(n):
    """新闻卡片上的跨天标记（HTML）；没有该字段时不返回任何东西。

    high → 「持续关注 · 第 N 天」+ 首次 MM-DD（确认了是同一事件，才敢说"持续"）
    low  → 「疑似同一事件（第 N 天）」，样式更弱
    """
    info = continuing_info(n)
    if not info:
        return ""
    if info["confidence"] == "high":
        first = (f'<span class="src-list">{esc(CONT_FIRST_PREFIX)}'
                 f'{esc(info["first_mmdd"])}</span>') if info["first_mmdd"] else ""
        return (f'<span class="badge continuing">持续关注 · 第 {info["days"]} 天</span>'
                f"{first}")
    return (f'<span class="badge continuing weak">疑似同一事件'
            f'（第 {info["days"]} 天）</span>')


def news_families(n):
    """该新闻的**全部报道来源家族** = 今天的 sources ∪ 历史的 continuing.families。

    families 只有历史那几天的记录（不含今天这条自己的来源），所以要报「N 家
    媒体」必须把 news_sources(n) 并进去。去重、保持原顺序（今天的在前）。
    """
    out = list(news_sources(n))
    c = n.get("continuing")
    if isinstance(c, dict):
        for f in (c.get("families") or []):
            f = f.strip() if isinstance(f, str) else ""
            if f and f not in out:
                out.append(f)
    return out


def news_family_count(n):
    """报道家数 = 独立来源家族数（今天的 sources + 历史 families）。"""
    return len(news_families(n))


def sources_label(n, max_sources=SOURCES_IN_BADGE):
    """「来源：新浪财经、东方财富」；来源过多时「来源：A、B、C、D 等 6 家」。

    没有可取来源时返回 ""（调用方据此不渲染空标签）。
    """
    srcs = news_families(n)
    if not srcs:
        return ""
    if max_sources and len(srcs) > max_sources:
        return "来源：" + "、".join(srcs[:max_sources]) + f" 等 {len(srcs)} 家"
    return "来源：" + "、".join(srcs)


def family_sources_label(n, max_sources=SOURCES_IN_BADGE):
    """「2 家媒体 · 来源：新浪财经、东方财富」；**单源不加家数**（没信息量），
    没有可取来源时返回 ""（调用方据此不渲染空标签）。"""
    lab = sources_label(n, max_sources)
    if not lab:
        return ""
    n_fam = news_family_count(n)
    if n_fam < 2:
        return lab
    return f"{n_fam} 家媒体 · {lab}"


def render_news(structured):
    """分板块新闻列表，带验证标记、情绪、板块/个股标签、原文链接。

    **板块内排序**（2026-10-05，只动展示顺序）：按「报道家数 desc → 时间 desc」。
    报道家数 = 该条 sources 的独立来源家族数（与「已确认」判据同源，见
    news_families）。**不碰 M3 的 digest 顺序** —— 正文里的 [n] 引用编号是另一
    套契约，改展示顺序不影响它（日报里每条新闻没有编号锚点）。
    """
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
        # 同一板块内：报道家数多的在前，家数相同按时间倒序（最新在前）。
        # time 缺失/为 None 时按空串处理 —— 否则与字符串比较会抛 TypeError。
        items = sorted(by_cat[cat],
                       key=lambda x: (news_family_count(x), str(x.get("time") or "")),
                       reverse=True)
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
            # 「N 家媒体」的家数把 M2 continuing.families 并进来了（单源不写家数）。
            src_text = family_sources_label(n) if verified else ""
            src_html = (
                f'<span class="src-list">· {esc(src_text)}</span>' if src_text else ""
            )
            # 跨天标记：新事件没有 continuing 键 → continuing_badge 返回 ""，
            # 卡片上不会多出一个空标签
            cont_badge = continuing_badge(n)
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
                f"{v_badge}{cont_badge}{src_html}{s_badge}"
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

# 两个基准并列（2026-10-05）：候选天然偏中小盘 + 事件驱动（M10 的 prompt 还要求
# "同等依据优先单价更低"的标的），只用沪深300 当基准会**系统性高估**这套判断的水平
# —— 小盘股整体跑赢的阶段，alpha 为正可能只是风格红利。所以同时给中证1000 口径。
# 名字与 M10（picks.BENCH_NAME / BENCH2_NAME）、M6 推送、M9 网页保持同一口径；
# 那三个模块各自独立运行，不为一句文案互相 import（与 conclusion 的三份副本同理）。
PICK_BENCH_CN = "沪深300"
PICK_BENCH2_CN = "中证1000"
# 为什么同时给两个基准 —— 这行解释是**固定文案**，与统计行一起出现。
PICK_BENCH_NOTE = (f"超额同时给{PICK_BENCH_CN} 与{PICK_BENCH2_CN}："
                   "候选偏中小盘，只用沪深300 会高估水平")

_STATUS_CN = {"no_quote": "未匹配到行情", "no_bench": "基准缺失", "expired": "未取到行情"}

# 推翻条件核查（invalidation_check，2026-10-05）：M10 每轮复盘时用"记录日之后
# 新出现的新闻"复核一次当初写的推翻条件，结果落在 reviews[k]["invalidation_check"]。
# 逐条展示与统计文案在这里定义一次；M6 推送、M9 网页各有一份**同口径**的短文案
# （三个模块各自独立运行，不为一句文案互相 import）。
# 四态必须严格区分，尤其是**没核查 ≠ 未触发**：
#   triggered     疑似触发（逻辑破产），给原因与依据编号
#   not_triggered 窗口里确有能证伪推翻条件的消息
#   unclear       窗口里没有相关信息（"没有消息"不等于"证明没触发"）
#   None          没核查（老账本行 / 本轮没查 / LLM 失败）—— 绝不说成"未触发"
INV_CHECK_CN = {"triggered": "推翻条件：疑似触发",
                "not_triggered": "推翻条件：未触发",
                "unclear": "推翻条件：无法判断（窗口内无相关信息）"}
INV_UNCHECKED_CN = "推翻条件核查：未核查"
INV_REASON_MAX = 60            # 逐条里的原因截断（完整原文在账本里）


def pick_inv_check(row):
    """账本行 → 该条的推翻条件核查结论（取**已核查过**的那一档）。

    一条候选有多档复盘（T+1/T+3/T+5），核查是随每档补录各做一次：只要任意一档
    给出了 verdict，就用它（取最严重的那档 —— triggered > not_triggered > unclear，
    同级别取最新补录的那档）。**全为 None（老账本行 / 没查过）时返回 None**，
    展示层据此说「未核查」，不能混成"未触发"。

    返回 (verdict, reason, basis[, done]) 或 None；done 是该档的补录日，
    用于在逐条里标出"这条结论是哪天查的"。
    """
    best = None
    for k in (1, 3, 5):
        rev = (row.get("reviews") or {}).get(str(k))
        if not isinstance(rev, dict):
            continue
        ic = rev.get("invalidation_check")
        if not isinstance(ic, dict):
            continue
        v = str(ic.get("verdict") or "").strip().lower()
        if v not in INV_CHECK_CN:
            continue
        rank = {"triggered": 2, "not_triggered": 1, "unclear": 0}[v]
        cand = (rank, str(rev.get("done") or ""), v, ic)
        if best is None or cand[:2] > best[:2]:
            best = cand
    if best is None:
        return None
    _rank, done, v, ic = best
    basis = [int(b) for b in (ic.get("basis") or [])
             if isinstance(b, (int, float)) or str(b).lstrip("-").isdigit()]
    return v, str(ic.get("reason") or ""), basis, done


def inv_check_html(row):
    """一条候选的「推翻条件核查」那一行（HTML）。**核查结论与 invalidation 原文
    是两件事**：前者是"后来查过了、结论如何"，后者是"当初写下的那句话"，
    两者都要显示（原文由 _pick_card 照旧渲染）。"""
    got = pick_inv_check(row)
    if not got:
        return f'<p class="pick-inval-check muted">{INV_UNCHECKED_CN}</p>'
    v, reason, basis, _done = got
    label = INV_CHECK_CN[v]
    if v == "triggered":
        detail = []
        if reason:
            detail.append(esc(reason[:INV_REASON_MAX]
                              + ("…" if len(reason) > INV_REASON_MAX else "")))
        if basis:
            # 编号体系要写清：这里的 #n 是**核查时的新闻窗口位置**（1-based），
            # 与卡片上「依据 [n]」引用的 M3 新闻编号是两套东西 ——
            # 同一个号会指到两条不同的新闻，不标明就是误导。
            detail.append("核查窗口 " + "、".join(f"#{b}" for b in basis[:6]))
        tail = ("：" + "　".join(detail)) if detail else ""
        return (f'<p class="pick-inval-check inv-triggered"><b>⚠️ {label}</b>'
                f'{tail}</p>')
    return f'<p class="pick-inval-check muted">{label}</p>'


def inv_check_stats(rows):
    """**关键统计**：已复核档里有多少条逻辑破产、其中多少条价格仍为正超额。

    口径（用户指定）：只在 verdict 非 null 的档里数 triggered；lucky 是这些
    triggered 档里 alpha > 0 的条数。sample 为 0 时调用方**不显示这条**
    （"0 条"会让读者以为这套核查没在跑）。
    """
    sample = trig = lucky = 0
    for r in (rows or []):
        for k in (1, 3, 5):
            rev = (r.get("reviews") or {}).get(str(k))
            if not isinstance(rev, dict):
                continue
            ic = rev.get("invalidation_check")
            if not isinstance(ic, dict):
                continue
            v = str(ic.get("verdict") or "").strip().lower()
            if v not in INV_CHECK_CN:
                continue
            sample += 1
            if v != "triggered":
                continue
            trig += 1
            a = rev.get("alpha")
            if isinstance(a, (int, float)) and a > 0:
                lucky += 1
    return {"sample": sample, "triggered": trig, "lucky": lucky}


def inv_check_stat_line(st, scope=""):
    """关键统计 → 一行文案。sample == 0 时返回空串（不显示）。

    用词刻意是「蒙对」而不是"胜率"：这些条目的判断逻辑已经被推翻，价格跑赢只是
    恰好，把它算进判断能力就是这套复盘最想避免的错觉。
    scope 是可选的口径后缀（如「（口径：近 14 天往期回填）」）——每个出口统计的
    行集不同（M5 只统计展示窗口内的往期行），不写清会让人把 N 当成整本账本。
    """
    if not st or not st.get("sample"):
        return ""
    return (f'⚠️ 已复核 {st["sample"]} 档中有 {st["triggered"]} 档逻辑破产'
            f'（价格仍跑赢 {st["lucky"]} 档）—— 这类"蒙对"不计入判断能力{scope}')

# 盘前/盘后两个批次分开显示（2026-10-04 盘前通道）：
#   am = 08:10 那轮生成的「今日可执行观察清单」，基准是**昨收**，当天即可对照执行；
#   pm = 收盘后那轮记录，基准是当日收盘价，属于事后判断。
# 2026-10-05 起 am 那批对外就叫**推荐**（「今日潜力个股（推荐）」）：「今日可执行观察
# 清单」这个名字只在卡片标签与分组标题里出现，账本字段一个都没动。
# 盘前组一律排在前面；**两组都有时才给小标题** —— 只有一组时没有可对比的另一组，
# 标题纯属多占一屏（只有 am 那一组时，"推荐"体现在卡片的「为什么推荐」与 h2 计数行里）。
SLOT_ORDER = ("am", "pm")
# 分组标题（M5 / M6 / M9 三处同一口径，2026-10-05 起）：
#   am 那批就是给用户看的**推荐**（「今日潜力个股（推荐）」，每条写明为什么推荐）；
#   pm 那批是收盘后的记录，已经复盘过，不叫推荐更准确。
SLOT_GROUP_CN = {"am": "今日潜力个股（推荐）", "pm": "盘后（收盘复盘后记录）"}
# h2 计数行（今日 N 条（推荐 2 · 盘后 1））用的短名：长标题并列会把这行撑爆
SLOT_COUNT_CN = {"am": "推荐", "pm": "盘后"}
# logic 的字段标签：**按 slot 分**。am 行是"推荐"，所以要回答"为什么推荐"；
# pm 行是收盘后的事后记录，仍然叫「逻辑」—— 把它也叫推荐并不准确。
PICK_LOGIC_LABEL = {"am": "为什么推荐", "pm": "逻辑"}
# 盘前（推荐）批次下方的固定风险提示。与 M6 推送、M9 网页同一口径
# （三处都有「不是买入指令」），版式各自沿用既有排版（这里用既有的 .pick-note，
# 不引入新类、不加新颜色）。HTML 片段，故用 <b> 而不是 markdown 的 **。
PICK_RECOMMEND_NOTE = (
    "以上是 AI 依据当日新闻给出的<b>观察建议</b>（含关注区间与触发条件），"
    "<b>不是买入指令、不构成投资建议</b>；新闻≠股价，已被消化的利好可能“利好出尽”；"
    "每条都写了推翻条件，跑输的记录不会删。"
)


def pick_slot(row):
    """账本行的批次：只有显式 am 才算盘前，其余（旧数据没 slot / 写坏）一律按盘后。

    盘后是账本的**历史默认值**：盘前通道是后加的，把缺失值当盘前会把整批老账本
    误标成「今日可执行」。
    """
    s = (row.get("slot") or "").strip().lower()
    return s if s in SLOT_ORDER else "pm"


# ---------------------------------------------------------------------------
# 连续推荐合并（2026-10-05，**只在展示层**）
#
# 用户要求：同一只票连续被推荐时不要一天一条地刷屏，合并成一条并标出「第几天」。
# 判据只有这一条：**同一 name，相邻记录日相差不超过 PICK_MERGE_GAP_DAYS 个自然日**
# 即视为同一次连续推荐；间隔更远的同名行是**两次独立推荐**，绝不合并。
#
# 铁律（不许破）：
#   · 账本（reports/picks/ledger.jsonl）一行都不改；
#   · 统计口径一行都不改 —— T+1/T+3/T+5 的均值与样本数照旧按**账本每一行**算
#     （picks_stats 收的仍是未合并的 hist）；
#   · 历史明细表（往期候选回填）**逐行列出，不得合并**。
# ---------------------------------------------------------------------------
PICK_MERGE_GAP_DAYS = 3


def _iso_date(s):
    """账本日期串 → datetime；解析不了返回 None（不猜）。"""
    try:
        return datetime.strptime(str(s or ""), "%Y-%m-%d")
    except Exception:
        return None


def merge_pick_rows(rows):
    """显示层合并：把「同一次连续推荐」的行并成一条，以**最新那条**为代表。

    返回 [{"row": 代表行, "days": 组内行数, "first_date": 组内最早记录日,
    "members": [...]}]；输入顺序按记录日倒序遍历，同日保持原顺序。
    日期缺失/坏掉的行不并入任何组（宁可各显各的，也不把两条无关的记录并起来）。
    """
    groups = []
    for r in sorted(rows or [], key=lambda x: str(x.get("date") or ""), reverse=True):
        name = r.get("name")
        d = _iso_date(r.get("date"))
        for g in groups:
            if g["row"].get("name") != name:
                continue
            prev = _iso_date(g["members"][-1].get("date"))
            if d and prev and 0 <= (prev - d).days <= PICK_MERGE_GAP_DAYS:
                g["members"].append(r)
                g["days"] += 1
                g["first_date"] = str(r.get("date") or "")
                break
        else:
            groups.append({"row": r, "days": 1,
                           "first_date": str(r.get("date") or ""), "members": [r]})
    return groups


def pick_run(all_rows, row):
    """该行所处的「连续推荐」区间（用**整本账本**算，不只看今天这一批）。

    返回 {"days": N, "first_date": ...}。同名、相邻记录日相差 ≤
    PICK_MERGE_GAP_DAYS 个自然日的一串行算同一次。

    **N 按「不同日期数」算，不按账本行数**（2026-10-05 修正）：从 10-08 起同一天会有
    盘前（am）与盘后（pm）两批，同一只票同日出现两条是常态 —— 按行数算会显示
    「连续第 2 天推荐」而实际上只过了一天，是明确的误导。
    """
    name = row.get("name")
    d = _iso_date(row.get("date"))
    if not name or not d:
        return {"days": 1, "first_date": str(row.get("date") or "")}
    dates = {str(row.get("date") or "")}
    cur = d
    rest = sorted((r for r in (all_rows or [])
                   if r is not row and r.get("name") == name
                   and _iso_date(r.get("date")) and _iso_date(r.get("date")) <= d),
                  key=lambda r: str(r.get("date")), reverse=True)
    for r in rest:
        rd = _iso_date(r.get("date"))
        gap = (cur - rd).days
        if 0 <= gap <= PICK_MERGE_GAP_DAYS:
            dates.add(str(r.get("date") or ""))
            cur = rd
        else:
            break      # 日期已按倒序排好，后面只会更远
    return {"days": len(dates), "first_date": min(dates)}


def streak_label(days, first_date):
    """「连续第 3 天推荐（首次 10-06）」；days < 2 返回 ""（一次记录不叫"连续"）。"""
    if not isinstance(days, int) or days < 2:
        return ""
    mm = _mmdd(first_date)
    return (f"连续第 {days} 天推荐（首次 {mm}）" if mm
            else f"连续第 {days} 天推荐")


def pick_streak(all_rows, row):
    """一条候选在整本账本里的连续推荐区间 → 可显示的短句（无则 ""）。"""
    run = pick_run(all_rows, row)
    return streak_label(run["days"], run["first_date"])


def merge_group_streak(grp, all_rows):
    """合并组 → (天数, 首次日期)：组内合并口径与整本账本的连续区间取**较大**者，
    首次日期取更早的那个（两者一致时就是它自己）。"""
    run = pick_run(all_rows, grp["row"])
    days = max(grp["days"], run["days"])
    firsts = [x for x in (grp.get("first_date"), run.get("first_date")) if x]
    return days, (min(firsts) if firsts else "")


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
    """一档复盘 → 单元格。未到期显示 —；补记的标注实际跨度（due 与 done 可能差几天）。

    两个基准并列（2026-10-05）：超额给两行（沪深300 / 中证1000）。老账本行没有
    bench2 数据时第二行是 **「—」** —— 不能显示 0%，也不能把它算进均值
    （那些行本来就没这个口径的数，写 0% 等于凭空造出一个"跑平"的样本）。
    """
    if not rev:
        return '<span class="pick-pending">T+%d 未到期</span>' % k
    if rev.get("status") != "ok":
        return f'<span class="pick-pending">{esc(_STATUS_CN.get(rev.get("status"), rev.get("status")))}</span>'
    lag = ""
    if rev.get("done") and rev.get("due") and rev["done"] != rev["due"]:
        lag = f'<span class="pick-lag">{esc(rev["done"][5:])}补</span>'
    return (f'<b>{_pct(rev.get("ret"))}</b>'
            f'<span class="pick-alpha">超额 {PICK_BENCH_CN} {_pct(rev.get("alpha"))}</span>'
            f'<span class="pick-alpha">{PICK_BENCH2_CN} {_pct(rev.get("alpha2"))}</span>'
            f"{lag}")


def _tier_stat_line(k, d):
    """一档的两口径统计文本（均值只在样本 ≥PICK_MIN_SAMPLE 时给）。

    格式（与 M6 推送同一口径、与用户给的样例一致）：
      T+1 均超额 +2.38%（相对沪深300） / +1.10%（相对中证1000）· 样本 4 条
    样本数**两个口径分别报**：中证1000 是后加的口径，老账本/bench2 缺失的档位
    只会进 n 不进 n2；两者不同时写成「样本 4 条 / 中证1000 口径 3 条」，
    相同时只写一个，不啰嗦也不谎报。
    """
    n, n2 = d["n"], d["n2"]
    tail = f"样本 {n} 条" + ("" if n2 == n else f" / {PICK_BENCH2_CN} 口径 {n2} 条")
    if n < PICK_MIN_SAMPLE:
        return f"T+{k} {tail}（少于 {PICK_MIN_SAMPLE} 条不给均值）"
    a2 = _pct(d["alpha2"]) if n2 else "—"
    return (f"T+{k} 均超额 {_pct(d['alpha'])}（相对{PICK_BENCH_CN}） / {a2}"
            f"（相对{PICK_BENCH2_CN}）· {tail}")


def picks_stats(rows):
    """各档平均收益与两个基准的平均超额 → {k: {"n","ret","alpha","n2","alpha2"}}。

    只统计 status == "ok" 的档位，n 就是参与平均的样本数 —— **n 必须与均值
    一起显示**，本函数因此把 n 一并返回而不是只给均值。
    n2 / alpha2 是第二基准（中证1000）口径，**独立计数**：老账本行、或 bench2
    任一端点缺失的档位（alpha2 为 null）不进 n2，于是"沪深300 4 条、中证1000
    3 条"能如实分别报出，而不是把一个缺数据的口径算成 0%。
    不产出胜率/命中率这类字段：样本量小时百分比没有意义，且它会把连续的超额
    压成二值的对/错。
    """
    out = {}
    for k in (1, 3, 5):
        vals_r, vals_a, vals_a2 = [], [], []
        for r in rows:
            rev = (r.get("reviews") or {}).get(str(k))
            if not rev or rev.get("status") != "ok":
                continue
            if rev.get("ret") is not None:
                vals_r.append(rev["ret"])
            if rev.get("alpha") is not None:
                vals_a.append(rev["alpha"])
            if rev.get("alpha2") is not None:
                vals_a2.append(rev["alpha2"])
        out[k] = {
            "n": len(vals_r),
            "ret": (sum(vals_r) / len(vals_r)) if vals_r else None,
            "alpha": (sum(vals_a) / len(vals_a)) if vals_a else None,
            "n2": len(vals_a2),
            "alpha2": (sum(vals_a2) / len(vals_a2)) if vals_a2 else None,
        }
    return out


def _pick_card(r, streak=""):
    """一条今日候选卡：推荐理由（am）/ 逻辑（pm）+ 推翻条件是主体，价格只是脚注。

    盘前行（slot=am）额外给出**关注区间**与**触发条件** —— 这两个字段是
    「今日可执行」的全部依据。任一字段为空串/缺失就整行不渲染，绝不出现
    「关注区间：」后面空着一串（那会让读者以为有区间、只是没写出来）。

    `logic` 的**标签按 slot 分**（2026-10-05）：am 行是推荐，标签为「为什么推荐」
    （内容由 M10 的 prompt + 校验保证写了依据/传导机制/预期差）；pm 行已经复盘过，
    仍叫「逻辑」。两处都显式写出标签，读者一眼能分清这条是推荐还是事后记录。

    **推翻条件的两个层次**（2026-10-05）：`invalidation` 是当初写下的那句话，
    `invalidation_check` 是后来复核的结论 —— 两句都显示，且**核查列的措辞必须
    与原文的「推翻条件」区分开**（前者是"当初写了什么"，后者是"后来查得怎样"）。

    `streak` 是显示层的连续推荐标记（「连续第 3 天推荐（首次 10-06）」）：
    同名的相邻记录日 ≤3 自然日的行已被 merge_pick_rows 并成这一条（2026-10-05）。
    为空串时一个字符都不加。
    """
    tag = '<span class="pick-tag">板块</span>' if r.get("kind") == "board" else ""
    refs = "".join(f'<span class="pick-ref">[{n}]</span>'
                   for n in (r.get("basis_refs") or [])[:8])
    is_am = pick_slot(r) == "am"
    plan = ""
    if is_am:
        zone = (r.get("entry_zone") or "").strip()
        trig = (r.get("trigger") or "").strip()
        if zone:
            plan += f'<p class="pick-plan"><b>关注区间</b>：{esc(zone)}</p>'
        if trig:
            plan += f'<p class="pick-plan"><b>触发条件</b>：{esc(trig)}</p>'
    # 盘前行的基准价是**昨收**，与盘后行的当日收盘价不是一个口径，故标出来；
    # 取不到价位时不标（否则会写成像「昨收 未匹配到行情」那样的病句）
    price = r.get("base_price")
    if not price:
        base = "未匹配到行情"
    elif is_am:
        base = f"昨收 {esc(price)}"
    else:
        base = esc(price)
    inv_check = inv_check_html(r)
    streak_html = f'<span class="pick-streak">{esc(streak)}</span>' if streak else ""
    return f"""<div class="pick-card">
  <div class="pick-head"><b>{esc(r.get("name"))}</b>{tag}{streak_html}
    <span class="pick-conf">置信度 {esc(r.get("confidence") or "—")}</span></div>
  <p class="pick-logic"><b>{PICK_LOGIC_LABEL["am" if is_am else "pm"]}</b>：{esc(r.get("logic"))}</p>
  {plan}<p class="pick-inval"><b>推翻条件</b>：{esc(r.get("invalidation"))}</p>
  {inv_check}
  <div class="pick-foot">基准 {base}
    {("· 所属板块 " + esc(r["board"])) if r.get("board") and r.get("kind") == "stock" else ""}
    {("· 新闻依据 " + refs) if refs else ""}</div>
</div>"""


def latest_batch(picks, date_str):
    """账本里最近一批记录的（日期, 距 date_str 的天数, 是不是"推荐"批）。

    "推荐"批 = slot=am 的那些行（盘前那轮）。账本里一条 am 都没有（全部是老账本）
    时退回最近一条记录，调用方据此把用词降级为"候选记录" —— 把 pm 批也叫"推荐"
    并不准确（它是收盘后的事后记录，见 PICK_LOGIC_LABEL 的说明）。
    取不到日期、或日期格式坏掉时对应位置返回 None，调用方不写那一段。
    """
    def _latest(rows):
        ds = [r.get("date") for r in rows if r.get("date")]
        return max(ds) if ds else None

    d = _latest([r for r in picks if pick_slot(r) == "am"])
    is_am = bool(d)
    if not d:
        d = _latest(picks)
    if not d:
        return None, None, False
    try:
        days = (datetime.strptime(date_str, "%Y-%m-%d")
                - datetime.strptime(str(d), "%Y-%m-%d")).days
    except Exception:
        return d, None, is_am
    return d, days, is_am


def picks_empty_note(picks, date_str):
    """今天一条候选都没有时的说明（休市/长假/本轮没产出）。

    旧文案只说"本时段没有新记录的候选"，读者没法判断这是"今天本来就不产出"还是
    "出问题了"，也不知道上一批是什么时候。现在明说**今天没有产出推荐**，并给出
    **最近一批**的日期与已过去的天数（账本里取不到就不写这一段）。
    """
    d, days, is_am = latest_batch(picks, date_str)
    tail = ""
    if d:
        what = "推荐" if is_am else "候选记录"
        ago = f"，已过去 {days} 天" if days is not None else ""
        tail = f"最近一批{what}是 {esc(d)}{ago}。"
    return ('<p class="pick-empty">今天没有产出推荐（休市日不产出；盘前 08:10 那轮记入'
            '「今日潜力个股（推荐）」，基准为昨收；收盘后那轮记入当日候选，基准为'
            f'当日收盘价）。{tail}以下是跟踪中候选的表现。</p>')


def render_picks(picks, date_str):
    """候选观察清单版块。没有任何账本数据时返回空串（不显示空版块）。

    今日候选按批次分组：**推荐（am）在前、盘后（收盘复盘后记录，pm）在后**；
    两组都有时才给组标题。**推荐那一组下方另加一句固定风险提示**
    （PICK_RECOMMEND_NOTE）——「推荐」这个词必须紧跟一句"不是买入指令"，
    否则读者会把推荐读成买入建议。往期回填明细与均值口径不受分组影响。
    """
    if not picks:
        return ""
    today_rows = [r for r in picks if r.get("date") == date_str]
    cutoff = (datetime.strptime(date_str, "%Y-%m-%d")
              - timedelta(days=PICK_HISTORY_DAYS)).strftime("%Y-%m-%d")
    hist = sorted([r for r in picks if r.get("date") != date_str
                   and r.get("date", "") >= cutoff],
                  key=lambda r: (r.get("date", ""), r.get("id", "")), reverse=True)

    # 今日候选卡按批次分组（推荐在前），组内保持账本原有顺序
    groups = [(s, [r for r in today_rows if pick_slot(r) == s]) for s in SLOT_ORDER]
    groups = [(s, rs) for s, rs in groups if rs]
    # **连续推荐合并**（2026-10-05，只在展示层）：同一 name 且相邻记录日 ≤3 自然日
    # 的行并成一条，以最新那条为代表，卡片上标出「连续第 N 天推荐（首次 MM-DD）」。
    # 天数取「组内合并口径」与「整本账本的连续区间」中较大者 —— 今天这张卡要说的
    # 是"这只票已经连续第几天被推荐"，不是"今天这一批里有几条同名"。
    # 账本一行不动、统计口径一行不动（往期回填与 stats 仍用未合并的 hist）。
    groups = [(s, [{"row": g["row"],
                    "streak": streak_label(*merge_group_streak(g, picks))}
                   for g in merge_pick_rows(rs)])
              for s, rs in groups]
    # 风险提示跟着**推荐那批**走：只要有 am 行就出现（哪怕只有一组、没出组标题）。
    # 只有 pm 批时不出 —— 那批没被叫过"推荐"，不需要这句。
    am_note = (f'<p class="pick-note">{PICK_RECOMMEND_NOTE}</p>'
               if any(s == "am" for s, _ in groups) else "")
    if len(groups) > 1:
        today_html = "".join(
            f'<div class="pick-group">'
            f'<h3 class="pick-group-title">{SLOT_GROUP_CN[s]}'
            f'<span class="h2-count">{len(rs)} 条</span></h3>'
            f'{"".join(_pick_card(g["row"], g["streak"]) for g in rs)}'
            f'{am_note if s == "am" else ""}</div>'
            for s, rs in groups
        )
        count_breakdown = ("（" + " · ".join(f"{SLOT_COUNT_CN[s]} {len(rs)}"
                                             for s, rs in groups) + "）")
    else:
        today_html = ("".join(_pick_card(g["row"], g["streak"])
                              for _, rs in groups for g in rs)
                      + (am_note if groups and groups[0][0] == "am" else ""))
        count_breakdown = ""

    if not today_rows:
        today_html = picks_empty_note(picks, date_str)

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
  <p class="pick-inval"><b>推翻条件</b>：{esc(r.get("invalidation"))}</p>
  {inv_check_html(r)}
</div>""")

    # 汇总行：均值与样本数**必须同时出现**，且**两个基准并列**（沪深300 / 中证1000）
    st = picks_stats(hist)
    parts = []
    for k in (1, 3, 5):
        d = st[k]
        if d["n"] == 0:
            continue
        parts.append(_tier_stat_line(k, d))
    stats_html = ("<p class=\"pick-stats\">" + "　".join(parts) + "</p>") if parts else ""
    # **关键统计**（2026-10-05）：逻辑被推翻却因价格跑赢而被记成"成功"的那几条。
    # 与均值同处统计区，读者一眼能看到"这种蒙对不算本事"；样本为 0（还没复核过
    # 任何一档，如老账本）时**整条不显示** —— 写"0 条"会让人以为核查没在跑。
    inv_line = inv_check_stat_line(inv_check_stats(hist), "（口径：本版块展示窗口内的往期回填）")
    inv_html = f'<p class="pick-stats pick-inval-warn">{inv_line}</p>' if inv_line else ""
    # 固定一行解释（用户要求）：为什么同时给两个口径。它跟着候选版块走，
    # 与统计行同处一段，读者看到两个数就知道差别在哪。
    bench_note = f'<p class="pick-note">{PICK_BENCH_NOTE}</p>'

    hist_html = ""
    if rows_html:
        hist_html = f"""<details class="fold">
  <summary>
    <h3>往期候选回填<span class="h2-count">{len(rows_html)} 条</span></h3>
    <span class="fold-hint"><span class="fold-label"></span><span class="fold-arrow"></span></span>
  </summary>
  {''.join(rows_html)}
  <p class="pick-note">收益率为<b>未复权</b>口径；区间内若发生除权除息，该档会被低估。
  每档给两行超额：{PICK_BENCH_CN} 与 {PICK_BENCH2_CN}；没有第二个口径的旧记录显示
  「—」（不计入均值）。「补」表示实际打分日与到期日不同（周末/停牌/限流所致）。</p>
</details>"""

    # 计数与**实际渲染出来的卡片数**一致（连续推荐合并后卡片会少于账本行数，
    # 表头仍写账本行数会与下面的卡片对不上）——账本行数本身在往期明细里逐行可见
    n_today = sum(len(rs) for _, rs in groups) if today_rows else 0
    return f"""<section class="block" id="picks">
  <h2>候选观察清单<span class="h2-count">今日 {n_today} 条{count_breakdown}</span></h2>
  <p class="pick-note">盘前 08:10 那批是<b>推荐</b>：每条都写明推荐的三件事
  （新闻依据 → 传导机制 → 预期差），并给出<b>关注区间</b>与<b>触发条件</b>；
  收盘后那批是当日复盘记录。每条都带<b>推翻条件</b>与新闻依据，其后续表现按
  T+1/T+3/T+5 原样回填，<b>含跑输的</b>（超额同时给{PICK_BENCH_CN} 与{PICK_BENCH2_CN}）。
  <b>不是买入指令</b>，决策权归您本人。</p>
  {today_html}
  {stats_html}
  {inv_html}
  {bench_note}
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
/* 跨天事件追踪标记（2026-10-05，M2 的 continuing）：high 是"确认了同一事件在
   持续发酵"，low 只是"疑似"，所以 low 用更弱的样式（灰、无边框强调）。
   只用既有 token（--accent / --muted / --border），不引入新颜色。 */
.badge.continuing{background:none; color:var(--accent); border:1px solid var(--accent);}
.badge.continuing.weak{color:var(--muted); border-color:var(--border);}
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
/* 非沪深 A 股单独一栏（2026-10-04）：降饱和 + 子标题说明，避免与主表混为一谈 */
.muted-table{opacity:.72;}
.q-sub-title{
  margin:14px 0 8px; font-size:13px; font-weight:600; color:var(--muted);
  border-top:1px solid var(--line); padding-top:10px;
}
.q-sub-note{font-weight:400; margin-left:8px;}
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
/* 连续推荐标记（2026-10-05）：同名、相邻记录日 ≤3 自然日的行已合并成这一条。
   与 .pick-tag 同版式（既有 token），但语义不同故单独一类，免得改 .pick-tag
   时把"板块"标签一起改了。 */
.pick-streak{font-size:11px; color:var(--warn); border:1px solid var(--warn);
  border-radius:2px; padding:1px 6px;}
.pick-logic{font-size:14px; line-height:1.65; margin:8px 0 6px;}
.pick-inval{font-size:13px; line-height:1.6; margin:0; color:var(--muted);}
.pick-inval b{color:var(--text);}
/* 推翻条件核查（2026-10-05）：triggered 是"逻辑破产"，要一眼看见；
   未核查/未触发/无法判断都归为弱提示。沿用既有 token（--up 在浅色里就是红）。 */
.pick-inval-check{font-size:13px; line-height:1.6; margin:4px 0 0; color:var(--muted);}
.pick-inval-check b{color:var(--text);}
.pick-inval-check.inv-triggered{color:var(--up); border-left:3px solid currentColor;
  padding-left:8px;}
.pick-inval-check.inv-triggered b{color:var(--up);}
.pick-inval-warn{color:var(--up) !important; font-weight:600;}
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
/* 候选分组（2026-10-04 盘前通道）：盘前（今日可执行）在前、盘后（收盘复盘后记录）
   在后；两组都有时才渲染标题（只有一组时没有可比对象）。沿用版块标题的衬线字号
   与细线，不引入新颜色。 */
.pick-group{margin-top:14px;}
.pick-group-title{
  font-family:var(--serif); font-size:15.5px; font-weight:700; color:var(--ink);
  margin:14px 0 8px; padding-bottom:6px; border-bottom:1px solid var(--border);
  display:flex; align-items:center; gap:8px;
}
/* 盘前条目独有的两行：关注区间 / 触发条件。字段为空串时整行不渲染，
   不会留下「关注区间：」后面空着的一行。 */
.pick-plan{font-size:13px; line-height:1.65; margin:0 0 4px;}
.pick-plan b{color:var(--text);}

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
    # 标题里分出「沪深 A 股 / 其它市场」：一眼看出今天新闻涉及的、你能买的有几只。
    # 优先用 M4 写下的 by_market/a_share_count（纯附加字段），缺失时按 market 标签现算，
    # 这样旧数据（M4 还没写这两个字段时）也能正确显示。
    _a_cnt = data["quotes"].get("a_share_count")
    if not isinstance(_a_cnt, int):
        _a_cnt = sum(1 for q in data["quotes"].get("quotes", [])
                     if _is_a_share_market(q.get("market")))
    _other = quote_count - _a_cnt
    quote_breakdown = (f"（沪深 A 股 {_a_cnt} 只"
                       + (f" / 其它市场 {_other} 只）" if _other else "）")) if quote_count else ""

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
    <h2>个股行情一览<span class="h2-count">{quote_count} 只{quote_breakdown}</span></h2>
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
    # 复盘看板（M12）入口：索引页只列"一天一份"的日报，而复盘看板回答的是
    # 「这一个月我到底准不准」，属于另一种入口，放在日报列表下方、与网页版门户同列。
    #
    # **不渲染比渲染死链好**：链接只在 reports/review-latest.html 真的存在时出现
    # （M12 允许失败，从未跑过、或被清掉时这个文件就不存在）。这条规则只影响
    # 索引页 —— .web-entry / .index-item 是索引页专属类，日报正文不用它们。
    review_path = REPORTS_DIR / "review-latest.html"
    review_entry = ""
    if review_path.exists():
        # 标题里带着它是哪一周的快照（review-latest.html 是"最近一次生成的"，
        # 流水线停摆时它会是旧周，写出来免得把旧快照当成今天的）
        try:
            _t = re.search(r"<title>[^<]*</title>",
                           review_path.read_text(encoding="utf-8"))
            _week = re.search(r"(\d{4}-W\d{2})", _t.group(0)) if _t else None
        except Exception:
            _week = None
        _label = f"复盘看板 · {_week.group(1)}" if _week else "复盘看板"
        review_entry = (
            f'<a class="web-entry" href="review-latest.html">'
            f'<div class="d">{esc(_label)}</div>'
            f'<div class="s">一段时间里的候选表现汇总：双基准均值超额 · 按批次/置信度/板块分组 · '
            f'逻辑核查（含"蒙对"）· 逐条明细</div>'
            f'<div class="go">查看复盘看板 →</div>'
            f"</a>"
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
    {review_entry}
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
