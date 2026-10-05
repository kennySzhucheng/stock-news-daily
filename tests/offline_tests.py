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

import hashlib
import importlib.util
import json
import os
import pathlib
import re
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


class TradingCalendarAndOneSlotPerRun(unittest.TestCase):
    """T+1/T+3/T+5 的到期日按**交易日**算，且**一轮只补最早的一档**（2026-10-04 用户决定）。

    改前的两个毛病（都已核实）：
      ① `due = 记录日 + k 个自然日` → 周末与节假日被当成"过了 1 天"；
      ② 一轮把所有到期档位一次性补齐 → 跨长假时 T+3 与 T+5 落在**同一天、同一个价格**，
         实际只有约 2 个交易日跨度，却在均值里各算一个样本。
    改后：due 走交易日历（东财沪深300日K，落 `reports/picks/trading_days.json` 缓存，
    取不到退化为「周一~周五 − 2026 休市表」并打 warn），每轮只补一档，
    新增 `span`（基准日→补录日的实际交易日数）供展示层说明"这条 T+1 其实是第 3 个交易日"。
    """

    def setUp(self):
        import tempfile
        self.pk = load("m10_picks", "modules/m10_picks/picks.py")
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="snd-cal-"))
        self._old_cache = self.pk.TRADING_DAYS_CACHE_PATH
        self.pk.TRADING_DAYS_CACHE_PATH = self.tmp / "trading_days.json"
        # 注入一段"真实"交易日历：跳过周末与 2026 中秋(09-25)
        self.pk._inject_trading_days([
            "2026-09-11", "2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17",
            "2026-09-18", "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24",
            "2026-09-28", "2026-09-29", "2026-09-30",
        ])

    def tearDown(self):
        self.pk._cal_reset()
        self.pk.TRADING_DAYS_CACHE_PATH = self._old_cache

    def _d(self, s):
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=hc.CST)

    def test_due_uses_trading_days(self):
        pk = self.pk
        # 周五记录 → T+1 是下周一（不是周六）
        self.assertEqual(pk.due(1, self._d("2026-09-11")).strftime("%Y-%m-%d"), "2026-09-14")
        self.assertEqual(pk.due(3, self._d("2026-09-11")).strftime("%Y-%m-%d"), "2026-09-16")
        self.assertEqual(pk.due(5, self._d("2026-09-11")).strftime("%Y-%m-%d"), "2026-09-18")
        # 跨中秋：09-18 的下一个交易日是 09-21，第 3 个是 09-23，第 5 个要跳过 09-25 到 09-28
        self.assertEqual(pk.due(5, self._d("2026-09-18")).strftime("%Y-%m-%d"), "2026-09-28")
        # 节前最后一天记录 → T+3 跨国庆，绝不能是自然日口径的 10-02
        self.assertNotEqual(pk.due(3, self._d("2026-09-30")).strftime("%Y-%m-%d"), "2026-10-02")

    def _row(self, base="2026-09-11", name="测试股份", secid="1.600001"):
        return {"id": f"{base}-pm-s-{name}", "date": base, "slot": "pm", "kind": "stock",
                "name": name, "code": "600001", "secid": secid, "market": "沪A",
                "base_price": 10.0, "base_prev_close": 9.9, "bench_level": 4000.0,
                "reviews": {str(k): None for k in self.pk.REVIEW_DAYS}}

    def test_one_slot_per_run(self):
        pk = self.pk
        row = self._row()
        row["reviews"] = {str(k): None for k in pk.REVIEW_DAYS}
        rows = [row]
        # 打桩：行情固定，避免联网。bench_now 是**基准指数点位（float）**
        pk.fetch_any = lambda secid: ({"price": 11.0, "prev_close": 10.0}, "stub")
        pk.fetch_bench = lambda: (None, {"level": 4000.0, "qdate": "20260914", "qhm": "1500"})

        # 第 1 轮（T+1 到期日 09-14）：只补 T+1
        pk.score_pending(rows, self._d("2026-09-14"), 4000.0)
        filled = [k for k in pk.REVIEW_DAYS if row["reviews"][str(k)]]
        self.assertEqual(filled, [1], f"第 1 轮应只补 T+1，实际 {filled}")
        self.assertEqual(row["reviews"]["1"]["span"], 1)

        # 第 2 轮（T+3 到期日 09-16）：只补 T+3
        pk.score_pending(rows, self._d("2026-09-16"), 4010.0)
        filled = [k for k in pk.REVIEW_DAYS if row["reviews"][str(k)]]
        self.assertEqual(filled, [1, 3], f"第 2 轮应只补 T+3，实际 {filled}")
        self.assertEqual(row["reviews"]["3"]["span"], 3)

        # 第 3 轮（T+5 到期日 09-18）：补齐
        pk.score_pending(rows, self._d("2026-09-18"), 4020.0)
        filled = [k for k in pk.REVIEW_DAYS if row["reviews"][str(k)]]
        self.assertEqual(filled, [1, 3, 5], f"第 3 轮应补齐，实际 {filled}")
        # 三档 done 必须落在三个不同日期（旧实现会三条同一天）
        dones = {row["reviews"][str(k)]["done"] for k in pk.REVIEW_DAYS}
        self.assertEqual(len(dones), 3, f"三档补录日期应互不相同：{dones}")
        # span 单调递增，且恰好等于 k（说明真的按交易日补上）
        spans = [row["reviews"][str(k)]["span"] for k in pk.REVIEW_DAYS]
        self.assertEqual(spans, [1, 3, 5], spans)
        # 账本 schema：due 仍是日期字符串
        self.assertRegex(row["reviews"]["1"]["due"], r"^\d{4}-\d{2}-\d{2}$")

    def test_all_due_at_once_still_fills_one_per_run(self):
        """三档都已到期（例如中间几天没跑）时，仍然一轮只补一档。"""
        pk = self.pk
        row = self._row()
        pk.fetch_any = lambda secid: ({"price": 11.0, "prev_close": 10.0}, "stub")
        rows = [row]
        for i in range(3):
            before = sum(1 for k in pk.REVIEW_DAYS if row["reviews"][str(k)])
            pk.score_pending(rows, self._d("2026-09-18"), 4000.0 + i * 10)
            after = sum(1 for k in pk.REVIEW_DAYS if row["reviews"][str(k)])
            self.assertEqual(after - before, 1, "一轮只应新增一档")
        self.assertTrue(all(row["reviews"][str(k)] for k in pk.REVIEW_DAYS))


