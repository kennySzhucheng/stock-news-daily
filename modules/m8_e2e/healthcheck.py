# -*- coding: utf-8 -*-
"""M8 送达体检：把「静默失败」主动查出来

这条链路上的失败几乎都是静默的，而且**表象与「只是延迟」长得一模一样**：

  * cron-job.org 的令牌失效时请求被拒（401），压根不产生运行，Actions
    运行记录里一条都看不到 —— 2026-09-19 建好的 job 就是这样潜伏到
    09-24 才被发现，那 5 天里日报每天晚到 4~5 小时却没人察觉；
  * Server酱 免费额度只有 5 条/天，额度耗尽时推送失败，但日报照常生成、
    网页照常上线，只有微信收不到；
  * workflow 整体没跑或跑挂，只要没人当天去翻日报，就发现不了。

本工具每天把「该到的到底到了没有」核一遍，检查项：

  1. 盘前/盘后两份日报是否都已产出并上线
  2. 各自的送达时刻，与计划时点（08:10 / 15:40）比是否准点
  3. 当天有没有**外部触发**（workflow_dispatch）—— 没有就说明
     cron-job.org / 本机计划任务这条「准点通路」断了，日报只能等
     GitHub 那个会延迟 4~5 小时的 schedule 兜底
  4. 当天**真正出报**的运行次数。判据同 observe.py：读产物名，而不是看
     conclusion —— 被去重跳过的运行结论同样是 success，只看结论会高估。
     超过 2 次说明去重没挡住，有重复推送的风险
  5. 推送是否成功（读 reports/status-<date>-<slot>.json，由 M6 写入）

用法：
    python modules/m8_e2e/healthcheck.py                  # 体检最近已结束的一天
    python modules/m8_e2e/healthcheck.py --date 2026-09-24
    python modules/m8_e2e/healthcheck.py --days 7         # 连看一周
    python modules/m8_e2e/healthcheck.py --notify         # 发现问题时推微信

退出码：0 = 正常；1 = 发现严重问题。CI 里据此让 workflow 失败，借 GitHub
自己的失败邮件告警 —— 这条通道不依赖 Server酱，所以「推送本身坏掉」这种
情况也报得出来。
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE.parent.parent

# 与仓库既有惯例一致：模块间靠 sys.path 直接 import 同级文件（无 __init__.py）
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(BASE / "modules" / "m6_push"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _find_token():
    """优先环境变量，否则复用本机 `gh` 的登录态。

    未认证时 api.github.com 只有 60 次/小时，而体检一天要十来次调用，连查几天
    就会撞上限额（表现为 403，看起来像故障）。CI 里 GITHUB_TOKEN 是自带的。
    """
    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    if tok:
        return tok
    for exe in (r"C:\Program Files\GitHub CLI\gh.exe", "gh"):
        try:
            r = subprocess.run([exe, "auth", "token"], capture_output=True,
                               text=True, timeout=20)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception:
            continue
    return ""


# observe 在**导入时**就把 GITHUB_TOKEN 读成模块常量了，所以要在这之前设好
if not os.environ.get("GITHUB_TOKEN"):
    _tok = _find_token()
    if _tok:
        os.environ["GITHUB_TOKEN"] = _tok

import observe  # noqa: E402  复用它的 API 读取与「产物名判产出」逻辑

CST = observe.CST
SLOT_CN = observe.SLOT_CN

# 计划时点（北京时间），与 daily.yml 的 cron 对应
PLAN = {"am": "08:10", "pm": "15:40"}
# 迟到多少分钟以内仍算准点。正常时外部触发在 1 分钟内送达，留 15 分钟余量
ON_TIME_TOL_MIN = 15
# 过了这个钟点（北京时间），当天的盘后时段就该有结果了 —— 用于推断体检的目标日
PM_SETTLED_HOUR = 16
# 一天真正出报的次数上限：盘前、盘后各一次
EXPECTED_PRODUCED = 2

SEVERE, WARN = "严重", "提醒"

# 推送状态文件（由 M6 写入）从这一天起才会出现在线上。早于它的日期查不到
# 状态属正常，只作提醒；从这一天起查不到就要当成问题——说明 M6 没写或没部署。
STATUS_SINCE = "2026-09-25"


# ---------------------------------------------------------------- 数据获取

def _retry(fn, retries=4):
    """跑一次可能因网络抖动失败的请求，失败后退避重试。

    国内直连 api.github.com 时常抖（IncompleteRead / RemoteDisconnected /
    超时），单次失败不代表接口有问题。带 HTTP 状态码的响应（4xx/5xx）是明确
    答复，不重试，直接交给调用方处理。
    """
    import time
    last = None
    for i in range(retries):
        try:
            return fn()
        except urllib.error.HTTPError:
            raise
        except Exception as e:
            last = e
            if i < retries - 1:
                time.sleep(2 * (i + 1))
    raise last


def get_json(url, retries=4):
    return _retry(lambda: observe.get_json(url), retries)


def runs_on(repo, date_str):
    """取「创建时间落在 date_str（北京时间）」的运行，返回 [(创建时刻, run)]。

    运行按 created_at 倒序返回，一旦出现早于目标日的运行即可停止翻页。
    """
    out = []
    for page in range(1, 6):
        d = get_json(
            f"https://api.github.com/repos/{repo}/actions/workflows/"
            f"daily.yml/runs?per_page=100&page={page}")
        runs = d.get("workflow_runs", [])
        if not runs:
            break
        stop = False
        for r in runs:
            created = datetime.fromisoformat(
                r["created_at"].replace("Z", "+00:00")).astimezone(CST)
            day = created.strftime("%Y-%m-%d")
            if day == date_str:
                out.append((created, r))
            elif day < date_str:
                stop = True
                break
        if stop:
            break
    return out


def push_status(pages_base, date_str, slot):
    """读 M6 写下的推送状态。返回 (push_ok 或 None, 说明)。None = 查不到。"""
    try:
        with _retry(lambda: observe._open(
                f"{pages_base}/status-{date_str}-{slot}.json")) as r:
            d = json.loads(r.read().decode("utf-8"))
        return bool(d.get("push_ok")), str(d.get("push_detail", ""))
    except urllib.error.HTTPError as e:
        return None, "线上无推送状态文件" if e.code == 404 else f"HTTP {e.code}"
    except Exception as e:
        return None, str(e)[:50]


def pages_deliveries(repo, date_str, pages=40):
    """从 gh-pages 的提交历史里取出该日各时段的**送达时刻**。

    返回 {slot: 时刻}；另外若该日存在「早期」命名的 `<日期>.html`（M5 加时段
    后缀之前的产物，只有一份、不分盘前盘后），以键 `legacy` 一并返回。

    为什么不用运行记录判时段：运行对象里既没有 `inputs` 也没有 `schedule` 字段
    （实测两者皆为 null），而 artifact 名在 2026-09-24 之前也不带时段。也就是说
    光看运行记录，根本推不出这次产出的是盘前还是盘后。

    部署提交则没有这个问题——**提交里改了哪些文件**是明确的：`2026-09-23-am.html`
    被写入的那一刻，就是盘前那份的送达时刻；文件名自带时段，旧命名同样有效。
    """
    out = {}
    d = get_json(f"https://api.github.com/repos/{repo}/commits"
                 f"?sha=gh-pages&per_page={pages}")
    for c in d:
        raw = (c.get("commit") or {}).get("committer", {}).get("date", "")
        if not raw:
            continue
        when = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(CST)
        day = when.strftime("%Y-%m-%d")
        if day > date_str:
            continue
        if day < date_str:      # 提交按时间倒序，再往前都不是目标日了
            break
        # 列表接口不含 files，得单独取一次提交详情
        try:
            det = get_json(f"https://api.github.com/repos/{repo}/commits/{c['sha']}")
        except Exception:
            continue
        names = [f.get("filename", "") for f in det.get("files", [])]
        for slot in ("am", "pm"):
            if f"{date_str}-{slot}.html" in names and slot not in out:
                out[slot] = when
        if f"{date_str}.html" in names and "legacy" not in out:
            out["legacy"] = when
    return out


def fmt_delay(minutes):
    h, m = divmod(int(minutes), 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


# ---------------------------------------------------------------- 单日体检

def check_date(repo, pages_base, date_str):
    """体检一天。返回 (逐行输出, 问题列表 [(级别, 文字)])。"""
    out, problems = [], []
    runs = runs_on(repo, date_str)

    out.append(f"\n{'=' * 74}")
    out.append(f"送达体检 · {date_str}（北京时间）  共 {len(runs)} 次运行")
    out.append("=" * 74)

    if not runs:
        problems.append((SEVERE, "当天没有任何运行记录 —— workflow 没被触发过"))
        out.append("  当天没有任何运行记录。")
        return out, problems

    # 按产物名判定每次运行**真正出报**还是被去重跳过（判据同 observe.py）。
    # 注意「哪个时段、几点送达」不看运行记录，而是看 gh-pages 的部署提交——
    # 产物名在 2026-09-24 之前不带时段，光看运行判不出时段（见 pages_deliveries）。
    produced_n = unknown_n = 0
    events = {}
    out.append(f"\n{'运行号':<8}{'触发时刻':<11}{'触发源':<18}{'状态':<7}{'耗时':<8}产出")
    out.append("-" * 74)
    for created, r in sorted(runs, key=lambda x: x[0]):
        slot, produced, note = observe.run_output(r)
        if produced:
            produced_n += 1
        elif "无法判定" in note:
            unknown_n += 1     # 产物已过 7 天保留期，判不出这次有没有出报
        event = r.get("event", "?")
        events[event] = events.get(event, 0) + 1
        updated = datetime.fromisoformat(
            r["updated_at"].replace("Z", "+00:00")).astimezone(CST)
        dur = (updated - created).total_seconds()
        shown = f"{date_str}-{slot}.html（{SLOT_CN[slot]}）" if slot else note
        out.append(f"{r['run_number']:<8}{created.strftime('%H:%M:%S'):<11}"
                   f"{event:<18}{observe.STATUS_CN.get(r.get('conclusion'), '?'):<7}"
                   f"{dur:>5.0f}s  {shown}")

    # 1 + 2：两份日报是否送达、送达时刻、是否准点
    deliveries = pages_deliveries(repo, date_str)
    out.append("\n各时段：")
    for slot in ("am", "pm"):
        plan_hm = PLAN[slot]
        if slot not in deliveries:
            # 早期日报只有一份、不分盘前盘后（名为 <日期>.html）。若它存在，
            # 说明当天确实出了报，只是还没有时段后缀，不算问题。
            if "legacy" in deliveries:
                out.append(f"  {SLOT_CN[slot]}  计划 {plan_hm}   —  无独立文件"
                           f"（该日系「早期」命名，仅一份 {date_str}.html，"
                           f"送至 {deliveries['legacy']:%H:%M:%S}）")
                continue
            problems.append((SEVERE, f"{SLOT_CN[slot]}日报当天**没有送达**"
                                     f"（线上没有 {date_str}-{slot}.html）"))
            out.append(f"  {SLOT_CN[slot]}  计划 {plan_hm}   ✗ 未送达")
            continue
        got = deliveries[slot]
        plan_dt = datetime.strptime(f"{date_str} {plan_hm}", "%Y-%m-%d %H:%M").replace(tzinfo=CST)
        delay = (got - plan_dt).total_seconds() / 60
        # 判据必须是**双向**的。原来只写 `delay <= ON_TIME_TOL_MIN`，于是「早到」
        # 被当成准点表扬：2026-09-25 盘前计划 08:10、实到 00:15，delay=-475，
        # −475 ≤ 15 成立 → 打印「✓ 准点（早7h55m）」，当晚体检因此输出
        # 「一切正常 ✓」，把一次「测试运行占掉正式时段、当天准点触发被去重挡掉」
        # 的真实故障完整地漏了过去。
        if delay > ON_TIME_TOL_MIN:
            problems.append((SEVERE, f"{SLOT_CN[slot]}日报迟 {fmt_delay(delay)}"
                                     f"（计划 {plan_hm}，实到 {got:%H:%M}）"))
            out.append(f"  {SLOT_CN[slot]}  计划 {plan_hm}  送达 {got:%H:%M:%S}  "
                       f"⚠ 迟 {fmt_delay(delay)}")
        elif delay < -ON_TIME_TOL_MIN:
            problems.append((SEVERE, f"{SLOT_CN[slot]}日报**过早送达** {fmt_delay(abs(delay))}"
                                     f"（计划 {plan_hm}，实到 {got:%H:%M}）——"
                                     f"多半是测试/调试运行产出了这一份，"
                                     f"内容并非该时段的快照"))
            out.append(f"  {SLOT_CN[slot]}  计划 {plan_hm}  送达 {got:%H:%M:%S}  "
                       f"⚠ 过早 {fmt_delay(abs(delay))}")
        else:
            out.append(f"  {SLOT_CN[slot]}  计划 {plan_hm}  送达 {got:%H:%M:%S}  "
                       f"✓ 准点（{'早' if delay < 0 else '迟'}{fmt_delay(abs(delay))}）")

    # 5：推送是否成功
    out.append("\n推送：")
    for slot in ("am", "pm"):
        ok, detail = push_status(pages_base, date_str, slot)
        if ok is True:
            out.append(f"  {SLOT_CN[slot]}  ✓ 已送达微信（{detail}）")
        elif ok is False:
            problems.append((SEVERE, f"{SLOT_CN[slot]}推送**失败**（{detail}）"
                                     f" —— 日报在，但微信收不到"))
            out.append(f"  {SLOT_CN[slot]}  ✗ 推送失败（{detail}）")
        else:
            # 查不到状态有两种可能：该日早于状态记录上线（正常），或 M6 没写 /
            # 没部署上去（有问题）。用 STATUS_SINCE 把两者分开，避免误报。
            old = date_str < STATUS_SINCE
            tail = "（该日早于状态记录上线，属预期）" if old else ""
            problems.append((WARN if old else SEVERE,
                             f"{SLOT_CN[slot]}推送状态**无法判定**（{detail}）"
                             f" —— 该时段推送可能没发生{tail}"))
            out.append(f"  {SLOT_CN[slot]}  ? 无法判定（{detail}）{tail}")

    # 3：外部触发是否还在工作
    out.append("\n触发源：")
    disp = events.get("workflow_dispatch", 0)
    out.append(f"  workflow_dispatch {disp} 次　schedule {events.get('schedule', 0)} 次")
    if disp == 0:
        problems.append((WARN, "当天没有任何外部触发（workflow_dispatch）—— "
                              "cron-job.org / 本机计划任务这条准点通路可能已失效，"
                              "日报只能等延迟 4~5 小时的 schedule 兜底"))
        out.append("  ⚠ 没有任何外部触发")
    else:
        out.append("  ✓ 外部触发在工作")

    # 4：出报次数（去重是否生效）
    if produced_n > EXPECTED_PRODUCED:
        problems.append((WARN, f"当天真正出报 {produced_n} 次（预期 {EXPECTED_PRODUCED} 次）"
                              f" —— 去重可能没挡住，有重复推送风险"))
    line = f"\n真正出报 {produced_n} 次 / 预期 {EXPECTED_PRODUCED} 次"
    if produced_n > EXPECTED_PRODUCED:
        line += "　⚠ 偏多"
    if unknown_n:
        line += f"（另有 {unknown_n} 次产物已过 7 天保留期，判不出，未计入）"
    out.append(line)
    return out, problems


# ---------------------------------------------------------------- 告警

def notify(problems, date_str, sendkey):
    """有问题时推一条微信。标题控制在 Server酱 的 32 字符上限内。"""
    try:
        import push as m6_push
    except Exception as e:
        print(f"[warn] 无法加载 M6 推送模块（{e}），跳过通知")
        return
    head = [f"[{lv}] {txt}" for lv, txt in problems[:4]]
    title = f"⚠️ 日报体检 {date_str[5:]}：{len(problems)} 项异常"
    desp = ("### 送达体检发现问题\n\n" + "\n\n".join(f"- {h}" for h in head)
            + ("\n\n（还有更多，见 Actions 日志）" if len(problems) > 4 else ""))
    ok, msg = m6_push.send(title, desp, sendkey)
    print(f"[{'OK' if ok else 'warn'}] 告警推送{'成功（' + msg + '）' if ok else '失败（' + msg + '）'}")


# ---------------------------------------------------------------- 入口

def main():
    ap = argparse.ArgumentParser(description="日报送达体检：把静默失败查出来")
    ap.add_argument("--date", default="",
                    help="要体检的日期 YYYY-MM-DD；默认昨天（北京时间）")
    ap.add_argument("--days", type=int, default=1, help="从 --date 起往前连看几天")
    ap.add_argument("--notify", action="store_true", help="发现问题时推微信")
    ap.add_argument("--repo", default="", help="owner/repo，默认从 git remote 推断")
    args = ap.parse_args()

    repo = args.repo or observe.detect_repo()
    owner, name = repo.split("/")
    pages_base = f"https://{owner.lower()}.github.io/{name}"

    # 目标日 = 「最近一个盘后时段已经过去的日子」：16:00 之后取今天，之前取昨天。
    #
    # 不能简单地固定取「今天」或「昨天」——本 workflow 自己的 schedule 同样会被
    # 延迟投递数小时，22:00 的计划很可能次日 02:00 才执行。固定取今天，延迟那次
    # 就会去体检一个还没结束的日子；固定取昨天，准时那次又会永远慢一天。
    # 按这条规则，无论延迟到几点，选中的都是同一个日子，结论稳定。
    if args.date:
        try:
            end = datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            raise SystemExit(f"[FAIL] 日期格式应为 YYYY-MM-DD：{args.date}")
    else:
        _now = datetime.now(CST)
        end = _now if _now.hour >= PM_SETTLED_HOUR else _now - timedelta(days=1)

    dates = [(end - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(args.days)]
    print(f"送达体检 · {repo}")
    print(f"目标日期：{', '.join(dates)}")
    print("API 令牌：" + ("已配置（限额 5000 次/小时）" if os.environ.get("GITHUB_TOKEN")
                        else "未配置（限额仅 60 次/小时，连查多天可能撞上限流）"))

    all_problems = []
    for date_str in dates:
        try:
            out, problems = check_date(repo, pages_base, date_str)
        except Exception as e:
            print(f"\n[FAIL] {date_str} 体检过程出错：{type(e).__name__}: {e}")
            all_problems.append((SEVERE, f"{date_str} 体检本身失败：{e}"))
            continue
        print("\n".join(out))
        for lv, txt in problems:
            all_problems.append((lv, f"{date_str[5:]} {txt}"))

    severe = [p for p in all_problems if p[0] == SEVERE]
    print(f"\n{'=' * 74}")
    if all_problems:
        print(f"结论：发现 {len(all_problems)} 项问题（严重 {len(severe)}）")
        for lv, txt in all_problems:
            print(f"  [{lv}] {txt}")
    else:
        print("结论：一切正常 ✓（两份日报都已送达，准点，推送成功）")
    print("=" * 74)

    if all_problems and args.notify:
        sendkey = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
        if sendkey:
            notify(all_problems, dates[0], sendkey)
        else:
            print("[warn] SERVERCHAN_SENDKEY 未设置，跳过告警推送")

    return 1 if severe else 0


if __name__ == "__main__":
    sys.exit(main())
