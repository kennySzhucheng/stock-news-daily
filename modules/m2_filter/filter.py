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


def normalize_cat(c):
    """模型可能输出 'finance' 等近义值，映射到合法分类"""
    c = (c or "").strip().lower()
    if c in VALID_CATEGORIES:
        return c
    alias = {"finance": "stock", "market": "international", "industry_news": "industry",
             "macro": "policy", "company": "stock", "company_news": "stock"}
    return alias.get(c, "other")
VALID_SENTIMENT = ["bullish", "bearish", "neutral"]
VALID_VERIFY = ["confirmed", "unverified"]


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
            with urllib.request.urlopen(req, timeout=60) as r:
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

输入新闻列表:
{batch_str}

输出要求：仅输出 JSON 数组，每条与输入一一对应（不要输出 verified 字段，它由系统另行计算）：
[{{"i":0,"category":"policy|industry|stock|international|other",
"board":["板块名"],"stocks":["股票名或6位代码"],"sentiment":"bullish|bearish|neutral",
"keep":true/false}}]
keep=false 表示纯社会新闻与股市无关应丢弃。"""
    content = glm_chat([{"role": "user", "content": prompt}], max_tokens=4000)
    if content is None:
        return None
    return _extract_json(content)


def prefilter_local(news_items, max_for_llm=150):
    """本地预筛：控制送进 LLM 的量。
    策略：finance/policy 类全保留候选；official/tech 类做关键字过滤。
    """
    keep, dropped = [], 0
    kw_fin = re.compile(r"股|市|基金|证券|央行|人民币|利[率率]|GDP|CPI|PMI|美联储|降准|降息|上市|发行|回购|并购|重组|营收|净利")
    for n in news_items:
        c = n.get("category", "")
        if c in ("finance", "policy"):
            keep.append(n)
        elif kw_fin.search(n["text"]):
            keep.append(n)
        else:
            dropped += 1
    if len(keep) > max_for_llm:
        # 优先保留 policy 类与 finance 中的头部，抽样截断
        policy = [n for n in keep if n["category"] == "policy"]
        rest = [n for n in keep if n["category"] != "policy"]
        rest = rest[:max_for_llm - len(policy)]
        kept, dropped = policy + rest, len(keep) - len(policy) - len(rest)
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
            # LLM 失败时本批全保留（保守策略，宁多勿漏）
            for i in range(len(batch)):
                structured.append({"category": batch[i].get("category", "other"),
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
    print(f"输出 → {out_path}")


if __name__ == "__main__":
    main()
