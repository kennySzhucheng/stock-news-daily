# -*- coding: utf-8 -*-
"""M12 复盘看板 — 把 M10 的候选账本聚合成「我到底准不准」，一周一个快照。

用法（仓库根目录）:
    python modules/m12_weekly/weekly.py
    python modules/m12_weekly/weekly.py --ledger <path> --out-dir <path>
    python modules/m12_weekly/weekly.py --asof 2026-10-05     # 快照截止日（默认取账本最后一行）

产出:
    reports/review-<ISO年>-W<ISO周>.html   当周快照（可累积：每周一个文件）
    reports/review-latest.html             同一份内容的固定入口（给索引页/网页版做链接）

为什么要有这个模块（2026-10-05 用户第四优先级）:
  此前只能一天天读日报，看不到"这一个月我到底准不准"。M10 已经把复盘数据备齐
  （每档 ret / alpha / alpha2 / span / status，以及 invalidation_check 四态），
  但没人把它聚合成结论。这一步只做聚合与排版。

设计约束（都是项目既有约定，不是新发明）:
  · **只读账本**：本模块不写 reports/picks/ 下任何文件、不联网、不调 LLM。
  · **不给百分比式的输赢结论**：个位数样本的百分比没有意义，全文（含代码注释之外的可见文案）
    刻意不出现这两个字；有均值的地方**必须**同时给样本数。
  · **双基准并列**：候选偏中小盘，只看沪深300 会高估水平，故每个统计块都同时给
    沪深300 与中证1000；两个口径各自的样本数独立标注（老账本没有 bench2 字段，
    两条口径的 n 天然不同，合并成一个 n 是骗人的）。
  · **未核查 ≠ 未触发**：`invalidation_check` 为 null 的档一律不计入任何核查口径，
    逐条明细里显示为「未核查」。
  · **跑输的不删不隐藏**：明细列出全部已复核档，跑赢跑输同版式同字号。
  · **模块独立**：不 import M5/M9 的样式，自带一小段 CSS（浅色纸面 / serif 标题 /
    单栏移动端可读）；页面**不含任何 http(s) 引用**，离线可看。
"""
import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime, timedelta

# 路径常量：允许命令行覆盖（测试用临时账本/临时输出目录）
BASE_LEDGER = "reports/picks/ledger.jsonl"
BASE_OUT_DIR = "reports"

BENCH_NAME = "沪深300"
BENCH2_NAME = "中证1000"
REVIEW_DAYS = (1, 3, 5)
SLOT_CN = {"am": "盘前推荐", "pm": "盘后记录"}
CONF_ORDER = ("高", "中", "低")
CONF_CN = {"高": "高", "中": "中", "低": "低"}

# 四态文案：与 M5 的 INV_CHECK_CN 同一口径（同一结论不能两处说法不同）。
# 这里复制一份常量而不是 import M5 —— 模块独立是项目既有约定；
# 语义由 tests 里的四态断言锁住。
# 这里复制一份常量而不是 import M5 —— 模块独立是项目既有约定；
# 语义由 tests 里的四态断言锁住。
#
# **文案必须与 M5/M9 逐字一致**（2026-10-05 统一）：同一条结论在两个页面用两种说法
# （这里是「推翻核查：疑似触发」、日报里是「推翻条件：疑似触发」）会让人怀疑它们
# 不是同一件事。tests/offline_tests.py 里有一条断言把三处文案钉死。
VERDICT_CN = {"triggered": "推翻条件：疑似触发",
              "not_triggered": "推翻条件：未触发",
              "unclear": "推翻条件：无法判断（窗口内无相关信息）"}
VERDICT_NONE_CN = "推翻条件核查：未核查"
VERDICT_RANK = {"triggered": 2, "not_triggered": 1, "unclear": 0}

MIN_SAMPLE = 5
SMALL_SAMPLE_NOTE = f"⚠️ 样本不足 {MIN_SAMPLE} 条，仅供参考，不足以判断"
NO_DATA = "暂无"

