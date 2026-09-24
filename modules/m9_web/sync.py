# -*- coding: utf-8 -*-
"""M9 云端产出同步 — 把 GitHub Pages 上最新的网页版产物拉到本地

用途：GitHub Actions 每天 08:10 / 15:40 各跑一次并部署，本地不必重跑流水线
（约 6 分钟 + API 费用）也能看到当天最新的内容。这里只负责取回来。

同步后本地有两种看法：
  1. 直接双击 `reports/web/index.html` —— 静态版，不含 AI 追问
  2. `python modules/m9_web/server.py --source export` —— 起本地服务，
     功能齐全，含 AI 追问（需要 DEEPSEEK_API_KEY）

用法:
    python modules/m9_web/sync.py            # 同步最新产出到 reports/web/
    python modules/m9_web/sync.py --list     # 只报告云端状态，不下载
    python modules/m9_web/sync.py --no-reports   # 只同步网页版，不拉历史日报
    python modules/m9_web/sync.py --days 5   # 只拉最近 5 天的日报（默认 10 天）
    python modules/m9_web/sync.py --base https://example.github.io/repo
"""
import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE.parent.parent
OUT_DIR = BASE / "reports" / "web"
API_DIR = OUT_DIR / "api"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

UA = {"User-Agent": "Mozilla/5.0"}

# 与 export.py 的产物保持一致
PAGE_FILES = ["index.html", "style.css", "app.js"]
# 漏加会被下面第 150 行附近的孤儿清理当成陌生文件删掉，表现为「同步后候选页空」
API_FILES = ["meta", "overview", "news", "raw", "quotes", "boards", "analysis",
             "history", "picks"]

_EXPORT_RE = re.compile(r'window\.__DATA__\["([^"]+)"\]\s*=\s*(.*?)\s*;\s*$', re.S)


def _open(url, timeout=30):
    """优先直连，失败回退系统代理。GitHub Pages 国内可直连。"""
    last_err = None
    for use_proxy in (False, True):
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({} if not use_proxy else None))
        try:
            return opener.open(urllib.request.Request(url, headers=UA), timeout=timeout)
        except Exception as e:
            last_err = e
    raise last_err


def detect_base():
    """从 git remote 推断 Pages 根地址"""
    try:
        r = subprocess.run(["git", "remote", "get-url", "origin"],
                           capture_output=True, text=True, cwd=str(BASE), timeout=10)
        m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", r.stdout.strip())
        if m:
            owner, name = m.group(1).split("/")[:2]
            return f"https://{owner.lower()}.github.io/{name}"
    except Exception:
        pass
    return ""


def fetch(url):
    with _open(url) as r:
        return r.read()


def parse_payload(text):
    m = _EXPORT_RE.search(text)
    if not m:
        return None, None
    try:
        return m.group(1), json.loads(m.group(2))
    except json.JSONDecodeError:
        return None, None


def main():
    ap = argparse.ArgumentParser(description="同步云端网页版产物到本地")
    ap.add_argument("--base", default="", help="Pages 根地址，默认从 git remote 推断")
    ap.add_argument("--list", action="store_true", help="只报告云端状态，不下载")
    ap.add_argument("--no-reports", action="store_true", help="不拉历史日报 HTML")
    ap.add_argument("--days", type=int, default=10, help="拉最近几天的日报（默认 10）")
    args = ap.parse_args()

    base = (args.base or detect_base()).rstrip("/")
    if not base:
        print("[FAIL] 无法确定 Pages 地址，请用 --base 指定")
        return 1
    web_base = f"{base}/web"
    print(f"云端地址: {web_base}")

    # 先探一次 meta：既确认可达，也拿到数据日期
    try:
        text = fetch(f"{web_base}/api/meta.js").decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"[FAIL] 云端还没有 /web/ 产物（HTTP 404）")
            print("       网页版是在 M9 加入工作流之后才随日报一起部署的。")
            print("       等下一次定时运行跑完，或到 Actions 页面手动触发一次 daily.yml。")
        else:
            print(f"[FAIL] 拉取失败：HTTP {e.code}")
        return 1
    except Exception as e:
        print(f"[FAIL] 拉取失败：{type(e).__name__}: {str(e)[:80]}")
        print("       线上地址国内可直连；若开了梯子但规则未覆盖 github.io，反而可能不通，")
        print("       可先关掉代理再试。")
        return 1
    _, meta = parse_payload(text)
    if not meta:
        print("[FAIL] 云端 meta 数据格式异常（可能部署产物不完整）")
        return 1

    print(f"云端数据日期: {meta.get('date')} · 结构化新闻 {meta.get('news_count')} 条")

    if args.list:
        print("\n（--list 模式，未下载）")
        return 0

    API_DIR.mkdir(parents=True, exist_ok=True)
    ok = fail = 0

    for name in PAGE_FILES:
        try:
            (OUT_DIR / name).write_bytes(fetch(f"{web_base}/{name}"))
            ok += 1
        except Exception as e:
            fail += 1
            print(f"  [warn] {name}: {str(e)[:60]}")

    for name in API_FILES:
        try:
            data = fetch(f"{web_base}/api/{name}.js")
            (API_DIR / f"{name}.js").write_bytes(data)
            ok += 1
        except Exception as e:
            fail += 1
            print(f"  [warn] api/{name}.js: {str(e)[:60]}")

    # 清掉本地残留但云端已没有的数据文件，避免本地服务读到过期数据
    valid = {f"{n}.js" for n in API_FILES}
    for p in list(API_DIR.glob("*.js")) + list(API_DIR.glob("*.json")):
        if p.name not in valid:
            p.unlink()

    # 历史日报：拉回本地 reports/，这样本地服务的「历史」页点开就是本地文件，
    # 不必依赖线上可达（也更省流量）。只取最近若干天，避免长期堆积。
    n_reports = 0
    if not args.no_reports:
        try:
            hist_text = (API_DIR / "history.js").read_text(encoding="utf-8")
            _, hist = parse_payload(hist_text)
            days = (hist or {}).get("reports", [])[:max(args.days, 1)]
            for day in days:
                for entry in day.get("entries", []):
                    fname = entry.get("file", "")
                    if not fname or (BASE / "reports" / fname).exists():
                        continue
                    try:
                        (BASE / "reports" / fname).write_bytes(fetch(f"{base}/{fname}"))
                        n_reports += 1
                    except Exception as e:
                        print(f"  [warn] {fname}: {str(e)[:60]}")
        except Exception as e:
            print(f"  [warn] 历史日报清单读取失败: {str(e)[:60]}")

    size = sum(p.stat().st_size for p in OUT_DIR.rglob("*") if p.is_file())
    print(f"\n[OK] 已同步 {ok} 个文件（失败 {fail}），网页版合计 {size / 1024:.0f} KB -> {OUT_DIR}")
    if not args.no_reports:
        print(f"     另拉回 {n_reports} 份历史日报 -> {BASE / 'reports'}")
    print("     看静态版：直接打开 reports/web/index.html")
    print("     看完整版：python modules/m9_web/server.py --source export")
    return 0


if __name__ == "__main__":
    sys.exit(main())
