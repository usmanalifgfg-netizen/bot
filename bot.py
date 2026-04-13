#!/usr/bin/env python3
"""
POLYMARKET 2-SECOND LATENCY ARB BOT
With full PnL tracking in Dry Run mode
"""

import requests
import time
import re
import threading
from datetime import datetime, timezone

# ─────────────────────────────────────────────
# SETTINGS
# ─────────────────────────────────────────────
BANKROLL     = 5.0
DRY_RUN      = True
SCAN_EVERY   = 2
MIN_GAP      = 60
MIN_EDGE     = 0.06
MIN_VOLUME   = 200
MAX_SPREAD   = 0.05
WIN_RATE     = 0.88   # 88% assumed win rate for PnL simulation
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# PnL TRACKER
# ─────────────────────────────────────────────
class PnL:
    def __init__(self, starting):
        self.starting    = starting
        self.bankroll    = starting
        self.total_bet   = 0.0
        self.total_wins  = 0.0
        self.total_loss  = 0.0
        self.trades      = []
        self.wins        = 0
        self.losses      = 0
        self.lock        = threading.Lock()

    def record(self, bet, price, win_prob, question, side):
        """Simulate trade outcome based on win probability"""
        import random
        won       = random.random() < win_prob
        payout    = round(bet / price, 2) if won else 0.0
        pnl       = round(payout - bet, 2)
        ret_pct   = round(pnl / bet * 100, 1) if bet > 0 else 0

        with self.lock:
            self.bankroll    += pnl
            self.total_bet   += bet
            if won:
                self.wins     += 1
                self.total_wins += payout
            else:
                self.losses   += 1
                self.total_loss += bet

            self.trades.append({
                "time":     datetime.now().strftime("%H:%M:%S"),
                "question": question[:45],
                "side":     side,
                "bet":      bet,
                "price":    price,
                "won":      won,
                "pnl":      pnl,
                "ret_pct":  ret_pct,
                "bankroll": round(self.bankroll, 2),
            })

        return won, pnl, ret_pct

    def summary(self):
        with self.lock:
            t      = len(self.trades)
            wr     = self.wins / t * 100 if t > 0 else 0
            profit = self.bankroll - self.starting
            roi    = profit / self.starting * 100 if self.starting > 0 else 0
            return {
                "trades":   t,
                "wins":     self.wins,
                "losses":   self.losses,
                "win_rate": round(wr, 1),
                "bankroll": round(self.bankroll, 2),
                "profit":   round(profit, 2),
                "roi":      round(roi, 1),
            }

    def last_trades(self, n=5):
        with self.lock:
            return list(self.trades[-n:])


# ─────────────────────────────────────────────
# GLOBALS
# ─────────────────────────────────────────────
btc_prices   = []
btc_lock     = threading.Lock()
last_btc     = None
market_cache = []
cache_time   = 0
CACHE_TTL    = 90
pnl          = PnL(BANKROLL)
# ─────────────────────────────────────────────


def safe_float(val, default=0.0):
    if val is None: return default
    if isinstance(val, (int, float)): return float(val)
    if isinstance(val, list): return safe_float(val[0]) if val else default
    if isinstance(val, str):
        val = val.strip().replace(",", "")
        try: return float(val)
        except: return default
    return default


def ts():
    return datetime.now().strftime("%H:%M:%S")


def get_btc():
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"}, timeout=2
        )
        if r.status_code == 200:
            return safe_float(r.json().get("price"))
    except: pass
    try:
        r = requests.get(
            "https://api.coinbase.com/v2/prices/BTC-USD/spot",
            timeout=2
        )
        if r.status_code == 200:
            return safe_float(r.json()["data"]["amount"])
    except: pass
    return None


def btc_tracker():
    global last_btc, btc_prices
    while True:
        p = get_btc()
        if p:
            with btc_lock:
                last_btc = p
                btc_prices.append(p)
                if len(btc_prices) > 10:
                    btc_prices.pop(0)
        time.sleep(1)


