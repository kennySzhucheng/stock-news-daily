# -*- coding: utf-8 -*-
"""
M10 候选清单与事后复盘模块 — 把 M3 的判断变成可检验的记录

输入: data/structured_news.json + data/analysis.md + data/quotes.json
      reports/picks/ledger.jsonl（上次运行留下的账本，工作流开头已从 gh-pages 恢复）
输出: reports/picks/ledger.jsonl（追加当日候选；回填到期候选的 1/3/5 日表现）

要点：
1. 候选由 DeepSeek 从 M3 已有的判断里挑（**不改 M3 的 prompt**，故 analysis.md
   的格式契约与那 5 个解析器都不受影响），产出机读 JSON
2. invalidation（推翻条件）必填：空话、缺失、或命中投资指令禁用词的条目
   **在代码里丢弃** —— 让"不构成投资建议"有结构支撑，而不只是句免责声明
3. **记录与打分共用一个收盘闸门**（`close_ready`）：判据是**行情自带的报价时刻**
   而非「现在几点」。缺了它，盘前 08:10 那次运行会拿昨天的收盘价去比昨天的基准价，
   把 T+1 写成 0%，而账本一写就不再改 —— 周六周日、节假日同理。看钟表分不出
   「盘前 08:10」与「延迟到 12:46 的盘前」，也认不出国庆节的周一，报价时刻可以。
   fail-closed：判定不了就不写，延后一天由「到期即补」自动吸收
3b. **盘前通道（am，2026-10-04 新增）**：盘前 08:10 那次运行产出「今日可执行观察
    清单」，基准价用**上一交易日收盘价**（新字段 base_date 记下基准日），
    因此它的 T+1 就是**记录日当天收盘** —— 当天 15:40 的 pm 运行正好补上
    （score_pending 的 d0 = base_date or date）。pm 通道一行未改。
    三条闸门全满足才记：① is_trading_day(今天)（**fail-closed**：判不出来按休市
    处理）；② 最新报价时刻停在上一交易日且 ≥15:00（报价日期=今天 → 这不是盘前
    快照，reason=not_premarket）；③ 材料齐（analysis.md + **当日** quotes.json，
    盘前的「现价」就是昨收）。盘前每条候选另有 entry_zone（基于昨收的观察区间）与
    trigger（今天看盘即可验证的触发条件），与 invalidation 一样在代码里逐条校验；
    解析不出区间的只清空该字段（区间是辅助），trigger 缺失或 invalidation <8 字
    则整条丢弃。**盘前清单只收沪深 A 股个股**（板块指数没有可比的昨收与触发条件）。
4. 记录当天锁下基准价 —— M4 只有实时行情、没有历史行情，事后补不了
   （盘前通道同理：它锁的是上一交易日收盘价，并写进 base_date 留痕）
5. 到期即补：target = 记录日**之后的第 k 个交易日**（日历口径见下），
   today >= target 且该档未填就填，记**实际**打分日（停牌会让它晚几天，如实反映）。
   各档独立判超期，不拿最早那档统一作废
5b. **一轮只补最早的那一档**：T+1/T+3/T+5 分三轮补，天然落在不同交易日、不同价格上。
   早期版本一轮把所有到期档位一次性补齐（同一轮只抓一次行情灌给所有档），跨长假时
   T+3 与 T+5 会挤在同一天、用同一个价格，实际只有约 2 个交易日跨度，却按两个样本
   各自计入均值（report.py / push.py 都是逐档独立统计）
6. 账本放 reports/picks/ —— 该目录随 gh-pages 自动跨天留存
   （daily.yml 部署 publish_dir 为 ./reports，开头 git archive 还原整棵树），
   无需 git commit 步骤、无需 artifact、无需改 .gitignore
7. 每轮结束都写机器可读状态 reports/picks/picks-<日期>-<时段>.json
   （gate_open / recorded / reason / detail），给 M6 推送与体检读；写失败只 warn
8. LLM 原文快照落 data/llm_candidates_raw.txt：整批 json.loads 失败时按花括号
   配平逐条抢救，一次字符级失误（如结构位的全角逗号）不会让整天样本归零
9. secid 为空的候选在 EXPIRE_AFTER_DAYS 宽限期内每轮重试解析（当日 quotes.json
   的 name 索引 → M4 内存缓存 → 与行情交叉核对过的 code_hint），超期才 no_quote
10. 账本坏行只在内存里隔离，save_ledger 按原位置原样写回 —— 不因一次重写而抹掉证据
11. 交易日历（trading_days）：东财沪深300日K线的日期序列 = 真实交易日序列（腾讯
    日K兜底），落缓存 reports/picks/trading_days.json；取不到则退化为「周一~周五
    − 内置 2026 休市表」的近似日历并打醒目 warn。due(k) 与 reviews[k]["span"] 都走
    这个日历 —— 「自然日 +1 天」在周末/长假上是错的（周五记录 T+1 会算成周六）

密钥来源: 环境变量 DEEPSEEK_API_KEY
"""
import argparse
import importlib.util
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE / "data"
PICKS_DIR = BASE / "reports" / "picks"          # 账本 / 板块缓存 / 机器可读状态文件
LEDGER_PATH = PICKS_DIR / "ledger.jsonl"
BOARD_CACHE_PATH = PICKS_DIR / "boards.json"    # 板块代码表缓存
# 交易日历缓存（与 boards.json 同目录，随 gh-pages 一起跨天留存）。
# 自检: probe() 会打印它的来源与最近几个交易日。
TRADING_DAYS_CACHE_PATH = PICKS_DIR / "trading_days.json"
RAW_DUMP_PATH = DATA_DIR / "llm_candidates_raw.txt"   # LLM 候选原文快照（诊断用）

CST = timezone(timedelta(hours=8))

BENCH_SECID = "1.000300"      # 沪深300，与个股同一端点同一字段结构，实测可用
BENCH_NAME = "沪深300"

# 板块指数：东财板块代码前缀 90（实测 secid=90.BK0447 可取到行情）。
# pz 服务端钳在 100（传 1000 也只回 100），且按 fid=f3 涨跌幅排序 ——
# 只取第一页 = 只有「当日涨幅前 100」的板块，半导体这类常态板块会整批漏掉
# （2026-10-01 --probe 实测三个常用板块全部未匹配），必须翻页取全。
BOARD_LIST_API = ("https://push2.eastmoney.com/api/qt/clist/get"
                  "?pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3"
                  "&fs=m:90+t:{t}&fields=f12,f14")
# M4._get_json 不打备用域名（那层循环在 fetch_quote 自己身上），故这里自己走一遍
BOARD_LIST_MIRROR = BOARD_LIST_API.replace("//push2.eastmoney.com", "//push2delay.eastmoney.com")
BOARD_TYPES = ("2", "3")      # 2=行业板块 3=概念板块

# ---------------------------------------------------------------- 交易日历常量
# 交易日历取数端点（东方财富沪深300日K线）：
#   https://push2his.eastmoney.com/api/qt/stock/kline/get
#     ?secid=1.000300&klt=101&fqt=1&beg=YYYYMMDD&end=YYYYMMDD
#     &fields1=f1,f2,f3&fields2=f51,f52
# 响应 data.klines 是 ["2026-09-30,4356.80", ...]，每行第一个字段即该交易日。
# 沪深300 每个交易日都有成交，所以它的 K 线日期序列就是 A 股交易日序列（实测
# 2026-09-15~10-04 返回 11 个日期，缺 09-25/10-01~10-02 等休市日，与交易所
# 2026 休市通知一致）。**只用 M4._get_json**，不新写 HTTP 客户端。
KLINE_API = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
             "?secid={secid}&klt=101&fqt=1&beg={beg}&end={end}"
             "&fields1=f1,f2,f3&fields2=f51,f52")
# 备用源：腾讯日K。与 fetch_any 的「东财优先、腾讯兜底」同一思路 —— 东财 push2his
# 会按 IP 限流（2026-10-04 本机连续几次取数后就开始 RemoteDisconnected，而腾讯源
# 同时完全正常），只有一条腿时日历会整块降级。
# 依据（2026-10-04 本机实测）：
#   GET 该 URL(param=sh000300,day,2026-09-01,2026-10-04,320,qfq)
#   → data.sh000300.day = [["2026-09-01","4618.730",...], ...]，21 个交易日，
#     与东财序列逐个相同（都缺 09-25 中秋）。整段休市/未来区间返回 day: []
#     （空列表 = "这几天没有交易日"，是有效答案，不是失败）。
#   **count 是"最多返回几根"，超出时从最早那段截断**（实测 2025-01-01 起、
#   count=300 只回到 2025-07-10），故 count 必须 ≥ 窗口自然日数，否则会留下缺口。
KLINE_API_TENCENT = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                     "?param={code},day,{beg},{end},{count},qfq")
TENCENT_BENCH_CODE = "sh000300"      # 与 BENCH_SECID=1.000300 同一个标的
TRADING_DAYS_PAD_DAYS = 45    # 每次联网向前多取的自然日天数（减少请求次数）
DUE_HORIZON_DAYS = 70         # due(k) 向后找交易日的自然日窗口（够 T+5 跨两个长假）
CAL_NET_MAX = 3               # 每进程最多联网几次（窗口不同才需要第二次；防失控重试）

REVIEW_DAYS = (1, 3, 5)       # 复盘周期
# 到期后仍取不到行情，放弃重试（防无限重试）。取 15 天而非 7：打分只在
# 「今日已收盘」时才发生（每个交易日一次机会），而春节/国庆能连休 9 个日历日，
# 7 天会让长假期间的档位在获得第一次机会之前就过期。
EXPIRE_AFTER_DAYS = 15

MAX_STOCKS = 4                # 候选数量上限（prompt 里也写了 2-4）
MAX_BOARDS = 2
MAX_CAND_TOKENS = 4000        # 与 M3 一致；2000 会在 JSON 中途截断，切出半截样本

# 盘前通道（am）：
PREV_DAY_LOOKBACK_DAYS = 30   # 求「上一交易日」时向前回看的自然日数（够跨春节/国庆）
# entry_zone 的合法形态：「两个数字 + 分隔符」。
# 分隔符取 ~ ～ - － – — 至 到（中英文/全半角都认，模型两种都会写）；
# 允许「约」前缀与「元」后缀（它们不影响"两个数字"这个信息量）。
ENTRY_ZONE_RE = re.compile(
    r"^([0-9]+(?:\.[0-9]+)?)\s*[~～\-－–—至到]\s*([0-9]+(?:\.[0-9]+)?)$")
# 盘前候选额外的禁用词（**只在 am 生效**，pm 的措辞检查保持原样）。
# M3.postcheck 覆盖了「建议买入/卖出/可以抄底/应该止损/马上买入」这类指令，
# 但没有覆盖承诺性措辞；这里补上，让「不得承诺」也有代码兜底而不是只有 prompt。
AM_EXTRA_BANNED = (r"满仓", r"梭哈", r"必涨", r"必跌", r"稳赚", r"包赚", r"无风险套利")


def _load(name, rel):
    """按路径载入兄弟模块。

    与仓库既有惯例一致（m9_web/ask.py 载入 m3_analyzer、aggregate.py 载入
    m5_report）：模块间靠文件系统耦合，无 __init__.py，用 importlib 绕开包结构。
    """
    path = BASE / rel
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M4 = _load("m4_quotes", "modules/m4_quotes/quotes.py")
M3 = _load("m3_analyzer", "modules/m3_analyzer/analyzer.py")


# ---------------------------------------------------------------- 时间与账本

def now_cst():
    return datetime.now(CST)


def detect_slot(now=None):
    """盘前 am / 盘后 pm。daily.yml 通过 REPORT_SLOT 注入，本地按小时兜底"""
    s = (os.environ.get("REPORT_SLOT") or "").strip().lower()
    if s in ("am", "pm"):
        return s
    return "am" if (now or now_cst()).hour < 12 else "pm"


class LedgerRows(list):
    """账本行列表。

    `bad` 里存**无法解析/无法打分**的原始行 (原始行号从 0 起, 原文)。它们只在
    内存里被隔离，save_ledger 写回时按原位置原样追加 —— 早期版本跳过坏行后
    整文件重写，等于把证据永久抹掉，事后连坏在哪都查不到。
    """

    def __init__(self, *args):
        super().__init__(*args)
        self.bad = []


def load_ledger(path):
    """读账本，并顺手做一次 schema 归一（缺失 reviews 补 null 档、坏行隔离）。

    归一不是洁癖：打分段（_has_due / score_pending）直接下标取 row["reviews"]
    与 row["date"]，一个缺字段的历史行会让**整轮打分**抛异常。缺 date 的行无从
    打分，归到 bad 里保留原文，不再进 rows。
    """
    rows = LedgerRows()
    if not path.exists():
        return rows
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        s = line.strip()
        if not s:
            continue
        try:
            r = json.loads(s)
        except json.JSONDecodeError:
            rows.bad.append((i, s))
            print(f"[warn] 账本第 {i + 1} 行 JSON 坏，已隔离并原样保留: {s[:60]}")
            continue
        if not isinstance(r, dict):
            rows.bad.append((i, s))
            print(f"[warn] 账本第 {i + 1} 行不是对象，已隔离并原样保留: {s[:60]}")
            continue
        rv = r.get("reviews")
        if not isinstance(rv, dict):
            rv = {}
            print(f"[warn] 账本第 {i + 1} 行缺 reviews，已补 null 档")
        for k in REVIEW_DAYS:
            rv.setdefault(str(k), None)
        r["reviews"] = rv
        if not r.get("date"):
            rows.bad.append((i, s))
            print(f"[warn] 账本第 {i + 1} 行缺 date，已隔离并原样保留: {s[:60]}")
            continue
        rows.append(r)
    return rows


