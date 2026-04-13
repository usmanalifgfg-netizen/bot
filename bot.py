#!/usr/bin/env python3
"""
POLYMARKET BOT - DEBUG + TRADE VERSION
Shows exactly WHY trades are/aren't being taken
"""

import requests
import time
import re
import threading
import random
from datetime import datetime, timezone

# ─────────────────────────────────────────────
# SETTINGS
# ─────────────────────────────────────────────
BANKROLL    = 5.0
DRY_RUN     = True
SCAN_EVERY  = 5
MIN_EDGE    = 0.05    # 5% (was 6% - lower)
MIN_VOLUME  = 100     # (was 200 - lower)
MIN_PRICE   = 0.88    # near-certain threshold (was 0.92)
DEBUG       = True    # Show why trades skipped
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# PnL TRACKER
# ─────────────────────────────────────────────
class PnL:
    def __init__(self, start):
        self.start    = start
        self.balance  = start
        self.trades   = []
        self.wins     = 0
        self.losses   = 0
        self.lock     = threading.Lock()

    def record(self, bet, price, conf, question, side):
        won    = random.random() < (conf / 100)
        payout = round(bet / price, 2) if won else 0.0
        net    = round(payout - bet, 2)
        with self.lock:
            self.balance += net
            if won: self.wins   += 1
            else:   self.losses += 1
            self.trades.append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "q":    question[:40],
                "side": side,
                "bet":  bet,
                "won":  won,
                "net":  net,
                "bal":  round(self.balance, 2),
            })
        return won, net

    @property
    def total(self): return len(self.trades)
    @property
    def profit(self): return round(self.balance - self.start, 2)
    @property
    def wr(self): return round(self.wins/self.total*100,1) if self.total else 0
    @property
    def roi(self): return round(self.profit/self.start*100,1)

    def show(self):
        sign = "+" if self.profit >= 0 else ""
        print(f"\n  {'='*55}")
        print(f"  📊  DRY RUN PnL  -  ALL TRADES SIMULATED")
        print(f"  {'='*55}")
        print(f"  Start Balance  : ${self.start:.2f}")
        print(f"  Now Balance    : ${self.balance:.2f}")
        print(f"  Profit / Loss  : {sign}${self.profit:.2f}")
        print(f"  ROI            : {sign}{self.roi:.1f}%")
        print(f"  {'─'*55}")
        print(f"  Trades         : {self.total}")
        print(f"  Wins           : {self.wins}  ✅")
        print(f"  Losses         : {self.losses}  ❌")
        print(f"  Win Rate       : {self.wr:.1f}%")
        print(f"  {'─'*55}")
        if self.trades:
            print(f"  LAST 5 TRADES:")
            print(f"  {'Time':<10}{'Side':<10}{'Bet':>5}  "
                  f"{'Result':<8}{'PnL':>7}  {'Bal':>7}")
            print(f"  {'-'*52}")
            for t in self.trades[-5:]:
                res = "WIN ✅ " if t["won"] else "LOSS ❌"
                pstr = f"+${t['net']:.2f}" if t["won"] else f"-${t['bet']:.2f}"
                print(f"  {t['time']:<10}{t['side']:<10}"
                      f"${t['bet']:>4.2f}  {res:<8}{pstr:>7}  ${t['bal']:>6.2f}")
        print(f"  {'='*55}\n")


tracker = PnL(BANKROLL)

# ─────────────────────────────────────────────
# BTC PRICE
# ─────────────────────────────────────────────
btc_now  = None
btc_prev = None
btc_lock = threading.Lock()

def safe_float(v, d=0.0):
    if v is None: return d
    if isinstance(v, (int, float)): return float(v)
    if isinstance(v, list): return safe_float(v[0]) if v else d
    if isinstance(v, str):
        try: return float(v.strip().replace(",",""))
        except: return d
    return d