# 固定写明的话（用户指定，一字不改地出现在页面上）
NOTE_ABOUT_TEXT = (
    f"超额同时给 {BENCH_NAME} 与 {BENCH2_NAME}：候选偏中小盘，"
    f"只用{BENCH_NAME} 会高估水平。"
)
NOTE_SMALL_SAMPLE = (
    f"任何统计块样本 < {MIN_SAMPLE} 条时都会显式标注；"
    f"某档 0 样本写「{NO_DATA}」而不是 0%。"
)
# 项目既有约定：不给百分比式的输赢结论（个位数样本的百分比没有意义）。这段文案
# **刻意不写出被禁的那两个汉字** —— 页面上连"我们不给 X"都不出现，
# 免得被截图或搜索当成一个指标名。tests/ 里有一条断言全文不含它。
NOTE_NO_WINRATE = (
    "本页不给「对了百分之几」这类百分比结论：个位数样本的百分比没有意义，"
    "所以只给样本数与均值，由您自己判断。"
)
NOTE_KEEP_LOSERS = "跑输的记录不删也不隐藏，按日期倒序全部列出。"
NOTE_NOT_ADVICE = "本页不是买入指令、不构成投资建议；决策权归您本人。"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CSS = """
:root{
  /* 与日报同一审美：浅色纸面 / 墨色字 / 细线分割，红绿只上数字 */
  --bg:#f7f4ee; --bg2:#efeae1; --card:#ffffff; --border:#ddd6c9;
  --ink:#1f1d1a; --text:#2b2822; --muted:#7a746a;
  --accent:#1e4d7a; --warn:#9a6b12;
  --up:#c4362b; --down:#12734f; --flat:#7a746a;
  --serif:Georgia,"Times New Roman","Songti SC","SimSun","Noto Serif CJK SC",
    "Source Han Serif SC",serif;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
    "Hiragino Sans GB","Microsoft YaHei","Noto Sans CJK SC",sans-serif;
}
*{box-sizing:border-box; margin:0; padding:0;}
html{-webkit-text-size-adjust:100%;}
body{
  background:var(--bg); color:var(--text); font-family:var(--sans);
  line-height:1.78; font-size:16px; padding:0 0 48px;
  max-width:760px; margin:0 auto;
}
a{color:var(--accent); text-decoration:none;}
header.hero{
  padding:0 16px 8px; border-top:4px solid var(--ink);
  border-bottom:1px solid var(--ink); margin:0 8px;
}
header.hero .kicker{
  font:12px/1.6 var(--sans); letter-spacing:.18em; color:var(--muted); padding-top:14px;
}
header.hero h1{font-family:var(--serif); font-size:26px; font-weight:700; line-height:1.25;}
header.hero .date{
  color:var(--muted); font-size:12.5px; letter-spacing:.06em;
  margin-top:8px; padding-top:6px; border-top:1px solid var(--border);
}
main{padding:0 12px;}
section.block{margin:24px 8px;}
section.block > h2{
  font-family:var(--serif); font-size:18px; font-weight:700;
  margin-bottom:12px; padding-bottom:8px; border-bottom:1px solid var(--border);
}
p.lead{font-size:13.5px; color:var(--muted); margin:6px 0 12px;}
.note{
  background:var(--bg2); border-left:3px solid var(--accent);
  padding:10px 12px; margin:10px 0; font-size:13.5px;
}
.note ul{margin:0 0 0 18px;}
.note li{margin:3px 0;}
.warn{
  color:var(--warn); font-size:13.5px; margin:8px 0;
  border-left:3px solid var(--warn); padding:6px 10px; background:var(--bg2);
}
table{width:100%; border-collapse:collapse; margin:8px 0 4px; font-size:14px;}
caption{caption-side:top; text-align:left; color:var(--muted); font-size:12.5px; padding:2px 0 6px;}
th,td{border-bottom:1px solid var(--border); padding:7px 8px; text-align:left; vertical-align:top;}
th{background:var(--bg2); font-weight:600; white-space:nowrap; font-size:13px;}
td.num,th.num{text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums;}
tr.total td{font-weight:600; border-top:1px solid var(--ink);}
.up{color:var(--up);} .down{color:var(--down);} .flat{color:var(--flat);}
.muted{color:var(--muted);} .small{font-size:13px;}
.nodata{color:var(--muted);}
.verdict-triggered{color:var(--up); font-weight:600;}
.verdict-not_triggered{color:var(--muted);}
.verdict-unclear{color:var(--warn);}
.verdict-none{color:var(--muted);}
.kv{display:flex; flex-wrap:wrap; gap:6px 18px; margin:8px 0; font-size:14px;}
.kv b{font-family:var(--serif); font-size:16px;}
.table-wrap{overflow-x:auto;}
footer.risk{
  margin:28px 8px 8px; padding:16px 0 8px;
  border-top:3px double var(--ink); font-size:13px; color:var(--muted); line-height:1.6;
}
footer.risk h3{font-family:var(--serif); font-size:14px; color:var(--ink); margin-bottom:6px;}
footer.risk ul{margin:6px 0 0 18px;}
footer.risk li{margin:3px 0;}
@media (min-width:600px){
  header.hero h1{font-size:32px;}
  section.block > h2{font-size:20px;}
}
"""


# ------------------------------------------------------------------ 账本读取

def load_ledger(path):
    """读 JSONL 账本 → (rows, bad_lines)。**只读**：绝不写回。

    文件不存在、整行坏掉都不该让看板挂掉：坏行只计数并如实写进页面，
    不猜、不补、不跳过不报。行里的 reviews 结构坏掉同样按"没有该档"处理。
    """
    rows, bad = [], 0
    try:
        text = open(path, "r", encoding="utf-8", errors="replace").read()
    except OSError:
        return rows, bad, False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            bad += 1
            continue
        if isinstance(obj, dict):
            rows.append(obj)
        else:
            bad += 1
    return rows, bad, True


def ledger_sha256(path):
    """账本内容哈希（验收用：跑完看板后必须与跑之前一致）。"""
    try:
        data = open(path, "rb").read()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()


def iso_week(d):
    """date → (ISO 年, ISO 周号, 周一)。**按 ISO 口径**（周一为一周之始）。"""
    y, w, _ = d.isocalendar()
    monday = d - timedelta(days=d.weekday())
    return y, w, monday


def parse_date(s):
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


# ------------------------------------------------------------------ 数值口径

