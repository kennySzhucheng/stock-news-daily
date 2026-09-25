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
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timedelta

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

# ── 2026-09-25 第二阶段新增 ────────────────────────────────────────────────
# 端点取自 cn-financial-scraper skill，三条均已实测；接入方式改写为本模块的
# 纯标准库风格（复用 _urlopen / 0.6s 间隔 / UA），不引入任何第三方依赖，
# 这样 GitHub Actions 的 runner 无需 pip install 就能跑。详见 docs/sources.md。
#
# 为什么不能直接 import skill：它装在 ~/.claude/skills/、**不是 git 仓库**，
# 云端 runner 拿不到；且依赖 requests + 自带的 http_utils（1932 行）。
EM_NEWS_API = ("https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"
               "?client=web&biz=web_news_col&column={column}&order=1"
               "&needInteractData=0&page_index=1&page_size={size}&req_trace={ts}")
SINA_ROLL_API = ("https://feed.mix.sina.com.cn/api/roll/get"
                 "?pageid=153&lid={lid}&k=&num={size}&page=1")
CNINFO_QUERY_API = "https://www.cninfo.com.cn/new/hisAnnouncement/query"

# 巨潮的类目（按「信号强度」排序，配额见 fetch_cninfo）。
# ⚠️ 这些码**无效时 cninfo 不报错，而是静默回退成「全部公告」** —— 2026-09-25 实测：
# gqbd / rcjy / yjygjxz / sjdbg / yjdbg 有效；skill 里带标签的 zcjy / gdqz / gdzc /
# hg / ndbg / bndbg 对线上 API 无效，会静默返回全部。故 fetch_cninfo 先取「全部」
# 基线逐个比对，对不上就**丢弃该类目并告警**，宁可少抓也不能抓错。
CNINFO_CATEGORIES = [
    ("category_gqbd_szsh", "股权变动", 0.45),   # 减持/增持/回购/质押
    ("category_yjygjxz_szsh", "业绩预告", 0.25),
    ("category_rcjy_szsh", "日常经营", 0.30),   # 含处罚、重组、重大合同等
]
# cninfo 与东财的 POST 需要这几个头，缺了会 403
CNINFO_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Referer": "http://www.cninfo.com.cn/new/commonUrl/pageOfSearch"
               "?url=disclosure/list/search",
    "Origin": "http://www.cninfo.com.cn",
    "X-Requested-With": "XMLHttpRequest",
}


def _urlopen(req, timeout=20):
    """优先直连，失败回退系统代理。

    Windows 上 urllib 会自动读取系统代理设置；若梯子开着但节点不通，
    所有请求都会失败。本项目数据源以国内站点为主，直连通常更快更稳。
    """
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(req, timeout=timeout)
    except Exception:
        return urllib.request.urlopen(req, timeout=timeout)


