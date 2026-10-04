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
# 中新网（2026-10-04 新增）：新华网/人民网的 RSS 内容已冻结（新华网停在 2022-12、
# 人民网停在 2025-06，详见 docs/sources.md 第八节），但**用户明确要求保留这两家**，
# 所以它们留在列表里；同时补上中新网作为**通用新闻**的活源 —— 实测三个频道
# 都是当日内容（`Sun, 4 Oct 2026 18:35:00 +0800`），30 条/次。
#
# 为什么需要通用源：项目定位虽然是"股市情报助手"，但只吃「提到个股的新闻」会让
# 分析面变窄 —— 宏观、政策、国际形势、产业动向本来就是判断依据。这里补的是
# 「与经济/社会相关的综合新闻」，不是娱乐体育（后者由 M2 的负向词挡掉）。
CN_CHINANEWS_ROLL = "https://www.chinanews.com.cn/rss/scroll-news.xml"
CN_CHINANEWS_FIN = "https://www.chinanews.com.cn/rss/finance.xml"
CN_CHINANEWS_WORLD = "https://www.chinanews.com.cn/rss/world.xml"
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


# ── RSS / 接口时间解析兜底表（2026-10-04 实测，三个 RSS 源各有各的坑）────────
#   36氪   "2026-09-30 19:38:45  +0800"（双空格 + 无冒号时区）
#   人民网 "2025-06-05"（**只有日期**，没有时分秒）
#   新华网 **整个 feed 没有 <pubDate> 标签**，时间是 </link> 之后的裸文本
#          "Wed,14-Dec-2022 11:37:37 GMT"（星期逗号后无空格、年月日之间是连字符）
# 表按「先具体后宽松」排序；strptime 要求全串匹配，所以谁先谁后不会互相误伤。
_TS_FORMATS = (
    "%Y-%m-%d %H:%M:%S %z",
    "%a, %d %b %Y %H:%M:%S %z",
    "%a, %d %b %Y %H:%M:%S %Z",
    "%a,%d-%b-%Y %H:%M:%S %Z",
    "%a, %d %b %Y %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d",
)

# 从任意文本里捞出一个「日期[时间]」片段的兜底正则（新华网那种裸文本用）
_TS_TEXT_RE = re.compile(
    r"[A-Z][a-z]{2},\s?\d{1,2}[-\s][A-Z][a-z]{2}[-\s]\d{4}"
    r"(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?(?:\s?[A-Z]{2,4})?"
)


class _NewsList(list):
    """带 partial 标记的抓取结果列表：分页中途失败时保留已抓内容并标记 partial。

    是 list 的子类，对 json.dumps / len / 下标全部透明，不改变既有返回类型语义。
    """
    partial = False


def _parse_ts(raw):
    """把各种时间文本解析成 "%Y-%m-%d %H:%M:%S"；解析不出来返回 ""。

    **不改动成功条目的时间格式**（仍是下游依赖的定长字符串），
    也不做时区换算，与既有实现（strptime + mktime）保持一致。
    """
    s = re.sub(r"\s+", " ", (raw or "").strip())
    if not s:
        return ""
    for fmt in _TS_FORMATS:
        try:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.strptime(s, fmt))
        except (ValueError, TypeError):
            continue
    m = _TS_TEXT_RE.search(s)
    if m and m.group(0).strip() != s:
        return _parse_ts(m.group(0))
    return ""


def _rss_item_ts_text(it):
    """取一条 RSS <item> 的时间文本。

    优先 <pubDate>；新华网的 feed 把时间当**裸文本**直接放在 </link> 后面
    （2026-10-04 实测 300/300 条都是这个形态），在 ET 里它是 link 元素的 tail。
    最后再对整条 item 做正则兜底，防止以后换源改了字段名。
    """
    pd = (it.findtext("pubDate") or "").strip()
    if pd:
        return pd
    el = it.find("link")
    if el is not None and (el.tail or "").strip():
        return el.tail.strip()
    m = _TS_TEXT_RE.search(ET.tostring(it, encoding="unicode"))
    return m.group(0) if m else ""


