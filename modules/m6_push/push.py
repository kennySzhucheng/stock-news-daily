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
    """从 M3 分析取市场情绪结论（三级兜底），取不到返回 ""。

    **同一判据在另外两处各有一份副本，改动必须三处同步、逐字同逻辑**：
      - modules/m5_report/report.py  :: extract_conclusion
      - modules/m9_web/aggregate.py  :: Bundle.conclusion
    调用方负责把「取不到」显式标记出来（本模块写"（⚠️ 未能从 M3 分析中解析出
    结论…）"）——**不得**再退回"暂无"型固定串（把解析失败说成"今天没有情绪
    判断"）：M3 明明有分析却显示"暂无"，会把解析故障伪装成"今天没有判断"
    （2026-09-28/10-04 的线上推送与总览卡都栽在这句话上）。

    级1  逐行剥掉成对 ** 后匹配 `^结论[：:]` + 正文（M3 有时写成
         `**结论：中性偏谨慎**——…`，加粗标记在行首，不剥就漏，故先剥加粗）；
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


# ---------------------------------------------------------------------------
# 重点新闻的「已确认」标记要带上来源清单
#
# 微信正文长度敏感（Server酱单条上限、手机屏幕都吃紧），所以最多列 3 家，
# 超出追加「等N家」（N 是来源总数）。与 M5 日报、M9 网页同一套判据 ——
# confirmed 表示至少两个独立出版方刊发过，列出来源读者才能自己看印证强度。
# 标题逻辑（32 字符上限）不在这里，也不受影响。
# ---------------------------------------------------------------------------
SOURCES_IN_PUSH = 3


def news_sources(n):
    """该新闻的全部报道来源：优先 sources，缺字段时退回自身 source（去重保序）。"""
    raw = n.get("sources") or []
    if not raw:
        raw = [n.get("source")]
    out = []
    for s in raw:
        s = s.strip() if isinstance(s, str) else ""
        if s and s not in out:
            out.append(s)
    return out


def news_families(n):
    """该新闻的**全部报道来源家族** = 今天的 sources ∪ 历史的 continuing.families。

    M2 的 `continuing.families` 是**历史记录**的来源家族，**不含今天这条自己的
    sources**（冻结契约），所以要报「N 源/家」必须自己把今天的并进去，否则会
    系统性少数一家。与 M5 日报（report.py::news_families）、M9 网页
    （aggregate.py::news_families）同一口径，三处各一份实现（模块各自独立运行）。
    """
    out = list(news_sources(n))
    c = n.get("continuing")
    if isinstance(c, dict):
        for f in (c.get("families") or []):
            f = f.strip() if isinstance(f, str) else ""
            if f and f not in out:
                out.append(f)
    return out


def push_verified_note(n):
    """「已确认 2 源：新浪财经、东方财富」/「待核实」——推送里的一句话形式。

    没有 sources 字段（旧数据）时退回「已确认」，绝不渲染成「已确认 0 源：」。
    家数把 M2 continuing.families（历史来源家族）并进来算（见 news_families）。
    """
    if n.get("verified") != "confirmed":
        return "待核实"
    srcs = news_families(n)
    if not srcs:
        return "已确认"
    note = f"已确认 {len(srcs)} 源：" + "、".join(srcs[:SOURCES_IN_PUSH])
    if len(srcs) > SOURCES_IN_PUSH:
        note += f"等{len(srcs)}家"
    return note


def push_continuing_note(n):
    """跨天持续标记的最短形式；**只在 confidence=high 时**给，其余返回 ""。

    微信正文长度敏感（Server酱），所以：
      · low（疑似同一事件）**不写** —— 弱结论不值得占字符，更不能写成像
        「持续关注第 N 天」那样让人以为已确认；
      · high 也只给最短的一句「（持续第 N 天）」（8 个字符）。
    """
    c = n.get("continuing")
    if not isinstance(c, dict):
        return ""
    try:
        days = int(c.get("days"))
    except (TypeError, ValueError):
        return ""
    if days < 1:
        return ""
    if str(c.get("confidence") or "").strip().lower() != "high":
        return ""
    return f"（持续第 {days} 天）"


def select_top_news(news, k=3):
    """按 **报道家数** > 确认优先 > 类别权重 > 情绪显著性 排序，去重后取前 k 条。

    2026-10-05 只**叠加**了一条排序键：独立报道家数多的优先（多家独立刊发 =
    这件事更重要）。原有三级判据（confirmed / 类别 / 情绪）原样保留在后面当
    平手判据 —— 不是替换，是加在最前面。
    """
    def score(n):
        verified = 1 if n.get("verified") == "confirmed" else 0
        cat = CAT_WEIGHT.get(n.get("category"), 1)
        senti = SENTI_WEIGHT.get(n.get("sentiment"), 1)
        return (len(news_families(n)), verified, cat, senti)
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

    先按「日期-时段」的约定名精确查找（同日盘前/盘后各一份，各自成文件）。
    精确名不存在时才回退到同日更早的那份并 warn —— 直接取 glob 结果的最后一份
    会在同日已有 pm 文件时把**盘前推送链到盘后报告**，内容对不上时段。
    文件全都不存在时返回约定名（不抛异常，链接 404 由读者端暴露）。
    """
    exact = REPORTS_DIR / f"{date_str}-{slot}.html"
    if exact.exists():
        return exact.name
    cands = sorted(REPORTS_DIR.glob(f"{date_str}-*.html"))
    if cands:
        fallback = cands[0].name          # 字典序即时段序：am 早于 pm
        print(f"[warn] 未找到 {date_str}-{slot}.html，日报链接回退到同日较早的 {fallback}")
        return fallback
    return f"{date_str}-{slot}.html"


