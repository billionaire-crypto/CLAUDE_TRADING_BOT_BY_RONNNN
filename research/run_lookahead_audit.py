"""
LOOK-AHEAD DETECTOR for the V29 backtest engine.

WHY THIS EXISTS
On 2026-07-25 an external audit found the backtest was consuming future
information. Correcting it turned a claimed +$429,763.60 / PF 4.06 into
-$23,262.94 / PF 0.894. Every walk-forward, holdout and stress test in the ledger
had run through that same engine, so none of them meant what they appeared to.
Months of code review by eye never caught it. This catches it mechanically.

TWO TESTS, BECAUSE ONE IS NOT ENOUGH
The first version of this file used only prefix-invariance and reported "CAUSAL"
on the known-broken engine. That near-miss is why both tests exist and why the
blind spot is documented rather than quietly patched.

  TEST 1 — INTRA-BAR BLINDING  (catches the leak that actually happened)
    Entry is modelled at bar i's OPEN. A causal engine therefore cannot let bar
    i's own high/low/close influence whether that entry happens. We run the
    pipeline twice on data ending at bar i: once normally, once with bar i's
    high/low/close overwritten by its open ("blinded"). If the entry decision at
    bar i changes, the engine read the bar's future within the bar itself.
    This is exactly the V29 leak: a bullish FVG created at bar i has
    top = row.low, so entering at row.open requires open == low — knowable only
    after the bar completes.

  TEST 2 — PREFIX INVARIANCE  (catches leaks that reach ACROSS bars)
    Running on bars[0:N] and bars[0:N+H] must yield identical trades for every
    trade opened before N. Appending future bars cannot change the past.
    BLIND SPOT, STATED PLAINLY: this test CANNOT see intra-bar leaks, because
    bar i is fully present in both runs. It is necessary, not sufficient.
    It also cannot see leaks in fetch_data's cleaning step (e.g.
    filter_phantom_bars, which deletes bar k using bar k+1) when that step has
    already run before slicing — see NOTE in _load_raw.

USAGE
    python -X utf8 -m research.run_lookahead_audit
    python -X utf8 -m research.run_lookahead_audit --bars 40000 --cuts 12

Exit 0 = both tests pass. Exit 1 = LEAK DETECTED.
"""
import argparse
import sys

import numpy as np
import pandas as pd

import src.bot as bot


def _load_raw(bars=None, start_bar=0):
    """Load a deterministic audit window from the validated historical series."""
    df = bot.fetch_data()
    start = max(0, int(start_bar))
    return df.iloc[start:] if bars is None else df.iloc[start:start + bars]


def _run_pipeline(df_raw):
    signal = {}
    _p, trades, _d, _s = bot.run_strategy_pipeline(
        df_raw,
        live_signal_sink=signal,
        enforce_evaluation_floor=False,
    )
    return trades, signal


def _blind_last_bar(df):
    """Overwrite the final bar's high/low/close with its open.

    Models 'we are standing at this bar's open and the rest of it has not
    happened yet'. Keeps OHLC internally valid (a zero-range bar).
    """
    df2 = df.copy()
    last = len(df2) - 1
    o = float(df2.iloc[last]["open"])
    for col in ("high", "low", "close"):
        df2.iloc[last, df2.columns.get_loc(col)] = o
    return df2


def _entries_at(result, ts):
    """Entries opened exactly at ts, as comparable tuples."""
    trades, signal = result
    out = []
    for t in trades:
        if t.entry_date is not None and pd.Timestamp(t.entry_date) == ts:
            out.append((str(t.direction), str(t.entry_type), round(float(t.entry), 4)))
    if signal and pd.Timestamp(signal.get("entry_timestamp")) == ts:
        out.append((
            str(signal["direction"]),
            str(signal["entry_type"]),
            round(float(signal["entry_price"]), 4),
        ))
    return sorted(out)


def _fingerprint(result, cutoff_ts):
    trades, _signal = result
    out = []
    for t in trades:
        if pd.Timestamp(t.date) >= cutoff_ts:
            continue
        out.append((str(pd.Timestamp(t.date)), str(t.direction), str(t.entry_type),
                    round(float(t.entry), 4), round(float(t.exit), 4),
                    int(t.contracts), round(float(t.pnl_usd), 4), str(t.exit_reason)))
    return sorted(out)


def _blind_variants(df):
    """Return multiple valid futures for the final bar while preserving its open."""
    zero = _blind_last_bar(df)
    variants = [zero]
    opening = float(df.iloc[-1]["open"])
    span = max(10.0, abs(opening) * 0.001)
    for close in (opening + span * 0.75, opening - span * 0.75):
        variant = df.copy()
        last = len(variant) - 1
        values = {
            "high": opening + span,
            "low": opening - span,
            "close": close,
        }
        for column, value in values.items():
            variant.iloc[last, variant.columns.get_loc(column)] = value
        variants.append(variant)
    return variants