def fetch_sina(days, max_items):
    """新浪财经 7x24 快讯"""
    out, seen, page = _NewsList(), set(), 1
    cutoff = time.time() - days * 86400
    while len(out) < max_items and page <= 10:
        stop = False
        try:
            d = _get_json(SINA_7X24.format(page=page))
            items = (d.get("result", {}).get("data", {}).get("feed", {}) or {}).get("list", [])
            if not items:
                break
            for it in items:
                ts = it.get("create_time") or ""
                if ts:
                    try:
                        t = time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
                    except (ValueError, TypeError):
                        # 一条坏时间不再废掉整源：按「无时间」处理，由 collect() 兜底剔除
                        ts = ""
                    else:
                        if t < cutoff:
                            stop = True
                            break
                text = _clean(it.get("rich_text") or it.get("text") or "")
                if not text or text in seen:
                    continue
                seen.add(text)
                out.append({"time": ts, "source": "新浪财经", "category": "finance",
                            "url": "", "text": text[:500]})
        except Exception as e:
            # 单页失败：保留本页之前已抓到的内容，别把整源归零
            print(f"[warn] 新浪财经7x24 第 {page} 页失败，保留已抓 {len(out)} 条：{str(e)[:80]}")
            out.partial = True
            break
        if stop:
            break
        page += 1
        time.sleep(0.6)
    return out


def fetch_eastmoney(days, max_items):
    """东方财富 7x24 快讯"""
    out, seen = _NewsList(), set()
    cutoff_ms = (time.time() - days * 86400) * 1000
    sort_end, page = "", 1
    # 两道闸防死循环：接口用 sortEnd 游标翻页，而 `seen` 去重后 out 可能不再增长，
    # 只要接口重复返回同一批（sortEnd 不变）就永远退不出去、每轮还 sleep 0.6s 打一次接口。
    # 故加「最多 30 页」（对照 fetch_sina 的 page <= 10）+「sortEnd 必须前进」。
    while len(out) < max_items and page <= 30:
        try:
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
            new_sort_end = data.get("sortEnd", "")
            if not new_sort_end:
                break
            if new_sort_end == sort_end:
                print(f"[warn] 东方财富7x24 sortEnd 未前进（第 {page} 页起重复），提前结束分页")
                break
            sort_end = new_sort_end
        except Exception as e:
            # 单页失败：保留已抓内容
            print(f"[warn] 东方财富7x24 第 {page} 页失败，保留已抓 {len(out)} 条：{str(e)[:80]}")
            out.partial = True
            break
        page += 1
        time.sleep(0.6)
    return out


def _parse_rss(xml_text, source, category, days, max_items=None):
    """通用 RSS 解析：只保留「时间能解析且在 cutoff 之后」的条目。

    时间解析不出来的**丢弃并计数告警**（旧实现静默留 ts=""，这些条目在 collect()
    里按时间倒序永远排最后，被 M2 的 150 条上限截掉 —— 2026-10-04 一次运行白抓
    390 条）。max_items 为 None 表示不截断，给数字则真正截断（旧实现收了参数却没用）。
    先清洗非法 XML 字符再解析；仍失败则用正则逐条提取（容错）。
    """
    out = _NewsList()
    cutoff = time.time() - days * 86400
    bad_ts, stale = 0, 0
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", xml_text or "")
    root = None
    try:
        root = ET.fromstring(cleaned)
    except ET.ParseError:
        root = None

    def _full():
        return bool(max_items) and len(out) >= max_items

    if root is None:
        # 正则回退：逐条 <item>...</item> 提取
        for block in re.findall(r"<item>(.*?)</item>", cleaned, re.S):
            if _full():
                break
            m_title = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", block, re.S)
            if not m_title:
                continue
            title = _clean(m_title.group(1))
            m_pub = re.search(r"<pubDate>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</pubDate>",
                              block, re.S)
            if m_pub:
                pd = m_pub.group(1).strip()
            else:
                # 新华网形态：</link> 后面直接跟着裸时间文本，没有标签
                m_tail = re.search(r"</link>\s*([^<>]{8,60})", block)
                pd = m_tail.group(1).strip() if m_tail else ""
            ts = _parse_ts(pd)
            if not ts:
                bad_ts += 1
                continue
            if time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S")) < cutoff:
                stale += 1
                continue
            m_link = re.search(r"<link>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>", block, re.S)
            link = m_link.group(1).strip() if m_link else ""
            m_desc = re.search(r"<description>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>",
                               block, re.S)
            desc = _clean(m_desc.group(1))[:200] if m_desc else ""
            text = f"{title}：{desc}" if desc else title
            if not text:
                continue
            out.append({"time": ts, "source": source, "category": category,
                        "url": link, "text": text[:500]})
    else:
        for it in root.findall(".//item"):
            if _full():
                break
            title = _clean(it.findtext("title") or "")
            link = (it.findtext("link") or "").strip()
            ts = _parse_ts(_rss_item_ts_text(it))
            if not ts:
                bad_ts += 1
                continue
            if time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S")) < cutoff:
                stale += 1
                continue
            desc = _clean(it.findtext("description") or "")
            text = f"{title}：{desc}"[:500] if desc else title
            if not text:
                continue
            out.append({"time": ts, "source": source, "category": category,
                        "url": link, "text": text})
    if max_items and len(out) > max_items:
        out = _NewsList(out[:max_items])
    if bad_ts:
        print(f"[warn] {source} RSS：{bad_ts} 条发布时间缺失/无法解析，已丢弃")
    if stale:
        print(f"[warn] {source} RSS：{stale} 条早于 cutoff（{days} 天内）已过滤")
    return out


