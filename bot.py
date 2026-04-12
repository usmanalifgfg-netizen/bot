"""
╔══════════════════════════════════════════════════════════════╗
║   POLYMARKET BTC 5-MINUTE UP/DOWN BOT                      ║
║                                                             ║
║   EXACT STRATEGY:                                           ║
║   - BTC 5-min binary markets: did BTC go UP or DOWN?       ║
║   - Buy UP tokens at $0.55–0.67 when momentum detected     ║
║   - Spread orders across price levels in one window        ║
║   - Collect $1.00 per token on resolution                  ║
║   - Repeat every 5 minutes, compound every win             ║
║                                                             ║
║   INSTALL:  pip install requests py-clob-client python-dotenv websocket-client  ║
║   RUN:      python bot.py           (dry run)              ║
║             python bot.py --live    (real money)           ║
╚══════════════════════════════════════════════════════════════╝
"""

import os, sys, re, math, time, logging, random, requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

# ════════════════════════════════════════════════
# YOUR KEYS — put in .env file
# ════════════════════════════════════════════════
PRIVATE_KEY    = os.getenv("POLY_PRIVATE_KEY", "")
API_KEY        = os.getenv("POLY_API_KEY", "")
API_SECRET     = os.getenv("POLY_API_SECRET", "")
API_PASSPHRASE = os.getenv("POLY_PASSPHRASE", "")

# ════════════════════════════════════════════════
# BOT SETTINGS
# ════════════════════════════════════════════════
STARTING_BANKROLL = 10.0

# Entry settings (from the strategy)
# Buy UP when price is 0.55–0.67 (market unsure, we have edge)
MIN_ENTRY_PRICE   = 0.52    # don't buy above this (too expensive)
MAX_ENTRY_PRICE   = 0.67    # sweet spot max
MIN_ENTRY_PRICE   = 0.52    # minimum (below this market too bearish)

# Kelly + sizing
KELLY_FRAC        = 0.25    # 25% Kelly — aggressive but safe
MAX_BET_PCT       = 0.40    # max 40% bankroll per 5-min window
MIN_BET           = 0.50    # minimum per order
SPREAD_ORDERS     = 3       # split bet into 3 orders at different prices

# Momentum thresholds
MOMENTUM_PERIOD   = 5       # minutes of BTC data to check
MIN_MOMENTUM      = 0.0008  # 0.08% move required to enter (strong signal)
STRONG_MOMENTUM   = 0.003   # 0.3% = very strong, bet more

# Timing
WINDOW_SECONDS    = 300     # 5 minutes per window
ENTRY_CUTOFF      = 60      # stop entering 60s before window closes
POLL_INTERVAL     = 8       # check price every 8 seconds

DRY_RUN = "--live" not in sys.argv

# ════════════════════════════════════════════════
# LOGGING
# ════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[logging.FileHandler("bot5min.log"), logging.StreamHandler()]
)
log = logging.getLogger()


# ════════════════════════════════════════════════════════════════
# BTC PRICE FEED
# Real-time from Binance — updates every second
# This is the KEY ADVANTAGE over Polymarket which lags
# ════════════════════════════════════════════════════════════════

