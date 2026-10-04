# -*- coding: utf-8 -*-
"""
M2 新闻筛选与结构化模块
用 GLM-4-Flash（免费）对 M1 产出的原始新闻做分类、去重校验、关联提取、可信度打标。

输入: data/raw_news.json
输出: data/structured_news.json

密钥来源: 环境变量 ZAI_API_KEY（不硬编码、不打印）
"""
import json
import os
import re
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE / "data"

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
    print(f"输出 → {out_path}")


if __name__ == "__main__":
    main()
