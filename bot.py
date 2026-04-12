"""
POLYMARKET ARB BOT — FIXED VERSION
Error fixed: 'price' key error from Binance API
Now uses 5 different price sources with fallback chain
"""

import os, sys, re, math, time, logging, random, requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

PRIVATE_KEY    = os.getenv("POLY_PRIVATE_KEY", "")
API_KEY        = os.getenv("POLY_API_KEY", "")
API_SECRET     = os.getenv("POLY_API_SECRET", "")
API_PASSPHRASE = os.getenv("POLY_PASSPHRASE", "")

BANKROLL       = 10.0
MIN_EDGE       = 0.12
MIN_BET        = 0.50
MAX_BET_PCT    = 0.35
KELLY_FRAC     = 0.30
SCAN_SECS      = 45
MAX_DAILY      = 25

DRY_RUN = ("--live" not in sys.argv)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[logging.FileHandler("polybot.log"), logging.StreamHandler()]
)
log = logging.getLogger()

# ══════════════════════════════════════════
# FIX: MULTIPLE BTC PRICE SOURCES
# Binance blocks Railway IPs sometimes
# We try 5 sources — one will always work
# ══════════════════════════════════════════

def get_btc_price() -> float:
    """
    Try 5 different APIs in order.
    Returns price as float, or 0.0 if all fail.
    """

    # SOURCE 1: Binance — fastest, but sometimes blocks VPS IPs
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"},
            timeout=5,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        data = r.json()
        # THE FIX: check key exists before accessing
        if isinstance(data, dict) and "price" in data:
            price = float(data["price"])
            log.info(f"BTC Price (Binance): ${price:,.0f}")
            return price
        else:
            log.warning(f"Binance returned unexpected format: {data}")
    except Exception as e:
        log.warning(f"Binance failed: {e}")

    # SOURCE 2: CoinGecko — free, no key needed
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin", "vs_currencies": "usd"},
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        data = r.json()
        if "bitcoin" in data and "usd" in data["bitcoin"]:
            price = float(data["bitcoin"]["usd"])
            log.info(f"BTC Price (CoinGecko): ${price:,.0f}")
            return price
        else:
            log.warning(f"CoinGecko unexpected: {data}")
    except Exception as e:
        log.warning(f"CoinGecko failed: {e}")

    # SOURCE 3: Kraken — very reliable, rarely blocked
    try:
        r = requests.get(
            "https://api.kraken.com/0/public/Ticker",
            params={"pair": "XBTUSD"},
            timeout=8
        )
        data = r.json()
        if "result" in data and "XXBTZUSD" in data["result"]:
            price = float(data["result"]["XXBTZUSD"]["c"][0])
            log.info(f"BTC Price (Kraken): ${price:,.0f}")
            return price
    except Exception as e:
        log.warning(f"Kraken failed: {e}")

    # SOURCE 4: Coinbase — US exchange, solid uptime
    try:
        r = requests.get(
            "https://api.coinbase.com/v2/prices/BTC-USD/spot",
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        data = r.json()
        if "data" in data and "amount" in data["data"]:
            price = float(data["data"]["amount"])
            log.info(f"BTC Price (Coinbase): ${price:,.0f}")
            return price
    except Exception as e:
        log.warning(f"Coinbase failed: {e}")

    # SOURCE 5: Blockchain.info — simplest API ever
    try:
        r = requests.get(
            "https://blockchain.info/ticker",
            timeout=8
        )
        data = r.json()
        if "USD" in data and "last" in data["USD"]:
            price = float(data["USD"]["last"])
            log.info(f"BTC Price (Blockchain.info): ${price:,.0f}")
            return price
    except Exception as e:
        log.warning(f"Blockchain.info failed: {e}")

    log.error("ALL price sources failed — skipping this scan")
    return 0.0


def get_btc_momentum() -> float:
    """Get 24h price change % for momentum signal."""
    # Try Binance first
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/24hr",
            params={"symbol": "BTCUSDT"},
            timeout=5,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        data = r.json()
        if isinstance(data, dict) and "priceChangePercent" in data:
            return float(data["priceChangePercent"]) / 100
    except:
        pass

    # Fallback: Kraken 24h
    try:
        r = requests.get(
            "https://api.kraken.com/0/public/Ticker",
            params={"pair": "XBTUSD"},
            timeout=6
        )
        data = r.json()
        if "result" in data and "XXBTZUSD" in data["result"]:
            ticker = data["result"]["XXBTZUSD"]
            # opening price (o) vs last price (c)
            open_p  = float(ticker["o"])
            close_p = float(ticker["c"][0])
            if open_p > 0:
                return (close_p - open_p) / open_p
    except:
        pass

    return 0.0  # neutral if all fail


# ══════════════════════════════════════════
# POLYMARKET MARKET SCANNER
# ══════════════════════════════════════════

GAMMA = "https://gamma-api.polymarket.com"

def get_markets(tag="", limit=150):
    try:
        p = {
            "active": "true",
            "closed": "false",
            "limit": limit,
            "order": "volume24hr",
            "ascending": "false"
        }
        if tag:
            p["tag_slug"] = tag
        r = requests.get(
            f"{GAMMA}/markets",
            params=p,
            timeout=12,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"Market fetch error: {e}")
        return []


def parse_prices(m):
    op = m.get("outcomePrices")
    if not op:
        return None, None
    try:
        prices = [float(x) for x in (op.split(",") if isinstance(op, str) else op)]
        return (prices[0], prices[1]) if len(prices) >= 2 else (None, None)
    except:
        return None, None


def hours_left(m):
    for k in ("endDate", "endDateIso"):
        v = m.get(k)
        if v:
            try:
                end = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
                return max(0.1, (end - datetime.now(timezone.utc)).total_seconds() / 3600)
            except:
                pass
    return 24.0


# ══════════════════════════════════════════
# MATH HELPERS
# ══════════════════════════════════════════

def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def btc_prob(current, target, direction, hours, momentum=0.0):
    if current <= 0 or target <= 0 or hours <= 0:
        return 0.5
    sigma = 0.035 * math.sqrt(hours / 24)
    drift = momentum * 0.25
    z = (math.log(target / current) - drift) / sigma if sigma else float('inf')
    p = 1 - norm_cdf(z) if direction == "above" else norm_cdf(z)
    return max(0.02, min(0.98, p))


def kelly_size(edge, price, bankroll):
    p = min(0.99, price + edge)
    b = (1 / price) - 1
    if b <= 0:
        return 0.0
    k = max(0, (p * b - (1 - p)) / b) * KELLY_FRAC
    return round(min(bankroll * k, bankroll * MAX_BET_PCT), 2)


# ══════════════════════════════════════════
# SIGNAL FINDER
# ══════════════════════════════════════════

def btc_signal(market, btc, momentum):
    q  = market.get("question", "")
    ql = q.lower()
    if not any(k in ql for k in ["bitcoin", "btc"]):
        return None

    yes_p, no_p = parse_prices(market)
    if yes_p is None:
        return None
    if float(market.get("liquidity", 0)) < 150:
        return None

    hrs = hours_left(market)
    if hrs < 0.5:
        return None

    m = re.search(r'\$([0-9,]+)', q)
    if not m:
        return None
    target = float(m.group(1).replace(",", ""))

    direction = None
    if any(w in ql for w in ["above", "reach", "exceed", "higher"]):
        direction = "above"
    elif any(w in ql for w in ["below", "dip", "drop", "under", "lower"]):
        direction = "below"
    if not direction:
        return None

    tp = btc_prob(btc, target, direction, hrs, momentum)
    ye = tp - yes_p
    ne = (1 - tp) - no_p

    if ye >= MIN_EDGE:
        return {
            "type": "BTC", "side": "YES",
            "market_price": yes_p, "true_prob": tp,
            "edge": ye, "question": q, "hrs": hrs,
            "market_id": market.get("id"),
            "condition_id": market.get("conditionId"),
            "liquidity": float(market.get("liquidity", 0)),
            "extra": f"BTC ${btc:,.0f} → target ${target:,.0f} ({direction})"
        }
    if ne >= MIN_EDGE:
        return {
            "type": "BTC", "side": "NO",
            "market_price": no_p, "true_prob": 1 - tp,
            "edge": ne, "question": q, "hrs": hrs,
            "market_id": market.get("id"),
            "condition_id": market.get("conditionId"),
            "liquidity": float(market.get("liquidity", 0)),
            "extra": f"BTC ${btc:,.0f} → target ${target:,.0f} ({direction})"
        }
    return None


# ══════════════════════════════════════════
# TRADE EXECUTION
# ══════════════════════════════════════════

def trade(sig, bet):
    log.info(
        f"\n┌── {'DRY RUN 🔵' if DRY_RUN else 'LIVE 🔴'} ──────────────────────────────\n"
        f"│ [{sig['type']}] {sig['question'][:58]}\n"
        f"│ Side: {sig['side']} @ {sig['market_price']*100:.1f}¢  "
        f"True: {sig['true_prob']*100:.0f}%  Edge: +{sig['edge']*100:.0f}%\n"
        f"│ {sig['extra']}\n"
        f"│ Bet: ${bet:.2f}  →  Win: ${bet/sig['market_price']:.2f}\n"
        f"└─────────────────────────────────────────────────────"
    )
    if DRY_RUN:
        return True

    if not PRIVATE_KEY:
        log.error("Set POLY_PRIVATE_KEY in Railway Variables!")
        return False

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import OrderArgs, Side
        from py_clob_client.constants import POLYGON

        client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=POLYGON,
            key=PRIVATE_KEY,
            signature_type=2,
            funder=PRIVATE_KEY,
        )
        client.set_api_creds(client.create_or_derive_api_creds())
        order = client.create_market_order(OrderArgs(
            token_id=sig["condition_id"],
            price=sig["market_price"],
            size=bet / sig["market_price"],
            side=Side.BUY,
        ))
        log.info(f"✅ Order: {client.post_order(order)}")
        return True
    except Exception as e:
        log.error(f"Trade failed: {e}")
        return False


