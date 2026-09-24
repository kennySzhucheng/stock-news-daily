# -*- coding: utf-8 -*-
"""M9 网页版自检 — 直接请求本地服务，核对各接口结构与数据一致性。

用法（先起服务）:
    python modules/m9_web/server.py --no-browser
    python modules/m9_web/check.py
"""
import argparse
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aggregate  # noqa: E402  仅用于比对板块黑名单

BASE = "http://127.0.0.1:8848"
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ok_count = fail_count = 0


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def check(label, cond, detail=""):
    global ok_count, fail_count
    if cond:
        ok_count += 1
        print(f"  [OK]   {label} {detail}")
    else:
        fail_count += 1
        print(f"  [FAIL] {label} {detail}")


def main():
    global BASE
    ap = argparse.ArgumentParser(description="M9 网页版自检")
    ap.add_argument("--port", type=int, default=8848)
    args = ap.parse_args()
    BASE = f"http://127.0.0.1:{args.port}"

    print("=" * 68)
    print(f"M9 网页版自检 · {BASE}")
    print("=" * 68)

    st, meta = get("/api/meta")
    check("GET /api/meta", st == 200 and "news_count" in meta,
          f"news={meta.get('news_count')} ask={meta.get('ask_enabled')} "
          f"source={meta.get('source')}")

    st, ov = get("/api/overview")
    check("GET /api/overview", st == 200 and ov.get("raw_count", 0) > 0,
          f"raw={ov.get('raw_count')} structured={ov.get('structured_count')} "
          f"quotes={ov.get('quotes', {}).get('count')}")
    check("结论句非空", bool(ov.get("conclusion")), ov.get("conclusion", "")[:34] + "…")
    check("情绪分在 [-1,1]", -1 <= ov.get("sentiment_score", 9) <= 1,
          str(ov.get("sentiment_score")))
    check("来源状态齐全", len(ov.get("sources", [])) >= 5,
          f"{len(ov.get('sources', []))} 个源")
    check("板块聚合非空", len(ov.get("top_boards", [])) > 0,
          f"{len(ov.get('top_boards', []))} 个")

    st, news = get("/api/news?limit=2000")
    items = news.get("items", [])
    check("GET /api/news 全量", st == 200 and len(items) == news.get("total"),
          f"total={news.get('total')}")
    check("新闻 id 连续", [n["id"] for n in items] == sorted(n["id"] for n in items),
          f"id {items[0]['id']}..{items[-1]['id']}" if items else "")

    st, raw = get("/api/raw")
    kept = sum(1 for x in raw.get("items", []) if x.get("kept"))
    check("GET /api/raw", st == 200 and raw.get("total", 0) > 0,
          f"total={raw.get('total')} kept={kept} dropped={raw.get('total', 0) - kept}")

    st, q = get("/api/quotes")
    check("GET /api/quotes", st == 200 and len(q.get("quotes", [])) > 0,
          f"{len(q.get('quotes', []))} 只 / 失败 {len(q.get('failed', []))}")

    st, b = get("/api/boards")
    check("GET /api/boards", st == 200 and len(b.get("boards", [])) > 0,
          f"{len(b.get('boards', []))} 个板块")
    names = [x["name"] for x in b.get("boards", [])]
    pseudo = [n2 for n2 in names if n2 in aggregate.BLOCKED_BOARDS]
    check("板块名无事件词/占位符", not pseudo, str(pseudo[:4]) if pseudo else "")

    st, a = get("/api/analysis")
    check("GET /api/analysis", st == 200 and len(a.get("html", "")) > 200,
          f"html {len(a.get('html', ''))} 字符")
    cites = a.get("html", "").count('class="cite"')
    check("引用编号已可点击", cites > 0, f"{cites} 处引用")
    check("无残留 markdown 标记",
          "##" not in a.get("html", "").replace("#news-", ""),
          "")

    st, h = get("/api/history")
    check("GET /api/history", st == 200, f"{len(h.get('reports', []))} 天")

    # ── M10 候选清单 ───────────────────────────────────────
    st, pk = get("/api/picks")
    rows = pk.get("rows", [])
    check("GET /api/picks", st == 200 and isinstance(rows, list),
          f"{pk.get('total')} 条 / 最新 {pk.get('latest_date')}")

    if rows:
        need = {"id", "date", "kind", "name", "reviews"}
        bad = [r.get("id", "?") for r in rows if not need.issubset(r)]
        check("候选条目字段完整", not bad, f"缺字段: {bad[:3]}" if bad else "")

        # 候选里的个股必须真在行情里出现过——两个接口之间的跨源一致性。
        # （板块走的是板块名单，不在 quotes 里，故只查个股）
        qnames = {x.get("name") for x in get("/api/quotes")[1].get("quotes", [])}
        stocks = [r["name"] for r in rows if r.get("kind") == "stock"]
        miss = [n for n in stocks if n not in qnames]
        check("候选个股在行情中存在", not miss, f"不在行情里: {miss[:3]}" if miss else "")

        # 结构上保证「不是荐股」：推翻条件由 M10 代码强制，账本里不该有空值
        noinv = [r.get("name") for r in rows if not (r.get("invalidation") or "").strip()]
        check("每条候选都有推翻条件", not noinv,
              f"缺失: {noinv[:3]}" if noinv else "（结构上保证非荐股）")

        # 引用编号必须已翻成 news 下标，否则点开会是另一条新闻
        nid = len(get("/api/news?limit=2000")[1].get("items", []))
        badref = []
        for r in rows:
            for i in (r.get("basis_ids") or []):
                if not isinstance(i, int) or not (0 <= i < nid):
                    badref.append((r.get("name"), i))
        check("新闻依据编号可解析", not badref, f"越界: {badref[:3]}" if badref else "")

        # 产品决策的回归测试：不得出现「胜率」类措辞（样本量小时它没有意义）
        blob = json.dumps(pk, ensure_ascii=False)
        banned = [w for w in ("胜率", "命中率", "盈利率") if w in blob]
        check("接口无「胜率」类字段", not banned, str(banned) if banned else "")

        # 有均值就必须同时有样本数——这是硬性展示要求，接口层先保证 n 存在
        st2 = pk.get("stats", {})
        with_mean = [k for k, v in st2.items() if v.get("alpha") is not None]
        no_n = [k for k in with_mean if not st2[k].get("n")]
        check("均值与样本数同时存在", not no_n,
              f"{'T+' + '/T+'.join(sorted(with_mean))} 有均值，均带 n={[st2[k]['n'] for k in sorted(with_mean)]}"
              if with_mean else "暂无到期回填，跳过")

    # 筛选功能：分类过滤后条数应等于分类统计
    st, pol = get("/api/news?cat=policy&limit=2000")
    check("分类筛选与统计一致",
          pol.get("total") == ov.get("categories", {}).get("policy"),
          f"筛选 {pol.get('total')} vs 统计 {ov.get('categories', {}).get('policy')}")

    st, key = get("/api/news?q=" + urllib.request.quote("减持") + "&limit=2000")
    check("关键词检索可用", key.get("total", 0) > 0, f"命中 {key.get('total')} 条")

    print("-" * 68)
    print(f"通过 {ok_count} 项，失败 {fail_count} 项")
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
