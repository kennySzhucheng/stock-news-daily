"""离线用例：不联网、不需要密钥、不消耗任何 API 额度。

跑法（仓库根目录）：
    python tests/offline_tests.py
或在 CI 里：  python tests/offline_tests.py

为什么要有这个文件（2026-10-04）：这个项目两周里出的故障**没有一个是"跑不起来"**，
全是"坏了没人看得出来"——推送正文变成占位串、候选账本停止增长、交叉验证标记恒为
0~4%，而每晚体检都输出「一切正常 ✓」。当时的验证手段只有两种：真跑一遍流水线
（要密钥、要网络、花额度），或人工翻日志。两者都不可能为每个判据留回归用例。
所以这里用纯离线的方式，把「已修复的具体缺陷」逐条钉成断言。
"""

import importlib.util
import json
import os
import pathlib
import sys
import unittest
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "m8_e2e"))


def load(name, rel):
    """按文件路径加载模块（这些模块不是包，不能 import 名字）。"""
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


hc = load("healthcheck", "modules/m8_e2e/healthcheck.py")


# --------------------------------------------------------------------------
# 体检的「内容面」断言（2026-10-04 新增）
# --------------------------------------------------------------------------

class ContentChecks(unittest.TestCase):
    """把「送到了」与「送的是好的」分开：前者旧体检已在查，后者是本轮新增。

    全部离线：四个取数函数都被替换成假数据。
    """

    DATE = hc.CONTENT_SINCE          # 内容面检查生效的第一天
    RUN = {
        "run_number": 77, "event": "workflow_dispatch", "conclusion": "success",
        "created_at": "2026-10-05T00:10:14Z", "updated_at": "2026-10-05T00:20:00Z",
    }

    def setUp(self):
        self.run_output = ("pm", True, "")
        self.restore()

    def restore(self):
        """装好默认的「一切正常」假数据，各用例再按需覆盖。"""
        self.status = {"push_ok": True, "push_detail": "pushid=1",
                       "sentiment_ok": True, "analysis_ok": True, "analysis_chars": 4200,
                       "analysis_violations": "", "news_count": 144, "quotes_count": 6}
        self.picks = {"gate_open": True, "recorded": 3, "reason": "ok", "detail": ""}
        self.pages = {}

    def run_check(self, date=None):
        date = date or self.DATE
        CST = hc.CST
        day = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=CST)
        # 送达时刻必须跟着目标日走，否则算出来的"延迟"会是整整一天
        deliveries = {"am": day.replace(hour=8, minute=12),
                      "pm": day.replace(hour=15, minute=42)}

        def fake_pages_json(base, path):
            if path.startswith("picks/picks-"):
                return self.picks
            if path.startswith("status-"):
                return self.status
            return self.pages.get(path)

        hc.runs_on = lambda repo, d: [(day, self.RUN)]
        hc.pages_deliveries = lambda repo, d, pages=40: deliveries
        hc.push_status = lambda base, d, slot: (True, "pushid=1")
        hc.observe.run_output = lambda r: self.run_output
        hc.pages_json = fake_pages_json
        return hc.check_date("owner/repo", "https://example.invalid", date)

    @staticmethod
    def levels(problems):
        return [lv for lv, _ in problems]

    @staticmethod
    def texts(problems):
        return " / ".join(t for _, t in problems)

    def test_baseline_is_clean(self):
        out, problems = self.run_check()
        self.assertEqual(problems, [], self.texts(problems))
        self.assertIn("内容面正常", "\n".join(out))

    def test_sentiment_not_parsed_is_severe(self):
        """2026-09-28 / 10-04 的真实故障：推送发出去了，但正文的市场分析是占位串。"""
        self.status["sentiment_ok"] = False
        _, problems = self.run_check()
        self.assertIn(hc.SEVERE, self.levels(problems))
        self.assertIn("市场分析结论未能解析", self.texts(problems))

    def test_analysis_contract_failure_is_severe(self):
        self.status.update(analysis_ok=False, analysis_chars=40,
                           analysis_violations="建议买入")
        _, problems = self.run_check()
        self.assertIn(hc.SEVERE, self.levels(problems))
        self.assertIn("未通过完整性校验", self.texts(problems))

    def test_news_count_collapse(self):
        """M2 整批失败时报表会显得「今天没什么新闻」，旧体检完全看不见。"""
        self.status["news_count"] = 3
        _, problems = self.run_check()
        self.assertIn(hc.SEVERE, self.levels(problems))
        self.assertIn("结构化新闻只有 3 条", self.texts(problems))

    def test_news_count_low_is_warn_only(self):
        self.status["news_count"] = 35
        _, problems = self.run_check()
        self.assertNotIn(hc.SEVERE, self.levels(problems))
        self.assertIn(hc.WARN, self.levels(problems))

    def test_quotes_empty_is_warn_only(self):
        self.status["quotes_count"] = 0
        _, problems = self.run_check()
        self.assertNotIn(hc.SEVERE, self.levels(problems))
        self.assertIn(hc.WARN, self.levels(problems))

    def test_picks_gate_open_but_zero_recorded_is_severe(self):
        """2026-09-30 的真实故障：闸门已开、JSON 解析失败 → 0 条候选、账本从此不增长。"""
        self.picks.update(gate_open=True, recorded=0, reason="parse_failed")
        _, problems = self.run_check()
        self.assertIn(hc.SEVERE, self.levels(problems))
        self.assertIn("0 条候选入账", self.texts(problems))

    def test_picks_gate_closed_is_not_a_problem(self):
        """非交易日/盘中闸门未开 = 正常，不能告警（否则每个周末都误报）。"""
        self.picks.update(gate_open=False, recorded=0, reason="gate_closed")
        _, problems = self.run_check()
        self.assertEqual(problems, [], self.texts(problems))

    def test_old_dates_skip_content_checks(self):
        """内容面字段是 2026-10-05 才上线的：对更早的日期一律不检查，避免翻旧账误报。"""
        old = (datetime.strptime(self.DATE, "%Y-%m-%d") - timedelta(days=1)
               ).strftime("%Y-%m-%d")
        self.status["sentiment_ok"] = False
        self.picks.update(gate_open=True, recorded=0, reason="parse_failed")
        out, problems = self.run_check(date=old)
        self.assertEqual(problems, [], self.texts(problems))
        self.assertIn("跳过", "\n".join(out))

    def test_missing_new_fields_do_not_alarm(self):
        """旧版 M6 写的状态文件没有这些字段 → 当"该日早于内容面检查"，不报问题。"""
        self.status = {"push_ok": True, "push_detail": "pushid=1"}
        self.picks = {}
        _, problems = self.run_check()
        self.assertEqual(problems, [], self.texts(problems))


