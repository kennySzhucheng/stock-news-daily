# -*- coding: utf-8 -*-
"""
M3 AI 深度分析模块 — 用 DeepSeek 对 M2 结构化新闻做综合分析

输入: data/structured_news.json
输出: data/analysis.md

Prompt 硬约束（tasks.md 要求）：
1. 只允许基于输入的新闻原文分析，禁止编造未提供的消息
2. 所有判断句附置信度（高/中/低）
3. 不得出现"建议买入/卖出"，只允许中性表述

密钥来源: 环境变量 DEEPSEEK_API_KEY
"""
import json
import os
import re
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE / "data"

DS_API = "https://api.deepseek.com/chat/completions"
DS_MODEL = "deepseek-chat"


def ds_chat(messages, max_tokens=4000, retries=2):
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY 未设置（环境变量）")
    body = json.dumps({
        "model": DS_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }).encode()
    req = urllib.request.Request(DS_API, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    })
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                d = json.loads(r.read().decode("utf-8"))
            return d["choices"][0]["message"]["content"]
        except Exception as e:
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
            else:
                raise


def build_news_digest(news, max_chars=24000):
    """把结构化新闻压成给 LLM 看的清单文本。
    优先级: policy > stock(含个股) > industry > international > confirmed 标记提升权重
    """
    weight = {"policy": 0, "stock": 1, "industry": 2, "international": 3, "other": 4}
    ranked = sorted(
        news,
        key=lambda n: (weight.get(n.get("category"), 4),
                       0 if n.get("verified") == "confirmed" else 1,
                       n.get("time", ""),),
    )
    lines, total = [], 0
    for i, n in enumerate(ranked):
        line = (f"[{i}] {n['time'][5:16]} {n['source']}|{n['category']}"
                f"|{n.get('verified','')[:4]}"
                f"|股:{','.join(n['stocks'][:3]) or '-'}"
                f"|板:{','.join(n['board'][:2]) or '-'}"
                f"|情:{n.get('sentiment','neutral')}\n{n['text'][:200]}")
        total += len(line)
        if total > max_chars:
            break
        lines.append(line)
    return "\n".join(lines), len(lines)


def analyze(digest, digest_count):
    prompt = f"""你是资深财经新闻分析师。以下编号新闻来自今日多源收集（已做分类、涉及个股/板块提取、多源交叉验证标记 confirmed=多源印证 / unve=单源待核实）。

## 硬约束（违反即废稿）
1. 【禁止编造】你的每一个论断必须能对应到给定新闻的编号。不允许输出任何未在给定新闻中出现的事实、数据、公司名、政策名。你训练数据中的"知识"一概不得使用——若新闻没有说，就当不知道。
2. 【置信度标注】每条判断末尾标（置信度:高/中/低）。单源待核实(unve)新闻支撑的判断最高只能给"中"。
3. 【禁止投资指令】不得出现"建议买入""建议卖出""可以抄底""应该止损"等表述。只允许"值得观察""可纳入跟踪清单""需持续关注验证"等中性表述。
4. 承认局限：新闻≠股价，利好落地可能是利好出尽，你没有历史价格数据，无法判断当前位置。

## 输出格式（markdown，严格遵守）

# 今日市场分析

## 一、市场情绪概览
（2-3 句话 + 一行结论：偏乐观/中性偏谨慎/等）

## 二、值得关注的板块（2-4 个）
### 板块名
- 新闻依据：[编号][编号]（列出对应新闻）
- 逻辑链：哪条新闻 → 通过什么机制 → 可能与该板块股价相关
- （置信度:高/中/低）

## 三、新闻涉及的个股（仅列新闻中出现过的）
| 个股 | 新闻要点 | 关联点与不确定性 | 置信度 |
|---|---|---|---|

## 四、风险提示
（至少 3 条，第 1 条固定为：新闻动向≠股价表现，已被消化的利好可能导致"利好出尽"）

---
*本报告由 AI 自动生成，仅供参考，不构成任何投资建议。所有判断基于文中列出新闻，新闻解读可能存在偏差。市场有风险，投资需谨慎。*

## 今日新闻（共 {digest_count} 条精选，编号@[]用于引用）
{digest}"""

    return ds_chat([{"role": "user", "content": prompt}], max_tokens=4000)


def postcheck(text):
    """输出合规检查：禁止投资指令、无编造标记可视化"""
    violations = []
    for pat in [r"建议买入", r"建议卖出", r"建议买", r"建议卖", r"可以抄底", r"应该止损", r"马上买入", r"立即买入"]:
        if re.search(pat, text):
            violations.append(pat)
    hard = re.findall(r"（置信度[:：][高中低]）", text)
    return violations, len(hard)


def main():
    d = json.loads((DATA_DIR / "structured_news.json").read_text(encoding="utf-8"))
    news = d["news"]
    print(f"输入 {len(news)} 条结构化新闻")

    digest, count = build_news_digest(news)
    print(f"压缩成 {count} 条摘要喂给 DeepSeek ({len(digest)} 字符)")

    result = analyze(digest, count)
    v, n_conf = postcheck(result)
    print(f"合规检查: 禁用词命中 {v}, 置信度标注 {n_conf} 处")

    out_path = DATA_DIR / "analysis.md"
    out_path.write_text(result, encoding="utf-8")
    print(f"输出 → {out_path} ({len(result)} 字符)")


if __name__ == "__main__":
    main()
