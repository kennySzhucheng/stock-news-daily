# -*- coding: utf-8 -*-
"""
M5 网页日报生成模块 — 把 M1~M4 产物整合成一份自包含 HTML 日报

输入:
  data/raw_news.json          （M1 原始新闻，用于统计）
  data/structured_news.json   （M2 结构化新闻，板块/个股/情绪/验证标记）
  data/analysis.md            （M3 DeepSeek 完整分析）
  data/quotes.json            （M4 个股行情）

输出:
  reports/YYYY-MM-DD.html     （当日自包含日报，移动端优先、暗色主题）
  reports/index.html          （索引页，按日期倒序）

要点：
1. 纯单文件 HTML，CSS/JS 全部内联，无任何外部依赖
2. 移动端优先：单栏、大字号、夜间友好，无横向滚动
3. 所有动态文本先 HTML 转义再拼接，防注入
"""
import json
import re
import html
from pathlib import Path
from datetime import datetime

BASE = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE / "data"
REPORTS_DIR = BASE / "reports"

# 分类中文名
CAT_CN = {
    "stock": "个股公告",
    "policy": "宏观政策",
    "industry": "行业动态",
    "international": "国际市场",
    "other": "其他",
}
SENTI_CN = {"bullish": "利好", "bearish": "利空", "neutral": "中性"}
SENTI_CLS = {"bullish": "up", "bearish": "down", "neutral": "flat"}


def esc(s):
    """HTML 转义，动态文本拼接前必须调用"""
    if s is None:
        return ""
    return html.escape(str(s), quote=True)


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------
def load_data():
    out = {}
    out["raw"] = json.loads((DATA_DIR / "raw_news.json").read_text(encoding="utf-8"))
    out["structured"] = json.loads((DATA_DIR / "structured_news.json").read_text(encoding="utf-8"))
    out["quotes"] = json.loads((DATA_DIR / "quotes.json").read_text(encoding="utf-8"))
    out["analysis_md"] = (DATA_DIR / "analysis.md").read_text(encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# 推送摘要（≤200 字）
# ---------------------------------------------------------------------------
def extract_summary(analysis_md):
    """从 M3 分析里取市场情绪结论句，压缩到 ≤200 字"""
    core = ""
    for line in analysis_md.splitlines():
        m = re.match(r"^\s*结论[：:]\s*(.+)$", line)
        if m:
            core = m.group(1).strip()
            break
    if not core:
        # 兜底：取第一段正文
        lines = [l.strip() for l in analysis_md.splitlines() if l.strip()]
        core = lines[1] if len(lines) > 1 else (lines[0] if lines else "")
    # 去掉 markdown 加粗标记
    core = re.sub(r"\*\*(.+?)\*\*", r"\1", core)
    summary = f"今日市场：{core}"
    if len(summary) > 200:
        summary = summary[:199] + "…"
    return summary


def extract_overview(analysis_md):
    """取分析的第一节（市场情绪概览），作为独立版块，避免与完整分析重复"""
    lines = analysis_md.splitlines()
    out = []
    for line in lines:
        # 跳过顶层 "# 今日市场分析" 标题（版块自身已有标题）
        if re.match(r"^#\s+今日市场分析\s*$", line.strip()):
            continue
        # 遇到第二节标题就停
        if re.match(r"^#{1,3}\s*二、", line.strip()):
            break
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Markdown → HTML（针对 M3 分析的固定结构做最小实现）
# ---------------------------------------------------------------------------
def md_to_html(md_text):
    """把 analysis.md 转成 HTML。支持 #/##/###、**加粗**、有序/无序列表、表格、---"""
    lines = md_text.splitlines()
    out = []
    i = 0
    in_table = False
    in_ul = False
    in_ol = False

    def close_lists():
        nonlocal in_ul, in_ol
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_ol:
            out.append("</ol>")
            in_ol = False

    def inline(t):
        t = esc(t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
        t = re.sub(r"\*(.+?)\*", r"<em>\1</em>", t)
        # 引用编号 [12] 保留原样（已经是普通文本）
        return t

    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()

        # 空行 / 分隔线
        if not stripped:
            close_lists()
            if in_table:
                out.append("</table>")
                in_table = False
            i += 1
            continue
        if re.match(r"^-{3,}$", stripped):
            close_lists()
            if in_table:
                out.append("</table>")
                in_table = False
            out.append("<hr>")
            i += 1
            continue

        # 表格
        if stripped.startswith("|"):
            close_lists()
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if not in_table:
                out.append('<table class="md-table">')
                in_table = True
            # 分隔行 |---|---|
            if all(re.match(r"^:?-{2,}:?$", c) for c in cells):
                i += 1
                continue
            is_header = all(c and (c.startswith("**") or True) for c in cells)
            # 表头判定：下一行是分隔行
            next_is_sep = (i + 1 < len(lines)
                           and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i + 1])
                           and "---" in lines[i + 1])
            tag = "th" if next_is_sep else "td"
            out.append("<tr>")
            for c in cells:
                out.append(f"<{tag}>{inline(c)}</{tag}>")
            out.append("</tr>")
            i += 1
            continue

        # 标题
        m = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if m:
            close_lists()
            if in_table:
                out.append("</table>")
                in_table = False
            level = len(m.group(1))
            out.append(f"<h{level} class='md-h{level}'>{inline(m.group(2))}</h{level}>")
            i += 1
            continue

        # 无序列表
        m = re.match(r"^[-*]\s+(.*)$", stripped)
        if m:
            close_lists()
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{inline(m.group(1))}</li>")
            i += 1
            continue

        # 有序列表
        m = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if m:
            close_lists()
            if not in_ol:
                out.append("<ol>")
                in_ol = True
            out.append(f"<li>{inline(m.group(1))}</li>")
            i += 1
            continue

        # 普通段落
        close_lists()
        if in_table:
            out.append("</table>")
            in_table = False
        out.append(f"<p>{inline(stripped)}</p>")
        i += 1

    close_lists()
    if in_table:
        out.append("</table>")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 各版块渲染
