# -*- coding: utf-8 -*-
"""
M10 候选清单与事后复盘模块 — 把 M3 的判断变成可检验的记录

输入: data/structured_news.json + data/analysis.md + data/quotes.json
      reports/picks/ledger.jsonl（上次运行留下的账本，工作流开头已从 gh-pages 恢复）
输出: reports/picks/ledger.jsonl（追加当日候选；回填到期候选的 1/3/5 日表现）

要点：
1. 候选由 DeepSeek 从 M3 已有的判断里挑（**不改 M3 的 prompt**，故 analysis.md
   的格式契约与那 5 个解析器都不受影响），产出机读 JSON
2. invalidation（推翻条件）必填：空话、缺失、或命中投资指令禁用词的条目
   **在代码里丢弃** —— 让"不构成投资建议"有结构支撑，而不只是句免责声明
3. **记录与打分共用一个收盘闸门**（`close_ready`）：判据是**行情自带的报价时刻**
   而非「现在几点」。缺了它，盘前 08:10 那次运行会拿昨天的收盘价去比昨天的基准价，
   把 T+1 写成 0%，而账本一写就不再改 —— 周六周日、节假日同理。看钟表分不出
   「盘前 08:10」与「延迟到 12:46 的盘前」，也认不出国庆节的周一，报价时刻可以。
   fail-closed：判定不了就不写，延后一天由「到期即补」自动吸收
4. 记录当天锁下基准价 —— M4 只有实时行情、没有历史行情，事后补不了
5. 到期即补：target = 记录日 + k 天，today >= target 且该档未填就填，
   记**实际**打分日（周末/停牌会让它晚几天，如实反映）。
   各档独立判超期，不拿最早那档统一作废
6. 账本放 reports/picks/ —— 该目录随 gh-pages 自动跨天留存
   （daily.yml 部署 publish_dir 为 ./reports，开头 git archive 还原整棵树），
   无需 git commit 步骤、无需 artifact、无需改 .gitignore

密钥来源: 环境变量 DEEPSEEK_API_KEY
"""
import argparse
import importlib.util
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE / "data"
LEDGER_PATH = BASE / "reports" / "picks" / "ledger.jsonl"
BOARD_CACHE_PATH = BASE / "reports" / "picks" / "boards.json"   # 板块代码表缓存

CST = timezone(timedelta(hours=8))

BENCH_SECID = "1.000300"      # 沪深300，与个股同一端点同一字段结构，实测可用
BENCH_NAME = "沪深300"

# 板块指数：东财板块代码前缀 90（实测 secid=90.BK0447 可取到行情）
BOARD_LIST_API = ("https://push2.eastmoney.com/api/qt/clist/get"
                  "?pn=1&pz=1000&po=1&np=1&fltt=2&invt=2&fid=f3"
                  "&fs=m:90+t:{t}&fields=f12,f14")
# M4._get_json 不打备用域名（那层循环在 fetch_quote 自己身上），故这里自己走一遍
BOARD_LIST_MIRROR = BOARD_LIST_API.replace("//push2.eastmoney.com", "//push2delay.eastmoney.com")
BOARD_TYPES = ("2", "3")      # 2=行业板块 3=概念板块

REVIEW_DAYS = (1, 3, 5)       # 复盘周期
# 到期后仍取不到行情，放弃重试（防无限重试）。取 15 天而非 7：打分只在
# 「今日已收盘」时才发生（每个交易日一次机会），而春节/国庆能连休 9 个日历日，
# 7 天会让长假期间的档位在获得第一次机会之前就过期。
EXPIRE_AFTER_DAYS = 15

MAX_STOCKS = 4                # 候选数量上限（prompt 里也写了 2-4）
MAX_BOARDS = 2


def _load(name, rel):
    """按路径载入兄弟模块。

    与仓库既有惯例一致（m9_web/ask.py 载入 m3_analyzer、aggregate.py 载入
    m5_report）：模块间靠文件系统耦合，无 __init__.py，用 importlib 绕开包结构。
    """
    path = BASE / rel
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M4 = _load("m4_quotes", "modules/m4_quotes/quotes.py")
M3 = _load("m3_analyzer", "modules/m3_analyzer/analyzer.py")


# ---------------------------------------------------------------- 时间与账本

def now_cst():
    return datetime.now(CST)


