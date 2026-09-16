# -*- coding: utf-8 -*-
"""M9 静态导出 — 把网页版导出成纯静态站点，随日报一起部署到 GitHub Pages

导出后 reports/web/ 里没有后端，除 AI 追问外的功能全部可用
（数据来自预生成的脚本文件，筛选/排序/搜索在浏览器端完成）。

在流水线中的位置：M5 生成日报之后、部署之前运行，
这样导出的「历史」页也包含当天刚生成的那两份。

数据用 `.js`（`window.__DATA__["news"] = {...}`）而不是 `.json`：
浏览器的 CORS 策略禁止从 `file://` 发起 fetch，双击打开本地文件时
`.json` 会报 "Failed to fetch"，而 `<script src>` 不受此限制。
这样导出的页面既能挂在 Pages 上，也能直接双击用。

用法:
    python modules/m9_web/export.py                 # 导出到 reports/web/
    python modules/m9_web/export.py --out some/dir  # 自定义输出目录
"""
import os
import re
import sys
import json
import shutil
import argparse
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import aggregate  # noqa: E402

BASE = HERE.parent.parent
WEB_DIR = HERE / "web"

# 原始新闻在静态版里只保留正文前若干字：完整 1000+ 条会撑到 600KB，
# 而这个视图的用途是"看看被丢掉的是什么"，摘要足够
RAW_TEXT_LIMIT = 300


def build_payloads(bundle):
    """生成与 server.py 各 API 同构的 JSON。

    key 即前端 apiGet(name) 里的 name，保证两种运行方式数据结构完全一致。
    """
    total, items = aggregate.query_news(bundle, limit=100000)

    raw_items = []
    for n in bundle.raw_view():
        n = dict(n)
        if len(n.get("text", "")) > RAW_TEXT_LIMIT:
            n["text"] = n["text"][:RAW_TEXT_LIMIT] + "…"
        raw_items.append(n)

    return {
        "meta": {
            "date": bundle.overview()["date"],
            "analysis_ready": bool(bundle.analysis_md),
            "news_count": len(bundle.news),
            "ask_enabled": False,          # 静态版没有后端，追问不可用
        },
        "overview": bundle.overview(),
        "news": {"total": total, "items": items},
        "raw": {"items": raw_items, "total": len(raw_items)},
        "quotes": {"quotes": bundle.quotes, "failed": bundle.quotes_failed,
                   "generated_at": bundle.quotes_generated_at},
        "boards": {"boards": bundle.boards()},
        "analysis": {
            "html": bundle.analysis_html(),
            "markdown": bundle.analysis_md,
            "conclusion": bundle.conclusion(),
            "market_view": bundle.market_view(),
        },
        "history": {"reports": bundle.history()},
    }


def _js_literal(obj):
    """把对象序列化成可安全嵌入 .js 的字面量。

    U+2028 / U+2029 在 JS 字符串字面量里是换行符（ES2019 之前直接是语法错误），
    JSON 不转义它们，这里补上。
    """
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return s.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def main():
    ap = argparse.ArgumentParser(description="M9 网页版静态导出")
    ap.add_argument("--out", default=str(BASE / "reports" / "web"))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    out_dir = Path(args.out)
    api_dir = out_dir / "api"

    bundle = aggregate.Bundle()
    if not bundle.news:
        print("[warn] structured_news.json 为空或不存在，导出的页面将没有内容")

    api_dir.mkdir(parents=True, exist_ok=True)

    payloads = build_payloads(bundle)
    for name, data in payloads.items():
        p = api_dir / f"{name}.js"
        p.write_text(
            'window.__DATA__=window.__DATA__||{};'
            f'window.__DATA__["{name}"]={_js_literal(data)};',
            encoding="utf-8")

    # 前端三件套；index.html 里的 __STATIC__ 开关翻成 true
    for fname in ("index.html", "style.css", "app.js"):
        src = WEB_DIR / fname
        if not src.is_file():
            raise SystemExit(f"[FAIL] 缺少前端文件 {src}")
        text = src.read_text(encoding="utf-8")
        if fname == "index.html":
            text = text.replace("window.__STATIC__ = false;", "window.__STATIC__ = true;")
        (out_dir / fname).write_text(text, encoding="utf-8")

    # 清理可能残留的旧数据文件（接口改名后不至于留下孤儿文件）
    valid = {f"{n}.js" for n in payloads}
    for p in list(api_dir.glob("*.js")) + list(api_dir.glob("*.json")):
        if p.name not in valid:
            p.unlink()

    total_bytes = sum(p.stat().st_size for p in out_dir.rglob("*") if p.is_file())
    if not args.quiet:
        print(f"[OK] 网页版已导出 -> {out_dir}")
        print(f"     新闻 {len(bundle.news)} 条 / 原始 {len(bundle.raw_news)} 条 / "
              f"板块 {len(bundle.boards())} 个 / 历史 {len(bundle.history())} 天")
        print(f"     合计 {total_bytes / 1024:.0f} KB")
        print("     本地可直接双击 index.html 打开；线上挂在 Pages 的 /web/ 下")
    return 0


if __name__ == "__main__":
    sys.exit(main())