class ConfirmedSourceList(unittest.TestCase):
    """`已确认` 要附**来源清单**，不再只给一个二值标记（2026-10-04 用户决定）。

    背景：这个标记此前长期只有 0~4% 且全是假阳性（详见 CHANGELOG）。修好之后
    它的含义是"至少两家独立出版方刊发了同一事件"，所以要把是哪几家显示出来，
    让读者自己判断印证强度 —— 一个孤零零的「已确认」没法体现这一点。
    """

    def setUp(self):
        self.agg = load("m9_aggregate", "modules/m9_web/aggregate.py")
        self.rep = load("m5_report", "modules/m5_report/report.py")
        self.psh = load("m6_push", "modules/m6_push/push.py")

    def _news(self, sources, verified):
        n = {"time": "2026-10-04 10:00:00", "source": sources[0] if sources else "未知源",
             "category": "stock", "url": "", "text": "某条新闻正文内容足够长用于渲染",
             "board": [], "stocks": [], "sentiment": "neutral", "keep": True,
             "verified": verified}
        if sources is not None:
            n["sources"] = sources
        return n

    def test_m5_badge_shows_sources(self):
        # render_news 收的是整个 structured 字典（内部 .get("news")），不是列表
        html = self.rep.render_news({"news": [self._news(["新浪财经", "东方财富"], "confirmed")]})
        self.assertIn("来源：新浪财经、东方财富", html, html[:400])

    def test_m5_unverified_has_no_source_list(self):
        html = self.rep.render_news({"news": [self._news(["36氪"], "unverified")]})
        self.assertNotIn("来源：", html)

    def test_m5_truncates_long_source_lists(self):
        html = self.rep.render_news({"news": [
            self._news(["A媒体", "B媒体", "C媒体", "D媒体", "E媒体", "F媒体"], "confirmed")]})
        self.assertIn("等 6 家", html)
        self.assertNotIn("E媒体", html, "超过 4 家时不应全部展开")

    def test_m6_push_note(self):
        note = self.psh.push_verified_note(self._news(["新浪财经", "东方财富"], "confirmed"))
        self.assertIn("2 源", note)
        self.assertIn("新浪财经", note)
        self.assertEqual(self.psh.push_verified_note(self._news(["36氪"], "unverified")), "待核实")

    def test_m9_labels(self):
        agg = self.agg
        n = self._news(["新浪财经", "东方财富"], "confirmed")
        self.assertIn("来源：新浪财经、东方财富", agg.sources_label(n, 4))
        self.assertEqual(agg.confirmed_sources(self._news(["36氪"], "unverified"), 4), "")

    def test_missing_sources_field_does_not_crash(self):
        """线上历史数据（M1 加 sources 之前）没有这个字段，不能因此报错。"""
        n = self._news(None, "confirmed")
        n.pop("sources", None)
        self.assertIn("已确认", self.rep.render_news({"news": [n]}))
        self.assertIn("1 源", self.psh.push_verified_note(n))


class M2TimeBudget(unittest.TestCase):
    """M2 的墙钟预算：GLM 变慢时必须**优雅降级**，而不是让整条流水线超时失败。

    2026-10-04 实测：M2 的耗时完全由 GLM 服务端速度决定 —— 同一份代码，
    批 20 条时两次运行是 295s / 316s，批 30 条那次是 **888s**，而 job 的
    `timeout-minutes` 当时是 30 分钟。硬超时的后果是**当天没有日报**，
    比"部分新闻没结构化"糟糕得多；所以超预算时改为跳过剩余批次、按原样保留并计数，
    再由体检把这件事报出来。
    """

    def setUp(self):
        self.f = load("m2_filter", "modules/m2_filter/filter.py")

    def _raw(self, n):
        return {"news": [{"time": "2026-10-04 10:00:%02d" % (i % 60), "source": "测试源",
                          "category": "finance", "url": "",
                          "text": f"第{i}条财经新闻：某公司披露经营数据，营收同比增长"}
                         for i in range(n)]}

    def test_budget_exhausted_skips_llm_but_still_produces(self):
        import tempfile
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="snd-m2-"))
        (tmp / "raw_news.json").write_text(json.dumps(self._raw(60), ensure_ascii=False),
                                          encoding="utf-8")
        old_data, old_budget = self.f.DATA_DIR, self.f.M2_BUDGET_SEC
        self.f.DATA_DIR = tmp
        self.f.M2_BUDGET_SEC = -1         # 保证第一批之前就超预算
        calls = []
        self.f.batch_filter = lambda batch: calls.append(batch) or []
        try:
            self.f.main()
        finally:
            self.f.DATA_DIR, self.f.M2_BUDGET_SEC = old_data, old_budget

        d = json.loads((tmp / "structured_news.json").read_text(encoding="utf-8"))
        self.assertEqual(calls, [], "超预算后不应再调用 GLM")
        self.assertEqual(d["llm_batches"], 0)
        # 60 条 / 20 条一批 = 3 批全部因预算跳过
        self.assertEqual(d["llm_failed_batches"], 3)
        self.assertEqual(d.get("llm_skipped_by_budget"), 3)
        self.assertEqual(len(d["news"]), 60, "新闻一条都不能丢（按原样保留）")
        self.assertTrue(all(n["keep"] for n in d["news"]))

    def test_budget_counts_as_failed_batch_ratio(self):
        """预算跳过的批次要算进「未结构化比例」，否则体检会以为一切正常。"""
        src = (ROOT / "modules/m2_filter/filter.py").read_text(encoding="utf-8")
        self.assertIn("llm_skipped_by_budget", src)
        self.assertIn("total_batches = llm_batches + budget_skipped", src)


