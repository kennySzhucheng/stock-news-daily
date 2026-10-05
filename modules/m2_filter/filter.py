# -*- coding: utf-8 -*-
"""
M2 新闻筛选与结构化模块
用 GLM-4-Flash（免费）对 M1 产出的原始新闻做分类、去重校验、关联提取、可信度打标。

输入: data/raw_news.json
输出: data/structured_news.json

另有**跨天事件追踪**（2026-10-05 新增，纯数据侧）：把每天保留下来的新闻算一个保守
的事件指纹，与 `reports/event_index.jsonl` 里最近若干天的历史记录比对，命中的新闻
追加一个 `continuing` 字段（"持续关注第 N 天" + 首次出现日期 + 历史来源家族）。
展示层由 M5/M9 负责。为什么索引必须落在 `reports/`：`data/` 每次运行开始时被清空
重建、不跨天留存，而 `reports/` 随 gh-pages 发布并在每轮开始时恢复。

密钥来源: 环境变量 ZAI_API_KEY（不硬编码、不打印）
"""
import hashlib
import json
import os
import re
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
DEFAULT_DATA_DIR = BASE / "data"
DATA_DIR = DEFAULT_DATA_DIR
_DEFAULT_DATA_DIR = DEFAULT_DATA_DIR   # main() 据此判断 DATA_DIR 是否被外部（离线用例）改写
REPORTS_DIR = BASE / "reports"

GLM_API = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
GLM_MODEL = "glm-4-flash"

VALID_CATEGORIES = ["policy", "industry", "stock", "international", "other"]

# ---------------------------------------------------------------------------
# 独立源家族（2026-10-04 新增，服务交叉验证）
# ---------------------------------------------------------------------------
# 依据：同一个出版方会以多个"频道"形式出现在 M1 的源清单里（见 collector.py 的
# SOURCES：`东方财富7x24`/`东方财富宏观政策`/`新浪财经7x24`/`新浪财经滚动`）。
# 这些频道**不是独立来源**——它们转载/复用同一套稿子，让它们互相"印证"会产出
# 假的 confirmed。故交叉验证前先把频道名归一到"出版方家族"。
# 10-04 线上逐条核验（见验收报告）：旧逻辑标的 6 条 confirmed 里，
# 2 条是同族自证（东方财富 ↔ 东方财富-宏观政策），另 4 条连"同一事件"都不成立。
#
# 写法容错：线上的源名既有"东方财富-宏观政策"（带连字符）也有
# "东方财富宏观政策"（无连字符，collector.py 里的写法），故这里列的是**前缀主干**，
# 用带边界的前缀匹配兜住两种写法，避免写死连字符。
#
# ⚠️ 新华网 / 人民网 / 中国政府网 **各自独立**，绝不并成一家：
# 它们会各自采写、也互为转载源，合并等于自造假 confirmed。
FAMILY_PREFIXES = (
    ("东方财富", "eastmoney"),
    ("新浪财经", "sina"),
    ("新浪", "sina"),
    ("新华网", "xinhua"),
    ("人民网", "people"),
    ("中国政府网", "gov_cn"),
)

# 家族前缀的连接符：归一化时先删掉这些，使"东方财富-宏观政策"与
# "东方财富宏观政策"都能被 FAMILY_PREFIXES 匹配到
_FAMILY_JOINERS = "-－—_・· "


def source_family(source):
    """把来源名归一到"独立源家族"ID。

    未知源一律视为独立（返回其归一化后的自身名字）——宁可保守：把两个真的不
    同源的媒体合并会让 confirmed 消失，把同族拆开才会造假 confirmed，而这里的
    默认分支只会让未知源**各自独立**，不会与任何已知源合并。
    """
    s = (source or "").strip()
    if not s:
        return ""
    for joiner in _FAMILY_JOINERS:
        s = s.replace(joiner, "")
    low = s.lower()
    for prefix, family in FAMILY_PREFIXES:
        if low.startswith(prefix.lower()):
            return family
    return low


def independent_source_count(sources):
    """一组来源名里有几个**独立源家族**。未知源各自计一个（保守）。"""
    return len({source_family(s) for s in (sources or []) if (s or "").strip()})


def event_sources(n):
    """取一条新闻"报道过该事件的全部来源"。

    M1（同事改造后）会在 raw_news.json 每条上写 `sources: list[str]`：报道过同一
    事件的所有来源，已去重排序，至少含自己的 `source`。

    容错顺序：
      ① `sources` 非空 → 直接用（并补上 `source`，因为 M1 的契约说它应已包含，
         但多算一个自己的名字不会改变家族数，属于兜底）；
      ② 否则退回 `[source]` —— 退化为旧行为（单源，必然 unverified）；
      ③ 两者都没有（异常数据）→ 返回 []，由调用方计入 unverified 计数。
    """
    raw = n.get("sources")
    if isinstance(raw, list):
        srcs = [s for s in raw if isinstance(s, str) and s.strip()]
        if srcs:
            own = n.get("source")
            if isinstance(own, str) and own.strip() and own not in srcs:
                srcs = srcs + [own]
            return srcs
    own = n.get("source")
    if isinstance(own, str) and own.strip():
        return [own]
    return []


def apply_cross_verification(rows):
    """交叉验证：代码层**确定性**比对，不用 LLM 判断（实测 LLM 判断不可靠）。

    ## 语义变更（2026-10-04，务必读完再改）
    旧规则（已删除）：把正文去掉标点/数字后取**前 16 字**做主键分组，同组内出现
    ≥2 个不同 `source` 即 confirmed。

    ① 它与 M1 的跨源去重**直接冲突**：M1 的 `collect`（collector.py 里的
       "跨源去重"段）先按"正文前 25 字"把多源重复合并成一条，第二个来源在进入
       M2 时就已消失 —— 于是"前 16 字相同且 ≥2 源"几乎不可能由真的多源重复
       触发；10-04 实测 confirmed 只有 6/144（4.2%），09-30 盘后 0/135。
    ② 它还会把**同一来源的两篇不同报道**（前 16 字恰好相同的巧合）算成
       "多源印证"——这是纯假阳性，比漏报更危险。

    新规则：改用 M1 聚合好的 `sources` 字段（"报道过同一事件的所有来源"），
    把来源归一到独立家族后，**独立源家族数 ≥2 → confirmed**。这比旧的文本前缀
    比对更准确：不依赖两家媒体措辞恰好一致（同一事件各家标题写法本就不同），
    也天然免疫"同族频道互相印证"。

    新规则：改用 M1 聚合好的 `sources` 字段（"报道过同一事件的所有来源"），
    把来源归一到独立家族后，**独立源家族数 ≥2 → confirmed**。这比旧的文本前缀
    比对更准确：不依赖两家媒体措辞恰好一致（同一事件各家标题写法本就不同），
    也天然免疫"同族频道互相印证"。

    家族归一后，同一家族内部有几个频道都只算 1 个独立源（见 FAMILY_PREFIXES）。

    返回 (rows, stats)，stats 供输出文件与日志使用。
    """
    for n in rows:
        srcs = event_sources(n)
        n["verified"] = ("confirmed" if independent_source_count(srcs) >= 2
                        else "unverified")
    no_source = sum(1 for n in rows if not event_sources(n))
    no_field = sum(1 for n in rows if not isinstance(n.get("sources"), list)
                   or not n.get("sources"))
    fams = {source_family(s) for n in rows for s in event_sources(n)}
    stats = {
        "confirmed": sum(1 for n in rows if n.get("verified") == "confirmed"),
        "no_sources_field": no_field,     # 退化到旧的 [source] 行为
        "no_source_at_all": no_source,    # 异常数据（连 source 都没有）
        "distinct_families": len(fams - {""}),
    }
    return rows, stats


# ---------------------------------------------------------------------------
# 跨天事件追踪（2026-10-05 新增）
# ---------------------------------------------------------------------------
# 用户痛点：日报每天独立看，看不出"这条新闻昨天已经报过"——同一个事件被当成新消息
# 反复呈现，早上读的时候只觉得重复，也判断不出进展。
#
# 设计原则（按优先级）：
#   ① **误判的代价远大于漏判**。漏判 = 少写一句"持续第 2 天"，用户看到的和今天一样；
#      误判 = 告诉用户"这个事件已经连续 3 天了"，而它其实是三件不同的事 —— 用户会
#      据此以为某条逻辑在被反复验证，这是**编造事实**。所以阈值一律往保守取，
#      并且用 `confidence` 把"强匹配"和"弱匹配"分开，让展示层自己决定敢不敢写"第 N 天"。
#   ② 模块化、可离线测：全部是纯函数 + `path=None` 注入，常量模块级（测试可改），
#      全程不联网、不调 LLM。
#   ③ **任何失败只能 warn**：M2 挂掉当天就没有结构化新闻，代价远大于这个功能。

# 指纹算法版本。**改了归一化/阈值/比对逻辑就必须把这个数字 +1**——
# 索引是跨天累积的，新旧指纹混在一份文件里必须能分辨（版本对不上的行会被
# `known_fp` 跳过，宁漏不误）。
EVENT_FP_VERSION = 1

# 跨天事件追踪总开关（模块级，便于离线用例把它关掉）。
#
# 为什么需要它：离线用例会把 `DATA_DIR` 指到临时目录（见 tests/offline_tests.py
# 的 M2TimeBudget），但**不会**动本模块的索引路径 —— 于是那 60 条假新闻会被写进
# **真实的** `reports/event_index.jsonl`，污染跨天数据（这正是我第一版实现踩到的坑：
# 跑一次 offline_tests 就在仓库里凭空多出 60 行 2026-10-05 的假记录）。
# `main()` 在 `DATA_DIR` 被外部改写时自动跳过索引维护（不会崩，只是不维护），
# 所以这个开关平时不需要手动关。
EVENT_INDEX_ENABLED = True

