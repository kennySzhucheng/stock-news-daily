# -*- coding: utf-8 -*-
"""M9 数据聚合层 — 把 M1~M4 的产物整理成网页版需要的结构。

被 server.py（本地服务）与 export.py（静态导出）共用，两种运行方式
产出的数据完全一致。

与 M5 网页日报的区别：
  M5 是给人从头读到尾的单页长文；M9 是可检索、可聚合、可追问的交互界面，
  面向"我想自己找"的场景。

数据来源:
  data/raw_news.json          M1 原始新闻 + 各源状态
  data/structured_news.json   M2 结构化新闻（分类/板块/个股/情绪/验证）
  data/quotes.json            M4 个股行情
  data/analysis.md            M3 深度分析
  reports/*.html              M5 历史日报（用于历史页）
"""
import json
import re
import importlib.util
from pathlib import Path
from datetime import datetime, timezone, timedelta

BASE = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE / "data"
REPORTS_DIR = BASE / "reports"

CST = timezone(timedelta(hours=8))
SLOT_CN = {"am": "盘前", "pm": "盘后"}

# 与 M3 analyzer.build_news_digest 保持一致的排序权重。
# 分析正文里的 [12] 引用指向按此规则排序后的编号，网页要据此还原出处。
DIGEST_WEIGHT = {"policy": 0, "stock": 1, "industry": 2, "international": 3, "other": 4}

# 与 M2 filter.BLOCKED_BOARDS 同源：事件类型、泛指词、以及模型照抄
# prompt 示例的占位符（实测 "行业或概念名" 一条数据里出现了 30 次）
BLOCKED_BOARDS = {
    "行业或概念名", "板块名", "行业", "概念", "主题", "其他", "无",
    "减持", "增持", "定增", "回购", "重组", "解禁", "停牌", "复牌", "上市", "退市",
    "公告", "新闻", "股市", "国际", "宏观", "政策",
}


