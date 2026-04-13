#!/usr/bin/env python3
"""
POLYMARKET SPEED BOT - Scan every 5 seconds
Fixed: safe price parsing (no more float conversion errors)
"""

import requests
import time
import re
import logging
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# SETTINGS
# ─────────────────────────────────────────────
BANKROLL     = 5.0
CHECK_EVERY  = 5
DRY_RUN      = True
MIN_EDGE     = 0.07
MAX_SPREAD   = 0.04
MIN_VOLUME   = 300
BTC_GAP_MIN  = 80
# ─────────────────────────────────────────────

_market_cache      = []
_market_cache_time = 0
MARKET_CACHE_TTL   = 120  # reload every 2 min


def safe_float(val, default=0.0):
    """
    Safely convert ANY value to float.
    Handles: string, int, float, list, None, dict
    This fixes the '[' conversion error!
    """
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, list):
        # Take first element if list
        return safe_float(val[0]) if val else default
    if isinstance(val, str):
        val = val.strip().replace(",", "")
        if not val or val in ["[", "]", ""]:
            return default
        try:
            return float(val)
        except:
            return default
    return default


def kelly_size(bankroll, win_prob, price):
    if price <= 0 or price >= 1 or win_prob <= 0:
        return 0.50
    b      = (1.0 - price) / price
    p      = win_prob
    q      = 1.0 - p
    kelly  = (b * p - q) / b
    half_k = kelly * 0.5
    frac   = max(0.02, min(half_k, 0.15))
    amt    = round(bankroll * frac, 2)
    return max(0.50, min(amt, bankroll * 0.15))


def get_btc_price():
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"}, timeout=3
        )
        if r.status_code == 200:
            return safe_float(r.json().get("price"))
    except:
        pass
    try:
        r = requests.get(
            "https://api.coinbase.com/v2/prices/BTC-USD/spot",
            timeout=3
        )
        if r.status_code == 200:
            return safe_float(r.json()["data"]["amount"])
    except:
        pass
    return None


def load_markets_cached():
    global _market_cache, _market_cache_time
    now = time.time()
    if _market_cache and (now - _market_cache_time) < MARKET_CACHE_TTL:
        return _market_cache

    log.info("Reloading markets from Polymarket...")
    all_m = {}
    for kw in ["bitcoin", "btc", "5-minute", "temperature",
               "weather", "will", "crypto", "price"]:
        try:
            r = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"active": "true", "closed": "false",
                        "limit": 100, "search": kw},
                timeout=10
            )
            if r.status_code == 200:
                for m in r.json():
                    mid = m.get("id", "")
                    if mid:
                        all_m[mid] = m
        except:
            pass

    _market_cache      = list(all_m.values())
    _market_cache_time = now
    log.info(f"Loaded {len(_market_cache)} markets")
    return _market_cache


def check_orderbook(token_id):
    if not token_id:
        return {"spread": 0.02, "depth": 100, "tradeable": True}
    try:
        r = requests.get(
            "https://clob.polymarket.com/book",
            params={"token_id": token_id}, timeout=4
        )
        if r.status_code != 200:
            return {"spread": 999, "depth": 0, "tradeable": False}
        book     = r.json()
        bids     = book.get("bids", [])
        asks     = book.get("asks", [])
        if not bids or not asks:
            return {"spread": 999, "depth": 0, "tradeable": False}
        best_bid = safe_float(bids[0].get("price"))
        best_ask = safe_float(asks[0].get("price"))
        spread   = round(best_ask - best_bid, 4)
        depth    = sum(safe_float(a.get("size")) for a in asks[:3])
        return {
            "spread":    spread,
            "depth":     round(depth, 0),
            "tradeable": spread <= MAX_SPREAD and depth >= 20
        }
    except:
        return {"spread": 999, "depth": 0, "tradeable": False}


def get_token_price(token):
    """Safely get price from token — handles all formats"""
    raw = token.get("price", 0)
    p   = safe_float(raw)
    # Sanity: price must be 0-1
    if p < 0 or p > 1:
        return None
    return p