def fetch_btc():
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price",
                         params={"symbol":"BTCUSDT"}, timeout=3)
        if r.status_code == 200:
            return safe_float(r.json().get("price"))
    except: pass
    try:
        r = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot",
                         timeout=3)
        if r.status_code == 200:
            return safe_float(r.json()["data"]["amount"])
    except: pass
    return None

def btc_thread():
    global btc_now, btc_prev
    while True:
        p = fetch_btc()
        if p:
            with btc_lock:
                btc_prev = btc_now
                btc_now  = p
        time.sleep(1)

# ─────────────────────────────────────────────
# MARKETS
# ─────────────────────────────────────────────
_mcache = []
_mtime  = 0

def load_markets():
    global _mcache, _mtime
    now = time.time()
    if _mcache and (now - _mtime) < 90:
        return _mcache
    print(f"\n  [{ts()}] 🔄 Reloading markets from Polymarket...")
    all_m = {}
    keywords = ["bitcoin","btc","5-minute","temperature","weather",
                "will","price","crypto","rain","above","below"]
    for kw in keywords:
        try:
            r = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"active":"true","closed":"false",
                        "limit":100,"search":kw},
                timeout=10
            )
            if r.status_code == 200:
                for m in r.json():
                    mid = m.get("id","")
                    if mid: all_m[mid] = m
        except: pass
        time.sleep(0.1)
    _mcache = list(all_m.values())
    _mtime  = now
    print(f"  [{ts()}] ✅ {len(_mcache)} total markets loaded")
    return _mcache

def ts():
    return datetime.now().strftime("%H:%M:%S")

def kelly(bal, conf, price):
    if price <= 0 or price >= 1: return 0.50
    b    = (1-price)/price
    p    = conf/100
    q    = 1-p
    k    = (b*p - q)/b
    frac = max(0.02, min(k*0.5, 0.15))
    return max(0.50, min(round(bal*frac,2), bal*0.20))

# ─────────────────────────────────────────────
# STRATEGY 1: NEAR CERTAIN
# Any market 88c+ with decent volume
# No order book check (it was blocking everything)
# ─────────────────────────────────────────────
def strategy_certain(markets):
    found     = 0
    skip_vol  = 0
    skip_price= 0
    opps      = []

    for m in markets:
        vol = safe_float(m.get("volumeNum",0))
        if vol < MIN_VOLUME:
            skip_vol += 1
            continue

        for t in m.get("tokens",[]):
            p   = safe_float(t.get("price",0))
            if p <= 0 or p > 1:
                continue
            if p < MIN_PRICE:
                skip_price += 1
                continue

            found += 1
            opps.append({
                "type":     "CERTAIN",
                "question": m.get("question","")[:70],
                "side":     t.get("outcome","YES").upper(),
                "token_id": t.get("token_id"),
                "price":    p,
                "expected": 0.99,
                "edge":     round(p - (MIN_PRICE - 0.02), 3),
                "conf":     round(p * 100, 1),
                "volume":   vol,
                "risk":     "LOW",
            })

    if DEBUG and not opps:
        print(f"     [CERTAIN debug] checked {len(markets)} markets | "
              f"skipped_vol:{skip_vol} skipped_price:{skip_price} found:{found}")

    opps.sort(key=lambda x: x["price"], reverse=True)
    return opps[:3]


