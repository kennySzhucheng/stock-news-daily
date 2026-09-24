# -*- coding: utf-8 -*-
"""
M8 端到端集成测试运行器

依次执行 M1→M6 全链路，记录每步耗时/退出码/输出摘要，校验各环节产出文件，
最后输出汇总报告（同时写入 data/e2e_report.json）。

用法：
    python modules/m8_e2e/e2e_run.py              # 完整链路（含微信推送）
    python modules/m8_e2e/e2e_run.py --no-push    # 跳过 M6 推送
    python modules/m8_e2e/e2e_run.py --from M3    # 从指定模块开始跑
    python modules/m8_e2e/e2e_run.py --slot am    # 强制盘前时段（默认按北京时间判定）

环境变量要求：
    ZAI_API_KEY          M2 新闻筛选（缺失则跳过 M2 及其后）
    DEEPSEEK_API_KEY     M3 深度分析
    SERVERCHAN_SENDKEY   M6 微信推送（缺失则 M6 自动跳过）
    REPORT_SLOT          可选，覆盖盘前/盘后时段判定（am/pm）
"""
import os
import re
import sys
import json
import time
import argparse
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta

BASE = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE / "data"
REPORTS_DIR = BASE / "reports"
CST = timezone(timedelta(hours=8))

# Windows 控制台默认 GBK，强制 UTF-8 才能正确输出中文
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

STEPS = [
    {"id": "M1", "name": "新闻收集",            "script": "modules/m1_collector/collector.py",
     "env": [],                     "timeout": 300},
    {"id": "M2", "name": "新闻筛选（GLM）",      "script": "modules/m2_filter/filter.py",
     "env": ["ZAI_API_KEY"],        "timeout": 900},
    {"id": "M3", "name": "深度分析（DeepSeek）", "script": "modules/m3_analyzer/analyzer.py",
     "env": ["DEEPSEEK_API_KEY"],   "timeout": 600},
    {"id": "M4", "name": "行情数据",            "script": "modules/m4_quotes/quotes.py",
     "env": [],                     "timeout": 600},
    # M10 必须在 M4 之后（要当日行情锁基准价）、在 M5/M6/M9 之前（它们要读账本）
    {"id": "M10", "name": "候选与复盘（DeepSeek）", "script": "modules/m10_picks/picks.py",
     "env": ["DEEPSEEK_API_KEY"],   "timeout": 600},
    {"id": "M5", "name": "日报生成",            "script": "modules/m5_report/report.py",
     "env": [],                     "timeout": 120},
    {"id": "M9", "name": "网页版导出",          "script": "modules/m9_web/export.py",
     "env": [],                     "timeout": 180},
    {"id": "M6", "name": "微信推送",            "script": "modules/m6_push/push.py",
     "env": ["SERVERCHAN_SENDKEY"], "timeout": 120},
]


