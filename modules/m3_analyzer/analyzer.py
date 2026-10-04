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


def _urlopen(req, timeout=120):
    """优先直连，失败回退系统代理。

    Windows 上 urllib 会自动读取系统代理设置；若梯子开着但节点不通，
    所有请求都会失败。
    """
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(req, timeout=timeout)
    except Exception:
        return urllib.request.urlopen(req, timeout=timeout)


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
            with _urlopen(req, 120) as r:
                d = json.loads(r.read().decode("utf-8"))
            return d["choices"][0]["message"]["content"]
        except Exception as e:
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
            else:
                raise


def build_news_digest(news, max_chars=24000):
    """把结构化新闻压成给 LLM 看的清单文本。

    排序（**2026-10-04 改向**）：
      ① 类别权重 policy > stock > industry > international > other
      ② confirmed 优先于 unverified
      ③ **时间降序（最新优先）** ← 旧实现是升序，见下方说明

    旧实现第三项是 `n.get("time","")` **升序**，配合"超预算即 break"的截断，
    实际丢掉的恰恰是**最新**的新闻：预算 24000 字符只够装前若干条，于是
    `fetch_gov` 抓来的旧政策（如 2026-07-02 的国函）排在最前、把当日快讯全部
    挤出 digest —— 付费 token 花在旧文上，还让模型把旧政策当"今日政策"。

    现在改成最新优先，截断发生在排序**之后**，所以**超预算时丢掉的是最旧的**
    （`break` 逻辑本身保留不变）。digest 头部的 `[i]` 编号与这里的顺序严格一致，
    下游 M9 的引用还原、M10 的候选引用都依赖它。

    实现说明：`time` 是字符串，形状为 "YYYY-MM-DD HH:MM:SS"，字典序与时间序一致，
    故用"两次稳定排序"实现多级排序：先按 time 降序，再按 (权重, confirmed) 升序。
    Python 的 sort 是稳定的，第二趟不会打乱第一趟相对同 key 元素的时间序，
    这样就不需要把 time 归一成可比较的数值或取负，也不会因 time 缺失（空串）
    而抛异常 —— 空串在降序里自然排到最后（最旧）。
    """
    weight = {"policy": 0, "stock": 1, "industry": 2, "international": 3, "other": 4}
    ranked = sorted(news, key=lambda n: n.get("time", "") or "", reverse=True)
    ranked.sort(key=lambda n: (weight.get(n.get("category"), 4),
                               0 if n.get("verified") == "confirmed" else 1))
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


# ---------------------------------------------------------------------------
# 输出契约（2026-10-04 新增）
# ---------------------------------------------------------------------------
# 既有事实：M3 的输出**零校验** —— 空串、半截、命中"建议买入"，只要 HTTP 200 就
# 一路算成功，写盘了事。10-04 分析正文里出现禁用表述也没人拦（postcheck 的结果
# 只是 print 出来，prompt 里写着"违反即废稿"却没有代码拦截）。
#
# 硬门槛阈值：
MIN_ANALYSIS_CHARS = 800

# 结论行里允许出现的内容下限：太短说明模型只吐了半句（如"结论："后什么都没有），
# 或回显了 prompt 的格式说明（"偏乐观/中性偏谨慎/等"），不能算有效结论。
MIN_CONCLUSION_CHARS = 2

# LLM 返回空/近空时写入的占位正文。**必须非空**：M6（push.py）用
# `(DATA_DIR/"analysis.md").read_text()` 无保护地读取，文件缺失会让 M6 直接抛异常；
# M5（report.py）也要把它转成 HTML。宁可写一段明确的占位文字，也不留下空文件。
PLACEHOLDER_ANALYSIS = (
    "# 今日市场分析\n\n"
    "（M3 未产出有效分析，本次分析缺失，请以下方新闻原文为准。）\n\n"
    "## 一、市场情绪概览\n\n"
    "结论：本次分析缺失（M3 模型未返回有效内容，置信度:低）\n\n"
    "## 四、风险提示\n\n"
    "1. 新闻动向≠股价表现，已被消化的利好可能导致\"利好出尽\"\n"
    "2. 本次分析因模型未产出有效内容而缺失，请勿据此判断市场情绪\n"
)


def extract_conclusion(analysis_md):
    """取 M3 的"结论：…"一行。三级兜底，逻辑照抄 M6 push.py 的
    `extract_sentiment`（2026-10-04 时点该文件写的仍是"正则匹配 +
    找不到就返回 None"的单级版本；push.py 里已有的是**加粗剥离**这一层，
    这里按任务要求实现同样简单的三级兜底并注明出处）：

      ① 剥掉 `**加粗**` 后按行首匹配 `结论：…`（push.py:108-113 的写法）
      ② 行内任意位置出现 `结论：…`（模型把结论写在行中间，如
         "综合看，结论：中性偏谨慎"）
      ③ `**结论**：…` 这种把"结论"二字加粗、冒号在粗体外的写法

    返回结论文本；找不到返回 ""。
    """
    # ① 行首匹配（先剥加粗）
    for line in (analysis_md or "").splitlines():
        s = re.sub(r"\*\*(.+?)\*\*", r"\1", line).strip()
        m = re.match(r"^结论[：:]\s*(.+)$", s)
        if m:
            return m.group(1).strip()
    # ② 行内任意位置
    for line in (analysis_md or "").splitlines():
        s = re.sub(r"\*\*(.+?)\*\*", r"\1", line).strip()
        m = re.search(r"结论[：:]\s*(.+)$", s)
        if m:
            return m.group(1).strip()
    # ③ `**结论**：…`（结论二字在粗体里，冒号在外面）
    m = re.search(r"\*\*结论\*\*\s*[：:]\s*(.+?)(?:\n|$)", analysis_md or "")
    if m:
        return m.group(1).strip()
    return ""