class QuotesMarketSplit(unittest.TestCase):
    """行情表按市场拆分：沪深 A 股放主表，美股/港股另起一栏并标注。

    2026-10-04 实测：那天 6 只行情里 **5 只是美股**（AAPL/TSLA/GOOG/ACN/PONY）。
    这份日报是给做 A 股的人看的，混在一张表里容易被读成"今天只有这些票"，
    也会把真正能在 A 股账户交易的线索淹掉。
    """

    def setUp(self):
        self.rep = load("m5_report", "modules/m5_report/report.py")

    def _q(self, name, code, market, pct):
        return {"name": name, "code": code, "market": market,
                "price": 10.0, "change_pct": pct}

    def test_market_predicate(self):
        f = self.rep._is_a_share_market
        for m in ("沪A", "深A", "科创板", "创业板", "北交所"):
            self.assertTrue(f(m), m)
        for m in ("美股", "港股", "沪B", "深B", "", None):
            self.assertFalse(f(m), m)

    def test_a_shares_first_others_labeled(self):
        html = self.rep.render_quotes({"quotes": [
            self._q("中国银行", "601988", "沪A", 1.2),
            self._q("苹果", "AAPL", "美股", 1.02),
            self._q("埃森哲", "ACN", "美股", -6.31),
        ]})
        plain = re.sub(r"<[^>]+>", " ", html)
        self.assertLess(plain.find("中国银行"), plain.find("苹果"), "A 股必须在主表（前）")
        self.assertIn("其它市场", plain)
        self.assertIn("不能在 A 股账户交易", plain)
        self.assertLess(plain.find("其它市场"), plain.find("苹果"))
        # 其它栏内按涨跌幅绝对值倒序（埃森哲 -6.31% 在苹果 +1.02% 之前）
        self.assertLess(plain.find("埃森哲"), plain.find("苹果"))

    def test_all_us_shows_hint(self):
        html = self.rep.render_quotes({"quotes": [self._q("苹果", "AAPL", "美股", 1.0)]})
        self.assertIn("没有沪深 A 股", re.sub(r"<[^>]+>", "", html))

    def test_all_a_share_has_no_other_section(self):
        html = self.rep.render_quotes({"quotes": [self._q("中国银行", "601988", "沪A", 1.0)]})
        self.assertNotIn("其它市场", re.sub(r"<[^>]+>", "", html))

    def test_m4_writes_market_breakdown(self):
        """M4 输出新增 by_market / a_share_count（纯附加字段，M5/M9 据此分组）。"""
        src = (ROOT / "modules/m4_quotes/quotes.py").read_text(encoding="utf-8")
        self.assertIn('"by_market": by_market', src)
        self.assertIn('"a_share_count": a_share', src)


class PremarketChannel(unittest.TestCase):
    """盘前通道（2026-10-04 用户决定 A 方案）。

    用户要的是"盘前告诉我今天可能看哪几只"。设计：
      · 08:10 生成「今日可执行观察清单」，每条给**关注区间 entry_zone** 与
        **触发条件 trigger**（加上原有的推翻条件）；
      · 基准价用**昨日收盘价**（盘前只有这个价是结算过的），`base_date` = 上一交易日；
      · 记入**同一个账本**（`slot=am`），因此盘前的判断**同样会被 T+1/3/5 公开回填**；
      · T+1 观察日 = 记录日**当天**（基准是昨收）→ 当天 15:40 那次运行就补上。
    这里钉住最容易错的三件事：记账字段齐不齐、休市/盘中快照必须拒绝、复盘基准日。
    """

    def setUp(self):
        self.pk = load("m10_picks", "modules/m10_picks/picks.py")

    def test_calendar_kind_marks_unverified_year(self):
        """未核实年份必须能被机器读出来（否则盘前清单会静默停摆）。"""
        self.assertEqual(self.pk.calendar_kind("2026-10-09"), "approx")  # 未来 → 近似但已核实
        self.assertEqual(self.pk.calendar_kind("2027-03-01"), "approx-unverified-year")
        src = (ROOT / "modules/m10_picks/picks.py").read_text(encoding="utf-8")
        self.assertIn('state["calendar"] = calendar_kind(today)', src)
        hc_src = (ROOT / "modules/m8_e2e/healthcheck.py").read_text(encoding="utf-8")
        self.assertIn("approx-unverified-year", hc_src, "体检必须能发现日历失效")

    def test_status_file_has_calendar_only_when_set(self):
        """`calendar` 是追加的第 8 个字段：pm 不写、am 写；原 7 字段契约不变。"""
        import tempfile
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="snd-am-"))
        old = self.pk.PICKS_DIR
        self.pk.PICKS_DIR = tmp
        try:
            pm = self.pk.write_status("2026-10-09", "pm", {
                "gate_open": True, "recorded": 1, "reason": "ok", "detail": ""})
            am = self.pk.write_status("2026-10-09", "am", {
                "gate_open": True, "recorded": 1, "reason": "ok", "detail": "",
                "calendar": "approx"})
        finally:
            self.pk.PICKS_DIR = old
        base = {"date", "slot", "generated_at", "gate_open", "recorded", "reason", "detail"}
        self.assertTrue(base.issubset(set(pm)), "pm 的 7 字段不能少")
        self.assertNotIn("calendar", pm, "pm 不必写 calendar")
        self.assertEqual(am["calendar"], "approx")

    def test_is_trading_day_fail_closed_on_unverified_year(self):
        """判不出来就按"不是交易日"处理（fail-closed）：宁可不记，也不能在休市日
        产出一份"今天可以买这些"的清单。"""
        self.assertFalse(self.pk.is_trading_day("2027-03-01"),
                         "未核实年份必须 fail-closed")

    def test_premarket_reason_enum_is_covered_by_push_wording(self):
        """盘前新增的两个 reason 必须在推送文案里有对应说法，不能落回"盘前不记录"。"""
        src = (ROOT / "modules/m6_push/push.py").read_text(encoding="utf-8")
        self.assertIn("non_trading_day", src)
        self.assertIn("not_premarket", src)
        self.assertNotIn('return "盘前不记录新候选', src,
                         "盘前现在会记账，这句话会掩盖真实故障")

    def test_ledger_row_schema_for_premarket(self):
        """am 行必须带 base_date / entry_zone / trigger；复盘要以 base_date 为基准日。"""
        src = (ROOT / "modules/m10_picks/picks.py").read_text(encoding="utf-8")
        for key in ("base_date", "entry_zone", "trigger"):
            self.assertIn(key, src, f"账本 schema 缺少 {key}")
        # 复盘基准日：am 行用 base_date（昨收所在交易日），pm 行回落到 date
        self.assertIn('row.get("base_date") or row["date"]', src,
                      "score_pending 必须用 base_date 作基准日，否则 am 行的 T+1 会差一天")