def _f(v):
    """只有真正的数值才算数：bool 除外（isinstance(True, int) 是真的）。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


class Cell:
    """一个（档位 × 基准）的累计：样本数与均值。均值**没有样本就不存在**。"""

    __slots__ = ("n", "_sum")

    def __init__(self):
        self.n = 0
        self._sum = 0.0

    def add(self, v):
        if v is None:
            return
        self.n += 1
        self._sum += v

    @property
    def mean(self):
        return (self._sum / self.n) if self.n else None


class HorizonStats:
    """一个分组里 T+1/T+3/T+5 各自的双基准累计。"""

    def __init__(self):
        # cells[k] = {"alpha": Cell, "alpha2": Cell}
        self.cells = {k: {"alpha": Cell(), "alpha2": Cell()} for k in REVIEW_DAYS}
        self.rev_n = {k: 0 for k in REVIEW_DAYS}      # 该档已复核的条数

    def add(self, k, rev):
        alpha = _f(rev.get("alpha"))
        alpha2 = _f(rev.get("alpha2"))
        if alpha is not None:
            self.cells[k]["alpha"].add(alpha)
        if alpha2 is not None:
            self.cells[k]["alpha2"].add(alpha2)
        if alpha is not None or alpha2 is not None:
            self.rev_n[k] += 1

    def mean(self, k, bench):
        return self.cells[k][bench].mean

    def n(self, k, bench):
        return self.cells[k][bench].n

    def any_sample(self):
        return any(self.cells[k][b].n for k in REVIEW_DAYS for b in ("alpha", "alpha2"))


def normalize_confidence(raw):
    """置信度 → 高/中/低 三档之一；认不出来的一律返回 None（**不硬塞进某一档**）。

    账本里写的是中文（高/中/低），兼容英文与「高置信度」这类写法。
    判据刻意是"剥掉置信度/信心/度/级/等 这些修饰字之后**正好**是 高/中/低"，
    而不是"字串里含有 高" —— 后者会把「很高?」「不高」这类认不出的写法
    误判成高置信度，那种错误会直接改写统计口径。
    """
    s = str(raw or "").strip().lower()
    if not s:
        return None
    for key in ("高", "中", "低"):
        if s == key:
            return key
    m = re.fullmatch(r"(?:confidence|conf)?[:\s]*(高|中|低)(?:置信度|信心|度|级|等)?",
                     s)
    if m:
        return m.group(1)
    return {"high": "高", "mid": "中", "medium": "中", "low": "低"}.get(s)


def normalize_verdict(ic):
    """invalidation_check → 四态之一。**null 是「未核查」，不是「未触发」。**"""
    if not isinstance(ic, dict):
        return None
    v = str(ic.get("verdict") or "").strip().lower()
    return v if v in VERDICT_CN else None


def row_verdict(row):
    """一条候选的核查结论 = 各档里"最有信息量"的那个（triggered > not_triggered
    > unclear），与 M5 的 pick_inv_check 同一口径。日期用于并列时取较晚的一档。

    返回 (verdict, reason, basis, done) 或 None（= 全档未核查）。
    """
    reviews = row.get("reviews") if isinstance(row.get("reviews"), dict) else {}
    best = None
    for k in REVIEW_DAYS:
        rev = reviews.get(str(k))
        if not isinstance(rev, dict):
            continue
        v = normalize_verdict(rev.get("invalidation_check"))
        if v is None:
            continue
        ic = rev["invalidation_check"]
        cand = (VERDICT_RANK[v], str(rev.get("done") or ""), v, ic)
        if best is None or cand[:2] > best[:2]:
            best = cand
    if best is None:
        return None
    _, done, v, ic = best
    basis = [b for b in (ic.get("basis") or [])
             if isinstance(b, (int, float)) or str(b).lstrip("-").isdigit()]
    return v, str(ic.get("reason") or ""), basis, done


def row_reviewed_count(row):
    """这条候选有几档已复核（alpha/alpha2 任一存在即算复核过一档）。"""
    reviews = row.get("reviews") if isinstance(row.get("reviews"), dict) else {}
    n = 0
    for k in REVIEW_DAYS:
        rev = reviews.get(str(k))
        if isinstance(rev, dict) and (_f(rev.get("alpha")) is not None
                                      or _f(rev.get("alpha2")) is not None):
            n += 1
    return n


def iter_reviewed(rows):
    """遍历"已复核的档" → (row, k, rev)。**没有 alpha/alpha2 的档不算复核过。**"""
    for row in rows:
        reviews = row.get("reviews") if isinstance(row.get("reviews"), dict) else {}
        for k in REVIEW_DAYS:
            rev = reviews.get(str(k))
            if not isinstance(rev, dict):
                continue
            if _f(rev.get("alpha")) is None and _f(rev.get("alpha2")) is None:
                continue
            yield row, k, rev


def review_scope(rows, asof=None):
    """看板口径：只统计 asof（含）之前记录的行 → {"rows": [...], "asof": date|None}。

    asof 默认取账本里最后一行的 date（**不取墙上时钟**：账本才是事实来源，
    用系统时间会让同一本账在不同日子跑出不同页面，幂等就没了）。
    没给 --asof 时窗口右侧不会被系统时间推动 → 同日重跑内容完全一致。

    返回 dict（而不是 (rows, asof) 元组）：元组很容易被整只当成 rows 传下去，
    2026-10-05 就这么错过一次 —— 统计正常但"最近 4 周"永远是空的，
    因为 aggregate 收到的 rows 是个 tuple，行里读不出日期。
    """
    dated = [(parse_date(r.get("date")), r) for r in rows]
    known = [d for d, _ in dated if d]
    if asof is None:
        asof = max(known) if known else None
    elif isinstance(asof, str):
        asof = parse_date(asof)
    if asof is None:
        return {"rows": [], "asof": None}
    return {"rows": [r for d, r in dated if d and d <= asof], "asof": asof}


# ------------------------------------------------------------------ 聚合

def aggregate(rows, asof, weeks=4):
    """把已复核的档聚合成页面需要的全部结构。**每个统计块都带样本数。**"""
    reviewed = list(iter_reviewed(rows))
    total = len(rows)
    reviewed_rows = sum(1 for r in rows if row_reviewed_count(r) > 0)
    reviewed_tiers = len(reviewed)
    conf_unknown = 0

    def new_group():
        return {"all": HorizonStats(), "by_slot": {}, "by_conf": {}, "by_board": {}}

    def bucket(container, key):
        if key not in container:
            container[key] = HorizonStats()
        return container[key]

    groups = new_group()
    board_counts = {}
    week_map = {}

    for row, k, rev in reviewed:
        hs_all = groups["all"]
        hs_all.add(k, rev)

        slot = str(row.get("slot") or "").strip().lower()
        if slot not in ("am", "pm"):
            slot = "pm"          # 与 M9/M10 同一兜底口径：缺失/写坏一律按盘后记录
        bucket(groups["by_slot"], slot).add(k, rev)

        conf = normalize_confidence(row.get("confidence"))
        if conf is None:
            conf_unknown += 1
        else:
            bucket(groups["by_conf"], conf).add(k, rev)

        board = str(row.get("board") or "").strip()
        if board:
            board_counts[board] = board_counts.get(board, 0) + 1
            bucket(groups["by_board"], board).add(k, rev)

        d = parse_date(row.get("date"))
        if d:
            y, w, _mon = iso_week(d)
            wk = week_map.setdefault((y, w), HorizonStats())
            wk.add(k, rev)

    # 板块：只在出现次数 ≥ 2 时列出（1 次的板块是孤例，列出来只会让人以为"这板块准"）
    boards = [b for b, c in sorted(board_counts.items(), key=lambda kv: (-kv[1], kv[0]))
              if c >= 2]

    # 逻辑核查：**只数已核查的档**，null 一律不计入
    inv_sample = inv_triggered = inv_lucky = 0
    for _row, _k, rev in reviewed:
        v = normalize_verdict(rev.get("invalidation_check"))
        if v is None:
            continue
        inv_sample += 1
        if v != "triggered":
            continue
        inv_triggered += 1
        a = _f(rev.get("alpha"))
        if a is not None and a > 0:
            inv_lucky += 1

    # 最近 N 周（含 asof 当周；账本里更早的周不列，更晚的周不存在）
    week_rows = []
    if asof is not None:
        ay, aw, _am = iso_week(asof)
        items = sorted(week_map.items(), key=lambda kv: kv[0], reverse=True)[:weeks]
        for (y, w), hs in items:
            week_rows.append({
                "year": y, "week": w,
                "is_current": (y, w) == (ay, aw),
                "hs": hs,
            })

    return {
        "asof": asof,
        "total": total,
        "reviewed_rows": reviewed_rows,
        "reviewed_tiers": reviewed_tiers,
        "conf_unknown": conf_unknown,
        "hs": groups,
        "board_counts": board_counts,
        "boards": boards,
        "inv": {"sample": inv_sample, "triggered": inv_triggered, "lucky": inv_lucky},
        "weeks": week_rows,
        "rows": rows,
    }


# ------------------------------------------------------------------ 排版

def esc(s):
    return (str(s if s is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def pct(v):
    """收益/超额：正负分别上色，缺失写「—」（**不写 0%**）。"""
    v = _f(v)
    if v is None:
        return '<span class="nodata">—</span>'
    cls = "up" if v > 0 else ("down" if v < 0 else "flat")
    return f'<span class="{cls}">{v:+.2%}</span>'


def mean_txt(mean, n):
    """均值单元格：0 样本 → 「暂无」，有样本 → 数字 + 样本数（均值与 n 不许分家）。"""
    if mean is None or not n:
        return f'<span class="nodata">{NO_DATA}</span>'
    return f'{pct(mean)} <span class="muted">n={n}</span>'


def small_note(n):
    """样本 < 5 时的显式提示（样本充足则返回空串）。"""
    return f'<p class="warn">{SMALL_SAMPLE_NOTE}</p>' if n < MIN_SAMPLE else ""


def stat_table(hs_by_key, key_labels, caption, row_filter=None):
    """一张分组统计表：每行一个分组，T+1/T+3/T+5 各给双基准均值 + 样本数。"""
    head = "".join(
        f'<th class="num">T+{k}<br><span class="muted small">{BENCH_NAME}</span></th>'
        f'<th class="num">T+{k}<br><span class="muted small">{BENCH2_NAME}</span></th>'
        for k in REVIEW_DAYS)
    body, worst = [], None
    for key in key_labels:
        hs = hs_by_key.get(key)
        if hs is None:
            continue
        if row_filter and not row_filter(key, hs):
            continue
        tds = []
        for k in REVIEW_DAYS:
            tds.append(f'<td class="num">{mean_txt(hs.mean(k, "alpha"), hs.n(k, "alpha"))}</td>')
            tds.append(f'<td class="num">{mean_txt(hs.mean(k, "alpha2"), hs.n(k, "alpha2"))}</td>')
        ns = [hs.n(k, b) for k in REVIEW_DAYS for b in ("alpha", "alpha2")]
        n_row = max(ns or [0])       # 该行的代表样本数：两条基准里取多的那条
        if worst is None or n_row < worst:
            worst = n_row
        body.append(f'<tr><th scope="row">{esc(key_labels[key])}</th>{"".join(tds)}</tr>')
    if not body:
        return f'<p class="warn">本分组{NO_DATA}可统计的已复核档。</p>'
    return (f'<div class="table-wrap"><table><caption>{caption}</caption>'
            f'<thead><tr><th>分组</th>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>'
            # 样本是否充足按本表里样本**最少**的那一行判：只要有一行不够，
            # 就如实提示；这一行自己的 n 就印在每个数字旁边。
            + small_note(worst if worst is not None else 0))


def tier_line(rev):
    """逐条明细里的一档：收益 + 双基准超额 + 推翻核查结论（四态）。"""
    ret = rev.get("ret")
    parts = [pct(ret)]
    parts.append(f'<span class="small">超额 {BENCH_NAME} {pct(rev.get("alpha"))}'
                 f' · {BENCH2_NAME} {pct(rev.get("alpha2"))}</span>')
    v = normalize_verdict(rev.get("invalidation_check"))
    # VERDICT_*_CN 里已经带「推翻条件：」前缀（与 M5/M9 逐字一致），这里不再拼前缀
    if v is None:
        parts.append(f'<span class="small verdict-none">{VERDICT_NONE_CN}</span>')
    else:
        parts.append(f'<span class="small verdict-{v}">{VERDICT_CN[v]}</span>')
    span = rev.get("span")
    kind = "交易日" if rev.get("span_kind") == "trading" else "自然日"
    if isinstance(span, int) and span:
        parts.append(f'<span class="small muted">跨度 {span} {kind}</span>')
    status = str(rev.get("status") or "")
    if status and status != "ok":
        parts.append(f'<span class="small muted">状态 {esc(status)}</span>')
    return " ".join(parts)


def render_detail(rows):
    """逐条明细：**所有已复核的档**（含跑输、含逻辑破产），按日期倒序。

    跑赢跑输同版式同字号：只用颜色区分数字，不做任何加粗/放大/图标暗示。
    """
    dated = [(parse_date(r.get("date")), i, r) for i, r in enumerate(rows)]
    dated.sort(key=lambda t: (t[0] or date.min, t[1]), reverse=True)
    out, n_rows = [], 0
    for d, _i, row in dated:
        revs = [(k, (row.get("reviews") or {}).get(str(k))) for k in REVIEW_DAYS]
        revs = [(k, rv) for k, rv in revs if isinstance(rv, dict)
                and (_f(rv.get("alpha")) is not None or _f(rv.get("alpha2")) is not None)]
        if not revs:
            continue
        n_rows += 1
        slot = str(row.get("slot") or "").strip().lower()
        slot_label = SLOT_CN.get(slot, "盘后记录")
        conf = normalize_confidence(row.get("confidence")) or "—"
        tiers = "".join(
            f'<div class="tier"><span class="tier-k">T+{k}</span> {tier_line(rv)}</div>'
            for k, rv in revs)
        out.append(
            f'<div class="row">'
            f'<div class="row-head"><span class="row-date">{esc(row.get("date"))}</span>'
            f'<span class="row-slot">{esc(slot_label)}</span>'
            f'<b>{esc(row.get("name"))}</b>'
            f'<span class="muted small">置信度 {esc(conf)}</span></div>'
            f'{tiers}</div>'
        )
    if not out:
        return f'<p class="warn">账本里{NO_DATA}已复核的档。</p>', 0
    return "".join(out), n_rows


def render_weeks(week_rows):
    """最近 4 周：每周的 T+1 样本数与双基准均值（不足 5 条标出来）。"""
    if not week_rows:
        return f'<p class="warn">{NO_DATA}可统计的周。</p>'
    nodata = f'<span class="nodata">{NO_DATA}</span>'
    body = []
    for w in week_rows:
        hs = w["hs"]
        n1a, n1b = hs.n(1, "alpha"), hs.n(1, "alpha2")
        ns = [n for n in (n1a, n1b) if n]
        # 0 样本的周不给"样本不足"提示：那一条已经写成「暂无」，再叠一层提示是噪音。
        n_worst = min(ns) if ns else 0
        flag = SMALL_SAMPLE_NOTE if 0 < n_worst < MIN_SAMPLE else "—"
        label = f'{w["year"]}-W{w["week"]:02d}' + ("（当周）" if w["is_current"] else "")
        body.append(
            f'<tr><th scope="row">{label}</th>'
            f'<td class="num">{n1a if n1a else nodata}</td>'
            f'<td class="num">{mean_txt(hs.mean(1, "alpha"), n1a)}</td>'
            f'<td class="num">{n1b if n1b else nodata}</td>'
            f'<td class="num">{mean_txt(hs.mean(1, "alpha2"), n1b)}</td>'
            f'<td class="small">{flag}</td></tr>'
        )
    return (f'<div class="table-wrap"><table>'
            f'<caption>T+1 口径（其余档位见上面的累计总览）；'
            f'提示列按两条基准里较少的一条判。</caption>'
            f'<thead><tr><th>周</th>'
            f'<th class="num">T+1 样本<br><span class="muted small">{BENCH_NAME}</span></th>'
            f'<th class="num">T+1 均值<br><span class="muted small">{BENCH_NAME}</span></th>'
            f'<th class="num">T+1 样本<br><span class="muted small">{BENCH2_NAME}</span></th>'
            f'<th class="num">T+1 均值<br><span class="muted small">{BENCH2_NAME}</span></th>'
            f'<th>提示</th></tr></thead><tbody>{"".join(body)}</tbody></table></div>')


EXTRA_CSS = """
.row{border-bottom:1px solid var(--border); padding:10px 0;}
.row-head{display:flex; flex-wrap:wrap; gap:4px 10px; align-items:baseline; font-size:15px;}
.row-date{font-variant-numeric:tabular-nums; color:var(--muted);}
.row-slot{font-size:12.5px; color:var(--muted); border:1px solid var(--border); padding:0 5px;}
.tier{font-size:14px; margin:2px 0 2px 0;}
.tier-k{display:inline-block; min-width:34px; color:var(--muted); font-size:12.5px;}
/* 无 JS 兜底：万一将来有人给这一页加脚本又写坏了，至少不会整页空白 */
.noscript-bar{background:var(--bg2); border-bottom:1px solid var(--border);
  padding:6px 16px; font-size:13px; color:var(--muted); margin:0 8px;}
