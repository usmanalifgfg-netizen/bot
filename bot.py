"""
╔══════════════════════════════════════════════════════╗
║   POLYMARKET BTC ARBITRAGE BOT                      ║
║   Strategy: Watch BTC price vs Polymarket odds      ║
║   Enter when they don't match. Exit when they do.   ║
║   Exact reverse-engineer of the $7→$112k account    ║
╚══════════════════════════════════════════════════════╝

HOW IT WORKS:
- Fetches real BTC price every 30 seconds (Binance API, free)
- Scans all open BTC prediction markets on Polymarket
- Calculates TRUE probability based on current BTC price + volatility model
- If Polymarket is showing WAY lower odds than reality → BUY CHEAP YES
- Compounds every win back into the next bet
- Runs 24/7 on a $5/month VPS

REQUIREMENTS:
  pip install requests py-clob-client python-dotenv

SETUP:
  1. Create .env file with your keys (see bottom of this file)
  2. Fund your Polymarket wallet with USDC on Polygon
  3. Run: python bot.py
"""

import os
import time
import math
import json
import logging
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

# ═══════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════

PRIVATE_KEY    = os.getenv("POLY_PRIVATE_KEY", "")   # Your wallet private key
API_KEY        = os.getenv("POLY_API_KEY", "")        # From polymarket.com/profile
API_SECRET     = os.getenv("POLY_API_SECRET", "")
API_PASSPHRASE = os.getenv("POLY_PASSPHRASE", "")

STARTING_BANKROLL  = 10.0    # USD
MIN_EDGE_TO_TRADE  = 0.15    # Only trade if we have 15%+ edge (market price is THIS wrong)
MIN_BET_USD        = 0.50    # Minimum bet
MAX_BET_FRACTION   = 0.40    # Max 40% of bankroll on one bet (aggressive compounding)
KELLY_FRACTION     = 0.30    # 30% Kelly (aggressive but not suicidal)
SCAN_INTERVAL_SEC  = 30      # Check every 30 seconds
MAX_DAILY_BETS     = 20      # Safety cap on bets per day

# BTC Volatility model parameters
# These control how far BTC can realistically move by resolution
DAILY_VOL_PCT = 0.035        # ~3.5% daily vol for BTC (conservative)

# ═══════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("polybot.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("polybot")


# ═══════════════════════════════════════════════════════
# STEP 1: GET REAL BTC PRICE (Binance — always fresh)
# ═══════════════════════════════════════════════════════

def get_btc_price() -> float:
    """Get current BTC/USDT price from Binance. No API key needed."""
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"},
            timeout=5
        )
        price = float(r.json()["price"])
        log.info(f"BTC Price: ${price:,.0f}")
        return price
    except Exception as e:
        # Fallback to CoinGecko
        try:
            r = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "bitcoin", "vs_currencies": "usd"},
                timeout=8
            )
            price = float(r.json()["bitcoin"]["usd"])
            log.info(f"BTC Price (CoinGecko): ${price:,.0f}")
            return price
        except:
            log.error(f"Failed to get BTC price: {e}")
            return 0.0


def get_btc_24h_change() -> float:
    """Get BTC 24h price change % for momentum signal."""
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/24hr",
            params={"symbol": "BTCUSDT"},
            timeout=5
        )
        return float(r.json()["priceChangePercent"]) / 100
    except:
        return 0.0


# ═══════════════════════════════════════════════════════
# STEP 2: SCAN POLYMARKET FOR BTC MARKETS
# ═══════════════════════════════════════════════════════

GAMMA_API = "https://gamma-api.polymarket.com"

def get_btc_markets() -> list:
    """
    Fetch all open BTC price prediction markets.
    These are markets like:
      - "Will Bitcoin be above $68,000 on April 2?"
      - "Will Bitcoin reach $76,000 March 30-April 5?"
      - "Bitcoin Up or Down on April 1?"
    """
    try:
        r = requests.get(
            f"{GAMMA_API}/markets",
            params={
                "active": "true",
                "closed": "false",
                "limit": 100,
                "tag_slug": "bitcoin",     # Filter for Bitcoin markets
                "order": "volume24hr",
                "ascending": "false",
            },
            timeout=10
        )
        r.raise_for_status()
        markets = r.json()

        # Keep only price prediction markets (not sentiment/misc)
        btc_markets = []
        keywords = ["price", "above", "below", "reach", "dip", "up or down",
                    "$", "btc", "bitcoin", "68,000", "70,000", "75,000"]
        for m in markets:
            q = (m.get("question") or "").lower()
            if any(k in q for k in keywords):
                btc_markets.append(m)

        log.info(f"Found {len(btc_markets)} BTC prediction markets")
        return btc_markets

    except Exception as e:
        log.error(f"Failed to fetch markets: {e}")
        return []


