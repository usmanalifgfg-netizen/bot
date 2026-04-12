"""
Main bot loop — scans markets, filters by edge, executes trades.
Run:  python bot.py --mode paper --bankroll 500
"""
import argparse
import logging
import time
import json
from datetime import datetime
from pathlib import Path

from core.math_engine import MathEngine, TradeSignal
from core.polymarket_client import PolymarketClient
from strategies.estimator import ProbabilityEstimator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("data/bot.log"),
    ],
)
logger = logging.getLogger(__name__)

Path("data").mkdir(exist_ok=True)


class TradingBot:
    def __init__(self, bankroll: float, min_edge: float = 0.05,
                 kelly_cap: float = 0.02, mode: str = "paper",
                 scan_limit: int = 80):
        self.engine     = MathEngine(bankroll, min_edge, kelly_cap)
        self.client     = PolymarketClient()
        self.estimator  = ProbabilityEstimator()
        self.mode       = mode           # "paper" or "live"
        self.scan_limit = scan_limit
        self.trades_log: list = []
        self.cycle      = 0

    # ── Single scan cycle ────────────────────────────────────────────────────
    def scan(self) -> list[TradeSignal]:
        self.cycle += 1
        logger.info(f"Cycle {self.cycle} — scanning {self.scan_limit} markets")

        markets = self.client.get_active_markets(limit=self.scan_limit)
        signals = []

        for m in markets:
            try:
                question    = m.get("question", "")
                market_prob = float(m.get("outcomePrices", [0.5])[0])

                # Clamp to valid range
                market_prob = max(0.01, min(0.99, market_prob))

                # Get our probability estimate
                our_prob = self.estimator.estimate(question, market_prob)
                our_prob = max(0.01, min(0.99, our_prob))

                signal = self.engine.evaluate_market(
                    market_id=m.get("id", "unknown"),
                    question=question,
                    our_prob=our_prob,
                    market_prob=market_prob,
                )
                if signal:
                    signals.append(signal)
                    logger.info(
                        f"  ✅ EDGE FOUND | {question[:60]} | "
                        f"edge={signal.edge:.1%} | side={signal.side} | "
                        f"size=${signal.position_size:.2f}"
                    )
            except Exception as e:
                logger.warning(f"  ⚠ Skipping market {m.get('id')}: {e}")

        logger.info(f"Cycle {self.cycle} complete — {len(signals)}/{len(markets)} trades found")
        return signals

    # ── Execute signals ──────────────────────────────────────────────────────
    def execute(self, signals: list[TradeSignal]):
        for sig in signals:
            if self.mode == "paper":
                self._paper_trade(sig)
            else:
                self._live_trade(sig)

    def _paper_trade(self, sig: TradeSignal):
        record = {
            "ts":        datetime.utcnow().isoformat(),
            "market_id": sig.market_id,
            "question":  sig.question[:80],
            "side":      sig.side,
            "edge":      sig.edge,
            "size":      sig.position_size,
            "bankroll":  round(self.engine.bankroll, 2),
        }
        self.trades_log.append(record)
        logger.info(f"  [PAPER] {sig.side} ${sig.position_size:.2f} — {sig.question[:50]}")
        self._save_log()

    def _live_trade(self, sig: TradeSignal):
        # Wire up py-clob-client here
        raise NotImplementedError("Set mode=paper until you configure live keys.")

    def _save_log(self):
        with open("data/trades.json", "w") as f:
            json.dump(self.trades_log, f, indent=2)

    # ── Main loop ────────────────────────────────────────────────────────────
    def run(self, interval_seconds: int = 300):
        logger.info(
            f"Bot starting | mode={self.mode} | bankroll=${self.engine.bankroll:.2f} | "
            f"min_edge={self.engine.min_edge:.1%} | kelly_cap={self.engine.kelly_cap:.1%}"
        )
        while True:
            try:
                signals = self.scan()
                if signals:
                    self.execute(signals)
                logger.info(
                    f"Portfolio | bankroll=${self.engine.bankroll:.2f} | "
                    f"growth={self.engine.portfolio_growth():.4f}x | "
                    f"log_return={self.engine.cumulative_log_return():.4f}"
                )
            except KeyboardInterrupt:
                logger.info("Shutting down.")
                break
            except Exception as e:
                logger.error(f"Cycle error: {e}")
            time.sleep(interval_seconds)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode",     default="paper",  choices=["paper", "live"])
    parser.add_argument("--bankroll", default=500.0,    type=float)
    parser.add_argument("--min-edge", default=0.05,     type=float)
    parser.add_argument("--kelly-cap",default=0.02,     type=float)
    parser.add_argument("--interval", default=300,      type=int, help="Seconds between scans")
    parser.add_argument("--limit",    default=80,       type=int, help="Markets per scan")
    args = parser.parse_args()

    bot = TradingBot(
        bankroll=args.bankroll,
        min_edge=args.min_edge,
        kelly_cap=args.kelly_cap,
        mode=args.mode,
        scan_limit=args.limit,
    )
    bot.run(interval_seconds=args.interval)