class BTCFeed:
    """
    Tracks BTC price history and calculates momentum.
    Binance gives us price BEFORE Polymarket updates.
    That lag = our edge.
    """
    def __init__(self):
        self.prices  = []   # list of (timestamp, price)
        self.last_fetch = 0

    def update(self):
        now = time.time()
        if now - self.last_fetch < 3:  # don't spam API
            return

        try:
            r = requests.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": "BTCUSDT"}, timeout=4
            )
            price = float(r.json()["price"])
            self.prices.append((now, price))
            # Keep only last 15 minutes
            cutoff = now - 900
            self.prices = [(t, p) for t, p in self.prices if t > cutoff]
            self.last_fetch = now
            return price
        except:
            return self.current_price()

    def current_price(self):
        return self.prices[-1][1] if self.prices else 0.0

    def price_n_seconds_ago(self, seconds):
        cutoff = time.time() - seconds
        old = [(t, p) for t, p in self.prices if t <= cutoff]
        return old[-1][1] if old else self.current_price()

    def momentum(self, seconds=300):
        """
        Returns % change over last N seconds.
        Positive = BTC going UP
        Negative = BTC going DOWN
        """
        current = self.current_price()
        old     = self.price_n_seconds_ago(seconds)
        if old <= 0:
            return 0.0
        return (current - old) / old

    def momentum_strength(self, seconds=60):
        """Short-term momentum — last 60 seconds."""
        return self.momentum(seconds)

    def is_trending_up(self):
        """Is BTC clearly going up right now?"""
        m5  = self.momentum(300)   # 5 min
        m1  = self.momentum(60)    # 1 min
        m30 = self.momentum(30)    # 30 sec

        # All timeframes agree = strong signal
        if m5 > MIN_MOMENTUM and m1 > 0 and m30 > 0:
            return True, max(m5, m1)
        return False, 0.0

    def is_trending_down(self):
        """Is BTC clearly going down right now?"""
        m5  = self.momentum(300)
        m1  = self.momentum(60)
        m30 = self.momentum(30)

        if m5 < -MIN_MOMENTUM and m1 < 0 and m30 < 0:
            return True, abs(min(m5, m1))
        return False, 0.0

    def summary(self):
        p = self.current_price()
        m5 = self.momentum(300) * 100
        m1 = self.momentum(60) * 100
        return f"BTC ${p:,.0f}  |  5m: {m5:+.3f}%  |  1m: {m1:+.3f}%"


# ════════════════════════════════════════════════════════════════
# POLYMARKET MARKET SCANNER
# Find the live 5-minute BTC up/down markets
# ════════════════════════════════════════════════════════════════

GAMMA = "https://gamma-api.polymarket.com"
CLOB  = "https://clob.polymarket.com"

def get_5min_btc_markets():
    """
    Find all currently open BTC 5-minute up/down markets.
    These look like:
      "Will BTC be higher or lower in 5 minutes?"
      "BTC Up or Down - 14:35 UTC"
      "Bitcoin price movement 5min"
    """
    try:
        markets = []
        for tag in ["bitcoin", "crypto", "btc"]:
            r = requests.get(f"{GAMMA}/markets", params={
                "active": "true", "closed": "false",
                "limit": 200, "tag_slug": tag,
                "order": "volume24hr", "ascending": "false",
            }, timeout=10)
            markets += r.json()

        # Deduplicate
        seen, unique = set(), []
        for m in markets:
            mid = m.get("id")
            if mid and mid not in seen:
                seen.add(mid)
                unique.append(m)

        # Filter for 5-minute up/down markets
        five_min = []
        keywords = ["5 min", "5min", "five min", "up or down", "higher or lower",
                    "up/down", "5-min", "5 minute", "movement"]
        for m in unique:
            q = (m.get("question") or "").lower()
            if any(k in q for k in keywords):
                # Check it resolves soon (within 10 minutes)
                hrs = hours_until_close(m)
                if 0 < hrs <= 0.17:  # 0-10 minutes
                    five_min.append(m)

        log.info(f"Found {len(five_min)} live 5-min BTC markets")
        return five_min

    except Exception as e:
        log.error(f"Market fetch error: {e}")
        return []


def get_all_btc_updown_markets():
    """
    Broader search — any BTC directional market resolving soon.
    Includes hourly, daily up/down markets too.
    """
    try:
        r = requests.get(f"{GAMMA}/markets", params={
            "active": "true", "closed": "false",
            "limit": 200, "tag_slug": "bitcoin",
            "order": "endDate", "ascending": "true",
        }, timeout=10)
        markets = r.json()

        candidates = []
        for m in markets:
            q = (m.get("question") or "").lower()
            hrs = hours_until_close(m)

            # Up/down directional markets resolving within 6 hours
            if (any(k in q for k in ["up or down", "higher or lower", "up/down"]) and
                    0.05 < hrs <= 6):
                candidates.append(m)

        log.info(f"Found {len(candidates)} BTC directional markets (next 6h)")
        return candidates

    except Exception as e:
        log.error(f"Broad market fetch error: {e}")
        return []


def hours_until_close(market):
    for key in ("endDate", "endDateIso"):
        val = market.get(key)
        if val:
            try:
                end = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
                delta = (end - datetime.now(timezone.utc)).total_seconds() / 3600
                return max(0, delta)
            except:
                pass
    return 999