def _get_json(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with _urlopen(req, timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get_xml(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with _urlopen(req, timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def _post_form(url, payload, timeout=25, extra_headers=None):
    """POST 表单并返回 JSON（巨潮公告接口用）。失败抛异常，由 collect() 兜住。"""
    headers = dict(UA)
    headers.update(extra_headers or {})
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with _urlopen(req, timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


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


def fetch_em_policy(days, max_items):
    """东方财富宏观政策栏（column=345）。

    与 gov.cn 政策库互补：gov.cn 给的是**文件原文**（标题+文号），这个给的是
    **围绕政策的新闻解读**（含国际财经，实测混有美联储/美股/港股条目）。
    所以 category 提示用 finance 而非 policy —— 避免和 gov.cn 抢 prefilter 的
    优先额度，最终分类仍由 M2 决定。
    """
    out, seen = [], set()
    cutoff = time.time() - days * 86400
    url = EM_NEWS_API.format(column=345, size=max_items, ts=int(time.time() * 1000))
    d = _get_json(url)
    if str(d.get("code")) != "1":
        return out
    for it in (d.get("data") or {}).get("list") or []:
        ts = (it.get("showTime") or "").strip()          # "2026-09-25 04:23:31"
        if ts:
            try:
                if time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S")) < cutoff:
                    continue
            except ValueError:
                ts = ""
        title = _clean(it.get("title") or "")
        url_ = (it.get("url") or "").strip()
        if not title or title in seen:
            continue
        seen.add(title)
        out.append({"time": ts, "source": "东方财富-宏观政策", "category": "finance",
                    "url": url_, "text": title[:500]})
        if len(out) >= max_items:
            break
    return out


def fetch_sina_roll(days, max_items):
    """新浪财经滚动新闻（lid=2516）—— 实测内容以港股/海外券商评级为主。

    这是 M1 唯一的境外视角来源，填 M2 的 `international` 类目（此前一直空着）。
    注意 lid=2510（"要闻"）2026-09-25 实测返回的是**几个月前的旧闻**，已弃用。
    category 提示用 finance：这些标题多无「股/市/证券」等关键词，
    走 prefilter 的关键词分支会被整条丢掉。
    """
    out, seen = [], set()
    cutoff = time.time() - days * 86400
    url = SINA_ROLL_API.format(lid=2516, size=max_items)
    d = _get_json(url)
    for it in (d.get("result") or {}).get("data") or []:
        ctime = it.get("ctime") or ""
        ts = ""
        if ctime:
            try:
                t = int(ctime)
                if t < cutoff:
                    continue
                ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
            except (ValueError, OSError, OverflowError):
                ts = ""
        title = _clean(it.get("title") or "")
        if not title or title in seen:
            continue
        seen.add(title)
        out.append({"time": ts, "source": "新浪财经-滚动", "category": "finance",
                    "url": (it.get("url") or "").strip(), "text": title[:500]})
        if len(out) >= max_items:
            break
    return out


def _cninfo_query(se_date, category, page_size):
    payload = {
        "pageNum": 1, "pageSize": page_size, "tabName": "fulltext",
        "plate": "", "stock": "", "searchkey": "", "secid": "",
        # column 只填 szse 是安全的：category 的 _szsh 后缀本身就代表「沪深」，
        # 实测 column=szse 与 column=sse 返回完全相同的结果（该参数实际被忽略）
        "column": "szse", "category": category, "trade": "",
        "seDate": se_date, "sortName": "", "sortType": "", "isHLtitle": "true",
    }
    return _post_form(CNINFO_QUERY_API, payload, extra_headers=CNINFO_HEADERS)


def fetch_cninfo(days, max_items):
    """巨潮资讯全市场公告 —— M2「个股公告」类目的**唯一**来源。

    M1 第一阶段 6 个源全是新闻流，**一条公告都没有**，M2 的 `stock` 类目里
    「个股公告」这个分支实际是空的。这里按三个高信号类目分额度抓取。

    两个坑，都是静默的：
    1. **类目码无效不报错，而是回退成「全部公告」**（近 1400 条/天）。故先取
       「全部」基线，逐类目比对 total，对不上就丢弃该类目并告警。
    2. **`announcementTime` 只有日期、时刻恒为北京 00:00**。时间戳如实照抄，
       不编造时刻；代价是这些条目在「按时间倒序」里排在当日快讯之后，
       故 category 提示用 `announcement`（见 M2 prefilter 的优先级）。
    """
    end = datetime.now()
    se_date = f"{end - timedelta(days=max(days, 1)):%Y-%m-%d}~{end:%Y-%m-%d}"

    baseline = (_cninfo_query(se_date, "", 1) or {}).get("totalRecordNum") or 0
    if not baseline:
        # 基线取不到说明接口整体不通，直接让上层按失败记录，别拿空类目充数
        raise RuntimeError(f"巨潮基线查询为空（seDate={se_date}）")

    out, seen = [], set()
    for code, label, share in CNINFO_CATEGORIES:
        if len(out) >= max_items:
            break
        quota = int(max_items * share)
        if quota <= 0:
            continue
        d = _cninfo_query(se_date, code, quota)
        total = d.get("totalRecordNum") or 0
        anns = d.get("announcements") or []
        if total == baseline:
            print(f"[warn] 巨潮类目「{label}」({code}) 过滤失效：total={total} "
                  f"与全部公告基线相同，已跳过（该码可能已变更）")
            continue
        for it in anns:
            title = _clean(it.get("announcementTitle") or "")
            if not title or title in seen:
                continue
            seen.add(title)
            ts = ""
            raw_ts = it.get("announcementTime")
            if raw_ts:
                try:
                    ts = time.strftime("%Y-%m-%d %H:%M:%S",
                                       time.localtime(int(raw_ts) / 1000))
                except (ValueError, OSError, OverflowError):
                    ts = ""
            adj = (it.get("adjunctUrl") or "").strip()
            href = f"http://static.cninfo.com.cn/{adj}" if adj else ""
            # 带上个股名与代码：同一标题（如"2026年前三季度业绩预告"）会被
            # 上层按文本前 25 字去重，不带前缀会把不同公司的公告并成一条
            sec = (it.get("secName") or "").strip()
            code_ = (it.get("secCode") or "").strip()
            prefix = f"{sec}({code_})：" if sec and code_ else ""
            out.append({"time": ts, "source": "巨潮资讯-公告", "category": "announcement",
                        "url": href, "text": f"{prefix}{title}"[:500]})
            if len(out) >= max_items:
                break
        time.sleep(0.6)
    return out


SOURCES = [
    ("新浪财经7x24", fetch_sina, 300),
    ("东方财富7x24", fetch_eastmoney, 300),
    ("36氪", fetch_36kr, 50),
    ("新华网", fetch_xinhua, 50),
    ("人民网", fetch_people, 50),
    ("中国政府网", fetch_gov, 100),
    # 第二阶段（2026-09-25，见 docs/sources.md 第 7 节）
    ("巨潮资讯公告", fetch_cninfo, 45),
    ("东方财富宏观政策", fetch_em_policy, 40),
    ("新浪财经滚动", fetch_sina_roll, 25),
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