# ---------------------------------------------------------------------------
def render_quotes(quotes):
    """个股行情一览：按涨跌幅绝对值倒序，红涨绿跌"""
    qs = quotes.get("quotes", [])
    if not qs:
        return '<p class="empty">今日无行情数据</p>'

    rows = []
    for q in sorted(qs, key=lambda x: abs(x.get("change_pct") or 0), reverse=True):
        pct = q.get("change_pct")
        pct_str = f"{pct:+.2f}%" if isinstance(pct, (int, float)) else "—"
        cls = "up" if (pct or 0) > 0 else ("down" if (pct or 0) < 0 else "flat")
        price = q.get("price")
        price_str = f"{price:.2f}" if isinstance(price, (int, float)) else "—"
        market = q.get("market") or ""
        rows.append(
            f"<tr>"
            f"<td class='q-name'>{esc(q.get('name'))}"
            f"<span class='q-code'>{esc(q.get('code'))}</span></td>"
            f"<td class='q-mkt'>{esc(market)}</td>"
            f"<td class='q-price'>{price_str}</td>"
            f"<td class='{cls}'>{pct_str}</td>"
            f"</tr>"
        )
    failed = quotes.get("failed", [])
    fail_note = ""
    if failed:
        names = "、".join(esc(f["name"]) for f in failed)
        fail_note = f'<p class="muted small">未取到行情：{names}</p>'

    return (
        '<div class="table-wrap"><table class="quote-table">'
        "<thead><tr><th>个股</th><th>市场</th><th>现价</th><th>涨跌</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>{fail_note}"
    )


def render_news(structured):
    """分板块新闻列表，带验证标记、情绪、板块/个股标签、原文链接"""
    news = structured.get("news", [])
    by_cat = {}
    for n in news:
        cat = n.get("category") or "other"
        by_cat.setdefault(cat, []).append(n)

    order = ["policy", "stock", "industry", "international", "other"]
    sections = []
    for cat in order:
        if cat not in by_cat:
            continue
        items = by_cat[cat]
        cards = []
        for n in items:
            verified = n.get("verified") == "confirmed"
            v_badge = (
                '<span class="badge confirmed">已确认</span>'
                if verified
                else '<span class="badge unverified">待核实</span>'
            )
            senti = n.get("sentiment") or "neutral"
            s_badge = (
                f'<span class="badge senti {SENTI_CLS.get(senti, "flat")}">'
                f"{SENTI_CN.get(senti, senti)}</span>"
            )
            boards = "".join(
                f'<span class="tag">{esc(b)}</span>' for b in (n.get("board") or []) if b and b != "其他"
            )
            stocks = "".join(
                f'<span class="tag stock">{esc(s)}</span>' for s in (n.get("stocks") or [])
            )
            url = n.get("url") or ""
            link = f'<a class="src-link" href="{esc(url)}" target="_blank" rel="noopener">原文↗</a>' if url else ""
            time_str = esc((n.get("time") or "")[:16])
            cards.append(
                '<div class="news-card">'
                f'<div class="news-text">{esc(n.get("text"))}</div>'
                '<div class="news-meta">'
                f'<span class="time">{time_str}</span>'
                f'<span class="src">{esc(n.get("source"))}</span>'
                f"{v_badge}{s_badge}"
                f'<span class="tags">{boards}{stocks}</span>'
                f"{link}"
                "</div></div>"
            )
        sections.append(
            f'<section class="news-group">'
            f'<h3>{CAT_CN.get(cat, cat)}<span class="count">{len(items)}</span></h3>'
            f"{''.join(cards)}</section>"
        )
    if not sections:
        return '<p class="empty">今日无结构化新闻</p>'
    return "".join(sections)