def parse_prices(market):
    op = market.get("outcomePrices")
    if not op:
        return None, None
    try:
        prices = [float(x) for x in (op.split(",") if isinstance(op, str) else op)]
        if len(prices) >= 2:
            return prices[0], prices[1]  # YES/UP price, NO/DOWN price
    except:
        pass
    return None, None


def get_orderbook(condition_id):
    """Get live orderbook to find best entry prices."""
    try:
        r = requests.get(f"{CLOB}/book",
                         params={"token_id": condition_id}, timeout=6)
        if r.status_code == 200:
            return r.json()
    except:
        pass
    return None


# ════════════════════════════════════════════════════════════════
# SIGNAL ENGINE
# "Buy UP tokens at $0.55–0.67 when momentum detected"
# ════════════════════════════════════════════════════════════════

def calculate_true_probability(momentum_5m, momentum_1m, momentum_30s):
    """
    Given BTC momentum signals, what's the TRUE probability of UP?

    Base: 50/50
    Adjust for momentum strength and agreement across timeframes.

    This is what the market SHOULD price in — but it lags.
    """
    base = 0.50

    # Weight short-term more (5-min market = short-term matters)
    score = (momentum_5m * 40) + (momentum_1m * 35) + (momentum_30s * 25)

    # Normalize: 0.5% move = strong signal
    adj = score / 0.005 * 0.20    # max ±20% adjustment
    adj = max(-0.25, min(0.25, adj))

    prob = base + adj
    return max(0.30, min(0.80, prob))


def find_signal(market, btc: BTCFeed):
    """
    Analyze a market and return trade signal or None.

    THE CORE LOGIC:
    1. Check BTC momentum (real-time from Binance)
    2. Check what Polymarket is pricing UP at
    3. If market price is LOWER than true probability → BUY UP
    4. If market price of DOWN is lower than true probability → BUY DOWN
    """
    up_price, down_price = parse_prices(market)
    if up_price is None:
        return None

    liq = float(market.get("liquidity", 0))
    if liq < 50:  # need some liquidity
        return None

    hrs = hours_until_close(market)
    if hrs <= 0.016:  # less than 1 minute left — too late
        return None

    # Get momentum at all timeframes
    m5  = btc.momentum(300)
    m1  = btc.momentum(60)
    m30 = btc.momentum(30)

    true_prob_up = calculate_true_probability(m5, m1, m30)
    true_prob_dn = 1 - true_prob_up

    # Check edge
    up_edge = true_prob_up - up_price
    dn_edge = true_prob_dn - down_price

    # Strategy: Buy UP at 0.55-0.67 when momentum is up
    if (up_edge > 0.08 and
            MIN_ENTRY_PRICE <= up_price <= MAX_ENTRY_PRICE and
            m5 > MIN_MOMENTUM):

        strength = "STRONG 🔥" if abs(m5) > STRONG_MOMENTUM else "normal"
        return {
            "side":        "UP",
            "market_price": up_price,
            "true_prob":   true_prob_up,
            "edge":        up_edge,
            "strength":    strength,
            "momentum_5m": m5,
            "momentum_1m": m1,
            "question":    market.get("question"),
            "market_id":   market.get("id"),
            "condition_id": market.get("conditionId"),
            "hrs_left":    hrs,
            "liquidity":   liq,
        }

    # Buy DOWN if momentum strongly negative
    if (dn_edge > 0.08 and
            MIN_ENTRY_PRICE <= down_price <= MAX_ENTRY_PRICE and
            m5 < -MIN_MOMENTUM):

        strength = "STRONG 🔥" if abs(m5) > STRONG_MOMENTUM else "normal"
        return {
            "side":        "DOWN",
            "market_price": down_price,
            "true_prob":   true_prob_dn,
            "edge":        dn_edge,
            "strength":    strength,
            "momentum_5m": m5,
            "momentum_1m": m1,
            "question":    market.get("question"),
            "market_id":   market.get("id"),
            "condition_id": market.get("conditionId"),
            "hrs_left":    hrs,
            "liquidity":   liq,
        }

    return None


# ════════════════════════════════════════════════════════════════
# POSITION SIZING
# "Spread orders across different price levels" — like the $7→$112k bot
# ════════════════════════════════════════════════════════════════

