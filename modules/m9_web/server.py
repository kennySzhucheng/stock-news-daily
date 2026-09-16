# -*- coding: utf-8 -*-
"""M9 本地网页版服务 — 比推送版更全的交互界面

推送版（M5/M6）是"读一遍就走"的长文；这里是可检索、可聚合、可追问的
界面，面向"我想自己找"的场景。

能力:
  总览仪表盘 / 新闻检索（多条件筛选 + 全文搜索）/ 个股行情 / 板块热度
  / 深度分析（引用可点击回原文）/ 历史日报 / AI 追问

用法:
    python modules/m9_web/server.py                 # 启动并打开浏览器
    python modules/m9_web/server.py --port 9000
    python modules/m9_web/server.py --no-browser

说明:
  - 只用 Python 标准库，无需安装任何依赖
  - 数据每次请求时按 mtime 自动重载，流水线跑完后刷新页面即可看到新数据
  - AI 追问需要环境变量 DEEPSEEK_API_KEY，未设置时该功能给出提示，其余功能不受影响
"""
import os
import re
import sys
import json
import argparse
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import aggregate  # noqa: E402
import ask as ask_mod  # noqa: E402

BASE = HERE.parent.parent
DATA_FILES = ["raw_news.json", "structured_news.json", "quotes.json", "analysis.md"]

WEB_DIR = HERE / "web"
# sync.py 同步下来的云端产物就放在这里，可作为本地流水线数据之外的数据源
EXPORT_API_DIR = BASE / "reports" / "web" / "api"

# 静态文件白名单后缀，避免把任意路径读出去
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}

SOURCE = "auto"
_cache = {"bundle": None, "key": None, "source": None}


def _mtime_key(paths):
    mtimes = [p.stat().st_mtime for p in paths if p.exists()]
    return max(mtimes) if mtimes else None


def _resolve_source(requested):
    """auto：本地流水线产出与云端同步产出，取更新的那一份。

    不能简单地"有本地数据就用本地"——用户跑完 sync.py 之后，
    本地 data/ 往往还是旧的，那样反而看不到刚同步下来的云端内容。
    """
    if requested != "auto":
        return requested
    local_key = _mtime_key([BASE / "data" / n for n in DATA_FILES])
    export_key = _mtime_key(list(EXPORT_API_DIR.glob("*.js")))
    has_local = (BASE / "data" / "structured_news.json").exists()
    if has_local and export_key is not None:
        return "export" if export_key > local_key else "local"
    if export_key is not None:
        return "export"
    return "local"        # 都没有时走本地，让"暂无数据"如实暴露出来


def get_bundle(force=None):
    """按数据文件 mtime 缓存 Bundle；数据更新后刷新页面即可，无需重启"""
    src = _resolve_source(force or SOURCE)
    if src == "export":
        key = (src, _mtime_key(list(EXPORT_API_DIR.glob("*.js"))))
    else:
        key = (src, _mtime_key([BASE / "data" / n for n in DATA_FILES]))
    if _cache["bundle"] is None or key != _cache["key"]:
        _cache["bundle"] = (aggregate.Bundle.from_export(EXPORT_API_DIR)
                            if src == "export" else aggregate.Bundle())
        _cache["key"] = key
        _cache["source"] = src
    return _cache["bundle"]