def detect_slot(now=None):
    """盘前 am / 盘后 pm。daily.yml 通过 REPORT_SLOT 注入，本地按小时兜底"""
    s = (os.environ.get("REPORT_SLOT") or "").strip().lower()
    if s in ("am", "pm"):
        return s
    return "am" if (now or now_cst()).hour < 12 else "pm"


def load_ledger(path):
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"[warn] 账本有坏行，已跳过: {line[:60]}")
    return rows


def save_ledger(path, rows):
    """整文件重写（先写 .tmp 再 replace，避免半截文件）。

    内容没变就不动它：闸门关闭的那些运行（盘前、周末、节假日）本来就不该
    改账本，少一次覆盖窗口，也不会白白改掉 mtime（M9 的缓存靠 mtime 失效）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return True


# ---------------------------------------------------------------- 行情与基准

def fetch_any(secid):
    """东财优先，失败回退腾讯源。返回 (行情, 来源) 或 (None, None)。

    **不能直接调 M4.fetch_quote** —— 它只打东财主域名和 push2delay 两个地址，
    腾讯回退是在 M4 自己的 main() 里编排的。M10 单独要行情，必须自己做这条链，
    否则东财一限流，基准和复盘就整块失效（2026-09-24 本地实测：东财两个域名
    同时打不通、腾讯源正常，正是这个场景）。

    板块指数（90.BKxxxx）在腾讯源没有对应代码，_to_tencent_code 返回 None，
    故板块退回东财单源 —— 这是可接受的降级。
    """
    q = M4.fetch_quote(secid)
    if q and q.get("price"):
        return q, "eastmoney"
    q = M4.fetch_quote_tencent(secid)
    if q and q.get("price"):
        return q, "tencent"
    return None, None


def fetch_bench():
    """沪深300 → (点位, 报价字典)。取不到返回 (None, None)。

    只用腾讯源：它的报价自带 [30] 时刻，收盘闸门要靠它（东财 f43 无时刻字段）。
    且东财 push2 会按 IP 整段限流，而基准取数失败会让整轮 fail-closed。
    """
    q = M4.fetch_quote_tencent(BENCH_SECID)
    if not q:
        print("[warn] 沪深300 基准取数失败（腾讯源）")
        return None, None
    print(f"[OK] 基准 {BENCH_NAME} {q['price']} ({q['change_pct']}%) 源=tencent")
    return q["price"], q


def close_ready(bench_q, day):
    """今日是否已有可用的收盘价 → (bool, 说明)。

    **这是复盘正确性的总闸门。** 缺了它，盘前 08:10 那次运行会拿昨天的收盘价
    去比昨天的基准价，把 T+1 写成 0%，而账本一写就不再改 —— 周六、周日、
    节假日同理（它们都会拿到上一交易日的收盘价）。

    判据是**报价自带的时刻**，不是「现在几点」：看钟表分不出「盘前 08:10」
    与「延迟到 12:46 的盘前」，也认不出国庆节的周一。报价时刻则如实反映
    最后一次成交发生在什么时候。

    fail-closed：取不到时刻就**不记录也不打分**。写错的 0% 是永久的，
    而延后一天由「到期即补」自动吸收，代价小得多。
    """
    today8 = day.strftime("%Y%m%d")
    if not bench_q:
        return False, "基准行情取不到，无法确认今日是否收盘（宁可不写）"
    t = (bench_q.get("time") or "").strip()
    if not re.fullmatch(r"\d{12,14}", t):
        return False, f"行情源未给报价时刻（{t or '空'}），无法确认今日是否收盘"
    qdate, qhm = t[:8], t[8:12]
    hhmm = f"{qhm[:2]}:{qhm[2:]}"
    if qdate != today8:
        return False, f"最新报价停在 {qdate} {hhmm}，今日（{today8}）尚未交易"
    if qhm < "1500":
        return False, f"最新报价停在 {hhmm}，今日尚未收盘（盘中价不是收盘价）"
    return True, f"{qdate} {hhmm} 已收盘"


def _load_board_cache():
    try:
        d = json.loads(BOARD_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {k: v for k, v in d.items()
            if isinstance(v, dict) and v.get("code") and v.get("secid")}


def _save_board_cache(board_map):
    try:
        BOARD_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = BOARD_CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(board_map, ensure_ascii=False, sort_keys=True),
                       encoding="utf-8")
        tmp.replace(BOARD_CACHE_PATH)
    except Exception as e:
        print(f"[warn] 板块缓存写入失败: {str(e)[:50]}")


def build_board_map():
    """板块名 → {code, secid}。

    板块代码表几乎不变，但东财会抽风（实测 push2 与 push2delay 同时拒连）。若本次
    抓不到，当天所有板块候选的 secid 都会是 None —— 而账本行只写一次、事后不补，
    板块复盘就永久缺一块。故：抓到的结果落缓存，抓不到则退回上一次的缓存。
    两者都没有（首次运行恰逢接口故障）才返回空字典，此时板块候选降级为只列不计分。
    """
    cached = _load_board_cache()
    fresh = {}
    for t in BOARD_TYPES:
        d, err = None, None
        for api in (BOARD_LIST_API, BOARD_LIST_MIRROR):
            try:
                d = M4._get_json(api.format(t=t))
                break
            except Exception as e:
                err = e
        if d is None:
            print(f"[warn] 板块列表 t={t} 取数失败（含备用域名）: {str(err)[:50]}")
            continue
        rows = (d.get("data") or {}).get("diff") or []
        for r in rows:
            code, name = r.get("f12"), r.get("f14")
            if code and name:
                fresh.setdefault(name, {"code": code, "secid": f"90.{code}"})
        print(f"[OK] 板块列表 t={t}: {len(rows)} 个")
    if fresh:
        merged = dict(cached)
        merged.update(fresh)          # 缓存打底，本次结果覆盖（只抓到一类也不丢另一类）
        _save_board_cache(merged)
        return merged
    if cached:
        print(f"[OK] 板块列表改用缓存: {len(cached)} 个（东财本次不可用）")
    return cached


def _joint_len(a, b):
    """a 与 b 的词根接合长度：a 的后缀与 b 的前缀相同、或反之，取最长的 k（k≥2）。

    「AI算力」与「算力概念」共享的是**词根**「算力」（一为后缀、一为前缀），
    这是 LLM 拼板块名最常见的形状，双向子串却抓不到。只认首尾相接而不认任意
    公共子串，是为精度：「量子计算」与「云计算」只共中间的「计算」，不是同一个板块。
    """
    for k in range(min(len(a), len(b)), 1, -1):
        if a[-k:] == b[:k] or a[:k] == b[-k:]:
            return k
    return 0


def _match_one(name, board_map):
    """单个板块名 → 板块指数。三级：精确 → 双向子串 → 词根接合。"""
    if name in board_map:
        return board_map[name]
    hits = [k for k in board_map if name and (name in k or k in name)]
    if hits:                       # 命中多个取名字最短的（「半导体」优先于「半导体设备」）
        return board_map[min(hits, key=len)]
    joints = [(_joint_len(name, k), k) for k in board_map]
    joints = [(j, k) for j, k in joints if j]
    if joints:                     # 接合最长者优先，同长取板块名最短的
        return board_map[min(joints, key=lambda jk: (-jk[0], len(jk[1])))[1]]
    return None


def match_board(name, board_map):
    """板块名 → 板块指数。

    「AI算力/服务器产业链」这类斜杠合称在板块上是合法的（东财自己的命名风格就是
    如此），故整体匹配不上时再拆开逐段试一次。
    """
    if not board_map:
        return None
    n = name.strip()
    if not n:
        return None
    hit = _match_one(n, board_map)
    if hit:
        return hit
    for part in (p.strip() for p in n.split("/")):
        if part:
            hit = _match_one(part, board_map)
            if hit:
                return hit
    return None


# ---------------------------------------------------------------- LLM 选股

PROMPT_HEAD = """你是 A 股研究助理。下面是今日新闻摘要、一份已完成的市场分析、以及相关个股的当日行情。