def load_picks(date_str):
    """reports/picks/ledger.jsonl → {"today": [...], "stats": "...", "inv_stats": "..."}。

    读不到返回 None。

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
        # 今天的**两批都要取到**：08:10 盘前批（slot=am，基准昨收、带关注区间与触发
        # 条件）与收盘后那批（slot=pm）。这里刻意**不按 slot 过滤**，分组交给
        # build_picks_block —— 盘前的判断同样要出现在推送里。
        today = [r for r in rows if r.get("date") == date_str]

        # 汇总：**口径是整个账本**（不是只有今天那几行），盘前/盘后、今天/往期
        # 全部算进来 —— 样本量本来就只有个位数，再按日期切一刀就没法看了。
        # **均值必须与样本数一起给**，且不叫「胜率」—— 样本量小时百分比没有意义，
        # 二值的对/错也会抹掉「跑赢 0.1%」与「跑输 0.1%」的区别。
        #
        # 两个基准并列（2026-10-05）：候选偏中小盘 + 事件驱动，只用沪深300 会系统性
        # 高估水平，故同时给中证1000 口径。微信正文长度敏感（Server酱），所以
        # **图例只写一次**（"两值依次为相对沪深300、相对中证1000"），每档只多
        # 「 / +x.xx%」这几个字符——实测整段「已回填」比改造前只长 40~50 字符
        # （T+1/T+3/T+5 三档全有数据时最多约 +55，仍低于 +60 的预算）。
        bits = []
        has_bench2 = False
        for k in (1, 3, 5):
            revs = [(r.get("reviews") or {}).get(str(k)) for r in rows]
            revs = [v for v in revs if v and v.get("status") == "ok"]
            vals = [v["alpha"] for v in revs if v.get("alpha") is not None]
            vals2 = [v["alpha2"] for v in revs if v.get("alpha2") is not None]
            if not vals:
                continue
            has_bench2 = has_bench2 or bool(vals2)
            # 两个口径的样本数**分别报**：老账本行、bench2 缺失的档位只进第一个口径，
            # 相同时只写一个（不啰嗦），不同时把中证1000 口径的点名列出（不谎报）
            tail = f"样本 {len(vals)} 条"
            if len(vals2) != len(vals):
                tail += f"·{BENCH2_CN} 口径 {len(vals2)} 条"
            if len(vals) < 3:
                bits.append(f"T+{k} {tail}（不足 3 条不给均值）")
            elif vals2:
                avg2 = sum(vals2) / len(vals2)
                bits.append(f"T+{k} 均超额 {sum(vals) / len(vals):+.2%} / "
                            f"{avg2:+.2%}（{tail}）")
            else:
                bits.append(f"T+{k} 均超额 {sum(vals) / len(vals):+.2%}（{tail}）")
        stats = "　".join(bits)
        if has_bench2:
            # 顺序说明只此一处：每档的两个数按「相对沪深300 / 相对中证1000」排列
            stats = f"（超额依次为相对{BENCH_CN}、相对{BENCH2_CN}）{stats}"
        elif stats:
            # 有回填、却**一条中证1000 口径的样本都没有**（老账本，或 bench2 长期
            # 取不到）：也要把第二个口径的存在与现状说清楚，不能让读者以为
            # "超额只有一种口径"——那正是这次改造要消除的错觉。
            stats = (f"（超额双口径：相对{BENCH_CN} 与相对{BENCH2_CN}；"
                     f"本账本暂时只有 {BENCH_CN} 口径的样本）{stats}")

        # 推翻条件核查（2026-10-05）：逻辑已被推翻、价格却仍跑赢的那几条 —— **蒙对**。
        # 微信正文长度敏感（Server酱），故只加**一句短提示**：实测整块比改造前
        # 只长 ≤60 字符（见 INV_STAT_PREFIX + inv_stat_line 的构造）。
        # 口径与 M5/M9 一致：只在 verdict 非 null 的档里数 triggered，lucky = alpha>0。
        # sample 为 0（老账本 / 还没复核过任何一档）时**一个字符都不加**。
        sample = trig = lucky = 0
        for r in rows:
            for k in (1, 3, 5):
                rev = (r.get("reviews") or {}).get(str(k))
                if not isinstance(rev, dict):
                    continue
                ic = rev.get("invalidation_check")
                if not isinstance(ic, dict):
                    continue
                v = str(ic.get("verdict") or "").strip().lower()
                if v not in INV_VERDICTS:
                    continue
                sample += 1
                if v == "triggered":
                    trig += 1
                    if isinstance(rev.get("alpha"), (int, float)) and rev["alpha"] > 0:
                        lucky += 1
        inv_line = ""
        if sample:
            inv_line = (f'⚠️ {sample} 档中 {trig} 档逻辑破产'
                        f'（价格仍跑赢 {lucky} 档）——"蒙对"不计入判断能力')

        # 最近一批（供"今天没有产出推荐"的空态用）：优先**盘前（推荐）批**的最近日期，
        # 一条 am 都没有（老账本）时退回最近一条记录并把用词降级为"候选记录"。
        # 日期取不到就留空串/None，_latest_note 会整段不写。
        am_dates = [r.get("date") for r in rows
                    if str(r.get("slot") or "").strip().lower() == "am" and r.get("date")]
        all_dates = [r.get("date") for r in rows if r.get("date")]
        latest = str(max(am_dates)) if am_dates else (
            str(max(all_dates)) if all_dates else "")
        latest_days = None
        if latest:
            try:
                latest_days = (datetime.strptime(date_str, "%Y-%m-%d")
                               - datetime.strptime(latest, "%Y-%m-%d")).days
            except Exception:
                latest_days = None
        return {"today": today, "stats": stats, "inv_stats": inv_line,
                "latest": latest, "latest_days": latest_days,
                "latest_is_am": bool(am_dates),
                # 整本账本（纯附加键，2026-10-05）：连续推荐的「第 N 天」要用**整本
                # 账本**算，只看今天那一批算不出"连续"；账本内容与统计口径都不动。
                "rows": rows}
    except Exception as e:
        print(f"[warn] 候选账本读取失败（{str(e)[:50]}），推送不含候选块")
        return None


def load_picks_status(date_str, slot):
    """读 M10 本轮写下的候选状态 reports/picks/picks-<日期>-<时段>.json。

    冻结的接口（M10 侧产出，本模块只读）：
      {"date","slot","generated_at","gate_open","recorded","reason","detail"}
      gate_open=false 收盘闸门未开（非交易日/盘中）；recorded 为本轮新入账条数；
      reason=ok 表示正常；异常码 parse_failed/llm_error/all_rejected/empty。

    文件可能不存在（旧版 M10、M10 被跳过、恢复失败），故读不到返回 None，
    任何异常都退化成 None 并打一行 warn —— 绝不因为缺一个状态文件让推送失败。
    """
    path = REPORTS_DIR / "picks" / f"picks-{date_str}-{slot}.json"
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return data
    except Exception as e:
        print(f"[warn] 候选状态读取失败（{path.name}: {str(e)[:50]}），按缺失处理")
        return None


def _clip(s, n):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


# 候选为空时的原因码 → 中文（M10 状态文件的 reason 字段）。
# 判据是「账本里今天没有行」，与 am/pm 无关：09-30 盘后闸门已开、LLM 解析失败
# 0 条候选，却被写成"盘前不记录新候选"，把真实故障伪装成了正常调度。
PICKS_REASON_CN = {
    "parse_failed": "模型输出解析失败",
    "llm_error": "模型输出解析失败",
    "all_rejected": "候选未通过校验（如推翻条件缺失）",
    "empty": "无合格材料",
}


def _latest_note(latest, days=None, is_am=True):
    """「最近一批推荐是 X（N 天前）。」——账本里取不到日期时返回空串。

    空态必须回答"上一批是什么时候"，只说"本轮未产出"读者没法判断是休市、
    长假，还是流水线坏了。取不到日期（空账本/字段坏）就整段不写。
    """
    if not latest:
        return ""
    what = "推荐" if is_am else "候选记录"
    ago = f"（{days} 天前）" if days is not None else ""
    return f"最近一批{what}是 {latest}{ago}。"


def _picks_empty_note(slot, status, latest="", latest_days=None, latest_is_am=True):
    """今天 0 条候选时的那句话 —— 按状态文件与时段分派，pm 绝不说「盘前」。

    latest / latest_days / latest_is_am 来自 load_picks()：末尾统一追加
    「最近一批推荐是 …」，把"今天没有"与"上一批隔了多久"一起说清。
    """
    is_am = (slot or "").lower() == "am"
    if status is not None and status.get("gate_open") in (False, 0):
        if is_am:
            # 盘前闸门未开的两种情况，都要说清 —— 否则会被读成"盘前本来就不记录"：
            #   ① 今天休市（non_trading_day）② 最新报价已是今日盘中价（not_premarket）
            reason = str(status.get("reason") or "").strip()
            if reason == "non_trading_day":
                msg = "今天休市，不产出推荐；以下是跟踪中候选的表现。"
            elif reason == "not_premarket":
                msg = ("本轮不是盘前快照（已出现今日盘中报价），不记录盘前候选；"
                       "以下是跟踪中候选的表现。")
            else:
                msg = "本时段不记录新候选（昨收尚未结算或非交易时段）；以下是跟踪中候选的表现。"
        else:
            msg = "本时段为非交易时段，不记录新候选；以下是跟踪中候选的表现。"
    elif (status is not None and status.get("gate_open") in (True, 1)
            and status.get("recorded") == 0):
        # 闸门开着却一条没记 → 异常，必须点名原因，不能再伪装成调度正常
        reason = str(status.get("reason") or "").strip()
        cn = PICKS_REASON_CN.get(reason, reason) or "未给出原因"
        msg = f"⚠️ 本时段未产出新候选（原因：{cn}）——这是异常，已记入体检。"
    elif is_am:
        # 盘前**会**产出推荐（2026-10-04 起：用昨收价作基准记入账本），
        # 所以这句话不能说"盘前不记录" —— 那是旧口径，现在只会把"本轮真没产出"
        # 说成"按设计不记录"，正是这个项目反复踩过的静默故障形态。
        msg = "本轮未产出新的推荐；以下是跟踪中候选的表现。"
    elif status is not None:
        # 文件在、但缺 gate_open（旧版或写坏了）：不能谎称「未找到状态文件」
        msg = "本时段未产出新候选记录（候选状态信息不完整）；以下是跟踪中候选的表现。"
    else:
        msg = "本时段未产出新候选记录（未找到候选状态文件）；以下是跟踪中候选的表现。"
    return msg + _latest_note(latest, latest_days, latest_is_am)


# 盘前/盘后两批候选的分组小标题（与 M5 日报同一口径，文案从简以省 Server酱 长度）：
#   am = 08:10 那批**推荐**（「今日潜力个股（推荐）」），基准昨收，条目带关注区间/触发条件；
#   pm = 收盘后那批，基准当日收盘价。
# 只有一批时不加标题：没有可混淆的另一批，标题纯属多占一行推送长度。
SLOT_ORDER = ("am", "pm")
SLOT_GROUP_CN = {"am": "今日潜力个股（推荐）", "pm": "盘后（收盘复盘后记录）"}
# 两个基准的中文名（与 M10 picks.BENCH_NAME/BENCH2_NAME、M5 日报、M9 网页同一口径）。
# 为什么同时给两个：候选偏中小盘 + 事件驱动，只用沪深300 会系统性高估水平 ——
# 明细在候选块里只出现一次图例，正文长度因此只增加几十个字符。
BENCH_CN = "沪深300"
BENCH2_CN = "中证1000"
# 推翻条件核查的 verdict 枚举（与 M10 picks.INVALIDATION_VERDICTS、M5 日报同一口径）。
INV_VERDICTS = ("triggered", "not_triggered", "unclear")
# `logic` 的字段标签（与 M5/M9 同一口径）：am 行是推荐 → 「为什么推荐」；
# pm 行是收盘后的事后记录 → 「逻辑」。
PICK_LOGIC_LABEL = {"am": "为什么推荐", "pm": "逻辑"}
# 推荐批下方的**短**风险提示：微信正文长度敏感（Server酱），把 M5/M9 那句压成一句，
# 但「非买入指令」必须留着 —— 「推荐」这个词旁边不能没有这句。
PICK_RECOMMEND_NOTE = "⚠️ 以上为 AI 观察建议（含关注区间与触发条件），非买入指令、不构成投资建议。"


def pick_slot(row):
    """账本行的批次：只有显式 am 才算盘前，其余（旧数据没 slot / 写坏）一律按盘后。

    盘后是账本的历史默认值 —— 盘前通道是后加的，把缺失值当盘前会把整批老账本
    误标成「今日可执行」。
    """
    s = (row.get("slot") or "").strip().lower()
    return s if s in SLOT_ORDER else "pm"


# ---------------------------------------------------------------------------
# 连续推荐合并（2026-10-05，**只在展示层**，与 M5 日报 / M9 网页同一口径）
#
# 同一 name、相邻记录日相差 ≤ PICK_MERGE_GAP_DAYS 个自然日 → 同一次连续推荐，
# 合并成一条并以最新那条为代表；间隔更远的同名行是两次独立推荐，不合并。
# 账本一行不动，统计口径一行不动（「已回填」那段仍按账本每一行算）。
# ---------------------------------------------------------------------------
PICK_MERGE_GAP_DAYS = 3


def _iso_date(s):
    try:
        return datetime.strptime(str(s or ""), "%Y-%m-%d")
    except Exception:
        return None


def merge_pick_rows(rows):
    """显示层合并 → [{"row", "days", "first_date", "members"}]（详见 M5 同名函数）。"""
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
    """该行所处的连续推荐区间（用整本账本算）→ {"days", "first_date"}。

    **N 按「不同日期数」算，不按账本行数**（2026-10-05 修正）：同一天 am/pm 两批
    出现同一只票是常态，按行数算会把一天说成「连续第 2 天」。
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
            break
    return {"days": len(dates), "first_date": min(dates)}