def fetch_36kr(days, max_items):
    return _parse_rss(_get_xml(RK_RSS), "36氪", "tech", days, max_items)


def fetch_xinhua(days, max_items):
    return _parse_rss(_get_xml(XINHUA_RSS), "新华网", "official", days, max_items)


def fetch_people(days, max_items):
    return _parse_rss(_get_xml(PEOPLE_RSS), "人民网", "official", days, max_items)


# ── 2026-10-04 第三阶段：新华网 / 人民网「栏目页 HTML」取数（RSS 保留为兜底）────
# 为什么是「换取数方式」而不是「换源」：用户明确要求**保留这两家媒体**，且希望它们
# 真的有内容。而两家的 RSS 层已整体停更（实测）：
#   · 新华网 RSS  整体冻结在 2022-12-09~14，item 还没有 <pubDate>（时间写在 </link>
#     之后的裸文本里，已由 _rss_item_ts_text 兼容）；
#   · 人民网 RSS 停在 2025-06-05；该站 politics/finance/world/society/it 五个频道的
#     RSS 路径**返回同一份 2025-06 的冻结内容** ⇒ 是 RSS 层停更，不是单频道问题。
# 所以新函数改为直接抓**栏目页 HTML**，从文章永久链接里的日期目录取时间。
#
# 候选端点探活结论（2026-10-04 实测，逐条见汇报）：
#   · https://qc.wa.news.cn/nodeart/list?nid=... —— 服务活着、返回 JSON（含 PubTime），
#     但①证书主机名不匹配（qc.wa.news.cn 的证书对不上域名，stdlib SSL 直接
#     CERTIFICATE_VERIFY_FAILED）；②已知 nid=11147664 内容停在 2021-07-23；
#     ③新版 news.cn 全站改成了 `data="datasource:<uuid>"` 的 CMS 预渲染，栏目页/文章页
#     HTML 里**搜不到任何 nid**，无法可靠枚举当前频道 ⇒ 放弃。
#   · https://so.news.cn/getNews —— GET/POST、换路径一律 405（CDN 错误页），放弃。
#   · 人民网 http://search.people.cn/api-search/elasticsearch/search —— https 下被 SPA
#     首页 HTML 顶掉（不是 JSON），http 下 405 Not Allowed，放弃。
#   · 栏目页 HTML —— **当日内容充足**，两家都走这一路：
#     新华网 m.news.cn 88 条当日链接（62 条带标题）、时政联播 45、world 28、
#     fortune 14、depthobserve 8；人民网 www.people.com.cn 133、world 104、
#     finance 60、滚动/今日头条页 34。
#
# 时间口径（重要）：栏目页 HTML 只在文章永久链接里带**日期**（`/20261004/`、
# `/n1/2026/1004/`），没有时分秒 ⇒ 这类条目的 time 记为「当日 00:00:00」。这与巨潮
# announcementTime「只有日期就照抄 00:00」的既有先例一致：**只照抄源给的精度，不编造
# 时刻**。人民网「滚动/今日头条」页额外带 `[10月04日16:59]`，这种条目用**真实分钟**。
# 过滤口径：有真实分钟的按 cutoff = now - days*86400 精确比较；只有日期的按**日历日**
# 比较（dt.date() >= 本地 cutoff 的日期）。否则 days=1 会把「昨天发布的新闻」整天误杀
# —— 日期精度下这是必要的放宽，且不引入任何虚构时间。
XINHUA_COLUMNS = (
    # (栏目页 URL, category 提示) —— 顺序即抓取顺序；每个栏目页有独立配额（见 _fetch_columns）
    ("https://www.news.cn/politics/szlb/index.html", "official"),    # 时政联播
    ("https://www.news.cn/fortune/index.htm", "finance"),            # 财经
    ("https://www.news.cn/world/", "international"),                 # 国际
    ("https://www.news.cn/depthobserve/", "official"),               # 深度观察
    ("https://m.news.cn/", "official"),                              # 移动端首页（兜量最大）
)
PEOPLE_COLUMNS = (
    # 顺序有意为之：GB/59476 是人民网「滚动新闻 / 今日头条一览」，条目自带
    # [10月04日16:59] 分钟级真实时间，排第一；财经/国际/社会三个频道页排在首页**之前**
    # —— 首页会把财经/国际的文章也列一遍，首页放前面会把它们的 path 先占掉，跨页去重后
    # 财经/国际频道就一条都进不来（实测：首页优先时全源只剩 30 条）。首页作为兜底最后抓。
    ("http://www.people.com.cn/GB/59476/index.html", "official"),
    ("http://finance.people.com.cn/", "finance"),
    ("http://world.people.com.cn/", "international"),
    ("http://society.people.com.cn/", "official"),
    ("http://www.people.com.cn/", "official"),
)