def size_orders(signal, bankroll):
    """
    Split total bet into multiple orders at slightly different prices.
    This is what the original bot did:
    "spread orders across different price levels within a single 5-min window"

    Returns list of (price, tokens, cost) tuples.
    """
    edge  = signal["edge"]
    price = signal["market_price"]
    strength = signal["momentum_5m"]

    # Kelly sizing
    p = min(0.95, price + edge)
    b = (1 / price) - 1
    k = max(0, (p * b - (1 - p)) / b) * KELLY_FRAC

    # Adjust for momentum strength
    if abs(strength) > STRONG_MOMENTUM:
        k *= 1.4  # bet more on strong signals

    total_bet = min(bankroll * k, bankroll * MAX_BET_PCT)
    total_bet = max(MIN_BET * SPREAD_ORDERS, total_bet)

    if total_bet < MIN_BET:
        return []

    # Split into SPREAD_ORDERS orders at slightly different prices
    orders = []
    weights = [0.50, 0.30, 0.20]  # bigger order at best price
    for i in range(SPREAD_ORDERS):
        order_cost = total_bet * weights[i]
        if order_cost < MIN_BET:
            continue
        # Slightly different price levels (buying at market + small increments)
        order_price = min(0.95, price + (i * 0.01))
        tokens = order_cost / order_price
        orders.append({
            "price":  order_price,
            "tokens": round(tokens, 1),
            "cost":   round(order_cost, 2),
        })

    return orders


# ════════════════════════════════════════════════════════════════
# TRADE EXECUTION
# ════════════════════════════════════════════════════════════════

def execute_orders(signal, orders, bankroll):
    """
    Place spread orders on Polymarket.
    On resolution → collect $1.00 per token = pure profit.
    """
    total_cost   = sum(o["cost"] for o in orders)
    total_tokens = sum(o["tokens"] for o in orders)
    potential    = total_tokens * 1.00  # $1 per token on win
    profit_if_win = potential - total_cost

    log.info(f"""
┌── {'DRY RUN 🔵' if DRY_RUN else 'LIVE TRADE 🔴'} ─────────────────────────────────────
│ Market:   {signal['question'][:55]}
│ Side:     {signal['side']}  ({signal['strength']})
│ Momentum: 5m={signal['momentum_5m']*100:+.3f}%  1m={signal['momentum_1m']*100:+.3f}%
│ Mkt Price:{signal['market_price']*100:.0f}¢  True Prob:{signal['true_prob']*100:.0f}%  Edge:+{signal['edge']*100:.0f}%
│ ─────────────────────────────────────────────────────
│ ORDERS:""")

    for i, o in enumerate(orders, 1):
        log.info(f"│   Order {i}: {o['tokens']:.0f} tokens @ {o['price']*100:.0f}¢ = ${o['cost']:.2f}")

    log.info(f"""│ ─────────────────────────────────────────────────────
│ Total spent:   ${total_cost:.2f}
│ Total tokens:  {total_tokens:.0f}
│ If WIN:        ${potential:.2f}  (profit: +${profit_if_win:.2f})
│ If LOSE:       $0.00  (loss: -${total_cost:.2f})
└─────────────────────────────────────────────────────────""")

    if DRY_RUN:
        return True, total_cost, total_tokens

    if not PRIVATE_KEY:
        log.error("❌ No POLY_PRIVATE_KEY in .env file!")
        return False, 0, 0

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

        placed = 0
        for o in orders:
            try:
                order = client.create_market_order(OrderArgs(
                    token_id = signal["condition_id"],
                    price    = o["price"],
                    size     = o["tokens"],
                    side     = Side.BUY,
                ))
                resp = client.post_order(order)
                log.info(f"  ✅ Order placed: {resp}")
                placed += 1
                time.sleep(0.5)
            except Exception as e:
                log.error(f"  ❌ Order failed: {e}")

        success = placed > 0
        return success, total_cost, total_tokens

    except ImportError:
        log.error("Run: pip install py-clob-client")
        return False, 0, 0
    except Exception as e:
        log.error(f"Execution error: {e}")
        return False, 0, 0


# ════════════════════════════════════════════════════════════════
# MAIN BOT LOOP
# Every 5 minutes = one window = one opportunity
# ════════════════════════════════════════════════════════════════