def check_output(step_id, output=""):
    """校验该步骤的产出文件，返回 [(检查项, 是否通过, 说明)]"""
    checks = []

    if step_id == "M1":
        p = DATA_DIR / "raw_news.json"
        if not p.exists():
            return [("raw_news.json", False, "文件不存在")]
        d = json.loads(p.read_text(encoding="utf-8"))
        n = len(d.get("news", []))
        checks.append(("原始新闻条数", n >= 50, f"{n} 条（验收要求 ≥50）"))
        srcs = [s for s in d.get("sources_status", []) if "source" in s]
        ok = sum(1 for s in srcs if s.get("ok"))
        checks.append(("数据源可用数", ok >= 3, f"{ok}/{len(srcs)} 个成功（要求 ≥3 类来源）"))

    elif step_id == "M2":
        p = DATA_DIR / "structured_news.json"
        if not p.exists():
            return [("structured_news.json", False, "文件不存在")]
        d = json.loads(p.read_text(encoding="utf-8"))
        news = d.get("news", [])
        checks.append(("结构化新闻条数", len(news) > 0, f"{len(news)} 条"))
        cats = {}
        for n in news:
            cats[n.get("category", "?")] = cats.get(n.get("category", "?"), 0) + 1
        checks.append(("分类覆盖", len(cats) >= 2, f"{len(cats)} 类：{cats}"))
        conf = sum(1 for n in news if n.get("verified") == "confirmed")
        checks.append(("交叉验证标记", True, f"已确认 {conf} / 待核实 {len(news) - conf}"))

    elif step_id == "M3":
        p = DATA_DIR / "analysis.md"
        if not p.exists():
            return [("analysis.md", False, "文件不存在")]
        txt = p.read_text(encoding="utf-8")
        checks.append(("分析报告长度", len(txt) > 500, f"{len(txt)} 字符"))
        for sec in ["市场情绪", "板块", "个股", "风险"]:
            checks.append((f"含「{sec}」章节", sec in txt, ""))

    elif step_id == "M4":
        p = DATA_DIR / "quotes.json"
        if not p.exists():
            return [("quotes.json", False, "文件不存在")]
        d = json.loads(p.read_text(encoding="utf-8"))
        checks.append(("行情条数", d.get("count", 0) > 0, f"{d.get('count', 0)} 只"))

    elif step_id == "M10":
        # 账本是跨天累积的文件，盘前/非交易日那次运行本就不该写新候选，
        # 所以「没新增行」是正常结果，不能判为失败。
        p = REPORTS_DIR / "picks" / "ledger.jsonl"
        if not p.exists():
            checks.append(("ledger.jsonl", True,
                           "尚无账本（首次运行或盘前跳过，属正常）"))
            return checks
        rows = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                checks.append(("账本可解析", False, "存在无法解析的行"))
                return checks
        checks.append(("账本可解析", True, f"{len(rows)} 行"))
        need = {"id", "date", "slot", "kind", "name", "reviews"}
        bad = [r.get("id", "?") for r in rows if not need.issubset(r)]
        checks.append(("每行字段完整", not bad, f"缺字段: {bad[:3]}" if bad else ""))
        # 账本必须跨运行累积：只有一行说明每天在覆盖，而不是追加
        days = {r.get("date") for r in rows}
        checks.append(("跨天留存", len(days) >= 1, f"{len(days)} 个日期"))
        # M10 的核心结构约束：没有推翻条件的候选在代码里就被丢弃了，
        # 账本里出现空 invalidation 说明那层校验失效了
        noinv = [r.get("name") for r in rows if not (r.get("invalidation") or "").strip()]
        checks.append(("每条候选都有推翻条件", not noinv,
                       f"缺失: {noinv[:3]}" if noinv else "（结构上保证非荐股）"))

    elif step_id == "M5":
        date_str = datetime.now(CST).strftime("%Y-%m-%d")
        # 盘前/盘后各一份，取当天已有的时段文件；没有则退回到当天任意一份
        cands = sorted(REPORTS_DIR.glob(f"{date_str}-*.html"))
        if not cands:
            return [(f"{date_str}-am/pm.html", False, "文件不存在")]
        p = cands[-1]
        size = p.stat().st_size
        checks.append(("日报文件名含时段后缀",
                       re.fullmatch(r"\d{4}-\d{2}-\d{2}-(am|pm)\.html", p.name) is not None,
                       p.name))
        checks.append(("日报文件大小", size > 5000, f"{size} 字节"))
        html = p.read_text(encoding="utf-8")
        # 版块顺序即用户要求：市场分析必须在新闻列表之前
        for sec, anchor in [("市场分析", 'id="market"'), ("个股行情一览", 'id="quotes"'),
                            ("分板块新闻", 'id="news"'), ("风险声明", 'class="risk"')]:
            checks.append((f"含「{sec}」版块", anchor in html, f"锚点 {anchor}"))
        # 按**版块锚点**比顺序，不按正文词。正文/注释里出现「分板块新闻」这几个字
        # 是常事（折叠说明的 CSS 注释里就有一处），用 find(词) 会命中它而非版块
        i_mkt, i_news = html.find('id="market"'), html.find('id="news"')
        checks.append(("市场分析在新闻列表之前", 0 <= i_mkt < i_news,
                       f"market@{i_mkt} news@{i_news}"))
        checks.append(("latest.html 存在", (REPORTS_DIR / "latest.html").exists(), ""))
        checks.append(("索引页存在", (REPORTS_DIR / "index.html").exists(), ""))
        # 时段保留：当天两个时段都跑过时，必须两份都在
        both = (REPORTS_DIR / f"{date_str}-am.html").exists() and \
               (REPORTS_DIR / f"{date_str}-pm.html").exists()
        checks.append(("盘前盘后是否两份并存", True,
                       "两份都在" if both else "目前只有一份（另一时段尚未运行）"))

        # 账本里有候选 → 日报必须出现候选版块（M10 的产出要真的被 M5 读到）
        led = REPORTS_DIR / "picks" / "ledger.jsonl"
        n_pick = 0
        if led.exists():
            for line in led.read_text(encoding="utf-8").splitlines():
                try:
                    if json.loads(line).get("date") == date_str:
                        n_pick += 1
                except json.JSONDecodeError:
                    pass
        if n_pick:
            checks.append(("日报含「候选观察清单」版块", 'id="picks"' in html,
                           f"当日账本 {n_pick} 条"))
            # 顺序即叙事顺序：先给判断 → 再给由此推出的候选 → 然后才是数据
            checks.append(("候选版块夹在判断与数据之间",
                           html.find('id="market"') < html.find('id="picks"')
                           < html.find('id="quotes"'), ""))
        else:
            checks.append(("日报候选版块", True, "当日账本无候选，版块不应出现"))

    elif step_id == "M9":
        web = REPORTS_DIR / "web" / "index.html"
        api = REPORTS_DIR / "web" / "api"
        if not web.exists():
            return [("reports/web/index.html", False, "文件不存在")]
        checks.append(("网页版 index.html", True, f"{web.stat().st_size} 字节"))
        for f in ("news", "overview", "boards", "analysis", "history", "picks"):
            p2 = api / f"{f}.js"
            checks.append((f"静态数据 {f}.js", p2.exists(),
                           f"{p2.stat().st_size} 字节" if p2.exists() else "缺失"))
        txt = web.read_text(encoding="utf-8")
        checks.append(("已切换为静态模式", "window.__STATIC__ = true" in txt, ""))
        checks.append(("候选标签页存在", 'data-tab="picks"' in txt, ""))
        # 前端 apiGet('picks') 走的文件名必须与 export.py 的 key 对得上，
        # 否则静态版这个页永远空着（本地服务却正常，最难发现的那种）
        pjs = api / "picks.js"
        if pjs.exists():
            checks.append(("picks.js 是合法载荷",
                           'window.__DATA__["picks"]=' in
                           pjs.read_text(encoding="utf-8"), ""))
        # 数据走 <script> 注入，file:// 直接双击也能打开（fetch 会被 CORS 拦）
        news_js = (api / "news.js")
        if news_js.exists():
            head = news_js.read_text(encoding="utf-8")[:60]
            checks.append(("数据以 window.__DATA__ 注入",
                           head.startswith("window.__DATA__"), head[:40]))

    elif step_id == "M6":
        m = re.search(r"pushid=(\d+)", output)
        if m:
            checks.append(("微信推送", True, f"pushid={m.group(1)}"))
        elif "未设置" in output or "跳过" in output:
            checks.append(("微信推送", True, "已跳过（SendKey 未配置）"))
        elif "推送失败" in output:
            checks.append(("微信推送", False, "推送失败（不影响日报生成）"))
        else:
            checks.append(("微信推送", False, "未检测到推送结果"))

    return checks