class RecommendationSection(unittest.TestCase):
    """「今日潜力个股（推荐）」版块（2026-10-05 用户要求）。

    用户原话："我希望有一个板块是推荐有潜力的个股，需要明确写出，以及为什么推荐"。
    落地方式：盘前那批（slot=am）对外就叫**推荐** —— 分组标题「今日潜力个股（推荐）」、
    字段标签「为什么推荐」（pm 批仍叫「逻辑」，它是事后记录，叫推荐不准确）；
    并强制 `logic`（推荐理由）写清【依据 → 传导机制 → 预期差】三件事，
    去空白后 < 20 字直接丢弃。**不是买入指令**这句话在三个出口都必须出现。
    """

    def setUp(self):
        self.rep = load("m5_report", "modules/m5_report/report.py")
        self.psh = load("m6_push", "modules/m6_push/push.py")

    def _row(self, slot, name, date="2026-10-09", **kw):
        r = {"id": f"{date}-{slot}-s-{name}", "date": date, "slot": slot, "kind": "stock",
             "name": name, "code": "000001", "secid": "0.000001", "market": "深A",
             "base_price": 12.34, "base_prev_close": 12.10, "bench_level": 4300.0,
             "board": "", "logic": "①依据 [1] 公告中标 ②订单未来两季确认收入 ③份额逆势提升未被反映",
             "invalidation": "若公司公告订单延期或取消", "confidence": "中",
             "basis_refs": [1], "reviews": {"1": None, "3": None, "5": None}}
        if slot == "am":
            r.update({"base_date": "2026-10-08", "entry_zone": "12.0~12.5",
                      "trigger": "开盘半小时站稳12.5且成交额较昨日同期放大"})
        r.update(kw)
        return r

    def _plain(self, html):
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))

    def test_m5_recommendation_group_and_labels(self):
        p = self._plain(self.rep.render_picks(
            [self._row("am", "甲股份"), self._row("pm", "乙股份")], "2026-10-09"))
        self.assertIn("今日潜力个股（推荐）", p)
        self.assertIn("盘后（收盘复盘后记录）", p)
        self.assertLess(p.find("今日潜力个股（推荐）"), p.find("盘后（收盘复盘后记录）"))
        self.assertIn("为什么推荐", p)
        self.assertIn("逻辑", p)
        self.assertIn("不是买入指令", p)
        for k in ("关注区间", "触发条件", "推翻条件"):
            self.assertIn(k, p)
        self.assertNotIn("建议买入", p)
        self.assertNotIn("满仓", p)

    def test_m5_pm_only_has_no_recommendation_title(self):
        p = self._plain(self.rep.render_picks([self._row("pm", "乙股份")], "2026-10-09"))
        self.assertNotIn("今日潜力个股（推荐）", p)

    def test_m5_empty_state_points_at_last_batch(self):
        """今天没有候选时，要如实说明并把最近一批的日期与天数报出来。"""
        p = self._plain(self.rep.render_picks([self._row("am", "丙股份", "2026-10-06")],
                                             "2026-10-09"))
        self.assertIn("今天没有产出推荐", p)
        self.assertIn("最近一批推荐是 2026-10-06", p)
        self.assertIn("3 天", p)

    def test_m10_rejects_short_recommendation_reason(self):
        """盘前「推荐理由」<20 字 = 没有推荐理由 → 丢弃该条（am 专用硬闸）。"""
        pk = load("m10_picks", "modules/m10_picks/picks.py")
        self.assertEqual(pk.AM_LOGIC_MIN_CHARS, 20)
        base = {"kind": "stock", "entry_zone": "12.0~12.5", "trigger": "开盘半小时站稳12.5",
                "invalidation": "公告否认该订单传闻", "confidence": "中"}
        cands = [dict(base, name="太短股份", logic="利好"),
                 dict(base, name="合格股份",
                      logic="①依据 [1] 中标12亿元订单 ②订单未来两季确认收入 ③份额提升未被反映"),
                 dict(base, name="乱填区间股份", entry_zone="便宜",
                      logic="①依据 [2] 补贴落地 ②直接降低采购成本、抬升毛利率 ③力度超预期")]
        kept = pk._validate_items(cands, am=True)
        names = [c["name"] for c in kept]
        self.assertNotIn("太短股份", names)
        self.assertIn("合格股份", names)
        # 区间解析不出只清字段，不丢整条（区间只是辅助信息）
        self.assertIn("乱填区间股份", names)
        self.assertEqual(next(c for c in kept if c["name"] == "乱填区间股份")["entry_zone"], "")

    def test_pm_logic_gate_unchanged(self):
        """pm 路径不得启用 20 字硬闸（既有行为与账本历史口径不能被改）。"""
        pk = load("m10_picks", "modules/m10_picks/picks.py")
        pm = pk._validate_items([{"kind": "stock", "name": "短逻辑股份", "logic": "利好",
                                  "invalidation": "公告否认该订单传闻", "confidence": "中"}],
                                am=False)
        self.assertEqual([c["name"] for c in pm], ["短逻辑股份"])


