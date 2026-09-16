# -*- coding: utf-8 -*-
"""
M1 新闻收集模块 — 多元化来源抓取
来源清单与验证状态见 docs/sources.md

输出统一结构：{time, source, category, url, text}
分类 category 用于 M2 的参考提示（hint），最终分类由 M2 决定。
"""
import json
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

# 第一阶段接入并通过验证的源（见 docs/sources.md）
SINA_7X24 = "https://zhibo.sina.com.cn/api/zhibo/feed?zhibo_id=152&page={page}&page_size=100"
EM_7X24 = ("https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
           "?client=web&biz=web_724&fastColumn=102&sortEnd={sort_end}&pageSize=50&req_trace=1")
RK_RSS = "https://www.36kr.com/feed"
XINHUA_RSS = "http://www.xinhuanet.com/politics/news_politics.xml"
PEOPLE_RSS = "http://www.people.com.cn/rss/politics.xml"
GOV_API = ("https://sousuo.www.gov.cn/search-gov/data?t=zhengcelibrary_gw"
           "&timetype=timeqb&mintime=&maxtime=&sort=publictime&sortType=-1"
           "&searchfield=title&p={page}&n=20")


def _get_json(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get_xml(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def _clean(text):
    """统一清洗：去 HTML 标签、压缩空白"""
    text = re.sub(r"<[^>]+>", "", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def fetch_sina(days, max_items):
    """新浪财经 7x24 快讯"""
    out, seen, page = [], set(), 1
    cutoff = time.time() - days * 86400
    while len(out) < max_items and page <= 10:
        d = _get_json(SINA_7X24.format(page=page))
        items = (d.get("result", {}).get("data", {}).get("feed", {}) or {}).get("list", [])
        if not items:
            break
        stop = False
        for it in items:
            ts = it.get("create_time") or ""
            if ts:
                t = time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
                if t < cutoff:
                    stop = True
                    break
            text = _clean(it.get("rich_text") or it.get("text") or "")
            if not text or text in seen:
                continue
            seen.add(text)
            out.append({"time": ts, "source": "新浪财经", "category": "finance",
                        "url": "", "text": text[:500]})
        if stop:
            break
        page += 1
        time.sleep(0.6)
    return out


def fetch_eastmoney(days, max_items):
    """东方财富 7x24 快讯"""
    out, seen = [], set()
    cutoff_ms = (time.time() - days * 86400) * 1000
    sort_end = ""
    while len(out) < max_items:
        d = _get_json(EM_7X24.format(sort_end=sort_end))
        if str(d.get("code")) != "1":
            break
        data = d.get("data", {})
        items = data.get("fastNewsList", [])
        if not items:
            break
        stop = False
        for it in items:
            # showTime 为字符串时间 "2026-09-16 17:19:07"
            ts = (it.get("showTime") or "").strip()
            ts_ms = 0
            if ts:
                try:
                    ts_ms = time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S")) * 1000
                except ValueError:
                    ts_ms = 0
            if ts_ms and ts_ms < cutoff_ms:
                stop = True
                break
            text = _clean(it.get("summary") or it.get("title") or "")
            if not text or text in seen:
                continue
            seen.add(text)
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts_ms / 1000)) if ts_ms else ""
            out.append({"time": ts, "source": "东方财富", "category": "finance",
                        "url": "", "text": text[:500]})
        if stop:
            break
        sort_end = data.get("sortEnd", "")
        if not sort_end:
            break
        time.sleep(0.6)
    return out


def _parse_rss(xml_text, source, category, days):
    """通用 RSS 解析，返回 cutoff 之后的条目。
    先清洗非法 XML 字符再解析；仍失败则用正则逐条提取（容错）。
    """
    out = []
    cutoff = time.time() - days * 86400
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", xml_text or "")
    root = None
    try:
        root = ET.fromstring(cleaned)
    except ET.ParseError:
        root = None
        # 正则回退：逐条 <item>...</item> 提取
        for block in re.findall(r"<item>(.*?)</item>", cleaned, re.S):
            m_title = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", block, re.S)
            if not m_title:
                continue
            title = _clean(m_title.group(1))
            m_pub = re.search(r"<pubDate>([^<]+)</pubDate>", block)
            ts = ""
            if m_pub:
                pd = m_pub.group(1).strip()
                try:
                    ts = time.strftime("%Y-%m-%d %H:%M:%S",
                                       time.strptime(re.sub(r"\s+", " ", pd), "%Y-%m-%d %H:%M:%S %z"))
                except ValueError:
                    try:
                        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.strptime(pd, "%a, %d %b %Y %H:%M:%S %z"))
                    except (ValueError, TypeError):
                        ts = ""
            if ts and time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S")) < cutoff:
                continue
            m_link = re.search(r"<link><!\[CDATA\[(.*?)\]\]></link>", block)
            link = m_link.group(1).strip() if m_link else ""
            m_desc = re.search(r"<description>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>", block, re.S)
            desc = _clean(m_desc.group(1))[:200] if m_desc else ""
            text = f"{title}：{desc}" if desc else title
            if text:
                out.append({"time": ts, "source": source, "category": category,
                            "url": link, "text": text[:500]})
        return out
    items = root.findall(".//item")
    for it in items:
        title = _clean(it.findtext("title") or "")
        link = (it.findtext("link") or "").strip()
        # pubDate 解析（RFC822）
        pd = (it.findtext("pubDate") or "").strip()
        ts = ""
        # 36氪格式 "2026-09-16 15:58:46  +0800" 先试，再退回 RFC822
        try:
            ts = time.strftime("%Y-%m-%d %H:%M:%S",
                               time.strptime(re.sub(r"\s+", " ", pd), "%Y-%m-%d %H:%M:%S %z"))
        except ValueError:
            try:
                ts = time.strftime("%Y-%m-%d %H:%M:%S", time.strptime(pd, "%a, %d %b %Y %H:%M:%S %z"))
            except (ValueError, TypeError):
                ts = ""
        # 时间过滤：解析不出的保留（宁多勿漏，后端有今日过滤）
        if ts and time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S")) < cutoff:
            continue
        desc = _clean(it.findtext("description") or "")
        text = f"{title}：{desc}"[:500] if desc else title
        if not text:
            continue
        out.append({"time": ts, "source": source, "category": category,
                    "url": link, "text": text})
    return out


