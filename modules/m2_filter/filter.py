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


def _extract_json(text):
    """从模型输出中提取 JSON（容错：剥 markdown 代码围栏）"""
    if not text:
        return None
    text = re.sub(r"```(?:json)?", "", text).strip().strip("`")
    m = re.search(r"\[[\s\S]*\]|\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


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


def main():
    raw = json.loads((DATA_DIR / "raw_news.json").read_text(encoding="utf-8"))
    news = raw["news"]
    print(f"原始新闻: {len(news)} 条")

    keep, dropped = prefilter_local(news)
    print(f"本地预筛: 保留 {len(keep)} 条，丢弃 {dropped} 条纯无关内容")

    structured = []
    for bs in range(0, len(keep), 20):
        batch = keep[bs:bs + 20]
        result = batch_filter(batch)
        if result is None:
            # LLM 失败时本批全保留（保守策略，宁多勿漏）。
            # 类别同样要过 normalize_cat：直接塞原始提示会把 finance / tech /
            # announcement 这些**不在 VALID_CATEGORIES 里**的值漏进下游。
            for i in range(len(batch)):
                structured.append({"category": normalize_cat(batch[i].get("category")),
                                   "board": [], "stocks": [],
                                   "sentiment": "neutral",
                                   "keep": True})
            continue
        # 对齐
        for i, n in enumerate(batch):
            row = result[i] if i < len(result) and isinstance(result[i], dict) else {}
            structured.append({
                "category": normalize_cat(row.get("category")),
                "board": row.get("board") if isinstance(row.get("board"), list) else [],
                "stocks": row.get("stocks") if isinstance(row.get("stocks"), list) else [],
                "sentiment": row.get("sentiment") if row.get("sentiment") in VALID_SENTIMENT else "neutral",
                "keep": bool(row.get("keep", True)),
            })
        time.sleep(1)
        print(f"  batch {bs//20}: done ({bs+len(batch)}/{len(keep)})")

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

    # 交叉验证：代码层确定性比对（不用 LLM 判断——实测不可靠）
    # 规则：去除标点/数字/空白后的正文主干（前16字）一致的条目，来自 ≥2 个独立源 → confirmed
    from collections import defaultdict
    groups = defaultdict(list)
    for n in out:
        key = re.sub(r"[【】\s：:，,。（）()0-9]", "", n["text"])[:16]
        groups[key].append(n["source"])
    for n in out:
        key = re.sub(r"[【】\s：:，,。（）()0-9]", "", n["text"])[:16]
        n["verified"] = "confirmed" if len(set(groups[key])) >= 2 else "unverified"
    # 修正顺序：verified 依据 out 内分组，逐条写回
    out.sort(key=lambda x: x["time"], reverse=True)

    out_path = DATA_DIR / "structured_news.json"
    out_path.write_text(json.dumps({
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "input_count": len(news),
        "prefilter_dropped": dropped,
        "news": out,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

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
    print(f"输出 → {out_path}")


if __name__ == "__main__":
    main()