def parse_market_target(question: str) -> dict:
    """
    Parse a market question to extract:
    - target_price: the price level being predicted
    - direction: "above" or "below" or "up_down"
    - resolution_date: when it resolves (if detectable)

    Examples:
      "Will Bitcoin be above $68,000 on April 2?" → target=68000, dir=above
      "Will Bitcoin dip to $66,000 on April 1?"   → target=66000, dir=below
      "Bitcoin Up or Down on April 1?"             → direction=up_down
    """
    import re

    q = question.lower()
    result = {"target_price": None, "direction": None, "raw": question}

    # Extract dollar amount
    price_match = re.search(r'\$([0-9,]+)', question.replace(",", ""))
    if price_match:
        # Remove commas and parse
        price_str = re.search(r'\$([0-9,]+)', question)
        if price_str:
            result["target_price"] = float(price_str.group(1).replace(",", ""))

    # Detect direction
    if "above" in q or "reach" in q or "exceed" in q or "over" in q:
        result["direction"] = "above"
    elif "below" in q or "dip" in q or "under" in q or "drop" in q:
        result["direction"] = "below"
    elif "up or down" in q or "higher or lower" in q:
        result["direction"] = "up_down"

    return result


# ═══════════════════════════════════════════════════════
# STEP 3: CALCULATE TRUE PROBABILITY
# Using a log-normal distribution model (how options are priced)
# ═══════════════════════════════════════════════════════

def calculate_true_probability(
    current_btc: float,
    target_price: float,
    direction: str,
    hours_to_resolution: float,
    momentum_24h: float = 0.0
) -> float:
    """
    Calculate mathematically what probability the market SHOULD be at.

    Uses log-normal model (same as Black-Scholes for options):
    - If BTC is at $74,000 and target is $68,000 (above), prob ≈ 96%+
    - If BTC is at $74,000 and target is $80,000 (above), prob depends on time/vol

    momentum_24h: adds drift signal (if BTC up 5% today, bullish adjustment)
    """
    if current_btc <= 0 or target_price <= 0 or hours_to_resolution <= 0:
        return 0.5  # Can't compute, return neutral

    # Days to resolution
    days = hours_to_resolution / 24.0

    # Log-normal parameters
    sigma = DAILY_VOL_PCT * math.sqrt(days)   # Volatility scaled to time
    drift = momentum_24h * 0.3                 # Slight momentum adjustment

    # Log return needed
    log_return = math.log(target_price / current_btc)

    # Z-score (standard deviations from current price)
    if sigma > 0:
        z = (log_return - drift) / sigma
    else:
        z = float('inf') if target_price > current_btc else float('-inf')

    # CDF of standard normal (probability)
    def norm_cdf(x):
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))

    if direction == "above":
        prob = 1 - norm_cdf(z)
    elif direction == "below":
        prob = norm_cdf(z)
    elif direction == "up_down":
        # "Up" = BTC higher than now. Simple 50/50 + momentum
        prob = 0.5 + momentum_24h * 2
        prob = max(0.1, min(0.9, prob))
    else:
        return 0.5

    # Clamp to avoid extreme values
    return max(0.01, min(0.99, prob))


# ═══════════════════════════════════════════════════════
# STEP 4: FIND THE EDGE
# The key logic: "Enter when they don't match"
# ═══════════════════════════════════════════════════════