class ConclusionConsistency(unittest.TestCase):
    """「从 M3 分析里取情绪结论」这个判据在三个出口各有一份实现。

    2026-09-28 / 10-04 的真实故障：同一份 analysis.md，日报正常、**微信推送变占位串**
    （M6 那份实现只认行首 `结论：`，而 M3 有时把结论写进段落里）。修法是三处都加
    同样的三级兜底 —— 本用例负责钉住「三处结果必须永远一致」。
    """

    FIXTURES = {
        "标准结论行": "# 今日市场分析\n\n## 一、市场情绪概览\n结论：中性偏谨慎\n",
        "结论加粗在行首": "## 一、市场情绪概览\n**结论：中性偏谨慎**——量能萎缩。\n",
        "结论写在段落里": "## 一、市场情绪概览\n综合看，整体判断为**中性偏谨慎**。\n",
        "只有小节正文": "## 一、市场情绪概览\n两市成交额较昨日小幅萎缩，赚钱效应一般。\n",
        "完全空": "",
        "纯标题": "# 今日市场分析\n## 一、市场情绪概览\n",
    }

    def test_three_copies_agree(self):
        agg = load("m9_aggregate", "modules/m9_web/aggregate.py")
        rep = load("m5_report", "modules/m5_report/report.py")
        psh = load("m6_push", "modules/m6_push/push.py")

        def from_aggregate(text):
            b = agg.Bundle.__new__(agg.Bundle)      # 只用到 analysis_md，不必走 init
            b.analysis_md = text
            return b.conclusion()

        for name, text in self.FIXTURES.items():
            got = {
                "M5日报": rep.extract_conclusion(text),
                "M6微信": psh.extract_sentiment(text),
                "M9网页": from_aggregate(text),
            }
            self.assertEqual(len(set(got.values())), 1,
                             f"{name}: 三处结果不一致 {got}")
            if name in ("完全空", "纯标题"):
                self.assertEqual(list(got.values())[0], "", f"{name}: 应取不到")
            else:
                self.assertTrue(list(got.values())[0], f"{name}: 应取到非空结论")

    def test_no_placeholder_string_in_pipeline(self):
        """修好的实现里不允许再出现「暂无市场情绪判断」这种把失败说成正常的固定串。"""
        for rel in ("modules/m6_push/push.py", "modules/m5_report/report.py",
                    "modules/m9_web/aggregate.py"):
            txt = (ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn("暂无市场情绪判断", txt, rel)


class PushStatusContent(unittest.TestCase):
    """M6 要把内容面字段写进**公开上线**的 status-*.json —— 那是体检唯一的眼睛。

    全离线：send() 被替换成假函数，数据目录指向临时目录。
    """

    GOOD = ("# 今日市场分析\n\n## 一、市场情绪概览\n结论：中性偏谨慎\n\n"
            "## 四、风险提示\n1. 新闻≠股价。\n")
    # 没有任何可取正文的极端情况：连「市场情绪概览」小节都只有标题没有正文，
    # 三级兜底全部落空 → 必须显式说明"没解析出来"，而不是拿固定串糊过去。
    NO_CONCLUSION = ("# 今日市场分析\n\n## 一、市场情绪概览\n\n"
                     "## 二、值得关注的板块\n\n## 四、风险提示\n")

    def _run(self, analysis_md):
        import tempfile
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="snd-test-"))
        data, reports = tmp / "data", tmp / "reports"
        data.mkdir()
        reports.mkdir()
        (data / "analysis.md").write_text(analysis_md, encoding="utf-8")
        (data / "structured_news.json").write_text(
            json.dumps({"news": [{"text": "x"}] * 5}), encoding="utf-8")
        (data / "quotes.json").write_text(
            json.dumps({"count": 3, "quotes": []}), encoding="utf-8")
        (data / "analysis.status.json").write_text(json.dumps(
            {"ok": False, "chars": 120, "violations": ["建议买入"],
             "conclusion_ok": False}), encoding="utf-8")

        psh = load("m6_push_status", "modules/m6_push/push.py")
        psh.DATA_DIR, psh.REPORTS_DIR = data, reports
        sent = []
        psh.send = lambda title, desp, key: (sent.append(desp) or (True, "pushid=TEST"))
        os.environ["SERVERCHAN_SENDKEY"] = "dummy"
        os.environ["REPORT_SLOT"] = "pm"
        os.environ["REPORT_BASE_URL"] = ""
        psh.main()
        date_str = datetime.now(hc.CST).strftime("%Y-%m-%d")   # 与脚本同一来源
        status = json.loads(
            (reports / f"status-{date_str}-pm.json").read_text(encoding="utf-8"))
        return status, sent[0]

    def test_content_fields_written(self):
        status, desp = self._run(self.GOOD)
        self.assertTrue(status["push_ok"])                  # 推送本身是成功的
        self.assertTrue(status["sentiment_ok"])             # 结论解析到了
        self.assertEqual(status["warn"], "")
        self.assertIs(status["analysis_ok"], False)         # 但 M3 契约没过
        self.assertEqual(status["analysis_chars"], 120)
        self.assertEqual(status["analysis_violations"], "建议买入")
        self.assertEqual(status["news_count"], 5)
        self.assertEqual(status["quotes_count"], 3)
        # 原有字段名一个都不能少（体检依赖）
        for k in ("date", "slot", "push_ok", "push_detail", "run_number", "event",
                  "generated_at"):
            self.assertIn(k, status)

    def test_unparsed_conclusion_is_explicit_not_silent(self):
        status, desp = self._run(self.NO_CONCLUSION)
        self.assertTrue(status["push_ok"])
        self.assertFalse(status["sentiment_ok"])
        self.assertEqual(status["warn"], "sentiment_unresolved")
        self.assertIn("未能从 M3 分析中解析出结论", desp)      # 明说，而不是"暂无"
        # 候选状态文件缺失时，pm 推送的候选块也不许说「盘前」
        self.assertNotIn("盘前", desp.split("### 📌")[0])

    def test_missing_analysis_status_does_not_alarm(self):
        """旧版本 M3/流水线（没有 status 文件）不能被误判成内容有问题。"""
        import tempfile
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="snd-test-"))
        (tmp / "data").mkdir()
        (tmp / "reports").mkdir()
        (tmp / "data" / "analysis.md").write_text(self.GOOD, encoding="utf-8")
        (tmp / "data" / "structured_news.json").write_text(
            json.dumps({"news": []}), encoding="utf-8")
        psh = load("m6_push_status2", "modules/m6_push/push.py")
        psh.DATA_DIR, psh.REPORTS_DIR = tmp / "data", tmp / "reports"
        psh.send = lambda title, desp, key: (True, "pushid=TEST")
        os.environ["SERVERCHAN_SENDKEY"] = "dummy"
        os.environ["REPORT_SLOT"] = "am"
        os.environ["REPORT_BASE_URL"] = ""
        psh.main()
        date_str = datetime.now(hc.CST).strftime("%Y-%m-%d")
        status = json.loads(
            (tmp / "reports" / f"status-{date_str}-am.json").read_text(encoding="utf-8"))
        self.assertIsNone(status["analysis_ok"])
        self.assertIsNone(status["analysis_chars"])