"""

# 自带 favicon（内联 SVG，data URI）。浏览器会**自动**请求 /favicon.ico，
# 这个页面没有它 → 控制台留下一条 404。这不是页面代码的错，但验收要求
# "无 JS 错误、无外部请求"，多一条红字容易让人误判，故显式给一个内联图标：
# 既是同一个"股"字的视觉延续，也**没有引入任何外部请求**。
FAVICON = (
    '<link rel="icon" href="data:image/svg+xml,'
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
    "%3Crect width='32' height='32' fill='%231e4d7a'/%3E"
    "%3Ctext x='16' y='23' font-size='20' text-anchor='middle' fill='%23f7f4ee'"
    "%3E%E8%82%A1%3C/text%3E%3C/svg%3E\">"
)


def build_page(agg, week_label, ledger_path, bad_lines, ledger_exists, generated_at):
    """整页 HTML。所有结论性文字都带样本数；0 样本写「暂无」。"""
    hs_all = agg["hs"]["all"]
    has_data = agg["reviewed_tiers"] > 0
    latest_week = agg["weeks"][0] if agg["weeks"] else None

    # 1) 累计总览
    head = "".join(
        f'<th class="num">T+{k}<br><span class="muted small">{BENCH_NAME}</span></th>'
        f'<th class="num">T+{k}<br><span class="muted small">{BENCH2_NAME}</span></th>'
        for k in REVIEW_DAYS)
    cells = "".join(
        f'<td class="num">{mean_txt(hs_all.mean(k, "alpha"), hs_all.n(k, "alpha"))}</td>'
        f'<td class="num">{mean_txt(hs_all.mean(k, "alpha2"), hs_all.n(k, "alpha2"))}</td>'
        for k in REVIEW_DAYS)
    n_bench_a = max([hs_all.n(k, "alpha") for k in REVIEW_DAYS] or [0])
    n_bench_b = max([hs_all.n(k, "alpha2") for k in REVIEW_DAYS] or [0])
    # 样本数按 T+1/T+3/T+5 逐档列出（两个基准各列一份）：
    # 只给一个总数会掩盖"两条基准的 n 不一样"这件事，而那正是这一节的重点。
    n_detail_a = "/".join(str(hs_all.n(k, "alpha")) for k in REVIEW_DAYS)
    n_detail_b = "/".join(str(hs_all.n(k, "alpha2")) for k in REVIEW_DAYS)
    overview = (
        f'<div class="kv">'
        f'<span>候选总条数 <b>{agg["total"]}</b></span>'
        f'<span>其中已复核过的候选 <b>{agg["reviewed_rows"]}</b> 条</span>'
        f'<span>已复核档位 <b>{agg["reviewed_tiers"]}</b> 档</span>'
        f'<span class="muted">样本数（T+1/T+3/T+5）：'
        f'{BENCH_NAME} {n_detail_a} · {BENCH2_NAME} {n_detail_b}</span>'
        f'</div>'
        f'<div class="table-wrap"><table>'
        f'<caption>全账本累计（截至 {esc(agg["asof"])} 记录的行）；'
        f'两条基准的样本数各自独立标注 —— 老账本没有中证1000 点位，n 天然更小。</caption>'
        f'<thead><tr><th>口径</th>{head}</tr></thead><tbody>'
        f'<tr class="total"><th scope="row">已复核档均值超额</th>{cells}</tr>'
        f'</tbody></table></div>'
        f'<p class="lead">每条候选按记录日之后第 1/3/5 个交易日补录；'
        f'「样本」= 该档里真的有该基准超额数字的条数，两条基准分开数。</p>'
        + small_note(max(n_bench_a, n_bench_b))
    )

    # 2) 按批次
    slot_tbl = stat_table(
        agg["hs"]["by_slot"],
        {"am": "盘前推荐（08:10 那批）", "pm": "盘后记录（收盘后那批）"},
        "盘前推荐 vs 盘后记录 —— 哪个更准，看这里的样本数与均值",
        row_filter=lambda k, hs: hs.any_sample())

    # 3) 按置信度
    conf_tbl = stat_table(
        agg["hs"]["by_conf"],
        {"高": "高置信度", "中": "中置信度", "低": "低置信度"},
        "高置信度是不是真的更准 —— 只列账本里出现过的档位",
        row_filter=lambda k, hs: hs.any_sample())
    # 认不出的置信度必须**显式说出来**（哪怕全表为空）：悄悄丢掉几档，
    # 读者会以为"账本里只有这几档"，而真相是那几档没法归类。
    if agg["conf_unknown"]:
        conf_tbl += (f'<p class="lead">另有 {agg["conf_unknown"]} 档的置信度字段是空的或'
                     f'认不出（未归入上面任何一档，也没有被塞进"中"）。</p>')

    # 4) 按板块（出现 ≥ 2 次才列）
    if agg["boards"]:
        board_tbl = stat_table(
            agg["hs"]["by_board"],
            {b: f'{b}（{agg["board_counts"][b]} 条候选）' for b in agg["boards"]},
            f'只列出现 ≥ 2 次的板块（出现 1 次的孤例不列：单条样本说明不了板块水平）',
            row_filter=lambda k, hs: hs.any_sample())
    else:
        board_tbl = (f'<p class="warn">{NO_DATA}出现 ≥ 2 次的板块'
                     f'（出现 1 次的板块刻意不列）。</p>')

    # 5) 逻辑核查
    inv = agg["inv"]
    if inv["sample"]:
        inv_html = (
            f'<div class="kv">'
            f'<span>已复核档数 <b>{inv["sample"]}</b></span>'
            f'<span>其中 <b>{inv["triggered"]}</b> 档逻辑破产（推翻条件疑似触发）</span>'
            f'<span>逻辑破产但价格仍跑赢（alpha&gt;0）<b>{inv["lucky"]}</b> 档</span>'
            f'</div>'
            f'<p class="lead">这类"蒙对"不算判断能力：判断逻辑已经被推翻，'
            f'价格跑赢只是恰好 —— 把它算进水平就是这套复盘最想避免的错觉。</p>'
        )
    else:
        inv_html = (f'<p class="warn">{NO_DATA}已核查的档'
                    f'（{agg["reviewed_tiers"]} 档已复核里，没有被做过推翻条件核查）。</p>')
    inv_html += (
        f'<p class="lead">口径：<b>只数 invalidation_check 非 null 的档</b>；'
        f'未核查的档（null）不计入任何核查口径 —— 没查过不等于没触发。</p>')

    # 6) 逐条明细
    detail_html, n_detail = render_detail(agg["rows"])

    # 7) 最近 4 周
    weeks_html = render_weeks(agg["weeks"])

    ledger_note = ("账本不存在（{p}）：本页只有说明，没有任何统计。").format(p=esc(ledger_path)) \
        if not ledger_exists else ""
    bad_note = (f'<p class="warn">账本里有 {bad_lines} 行无法解析（已跳过并如实计数，'
                f'没有猜、没有补）。</p>' if bad_lines else "")

    empty_note = ('' if has_data else
                  f'<p class="warn">账本里{NO_DATA}已复核的档：'
                  f'候选记录了 {agg["total"]} 条，但还没有任何一档到期回填'
                  f'（T+1/T+3/T+5 都要等对应交易日跑过复盘）。'
                  f'此时不给任何均值 —— 0 样本的均值是编出来的。</p>')

    week_name = f'{week_label[0]}-W{week_label[1]:02d}'

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>复盘看板 · {esc(week_name)}</title>
{FAVICON}
<style>{CSS}{EXTRA_CSS}</style>
</head>
<body>
<noscript><div class="noscript-bar">本页不需要 JavaScript：所有统计在生成时就算好了，
样式也全部内联。若看到这一行，说明您的浏览器禁用了脚本，本页照常可读。</div></noscript>
<header class="hero">
  <div class="kicker">STOCK NEWS DAILY · REVIEW BOARD</div>
  <h1>复盘看板 · {esc(week_name)}</h1>
  <div class="date">截至 {esc(agg["asof"])} 记录的行 · 生成于 {esc(generated_at)} ·
    ISO 周（周一为一周之始）· 同一周重跑覆盖同一文件</div>
</header>
<main>

<section class="block" id="overview">
  <h2>累计总览</h2>
  <p class="lead">这里回答"到现在为止我到底准不准"。所有数字都是<b>已复核</b>档位的
  实际回填值，没有预测、没有模型打分。</p>
  {empty_note}
  {ledger_note}
  {bad_note}
  {overview}
</section>

<section class="block" id="by-slot">
  <h2>按批次：盘前推荐 vs 盘后记录</h2>
  <p class="lead">盘前那批是收盘前给出的可执行观察清单（基准是昨收），
  盘后那批是收盘后的事后记录（基准是当日收盘价）—— 两者难度不同，混在一起算会失真。</p>
  {slot_tbl}
</section>

<section class="block" id="by-confidence">
  <h2>按置信度</h2>
  <p class="lead">回答"高置信度是不是真的更准"。置信度是当时自己给的，
  这一节的数字就是它的对账单。</p>
  {conf_tbl}
</section>

<section class="block" id="by-board">
  <h2>按板块</h2>
  {board_tbl}
</section>

<section class="block" id="invalidation">
  <h2>逻辑核查（推翻条件是否被触发）</h2>
  {inv_html}
</section>

<section class="block" id="detail">
  <h2>逐条明细<span class="muted small">　已复核 {n_detail} 条</span></h2>
  <p class="lead">按记录日倒序，列出<b>所有已复核的档</b> —— 跑输的、逻辑破产的都在里面，
  与跑赢的同版式同字号。收益率为未复权口径。</p>
  <div class="detail">{detail_html}</div>
</section>

<section class="block" id="weeks">
  <h2>最近 4 周</h2>
  {weeks_html}
</section>

</main>
<footer class="risk">
  <h3>看这个页面时要记住的三件事</h3>
  <ul>
    <li>{esc(NOTE_ABOUT_TEXT)}</li>
    <li>{esc(NOTE_SMALL_SAMPLE)}</li>
    <li>{esc(NOTE_NO_WINRATE)}</li>
    <li>{esc(NOTE_KEEP_LOSERS)}</li>
    <li>{esc(NOTE_NOT_ADVICE)}</li>
  </ul>
  <p style="margin-top:8px">数据来源：M10 候选账本 {esc(ledger_path)}（本页只读，不修改账本）。
  本页由脚本自动生成，仅供参考。<b>不是买入指令、不构成投资建议</b>；
  市场有风险，投资需谨慎，决策权归您本人。</p>
</footer>
</body>
</html>"""


