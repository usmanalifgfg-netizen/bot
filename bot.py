"""
Prediction Market Trading Bot — Single File Version
No folders, no imports — just run: python bot.py
"""
import math
import time
import json
import logging
import argparse
import requests
from dataclasses import dataclass
from typing import Optional
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

Path("data").mkdir(exist_ok=True)


# ════════════════════════════════════════════════════════
#  MATH ENGINE
# ════════════════════════════════════════════════════════

@dataclass
class TradeSignal:
    market_id: str
    question: str
    our_prob: float
    market_prob: float
    edge: float
    kelly_fraction: float
    position_size: float
    side: str


class MathEngine:
    def __init__(self, bankroll: float, min_edge: float = 0.05, kelly_cap: float = 0.02):
        self.bankroll = bankroll
        self.min_edge = min_edge
        self.kelly_cap = kelly_cap
        self.log_returns = []

    def edge(self, our_prob: float, market_prob: float) -> float:
        yes_edge = our_prob - market_prob
        no_edge = (1 - our_prob) - (1 - market_prob)
        return max(yes_edge, no_edge)

    def best_side(self, our_prob: float, market_prob: float) -> str:
        return "YES" if (our_prob - market_prob) >= ((1 - our_prob) - (1 - market_prob)) else "NO"

    def kelly_fraction(self, our_prob: float, market_prob: float) -> float:
        b = (1 / market_prob) - 1
        p = our_prob
        q = 1 - p
        if b <= 0:
            return 0.0
        f = (p * b - q) / b
        return max(0.0, min(f, self.kelly_cap))

    def position_size(self, our_prob: float, market_prob: float) -> float:
        f = self.kelly_fraction(our_prob, market_prob)
        return round(self.bankroll * f, 2)

    def bayesian_update(self, prior: float, likelihood_yes: float, likelihood_no: float) -> float:
        numerator = likelihood_yes * prior
        denominator = (likelihood_yes * prior) + (likelihood_no * (1 - prior))
        return numerator / denominator if denominator else prior

    def record_trade(self, pnl: float):
        new_bankroll = self.bankroll + pnl
        self.log_returns.append(math.log(new_bankroll / self.bankroll))
        self.bankroll = new_bankroll

    def portfolio_growth(self) -> float:
        return math.exp(sum(self.log_returns)) if self.log_returns else 1.0

    def evaluate_market(self, market_id, question, our_prob, market_prob) -> Optional[TradeSignal]:
        e = self.edge(our_prob, market_prob)
        if e < self.min_edge:
            return None
        side = self.best_side(our_prob, market_prob)
        p = our_prob if side == "YES" else (1 - our_prob)
        m = market_prob if side == "YES" else (1 - market_prob)
        return TradeSignal(
            market_id=market_id,
            question=question,
            our_prob=our_prob,
            market_prob=market_prob,
            edge=round(e, 4),
            kelly_fraction=self.kelly_fraction(p, m),
            position_size=self.position_size(p, m),
            side=side,
        )


# ════════════════════════════════════════════════════════
#  POLYMARKET CLIENT
# ════════════════════════════════════════════════════════

class PolymarketClient:
    GAMMA = "https://gamma-api.polymarket.com"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "predbot/1.0"})

    def get_active_markets(self, limit: int = 80):
        try:
            resp = self.session.get(
                f"{self.GAMMA}/markets",
                params={"active": "true", "closed": "false", "limit": limit},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else data.get("markets", [])
        except Exception as e:
            logger.error(f"API error: {e}")
            return []


# ════════════════════════════════════════════════════════
#  PROBABILITY ESTIMATOR  ← apna signal yahan dalo
# ════════════════════════════════════════════════════════

class Estimator:
    def estimate(self, question: str, market_prob: float) -> float:
        # Abhi market price hi return karta hai = no edge = no trades
        # Yahan apna model / LLM call / stats API dalo
        return market_prob


# ════════════════════════════════════════════════════════
#  MAIN BOT
# ════════════════════════════════════════════════════════

class Bot:
    def __init__(self, bankroll, min_edge, kelly_cap, mode, limit):
        self.engine    = MathEngine(bankroll, min_edge, kelly_cap)
        self.client    = PolymarketClient()
        self.estimator = Estimator()
        self.mode      = mode
        self.limit     = limit
        self.trades    = []
        self.cycle     = 0

    def scan(self):
        self.cycle += 1
        logger.info(f"Cycle {self.cycle} | scanning {self.limit} markets | bankroll=${self.engine.bankroll:.2f}")
        markets = self.client.get_active_markets(self.limit)
        signals = []
        for m in markets:
            try:
                question = m.get("question", "")
                prices   = m.get("outcomePrices", ["0.5"])
                mp       = float(prices[0]) if prices else 0.5
                mp       = max(0.01, min(0.99, mp))
                op       = max(0.01, min(0.99, self.estimator.estimate(question, mp)))
                sig      = self.engine.evaluate_market(m.get("id","?"), question, op, mp)
                if sig:
                    signals.append(sig)
                    logger.info(f"  EDGE | {question[:55]} | edge={sig.edge:.1%} | {sig.side} | ${sig.position_size}")
            except Exception as e:
                logger.warning(f"  skip market: {e}")
        logger.info(f"Cycle {self.cycle} done | {len(signals)}/{len(markets)} trades found")
        return signals

    def execute(self, signals):
        for sig in signals:
            record = {
                "ts": datetime.utcnow().isoformat(),
                "market_id": sig.market_id,
                "question": sig.question[:80],
                "side": sig.side,
                "edge": sig.edge,
                "size": sig.position_size,
                "bankroll": round(self.engine.bankroll, 2),
            }
            self.trades.append(record)
            logger.info(f"  [PAPER] {sig.side} ${sig.position_size} | {sig.question[:50]}")
        if self.trades:
            with open("data/trades.json", "w") as f:
                json.dump(self.trades, f, indent=2)

    def run(self, interval):
        logger.info(f"Bot started | mode={self.mode} | bankroll=${self.engine.bankroll}")
        while True:
            try:
                sigs = self.scan()
                if sigs:
                    self.execute(sigs)
                logger.info(f"Portfolio growth: {self.engine.portfolio_growth():.4f}x")
            except KeyboardInterrupt:
                logger.info("Stopped.")
                break
            except Exception as e:
                logger.error(f"Cycle error: {e}")
            time.sleep(interval)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--mode",      default="paper", choices=["paper","live"])
    p.add_argument("--bankroll",  default=500.0,   type=float)
    p.add_argument("--min-edge",  default=0.05,    type=float)
    p.add_argument("--kelly-cap", default=0.02,    type=float)
    p.add_argument("--interval",  default=300,     type=int)
    p.add_argument("--limit",     default=80,      type=int)
    args = p.parse_args()

    Bot(
        bankroll  = args.bankroll,
        min_edge  = args.min_edge,
        kelly_cap = args.kelly_cap,
        mode      = args.mode,
        limit     = args.limit,
    ).run(args.interval)