# 新华网文章永久链接：<栏目>/<YYYYMMDD>/<32位hex>/c.html（href 可能是相对路径，也会是单引号）
# 注意 `[^'"]*?` 用 * 而不是 +：人民网/新华网的 href 常常**以 / 开头**（如 `/n1/2026/1004/...`、
# `/20261004/<hash>/c.html`），用 `+?` 要求斜杠前至少有一个字符，会把这类相对链接整批漏掉
# （离线用例就是这么抓到的：绝对 URL 能过、站内相对 URL 全丢）。
_XH_ANCHOR_RE = re.compile(
    r"""<a\b[^>]*?href=['"]([^'"]*?/(\d{8})/[0-9a-fA-F]{16,}/c\.html)['"][^>]*>(.*?)</a>""",
    re.S)
# 人民网文章永久链接：/n1/<YYYY>/<MMDD>/c<栏目>-<id>.html，锚点后可能跟 [MM月DD日HH:MM]
_PPL_ANCHOR_RE = re.compile(
    r"""<a\b[^>]*?href=['"]([^'"]*?/n1/(\d{4})/(\d{4})/c\d+-\d+\.html)['"][^>]*>(.*?)</a>"""
    r"""(?:[^\n\[]{0,10}\[(\d{1,2})月(\d{1,2})日(\d{1,2}):(\d{2})\])?""",
    re.S)


def _get_html(url, timeout=20):
    """GET 一个 HTML 页面并解码：显式 try utf-8 → gb18030。

    先用 <meta charset> 声明判断（新华网/人民网历史上都用过 gb2312），声明里写了
    gb* 就先按 gb18030 解；否则先 utf-8、UnicodeDecodeError 再退 gb18030。
    最后兜底 errors="replace"，保证返回 str 而不是把解码异常抛给分页循环。
    """
    req = urllib.request.Request(url, headers=UA)
    with _urlopen(req, timeout) as r:
        raw = r.read()
    head = raw[:2048].decode("ascii", errors="ignore").lower()
    m = re.search(r'charset=["\']?\s*([\w-]+)', head)
    declared = (m.group(1) if m else "").lower()
    order = ("gb18030", "utf-8") if declared.startswith("gb") else ("utf-8", "gb18030")
    for enc in order:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _iter_xinhua_links(html):
    """新华网栏目页 → (url, 日期串, 标题, 精确时刻 or None)。日期取自永久链接目录。"""
    for m in _XH_ANCHOR_RE.finditer(html):
        href, d8, title = m.group(1).strip(), m.group(2), _clean(m.group(3))
        yield href, f"{d8[:4]}-{d8[4:6]}-{d8[6:]}", title, None