class TwoBenchmarks(unittest.TestCase):
    """复盘同时给两个基准：沪深300 与 **中证1000**（2026-10-05）。

    动机：候选天然偏向中小盘（prompt 里明确"同等依据优先单价更低"），只用沪深300
    当基准会**系统性高估**这套判断的水平 —— 小盘股整体跑赢时，"判断对"是假的。
    所以每档同时写 `bench_ret/alpha`（沪深300）与 `bench2_ret/alpha2`（中证1000），
    **两个口径互不阻塞**：任一缺失只让那一个写 null，另一个照常算。
    """

    def setUp(self):
        self.pk = load("m10_picks", "modules/m10_picks/picks.py")
        self.rep = load("m5_report", "modules/m5_report/report.py")
        self.psh = load("m6_push", "modules/m6_push/push.py")

    def _d(self, s):
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=hc.CST)

    def _row(self, base_date=None, bench2=7300.0, name="甲股份"):
        r = {"id": f"2026-09-11-pm-s-{name}", "date": "2026-09-11", "slot": "pm",
             "kind": "stock", "name": name, "code": "600001", "secid": "1.600001",
             "market": "沪A", "base_price": 10.0, "base_prev_close": 9.9,
             "bench_level": 4000.0, "board": "", "logic": "x" * 30,
             "invalidation": "若公司公告订单延期或取消", "confidence": "中",
             "basis_refs": [1], "reviews": {str(k): None for k in self.pk.REVIEW_DAYS}}
        if bench2 is not None:
            r["bench2_level"] = bench2
            r["bench2_name"] = "中证1000"
        return r

    def _stub(self):
        self.pk.fetch_any = lambda secid: ({"price": 11.0, "prev_close": 10.0}, "stub")
        self.pk.fetch_bench = lambda: (None, {"level": 4000.0, "qdate": "20260914"})
        self.pk._inject_trading_days(["2026-09-14", "2026-09-16", "2026-09-18"])

    def test_index_secid_is_csi1000(self):
        self.assertEqual(self.pk.BENCH2_SECID, "1.000852", "中证1000 = 000852.SH")
        self.assertEqual(self.pk.BENCH2_NAME, "中证1000")

    def test_alpha2_computed_next_to_alpha(self):
        self._stub()
        row = self._row()
        # 沪深300 +1%、中证1000 +2%、个股 +10% → 两个口径都算出来且互不覆盖
        self.pk.score_pending([row], self._d("2026-09-14"), 4040.0, bench2_now=7446.0)
        rev = row["reviews"]["1"]
        self.assertAlmostEqual(rev["ret"], 0.1, places=4)
        self.assertAlmostEqual(rev["alpha"], 0.09, places=3)
        self.assertAlmostEqual(rev["bench2_ret"], 0.02, places=3)
        self.assertAlmostEqual(rev["alpha2"], 0.08, places=3)
        self.assertAlmostEqual(rev["alpha2"], rev["ret"] - rev["bench2_ret"], places=4)

    def test_missing_bench2_does_not_block_alpha(self):
        self._stub()
        row = self._row(bench2=None)          # 老账本行：没有第二基准
        self.pk.score_pending([row], self._d("2026-09-14"), 4040.0, bench2_now=7446.0)
        rev = row["reviews"]["1"]
        self.assertIsNotNone(rev["alpha"], "沪深300 口径必须照常算")
        self.assertIsNone(rev["alpha2"], "缺第二基准端点 → alpha2 为 null，不许编数")
        self.assertEqual(rev["status"], "ok")

    def test_missing_first_bench_still_gives_alpha2(self):
        """反方向也要成立：两个口径互不阻塞。"""
        self._stub()
        row = self._row()
        row["bench_level"] = None             # 沪深300 基准日缺失
        self.pk.score_pending([row], self._d("2026-09-14"), 4040.0, bench2_now=7446.0)
        rev = row["reviews"]["1"]
        self.assertIsNone(rev["alpha"])
        self.assertIsNotNone(rev["alpha2"])

    def test_display_names_both_benchmarks(self):
        rows = [self._row()]
        rows[0]["reviews"]["1"] = {"due": "2026-09-14", "done": "2026-09-14",
                                   "price": 11.0, "ret": 0.1, "bench": 4040.0,
                                   "bench_ret": 0.01, "alpha": 0.09, "status": "ok",
                                   "span": 1, "span_kind": "trading",
                                   "bench2": 7446.0, "bench2_ret": 0.02, "alpha2": 0.08}
        html = self.rep.render_picks(rows, "2026-10-09")
        p = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
        self.assertIn("沪深300", p)
        self.assertIn("中证1000", p)
        self.assertIn("只用沪深300 会高估水平", p, "必须解释为什么给两个基准")

    def test_missing_alpha2_shows_dash_not_zero(self):
        # 日期要在"往期候选"的展示窗口内（太旧的行走的是另一条分支）
        rows = [self._row(bench2=None)]
        rows[0]["date"] = "2026-10-06"
        rows[0]["reviews"]["1"] = {"due": "2026-09-14", "done": "2026-09-14",
                                   "price": 11.0, "ret": 0.1, "bench": 4040.0,
                                   "bench_ret": 0.01, "alpha": 0.09, "status": "ok",
                                   "span": 1, "span_kind": "trading",
                                   "bench2_ret": None, "alpha2": None}
        p = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", self.rep.render_picks(rows, "2026-10-09")))
        self.assertIn("甲股份", p, "该行应在往期回填里渲染")
        self.assertIn("中证1000", p)
        self.assertRegex(p, r"中证1000\s*[—–-]", "缺第二口径要显示破折号，不能显示 0%")