def _mmdd(s):
    """2026-10-06 → "10-06"（推送正文里不写全年份）；取不到返回 ""。"""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(s or ""))
    return f"{m.group(2)}-{m.group(3)}" if m else ""


def pick_streak_note(all_rows, grp):
    """合并组 → 「（连续第3天推荐·首次10-06）」；只一天时返回 ""。

    微信正文长度敏感，所以是**最短形**：不加空格、不加「日」以外的字。
    """
    run = pick_run(all_rows, grp["row"])
    days = max(grp.get("days", 1), run.get("days", 1))
    if days < 2:
        return ""
    firsts = [x for x in (grp.get("first_date"), run.get("first_date")) if x]
    mm = _mmdd(min(firsts) if firsts else "")
    return (f"（连续第{days}天推荐·首次{mm}）" if mm
            else f"（连续第{days}天推荐）")


def _pick_lines(i, r, streak=""):
    """一条候选 → [标题行, 推翻信号行]。

    第 1 行的 `logic` 带**按 slot 分的标签**（与 M5 日报 / M9 网页同一口径）：
    `am` 行是推荐 → 「为什么推荐：」，`pm` 行是收盘后的事后记录 → 「逻辑：」。
    标签净增 5 / 3 个字符（远低于单条 60 字符的上限），logic 本身仍截断 60 字 ——
    微信正文长度敏感，完整理由在日报/网页里给。

    盘前行额外带「关注 区间 · 触发：条件」（这两个字段是「今日可执行」的全部
    依据，见 M10 冻结 schema）。区间原样给出、触发条件截断 30 字；两者都为空时
    一个字符都不追加 —— 不留「关注 」后面空着的半截标签。

    `streak` 是连续推荐的最短标记（同名的相邻记录日 ≤3 自然日的行已合并成这一条）；
    空串时一个字符都不加。
    """
    tag = "〔板块〕" if r.get("kind") == "board" else ""
    conf = f"（{r['confidence']}）" if r.get("confidence") else ""
    is_am = pick_slot(r) == "am"
    extra = ""
    if is_am:
        bits = []
        zone = (r.get("entry_zone") or "").strip()
        trig = (r.get("trigger") or "").strip()
        if zone:
            bits.append(f"关注 {zone}")
        if trig:
            bits.append(f"触发：{_clip(trig, 30)}")
        if bits:
            extra = "（" + " · ".join(bits) + "）"
    label = PICK_LOGIC_LABEL["am" if is_am else "pm"]
    return [f"{i}. **{r.get('name')}**{tag}{streak} {label}："
            f"{_clip(r.get('logic'), 60)}{conf}{extra}",
            f"   推翻信号：{_clip(r.get('invalidation'), 40)}"]


