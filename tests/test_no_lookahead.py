"""Behavioral causality guards for the strategy and loader."""

import os
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src import bot
from src import topstepx_runtime as runtime


def _synthetic_frame():
    """Warm history with a fresh age-1 FVG and an entry-bar stop."""
    periods = 44
    index = pd.date_range(
        "2026-06-02 09:30", periods=periods, freq="5min", tz="US/Eastern"
    )
    base = 20_000.0 + np.arange(periods) * 0.5
    open_ = base.copy()
    high = open_ + 1.0
    low = open_ - 1.0
    close = open_ + 0.5
    volume = np.full(periods, 1_000)

    j = 40
    open_[j], high[j], low[j], close[j] = 20_020, 20_021, 20_019, 20_020.5
    # This close is below the future gap floor. It must not invalidate a gap
    # that does not exist until the following bar has closed.
    open_[j + 1], high[j + 1], low[j + 1], close[j + 1] = (
        20_023, 20_027, 20_019, 20_020
    )
    open_[j + 2], high[j + 2], low[j + 2], close[j + 2] = (
        20_025, 20_029, 20_024, 20_028
    )
    # First legal entry open lies in the gap; the same bar crosses its stop.
    open_[j + 3], high[j + 3], low[j + 3], close[j + 3] = (
        20_023, 20_026, 20_018, 20_024
    )
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        },
        index=index,
    )


def test_current_bar_futures_cannot_change_entry_at_its_open():
    from research.run_lookahead_audit import (
        _blind_variants,
        _entries_at,
        _run_pipeline,
    )

    frame = _synthetic_frame()
    timestamp = frame.index[-1]
    expected = _entries_at(_run_pipeline(frame), timestamp)
    assert expected, "fixture must open an entry at the final bar"
    for variant in _blind_variants(frame):
        assert _entries_at(_run_pipeline(variant), timestamp) == expected


def test_broker_bar_adapter_and_historical_pipeline_emit_identical_signal():
    frame = _synthetic_frame()
    blinded = frame.copy()
    opening = float(blinded.iloc[-1]["open"])
    blinded.iloc[-1, blinded.columns.get_indexer(["high", "low", "close"])] = opening
    blinded.iloc[-1, blinded.columns.get_loc("volume")] = 0.0

    historical_signal = {}
    bot.run_strategy_pipeline(blinded, live_signal_sink=historical_signal)

    broker_bars = [
        {
            "t": timestamp.tz_convert("UTC").isoformat(),
            "o": row.open,
            "h": row.high,
            "l": row.low,
            "c": row.close,
            "v": row.volume,
        }
        for timestamp, row in frame.iterrows()
    ]
    live_frame = runtime._bars_to_strategy_df(
        broker_bars,
        blind_last_bar_to_open=True,
    )
    live_signal = {}
    bot.run_strategy_pipeline(live_frame, live_signal_sink=live_signal)

    assert historical_signal
    assert live_signal == historical_signal


def test_data_fingerprint_is_timezone_canonical_and_value_sensitive():
    frame = _synthetic_frame()
    eastern = bot.validate_loaded_data(frame)
    chicago_frame = frame.copy()
    chicago_frame.index = chicago_frame.index.tz_convert(bot.TIMEZONE)
    chicago = bot.validate_loaded_data(chicago_frame)
    changed_frame = frame.copy()
    changed_frame.iloc[0, changed_frame.columns.get_loc("close")] += 0.25
    changed = bot.validate_loaded_data(changed_frame)

    assert eastern["sha256"] == chicago["sha256"]
    assert changed["sha256"] != eastern["sha256"]


def test_blinding_detector_goes_red_for_the_original_open_equals_low_leak():
    from research.run_lookahead_audit import _blind_variants, _entries_at

    frame = _synthetic_frame()
    timestamp = frame.index[-1]

    def known_bad_result(candidate):
        # This is the exact forbidden predicate behind the original age-zero
        # bullish-FVG bug: it consumes the decision bar's eventual low.
        if float(candidate.iloc[-1]["open"]) != float(candidate.iloc[-1]["low"]):
            return [], {}
        trade = SimpleNamespace(
            entry_date=timestamp,
            direction="long",
            entry_type="FVG",
            entry=float(candidate.iloc[-1]["open"]),
        )
        return [trade], {}

    original = _entries_at(known_bad_result(frame), timestamp)
    alternatives = [
        _entries_at(known_bad_result(variant), timestamp)
        for variant in _blind_variants(frame)
    ]
    assert any(candidate != original for candidate in alternatives)