def save_ledger(path, rows):
    """整文件重写（先写 .tmp 再 replace，避免半截文件）。

    内容没变就不动它：闸门关闭的那些运行（盘前、周末、节假日）本来就不该
    改账本，少一次覆盖窗口，也不会白白改掉 mtime（M9 的缓存靠 mtime 失效）。

    load_ledger 隔离出来的坏行按**原位置原样**写回（rows.bad）。这里没有选
    「跳过前备份成 ledger.bad.jsonl」：备份只是旁证，多一个没人清理的文件，
    而保留原行能保证下一次运行继续报同一处异常，账本这份唯一复盘样本也真正
    做到了「读不出来 ≠ 被删掉」。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    bad = dict(getattr(rows, "bad", None) or {})
    if bad:
        lines, it = [], iter(rows)
        for i in range(len(rows) + len(bad)):
            lines.append(bad[i] if i in bad else json.dumps(next(it), ensure_ascii=False))
    else:
        lines = [json.dumps(r, ensure_ascii=False) for r in rows]
    text = "\n".join(lines) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return True


# ---------------------------------------------------------------- 行情与基准

def fetch_any(secid):
    """东财优先，失败回退腾讯源。返回 (行情, 来源) 或 (None, None)。

    **不能直接调 M4.fetch_quote** —— 它只打东财主域名和 push2delay 两个地址，
    腾讯回退是在 M4 自己的 main() 里编排的。M10 单独要行情，必须自己做这条链，
    否则东财一限流，基准和复盘就整块失效（2026-09-24 本地实测：东财两个域名
    同时打不通、腾讯源正常，正是这个场景）。

    板块指数（90.BKxxxx）在腾讯源没有对应代码，_to_tencent_code 返回 None，
    故板块退回东财单源 —— 这是可接受的降级。
    """
    q = M4.fetch_quote(secid)
    if q and q.get("price"):
        return q, "eastmoney"
    q = M4.fetch_quote_tencent(secid)
    if q and q.get("price"):
        return q, "tencent"
    return None, None


def fetch_bench():
    """沪深300 → (点位, 报价字典)。取不到返回 (None, None)。

    只用腾讯源：它的报价自带 [30] 时刻，收盘闸门要靠它（东财 f43 无时刻字段）。
    且东财 push2 会按 IP 整段限流，而基准取数失败会让整轮 fail-closed。
    """
    q = M4.fetch_quote_tencent(BENCH_SECID)
    if not q:
        print("[warn] 沪深300 基准取数失败（腾讯源）")
        return None, None
    print(f"[OK] 基准 {BENCH_NAME} {q['price']} ({q['change_pct']}%) 源=tencent")
    return q["price"], q


def close_ready(bench_q, day):
    """今日是否已有可用的收盘价 → (bool, 说明)。

    **这是复盘正确性的总闸门。** 缺了它，盘前 08:10 那次运行会拿昨天的收盘价
    去比昨天的基准价，把 T+1 写成 0%，而账本一写就不再改 —— 周六、周日、
    节假日同理（它们都会拿到上一交易日的收盘价）。

    判据是**报价自带的时刻**，不是「现在几点」：看钟表分不出「盘前 08:10」
    与「延迟到 12:46 的盘前」，也认不出国庆节的周一。报价时刻则如实反映
    最后一次成交发生在什么时候。

    fail-closed：取不到时刻就**不记录也不打分**。写错的 0% 是永久的，
    而延后一天由「到期即补」自动吸收，代价小得多。
    """
    today8 = day.strftime("%Y%m%d")
    if not bench_q:
        return False, "基准行情取不到，无法确认今日是否收盘（宁可不写）"
    t = (bench_q.get("time") or "").strip()
    if not re.fullmatch(r"\d{12,14}", t):
        return False, f"行情源未给报价时刻（{t or '空'}），无法确认今日是否收盘"
    qdate, qhm = t[:8], t[8:12]
    hhmm = f"{qhm[:2]}:{qhm[2:]}"
    if qdate != today8:
        return False, f"最新报价停在 {qdate} {hhmm}，今日（{today8}）尚未交易"
    if qhm < "1500":
        return False, f"最新报价停在 {hhmm}，今日尚未收盘（盘中价不是收盘价）"
    return True, f"{qdate} {hhmm} 已收盘"


def premarket_ready(bench_q, day, prev_day):
    """是否处于「昨收已结算」的盘前快照 → (bool, 说明, reason)。

    **这是盘前通道（am）的总闸门**，与 close_ready 互补：close_ready 判「今天该
    不该在盘后记账」，本函数判「现在能不能拿上一交易日收盘价当基准记账」。

    判据同样是**行情自带的报价时刻**（只认形态：报价日期 == 上一交易日 且 ≥15:00）：
      · 报价日期 = 今天     → 已有盘中成交，这不是盘前快照（reason=not_premarket）
      · 报价日期 != 上一交易日 → 与交易日历对不上（数据停在更早的某天），同样不记
      · 上一交易日报价 <15:00 → 那天还没收盘，昨收还不存在（not_premarket）
    报不出准确形态就不记：写错的基准价会把 T+1/T+3/T+5 全部污染，而延后一天的
    代价只是「今天没有清单」（到期即补的复盘口径不受影响）。

    reason 只在返回 False 时有意义，取值 ∈ {"not_premarket", "gate_closed"}：
    判不出来（缺时刻字段/取不到基准行情）归 gate_closed，判出来"不是盘前"归
    not_premarket —— 后者是"这份文档今天不该产出"，前者是"证据不足"。
    """
    if not bench_q:
        return (False, "基准行情取不到，无法确认最新报价是否停在上一交易日（宁可不写）",
                "gate_closed")
    t = (bench_q.get("time") or "").strip()
    if not re.fullmatch(r"\d{12,14}", t):
        return False, f"行情源未给报价时刻（{t or '空'}），无法确认这是盘前快照", "gate_closed"
    qdate, qhm = t[:8], t[8:12]
    hhmm = f"{qhm[:2]}:{qhm[2:]}"
    today8, prev8 = day.strftime("%Y%m%d"), prev_day.strftime("%Y%m%d")
    if qdate == today8:
        return (False, f"最新报价已是今日 {hhmm}（盘中价，不是昨收）——本轮不是盘前快照",
                "not_premarket")
    if qdate != prev8:
        return (False, f"最新报价停在 {qdate} {hhmm}，不是上一交易日（{prev8}）的收盘",
                "not_premarket")
    if qhm < "1500":
        return (False, f"上一交易日（{prev8}）的报价停在 {hhmm}，那天尚未收盘",
                "not_premarket")
    return True, f"最新报价 {qdate} {hhmm} = 上一交易日（{prev8}）收盘，昨收已结算", ""


def am_gate(bench_q, day):
    """盘前通道（am）总闸门 → (ok, 说明, reason, base_date)。

    三条判据（全满足才 ok=true，也就是状态文件里的 gate_open=true=「能记录」）：
      ① is_trading_day(day)：今天开市才谈得上"今天可购入" —— fail-closed，
         判不出来按休市处理（reason=non_trading_day）
      ② 上一交易日可求：它就是 base_date（基准价所在的交易日，**用日历求，
         不是自然日 -1**）；求不出来 reason=gate_closed
      ③ premarket_ready：最新报价时刻正好停在上一交易日且 ≥15:00（昨收已结算）。
         报价日期=今天 → 已有盘中成交，这不是盘前快照（reason=not_premarket）；
         取不到报价时刻 → 判不出来（reason=gate_closed，fail-closed）

    返回的 base_date 是 "YYYY-MM-DD" 字符串（账本里就是这个类型）；它同时是 am 行
    复盘用的 d0，所以错了 T+1/T+3/T+5 全错，故宁可 ok=False 也不猜。
    """
    d = _plain_date(day)
    if not is_trading_day(d):
        return (False,
                f"{d} 是休市日（非交易日）—— 今天不能买入，不该产出「今日可购入清单」",
                "non_trading_day", None)
    prev = prev_trading_day(d)
    if prev is None:
        return (False, "上一交易日求不出来（交易日历不可用），盘前基准价无从锁定",
                "gate_closed", None)
    ok, why, reason = premarket_ready(bench_q, d, prev)
    return ok, why, (reason or ""), prev.strftime("%Y-%m-%d")


# ---------------------------------------------------------------- 交易日历
#
# 为什么不用「记录日 + k 天」：那是自然日。周五记录、T+1 会算成周六；国庆连休时
# T+3 与 T+5 会落在同一天、用同一个收盘价，实际只有约 2 个交易日跨度，却按两个
# 样本各自计入均值。故一切到期日都改走**真实交易日序列**。

# 内置的 A 股休市日表 —— **仅作降级用**（网络失败且无缓存时），且**只覆盖 2026 年**。
# 来源：上海/深圳/北京证券交易所 2026 年部分节假日休市安排通知（2025-12-22 发布）。
#   证券时报网 https://stcn.com/article/detail/3551896.html
#   新浪财经/中证网 https://finance.sina.com.cn/roll/2025-12-22/doc-inhcsrnz4424581.shtml
# 原文（A 股，非港股通）：
#   元旦 1/1(四)~1/3(六) 休市，1/4(日) 周末休市，1/5(一) 开市
#   春节 2/15(日)~2/23(一) 休市，2/14(六)、2/28(六) 周末休市，2/24(二) 开市
#   清明 4/4(六)~4/6(一) 休市，4/7(二) 开市
#   劳动 5/1(五)~5/5(二) 休市，5/9(六) 周末休市，5/6(三) 开市
#   端午 6/19(五)~6/21(日) 休市，6/22(一) 开市
#   中秋 9/25(五)~9/27(日) 休市，9/28(一) 开市
#   国庆 10/1(四)~10/7(三) 休市，9/20(日)、10/10(六) 周末休市，10/8(四) 开市
# 下面把整段休市区间逐个列出（含落在周末的日期 —— 近似日历本来就先跳周末，多列
# 无害；列全是为了这份表单看也是对的）。年份写死在日期里，故 2027 及以后不适用。
FALLBACK_HOLIDAYS = frozenset(
    d.strftime("%Y-%m-%d")
    for a, b in (
        ("2026-01-01", "2026-01-04"),
        ("2026-02-14", "2026-02-23"),
        ("2026-04-04", "2026-04-06"),
        ("2026-05-01", "2026-05-05"),
        ("2026-06-19", "2026-06-21"),
        ("2026-09-25", "2026-09-27"),
        ("2026-10-01", "2026-10-07"),
    )
    for d in [datetime.strptime(a, "%Y-%m-%d") + timedelta(days=i)
              for i in range((datetime.strptime(b, "%Y-%m-%d")
                              - datetime.strptime(a, "%Y-%m-%d")).days + 1)]
)
FALLBACK_HOLIDAY_YEARS = (2026,)   # 上表核实过的年份；其它年份只跳周末

# 进程内日历状态。
#   days    已取得的真实交易日（date 集合）
#   beg/end **已经查询过的自然日窗口**（不是"最后一个交易日"）—— 周末/节假日当天
#           没有 K 线，但那天确实查过了，用窗口判覆盖才不会天天重复联网
#   source  人类可读的来源说明（--probe 打印）
#   approx  已降级（本次进程内不再联网）
#   warned  降级 warn 只打一次
#   net_ok  已尝试联网的次数（每进程最多 CAL_NET_MAX；窗口不同才需要第二次）
#   dead    本次进程内已失败的取数源（东财限流时就别再等它第二次超时）
#   api     本次日历数据实际来自哪个源（写进缓存文件的 source 字段）
#   override 离线测试注入的假交易日序列（见 _inject_trading_days）
_CAL = {"days": set(), "beg": None, "end": None, "source": "", "api": "",
        "approx": False, "warned": False, "year_warned": False, "net_ok": 0,
        "dead": set(), "disk_loaded": False, "override": None}


def _plain_date(v):
    """date / datetime / "YYYY-MM-DD" → datetime.date（无时区）。"""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v), "%Y-%m-%d").date()


def _midnight(d):
    """date → CST 零点的 datetime（与 _date() 同类型，便于与 today 相减）。"""
    return datetime(d.year, d.month, d.day, tzinfo=CST)


def _today():
    return now_cst().date()


def _inject_trading_days(days):
    """**离线测试钩子**：注入假交易日序列（date/str 列表）后，所有日历查询只认它。

    传 None 恢复真实取数。注入被视为"真实日历可用"，故 due()/span 都按交易日口径走。
    """
    _CAL["override"] = None if days is None else sorted(_plain_date(d) for d in days)
    _CAL["approx"] = False
    _CAL["warned"] = False


def _cal_reset():
    """**离线测试钩子**：清空进程内日历状态（含注入与降级标记）。

    不动磁盘缓存；测试要避开真实缓存时自行把 TRADING_DAYS_CACHE_PATH 指到临时目录。
    """
    _CAL.update(days=set(), beg=None, end=None, source="", api="", approx=False,
                warned=False, year_warned=False, net_ok=0, dead=set(),
                disk_loaded=False, override=None)


def _cal_covers(lo, hi):
    """已查询过的自然日窗口是否覆盖 [lo, hi]（含端点）。"""
    return (_CAL["beg"] is not None and _CAL["end"] is not None
            and _CAL["beg"] <= lo and _CAL["end"] >= hi)


def _cal_load_disk():
    """读 TRADING_DAYS_CACHE_PATH。文件缺失/坏掉一律当作无缓存，不抛异常。

    days 为空也当作无缓存（宁可重取一次）：45 天窗口内必有交易日，空的 days 只可能
    来自损坏的文件，而"已覆盖却没数据"会让 due(k) 悄悄走近似口径。
    """
    _CAL["disk_loaded"] = True
    try:
        d = json.loads(TRADING_DAYS_CACHE_PATH.read_text(encoding="utf-8"))
        days = [_plain_date(s) for s in (d.get("days") or [])]
        beg, end = d.get("beg"), d.get("end")
        if not days or not beg or not end:
            return False
        _CAL["days"].update(days)
        _CAL["beg"], _CAL["end"] = _plain_date(beg), _plain_date(end)
        _CAL["api"] = str(d.get("source") or "")
        _CAL["source"] = (f"磁盘缓存 {TRADING_DAYS_CACHE_PATH}"
                          f"（updated_at={d.get('updated_at') or '?'}，"
                          f"源={_CAL['api'] or '?'}）")
        return True
    except Exception:
        return False


def _cal_save_disk():
    """写缓存（先 .tmp 再 replace）。失败只 warn —— 缓存是加速器，不是前提。"""
    try:
        TRADING_DAYS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": now_cst().strftime("%Y-%m-%d %H:%M:%S"),
            "source": _CAL["api"] or "unknown",
            "beg": _CAL["beg"].strftime("%Y-%m-%d"),
            "end": _CAL["end"].strftime("%Y-%m-%d"),
            "last_trading_day": (max(_CAL["days"]).strftime("%Y-%m-%d")
                                 if _CAL["days"] else None),
            "days": [d.strftime("%Y-%m-%d") for d in sorted(_CAL["days"])],
        }
        tmp = TRADING_DAYS_CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(TRADING_DAYS_CACHE_PATH)
    except Exception as e:
        print(f"[warn] 交易日历缓存写入失败: {type(e).__name__}: {str(e)[:60]}")


def _cal_parse_eastmoney(d):
    """东财 kline 响应 → date 列表；结构不符返回 None（区别于"有效空窗口"的 []）。"""
    data = d.get("data") if isinstance(d, dict) else None
    if not isinstance(data, dict):
        return None
    out = []
    for line in (data.get("klines") or []):
        try:
            out.append(_plain_date(str(line).split(",", 1)[0].strip()))
        except Exception:
            continue
    return out


def _cal_parse_tencent(d):
    """腾讯 fqkline 响应 → date 列表；结构不符返回 None。

    data.sh000300.day / qfqday 是 [[日期, 开, 收, 高, 低, 量, ...], ...]，日期在 [0]。
    整段休市或全在未来的窗口会返回 day: []，那是有效答案（返回 []）。
    """
    data = d.get("data") if isinstance(d, dict) else None
    node = data.get(TENCENT_BENCH_CODE) if isinstance(data, dict) else None
    if not isinstance(node, dict):
        return None
    for key in ("qfqday", "day"):
        rows = node.get(key)
        if isinstance(rows, list):
            out = []
            for row in rows:
                try:
                    out.append(_plain_date(str(row[0]).strip()))
                except Exception:
                    continue
            return out
    return None


def _cal_fetch(beg, end):
    """联网取 [beg, end] 的沪深300日K → 更新内存与磁盘缓存。成功返回 True。

    东财优先、腾讯兜底（两家的响应结构不同，各有一个解析器）。空窗口（整段休市，
    日期列表为空）也算**成功**：那正是"这几天没有交易日"的正确答案，必须把它记进
    已查询窗口，否则每个进程都会为这几天反复联网。
    与旧窗口重叠时取窗口并集并保留旧数据（接口万一截断也不丢历史）；不重叠（中间
    有空档）则丢弃旧数据，避免把没查过的空档当成"已覆盖"。
    """
    if end < beg:
        return False
    span = (end - beg).days + 10          # 自然日数是交易日的上界，保证不会被截断
    attempts = (
        ("eastmoney push2his kline",
         KLINE_API.format(secid=BENCH_SECID, beg=beg.strftime("%Y%m%d"),
                          end=end.strftime("%Y%m%d")),
         _cal_parse_eastmoney),
        ("tencent fqkline",
         KLINE_API_TENCENT.format(code=TENCENT_BENCH_CODE, beg=beg.strftime("%Y-%m-%d"),
                                  end=end.strftime("%Y-%m-%d"), count=span),
         _cal_parse_tencent),
    )
    days, api = None, ""
    for name, url, parse in attempts:
        if name in _CAL["dead"]:        # 本进程里已经失败过，不再白等一次超时
            continue
        try:
            d = M4._get_json(url)
        except Exception as e:
            _CAL["dead"].add(name)
            print(f"[warn] 交易日历取数失败（{name}）: {type(e).__name__}: {str(e)[:50]}")
            continue
        days = parse(d)
        if days is None:
            _CAL["dead"].add(name)
            print(f"[warn] 交易日历响应结构不符（{name}），换下一个源")
            continue
        api = name
        break
    if days is None:
        print("[warn] 交易日历所有取数源都失败")
        return False

    old_beg, old_end = _CAL["beg"], _CAL["end"]
    if old_beg is not None and old_end is not None and beg <= old_end and old_beg <= end:
        new_beg, new_end = min(old_beg, beg), max(old_end, end)
        keep = {x for x in _CAL["days"] if new_beg <= x <= new_end}
    else:
        new_beg, new_end, keep = beg, end, set()
    _CAL["days"] = keep | set(days)
    _CAL["beg"], _CAL["end"] = new_beg, new_end
    _CAL["api"] = api
    _CAL["source"] = (f"{api} 查询窗口 {new_beg}~{new_end}，本次 {len(days)} 个交易日")
    print(f"[OK] 交易日历取数 {beg}~{end} → {len(days)} 个交易日（源 {api}，"
          f"缓存窗口 {new_beg}~{new_end}）")
    _cal_save_disk()
    return True


def _cal_ensure(start, end):
    """确保真实日历覆盖自然日区间 [start, min(end, today)]；返回是否可用真实日历。

    顺序：内存 → 磁盘缓存 → 联网（每进程最多 CAL_NET_MAX 次）→ 降级。覆盖以
    **已查询窗口**为准，见 _CAL 注释。联网端点只到今天（K 线没有未来），故 end
    先与今天取小。

    联网成功一次必然覆盖本次请求（窗口就是按请求算出来的），因此不会为同一个区间
    反复请求；窗口不同（例如先算今天的 due，再补一条三个月前的老账本）才需要第二次。
    **取数失败即降级**，不做多次重试 —— 网络不通时等三次超时会把整轮拖垮。
    """
    if _CAL["override"] is not None:
        return True
    if _CAL["approx"]:
        return False
    capped = min(end, _today())
    lo = min(start, capped)
    if _cal_covers(lo, capped):
        return True
    if not _CAL["disk_loaded"]:
        _cal_load_disk()
        if _cal_covers(lo, capped):
            return True
    if _CAL["net_ok"] < CAL_NET_MAX:
        _CAL["net_ok"] += 1
        beg = lo - timedelta(days=TRADING_DAYS_PAD_DAYS)
        if _CAL["beg"] is not None:
            beg = min(beg, _CAL["beg"])
        if _cal_fetch(beg, capped):
            if _cal_covers(lo, capped):
                return True
            print(f"[warn] 交易日历覆盖不足：需要 {lo}~{capped}，"
                  f"实际 {_CAL['beg']}~{_CAL['end']}")
    _CAL["approx"] = True
    if not _CAL["warned"]:
        _CAL["warned"] = True
        print("[warn] ===== 交易日历不可用（联网失败且无可用缓存）→ 已降级为近似日历 =====")
        print("[warn] 近似规则：周一~周五，再减去内置的 A 股休市日表；"
              f"该表只核实过 {FALLBACK_HOLIDAY_YEARS[0]} 年（来源：沪深北交易所 "
              "2026 年休市通知），其它年份只跳周末。")
        print("[warn] 影响：due(k) 与 reviews[k]['span'] 可能与真实交易日不符，"
              "请优先修复网络/缓存。")
    return False


def _approx_days(start, end):
    """近似交易日：周一~周五 − FALLBACK_HOLIDAYS（仅 2026 年核实过）。

    两处用它：①真实日历整体不可用时的降级；②真实日历只到"今天"、而 due(k) 落在
    未来时的**接续**。②必须带上节假日表 —— 否则 2026-09-30 之后的 T+3 会被算成
    10-02（真值 10-09），等于把刚修掉的 bug 从后门放回来。
    """
    out, cur = [], start
    while cur <= end:
        if cur.weekday() < 5 and cur.strftime("%Y-%m-%d") not in FALLBACK_HOLIDAYS:
            out.append(cur)
        cur += timedelta(days=1)
    if not _CAL["year_warned"] and any(
            y not in FALLBACK_HOLIDAY_YEARS for y in range(start.year, end.year + 1)):
        _CAL["year_warned"] = True
        print(f"[warn] 近似日历的节假日表只到 {max(FALLBACK_HOLIDAY_YEARS)} 年，"
              f"{max(FALLBACK_HOLIDAY_YEARS) + 1} 年起的日期只跳周末、不跳节假日")
    return out


def _trading_days_ex(start, end):
    """(区间内交易日列表, 来源)，来源 ∈ {"real", "fallback"}。

    "real" = 真实 K 线（或测试注入）；"fallback" = 近似日历。span 需要区分这两者。
    """
    start, end = _plain_date(start), _plain_date(end)
    if end < start:
        return [], ("fallback" if _CAL["approx"] else "real")
    if _CAL["override"] is not None:
        return [d for d in _CAL["override"] if start <= d <= end], "real"
    if _cal_ensure(start, end):
        return [d for d in sorted(_CAL["days"]) if start <= d <= end], "real"
    return _approx_days(start, end), "fallback"


def trading_days(start_date, end_date):
    """区间 [start_date, end_date] 内的 A 股交易日，升序 datetime.date 列表。

    入参接受 date / datetime / "YYYY-MM-DD"（含端点）；end < start 返回 []。

    取数: 东方财富沪深300日K线（见 KLINE_API 的注释），只用 M4._get_json。
    缓存: TRADING_DAYS_CACHE_PATH = reports/picks/trading_days.json
          （与 boards.json 同目录；含 updated_at / source / beg / end / days）
          命中缓存且覆盖所请求区间时**不再联网**；--probe 会打印来源与最近交易日。
    降级: 联网失败且无可用缓存 → 周一~周五 − FALLBACK_HOLIDAYS（仅 2026 年核实），
          并打醒目 warn。降级只发生在"一个真实交易日都拿不到"时；真实日历可用但
          只到今天时，未来那段由 _approx_days 接续（due() 内部处理）。
    """
    return _trading_days_ex(start_date, end_date)[0]


def is_trading_day(day=None):
    """day（默认今天）是否**开市日**。**fail-closed**：判不出来就当作不是交易日。

    为什么需要它：假期/周末的 08:10 运行不能产出「今日可购入清单」——那天根本不能
    买。行情本身分不出这件事（上一交易日的收盘价在任何一天早上都长一个样），所以
    必须问日历。

    判定顺序：
      1. 测试注入的序列（_inject_trading_days）→ 直接看成员关系
      2. 真实交易日历（K 线）能回答该日 → 用真实答案
         "能回答"= 该日 **早于今天** 且已被查询窗口覆盖：过去的日期不在 K 线里，
         只可能是休市。**今天本身不算"能回答"** —— 盘前 08:10 时今天还没有 K 线，
         "查不到"绝不等于"休市"，必须走第 3 步，否则每天盘前都会判成休市。
      3. 今天 / 未来（K 线里没有）→ _approx_days（周一~周五 − 内置 2026 休市表）
      4. 兜底再判不出来（年份不在已核实的休市表覆盖范围内，或日历/入参坏掉）
         → False + 打印原因

    第 4 条是刻意的保守：近似日历只核实过 FALLBACK_HOLIDAY_YEARS，"周一~周五"在
    未核实年份会把春节/国庆算成交易日。**代价与处置**：进入未核实年份后，盘前
    通道会退化成"每天都判成休市"（状态文件 reason=non_trading_day，日志有醒目
    warn）—— 这是可见的、可修的，而按错误清单买入是不可修的。修法：把下一年度的
    休市安排加进 FALLBACK_HOLIDAYS / FALLBACK_HOLIDAY_YEARS（每年 12 月一次），
    或让交易日历缓存/联网可用。
    """
    if day is None:
        d = _today()
    else:
        try:
            d = _plain_date(day)
        except Exception as e:
            print(f"[warn] 交易日判定失败（入参 {day!r}）：{type(e).__name__}: "
                  f"{str(e)[:60]} → 按非交易日处理（fail-closed）")
            return False

    if _CAL["override"] is not None:
        ok = d in set(_CAL["override"])
        print(f"{'[OK]' if ok else '[warn]'} {d} "
              f"{'是' if ok else '不是'}交易日（测试注入的日历）")
        return ok

    if not _CAL["approx"]:
        days, kind = _trading_days_ex(d, d)
        if kind == "real":
            if days:
                print(f"[OK] {d} 是交易日（真实日历）")
                return True
            if d < _today():
                # 真实日历覆盖了这个过去的日期、里面没有它 → 确实休市
                print(f"[OK] {d} 不是交易日（真实日历：该日休市/周末）")
                return False
            print(f"[info] {d} 还没有 K 线（日历只到已发生的交易日），"
                  "改用近似日历判定")

    if d.year not in FALLBACK_HOLIDAY_YEARS:
        print(f"[warn] {d} 的节假日表未核实（内置表只覆盖 "
              f"{'/'.join(str(y) for y in FALLBACK_HOLIDAY_YEARS)} 年），"
              "无法判断它是不是休市日 → 按非交易日处理（fail-closed，盘前不记录）")
        return False

    ok = bool(_approx_days(d, d))
    if ok:
        print(f"[warn] {d} 按近似日历判定为交易日（周一~周五且不在内置休市表里）")
    else:
        print(f"[warn] {d} 不是交易日（近似日历：周末或内置休市日）")
    return ok


def calendar_kind(day=None):
    """这个日期是用**哪种口径**判定的？`real` / `approx` / `approx-unverified-year`。

    只给状态文件与体检用（不改变判定结果）：
      · `real`                   —— 真实交易日历（东财/腾讯日K）能回答该日；
      · `approx`                 —— 近似日历（周一~周五 − 内置休市表），该年份已核实；
      · `approx-unverified-year` —— 近似日历且**该年份未核实**：此时 is_trading_day
                                    按 fail-closed 一律返回"非交易日"，盘前通道会
                                    每天停摆。这是唯一会"静默失效"的地方，必须让
                                    体检看得见（healthcheck.py 会因此报严重）。
    """
    d = _plain_date(day) if day is not None else _today()
    if _CAL.get("override") is not None:
        return "override"
    try:
        days, kind = _trading_days_ex(d, d)
        if kind == "real" and d < _today():
            return "real"
    except Exception:
        pass
    if d.year not in FALLBACK_HOLIDAY_YEARS:
        return "approx-unverified-year"
    return "approx"


def prev_trading_day(day=None, lookback=None):
    """day **之前最近的一个交易日**（严格早于 day），没有则 None。

    用交易日历求，**不能用自然日 -1**：10-09（周五）的上一交易日是 10-08，
    而 10-12（周一）的上一交易日是 10-09（跨周末），长假后更是差好几天。
    盘前行用它写 base_date（基准价所在的交易日），错了整条记录的 T+1 就错了。

    回看窗口 lookback 个自然日（默认 PREV_DAY_LOOKBACK_DAYS=30，够跨春节/国庆）；
    真实日历不可用时自动退化为近似日历（_trading_days_ex 内部处理，已打 warn）。

    窗口里落在**未来**的那一段（day > 今天时必然存在）K 线还没有，必须用
    _approx_days 接续 —— 否则 10-09 的"上一交易日"会答成 09-30（K 线的最后一根），
    而正确答案是 10-08。生产路径只对"今天"调用本函数（窗口全在过去），这一条是为
    --probe 与测试里的未来日期准备的。
    """
    d = _plain_date(day) if day is not None else _today()
    lo = d - timedelta(days=lookback or PREV_DAY_LOOKBACK_DAYS)
    hi = d - timedelta(days=1)
    if hi < lo:
        return None
    days, kind = _trading_days_ex(lo, hi)
    if kind == "real" and hi > _today() and _CAL["override"] is None:
        # 未来那一段用近似日历接续（注入的日历不接续：它本身就是测试里的"全部真相"）
        days = sorted(set(days)
                      | set(_approx_days(max(lo, _today() + timedelta(days=1)), hi)))
    if kind == "real" and not days:
        # 真实日历把这一整段都判成休市（不可能有 30 天连休）：宁可退回近似日历，
        # 也不要让 base_date 变成 None 把整条盘前通道掐掉
        print(f"[warn] 真实日历在 {lo}~{hi} 内没有任何交易日（异常），改用近似日历求上一交易日")
        days = _approx_days(lo, hi)
    return days[-1] if days else None


def _span_of(base, done):
    """基准日 → 补录日的跨度：真实日历可用时为**交易日数**，否则自然日数。

    口径：不含 base、含 done（记录日之后的第 k 个交易日补上 → span == k）。
    返回 (int, "trading"/"natural")；"natural" 表示日历不可用已退化，展示层据此说明。

    生产路径里 done 恒为 today（补录只发生在今天）。若调用方传了**未来**的 done，
    真实 K 线不可能有那段数据，此时不谎报"交易日"口径，退回自然日跨度。
    """
    base, done = _plain_date(base), _plain_date(done)
    days, kind = _trading_days_ex(base + timedelta(days=1), done)
    if kind == "real" and (done <= _today() or _CAL["override"] is not None):
        return len(days), "trading"
    return max((done - base).days, 0), "natural"


def due(k, base=None):
    """基准日**之后的第 k 个交易日** → CST 零点 datetime（k 为 T+1/T+3/T+5 的 1/3/5）。

    基准日 base 默认取今天；调用方（打分/复盘）显式传记录日 d0，因此
    「第 1 个交易日」通常就是记录的次日 —— 记录发生在当日收盘后，当天不算。
    返回 datetime 而非 date：与 today 同类型，(today - due).days 的既有用法不变；
    _review 再把它格式化成 "YYYY-MM-DD" 写进账本的 due 字段（schema 不变）。

    K 线只有已发生的交易日，base 之后的已知交易日不足 k 个时，未来那段用
    _approx_days（周一~周五 − 2026 休市表）接续 —— 这是跨长假时"该等到哪天"的判据。
    真实日历整体不可用则全程走 _approx_days（_cal_ensure 已打过 warn）。
    """
    base_d = _plain_date(base) if base is not None else _today()
    lo = base_d + timedelta(days=1)
    hi = base_d + timedelta(days=DUE_HORIZON_DAYS)
    days, kind = _trading_days_ex(lo, hi)
    if kind == "fallback":
        days = _approx_days(lo, hi)
    if len(days) >= k:
        return _midnight(days[k - 1])
    anchor = days[-1] if days else base_d
    ext = _approx_days(anchor + timedelta(days=1), hi)
    need = k - len(days)
    if len(ext) >= need:
        return _midnight(ext[need - 1])
    # 理论上到不了（hi 留了 70 天）；真到了就按自然日硬推，绝不抛异常
    return _midnight((ext[-1] if ext else anchor) + timedelta(days=need - len(ext)))


def _load_board_cache():
    try:
        d = json.loads(BOARD_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {k: v for k, v in d.items()
            if isinstance(v, dict) and v.get("code") and v.get("secid")}


def _save_board_cache(board_map):
    try:
        BOARD_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = BOARD_CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(board_map, ensure_ascii=False, sort_keys=True),
                       encoding="utf-8")
        tmp.replace(BOARD_CACHE_PATH)
    except Exception as e:
        print(f"[warn] 板块缓存写入失败: {str(e)[:50]}")


def build_board_map():
    """板块名 → {code, secid}。

    板块代码表几乎不变，但东财会抽风（实测 push2 与 push2delay 同时拒连）。若本次
    抓不到，当天所有板块候选的 secid 都会是 None —— 而账本行只写一次、事后不补，
    板块复盘就永久缺一块。故：抓到的结果落缓存，抓不到则退回上一次的缓存。
    两者都没有（首次运行恰逢接口故障）才返回空字典，此时板块候选降级为只列不计分。
    """
    cached = _load_board_cache()
    fresh = {}
    for t in BOARD_TYPES:
        # 服务端每页最多 100（pz 钳制），逐页取到末页；total 在第一页的
        # data.total 里，比 len 多一层保险（接口抽风返回空页时也能停）
        rows, total = [], None
        for pn in range(1, 21):
            d, err = None, None
            for api in (BOARD_LIST_API, BOARD_LIST_MIRROR):
                try:
                    d = M4._get_json(api.format(t=t, pn=pn))
                    break
                except Exception as e:
                    err = e
            if d is None:
                if pn == 1:
                    print(f"[warn] 板块列表 t={t} 取数失败（含备用域名）: {str(err)[:50]}")
                else:
                    print(f"[warn] 板块列表 t={t} 第 {pn} 页失败，用已取到的 {len(rows)} 条")
                break
            page = (d.get("data") or {}).get("diff") or []
            rows.extend(page)
            total = (d.get("data") or {}).get("total") or total
            if len(page) < 100 or (total and len(rows) >= total):
                break
            time.sleep(0.3)
        for r in rows:
            code, name = r.get("f12"), r.get("f14")
            if code and name:
                fresh.setdefault(name, {"code": code, "secid": f"90.{code}"})
        print(f"[OK] 板块列表 t={t}: {len(rows)} 个" + (f"/共 {total}" if total else ""))
    if fresh:
        merged = dict(cached)
        merged.update(fresh)          # 缓存打底，本次结果覆盖（只抓到一类也不丢另一类）
        _save_board_cache(merged)
        return merged
    if cached:
        print(f"[OK] 板块列表改用缓存: {len(cached)} 个（东财本次不可用）")
    return cached


def _joint_len(a, b):
    """a 与 b 的词根接合长度：a 的后缀与 b 的前缀相同、或反之，取最长的 k（k≥2）。

    「AI算力」与「算力概念」共享的是**词根**「算力」（一为后缀、一为前缀），
    这是 LLM 拼板块名最常见的形状，双向子串却抓不到。只认首尾相接而不认任意
    公共子串，是为精度：「量子计算」与「云计算」只共中间的「计算」，不是同一个板块。
    """
    for k in range(min(len(a), len(b)), 1, -1):
        if a[-k:] == b[:k] or a[:k] == b[-k:]:
            return k
    return 0


def _match_one(name, board_map):
    """单个板块名 → 板块指数。三级：精确 → 双向子串 → 词根接合。"""
    if name in board_map:
        return board_map[name]
    hits = [k for k in board_map if name and (name in k or k in name)]
    if hits:                       # 命中多个取名字最短的（「半导体」优先于「半导体设备」）
        return board_map[min(hits, key=len)]
    joints = [(_joint_len(name, k), k) for k in board_map]
    joints = [(j, k) for j, k in joints if j]
    if joints:                     # 接合最长者优先，同长取板块名最短的
        return board_map[min(joints, key=lambda jk: (-jk[0], len(jk[1])))[1]]
    return None


def match_board(name, board_map):
    """板块名 → 板块指数。

    「AI算力/服务器产业链」这类斜杠合称在板块上是合法的（东财自己的命名风格就是
    如此），故整体匹配不上时再拆开逐段试一次。
    """
    if not board_map:
        return None
    n = name.strip()
    if not n:
        return None
    hit = _match_one(n, board_map)
    if hit:
        return hit
    for part in (p.strip() for p in n.split("/")):
        if part:
            hit = _match_one(part, board_map)
            if hit:
                return hit
    return None


# ---------------------------------------------------------------- LLM 选股

def is_sh_sz_a(code):
    """是否沪深 A 股 6 位代码：6 开头（沪）/ 0、3 开头（深）。

    排除：北交所（4/8 开头）、沪 B（9 开头）、深 B（2 开头）、
    港股（5 位数字）、美股（字母）。与 M9 持仓页的 pfTencentCode 判据一致。
    """
    return bool(re.fullmatch(r"[603]\d{5}", code or ""))


PROMPT_HEAD = """你是 A 股研究助理。下面是今日新闻摘要、一份已完成的市场分析、以及相关个股的当日行情。

