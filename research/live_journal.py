"""
Live Trade Journal — automatic extraction from the bot's live logs.
==================================================================
Parses src/exports/live_log_YYYYMMDD.txt files and reconstructs every live
trade: signal_id, direction, contracts, and realised P&L. Persists to a
cumulative CSV so history survives log rotation/cleanup.

How P&L attribution works (beginner explanation):
  The bot logs a "reconcile" line about once a minute with the account's
  running P&L for the day. When the account is FLAT (no open position), that
  number is pure realised P&L. So for each order submission, the trade's P&L
  is simply:  (flat P&L after the trade) - (flat P&L before the trade).
  The strategy holds one position at a time, so the next submission can only
  happen after the previous trade closed - which makes this attribution exact.

Run from repo root:
    python -m research.live_journal          # parse logs, update CSV, print

Used by research/shadow_analysis.py as its data source.
"""

import csv
import glob
import os
import re
from typing import Dict, List, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPORTS_DIR = os.path.join(REPO_ROOT, "src", "exports")
JOURNAL_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "live_trade_journal.csv")

JOURNAL_FIELDS = ["date", "signal_id", "direction", "bar_time_ct",
                  "contracts", "live_pnl_usd", "source_log"]

# Log line patterns (single-line entries in live_log_*.txt)
_RE_RECONCILE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) C[DS]T\] \[INFO\] reconcile_state "
    r".*open_orders=(\d+) open_positions=(\d+) session_pnl=(-?[\d.]+)"
)
_RE_SUBMITTED = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) C[DS]T\] \[INFO\] order_submitted "
    r"signal_id=(\S+)"
)
_RE_SIZING = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) C[DS]T\] \[INFO\] order_sizing "
    r".*final_size=(\d+)"
)
_RE_SIGNAL_ID = re.compile(r"^V29-\w+-(long|short)-\d{4}-\d{2}-\d{2}T(\d{2})(\d{2})")

# P&L resets to 0 at the 17:00 CT session rollover; ignore snapshots after it.
_EOD_CUTOFF = "17:00:00"


def _parse_day_log(path: str) -> List[Dict]:
    """Extract completed trades from one day's live log (sequential scan)."""
    submissions = []          # {signal_id, time, contracts, baseline_pnl}
    last_flat_pnl = 0.0       # most recent P&L while account was flat
    last_sizing: Optional[int] = None

    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = _RE_RECONCILE.match(line)
            if m:
                _, t, orders, positions, pnl = m.groups()
                if t >= _EOD_CUTOFF:
                    continue          # post-rollover: P&L was reset to 0
                # Truly flat = no position AND no working orders. A submitted-
                # but-unfilled entry shows positions=0 with orders>0 and must
                # not be mistaken for a completed trade.
                if int(positions) == 0 and int(orders) == 0:
                    last_flat_pnl = float(pnl)
                    # a flat snapshot closes out any still-open submission
                    if submissions and submissions[-1].get("final_pnl") is None:
                        submissions[-1]["final_pnl"] = last_flat_pnl
                continue

            m = _RE_SIZING.match(line)
            if m:
                last_sizing = int(m.group(3))
                continue

            m = _RE_SUBMITTED.match(line)
            if m:
                date, t, signal_id = m.groups()
                submissions.append({
                    "date": date,
                    "time": t,
                    "signal_id": signal_id,
                    "contracts": last_sizing,
                    "baseline_pnl": last_flat_pnl,
                    "final_pnl": None,
                })

    trades = []
    for sub in submissions:
        if sub["final_pnl"] is None:
            continue                  # trade never closed within the log (skip)
        sid_m = _RE_SIGNAL_ID.match(sub["signal_id"])
        direction = sid_m.group(1) if sid_m else "?"
        bar_time = f"{sid_m.group(2)}:{sid_m.group(3)}" if sid_m else "?"
        trades.append({
            "date": sub["date"],
            "signal_id": sub["signal_id"],
            "direction": direction,
            "bar_time_ct": bar_time,
            "contracts": sub["contracts"] if sub["contracts"] is not None else "",
            "live_pnl_usd": round(sub["final_pnl"] - sub["baseline_pnl"], 2),
            "source_log": os.path.basename(path),
        })
    return trades


def _load_existing_journal() -> Dict[str, Dict]:
    if not os.path.exists(JOURNAL_CSV):
        return {}
    with open(JOURNAL_CSV, encoding="utf-8", newline="") as fh:
        return {r["signal_id"]: r for r in csv.DictReader(fh)}


def extract_live_trades(write_csv: bool = True) -> List[Dict]:
    """Parse all available live logs, merge with the persisted journal
    (so trades survive log rotation), return trades sorted by signal time."""
    journal = _load_existing_journal()
    for path in sorted(glob.glob(os.path.join(EXPORTS_DIR, "live_log_*.txt"))):
        for trade in _parse_day_log(path):
            journal[trade["signal_id"]] = trade   # newest parse wins

    trades = sorted(journal.values(), key=lambda r: r["signal_id"].split("-", 3)[-1])
    if write_csv and trades:
        with open(JOURNAL_CSV, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=JOURNAL_FIELDS)
            w.writeheader()
            for t in trades:
                w.writerow({f: t.get(f, "") for f in JOURNAL_FIELDS})
    return trades


def main():
    trades = extract_live_trades(write_csv=True)
    if not trades:
        print("No completed live trades found in the logs.")
        return
    print(f"{'Date':<12} {'Bar CT':<7} {'Dir':<6} {'Ctr':>3} {'P&L $':>9}  Signal")
    print("-" * 78)
    total = 0.0
    for t in trades:
        pnl = float(t["live_pnl_usd"])
        total += pnl
        print(f"{t['date']:<12} {t['bar_time_ct']:<7} {t['direction']:<6} "
              f"{str(t['contracts']):>3} {pnl:>9.2f}  {t['signal_id']}")
    wins = sum(1 for t in trades if float(t["live_pnl_usd"]) > 0)
    print("-" * 78)
    print(f"{len(trades)} trades, {wins} wins, total ${total:+.2f}")
    print(f"journal -> {JOURNAL_CSV}")


if __name__ == "__main__":
    main()