# 索引保留天数（裁剪口径）：最近 30 天。
EVENT_INDEX_DAYS = 30
# 索引总行数上限：超过就丢**最旧**的行（保新）。6000 行 ≈ 每天 200 条 × 30 天，
# 对本项目当前体量（180 条/天）有 3 倍余量。
EVENT_INDEX_MAX = 6000
# 比对窗口：只看 `0 < 今天 - d <= 10` 天，且 `d < 今天`（不含今天）。
# 为什么 10 天：一个事件连续 10 天还挂在新闻流里已经属于长期主题，再往前的记录
# 与"今天这条是不是老事件"的相关性已经很弱，而窗口越宽误判概率越高。
EVENT_WINDOW_DAYS = 10
# 索引行里 `t` / `x` 的截断长度（`x` 直接参与相似度比对，必须固定，见 known_fp）。
EVENT_TITLE_CHARS = 80
EVENT_TEXT_CHARS = 200
# `continuing.families` 最多给几个来源家族。
EVENT_FAMILIES_MAX = 4

# ---------------------------------------------------------------------------
# 指纹阈值（保守值；选值依据、对照样例与真实语料压力测试见 `event_match`）
# ---------------------------------------------------------------------------
# 三条**并列**通道（任一成立即算命中，但都要先过"数字冲突硬拒"）：
#   A 强文本关：去套话 2-gram Jaccard ≥ EVENT_JACCARD_STRONG（0.60）
#   B 内容关  ：Jaccard ≥ 0.25 且 重叠系数 ≥ 0.45 且 **有实质共享实体**
#   C 实体关  ：名称骨架覆盖率 ≥ 0.50 且 有同一数字（如都写"12亿元"）
EVENT_JACCARD_STRONG = 0.60
EVENT_JACCARD_MIN = 0.25
EVENT_OVERLAP_MIN = 0.45
# 通道 D（同名同额）用的稍宽门槛：它必须同时满足"≥4 字共享实体的名称" +
# "≥4 字的同一金额"，误判面已经很窄，重叠系数放到 0.30 即可。
EVENT_OVERLAP_WEAK = 0.30
EVENT_NAME_WEAK = 0.33
EVENT_NAME_ONE_WAY = 0.50
# "实质共享实体" = 最长共享汉字串 ≥ 4 字，或名称骨架覆盖率 ≥ 0.70。
# 为什么是 4 字：3 字只会命中"新一代""亚运会"这类**品类词**，
# 「宁德时代发布新一代电池」vs「比亚迪发布新一代电池」就卡在 3 字上（ms=3）——
# 那是两条不同公司、不同产品的新闻，必须拒。
EVENT_SPAN_MIN = 4
EVENT_NAME_MIN = 0.50
EVENT_NAME_HI = 0.70
# 实体关里"同一个数字"的最小长度：1~2 字的数字（"1家""2月"）到处都是，
# 只有 ≥4 字（"12亿元""5000亿元""30356.8万人次"）才算证据。
# 注意 3 字的"18时""9亿元""2亿元"被刻意排除在外 —— 那是**时间/泛指**数字，
# 真实语料里"自然资源部…10月4日18时"与"水利部…10月4日18时"会因此被误判成同一事件。
EVENT_NUM_MATCH_MIN = 4
# 命中强度 → `confidence` 的分界：
#   exact  = 去套话 Jaccard ≥ 0.90（几乎逐字相同）
#   strong = 走通道 A 或 B（有长文本骨架 / 长实体的支持）
#   weak   = 只走通道 C（措辞完全不同，全靠"同一名称 + 同一数字"）
# 展示层只在 confidence == "high" 时写"持续关注第 N 天"是安全的。
EVENT_EXACT_JACCARD = 0.90

# 去掉这些**结构套话**后再比内容（不做分词，直接删子串）。
# 为什么必须要这一步：财经稿的骨架高度雷同（"XX公司发布公告，同比…"），
# 不删这些，两条讲不同事情的同板块新闻光靠"公司/发布/同比/增长"就能刷到 0.3+ 的
# 字符相似度，直接把阈值淹掉。表里的词都是"任何一条财经稿都可能出现"的：
# 动词/连词/时间词/体裁词/公司后缀。行业专有名词**不删**——那才是判定依据。
EVENT_BOILERPLATE = (
    "公司", "同比", "环比", "增长", "下降", "发布", "公告", "表示", "记者", "报道",
    "消息", "相关", "进行", "以及", "已经", "预计", "认为", "指出", "显示", "数据",
    "方面", "其中", "目前", "情况", "分析", "证券", "新闻", "有限", "股份", "集团",
    "控股", "中国", "市场", "今日", "昨日", "今年", "去年", "上午", "下午", "晚间",
    "日电", "称", "将", "已", "或", "和", "与", "及", "等", "为", "在", "的", "了",
    "是", "有", "就", "对", "从", "到", "据",
)

# 实体候选里"太泛"的成分：机构名主干（央行/政府/委员会）、国家名、公司后缀、
# 时间量词。含这些成分的候选串不算"名称"。
# 为什么必须剔："中国气象局"与"水利部和中国气象局"里真正共享的只是"中国"+体裁，
# 「国务院部署促进消费」vs「国务院部署秋冬农业生产」更是只共享"国务院"——
# 泛词共享不能当同一事件的证据。剔掉之后，这些案例的名称覆盖率掉到 0.5 以下
# 或最长共享串只剩 2~3 字，通道 B/C 都进不去。
EVENT_GENERIC = (
    "中国", "美国", "日本", "欧洲", "欧盟", "俄罗斯", "乌克兰", "全球", "国际",
    "国家", "全国", "全网", "央行", "银行", "政府", "部门", "会议", "委员会",
    "有限公司", "公司", "集团", "股份", "证券", "基金", "指数", "市场",
    "月", "日", "号", "年", "时", "分", "电", "第", "届", "次",
)

# 归一化时剔掉的字符：空白 + 中英文标点（保留字母/数字/汉字，
# 因为数字是金额/比例这类最重要的实体）。
_EVENT_PUNCT = re.compile(
    r"[\s\u3000!-/:-@\[-`{-~！-／：-＠［-｀｛-～、。，；：？！“”‘’"
    r"（）《》〈〉【】〔〕—…·「」『』]+")
# 数字实体：数字 + 可选量级词，后面再吃掉一个连续单位序列（元/亿/万/吨/人次…）。
# 不做这一步的话「12亿元」在字符 2-gram 里只能和「12亿元」逐字相同才算共享，
# 而实际报道里有「12亿」「12亿元」「12 亿元」三种写法。
_EVENT_NUM = re.compile(r"\d+(?:\.\d+)?(?:[万亿千百])?(?:%|％)?")
_EVENT_UNIT_CHARS = "元万亿千百吨人家倍个百分点月日号年季周天次辆架枚"
_EVENT_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")
# 这些数字只是日期/序数（"第3季度"的 3），单独出现不构成"同一实体"的证据，
# 避免两条新闻仅仅因为都提到"10月""20%"就被判成同一事件。
_EVENT_NUM_STOP = {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10",
                   "11", "12", "20", "30", "50", "60", "100", "1000", "10000"}


def event_reports_dir():
    """索引该落在哪个 `reports/`：仓库根的 `reports/`（测试可改写 `REPORTS_DIR`）。"""
    return REPORTS_DIR


def event_index_path():
    """滚动索引文件路径。每次调用都重算，便于测试改 `REPORTS_DIR`。"""
    return REPORTS_DIR / "event_index.jsonl"


def event_index_default_path():
    """`main()` 用的索引路径；`EVENT_INDEX_ENABLED=False` 时返回 None（不维护索引）。"""
    return event_index_path() if EVENT_INDEX_ENABLED else None


def normalize_event_text(text, cap=EVENT_TEXT_CHARS):
    """比对用的规范化文本：去掉全部空白与标点，保留汉字/字母/数字，截前 cap 字。

    不做分词：中文没有空白边界，而这里的要求只是"同一事件的不同措辞要能对上"，
    字符 n-gram 正好不依赖分词器（也不需要联网装 jieba）。
    """
    s = _EVENT_PUNCT.sub("", str(text or ""))
    return s[:cap]


def event_grams(text, n=2):
    """字符 n-gram 集合（去重）。文本短于 n 时返回空集。"""
    s = _EVENT_PUNCT.sub("", str(text or ""))
    return {s[i:i + n] for i in range(0, len(s) - n + 1)}


def _event_strip_boilerplate(text):
    s = _EVENT_PUNCT.sub("", str(text or ""))
    out = s[:EVENT_TEXT_CHARS]
    for b in EVENT_BOILERPLATE:
        out = out.replace(b, "")
    return out


def event_num_tokens(text, cap=EVENT_TEXT_CHARS):
    """数字实体集合：数字 + 量级 + 连续单位。例：`12亿元` / `5000亿` / `30356.8万人次`。

    超过 `EVENT_TEXT_CHARS` 的部分不参与——与 `x` 的截断口径保持一致，
    否则会出现"索引里存了前 200 字、指纹却按全文算"的静默错位。
    """
    s = _EVENT_PUNCT.sub("", str(text or ""))[:cap]
    out = set()
    for m in _EVENT_NUM.finditer(s):
        j = m.end()
        while j < len(s) and s[j] in _EVENT_UNIT_CHARS:
            j += 1
        tok = s[m.start():j]
        if tok and tok not in _EVENT_NUM_STOP:
            out.add(tok)
    return out