def _select_intrabar_cuts(df_all, discovered_result, count, all_entries=False):
    """Exercise known entry bars plus broad no-entry coverage.

    Uniform cuts alone mostly sample bars with no decision and can report a
    meaningless pass on a sparse strategy.
    """
    count = max(1, int(count))
    trades, signal = discovered_result
    entry_timestamps = {
        pd.Timestamp(trade.entry_date)
        for trade in trades
        if trade.entry_date is not None
    }
    if signal and signal.get("entry_timestamp") is not None:
        entry_timestamps.add(pd.Timestamp(signal["entry_timestamp"]))
    locations = {
        int(df_all.index.get_loc(timestamp)) + 1
        for timestamp in entry_timestamps
        if timestamp in df_all.index
    }
    entry_cuts = sorted(locations)
    if all_entries:
        lo, hi = max(3, int(len(df_all) * 0.30)), len(df_all) - 5
        spread = np.linspace(lo, hi, max(1, count), dtype=int).tolist()
        return sorted(set(entry_cuts + spread))
    entry_budget = min(len(entry_cuts), max(1, (count * 3) // 4))
    if len(entry_cuts) > entry_budget:
        selection = np.linspace(0, len(entry_cuts) - 1, entry_budget, dtype=int)
        entry_cuts = [entry_cuts[position] for position in selection]

    spread_budget = max(0, count - len(entry_cuts))
    spread_cuts = []
    if spread_budget:
        lo, hi = max(3, int(len(df_all) * 0.30)), len(df_all) - 5
        spread_cuts = np.linspace(lo, hi, spread_budget, dtype=int).tolist()
    return sorted(set(entry_cuts + spread_cuts))


def test_intrabar(df_all, cuts):
    print("\n" + "=" * 78)
    print("  TEST 1 — INTRA-BAR BLINDING")
    print("  Can bar i's own high/low/close change the entry priced at bar i's open?")
    print("=" * 78)

    leaks = checked = 0
    for n, cut in enumerate(cuts, 1):
        sl = df_all.iloc[:cut]
        ts = sl.index[-1]
        seen = _run_pipeline(sl)
        a = _entries_at(seen, ts)
        blinded_entries = [
            _entries_at(_run_pipeline(variant), ts)
            for variant in _blind_variants(sl)
        ]
        checked += 1
        if any(a != candidate for candidate in blinded_entries):
            leaks += 1
            print(f"\n  *** LEAK *** cut {n} @ bar {cut:,} ({ts})")
            print(f"      with the full bar visible : {a}")
            print(f"      blinded alternatives      : {blinded_entries}")
            print("      The entry decision depended on data from inside its own bar.")
        elif a:
            print(f"  cut {n:>3} @ {ts}  entry {a[0][0]:<5} -> unchanged when blinded  OK")

    print(f"\n  {checked - leaks}/{checked} decision bars causal.")
    return leaks


def test_prefix(df_all, cuts, horizon):
    print("\n" + "=" * 78)
    print("  TEST 2 — PREFIX INVARIANCE")
    print("  Can appending future bars change a trade that already happened?")
    print("=" * 78)

    leaks = 0
    for n, cut in enumerate(cuts, 1):
        if cut + horizon >= len(df_all):
            continue
        safe_ts = df_all.index[max(0, cut - 200)]
        a = _fingerprint(_run_pipeline(df_all.iloc[:cut]), safe_ts)
        b = _fingerprint(_run_pipeline(df_all.iloc[:cut + horizon]), safe_ts)
        only_a, only_b = sorted(set(a) - set(b)), sorted(set(b) - set(a))
        if only_a or only_b:
            leaks += 1
            print(f"\n  *** LEAK *** cut {n} @ bar {cut:,}")
            print(f"      only in A: {len(only_a)}   only in B: {len(only_b)}")
            for r in (only_b or only_a)[:3]:
                print(f"        {r}")
        else:
            print(f"  cut {n:>3} @ bar {cut:,}  {len(a)} past trades identical  OK")
    return leaks


def _configure_stdout():
    """Configure the CLI without replacing an importer's capture stream."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main():
    _configure_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=int, default=25_000)
    ap.add_argument(
        "--start-bar",
        type=int,
        default=0,
        help="Start offset for an independent audit window.",
    )
    ap.add_argument("--cuts", type=int, default=40)
    ap.add_argument("--horizon", type=int, default=800)
    ap.add_argument("--prefix-cuts", type=int, default=3)
    ap.add_argument(
        "--all-entry-bars",
        action="store_true",
        help="Blind every observed entry bar instead of sampling them.",
    )
    args = ap.parse_args()

    print("Loading raw bars...")
    df_all = _load_raw(args.bars, args.start_bar)
    print(f"  {len(df_all):,} bars  {df_all.index[0]} -> {df_all.index[-1]}")

    print("Discovering actual entry bars for adversarial cut selection...")
    discovered_result = _run_pipeline(df_all)
    ib_cuts = _select_intrabar_cuts(
        df_all,
        discovered_result,
        args.cuts,
        all_entries=args.all_entry_bars,
    )
    discovered_entries = sum(
        bool(_entries_at(discovered_result, df_all.index[cut - 1]))
        for cut in ib_cuts
    )
    print(
        f"  selected {len(ib_cuts)} cuts, including "
        f"{discovered_entries} known entry bar(s)"
    )

    pf_lo, pf_hi = int(len(df_all) * 0.35), len(df_all) - args.horizon - 10
    pf_step = max(1, (pf_hi - pf_lo) // max(1, args.prefix_cuts))
    pf_cuts = [pf_lo + k * pf_step for k in range(args.prefix_cuts) if pf_lo + k * pf_step < pf_hi]

    leaks = test_intrabar(df_all, ib_cuts)
    leaks += test_prefix(df_all, pf_cuts, args.horizon)

    print("\n" + "=" * 78)
    if leaks:
        print(f"  RESULT: LOOK-AHEAD DETECTED ({leaks} failing check(s)).")
        print("  The backtest is NOT causal. Do not trust its numbers.")
        print("=" * 78)
        return 1
    print("  RESULT: CAUSAL on both tests.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