def build_picks_block(picks, slot="", status=None):
    """候选观察清单 → 推送正文的一段。picks 为 None（账本缺失）时返回 []。

    今天的候选**按批次分组**：推荐（am，今日潜力个股）在前、盘后（收盘复盘后记录）在后，
    两组都有时才各给一行小标题（只有一组时不加，省推送长度也不制造"另一组呢"的疑问）。
    **推荐那一组下面另给一句短风险提示**（PICK_RECOMMEND_NOTE）——「推荐」旁边
    必须有"非买入指令"，不能只在文末那一行里兜着。

    slot 为时段（am/pm），status 为 load_picks_status() 的结果（可为 None）；
    二者只影响「今天 0 条候选」时的措辞：账本里今天没有行既可能是盘前调度，
    也可能是闸门已开却选股失败，不能一律说成"盘前不记录"。
    picks 里的 latest / latest_days / latest_is_am 由 load_picks() 给出，
    用于空态里补一句"最近一批推荐是 X（N 天前）"。
    """
    if not picks:
        return []
    parts = ["", "### 🎯 候选观察清单（不是买入建议）"]
    today = picks.get("today") or []
    all_rows = picks.get("rows") or []       # 整本账本：连续推荐的"第 N 天"要跨天算
    if today:
        groups = [(s, [r for r in today if pick_slot(r) == s]) for s in SLOT_ORDER]
        groups = [(s, rs) for s, rs in groups if rs]
        for s, rs in groups:
            if len(groups) > 1:
                parts.append(f"**{SLOT_GROUP_CN[s]}**")
            # 连续推荐合并（显示层）：同一 name、相邻记录日 ≤3 自然日的行并成一条
            # （以最新那条为代表），标记最短形「（连续第N天推荐·首次MM-DD）」。
            # 账本与统计口径都不动；这里的"整本账本"只是用来算天数，不参与统计。
            for i, g in enumerate(merge_pick_rows(rs), 1):
                parts += _pick_lines(i, g["row"], pick_streak_note(all_rows or rs, g))
            if s == "am":
                parts.append(PICK_RECOMMEND_NOTE)
    else:
        # 本轮没有当日候选（盘前那轮还没跑 / 没有合格材料）；但**跟踪中的候选往往
        # 正好到期**，那段表现是这一条推送唯一的价值增量，不能只留一块空白
        parts.append(_picks_empty_note(slot, status, picks.get("latest") or "",
                                       picks.get("latest_days"),
                                       picks.get("latest_is_am", True)))
    if picks.get("stats"):
        parts.append(f"📊 已回填：{picks['stats']}")
    # 推翻条件核查的短提示（2026-10-05）：紧跟在"已回填"后面 —— 读者先看到超额，
    # 再看到"其中 N 档逻辑已经破产、价格却仍跑赢"，这个顺序才有冲击力。
    # ⚠️ 已在 load_picks 的文案里，这里不再重复一个；sample 为 0 时该字段不存在。
    if picks.get("inv_stats"):
        parts.append(picks["inv_stats"])
    parts.append("（候选为观察清单，非买入指令；历史表现不代表未来）")
    return parts


