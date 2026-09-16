# -*- coding: utf-8 -*-
"""
M8 云端产出观察工具

查询 GitHub Actions 最近运行记录，核对每次运行的状态与耗时，
并检查对应日期的日报是否已发布到线上。用于 tasks.md M8 的
「连续观察 3 天云端产出，核对时效性与稳定性」验收项。

用法：
    python modules/m8_e2e/observe.py           # 看最近 10 次运行
    python modules/m8_e2e/observe.py --limit 5

说明：
1. 公开仓库的 Actions 记录可匿名读取；设置 GITHUB_TOKEN 环境变量可提高速率限制
2. 日报文件名按**北京时间**取日期，并带盘前/盘后后缀
   （YYYY-MM-DD-am.html / YYYY-MM-DD-pm.html），同日两份互不覆盖
3. 本工具按每次运行的触发时间推断它应产出的时段，逐个核对是否已上线
"""
import os
import re
import sys
import json
import subprocess
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
CST = timezone(timedelta(hours=8))
SLOT_CN = {"am": "盘前", "pm": "盘后"}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/vnd.github+json"}


def slot_of(dt_cst):
    """与 M5/M6 相同的时段判定：北京时间 12:00 前为盘前"""
    return "am" if dt_cst.hour < 12 else "pm"


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


def _open(url, timeout=30, headers=None):
    """打开 URL。优先直连，失败后回退到系统代理。

    Windows 上 urllib 默认会读取系统代理设置，若梯子开着但节点不通，
    所有请求都会失败——而 GitHub 在国内通常可直连，故先试直连。
    """
    hdrs = dict(UA)
    if headers:
        hdrs.update(headers)
    last_err = None
    for use_proxy in (False, True):
        handler = urllib.request.ProxyHandler({} if not use_proxy else None)
        opener = urllib.request.build_opener(handler)
        try:
            req = urllib.request.Request(url, headers=hdrs)
            return opener.open(req, timeout=timeout)
        except Exception as e:
            last_err = e
    raise last_err


def get_json(url, timeout=30):
    with _open(url, timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def check_url(url, timeout=30):
    """访问 URL，返回 (状态码, 字节数或错误信息)"""
    try:
        with _open(url, timeout) as r:
            return r.status, len(r.read())
    except Exception as e:
        return None, str(e)[:60]


def main():
    limit = 10
    if "--limit" in sys.argv:
        try:
            limit = int(sys.argv[sys.argv.index("--limit") + 1])
        except (IndexError, ValueError):
            pass

    repo = detect_repo()
    owner, name = repo.split("/")
    pages_base = f"https://{owner.lower()}.github.io/{name}"

    print("=" * 74)
    print(f"云端产出观察 · {repo}")
    print(f"生成时间 {datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')} (北京时间)")
    print("=" * 74)

    try:
        # 只查日报 workflow，否则会把 Pages 自身的构建 workflow 也列进来
        d = get_json(f"https://api.github.com/repos/{repo}/actions/workflows/"
                     f"daily.yml/runs?per_page={limit}")
    except Exception as e:
        print(f"\n获取 Actions 记录失败：{e}")
        return 1

    runs = d.get("workflow_runs", [])
    if not runs:
        print("\n暂无运行记录。")
        return 0

    print(f"\n最近 {len(runs)} 次运行：\n")
    print(f"{'#':<6}{'北京时间':<14}{'触发':<18}{'状态':<12}{'耗时':<9}{'产出文件'}")
    print("-" * 74)

    by_date = {}
    for r in runs:
        created = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")).astimezone(CST)
        updated = datetime.fromisoformat(r["updated_at"].replace("Z", "+00:00")).astimezone(CST)
        dur = (updated - created).total_seconds()
        concl = r.get("conclusion") or r.get("status") or "?"
        event = r.get("event", "?")
        # 文件名用北京时间：两次定时（08:10 / 15:40 北京）分属同日不同时段
        date_str = created.strftime("%Y-%m-%d")
        slot = slot_of(created)
        fname = f"{date_str}-{slot}.html"
        by_date.setdefault(date_str, {})[slot] = created

        mark = {"success": "成功", "failure": "失败", "cancelled": "取消",
                "in_progress": "进行中", "queued": "排队中"}.get(concl, concl)
        print(f"{r['run_number']:<6}{created.strftime('%m-%d %H:%M'):<14}"
              f"{event:<18}{mark:<12}{dur:>6.0f}s   {fname}")

    print(f"\n线上日报检查（{pages_base}）：")
    for date_str in sorted(by_date, reverse=True):
        for slot in ("am", "pm"):
            fname = f"{date_str}-{slot}.html"
            url = f"{pages_base}/{fname}"
            status, info = check_url(url)
            ran = slot in by_date[date_str]
            if status == 200:
                print(f"  {fname}  OK   {info} 字节")
            elif ran:
                print(f"  {fname}  运行成功但线上不可访问  ({info})")
            else:
                print(f"  {fname}  —    今日无该时段的运行记录")
        # 同日两次都跑过、但只上线了一份，才是真问题
        both_ran = {"am", "pm"} <= set(by_date[date_str])
        if both_ran:
            ok = all(check_url(f"{pages_base}/{date_str}-{s}.html")[0] == 200
                     for s in ("am", "pm"))
            print(f"            {'✓ 盘前/盘后两份均已保留' if ok else '⚠ 两份运行齐全但线上缺一份'}")

    # 索引页与网页版
    status, info = check_url(f"{pages_base}/index.html")
    print(f"  index.html  {'OK   ' + str(info) + ' 字节' if status == 200 else '不可访问  (' + str(info) + ')'}")
    status, info = check_url(f"{pages_base}/web/")
    print(f"  web/（增强版网页）  {'OK   ' + str(info) + ' 字节' if status == 200 else '不可访问  (' + str(info) + ')'}")

    # 稳定性小结
    succ = sum(1 for r in runs if r.get("conclusion") == "success")
    print(f"\n稳定性：最近 {len(runs)} 次中成功 {succ} 次，失败 {len(runs) - succ} 次")
    return 0


if __name__ == "__main__":
    sys.exit(main())
