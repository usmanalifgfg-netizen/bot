"""Tests for the math engine."""
import math
import pytest
from core.math_engine import MathEngine


def test_edge_detects_mispricing():
    engine = MathEngine(bankroll=1000)
    # We think 70%, market says 50% → huge edge
    signal = engine.evaluate_market("1", "Will X happen?", 0.70, 0.50)
    assert signal is not None
    assert signal.edge > 0.10
    assert signal.side == "YES"

def test_no_signal_below_min_edge():
    engine = MathEngine(bankroll=1000, min_edge=0.05)
    # We think 52%, market says 50% → 2% edge, below threshold
    signal = engine.evaluate_market("2", "Close call?", 0.52, 0.50)
    assert signal is None

def test_kelly_capped():
    engine = MathEngine(bankroll=1000, kelly_cap=0.02)
    f = engine.kelly_fraction(0.90, 0.50)
    assert f <= 0.02

def test_position_size_within_cap():
    engine = MathEngine(bankroll=1000, kelly_cap=0.02)
    size = engine.position_size(0.85, 0.50)
    assert size <= 20.0   # 2% of 1000

def test_bayesian_update_bullish():
    engine = MathEngine(bankroll=1000)
    updated = engine.bayesian_update(prior=0.5, likelihood_yes=0.8, likelihood_no=0.3)
    assert updated > 0.5  # bullish evidence raised probability

def test_bayesian_update_bearish():
    engine = MathEngine(bankroll=1000)
    updated = engine.bayesian_update(prior=0.5, likelihood_yes=0.2, likelihood_no=0.7)
    assert updated < 0.5

def test_log_return_tracking():
    engine = MathEngine(bankroll=1000)
    engine.record_trade(stake=20, pnl=20)   # win
    assert engine.bankroll == 1020
    assert len(engine.log_returns) == 1
    assert engine.cumulative_log_return() == pytest.approx(math.log(1020/1000))

def test_no_side_bet_on_zero_edge():
    engine = MathEngine(bankroll=1000, min_edge=0.0)
    f = engine.kelly_fraction(0.5, 0.5)
    assert f == 0.0   # no edge → no bet
