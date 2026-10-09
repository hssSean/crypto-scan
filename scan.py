"""雲端掃描：短線 1R 助手 v3（只做多，山寨幣）。規則與 pine/short_1r_helper.pine 相同。
由 GitHub Actions 每 15 分鐘執行一次；有訊號就傳 Telegram（或 ntfy），並追蹤到目標／止損後再通知一次。

規則：日線＋4h 綠燈（Supertrend(10,2) 與 EMA20±0.5ATR 一致）時，15 分 RSI(14) 跌破 30，
      止損＝1 倍 1h ATR（≥1%），目標＝1R，加分 ≥ MIN_SCORE（8 項，見 README）。
資料：幣安公開 API。合約 API（fapi）連不上（例如美國 IP）時自動改用現貨公開資料 data-api.binance.vision。

本機測試：
  python scan.py --dry            掃一次，只印出不傳送
  python scan.py --replay 7       用最近 7 天資料模擬會收到哪些訊號（不傳送）
  python scan.py --test           傳一則測試訊息
環境變數：TG_TOKEN、TG_CHAT（Telegram）、NTFY_TOPIC（ntfy，可選）
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests

HERE = Path(__file__).parent
CFG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
if os.environ.get("EQUITY_USDT"):                           # 帳戶資金可放在 GitHub Secrets，不公開
    CFG["equity_usdt"] = float(os.environ["EQUITY_USDT"])
STATE = HERE / "state.json"
TW = timezone(timedelta(hours=8))
M15, H1, H4, D1 = 900_000, 3_600_000, 14_400_000, 86_400_000
EXCLUDE = {"BTC", "ETH", "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "EUR", "USDE", "PAXG", "XAUT", "WBTC", "WBETH",
           "BTCDOM", "AEUR", "EURI", "USD1", "BFUSD", "RLUSD"}
# 代幣化股票／商品（現貨常見「代號＋B」，例如 MSTRB、CRCLB），不是加密貨幣，回測也排除
TRADFI = set("AAPL AMZN AVGO BABA COIN CRCL EWJ EWY GOOGL GOOG HOOD INTC META MSFT MSTR MU NVDA PLTR QQQ SPY TSLA TSM "
             "SNDK SPCX AMD NFLX ORCL XAU XAG XPD XPT CL BZ NATGAS COPPER "
             "SOXL TQQQ SQQQ SPXL UPRO TNA NVDL TSLL MSTU CONL GLD SLV USO TLT IWM DIA ARKK".split())
GREEN = {}                                                      # 本輪各幣「日線＋4h 都綠」（每日狀態用）
S = requests.Session()
SRC = {"base": "https://fapi.binance.com", "kl": "/fapi/v1/klines", "tick": "/fapi/v1/ticker/24hr", "btc": "BTCUSDT"}


def get(path, **p):
    for i in range(4):
        try:
            r = S.get(SRC["base"] + path, params=p, timeout=20)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (451, 403) and "fapi" in SRC["base"]:
                raise PermissionError(r.status_code)
            if r.status_code in (418, 429):
                time.sleep(10)
                continue
        except PermissionError:
            raise
        except requests.RequestException:
            time.sleep(2 ** i)
    raise RuntimeError(f"API 失敗 {path} {p}")


def pick_source():
    try:
        get("/fapi/v1/time")
    except Exception:
        SRC.update(base="https://data-api.binance.vision", kl="/api/v3/klines", tick="/api/v3/ticker/24hr")
    print("資料來源：", SRC["base"])


def kl(sym, interval, limit):
    k = get(SRC["kl"], symbol=sym, interval=interval, limit=limit)
    now = int(time.time() * 1000)
    k = [x for x in k if int(x[6]) < now]                         # 只要已收盤
    a = np.array([[float(x[i]) for i in (0, 1, 2, 3, 4)] for x in k])
    return a[:, 0].astype(np.int64), a[:, 1], a[:, 2], a[:, 3], a[:, 4]


# ───────────── 與 Pine 相同的計算（ta.ema / ta.rma 以 SMA 起算）─────────────
def seed(x, n, alpha):
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    out[n - 1] = np.mean(x[:n])
    for i in range(n, len(x)):
        out[i] = alpha * x[i] + (1 - alpha) * out[i - 1]
    return out


def ema(x, n):
    return seed(x, n, 2 / (n + 1))


def atr(h, l, c, n=14):
    pc = np.r_[np.nan, c[:-1]]
    tr = np.where(np.isnan(pc), h - l, np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc))))
    return seed(tr, n, 1 / n)


def rsi(c, n=14):
    d = np.diff(c, prepend=np.nan)
    up, dn = np.where(d > 0, d, 0.0), np.where(d < 0, -d, 0.0)
    up[0] = dn[0] = 0.0
    ru, rd = seed(up[1:], n, 1 / n), seed(dn[1:], n, 1 / n)
    out = np.r_[np.nan, 100 - 100 / (1 + ru / np.where(rd > 0, rd, np.nan))]
    return np.where(np.r_[False, rd == 0], 100.0, out)


def light(h, l, c):
    """v10 標準版：Supertrend(10,2) 方向與 EMA20±0.5ATR 一致才算（每根收盤後的狀態）。"""
    a10 = atr(h, l, c, 10)
    src = (h + l) / 2
    ub, lb = src + 2.0 * a10, src - 2.0 * a10
    st = np.full(len(c), np.nan)
    d = np.zeros(len(c))
    pu = pl = np.nan
    for i in range(len(c)):
        u, lo = ub[i], lb[i]
        ppl, ppu = (0.0 if np.isnan(pl) else pl), (0.0 if np.isnan(pu) else pu)
        if not np.isnan(lo):
            lo = lo if (lo > ppl or (i > 0 and c[i - 1] < ppl)) else ppl
        if not np.isnan(u):
            u = u if (u < ppu or (i > 0 and c[i - 1] > ppu)) else ppu
        if i == 0 or np.isnan(a10[i - 1]):
            d[i] = 1
        elif st[i - 1] == pu:
            d[i] = -1 if c[i] > u else 1
        else:
            d[i] = 1 if c[i] < lo else -1
        st[i] = lo if d[i] == -1 else u
        pu, pl = u, lo
    stdir = np.where(d < 0, 1, -1)
    e20, a14 = ema(c, 20), atr(h, l, c, 14)
    hy, out = 0, np.zeros(len(c), int)
    for i in range(len(c)):
        if not np.isnan(e20[i]) and not np.isnan(a14[i]):
            if c[i] > e20[i] + 0.5 * a14[i]:
                hy = 1
            elif c[i] < e20[i] - 0.5 * a14[i]:
                hy = -1
        out[i] = hy if stdir[i] == hy else 0
    return out


def pivots(x, L, hi):
    """與 ta.pivothigh / ta.pivotlow 相同概念：左右各 L 根都比它低（高）。"""
    out = np.zeros(len(x), bool)
    for p in range(L, len(x) - L):
        w = x[p - L:p + L + 1]
        out[p] = (x[p] == w.max() if hi else x[p] == w.min()) and (w == x[p]).sum() == 1
    return out


def res_above(t4, h4, l4, ph, pl, a4, j4, entry):
    """4h 已確認轉折、最近 30 天、0.5×ATR 內合併；回傳進場價上方最近壓力區的下緣（沒有則 None）。"""
    conf, A = j4 - 5, a4[j4]
    Z = []
    for q in range(max(0, conf - 200), conf + 1):
        if t4[q] < t4[j4] - 30 * D1 or not (ph[q] or pl[q]):
            continue
        for p in ([h4[q]] if ph[q] else []) + ([l4[q]] if pl[q] else []):
            for z in Z:
                if abs(z[0] - p) <= 0.5 * A:
                    z[0] = (z[0] * z[1] + p) / (z[1] + 1)
                    z[1] += 1
                    break
            else:
                Z.append([p, 1])
    bots = [z[0] - 0.25 * A for z in Z if z[0] - 0.25 * A > entry]
    return min(bots) if bots else None


def last_closed(t, ms, tc):
    return np.searchsorted(t + ms, tc, side="right") - 1


def wr(s):
    return "約 66–70%" if s >= 7 else "約 62–64%" if s == 6 else "約 59–63%" if s == 5 else "約 56–59%" if s == 4 else "約 43–50%"


# ───────────── 幣池：前 50 名（30 日平均成交額），每天更新一次 ─────────────
def universe(st):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if st.get("uni_date") == today and st.get("uni"):
        return st["uni"]
    tick = get(SRC["tick"])
    if "fapi" in SRC["base"]:                                   # 合約：只留加密貨幣（排除美股、港股、商品等 TradFi 合約）
        coins = {x["symbol"] for x in get("/fapi/v1/exchangeInfo")["symbols"] if x.get("underlyingType") == "COIN"}
        tick = [x for x in tick if x["symbol"] in coins]
    cand = []
    for x in tick:
        s = x["symbol"]
        if not s.endswith("USDT"):
            continue
        base = s[:-4]
        if base in EXCLUDE or base.endswith(("UP", "DOWN", "BULL", "BEAR")) or base in TRADFI or base[:-1] in TRADFI:
            continue
        cand.append((float(x["quoteVolume"]), s))
    cand = [s for _, s in sorted(cand, reverse=True)[:max(90, int(CFG['top_n'] * 1.8))]]
    vol30 = {}
    for s in cand:
        try:
            k = get(SRC["kl"], symbol=s, interval="1d", limit=32)[:-1][-30:]
            qv = [float(x[7]) for x in k]
            hi, lo = max(float(x[2]) for x in k), min(float(x[3]) for x in k)
            if len(qv) >= 30 and lo > 0 and hi / lo - 1 >= 0.03:   # 30 天振幅不到 3% → 穩定幣類（XUSD、U…），排除
                vol30[s] = float(np.mean(qv))
        except Exception:
            continue
    uni = sorted(vol30, key=vol30.get, reverse=True)[:CFG["top_n"]]
    st["uni"], st["uni_date"] = uni, today
    return uni


# ───────────── 掃描一個幣：回傳最近幾根 15m 的訊號 ─────────────
def btc_ctx():
    t4, o4, h4, l4, c4 = kl(SRC["btc"], "4h", 500)
    t15, o15, h15, l15, c15 = kl(SRC["btc"], "15m", 1000)
    return (t4, light(h4, l4, c4)), dict(zip(t15.tolist(), rsi(c15).tolist()))


def analyse(sym, btc, since_ms):
    (bt4, bl4), brsi = btc
    tD, oD, hD, lD, cD = kl(sym, "1d", 1000)
    t4, o4, h4, l4, c4 = kl(sym, "4h", 1000)
    t1, o1, h1, l1, c1 = kl(sym, "1h", 300)
    t, o, h, l, c = kl(sym, "15m", 1000)
    if len(cD) < 60 or len(c4) < 60 or len(c) < 60:
        return [], (t, h, l, c)
    LtD, l4s = light(hD, lD, cD), light(h4, l4, c4)              # 燈號另存，不要蓋掉日線最低價 lD
    GREEN[sym] = bool(LtD[-1] == 1 and l4s[-1] == 1)
    eD, aD = ema(cD, 20), atr(hD, lD, cD, 14)
    e4 = ema(c4, 20)
    a4 = atr(h4, l4, c4, 14)
    ph4, pl4 = pivots(h4, 5, True), pivots(l4, 5, False)
    a1 = atr(h1, l1, c1, 14)
    r = rsi(c)
    a15 = atr(h, l, c, 14)
    out = []
    for k in range(20, len(c)):
        tc = t[k] + M15
        if tc <= since_ms:
            continue
        jD, j4, j1, jb = last_closed(tD, D1, tc), last_closed(t4, H4, tc), last_closed(t1, H1, tc), last_closed(bt4, H4, tc)
        if min(jD, j4, j1, jb) < 0:
            continue
        dip = r[k] < 30 and r[k - 1] >= 30                    # 急跌：RSI 跌破 30
        shallow = r[k] < 40 and r[k - 1] >= 40                # 淺回調：RSI 跌破 40（需加分 ≥6）
        if not (LtD[jD] == 1 and l4s[j4] == 1 and (dip or (shallow and CFG.get("shallow_pullback", True)))):
            continue
        atr1 = a1[j1]
        if not np.isfinite(atr1) or atr1 <= 0:
            continue
        br = brsi.get(int(t[k]), np.nan)
        items = {
            "BTC4h綠": bl4[jb] == 1,
            "BTC急跌": br < 36,
            "波動大": a15[k] / c[k] >= 0.0102,
            "4小時急跌": (c[k] - c[k - 16]) / atr1 <= -2.1,
            "RSI很低": r[k] < 27.9,
            "週末": datetime.fromtimestamp(t[k] / 1000, timezone.utc).weekday() >= 5,
            "在4h均線上": c[k] > e4[j4],
            "日線強": (cD[jD] - eD[jD]) / aD[jD] >= 1.7,
        }
        score = int(sum(bool(v) for v in items.values()))
        res = res_above(t4, h4, l4, ph4, pl4, a4, j4, c[k]) if np.isfinite(a4[j4]) else None
        # 先全部收集（廣度要算所有急跌事件），過濾在 finalize() 做
        out.append(dict(sym=sym, bar=int(t[k]), entry=float(c[k]), stop=float(c[k] - atr1), target=float(c[k] + atr1),
                        score=score, items=[n for n, v in items.items() if v], res=res, kind="急跌" if dip else "淺回調",
                        stop_ok=bool(atr1 / c[k] >= CFG["min_stop_pct"] / 100),
                        res_ok=not (CFG.get("skip_near_resistance", True) and res is not None and res - c[k] < 0.5 * atr1)))
    return out, (t, h, l, c)


def finalize(cands, since_ms):
    """算廣度（過去 1 小時內同樣出現『日線＋4h 綠、RSI 跌破 30』的幣數，含自己），再套用所有過濾。"""
    dips = [(c_["bar"], c_["sym"]) for c_ in cands if c_["kind"] == "急跌"]
    out = []
    for s in cands:
        if s["bar"] + M15 <= since_ms or not (s["stop_ok"] and s["res_ok"]):
            continue
        s["breadth"] = len({sym for b, sym in dips if s["bar"] - H1 <= b <= s["bar"]})
        if s["kind"] == "急跌":
            if s["score"] < CFG["min_score"] or s["breadth"] < CFG.get("min_breadth", 1):
                continue
        elif s["score"] < max(6, CFG["min_score"]):
            continue
        out.append(s)
    return out


# ───────────── 通知 ─────────────
def send(title, body, dry):
    msg = f"{title}\n{body}"
    print("\n" + msg)
    if dry:
        return
    tok, chat, topic = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT"), os.environ.get("NTFY_TOPIC")
    if tok and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", data={"chat_id": chat, "text": msg}, timeout=20).raise_for_status()
        except Exception as e:
            print("Telegram 失敗：", e)
    if topic:
        try:
            requests.post("https://ntfy.sh/", json={"topic": topic, "title": title, "message": body}, timeout=20).raise_for_status()
        except Exception as e:
            print("ntfy 失敗：", e)


def fmt(x):
    return f"{x:.6g}"


def signal_msg(s, last_close):
    rk = s["entry"] - s["stop"]
    qty = CFG["equity_usdt"] * CFG["risk_pct"] / 100 / rk
    bar = datetime.fromtimestamp((s["bar"] + M15) / 1000, TW).strftime("%m/%d %H:%M")
    moved = (last_close - s["entry"]) / rk
    hint = "可以照計畫進" if -0.5 < moved < 0.3 else ("已往目標走一段，別追" if moved >= 0.3 else "已接近止損，小心")
    res_txt = "30 天內沒有" if s.get("res") is None else f"{fmt(s['res'])}（離進場 {(s['res'] - s['entry']) / rk:.1f}R）"
    kind = s.get("kind", "急跌")
    b = s.get("breadth", 0)
    if kind != "急跌":
        rate = "約 58–65%"
    elif b >= 25:
        kind, rate = "🔥全市場洗盤", "約 66–74%" if s["score"] >= 5 else "約 66–70%"
    else:
        rate = wr(s["score"]) if b < 4 else {4: "約 57–60%", 5: "約 59–63%"}.get(s["score"], "約 62–66%")
    title = f"【{s['sym'].replace('USDT', '')} 做多・{kind}】加分 {s['score']}/8（歷史勝率{rate}）"
    body = (f"訊號 K 線收盤：{bar}（台灣）\n"
            f"進場 {fmt(s['entry'])}｜止損 {fmt(s['stop'])}（{-rk / s['entry']:+.2%}）｜目標 {fmt(s['target'])}\n"
            f"數量約 {qty:.4g} 顆（打到止損賠 {CFG['equity_usdt'] * CFG['risk_pct'] / 100:.2f}U）\n"
            f"現價 {fmt(last_close)}（{(last_close / s['entry'] - 1):+.2%}）→ {hint}\n"
            f"上方壓力：{res_txt}\n"
            f"同時急跌：{b} 個幣（1 小時內，前 {CFG['top_n']} 名）\n"
            f"符合：{'、'.join(s['items'])}")
    return title, body


# ───────────── 主流程 ─────────────
def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def status_msg(cands, btc, n_uni, now):
    """每日狀態：讓你知道掃描器正常，以及今天為什麼有／沒有訊號。"""
    bl4 = btc[0][1]
    btc_light = {1: "綠", -1: "紅"}.get(int(bl4[-1]), "灰")
    green = sum(GREEN.values())
    day0 = now - D1
    dips = [c_ for c_ in cands if c_["kind"] == "急跌" and c_["bar"] + M15 > day0]
    passed = finalize(cands, day0)
    hint = ("目前多數幣不在多頭（這套規則只在『日線＋4h 綠』的幣急跌時出手），沒有訊號是正常的。"
            "歷史上約 7 成的日子沒有訊號，連續好幾天沒有也很常見。"
            if btc_light != "綠" or green < 15 else "多頭環境，出現急跌就會通知。")
    title = f"【1R 助手】每日狀態 {datetime.now(TW).strftime('%m/%d')}"
    body = (f"掃描器正常（每 15 分鐘，{n_uni} 個幣）\n"
            f"BTC 4h 燈：{btc_light}\n"
            f"日線＋4h 都綠的幣：{green} 個\n"
            f"過去 24 小時：急跌候選 {len(dips)} 個，符合全部條件 {len(passed)} 個\n"
            f"{hint}")
    return title, body


def run(dry):
    st = load_state()
    st.setdefault("active", {})
    now = int(time.time() * 1000)
    since = now - CFG["lookback_min"] * 60_000                  # 雲端排程可能延遲，往回看一段時間
    tw_now = datetime.now(TW)
    status_due = (CFG.get("daily_status", True) and tw_now.hour >= 8
                  and st.get("status_date") != tw_now.strftime("%Y-%m-%d"))
    an_since = min(since, now - D1) if status_due else since     # 每日狀態要統計過去 24 小時
    uni = universe(st)
    btc = btc_ctx()
    found = 0
    cands, last_close = [], {}
    for sym in uni:
        try:
            cs, (t, h, l, c) = analyse(sym, btc, an_since - H1)    # 多抓 1 小時給廣度用
        except Exception as e:
            print(sym, "失敗：", e)
            continue
        cands += cs
        last_close[sym] = float(c[-1])
        a = st["active"].get(sym)
        if a:                                                   # 追蹤進行中的訊號
            m = t > a["bar"]
            hit_s = np.flatnonzero(m & (l <= a["stop"]))
            hit_t = np.flatnonzero(m & (h >= a["target"]))
            fs = hit_s[0] if len(hit_s) else 10**9
            ft = hit_t[0] if len(hit_t) else 10**9
            name = sym.replace("USDT", "")
            if fs < 10**9 and fs <= ft:
                send(f"【{name}】✗ 止損", f"止損 {fmt(a['stop'])}，這筆結束（-1R）", dry)
                del st["active"][sym]
            elif ft < 10**9:
                send(f"【{name}】✓ 到目標", f"目標 {fmt(a['target'])}，這筆結束（+1R）", dry)
                del st["active"][sym]
            elif now - a["bar"] > 12 * H1:
                r = (c[-1] - a["entry"]) / (a["entry"] - a["stop"])
                send(f"【{name}】⏱ 12 小時未觸及", f"現價 {fmt(c[-1])}（{r:+.2f}R），這筆結束", dry)
                del st["active"][sym]
        time.sleep(0.05)
    latest = {}
    for s in finalize(cands, since):
        latest[s["sym"]] = s                                    # 每個幣只取最新一筆
    for sym, s in latest.items():
        if sym in st["active"]:
            continue                                            # 一次一筆
        send(*signal_msg(s, last_close[sym]), dry)
        st["active"][sym] = s
        found += 1
    if status_due:
        send(*status_msg(cands, btc, len(uni), now), dry)
        st["status_date"] = tw_now.strftime("%Y-%m-%d")
    print(f"\n掃描完成：{len(uni)} 個幣，新訊號 {found} 個，追蹤中 {len(st['active'])} 個")
    if not dry:
        STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def replay(days):
    st = {}
    uni = universe(st)
    btc = btc_ctx()
    since = int(time.time() * 1000) - days * D1
    cands = []
    for sym in uni:
        try:
            cs, _ = analyse(sym, btc, since - H1)
            cands += cs
        except Exception as e:
            print(sym, "失敗：", e)
    allsig, last = [], {}
    for s in sorted(finalize(cands, since), key=lambda s: s["bar"]):   # 同幣 12 小時內只算一次
        if s["bar"] - last.get(s["sym"], -10**15) >= 12 * H1:
            allsig.append(s)
            last[s["sym"]] = s["bar"]
    for s in allsig:
        print(datetime.fromtimestamp((s["bar"] + M15) / 1000, TW).strftime("%m/%d %H:%M"), s["sym"], s["kind"], f"{s['score']}/8",
              f"同時急跌 {s['breadth']} 個", "進場", fmt(s["entry"]), "止損", fmt(s["stop"]))
    print(f"\n最近 {days} 天（最低加分 {CFG['min_score']}、同時急跌 ≥{CFG.get('min_breadth', 1)}）：{len(allsig)} 個訊號，"
          f"平均每天 {len(allsig) / days:.1f} 個，其中全市場洗盤（≥25）{sum(s['breadth'] >= 25 for s in allsig)} 個")


if __name__ == "__main__":
    for f in (sys.stdout, sys.stderr):
        try:
            f.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--replay", type=int, default=0)
    a = ap.parse_args()
    pick_source()
    if a.test or os.environ.get("TEST_MESSAGE") == "true":
        send("【1R 助手】測試訊息", "收到這則就代表通知設定成功。", False)
    elif a.replay:
        replay(a.replay)
    else:
        run(a.dry)