请从中挑出 2-{max_stocks} 只个股 + 0-{max_boards} 个板块，作为「今日候选观察清单」。

## 硬约束（违反即废稿）
1. 【只能从给定材料里选】候选必须出自市场分析的「值得关注的板块」或「新闻涉及的个股」，
   不得引入材料里没有的标的。**没有够格的标的时宁可比 2 只还少，也不要凑数。**
2. 【单一标的】name 必须是单一股票名或单一板块名。
   个股：禁止 "A/B" 斜杠合称（如「零跑科技/零跑汽车」）、禁止「等」、禁止并列。
   板块：可以用板块的通用叫法（「AI算力/服务器产业链」这种斜杠写法可以接受），
   但必须是一个主题，不能把两个不相干的板块拼在一起。
3. 【必须给出推翻条件】invalidation 必填，且必须是**可观测的证伪信号**
   （如「若公司公告否认该传闻」「若板块成交额连续两日萎缩」）。
   「注意风险」「谨慎参与」「关注后续」这类空话一律不合格。
4. 【禁止投资指令】不得出现「建议买入」「建议卖出」「可以抄底」「应该止损」
   「马上买入」等表述。用「纳入观察」「值得跟踪」。
5. 【置信度】每条标 高/中/低。单源待核实(unve)新闻支撑的最高只能给「中」。
6. 【依据编号】basis_refs 列出支撑该候选的新闻编号（就是下面清单里的 [n]）。
7. 【只选沪深 A 股】个股必须是沪深 A 股：沪市 6 开头、深市 0 或 3 开头的
   6 位数字代码。港股、美股、北交所（4/8 开头）、B 股一律不选，
   即使它们出现在行情清单或新闻里 —— 下面的行情清单已按此过滤。
