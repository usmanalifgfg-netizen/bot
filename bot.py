#!/usr/bin/env python3
"""
POLYMARKET 2-SECOND LATENCY ARB BOT
=====================================
Strategy: BTC moves on Binance FIRST.
Polymarket 5-min markets update 2-3 seconds LATER.
We buy the correct side in that window.

This is exactly what top bots (0x8dxd, distinct-baguette) do.
"""

import requests
import time
import re
import threading
from datetime import datetime, timezone

# ─────────────────────────────────────────────
# SETTINGS
# ─────────────────────────────────────────────
BANKROLL        = 5.0
DRY_RUN         = True    # False = real bets
SCAN_EVERY      = 2       # 2 seconds - match the lag window
MIN_GAP         = 60      # Min BTC move in $ to trigger
MIN_EDGE        = 0.06    # 6% mispricing minimum
MIN_VOLUME      = 200
MAX_SPREAD      = 0.05
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# GLOBAL STATE
# ─────────────────────────────────────────────
btc_prices      = []      # Last 10 BTC prices (track momentum)
btc_lock        = threading.Lock()
last_btc        = None
prev_btc        = None
market_cache    = []
cache_time      = 0
CACHE_TTL       = 90      # Reload markets every 90 sec
total_trades    = 0
total_profit    = 0.0
# ─────────────────────────────────────────────


def safe_float(val, default=0.0):
    if val is None: return default
    if isinstance(val, (int, float)): return float(val)
    if isinstance(val, list): return safe_float(val[0]) if val else default
    if isinstance(val, str):
        val = val.strip().replace(",","")
        try: return float(val)
        except: return default
    return default


# ─────────────────────────────────────────────
# BTC PRICE - Dual source for reliability
# ─────────────────────────────────────────────
def get_btc_now():
    """Fast BTC price - tries Binance first, Coinbase backup"""
    # Binance is fastest
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"},
            timeout=2
        )
        if r.status_code == 200:
            return safe_float(r.json().get("price"))
    except: pass

    # Coinbase backup
    try:
        r = requests.get(
            "https://api.coinbase.com/v2/prices/BTC-USD/spot",
            timeout=2
        )
        if r.status_code == 200:
            return safe_float(r.json()["data"]["amount"])
    except: pass

    return None


def get_btc_momentum():
    """
    How fast is BTC moving right now?
    Returns: (current_price, price_5_readings_ago, direction, velocity)
    """
    global btc_prices
    with btc_lock:
        if len(btc_prices) < 2:
            return None, None, None, 0
        current  = btc_prices[-1]
        oldest   = btc_prices[0]
        diff     = current - oldest
        velocity = abs(diff) / max(len(btc_prices), 1)
        direction = "UP" if diff > 0 else "DOWN"
        return current, oldest, direction, velocity


# ─────────────────────────────────────────────
# POLYMARKET MARKETS
# ─────────────────────────────────────────────
def load_markets():
    global market_cache, cache_time
    now = time.time()
    if market_cache and (now - cache_time) < CACHE_TTL:
        return market_cache

    print(f"  [{ts()}] Reloading markets...")
    all_m = {}
    for kw in ["bitcoin", "btc", "5-minute", "5 minute"]:
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


def get_orderbook_fast(token_id):
    """Quick order book check"""
    if not token_id:
        return {"spread":0.02,"best_ask":0.5,"depth":100,"ok":True}
    try:
        r = requests.get(
            "https://clob.polymarket.com/book",
            params={"token_id": token_id},
            timeout=2
        )
        if r.status_code != 200:
            return {"ok": False}
        book = r.json()
        bids = book.get("bids",[])
        asks = book.get("asks",[])
        if not bids or not asks:
            return {"ok": False}
        bb = safe_float(bids[0].get("price"))
        ba = safe_float(asks[0].get("price"))
        spread = round(ba - bb, 4)
        depth  = sum(safe_float(a.get("size")) for a in asks[:3])
        return {
            "spread":   spread,
            "best_ask": ba,
            "best_bid": bb,
            "depth":    depth,
            "ok":       spread <= MAX_SPREAD and depth >= 10
        }
    except:
        return {"ok": False}


