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
   （可选，但本工具要逐条查运行产物，带令牌更稳）
2. 日报文件名按**北京时间**取日期，并带盘前/盘后后缀
   （YYYY-MM-DD-am.html / YYYY-MM-DD-pm.html），同日两份互不覆盖
3. 时段**不按触发时刻推断**，而是读运行产物的名字——workflow 把时段写进了
   artifact：daily-data-<run_number>-<slot>。按触发时刻推在延迟场景下必错：
   盘前 cron 可能 12:4x 才跑，会被算成盘后；且去重跳过的运行与真正产出的
   运行光看时间完全分不出来（2026-09-21 那天两次都会被算成 pm）。
   产物由 daily.yml 的「上传产物」步骤生成，保留 7 天。
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
STATUS_CN = {"success": "成功", "failure": "失败", "cancelled": "取消",
             "in_progress": "进行中", "queued": "排队中", "skipped": "跳过"}

# 运行产物名由 daily.yml 生成：daily-data-<run_number>-<slot>
ART_RE = re.compile(r"^daily-data-\d+-(am|pm)$")
# 2026-09-24 之前的老命名（不带时段），只用于识别「有产出但时段未知」
ART_RE_LEGACY = re.compile(r"^daily-data-\d+$")
# 与 daily.yml 里 upload-artifact 的 retention-days 保持一致
ART_RETAIN_DAYS = 7

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/vnd.github+json"}
# 只用于 api.github.com 的请求（见 get_json）；不要加到 UA 里——UA 也被
# check_url 用来访问 github.io，把令牌发到别的域是没必要的泄露面。
_GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()


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
    """读 api.github.com 的 JSON。带令牌可提高速率限制（可选）。"""
    headers = {"Authorization": f"Bearer {_GITHUB_TOKEN}"} if _GITHUB_TOKEN else None
    with _open(url, timeout, headers) as r:
        return json.loads(r.read().decode("utf-8"))


def run_output(run):
    """判断一次运行实际产出了哪个时段的日报，返回 (slot, 是否产出, 说明)。

    slot 非 None 时表示产出了该时段；slot 为 None 但 produced=True 表示确实出了报、
    只是产物名是 2026-09-24 之前的旧命名（不带时段），推不出是哪个时段；
    produced=False 时说明字符串是给表格用的文字。

    为什么读产物名而不是触发时刻：运行对象里既没有 inputs 也没有 schedule 字段，
    按「触发时刻是否过 12:00」推时段在延迟场景下必错 —— 2026-09-21 盘前那份是
    12:48 才跑的 cron 产出的，会被算成盘后，印出来的文件名是错的；而且那一整天
    两次运行都会被算成 pm，后者覆盖前者，令「盘前/盘后两份均已保留」的汇总判定
    永远不触发。改为读 daily.yml 写进 artifact 名的时段后就没这个问题。

    判据：运行成功 + 有产物 = 真正出报；运行成功但没有产物 = 被 workflow 的
    去重步骤挡下了（那一步已把上传产物一并跳过）；运行失败 = 未产出。
    """
    concl = run.get("conclusion")
    if concl and concl != "success":
        return None, False, f"—  {STATUS_CN.get(concl, concl)}，未产出"

    created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
    if (datetime.now(timezone.utc) - created).days >= ART_RETAIN_DAYS:
        return None, False, "—  运行过久，产物已过保留期，无法判定"

    try:
        d = get_json(run["artifacts_url"])
    except Exception as e:
        return None, False, f"—  查产物失败（{str(e)[:30]}）"
    names = [a.get("name", "") for a in d.get("artifacts", [])]
    for name in names:
        m = ART_RE.match(name)
        if m:
            return m.group(1), True, ""
    # 2026-09-24 之前产物名不带时段：确实出了报，但判不出是哪个时段
    if any(ART_RE_LEGACY.match(n) for n in names):
        return None, True, "—  有产出（旧版产物名，时段未知）"
    return None, False, "—  无产出（该时段已出报，被去重跳过）"


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
    print(f"{'#':<6}{'北京时间':<13}{'触发':<18}{'状态':<8}{'耗时':<8}{'本次产出'}")
    print("-" * 78)

    by_date = {}
    produced_n = skipped_n = 0
    for r in runs:
        created = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")).astimezone(CST)
        updated = datetime.fromisoformat(r["updated_at"].replace("Z", "+00:00")).astimezone(CST)
        dur = (updated - created).total_seconds()
        concl = r.get("conclusion") or r.get("status") or "?"
        event = r.get("event", "?")
        # 文件名用北京时间：两次定时（08:10 / 15:40 北京）分属同日不同时段
        date_str = created.strftime("%Y-%m-%d")

        slot, produced, note = run_output(r)
        if produced:
            produced_n += 1
        if slot:
            by_date.setdefault(date_str, {})[slot] = created
            shown = f"{date_str}-{slot}.html（{SLOT_CN[slot]}）"
        else:
            shown = note
            if not produced and concl == "success":
                skipped_n += 1

        mark = STATUS_CN.get(concl, concl)
        print(f"{r['run_number']:<6}{created.strftime('%m-%d %H:%M'):<13}"
              f"{event:<18}{mark:<8}{dur:>6.0f}s  {shown}")

    # 线上文件逐个查。日期取自运行记录本身，**不能**取自 by_date——产物名是旧命名
    # （时段未知）时 by_date 是空的，那样整段检查会一行都不打印。
    dates = sorted({datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
                    .astimezone(CST).strftime("%Y-%m-%d") for r in runs}, reverse=True)
    print(f"\n线上日报检查（{pages_base}）：")
    for date_str in dates:
        known = by_date.get(date_str, {})   # 已判定出时段的产出记录
        for slot in ("am", "pm"):
            fname = f"{date_str}-{slot}.html"
            status, info = check_url(f"{pages_base}/{fname}")
            if status == 200:
                print(f"  {fname}  OK   {info} 字节")
            elif slot in known:
                print(f"  {fname}  该时段运行成功但线上不可访问  ({info})")
            else:
                print(f"  {fname}  线上无此文件  ({info})")
        # 同日两个时段都确认产出过、但只上线了一份，才是真问题
        if {"am", "pm"} <= set(known):
            ok = all(check_url(f"{pages_base}/{date_str}-{s}.html")[0] == 200
                     for s in ("am", "pm"))
            print(f"            {'✓ 盘前/盘后两份均已保留' if ok else '⚠ 两份运行齐全但线上缺一份'}")

    # 索引页与网页版
    status, info = check_url(f"{pages_base}/index.html")
    print(f"  index.html  {'OK   ' + str(info) + ' 字节' if status == 200 else '不可访问  (' + str(info) + ')'}")
    status, info = check_url(f"{pages_base}/web/")
    print(f"  web/（增强版网页）  {'OK   ' + str(info) + ' 字节' if status == 200 else '不可访问  (' + str(info) + ')'}")

    # 稳定性小结。注意「成功」包含被去重跳过的运行——它们结论同样是 success，
    # 所以单看成功率会高估产出次数；真正的产出次数看上面按产物判定出来的计数。
    succ = sum(1 for r in runs if r.get("conclusion") == "success")
    print(f"\n稳定性：最近 {len(runs)} 次中成功 {succ} 次，失败 {len(runs) - succ} 次")
    print(f"产出：真正出报 {produced_n} 次，被去重跳过 {skipped_n} 次")
    return 0


if __name__ == "__main__":
    sys.exit(main())
