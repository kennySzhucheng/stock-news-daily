# -*- coding: utf-8 -*-
"""经 GitHub API 推送本地提交（github.com 被墙时的替代路径）

背景：国内 github.com 的 443 时常不通（`git push` 报 Connection was reset /
Could not connect），但 api.github.com 一直可直连，`gh` 命令也能用。
Git Data API 可以完整复刻一次 push：建 blob → 建 tree → 建 commit → 更新 ref。

与 `git push` 的差异：远程会多出一个新的 commit SHA（内容相同，SHA 不同）。
梯子恢复后执行 `git fetch && git reset --hard origin/main` 即可对齐。

用法:
    python tools/push_via_api.py              # 推送本地 HEAD 到 origin 的默认分支
    python tools/push_via_api.py --dry-run    # 只列出会推送哪些文件
    python tools/push_via_api.py --branch dev # 推到别的分支
"""
import argparse
import base64
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

# 仓库根目录 = 本文件的上一级
BASE = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def sh(*args):
    r = subprocess.run(args, cwd=str(BASE), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    return r.stdout.strip()


def api(method, path, body=None, token="", retries=4):
    """调 GitHub API。国内直连 api.github.com 时常抖动，
    表现为 RemoteDisconnected / 超时，故失败后退避重试。"""
    import time
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(
            f"https://api.github.com{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "User-Agent": "push-via-api"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            # 4xx 是请求本身的问题，重试没意义
            if 400 <= e.code < 500:
                raise SystemExit(f"[FAIL] {method} {path} -> HTTP {e.code}\n{detail}")
            last = f"HTTP {e.code}: {detail}"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        if attempt < retries - 1:
            wait = 2 * (attempt + 1)
            print(f"  [retry] {method} {path} 失败（{last[:60]}），{wait}s 后重试")
            time.sleep(wait)
    raise SystemExit(f"[FAIL] {method} {path} 重试 {retries} 次仍失败：{last}")


def find_gh():
    """Windows 上 Python 的 PATH 与 Git Bash 不同，gh 常要用完整路径"""
    for p in (r"C:\Program Files\GitHub CLI\gh.exe",
              r"C:\Program Files (x86)\GitHub CLI\gh.exe"):
        if Path(p).exists():
            return p
    return "gh"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--branch", default="main")
    args = ap.parse_args()

    token = subprocess.run([find_gh(), "auth", "token"], capture_output=True,
                           text=True).stdout.strip()
    if not token:
        raise SystemExit("[FAIL] 取不到 gh token，先跑 gh auth login")

    url = sh("git", "remote", "get-url", "origin")
    if "github.com" not in url:
        raise SystemExit(f"[FAIL] origin 不是 GitHub 地址: {url}")
    repo = url.split("github.com")[1].lstrip(":/").removesuffix(".git")
    print(f"仓库: {repo}  分支: {args.branch}")

    remote_sha = api("GET", f"/repos/{repo}/git/ref/heads/{args.branch}",
                     token=token)["object"]["sha"]
    # 注意：Git Data API 的 /git/commits/{sha} 把 message/tree 放在顶层，
    # 与常见的 /commits/{sha}（嵌在 commit 字段下）不是一回事
    remote = api("GET", f"/repos/{repo}/git/commits/{remote_sha}", token=token)
    remote_tree = remote["tree"]["sha"]
    local_sha = sh("git", "rev-parse", "HEAD")
    local_tree = sh("git", "rev-parse", "HEAD^{tree}")

    print(f"远程 {remote_sha[:8]}  {remote['message'].splitlines()[0][:44]}")
    print(f"本地 {local_sha[:8]}  {sh('git','log','-1','--pretty=%s')[:44]}")

    # 用文件树判断是否一致，而不是提交 SHA。
    # 经本工具推过的分支，远程 SHA 本地并不存在，git 的祖先判断用不了。
    dirty = sh("git", "status", "--porcelain")
    if remote_tree == local_tree:
        print("\n[OK] 两边文件内容完全一致，无需推送")
        if remote_sha != local_sha:
            print(f"     （仅提交 SHA 不同；梯子恢复后 git fetch && "
                  f"git reset --hard origin/{args.branch} 可对齐）")
        if dirty:
            print("\n[warn] 工作区还有未提交的改动，本次推送不包含它们：")
            for line in dirty.splitlines()[:10]:
                print(f"       {line}")
        return 0

    # 本地 HEAD 的完整文件清单
    local_files = {}
    for line in sh("git", "ls-tree", "-r", "HEAD").splitlines():
        meta, path = line.split("\t", 1)
        mode, _typ, sha = meta.split()
        local_files[path] = (mode, sha)

    # 远程树里本地没有的文件 = 需要删除的
    remote_tree_data = api(
        "GET", f"/repos/{repo}/git/trees/{remote_tree}?recursive=1", token=token)
    remote_files = {e["path"]: (e["mode"], e["sha"])
                    for e in remote_tree_data.get("tree", []) if e["type"] == "blob"}

    changed = sorted(p for p, v in local_files.items() if remote_files.get(p) != v)
    deleted = sorted(p for p in remote_files if p not in local_files)

    print(f"\n待推送 {len(changed)} 个文件，删除 {len(deleted)} 个：")
    for p in changed:
        print(f"  M  {p}"
              if p in remote_files else f"  A  {p}")
    for p in deleted:
        print(f"  D  {p}")

    # 远程若有本地历史里没有的提交，说明两边分叉，不做覆盖
    known = subprocess.run(["git", "cat-file", "-e", f"{remote_sha}^{{commit}}"],
                           cwd=str(BASE), capture_output=True).returncode == 0
    if not known:
        print(f"\n[warn] 远程 {remote_sha[:8]} 不在本地历史中（多半是之前经 API 推的）。")
        print("       本次推送会让远程文件树与本地 HEAD 完全一致；")
        print("       若确认本地是最新的，继续即可。")

    if args.dry_run:
        print("\n（--dry-run，未改动远程）")
        return 0

    entries = []
    for path in changed:
        mode = local_files[path][0]
        content = (BASE / path).read_bytes()
        blob = api("POST", f"/repos/{repo}/git/blobs",
                   {"content": base64.b64encode(content).decode(), "encoding": "base64"},
                   token=token)
        entries.append({"path": path, "mode": mode, "type": "blob", "sha": blob["sha"]})
    for path in deleted:
        # GitHub 用 sha=null 表示在树里删除该路径
        entries.append({"path": path, "mode": remote_files[path][0],
                        "type": "blob", "sha": None})

    tree = api("POST", f"/repos/{repo}/git/trees",
               {"base_tree": remote_tree, "tree": entries}, token=token)

    message = sh("git", "log", "-1", "--pretty=%B")
    ident = {"name": sh("git", "config", "user.name") or "kennySzhucheng",
             "email": sh("git", "config", "user.email") or "kennyS_li@163.com"}
    commit = api("POST", f"/repos/{repo}/git/commits",
                 {"message": message, "tree": tree["sha"], "parents": [remote_sha],
                  "author": ident, "committer": ident}, token=token)

    api("PATCH", f"/repos/{repo}/git/refs/heads/{args.branch}",
        {"sha": commit["sha"], "force": False}, token=token)

    print(f"\n[OK] 已推送 -> {commit['sha'][:8]}")
    print(f"     本地 {local_sha[:8]} 与远程 {commit['sha'][:8]} SHA 不同但内容一致")
    print(f"     梯子恢复后执行：git fetch && git reset --hard origin/{args.branch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