def load_markets():
    global market_cache, cache_time
    now = time.time()
    if market_cache and (now - cache_time) < CACHE_TTL:
        return market_cache
    print(f"  [{ts()}] Loading markets...")
    all_m = {}
    for kw in ["bitcoin", "btc", "5-minute", "5 minute",
               "temperature", "weather", "will", "crypto"]:
        try:
            r = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"active":"true","closed":"false",
                        "limit":100,"search":kw},
                timeout=8
            )
            if r.status_code == 200:
                for m in r.json():
                    mid = m.get("id","")
                    if mid: all_m[mid] = m
        except: pass
        time.sleep(0.15)
    market_cache = list(all_m.values())
    cache_time   = now
    print(f"  [{ts()}] {len(market_cache)} markets loaded")
    return market_cache


def check_book(token_id):
    if not token_id:
        return {"spread":0.02,"depth":100,"ok":True}
    try:
        r = requests.get(
            "https://clob.polymarket.com/book",
            params={"token_id": token_id}, timeout=2
        )
        if r.status_code != 200: return {"ok":False}
        book = r.json()
        bids = book.get("bids",[])
        asks = book.get("asks",[])
        if not bids or not asks: return {"ok":False}
        bb     = safe_float(bids[0].get("price"))
        ba     = safe_float(asks[0].get("price"))
        spread = round(ba - bb, 4)
        depth  = sum(safe_float(a.get("size")) for a in asks[:3])
        return {"spread":spread,"depth":depth,
                "ok": spread <= MAX_SPREAD and depth >= 10}
    except:
        return {"ok":False}


def kelly(bankroll, conf_pct, price):
    if price <= 0 or price >= 1: return 0.50
    b    = (1 - price) / price
    p    = conf_pct / 100
    q    = 1 - p
    k    = (b * p - q) / b
    frac = max(0.02, min(k * 0.5, 0.15))
    amt  = round(bankroll * frac, 2)
    return max(0.50, min(amt, bankroll * 0.20))


# ─────────────────────────────────────────────
# STRATEGY 1: LATENCY ARB (core)
# ─────────────────────────────────────────────
def find_arb(markets, btc_now, btc_old):
    if not btc_now or not btc_old: return []
    move = btc_now - btc_old
    if abs(move) < MIN_GAP: return []

    opps = []
    for m in markets:
        try:
            q   = m.get("question","").lower()
            vol = safe_float(m.get("volumeNum",0))
            if not any(x in q for x in
                       ["5-minute","5 minute","5min","bitcoin","btc"]):
                continue
            if vol < MIN_VOLUME: continue

            beat = None
            for pat in [r'\$([0-9,]+\.?[0-9]*)', r'([0-9]{4,6})']:
                mm = re.search(pat, m.get("question",""))
                if mm:
                    try:
                        v = float(mm.group(1).replace(",",""))
                        if 10000 <= v <= 200000:
                            beat = v; break
                    except: pass
            if not beat: continue

            real_gap = btc_now - beat
            if abs(real_gap) < MIN_GAP: continue

            ag = abs(real_gap)
            if   ag > 300: exp = 0.97
            elif ag > 200: exp = 0.94
            elif ag > 150: exp = 0.91
            elif ag > 100: exp = 0.87
            elif ag >  80: exp = 0.83
            elif ag >  60: exp = 0.75
            else:          exp = 0.65

            yes_p = no_p = None
            yes_t = no_t = None
            for t in m.get("tokens",[]):
                out = t.get("outcome","").upper()
                p   = safe_float(t.get("price",0))
                if p <= 0 or p >= 1: continue
                if out in ["YES","UP","ABOVE"]:
                    yes_p = p; yes_t = t.get("token_id")
                else:
                    no_p  = p; no_t  = t.get("token_id")

            if yes_p is None: continue
            no_p = no_p or (1 - yes_p)

            if real_gap > 0:
                edge = exp - yes_p
                side = "YES/UP"; tid = yes_t; actual = yes_p
            else:
                edge = exp - no_p
                side = "NO/DOWN"; tid = no_t; actual = no_p

            if edge < MIN_EDGE: continue

            ob = check_book(tid)
            if not ob.get("ok"): continue

            opps.append({
                "type":     "ARB",
                "question": m.get("question","")[:70],
                "side":     side,
                "token_id": tid,
                "price":    actual,
                "expected": exp,
                "edge":     round(edge, 3),
                "conf":     round(min(exp + 0.02, 0.97) * 100, 1),
                "gap":      round(real_gap, 0),
                "btc":      round(btc_now, 0),
                "beat":     beat,
                "move":     round(move, 0),
                "volume":   vol,
                "spread":   ob["spread"],
                "depth":    ob["depth"],
                "risk":     "LOW" if ag > 150 else "MED",
            })
        except: continue

    opps.sort(key=lambda x: x["edge"], reverse=True)
    return opps[:3]