def run():
    bankroll   = STARTING_BANKROLL
    wins = losses = total = 0
    peak       = bankroll
    traded     = set()
    session_start = time.time()

    btc = BTCFeed()

    log.info(f"""
╔══════════════════════════════════════════════════════╗
║   BTC 5-MINUTE UP/DOWN BOT — STARTED               ║
║   Bankroll: ${bankroll:.2f}                                ║
║   Mode: {'DRY RUN (safe simulation)' if DRY_RUN else '🔴 LIVE TRADING — REAL MONEY'}    ║
║                                                     ║
║   Strategy:                                         ║
║   • Monitor BTC momentum every 8 seconds            ║
║   • Find 5-min up/down markets on Polymarket        ║
║   • Buy UP at 55-67¢ when momentum confirms         ║
║   • Spread 3 orders per window                      ║
║   • Collect $1.00/token on win, compound            ║
╚══════════════════════════════════════════════════════╝
""")

    # Warm up price feed
    log.info("Warming up BTC price feed (15 seconds)...")
    for _ in range(5):
        btc.update()
        time.sleep(3)

    scan = 0
    while True:
        try:
            scan += 1
            btc.update()

            roi = (bankroll - STARTING_BANKROLL) / STARTING_BANKROLL * 100
            uptime = (time.time() - session_start) / 3600

            log.info(f"\n─── Scan {scan} │ {btc.summary()} │ "
                     f"${bankroll:.2f} ({roi:+.0f}%) │ {wins}W/{losses}L │ "
                     f"Uptime: {uptime:.1f}h")

            if bankroll < 0.50:
                log.error("❌ Bankroll depleted!")
                break

            # Get live markets (5-min first, then broader)
            markets = get_5min_btc_markets()
            if not markets:
                markets = get_all_btc_updown_markets()

            if not markets:
                log.info("  No 5-min markets open right now. Waiting...")
                time.sleep(POLL_INTERVAL)
                continue

            # Scan each market for signal
            best_signal = None
            for m in markets:
                mid = m.get("id")
                if mid in traded:
                    continue

                sig = find_signal(m, btc)
                if sig:
                    if best_signal is None or sig["edge"] > best_signal["edge"]:
                        best_signal = sig

            if not best_signal:
                log.info(f"  No edge found (BTC momentum weak or markets fairly priced)")
                time.sleep(POLL_INTERVAL)
                continue

            # Found a signal — size and execute
            orders = size_orders(best_signal, bankroll)
            if not orders:
                log.info("  Signal found but bet too small — skipping")
                time.sleep(POLL_INTERVAL)
                continue

            ok, spent, tokens = execute_orders(best_signal, orders, bankroll)

            if ok and spent > 0:
                traded.add(best_signal["market_id"])
                total += 1

                if DRY_RUN:
                    # Simulate result based on true probability
                    won = random.random() < best_signal["true_prob"]
                    if won:
                        collected = tokens * 1.00  # $1 per token
                        profit    = collected - spent
                        bankroll  += profit
                        wins += 1
                        log.info(f"\n  ✅ WIN!  Collected ${collected:.2f}  "
                                 f"Profit: +${profit:.2f}  "
                                 f"Bankroll: ${bankroll:.2f}")
                    else:
                        bankroll -= spent
                        losses += 1
                        log.info(f"\n  ❌ LOSS  Lost ${spent:.2f}  "
                                 f"Bankroll: ${bankroll:.2f}")

                    peak = max(peak, bankroll)

                    # Print milestone
                    if bankroll >= 100 and (bankroll - spent) < 100:
                        log.info("🎯 MILESTONE: $100 reached!")
                    if bankroll >= 1000 and (bankroll - spent) < 1000:
                        log.info("🎯 MILESTONE: $1,000 reached!")
                    if bankroll >= 10000 and (bankroll - spent) < 10000:
                        log.info("🎯 TARGET HIT: $10,000! 🚀🚀🚀")

            # Wait before next scan
            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            roi = (bankroll - STARTING_BANKROLL) / STARTING_BANKROLL * 100
            log.info(f"""
╔═══════════════════════════════════╗
║   BOT STOPPED                    ║
║   Final:  ${bankroll:,.2f}             ║
║   Peak:   ${peak:,.2f}             ║
║   ROI:    {roi:+.0f}%                ║
║   Record: {wins}W / {losses}L          ║
╚═══════════════════════════════════╝
""")
            break
        except Exception as e:
            log.error(f"Loop error: {e}")
            time.sleep(10)


if __name__ == "__main__":
    run()
