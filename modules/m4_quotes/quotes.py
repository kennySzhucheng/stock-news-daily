# -*- coding: utf-8 -*-
"""
M4 行情数据模块 — 东财接口拉取 M2/M3 提取个股的行情

输入: data/structured_news.json（个股名单）+ data/analysis.md（备用）
输出: data/quotes.json

要点：
1. 股票名 → secid 用东财搜索接口解析（不猜市场前缀）
2. 关注 A 股（沪深）为主，港股顺带支持
3. 接口失败静默跳过个股
"""
import json
import re
import time
import urllib.request
import urllib.parse
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE / "data"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
SUGGEST_API = "https://searchadapter.eastmoney.com/api/suggest/get?input={q}&type=14&count=5"
QUOTE_API = "https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields=f43,f57,f58,f60,f170,f171"

# 只关注 A 股市场（MktNum: 17=沪A, 33=深A？实际上证 1.x / 0.x 深系）
A_SEC_BINARY = {"17", "30"}  # 搜索返回的 MktNum 17=上A 33=深A(不同版本接口 nginx 会有差异，用 validate 判断)


def _get_json(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def resolve_stock(name, seen_cache=None):
    """股票名 → {secid, code, name, market} 或 None。港股结果也返回（market 标注）"""
    if seen_cache is not None and name in seen_cache:
        return seen_cache[name]
    try:
        q = urllib.parse.quote(name.strip())
        d = _get_json(SUGGEST_API.format(q=q))
    except Exception as e:
        print(f"[warn] suggest '{name}' failed: {e}")
        return None
    rows = (d.get("QuotationCodeTable", {}) or {}).get("Data") or []
    if not rows:
        return None
    # 优先精确同名；其次第一条
    pick = next((r for r in rows if r.get("Name") == name.strip()), rows[0])
    # MktNum 就是 secid 的市场前缀：1=沪A 0=深A 106/116=港股
    mkt = (pick.get("MktNum") or "").strip()
    code = pick["Code"]
    if mkt in ("1", "0", "105", "106", "107", "116"):
        secid = f"{mkt}.{code}"
    elif mkt == "17":       # 部分接口版本用 17 表示沪
        secid = f"1.{code}"
    else:
        secid = None  # 不认识的市场类型，跳过
    if secid is None:
        return None
    result = {"secid": secid, "code": code, "name": pick["Name"],
              "market": pick.get("SecurityTypeName", "")}
    if seen_cache is not None:
        seen_cache[name] = result
    return result


def fetch_quote(secid, retries=3):
    """拉单只股票行情。f43=现价(×100) f170=今日涨跌幅(×100) f60=昨收
    push2 对高频请求有限流（短期断连），失败指数退避重试。"""
    for attempt in range(retries):
        try:
            d = _get_json(QUOTE_API.format(secid=secid))
            data = d.get("data") or {}
            if not data.get("f58"):
                return None
            def _p(v):
                return v / 100 if isinstance(v, int) else v
            return {
                "code": data.get("f57"),
                "name": data.get("f58"),
                "price": _p(data.get("f43")),
                "prev_close": _p(data.get("f60")),
                "change_pct": _p(data.get("f170")),
            }
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))  # 5s, 10s, 15s
            else:
                print(f"[warn] quote {secid} failed after {retries} tries: {e}")
                return None
    return None


def extract_names_from_news(news, min_occurrence=1):
    """从 M2 结构化新闻提取所有出现过的个股名（去重计数）"""
    from collections import Counter
    counter = Counter()
    for n in news:
        for s in n.get("stocks", []):
            s = (s or "").strip()
            if len(s) >= 2:
                # 清理噪音：括号内容、纯代码（含.HK等）、含明显非股票名词汇
                s = re.sub(r"[（(].*?[)）]", "", s).strip()
                if re.match(r"^\d+\.(HK|hk|SZ|sz|SH|sh)$", s):
                    continue  # 纯代码形式，resolve 会查不到中文，跳过
                if 2 <= len(s) <= 8 and re.search(r"[一-鿿]", s):
                    counter[s] += 1
    # 出现次数排序，高频优先
    return [name for name, c in counter.most_common() if c >= min_occurrence]


def main():
    d = json.loads((DATA_DIR / "structured_news.json").read_text(encoding="utf-8"))
    names = extract_names_from_news(d["news"])
    print(f"提取个股名 {len(names)} 个: {names[:10]}{'...' if len(names) > 10 else ''}")

    cache = {}
    quotes, failed = [], []
    for name in names:
        info = resolve_stock(name, cache)
        if not info:
            failed.append({"name": name, "reason": "not_found"})
            continue
        q = fetch_quote(info["secid"])
        if not q:
            failed.append({"name": name, "reason": "quote_fail", "secid": info["secid"]})
            continue
        q.update({"market": info["market"], "matched_by": name})
        quotes.append(q)
        time.sleep(1.0)  # push2 限流敏感，间隔加大到 1s

    out = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "count": len(quotes), "failed": failed, "quotes": quotes}
    out_path = DATA_DIR / "quotes.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    a_cnt = sum(1 for q in quotes if q["market"] and "A" in q["market"] and "H" not in q["market"])
    hk = sum(1 for q in quotes if "港" in (q["market"] or ""))
    print(f"成功 {len(quotes)} 只 (A股 {a_cnt} / 港股 {hk}), 失败 {len(failed)}")
    for q in quotes[:5]:
        print(f"  {q['name']}({q['code']}) 现价 {q['price']} 涨跌 {q['change_pct']}%")
    print(f"输出 → {out_path}")


if __name__ == "__main__":
    main()