class Handler(BaseHTTPRequestHandler):
    server_version = "StockNewsWeb/1.0"

    # -- 基础输出 ----------------------------------------------------------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _err(self, code, msg):
        self._json({"error": msg}, code)

    def log_message(self, fmt, *args):
        # 默认日志太吵，只留错误
        pass

    # -- 路由 --------------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if path.startswith("/api/"):
                return self._api(path, q)
            return self._static(path)
        except Exception as e:
            self._err(500, f"{type(e).__name__}: {e}")

    def do_POST(self):
        u = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception as e:
            return self._err(400, f"请求体解析失败: {e}")

        try:
            if u.path == "/api/ask":
                return self._ask(payload)
            return self._err(404, "未知接口")
        except Exception as e:
            return self._err(500, f"{type(e).__name__}: {e}")

    # -- API ---------------------------------------------------------------
    def _api(self, path, q):
        b = get_bundle()

        if path == "/api/meta":
            return self._json({
                "date": b.overview()["date"],
                "analysis_ready": bool(b.analysis_md),
                "news_count": len(b.news),
                "ask_enabled": bool(os.environ.get("DEEPSEEK_API_KEY", "").strip()),
                "source": _cache.get("source") or "local",
            })

        if path == "/api/overview":
            return self._json(b.overview())

        if path == "/api/news":
            def _int(name, dflt):
                try:
                    return int(q.get(name, dflt))
                except (TypeError, ValueError):
                    return dflt
            total, items = aggregate.query_news(
                b,
                q=q.get("q", ""), cat=q.get("cat", ""), senti=q.get("senti", ""),
                verified=q.get("verified", ""), source=q.get("source", ""),
                board=q.get("board", ""), stock=q.get("stock", ""),
                sort=q.get("sort", "time"),
                limit=min(_int("limit", 40), 2000), offset=max(_int("offset", 0), 0),
            )
            return self._json({"total": total, "items": items})

        if path == "/api/raw":
            return self._json({"items": b.raw_view(), "total": len(b.raw_news)})

        if path == "/api/quotes":
            return self._json({"quotes": b.quotes, "failed": b.quotes_failed,
                               "generated_at": b.quotes_generated_at})

        if path == "/api/boards":
            return self._json({"boards": b.boards()})

        if path == "/api/analysis":
            return self._json({
                "html": b.analysis_html(),
                "markdown": b.analysis_md,
                "conclusion": b.conclusion(),
                "market_view": b.market_view(),
            })

        if path == "/api/history":
            return self._json({"reports": b.history()})

        return self._err(404, "未知接口")

    def _ask(self, payload):
        question = (payload.get("question") or "").strip()
        if not question:
            return self._err(400, "问题不能为空")
        if not os.environ.get("DEEPSEEK_API_KEY", "").strip():
            return self._err(503, "未设置 DEEPSEEK_API_KEY，AI 追问不可用。"
                                  "设置后可重启服务或在本地跑流水线时启用。")
        scope = payload.get("scope_ids") or []
        try:
            scope = [int(x) for x in scope][:60]
        except (TypeError, ValueError):
            scope = []
        result = ask_mod.ask(get_bundle(), question, scope_ids=scope)
        return self._json(result)

    # -- 静态文件 ----------------------------------------------------------
    def _static(self, path):
        # M5 生成的日报放在 reports/，网页版的「历史」页要能直接打开
        if path.startswith("/reports/"):
            name = path[len("/reports/"):]
            if not re.fullmatch(r"[\w\-.]+\.html", name):
                return self._err(403, "非法文件名")
            target = (BASE / "reports" / name).resolve()
            try:
                target.relative_to((BASE / "reports").resolve())
            except ValueError:
                return self._err(403, "越权路径")
            if not target.is_file():
                return self._err(404, "日报不存在")
            return self._send(200, target.read_bytes(), STATIC_TYPES[".html"])

        if path in ("/", ""):
            path = "/index.html"
        rel = path.lstrip("/")
        target = (WEB_DIR / rel).resolve()
        try:
            target.relative_to(WEB_DIR.resolve())
        except ValueError:
            return self._err(403, "越权路径")

        if not target.is_file():
            return self._err(404, "文件不存在")
        ctype = STATIC_TYPES.get(target.suffix.lower(), "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)


def main():
    global SOURCE
    ap = argparse.ArgumentParser(description="M9 本地网页版服务")
    ap.add_argument("--port", type=int, default=8848)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--source", choices=["auto", "local", "export"], default="auto",
                    help="数据来源：auto=本地 data/ 优先，缺失时用 sync.py 同步下来的"
                         "云端导出；local=只读 data/；export=只读 reports/web/api")
    args = ap.parse_args()

    SOURCE = args.source
    url = f"http://{args.host}:{args.port}/"
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)

    b = get_bundle()
    src_cn = {"local": "本地流水线产出 data/", "export": "云端导出 reports/web/api（sync.py 同步）"}
    print(f"[OK] 数据来源: {src_cn.get(_cache['source'], _cache['source'])}")
    print(f"[OK] 数据已载入: 新闻 {len(b.news)} 条 / 行情 {len(b.quotes)} 只 / "
          f"分析 {'有' if b.analysis_md else '无'}")
    if not b.news:
        print("[warn] 没有新闻数据。先跑一次流水线，或运行 "
              "'python modules/m9_web/sync.py' 拉取云端最新产出")
    if not os.environ.get("DEEPSEEK_API_KEY", "").strip():
        print("[warn] DEEPSEEK_API_KEY 未设置 -> AI 追问不可用，其余功能正常")
    print(f"[OK] 网页版已启动: {url}")
    print("     数据更新后刷新页面即可，无需重启（按文件 mtime 自动重载）")
    print("     Ctrl+C 停止")

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