def event_name_tokens(text, cap=EVENT_TEXT_CHARS):
    """名称实体候选：先删结构套话，再取所有连续汉字串（长度 2~12）的 2/3/4-gram，
    并剔除含 `EVENT_GENERIC` 成分的候选。

    为什么用 n-gram 而不是分词：没有词典也不联网的前提下，"甲公司""宁德时代"
    "维谛技术"这些词无法靠规则切出来，但它们的 4-gram 一定在集合里 —— 交给下游的
    "最长共享串 ≥4 字"去筛即可。
    为什么长度 >12 的连续汉字串整段跳过：那是无标点的长句（标题正文连排），
    会产生上百个无意义 2-gram，把内存和比对时间都推上去。
    """
    s = _event_strip_boilerplate(text)
    out = set()
    for m in _EVENT_CJK_RUN.finditer(s):
        run = m.group(0)
        if len(run) > 12:
            continue
        for n in (4, 3, 2):
            for i in range(0, len(run) - n + 1):
                t = run[i:i + n]
                if any(g in t for g in EVENT_GENERIC):
                    continue
                out.add(t)
    return out


def _event_common_span(a, b):
    """两个串的最长公共连续片段长度。

    为什么要它：线上同一实体写法经常被标点/数字切开——一条写「宁德时代」、
    另一条因为标题里带了数字被切成「时代」。纯全等比较会把它们算成"没有共享
    实体"，于是同一事件被判成两个。串都不长（≤12），三重循环完全够快。
    """
    best = 0
    for i in range(len(a)):
        for j in range(i + best + 1, len(a) + 1):
            if a[i:j] in b:
                best = j - i
            else:
                break
    return best


def _event_pair_span(a, b):
    """两个串的"共享长度"：一方包含另一方取较短者，否则取最长公共片段。"""
    if a in b or b in a:
        return min(len(a), len(b))
    return max(_event_common_span(a, b), _event_common_span(b, a))


def _event_max_pair_span(set_a, set_b, min_len=2):
    """两组串里最大的共享长度；都不超过 `min_len` 时返回 0。"""
    best = 0
    for a in set_a:
        for b in set_b:
            s = _event_pair_span(a, b)
            if s > best:
                best = s
    return best if best >= min_len else 0


def _event_cover_ratio(set_a, set_b):
    """`set_a` 里有多少比例的串能在 `set_b` 里找到（共享 ≥2 字连续片段）。

    2 字门槛是刻意的：1 个字（"铁""桥"）在中文里到处都是，拿它当"同一实体"等于
    没有门槛。
    """
    if not set_a:
        return 0.0
    hit = 0
    for a in set_a:
        for b in set_b:
            if _event_pair_span(a, b) >= 2:
                hit += 1
                break
    return hit / len(set_a)


def event_fingerprint(text, cap=EVENT_TEXT_CHARS):
    """事件指纹（sha1 of 规范化文本前 cap 字）。

    ⚠️ **不要用内置 `hash()`**：CPython 对 str 的 `hash()` 带进程随机盐
    （PYTHONHASHSEED），同一段文本在两个进程里算出来的值不同 —— 索引要跨天、跨进程
    复用，用 `hash()` 等于每天全量失效，而且这种失效是静默的（看起来只是"没匹配上"）。
    """
    return hashlib.sha1(normalize_event_text(text, cap).encode("utf-8")).hexdigest()


def event_candidate(text):
    """一条新闻的全部比对素材（算一次，供 `known_fp` / `event_match` 复用）。"""
    norm_x = normalize_event_text(text)
    return {
        "x": norm_x,
        "fp": event_fingerprint(text),
        "g_raw": event_grams(norm_x, 2),                             # 原样 2-gram
        "g_strip": event_grams(_event_strip_boilerplate(text), 2),    # 去套话 2-gram
        "nm": event_name_tokens(text),
        "nu": event_num_tokens(text),
    }


def known_fp(rec):
    """索引行 → 可直接比对的素材；认不出来的行返回 None。

    兼容策略（跨版本升级的必经之路，写在这里免得被当成死代码删掉）：
      · `x` 是判定基准，缺了就跳过（旧版行 / 写坏的截断行）；
      · `fp` 与按 `x` 现算的值不一致时把 `fp` 清空 —— 于是这一行走不到"指纹完全
        一致"的快通道，退化为纯 n-gram + 实体判定。宁可多算一点，也不能拿旧算法
        或别人文本的指纹去声称"强匹配"；
      · `v != EVENT_FP_VERSION` 的行一律跳过：算法都换了，旧行的相似度语义不可比。
    """
    if not isinstance(rec, dict):
        return None
    if rec.get("v") is not None and rec.get("v") != EVENT_FP_VERSION:
        return None
    x = str(rec.get("x") or "")
    if not x:
        return None
    fp = str(rec.get("fp") or "")
    if fp and fp != event_fingerprint(x, len(x)):
        fp = ""
    return {"x": x, "fp": fp, "nm": event_name_tokens(x, len(x)),
            "nu": event_num_tokens(x, len(x)),
            "g_raw": event_grams(x, 2),
            "g_strip": event_grams(_event_strip_boilerplate(x), 2)}


def _event_jaccard(sa, sb):
    return len(sa & sb) / len(sa | sb) if (sa and sb) else 0.0


def _event_overlap(sa, sb):
    return len(sa & sb) / min(len(sa), len(sb)) if (sa and sb) else 0.0


def event_match(cand, hist, fp=None):
    """候选新闻 `cand` 与历史记录 `hist` 是否同一事件。

    返回 `(level, score, detail)`，level ∈ {None, "exact", "strong", "weak"}；
    `detail` 是判据本身的数字（不是"看着像"），便于事后核查与调阈值。

    ## 判据
    0. **数字冲突硬拒**：双方都含数字实体，却没有任何一对能互含/共享 2 字以上
       —— 直接判不匹配。两个不同的事件几乎不可能共用一个金额/比例，
       而"同一板块"的两条新闻几乎必然带不同的数字。这是最便宜也最有效的一道闸门。
       反例守卫：「甲公司拟回购2亿元」vs「甲公司拟减持2亿元」→ 数字相同、名称相同，
       但回购与减持是反向事件，靠下面通道 D 的 Jaccard 下限挡住。
    1. **通道 A（强文本）**：去套话 2-gram Jaccard ≥ 0.60（`EVENT_JACCARD_STRONG`）。
       措辞高度一致，多数是同一稿的两次转载。
    2. **通道 B（内容）**：Jaccard ≥ 0.25 且 重叠系数 ≥ 0.45 且
       名称骨架覆盖率 ≥ 0.50 且（最长共享名称串 ≥ 4 字 或 覆盖率 ≥ 0.70）。
    3. **通道 D（同名同额）**：最长共享名称串 ≥ 4 字 且 名称覆盖率（双向取小）≥ 0.33
       且 单向覆盖率 ≥ 0.50 且 有一对**互含**且 ≥4 字的相同数字，
       且 Jaccard ≥ 0.25、重叠系数 ≥ 0.30。
       用于"一条是快讯、一条是详稿"这种措辞差异较大、但公司与金额都对得上的情况
       （真实案例：「英国国家电网…希舍姆1号核电站…停止运行」vs「…11号核反应堆…
       已恢复并网」）。为什么要用"单向覆盖率"：短快讯里的实体能全部出现在详稿里，
       反过来则不成立（详稿多出来的实体不该扣分）。

    ## 阈值是怎么选的
    在**真实语料**（data/structured_news.json，180 条 × 两两 = 14706 对，
    其中共享实体的 2720 对）上逐条人眼核对：
      · 通道 A 命中的每一对都是同一事件（多为同一天 M1 跨源去重后的残留近似稿）；
      · 反面样例「乙公司发布三季报」vs「丙公司获补贴」的 Jaccard 是 0.00，
        「国务院部署促进消费」vs「国务院部署秋冬农业生产」是 0.19，
        都远在 0.25 之下 —— 0.25 落在两者之间很宽的空带里；
      · 最难的对照「宁德时代发布新一代电池」vs「比亚迪发布新一代电池」：
        Jaccard 0.364、覆盖率 0.50、最长共享串只有 3 字（"新一代"）→
        A/B/D 三条通道全部进不去。**这正是把"最长共享串"门槛定在 4 字的原因**；
      · 「光伏组件价格上涨」vs「光伏组件价格下跌」：Jaccard 0.294、覆盖率 0.50，
        最长共享串 4 字（"光伏组件"）→ 通道 B 一度误判，所以 B 的名称覆盖率
        门槛从 0.50 提到 0.70（该例只有 0.50）。同类的「中信证券看好银行板块」vs
        「看好券商板块」也一并拦下；
      · 「自然资源部…地质灾害预警」vs「水利部…山洪灾害预警」：两条预警同一天发布、
        时间窗与落区都不同 —— 通道 D 的"数字 ≥4 字"门槛就是为了不让"18时/20时"
        这种 3 字时间数字把它们撮合到一起。
    用**已落地的实现**重跑真实语料（172 条 / 14706 对，其中共享实体 2720 对）：
    **15 对命中**，逐条人眼核对全部是同一事件的不同报道（多数是 M1 跨源去重后
    同一天残留的近似稿），**没有一条误判**。

    ## 会漏判什么（已知，且接受）
      · 「甲公司中标12亿元订单」vs「甲公司获12亿元大单，机构看好」这种**极短**标题
        （双方正文都只有 10 来字）：Jaccard 0.19、名称覆盖率 0.16、最长共享名称串
        2 字（"公司"）—— 三条通道全部进不去。原因是**删结构套话时"公司后缀"被删掉**，
        共享的"甲公司"在字面上变成了"甲中"/"甲获"。要救它只能把"公司"重新算作实体，
        而那样做会让「乙公司发布三季报」这类只共享泛化后缀的对也能通过，
        真实语料上的误判会立刻涨起来。取舍是**故意放弃**这一类超短标题。
      · 同一事实用不同数字口径（"净利 +20%" vs "净利增两成"）——数字冲突硬拒。
      · 「国家能源局发布新型储能政策」vs「新型储能政策落地：能源局明确…」这类
        只有品类词重叠、没有公司名/金额的行业稿。
    这些都是"少标一天持续关注"，代价远小于把三件不同的事说成同一件。
    """
    if not isinstance(cand, dict) or not isinstance(hist, dict):
        return None, 0.0, ""
    nu_a, nu_b = cand.get("nu") or set(), hist.get("nu") or set()
    # 只有**互含**才算"同一个数字"：'12亿元' 与 '12亿元' 互含；'18时' 与 '10月4日18时'
    # 也互含，所以再用下面的长度门槛把它挡住。
    nu_span = max([min(len(t), len(x)) if (t in x or x in t) else 0
                   for t in nu_a for x in nu_b] or [0])
    if nu_a and nu_b and not (nu_a & nu_b) and nu_span == 0:
        return None, 0.0, ""                      # 0. 数字冲突
    gs_a, gs_b = cand.get("g_strip") or set(), hist.get("g_strip") or set()
    jb = _event_jaccard(gs_a, gs_b)
    ov = _event_overlap(gs_a, gs_b)
    jr = _event_jaccard(cand.get("g_raw") or set(), hist.get("g_raw") or set())
    nm_a, nm_b = cand.get("nm") or set(), hist.get("nm") or set()
    nm_a_to_b = _event_cover_ratio(nm_a, nm_b)
    nm = min(nm_a_to_b, _event_cover_ratio(nm_b, nm_a))
    ms = _event_max_pair_span(nm_a, nm_b, min_len=3)
    fp = fp if fp is not None else cand.get("fp")
    fp_hit = bool(fp) and fp == hist.get("fp")
    detail = (f"jb={jb:.2f} ov={ov:.2f} nm={nm:.2f} nm1={max(nm_a_to_b, _event_cover_ratio(nm_b, nm_a)):.2f} "
              f"ms={ms} nu={nu_span} jr={jr:.2f} fp={'hit' if fp_hit else 'miss'}")
    if fp_hit and jr >= EVENT_EXACT_JACCARD:
        return "exact", 1.0, detail
    if jb >= EVENT_JACCARD_STRONG:
        return ("exact" if jb >= EVENT_EXACT_JACCARD and ov >= EVENT_EXACT_JACCARD
                else "strong"), jb, detail
    if (jb >= EVENT_JACCARD_MIN and ov >= EVENT_OVERLAP_MIN
            and nm >= EVENT_NAME_MIN and (ms >= EVENT_SPAN_MIN or nm >= EVENT_NAME_HI)):
        return "strong", jb, detail
    if (jb >= EVENT_JACCARD_MIN and ov >= EVENT_OVERLAP_WEAK
            and nm >= EVENT_NAME_WEAK and max(nm_a_to_b, _event_cover_ratio(nm_b, nm_a)) >= EVENT_NAME_ONE_WAY
            and ms >= EVENT_SPAN_MIN and nu_span >= EVENT_NUM_MATCH_MIN):
        return "weak", nm, detail
    return None, 0.0, detail