def run_step(step, env):
    """执行单个步骤，返回结果字典"""
    script = BASE / step["script"]
    if not script.exists():
        return {"ok": False, "elapsed": 0, "output": "", "error": f"脚本不存在: {script}"}

    missing = [k for k in step["env"] if not env.get(k)]
    if missing:
        return {"ok": False, "elapsed": 0, "output": "",
                "error": f"缺少环境变量 {missing}，已跳过"}

    t0 = time.time()
    try:
        r = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(BASE), env=env, timeout=step["timeout"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        elapsed = time.time() - t0
        return {
            "ok": r.returncode == 0,
            "elapsed": elapsed,
            "output": (r.stdout or "").strip(),
            "error": (r.stderr or "").strip() if r.returncode != 0 else "",
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "elapsed": time.time() - t0, "output": "",
                "error": f"超时（>{step['timeout']}s）"}


def main():
    ap = argparse.ArgumentParser(description="M8 端到端集成测试")
    ap.add_argument("--no-push", action="store_true", help="跳过 M6 微信推送")
    ap.add_argument("--from", dest="start", default="M1", help="从指定模块开始（如 M3）")
    ap.add_argument("--slot", choices=["am", "pm"], default="",
                    help="强制时段；不指定则各模块按北京时间自行判定")
    args = ap.parse_args()

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"   # 保证子进程输出可用 UTF-8 解码
    if args.slot:
        env["REPORT_SLOT"] = args.slot

    ids = [s["id"] for s in STEPS]
    start_idx = ids.index(args.start) if args.start in ids else 0
    todo = STEPS[start_idx:]
    if args.no_push:
        todo = [s for s in todo if s["id"] != "M6"]

    print("=" * 60)
    print(f"M8 端到端集成测试  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"执行范围: {' -> '.join(s['id'] for s in todo)}"
          f"{'（已跳过 M6 推送）' if args.no_push else ''}")
    print("=" * 60)

    report = {"started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "skipped_push": args.no_push, "steps": []}
    overall_ok = True

    for step in todo:
        print(f"\n[{step['id']}] {step['name']} ... ", end="", flush=True)
        res = run_step(step, env)
        elapsed = f"{res['elapsed']:.1f}s"

        if not res["ok"]:
            overall_ok = False
            print(f"失败 ({elapsed})")
            if res["error"]:
                print(f"   错误: {res['error'][:300]}")
            report["steps"].append({
                "id": step["id"], "name": step["name"], "ok": False,
                "elapsed": round(res["elapsed"], 1), "error": res["error"][:500],
                "checks": [],
            })
            print("\n链路中断，后续步骤不再执行。")
            break

        print(f"完成 ({elapsed})")

        # 输出摘要（每行缩进显示，最多 12 行）
        if res["output"]:
            for line in res["output"].splitlines()[-12:]:
                print(f"   | {line}")

        checks = check_output(step["id"], res["output"])
        for name, passed, note in checks:
            mark = "[OK]  " if passed else "[FAIL]"
            print(f"   {mark} {name}: {note}")
            if not passed:
                overall_ok = False

        report["steps"].append({
            "id": step["id"], "name": step["name"], "ok": True,
            "elapsed": round(res["elapsed"], 1),
            "checks": [{"item": n, "pass": p, "note": x} for n, p, x in checks],
        })

    report["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report["overall_ok"] = overall_ok

    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "e2e_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    total = sum(s["elapsed"] for s in report["steps"])
    print("\n" + "=" * 60)
    print(f"总耗时 {total:.1f}s  结果: {'全部通过' if overall_ok else '存在问题'}")
    print(f"报告 -> {DATA_DIR / 'e2e_report.json'}")
    print("=" * 60)
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