# ─────────────────────────────────────────────
# STRATEGY 2: NEAR CERTAIN
# ─────────────────────────────────────────────
def find_certain(markets):
    opps = []
    for m in markets:
        try:
            vol = safe_float(m.get("volumeNum",0))
            if vol < MIN_VOLUME: continue
            for t in m.get("tokens",[]):
                p   = safe_float(t.get("price",0))
                tid = t.get("token_id")
                if not (0.92 <= p <= 0.985): continue
                ob = check_book(tid)
                if not ob.get("ok"): continue
                opps.append({
                    "type":     "CERTAIN",
                    "question": m.get("question","")[:70],
                    "side":     t.get("outcome","YES").upper(),
                    "token_id": tid,
                    "price":    p,
                    "expected": 0.99,
                    "edge":     round(p - 0.90, 3),
                    "conf":     round(p * 100, 1),
                    "volume":   vol,
                    "spread":   ob["spread"],
                    "depth":    ob["depth"],
                    "risk":     "LOW",
                })
        except: continue
    opps.sort(key=lambda x: x["price"], reverse=True)
    return opps[:2]


# ─────────────────────────────────────────────
# PRINT PnL TABLE
# ─────────────────────────────────────────────
def print_pnl():
    s  = pnl.summary()
    lt = pnl.last_trades(5)

    profit_sign = "+" if s["profit"] >= 0 else ""
    roi_sign    = "+" if s["roi"] >= 0 else ""

    print(f"\n  {'═'*58}")
    print(f"  {'📊 DRY RUN PnL TRACKER':^58}")
    print(f"  {'═'*58}")
    print(f"  Starting Bankroll : ${BANKROLL:.2f}")
    print(f"  Current Bankroll  : ${s['bankroll']:.2f}")
    print(f"  Total Profit/Loss : {profit_sign}${s['profit']:.2f}")
    print(f"  ROI               : {roi_sign}{s['roi']:.1f}%")
    print(f"  ─────────────────────────────────────────────────")
    print(f"  Total Trades      : {s['trades']}")
    print(f"  Wins              : {s['wins']}")
    print(f"  Losses            : {s['losses']}")
    print(f"  Win Rate          : {s['win_rate']:.1f}%")
    print(f"  ─────────────────────────────────────────────────")

    if lt:
        print(f"  LAST {len(lt)} TRADES:")
        print(f"  {'Time':<10} {'Side':<8} {'Bet':>5} "
              f"{'PnL':>7} {'Result':<6} {'Balance':>8}")
        print(f"  {'-'*52}")
        for tr in lt:
            result = "WIN ✅" if tr["won"] else "LOSS ❌"
            pnl_str = f"+${tr['pnl']:.2f}" if tr["won"] else f"-${tr['bet']:.2f}"
            print(f"  {tr['time']:<10} {tr['side']:<8} "
                  f"${tr['bet']:>4.2f} {pnl_str:>7} "
                  f"{result:<6} ${tr['bankroll']:>7.2f}")

    print(f"  {'═'*58}\n")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def run_bot():
    cycle = 0

    print("=" * 62)
    print("  POLYMARKET 2-SEC LATENCY ARB BOT + PnL TRACKER")
    print("=" * 62)
    print(f"  Bankroll   : ${BANKROLL:.2f}")
    print(f"  Scan Every : {SCAN_EVERY}s")
    print(f"  Min Gap    : ${MIN_GAP} BTC move")
    print(f"  Min Edge   : {MIN_EDGE*100:.0f}%")
    print(f"  Mode       : {'DRY RUN (PnL Simulated)' if DRY_RUN else 'LIVE'}")
    print("=" * 62)

    # Start BTC tracker
    threading.Thread(target=btc_tracker, daemon=True).start()
    print(f"  [{ts()}] BTC tracker started...")
    time.sleep(3)

    last_pnl_print = 0

    while True:
        cycle += 1

        with btc_lock:
            btc_now  = last_btc
            btc_old  = btc_prices[0] if len(btc_prices) >= 5 else None
            btc_list = list(btc_prices)

        btc_move = round(btc_now - btc_old, 0) if (btc_now and btc_old) else 0
        btc_str  = f"${btc_now:,.0f}" if btc_now else "N/A"
        arrow    = "↑" if btc_move > 0 else "↓" if btc_move < 0 else "→"

        markets = load_markets()

        arb_opps  = find_arb(markets, btc_now, btc_old)
        cert_opps = find_certain(markets)
        all_opps  = arb_opps + cert_opps

        now_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]

        if all_opps:
            print(f"\n{'█'*62}")
            print(f"  [{now_str}] BTC:{btc_str} {arrow}${abs(btc_move):.0f} "
                  f"| {len(all_opps)} TRADE(S) FOUND")
            print(f"{'█'*62}")

            for i, o in enumerate(all_opps[:3]):
                bet    = kelly(pnl.bankroll, o["conf"], o["price"])
                back   = round(bet / o["price"], 2)
                profit = round(back - bet, 2)
                ret    = round(profit / bet * 100, 0) if bet > 0 else 0
                sflag  = "OK" if o["spread"] <= 0.02 else "FAIR"

                print(f"\n  #{i+1} [{o['type']}] {o['risk']} "
                      f"| Edge:+{o['edge']*100:.1f}% "
                      f"| Conf:{o['conf']:.0f}%")
                print(f"     {o['question']}")
                print(f"     BET: {o['side']} @ {o['price']*100:.1f}c "
                      f"(expected {o['expected']*100:.0f}c)")

                if "gap" in o:
                    print(f"     BTC: ${o['btc']:,.0f} vs "
                          f"beat ${o['beat']:,.0f} "
                          f"= gap ${o['gap']:+,.0f} "
                          f"(moved ${o['move']:+,.0f})")

                print(f"     Book: {o['spread']*100:.1f}c [{sflag}] "
                      f"depth:{o['depth']:.0f}")
                print(f"     Kelly: ${bet:.2f} → ${back:.2f} "
                      f"(+${profit:.2f} / +{ret:.0f}%)")

                if DRY_RUN:
                    won, trade_pnl, ret_pct = pnl.record(
                        bet, o["price"],
                        o["conf"] / 100,
                        o["question"], o["side"]
                    )
                    result = "WIN ✅" if won else "LOSS ❌"
                    pnl_str = f"+${trade_pnl:.2f}" if won else f"-${bet:.2f}"
                    print(f"     ► [DRY RUN] {result} | "
                          f"PnL: {pnl_str} | "
                          f"Balance: ${pnl.bankroll:.2f}")
                else:
                    print(f"     ► [LIVE] Placing ${bet:.2f}...")

            # Print full PnL table every 10 trades or every 60s
            s = pnl.summary()
            if s["trades"] % 10 == 0 or (time.time() - last_pnl_print) > 60:
                print_pnl()
                last_pnl_print = time.time()

        else:
            # Compact line when no opportunity
            s = pnl.summary()
            print(f"  [{now_str}] BTC:{btc_str} {arrow}${abs(btc_move):.0f} "
                  f"| No arb | "
                  f"Trades:{s['trades']} "
                  f"WR:{s['win_rate']:.0f}% "
                  f"PnL:{'+' if s['profit']>=0 else ''}${s['profit']:.2f} "
                  f"Bal:${s['bankroll']:.2f}")

            # Print full table every 60s
            if (time.time() - last_pnl_print) > 60:
                print_pnl()
                last_pnl_print = time.time()

        time.sleep(SCAN_EVERY)


if __name__ == "__main__":
    run_bot()