# ---------------------------------------------------------------------------
# 滚动索引的读写（唯一跨天留存手段）
# ---------------------------------------------------------------------------

def _event_index_load_raw(path):
    """读索引 → 行 dict 列表。文件不存在/读不到 → []（**静默**，不是错误）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:                                     # noqa: BLE001
        return []
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            o = json.loads(s)
        except json.JSONDecodeError:
            continue            # 坏行静默丢弃：旁路数据，不值得每天刷 warn
        if isinstance(o, dict):
            out.append(o)
    return out


def _event_index_write(path, recs):
    """整文件重写（先写 .tmp 再 replace，与 picks.py::write_status 同风格）。

    失败**只 warn**，绝不抛出：索引写不进去的后果只是"明天认不出今天"，
    而 M2 主流程失败的后果是当天没有结构化新闻。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
                       encoding="utf-8")
        tmp.replace(path)
    except Exception as e:                                # noqa: BLE001
        print(f"[warn] 事件索引写入失败（不影响 M2 主流程，今天的事件明天认不出来）: "
              f"{type(e).__name__}: {str(e)[:60]}")
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:                                 # noqa: BLE001
            pass


def _event_index_retain(lines, today_s):
    """索引行 → (要保留的行, 是否需要重写文件)。

    规则：**最近 EVENT_INDEX_DAYS 天** 且 **总数不超过 EVENT_INDEX_MAX**；
    超上限时丢**最旧**的（保新）。与 picks.py::_news_roll_retain 同口径，
    但这里不补字段——索引行是当场写出来的，格式自己说了算。

    边界：`EVENT_INDEX_MAX <= 0` 视为"不设上限"（避免把整个文件裁成空）；
    日期解析不出来的行一并丢弃（它的 `d` 不可信，且会污染窗口判断）。
    """
    try:
        newest = datetime.strptime(str(today_s), "%Y-%m-%d").date()
    except Exception:                                     # noqa: BLE001
        newest = datetime.now().date()
    oldest = newest - timedelta(days=EVENT_INDEX_DAYS - 1)
    keep = []
    for it in lines:
        if not isinstance(it, dict):
            continue
        try:
            day = datetime.strptime(str(it.get("d") or ""), "%Y-%m-%d").date()
        except Exception:                                 # noqa: BLE001
            continue
        if day > newest or day < oldest:
            continue
        keep.append(it)
    trimmed = len(keep) != len(lines)
    limit = int(EVENT_INDEX_MAX or 0)
    if limit > 0 and len(keep) > limit:
        keep = keep[-limit:]                              # 保新丢旧
        trimmed = True
    return keep, trimmed


def event_index_read(today_s, path=None, days=EVENT_WINDOW_DAYS):
    """取"参与比对"的历史记录：`d < today_s` 且 `today_s - d <= days` 天。

    不含今天：拿今天的记录跟今天比对，只会把**同一天**的多篇报道算成"持续关注"。
    按日期降序（最近的在最前）。同 `(d, fp)` 只留一条。
    """
    path = Path(path) if path is not None else event_index_path()
    try:
        newest = datetime.strptime(str(today_s), "%Y-%m-%d").date()
    except Exception:                                     # noqa: BLE001
        return []
    oldest = newest - timedelta(days=int(days))
    out, seen = [], set()
    for it in _event_index_load_raw(path):
        d = str(it.get("d") or "")
        try:
            day = datetime.strptime(d, "%Y-%m-%d").date()
        except Exception:                                 # noqa: BLE001
            continue
        if not (oldest <= day < newest):
            continue
        key = (d, str(it.get("fp") or ""))
        if key in seen:
            continue           # 同一天同一指纹只留一条（索引本身也不该重复写）
        seen.add(key)
        out.append(it)
    out.sort(key=lambda r: str(r.get("d") or ""), reverse=True)
    return out


def event_index_append(rows, today_s, path=None):
    """把今天的事件记录**追加**进索引；`(d, fp)` 已存在则跳过。

    `rows` 是 `[{"fp":…, "t":…, "f":[…], "fn":[…], "x":…}, …]`（见 `event_record`）。
    返回实际新增行数。

    写入策略：能纯追加就纯追加（`open(path,"a")`，索引是每天增长的小文件）；
    只有需要**裁剪**（超 30 天 / 超 EVENT_INDEX_MAX）时才整文件重写
    （.tmp → replace）。两种情况都在 try 里，写失败只 warn、返回 0 ——
    上层绝不能因为这个功能挂掉。
    """
    path = Path(path) if path is not None else event_index_path()
    lines = _event_index_load_raw(path)
    existing = {(str(it.get("d") or ""), str(it.get("fp") or "")) for it in lines}
    added = 0
    today = str(today_s)
    try:
        # 日期必须可解析：写进去的行最终要过 _event_index_retain 的日期裁剪，
        # 脏日期会在下一次追加时凭空把**整个文件**裁空（实测过一次，很吓人）。
        if today:
            datetime.strptime(today, "%Y-%m-%d")
    except Exception:                                     # noqa: BLE001
        print(f"[warn] 事件索引未写入：today_s 不是合法日期（{today_s!r}）"
              f"—— 不影响 M2 主流程")
        return 0
    fresh = []
    for r in (rows or []):
        if not isinstance(r, dict):
            continue
        fp = str(r.get("fp") or "")
        x = str(r.get("x") or "")
        if not fp or not x:
            continue
        if today and str(r.get("d") or today) > today:
            continue                    # 未来日期（脏数据）不进索引
        if (today, fp) in existing:
            continue                    # 同一 (d, fp) 不重复写
        rec = {"d": today, "fp": fp, "v": EVENT_FP_VERSION,
               "t": str(r.get("t") or "")[:EVENT_TITLE_CHARS],
               "f": [str(s)[:24] for s in (r.get("f") or [])][:4],
               # 家族**展示名**（"新浪财经"），与 `f` 的内部 ID（"sina"）并存：
               # 展示层要的是前者，跨版本兼容判断仍可用后者。
               "fn": [str(s)[:40] for s in (r.get("fn") or [])][:4],
               "x": x[:EVENT_TEXT_CHARS]}
        lines.append(rec)
        fresh.append(rec)
        existing.add((today, fp))
        added += 1
    keep, need_rewrite = _event_index_retain(lines, today_s)
    if not added and not need_rewrite:
        return 0
    try:
        if added and not need_rewrite:
            path.parent.mkdir(parents=True, exist_ok=True)     # reports/ 不存在时自动建
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in fresh) + "\n")
        else:
            _event_index_write(path, keep)                     # 只有裁剪才整文件重写
    except Exception as e:                                     # noqa: BLE001
        print(f"[warn] 事件索引追加失败（不影响 M2 主流程，今天的事件明天认不出来）: "
              f"{type(e).__name__}: {str(e)[:60]}")
        return 0
    return added


