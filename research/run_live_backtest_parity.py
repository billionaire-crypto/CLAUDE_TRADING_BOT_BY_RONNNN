"""Deterministic batch-vs-live-lookback parity audit.

This does not contact ProjectX. It feeds the same validated historical bars
through a full-history research run and a 727-bar live-style lookback, then
compares decisions after the suffix's first partial session.
"""

import argparse
import math

import pandas as pd

from src import bot


def _canonical_timestamp(value) -> str:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = bot.TIMEZONE.localize(timestamp)
    else:
        timestamp = timestamp.tz_convert(bot.TIMEZONE)
    return timestamp.isoformat()


def _trade_key(trade) -> tuple:
    return (
        _canonical_timestamp(trade.entry_date),
        str(trade.direction),
        str(trade.entry_type),
    )


def _trade_fingerprint(trade) -> tuple:
    return (
        round(float(trade.entry), 8),
        round(float(trade.stop_price), 8),
        round(float(trade.target_price), 8),
        int(trade.fvg_age_bars),
        int(trade.fvg_quality_score),
        int(trade.base_contracts),
        int(trade.candidate_contracts),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback", type=int, default=727)
    args = parser.parse_args()

    raw = bot.fetch_data()
    if len(raw) <= args.lookback:
        raise ValueError("Lookback must be smaller than the validated dataset.")

    print("Running full-history research decisions...")
    full_frame, full_trades, _daily, _state = bot.run_strategy_pipeline(
        raw,
        enforce_evaluation_floor=False,
    )
    suffix_raw = raw.iloc[-args.lookback:].copy()
    print(f"Running live-style suffix ({len(suffix_raw)} bars)...")
    suffix_frame, suffix_trades, _daily, _state = bot.run_strategy_pipeline(
        suffix_raw,
        enforce_evaluation_floor=False,
    )

    suffix_dates = pd.Index(suffix_raw.index.tz_convert(bot.TIMEZONE).date).unique()
    if len(suffix_dates) < 2:
        raise ValueError("Parity suffix must contain at least two sessions.")
    first_complete_date = suffix_dates[1]

    def after_warm_boundary(trade):
        timestamp = pd.Timestamp(trade.entry_date).tz_convert(bot.TIMEZONE)
        return timestamp.date() >= first_complete_date

    full_map = {
        _trade_key(trade): _trade_fingerprint(trade)
        for trade in full_trades
        if after_warm_boundary(trade)
        and pd.Timestamp(trade.entry_date) >= suffix_raw.index[0]
    }
    suffix_map = {
        _trade_key(trade): _trade_fingerprint(trade)
        for trade in suffix_trades
        if after_warm_boundary(trade)
    }

    only_full = sorted(set(full_map) - set(suffix_map))
    only_suffix = sorted(set(suffix_map) - set(full_map))
    changed = sorted(
        key
        for key in set(full_map) & set(suffix_map)
        if full_map[key] != suffix_map[key]
    )

    indicator_errors = []
    for column in (
        "ema_fast",
        "ema_slow",
        "atr",
        "atr_avg_20",
        "adx",
        "vwap",
        "mtf_15m_bull",
        "mtf_15m_bear",
    ):
        full_value = full_frame.iloc[-2][column]
        suffix_value = suffix_frame.iloc[-2][column]
        if isinstance(full_value, (bool,)) or isinstance(suffix_value, (bool,)):
            equal = bool(full_value) == bool(suffix_value)
        else:
            equal = math.isclose(
                float(full_value),
                float(suffix_value),
                rel_tol=1e-12,
                abs_tol=1e-9,
            )
        if not equal:
            indicator_errors.append((column, full_value, suffix_value))

    print(f"Matched decisions: {len(set(full_map) & set(suffix_map))}")
    print(f"Only full: {len(only_full)}")
    print(f"Only suffix: {len(only_suffix)}")
    print(f"Changed geometry/candidate size: {len(changed)}")
    print(f"Indicator mismatches: {len(indicator_errors)}")
    for item in (only_full + only_suffix + changed)[:10]:
        print(f"  decision mismatch: {item}")
    for item in indicator_errors:
        print(f"  indicator mismatch: {item}")

    if only_full or only_suffix or changed or indicator_errors:
        print("RESULT: PARITY FAILURE")
        return 1
    print("RESULT: PARITY PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
