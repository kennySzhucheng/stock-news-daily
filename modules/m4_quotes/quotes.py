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
QUOTE_API = ("https://push2.eastmoney.com/api/qt/stock/get"
             "?secid={secid}&fields=f43,f57,f58,f59,f60,f170,f171")
# push2 主域名限流时（RemoteDisconnected），延时域名实测仍可用
QUOTE_API_MIRROR = QUOTE_API.replace("//push2.eastmoney.com", "//push2delay.eastmoney.com")

# 只关注 A 股市场（MktNum: 17=沪A, 33=深A？实际上证 1.x / 0.x 深系）
A_SEC_BINARY = {"17", "30"}  # 搜索返回的 MktNum 17=上A 33=深A(不同版本接口 nginx 会有差异，用 validate 判断)


def _urlopen(req, timeout=15):
    """优先直连，失败回退系统代理。

    Windows 上 urllib 会自动读取系统代理设置；若梯子开着但节点不通，
    所有请求都会失败。
    """
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(req, timeout=timeout)
    except Exception:
        return urllib.request.urlopen(req, timeout=timeout)


def _get_json(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    with _urlopen(req, timeout) as r:
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
    # MktNum 就是 secid 的市场前缀：1=沪A 0=深A 105/106/107=美股 116=港股
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


def fetch_quote(secid, retries=2, timeout=5):
    """拉单只股票行情。

    字段：f43=现价 f60=昨收 f170=今日涨跌幅 f59=小数位数
    **f43/f60 的缩放系数随市场而变**（实测：沪深 f59=2 → ÷100，
    美股与港股 f59=3 → ÷1000），写死 ÷100 会让美股港股价格放大 10 倍。
    故按接口返回的 f59 动态换算；f170 则固定为 ×100。

    push2 对高频请求有限流（表现为 RemoteDisconnected）。实测限流时
    push2delay 域名仍可正常返回，故每次尝试都把两个域名走一遍；
    重试次数少、等待短，失败即跳过该个股，不拖垮流水线。
    """
    for attempt in range(retries):
        last_err = None
        for api in (QUOTE_API, QUOTE_API_MIRROR):
            try:
                d = _get_json(api.format(secid=secid), timeout=timeout)
                data = d.get("data") or {}
                if not data.get("f58"):
                    return None
                f59 = data.get("f59")
                scale = 10 ** (f59 if isinstance(f59, int) and 0 <= f59 <= 6 else 2)

                def _px(v):
                    return round(v / scale, 4) if isinstance(v, (int, float)) else v

                def _pct(v):
                    return round(v / 100, 2) if isinstance(v, (int, float)) else v

                return {
                    "code": data.get("f57"),
                    "name": data.get("f58"),
                    "price": _px(data.get("f43")),
                    "prev_close": _px(data.get("f60")),
                    "change_pct": _pct(data.get("f170")),
                }
            except Exception as e:
                last_err = e
                continue
        if attempt < retries - 1:
            time.sleep(1.5 * (attempt + 1))   # 1.5s, 3s
    print(f"[warn] quote {secid} failed: {str(last_err)[:50]}")
    return None


TENCENT_API = "https://qt.gtimg.cn/q={code}"


def _to_tencent_code(secid):
    """东财 secid → 腾讯代码。

    东财市场前缀：0=深A 1=沪A 105=美股NASDAQ 106=美股NYSE 107=美股AMEX 116=港股。
    腾讯的美股代码是 us+股票代码（usAAPL），不带交易所后缀。

    注意：105/106/107 全都是美股。早期版本把 106/107 当成沪/深，
    美股于是被拼成 "shJNJ" 这种不存在的代码，请求必然失败却计入熔断，
    连续 5 次就把后面几十只 A 股一起跳过了。
    """
    prefix, code = secid.split(".", 1)
    if prefix == "1":
        return "sh" + code
    if prefix == "0":
        return "sz" + code
    if prefix == "116":
        return "hk" + code
    if prefix in ("105", "106", "107"):
        return "us" + code
    return None


def fetch_quote_tencent(secid, timeout=8):
    """备选源：腾讯行情。返回结构与 fetch_quote 一致，失败返回 None。

    东财 push2 存在按 IP 的临时限流（表现为 RemoteDisconnected），
    限流期间切到腾讯源可保证行情板块不至于整块缺失。

    字段下标：[1]名称 [2]代码 [3]现价 [4]昨收 [30]报价时刻 [32]涨跌幅%。
    涨跌幅直接取 [32] 而不是自己用 [3]/[4] 算——**腾讯的美股在休市时段
    不返回昨收与涨跌幅**（[4] 等于 [3]、[32] 为空），自己算会得到
    一个假的 0.00%。而 A 股收盘后 [32] 仍是当日真实涨跌幅。

    [30] 是 `YYYYMMDDhhmmss` 的报价时刻（如 `20260924161413`），东财的 f43
    没有对应字段。M10 的复盘闸门靠它判断"今日是否已收盘"——比看时钟准，
    节假日/午休/延迟运行都能如实识别。
    """
    tcode = _to_tencent_code(secid)
    if not tcode:
        return None
    try:
        req = urllib.request.Request(TENCENT_API.format(code=tcode), headers=UA)
        with _urlopen(req, timeout) as r:
            raw = r.read().decode("gbk", errors="replace")
        # 形如 v_sh600519="1~贵州茅台~600519~1258.00~1272.75~1273.93~...~-1.16~..."
        m = re.search(r'="([^"]*)"', raw)
        if not m:
            return None
        parts = m.group(1).split("~")
        if len(parts) <= 32:
            return None
        if not parts[32].strip():
            return None            # 无涨跌幅 = 该时段没有可比数据，不编造
        code = parts[2].split(".")[0]   # 美股带交易所后缀（AAPL.OQ），去掉
        price = float(parts[3])
        prev_close = float(parts[4])
        change_pct = round(float(parts[32]), 2)
        if prev_close == 0:
            return None
        # 腾讯的美股在休市时段返回 [4]==[3]、[32]=0.00，这不是"平盘"，
        # 而是根本没有当日数据（A 股/港股收盘后仍有真实涨跌幅）。
        # 我们的定时任务跑在北京时间早上/下午，正好是美国休市时段。
        if tcode.startswith("us") and price == prev_close and change_pct == 0:
            return None
        return {
            "code": code,
            "name": parts[1],
            "price": price,
            "prev_close": prev_close,
            "change_pct": change_pct,
            "time": parts[30].strip(),   # 报价时刻 YYYYMMDDhhmmss，M10 的收盘闸门用
        }
    except Exception as e:
        print(f"[warn] tencent {tcode}: {str(e)[:50]}")
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
    consecutive_fail = 0
    em_fail = 0
    eastmoney_down = False     # 东财连续失败后停用，后续直接走备选源
    MAX_CONSECUTIVE_FAIL = 5   # 两源都失败才计一次，连续失败被放弃

    for idx, name in enumerate(names):
        info = resolve_stock(name, cache)
        if not info:
            failed.append({"name": name, "reason": "not_found"})
            continue

        q, src = None, "eastmoney"
        if not eastmoney_down:
            q = fetch_quote(info["secid"])
            if q:
                em_fail = 0
            else:
                em_fail += 1
                if em_fail >= 3:
                    eastmoney_down = True
                    print("[warn] 东财行情连续失败（含 push2delay 备用域名），后续改用腾讯源")
        if not q:
            # 东财限流时切备选源，避免行情板块整块缺失
            if _to_tencent_code(info["secid"]) is None:
                # 该市场在备选源没有对应代码：属个股层面的确定性失败，
                # 不代表行情源不健康。若计进熔断，几只美股就能把后面的 A 股全跳过。
                failed.append({"name": name, "reason": "unsupported_market",
                               "secid": info["secid"]})
                continue
            q = fetch_quote_tencent(info["secid"])
            src = "tencent"

        if not q:
            failed.append({"name": name, "reason": "quote_fail", "secid": info["secid"]})
            consecutive_fail += 1
            if consecutive_fail >= MAX_CONSECUTIVE_FAIL:
                rest = names[idx + 1:]
                print(f"[warn] 两个行情源连续 {consecutive_fail} 次失败，"
                      f"跳过剩余 {len(rest)} 只")
                failed.extend({"name": n, "reason": "skipped_rate_limited"} for n in rest)
                break
            continue

        consecutive_fail = 0
        q.update({"market": info["market"], "matched_by": name, "source": src})
        quotes.append(q)
        time.sleep(1.0)  # push2 限流敏感，间隔 1s

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