def test_entry_bar_stop_is_applied_and_fresh_gap_survives():
    from research.run_lookahead_audit import _run_pipeline

    frame = _synthetic_frame()
    trades, signal = _run_pipeline(frame)
    assert signal == {}
    assert len(trades) == 1
    trade = trades[0]
    assert trade.entry_date == trade.date == frame.index[-1]
    assert trade.fvg_age_bars == 1
    assert trade.exit_reason == "stop"
    assert trade.bars_to_exit == 0


def test_partial_and_final_exit_reconcile_to_cash_and_initial_size(monkeypatch):
    monkeypatch.setattr(bot, "PARTIAL_PROFIT_ENABLED", True)
    frame = _synthetic_frame()
    final = frame.index[-1]
    frame.loc[final, "high"] = frame.loc[final, "open"] + 30.0
    frame.loc[final, "low"] = frame.loc[final, "open"] - 2.0
    frame.loc[final, "close"] = frame.loc[final, "open"] + 20.0

    indicators = bot.add_indicators(frame.copy())
    signals = bot.generate_signals(indicators)
    levels = bot.compute_session_levels(signals)
    result, trades, _daily, _state = bot.run_backtest(signals, levels)

    assert len(trades) == 1
    trade = trades[0]
    assert trade.contracts > 1
    assert trade.gross_pnl_usd - trade.costs_usd == pytest.approx(trade.pnl_usd)
    assert result["portfolio_value"].iloc[-1] == pytest.approx(
        bot.INIT_CASH + trade.pnl_usd
    )


def test_fvg_window_must_not_cross_sessions():
    prior_close = pd.Timestamp("2026-06-01 16:00", tz="US/Eastern")
    next_open = pd.Timestamp("2026-06-02 09:30", tz="US/Eastern")
    assert not bot._same_session_window(prior_close, next_open, next_open)
    assert bot._same_session_window(
        next_open,
        next_open + pd.Timedelta(minutes=5),
        next_open + pd.Timedelta(minutes=10),
    )


def test_live_decision_window_matches_backtest_session_schedule():
    tz = bot.TIMEZONE
    normal = tz.localize(pd.Timestamp("2026-07-23 08:30").to_pydatetime())
    normal_last_bar = tz.localize(pd.Timestamp("2026-07-23 15:00").to_pydatetime())
    early_last_bar = tz.localize(pd.Timestamp("2026-11-27 12:10").to_pydatetime())
    weekend = tz.localize(pd.Timestamp("2026-07-25 10:00").to_pydatetime())

    assert bot.is_strategy_decision_time_ct(normal)
    assert not bot.is_strategy_decision_time_ct(normal_last_bar)
    assert not bot.is_strategy_decision_time_ct(early_last_bar)
    assert not bot.is_strategy_decision_time_ct(weekend)


def test_is_fvg_valid_is_pure_and_age_bounded():
    gap = bot.FVG(
        direction="bullish",
        top=105.0,
        bottom=100.0,
        created_bar=10,
        session_date="2026-06-01",
    )
    a = bot.is_fvg_valid(gap, 11, "2026-06-01", 106.0, 101.0, 103.0)
    b = bot.is_fvg_valid(gap, 11, "2026-06-01", 106.0, 101.0, 103.0)
    assert a == b is True
    assert bot.is_fvg_valid(
        gap, 11, "2026-06-01", 106.0, 99.0, 99.0
    ) is False
    assert bot.is_fvg_valid(
        gap,
        10 + bot.FVG_MAX_AGE_BARS + 1,
        "2026-06-01",
        106.0,
        101.0,
        103.0,
    ) is False


def test_exported_fvg_trades_have_no_age_zero_if_artifact_exists():
    path = os.path.join(bot.EXPORT_DIR, "v29_trades_full.csv")
    if not os.path.exists(path):
        pytest.skip("no exported trades to check")
    exported = pd.read_csv(path, encoding="utf-8")
    if "fvg_age_bars" not in exported.columns:
        pytest.fail("trade export is missing fvg_age_bars")
    fvg = exported[exported["entry_type"] == "FVG"]
    assert not fvg.empty
    assert int((fvg["fvg_age_bars"] == 0).sum()) == 0