class InvalidationCheck(unittest.TestCase):
    """推翻条件**自动核查**（2026-10-05）。

    动机：每条候选都写了推翻条件（"若公司公告否认订单"），但复盘**只看价格** ——
    一条逻辑已破产、价格却恰好涨了的判断会被记成"成功"。现在复盘时把记录日之后
    新出现的新闻与该条的推翻条件做一次匹配，给出 triggered / not_triggered / unclear，
    并把"逻辑破产却价格跑赢"单独点出来。
    另一条不能破的规矩：**没查到 ≠ 未触发**（`null` 展示为「未核查」）。
    """

    def setUp(self):
        self.pk = load("m10_picks", "modules/m10_picks/picks.py")
        self.rep = load("m5_report", "modules/m5_report/report.py")
        import tempfile
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="snd-inv-"))
        self.roll = self.tmp / "news_roll.jsonl"

    def tearDown(self):
        self.pk._cal_reset() if hasattr(self.pk, "_cal_reset") else None

    # ── 新闻窗口留存 ────────────────────────────────────────────
    def _news(self, text, d="2026-10-05", source="新浪财经", t="10:23"):
        # 留存行的日期取自 time 字段（不是入参 d），所以这里必须把日期写进 time
        return {"time": f"{d} {t}:00", "source": source, "text": text,
                "category": "finance", "url": "", "keep": True}

    def test_roll_append_dedupes_and_truncates(self):
        n1 = self.pk.news_roll_append([self._news("甲公司公告：否认此前订单传闻")],
                                      "2026-10-05", path=self.roll)
        self.assertEqual(n1, 1)
        # 同内容再追加一次不应产生第二行（用内容哈希，不是每次新 id）
        n2 = self.pk.news_roll_append([self._news("甲公司公告：否认此前订单传闻")],
                                      "2026-10-05", path=self.roll)
        self.assertEqual(n2, 0)
        lines = [json.loads(x) for x in self.roll.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(set(lines[0]), {"d", "t", "s", "x", "h"})
        # 正文截断
        self.pk.news_roll_append([self._news("很长的新闻" * 100)], "2026-10-05", path=self.roll)
        last = json.loads(self.roll.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(len(last["x"]), self.pk.NEWS_ROLL_TEXT_CHARS)

    def test_roll_read_window_and_order(self):
        for d in ("2026-09-30", "2026-10-01", "2026-10-03", "2026-10-05"):
            self.pk.news_roll_append([self._news(f"{d} 的消息", d)], d, path=self.roll)
        got = self.pk.news_roll_read("2026-10-01", "2026-10-05", path=self.roll)
        days = [g["d"] for g in got]
        self.assertEqual(days, ["2026-10-05", "2026-10-03"], "窗口是 (记录日, 今天]，且最新在前")
        self.assertNotIn("2026-09-30", days)
        self.assertNotIn("2026-10-01", days, "记录日当天的新闻不算『之后新出现』")

    def test_roll_write_failure_is_only_warn(self):
        # 不可写路径必须**跨平台**：最初写的是 "Z:/…"，在 Windows 上 Z 盘不存在 → 写失败，
        # 但在 Linux（CI）上那只是普通相对目录、反而写成功 —— CI 因此红过一次。
        # 改成"父路径是一个文件"，两个平台上 mkdir 都会抛 OSError。
        blocker = self.tmp / "blocker"
        blocker.write_text("x", encoding="utf-8")
        bad = blocker / "news_roll.jsonl"
        self.assertEqual(self.pk.news_roll_append([self._news("x")], "2026-10-05", path=bad), 0)

    # ── 批量核查 ───────────────────────────────────────────────
    def _cand(self, cid, name="甲股份"):
        return {"id": cid, "name": name,
                "invalidation": "若公司公告否认该订单或产线未按期投产",
                "reviews": {"1": None, "3": None, "5": None}}

    def test_one_llm_call_covers_all_candidates(self):
        """三条候选必须共用**一次**调用（每轮一次批量核查，不是每条一次）。"""
        calls = []
        self.pk.M3.ds_chat = lambda msgs, max_tokens=None: calls.append(msgs) or json.dumps(
            {"checks": [{"id": "A", "verdict": "triggered", "reason": "窗口[1]公告否认该订单",
                         "basis": [1]},
                        {"id": "B", "verdict": "not_triggered",
                         "reason": "窗口[2]三季报毛利率 26.4%，未低于 20%", "basis": [2]},
                        {"id": "C", "verdict": "unclear", "reason": "窗口内无相关信息",
                         "basis": []}]}, ensure_ascii=False)
        news = [{"d": "2026-10-03", "t": "10:00", "s": "新浪财经", "x": "公告否认该订单"},
                {"d": "2026-10-04", "t": "10:00", "s": "财联社", "x": "三季报毛利率 26.4%"}]
        checks, note = self.pk.check_invalidations(
            [self._cand("A"), self._cand("B", "乙股份"), self._cand("C", "丙股份")], news)
        self.assertEqual(len(calls), 1, "三条候选必须共用一次调用")
        self.assertEqual({c: v["verdict"] for c, v in checks.items()},
                         {"A": "triggered", "B": "not_triggered", "C": "unclear"})

    def test_fabricated_basis_is_stripped_or_nulled(self):
        news = [{"d": "2026-10-03", "t": "10:00", "s": "新浪财经", "x": "只有一条新闻"}]
        self.pk.M3.ds_chat = lambda msgs, max_tokens=None: json.dumps({"checks": [
            {"id": "A", "verdict": "triggered", "reason": "编的依据", "basis": [99]}]},
            ensure_ascii=False)
        checks, _note = self.pk.check_invalidations([self._cand("A")], news)
        self.assertEqual(checks, {}, "全编造的 basis 应判无效（宁可不给结论）")

    def test_llm_failure_yields_null_and_keeps_ledger(self):
        def boom(msgs, max_tokens=None):
            raise RuntimeError("模拟网络炸了")
        self.pk.M3.ds_chat = boom
        self.pk.fetch_any = lambda secid: ({"price": 11.0, "prev_close": 10.0}, "stub")
        self.pk._inject_trading_days(["2026-09-14"])
        # 有可用的新闻窗口，否则会在"窗口为空"那道闸门就返回，测不到 LLM 失败路径
        self.pk.news_roll_append([self._news("公告否认该订单", "2026-09-13")],
                                 "2026-09-13", path=self.roll)
        row = {"id": "2026-09-11-pm-s-甲股份", "date": "2026-09-11", "slot": "pm",
               "kind": "stock", "name": "甲股份", "code": "600001", "secid": "1.600001",
               "market": "沪A", "base_price": 10.0, "base_prev_close": 9.9,
               "bench_level": 4000.0, "logic": "x" * 30,
               "invalidation": "若公司公告否认该订单或产线未按期投产",
               "confidence": "中", "basis_refs": [1],
               "reviews": {str(k): None for k in self.pk.REVIEW_DAYS}}
        rows = [row]
        today = datetime.strptime("2026-09-14", "%Y-%m-%d").replace(tzinfo=hc.CST)
        filled = self.pk.score_pending(rows, today, 4040.0)
        self.pk.review_pending(filled, today, news_path=self.roll)
        rev = row["reviews"]["1"]
        self.assertIn("invalidation_check", rev, "键必须存在")
        self.assertIsNone(rev["invalidation_check"], "核查失败写 null，不冒充 unclear")
        self.assertIsNotNone(rev["alpha"], "价格口径必须照常算出来")

    def test_no_rows_no_llm_call(self):
        calls = []
        self.pk.M3.ds_chat = lambda msgs, max_tokens=None: calls.append(1) or "{}"
        today = datetime.strptime("2026-09-12", "%Y-%m-%d").replace(tzinfo=hc.CST)
        self.assertEqual(self.pk.review_pending([], today, news_path=self.roll), 0)
        self.assertEqual(calls, [], "没有待复查的行时一次 LLM 都不许调")
        # 窗口为空同样不调（"没查到"不等于 unclear）
        row = {"id": "X", "date": "2026-09-11", "name": "甲股份",
               "invalidation": "若公司公告否认该订单或产线未按期投产"}
        self.assertEqual(self.pk.review_pending([row], today, news_path=self.roll), 0)
        self.assertEqual(calls, [])

    # ── 展示与关键统计 ──────────────────────────────────────────
    def _row_with(self, verdict, alpha=0.09, alpha2=None):
        ic = None if verdict is None else {"verdict": verdict, "reason": "窗口[1]公告否认该订单",
                                          "basis": [1], "checked_at": "2026-10-05 15:49:00"}
        return {"id": "2026-09-29-pm-s-甲股份", "date": "2026-09-29", "slot": "pm",
                "kind": "stock", "name": "甲股份", "code": "600001", "market": "沪A",
                "base_price": 10.0, "board": "", "logic": "x" * 30,
                "invalidation": "若公司公告否认订单", "confidence": "中", "basis_refs": [1],
                "reviews": {"1": {"due": "2026-09-30", "done": "2026-10-05", "price": 11.0,
                                  "ret": 0.1, "bench": 4040.0, "bench_ret": 0.01,
                                  "alpha": alpha, "status": "ok", "span": 6,
                                  "span_kind": "trading",
                                  "bench2": 7333.0, "bench2_ret": 0.0045, "alpha2": alpha2,
                                  "invalidation_check": ic}, "3": None, "5": None}}

    def test_four_states_and_never_confuse_null_with_not_triggered(self):
        cases = {
            "triggered": "疑似触发",
            "not_triggered": "未触发",
            "unclear": "无法判断",
            None: "未核查",
        }
        for verdict, expect in cases.items():
            html = self.rep.inv_check_html(self._row_with(verdict))
            self.assertIn(expect, html, f"{verdict} → {expect}")
            if verdict is None:
                self.assertNotIn("未触发", html, "未核查绝不能显示成未触发")
        # 核查结论与当初写的推翻条件原文是两件事，原文照旧显示
        card = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ",
                      self.rep.render_picks([self._row_with("triggered")], "2026-10-05")))
        self.assertIn("若公司公告否认订单", card)

    def test_key_stat_counts_bankrupt_logic(self):
        rows = [self._row_with("triggered"), self._row_with("triggered"),
                self._row_with("not_triggered"), self._row_with(None)]
        st = self.rep.inv_check_stats(rows)
        self.assertEqual(st["sample"], 3, "只数已核查的档")
        self.assertEqual(st["triggered"], 2)
        line = self.rep.inv_check_stat_line(st)
        self.assertIn("2 档逻辑破产", line)
        self.assertIn("已复核 3 档", line)
        # 样本为 0 时不显示这条（"0 条"会让人以为核查没在跑）
        self.assertEqual(self.rep.inv_check_stat_line(self.rep.inv_check_stats([])), "")

    def test_m9_surfaces_verdicts_and_stays_flat_rows(self):
        """M9 要透出核查口径与四态文案，且**文案只有一份来源**（复用 M5 常量）。

        行内的 `invalidation_check` 随 `reviews` 原样透传（所以不在这里出现字面量）；
        四态文案与统计口径都从 M5 复用，避免同一结论两处说法不同。
        """
        src = (ROOT / "modules/m9_web/aggregate.py").read_text(encoding="utf-8")
        self.assertIn('"inv"', src)
        self.assertIn("INV_CHECK_CN", src, "四态文案必须复用 M5 常量的单一定义")
        self.assertIn("inv_check_stats", src)
        app = (ROOT / "modules/m9_web/web/app.js").read_text(encoding="utf-8")
        self.assertIn("invalidation_check", app, "前端要读这个字段")
        for cn in ("未核查", "推翻条件：未触发", "无法判断"):
            self.assertIn(cn, app, f"前端缺少文案：{cn}")