def _event_family_display(source):
    """来源名 → 家族**展示名**。

    `families` 要给用户看，所以不能塞内部 ID（"sina"）。做法：命中
    `FAMILY_PREFIXES` 时截出对应的主干前缀，未知源用它自己的规范化名字
    （与 `source_family` 的保守默认一致）。
    """
    s = (source or "").strip()
    if not s:
        return ""
    flat = s
    for joiner in _FAMILY_JOINERS:
        flat = flat.replace(joiner, "")
    low = flat.lower()
    for prefix, _family in FAMILY_PREFIXES:
        if low.startswith(prefix.lower()):
            return flat[:len(prefix)]
    return flat[:24]


def _event_families_of(names):
    """来源名列表 → 去重后的家族展示名列表（保序）。"""
    out, seen = [], set()
    for s in (names or []):
        if not isinstance(s, str) or not s.strip():
            continue
        fam = source_family(s)
        if not fam or fam in seen:
            continue
        seen.add(fam)
        disp = _event_family_display(s) or fam
        if disp not in out:
            out.append(disp)
    return out


def event_record(n, today_s):
    """一条新闻 → 索引行素材。文本为空时返回 None（这种条目进不了索引）。"""
    text = str(n.get("text") or "").strip()
    if not text:
        return None
    return {"d": str(today_s), "fp": event_fingerprint(text),
            "t": text[:EVENT_TITLE_CHARS], "x": normalize_event_text(text),
            "f": [source_family(s) for s in event_sources(n)][:4],
            "fn": _event_families_of(event_sources(n))[:4]}


def continuing_for(cand, hist, today_s):
    """候选新闻 → `continuing` 字段内容；没有命中任何历史事件时返回 None。

    ## 口径（三个日期必须能互相解释）
      · `days` = 历史里匹配到的**不同日期数** + 1（含今天）。同一天命中 3 条只算 1 天。
      · `first_date` = 其中**最早**的日期。注意这是**索引建立以来**能看到的首次日期
        （索引只留 30 天、窗口只比 10 天），不是"这个事件在世界上第一次出现"的日期 ——
        展示层写"持续关注第 N 天"是对的，写"该事件始于 X 日"就过度解读了。
      · `prev_date` = 其中**最近**的日期，必然 < 今天（窗口本身不含今天）。
      · `families` = 历史命中记录里出现过的独立来源家族展示名，最多
        `EVENT_FAMILIES_MAX` 个。
      · `confidence` = 只要有一条命中是 `exact`/`strong` 就是 "high"，全是 `weak`
        才是 "low"。规则刻意简单：展示层只需要回答"敢不敢写第 N 天"，
        而 weak 意味着两条新闻措辞差异大到只能靠"同一名称 + 同一数字"判定 ——
        那种情况误判概率明显更高。
    """
    if not isinstance(cand, dict) or not hist:
        return None
    matched, levels = [], []
    for h in hist:
        rec = known_fp(h)
        if rec is None:
            continue
        level, _score, _detail = event_match(cand, rec, fp=cand.get("fp"))
        if level is None:
            continue
        matched.append(h)
        levels.append(level)
    if not matched:
        return None
    today = str(today_s)
    days = sorted({str(r.get("d") or "") for r in matched if str(r.get("d") or "")})
    days = [d for d in days if d and d < today]
    if not days:
        return None
    fams, seen = [], set()
    for r in matched:
        names = r.get("fn") or r.get("f") or []
        if isinstance(names, str):
            names = [names]
        for nm in names:
            nm = str(nm or "")
            if nm and nm not in seen:
                seen.add(nm)
                fams.append(nm)
    return {
        "days": len(days) + 1,
        "first_date": days[0],
        "prev_date": days[-1],
        "families": fams[:EVENT_FAMILIES_MAX],
        "confidence": "high" if any(l in ("exact", "strong") for l in levels) else "low",
    }


def compute_continuing(rows, hist, today_s):
    """**纯计算**：给保留下来的新闻打 `continuing`，并算出今天要写进索引的记录。

    返回 `(stats, records)`。stats 只有 {new, continuing, max_days}；
    `records` 是待写索引的行（不去重、不落盘，落盘由 `event_index_append` 负责）。

    拆出来是为了让"只要字段、不要落盘"的场景（离线用例、以后可能的只读模式）
    复用同一套判定，而不是各写一份。
    """
    new_cnt = cont_cnt = 0
    max_days = 0
    records, seen_fp = [], set()
    for n in (rows or []):
        if not isinstance(n, dict):
            continue
        text = str(n.get("text") or "").strip()
        if not text:
            continue
        cand = event_candidate(text)
        conf = continuing_for(cand, hist, today_s)
        if conf:
            n["continuing"] = conf
            cont_cnt += 1
            max_days = max(max_days, conf["days"])
        else:
            # 首次出现：**不加** `continuing` 键（不是写 null）。理由：180 条新闻里
            # 只有少数是持续事件，写 null 会让输出体积凭空多出上百个空字段。
            n.pop("continuing", None)
            new_cnt += 1
        if cand["fp"] in seen_fp:
            continue                      # 同一天同一指纹只写一条
        seen_fp.add(cand["fp"])
        rec = event_record(n, today_s)
        if rec:
            records.append(rec)
    return {"new": new_cnt, "continuing": cont_cnt, "max_days": max_days}, records


def track_continuing_events(rows, today_s, path=None):
    """对**保留下来的**新闻逐条做跨天事件追踪，并维护滚动索引。

    必须在 `apply_cross_verification` **之后**调用：那时每条新闻已经有
    `verified` / `sources`，`families` 才能反映"这个事件历史上被哪几家独立来源报道过"。

    返回 `(rows, stats)`：
      stats = {"new": 新事件条数, "continuing": 持续关注条数, "max_days": 最长第 N 天,
               "index_added": 实际写入索引的行数}

    先读后写（读的是"今天以前"的记录，所以不会被本轮的写入影响）。
    同一天内多条命中同一历史事件：**都标**（它们确实是对同一事件的多篇报道），
    但索引里只写一条 `(d, fp)` —— 由 `event_index_append` 的去重保证。

    ⚠️ `path=None` 的含义是"用默认索引路径"，**不是**"不落盘"。想只算字段不写文件
    的调用方请直接用 `compute_continuing`（`main()` 在离线环境下就是这么做的）。
    """
    path = Path(path) if path is not None else event_index_path()
    hist = event_index_read(today_s, path=path)
    stats, records = compute_continuing(rows, hist, today_s)
    added = 0
    try:
        added = event_index_append(records, today_s, path=path)
    except Exception as e:                                # noqa: BLE001
        # 理论上 append 内部已经吞掉异常；这里再兜一层，防止将来有人往上面加东西。
        print(f"[warn] 事件索引维护失败（不影响 M2 主流程）: "
              f"{type(e).__name__}: {str(e)[:60]}")
    stats["index_added"] = added
    return rows, stats


# prefilter 的优先组：先占 max_for_llm 的名额，再由其余类别按时间倒序补满。
# `announcement` 是 M1 巨潮公告的 category 提示（不是 M2 的最终分类，最终仍由
# 模型判定）——它的原始时间戳恒为 00:00，不进优先组就会被时间截断整批挤掉。
PRIORITY_CATEGORIES = ("policy", "announcement")

# ---- 覆盖面（2026-10-04 新增）------------------------------------------------
# 用户要求：**新闻要包括所有相关的，而不是只有股票相关的** ——
# 只吃「提到个股的新闻」会让分析面变窄，宏观/政策/国际/产业动向本身就是判断依据。
#
# 此前只有 policy / announcement 受保护，其余类别一律被 614 条财经快讯按时间倒序
# 挤出 150 个名额：实测 2026-10-04 的 official 存活 **0 条**、tech 3 条。
# 现在给通用类别设**保底名额**，超预算时先按类别保底、再按时间补满。
CATEGORY_FLOOR = {"official": 25, "international": 25, "tech": 10}

# 与市场判断无关的内容直接丢（纯娱乐/体育/彩票/刑案八卦）：不送 LLM，省额度也降噪。
# 注意刻意保持"窄"：只要沾到经济、政策、产业、国际、科技、民生就留，交给 LLM 判 keep。
IRRELEVANT_PAT = re.compile(
    r"明星|艺人|综艺|演唱会|电影票房|剧组|恋情|绯闻|选秀|"
    r"足球|篮球|中超|NBA|世界杯|奥运|网球|高尔夫|夺冠|国足|"
    r"彩票|殡葬|寻人|失联|绑架|凶杀|判刑|入狱")
