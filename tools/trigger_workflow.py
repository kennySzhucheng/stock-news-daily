# -*- coding: utf-8 -*-
"""按点触发云端日报工作流（绕开 GitHub cron 的延迟投递）

背景：GitHub Actions 的 schedule 事件会被延迟投递——2026-09-17 实测
盘前（计划 08:10）实际 12:46 才开始，盘后（计划 15:40）实际 20:45 才开始，
延迟 4~5 小时。本工具从外部按点调用 workflow_dispatch，绕开这个队列。

令牌（按优先级取第一个可用的）：
  1. `--token` 参数
  2. `GITHUB_TOKEN` / `GH_PAT` 环境变量
  3. **本机 `gh auth token`** —— 默认路径，什么都不用配（本机计划任务走这条）

  只有想用一枚独立的 fine-grained PAT 时才需要 1/2：
  Repository 选本仓库，Permissions 用 Actions = Read and write。

用法:
    python tools/trigger_workflow.py                    # 按当前时间自动判定时段
    python tools/trigger_workflow.py --slot am
    python tools/trigger_workflow.py --dry-run          # 只打印，不触发
    python tools/trigger_workflow.py --force            # 跳过"近期已有运行"的检查
    python tools/trigger_workflow.py --wait             # 触发后等待运行结束并打印结果
    python tools/trigger_workflow.py --print-task-cmd   # 打印 Windows 计划任务注册命令
    python tools/trigger_workflow.py --check            # 只验证令牌能不能用，不触发运行

查重：默认若「今天的这个时段」已经出过报告（查 gh-pages 上的产出文件），
      就不再触发（`--force` 可忽略）。这是为了避免与云端 cron 撞车——
      cron 可能延迟数小时后在同日重复跑一遍。
      按「日期 + 时段」判断，而不是「最近 N 小时跑过没有」：后者不分时段，
      会把另一个时段刚跑完的运行误判成重复——2026-09-17 盘前 cron 延迟到
      12:46 才跑，15:40 触发盘后时查到「近 6 小时跑过」就 skip 了，盘后只能
      退回等延迟的 cron，结果 20:53 才推。

若改用云端外部调度器（如 cron-job.org），不必用本脚本，直接发一个请求即可：
    curl -X POST https://api.github.com/repos/kennySzhucheng/stock-news-daily/actions/workflows/daily.yml/dispatches \\
         -H "Authorization: Bearer $GITHUB_TOKEN" \\
         -H "Accept: application/vnd.github+json" \\
         -d '{"ref":"main","inputs":{"slot":"am"}}'
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CST = timezone(timedelta(hours=8))
WORKFLOW = "daily.yml"
SLOT_CN = {"am": "盘前", "pm": "盘后"}


def detect_repo():
    """从 git remote 推断 owner/repo，失败则回退到默认值"""
    try:
        r = subprocess.run(["git", "remote", "get-url", "origin"],
                           capture_output=True, text=True, cwd=str(BASE), timeout=10)
        m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", r.stdout.strip())
        if m:
            return m.group(1)
    except Exception:
        pass
    return "kennySzhucheng/stock-news-daily"


def api(method, path, body=None, token="", retries=3):
    """调 GitHub API。优先直连（Windows 系统代理可能指向已挂掉的节点）。"""
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(
            f"https://api.github.com{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "User-Agent": "trigger-workflow"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=30) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            if 400 <= e.code < 500:
                raise SystemExit(f"[FAIL] {method} {path} -> HTTP {e.code}\n{detail}")
            last = f"HTTP {e.code}: {detail}"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        if attempt < retries - 1:
            time.sleep(2 * (attempt + 1))
    raise SystemExit(f"[FAIL] {method} {path} 重试 {retries} 次仍失败：{last}")


def report_exists(repo, token, date_str, slot):
    """查 gh-pages 上今天的这个时段是否已经出过报告。

    按「日期 + 时段」判断，而不是「最近 N 小时跑过没有」——后者不分时段，
    会把另一个时段刚跑完的运行误当成重复：2026-09-17 盘前 cron 延迟到 12:46
    才跑，15:40 触发盘后时查到「近 6 小时跑过」就 skip 了，盘后只能退回等
    延迟的 cron，结果 20:53 才推。

    返回 True=已出报，False=未出报，None=查询失败（调用方按未出报放行）。
    """
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/contents/{date_str}-{slot}.html?ref=gh-pages",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "trigger-workflow"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=30):
            return True
    except urllib.error.HTTPError as e:
        return False if e.code == 404 else None
    except Exception:
        return None


PROBE_WORKFLOW = "__token-check-does-not-exist__.yml"


def check_token(repo, token):
    """验证令牌能否通过 dispatch 端点的鉴权——**不会真的触发运行**。

    原理：GitHub 先鉴权、后查 workflow 文件。所以往一个**不存在的 workflow**
    打 dispatch：鉴权通过得到 404（文件不存在），鉴权失败则是 401。
    于是不触发任何运行也能验令牌——正是 cron-job.org 那种静默失败的场景。

    判据（2026-09-24 实测）：
        404 Not Found               → 令牌有效，鉴权已通过
        401 Bad credentials         → 令牌无效/已撤销/值被粘坏（多空格、重复 Bearer 前缀）
        401 Requires authentication → Authorization 头压根没发出去
        403                         → 令牌有效，但缺 Actions: Read and write 权限
        204                         → 不可能出现；真出现说明假文件名撞上了真实 workflow
    """
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/workflows/{PROBE_WORKFLOW}/dispatches",
        method="POST", data=json.dumps({"ref": "main"}).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json",
                 "User-Agent": "trigger-workflow"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=30) as r:
            code, body = r.status, ""
    except urllib.error.HTTPError as e:
        code, body = e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[FAIL] 探测请求本身失败：{type(e).__name__}: {e}")
        return None

    kind = ("fine-grained PAT" if token.startswith("github_pat_")
            else "gh CLI 令牌" if token.startswith("gho_")
            else "classic PAT" if token.startswith("ghp_") else "未知类型")
    print(f"令牌：{kind}　长度 {len(token)}　开头 {token[:14]}…　结尾 …{token[-6:]}")
    print("      （拿指纹去和 cron-job.org 里存的值对一下，能看出是不是同一枚）")
    try:
        msg = json.loads(body).get("message", "")
    except Exception:
        msg = body[:120]

    if code == 404:
        print("[OK] 令牌有效 —— 鉴权已通过")
        print("     （404 是刻意用的假文件名，属预期；本次没有触发任何运行）")
        return True
    if code == 401:
        print(f"[FAIL] 令牌无效：HTTP 401 {msg}")
        print("      Bad credentials          → 值不对：已撤销/换过、多了空格、重复 Bearer 前缀")
        print("      Requires authentication  → Authorization 头根本没发出去")
        return False
    if code == 403:
        print(f"[FAIL] 鉴权通过但权限不足：HTTP 403 {msg}")
        print("     到 PAT 设置里把 Actions 改成 Read and write")
        return False
    print(f"[??] 未预料的响应：HTTP {code} {msg}")
    return None


def wait_for_run(repo, token, before_ids, timeout=900):
    """等待新出现一次运行并跑到结束。before_ids 为触发前已有的 run id 集合。"""
    deadline = time.time() + timeout
    target = None
    while time.time() < deadline:
        data = api("GET", f"/repos/{repo}/actions/workflows/{WORKFLOW}/runs?per_page=5",
                   token=token)
        for r in data.get("workflow_runs", []):
            if r["id"] not in before_ids and r["event"] == "workflow_dispatch":
                target = r
                break
        if target:
            break
        time.sleep(10)
    if not target:
        print("[warn] 未等到新运行出现（可能仍在排队）")
        return
    print(f"[OK] 新运行已创建: {target['id']}  {target['html_url']}")
    while time.time() < deadline:
        r = api("GET", f"/repos/{repo}/actions/runs/{target['id']}", token=token)
        status, conclusion = r.get("status"), r.get("conclusion")
        if status == "completed":
            mark = "成功" if conclusion == "success" else ("失败" if conclusion else str(conclusion))
            print(f"[{'OK' if conclusion == 'success' else 'FAIL'}] 运行结束: {mark} "
                  f"（{target['html_url']}）")
            return
        time.sleep(15)
    print("[warn] 等待超时，运行仍在进行中")


def gh_token():
    """从 `gh auth token` 取令牌。

    本机计划任务靠这个：令牌只在本机、由 gh 自己保管，既不必 setx 到环境变量，
    也不必新建 PAT。Windows 上 Python 的 PATH 与 Git Bash 不同，故先试完整路径。
    """
    for exe in (r"C:\Program Files\GitHub CLI\gh.exe",
                r"C:\Program Files (x86)\GitHub CLI\gh.exe", "gh"):
        try:
            r = subprocess.run([exe, "auth", "token"], capture_output=True,
                               text=True, timeout=20)
        except Exception:
            continue
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return ""


def print_task_cmd(script_path):
    """打印 Windows 计划任务的注册命令（PowerShell）

    用 `Register-ScheduledTask` 而非 `schtasks /Create`：后者没有关掉
    「用电池时不执行」的开关（DisallowStartIfOnBatteries 默认 True），
    笔记本拔电或合盖时会静默漏触发，且不报错。
    """
    py = sys.executable
    log_dir = Path.home() / ".stock-news-daily"
    print("令牌：默认自动用本机 `gh auth token`，不必设置任何环境变量；")
    print("      若要改用别的令牌，设 GITHUB_TOKEN 环境变量或用 --token 传入。")
    print()
    print(f'在 PowerShell 里执行（先建目录 New-Item -ItemType Directory -Force "{log_dir}"）：')
    print()
    for slot, hhmm, cn in (("am", "08:10", "盘前"), ("pm", "15:40", "盘后")):
        log = log_dir / f"trigger-{slot}.log"
        print(f'    # {cn} {hhmm}')
        print(f'    $a = New-ScheduledTaskAction -Execute "{py}" -Argument '
              f'\'"{script_path}" --slot {slot} --log-file "{log}"\'')
        print(f'    $t = New-ScheduledTaskTrigger -Daily -At {hhmm}')
        print('    $s = New-ScheduledTaskSettingsSet -StartWhenAvailable '
              '-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries '
              '-ExecutionTimeLimit (New-TimeSpan -Minutes 15)')
        print(f'    Register-ScheduledTask -TaskName "股市情报-{cn}" '
              f'-Action $a -Trigger $t -Settings $s -Force')
        print()
    print("查看/删除：")
    print('    Get-ScheduledTask -TaskName "股市情报-*" | ft TaskName, State')
    print('    Get-ScheduledTaskInfo -TaskName "股市情报-盘前"      # 看下次运行时间与上次结果')
    print(f'    Get-Content "{log_dir / "trigger-am.log"}" -Encoding UTF8')
    print('    Unregister-ScheduledTask -TaskName "股市情报-盘前" -Confirm:$false')
    print()
    print("说明：上面命令里的 python 取自当前执行本脚本的解释器；"
          "若你平时用别的 python，替换成对应路径或命令即可。")
    print("限制：任务以「交互式登录」身份运行，故要求电脑开着**且已登录**；"
          "08:10 机器关着或处于注销状态时该次不跑（云端 cron 仍会兜底，只是会延迟）。"
          "若机器只是休眠/关机错过时点，StartWhenAvailable 会在恢复后尽快补跑一次。")


def main():
    ap = argparse.ArgumentParser(description="按点触发云端日报工作流")
    ap.add_argument("--slot", choices=["am", "pm"], default="",
                    help="时段，留空则按北京时间 12:00 前/后自动判定")
    ap.add_argument("--ref", default="main", help="分支，默认 main")
    ap.add_argument("--token", default="",
                    help="令牌；不传则依次读 GITHUB_TOKEN / GH_PAT 环境变量、本机 gh auth token")
    ap.add_argument("--token-file", default="",
                    help="从文件里取令牌（自动抓 github_pat_/ghp_/gho_ 开头的串），"
                         "如 --token-file docs/keys.md；避免把密钥写进命令行历史")
    ap.add_argument("--check", action="store_true",
                    help="只验证令牌能否通过鉴权，**不触发运行**，然后退出")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要执行的动作")
    ap.add_argument("--force", action="store_true",
                    help="跳过本机侧与本时段的查重并强制重跑（会覆盖当天该时段的日报）")
    ap.add_argument("--wait", action="store_true", help="触发后等待运行结束")
    ap.add_argument("--log-file", default="",
                    help="把输出追加写入该文件（计划任务用；无控制台可看）")
    ap.add_argument("--print-task-cmd", action="store_true",
                    help="打印 Windows 计划任务注册命令后退出")
    args = ap.parse_args()

    if args.log_file:
        log = Path(args.log_file)
        log.parent.mkdir(parents=True, exist_ok=True)
        fh = open(log, "a", encoding="utf-8")
        print(f"\n===== {datetime.now(CST):%Y-%m-%d %H:%M:%S} =====", file=fh)
        sys.stdout = sys.stderr = fh

    if args.print_task_cmd:
        print_task_cmd(str(Path(__file__).resolve()))
        return

    slot = args.slot or ("am" if datetime.now(CST).hour < 12 else "pm")
    token = args.token.strip()
    if not token and args.token_file:
        try:
            txt = Path(args.token_file).read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            raise SystemExit(f"[FAIL] 读不到令牌文件 {args.token_file}：{e}")
        m = re.search(r"(github_pat_\w+|ghp_\w+|gho_\w+)", txt)
        if not m:
            raise SystemExit(f"[FAIL] {args.token_file} 里没找到 github_pat_/ghp_/gho_ 开头的令牌")
        token = m.group(1)
    token = (token or os.environ.get("GITHUB_TOKEN", "").strip()
             or os.environ.get("GH_PAT", "").strip() or gh_token())
    if not token:
        raise SystemExit("[FAIL] 未找到令牌：设 GITHUB_TOKEN / GH_PAT 环境变量、"
                         "用 --token / --token-file 传入，或先 gh auth login")

    repo = detect_repo()
    now_cst = datetime.now(CST)
    print(f"仓库: {repo}   工作流: {WORKFLOW}   分支: {args.ref}")
    print(f"时段: {slot}（{SLOT_CN[slot]}）   本地时间: {now_cst.strftime('%m-%d %H:%M')}")

    if args.check:
        check_token(repo, token)
        return

    if not args.force:
        date_str = now_cst.strftime("%Y-%m-%d")
        exists = report_exists(repo, token, date_str, slot)
        if exists is True:
            print(f"[skip] {date_str} 的{SLOT_CN[slot]}报告已存在，"
                  f"不重复触发（--force 可忽略）")
            return
        if exists is None:
            print("[warn] 查询 gh-pages 产出失败，按未出报处理，继续触发")

    if args.dry_run:
        print(f"[dry-run] 将触发 {WORKFLOW} @ {args.ref}，inputs.slot={slot}")
        return

    before = {r["id"] for r in
              api("GET", f"/repos/{repo}/actions/workflows/{WORKFLOW}/runs?per_page=5",
                  token=token).get("workflow_runs", [])}
    api("POST", f"/repos/{repo}/actions/workflows/{WORKFLOW}/dispatches",
        body={"ref": args.ref,
              "inputs": {"slot": slot, "force": "true" if args.force else "false"}},
        token=token)
    print(f"[OK] 已触发 {SLOT_CN[slot]}日报工作流（HTTP 204）")

    if args.wait:
        wait_for_run(repo, token, before)


if __name__ == "__main__":
    main()