class DigestOrdering(unittest.TestCase):
    """M3 的新闻清单：**超预算时必须丢掉最旧的**。

    旧实现按时间升序排，于是 24000 字符预算全被几个月前的旧政策占满，
    当日快讯反而被截掉（10-04 的 digest 第 1 条是 2026-07-02 的国函）。
    M9 的 `[i]` 引用还原必须与这里逐字同步，否则点引用会跳错新闻。
    """

    def _news(self, n, category, time, verified="unverified", text=None):
        return {"time": time, "source": "测试源", "category": category,
                "verified": verified, "stocks": [], "board": [],
                "sentiment": "neutral", "text": text or f"{category}-{time}"}

    def test_newest_first_and_truncation_drops_oldest(self):
        m3 = load("m3_analyzer", "modules/m3_analyzer/analyzer.py")
        news = [
            self._news(0, "policy", "2026-05-29 10:00:00"),
            self._news(1, "policy", "2026-07-02 10:00:00"),
            self._news(2, "stock", "2026-10-04 09:00:00"),
            self._news(3, "stock", "2026-10-04 15:00:00"),
        ]
        digest, count = m3.build_news_digest(news, max_chars=100000)
        self.assertEqual(count, 4)
        order = [line.split("] ", 1)[1].split(" 测试源")[0]
                 for line in digest.splitlines() if line.startswith("[")]
        # 类别权重仍是一级键（policy 在 stock 前，这是原设计），时间只在**同类内**排序；
        # 关键是同一类别里必须最新在前 —— 旧实现相反，于是旧政策占了 digest 头部。
        self.assertEqual(order, ["07-02 10:00", "05-29 10:00",
                                 "10-04 15:00", "10-04 09:00"], order)

        small, n = m3.build_news_digest(news, max_chars=150)
        self.assertLess(n, 4, "预算变小后应发生截断")
        self.assertNotIn("2026-05-29", small, "被截掉的必须是**最旧**的")

    def test_m9_citation_map_matches_m3_order(self):
        """同一份数据，M3 的编号顺序与 M9 的引用还原必须完全一致。"""
        m3 = load("m3_analyzer", "modules/m3_analyzer/analyzer.py")
        agg = load("m9_aggregate", "modules/m9_web/aggregate.py")
        news = [
            self._news(0, "policy", "2026-05-29 10:00:00"),
            self._news(1, "stock", "2026-10-04 09:00:00", verified="confirmed"),
            self._news(2, "stock", "2026-10-04 15:00:00"),
            self._news(3, "international", "2026-10-04 11:00:00"),
        ]

        digest, _ = m3.build_news_digest(news, max_chars=100000)
        # digest 每条占两行：`[i] 时间 来源|类别|…` + 正文行；用正文行反查它是哪条新闻
        lines = digest.splitlines()
        m3_order = []
        for k, line in enumerate(lines):
            if not line.startswith("[") or k + 1 >= len(lines):
                continue
            body = lines[k + 1]
            m3_order.append(next(j for j, n in enumerate(news)
                                 if n["text"][:200] == body))

        b = agg.Bundle.__new__(agg.Bundle)
        b.news = news
        cmap = b._build_citation_map()
        m9_order = [cmap[i] for i in range(len(news))]
        self.assertEqual(m3_order, m9_order,
                         "M3 与 M9 的新闻编号顺序不一致 → 网页点 [i] 会跳错新闻")