# ─────────────────────────────────────────────
# STRATEGY 2: CLOSING SOON (< 3 hours left)
# ─────────────────────────────────────────────
def strategy_closing(markets):
    now   = datetime.now(timezone.utc)
    opps  = []
    skip_time = skip_vol = skip_price = 0

    for m in markets:
        end_str = m.get("endDateIso") or m.get("endDate","")
        if not end_str:
            continue
        try:
            end   = datetime.fromisoformat(end_str.replace("Z","+00:00"))
            hours = (end - now).total_seconds() / 3600
            if not (0.25 <= hours <= 3):
                skip_time += 1
                continue
        except:
            continue

        vol = safe_float(m.get("volumeNum",0))
        if vol < MIN_VOLUME:
            skip_vol += 1
            continue

        for t in m.get("tokens",[]):
            p = safe_float(t.get("price",0))
            if p <= 0 or p > 1: continue
            if p < 0.82:
                skip_price += 1
                continue
            opps.append({
                "type":     "CLOSING",
                "question": m.get("question","")[:70],
                "side":     t.get("outcome","YES").upper(),
                "token_id": t.get("token_id"),
                "price":    p,
                "expected": 0.99,
                "edge":     round(p - 0.80, 3),
                "conf":     round(p * 100, 1),
                "hours":    round(hours, 1),
                "volume":   vol,
                "risk":     "LOW",
            })

    if DEBUG and not opps:
        print(f"     [CLOSING debug] skip_time:{skip_time} "
              f"skip_vol:{skip_vol} skip_price:{skip_price}")

    opps.sort(key=lambda x: x["price"], reverse=True)
    return opps[:3]