请从中挑出 2-{max_stocks} 只个股 + 0-{max_boards} 个板块，作为「今日候选观察清单」。

## 硬约束（违反即废稿）
1. 【只能从给定材料里选】候选必须出自市场分析的「值得关注的板块」或「新闻涉及的个股」，
   不得引入材料里没有的标的。**没有够格的标的时宁可比 2 只还少，也不要凑数。**
2. 【单一标的】name 必须是单一股票名或单一板块名。
   个股：禁止 "A/B" 斜杠合称（如「零跑科技/零跑汽车」）、禁止「等」、禁止并列。
   板块：可以用板块的通用叫法（「AI算力/服务器产业链」这种斜杠写法可以接受），
   但必须是一个主题，不能把两个不相干的板块拼在一起。
3. 【必须给出推翻条件】invalidation 必填，且必须是**可观测的证伪信号**
   （如「若公司公告否认该传闻」「若板块成交额连续两日萎缩」）。
   「注意风险」「谨慎参与」「关注后续」这类空话一律不合格。
4. 【禁止投资指令】不得出现「建议买入」「建议卖出」「可以抄底」「应该止损」
   「马上买入」等表述。用「纳入观察」「值得跟踪」。
5. 【置信度】每条标 高/中/低。单源待核实(unve)新闻支撑的最高只能给「中」。
6. 【依据编号】basis_refs 列出支撑该候选的新闻编号（就是下面清单里的 [n]）。