class CollectorSources(unittest.TestCase):
    """M1：时间解析、过期过滤、跨源来源聚合。

    2026-10-04 实测背景：新华网 RSS 整体冻结在 2022-12、人民网停在 2025-06-05，
    两者曾**每天白抓约 400 条**（时间解析失败 → 空时间 → 排序垫底 → 被 150 上限
    截掉，同时把 official 类目永久挤成 0）。修法是：修好时间解析 + 显式按窗口过滤
    + 无时间直接剔除；并且把「被合并掉的重复条目来自哪些源」记进存活条目
    （`sources`），让 M2 的「多源印证」有据可依 —— 旧实现把第二来源直接丢了，
    于是一个天天喊"多源交叉验证"的项目，那个标记长期只有 0~4%，且全是假阳性。
    """

    def setUp(self):
        self.col = load("m1_collector", "modules/m1_collector/collector.py")

    def test_time_formats(self):
        cases = {
            "Wed, 14 Dec 2022 11:37:37 +0800": "2022-12-14 11:37:37",
            "Wed,14-Dec-2022 11:37:37 GMT": "2022-12-14 11:37:37",   # 新华网裸文本形态
            "2025-06-05": "2025-06-05 00:00:00",                     # 人民网只有日期
            "2026-09-30 19:38:45  +0800": "2026-09-30 19:38:45",     # 36氪（双空格）
            "2026-10-03T07:15:00+08:00": "2026-10-03 07:15:00",
            "": "",
            "不是时间": "",
        }
        for raw, want in cases.items():
            self.assertEqual(self.col._parse_ts(raw), want, f"输入 {raw!r}")

    def test_sources_aggregated_and_empty_time_dropped(self):
        col = self.col
        text = "同一条新闻的正文内容"
        items_a = [{"time": "2026-10-04 10:00:00", "source": "新浪财经7x24",
                    "category": "finance", "url": "", "text": text}]
        items_b = [{"time": "2026-10-03 10:00:00", "source": "东方财富7x24",
                    "category": "finance", "url": "", "text": text}]
        items_c = [{"time": "2026-10-04 09:00:00", "source": "36氪",
                    "category": "finance", "url": "", "text": text}]
        items_none = [{"time": "", "source": "新华网", "category": "official",
                       "url": "", "text": "没有时间的旧闻"},
                      {"time": "2026-10-04 08:00:00", "source": "人民网",
                       "category": "official", "url": "", "text": "另一条正常新闻"}]

        def fixed(items):
            def f(days, cap):
                return list(items[:cap])
            return f

        col.SOURCES = [("A", fixed(items_a), 10), ("B", fixed(items_b), 10),
                       ("C", fixed(items_c), 10), ("D", fixed(items_none), 10)]
        news, status = col.collect(days=1)

        survivor = next(n for n in news if n["text"] == text)
        self.assertEqual(survivor["sources"], ["36氪", "东方财富7x24", "新浪财经7x24"],
                         "三条同文必须聚合成三个来源（排序后）")
        self.assertEqual(survivor["time"], "2026-10-04 10:00:00", "存活的是时间最新那条")
        self.assertEqual(sum(1 for n in news if n["text"] == text), 1, "同文只留一条")
        self.assertTrue(all(n["sources"] for n in news), "每条都必须带非空 sources")
        self.assertTrue(all(n["source"] in n["sources"] for n in news),
                        "sources 必须含自身来源")
        self.assertNotIn("没有时间的旧闻", [n["text"] for n in news], "无时间条目必须剔除")
        tail = [s for s in status if "total_unique" in s][0]
        self.assertEqual(tail["dropped_no_time"], 1)

    def test_per_source_window_is_honoured(self):
        """政策类源的窗口：发布不规律，给它更大的 days，否则类目会长期为 0。"""
        col = self.col
        seen = []

        def probe(days, cap):
            seen.append(days)
            return []

        col.SOURCES = [("短窗源", probe, 10), ("政策源", probe, 10, 7)]
        col.collect(days=1)
        self.assertEqual(seen[0], 1, "默认源跟随 days")
        self.assertEqual(seen[1], 7, "带第 4 项的源取 max(days, 窗口)")