def _iter_people_links(html):
    """人民网栏目页 → 同上；锚点后的 [10月04日16:59] 给出分钟级真实时间。"""
    for m in _PPL_ANCHOR_RE.finditer(html):
        href = m.group(1).strip()
        year, mmdd, title = m.group(2), m.group(3), _clean(m.group(4))
        bmon, bday, bhh, bmi = m.group(5), m.group(6), m.group(7), m.group(8)
        hhmm = None
        # 括号里的月日与链接目录里的月日一致才敢用来当精确时刻（跨零点可能不一致）
        if bmon and bmon.zfill(2) == mmdd[:2] and bday.zfill(2) == mmdd[2:]:
            hhmm = f"{bhh.zfill(2)}:{bmi}"
        yield href, f"{year}-{mmdd[:2]}-{mmdd[2:]}", title, hhmm


def _fetch_columns(pages, source, days, max_items, iterator):
    """抓一组栏目页 HTML 并归一化成统一新闻条目。

    · 每页独立 try/except：单页失败只标记 `partial`，**已抓内容不丢**；
    · 每页有独立配额（max_items // 页数，下限 8），避免第一个页面吃满 cap 把
      其它频道（财经/国际）挤掉；
    · **所有页都失败才 raise** —— 让 collect() 记失败、让 live 函数回退 RSS，
      绝不「静默返回空」把故障伪装成 0 条。
    """
    out, seen_url, seen_title = _NewsList(), set(), set()
    cutoff = time.time() - days * 86400
    cutoff_date = datetime.fromtimestamp(cutoff).date()
    per_page = max(8, max_items // max(len(pages), 1))
    bad_ts = stale = failed = 0
    for page_url, category in pages:
        if len(out) >= max_items:
            break
        try:
            html = _get_html(page_url)
        except Exception as e:
            failed += 1
            out.partial = True
            print(f"[warn] {source} 栏目页 {page_url} 抓取失败，保留已抓 {len(out)} 条：{str(e)[:80]}")
            continue
        taken = 0
        for href, date_s, title, hhmm in iterator(html):
            if len(out) >= max_items or taken >= per_page:
                break
            if not title:
                continue
            ts = _parse_ts(f"{date_s} {hhmm}:00" if hhmm else f"{date_s} 00:00:00")
            if not ts:
                bad_ts += 1
                continue
            link = urllib.parse.urljoin(page_url, href)
            path = urllib.parse.urlsplit(link).path
            if path in seen_url or title in seen_title:
                continue
            # 精确到分钟的按 cutoff 精确比；只有日期的按日历日比（见上方时间口径注释）
            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            if hhmm:
                if time.mktime(dt.timetuple()) < cutoff:
                    stale += 1
                    continue
            elif dt.date() < cutoff_date:
                stale += 1
                continue
            seen_url.add(path)
            seen_title.add(title)
            out.append({"time": ts, "source": source, "category": category,
                        "url": link, "text": title[:500]})
            taken += 1
        time.sleep(0.6)
    if pages and failed == len(pages):
        raise RuntimeError(f"{source} 栏目页全部抓取失败（{failed}/{len(pages)}）")
    if bad_ts:
        print(f"[warn] {source} 栏目页：{bad_ts} 条发布时间无法解析，已丢弃")
    if stale:
        print(f"[warn] {source} 栏目页：{stale} 条早于 cutoff（{days} 天）已过滤")
    return out


def fetch_xinhua_live(days, max_items):
    """新华网 live 取数：栏目页 HTML（见上方 XINHUA_COLUMNS 注释）。

    回退逻辑（兜底，保证「保留新华网」最坏也不比现在差）：
      新端点异常 **或** 抓到 0 条 → print 原因并回退 `fetch_xinhua`（原 RSS）。
    若 RSS 也失败，异常继续上抛给 collect() 记为该源失败（不吞异常返回空）。
    """
    try:
        items = _fetch_columns(XINHUA_COLUMNS, "新华网", days, max_items, _iter_xinhua_links)
    except Exception as e:
        print(f"[warn] 新华网 live 栏目页全部失败（{str(e)[:80]}），回退 RSS：{XINHUA_RSS}")
        return fetch_xinhua(days, max_items)
    if not items:
        print(f"[warn] 新华网 live 栏目页 0 条，回退 RSS：{XINHUA_RSS}"
              f"（该 RSS 自 2022-12 起冻结，days=1 下预计仍为 0）")
        return fetch_xinhua(days, max_items)
    return items


def fetch_people_live(days, max_items):
    """人民网 live 取数：栏目页 HTML（见上方 PEOPLE_COLUMNS 注释）。

    回退逻辑同 fetch_xinhua_live：新端点异常或 0 条 → 回退 `fetch_people`（原 RSS，
    实测停在 2025-06-05，因此回退后的 0 条会如实反映为 0 条而不是编造）。
    """
    try:
        items = _fetch_columns(PEOPLE_COLUMNS, "人民网", days, max_items, _iter_people_links)
    except Exception as e:
        print(f"[warn] 人民网 live 栏目页全部失败（{str(e)[:80]}），回退 RSS：{PEOPLE_RSS}")
        return fetch_people(days, max_items)
    if not items:
        print(f"[warn] 人民网 live 栏目页 0 条，回退 RSS：{PEOPLE_RSS}"
              f"（该 RSS 自 2025-06 起冻结，days=1 下预计仍为 0）")
        return fetch_people(days, max_items)
    return items


def fetch_chinanews_roll(days, max_items):
    """中新网 滚动（国内/综合）—— 官方通讯社口径的通用新闻源。"""
    return _parse_rss(_get_xml(CN_CHINANEWS_ROLL), "中新网滚动", "official", days, max_items)


def fetch_chinanews_fin(days, max_items):
    return _parse_rss(_get_xml(CN_CHINANEWS_FIN), "中新网财经", "finance", days, max_items)


def fetch_chinanews_world(days, max_items):
    return _parse_rss(_get_xml(CN_CHINANEWS_WORLD), "中新网国际", "international", days, max_items)


def fetch_gov(days, max_items):
    """中国政府网政策库：官方政策最高权重源

    政策库按 publictime 倒序返回，但**同一页里会混进几个月前的国函国令**
    （2026-10-04 实测 40 条里混着 2026-05-29 / 06-01 / 07-02 的旧文），
    所以必须按 pubtimeStr 做 days 过滤，否则旧政策会被当成「今日新闻」。
    抓取方式（政策库第 1~2 页）保持不变。
    """
    out = _NewsList()
    cutoff = time.time() - days * 86400
    for page in (1, 2):
        try:
            d = _get_json(GOV_API.format(page=page))
            items = ((d.get("searchVO", {}) or {}).get("listVO") or [])
            if not items:
                break
            bad = 0
            stale = 0
            for it in items:
                title = _clean(it.get("title") or "")
                if not title:
                    continue
                summary = _clean(it.get("summary") or "")
                pcode = it.get("pcode") or ""
                pub = (it.get("pubtimeStr") or "").replace(".", "-").strip()
                # pubtimeStr 实测是 "2026.09.24"（只有日期）；解析失败就丢弃，
                # 别留给 collect() 当「无时间」条目静默扔掉
                ts = _parse_ts(pub)
                if not ts:
                    bad += 1
                    continue
                if time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S")) < cutoff:
                    stale += 1
                    continue
                text = f"{title}（{pcode}）{('：' + summary) if summary else ''}"[:500]
                out.append({"time": pub, "source": "中国政府网", "category": "policy",
                            "url": "", "text": text})
            if bad:
                print(f"[warn] 中国政府网 第 {page} 页有 {bad} 条发布时间无法解析，已丢弃")
            if stale:
                print(f"[warn] 中国政府网 第 {page} 页有 {stale} 条早于 cutoff（{days} 天内）已过滤")
        except Exception as e:
            # 单页失败：保留已抓内容
            print(f"[warn] 中国政府网 第 {page} 页失败，保留已抓 {len(out)} 条：{str(e)[:80]}")
            out.partial = True
            break
    return out


def fetch_sina_domestic(days, max_items):
    """新浪滚动「国内」（lid=2509）—— 2026-10-04 实测为当日内容。

    复用已验证的新浪滚动端点族补**通用新闻**：这一路是时政/社会/民生类，
    填 M2 的 official 桶，避免"只吃财经快讯 + 个股公告"把分析面收窄。
    （体育 lid=2512、娱乐 lid=2513 不接：与市场判断无关，接了也只是噪声。）
    """
    return fetch_sina_roll(days, max_items, lid=2509, source_name="新浪滚动-国内",
                           category="official")


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


def fetch_sina_roll(days, max_items, lid=2516, source_name="新浪财经-滚动",
                    category="finance"):
    """新浪滚动新闻。lid=2516 实测内容以港股/海外券商评级为主。

    这是 M1 的境外视角来源，填 M2 的 `international` 类目（此前一直空着）。
    注意 lid=2510（"要闻"）、2514 实测返回的是**几个月/几年前的旧闻**，已弃用。
    category 提示用 finance：这些标题多无「股/市/证券」等关键词，
    走 prefilter 的关键词分支会被整条丢掉。

    2026-10-04：加 `lid` 参数以便复用同一端点族补齐**通用新闻**（见
    `fetch_sina_domestic` 与 SOURCES 注释）。
    """
    out, seen = [], set()
    cutoff = time.time() - days * 86400
    url = SINA_ROLL_API.format(lid=lid, size=max_items)
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
        out.append({"time": ts, "source": source_name, "category": category,
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

    q = _cninfo_query(se_date, "", 1)
    if not isinstance(q, dict) or not q:
        # 连响应体都拿不到 = 接口真不通，让上层按失败记录，别拿空类目充数
        raise RuntimeError(f"巨潮基线查询无有效响应（seDate={se_date}）")
    baseline = q.get("totalRecordNum") or 0
    if not baseline:
        # **区间内本来就没有公告 ≠ 接口故障**（2026-10-04 修）。
        # 旧实现一律 raise，于是每个周末与长假都报「巨潮基线查询为空」——
        # 与真故障长同一张脸，日志噪声大了就没人再当真。
        print(f"[warn] 巨潮 {se_date} 区间内没有公告（周末/长假属正常），本源于 0 条")
        return _NewsList()

    out, seen = _NewsList(), set()
    for code, label, share in CNINFO_CATEGORIES:
        if len(out) >= max_items:
            break
        quota = int(max_items * share)
        if quota <= 0:
            continue
        try:
            d = _cninfo_query(se_date, code, quota)
        except Exception as e:
            # 单个类目查询失败不再把前面已抓到的类目一起丢掉
            print(f"[warn] 巨潮类目「{label}」查询失败，保留已抓 {len(out)} 条：{str(e)[:80]}")
            out.partial = True
            break
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
    # 结构：(名称, 抓取函数, 条数上限[, 时间窗天数下限])
    #   第 4 项是**该源特有的最小时间窗**。默认跟随 `collect(days=1)`（24 小时），
    #   只有「发布节奏天然不规律、且过期不影响正确性」的源才放宽，见下面 gov 的注释。
    ("新浪财经7x24", fetch_sina, 300),
    ("东方财富7x24", fetch_eastmoney, 300),
    # 三个 RSS 源的 cap 以前是纯装饰（fetch_* 收了 max_items 却没用，实测新华网
    # 照样返回 300、人民网 100），2026-10-04 起真正传进 _parse_rss 生效。
    # 取值依据（实测 feed 规模）：36氪 feed 只有 30 条/次 → 30；新华网 feed 300 条，
    # 但时间过滤后远少于 100，截到 100 只为省内存/降噪；人民网 feed 100 条 → 60。
    #
    # ⚠️ 2026-10-04 实跑查明（详见 docs/sources.md 第八节与 CHANGELOG）：
    #   · 新华网 RSS **整体冻结在 2022-12-09~14**，且 item 没有 <pubDate>（时间写在
    #     </link> 之后的裸文本里，已兼容解析）；在 days=1 下必然 0 条。
    #   · 人民网 RSS 停在 **2025-06-05**（item 只有纯日期），同样必然 0 条。
    #   ⚠️ **用户明确要求保留这两家**（宁可留着等它们复活，也不移除），所以它们留在
    #   列表里；但通用新闻的供给交给下面新增的活源，别指望这两条。
    ("36氪", fetch_36kr, 30),
    # 新华网 / 人民网（2026-10-04 第三阶段，第三次改动）：**源名与媒体不变**，只把
    # 取数方式从「停更的 RSS」换成「栏目页 HTML」：
    #   · 新端点：XINHUA_COLUMNS（新华网时政联播/财经/国际/深度观察/m.news.cn）与
    #     PEOPLE_COLUMNS（人民网滚动新闻/首页/财经/国际/社会）—— 端点清单、实测数据
    #     （2026-10-04 当日链接数）、RSS 为何停更、时间口径，全部写在两个常量上方的注释里。
    #   · 回退：fetch_xinhua_live / fetch_people_live 内先试新端点，**异常或 0 条**时
    #     print 原因并回退到原 fetch_xinhua / fetch_people（RSS）；两者都失败才上抛给
    #     collect() 记为该源失败。原 RSS 函数**一行未动**，所以最坏情况不比改动前差。
    #   · cap：实测栏目页当日供给 新华网 ~74 条（5 页各自独立配额）、人民网 ~60 条，
    #     故 100 / 60 是「够用不虚高」的取值；两者仍按 days=1（24 小时窗）过滤。
    #   ⚠️ 注意：第 4 项是「该源最小时间窗」，这两个源**不设**第 4 项，即跟随 collect(days)。
    ("新华网", fetch_xinhua_live, 100),
    ("人民网", fetch_people_live, 60),
    # 通用新闻（2026-10-04 新增）——用户要求「包括所有相关新闻，而不是只有股票相关的」。
    # 只吃财经快讯 + 个股公告会让分析面变窄：宏观、政策、国际形势、产业动向本身就是判断依据。
    # 实测（当日 18:35 仍在更新）：中新网三个频道各 30 条；新浪滚动 lid=2509 当日有效。
    # 娱乐（lid=2513）与体育（lid=2512）刻意不接 —— 与市场判断无关，接了只是噪声。
    ("中新网滚动", fetch_chinanews_roll, 30),
    ("中新网财经", fetch_chinanews_fin, 30),
    ("中新网国际", fetch_chinanews_world, 30),
    ("新浪滚动-国内", fetch_sina_domestic, 25),
    # 政策文件不是每天都有：2026-10-04 实测政策库两页 40 条全部落在
    # 2026-05-29 ~ 2026-09-30，**没有任何 10-01~10-04 发布的内容**。若也按 24 小时
    # 过滤，这个源在绝大多数日子都是 0 条 —— 那等于把「政府发布」这个类目砍掉。
    # 所以给它 7 天窗：既不会再把几个月前的国函当「今日政策」（旧实现就是如此），
    # 又能保住类目覆盖。下游能看见真实日期：M3 的清单每条都带 `time`，M5/M9 的
    # 新闻条目也显示原始时间戳，因此「这是 3 天前的政策」是可见的、不会被伪装成今天。
    ("中国政府网", fetch_gov, 100, 7),
    # 第二阶段（2026-09-25，见 docs/sources.md 第 7 节）
    ("巨潮资讯公告", fetch_cninfo, 45),
    ("东方财富宏观政策", fetch_em_policy, 40),
    ("新浪财经滚动", fetch_sina_roll, 25),
]


def collect(days=1):
    """抓取全部源，失败源跳过并记录。返回 (news_list, status_list)"""
    all_news, status = [], []
    for src in SOURCES:
        name, fn, cap = src[0], src[1], src[2]
        win = src[3] if len(src) > 3 else days      # 该源的最小时间窗（见 SOURCES 注释）
        try:
            items = fn(max(days, win), cap)
            all_news.extend(items)
            rec = {"source": name, "ok": True, "count": len(items)}
            if win > days:
                rec["window_days"] = max(days, win)
            if getattr(items, "partial", False):
                # 可选字段：分页中途失败但保留了已抓内容
                rec["partial"] = True
            status.append(rec)
        except Exception as e:
            status.append({"source": name, "ok": False, "error": str(e)[:100]})
        time.sleep(0.6)
    # 跨源去重：文本主干 + 时间倒序保留最先出现的那条。
    # 被合并掉的重复条目不再直接丢弃 —— 把它们所在的**来源**聚合进存活条目的
    # `sources`（list[str]，已去重排序，至少含自己的 source），这样 M2 才能做
    # 「多源印证」；去重键与「保留谁」的判据与旧实现完全一致。
    seen, uniq, dropped_no_time = {}, [], 0
    for n in sorted(all_news, key=lambda x: x["time"], reverse=True):
        if not n["time"]:
            # 兜底：时间为空的条目永远抢不到 M2 的名额，直接剔除并计数
            dropped_no_time += 1
            continue
        key = re.sub(r"[【】\s：:，,。]", "", n["text"])[:25]
        src = n.get("source") or ""
        if key in seen:
            keep = seen[key]
            merged = set(keep.get("sources") or [])
            if src:
                merged.add(src)
            keep["sources"] = sorted(merged)
            continue
        n["sources"] = sorted({src}) if src else []
        seen[key] = n
        uniq.append(n)
    status.append({"total_unique": len(uniq), "dropped_no_time": dropped_no_time})
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