# ══════════════════════════════════════════
# MAIN LOOP
# ══════════════════════════════════════════

def run():
    bankroll   = BANKROLL
    bets_today = wins = losses = total = 0
    peak       = BANKROLL
    traded     = set()
    last_day   = datetime.now().date()
    scan       = 0
    consecutive_price_failures = 0

    log.info(f"""
╔══════════════════════════════════════════════╗
║   POLYMARKET BOT — FIXED VERSION            ║
║   Bankroll: ${bankroll:.2f}                        ║
║   Mode: {'DRY RUN' if DRY_RUN else '🔴 LIVE TRADING'}                    ║
║   Price: 5 sources with fallback chain      ║
╚══════════════════════════════════════════════╝
""")

    while True:
        try:
            # Daily reset
            today = datetime.now().date()
            if today != last_day:
                bets_today = 0
                last_day = today

            if bets_today >= MAX_DAILY:
                log.info("Daily limit hit — sleeping 1h")
                time.sleep(3600)
                continue

            if bankroll < MIN_BET:
                log.error("Bankroll too low. Stopping.")
                break

            scan += 1
            roi = (bankroll - BANKROLL) / BANKROLL * 100
            log.info(f"\n─── Scan {scan} │ ${bankroll:.2f} │ {roi:+.1f}% ROI │ {wins}W/{losses}L")

            # Get BTC price — now with 5 fallback sources
            btc = get_btc_price()
            if btc == 0.0:
                consecutive_price_failures += 1
                wait = min(60 * consecutive_price_failures, 300)
                log.warning(f"Price failed {consecutive_price_failures}x — waiting {wait}s")
                time.sleep(wait)
                continue
            else:
                consecutive_price_failures = 0

            momentum = get_btc_momentum()
            log.info(f"BTC: ${btc:,.0f}  Momentum: {momentum*100:+.1f}%")

            # Fetch markets
            markets = []
            for tag in ["bitcoin", "crypto"]:
                markets += get_markets(tag, 100)

            # Deduplicate
            seen, unique = set(), []
            for m in markets:
                mid = m.get("id")
                if mid and mid not in seen:
                    seen.add(mid)
                    unique.append(m)

            log.info(f"Scanning {len(unique)} markets...")

            # Find signals
            signals = []
            for m in unique:
                if m.get("id") in traded:
                    continue
                sig = btc_signal(m, btc, momentum)
                if sig:
                    signals.append(sig)

            signals.sort(key=lambda s: s["edge"], reverse=True)

            if signals:
                log.info(f"🎯 {len(signals)} edge(s)! Best: +{signals[0]['edge']*100:.0f}% [{signals[0]['type']}]")
            else:
                log.info("No edge found — waiting...")

            # Execute top signals
            for sig in signals[:3]:
                if bets_today >= MAX_DAILY or bankroll < MIN_BET:
                    break
                bet = kelly_size(sig["edge"], sig["market_price"], bankroll)
                if bet < MIN_BET:
                    continue

                ok = trade(sig, bet)
                if ok:
                    traded.add(sig["market_id"])
                    bets_today += 1
                    total += 1

                    if DRY_RUN:
                        won = random.random() < sig["true_prob"]
                        if won:
                            profit = bet * ((1 / sig["market_price"]) - 1)
                            bankroll += profit
                            wins += 1
                            log.info(f"  ✅ WIN  +${profit:.2f}  →  ${bankroll:.2f}")
                        else:
                            bankroll -= bet
                            losses += 1
                            log.info(f"  ❌ LOSS -${bet:.2f}  →  ${bankroll:.2f}")
                        peak = max(peak, bankroll)

            time.sleep(SCAN_SECS)

        except KeyboardInterrupt:
            log.info(f"Stopped. Final: ${bankroll:.2f} (peak ${peak:.2f})")
            break
        except Exception as e:
            log.error(f"Loop error: {e}")
            time.sleep(15)


if __name__ == "__main__":
    run()
