"""A股资讯中心 - 本地服务：静态页面 + 行情/K线/新闻/公告 数据代理（仅标准库）。
运行：python server.py  →  http://localhost:8765
"""
import json, re, time, calendar, threading, os, sys
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.parse import urlparse, parse_qs, quote

ROOT = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"

_cache, _lock = {}, threading.Lock()


def fetch(url, headers=None, timeout=8, enc="utf-8"):
    h = {"User-Agent": UA}
    h.update(headers or {})
    with urlopen(Request(url, headers=h), timeout=timeout) as r:
        return r.read().decode(enc, errors="replace")


def cached(key, ttl, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    try:
        val = fn()
    except Exception:
        if hit:
            return hit[1]
        raise
    with _lock:
        _cache[key] = (now, val)
    return val


# ---------------------------------------------------------------- 代码转换
def sina_code(code):
    code = code.lower()
    if code[:2] in ("sh", "sz", "bj"):
        return code
    return ("sh" if code[0] in "69" else "bj" if code[0] in "48" or code.startswith("92") else "sz") + code


def secid(code):
    c = sina_code(code)
    return ("1." if c.startswith("sh") else "0.") + c[2:]


# ---------------------------------------------------------------- 行情
def quotes(codes):
    codes = [sina_code(c) for c in codes if c]
    if not codes:
        return []
    txt = fetch("https://hq.sinajs.cn/list=" + ",".join(codes),
                {"Referer": "https://finance.sina.com.cn"}, enc="gbk")
    out = []
    for m in re.finditer(r'hq_str_(\w+)="([^"]*)"', txt):
        code, raw = m.group(1), m.group(2).split(",")
        if len(raw) < 32 or not raw[0]:
            continue
        f = lambda i: float(raw[i] or 0)
        prev, price = f(2), f(3)
        if price == 0:  # 停牌/未开盘
            price = prev
        book = lambda s: [{"p": f(s + 2 * i + 1), "v": int(f(s + 2 * i) / 100)} for i in range(5)]
        out.append({
            "code": code, "name": raw[0].replace(" ", ""), "open": f(1), "prev": prev, "price": price,
            "high": f(4), "low": f(5), "vol": int(f(8) / 100), "amount": f(9),
            "chg": round(price - prev, 3), "pct": round((price - prev) / prev * 100, 2) if prev else 0,
            "bids": book(10), "asks": book(20), "date": raw[30], "time": raw[31],
        })
    return out


def indices():
    txt = fetch("https://hq.sinajs.cn/list=s_sh000001,s_sz399001,s_sz399006,s_sh000688,s_sh000300,s_bj899050",
                {"Referer": "https://finance.sina.com.cn"}, enc="gbk")
    out = []
    for m in re.finditer(r'hq_str_s_(\w+)="([^"]*)"', txt):
        r = m.group(2).split(",")
        if len(r) >= 6:
            out.append({"code": m.group(1), "name": r[0], "price": float(r[1]), "chg": float(r[2]),
                        "pct": float(r[3]), "amount": float(r[5]) * 1e4})
    return out


def kline(code, n=320):
    d = json.loads(fetch(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s&fields1=f1,f2,f3&"
        "fields2=f51,f52,f53,f54,f55,f56,f57&klt=101&fqt=1&end=20500101&lmt=%d" % (secid(code), n)))
    rows = []
    for k in (d.get("data") or {}).get("klines", []):
        t, o, c, h, l, v, a = k.split(",")
        rows.append([t, float(o), float(c), float(h), float(l), float(v), float(a)])
    return {"name": (d.get("data") or {}).get("name", ""), "rows": rows}


def trends(code):
    d = json.loads(fetch(
        "https://push2his.eastmoney.com/api/qt/stock/trends2/get?secid=%s&fields1=f1,f2,f3,f4,f5,f6,f7,f8,"
        "f9,f10,f11,f12,f13&fields2=f51,f52,f53,f54,f55,f56,f57,f58&iscr=0&ndays=1" % secid(code)))
    dd = d.get("data") or {}
    pts = []
    for k in dd.get("trends", []):
        p = k.split(",")
        pts.append([p[0][-5:], float(p[2]), float(p[5]), float(p[7])])  # 时间, 价, 量(手), 均价
    return {"prev": dd.get("preClose"), "pts": pts}


def search(q):
    d = json.loads(fetch(
        "https://searchapi.eastmoney.com/api/suggest/get?input=%s&type=14&"
        "token=D43BF722C8E33BDC906FB84D85E326E8&count=8" % quote(q)))
    out = []
    for it in (d.get("QuotationCodeTable") or {}).get("Data", []) or []:
        if it.get("Classify") == "AStock" or it.get("SecurityTypeName") in ("沪A", "深A", "京A", "科创板", "创业板"):
            out.append({"code": sina_code(it["Code"]), "name": it["Name"], "type": it.get("SecurityTypeName", "")})
    return out


def announcements(code, n=8):
    c = sina_code(code)[2:]
    d = json.loads(fetch(
        "https://np-anotice-stock.eastmoney.com/api/security/ann?sr=-1&page_size=%d&page_index=1&ann_type=A&"
        "client_source=web&stock_list=%s&f_node=0&s_node=0" % (n, c)))
    out = []
    for it in (d.get("data") or {}).get("list", []):
        cols = it.get("columns") or [{}]
        out.append({"title": it["title"], "type": cols[0].get("column_name", ""),
                    "time": (it.get("notice_date") or "")[:10],
                    "url": "https://data.eastmoney.com/notices/detail/%s/%s.html" % (c, it["art_code"])})
    return out


# ---------------------------------------------------------------- 新闻与可信度分级
# 层级：A 官方/法定披露  B 主流财经媒体/外媒  C 门户·聚合转载(出处不明)  D 传闻/未证实
TIER_A = ["中国证监会", "证监会", "上交所", "深交所", "北交所", "上海证券交易所", "深圳证券交易所", "北京证券交易所",
          "中国人民银行", "央行", "国务院", "国家统计局", "财政部", "发改委", "国家发展改革委", "工信部", "商务部",
          "外汇局", "金融监管总局", "国家金融监督管理总局", "海关总署", "新华社", "央视", "人民日报", "经济日报", "求是",
          "上海证券报", "中国证券报", "证券时报", "证券日报", "中证网", "人民网", "参考消息", "中新网", "光明日报", "科技日报", "新华财经", "上证报", "中证报", "中国政府网",
          "中国结算", "中金所", "证券交易所", "公司公告", "上市公司公告", "公告称", "发布公告", "互动平台", "互动易", "e互动"]
TIER_B = ["财联社", "第一财经", "界面新闻", "澎湃新闻", "21世纪经济报道", "每日经济新闻", "经济观察报", "财新", "华尔街见闻",
          "券商中国", "中国基金报", "中新经纬", "中新社", "界面", "上游新闻", "时代周报", "证券之星", "智通财经",
          "路透", "彭博", "华尔街日报", "金融时报", "CNBC", "美联社", "日经", "纽约时报", "FT", "Bloomberg", "Reuters",
          "国际金融报", "华夏时报", "中国经营报", "北京商报", "红周刊", "新京报", "南方都市报", "红星新闻", "极目新闻", "财经网", "澎湃", "观察者网", "证券市场周刊", "上海证券报·中国证券网", "中国证券网", "央广网", "中国日报", "环球时报", "第一财经日报", "上海证券交易所官网", "21财经"]
TIER_C = ["新浪财经", "新浪港股", "新浪证券", "新浪科技", "东方财富", "东财", "同花顺", "雪球", "网易", "腾讯", "搜狐",
          "凤凰", "百度", "今日头条", "金融界", "和讯", "股吧", "市场资讯", "滚动播报", "环球市场播报", "微博", "自媒体"]
RUMOR_KW = ["传闻", "网传", "据传", "传言", "小作文", "消息人士", "知情人士", "市场消息", "市场传", "未经证实", "未证实",
            "爆料", "内幕", "疑似", "据说", "匿名", "朋友圈", "有消息称", "业内人士", "接近.{0,6}人士",
            "熟悉.{0,8}人士", "据市场", "群聊", "社交媒体", "社交平台"]
SOFT_KW = ["据悉", "或将", "有望", "料将", "预计", "拟", "有传", "不排除", "可能"]
DEBUNK_KW = ["辟谣", "澄清", "不属实", "否认", "没有.{0,6}业务", "没有.{0,4}产品", "系谣言", "不实"]

ORIGIN_RES = [
    re.compile(r"[（(]\s*来源[:：]\s*([^）)\s，。]{2,14})"),
    re.compile(r"来源[:：]\s*([^\s，。）)]{2,14})"),
    re.compile(r"据\s*([一-龥A-Za-z0-9·]{2,14}?)\s*(?:\d{1,2}月\d{1,2}日)?\s*(?:报道|援引|引述|电|讯)"),
    re.compile(r"^[【\[]?[^】\]]{0,60}[】\]]?\s*([一-龥A-Za-z]{2,10})\s*\d{1,2}月\d{1,2}日\s*(?:电|讯|消息)"),
    re.compile(r"([一-龥A-Za-z]{2,10})\s*\d{1,2}月\d{1,2}日\s*(?:电|讯|消息)"),
]


def _match(name, table):
    for t in table:
        if t in name:
            return t
    return None


def classify(title, body, channel_name, channel):
    text = (title + " " + body)
    head = text[:140]
    origin = None
    for rx in ORIGIN_RES:
        m = rx.search(head if rx is ORIGIN_RES[3] else text[:300])
        if m:
            origin = m.group(1).strip()
            if rx not in ORIGIN_RES[:2] and not any(_match(origin, t) for t in (TIER_A, TIER_B, TIER_C)):
                origin = None  # 电头多为地名而非媒体，无法验证则丢弃
                continue
            break
    tier, hit = None, None
    if re.search(r"互动平台|互动易|e互动", text):
        tier, origin = "A", "上市公司互动平台"
    elif not re.search(r"涨|跌|早盘|午前|盘中|收涨|收跌", title) and re.search(r"(发布|披露|刊发).{0,6}公告|公告称|公告显示|公告[,，]", text[:120]):
        tier, origin = "A", "上市公司公告"
    for src in ([] if tier else filter(None, [origin, channel_name])):
        for tid, table in (("A", TIER_A), ("B", TIER_B)):
            h = _match(src, table)
            if h:
                tier, hit = tid, h
                break
        if tier:
            break
    if not tier:
        tier = "C"
    flags = []
    rumor = [k for k in RUMOR_KW if re.search(k, text)]
    debunk = [k for k in DEBUNK_KW if re.search(k, text)]
    soft = [k for k in SOFT_KW if k in text]
    if debunk:
        flags.append("澄清/辟谣")
    if rumor and tier != "A":
        tier = "D"
        flags.append("含传闻措辞：" + "、".join(re.sub(r"[.{}0-9,]", "", r)[:6] for r in rumor[:3]))
    elif rumor:
        flags.append("官方源但含传闻措辞，注意区分")
    if soft and tier in ("C", "D"):
        flags.append("预测性表述：" + "、".join(soft[:3]))
    shown_origin = hit if hit and not origin else (origin or "")
    if tier == "C" and not origin:
        shown_origin = ""
    notes = {
        "A": "官方/法定披露渠道，可信度最高，仍建议核对原文。",
        "B": "主流财经媒体，有采编责任，重大消息建议交叉印证。",
        "C": "门户/聚合转载%s，未见可核实的原始出处，勿单凭此决策。" % ("（出处：" + origin + "）" if origin else ""),
        "D": "传闻/未证实信息，可能是“小作文”，请等待公司公告或交易所披露后再判断。",
    }
    return {"tier": tier, "origin": shown_origin or origin or "", "flags": flags, "note": notes[tier]}


def clean(s):
    s = re.sub(r"<[^>]+>", "", s or "")
    return re.sub(r"\s+", " ", s).strip()


def news_sina(lid, num=40):
    d = json.loads(fetch("https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=%d&num=%d&page=1" % (lid, num)))
    out = []
    for it in d["result"]["data"]:
        title = clean(it.get("title"))
        body = clean(it.get("intro") or it.get("summary") or "")
        c = classify(title, body, it.get("media_name", ""), "新浪财经")
        out.append({"id": "s" + str(it.get("docid", it.get("oid", title))), "title": title, "summary": body[:200],
                    "url": it.get("url"), "ts": int(it.get("ctime") or 0), "channel": "新浪财经", **c})
    return out


def news_em(num=60):
    s = fetch("https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_%d_1_.html" % num)
    d = json.loads(s[s.index("{"):])
    out = []
    for it in d["LivesList"]:
        title = clean(it.get("title"))
        body = clean(it.get("digest"))
        body = re.sub(r"^【[^】]*】", "", body)
        ts = calendar.timegm(time.strptime(it["showtime"], "%Y-%m-%d %H:%M:%S")) - 8 * 3600  # 东财为北京时间，与服务器时区无关
        c = classify(title, body, "", "东方财富")
        out.append({"id": "e" + it["newsid"], "title": title, "summary": body[:200],
                    "url": it.get("url_w") or it.get("url_unique"), "ts": ts, "channel": "东方财富快讯", **c})
    return out


def news():
    items = []
    for fn in (lambda: news_em(60), lambda: news_sina(2516), lambda: news_sina(2509, 30)):
        try:
            items += fn()
        except Exception as e:
            print("news source failed:", e, file=sys.stderr)
    seen, out = set(), []
    for it in sorted(items, key=lambda x: -x["ts"]):
        k = re.sub(r"\W", "", it["title"])[:16]
        if k in seen or not it["title"]:
            continue
        seen.add(k)
        out.append(it)
    return out[:120]


# ---------------------------------------------------------------- HTTP
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype="application/json; charset=utf-8", code=200):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(ROOT, "index.html"), "rb") as f:
                    return self.send(f.read(), "text/html; charset=utf-8")
            codes = [c for c in q.get("codes", "").split(",") if re.fullmatch(r"(sh|sz|bj)?\d{6}", c.lower())]
            code = q.get("code", "")
            if u.path.startswith("/api/") and u.path in ("/api/kline", "/api/trends", "/api/ann") \
                    and not re.fullmatch(r"(sh|sz|bj)?\d{6}", code.lower()):
                return self.send('{"error":"bad code"}', code=400)
            if u.path == "/api/quotes":
                r = quotes(codes)
            elif u.path == "/api/indices":
                r = cached("idx", 4, indices)
            elif u.path == "/api/kline":
                r = cached("k" + code, 120, lambda: kline(code))
            elif u.path == "/api/trends":
                r = cached("t" + code, 10, lambda: trends(code))
            elif u.path == "/api/news":
                r = cached("news", 40, news)
            elif u.path == "/api/ann":
                r = cached("a" + code, 300, lambda: announcements(code))
            elif u.path == "/api/search":
                r = search(q.get("q", ""))
            else:
                return self.send('{"error":"not found"}', code=404)
            self.send(json.dumps(r, ensure_ascii=False))
        except Exception as e:
            self.send(json.dumps({"error": str(e)}, ensure_ascii=False), code=502)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8765))
    print("A股资讯中心 → http://localhost:%d" % port)
    ThreadingHTTPServer((os.environ.get("HOST", "127.0.0.1"), port), H).serve_forever()
