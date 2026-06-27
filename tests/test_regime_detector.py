"""Unit tests for the clock + ADX regime detector (no market data required).

Covers:
  - classify_regime_adx()  : ADX -> trending / choppy / neutral / unknown
  - Choppiness Index gating : optional confirmation for "choppy"
  - session_phase()         : clock -> am_trend / lunch_chop / pm_trend / other
  - is_mean_reversion_window: clock(lunch) AND ADX(chop) must BOTH agree
  - detect_regime()         : ADX path vs legacy ATR fallback
  - add_indicators()        : chop_index column is produced and well-behaved

Run with:  python tests/test_regime_detector.py   (or: pytest tests/)
"""
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src import bot


# ── classify_regime_adx ────────────────────────────────────────────────────────
def test_adx_trending():
    assert bot.classify_regime_adx({"adx": 30.0}) == "trending"

def test_adx_choppy():
    assert bot.classify_regime_adx({"adx": 12.0}) == "choppy"

def test_adx_neutral_zone():
    # Between ADX_CHOP_MAX (20) and ADX_TREND_MIN (25) -> stand aside
    assert bot.classify_regime_adx({"adx": 22.0}) == "neutral"

def test_adx_unknown_when_nan():
    assert bot.classify_regime_adx({"adx": np.nan}) == "unknown"


# ── Choppiness Index confirmation gate ─────────────────────────────────────────
def test_chop_index_gate_blocks_when_squiggle_low():
    bot.CHOP_INDEX_ENABLED = True
    try:
        # Low ADX would say "choppy", but a low Choppiness Index (straight path)
        # downgrades it to neutral when confirmation is required.
        assert bot.classify_regime_adx({"adx": 12.0, "chop_index": 40.0}) == "neutral"
        assert bot.classify_regime_adx({"adx": 12.0, "chop_index": 70.0}) == "choppy"
    finally:
        bot.CHOP_INDEX_ENABLED = False


# ── session_phase (clock) ──────────────────────────────────────────────────────
def test_session_phase_am_trend():
    assert bot.session_phase(datetime(2026, 6, 27, 9, 0)) == "am_trend"

def test_session_phase_lunch_chop():
    assert bot.session_phase(datetime(2026, 6, 27, 12, 0)) == "lunch_chop"

def test_session_phase_pm_trend():
    assert bot.session_phase(datetime(2026, 6, 27, 14, 30)) == "pm_trend"

def test_session_phase_other():
    assert bot.session_phase(datetime(2026, 6, 27, 16, 0)) == "other"


# ── is_mean_reversion_window (clock AND adx) ───────────────────────────────────
def test_mr_window_green_light():
    # Lunch on the clock AND ADX confirms chop -> True
    assert bot.is_mean_reversion_window({"adx": 12.0}, datetime(2026, 6, 27, 12, 0)) is True

def test_mr_window_blocked_by_trend_at_lunch():
    # Clock says lunch, but the owner skipped the bench (trending) -> False
    assert bot.is_mean_reversion_window({"adx": 30.0}, datetime(2026, 6, 27, 12, 0)) is False

def test_mr_window_blocked_outside_lunch():
    # Choppy but it's the morning rush, not the bench -> False
    assert bot.is_mean_reversion_window({"adx": 12.0}, datetime(2026, 6, 27, 9, 0)) is False


# ── detect_regime: ADX path vs legacy ATR fallback ─────────────────────────────
def test_detect_regime_uses_adx_by_default():
    assert bot.REGIME_USE_ADX is True
    assert bot.detect_regime({"adx": 12.0}) == "choppy"
    assert bot.detect_regime({"adx": 30.0}) == "trending"

def test_detect_regime_legacy_atr_fallback():
    bot.REGIME_USE_ADX = False
    try:
        # Legacy rule: ATR below 0.8 x ATR20 -> choppy; above -> trending
        assert bot.detect_regime({"atr": 1.0, "atr_avg_20": 10.0}) == "choppy"
        assert bot.detect_regime({"atr": 10.0, "atr_avg_20": 1.0}) == "trending"
    finally:
        bot.REGIME_USE_ADX = True


# ── compute_choppiness_index ───────────────────────────────────────────────────
def _true_range(high, low, close):
    return pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)

def test_chop_index_bounded():
    rng = np.random.default_rng(42)
    close = pd.Series(20000 + np.cumsum(rng.normal(0, 5, 200)))
    high = close + rng.uniform(1, 8, 200)
    low = close - rng.uniform(1, 8, 200)
    ci = bot.compute_choppiness_index(high, low, _true_range(high, low, close), 14)
    vals = ci.dropna()
    assert len(vals) > 0
    # Choppiness Index is bounded in [0, 100] by construction.
    assert vals.min() >= -1e-6
    assert vals.max() <= 100 + 1e-6

def test_chop_index_ranges_higher_than_trend():
    # A pure straight-line trend should score LOW; a tight oscillation HIGH.
    n = 60
    trend_close = pd.Series(np.linspace(20000, 20300, n))          # straight up
    osc_close = pd.Series(20000 + 10 * np.sin(np.linspace(0, 12 * np.pi, n)))  # chop
    def ci(c):
        h, l = c + 1.0, c - 1.0
        return bot.compute_choppiness_index(h, l, _true_range(h, l, c), 14).dropna().mean()
    assert ci(osc_close) > ci(trend_close)


# ── tiny runner so it works without pytest installed ───────────────────────────
if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