## 输出格式
只输出 JSON，不要任何解释文字，不要 markdown 围栏：
{{"candidates":[{{"kind":"stock","name":"","code_hint":"","board":"",
  "logic":"一句话逻辑链","invalidation":"可观测的证伪信号","confidence":"中","basis_refs":[1,2]}}]}}

kind 取 "stock" 或 "board"；code_hint 只在个股且你知道 6 位代码时填，否则留空。

## 已完成的市场分析
{analysis}

## 相关个股当日行情
{quotes}

## 今日新闻（共 {digest_count} 条精选，编号@[]用于引用）
{digest}"""


def build_prompt(digest, digest_count, analysis_md, quotes):
    q_lines = "\n".join(
        f"- {q['name']}({q.get('code','')}) 现价 {q.get('price')} 涨跌 {q.get('change_pct')}%"
        for q in quotes[:60]) or "（今日未取到行情）"
    return PROMPT_HEAD.format(
        max_stocks=MAX_STOCKS, max_boards=MAX_BOARDS,
        analysis=analysis_md[:12000], quotes=q_lines,
        digest_count=digest_count, digest=digest)


def parse_candidates(text):
    """LLM 输出 → list[dict]。剥围栏、逐条校验，**不合格的逐条丢弃**而非整批失败"""
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    else:                                  # 没有围栏时，从第一个 { 截到最后一个 }
        i, j = t.find("{"), t.rfind("}")
        if i >= 0 and j > i:
            t = t[i:j + 1]
    try:
        data = json.loads(t)
    except json.JSONDecodeError as e:
        print(f"[warn] 候选 JSON 解析失败: {e}")
        return []
    items = data.get("candidates") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []

    out, seen = [], set()
    for it in items:
        if not isinstance(it, dict):
            continue
        kind = (it.get("kind") or "").strip().lower()
        name = re.sub(r"\s+", "", str(it.get("name") or ""))
        logic = str(it.get("logic") or "").strip()
        inval = str(it.get("invalidation") or "").strip()

        if kind not in ("stock", "board"):
            print(f"[warn] 丢弃「{name}」：kind 非法（{kind}）")
            continue
        # 个股的 name 必须能被 resolve_stock 解析：斜杠合称（「零跑科技/零跑汽车」）
        # 与并列列表喂进去必然 not_found，一律丢弃。
        # **板块不受斜杠这条约束** —— 斜杠本就是东财板块名的常见写法
        # （实测 M3 里就有「AI算力/服务器产业链」这个真实小节标题），
        # 误杀它等于让板块候选全灭；而板块匹配失败只是降级为不计分，代价低。
        max_len = 12 if kind == "stock" else 20
        too_broad = "等" in name or "、" in name or (kind == "stock" and "/" in name)
        if not (2 <= len(name) <= max_len) or too_broad:
            print(f"[warn] 丢弃「{name}」：不是单一标的")
            continue
        if len(inval) < 8:
            print(f"[warn] 丢弃「{name}」：推翻条件缺失或过短")
            continue
        if not logic:
            print(f"[warn] 丢弃「{name}」：缺逻辑链")
            continue
        # 复用 M3 的禁用词扫描，让「禁止投资指令」由代码强制而非只靠措辞
        bad, _ = M3.postcheck(logic + inval + name)
        if bad:
            print(f"[warn] 丢弃「{name}」：命中投资指令禁用词 {bad}")
            continue
        key = (kind, name)
        if key in seen:
            continue
        seen.add(key)

        conf = str(it.get("confidence") or "").strip()
        refs = it.get("basis_refs")
        out.append({
            "kind": kind, "name": name,
            "code_hint": str(it.get("code_hint") or "").strip(),
            "board": str(it.get("board") or "").strip(),
            "logic": logic, "invalidation": inval,
            "confidence": conf if conf in ("高", "中", "低") else "中",
            "basis_refs": [r for r in refs if isinstance(r, int)] if isinstance(refs, list) else [],
        })

    stocks = [c for c in out if c["kind"] == "stock"][:MAX_STOCKS]
    boards = [c for c in out if c["kind"] == "board"][:MAX_BOARDS]
    return stocks + boards


def ask_candidates(digest, digest_count, analysis_md, quotes):
    prompt = build_prompt(digest, digest_count, analysis_md, quotes)
    print(f"候选 prompt {len(prompt)} 字符，调用 DeepSeek…")
    text = M3.ds_chat([{"role": "user", "content": prompt}], max_tokens=2000)
    return parse_candidates(text)


# ---------------------------------------------------------------- 记录候选

def resolve_target(cand, board_map, cache, by_name):
    """候选 → (secid, code, market)。解析不到返回 (None, None, '')，条目仍入账但不打分"""
    if cand["kind"] == "stock":
        info = M4.resolve_stock(cand["name"], cache)
        if info:
            return info["secid"], info["code"], info.get("market", "")
        # 东财搜索接口不可用时的兜底：M4 刚跑过，quotes.json 里就有同名标的。
        # A 股 6 位代码的市场前缀是确定的（6→沪，0/3→深），这不是 M4 刻意回避的
        # 「猜市场」——那条针对的是美股/港股混在一起、前缀无从推断的情况。
        q = by_name.get(cand["name"])
        code = (q or {}).get("code") or ""
        if re.fullmatch(r"\d{6}", code):
            return (f"{'1' if code[0] == '6' else '0'}.{code}",
                    code, q.get("market", ""))
        return None, None, ""
    hit = match_board(cand["name"], board_map)
    if not hit:
        return None, None, ""
    return hit["secid"], hit["code"], "板块指数"


def base_quote(secid, code, quotes_by_code):
    """优先复用 M4 已抓的行情（省一次请求，且与日报里的行情表一致），
    仅当 quotes.json 确实是今天生成的才复用 —— 本地跑时它可能是旧的。"""
    q = quotes_by_code.get(code)
    if q and q.get("price"):
        return q["price"], q.get("prev_close"), "m4_quotes.json"
    q, src = fetch_any(secid)
    if q:
        time.sleep(1.0)     # push2 限流敏感，与 M4 一致
        return q["price"], q.get("prev_close"), src
    return None, None, None


def record(rows, today, slot, cands, bench, board_map, cache, by_code, by_name):
    """把今日候选追加进账本。已存在的 id 跳过 —— 幂等保险"""
    existing = {r.get("id") for r in rows}
    added = 0
    for cand in cands:
        # id 用内容寻址（日期+时段+类型+名称），不用序号：序号依赖列表位置，
        # 一旦某条被跳过就会让后续候选错位（曾出现两条都算成 s1、第二条
        # 压根没比对 s2 的情况）。内容寻址下同名候选天然幂等。
        cid = f"{today}-{slot}-{'s' if cand['kind'] == 'stock' else 'b'}-{cand['name']}"
        if cid in existing:
            print(f"[warn] {cid} 已存在，跳过（幂等）")
            continue
        secid, code, market = resolve_target(cand, board_map, cache, by_name)
        price = prev = src = None
        if secid:
            price, prev, src = base_quote(secid, code, by_code)
        if not price:
            print(f"[warn] 「{cand['name']}」未匹配到行情，记账但不打分")

        rows.append({
            "id": cid, "date": today, "slot": slot,
            "kind": cand["kind"], "name": cand["name"],
            "code": code, "secid": secid, "market": market,
            "base_price": price, "base_prev_close": prev,
            "quote_source": src, "bench_level": bench,
            "board": cand["board"],
            "logic": cand["logic"], "invalidation": cand["invalidation"],
            "confidence": cand["confidence"], "basis_refs": cand["basis_refs"],
            "reviews": {str(k): None for k in REVIEW_DAYS},
        })
        added += 1
        tag = "板块" if cand["kind"] == "board" else "个股"
        print(f"  + [{tag}] {cand['name']} 基准 {price} ({cand['confidence']})")
    return added


# ---------------------------------------------------------------- 到期打分

def score_pending(rows, today, bench_now):
    """到期即补：today >= 记录日 + k 天 且该档未填 → 现在就打分。

    同一候选同一天只抓一次行情，供多个到期的 k 共用。
    """
    scored = 0
    for row in rows:
        due_ks = []
        for k in REVIEW_DAYS:
            if row["reviews"].get(str(k)) is not None:
                continue
            due = _date(row["date"]) + timedelta(days=k)
            if today >= due:
                due_ks.append((k, due))
        if not due_ks:
            continue

        # 解析不到 secid 的条目：到期即标记，不再重复尝试
        if not row.get("secid"):
            for k, due in due_ks:
                row["reviews"][str(k)] = _review(due, today, status="no_quote")
            continue

        # 超过放弃期仍未补上的，按档独立标记 expired。
        # **不能拿最早那档统一判**：T+1 逾期 8 天时 T+5 可能刚到期，
        # 一并作废等于白白丢掉一条本可以打的分。
        alive = []
        for k, due in due_ks:
            if (today - due).days > EXPIRE_AFTER_DAYS:
                row["reviews"][str(k)] = _review(due, today, status="expired")
                print(f"[warn] {row['name']} T+{k} 逾期未补，标记 expired")
            else:
                alive.append((k, due))
        due_ks = alive
        if not due_ks:
            continue

        q, _src = fetch_any(row["secid"])
        if q:
            time.sleep(1.0)
        price = q.get("price") if q else None
        if not price:
            continue        # 停牌 / 限流：留待下次运行再补

        base = row.get("base_price")
        ret = round((price - base) / base, 4) if base else None
        b0 = row.get("bench_level")
        bret = round((bench_now - b0) / b0, 4) if (b0 and bench_now) else None
        alpha = round(ret - bret, 4) if (ret is not None and bret is not None) else None

        for k, due in due_ks:
            row["reviews"][str(k)] = _review(
                due, today, price=price, ret=ret, bench=bench_now,
                bench_ret=bret, alpha=alpha,
                status="ok" if alpha is not None else "no_bench")
            scored += 1
            a = f"{alpha:+.2%}" if alpha is not None else "—"
            print(f"  ~ {row['name']} T+{k} 收 {price} 收益 {ret:+.2%} 超额 {a}")
    return scored


def _review(due, done, price=None, ret=None, bench=None, bench_ret=None,
            alpha=None, status="ok"):
    return {"due": due.strftime("%Y-%m-%d"), "done": done.strftime("%Y-%m-%d"),
            "price": price, "ret": ret, "bench": bench, "bench_ret": bench_ret,
            "alpha": alpha, "status": status}


def _date(s):
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=CST)


# ---------------------------------------------------------------- 自检

def probe():
    print("=" * 52)
    print("M10 接口自检（不写账本）")
    print("=" * 52)
    ok = True

    bench, bench_q = fetch_bench()
    if bench_q:
        ready, why = close_ready(bench_q, now_cst())
        print(f"[OK]   沪深300 {bench_q['price']} ({bench_q['change_pct']}%) "
              f"报价时刻={bench_q.get('time') or '无'}")
        print(f"{'[OK]  ' if ready else '[warn]'} 收盘闸门：{why}")
        if not bench_q.get("time"):
            print("[FAIL] 腾讯源未给报价时刻 —— 闸门无法判定，复盘会 fail-closed")
            ok = False
    else:
        print("[FAIL] 沪深300 取数失败 —— 超额无法计算，且闸门 fail-closed")
        ok = False

    q, src = fetch_any("0.001216")
    if q:
        print(f"[OK]   个股样例 华瓷股份 {q['price']} ({q['change_pct']}%) 源={src}")
    else:
        print("[FAIL] 个股行情取数失败 —— 候选基准价无从锁定")
        ok = False

    bm = build_board_map()
    if bm:
        sample = list(bm.items())[:3]
        print(f"[OK]   板块映射 {len(bm)} 个，例: "
              + ", ".join(f"{k}→{v['secid']}" for k, v in sample))
    else:
        print("[FAIL] 板块列表取数失败 —— 板块候选将降级为只列不计分")
        ok = False

    for name in ("半导体", "AI算力", "算力", "不存在板块"):
        hit = match_board(name, bm)
        print(f"       匹配「{name}」→ {hit['secid'] if hit else '未匹配'}")

    print("=" * 52)
    print("[OK] 自检通过" if ok else "[FAIL] 自检未通过")
    return 0 if ok else 1


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="M10 候选清单与事后复盘")
    ap.add_argument("--probe", action="store_true", help="只自检接口，不读写账本")
    ap.add_argument("--force", action="store_true",
                    help="跳过收盘闸门与时段限制（**本地测试用，会写脏数据**）")
    ap.add_argument("--score-only", action="store_true",
                    help="只做到期复盘，不选新股（不消耗 LLM 调用）")
    ap.add_argument("--today", help="覆盖今日日期 YYYY-MM-DD（测试用）")
    args = ap.parse_args()

    if args.probe:
        return probe()

    now = now_cst()
    today = _date(args.today) if args.today else now.replace(
        hour=0, minute=0, second=0, microsecond=0)
    today_s = today.strftime("%Y-%m-%d")
    slot = detect_slot(now)
    print(f"日期 {today_s}　时段 {slot}")

    rows = load_ledger(LEDGER_PATH)
    print(f"账本 {len(rows)} 条 → {LEDGER_PATH}")

    # 0) 收盘闸门：记录与打分共同的前提，见 close_ready 的 docstring
    bench, bench_q = fetch_bench()
    ok_close, why = close_ready(bench_q, today)
    if args.force:
        print(f"[warn] --force：跳过收盘闸门（{why}）——仅限本地测试")
        ok_close = True
    else:
        print(f"{'[OK]' if ok_close else '[warn]'} 收盘闸门：{why}")
    if not ok_close and not rows:
        print("闸门未开且账本为空，无事可做")
        save_ledger(LEDGER_PATH, rows)
        return 0

    # 1) 记录今日候选（只在盘后、且今日已收盘）
    if args.score_only:
        print("--score-only：跳过选股，只做到期复盘")
    elif not ok_close:
        print("闸门未开，本轮不记录新候选（只在今日收盘后记录）")
    elif slot != "pm":
        print("盘前不记录新候选（盘前拿到的现价是昨收，当基准会把 T+1 拉成两天）；"
              "盘前只做到期复盘")
    elif not (DATA_DIR / "analysis.md").exists():
        print("[warn] data/analysis.md 不存在，跳过选股")
    else:
        try:
            news = json.loads(
                (DATA_DIR / "structured_news.json").read_text(encoding="utf-8"))["news"]
            quotes_json = json.loads(
                (DATA_DIR / "quotes.json").read_text(encoding="utf-8"))
            analysis_md = (DATA_DIR / "analysis.md").read_text(encoding="utf-8")

            # 只有当天生成的 quotes.json 才能复用，否则本地跑会拿到旧价
            fresh = quotes_json.get("generated_at", "").startswith(today_s)
            by_code = ({q["code"]: q for q in quotes_json.get("quotes", [])}
                       if fresh else {})
            by_name = ({q["name"]: q for q in quotes_json.get("quotes", [])}
                       if fresh else {})
            if not fresh:
                print(f"[warn] quotes.json 生成于 {quotes_json.get('generated_at','?')}，"
                      "非今日，基准价将现抓")

            digest, count = M3.build_news_digest(news)
            cands = ask_candidates(digest, count, analysis_md,
                                   quotes_json.get("quotes", []))
            if not cands:
                print("[warn] 未产出合格候选（LLM 失败或全部未通过校验）")
            else:
                board_map = build_board_map() if any(
                    c["kind"] == "board" for c in cands) else {}
                n = record(rows, today_s, slot, cands, bench, board_map, {},
                           by_code, by_name)
                print(f"[OK] 记录 {n} 条候选")
        except Exception as e:
            # 可选产出：选股失败不该影响复盘，也不该掐断主链路
            print(f"[warn] 选股失败（不影响复盘）: {type(e).__name__}: {str(e)[:80]}")

    # 2) 给到期候选打分
    if not ok_close:
        print("闸门未开，本轮不打分（到期即补，下次运行会补上）")
    elif any(_has_due(r, today) for r in rows):
        n = score_pending(rows, today, bench)
        print(f"[OK] 回填 {n} 档复盘")
    else:
        print("今日无到期复盘")

    if save_ledger(LEDGER_PATH, rows):
        print(f"[OK] 账本已写回 {LEDGER_PATH}（{len(rows)} 条）")
    else:
        print(f"[OK] 账本无变化（{len(rows)} 条）")
    return 0


def _has_due(row, today):
    return any(row["reviews"].get(str(k)) is None
               and today >= _date(row["date"]) + timedelta(days=k)
               for k in REVIEW_DAYS)


if __name__ == "__main__":
    sys.exit(main())
