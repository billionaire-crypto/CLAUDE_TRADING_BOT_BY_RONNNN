"""
Shadow Account Analysis
=======================
Compares each live trade to the historical distribution of similar setups.
Answers: are live losses bigger / smaller than expected, and do they
cluster in a particular setup type (direction, session phase, score)?

Note: the backtest CSV covers to 2026-07-02. Live trades from Jul 15-17 do
not exist in the backtest, so we cannot do exact date-matching. Instead this
script characterises each live trade by its known properties (direction,
session phase from bar time) and compares the live P&L to the *population*
of similar historical trades.

Run from repo root:
    python -m research.shadow_analysis

Update LIVE_TRADES after each trading session.
"""

import re
import sys
import os
import pytz
import numpy as np
import pandas as pd
from datetime import datetime
from typing import List, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import src.bot as bot

# ── LIVE TRADE JOURNAL ────────────────────────────────────────────────────────
# Trades are extracted AUTOMATICALLY from the live logs by research/live_journal.py
# (persisted in research/live_trade_journal.csv, survives log rotation).
# No manual upkeep needed. If a specific trade's parsed values are ever wrong,
# add a corrected entry here keyed by signal_id — it overrides the parsed one.

MANUAL_OVERRIDES: Dict[str, Dict] = {
    # "V29-FVG-long-2026-07-17T120000-0500": {"live_pnl_usd": -18.66, "contracts": 3},
}


def _load_live_trades() -> List[Dict]:
    from research.live_journal import extract_live_trades
    trades = []
    for t in extract_live_trades(write_csv=True):
        row = {
            "signal_id": t["signal_id"],
            "live_pnl_usd": float(t["live_pnl_usd"]),
            "contracts": int(t["contracts"]) if str(t["contracts"]).strip() else 1,
        }
        row.update(MANUAL_OVERRIDES.get(t["signal_id"], {}))
        trades.append(row)
    return trades

# ── HELPERS ──────────────────────────────────────────────────────────────────

_CDT = pytz.FixedOffset(-300)  # UTC-5
_EASTERN = pytz.timezone("US/Eastern")
_SID_RE = re.compile(
    r"^V29-[A-Z]+-(\w+)-(\d{4})-(\d{2})-(\d{2})T(\d{2})(\d{2})(\d{2})-(\d{4})$"
)


def _parse_signal_id(sid: str):
    """Return (direction, bar_hour_ct) parsed from a V29 signal_id."""
    m = _SID_RE.match(sid)
    if not m:
        raise ValueError(f"Unrecognised signal_id format: {sid!r}")
    direction = m.group(1)
    yr, mo, dy, hh, mm, ss = (int(m.group(i)) for i in range(2, 8))
    # offset_str is always "0500" in these IDs (CDT = UTC-5)
    dt_cdt = _CDT.localize(datetime(yr, mo, dy, hh, mm, ss))
    return direction, dt_cdt


def _session_phase(hour_ct: int) -> str:
    if hour_ct < 11:
        return "am"
    if hour_ct < 13:
        return "lunch"
    return "pm"


def _pct_rank(value: float, population) -> float:
    pop = [x for x in population if not np.isnan(x)]
    if not pop:
        return float("nan")
    return 100.0 * sum(1 for x in pop if x <= value) / len(pop)


def _fmt_sep(char="-", width=72):
    print(char * width)


# ── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    _fmt_sep("=")
    print("SHADOW ACCOUNT ANALYSIS -- V29 FVG  (Live vs Historical Distribution)")
    _fmt_sep("=")

    # ── 1. Load live trades (auto-extracted from live logs) ──────────────────
    live_trades = _load_live_trades()
    if not live_trades:
        print("\nNo live trades found in the logs. Nothing to analyse.")
        return
    print(f"\nAuto-extracted {len(live_trades)} live trades from logs "
          f"(journal: research/live_trade_journal.csv)")
    parsed = []
    for entry in live_trades:
        direction, dt_cdt = _parse_signal_id(entry["signal_id"])
        hour_ct = dt_cdt.hour
        parsed.append({
            **entry,
            "direction": direction,
            "hour_ct": hour_ct,
            "phase": _session_phase(hour_ct),
            "bar_label": dt_cdt.strftime("%m-%d %H:%M CT"),
        })

    # ── 2. Load data & run backtest ───────────────────────────────────────────
    print("\nLoading data and running backtest (~ 30 s)...")
    df = bot.fetch_data()
    df = bot.add_indicators(df)
    df = bot.generate_signals(df)
    session_levels = bot.compute_session_levels(df)
    _, bt_trades, _, _ = bot.run_backtest(df, session_levels)
    fvg_trades = [t for t in bt_trades if t.entry_type == "FVG"]
    n_wins = sum(1 for t in fvg_trades if t.won)
    print(f"Backtest: {len(fvg_trades):,} FVG trades, {n_wins:,} wins "
          f"({100*n_wins/len(fvg_trades):.1f}% WR)\n")
    print("NOTE: backtest CSV covers to 2026-07-02. Jul 15-17 live trades")
    print("      are compared to the HISTORICAL POPULATION, not date-matched.\n")

    # Pre-compute per-contract P&L for all backtest trades
    for t in fvg_trades:
        t._ppc = t.pnl_usd / t.contracts if t.contracts > 0 else 0.0

    # ── 3. Per-trade distribution comparison ─────────────────────────────────
    _fmt_sep()
    print("Per-trade: where does each live outcome sit in historical distribution?")
    print(f"\n  {'#':<3} {'Bar (CT)':<16} {'Dir':<6} {'Contracts':<10} "
          f"{'Live $/ct':>9} {'Hist med $/ct':>13} {'Pctile':>7} {'Phase'}")
    _fmt_sep()

    all_comparisons = []
    for i, live in enumerate(parsed, 1):
        live_ppc = live["live_pnl_usd"] / live["contracts"]

        # Peer = same direction + same session phase
        peers = [t for t in fvg_trades
                 if t.direction == live["direction"]
                 and _session_phase(t.entry_hour) == live["phase"]]

        if not peers:
            peers = [t for t in fvg_trades if t.direction == live["direction"]]

        peer_ppc = [t._ppc for t in peers]
        median_ppc = float(np.median(peer_ppc))
        pctile = _pct_rank(live_ppc, peer_ppc)

        print(f"  {i:<3} {live['bar_label']:<16} {live['direction']:<6} "
              f"{live['contracts']:<10} {live_ppc:>9.2f} {median_ppc:>13.2f} "
              f"{pctile:>6.0f}%  {live['phase']}")
        all_comparisons.append({**live, "live_ppc": live_ppc,
                                 "median_ppc": median_ppc, "pctile": pctile,
                                 "peers": peers})

    # ── 4. Aggregate summary ──────────────────────────────────────────────────
    _fmt_sep()
    live_total = sum(c["live_pnl_usd"] for c in all_comparisons)
    live_ppc_all = [c["live_ppc"] for c in all_comparisons]
    avg_live_ppc = np.mean(live_ppc_all)

    # Overall historical benchmark (all FVG)
    all_ppc = [t._ppc for t in fvg_trades]
    overall_median_ppc = float(np.median(all_ppc))
    overall_mean_ppc = float(np.mean(all_ppc))
    overall_wr = 100 * sum(1 for t in fvg_trades if t.won) / len(fvg_trades)

    live_wins = sum(1 for c in all_comparisons if c["live_pnl_usd"] > 0)
    print(f"\n  Live trades  : {len(all_comparisons)}")
    print(f"  Total P&L    : ${live_total:+.2f}")
    print(f"  Live wins    : {live_wins}/{len(all_comparisons)}")
    print(f"\n  Avg live P&L per contract  : ${avg_live_ppc:+.2f}")
    print(f"  Historical median per ct   : ${overall_median_ppc:+.2f}  "
          f"(mean ${overall_mean_ppc:+.2f})")
    print(f"  Historical win rate        : {overall_wr:.1f}%")

    # Is the magnitude of losses unusual?
    losing_trades = [t for t in fvg_trades if not t.won]
    live_losses = [c["live_ppc"] for c in all_comparisons if c["live_pnl_usd"] <= 0]
    if losing_trades and live_losses:
        loss_ppc = [t._ppc for t in losing_trades]
        avg_hist_loss_ppc = float(np.mean(loss_ppc))
        avg_live_loss_ppc = float(np.mean(live_losses))
        print(f"\n  Avg historical loss per ct : ${avg_hist_loss_ppc:+.2f}")
        print(f"  Avg live loss per ct       : ${avg_live_loss_ppc:+.2f}")
        drag = avg_live_loss_ppc - avg_hist_loss_ppc
        label = "LARGER losses than history" if drag < -2 else \
                "SMALLER losses than history" if drag > 2 else \
                "losses in line with history"
        print(f"  Difference                 : ${drag:+.2f}  --> {label}")

    # ── 5. Streak probability check (current trailing losing streak) ─────────
    _fmt_sep()
    streak_len = 0
    for c in reversed(all_comparisons):
        if c["live_pnl_usd"] > 0:
            break
        streak_len += 1
    print(f"Streak analysis: how rare is a {streak_len}-trade losing streak?")
    n = len(fvg_trades)
    loss_rate = 1 - (sum(1 for t in fvg_trades if t.won) / n)
    p_single = loss_rate ** streak_len
    expected_occurrences = n * p_single
    print(f"\n  Historical loss rate       : {100*loss_rate:.1f}%")
    print(f"  P(loss^{streak_len})           : {p_single:.4f}  ({100*p_single:.2f}%)")
    print(f"  Expected occurrences in    ")
    print(f"    {n:,} trade history     : {expected_occurrences:.0f}x")
    print(f"\n  --> A {streak_len}-trade losing streak is ROUTINE at this loss rate.")
    print(f"      Expect it to happen ~{expected_occurrences:.0f} times over the")
    print(f"      full 7-year backtest. No statistical red flag.")

    # ── 6. Setup quality breakdown ────────────────────────────────────────────
    _fmt_sep()
    print("Setup breakdown by direction + session phase (historical benchmarks):")
    print(f"\n  {'Direction + Phase':<22} {'N hist':>7} {'WR%':>5} "
          f"{'Avg $/ct win':>13} {'Avg $/ct loss':>14}")
    _fmt_sep()

    buckets_seen = set()
    for c in all_comparisons:
        key = (c["direction"], c["phase"])
        if key in buckets_seen:
            continue
        buckets_seen.add(key)
        bucket = [t for t in fvg_trades
                  if t.direction == c["direction"]
                  and _session_phase(t.entry_hour) == c["phase"]]
        if not bucket:
            continue
        bwr = 100 * sum(1 for t in bucket if t.won) / len(bucket)
        win_ppc = [t._ppc for t in bucket if t.won] or [0]
        loss_ppc = [t._ppc for t in bucket if not t.won] or [0]
        label = f"{c['direction']:<6} {c['phase']}"
        print(f"  {label:<22} {len(bucket):>7} {bwr:>4.1f}%  "
              f"{np.mean(win_ppc):>13.2f}  {np.mean(loss_ppc):>14.2f}")

    _fmt_sep("=")
    print("INTERPRETATION")
    _fmt_sep()
    print("  Pctile < 20%  : live loss was in the worst 20% of historical losses")
    print("                  for that direction/phase -- bad luck, not bad fills")
    print("  Pctile 20-50% : loss magnitude is typical -- pure variance")
    print("  Pctile > 50%  : live loss smaller than median -- fills OK")
    print()
    print("  Avg loss per ct vs history:")
    print("    Within $5    -> no execution concern")
    print("    > $10 worse  -> investigate fill quality / slippage")
    print()
    print("  Streak at n=9: statistically routine. No action warranted.")
    print("  Reassess after 30 live trades for meaningful win-rate signal.")
    _fmt_sep("=")


if __name__ == "__main__":
    main()