8. 【小资金偏好】用户资金规模小：依据强度相当时**优先单价更低的个股**
   （一般 20 元以下优先）；单价高的只有依据明显更强时才入选。

## 输出格式
只输出 JSON，不要任何解释文字，不要 markdown 围栏：
{{"candidates":[{{"kind":"stock","name":"","code_hint":"","board":"",
  "logic":"一句话逻辑链","invalidation":"可观测的证伪信号","confidence":"中","basis_refs":[1,2]}}]}}

kind 取 "stock" 或 "board"；code_hint 只在个股且你知道沪深 A 股 6 位代码时填，否则留空。

## 已完成的市场分析
{analysis}

## 相关个股当日行情
{quotes}

## 今日新闻（共 {digest_count} 条精选，编号@[]用于引用）
{digest}"""


def build_prompt(digest, digest_count, analysis_md, quotes):
    # 行情清单只给沪深 A 股：新闻里偶尔会带港股/美股，喂进去就是给 LLM 递刀
    # （prompt 第 7 条要求它不选，但材料里根本不出现才最稳）。
    a_quotes = [q for q in quotes if is_sh_sz_a(str(q.get("code") or ""))]
    q_lines = "\n".join(
        f"- {q['name']}({q.get('code','')}) 现价 {q.get('price')} 涨跌 {q.get('change_pct')}%"
        for q in a_quotes[:60]) or "（今日未取到行情）"
    return PROMPT_HEAD.format(
        max_stocks=MAX_STOCKS, max_boards=MAX_BOARDS,
        analysis=analysis_md[:12000], quotes=q_lines,
        digest_count=digest_count, digest=digest)


# 盘前版 prompt（2026-10-04 新增）。与 PROMPT_HEAD 同一骨架（同一份材料、同样的
# 编号引用与禁用词约束），差别只有三处，都来自「盘前要能指导今天怎么做」：
#   ① 只挑个股（板块指数给不出"昨收 → 观察区间 → 今日触发"这条链）
#   ② 每条多两个可执行字段：entry_zone（基于昨收的观察区间）+ trigger（今天看盘
#      即可验证的触发条件）；invalidation 仍必填
#   ③ 明确告诉它"行情里的现价就是上一交易日收盘价"，否则它会拿昨收当"今日价"，
#      entry_zone 会整整偏一天
AM_PROMPT_HEAD = """你是 A 股研究助理。现在是**开盘前**。下面是今日新闻摘要、一份已完成的市场分析、以及相关个股的行情（**行情里的"昨收"就是上一交易日收盘价，今天还没有成交**）。

请从中挑出 1-{max_stocks} 只个股，作为「今日观察清单」——每条都要让读者在**今天开盘后自己盯着盘面**判断这条逻辑还成不成立、值不值得继续跟。

## 硬约束（违反即废稿）
1. 【只能从给定材料里选】候选必须出自市场分析的「值得关注的板块」或「新闻涉及的个股」，
   不得引入材料里没有的标的。**没有够格的标的时宁可比 1 只还少，也不要凑数。**
2. 【只选沪深 A 股个股】必须是沪市 6 开头、深市 0 或 3 开头的 6 位数字代码。
   港股、美股、北交所（4/8 开头）、B 股、板块指数一律不选（行情清单已按此过滤）。
3. 【单一标的】name 必须是单一股票名：禁止 "A/B" 斜杠合称、禁止「等」、禁止并列。
4. 【关注区间 entry_zone】以**上一交易日收盘价**为锚，给一个今天值得观察的价格区间，
   格式固定为「低~高」（两个数字 + ~），例：`12.0~12.5`。这是"这个价位附近才值得看"，
   不是目标价、不是止损价。
5. 【触发条件 trigger】必填，必须是**今天看盘就能验证**的可观测信号
   （如「开盘半小时站稳 12.5 且成交额较昨日同期放大」）。
   「关注量能变化」「看盘面表现」这类没法验证的一律不合格。
6. 【推翻条件 invalidation】必填，且必须是**可观测的证伪信号**
   （如「若公司公告否认该传闻」「若板块成交额连续两日萎缩」）。
   「注意风险」「谨慎参与」「关注后续」这类空话一律不合格。
7. 【禁止投资指令与承诺】不得出现「建议买入」「建议卖出」「满仓」「梭哈」「必涨」
   「稳赚」等表述。用「纳入观察」「值得跟踪」。
8. 【置信度】每条标 高/中/低。单源待核实(unve)新闻支撑的最高只能给「中」。
9. 【依据编号】basis_refs 列出支撑该候选的新闻编号（就是下面清单里的 [n]）。
10. 【小资金偏好】用户资金规模小：依据强度相当时**优先单价更低的个股**
    （一般 20 元以下优先）；单价高的只有依据明显更强时才入选。

## 输出格式
只输出 JSON，不要任何解释文字，不要 markdown 围栏：
{{"candidates":[{{"kind":"stock","name":"","code_hint":"","logic":"一句话逻辑链",
  "entry_zone":"12.0~12.5","trigger":"今天可观测的触发条件",
  "invalidation":"可观测的证伪信号","confidence":"中","basis_refs":[1,2]}}]}}

kind 固定填 "stock"；code_hint 只在你知道沪深 A 股 6 位代码时填，否则留空。

## 已完成的市场分析
{analysis}

## 相关个股行情（昨收 = 上一交易日收盘价）
{quotes}

