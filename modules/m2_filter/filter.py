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
keep=false 表示纯社会新闻与股市无关应丢弃。"""
    content = glm_chat([{"role": "user", "content": prompt}], max_tokens=4000)
    if content is None:
        return None
    return _extract_json(content)


def prefilter_local(news_items, max_for_llm=150):
    """本地预筛：控制送进 LLM 的量。

    策略（**优先组先占位，其余按时间倒序补满**）：
    - `policy`（政府文件原文）与 `announcement`（上市公司公告）优先保留 ——
      两者条数有界、信号密度高，且**不能被「按时间倒序」的截断挤掉**；
    - `finance` 全部进候选；`official` / `tech` 走关键字过滤。

    为什么公告必须进优先组（2026-09-25 实测）：巨潮的 `announcementTime`
    **只有日期、时刻恒为 00:00**，于是按时间倒序排时，全部 32 条公告落在当日
    快讯之后 —— 截断线当天在 11:02，**0/32 存活**。不给优先级等于白抓。
    """
    keep, dropped = [], 0
    kw_fin = re.compile(r"股|市|基金|证券|央行|人民币|利[率率]|GDP|CPI|PMI|美联储|降准|降息|上市|发行|回购|并购|重组|营收|净利")
    for n in news_items:
        c = n.get("category", "")
        if c in ("finance", "policy", "announcement"):
            keep.append(n)
        elif kw_fin.search(n["text"]):
            keep.append(n)
        else:
            dropped += 1
    if len(keep) > max_for_llm:
        priority = [n for n in keep if n["category"] in PRIORITY_CATEGORIES]
        rest = [n for n in keep if n["category"] not in PRIORITY_CATEGORIES]
        # 优先组自身也可能超预算（公告被大量抓入时），此时它内部按原序
        # （即时间倒序）截断，不会反过来吃掉全部额度
        priority = priority[:max_for_llm]
        rest = rest[:max(0, max_for_llm - len(priority))]
        kept = priority + rest
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


def main():
    raw = json.loads((DATA_DIR / "raw_news.json").read_text(encoding="utf-8"))
    news = raw["news"]
    print(f"原始新闻: {len(news)} 条")

    keep, dropped = prefilter_local(news)
    print(f"本地预筛: 保留 {len(keep)} 条，丢弃 {dropped} 条纯无关内容")

    structured = []
    llm_batches = 0            # 实际发出的批次数
    llm_failed_batches = 0     # batch_filter 返回 None，或未匹配比例超阈值的批次
    unmatched_rows = 0         # 单条未匹配（i 缺失/越界/重复）的总数
    for bs in range(0, len(keep), 20):
        batch = keep[bs:bs + 20]
        batch_no = bs // 20
        llm_batches += 1
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
        print(f"  batch {batch_no}: done ({bs+len(batch)}/{len(keep)})")

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
        "unmatched_rows": unmatched_rows,
        "verify_stats": verify_stats,
        "news": out,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    if llm_batches and llm_failed_batches / llm_batches > FAILED_BATCH_ALERT_RATIO:
        print("!" * 72)
        print(f"[WARN] LLM 失败率过高：{llm_failed_batches}/{llm_batches} 批失败"
              f"（> {FAILED_BATCH_ALERT_RATIO:.0%}）。本日 structured_news.json 中"
              f"大量条目**只有类别、无板块/个股/情绪**——这不是'今天新闻少'，"
              f"是模型没跑通。请检查 ZAI_API_KEY / 额度 / 网络后重跑 M2。")
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
