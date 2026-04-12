"""
Core mathematical models for prediction market trading.
"""
import math
from dataclasses import dataclass
from typing import Optional


@dataclass
class TradeSignal:
    market_id: str
    question: str
    our_prob: float
    market_prob: float
    edge: float
    kelly_fraction: float
    position_size: float
    side: str  # "YES" or "NO"


class MathEngine:
    def __init__(self, bankroll: float, min_edge: float = 0.05, kelly_cap: float = 0.02):
        self.bankroll = bankroll
        self.min_edge = min_edge          # Minimum edge to trade (5%)
        self.kelly_cap = kelly_cap        # Max 2% of bankroll per trade
        self.log_returns = []

    # ── Expected Value ──────────────────────────────────────────────────────
    def expected_value(self, our_prob: float, market_prob: float, payout: float = 1.0) -> float:
        """EV = p_win * payout - p_lose * stake"""
        p_win = our_prob
        p_lose = 1 - our_prob
        odds = (1 / market_prob) - 1      # decimal odds from market price
        return (p_win * odds * payout) - (p_lose * 1.0)

    # ── Edge ────────────────────────────────────────────────────────────────
    def edge(self, our_prob: float, market_prob: float) -> float:
        """Edge = our_prob - market_prob (or vs NO side)"""
        yes_edge = our_prob - market_prob
        no_edge = (1 - our_prob) - (1 - market_prob)
        return max(yes_edge, no_edge)

    def best_side(self, our_prob: float, market_prob: float) -> str:
        return "YES" if (our_prob - market_prob) >= ((1 - our_prob) - (1 - market_prob)) else "NO"

    # ── Kelly Criterion ──────────────────────────────────────────────────────
    def kelly_fraction(self, our_prob: float, market_prob: float) -> float:
        """
        f* = (p * b - q) / b
        b = decimal odds, p = our win prob, q = 1 - p
        Capped at kelly_cap for safety.
        """
        b = (1 / market_prob) - 1
        p = our_prob
        q = 1 - p
        if b <= 0:
            return 0.0
        f = (p * b - q) / b
        return max(0.0, min(f, self.kelly_cap))   # cap at max risk %

    def position_size(self, our_prob: float, market_prob: float) -> float:
        f = self.kelly_fraction(our_prob, market_prob)
        return round(self.bankroll * f, 2)

    # ── Bayesian Update ──────────────────────────────────────────────────────
    def bayesian_update(self, prior: float, likelihood_yes: float, likelihood_no: float) -> float:
        """
        P(H|E) = P(E|H) * P(H) / P(E)
        prior:           P(YES) before new evidence
        likelihood_yes:  P(evidence | event happens)
        likelihood_no:   P(evidence | event doesn't happen)
        """
        numerator = likelihood_yes * prior
        denominator = (likelihood_yes * prior) + (likelihood_no * (1 - prior))
        if denominator == 0:
            return prior
        return numerator / denominator

    # ── Log Returns ──────────────────────────────────────────────────────────
    def record_trade(self, stake: float, pnl: float):
        new_bankroll = self.bankroll + pnl
        log_return = math.log(new_bankroll / self.bankroll)
        self.log_returns.append(log_return)
        self.bankroll = new_bankroll

    def cumulative_log_return(self) -> float:
        return sum(self.log_returns)

    def portfolio_growth(self) -> float:
        return math.exp(self.cumulative_log_return())

    # ── Signal Builder ───────────────────────────────────────────────────────
    def evaluate_market(self, market_id: str, question: str,
                        our_prob: float, market_prob: float) -> Optional[TradeSignal]:
        e = self.edge(our_prob, market_prob)
        if e < self.min_edge:
            return None                   # below threshold — skip

        side = self.best_side(our_prob, market_prob)
        prob_for_kelly = our_prob if side == "YES" else (1 - our_prob)
        mkt_for_kelly  = market_prob if side == "YES" else (1 - market_prob)

        return TradeSignal(
            market_id=market_id,
            question=question,
            our_prob=our_prob,
            market_prob=market_prob,
            edge=round(e, 4),
            kelly_fraction=self.kelly_fraction(prob_for_kelly, mkt_for_kelly),
            position_size=self.position_size(prob_for_kelly, mkt_for_kelly),
            side=side,
        )