# ---------------------------------------------------------------------------
# 页面骨架
# ---------------------------------------------------------------------------
CSS = """
:root{
  --bg:#0f1115; --bg2:#161a22; --card:#1a1f2b; --border:#2a3140;
  --text:#e6e9ef; --muted:#9aa4b5; --accent:#4c8dff;
  --up:#ff5c5c; --down:#2ecc71; --flat:#9aa4b5;
  --confirmed:#4c8dff; --unverified:#f5a623;
}
@media (prefers-color-scheme: light){
  :root:not([data-theme="dark"]){
    --bg:#f5f6f8; --bg2:#ffffff; --card:#ffffff; --border:#e3e6ec;
    --text:#1c2230; --muted:#6b7486; --accent:#2f6fe0;
    --up:#e04848; --down:#1a9e5c; --flat:#6b7486;
  }
}
*{box-sizing:border-box; margin:0; padding:0;}
html{-webkit-text-size-adjust:100%;}
body{
  background:var(--bg); color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
    "Hiragino Sans GB","Microsoft YaHei",sans-serif;
  line-height:1.7; font-size:16px; padding:0 0 48px;
  max-width:760px; margin:0 auto;
}
a{color:var(--accent); text-decoration:none; word-break:break-all;}
header.hero{
  padding:24px 16px 12px; background:var(--bg2);
  border-bottom:1px solid var(--border);
}
header.hero h1{font-size:22px; font-weight:700;}
header.hero .date{color:var(--muted); font-size:14px; margin-top:4px;}
.summary-box{
  margin:16px; padding:14px 16px; background:var(--card);
  border:1px solid var(--border); border-left:3px solid var(--accent);
  border-radius:8px; font-size:15px;
}
main{padding:0 12px;}
section.block{margin:20px 8px;}
section.block > h2{
  font-size:18px; font-weight:700; margin-bottom:12px;
  padding-left:10px; border-left:4px solid var(--accent);
}
.news-group{margin-bottom:18px;}
.news-group h3{
  font-size:15px; color:var(--accent); font-weight:600;
  margin:14px 0 8px; display:flex; align-items:center; gap:8px;
}
.news-group h3 .count{
  background:var(--bg2); color:var(--muted); font-size:12px;
  padding:1px 8px; border-radius:10px; border:1px solid var(--border);
}
.news-card{
  background:var(--card); border:1px solid var(--border);
  border-radius:8px; padding:12px 14px; margin-bottom:10px;
}
.news-text{font-size:15px; word-break:break-word;}
.news-meta{
  display:flex; flex-wrap:wrap; align-items:center; gap:6px;
  margin-top:8px; font-size:13px; color:var(--muted);
}
.badge{
  display:inline-block; font-size:12px; padding:1px 8px;
  border-radius:4px; line-height:1.5; white-space:nowrap;
}
.badge.confirmed{background:rgba(76,141,255,.15); color:var(--confirmed); border:1px solid var(--confirmed);}
.badge.unverified{background:rgba(245,166,35,.15); color:var(--unverified); border:1px solid var(--unverified);}
.badge.senti{border:1px solid var(--border);}
.badge.senti.up{color:var(--up); border-color:var(--up); background:rgba(255,92,92,.08);}
.badge.senti.down{color:var(--down); border-color:var(--down); background:rgba(46,204,113,.08);}
.badge.senti.flat{color:var(--flat);}
.tag{
  font-size:12px; padding:1px 7px; border-radius:4px;
  background:var(--bg2); border:1px solid var(--border); color:var(--muted);
}
.tag.stock{color:var(--accent); border-color:var(--accent);}
.src-link{font-size:13px;}
.table-wrap{overflow-x:auto; -webkit-overflow-scrolling:touch; border-radius:8px; border:1px solid var(--border);}
table.quote-table{width:100%; border-collapse:collapse; background:var(--card); font-size:14px; min-width:420px;}
table.quote-table th, table.quote-table td{
  padding:10px 12px; text-align:left; border-bottom:1px solid var(--border);
  white-space:nowrap;
}
table.quote-table thead th{background:var(--bg2); color:var(--muted); font-weight:600; font-size:13px;}
.q-name{font-weight:600;}
.q-code{display:block; font-size:12px; color:var(--muted); font-weight:400;}
.q-mkt{color:var(--muted); font-size:13px;}
.up{color:var(--up);} .down{color:var(--down);} .flat{color:var(--flat);}
.analysis{
  background:var(--card); border:1px solid var(--border);
  border-radius:8px; padding:16px; font-size:15px;
}
.analysis h1,.analysis h2,.analysis h3{margin:16px 0 8px; line-height:1.4;}
.analysis h1{font-size:20px;} .analysis h2{font-size:18px;} .analysis h3{font-size:16px;}
.analysis p{margin:8px 0;}
.analysis ul,.analysis ol{margin:8px 0 8px 22px;}
.analysis li{margin:4px 0;}
.analysis hr{border:none; border-top:1px solid var(--border); margin:16px 0;}
.analysis .md-table{border-collapse:collapse; margin:12px 0; font-size:14px; display:block; overflow-x:auto; max-width:100%;}
.analysis .md-table th,.analysis .md-table td{
  border:1px solid var(--border); padding:8px 10px; text-align:left; vertical-align:top;
}
.analysis .md-table th{background:var(--bg2); white-space:nowrap;}
.analysis strong{color:var(--text);}
footer.risk{
  margin:28px 8px 8px; padding:16px; background:var(--bg2);
  border:1px solid var(--border); border-radius:8px;
  font-size:13px; color:var(--muted); line-height:1.6;
}
footer.risk h3{font-size:14px; color:var(--unverified); margin-bottom:6px;}
.muted{color:var(--muted);} .small{font-size:13px;} .empty{color:var(--muted); padding:12px 0;}
.index-item{
  display:block; background:var(--card); border:1px solid var(--border);
  border-radius:8px; padding:14px 16px; margin-bottom:10px; color:var(--text);
}
.index-item .d{font-weight:600;} .index-item .s{color:var(--muted); font-size:13px;}
"""