# 相关性词（旧实现只有 ~20 个财经词，导致国际/产业类整条被丢）：
# 命中即视为「与市场判断可能相关」，**不要求提到股票**。
RELEVANT_PAT = re.compile(
    r"政策|规划|国务院|发改委|商务部|财政部|央行|证监会|工信部|能源局|药监|海关|"
    r"关税|贸易|出口|进口|制裁|管制|谈判|峰会|论坛|法案|监管|改革|试点|"
    r"经济|GDP|CPI|PPI|PMI|通胀|通缩|就业|失业|消费|零售|投资|财政|货币|"
    r"汇率|人民币|美元|利率|降息|加息|降准|债券|贷款|银行|保险|"
    r"能源|原油|石油|天然气|煤炭|电力|光伏|风电|储能|锂|稀土|黄金|白银|铜|铝|钢|化工|水泥|"
    r"芯片|半导体|人工智能|算力|机器人|自动驾驶|新能源|汽车|电池|医药|创新药|疫苗|医疗|"
    r"地产|楼市|房价|基建|铁路|机场|港口|航运|航空|物流|农业|粮食|生猪|"
    r"美国|欧盟|日本|韩国|俄罗斯|乌克兰|中东|以色列|伊朗|北约|全球|国际|海外|"
    r"股|市|基金|证券")

# 送进 GLM 的批量大小：20 条/批。
# 2026-10-04 一度改成 30（想减少批次数），实测当天两次运行的 M2 耗时是
# **批 20 时 295~316s / 批 30 时 888s** —— 每次请求的输出更长，在 API 变慢时被放大，
# 逼近 job 的 30 分钟上限。现在配合 M2_BUDGET_SEC 兜底，批大小回到 20：
# 单次生成更短、更可预测，预算检查的粒度也更细。
LLM_BATCH_SIZE = 20


def normalize_cat(c):
    """模型可能输出 'finance' 等近义值，映射到合法分类"""
    c = (c or "").strip().lower()
    if c in VALID_CATEGORIES:
        return c
    alias = {"finance": "stock", "market": "international", "industry_news": "industry",
             "macro": "policy", "company": "stock", "company_news": "stock",
             "announcement": "stock",   # M1 的公告提示 → 最终归入个股类
             "tech": "other", "official": "policy"}
    return alias.get(c, "other")
VALID_SENTIMENT = ["bullish", "bearish", "neutral"]
VALID_VERIFY = ["confirmed", "unverified"]

# board 里的非法值：事件类型、泛指词，以及模型照抄 prompt 示例的占位符
BLOCKED_BOARDS = {
    "行业或概念名", "板块名", "行业", "概念", "主题", "其他", "无",
    "减持", "增持", "定增", "回购", "重组", "解禁", "停牌", "复牌", "上市", "退市",
    "公告", "新闻", "股市", "国际", "宏观", "政策",
}


def _urlopen(req, timeout=60):
    """优先直连，失败回退系统代理。

    Windows 上 urllib 会自动读取系统代理设置；若梯子开着但节点不通，
    所有请求都会失败。
    """
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(req, timeout=timeout)
    except Exception:
        return urllib.request.urlopen(req, timeout=timeout)