def find_edge(market: dict, btc_price: float, btc_momentum: float) -> dict | None:
    """
    Compare our calculated probability vs Polymarket's current price.
    If the gap (edge) is large enough, return a trade signal.

    The original bot strategy:
    - Market shows YES at 1.7¢ → market thinks 1.7% chance
    - Real BTC math says 85% chance → MASSIVE edge → BUY YES cheap
    """
    question = market.get("question", "")
    parsed   = parse_market_target(question)

    target_price = parsed["target_price"]
    direction    = parsed["direction"]

    if not target_price or not direction:
        return None

    # Get market's current YES price
    outcome_prices = market.get("outcomePrices")
    if not outcome_prices:
        return None

    try:
        if isinstance(outcome_prices, str):
            prices = [float(p) for p in outcome_prices.split(",")]
        else:
            prices = [float(p) for p in outcome_prices]
        yes_price = prices[0]
        no_price  = prices[1] if len(prices) > 1 else 1 - yes_price
    except:
        return None

    # Skip markets with very low liquidity
    if float(market.get("liquidity", 0)) < 200:
        return None

    # Estimate hours to resolution
    end_date_str = market.get("endDate") or market.get("endDateIso")
    hours_left = 48  # default fallback
    if end_date_str:
        try:
            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            now_dt = datetime.now(timezone.utc)
            hours_left = max(0.5, (end_dt - now_dt).total_seconds() / 3600)
        except:
            pass

    # Don't trade if < 1 hour left (price already locked in)
    if hours_left < 1:
        return None

    # Calculate what the TRUE probability should be
    true_prob = calculate_true_probability(
        current_btc    = btc_price,
        target_price   = target_price,
        direction      = direction,
        hours_to_resolution = hours_left,
        momentum_24h   = btc_momentum
    )

    # ──────────────────────────────────────────
    # THE CORE LOGIC: Find mispricing
    # ──────────────────────────────────────────

    # Case 1: YES is massively underpriced (like the screenshot trades)
    # e.g., YES at 1.7¢ but real probability is 85%
    yes_edge = true_prob - yes_price
    no_edge  = (1 - true_prob) - no_price

    best_side = None
    best_edge = 0
    market_price = 0

    if yes_edge > no_edge and yes_edge >= MIN_EDGE_TO_TRADE:
        best_side  = "YES"
        best_edge  = yes_edge
        market_price = yes_price
    elif no_edge >= MIN_EDGE_TO_TRADE:
        best_side  = "NO"
        best_edge  = no_edge
        market_price = no_price

    if not best_side:
        return None

    return {
        "market_id":    market.get("id"),
        "condition_id": market.get("conditionId"),
        "question":     question,
        "side":         best_side,
        "market_price": market_price,
        "true_prob":    true_prob,
        "edge":         best_edge,
        "hours_left":   hours_left,
        "target_price": target_price,
        "direction":    direction,
        "btc_at_scan":  btc_price,
        "liquidity":    float(market.get("liquidity", 0)),
    }


# ═══════════════════════════════════════════════════════
# STEP 5: KELLY POSITION SIZING
# ═══════════════════════════════════════════════════════

def kelly_size(edge: float, market_price: float, bankroll: float) -> float:
    """
    Kelly criterion: bet the mathematically optimal fraction.
    With 30% Kelly fraction for aggression without ruin.

    For binary bets:
    f* = (p*b - q) / b
    where b = (1/price - 1), p = our true prob, q = 1-p
    """
    p = market_price + edge          # our estimated probability
    q = 1 - p
    b = (1 / market_price) - 1       # payout multiplier

    if b <= 0:
        return 0

    kelly_full = (p * b - q) / b
    kelly_frac = max(0, kelly_full) * KELLY_FRACTION

    bet = bankroll * kelly_frac
    bet = min(bet, bankroll * MAX_BET_FRACTION)
    bet = max(MIN_BET_USD, bet) if kelly_frac > 0 else 0

    return round(bet, 2)


# ═══════════════════════════════════════════════════════
# STEP 6: EXECUTE THE TRADE (via Polymarket CLOB)
# ═══════════════════════════════════════════════════════

def place_trade(signal: dict, bet_usd: float, dry_run: bool = True) -> bool:
    """
    Place the actual trade on Polymarket.

    dry_run=True  → Just log it, don't actually send (for testing)
    dry_run=False → Live trading (use after you've verified the bot works)
    """
    log.info(f"""
    ╔═══ TRADE SIGNAL ══════════════════════════════╗
    ║ Market:  {signal['question'][:55]}
    ║ Side:    {signal['side']}
    ║ Price:   {signal['market_price']:.3f} ({signal['market_price']*100:.1f}¢)
    ║ Our Est: {signal['true_prob']:.3f} ({signal['true_prob']*100:.1f}%)
    ║ Edge:    {signal['edge']*100:.1f}%
    ║ BTC Now: ${signal['btc_at_scan']:,.0f} vs Target ${signal['target_price']:,.0f}
    ║ Expires: {signal['hours_left']:.1f} hours
    ║ Bet:     ${bet_usd:.2f} USDC
    ║ Mode:    {'DRY RUN 🔵' if dry_run else 'LIVE 🔴'}
    ╚═══════════════════════════════════════════════╝
    """)

    if dry_run:
        return True  # Simulate success

    # ── LIVE TRADING ────────────────────────────────
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import OrderArgs, Side
        from py_clob_client.constants import POLYGON

        client = ClobClient(
            host       = "https://clob.polymarket.com",
            chain_id   = POLYGON,
            key        = PRIVATE_KEY,
            signature_type = 2,
            funder     = PRIVATE_KEY,
        )

        # Get API credentials if not set
        if not API_KEY:
            log.warning("No API key set — create one at polymarket.com")
            return False

        client.set_api_creds(client.create_or_derive_api_creds())

        side = Side.BUY
        token_id = signal["condition_id"]

        order = client.create_market_order(
            OrderArgs(
                token_id = token_id,
                price    = signal["market_price"],
                size     = bet_usd / signal["market_price"],  # shares
                side     = side,
            )
        )
        resp = client.post_order(order)
        log.info(f"Order placed: {resp}")
        return True

    except ImportError:
        log.error("py-clob-client not installed. Run: pip install py-clob-client")
        return False
    except Exception as e:
        log.error(f"Trade failed: {e}")
        return False