def _conclusion_ok(conclusion):
    """结论行是否"有效"：非空、够长、且不是 prompt 里的占位选项。

    prompt 的输出格式示例写着"（2-3 句话 + 一行结论：偏乐观/中性偏谨慎/等）"，
    模型偶尔会把这行原样抄回来 —— "偏乐观/中性偏谨慎/等"整串明显是模板而非结论，
    但"偏乐观"本身是真结论，故只在**同时**出现多个斜杠选项时才判为占位。
    """
    c = (conclusion or "").strip().strip("*_` ")
    if len(c) < MIN_CONCLUSION_CHARS:
        return False
    if "本次分析缺失" in c:            # 占位正文，明确不算通过
        return False
    if c.count("/") >= 2 and ("等" in c or "偏乐观" in c and "偏谨慎" in c):
        return False                   # 回显 prompt 模板
    return True


def validate_analysis(text):
    """对 M3 输出做硬门槛校验，返回 status dict（字段见任务契约）。

    为什么**不** `sys.exit(1)`（关键取舍）：M3 位于主链路中段，M5（日报）、
    M6（微信推送，workflow 里带 always()）都在它后面。一旦这里 exit 掉，
    后续步骤连锁异常/跳过 —— 用户拿到的不是一个带标记的坏报告，而是**什么都
    没有**（连新闻原文都看不到）。故取舍是：**宁可出报告 + 落状态文件，让 M8
    体检去告警**，也不让整条链路断掉。失败信息通过三处暴露：
      ① 本函数写的 data/analysis.status.json（ok=false + 具体原因）
      ② print 的醒目 warn
      ③ 正文里的占位标记（当 LLM 返回空/极短时）
    """
    text = text or ""
    violations, n_conf = postcheck(text)
    conclusion = extract_conclusion(text)
    has_sections = ("市场情绪概览" in text) and ("风险提示" in text)
    chars = len(text)
    ok = bool(
        chars >= MIN_ANALYSIS_CHARS
        and has_sections
        and not violations
        and _conclusion_ok(conclusion)
    )
    return {
        "ok": ok,
        "chars": chars,
        "has_sections": has_sections,
        "violations": violations,
        "conclusion_ok": _conclusion_ok(conclusion),
        "confidence_marks": n_conf,
        "conclusion": conclusion,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def write_status(status):
    """落 data/analysis.status.json（先写 .tmp 再 replace，避免读到半截文件）"""
    path = DATA_DIR / "analysis.status.json"
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(path)
        print(f"状态 → {path} (ok={status['ok']})")
    except Exception as e:
        print(f"[warn] analysis.status.json 写入失败（{e}），不影响报告")


def main():
    d = json.loads((DATA_DIR / "structured_news.json").read_text(encoding="utf-8"))
    news = d["news"]
    print(f"输入 {len(news)} 条结构化新闻")

    digest, count = build_news_digest(news)
    print(f"压缩成 {count} 条摘要喂给 DeepSeek ({len(digest)} 字符)")

    # analyze() 可能抛异常（DEEPSEEK_API_KEY 未设置 → RuntimeError；ds_chat 重试
    # 耗尽后 re-raise）。这里吞掉并降级为占位正文：M3 失败不该让 M5/M6 连锁失败
    # （同 validate_analysis 里的取舍说明）。
    try:
        result = analyze(digest, count)
    except Exception as e:
        print("!" * 72)
        print(f"[WARN] DeepSeek 调用失败（{type(e).__name__}: {str(e)[:120]}），"
              f"降级为占位分析")
        print("!" * 72)
        result = None
    v, n_conf = postcheck(result or "")
    print(f"合规检查: 禁用词命中 {v}, 置信度标注 {n_conf} 处")

    out_path = DATA_DIR / "analysis.md"
    if not (result or "").strip():
        # LLM 返回空串：也必须写盘且非空（理由见 PLACEHOLDER_ANALYSIS 注释）
        result = PLACEHOLDER_ANALYSIS
        print("!" * 72)
        print("[WARN] DeepSeek 返回空内容，已写入占位分析；"
              "本次 analysis.md 不含有效市场判断（下游会看到占位标记）")
        print("!" * 72)
    out_path.write_text(result, encoding="utf-8")
    print(f"输出 → {out_path} ({len(result)} 字符)")

    status = validate_analysis(result)
    write_status(status)

    if not status["ok"]:
        reasons = []
        if status["chars"] < MIN_ANALYSIS_CHARS:
            reasons.append(f"长度 {status['chars']} < {MIN_ANALYSIS_CHARS}")
        if not status["has_sections"]:
            reasons.append("缺「市场情绪概览」或「风险提示」小节")
        if status["violations"]:
            reasons.append(f"禁用词 {status['violations']}")
        if not status["conclusion_ok"]:
            reasons.append("未能解析出有效「结论：」行")
        print("!" * 72)
        print(f"[WARN] M3 输出契约未通过：{'；'.join(reasons)}")
        print("       报告已照常落盘（不让 M5/M6 连锁失败），"
              "但内容不可信，请检查 data/analysis.status.json")
        print("!" * 72)


if __name__ == "__main__":
    main()