class ReviewDashboard(unittest.TestCase):
    """M12 复盘看板（2026-10-05，第 4 项）。

    它回答的是"这套判断到底准不准"，所以**不许美化数字**：均值没有样本就不存在
    （写「暂无」而不是 0.00%）、每个均值必须带样本数、样本 <5 要标注不足、
    未核查的档不许算进核查口径、全文不给"胜率"。
    """

    def setUp(self):
        import tempfile
        self.wk = load("m12_weekly", "modules/m12_weekly/weekly.py")
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="snd-m12-"))
        self.ledger = self.tmp / "ledger.jsonl"

    def _write(self, rows):
        self.ledger.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                               encoding="utf-8")

    def _row(self, name, date="2026-09-21", slot="pm", conf="中", alpha=0.10,
             alpha2=0.08, verdict=None, board="", t1=True):
        rev1 = None
        if t1:
            rev1 = {"due": "2026-09-22", "done": "2026-09-22", "price": 11.0, "ret": 0.1,
                    "bench": 4040.0, "bench_ret": 0.01, "alpha": alpha, "status": "ok",
                    "span": 1, "span_kind": "trading",
                    "bench2_ret": 0.004, "alpha2": alpha2,
                    "invalidation_check": (None if verdict is None else
                                           {"verdict": verdict, "reason": "r", "basis": [1],
                                            "checked_at": "2026-09-22 15:49:00"})}
        return {"id": f"{date}-{slot}-s-{name}", "date": date, "slot": slot, "kind": "stock",
                "name": name, "code": "000001", "market": "深A", "base_price": 10.0,
                "board": board, "logic": "x" * 30, "invalidation": "若公告否认订单",
                "confidence": conf, "basis_refs": [1],
                "reviews": {"1": rev1, "3": None, "5": None}}

    def _page(self, rows, asof="2026-09-30"):
        self._write(rows)
        out = self.tmp / "out"
        rc = self.wk.main(["--ledger", str(self.ledger), "--out-dir", str(out),
                           "--asof", asof, "--quiet"])
        self.assertEqual(rc, 0)
        html = (out / "review-latest.html").read_text(encoding="utf-8")
        return out, html

    def test_mean_matches_hand_calculation(self):
        _, html = self._page([self._row("A", alpha=0.10, alpha2=0.08),
                              self._row("B", alpha=0.02, alpha2=0.00),
                              self._row("C", alpha=-0.03, alpha2=-0.01)])
        # 手算：沪深300 (0.10+0.02-0.03)/3 = +3.00%；中证1000 (0.08+0-0.01)/3 = +2.33%
        self.assertIn("+3.00%", html)
        self.assertIn("+2.33%", html)
        self.assertIn("n=3", html, "每个均值必须带样本数")

    def test_no_sample_shows_dash_not_zero(self):
        _, html = self._page([self._row("A", t1=False)])
        self.assertIn("暂无", html)
        self.assertNotIn("0.00%", html, "0 样本的均值是编出来的，绝不能显示 0.00%")

    def test_unchecked_not_counted_in_check_scope(self):
        # 3 档已核查（2 triggered / 1 not_triggered）+ 1 档 null（未核查）
        _, html = self._page([self._row("A", verdict="triggered", alpha=0.05),
                              self._row("B", verdict="triggered", alpha=-0.02),
                              self._row("C", verdict="not_triggered"),
                              self._row("D", verdict=None)])
        self.assertIn("已复核档数", html)
        self.assertNotIn("已复核档数 4", html, "null 的档不算已核查")
        self.assertIn("逻辑破产但价格仍跑赢", html)

    def test_small_sample_note_threshold(self):
        _, html3 = self._page([self._row(f"S{i}") for i in range(3)])
        self.assertIn("样本不足", html3)
        _, html6 = self._page([self._row(f"S{i}") for i in range(6)])
        self.assertNotIn("样本不足", html6)

    def test_honesty_words_and_no_winrate(self):
        _, html = self._page([self._row("A")])
        self.assertNotIn("胜率", html)
        self.assertIn("不是买入指令", html)
        self.assertIn("中证1000", html)
        self.assertIn("沪深300", html)

    def test_ledger_is_read_only(self):
        self._write([self._row("A")])
        before = hashlib.sha256(self.ledger.read_bytes()).hexdigest()
        self.wk.main(["--ledger", str(self.ledger), "--out-dir", str(self.tmp / "o2"),
                      "--asof", "2026-09-30", "--quiet"])
        self.assertEqual(hashlib.sha256(self.ledger.read_bytes()).hexdigest(), before,
                         "看板必须只读账本")

    def test_missing_ledger_does_not_crash(self):
        out = self.tmp / "nope"
        rc = self.wk.main(["--ledger", str(self.tmp / "不存在.jsonl"),
                           "--out-dir", str(out), "--asof", "2026-09-30", "--quiet"])
        self.assertEqual(rc, 0)
        self.assertTrue((out / "review-latest.html").is_file())

    def test_verdict_labels_consistent_across_modules(self):
        """四态文案在三处必须**逐字一致**（各模块独立复制常量，语义由本断言锁住）。

        同一条结论在日报里叫「推翻条件：疑似触发」、在复盘看板里叫「推翻核查：疑似触发」，
        会让人怀疑它们不是同一件事 —— 这是纯粹的措辞漂移，没有理由存在。
        """
        rep = load("m5_report", "modules/m5_report/report.py")
        canonical = list(rep.INV_CHECK_CN.values()) + [rep.INV_UNCHECKED_CN]
        app = (ROOT / "modules/m9_web/web/app.js").read_text(encoding="utf-8")
        wk = (ROOT / "modules/m12_weekly/weekly.py").read_text(encoding="utf-8")
        wkmod = load("m12_weekly", "modules/m12_weekly/weekly.py")
        for txt in canonical:
            self.assertIn(txt, app, f"app.js 缺少与 M5 一致的文案：{txt}")
            self.assertIn(txt, wk, f"weekly.py 缺少与 M5 一致的文案：{txt}")
        self.assertEqual(sorted(wkmod.VERDICT_CN.values()),
                         sorted(rep.INV_CHECK_CN.values()),
                         "看板的四态文案必须与日报逐字相同")
        self.assertEqual(wkmod.VERDICT_NONE_CN, rep.INV_UNCHECKED_CN)

    def test_wired_into_history_api_and_export(self):
        """看板入口必须走数据（不是硬编码链接）：接口与静态导出都要带、前端要判 exists。"""
        srv = (ROOT / "modules/m9_web/server.py").read_text(encoding="utf-8")
        exp = (ROOT / "modules/m9_web/export.py").read_text(encoding="utf-8")
        app = (ROOT / "modules/m9_web/web/app.js").read_text(encoding="utf-8")
        idx = (ROOT / "modules/m9_web/web/index.html").read_text(encoding="utf-8")
        self.assertIn("b.review_view()", srv)
        self.assertIn("bundle.review_view()", exp)
        self.assertIn("rv.exists", app)
        self.assertNotIn('href="../review-latest.html"', idx,
                         "硬编码链接会与接口形成两份真相，且本地必 404")


if __name__ == "__main__":
    unittest.main(verbosity=2)