# ═══════════════════════════════════════════════════════
# STEP 7: MAIN LOOP — runs 24/7
# ═══════════════════════════════════════════════════════

class State:
    def __init__(self):
        self.bankroll    = STARTING_BANKROLL
        self.bets_today  = 0
        self.total_bets  = 0
        self.wins        = 0
        self.losses      = 0
        self.peak        = STARTING_BANKROLL
        self.traded_ids  = set()   # Don't trade same market twice


def print_status(state: State, btc_price: float):
    roi = ((state.bankroll - STARTING_BANKROLL) / STARTING_BANKROLL) * 100
    log.info(f"""
    ═══ BOT STATUS ═════════════════════════════════
    💰 Bankroll:  ${state.bankroll:.2f}  (Peak: ${state.peak:.2f})
    📈 ROI:       {roi:+.1f}%
    🎯 Record:    {state.wins}W / {state.losses}L
    📊 BTC:       ${btc_price:,.0f}
    🎲 Bets made: {state.total_bets} total, {state.bets_today} today
    ════════════════════════════════════════════════
    """)


def run(dry_run: bool = True):
    """
    Main bot loop.
    Set dry_run=False when ready to trade real money.
    """
    state = State()
    last_date = datetime.now().date()

    log.info(f"""
    ╔══════════════════════════════════════════════╗
    ║   POLYMARKET BTC BOT STARTED                ║
    ║   Starting bankroll: ${STARTING_BANKROLL:.2f}              ║
    ║   Mode: {'DRY RUN (no real trades)' if dry_run else '🔴 LIVE TRADING'}         ║
    ╚══════════════════════════════════════════════╝
    """)

    while True:
        try:
            # Reset daily counter
            today = datetime.now().date()
            if today != last_date:
                state.bets_today = 0
                last_date = today

            if state.bets_today >= MAX_DAILY_BETS:
                log.info("Daily bet limit reached. Sleeping until tomorrow...")
                time.sleep(3600)
                continue

            # ── GATHER DATA ─────────────────────────────
            btc_price  = get_btc_price()
            btc_change = get_btc_24h_change()
            markets    = get_btc_markets()

            if not btc_price or not markets:
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            print_status(state, btc_price)

            # ── SCAN ALL MARKETS FOR EDGE ────────────────
            signals = []
            for market in markets:
                market_id = market.get("id")
                if market_id in state.traded_ids:
                    continue  # Already traded this one
                signal = find_edge(market, btc_price, btc_change)
                if signal:
                    signals.append(signal)

            # Sort by edge size (take best opportunity first)
            signals.sort(key=lambda s: s["edge"], reverse=True)

            if not signals:
                log.info(f"No edge found in any market. Sleeping {SCAN_INTERVAL_SEC}s...")
            else:
                log.info(f"Found {len(signals)} edge signal(s)!")

            # ── EXECUTE BEST SIGNAL ──────────────────────
            for signal in signals[:2]:   # Max 2 trades per scan
                if state.bets_today >= MAX_DAILY_BETS:
                    break
                if state.bankroll < MIN_BET_USD:
                    log.error("Bankroll too low to continue!")
                    break

                bet = kelly_size(signal["edge"], signal["market_price"], state.bankroll)
                if bet < MIN_BET_USD:
                    continue

                success = place_trade(signal, bet, dry_run=dry_run)
                if success:
                    state.traded_ids.add(signal["market_id"])
                    state.total_bets += 1
                    state.bets_today += 1

                    if dry_run:
                        # Simulate outcome based on our true probability
                        import random
                        won = random.random() < signal["true_prob"]
                        if won:
                            profit = bet * (1 / signal["market_price"] - 1)
                            state.bankroll += profit
                            state.wins += 1
                            log.info(f"  ✅ WIN +${profit:.2f} → Bankroll: ${state.bankroll:.2f}")
                        else:
                            state.bankroll -= bet
                            state.losses += 1
                            log.info(f"  ❌ LOSS -${bet:.2f} → Bankroll: ${state.bankroll:.2f}")

                        state.peak = max(state.peak, state.bankroll)

            time.sleep(SCAN_INTERVAL_SEC)

        except KeyboardInterrupt:
            log.info("Bot stopped by user.")
            break
        except Exception as e:
            log.error(f"Unexpected error: {e}")
            time.sleep(10)


# ═══════════════════════════════════════════════════════
# RUN
# ═══════════════════════════════════════════════════════
if __name__ == "__main__":
    # Set dry_run=True first to test without real money
    # Change to dry_run=False when ready to go live
    run(dry_run=True)