def fetch_36kr(days, max_items):
    return _parse_rss(_get_xml(RK_RSS), "36氪", "tech", days)


def fetch_xinhua(days, max_items):
    return _parse_rss(_get_xml(XINHUA_RSS), "新华网", "official", days)


def fetch_people(days, max_items):
    return _parse_rss(_get_xml(PEOPLE_RSS), "人民网", "official", days)


def fetch_gov(days, max_items):
    """中国政府网政策库：官方政策最高权重源"""
    out = []
    for page in (1, 2):
        d = _get_json(GOV_API.format(page=page))
        items = ((d.get("searchVO", {}) or {}).get("listVO") or [])
        if not items:
            break
        for it in items:
            title = _clean(it.get("title") or "")
            summary = _clean(it.get("summary") or "")
            pcode = it.get("pcode") or ""
            pub = (it.get("pubtimeStr") or "").replace(".", "-")
            text = f"{title}（{pcode}）{('：' + summary) if summary else ''}"[:500]
            if not title:
                continue
            out.append({"time": pub, "source": "中国政府网", "category": "policy",
                        "url": "", "text": text})
    return out


SOURCES = [
    ("新浪财经7x24", fetch_sina, 300),
    ("东方财富7x24", fetch_eastmoney, 300),
    ("36氪", fetch_36kr, 50),
    ("新华网", fetch_xinhua, 50),
    ("人民网", fetch_people, 50),
    ("中国政府网", fetch_gov, 100),
]


def collect(days=1):
    """抓取全部源，失败源跳过并记录。返回 (news_list, status_list)"""
    all_news, status = [], []
    for name, fn, cap in SOURCES:
        try:
            items = fn(days, cap)
            all_news.extend(items)
            status.append({"source": name, "ok": True, "count": len(items)})
        except Exception as e:
            status.append({"source": name, "ok": False, "error": str(e)[:100]})
        time.sleep(0.6)
    # 跨源去重：文本主干 + 时间倒序保留
    seen, uniq = set(), []
    for n in sorted(all_news, key=lambda x: x["time"], reverse=True):
        key = re.sub(r"[【】\s：:，,。]", "", n["text"])[:25]
        if key in seen:
            continue
        seen.add(key)
        n.pop("category", None) if n["category"] == "finance" and False else None
        uniq.append(n)
    status.append({"total_unique": len(uniq)})
    return uniq, status


if __name__ == "__main__":
    news, st = collect(days=1)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / "raw_news.json"
    out_path.write_text(json.dumps({
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sources_status": st,
        "news": news,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    for s in st:
        print(" ", s)
    cats = {}
    for n in news:
        cats[n["category"]] = cats.get(n["category"], 0) + 1
    print(f"共 {len(news)} 条，分类分布: {cats}")
    print(f"输出 → {out_path}")