# ─────────────────────────────────────────────
# STRATEGY 1: BTC 5-MIN GAP
# ─────────────────────────────────────────────
def strategy_btc_gap(markets, btc_price):
    if not btc_price:
        return []
    opps = []
    for m in markets:
        try:
            q   = m.get("question", "").lower()
            vol = safe_float(m.get("volumeNum", 0))
            if not any(w in q for w in
                       ["5-minute","5 minute","5min","bitcoin","btc"]):
                continue
            if vol < MIN_VOLUME:
                continue

            price_to_beat = None
            for pat in [r'\$([0-9,]+)', r'([0-9]{4,6})']:
                mm = re.search(pat, m.get("question",""))
                if mm:
                    try:
                        val = float(mm.group(1).replace(",",""))
                        if 10000 <= val <= 200000:
                            price_to_beat = val
                            break
                    except:
                        pass
            if not price_to_beat:
                continue

            gap = btc_price - price_to_beat
            if abs(gap) < BTC_GAP_MIN:
                continue

            ag = abs(gap)
            if   ag > 200: exp = 0.96
            elif ag > 150: exp = 0.93
            elif ag > 100: exp = 0.89
            elif ag >  80: exp = 0.82
            else:          exp = 0.70

            for t in m.get("tokens", []):
                out   = t.get("outcome", "").upper()
                price = get_token_price(t)
                if price is None:
                    continue
                tid   = t.get("token_id")
                is_up = out in ["YES", "UP", "ABOVE"]

                if gap > 0 and is_up:
                    edge = exp - price
                elif gap < 0 and not is_up:
                    edge = exp - price
                else:
                    continue

                if edge >= MIN_EDGE:
                    ob = check_orderbook(tid)
                    if not ob["tradeable"]:
                        continue
                    opps.append({
                        "strategy":   "BTC-GAP",
                        "question":   m.get("question","")[:70],
                        "side":       out,
                        "token_id":   tid,
                        "price":      price,
                        "expected":   exp,
                        "edge":       round(edge, 3),
                        "confidence": round(exp * 100, 1),
                        "gap":        round(gap, 0),
                        "btc":        btc_price,
                        "beat":       price_to_beat,
                        "volume":     vol,
                        "spread":     ob["spread"],
                        "depth":      ob["depth"],
                        "risk":       "LOW" if ag > 150 else "MED",
                    })
        except Exception as e:
            continue
    opps.sort(key=lambda x: x["edge"], reverse=True)
    return opps[:3]


# ─────────────────────────────────────────────
# STRATEGY 2: NEAR CERTAIN (92c+)
# ─────────────────────────────────────────────
def strategy_near_certain(markets):
    opps = []
    for m in markets:
        try:
            vol = safe_float(m.get("volumeNum", 0))
            if vol < MIN_VOLUME:
                continue
            for t in m.get("tokens", []):
                price = get_token_price(t)
                if price is None:
                    continue
                tid = t.get("token_id")
                if 0.92 <= price <= 0.985:
                    ob = check_orderbook(tid)
                    if not ob["tradeable"]:
                        continue
                    opps.append({
                        "strategy":   "CERTAIN",
                        "question":   m.get("question","")[:70],
                        "side":       t.get("outcome","YES").upper(),
                        "token_id":   tid,
                        "price":      price,
                        "expected":   0.99,
                        "edge":       round(price - 0.90, 3),
                        "confidence": round(price * 100, 1),
                        "volume":     vol,
                        "spread":     ob["spread"],
                        "depth":      ob["depth"],
                        "risk":       "LOW",
                    })
        except:
            continue
    opps.sort(key=lambda x: x["price"], reverse=True)
    return opps[:3]