# ------------------------------------------------------------------ 主流程

def summarize(agg):
    """一行摘要（样本数 / 均值 / 输出路径在调用处补）。"""
    parts = []
    for k in REVIEW_DAYS:
        a, b = agg["hs"]["all"].mean(k, "alpha"), agg["hs"]["all"].mean(k, "alpha2")
        na = agg["hs"]["all"].n(k, "alpha")
        nb = agg["hs"]["all"].n(k, "alpha2")
        fa = f"{a:+.2%}(n={na})" if a is not None else f"{NO_DATA}(n={na})"
        fb = f"{b:+.2%}(n={nb})" if b is not None else f"{NO_DATA}(n={nb})"
        parts.append(f"T+{k} {BENCH_NAME} {fa} / {BENCH2_NAME} {fb}")
    inv = agg["inv"]
    return (f"候选 {agg['total']} 条 / 已复核 {agg['reviewed_rows']} 条候选"
            f"（{agg['reviewed_tiers']} 档）/ " + "；".join(parts)
            + f"；已核查 {inv['sample']} 档（逻辑破产 {inv['triggered']}、其中蒙对 {inv['lucky']}）")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="M12 复盘看板：把 M10 候选账本聚合成一周一个快照的 HTML（只读账本）")
    ap.add_argument("--ledger", default=BASE_LEDGER, help=f"账本路径（默认 {BASE_LEDGER}）")
    ap.add_argument("--out-dir", default=BASE_OUT_DIR, help=f"输出目录（默认 {BASE_OUT_DIR}）")
    ap.add_argument("--asof", default=None,
                    help="快照截止日 YYYY-MM-DD（默认取账本最后一行的日期；不取墙上时钟）")
    ap.add_argument("--weeks", type=int, default=4, help="「最近 N 周」的 N（默认 4）")
    ap.add_argument("--no-latest", action="store_true",
                    help="不写 review-latest.html（只写周快照）")
    ap.add_argument("--quiet", action="store_true", help="只打印一行摘要")
    args = ap.parse_args(argv)

    from pathlib import Path

    ledger_path = Path(args.ledger)
    out_dir = Path(args.out_dir)
    sha_before = ledger_sha256(ledger_path)          # 只读验证：跑完必须不变

    rows, bad_lines, exists = load_ledger(ledger_path)
    scope = review_scope(rows, args.asof)
    agg = aggregate(scope["rows"], scope["asof"], weeks=max(1, args.weeks))
    asof = scope["asof"]

    if asof is None:
        week_label = iso_week(datetime.now().date())[:2]
        agg["asof"] = "（账本为空）"
    else:
        week_label = iso_week(asof)[:2]

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    html = build_page(agg, week_label, str(ledger_path), bad_lines, exists, generated_at)

    out_dir.mkdir(parents=True, exist_ok=True)
    # 幂等覆盖：同一 ISO 周永远写同一个文件名（**不加 -1/-2 后缀**）
    weekly_path = out_dir / f"review-{week_label[0]}-W{week_label[1]:02d}.html"
    weekly_path.write_text(html, encoding="utf-8")
    written = [weekly_path]
    if not args.no_latest:
        latest_path = out_dir / "review-latest.html"
        latest_path.write_text(html, encoding="utf-8")
        written.append(latest_path)

    sha_after = ledger_sha256(ledger_path)
    if sha_before != sha_after:
        print("[warn] 账本在本次运行中发生了变化 —— 本模块本应只读，请检查！",
              file=sys.stderr)
    if not args.quiet:
        print(f"[OK] 账本 {ledger_path}（{len(rows)} 行，坏行 {bad_lines}，"
              f"口径截至 {agg['asof']}）")
    for p in written:
        print(f"[OK] review -> {p} ({len(html)} bytes)")
    print(f"[summary] {summarize(agg)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