def glm_chat(messages, temperature=0.1, max_tokens=2000, retries=2):
    """调用 GLM-4-Flash，返回文本。失败返回 None"""
    key = os.environ.get("ZAI_API_KEY")
    if not key:
        raise RuntimeError("ZAI_API_KEY 未设置（环境变量）")
    body = json.dumps({
        "model": GLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(GLM_API, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    })
    for attempt in range(retries + 1):
        try:
            with _urlopen(req, 60) as r:
                d = json.loads(r.read().decode("utf-8"))
            return d["choices"][0]["message"]["content"]
        except Exception as e:
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
            else:
                print(f"[warn] glm_chat failed: {e}")
                return None


def _extract_json(text, unwrap_keys=("news", "data", "items")):
    """从模型输出中提取 **JSON 数组**（容错：剥 markdown 代码围栏）。

    崩溃防护（2026-10-04）：旧实现直接把 `json.loads` 的结果返回，模型若返回
    `{"news":[...]}` 这样的**对象**，调用方 `result[i]`（int 下标）会立刻抛
    KeyError 冲出 main —— 而 structured_news.json 在 main 末尾才写，于是 M2 全无
    产出、M3/M5 连锁失败（10-04 就是这种"整条链静默断掉"的表现）。

    现在：是 dict 就依次尝试 news / data / items 取内层 list；取不到 list（仍是
    dict，或是数字/字符串等）一律返回 None，由调用方按"该批 LLM 失败"走兜底。
    """
    if not text:
        return None
    text = re.sub(r"```(?:json)?", "", text).strip().strip("`")
    m = re.search(r"\[[\s\S]*\]|\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in unwrap_keys:
            inner = data.get(key)
            if isinstance(inner, list):
                return inner
        return None       # dict 但取不到 list → 视为该批失败
    return None           # 数字/字符串/布尔 → 视为该批失败


def normalize_boards(board, stocks, global_stocks):
    """board 字段的确定性清洗。

    实测 GLM 在 board 上有两类稳定错误：
    ① 把公司名当板块填（"雷柏科技""宁德时代""招商蛇口"…），
       一天数据里 31 个 board 有 20 个是公司名，板块聚合视图直接失效；
    ② 把事件类型/泛指词当板块（"减持""定增""股市""国际"…），
       以及照抄 prompt 示例里的占位符（"行业或概念名"）。
    仅靠 prompt 约束不稳定，故在代码层再拦一道。

    公司名判定只做**全等**比较：用包含关系会误伤"创投"（因"浦东创投集团"）。
    """
    out = []
    stock_set = {s.strip() for s in (stocks or []) if s}
    for b in (board or []):
        if not isinstance(b, str):
            continue
        b = b.strip()
        if not b or b in BLOCKED_BOARDS:
            continue
        if b in stock_set or b in global_stocks:   # 与个股名完全相同 → 是公司，不是板块
            continue
        if b not in out:
            out.append(b)
    return out


def collect_stock_names(structured_rows):
    """汇总所有批次提取到的个股名，供 normalize_boards 做全局比对"""
    names = set()
    for s in structured_rows:
        for x in (s.get("stocks") or []):
            if isinstance(x, str) and x.strip():
                names.add(x.strip())
    return names


def batch_filter(news_items, batch_size=20):
    """
    对一批新闻做结构化。prompt 硬约束：
    - 只基于原文，不编造
    - 股票代码只提取原文中出现的
    - 与"现实世界知识"相关的判断保持保守

    返回 list（每条含模型自己回填的 `i`）或 **None（该批失败）**。
    调用方必须按 `i` 回填，不得按列表下标对齐 —— 见 main() 的说明。
    """
    items = [{"i": i, "text": (n["text"] or "")[:300],
              "source": n["source"], "time": n["time"]}
             for i, n in enumerate(news_items)]
    batch_str = json.dumps(items, ensure_ascii=False)

    prompt = f"""你是财经新闻编辑。对下列每条新闻做结构化处理。

## 硬约束
1. 只基于给出的新闻原文判断，禁止使用原文之外的知识编造补充信息
2. board/stocks 只填原文中实际提及的，未提及填空数组
3. sentiment 仅描述新闻本身的倾向，不是你对股价的预测

## 分类说明
- policy: 宏观政策/监管/政府发布
- industry: 行业动态/供需/技术趋势
- stock: 具体个股公告/回购/重组/诉讼等
- international: 国际市场/地缘/外围行情
- other: 与股市无关的（可直接丢弃候选）

## board 与 stocks 的区别（最容易出错，务必区分）
- board 填**行业 / 概念 / 主题**，代表一个群体，例如：半导体、光伏、房地产、
  医药、军工、储能、文旅、券商。板块名不得是某一家公司的名字。
- stocks 填**具体公司**，例如：宁德时代、盛美上海、雷柏科技。
- ⚠️ 公司名、机构名、政府部门名一律不得出现在 board 里。
- ⚠️ 事件类型不得作为板块名：减持、增持、定增、回购、重组、解禁、停牌、
  上市等描述的是"发生了什么"，不是"属于哪个行业"。
- ⚠️ 泛指词不得作为板块名：股市、国际、宏观、政策、其他。
- ⚠️ 原文若只讲某家公司的公告、未提及所属行业或概念，board 必须是 []，
  不能把这家公司的名字填进 board。
- ⚠️ 同一条里 board 与 stocks 不得有重复项。
- 正例：原文提到"晶圆制造设备需求稳健" → board: ["半导体设备"]
- 反例：原文只讲"某某公司拟减持1.78%股份" → board: []，不能填 ["某某公司"]，也不能填 ["减持"]

输入新闻列表:
{batch_str}

输出要求：仅输出 JSON 数组，每条与输入一一对应（不要输出 verified 字段，它由系统另行计算）：
[{{"i":0,"category":"policy|industry|stock|international|other",
"board":["半导体设备"],"stocks":["盛美上海"],"sentiment":"bullish|bearish|neutral",
"keep":true/false}}]
上面的"半导体设备""盛美上海"只是字段格式示意，不是固定答案，更不要原样照抄到结果里。

**keep 的判据（2026-10-04 放宽）**：宁可多留，不要因为"没提到个股"就丢。
- keep=true：只要与经济、政策、产业、国际形势、科技、民生相关都保留 ——
  包括**没有提到任何公司**的宏观/国际/产业新闻（它们是判断市场环境的依据）。
- keep=false：**仅限**纯娱乐八卦、体育赛事结果、彩票/生活服务信息、
  与市场无关的地方社会琐事或刑案通报。"""
    content = glm_chat([{"role": "user", "content": prompt}], max_tokens=4000)
    if content is None:
        return None
    return _extract_json(content)


def prefilter_local(news_items, max_for_llm=180):
    """本地预筛：控制送进 LLM 的量，同时**保证新闻覆盖面**。

    策略（2026-10-04 改）：
    1. **负向词先丢**：纯娱乐/体育/彩票/刑案八卦，与市场判断无关，不送 LLM；
    2. `finance`（财经快讯）、`policy`、`announcement` 整体保留（这三类本身即相关）；
       其余类别（official / international / tech / other）走**相关性词**判断 ——
       旧实现只认 ~20 个财经词，导致「北约在日本设联络处」这类国际新闻整条被丢；
    3. 超预算时：优先组（policy+announcement）先占位 → **通用类别按保底名额** →
       剩余额度按时间倒序补满。这样财经快讯的条数再多，也挤不掉通用/国际/科技类。

    为什么要有保底：实测 2026-10-04，150 个名额里 official 存活 **0**、tech 3，
    而财经快讯有 614 条可竞争 —— 用户要的是「所有相关新闻」，不是「只有股票相关」。
    为什么公告必须进优先组（2026-09-25 实测）：巨潮的 `announcementTime`
    **只有日期、时刻恒为 00:00**，按时间倒序时全部 32 条公告落在当日快讯之后，
    截断线当天在 11:02，**0/32 存活**。不给优先级等于白抓。
    """
    keep, taken = [], set()

    def _take(n):
        keep.append(n)
        taken.add(id(n))

    # ⓿ 负向词**最优先**：纯娱乐/体育/彩票/刑案通报一律不进场。
    #    必须在保底名额之前 —— 否则某个通用类别恰好是体育新闻时，保底会把它们
    #    "保护"进来（负向词表刻意很窄，误杀风险低；而"术语没被正向词表覆盖"
    #    是另一回事，那种情况由下面的保底兜住）。
    clean = [n for n in news_items if not IRRELEVANT_PAT.search(n.get("text") or "")]

    # 按类别分桶（保持 M1 的时间倒序）
    buckets = {}
    for n in clean:
        buckets.setdefault(n.get("category", ""), []).append(n)

    # ① 优先组：政策文件原文与上市公司公告整体保留（条数有界、信号密度高）
    for cat in PRIORITY_CATEGORIES:
        for n in buckets.get(cat, []):
            _take(n)

    # ② 通用类别保底：**在关键词判定之前**先各取最新的若干条。
    #    为什么必须前置：术语没被词表覆盖 ≠ 不相关。"国产大模型在工业质检场景落地"
    #    这类条目在 2026-10-04 之前会被关键词分支直接丢掉 —— 词表总有盲区，
    #    而保底名额是"全覆盖"的兜底。
    for cat, floor in CATEGORY_FLOOR.items():
        for n in buckets.get(cat, [])[:floor]:
            if id(n) not in taken:
                _take(n)

    # ③ 其余条目：财经类直接留；其它类别看是否命中相关性词
    for n in clean:
        if id(n) in taken:
            continue
        c = n.get("category", "")
        if c in ("finance", "policy", "announcement") or RELEVANT_PAT.search(n.get("text") or ""):
            _take(n)
    # 此时已丢弃 = 负向词命中的条目 + 既不相关也未命中相关性词的条目
    dropped = len(news_items) - len(keep)
    # （下面若因超上限截断，继续在 dropped 上累加）

    if len(keep) > max_for_llm:
        priority = [n for n in keep if n["category"] in PRIORITY_CATEGORIES]
        # 优先组自身也可能超预算（公告被大量抓入时），此时它内部按原序
        # （即时间倒序）截断，不会反过来吃掉全部额度
        priority = priority[:max_for_llm]
        rest = [n for n in keep if n["category"] not in PRIORITY_CATEGORIES]
        budget = max(0, max_for_llm - len(priority))

        # 通用类别保底（取各类别里最新的若干条 —— rest 保持时间倒序）
        picked, picked_ids = [], set()
        for cat, floor in CATEGORY_FLOOR.items():
            got = [n for n in rest if n.get("category") == cat][:floor]
            picked.extend(got)
            picked_ids.update(id(n) for n in got)
        # 剩余额度按时间倒序补满（通常是财经快讯）
        for n in rest:
            if len(picked) >= budget:
                break
            if id(n) not in picked_ids:
                picked.append(n)
                picked_ids.add(id(n))

        kept = priority + picked[:budget]
        dropped += len(keep) - len(kept)
        keep = kept
    return keep, dropped


def cross_verify_news(running_list, new_items):
    """跨批次的交叉验证（预留接口，M3/M8 的跨批合并时用）"""
    return running_list + new_items


def fallback_row(hint=None):
    """LLM 失败时的兜底结构化结果：本批全保留（保守策略，宁多勿漏）。

    `hint` 是 M1 给的原类别提示（finance / policy / announcement …）。类别同样
    要过 normalize_cat：直接塞原始提示会把 finance / tech / announcement 这些
    **不在 VALID_CATEGORIES 里**的值漏进下游。
    """
    return {"category": normalize_cat(hint), "board": [], "stocks": [],
            "sentiment": "neutral", "keep": True}


def _match_row(s):
    """把模型返回的一行字典规范成结构化结果（字段级容错）"""
    return {
        "category": normalize_cat(s.get("category")),
        "board": s.get("board") if isinstance(s.get("board"), list) else [],
        "stocks": s.get("stocks") if isinstance(s.get("stocks"), list) else [],
        "sentiment": s.get("sentiment") if s.get("sentiment") in VALID_SENTIMENT else "neutral",
        "keep": bool(s.get("keep", True)),
    }


# 一批里模型回填的 `i` 无法对上原文的比例超过此值 → 整批判为 LLM 失败。
# 依据：prompt 要求"每条与输入一一对应"，正常情况未匹配应为 0；偶发一条漏返
# （1/20 = 5%）尚可容忍并单独记为空结构，成片错位则说明模型调序/漏段，
# 此时任何"按下标硬对齐"的结果都会把分类挂到错误的新闻上，宁可按失败兜底。
UNMATCHED_FAIL_RATIO = 0.10

# 全批次失败率超过此值 → 额外打一行醒目 warn（"今天新闻很少"的假象源头）
FAILED_BATCH_ALERT_RATIO = 0.30

# 整个 M2 步骤的**墙钟预算**（秒）。超过就不再调 LLM，剩下的批次直接走"全保留"兜底。
#
# 为什么需要（2026-10-04 实测）：M2 的耗时完全由 GLM 服务端速度决定，同样的代码
# 同一天两次运行分别是 **295s / 316s**（批 20 条）与 **888s**（批 30 条），而 job 的
# `timeout-minutes` 是 30 分钟 —— 一旦 API 变慢，硬超时会**整条流水线失败、当天没有日报**，
# 比"部分新闻没结构化"糟糕得多。有了预算，最坏情况是"后面的批次退化为原样保留"，
# 报告照出，而且 `llm_failed_batches` 会把这件事如实暴露给体检。
M2_BUDGET_SEC = 900


def main():
    import time as _time
    t_start = _time.monotonic()
    raw = json.loads((DATA_DIR / "raw_news.json").read_text(encoding="utf-8"))
    news = raw["news"]
    print(f"原始新闻: {len(news)} 条")

    keep, dropped = prefilter_local(news)
    # 把「留下来的都是什么」打出来：用户明确要求覆盖面（所有相关新闻，而不是只有
    # 股票相关），而这正是以前看不见的地方 —— 10-04 的 official 存活 0 条、tech 3 条，
    # 日志里却只显示"保留 150 条"。现在按类别分列，覆盖面退化了当场可见。
    cat_mix = {}
    for n in keep:
        cat_mix[n.get("category", "?")] = cat_mix.get(n.get("category", "?"), 0) + 1
    print(f"本地预筛: 保留 {len(keep)} 条，丢弃 {dropped} 条（含纯娱乐/体育 {sum(1 for n in news if IRRELEVANT_PAT.search(n.get('text') or ''))} 条）")
    print(f"  类别分布: {dict(sorted(cat_mix.items(), key=lambda kv: -kv[1]))}")

    structured = []
    llm_batches = 0            # 实际发出的批次数
    llm_failed_batches = 0     # batch_filter 返回 None、未匹配超阈值、或**预算耗尽**的批次
    budget_skipped = 0         # 其中"因 M2_BUDGET_SEC 预算耗尽而根本没跑"的批次数
    unmatched_rows = 0         # 单条未匹配（i 缺失/越界/重复）的总数
    budget_hit = False
    for bs in range(0, len(keep), LLM_BATCH_SIZE):
        batch = keep[bs:bs + LLM_BATCH_SIZE]
        batch_no = bs // LLM_BATCH_SIZE
        elapsed = _time.monotonic() - t_start
        if elapsed > M2_BUDGET_SEC:
            # 预算耗尽：不再调 LLM。剩下的批次全部走"全保留"兜底并计数，
            # 让报告照出、让体检看得见（见 M2_BUDGET_SEC 的注释）。
            if not budget_hit:
                budget_hit = True
                print(f"  [WARN] 已用 {elapsed:.0f}s，超过 M2 预算 {M2_BUDGET_SEC}s —— "
                      f"从 batch {batch_no} 起的剩余批次不再调用 GLM，按全保留兜底"
                      f"（报告照出，但后面的新闻没有结构化字段）")
            llm_failed_batches += 1
            budget_skipped += 1
            structured.extend(fallback_row(n.get("category")) for n in batch)
            continue
        llm_batches += 1
        t_batch = _time.monotonic()
        result = batch_filter(batch)
        if result is None or not isinstance(result, list):
            # 任务 3：LLM 失败时本批全保留（保守策略，宁多勿漏），但**必须计数**——
            # 10-04 的假象正是"GLM 全挂 → 150 条零结构化但全保留 → 报表看起来
            # 像今天没什么新闻"，而输出文件里没有任何失败标记。
            llm_failed_batches += 1
            structured.extend(fallback_row(n.get("category")) for n in batch)
            print(f"  [warn] batch {batch_no}: LLM 返回不可用（{type(result).__name__}），"
                  f"本批 {len(batch)} 条按全保留兜底（无结构化字段）")
            continue

        # 按 `i` 回填（2026-10-04 修正）。
        # 旧实现用 `result[i]`（列表下标）对齐，但 prompt 要求模型**自己**在每条里
        # 回填 `i`，模型漏一条或调序时，该批其后所有新闻的分类/板块/个股/情绪会
        # 全部挂错原文，且没有任何断言。现在改为按模型给出的 `i` 建映射：
        #   i 缺失 / 非 int / 越界 / 重复 → 该条记为空结构 {}，并计入"未匹配"。
        by_index = {}
        for row in result:
            if not isinstance(row, dict):
                continue
            i = row.get("i")
            if isinstance(i, bool) or not isinstance(i, int):
                continue
            if not (0 <= i < len(batch)):     # 越界（模型把 i 当成 1-based 或串批）
                continue
            if i in by_index:                 # 重复 → 只认第一条，第二条算未匹配
                continue
            by_index[i] = row

        for i, n in enumerate(batch):
            row = by_index.get(i)
            if row is None:
                unmatched_rows += 1
                structured.append(_match_row({}))      # 空结构 = {} 的效果
            else:
                structured.append(_match_row(row))

        ratio = (len(batch) - len(by_index)) / len(batch)
        if ratio > UNMATCHED_FAIL_RATIO:
            # 未匹配比例超阈值 → 该批视为 LLM 失败，走与 result is None 相同的兜底
            # 路径。已经 append 的那部分结果必须整批回滚，否则会与兜底结果重影。
            del structured[len(structured) - len(batch):]
            structured.extend(fallback_row(n.get("category")) for n in batch)
            llm_failed_batches += 1
            print(f"  [warn] batch {batch_no}: 未匹配 {len(batch) - len(by_index)}/{len(batch)} "
                  f"条（{ratio:.0%} > {UNMATCHED_FAIL_RATIO:.0%}），整批判为 LLM 失败，"
                  f"按全保留兜底")
            continue
        time.sleep(1)
        # 打印本批耗时：M2 是整条流水线里最慢的一步，而它的耗时完全由 GLM 服务端
        # 决定（实测同一份代码 295s ~ 888s）。没有逐批耗时，事后无法判断"是整体变慢
        # 还是某几批卡住"，只能像 2026-10-04 那样靠人工猜。
        print(f"  batch {batch_no}: done ({bs+len(batch)}/{len(keep)}, "
              f"{_time.monotonic() - t_batch:.0f}s)")

    # board 清洗：先汇总全局个股名，再逐条剔除被误当成板块的公司名
    all_stocks = collect_stock_names(structured)
    before = sum(len(s.get("board") or []) for s in structured)
    for s in structured:
        s["board"] = normalize_boards(s.get("board"), s.get("stocks"), all_stocks)
    after = sum(len(s.get("board") or []) for s in structured)

    # 合并原始内容与结构化结果，丢弃 keep=false 的
    out = []
    for n, s in zip(keep, structured):
        if not s["keep"]:
            continue
        out.append({**n, **s})

    # 交叉验证：代码层确定性比对（不用 LLM 判断——实测不可靠）。
    # 2026-10-04 起改用 M1 聚合的 `sources` 字段 + 独立源家族归一，
    # 旧的"正文前 16 字相同且 ≥2 源"规则已删除（与 M1 的跨源去重直接冲突，
    # 详见 apply_cross_verification 的 docstring）。
    out, verify_stats = apply_cross_verification(out)
    out.sort(key=lambda x: x["time"], reverse=True)

    # 跨天事件追踪（2026-10-05 新增）：位置必须在 apply_cross_verification **之后**
    # （那时才有 verified/sources），且只处理保留下来的（keep=true）新闻。
    # 整块套 try：它是**附加信息**，失败绝不能连累当天有没有结构化新闻。
    #
    # ⚠️ `DATA_DIR` 被外部改写（离线用例）时**整块跳过**，不是把 path 传成 None：
    # 本模块的约定是"path=None → 用默认路径"，传 None 等于照样写进真实索引。
    # 这个坑我踩过一次 —— 跑一遍 offline_tests 就在仓库的 reports/ 里凭空多出
    # 60 行 `测试源` 的假新闻。见 EVENT_INDEX_ENABLED 的注释。
    today_s = time.strftime("%Y-%m-%d")
    idx_path = event_index_default_path() if DATA_DIR == _DEFAULT_DATA_DIR else None
    try:
        if idx_path is None:
            # 只算字段、不落盘：直接走纯计算版（不能传 path=None，那是"用默认路径"）
            print("[warn] DATA_DIR 被改写（离线/测试环境）→ 本轮不维护事件索引，"
                  "只计算 continuing 字段")
            stats, _records = compute_continuing(out, [], today_s)
            track_stats = {"new": stats["new"], "continuing": stats["continuing"],
                           "max_days": stats["max_days"], "index_added": 0}
        else:
            out, track_stats = track_continuing_events(out, today_s, path=idx_path)
    except Exception as e:                                # noqa: BLE001
        print(f"[warn] 跨天事件追踪整体失败（不影响 M2 主流程与输出文件）: "
              f"{type(e).__name__}: {str(e)[:80]}")
        track_stats = {"new": 0, "continuing": 0, "max_days": 0, "index_added": 0}

    out_path = DATA_DIR / "structured_news.json"
    out_path.write_text(json.dumps({
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "input_count": len(news),
        "prefilter_dropped": dropped,
        # 以下三个为 2026-10-04 新增（只增不改，老读者不受影响）。
        # 为什么必须落盘：LLM 全挂时旧输出文件里没有任何失败标记，于是"150 条
        # 零结构化但全保留"看起来就像"今天没什么新闻"，报表层面完全看不出异常。
        "llm_batches": llm_batches,
        "llm_failed_batches": llm_failed_batches,
        "llm_skipped_by_budget": budget_skipped,
        "unmatched_rows": unmatched_rows,
        "verify_stats": verify_stats,
        # 2026-10-05 新增（纯附加）：跨天事件追踪口径。
        # continuing_* 供展示层与体检直接用；event_index_added 反映索引到底写进去没有
        # （写不进去时它会低于当天新闻数 —— 那是"明天认不出今天"的唯一可见信号）。
        "continuing_count": track_stats["continuing"],
        "continuing_max_days": track_stats["max_days"],
        "event_index_added": track_stats["index_added"],
        "news": out,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # 分母用「应跑的批次数」（含因预算没跑的），否则预算一触发只会得到 llm_batches=1
    # 这种失真比例。体检也读 llm_failed_batches（见 healthcheck.py 的内容面检查）。
    total_batches = llm_batches + budget_skipped
    if total_batches and llm_failed_batches / total_batches > FAILED_BATCH_ALERT_RATIO:
        print("!" * 72)
        print(f"[WARN] LLM 失败/未跑率过高：{llm_failed_batches}/{total_batches} 批"
              f"（> {FAILED_BATCH_ALERT_RATIO:.0%}，其中因 {M2_BUDGET_SEC}s 预算跳过的"
              f"{budget_skipped} 批）。本日 structured_news.json 中大量条目"
              f"**只有类别、无板块/个股/情绪**——这不是'今天新闻少'，是模型没跑通。"
              f"请检查 ZAI_API_KEY / 额度 / 网络后重跑 M2。")
        print("!" * 72)

    cats, ver = {}, {}
    for o in out:
        cats[o["category"]] = cats.get(o["category"], 0) + 1
        ver[o["verified"]] = ver.get(o["verified"], 0) + 1
    stock_cnt = sum(1 for o in out if o["stocks"])
    board_cnt = sum(1 for o in out if o["board"])
    print(f"分类分布: {cats}")
    print(f"可信度: {ver}")
    print(f"含个股: {stock_cnt} 条, 含板块: {board_cnt} 条")
    print(f"板块清洗: {before} -> {after} 个标签（剔除 {before - after} 个误填的公司名）")
    print(f"LLM 批次: {llm_batches} 批，失败 {llm_failed_batches} 批，"
          f"未匹配行 {unmatched_rows} 条")
    print(f"交叉验证: {verify_stats}")
    # 事件追踪摘要（2026-10-05）：一行说清"今天多少条是老事件"。
    # 索引写入数与当天条数不一致时说明索引没写全（写失败只 warn，这里再暴露一次）。
    print(f"事件追踪: 新事件 {track_stats['new']} 条 / 持续关注 "
          f"{track_stats['continuing']} 条（最长第 {track_stats['max_days']} 天）"
          f"[索引新增 {track_stats['index_added']} 行 → {idx_path or '（本轮未维护索引）'}]")
    print(f"输出 → {out_path}")


if __name__ == "__main__":
    main()