# ─────────────────────────────────────────────
# STRATEGY 3: BTC LATENCY ARB
# ─────────────────────────────────────────────
def strategy_btc_arb(markets, btc, prev):
    if not btc or not prev: return []
    move = btc - prev
    if abs(move) < 50:
        if DEBUG:
            print(f"     [BTC-ARB debug] move=${move:.0f} "
                  f"(need $50+) — skipping")
        return []

    opps      = []
    no_beat   = 0
    low_gap   = 0
    low_edge  = 0

    for m in markets:
        q   = m.get("question","").lower()
        vol = safe_float(m.get("volumeNum",0))
        if not any(x in q for x in
                   ["5-minute","5 minute","bitcoin","btc","above","below"]):
            continue
        if vol < MIN_VOLUME: continue

        beat = None
        for pat in [r'\$([0-9,]+\.?[0-9]*)',r'([0-9]{4,6})']:
            mm = re.search(pat, m.get("question",""))
            if mm:
                try:
                    v = float(mm.group(1).replace(",",""))
                    if 10000 <= v <= 200000:
                        beat = v; break
                except: pass
        if not beat:
            no_beat += 1
            continue

        gap = btc - beat
        if abs(gap) < 50:
            low_gap += 1
            continue

        ag = abs(gap)
        if   ag > 300: exp = 0.97
        elif ag > 200: exp = 0.94
        elif ag > 150: exp = 0.91
        elif ag > 100: exp = 0.87
        elif ag >  70: exp = 0.82
        else:          exp = 0.72

        yes_p = no_p = None
        yes_t = no_t = None
        for t in m.get("tokens",[]):
            out = t.get("outcome","").upper()
            p   = safe_float(t.get("price",0))
            if p<=0 or p>=1: continue
            if out in ["YES","UP","ABOVE"]:
                yes_p=p; yes_t=t.get("token_id")
            else:
                no_p=p;  no_t=t.get("token_id")

        if yes_p is None: continue
        no_p = no_p or (1-yes_p)

        if gap > 0:
            edge=exp-yes_p; side="YES/UP"; tid=yes_t; actual=yes_p
        else:
            edge=exp-no_p; side="NO/DOWN"; tid=no_t; actual=no_p

        if edge < MIN_EDGE:
            low_edge += 1
            continue

        opps.append({
            "type":     "BTC-ARB",
            "question": m.get("question","")[:70],
            "side":     side,
            "token_id": tid,
            "price":    actual,
            "expected": exp,
            "edge":     round(edge,3),
            "conf":     round(min(exp+0.02,0.97)*100,1),
            "gap":      round(gap,0),
            "btc":      round(btc,0),
            "beat":     beat,
            "move":     round(move,0),
            "volume":   vol,
            "risk":     "LOW" if ag>150 else "MED",
        })

    if DEBUG and not opps:
        print(f"     [BTC-ARB debug] move=${move:+.0f} | "
              f"no_beat:{no_beat} low_gap:{low_gap} low_edge:{low_edge}")

    opps.sort(key=lambda x: x["edge"], reverse=True)
    return opps[:3]


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def run_bot():
    cycle      = 0
    last_table = 0

    print("=" * 58)
    print("  POLYMARKET BOT  -  DRY RUN WITH PnL")
    print("=" * 58)
    print(f"  Bankroll : ${BANKROLL:.2f}")
    print(f"  Scan     : every {SCAN_EVERY}s")
    print(f"  Min Edge : {MIN_EDGE*100:.0f}%")
    print(f"  Min Price: {MIN_PRICE*100:.0f}c+")
    print(f"  Debug    : ON (shows why trades skipped)")
    print("=" * 58)

    threading.Thread(target=btc_thread, daemon=True).start()
    print(f"  [{ts()}] BTC tracker running...")
    time.sleep(3)

    while True:
        cycle += 1
        now_str = datetime.now().strftime("%H:%M:%S")

        with btc_lock:
            cur  = btc_now
            prev = btc_prev

        btc_str  = f"${cur:,.0f}"  if cur  else "..."
        move     = round(cur-prev,0) if (cur and prev) else 0
        arrow    = "↑" if move>0 else "↓" if move<0 else "→"

        markets = load_markets()

        print(f"\n[{now_str}] Cycle#{cycle} | "
              f"BTC:{btc_str} {arrow}${abs(move):.0f} | "
              f"{len(markets)} markets")

        # Run all 3 strategies
        s1 = strategy_certain(markets)
        s2 = strategy_closing(markets)
        s3 = strategy_btc_arb(markets, cur, prev)

        print(f"  Strategies → Certain:{len(s1)} "
              f"Closing:{len(s2)} BTC-Arb:{len(s3)}")

        all_opps = s1 + s2 + s3

        if not all_opps:
            s = tracker
            print(f"  No trades | "
                  f"Total:{s.total} W:{s.wins} L:{s.losses} "
                  f"WR:{s.wr:.0f}% "
                  f"PnL:{'+' if s.profit>=0 else ''}${s.profit:.2f} "
                  f"Bal:${s.balance:.2f}")
        else:
            print(f"\n  *** {len(all_opps)} TRADE(S) FOUND! ***")
            print(f"  {'─'*54}")

            for i, o in enumerate(all_opps[:3]):
                bet    = kelly(tracker.balance, o["conf"], o["price"])
                back   = round(bet / o["price"], 2)
                profit = round(back - bet, 2)
                ret    = round(profit/bet*100,0) if bet>0 else 0

                print(f"\n  #{i+1} [{o['type']}] {o['risk']} | "
                      f"Edge:+{o['edge']*100:.1f}% | "
                      f"Conf:{o['conf']:.0f}%")
                print(f"     {o['question']}")
                print(f"     BET {o['side']} @ {o['price']*100:.1f}c "
                      f"→ expected {o['expected']*100:.0f}c")

                if "gap" in o:
                    print(f"     BTC:${o['btc']:,.0f} vs "
                          f"beat:${o['beat']:,.0f} "
                          f"gap:${o['gap']:+,.0f} "
                          f"(moved ${o['move']:+,.0f})")
                if "hours" in o:
                    print(f"     Closes in {o['hours']}h")

                print(f"     Kelly: ${bet:.2f} → "
                      f"${back:.2f} (+${profit:.2f}/+{ret:.0f}%)")

                if DRY_RUN:
                    won, net = tracker.record(
                        bet, o["price"], o["conf"],
                        o["question"], o["side"]
                    )
                    result = "WIN ✅" if won else "LOSS ❌"
                    pstr   = f"+${net:.2f}" if won else f"-${bet:.2f}"
                    print(f"     ► DRY RUN → {result} | "
                          f"PnL:{pstr} | "
                          f"Balance:${tracker.balance:.2f}")

        # Show full PnL table every 60 seconds
        if (time.time() - last_table) >= 60 and tracker.total > 0:
            tracker.show()
            last_table = time.time()

        time.sleep(SCAN_EVERY)


if __name__ == "__main__":
    run_bot()