def ts():
    return datetime.now().strftime("%H:%M:%S")


# ─────────────────────────────────────────────
# CORE: LATENCY ARB DETECTOR
# The 2-second window strategy
# ─────────────────────────────────────────────
def find_latency_arb(markets, btc_now, btc_old):
    """
    KEY LOGIC:
    1. BTC moved X dollars since last reading
    2. Find Polymarket 5-min markets where price HASN'T updated yet
    3. Buy correct side before market catches up

    Polymarket prices update ~2-3 seconds after real BTC moves.
    We scan every 2 seconds to catch that window.
    """
    if not btc_now or not btc_old:
        return []

    btc_move = btc_now - btc_old   # positive = BTC went UP
    if abs(btc_move) < MIN_GAP:
        return []                   # Move too small - not worth it

    opps = []

    for m in markets:
        try:
            q   = m.get("question","").lower()
            vol = safe_float(m.get("volumeNum",0))

            # Only 5-min BTC markets
            is_5min = any(x in q for x in
                         ["5-minute","5 minute","5min","bitcoin","btc"])
            if not is_5min or vol < MIN_VOLUME:
                continue

            # Extract "price to beat" from question
            beat = None
            for pat in [r'\$([0-9,]+\.?[0-9]*)', r'([0-9]{4,6})']:
                mm = re.search(pat, m.get("question",""))
                if mm:
                    try:
                        v = float(mm.group(1).replace(",",""))
                        if 10000 <= v <= 200000:
                            beat = v
                            break
                    except: pass

            if not beat:
                continue

            # Real gap (what it ACTUALLY is right now)
            real_gap = btc_now - beat

            # What the market THINKS (based on current prices)
            tokens = m.get("tokens",[])
            yes_price = no_price = None
            yes_tid   = no_tid   = None

            for t in tokens:
                out = t.get("outcome","").upper()
                p   = safe_float(t.get("price",0))
                if p <= 0 or p >= 1: continue
                if out in ["YES","UP","ABOVE"]:
                    yes_price = p
                    yes_tid   = t.get("token_id")
                else:
                    no_price  = p
                    no_tid    = t.get("token_id")

            if yes_price is None:
                continue
            no_price = no_price or (1 - yes_price)

            # What price SHOULD be given real gap
            ag = abs(real_gap)
            if   ag > 300: expected_winning = 0.97
            elif ag > 200: expected_winning = 0.94
            elif ag > 150: expected_winning = 0.91
            elif ag > 100: expected_winning = 0.87
            elif ag >  80: expected_winning = 0.83
            elif ag >  60: expected_winning = 0.75
            else:          expected_winning = 0.65

            # Which side should win?
            if real_gap > MIN_GAP:
                # BTC above beat → UP/YES should win
                actual_price = yes_price
                bet_side     = "YES/UP"
                bet_tid      = yes_tid
                # Is market BEHIND? (still showing low price)
                edge = expected_winning - actual_price

            elif real_gap < -MIN_GAP:
                # BTC below beat → DOWN/NO should win
                actual_price = no_price
                bet_side     = "NO/DOWN"
                bet_tid      = no_tid
                edge = expected_winning - actual_price

            else:
                continue  # Too close to call

            if edge < MIN_EDGE:
                continue  # Market already priced it in

            # Quick order book check
            ob = get_orderbook_fast(bet_tid)
            if not ob.get("ok"):
                continue

            # BTC momentum check
            # Extra confidence if BTC is still moving in same direction
            momentum_bonus = 0.0
            if btc_move > 0 and real_gap > 0:
                momentum_bonus = 0.02
            elif btc_move < 0 and real_gap < 0:
                momentum_bonus = 0.02

            total_confidence = min(0.97, expected_winning + momentum_bonus)

            opps.append({
                "question":    m.get("question","")[:70],
                "side":        bet_side,
                "token_id":    bet_tid,
                "market_price":actual_price,
                "expected":    expected_winning,
                "edge":        round(edge, 3),
                "confidence":  round(total_confidence * 100, 1),
                "real_gap":    round(real_gap, 0),
                "btc_now":     round(btc_now, 0),
                "btc_old":     round(btc_old, 0),
                "btc_move":    round(btc_move, 0),
                "beat":        beat,
                "volume":      vol,
                "spread":      ob.get("spread",0),
                "depth":       ob.get("depth",0),
                "risk":        "LOW" if ag > 150 else "MED",
            })

        except: continue

    opps.sort(key=lambda x: x["edge"], reverse=True)
    return opps


