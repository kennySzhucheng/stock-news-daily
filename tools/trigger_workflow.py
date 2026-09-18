# -*- coding: utf-8 -*-
"""按点触发云端日报工作流（绕开 GitHub cron 的延迟投递）

背景：GitHub Actions 的 schedule 事件会被延迟投递——2026-09-17 实测
盘前（计划 08:10）实际 12:46 才开始，盘后（计划 15:40）实际 20:45 才开始，
延迟 4~5 小时。本工具从外部按点调用 workflow_dispatch，绕开这个队列。

需要一枚 Personal Access Token（fine-grained）：
  - Repository: 选本仓库
  - Permissions: Actions = Read and write, Contents = Read
  - 生成后执行 `setx GITHUB_TOKEN <token>`（用户级环境变量，计划任务可继承）

用法:
    python tools/trigger_workflow.py                    # 按当前时间自动判定时段
    python tools/trigger_workflow.py --slot am
    python tools/trigger_workflow.py --dry-run          # 只打印，不触发
    python tools/trigger_workflow.py --force            # 跳过"近期已有运行"的检查
    python tools/trigger_workflow.py --wait             # 触发后等待运行结束并打印结果
    python tools/trigger_workflow.py --print-task-cmd   # 打印 Windows 计划任务注册命令

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


def print_task_cmd(script_path):
    """打印 Windows 计划任务的注册命令（需先 setx GITHUB_TOKEN）"""
    py = sys.executable
    print("先设置令牌（只需一次，重启终端或注销后对新进程生效）：")
    print("    setx GITHUB_TOKEN <你的 PAT>")
    print()
    print("再注册两个计划任务（复制执行，/F 表示覆盖同名任务）：")
    for slot, hhmm, cn in (("am", "08:10", "盘前"), ("pm", "15:40", "盘后")):
        print(f'    schtasks /Create /TN "股市情报-{cn}" /TR '
              f'"\\"{py}\\" \\"{script_path}\\" --slot {slot}" '
              f'/SC DAILY /ST {hhmm} /F')
    print()
    print("查看/删除：")
    print('    schtasks /Query /TN "股市情报-盘前"')
    print('    schtasks /Delete /TN "股市情报-盘前" /F')
    print()
    print("说明：上面命令里的 python 取自当前执行本脚本的解释器；"
          "若你平时用别的 python，替换成对应路径或命令即可。")
    print("注意：计划任务只在电脑开机且未休眠时执行；"
          "若 08:10 机器没开，该次不会补跑（云端 cron 仍会兜底，只是会延迟）。")


def main():
    ap = argparse.ArgumentParser(description="按点触发云端日报工作流")
    ap.add_argument("--slot", choices=["am", "pm"], default="",
                    help="时段，留空则按北京时间 12:00 前/后自动判定")
    ap.add_argument("--ref", default="main", help="分支，默认 main")
    ap.add_argument("--token", default="", help="PAT；不传则读环境变量 GITHUB_TOKEN / GH_PAT")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要执行的动作")
    ap.add_argument("--force", action="store_true", help="跳过「本时段已出报」的检查")
    ap.add_argument("--wait", action="store_true", help="触发后等待运行结束")
    ap.add_argument("--print-task-cmd", action="store_true",
                    help="打印 Windows 计划任务注册命令后退出")
    args = ap.parse_args()

    if args.print_task_cmd:
        print_task_cmd(str(Path(__file__).resolve()))
        return

    slot = args.slot or ("am" if datetime.now(CST).hour < 12 else "pm")
    token = args.token or os.environ.get("GITHUB_TOKEN", "").strip() \
        or os.environ.get("GH_PAT", "").strip()
    if not token:
        raise SystemExit("[FAIL] 未找到令牌：请先 setx GITHUB_TOKEN <PAT>，"
                         "或用 --token 传入")

    repo = detect_repo()
    now_cst = datetime.now(CST)
    print(f"仓库: {repo}   工作流: {WORKFLOW}   分支: {args.ref}")
    print(f"时段: {slot}（{SLOT_CN[slot]}）   本地时间: {now_cst.strftime('%m-%d %H:%M')}")

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
        body={"ref": args.ref, "inputs": {"slot": slot}}, token=token)
    print(f"[OK] 已触发 {SLOT_CN[slot]}日报工作流（HTTP 204）")

    if args.wait:
        wait_for_run(repo, token, before)


if __name__ == "__main__":
    main()