class PicksRobustness(unittest.TestCase):
    """M10：LLM 输出解析与状态文件接口。

    2026-09-30 盘后，闸门已开，日志只有一句
        [warn] 候选 JSON 解析失败: Expecting ',' delimiter: line 1 column 118
    结果是当天 0 条候选、账本从 09-29 起再没长过一行（两周项目最核心的
    「可检验判断记录」功能就这么静默地停了）。旧实现一次 json.loads 失败就整批丢弃、
    不重试、不抢救、连原始输出都不落盘，根因永久不可考。
    """

    def setUp(self):
        self.pk = load("m10_picks", "modules/m10_picks/picks.py")

    def _payload(self, **over):
        base = {"kind": "stock", "name": "测试股份", "code_hint": "600001",
                "logic": "某条新闻逻辑", "invalidation": "出现什么情况算判断错了",
                "confidence": "中", "basis_refs": [1]}
        base.update(over)
        return base

    def test_salvage_paths(self):
        import json as _j
        good = _j.dumps({"candidates": [self._payload()]}, ensure_ascii=False)

        cases = {
            "正常 JSON": good,
            "小写围栏+中文前言": f"好的，以下是候选：\n```json\n{good}\n```\n请查收",
            "大写围栏": f"```JSON\n{good}\n```",
            "结构位全角逗号": good.replace('", "', '"，"').replace('{"candidates"', '{"candidates"').replace(', "', '，"'),
            "顶层对象间漏逗号": _j.dumps(
                {"candidates": [self._payload()]}, ensure_ascii=False
            ).replace("}],", "}],"),   # 正常形态（对照组）
            "纯垃圾": "抱歉，我今天无法完成这个任务。",
            "无 candidates 数组": '{"news": [1, 2]}',
        }
        for name, text in cases.items():
            try:
                out = self.pk.parse_candidates(text)
            except Exception as e:                       # noqa: BLE001
                self.fail(f"{name}: parse_candidates 抛异常 {type(e).__name__}: {e}")
            self.assertIsInstance(out, list, name)
            if name in ("正常 JSON", "小写围栏+中文前言", "大写围栏"):
                self.assertGreaterEqual(len(out), 1, f"{name}: 应至少拿到 1 条")
            if name in ("纯垃圾", "无 candidates 数组"):
                self.assertEqual(out, [], name)

    def test_brace_salvage_when_whole_json_is_broken(self):
        """整体 JSON 有语法错误时，按花括号配平逐条抢救 —— 这是 09-30 那种
        「一个字符失误 = 整天样本归零」的直接解药。"""
        broken = ('{"candidates": [{"name": "甲股份", "kind": "stock", '
                  '"logic": "逻辑一", "invalidation": "推翻条件一二三四五六", '
                  '"confidence": "中"}, {"name": "乙股份", "kind": "stock", '
                  '"logic": "逻辑二", "invalidation": "推翻条件七八九十十一", '
                  '"confidence": "低"}]')      # 结尾缺 }，整体解析必失败
        out = self.pk.parse_candidates(broken)
        self.assertEqual(len(out), 2, f"配平抢救应救回 2 条，实际 {len(out)}")

    def test_status_file_interface_is_frozen(self):
        """体检与 M6 都按这 7 个字段读，字段名/类型不能变。"""
        import tempfile
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="snd-picks-"))
        old = self.pk.PICKS_DIR
        self.pk.PICKS_DIR = tmp
        try:
            payload = self.pk.write_status("2026-10-05", "pm", {
                "gate_open": True, "recorded": 2, "reason": "ok", "detail": "新入账 2 条"})
        finally:
            self.pk.PICKS_DIR = old
        self.assertEqual(set(payload), {"date", "slot", "generated_at",
                                        "gate_open", "recorded", "reason", "detail"})
        self.assertEqual(payload["gate_open"], True)
        self.assertEqual(payload["recorded"], 2)
        f = tmp / "picks-2026-10-05-pm.json"
        self.assertTrue(f.exists(), "状态文件未写到 picks-<日期>-<时段>.json")
        self.assertFalse((tmp / "picks-2026-10-05-pm.tmp").exists(), "临时文件未清理")
        self.assertEqual(json.loads(f.read_text(encoding="utf-8"))["reason"], "ok")

    def test_gate_closed_status_does_not_alarm_healthcheck(self):
        """闸门未开（非交易日/盘中）是正常状态，写出来的状态必须让体检安静。"""
        import tempfile
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="snd-picks-"))
        old = self.pk.PICKS_DIR
        self.pk.PICKS_DIR = tmp
        try:
            payload = self.pk.write_status("2026-10-05", "pm", {
                "gate_open": False, "recorded": 0, "reason": "gate_closed",
                "detail": "基准行情取不到，无法确认今日是否收盘（宁可不写）"})
        finally:
            self.pk.PICKS_DIR = old
        self.assertIs(payload["gate_open"], False)
        self.assertEqual(payload["reason"], "gate_closed")