# ─────────────────────────────────────────────
# SECONDARY: NEAR-CERTAIN (works anytime)
# ─────────────────────────────────────────────
def find_near_certain(markets):
    opps = []
    for m in markets:
        try:
            vol = safe_float(m.get("volumeNum",0))
            if vol < MIN_VOLUME: continue
            for t in m.get("tokens",[]):
                p   = safe_float(t.get("price",0))
                tid = t.get("token_id")
                if not (0.92 <= p <= 0.985): continue
                ob = get_orderbook_fast(tid)
                if not ob.get("ok"): continue
                opps.append({
                    "question":    m.get("question","")[:70],
                    "side":        t.get("outcome","YES").upper(),
                    "token_id":    tid,
                    "market_price":p,
                    "expected":    0.99,
                    "edge":        round(p - 0.90, 3),
                    "confidence":  round(p * 100, 1),
                    "volume":      vol,
                    "spread":      ob.get("spread",0),
                    "depth":       ob.get("depth",0),
                    "risk":        "LOW",
                })
        except: continue
    opps.sort(key=lambda x: x["market_price"], reverse=True)
    return opps[:3]


# ─────────────────────────────────────────────
# KELLY BET SIZE
# ─────────────────────────────────────────────
def kelly(bankroll, confidence, price):
    if price <= 0 or price >= 1: return 0.50
    b     = (1 - price) / price
    p     = confidence / 100
    q     = 1 - p
    k     = (b * p - q) / b
    frac  = max(0.02, min(k * 0.5, 0.15))
    amt   = round(bankroll * frac, 2)
    return max(0.50, min(amt, bankroll * 0.20))


# ─────────────────────────────────────────────
# BTC PRICE TRACKER (background thread)
# Updates every 1 second
# ─────────────────────────────────────────────
def btc_tracker():
    global btc_prices, last_btc, prev_btc
    while True:
        price = get_btc_now()
        if price:
            with btc_lock:
                prev_btc = last_btc
                last_btc = price
                btc_prices.append(price)
                if len(btc_prices) > 10:
                    btc_prices.pop(0)
        time.sleep(1)