## 今日新闻（共 {digest_count} 条精选，编号@[]用于引用）
{digest}"""


def build_am_prompt(digest, digest_count, analysis_md, quotes):
    """盘前版 prompt。行情清单同样只给沪深 A 股（与 build_prompt 同一条理由）。"""
    a_quotes = [q for q in quotes if is_sh_sz_a(str(q.get("code") or ""))]
    q_lines = "\n".join(
        f"- {q['name']}({q.get('code','')}) 昨收 {q.get('price')}"
        f"（上一交易日涨跌 {q.get('change_pct')}%）"
        for q in a_quotes[:60]) or "（今日未取到行情）"
    return AM_PROMPT_HEAD.format(
        max_stocks=MAX_STOCKS,
        analysis=analysis_md[:12000], quotes=q_lines,
        digest_count=digest_count, digest=digest)


def clean_entry_zone(raw):
    """entry_zone 字符串 → 合法则原样返回（去空白），否则返回 ""。

    判据只有一条：**能解析出两个数字 + 分隔符**（见 ENTRY_ZONE_RE）。解析不出就
    清空 —— 区间是辅助信息，为它丢掉整条候选（连同逻辑链与推翻条件）不划算。
    不做"数字合理性"校验：模型给 12.0~12.5 还是 120~125 都可能是对的
    （有的标的单价就是三位数），代码无从判断，只能校验形态。
    """
    s = str(raw or "").strip().strip("`")
    if not s:
        return ""
    s = re.sub(r"^约\s*", "", s)
    s = re.sub(r"\s*元$", "", s).strip()
    return s if ENTRY_ZONE_RE.match(s) else ""


_FW_STRUCT_PUNCT = {"，": ",", "：": ":", "【": "[", "】": "]",
                    "（": "(", "）": ")"}
_FW_QUOTES = {"“": '"', "”": '"', "‘": "'", "’": "'"}

# 最近一次解析的机器可读状态（供 main 分类状态文件的 reason，不改 parse_candidates
# 的返回值契约 —— 它对外仍然是「返回列表」）
_PARSE_STATE = {"json_ok": False, "extracted": 0, "kept": 0, "error": ""}
# 最近一次 ask_candidates 的结果状态；parse_failed 指「重问后仍没解析出 JSON」
LAST_LLM = {"parse_failed": False, "extracted": 0, "kept": 0,
            "raw_len": 0, "retry": "", "error": ""}


def _normalize_json_punct(t, aggressive_quotes=False):
    """JSON **结构位**上的全角标点 → 半角，字符串内部一律不动。

    中文正文里的「，」「：」是内容不是语法，全局替换会把 logic/invalidation
    改成半角（内容被篡改，且不解决结构问题）。2026-09-30 线上那次
    `Expecting ',' delimiter: line 1 column 118 (char 117)` 正是结构位的全角逗号。

    aggressive_quotes=True 时先把全角引号全局换成半角再走同一套扫描：模型用
    「“name”」这种中文引号当**定界符**时，只有这样才能救回来；代价是正文里的
    引号也会被换掉，故只作为整体解析失败后的兜底变体，不与保守变体混用。
    """
    if aggressive_quotes:
        t = t.translate(str.maketrans(_FW_QUOTES))
    out, in_str, esc = [], False, False
    for ch in t:
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
        else:
            out.append(_FW_STRUCT_PUNCT.get(ch, ch))
    return "".join(out)


def _strip_fence(t):
    """取代码围栏里的内容：re.I（```` ```JSON ```` 也认），多个围栏取**最后一个含
    `{` 的**（模型通常先解释后给 JSON；早期版本取第一块，正好拿到解释段）。

    没有围栏时原样返回 —— 裁花括号是下一步的事，两步解耦、都要执行。
    """
    blocks = re.findall(r"```[ \t]*(?:json)?[ \t]*\r?\n?(.*?)```", t, re.S | re.I)
    if not blocks:
        return t
    with_brace = [b for b in blocks if "{" in b]
    return (with_brace or blocks)[-1]


def _trim_variants(s):
    """① 原样 ② 从第一个 { 裁到最后一个 }。裁花括号与剥围栏解耦，两步都执行。"""
    yield s
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        yield s[i:j + 1]


def _brace_items(t):
    """按**花括号配平**切出顶层 `{...}` 并逐条 json.loads —— 整批失败后的逐条抢救。

    跳过字符串内部的 `{}` 与转义引号（logic 正文里出现花括号/引号是常事）。
    """
    key = t.find('"candidates"')
    start = t.find("[", key) if key >= 0 else -1
    if start < 0:
        start = t.find("[")
    if start < 0:
        start = t.find("{")
    if start < 0:
        return []
    items, buf, depth = [], None, 0
    in_str = esc = False
    for ch in t[start:]:
        if in_str:
            if buf is not None:
                buf.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            if buf is not None:
                buf.append(ch)
        elif ch == "{":
            if buf is None:
                buf, depth = ["{"], 1
            else:
                depth += 1
                buf.append(ch)
        elif ch == "}":
            if buf is not None:
                buf.append(ch)
                depth -= 1
                if depth <= 0:
                    try:
                        o = json.loads("".join(buf))
                    except Exception:
                        o = None
                    if isinstance(o, dict):
                        items.append(o)
                    buf = None
        elif buf is not None:
            buf.append(ch)
    return items


def _dump_raw(text, max_tokens=None, err=None, mode="w"):
    """LLM 原文落盘（含时间戳 / max_tokens / 失败位置上下文）。

    落盘失败只 print warn，绝不影响主流程。mode="a" 用于重问那一次的追加，
    这样一轮里「原始输出 + 重问输出」都在同一个快照里可查。
    """
    try:
        RAW_DUMP_PATH.parent.mkdir(parents=True, exist_ok=True)
        head = (f"# {now_cst().strftime('%Y-%m-%d %H:%M:%S')} "
                f"max_tokens={max_tokens if max_tokens is not None else '?'} "
                f"len={len(text)}")
        body = [head, text]
        pos = getattr(err, "pos", None)
        if isinstance(pos, int):
            body.append(f"\n--- 解析失败位置 char {pos} 前后各 80 字符 ---")
            body.append(text[max(0, pos - 80):pos + 80])
        with open(RAW_DUMP_PATH, mode, encoding="utf-8") as f:
            f.write("\n".join(body) + "\n")
    except Exception as e:
        print(f"[warn] LLM 原文落盘失败: {type(e).__name__}: {str(e)[:60]}")


def _extract_items(text, max_tokens=None, dump_mode="w"):
    """LLM 原文 → list[dict]：**只做「文本 → JSON 条目」**，不做业务校验。

    解析策略（修正后的真实语义）：
    1. 围栏与花括号**两步都做**：剥离围栏 → 全角结构标点归一 → 裁到 {...}
    2. 整体 json.loads 成功就用它；**整体失败则按花括号配平逐条抢救**，抢回来的
       条目照常进入后续校验 —— 一次字符级失误不再让整天样本归零
    3. 无论成败都落盘原始输出（见 _dump_raw）

    抽成独立函数是为了让 pm/am 两条通道共用同一份抢救实现（2026-10-04）：09-30 那种
    「一个字符失误 = 整天样本归零」的解药只能有一份，否则新通道会悄悄退化成整批丢弃。
    同时更新 _PARSE_STATE 的 json_ok / extracted / error（kept 由 _validate_items 填）。
    """
    raw = text if isinstance(text, str) else ""
    _PARSE_STATE.update(json_ok=False, extracted=0, kept=0, error="")
    _dump_raw(raw, max_tokens, mode=dump_mode)
    if not raw.strip():
        # 空原文不是「语法错误」：状态归类交给 ask_candidates/main 判成 empty
        print("[warn] LLM 原文为空，没有可解析的候选")
        return []

    t = _strip_fence(raw.strip())
    variants = []
    for aggr in (False, True):          # 保守变体优先，引号全局替换的兜底在后
        for v in _trim_variants(_normalize_json_punct(t, aggr)):
            if v and v not in variants:
                variants.append(v)

    data, items, last_err = None, None, None
    for v in variants:
        try:
            data = json.loads(v)
            last_err = None
            break
        except Exception as e:
            last_err = e

    if data is not None:
        _PARSE_STATE["json_ok"] = True
        items = data.get("candidates") if isinstance(data, dict) else data
        if not isinstance(items, list):
            items = []
            print("[warn] 候选 JSON 解析成功但没有 candidates 数组")
    else:
        items = []
        for v in variants:              # 保守变体先试，全角引号兜底变体后试
            items = _brace_items(v)
            if items:
                break
        detail = f"{type(last_err).__name__}: {str(last_err)[:60]}" if last_err else "结构不符"
        if items:
            _PARSE_STATE["json_ok"] = True
            _dump_raw(raw, max_tokens, err=last_err, mode=dump_mode)
            print(f"[warn] 候选 JSON 整体解析失败（{detail}），"
                  f"按花括号配平逐条抢救出 {len(items)} 条")
        else:
            _PARSE_STATE["error"] = detail
            _dump_raw(raw, max_tokens, err=last_err, mode=dump_mode)
            print(f"[warn] 候选 JSON 解析失败: {detail}")

    _PARSE_STATE["extracted"] = len(items)
    return items


def _validate_items(items, am=False):
    """条目 → 通过校验的候选 dict 列表（**逐条**丢弃不合格的，不是整批失败）。

    校验链（pm/am 共用，am 只多几条）：
      · kind 合法（am 只收 stock —— 盘前通道要的是"昨收 → 区间 → 今日触发"这条链，
        板块指数给不出这一套）；name 是单一标的（长度/「等」/「、」/个股斜杠）
      · invalidation ≥8 字（空话硬闸，见模块 docstring 第 2 条）
      · logic 非空
      · **am 追加**：trigger 非空（今天看盘可验证的触发条件）、entry_zone 形态校验
        （解析不出就清空该字段，**不丢整条**）
      · code_hint 看得出 6 位数字却不是沪深 A 股 → 整条丢弃（北交所/B 股/港股）
      · 投资指令/承诺禁用词（pm 只用 M3.postcheck；am 另加 AM_EXTRA_BANNED，且
        trigger/entry_zone 一并入扫描）
    """
    out, seen = [], set()
    for it in items:
        if not isinstance(it, dict):
            continue
        kind = (it.get("kind") or "").strip().lower()
        name = re.sub(r"\s+", "", str(it.get("name") or ""))
        logic = str(it.get("logic") or "").strip()
        inval = str(it.get("invalidation") or "").strip()

        if kind not in ("stock", "board"):
            print(f"[warn] 丢弃「{name}」：kind 非法（{kind}）")
            continue
        if am and kind != "stock":
            print(f"[warn] 丢弃「{name}」：盘前清单只收沪深 A 股个股（kind={kind}）")
            continue
        # 个股的 name 必须能被 resolve_stock 解析：斜杠合称（「零跑科技/零跑汽车」）
        # 与并列列表喂进去必然 not_found，一律丢弃。
        # **板块不受斜杠这条约束** —— 斜杠本就是东财板块名的常见写法
        # （实测 M3 里就有「AI算力/服务器产业链」这个真实小节标题），
        # 误杀它等于让板块候选全灭；而板块匹配失败只是降级为不计分，代价低。
        max_len = 12 if kind == "stock" else 20
        too_broad = "等" in name or "、" in name or (kind == "stock" and "/" in name)
        if not (2 <= len(name) <= max_len) or too_broad:
            print(f"[warn] 丢弃「{name}」：不是单一标的")
            continue
        if len(inval) < 8:
            print(f"[warn] 丢弃「{name}」：推翻条件缺失或过短")
            continue
        if not logic:
            print(f"[warn] 丢弃「{name}」：缺逻辑链")
            continue
        # 盘前通道额外两个字段：trigger 必填（丢了它就只剩"一只票 + 一条逻辑"，
        # 没法指导今天看什么）；entry_zone 只校验形态，不合法就清空**不丢整条**
        trigger, entry_zone = "", ""
        if am:
            trigger = str(it.get("trigger") or "").strip()
            if not trigger:
                print(f"[warn] 丢弃「{name}」：盘前候选缺触发条件（trigger）")
                continue
            raw_zone = str(it.get("entry_zone") or "").strip()
            entry_zone = clean_entry_zone(raw_zone)
            if raw_zone and not entry_zone:
                print(f"[warn] 「{name}」的关注区间无法解析（{raw_zone[:30]!r}），"
                      "已清空该字段，候选仍入账")
        # 个股代码：看得出 6 位数字但不是沪深 A 股（北交所/B 股/港股带后缀等）
        # 直接丢弃；看不出 6 位代码则清空，交给 record()/复盘兜底按名解析后再判一次
        code_hint = str(it.get("code_hint") or "").strip()
        if kind == "stock" and code_hint:
            m = re.search(r"\d{6}", code_hint)
            if m and not is_sh_sz_a(m.group(0)):
                print(f"[warn] 丢弃「{name}」：非沪深 A 股代码 {code_hint}")
                continue
            code_hint = m.group(0) if m else ""
        # 复用 M3 的禁用词扫描，让「禁止投资指令」由代码强制而非只靠措辞。
        # am 额外：新字段（trigger/entry_zone）也要扫，且补一层承诺性措辞
        # （满仓/梭哈/必涨…）—— M3.postcheck 只覆盖指令，不覆盖承诺。
        scan = logic + inval + name + (trigger + entry_zone if am else "")
        bad, _ = M3.postcheck(scan)
        if not bad and am:
            bad = [p for p in AM_EXTRA_BANNED if re.search(p, scan)]
        if bad:
            print(f"[warn] 丢弃「{name}」：命中投资指令禁用词 {bad}")
            continue
        key = (kind, name)
        if key in seen:
            continue
        seen.add(key)

        conf = str(it.get("confidence") or "").strip()
        refs = it.get("basis_refs")
        out.append({
            "kind": kind, "name": name,
            "code_hint": code_hint if kind == "stock" else "",
            "board": str(it.get("board") or "").strip(),
            "logic": logic, "invalidation": inval,
            "confidence": conf if conf in ("高", "中", "低") else "中",
            "basis_refs": [r for r in refs if isinstance(r, int)] if isinstance(refs, list) else [],
            # 盘前新增字段（pm 时恒为空串）：record() 只在 am 行把它们写进账本，
            # 所以 pm 账本行的字段集与改造前逐字相同
            "entry_zone": entry_zone, "trigger": trigger,
        })

    _PARSE_STATE["kept"] = len(out)
    return out


def parse_candidates(text, max_tokens=None, dump_mode="w", am=False):
    """LLM 输出 → list[dict]（供 record() 使用；顺序 = 模型给的顺序）。

    逐条校验阶段是「**不合格的逐条丢弃**」而不是整批失败；只有当整体解析失败且
    一条都抢救不回来时才返回 []。文本 → JSON 的抢救逻辑见 _extract_items，
    业务闸门见 _validate_items。

    am=True → 盘前通道：只收个股、每条必须带 trigger，entry_zone 形态不合法则
    清空该字段；返回条数上限仍是 MAX_STOCKS。pm（默认）行为与改造前完全一致。
    """
    out = _validate_items(_extract_items(text, max_tokens, dump_mode), am=am)
    if am:
        return out[:MAX_STOCKS]
    stocks = [c for c in out if c["kind"] == "stock"][:MAX_STOCKS]
    boards = [c for c in out if c["kind"] == "board"][:MAX_BOARDS]
    return stocks + boards


def _pick_reason(llm):
    """LAST_LLM → (reason, detail)，只在「没拿到任何候选」时用于状态文件。"""
    if llm.get("parse_failed"):
        d = f"JSON 解析失败: {llm.get('error') or '未知'}"
        retry = str(llm.get("retry") or "")
        if retry.startswith("error"):
            d += f"；重问也失败（{retry[7:]}）"
        elif retry == "done":
            d += "；带原文重问后仍失败"
        return "parse_failed", d
    if not llm.get("extracted"):
        return "empty", "LLM 未返回 candidates 数组（或无材料）"
    return "all_rejected", f"解析到 {llm['extracted']} 条候选，全部未通过校验"


def ask_candidates(digest, digest_count, analysis_md, quotes, am=False):
    """调用一次 DeepSeek；解析出 0 条且原文非空时**带原文重问一次**。

    重问用的是同一 ds_chat、同一 max_tokens；重问本身失败（网络等）不算主流程
    失败，维持 0 条即可。状态写进模块级 LAST_LLM，供 main 分类 reason。

    am=True 走盘前版 prompt 与盘前校验链（build_am_prompt / parse_candidates(am=True)），
    其余（重问、LAST_LLM 状态归类）完全共用一条路径 —— 重问逻辑只该有一份实现。
    """
    LAST_LLM.update(parse_failed=False, extracted=0, kept=0, raw_len=0,
                    retry="", error="")

    prompt = (build_am_prompt if am else build_prompt)(digest, digest_count,
                                                       analysis_md, quotes)
    print(f"{'盘前' if am else '候选'} prompt {len(prompt)} 字符，调用 DeepSeek…")
    text = M3.ds_chat([{"role": "user", "content": prompt}], max_tokens=MAX_CAND_TOKENS)
    cands = parse_candidates(text, max_tokens=MAX_CAND_TOKENS, am=am)
    ok1, ex1, kp1 = (_PARSE_STATE["json_ok"], _PARSE_STATE["extracted"],
                     _PARSE_STATE["kept"])
    err1 = _PARSE_STATE["error"]
    # 原文为空不算「解析失败」：那既没有语法错误，也没有可抢救的内容，
    # 归到 empty（无材料）比 parse_failed 更贴近事实
    LAST_LLM.update(raw_len=len(text or ""), error=err1, extracted=ex1, kept=kp1,
                    parse_failed=not ok1 and bool((text or "").strip()))

    if cands or not (text or "").strip():
        return cands

    LAST_LLM["retry"] = "asked"
    if am:
        # 盘前的 0 条有两种成因：JSON 语法坏，或**语法没问题但字段没给全**
        # （trigger/entry_zone 是新加的要求，模型第一遍漏掉是常事）。后者用
        # 「修正语法」那句话重问等于什么都没说，所以这里把要求再点一遍。
        # pm 的重问文案一字未改（见 else）。
        print("[warn] 盘前候选一条都没通过校验（或 JSON 有语法错误），带原文重问一次…")
        fix = ("以下盘前候选 JSON 不合格。每条候选都必须给出："
               "非空的 trigger（今天开盘后即可验证的触发条件）、"
               "形如 12.0~12.5 的 entry_zone（两个数字 + ~）、"
               "以及 ≥8 字的 invalidation；且只选沪深 A 股个股（6 位代码）。"
               "请只输出修正后的 JSON，不要解释、不要 markdown 围栏：\n" + text)
    else:
        print("[warn] 候选 JSON 解析失败，带原文重问一次…")
        fix = ("以下 JSON 有语法错误，请只输出修正后的 JSON，不要解释、不要 markdown 围栏：\n"
               + text)
    try:
        text2 = M3.ds_chat([{"role": "user", "content": fix}], max_tokens=MAX_CAND_TOKENS)
    except Exception as e:
        LAST_LLM["retry"] = f"error: {type(e).__name__}: {str(e)[:60]}"
        print(f"[warn] 重问失败，维持 0 条: {type(e).__name__}: {str(e)[:60]}")
        return []

    cands2 = parse_candidates(text2, max_tokens=MAX_CAND_TOKENS, dump_mode="a", am=am)
    ok2, ex2, kp2 = (_PARSE_STATE["json_ok"], _PARSE_STATE["extracted"],
                     _PARSE_STATE["kept"])
    LAST_LLM.update(parse_failed=not (ok1 or ok2), extracted=max(ex1, ex2),
                    kept=max(kp1, kp2), error=_PARSE_STATE["error"] or err1,
                    retry="done")
    return cands2


# ---------------------------------------------------------------- 记录候选

def resolve_target(cand, board_map, cache, by_name):
    """候选 → (secid, code, market)。解析不到返回 (None, None, '')，条目仍入账但不打分"""
    if cand["kind"] == "stock":
        info = M4.resolve_stock(cand["name"], cache)
        if info:
            return info["secid"], info["code"], info.get("market", "")
        # 东财搜索接口不可用时的兜底：M4 刚跑过，quotes.json 里就有同名标的。
        # A 股 6 位代码的市场前缀是确定的（6→沪，0/3→深），这不是 M4 刻意回避的
        # 「猜市场」——那条针对的是美股/港股混在一起、前缀无从推断的情况。
        q = by_name.get(cand["name"])
        code = (q or {}).get("code") or ""
        if is_sh_sz_a(code):            # 只兜底沪深 A 股，港股 5 位码等一律不猜
            return (f"{'1' if code[0] == '6' else '0'}.{code}",
                    code, q.get("market", ""))
        return None, None, ""
    hit = match_board(cand["name"], board_map)
    if not hit:
        return None, None, ""
    return hit["secid"], hit["code"], "板块指数"


def base_quote(secid, code, quotes_by_code):
    """优先复用 M4 已抓的行情（省一次请求，且与日报里的行情表一致），
    仅当 quotes.json 确实是今天生成的才复用 —— 本地跑时它可能是旧的。"""
    q = quotes_by_code.get(code)
    if q and q.get("price"):
        return q["price"], q.get("prev_close"), "m4_quotes.json"
    q, src = fetch_any(secid)
    if q:
        time.sleep(1.0)     # push2 限流敏感，与 M4 一致
        return q["price"], q.get("prev_close"), src
    return None, None, None


def am_base_quote(secid, code, quotes_by_code, prev_day):
    """盘前基准价（= **上一交易日收盘价**）→ (price, prev_close, source) 或三个 None。

    与 base_quote 的区别：这里锁的是**昨收**，来不得半点含糊 —— 来源必须能自证
    "这是上一交易日收盘价"，否则宁可不记（调用方会把该条候选丢掉）。

    ① 首选当日 quotes.json（M4 本轮刚抓的，盘前的"现价"就是昨收）。若该条行情
       自带 `time`（腾讯源有、东财源没有），要求它的日期正好是 prev_day ——
       带了时刻却对不上的，说明这不是昨收（例如延迟到上午十点才跑的 M4）。
    ② quotes.json 里没有这只标的时才现抓一次（最多 MAX_STOCKS 次）。现抓要
       **严格要求时刻字段存在且落在 prev_day ≥15:00**：东财源没有时刻字段，故这条
       路等价于"腾讯源 + 时刻自证"，不会把无法验证的价格写进账本。

    为什么允许 ②：候选是从 analysis/news 里挑的，未必都落在 M4 抓过的那批名字里；
    没有它，"盘前清单"会因为 M4 的取样范围而整条整条地缩水。
    """
    q = quotes_by_code.get(code) if code else None
    if q and q.get("price"):
        t = str(q.get("time") or "").strip()
        if t and not t.startswith(prev_day.strftime("%Y%m%d")):
            print(f"[warn] quotes.json 里 {code} 的报价时刻 {t} 不是上一交易日"
                  f"（{prev_day}）的收盘，不用于盘前基准价")
        else:
            return q["price"], q.get("prev_close"), "m4_quotes.json"
    if not secid:
        return None, None, None
    q, src = fetch_any(secid)
    if not q or not q.get("price"):
        return None, None, None
    time.sleep(1.0)             # push2 限流敏感，与 M4/base_quote 一致
    t = str(q.get("time") or "").strip()
    prev8 = prev_day.strftime("%Y%m%d")
    if not (re.fullmatch(r"\d{12,14}", t) and t[:8] == prev8 and t[8:12] >= "1500"):
        print(f"[warn] 现抓的 {secid} 行情时刻为 {t or '空'}，无法确认是上一交易日"
              f"（{prev8}）收盘 → 不用作盘前基准价")
        return None, None, None
    return q["price"], q.get("prev_close"), src


def _bump(stats, key):
    """stats 计数（stats 为 None 时什么都不做 —— pm 通道不关心这些计数）。"""
    if stats is not None:
        stats[key] = stats.get(key, 0) + 1


def record(rows, today, slot, cands, bench, board_map, cache, by_code, by_name,
           am=False, base_date=None, stats=None):
    """把今日候选追加进账本。已存在的 id 跳过 —— 幂等保险。

    am=True 走盘前通道：基准价换成上一交易日收盘价（见 am_base_quote / base_date），
    并新写 `base_date` / `entry_zone` / `trigger` 三个字段（**只增不改**已有字段名与
    类型）；锁不到昨收的候选**整条丢弃** —— 盘前清单的价值就在于"以昨收为基准"，
    一条没有基准的记录既指导不了今天，也永远打不了分。

    stats（可选 dict）：回填 added/dup/no_market/no_base 计数，供 main 分类 reason
    （am 通道 0 条时要能说清是"已在账本里（幂等）"还是"全被丢弃"）。
    """
    existing = {r.get("id") for r in rows}
    prev_d = None
    if am:
        try:
            prev_d = _plain_date(base_date)
        except Exception:
            # base_date 缺了就没法证明基准价属于哪个交易日 —— 盘前通道 fail-closed，
            # 一条都不记（调用方本应先算出来；这里只是不让它写成 base_date=null 的行）
            print(f"[warn] 盘前基准日（base_date={base_date!r}）无效，本轮不入账")
            return 0
    added = 0
    for cand in cands:
        # id 用内容寻址（日期+时段+类型+名称），不用序号：序号依赖列表位置，
        # 一旦某条被跳过就会让后续候选错位（曾出现两条都算成 s1、第二条
        # 压根没比对 s2 的情况）。内容寻址下同名候选天然幂等。
        cid = f"{today}-{slot}-{'s' if cand['kind'] == 'stock' else 'b'}-{cand['name']}"
        if cid in existing:
            print(f"[warn] {cid} 已存在，跳过（幂等）")
            _bump(stats, "dup")
            continue
        secid, code, market = resolve_target(cand, board_map, cache, by_name)
        # 沪深 A 股硬闸（用户要求「主要为沪A和深A」）：解析出来发现是港股/美股/
        # 北交所/B 股的，整条丢弃而非降级记账 —— 用户账户买不了这些，
        # 留在账本里只会污染复盘样本。name 解析失败（code/secid 均空）的
        # 仍按 M10 要求 4 记账不打分，因为无从判断它属于哪个市场。
        if cand["kind"] == "stock":
            sec_prefix = (secid or "").split(".", 1)[0]
            # 解析全失败时再查一次 quotes.json 的市场标签（M4 给过「港股/美股」
            # 但本次 resolve 抽风的场景），有标签就按标签判
            mkt = market or str(
                (by_name.get(cand["name"]) or {}).get("market") or "")
            if ((code and not is_sh_sz_a(code))
                    or sec_prefix in ("105", "106", "107", "116")
                    or "港" in mkt or "美" in mkt):
                print(f"[warn] 丢弃「{cand['name']}」：非沪深 A 股"
                      f"（code={code} secid={secid} market={mkt}）")
                _bump(stats, "no_market")
                continue
        price = prev = src = None
        if am:
            price, prev, src = am_base_quote(secid, code, by_code, prev_d)
        elif secid:
            price, prev, src = base_quote(secid, code, by_code)
        if am and not price:
            print(f"[warn] 丢弃「{cand['name']}」：锁不到上一交易日收盘价"
                  "（盘前清单每条都必须有昨收基准）")
            _bump(stats, "no_base")
            continue
        if not price:
            print(f"[warn] 「{cand['name']}」未匹配到行情，记账但不打分")

        row = {
            "id": cid, "date": today, "slot": slot,
            "kind": cand["kind"], "name": cand["name"],
            "code": code, "secid": secid, "market": market,
            # 候选里已校验为沪深 A 股的代码留档：按 name 解析不到 secid 时，
            # 复盘在宽限期内靠它与当日 quotes.json 交叉核对后兜底（见 _resolve_offline）
            "code_hint": cand.get("code_hint") or "",
            "base_price": price, "base_prev_close": prev,
            "quote_source": src, "bench_level": bench,
            "board": cand["board"],
            "logic": cand["logic"], "invalidation": cand["invalidation"],
            "confidence": cand["confidence"], "basis_refs": cand["basis_refs"],
            "reviews": {str(k): None for k in REVIEW_DAYS},
        }
        if am:
            # 盘前新增三字段（只增不改）：base_date = 基准价所在的交易日，
            # entry_zone / trigger = 今天的可执行判据。复盘拿 base_date 当 d0，
            # 于是 am 行的 T+1 正好落在**记录日当天**（见 score_pending）。
            row["base_date"] = base_date
            row["entry_zone"] = cand.get("entry_zone") or ""
            row["trigger"] = cand.get("trigger") or ""
        rows.append(row)
        added += 1
        _bump(stats, "added")
        tag = "板块" if cand["kind"] == "board" else "个股"
        zone = f"　区间 {row['entry_zone']}" if am and row.get("entry_zone") else ""
        print(f"  + [{tag}] {cand['name']} 基准 {price}{zone} ({cand['confidence']})")
    return added


# ---------------------------------------------------------------- 到期打分

def _secid_of(code):
    """沪深 A 股 6 位代码 → 东财 secid。6 开头沪 / 0、3 开头深，前缀是确定的。"""
    return f"{'1' if code[0] == '6' else '0'}.{code}"


def _load_quotes_maps(today_s):
    """当日 quotes.json → (code→行情, name→行情)。非当日或读不到返回 ({}, {})。

    非当日不复用：本地补跑时它是旧价，名称→代码虽几乎不变，但把「今天的材料」
    这个前提守住，才不会在复盘里引入一个无法解释的来源。
    """
    try:
        d = json.loads((DATA_DIR / "quotes.json").read_text(encoding="utf-8"))
    except Exception:
        return {}, {}
    if not str(d.get("generated_at") or "").startswith(today_s):
        return {}, {}
    qs = [q for q in (d.get("quotes") or []) if isinstance(q, dict)]
    return ({q["code"]: q for q in qs if q.get("code")},
            {q["name"]: q for q in qs if q.get("name")})


def _resolve_offline(row, by_name, by_code, cache=None):
    """账本行 → secid 的**不联网**兜底解析，返回 (secid, code, market, 来源)。

    ① 当日 quotes.json 的 name 索引（M4 刚跑过，同名标的就在里面）
    ② M4.resolve_stock 的内存缓存命中 —— name 不在 cache 里时**绝不调用它**：
       那函数缓存未命中就会去打东财搜索接口，等于在复盘阶段偷偷加一次联网，
       通道不通时还要白等超时
    ③ 候选自带的 code_hint（沪深 A 股，parse_candidates 已校验），但必须先与
       当日 quotes.json 交叉核对（同名同码，或该代码对应的记录同名）：模型的
       代码是幻觉高发区，仅凭 hint 直接构造 secid 会把别的股票的价格接到这条
       记录上，而这种错误一写进账本就永久留存
    """
    name = str(row.get("name") or "")
    same = by_name.get(name) or {}
    code = str(same.get("code") or "")
    if is_sh_sz_a(code):
        return _secid_of(code), code, same.get("market", ""), "quotes.json"

    if cache and name in cache:
        info = M4.resolve_stock(name, cache)
        if isinstance(info, dict) and info.get("secid"):
            return (info["secid"], info.get("code") or "",
                    info.get("market", ""), "m4_cache")

    m = re.search(r"\d{6}", str(row.get("code_hint") or ""))
    hint = m.group(0) if m else ""
    if is_sh_sz_a(hint):
        rec = by_code.get(hint) or {}
        if code == hint or str(rec.get("name") or "") == name:
            return (_secid_of(hint), hint,
                    rec.get("market") or same.get("market") or "", "code_hint")
    return None, None, "", ""


def score_pending(rows, today, bench_now, by_name=None, by_code=None, cache=None):
    """到期即补：today >= 基准日之后的第 k 个交易日 且该档未填 → 现在就打分。

    **基准日 d0 = row["base_date"] or row["date"]**（2026-10-04）：pm 行没有
    base_date，仍用记录日（行为与改造前完全一致）；am 行的 base_date 是**上一交易日**，
    于是它的 T+1 = 记录日当天 —— 当天 15:40 的 pm 运行正好把这一档补上
    （盘前清单当天收盘就能看到第一次验证，这是"盘前清单值得记"的关键）。
    同一条 am 行的 span 仍是"基准日 → 补录日的实际交易日数"，T+1 当天补上 → span=1。

    **一轮每行只补最早的那一档**（`min(未填且未作废的 k)`）。早期版本会把所有到期
    档位一次性补齐，跨长假时 T+3 与 T+5 会落到同一天、用同一个收盘价，实际只有约
    2 个交易日跨度，却在均值里各算一个样本。改成一档一轮后，T+1/T+3/T+5 自然落在
    不同交易日、不同价格上。因此同一候选一轮也只抓一次行情（只剩一个 k）。
    **am 行不放宽 EXPIRE_AFTER_DAYS**：它的基准日更早，宽限期只会更紧、不会更松。

    secid 为空的条目在 EXPIRE_AFTER_DAYS 宽限期内**每轮都重试解析**（见
    _resolve_offline），超过宽限才写 status="no_quote"。早期版本首个到期日就
    直接写死 no_quote，而"取不到行情"却有 15 天宽限 —— 宽严倒挂：明明可以
    重试的解析失败被当成终态，明明会自己好的行情失败反而有宽限。

    by_name / by_code / cache 都是可选的：不传就是旧行为（不做兜底解析），
    其它调用点不受影响。
    """
    scored = 0
    by_name = by_name or {}
    by_code = by_code or {}
    for row in rows:
        rv = row.get("reviews")
        if not isinstance(rv, dict):        # 直接调用本函数时的兜底，load_ledger 已归一
            rv = row["reviews"] = {str(k): None for k in REVIEW_DAYS}
        try:
            d0 = _date(row.get("base_date") or row["date"])
        except Exception:
            print(f"[warn] 「{row.get('name')}」日期无法解析（{row.get('date')!r}），跳过")
            continue

        unfilled = [k for k in REVIEW_DAYS if rv.get(str(k)) is None]
        if not unfilled:
            continue

        # ① 先筛出"已经到期"的档位（due(k) = **基准日**之后的第 k 个交易日；
        # 基准日 = base_date（am 行）或 date（pm 行））。
        # 都没到期就整行跳过 —— 连 secid 兜底解析都不做，与旧行为一致。
        due_now = [(k, due(k, d0)) for k in unfilled]
        due_now = [(k, k_due) for k, k_due in due_now if today >= k_due]
        if not due_now:
            continue

        if not row.get("secid"):
            secid, code, market, src = _resolve_offline(row, by_name, by_code, cache)
            if secid:
                row["secid"] = secid
                if not row.get("code"):
                    row["code"] = code
                if not row.get("market"):
                    row["market"] = market
                print(f"[OK] {row['name']} 复盘兜底解析出 secid={secid}（来源 {src}）")
        has_secid = bool(row.get("secid"))

        # ② 逐档独立判逾期。**不能拿最早那档统一作废**：T+1 逾期 15 天以上时，
        # T+3/T+5 可能刚到到期窗口，一并作废等于白丢一条本可以打的分。
        # 状态语义与旧版一致：解析不到 secid 的写 no_quote，有 secid 写 expired。
        alive = []
        for k, k_due in due_now:
            if (today - k_due).days > EXPIRE_AFTER_DAYS:
                span, kind = _span_of(d0, today)
                rv[str(k)] = _review(k_due, today,
                                     status="expired" if has_secid else "no_quote",
                                     span=span, span_kind=kind)
                print(f"[warn] {row['name']} T+{k} 逾期未补"
                      f"（到期日 {k_due.strftime('%Y-%m-%d')}），标记 "
                      f"{'expired' if has_secid else 'no_quote'}")
            else:
                alive.append((k, k_due))
        if not alive:
            continue

        # ③ **本轮只补最早的那一档**。其余活着的档保持 null，下一轮再看 ——
        # 这样 T+1/T+3/T+5 会落在不同交易日、不同价格上（见 docstring）。
        k, k_due = min(alive)

        # 仍解析不到 secid：宽限期内留待下次运行重试，逾期才认输（②里已写过）
        if not has_secid:
            print(f"[warn] {row['name']} 未解析到 secid，宽限期内继续重试（T+{k}）")
            continue

        q, _src = fetch_any(row["secid"])
        if q:
            time.sleep(1.0)
        price = q.get("price") if q else None
        if not price:
            continue        # 停牌 / 限流：留待下次运行再补（本档仍未填）

        base = row.get("base_price")
        ret = round((price - base) / base, 4) if base else None
        b0 = row.get("bench_level")
        bret = round((bench_now - b0) / b0, 4) if (b0 and bench_now) else None
        alpha = round(ret - bret, 4) if (ret is not None and bret is not None) else None

        span, kind = _span_of(d0, today)
        rv[str(k)] = _review(
            k_due, today, price=price, ret=ret, bench=bench_now,
            bench_ret=bret, alpha=alpha,
            status="ok" if alpha is not None else "no_bench",
            span=span, span_kind=kind)
        scored += 1
        a = f"{alpha:+.2%}" if alpha is not None else "—"
        print(f"  ~ {row['name']} T+{k} 收 {price} 收益 {ret:+.2%} 超额 {a}"
              f"（实际跨度 {span} 个{'交易日' if kind == 'trading' else '自然日'}）")
    return scored


def _review(due_day, done, price=None, ret=None, bench=None, bench_ret=None,
            alpha=None, status="ok", span=None, span_kind=None):
    """一档复盘记录。**已有字段名/类型一律不变**（账本 schema 只允许新增）。

    参数 due_day 就是模块级 due() 的返回值（CST 零点 datetime），写进账本时
    格式化成 "YYYY-MM-DD" 存进 **due** 字段 —— 字段名与类型都没变。

    新增（纯附加）：
      span      基准日 → 本次判定日之间的**实际交易日数**（int）；日历不可用时是
                自然日天数，由 span_kind 指出
      span_kind "trading"（真实交易日历）/ "natural"（降级为自然日）
    展示层可据此说清"这条 T+3 其实是第 5 个交易日的价"。
    注：expired / no_quote 的 span 是"基准日 → 判该档作废那天"，不是补录跨度（没有补录）。
    """
    return {"due": due_day.strftime("%Y-%m-%d"), "done": done.strftime("%Y-%m-%d"),
            "price": price, "ret": ret, "bench": bench, "bench_ret": bench_ret,
            "alpha": alpha, "status": status,
            "span": span, "span_kind": span_kind}


def _date(s):
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=CST)


# ---------------------------------------------------------------- 自检

def probe():
    print("=" * 52)
    print("M10 接口自检（不写账本）")
    print("=" * 52)
    ok = True

    # 时段与「今天是不是交易日」：盘前通道的开关，且直接决定"若现在记录"走哪条路
    slot = detect_slot()
    today = _today()
    print(f"       日期 {today}　时段 slot={slot}"
          f"（REPORT_SLOT={os.environ.get('REPORT_SLOT') or '未设置'}）")

    bench, bench_q = fetch_bench()
    if bench_q:
        ready, why = close_ready(bench_q, now_cst())
        print(f"[OK]   沪深300 {bench_q['price']} ({bench_q['change_pct']}%) "
              f"报价时刻={bench_q.get('time') or '无'}")
        print(f"{'[OK]  ' if ready else '[warn]'} 收盘闸门：{why}")
        if not bench_q.get("time"):
            print("[FAIL] 腾讯源未给报价时刻 —— 闸门无法判定，复盘会 fail-closed")
            ok = False
    else:
        print("[FAIL] 沪深300 取数失败 —— 超额无法计算，且闸门 fail-closed")
        ok = False

    q, src = fetch_any("0.001216")
    if q:
        print(f"[OK]   个股样例 华瓷股份 {q['price']} ({q['change_pct']}%) 源={src}")
    else:
        print("[FAIL] 个股行情取数失败 —— 候选基准价无从锁定")
        ok = False

    bm = build_board_map()
    if bm:
        sample = list(bm.items())[:3]
        print(f"[OK]   板块映射 {len(bm)} 个，例: "
              + ", ".join(f"{k}→{v['secid']}" for k, v in sample))
    else:
        print("[FAIL] 板块列表取数失败 —— 板块候选将降级为只列不计分")
        ok = False

    for name in ("半导体", "AI算力", "算力", "不存在板块"):
        hit = match_board(name, bm)
        print(f"       匹配「{name}」→ {hit['secid'] if hit else '未匹配'}")

    # 交易日历：来源 + 最近 10 个交易日（排查 due(k)/span 用）。
    # 取不到不是 FAIL —— 有内置近似日历兜底，但要把来源打清楚。
    print("-" * 52)
    td = trading_days(_today() - timedelta(days=40), _today())
    kind = "fallback" if _CAL["approx"] else "real"
    tag = "[OK]  " if kind == "real" else "[warn]"
    print(f"{tag} 交易日历来源={_CAL['source'] or '（未取到，用内置近似日历）'}")
    print(f"       查询窗口 {_CAL['beg']}~{_CAL['end']}"
          f"　口径={'真实交易日' if kind == 'real' else '近似（周一~周五−2026休市表）'}")
    print(f"       最近 10 个交易日: "
          + (" ".join(d.strftime('%Y-%m-%d') for d in td[-10:]) or "（无）"))
    print(f"       缓存文件: {TRADING_DAYS_CACHE_PATH}"
          f"（存在={TRADING_DAYS_CACHE_PATH.exists()}）")
    for k in REVIEW_DAYS:
        print(f"       T+{k}: 若今日记录 → 到期 {due(k).strftime('%Y-%m-%d')}")

    # 盘前通道（am）判据 + "若现在记录，基准价会取哪个报价时刻"（2026-10-04 新增）。
    # 打印顺序刻意与 am_gate 的判定顺序一致：① 交易日 → ② 上一交易日 → ③ 报价时刻形态；
    # 最后给一个**综合结论** —— 只看判据③ 会得出"昨收已结算"而在休市日显得自相矛盾。
    print("-" * 52)
    trading = is_trading_day(today)
    print(f"{'[OK]  ' if trading else '[warn]'} 判据① 今日是否交易日：{trading}")
    prev = prev_trading_day(today)
    print(f"       判据② 上一交易日（基准日 base_date）：{prev or '（求不出来）'}")
    if bench_q and prev:
        am_ok, am_why, am_reason = premarket_ready(bench_q, today, prev)
        print(f"{'[OK]  ' if am_ok else '[warn]'} 判据③ 报价时刻形态：{am_why}"
              + (f"（reason={am_reason}）" if am_reason else ""))
        total = bool(trading and am_ok)
        print(f"       → 盘前通道综合结论：{'可记录（gate_open=true）' if total else '不记录'}"
              + (f"，reason={am_reason or 'non_trading_day'}" if not total else ""))
        # "若现在记录，基准价取哪个报价时刻"：am 优先用当日 quotes.json 里该标的的最新价
        # （盘前就是昨收）。这里只报"取哪一刻/哪个来源"，不联网抓个股。
        by_code, _by_name = _load_quotes_maps(today.strftime("%Y-%m-%d"))
        sample = next(iter(by_code.values()), None)
        if sample:
            print(f"       若现在记录，基准价取：m4_quotes.json 的 {sample.get('name')}"
                  f"({sample.get('code')}) 最新价 {sample.get('price')}"
                  f"　该行情报价时刻={sample.get('time') or '（东财源无时刻字段）'}"
                  f"　预期基准日 {prev}")
        else:
            print("       若现在记录，基准价取：当日 quotes.json 不可用（非今日生成或为空）"
                  "→ 盘前通道会跳过（reason=empty）")
        print(f"       am 行 → T+1 到期日 = {due(1, _midnight(prev)).strftime('%Y-%m-%d')}"
              "（= 记录日当天收盘后由 pm 运行补上）")
    else:
        print("[warn] 缺基准行情或基准日，判据③ 无法判定（fail-closed，不记录）")

    print("=" * 52)
    print("[OK] 自检通过" if ok else "[FAIL] 自检未通过")
    return 0 if ok else 1


# ---------------------------------------------------------------- 状态文件

REASON_DOC = ("ok=正常入账 / gate_closed=闸门未开 / llm_error=调用异常 / "
              "parse_failed=JSON 解析失败（含重问后仍失败）/ "
              "all_rejected=解析到候选但全被校验丢弃 / empty=无材料 / "
              "non_trading_day=今天休市（盘前通道判据①，am 专用）/ "
              "not_premarket=最新报价不是上一交易日收盘（盘前通道判据②，am 专用）")


def write_status(today_s, slot, state):
    """写 reports/picks/picks-<日期>-<时段>.json（先写 .tmp 再 replace）。

    接口冻结：M6 推送与体检按 date/slot/generated_at/gate_open/recorded/reason/
    detail 这 7 个字段读。**写失败只 warn** —— 状态文件是旁路产物，绝不能让它的
    失败掐断已经算好的账本写回。

    第 8 个字段 `calendar` 是 2026-10-04 追加的（**只增不改**，老读者不受影响）：
    盘前通道的日历口径 real / approx / approx-unverified-year。体检靠它发现
    "进入未核实年份 → 盘前清单每天静默停摆"这件事（见 calendar_kind 的注释）。
    """
    path = PICKS_DIR / f"picks-{today_s}-{slot}.json"
    payload = {
        "date": today_s,
        "slot": slot,
        "generated_at": now_cst().strftime("%Y-%m-%d %H:%M:%S"),
        "gate_open": bool(state.get("gate_open")),
        "recorded": int(state.get("recorded") or 0),
        "reason": state.get("reason") or "empty",
        "detail": str(state.get("detail") or "")[:200],
    }
    if state.get("calendar"):
        payload["calendar"] = state["calendar"]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        print(f"[OK] 状态文件 → {path}（reason={payload['reason']} "
              f"recorded={payload['recorded']}）")
    except Exception as e:
        print(f"[warn] 状态文件写入失败（不影响主流程）: {type(e).__name__}: {str(e)[:60]}")
    return payload


# ---------------------------------------------------------------- 盘前通道

def record_premarket(rows, today_s, bench, by_code, by_name, base_date, state):
    """盘前通道的「选股 → 入账」（main 在 am 且闸门已开时调用）。state 就地更新。

    与 pm 那条路的三点不同，全部收在这里（main 只在槽位上分叉一次）：
      ① 材料必须带**当日** quotes.json：盘前基准价只能来自本轮行情
         （am_base_quote 首选它），旧的 quotes.json 会让「昨收」变成几天前的价，
         而这行记录一写就不再改 —— 故非今日一律跳过，reason=empty
      ② 用盘前版 prompt（ask_candidates(am=True)）：多要 entry_zone 与 trigger
      ③ record(am=True)：基准=上一交易日收盘价，写 base_date/entry_zone/trigger，
         锁不到昨收的候选整条丢弃

    0 条时的 reason 分类与 pm 一致（_pick_reason 给 empty/all_rejected/parse_failed），
    但多一层"其实是幂等重跑"的区分：id 是内容寻址的，同一批重跑第二次必然 0 条新增，
    那不是故障。
    """
    if not (DATA_DIR / "analysis.md").exists():
        state.update(reason="empty", detail="无 data/analysis.md 材料，跳过盘前清单")
        print("[warn] data/analysis.md 不存在，跳过盘前清单")
        return
    try:
        news = json.loads(
            (DATA_DIR / "structured_news.json").read_text(encoding="utf-8"))["news"]
        quotes_json = json.loads(
            (DATA_DIR / "quotes.json").read_text(encoding="utf-8"))
        analysis_md = (DATA_DIR / "analysis.md").read_text(encoding="utf-8")
    except Exception as e:
        state.update(reason="empty",
                     detail=f"读取材料失败: {type(e).__name__}: {str(e)[:60]}")
        print(f"[warn] 材料读取失败，跳过盘前清单: {type(e).__name__}: {str(e)[:80]}")
        return

    gen = str(quotes_json.get("generated_at") or "")
    if not gen.startswith(today_s):
        state.update(reason="empty",
                     detail=f"quotes.json 生成于 {gen or '?'}，非本轮材料："
                            "盘前基准价（昨收）不可信")
        print(f"[warn] quotes.json 生成于 {gen or '?'}，非今日 → 盘前通道本轮不产出清单")
        return

    try:
        digest, count = M3.build_news_digest(news)
        cands = ask_candidates(digest, count, analysis_md,
                               quotes_json.get("quotes", []), am=True)
    except Exception as e:
        # 可选产出：盘前选股失败不该掐断主链路（复盘在 pm 那次运行里）
        state.update(reason="llm_error", detail=f"{type(e).__name__}: {str(e)[:80]}")
        print(f"[warn] 盘前选股失败: {type(e).__name__}: {str(e)[:80]}")
        return

    if not cands:
        reason, detail = _pick_reason(LAST_LLM)
        state.update(reason=reason, detail=detail)
        print("[warn] 盘前未产出合格候选（LLM 失败或全部未通过校验）")
        return

    stats = {}
    try:
        n = record(rows, today_s, "am", cands, bench, {}, {}, by_code, by_name,
                   am=True, base_date=base_date, stats=stats)
    except Exception as e:
        state.update(reason="empty",
                     detail=f"入账失败: {type(e).__name__}: {str(e)[:60]}")
        print(f"[warn] 盘前候选入账失败: {type(e).__name__}: {str(e)[:80]}")
        return

    state["recorded"] = n
    print(f"[OK] 盘前记录 {n} 条观察候选（基准日 {base_date}）")
    if n:
        state.update(reason="ok",
                     detail=f"新入账 {n} 条盘前观察候选（基准日 {base_date}）")
    elif stats.get("dup") == len(cands):
        state.update(reason="ok", detail="候选已在账本中（幂等跳过），本轮无新增")
    elif stats.get("no_base"):
        state.update(reason="all_rejected",
                     detail=f"{stats['no_base']} 条候选锁不到上一交易日收盘价，未入账")
    elif stats.get("no_market"):
        state.update(reason="all_rejected",
                     detail=f"{stats['no_market']} 条候选非沪深 A 股，未入账")
    else:
        state.update(reason="all_rejected", detail="候选全部未通过入账校验")


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="M10 候选清单与事后复盘")
    ap.add_argument("--probe", action="store_true", help="只自检接口，不读写账本")
    ap.add_argument("--force", action="store_true",
                    help="跳过收盘闸门与时段限制（**本地测试用，会写脏数据**）")
    ap.add_argument("--score-only", action="store_true",
                    help="只做到期复盘，不选新股（不消耗 LLM 调用）")
    ap.add_argument("--today", help="覆盖今日日期 YYYY-MM-DD（测试用）")
    args = ap.parse_args()

    if args.probe:
        return probe()

    now = now_cst()
    today = _date(args.today) if args.today else now.replace(
        hour=0, minute=0, second=0, microsecond=0)
    today_s = today.strftime("%Y-%m-%d")
    slot = detect_slot(now)
    print(f"日期 {today_s}　时段 {slot}")

    # 本轮状态（机器可读，见 write_status）：除 --probe 外**每条退出路径都写**
    state = {"gate_open": False, "recorded": 0, "reason": "empty", "detail": ""}
    try:
        rows = load_ledger(LEDGER_PATH)
        print(f"账本 {len(rows)} 条 → {LEDGER_PATH}")

        # 当日 quotes.json 的 name/code 索引：选股复用行情，复盘兼做 secid 兜底
        by_code, by_name = _load_quotes_maps(today_s)

        # 0) 闸门：pm 走收盘闸门（未改，见 close_ready 的 docstring）；
        #    am 走盘前闸门（新增，见 am_gate/premarket_ready）
        bench, bench_q = fetch_bench()
        base_date = None
        if slot == "am":
            gate_ok, why, gate_reason, base_date = am_gate(bench_q, today)
            if args.force:
                print(f"[warn] --force：跳过盘前闸门（{why}）——仅限本地测试")
                gate_ok, gate_reason = True, ""
                if not base_date:
                    # 闸门被判住时 base_date 也是空的（休市 / 上一交易日求不出来）。
                    # --force 的契约就是"跳过闸门、会写脏数据"，所以给一个自然日兜底 ——
                    # 不然入账会因为"没有基准日"整批失败，--force 在 am 下等于没用。
                    base_date = (today - timedelta(days=1)).strftime("%Y-%m-%d")
                    print(f"[warn] --force：上一交易日求不出来，base_date 暂用自然日 "
                          f"{base_date}（T+1 基准可能偏一天）")
            else:
                print(f"{'[OK]' if gate_ok else '[warn]'} 盘前闸门：{why}")
            # 盘前拿不到"今日收盘价"：收盘闸门天然未开 → 本轮不做复盘打分
            # （与改造前一致：打分的行情是"今天收盘价"，只有 pm 那次运行拿得到）
            ok_close = False
        else:
            ok_close, why = close_ready(bench_q, today)
            if args.force:
                print(f"[warn] --force：跳过收盘闸门（{why}）——仅限本地测试")
                ok_close = True
            else:
                print(f"{'[OK]' if ok_close else '[warn]'} 收盘闸门：{why}")
            gate_ok, gate_reason = ok_close, ""
        state["gate_open"] = bool(gate_ok)
        # 机器可读的日历口径（2026-10-04 加）：盘前通道的"今天是不是交易日"在
        # **今天没有 K 线** 时只能走内置近似日历，而那张表只核实过
        # FALLBACK_HOLIDAY_YEARS。进入未核实年份后，盘前清单会**每天**被判成休市
        # 而静默停摆 —— 把口径写进状态文件，送达体检就能把这件事喊出来
        # （见 healthcheck.py 的「盘前交易日历未覆盖该年份」检查）。
        if slot == "am":
            state["calendar"] = calendar_kind(today)

        if not gate_ok and not rows:
            print("闸门未开且账本为空，无事可做")
            state.update(reason=(gate_reason or "gate_closed"), detail=why)
            save_ledger(LEDGER_PATH, rows)
            return 0

        # 1) 记录今日候选：pm 只在盘后、且今日已收盘；am 走盘前通道
        if args.score_only:
            state.update(detail="--score-only：本轮不选股，只做到期复盘")
            print("--score-only：跳过选股，只做到期复盘")
        elif slot == "am":
            # ---- 盘前通道：产出「今日可执行观察清单」（基准 = 上一交易日收盘）----
            if not gate_ok:
                state.update(reason=(gate_reason or "gate_closed"), detail=why)
                print(f"盘前闸门未开，本轮不产出观察清单（{why}）")
            else:
                record_premarket(rows, today_s, bench, by_code, by_name,
                                 base_date, state)
        elif not ok_close:
            state.update(reason="gate_closed", detail=why)
            print("闸门未开，本轮不记录新候选（只在今日收盘后记录）")
        elif slot != "pm":
            state.update(detail="盘前不记录新候选（只做到期复盘）")
            print("盘前不记录新候选（盘前拿到的现价是昨收，当基准会把 T+1 拉成两天）；"
                  "盘前只做到期复盘")
        elif not (DATA_DIR / "analysis.md").exists():
            state.update(detail="无 data/analysis.md 材料，跳过选股")
            print("[warn] data/analysis.md 不存在，跳过选股")
        else:
            try:
                news = json.loads(
                    (DATA_DIR / "structured_news.json").read_text(encoding="utf-8"))["news"]
                quotes_json = json.loads(
                    (DATA_DIR / "quotes.json").read_text(encoding="utf-8"))
                analysis_md = (DATA_DIR / "analysis.md").read_text(encoding="utf-8")
            except Exception as e:
                # 材料不在/坏掉：本轮无候选，但复盘照常
                state.update(reason="empty",
                             detail=f"读取材料失败: {type(e).__name__}: {str(e)[:60]}")
                print(f"[warn] 材料读取失败，跳过选股: {type(e).__name__}: {str(e)[:80]}")
            else:
                if not str(quotes_json.get("generated_at") or "").startswith(today_s):
                    print(f"[warn] quotes.json 生成于 {quotes_json.get('generated_at','?')}，"
                          "非今日：基准价将现抓，且本轮不做 secid 兜底")
                try:
                    digest, count = M3.build_news_digest(news)
                    cands = ask_candidates(digest, count, analysis_md,
                                           quotes_json.get("quotes", []))
                except Exception as e:
                    # 可选产出：选股失败不该影响复盘，也不该掐断主链路
                    state.update(reason="llm_error",
                                 detail=f"{type(e).__name__}: {str(e)[:80]}")
                    print(f"[warn] 选股失败（不影响复盘）: {type(e).__name__}: {str(e)[:80]}")
                else:
                    if not cands:
                        reason, detail = _pick_reason(LAST_LLM)
                        state.update(reason=reason, detail=detail)
                        print("[warn] 未产出合格候选（LLM 失败或全部未通过校验）")
                    else:
                        try:
                            board_map = build_board_map() if any(
                                c["kind"] == "board" for c in cands) else {}
                            n = record(rows, today_s, slot, cands, bench, board_map, {},
                                       by_code, by_name)
                        except Exception as e:
                            state.update(reason="empty",
                                         detail=f"入账失败: {type(e).__name__}: {str(e)[:60]}")
                            print(f"[warn] 候选入账失败: {type(e).__name__}: {str(e)[:80]}")
                        else:
                            state["recorded"] = n
                            print(f"[OK] 记录 {n} 条候选")
                            state.update(reason="ok",
                                         detail=(f"新入账 {n} 条" if n else
                                                 "候选已在账本中（幂等跳过），本轮无新增"))

        # 2) 给到期候选打分。**纳入 try**：打分段读的是历史行（可能有脏字段），
        #    它出问题只该放弃本轮打分，不该吞掉上面已经算好的记录
        try:
            if not ok_close:
                print("闸门未开，本轮不打分（到期即补，下次运行会补上）")
            elif any(_has_due(r, today) for r in rows):
                n = score_pending(rows, today, bench, by_name=by_name,
                                  by_code=by_code, cache={})
                print(f"[OK] 回填 {n} 档复盘")
            else:
                print("今日无到期复盘")
        except Exception as e:
            print(f"[warn] 复盘打分失败（已算好的记录照常写回）: "
                  f"{type(e).__name__}: {str(e)[:80]}")

        if save_ledger(LEDGER_PATH, rows):
            print(f"[OK] 账本已写回 {LEDGER_PATH}（{len(rows)} 条）")
        else:
            print(f"[OK] 账本无变化（{len(rows)} 条）")
        return 0
    except Exception as e:
        # 闸门/账本阶段的未预期失败：状态文件的 reason 枚举里没有 crash，用默认的
        # empty 配上 detail 说明，至少让 M6 体检能读到「这轮为什么什么都没有」；
        # 异常照旧抛出（daily.yml 对 M10 是 continue-on-error，行为不变）
        if state["reason"] == "empty" and not state["detail"]:
            state.update(detail=f"未预期失败: {type(e).__name__}: {str(e)[:80]}")
        print(f"[warn] M10 本轮异常: {type(e).__name__}: {str(e)[:80]}")
        raise
    finally:
        write_status(today_s, slot, state)


def _has_due(row, today):
    """本轮是否有到期/可判逾期的档 → main 据此决定要不要进打分。

    与 score_pending 同口径：基准日 d0 = base_date（am 行）或 date（pm 行），
    且只看**最早未填**的那一档（due 随 k 单调递增，故最早未填档就是最早到期的档，
    一旦它没到期，后面几档更没到期）。日历取不到时 due() 内部已降级为近似日历，
    不抛异常。
    """
    try:
        d0 = _date(row.get("base_date") or row["date"])
    except Exception:
        return False
    rv = row.get("reviews")
    unfilled = [k for k in REVIEW_DAYS
                if not isinstance(rv, dict) or rv.get(str(k)) is None]
    if not unfilled:
        return False
    return today >= due(min(unfilled), d0)


if __name__ == "__main__":
    sys.exit(main())