def build_page(data, date_str):
    summary = extract_summary(data["analysis_md"])
    overview_html = md_to_html(extract_overview(data["analysis_md"]))
    analysis_html = md_to_html(data["analysis_md"])
    quotes_html = render_quotes(data["quotes"])
    news_html = render_news(data["structured"])

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>股市情报日报 · {esc(date_str)}</title>
<style>{CSS}</style>
</head>
<body>
<header class="hero">
  <h1>📊 股市情报日报</h1>
  <div class="date">{esc(date_str)} · AI 自动生成 · 仅供参考</div>
</header>

<div class="summary-box">{esc(summary)}</div>

<main>
  <section class="block">
    <h2>市场情绪概览</h2>
    <div class="analysis">{overview_html}</div>
  </section>

  <section class="block">
    <h2>个股行情一览</h2>
    {quotes_html}
  </section>

  <section class="block">
    <h2>分板块新闻</h2>
    {news_html}
  </section>

  <section class="block">
    <h2>完整分析</h2>
    <div class="analysis">{analysis_html}</div>
  </section>
</main>

<footer class="risk">
  <h3>⚠️ 风险声明</h3>
  <p>本报告由 AI 自动生成，仅供参考，不构成任何投资建议。新闻解读可能存在偏差，
  市场有风险，投资需谨慎，请独立判断。所有 AI 结论均基于文中列出的新闻原文，
  未经过人工核实，单一来源信息标注"待核实"。决策权归您本人。</p>
</footer>
</body>
</html>"""


def build_index(report_files):
    items = ""
    for f in report_files:
        date_str = f.stem
        items += (
            f'<a class="index-item" href="{esc(f.name)}">'
            f'<div class="d">{esc(date_str)}</div>'
            f'<div class="s">点击查看当日日报</div>'
            f"</a>"
        )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>股市情报日报 · 索引</title>
<style>{CSS}</style>
</head>
<body>
<header class="hero">
  <h1>📊 股市情报日报</h1>
  <div class="date">全部日报（按日期倒序）</div>
</header>
<main>
  <section class="block">
    {items if items else '<p class="empty">暂无日报</p>'}
  </section>
</main>
</body>
</html>"""


def main():
    date_str = datetime.now().strftime("%Y-%m-%d")
    data = load_data()

    REPORTS_DIR.mkdir(exist_ok=True)

    page = build_page(data, date_str)
    page_path = REPORTS_DIR / f"{date_str}.html"
    page_path.write_text(page, encoding="utf-8")
    print(f"[OK] report -> {page_path} ({len(page)} bytes)")

    # 更新索引
    report_files = sorted(
        [p for p in REPORTS_DIR.glob("*.html") if p.name != "index.html"],
        key=lambda p: p.stem, reverse=True,
    )
    idx = build_index(report_files)
    idx_path = REPORTS_DIR / "index.html"
    idx_path.write_text(idx, encoding="utf-8")
    print(f"[OK] index  -> {idx_path} ({len(report_files)} reports)")

    # 摘要统计
    print(f"    news={len(data['structured']['news'])} quotes={data['quotes']['count']} summary_len={len(extract_summary(data['analysis_md']))}")


if __name__ == "__main__":
    main()