# ─────────────────────────────────────────────
# STRATEGY 3: CLOSING SOON
# ─────────────────────────────────────────────
def strategy_closing(markets):
    opps = []
    now  = datetime.now(timezone.utc)
    for m in markets:
        try:
            end_str = m.get("endDateIso") or m.get("endDate","")
            if not end_str:
                continue
            end   = datetime.fromisoformat(end_str.replace("Z","+00:00"))
            hours = (end - now).total_seconds() / 3600
            if not (0.5 <= hours <= 4):
                continue
            vol = safe_float(m.get("volumeNum", 0))
            if vol < MIN_VOLUME:
                continue
            for t in m.get("tokens", []):
                price = get_token_price(t)
                if price is None:
                    continue
                tid = t.get("token_id")
                if 0.86 <= price <= 0.985:
                    ob = check_orderbook(tid)
                    if not ob["tradeable"]:
                        continue
                    opps.append({
                        "strategy":   "CLOSING",
                        "question":   m.get("question","")[:70],
                        "side":       t.get("outcome","YES").upper(),
                        "token_id":   tid,
                        "price":      price,
                        "expected":   0.99,
                        "edge":       round(price - 0.84, 3),
                        "confidence": round(price * 100, 1),
                        "hours_left": round(hours, 1),
                        "volume":     vol,
                        "spread":     ob["spread"],
                        "depth":      ob["depth"],
                        "risk":       "LOW",
                    })
        except:
            continue
    opps.sort(key=lambda x: x["price"], reverse=True)
    return opps[:3]


# ─────────────────────────────────────────────
# MAIN BOT
# ─────────────────────────────────────────────
def run_bot():
    total_trades = 0
    cycle        = 0
    bankroll     = BANKROLL

    print("=" * 60)
    print("  POLYMARKET SPEED BOT  -  EVERY 5 SECONDS")
    print("=" * 60)
    print(f"  Bankroll : ${bankroll:.2f}  |  Mode: {'DRY RUN' if DRY_RUN else 'LIVE'}")
    print(f"  Edge Min : {MIN_EDGE*100:.0f}%  |  Spread Max: {MAX_SPREAD*100:.0f}c")
    print("=" * 60)

    while True:
        cycle += 1
        ts = datetime.now().strftime("%H:%M:%S")

        btc     = get_btc_price()
        markets = load_markets_cached()
        btc_str = f"${btc:,.0f}" if btc else "N/A"

        print(f"\n[{ts}] #{cycle} | BTC:{btc_str} | {len(markets)} markets")

        s1 = strategy_btc_gap(markets, btc)
        s2 = strategy_near_certain(markets)
        s3 = strategy_closing(markets)

        all_opps = s1 + s2 + s3

        if not all_opps:
            print(f"  -- No trades (edge<{MIN_EDGE*100:.0f}% or spread too wide)")
        else:
            print(f"  *** {len(all_opps)} FOUND | "
                  f"BTC-Gap:{len(s1)} Certain:{len(s2)} Closing:{len(s3)} ***")

            for i, o in enumerate(all_opps[:4]):
                price  = o["price"]
                conf   = o["confidence"] / 100
                bet    = kelly_size(bankroll, conf, price)
                back   = round(bet / price, 2)
                profit = round(back - bet, 2)
                ret    = round(profit / bet * 100, 0) if bet > 0 else 0
                sflag  = "OK" if o["spread"] <= 0.02 else \
                         "FAIR" if o["spread"] <= 0.03 else "WIDE"

                print(f"\n  #{i+1} [{o['strategy']}] {o['risk']}")
                print(f"     {o['question']}")
                print(f"     {o['side']} @ {price*100:.1f}c | "
                      f"Edge:+{o['edge']*100:.1f}% | Conf:{o['confidence']:.0f}%")
                print(f"     Book: {o['spread']*100:.1f}c spread [{sflag}] "
                      f"| depth:{o['depth']:.0f}")
                if "gap" in o:
                    print(f"     BTC Gap: ${o['gap']:+,.0f} "
                          f"(${o['btc']:,.0f} vs beat ${o['beat']:,.0f})")
                if "hours_left" in o:
                    print(f"     Closes: {o['hours_left']}h left")
                print(f"     Kelly: ${bet:.2f} -> ${back:.2f} "
                      f"(+${profit:.2f}/+{ret:.0f}%)")
                if DRY_RUN:
                    print(f"     [DRY RUN] bet ${bet:.2f}")
                    total_trades += 1

        print(f"  Total logged: {total_trades} | "
              f"Next in {CHECK_EVERY}s...")
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    run_bot()