def _load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def _load_m5_module():
    """按路径载入 M5 的 markdown 转换器，避免复制一份实现导致两处渲染不一致"""
    path = BASE / "modules" / "m5_report" / "report.py"
    spec = importlib.util.spec_from_file_location("m5_report", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# 静态导出的数据形如：
#   window.__DATA__=window.__DATA__||{};window.__DATA__["news"]={...};
_EXPORT_RE = re.compile(
    r'^\s*window\.__DATA__\s*=\s*window\.__DATA__\s*\|\|\s*\{\}\s*;\s*'
    r'window\.__DATA__\["([^"]+)"\]\s*=\s*(.*?)\s*;\s*$', re.S)


def read_export(api_dir):
    """读取 M9 静态导出的 api/*.js，返回 {名字: 数据}"""
    out = {}
    d = Path(api_dir)
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.js")):
        try:
            m = _EXPORT_RE.match(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not m:
            continue
        try:
            out[m.group(1)] = json.loads(m.group(2))
        except json.JSONDecodeError:
            continue
    return out


class Bundle:
    """一次数据快照。所有 API 都从这里取数，避免每请求重读磁盘。"""

    def __init__(self, data_dir=None, reports_dir=None):
        self.data_dir = Path(data_dir or DATA_DIR)
        self.reports_dir = Path(reports_dir or REPORTS_DIR)

        raw = _load_json(self.data_dir / "raw_news.json", {}) or {}
        structured = _load_json(self.data_dir / "structured_news.json", {}) or {}
        quotes = _load_json(self.data_dir / "quotes.json", {}) or {}
        try:
            self.analysis_md = (self.data_dir / "analysis.md").read_text(encoding="utf-8")
        except Exception:
            self.analysis_md = ""

        self.raw_news = raw.get("news", [])
        self.sources_status = raw.get("sources_status", [])
        self.raw_generated_at = raw.get("generated_at", "")

        self.news = structured.get("news", [])
        self.structured_generated_at = structured.get("generated_at", "")
        self.input_count = structured.get("input_count", 0)
        self.prefilter_dropped = structured.get("prefilter_dropped", 0)

        self.quotes = quotes.get("quotes", [])
        self.quotes_failed = quotes.get("failed", [])
        self.quotes_generated_at = quotes.get("generated_at", "")

        self.citation_map = self._build_citation_map()
        self._export_history = None
        self._m5 = None

    @classmethod
    def from_export(cls, api_dir):
        """从 M9 静态导出的 api/*.js 构造 Bundle。

        用于本地直接查看**云端最新产出**的场景（见 sync.py）：不必重跑流水线，
        导出文件里已经覆盖了本地服务需要的全部字段。
        """
        payloads = read_export(api_dir)
        b = cls.__new__(cls)
        b._m5 = None
        b.data_dir = None
        b.reports_dir = None

        ov = payloads.get("overview") or {}
        gen = ov.get("generated_at") or {}
        b.news = (payloads.get("news") or {}).get("items") or []
        b.raw_news = (payloads.get("raw") or {}).get("items") or []
        b.raw_generated_at = gen.get("raw", "")
        # 导出里来源字段叫 name，Bundle 内部统一用 source
        b.sources_status = [
            {"source": s.get("name", "?"), "ok": s.get("ok"),
             "count": s.get("count", 0), "error": s.get("error", "")}
            for s in (ov.get("sources") or [])
        ]
        b.structured_generated_at = gen.get("structured", "")
        b.input_count = ov.get("input_count", 0)
        b.prefilter_dropped = ov.get("prefilter_dropped", 0)

        q = payloads.get("quotes") or {}
        b.quotes = q.get("quotes") or []
        b.quotes_failed = q.get("failed") or []
        b.quotes_generated_at = q.get("generated_at", "")

        b.analysis_md = (payloads.get("analysis") or {}).get("markdown", "") or ""
        b._export_history = (payloads.get("history") or {}).get("reports") or []
        b.citation_map = b._build_citation_map()
        return b

    # -- 引用编号还原 ------------------------------------------------------
    def _build_citation_map(self):
        """复刻 M3 的排序，得到 编号 -> 结构化新闻下标 的映射。

        M3 用稳定排序按 (类别权重, 是否已确认, 时间) 排列后从 0 编号，
        这里的 order 与之一致，故 order[i] 就是正文里 [i] 指的那条新闻。
        """
        order = sorted(
            range(len(self.news)),
            key=lambda j: (
                DIGEST_WEIGHT.get(self.news[j].get("category"), 4),
                0 if self.news[j].get("verified") == "confirmed" else 1,
                self.news[j].get("time", ""),
            ),
        )
        return {i: j for i, j in enumerate(order)}

    def news_by_id(self, nid):
        if 0 <= nid < len(self.news):
            return self.news[nid]
        return None

    # -- 渲染 --------------------------------------------------------------
    @property
    def m5(self):
        if self._m5 is None:
            self._m5 = _load_m5_module()
        return self._m5

    def linkify_citations(self, html_text):
        """把分析 HTML 里的 [12] 换成可点击的引用链接。

        只处理标签之外的文本，避免误伤属性值。
        """
        def repl(m):
            num = int(m.group(1))
            if num not in self.citation_map:
                return m.group(0)
            return (f'<a class="cite" href="#news-{self.citation_map[num]}" '
                    f'data-news-id="{self.citation_map[num]}" '
                    f'title="查看出处新闻">{num}</a>')

        parts = re.split(r"(<[^>]+>)", html_text)
        for i, p in enumerate(parts):
            if p.startswith("<"):
                continue
            parts[i] = re.sub(r"[\[［](\d{1,4})[\]］]", repl, p)
        return "".join(parts)

    def analysis_html(self):
        """M3 分析 → 带引用链接的 HTML"""
        if not self.analysis_md:
            return "<p class='empty'>今日暂无分析（M3 未产出）</p>"
        return self.linkify_citations(self.m5.md_to_html(self.analysis_md))

    # -- 统计 --------------------------------------------------------------
    def overview(self):
        cats, sentis, veri = {}, {}, {}
        for n in self.news:
            cats[n.get("category") or "other"] = cats.get(n.get("category") or "other", 0) + 1
            s = n.get("sentiment") or "neutral"
            sentis[s] = sentis.get(s, 0) + 1
            v = n.get("verified") or "unverified"
            veri[v] = veri.get(v, 0) + 1

        # 多空倾向分：只在"有倾向"的新闻里算，neutral 不计入分母。
        # 但必须同时把基数（directional）给出去——100+ 条里只有 8 条带倾向
        # 时，单看分数会误以为情绪极强，前端要一并显示基数。
        bull = sentis.get("bullish", 0)
        bear = sentis.get("bearish", 0)
        directional = bull + bear
        senti_score = round((bull - bear) / directional, 3) if directional else 0.0

        qs = [q for q in self.quotes if isinstance(q.get("change_pct"), (int, float))]
        up = sum(1 for q in qs if q["change_pct"] > 0)
        down = sum(1 for q in qs if q["change_pct"] < 0)
        flat = len(qs) - up - down
        by_pct = sorted(qs, key=lambda q: q["change_pct"], reverse=True)
        # 榜单独取真正涨/跌的：直接切列表头尾时，若下跌个股不足 5 只，
        # "跌幅居前"里会混进上涨的股票，读起来像是跌了。
        gainers = [q for q in by_pct if q["change_pct"] > 0][:5]
        losers = [q for q in by_pct if q["change_pct"] < 0][-5:][::-1]

        return {
            "date": datetime.now(CST).strftime("%Y-%m-%d"),
            "raw_count": len(self.raw_news),
            "structured_count": len(self.news),
            "input_count": self.input_count,
            "prefilter_dropped": self.prefilter_dropped,
            "sources": [
                {"name": s.get("source", "?"), "ok": bool(s.get("ok")),
                 "count": s.get("count", 0), "error": s.get("error", "")}
                for s in self.sources_status if "source" in s
            ],
            "categories": cats,
            "sentiments": sentis,
            "verified": veri,
            "sentiment_score": senti_score,
            "directional": directional,       # 分子分母的基数，前端必须展示
            "directional_note": (f"以 {directional} 条有倾向的新闻为基数"
                                 f"（共 {len(self.news)} 条）") if directional else "今日无带倾向的新闻",
            "conclusion": self.conclusion(),
            "summary": self.summary(),
            "market_view": self.market_view(),
            "quotes": {
                "count": len(self.quotes),
                "failed": len(self.quotes_failed),
                "up": up, "down": down, "flat": flat,
                "gainers": gainers,
                "losers": losers,
            },
            "top_boards": self.boards()[:8],
            "generated_at": {
                "raw": self.raw_generated_at,
                "structured": self.structured_generated_at,
                "quotes": self.quotes_generated_at,
            },
        }

    def conclusion(self):
        """M3 的"结论：…"一行。

        M3 会不定期把整行写成 `**结论：中性偏谨慎**——…`（加粗标记在行首），
        所以先剥掉加粗再匹配，否则总览页的结论卡会空掉。
        """
        for line in self.analysis_md.splitlines():
            s = re.sub(r"\*\*(.+?)\*\*", r"\1", line).strip()
            m = re.match(r"^结论[：:]\s*(.+)$", s)
            if m:
                return m.group(1).strip()
        return ""

    def summary(self):
        c = self.conclusion()
        return f"今日市场：{c}" if c else "今日暂无市场情绪判断"

    def market_view(self, max_sections=6):
        """M3「值得关注的板块」的结构化版本，供网页展示与追问上下文复用"""
        sections, cur = [], None
        for line in self.analysis_md.splitlines():
            s = line.strip()
            m = re.match(r"^###\s+(.+?)\s*$", s)
            if m:
                cur = {"name": m.group(1).strip(), "basis": "", "logic": "", "confidence": ""}
                sections.append(cur)
                continue
            if cur is None:
                continue
            if re.match(r"^#{1,2}\s", s):
                cur = None
                continue
            m = re.match(r"^[-*]\s*新闻依据[：:]\s*(.*)$", s)
            if m and not cur["basis"]:
                cur["basis"] = m.group(1).strip()
                continue
            m = re.match(r"^[-*]\s*逻辑链[：:]\s*(.*)$", s)
            if m and not cur["logic"]:
                cur["logic"] = re.sub(r"\*\*(.+?)\*\*", r"\1", m.group(1)).strip()
                continue
            m = re.match(r"^[-*]\s*[（(]置信度[：:]\s*([^）)]+)[）)]\s*$", s)
            if m and not cur["confidence"]:
                cur["confidence"] = m.group(1).strip()
        return [s for s in sections[:max_sections] if s["logic"] or s["basis"]]

    def boards(self):
        """按 M2 提取的板块标签聚合，带情绪分布与关联新闻。

        与 M2 的 normalize_boards 同一套判定：把被误填进 board 的公司名滤掉。
        M2 已清过一遍，这里再拦一道是为了兼容修复前产出的旧数据。
        只做全等比较，避免误伤"创投"（因"浦东创投集团"）这类真板块。
        """
        all_stocks = set()
        for n in self.news:
            for s in (n.get("stocks") or []):
                if s and isinstance(s, str):
                    all_stocks.add(s.strip())

        agg = {}
        for i, n in enumerate(self.news):
            own = {s.strip() for s in (n.get("stocks") or []) if s}
            for b in (n.get("board") or []):
                b = (b or "").strip()
                if not b or b in BLOCKED_BOARDS or b in own or b in all_stocks:
                    continue
                e = agg.setdefault(b, {"name": b, "count": 0, "news": [],
                                       "sentiments": {"bullish": 0, "bearish": 0, "neutral": 0},
                                       "stocks": set()})
                e["count"] += 1
                e["news"].append(i)
                e["sentiments"][n.get("sentiment") or "neutral"] += 1
                for s in (n.get("stocks") or []):
                    if s:
                        e["stocks"].add(s)
        out = []
        for e in agg.values():
            e["stocks"] = sorted(e["stocks"])
            out.append(e)
        out.sort(key=lambda x: (x["count"], x["sentiments"]["bullish"]), reverse=True)
        return out

    def history(self):
        """历史日报清单。

        本地模式扫 reports/；export 模式直接用导出里带过来的清单
        （本地没有那些 HTML 文件）。
        """
        if self._export_history is not None:
            return self._export_history
        by_date = {}
        for p in self.reports_dir.glob("*.html"):
            if p.name in ("index.html", "latest.html"):
                continue
            m = re.match(r"^(\d{4}-\d{2}-\d{2})(?:-(am|pm))?$", p.stem)
            if not m:
                continue
            by_date.setdefault(m.group(1), []).append({
                "slot": m.group(2) or "", "file": p.name,
                "size": p.stat().st_size,
                "label": SLOT_CN.get(m.group(2) or "", "日报"),
            })
        out = []
        for d in sorted(by_date, reverse=True):
            entries = sorted(by_date[d], key=lambda x: x["slot"])
            out.append({"date": d, "entries": entries,
                        "latest": entries[-1]["file"]})
        return out

    def raw_view(self):
        """M1 原始新闻 + 是否被 M2 选中。

        网页版的「原始视图」——推送版只给筛过的结果，这里能让用户看到
        被丢掉的都是些什么，便于判断筛选是否合理。
        """
        kept = set()
        for n in self.news:
            kept.add(self._key(n.get("time"), n.get("text")))
        out = []
        for i, n in enumerate(self.raw_news):
            # 导出的数据已带 kept 标记且正文被截断到 300 字；
            # 本地数据则按"时间 + 正文主干"回推是否被 M2 选中
            if "kept" in n:
                is_kept = bool(n["kept"])
            else:
                is_kept = self._key(n.get("time"), n.get("text")) in kept
            out.append({
                "id": i, "time": n.get("time", ""), "source": n.get("source", ""),
                "category": n.get("category", ""), "url": n.get("url", ""),
                "text": n.get("text", ""), "kept": is_kept, "dropped": not is_kept,
            })
        return out

    @staticmethod
    def _key(time_str, text):
        """原始新闻与结构化新闻的对应键：时间 + 去空白后的正文主干"""
        return ((time_str or "")[:16], re.sub(r"\s+", "", text or "")[:24])


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------
def query_news(bundle, q="", cat="", senti="", verified="", source="",
               board="", stock="", sort="time", limit=40, offset=0):
    """按条件筛选新闻。返回 (总数, 当前页条目)。

    q 支持空格分隔的多关键词，全部命中才算匹配（AND）。
    """
    items = []
    terms = [t.lower() for t in (q or "").split() if t.strip()]
    for i, n in enumerate(bundle.news):
        if cat and (n.get("category") or "other") != cat:
            continue
        if senti and (n.get("sentiment") or "neutral") != senti:
            continue
        if verified and (n.get("verified") or "unverified") != verified:
            continue
        if source and n.get("source") != source:
            continue
        if board and board not in (n.get("board") or []):
            continue
        if stock and stock not in (n.get("stocks") or []):
            continue
        if terms:
            hay = ((n.get("text") or "") + " " + " ".join(n.get("stocks") or [])
                   + " " + " ".join(n.get("board") or [])).lower()
            if not all(t in hay for t in terms):
                continue
        items.append({"id": i, **n})

    if sort == "source":
        items.sort(key=lambda x: (x.get("source") or "", x.get("time") or ""), reverse=True)
    elif sort == "sentiment":
        rank = {"bullish": 0, "bearish": 1, "neutral": 2}
        items.sort(key=lambda x: (rank.get(x.get("sentiment"), 3), x.get("time") or ""),
                   reverse=False)
    else:
        items.sort(key=lambda x: x.get("time") or "", reverse=True)

    return len(items), items[offset:offset + limit]