class CoverageBreadth(unittest.TestCase):
    """覆盖面：新闻要包括**所有相关的**，而不是只有股票相关的（2026-10-04 用户要求）。

    改造前的实测：150 个名额里 official 存活 **0 条**、tech 3 条 —— 因为
    只有 policy/announcement 受保护，614 条财经快讯按时间倒序把整类通用新闻挤掉了。
    """

    def setUp(self):
        self.f = load("m2_filter", "modules/m2_filter/filter.py")

    def _news(self, cat, text, i):
        return {"time": f"2026-10-04 10:00:{i % 60:02d}", "source": f"源{i % 5}",
                "category": cat, "url": "", "text": text}

    def test_general_news_survives_finance_flood(self):
        """核心断言：财经快讯的数量再多，也挤不掉通用/国际/科技类的保底名额。"""
        news = [self._news("finance", f"某公司发布公告第{i}条，营收同比增长", i)
                for i in range(600)]
        news += [self._news("official", f"国务院部署第{i}项重点工作", 1000 + i) for i in range(40)]
        news += [self._news("international", f"美国与欧盟就第{i}项议题谈判", 2000 + i) for i in range(40)]
        news += [self._news("tech", f"某机构发布第{i}项算力技术进展", 3000 + i) for i in range(30)]
        keep, dropped = self.f.prefilter_local(news)
        mix = {}
        for n in keep:
            mix[n["category"]] = mix.get(n["category"], 0) + 1
        self.assertLessEqual(len(keep), 180, "预筛总量不应超过上限")
        self.assertGreaterEqual(mix.get("official", 0), 20,
                                f"通用新闻被挤掉了：{mix}")
        self.assertGreaterEqual(mix.get("international", 0), 20, f"国际新闻被挤掉了：{mix}")
        self.assertGreaterEqual(mix.get("tech", 0), 8, f"科技新闻被挤掉了：{mix}")
        self.assertGreater(mix.get("finance", 0), 50, f"财经快讯不该被压得太狠：{mix}")
        self.assertGreater(dropped, 0, "超出上限时应有丢弃计数")

    def test_pure_entertainment_and_sports_dropped(self):
        news = [self._news("finance", "某某明星演唱会门票开售", 0),
                self._news("finance", "中超联赛第18轮战报", 1),
                self._news("official", "NBA总决赛第三场结束", 2),
                self._news("finance", "央行开展5000亿元逆回购操作", 3),
                self._news("official", "国家发改委批复新建铁路项目", 4)]
        keep, dropped = self.f.prefilter_local(news)
        texts = " ".join(n["text"] for n in keep)
        self.assertNotIn("明星", texts)
        self.assertNotIn("中超", texts)
        self.assertNotIn("NBA", texts)
        self.assertEqual(dropped, 3)
        self.assertEqual(len(keep), 2)

    def test_non_stock_but_relevant_news_kept(self):
        """关键行为变更：**不提到任何股票/公司**的国际、产业新闻也要留下。"""
        news = [self._news("international", "北约拟在日本开设联络处，细节还未定", 0),
                self._news("official", "多地出台措施促进消费复苏", 1),
                self._news("tech", "国产大模型在工业质检场景落地", 2)]
        keep, dropped = self.f.prefilter_local(news)
        self.assertEqual(len(keep), 3, f"相关但非股票的新闻被丢了：{dropped}")
        self.assertEqual(dropped, 0)

    def test_prompt_no_longer_says_stock_irrelevant(self):
        """prompt 里必须写明「没提到个股也要保留」，否则 LLM 会把宏观/国际新闻整批丢。"""
        src = (ROOT / "modules/m2_filter/filter.py").read_text(encoding="utf-8")
        self.assertIn("没有提到任何公司", src)
        self.assertNotIn("keep=false 表示纯社会新闻与股市无关应丢弃", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