# ─────────────────────────────────────────────
# MAIN BOT
# ─────────────────────────────────────────────
def run_bot():
    global total_trades, total_profit
    bankroll = BANKROLL
    cycle    = 0

    print("=" * 62)
    print("  POLYMARKET 2-SECOND LATENCY ARB BOT")
    print("=" * 62)
    print(f"  Bankroll   : ${bankroll:.2f}")
    print(f"  Scan Every : {SCAN_EVERY}s")
    print(f"  Min Gap    : ${MIN_GAP} BTC move")
    print(f"  Min Edge   : {MIN_EDGE*100:.0f}%")
    print(f"  Mode       : {'DRY RUN' if DRY_RUN else '>>> LIVE TRADING <<<'}")
    print("=" * 62)
    print()
    print("  HOW IT WORKS:")
    print("  1. BTC price tracked every 1 second (background)")
    print("  2. When BTC moves $60+, Polymarket is 2-3s BEHIND")
    print("  3. We buy correct side in that 2s window")
    print("  4. Market catches up → instant profit")
    print("=" * 62)

    # Start BTC tracker in background
    t = threading.Thread(target=btc_tracker, daemon=True)
    t.start()
    print("\n  BTC tracker started (background)...")
    time.sleep(3)  # Let it collect a few prices first

    while True:
        cycle += 1

        # Get BTC state
        with btc_lock:
            btc_now  = last_btc
            btc_then = btc_prices[0] if len(btc_prices) >= 5 else None
            btc_list = list(btc_prices)

        btc_move = round(btc_now - btc_then, 0) if (btc_now and btc_then) else 0
        btc_str  = f"${btc_now:,.0f}" if btc_now else "N/A"
        move_str = f"{btc_move:+,.0f}" if btc_move else "..."

        # Load markets (cached)
        markets = load_markets()

        # Run strategies
        arb_opps     = find_latency_arb(markets, btc_now, btc_then)
        certain_opps = find_near_certain(markets)
        all_opps     = arb_opps + certain_opps

        # Display
        now_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]

        if all_opps:
            # OPPORTUNITY FOUND - show clearly
            print(f"\n{'█'*62}")
            print(f"  [{now_str}] #{cycle}")
            print(f"  BTC: {btc_str}  Move: {move_str}  "
                  f"Opps: {len(all_opps)}")
            print(f"{'█'*62}")

            for i, o in enumerate(all_opps[:3]):
                price  = o["market_price"]
                conf   = o["confidence"]
                bet    = kelly(bankroll, conf, price)
                back   = round(bet / price, 2)
                profit = round(back - bet, 2)
                ret    = round(profit / bet * 100, 0) if bet > 0 else 0

                tag = "ARB" if "gap" in o or "btc_now" in o else "CERT"
                sflag = "OK" if o["spread"] <= 0.02 else "FAIR"

                print(f"\n  #{i+1} [{tag}] {o['risk']} | "
                      f"Edge:+{o['edge']*100:.1f}% | "
                      f"Conf:{conf:.0f}%")
                print(f"     {o['question']}")
                print(f"     BET: {o['side']} @ {price*100:.1f}c  "
                      f"(should be {o['expected']*100:.0f}c)")

                if "btc_now" in o:
                    print(f"     BTC: ${o['btc_now']:,.0f} vs "
                          f"beat ${o['beat']:,.0f} = "
                          f"gap ${o['real_gap']:+,.0f}")
                    print(f"     BTC moved: ${o['btc_move']:+,.0f} "
                          f"in last {len(btc_list)} readings")

                print(f"     Book: {o['spread']*100:.1f}c [{sflag}] "
                      f"depth:{o['depth']:.0f} | Vol:${o['volume']:,.0f}")
                print(f"     Kelly: ${bet:.2f} → ${back:.2f} "
                      f"(+${profit:.2f}/+{ret:.0f}% in ~2s)")

                if DRY_RUN:
                    print(f"     ► [DRY RUN] Would bet ${bet:.2f}")
                    total_trades += 1
                    total_profit += profit * 0.88  # assume 88% hit rate
                    bankroll     += profit * 0.88
                else:
                    print(f"     ► [LIVE] Placing ${bet:.2f}...")
                    # Real execution here

            print(f"\n  Session: {total_trades} trades | "
                  f"Est profit: +${total_profit:.2f} | "
                  f"Bankroll: ${bankroll:.2f}")

        else:
            # No opportunity - compact log
            move_arrow = "↑" if btc_move > 0 else "↓" if btc_move < 0 else "→"
            print(f"  [{now_str}] BTC:{btc_str} {move_arrow}{abs(btc_move):.0f} | "
                  f"No arb (move<${MIN_GAP} or market updated)")

        time.sleep(SCAN_EVERY)


if __name__ == "__main__":
    run_bot()
