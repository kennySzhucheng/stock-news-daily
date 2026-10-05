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


def _load_jsonl(path):
    """JSON Lines → list[dict]。文件缺失或某行坏掉都不该让整个页面挂掉。

    M10 的候选账本在 reports/picks/ 而不是 data/ —— 它随 gh-pages 跨天留存
    （见 m10_picks/picks.py 的说明），是唯一跨运行累积的数据文件。
    """
    rows = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception:
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


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

        # M10 候选账本：跨天累积，必须与 from_export 成对赋值，
        # 否则 export 模式下这一页会永远空着
        self.picks = _load_jsonl(self.reports_dir / "picks" / "ledger.jsonl")

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
        # 与 __init__ 成对：少了这一行，export 模式下候选页永远是空的
        b.picks = (payloads.get("picks") or {}).get("rows") or []
        b.citation_map = b._build_citation_map()
        return b

    # -- 引用编号还原 ------------------------------------------------------
    def _build_citation_map(self):
        """复刻 M3 的排序，得到 编号 -> 结构化新闻下标 的映射。

        M3 用**两次稳定排序**排列后从 0 编号：
          ① 先按 time **降序**（最新优先）—— 2026-10-04 起由升序改为降序
          ② 再按 (类别权重, 是否已确认) 升序（稳定排序，不打乱同一 key 内的时间序）
        下面必须与 `modules/m3_analyzer/analyzer.py` 的 `build_news_digest`
        **逐字同步**，否则正文里的 [12] 会点到错的新闻 —— 这个错位是静默的，
        页面上看起来一切正常。改任何一边都必须同时改另一边。
        """
        order = sorted(
            range(len(self.news)),
            key=lambda j: self.news[j].get("time", "") or "",
            reverse=True,
        )
        order.sort(
            key=lambda j: (
                DIGEST_WEIGHT.get(self.news[j].get("category"), 4),
                0 if self.news[j].get("verified") == "confirmed" else 1,
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
        M5 的 md_to_html 已把 [12] 排成 <sup class="cite-n">12</sup>（日报
        上标样式），先还原成 [12] 再挂链，两条渲染管线产出同一种可点引用。
        """
        html_text = re.sub(r'<sup class="cite-n">(\d{1,4})</sup>', r"[\1]", html_text)

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
        """M3 的市场情绪结论（三级兜底），取不到返回 ""。

        **同一判据在另外两处各有一份副本，改动必须三处同步、逐字同逻辑**：
          - modules/m6_push/push.py     :: extract_sentiment
          - modules/m5_report/report.py :: extract_conclusion
        这里返回 ""（而不是某句固定文案），由 summary()/前端决定怎么标记"没解析到"。

        级1  逐行剥掉成对 ** 后匹配 `^结论[：:]` + 正文（M3 有时写成
             `**结论：中性偏谨慎**——…`，加粗标记在行首，不剥就漏，总览卡会空掉）；
        级2  首个含「结论/情绪」或带情绪词、且剥 ** 后 >= 6 字的正文行（跳过标题行），
             取「结论/情绪」之后的文本，再砍到最后一个「为/是/：/，」之后、
             含 乐观|中性|谨慎|悲观|积极|偏 的短句；取不到则整行截断 40 字；
        级3  「## 一、市场情绪概览」小节内的第一段正文（跳过标题行），截断 60 字。
        """
        text = self.analysis_md or ""

        def strip_bold(line):
            return re.sub(r"\*\*(.+?)\*\*", r"\1", line).strip()

        def after_marker(s):
            """取「结论/情绪」之后、最后一个「为/是/：/，」之后的情绪短句"""
            pos = max(s.rfind("结论"), s.rfind("情绪"))
            body = s[pos + 2:] if pos >= 0 else s
            cut = -1
            for ch in ("为", "是", "：", ":", "，", ","):
                cut = max(cut, body.rfind(ch))
            cand = (body[cut + 1:] if cut >= 0 else body).strip(
                " 　*#-—…。；;、!！?？\"'“”‘’()（）[]【】")
            if cand and any(w in cand for w in ("乐观", "中性", "谨慎", "悲观", "积极", "偏")):
                return cand[:40]
            return ""

        # 级 1：行首「结论：…」
        for line in text.splitlines():
            s = strip_bold(line)
            m = re.match(r"^结论[：:]\s*(.+)$", s)
            if m and m.group(1).strip():
                return m.group(1).strip()

        # 级 2：没有独立成行的「结论：」，就从含结论/情绪词的正文行里抠
        for line in text.splitlines():
            s = strip_bold(line)
            if not s or s.startswith("#") or len(s) < 6:
                continue
            if not ("结论" in s or "情绪" in s
                    or any(w in s for w in ("乐观", "中性", "谨慎", "悲观", "积极", "偏"))):
                continue
            frag = after_marker(s)
            return frag if frag else s[:40]

        # 级 3：「市场情绪概览」小节的第一段正文
        inside = False
        for line in text.splitlines():
            s = line.strip()
            if not inside:
                if re.match(r"^#{1,6}\s", s) and "市场情绪概览" in s:
                    inside = True
                continue
            if re.match(r"^#{1,6}\s", s):
                break
            if not s or re.match(r"^-{3,}$", s):
                continue
            para = re.sub(r"^(?:[-*]|\d+[.、)])\s*", "", strip_bold(s)).strip()
            if para:
                return para[:60]
        return ""

    def summary(self):
        """总览卡的一句话结论。

        解析不到就**明说没解析到**，不再回"暂无"型固定文案——那是把解析
        失败伪装成"今天没有情绪判断"，而同一份 analysis.md 里其实有完整分析
        （2026-09-28/10-04 的推送与总览卡都栽在这句话上）。详见 conclusion()。
        """
        c = self.conclusion()
        if c:
            return f"今日市场：{c}"
        return "（M3 结论未解析，见「深度分析」页）"

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

    def picks_view(self, days=None):
        """M10 候选观察清单 → 网页版需要的结构。

        days 为 None 表示全量。静态导出传一个较小的窗口（见 export.py），
        否则账本会常年累积（一天最多 6 条，一年约 1500 条）把首页流量翻倍；
        local/export 两端**结构完全一致，只是行数不同**，前端不需要分支。

        「今日」取账本里最新的日期而不是墙上时钟：盘前运行时当日还没有候选，
        此时把昨天的候选标成「今日」是错的；标成日期本身（`latest_date`）才如实。

        批次与字段（2026-10-04 盘前通道）：
          - rows 仍是**扁平列表**（server.py / check.py / export.py 都按列表读），
            但排序改成「日期倒序 → 同日盘前在前 → id 倒序」，并给每行补
            `slot_label`（推荐/盘后中文名），前端据此插入分组小标题即可；
          - `entry_zone` / `trigger` 原样透传（缺失补空串），前端判空后渲染，
            export.py 不需要改 —— 它把 rows 整个序列化，不做字段白名单；
          - `is_recommendation`（2026-10-05，纯附加布尔）：该行是不是「推荐」批
            （slot=am）。前端不必自己认 slot 的取值口径 —— 那个口径（缺失/写坏
            一律按 pm）只在这里定义一次；账本既有字段一个都没动。
          - `groups` 是纯附加的分组摘要（推荐在前），行数仍以 rows 为准。
          - `bench` / `bench2` / `bench_note`（2026-10-05，纯附加）：两个基准的中文名
            与「为什么同时给两个」的那句解释；每档的两个口径在
            `stats[k]`（n/alpha 与 n2/alpha2）与 `rows[].reviews[k]`（alpha/alpha2）
            里，缺第二个口径的旧记录是 null，前端显示「—」而不是 0%。
        """
        rows = []
        for r in (self.picks or []):
            # basis_refs 是 M3 digest 里的编号（M3 先按类别重排过），不是 news 数组
            # 下标，直接拿去查新闻会翻出**另一条**。这里统一翻成真实下标，
            # 前端只认 basis_ids，不必知道 digest 的排序规则。
            r = dict(r)
            r["basis_ids"] = [self.citation_map[b] for b in (r.get("basis_refs") or [])
                              if b in self.citation_map]
            # 盘前通道的两个字段**原样透传**（只把缺失/None 归一成空串，值本身不动）：
            # 前端判空即可决定渲不渲染，不必知道账本里旧行没有这两个键
            r["entry_zone"] = r.get("entry_zone") or ""
            r["trigger"] = r.get("trigger") or ""
            r["slot_label"] = SLOT_GROUP_CN[pick_slot(r)]
            # 该行是不是「推荐」（盘前 08:10 那批）。**只增不改**：前端可以直接用
            # 这个布尔渲染「为什么推荐」，不必自己判 slot（旧行没有 slot 字段，
            # 判据只在 pick_slot 里定义一次）。
            r["is_recommendation"] = pick_slot(r) == "am"
            rows.append(r)
        if days is not None:      # 注意用 is not None：days=0 是「只要今天」，不是「不筛选」
            cutoff = (datetime.now(CST) - timedelta(days=days)).strftime("%Y-%m-%d")
            rows = [r for r in rows if (r.get("date") or "") >= cutoff]
        # 日期倒序 → 同日盘前在前 → id 倒序。用 -rank 配合 reverse=True：元组整体
        # 取反序，于是日期仍是倒序、rank 变成升序（am=0 在 pm=1 之前）。
        rows.sort(key=lambda r: (r.get("date") or "", -SLOT_ORDER[pick_slot(r)],
                                 r.get("id") or ""), reverse=True)

        latest = max((r.get("date") or "" for r in rows), default="")
        # 均值与样本数由 M5 同一份实现算出，保证日报与网页版不会各算各的。
        # picks_stats 现在同时给两个口径：n/alpha（沪深300）与 n2/alpha2（中证1000），
        # 两者样本数各算各的（老账本行只进第一个口径）—— 前端据此分别显示。
        stats = self.m5.picks_stats(rows)
        # 推翻条件核查（2026-10-05）：逐条结论的标签表 + **关键统计**（逻辑破产却
        # 价格跑赢的条数）。判据与口径只在 M5 里定义一次，这里原样透传 —— 静态导出
        # 把整个 dict 序列化，前端不必自己认 verdict 的取值，也不必自己写统计文案。
        inv = self.m5.inv_check_stats(rows)
        inv_line = self.m5.inv_check_stat_line(inv)
        groups = []
        for s in ("am", "pm"):
            grp = [r for r in rows if pick_slot(r) == s]
            if grp:
                groups.append({"slot": s, "label": SLOT_GROUP_CN[s], "count": len(grp)})
        return {
            "rows": rows,
            "groups": groups,
            "latest_date": latest,
            "stats": {str(k): v for k, v in stats.items()},
            # 两个基准的名字与那句解释（纯附加字段，2026-10-05）：前端照抄渲染即可，
            # 判据/文案只在这里定义一次，旧导出没有这些字段时前端回退到内置常量。
            "bench": BENCH_CN,
            "bench2": BENCH2_CN,
            "bench_note": PICK_BENCH_NOTE,
            # 推翻条件核查（纯附加字段，2026-10-05）：labels 是四态文案（**没核查 ≠
            # 未触发**，故 null 单独一条 unchecked），stats 是数字口径，line 是渲染好
            # 的一句关键统计（sample 为 0 时为空串 → 前端整条不显示）。
            "inv": {
                "labels": dict(self.m5.INV_CHECK_CN),
                "unchecked": self.m5.INV_UNCHECKED_CN,
                # 逐条原因在网格里截断的宽度（前端不自己硬编码截断长度）
                "reason_max": self.m5.INV_REASON_MAX,
                "stats": {str(k): v for k, v in inv.items()},
                "line": inv_line,
            },
            "total": len(rows),
        }

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
# 来源清单：confirmed 条目的徽章要带上"是谁刊发的"
#
# confirmed = 至少两个**独立出版方**刊发过同一事件（M2 判据），依据是 M1 跨源
# 去重时聚合进 `sources` 的全部报道方（已排序，至少含自身 source）。只给二值
# 徽章等于替读者下结论；列出来源，印证强度由读者自己判断。
#
# 与 M5 日报（report.py::sources_label）、M6 推送（push.py::push_verified_note）
# 同一套判据，三处各有一份实现 —— 与 conclusion() 的三份副本同理：这三个模块
# 是各自独立运行的入口，不为一句文案互相 import（少一个可以静默炸掉的依赖）。
# 改动任何一处时，另两处必须同步。
#
# 列表徽章截断到 4 家、详情弹层不截断（SOURCES_FULL=0 表示不限）。
# ---------------------------------------------------------------------------
SOURCES_IN_LIST = 4
SOURCES_FULL = 0


def news_sources(n):
    """该新闻的全部报道来源：优先 sources，缺字段时退回自身 source。

    去重、保持原顺序（M1 已按名称排好），过滤空值。旧数据没有 sources 字段，
    退化成只显示自己那一家；两者都取不到时返回 []。
    """
    raw = n.get("sources") or []
    if not raw:
        raw = [n.get("source")]
    out = []
    for s in raw:
        s = s.strip() if isinstance(s, str) else ""
        if s and s not in out:
            out.append(s)
    return out


def sources_label(n, max_sources=SOURCES_IN_LIST):
    """「来源：新浪财经、东方财富」；来源过多时「来源：A、B、C、D 等 6 家」。

    max_sources=0（SOURCES_FULL）表示不截断。没有可取来源时返回 ""。
    """
    srcs = news_sources(n)
    if not srcs:
        return ""
    if max_sources and len(srcs) > max_sources:
        return "来源：" + "、".join(srcs[:max_sources]) + f" 等 {len(srcs)} 家"
    return "来源：" + "、".join(srcs)


def verified_label(n, max_sources=SOURCES_IN_LIST):
    """列表徽章的整句文案：「已确认 · 来源：A、B、C、D 等 6 家」/「待核实」。

    单源条目保持原样 —— 来源就是它自己那一家，元信息里已经显示了。
    """
    if n.get("verified") != "confirmed":
        return "待核实"
    lab = sources_label(n, max_sources)
    return f"已确认 · {lab}" if lab else "已确认"


def verified_detail_label(n):
    """详情弹层的整句文案：来源**完整列出**、不截断（弹层里有的是空间）。"""
    if n.get("verified") != "confirmed":
        return "待核实（单源）"
    lab = sources_label(n, SOURCES_FULL)
    return f"已确认（多源） · {lab}" if lab else "已确认（多源）"


def confirmed_sources(n, max_sources=SOURCES_IN_LIST):
    """confirmed 条目裸的来源清单（「来源：A、B 等 6 家」），其余一律返回 ""。

    字段名带 confirmed 是故意的：前端拿到就能直接塞进元信息，不必再判一次
    verified，也不会把单源条目自己那一家当成"来源清单"渲染出来。
    """
    if n.get("verified") != "confirmed":
        return ""
    return sources_label(n, max_sources)


# ---------------------------------------------------------------------------
# M10 候选账本的批次（与 M5 日报 / M6 推送同一口径，2026-10-04 盘前通道）
#
#   am = 08:10 盘前那轮生成的**推荐**（「今日潜力个股（推荐）」，2026-10-05 起
#        对外就叫推荐：每条写明为什么推荐 —— 依据/传导机制/预期差），基准是**昨收**，
#        条目多带 entry_zone（关注区间）与 trigger（触发条件）；
#   pm = 收盘后那轮记录，基准是当日收盘价，属于事后复盘，不叫推荐。
#
# 盘前（推荐）组一律排在前。
# ---------------------------------------------------------------------------
SLOT_ORDER = {"am": 0, "pm": 1}
SLOT_GROUP_CN = {"am": "今日潜力个股（推荐）", "pm": "盘后（收盘复盘后记录）"}

# 两个基准并列（2026-10-05，与 M5 日报 / M6 推送同一口径）。
# 候选天然偏中小盘 + 事件驱动，只用沪深300 当基准会**系统性高估**这套判断的水平
# （小盘股整体跑赢时，"判断对"是假的）。故每档同时给相对沪深300 与相对中证1000
# 的超额，并在前端给一句解释。名字与备注在这里定义一次，前端不必自己硬编码
# —— 静态导出把整个 dict 序列化，这些字段跟着 picks.js 一起上线。
BENCH_CN = "沪深300"
BENCH2_CN = "中证1000"
PICK_BENCH_NOTE = (f"超额同时给{BENCH_CN} 与{BENCH2_CN}："
                   "候选偏中小盘，只用沪深300 会高估水平")


def pick_slot(row):
    """账本行的批次：只有显式 am 才算盘前，其余（旧数据没 slot / 写坏）一律按盘后。

    盘后是账本的历史默认值 —— 盘前通道是后加的，把缺失值当盘前会把整批老账本
    误标成「今日可执行」。
    """
    s = (row.get("slot") or "").strip().lower()
    return s if s in SLOT_ORDER else "pm"


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------
def query_news(bundle, q="", cat="", senti="", verified="", source="",
               board="", stock="", sort="time", limit=40, offset=0):
    """按条件筛选新闻。返回 (总数, 当前页条目)。

    q 支持空格分隔的多关键词，全部命中才算匹配（AND）。

    条目原样带上 sources（列表）与下面几个**展示用**字段，前端不必自己知道
    截断规则：
      - verified_label  / verified_detail    整句徽章文案（列表截断 / 详情完整）
      - confirmed_sources / confirmed_sources_full  裸「来源：…」清单（非
        confirmed 为空串，可直接判断要不要渲染）
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
        item = {"id": i, **n}
        # 前端只负责把这几句放进徽章/元信息：截断规则只在这里定义一份。
        # verified_label / verified_detail：整句（徽章文案直接替换）
        # confirmed_sources / confirmed_sources_full：裸清单（另起一个 span，
        #   与 M5 日报「徽章 + 来源清单」的版式一致；非 confirmed 为空串）
        # 注意 sources（原始列表）必须原样带出去 —— 静态导出走的就是本函数。
        item["verified_label"] = verified_label(n)
        item["verified_detail"] = verified_detail_label(n)
        item["confirmed_sources"] = confirmed_sources(n)
        item["confirmed_sources_full"] = confirmed_sources(n, SOURCES_FULL)
        items.append(item)

    if sort == "source":
        items.sort(key=lambda x: (x.get("source") or "", x.get("time") or ""), reverse=True)
    elif sort == "sentiment":
        rank = {"bullish": 0, "bearish": 1, "neutral": 2}
        items.sort(key=lambda x: (rank.get(x.get("sentiment"), 3), x.get("time") or ""),
                   reverse=False)
    else:
        items.sort(key=lambda x: x.get("time") or "", reverse=True)

    return len(items), items[offset:offset + limit]
