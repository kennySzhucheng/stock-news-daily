# -*- coding: utf-8 -*-
"""M9 AI 追问 — 针对当日新闻回答用户提问。

网页版相对推送版的核心增量：推送只能给固定几段，这里可以就任意问题
基于当天新闻做二次分析。

硬约束与 M3 一致（禁止编造 / 标注置信度 / 禁止投资指令 / 承认局限），
并额外要求：新闻不足以回答时必须明说，不许用模型自身知识补位。

密钥来源: 环境变量 DEEPSEEK_API_KEY
"""
import re
import importlib.util
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent

# 中文里高频但无检索意义的词，避免它们把不相关的新闻拉进上下文
STOPWORDS = {
    "什么", "怎么", "如何", "哪些", "哪个", "今日", "今天", "最近", "现在",
    "是否", "有没", "没有", "可以", "可能", "这个", "这些", "那那", "为什",
    "的话", "了吗", "一下", "情况", "影响", "分析", "看一", "看看", "觉得",
    "请问", "告诉", "我们", "他们", "以及", "还有", "对于", "关于", "方面",
}

MAX_CONTEXT_CHARS = 16000


def _load_m3_module():
    """复用 M3 的 DeepSeek 调用（含重试与直连回退），避免重复实现"""
    path = BASE / "modules" / "m3_analyzer" / "analyzer.py"
    spec = importlib.util.spec_from_file_location("m3_analyzer", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def keywords(question):
    """从问题里抽检索词：英文/数字 token + 中文二元组 + 完整中文词段"""
    q = (question or "").lower()
    toks = re.findall(r"[a-z0-9]{2,}", q)
    for seg in re.findall(r"[一-鿿]+", q):
        if len(seg) >= 2 and seg not in STOPWORDS:
            toks.append(seg)
        for i in range(len(seg) - 1):
            bigram = seg[i:i + 2]
            if bigram not in STOPWORDS:
                toks.append(bigram)

    seen, out = set(), []
    for t in toks:
        if len(t) >= 2 and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def select_relevant(bundle, question, scope_ids=None, limit=45):
    """挑出与问题最相关的新闻，返回 [(编号, 新闻)]。

    编号沿用 M3 的引用体系（bundle.citation_map 的 key），
    这样追问答案里的 [n] 与日报正文里的 [n] 指向同一条。
    """
    terms = keywords(question)
    explicit = set(scope_ids or [])

    scored = []
    for cite_no, idx in bundle.citation_map.items():
        n = bundle.news[idx]
        hay = ((n.get("text") or "") + " " + " ".join(n.get("stocks") or [])
               + " " + " ".join(n.get("board") or [])).lower()
        score = 0.0
        for t in terms:
            c = hay.count(t)
            if c:
                # 长词更有区分度，短词（二字）权重低
                score += min(c, 4) * (len(t) ** 1.5)
        if idx in explicit:
            score += 1000          # 用户手动勾选的新闻必须进上下文
        if n.get("verified") == "confirmed":
            score += 1.5
        if n.get("category") == "policy":
            score += 1.0
        scored.append((score, cite_no, idx))

    scored.sort(key=lambda x: (-x[0], x[1]))
    picked = [(c, i) for s, c, i in scored if s > 0][:limit]

    # 问题太泛（一个词都没命中）时，退回按权重取前若干条，保证仍有上下文
    if len(picked) < 8:
        fallback = [(c, i) for s, c, i in scored[:limit]]
        got = {i for _, i in picked}
        picked += [(c, i) for c, i in fallback if i not in got]
    return picked


def build_context(bundle, picked, max_chars=MAX_CONTEXT_CHARS):
    """把选中的新闻拼成给模型的清单，沿用 M3 的字段格式"""
    lines, total = [], 0
    for cite_no, idx in picked:
        n = bundle.news[idx]
        line = (f"[{cite_no}] {n['time'][5:16]} {n['source']}|{n.get('category','')}"
                f"|{n.get('verified','')[:4]}"
                f"|股:{','.join(n.get('stocks') or [])[:40] or '-'}"
                f"|板:{','.join(n.get('board') or [])[:30] or '-'}"
                f"|情:{n.get('sentiment','neutral')}\n{n['text'][:300]}")
        if total + len(line) > max_chars:
            break
        total += len(line)
        lines.append(line)
    return "\n\n".join(lines), len(lines)


PROMPT = """你是资深财经新闻分析师。用户针对**今日已收集的新闻**提问，请仅基于下面的新闻作答。

## 硬约束（违反即废稿）
1. 【禁止编造】每个论断必须能对应到给定新闻的编号 [n]。不允许输出任何未在给定新闻中出现的事实、数据、公司名、政策名。你训练数据里的"知识"一概不得使用。
2. 【引用出处】关键论断后用 [n] 标注来源编号。
3. 【置信度】每条判断末尾标（置信度:高/中/低）。单源待核实(unve)新闻支撑的判断最高只能给"中"。
4. 【禁止投资指令】不得出现"建议买入""建议卖出""可以抄底""应该止损"等表述，只允许"值得观察""需持续关注验证"等中性表述。
5. 【承认局限】新闻≠股价，利好落地可能是利好出尽；你没有历史价格数据，无法判断当前位置。
6. 【不足则明说】若给定新闻不足以回答该问题，直接说明"今日新闻中没有足够信息"，并指出还缺哪类信息。不要用常识或历史经验补足。

## 今日市场分析（M3 已产出，供参考，同样不得超出新闻范围）
{analysis}

## 用户问题
{question}

## 今日新闻（共 {count} 条，编号 [] 用于引用）
{context}

## 回答要求
- 用 markdown，先给结论再给依据，控制在 600 字以内
- 若问题涉及个股或板块，请分别说明"新闻里说了什么"与"据此能推到什么、推不到什么"
- 结尾附一行：*以上基于今日 {count} 条新闻，不构成投资建议。*"""


def ask(bundle, question, scope_ids=None):
    """返回 {answer_md, answer_html, used_news: [编号], count}"""
    question = (question or "").strip()
    if not question:
        raise ValueError("问题为空")

    picked = select_relevant(bundle, question, scope_ids=scope_ids)
    context, count = build_context(bundle, picked)
    if not count:
        return {"answer_md": "今日没有可用的新闻数据，无法回答。", "answer_html":
                "<p>今日没有可用的新闻数据，无法回答。</p>", "used_news": [], "count": 0}

    # 分析正文只保留前 2000 字，避免挤压新闻上下文的预算
    analysis_digest = (bundle.analysis_md or "（无）")[:2000]

    prompt = PROMPT.format(analysis=analysis_digest, question=question,
                           context=context, count=count)

    ds = _load_m3_module()
    answer = ds.ds_chat([{"role": "user", "content": prompt}], max_tokens=2000)

    return {
        "answer_md": answer,
        "answer_html": bundle.linkify_citations(bundle.m5.md_to_html(answer)),
        "used_news": [c for c, _ in picked],
        "count": count,
    }