def build_digest(date_str, slot, sentiment, market_view, top_news, delay=None, picks=None,
                 picks_status=None):
    """拼推送正文。顺序：延迟提示（若有）→ 市场分析（情绪 + 关注板块）
    → 候选观察清单 → 重点新闻 → 日报链接。

    市场判断放最前，让用户不点开链接也能先拿到结论；新闻标题退居其次。
    候选块紧跟市场分析 —— 它是从那份分析派生出来的、可事后检验的一层。
    picks_status 是 load_picks_status() 的结果，交给候选块区分「盘前」与「选股失败」。
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

    parts += build_picks_block(picks, slot, picks_status)

    parts.append("")
    parts.append("### 📌 重点新闻")
    for i, n in enumerate(top_news, 1):
        title = extract_title(n.get("text", ""))
        cat = CAT_CN.get(n.get("category"), n.get("category"))
        senti = SENTI_CN.get(n.get("sentiment"), "")
        verified = push_verified_note(n)
        # 跨天持续标记：只在 confidence=high 时给最短的一句「（持续第 N 天）」；
        # 新事件没有 continuing 键 → 空串，一个字符都不加。
        parts.append(f"{i}. [{cat}] {title}（{senti}·{verified}）"
                     f"{push_continuing_note(n)}")

    return "\n".join(parts)


def load_analysis_status():
    """读 M3 写下的 data/analysis.status.json（分析内容契约）。读不到返回 None。

    这是 M3 → 送达体检 的唯一通道：M3 的状态文件落在 data/ 里（不上线），
    M6 顺手把它并进**公开上线**的 reports/status-<日期>-<时段>.json，
    体检就能核对「推送内容与报表内容是不是好的」，而不只是「送到了没有」。
    任何异常都退化为 None（宁可不报，也不能因此掐断推送）。
    """
    try:
        p = DATA_DIR / "analysis.status.json"
        if not p.exists():
            return None
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except Exception as e:
        print(f"[warn] 分析状态读取失败（{str(e)[:50]}），状态里不含内容面字段")
        return None


def write_push_status(date_str, slot, ok, detail, sentiment_ok=False, warn="",
                      analysis_status=None, news_count=None, quotes_count=None,
                      llm_batches=None, llm_failed_batches=None):
    """把推送结果落成 reports/status-<date>-<slot>.json，随日报一起上线。

    为什么需要：**推送失败是静默的** —— 日报照常生成、网页照常上线，只有微信
    收不到。Server酱免费额度只有 5 条/天，额度耗尽正是这种表现（长期笔记里记过）。
    M8 的送达体检（modules/m8_e2e/healthcheck.py）读这个文件，判断「该到的推送
    到底到了没有」—— 否则这种失败只能靠人碰巧发现。

    文件名以 status- 开头，不会被 M5 的索引页（glob *.html）或 M6 自己的
    resolve_report_name（glob <日期>-*.html）误取。写失败不影响推送与日报。

    **push_ok 只表示"推没推出去"**，内容坏了不改它（改了会让体检误判成推送失败）。
    内容问题由这两个附加字段承载（原有字段名不改，体检脚本依赖）：
      sentiment_ok: 结论是否从 M3 分析里解析到（未解析 → False）
      warn:         内容告警码，如 "sentiment_unresolved"；无问题时为空串
    注意：未推送的路径（如缺 SendKey）根本没解析分析，sentiment_ok 也是 False，
    体检应先看 push_ok，再看 warn。
    """
    try:
        REPORTS_DIR.mkdir(exist_ok=True)
        payload = {
            "date": date_str,
            "slot": slot,
            "push_ok": bool(ok),
            "push_detail": detail,
            "sentiment_ok": bool(sentiment_ok),
            "warn": warn or "",
            # 内容面字段（2026-10-04 新增，体检据此判断"送的是好的"）：
            # 没有 analysis.status.json 时一律为 null —— 体检对 null 不报警，
            # 这样旧版本 M6 写的状态文件不会被误判成内容有问题。
            "analysis_ok": (None if not analysis_status
                            else bool(analysis_status.get("ok"))),
            "analysis_chars": (None if not analysis_status
                               else analysis_status.get("chars")),
            "analysis_conclusion_ok": (None if not analysis_status
                                       else bool(analysis_status.get("conclusion_ok"))),
            "analysis_violations": (""
                                    if not analysis_status
                                    else "、".join(analysis_status.get("violations") or [])),
            "news_count": news_count,
            "quotes_count": quotes_count,
            "llm_batches": llm_batches,
            "llm_failed_batches": llm_failed_batches,
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
        # 内容面字段仍要如实：密钥缺失时 M3 分析通常是好的，若这里偷懒记 False，
        # 体检会在「推送失败」之外再多报一条"结论未解析"，把读者引向错误原因
        _sent_ok = False
        try:
            _sent_ok = bool(extract_sentiment(
                (DATA_DIR / "analysis.md").read_text(encoding="utf-8")))
        except Exception:
            _sent_ok = False
        write_push_status(_now.strftime("%Y-%m-%d"), _slot, False,
                          "SERVERCHAN_SENDKEY 未设置",
                          sentiment_ok=_sent_ok,
                          warn="" if _sent_ok else "sentiment_unresolved",
                          analysis_status=load_analysis_status())
        return

    structured = json.loads((DATA_DIR / "structured_news.json").read_text(encoding="utf-8"))
    analysis_md = (DATA_DIR / "analysis.md").read_text(encoding="utf-8")
    # 内容面：M3 的契约状态 + 新闻/行情条数（条数骤降是 M2/M4 静默失效最直观的信号，
    # 老体检只看"日报到没到"，这两类故障它一个都发现不了）
    analysis_status = load_analysis_status()
    news_count = len(structured.get("news") or [])
    # M2 的批次统计：`llm_failed_batches > 0` 表示"有一部分新闻没有结构化字段"
    # （GLM 失败，或 M2 的墙钟预算耗尽而跳过了剩余批次）。体检据此提醒，
    # 免得这种降级被读成"今天没什么新闻"。
    llm_batches = structured.get("llm_batches")
    llm_failed_batches = structured.get("llm_failed_batches")
    quotes_count = None
    try:
        _q = json.loads((DATA_DIR / "quotes.json").read_text(encoding="utf-8"))
        quotes_count = _q.get("count")
        if not isinstance(quotes_count, int):
            quotes_count = len(_q.get("quotes") or [])
    except Exception:
        pass

    now = datetime.now(CST)
    date_str = now.strftime("%Y-%m-%d")
    slot = os.environ.get("REPORT_SLOT", "").strip().lower()
    if slot not in ("am", "pm"):
        slot = detect_slot(now)
    os.environ["REPORT_SLOT"] = slot          # 让 delay_context 拿到最终时段
    delay = delay_context(now)

    sentiment = extract_sentiment(analysis_md)
    sentiment_ok = bool(sentiment)
    if not sentiment_ok:
        # 取不到就明说"没解析出来"，绝不拿"暂无"型固定文案糊过去 ——
        # 那会让用户以为今天没有判断，而 M3 其实给了完整分析
        print("[warn] M3 结论未解析")
        sentiment = ("（⚠️ 未能从 M3 分析中解析出结论，"
                     "请点开完整日报看「市场情绪概览」一节）")
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
    picks_status = load_picks_status(date_str, slot)
    desp = build_digest(date_str, slot, sentiment, market_view, top, delay,
                        picks=picks, picks_status=picks_status) + link_line

    ok, msg = send(title, desp, sendkey)
    if ok:
        print(f"[OK] 已推送到微信（{msg}）")
    else:
        print(f"[warn] 推送失败（{msg}），日报已生成不受影响")
    write_push_status(date_str, slot, ok, msg, sentiment_ok=sentiment_ok,
                      warn="" if sentiment_ok else "sentiment_unresolved",
                      analysis_status=analysis_status,
                      news_count=news_count, quotes_count=quotes_count,
                      llm_batches=llm_batches, llm_failed_batches=llm_failed_batches)
    # 成功与否都打印内容，便于核对
    print(f"     时段: {slot} / 关注板块 {len(market_view)} 个 / "
          f"候选 {len((picks or {}).get('today') or [])} 条 / "
          f"结论 {'已解析' if sentiment_ok else '未解析'}")
    print(f"     内容面: 新闻 {news_count} 条 / 行情 {quotes_count if quotes_count is not None else '?'} 只 / "
          f"分析契约 {('未记录' if not analysis_status else ('通过' if analysis_status.get('ok') else '未通过'))}")
    print(f"     标题: {title}")
    print(f"     正文:\n{desp}")


if __name__ == "__main__":
    main()
