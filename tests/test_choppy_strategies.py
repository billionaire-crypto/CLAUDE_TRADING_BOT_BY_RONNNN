from types import SimpleNamespace
from datetime import datetime, date

import pandas as pd
from src import strategies


def _ctx(hh=11, mm=0):
    return SimpleNamespace(
        ts_ct=datetime(2024, 1, 2, hh, mm),
        session_date=date(2024, 1, 2),
        tick=strategies.TICK,
    )


# ── RangeEdgeFade ────────────────────────────────────────────────────────────
def test_range_edge_fade_short_on_failed_high_break():
    s = strategies.RangeEdgeFade()
    s.prior_high, s.prior_low = 100.0, 90.0
    prev = {"high": 101.0, "low": 99.0, "close": 99.5, "vwap": 95.0}
    row = {"open": 100.0}
    sig = s.entry(1, row, prev, prev, _ctx())
    assert sig is not None
    assert sig.direction == "short"
    assert sig.target_price == 95.0
    assert sig.exit_on_trend_flip is False


def test_range_edge_fade_none_without_poke():
    s = strategies.RangeEdgeFade()
    s.prior_high, s.prior_low = 100.0, 90.0
    prev = {"high": 99.0, "low": 95.0, "close": 98.0, "vwap": 96.0}
    row = {"open": 97.0}
    assert s.entry(1, row, prev, prev, _ctx()) is None


def test_range_edge_fade_none_without_prior_day():
    s = strategies.RangeEdgeFade()
    prev = {"high": 101.0, "low": 99.0, "close": 99.5, "vwap": 95.0}
    row = {"open": 100.0}
    assert s.entry(1, row, prev, prev, _ctx()) is None


# ── SqueezeBreakout ──────────────────────────────────────────────────────────
def test_squeeze_breakout_long_on_close_above_band():
    s = strategies.SqueezeBreakout()
    s._since_squeeze = 0  # just came out of a squeeze
    prev = {"bb_upper": 100.0, "bb_lower": 95.0, "close": 101.0}
    row = {"open": 100.5}
    sig = s.entry(1, row, prev, prev, _ctx())
    assert sig is not None
    assert sig.direction == "long"
    assert sig.target_price is None  # uses shared 2R target
    assert sig.exit_on_trend_flip is False


def test_squeeze_breakout_none_when_not_recently_squeezed():
    s = strategies.SqueezeBreakout()
    s._since_squeeze = 50  # no recent squeeze
    prev = {"bb_upper": 100.0, "bb_lower": 95.0, "close": 101.0}
    row = {"open": 100.5}
    assert s.entry(1, row, prev, prev, _ctx()) is None


# ── OpeningRangeFade ─────────────────────────────────────────────────────────
def test_opening_range_fade_short_on_failed_high_break():
    s = strategies.OpeningRangeFade()
    s.reset_session(date(2024, 1, 2))
    s.or_high, s.or_low, s.or_mid, s.formed = 100.0, 90.0, 95.0, True
    prev = {"high": 101.0, "low": 99.0, "close": 99.5}
    row = {"open": 100.0}
    sig = s.entry(1, row, prev, prev, _ctx())
    assert sig is not None
    assert sig.direction == "short"
    assert sig.target_price == 95.0
    s.confirm(sig)
    assert s.faded_high is True
    # one fade per side: the same setup no longer fires
    assert s.entry(1, row, prev, prev, _ctx()) is None


def test_opening_range_fade_none_before_formed():
    s = strategies.OpeningRangeFade()
    s.reset_session(date(2024, 1, 2))
    prev = {"high": 101.0, "low": 99.0, "close": 99.5}
    row = {"open": 100.0}
    assert s.entry(1, row, prev, prev, _ctx()) is None


# ── LunchLullReversion ───────────────────────────────────────────────────────
def test_lunch_lull_long_inside_window():
    s = strategies.LunchLullReversion()
    prev = {"vwap": 100.0, "close": 90.0, "atr": 5.0, "rsi2": 10.0,
            "low": 89.0, "high": 91.0}
    row = {"open": 95.0}
    sig = s.entry(1, row, prev, prev, _ctx(hh=11, mm=0))  # 11:00 CT = inside lull
    assert sig is not None
    assert sig.direction == "long"
    assert sig.target_price == 100.0
    assert sig.exit_on_trend_flip is False


def test_lunch_lull_none_outside_window():
    s = strategies.LunchLullReversion()
    prev = {"vwap": 100.0, "close": 90.0, "atr": 5.0, "rsi2": 10.0,
            "low": 89.0, "high": 91.0}
    row = {"open": 95.0}
    assert s.entry(1, row, prev, prev, _ctx(hh=9, mm=0)) is None  # 09:00 CT = outside


# ── Registration ─────────────────────────────────────────────────────────────
def test_new_candidates_registered():
    names = {c.name for c in strategies.build_candidates()}
    for expected in ("Range-edge-fade", "Squeeze-breakout", "OR-fade", "Lunch-reversion"):
        assert expected in names
        assert strategies.build_strategy(expected).name == expected
