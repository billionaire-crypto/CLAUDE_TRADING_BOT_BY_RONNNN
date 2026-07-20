"""
Live-vs-backtest drift monitor. Reads the live fill-forensics CSV the bot writes
and compares live execution to what the backtest promised, so you learn EARLY if
reality is diverging from the plan (worse fills, lower win rate, weaker edge).

Read-only. Run any time:  python -X utf8 drift_report.py
Useful only once live fills exist; before that it says so and exits cleanly.
"""
import csv
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

CSV_PATH = os.path.join(os.path.dirname(__file__), "src", "exports", "v29_fill_forensics.csv")

# Backtest baselines (vwap_only shipped config). PF is intentionally the LIVE
# plan (~3), not the 4.10 backtest — real fills are worse; see PLAYBOOK.md.
EXPECT = {
    "win_rate": 0.296,        # ~29.6% (recent samples higher)
    "avg_trade_usd": 154.0,   # avg $/trade
    "profit_factor": 3.0,     # plan for ~3 live (backtest 4.10)
    "entry_slip_ticks": 1.0,  # backtest assumes ~1 tick
}
MIN_TRADES_TO_JUDGE = 20      # below this, sample is noise — informational only

# Tripwires (from PLAYBOOK.md) — these mean "stop and investigate".
WR_ALARM_BELOW = 0.25        # over 30+ trades
SLIP_ALARM_TICKS = 3.0       # the bot auto-halts here too


def _f(row, key):
    try:
        return float(row.get(key, "") or "")
    except (TypeError, ValueError):
        return None


def load_fills():
    if not os.path.exists(CSV_PATH):
        return []
    with open(CSV_PATH, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def main():
    print("=== Live vs. backtest drift report ===\n")
    rows = load_fills()
    entries = [r for r in rows if r.get("kind") == "entry"]
    exits = [r for r in rows if str(r.get("kind", "")).startswith("exit")]
    n = len(exits)  # one exit == one completed (round-trip) trade

    if n == 0:
        print("No completed live trades yet.")
        print(f"(entries logged so far: {len(entries)})")
        print("\nRun this again after the bot has taken some trades.")
        return 0

    pnls = [p for r in exits if (p := _f(r, "trade_pnl")) is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    entry_slips = [s for r in entries if (s := _f(r, "slippage_ticks")) is not None]
    exit_slips = [s for r in exits if (s := _f(r, "slippage_ticks")) is not None]

    win_rate = len(wins) / len(pnls) if pnls else 0.0
    avg_trade = sum(pnls) / len(pnls) if pnls else 0.0
    total_pnl = sum(pnls)
    pf = (sum(wins) / abs(sum(losses))) if losses else float("inf")
    avg_entry_slip = sum(entry_slips) / len(entry_slips) if entry_slips else 0.0
    avg_exit_slip = sum(exit_slips) / len(exit_slips) if exit_slips else 0.0

    def line(label, live, exp, fmt, good_high=True):
        gap = live - exp
        arrow = "≈"
        if abs(gap) > abs(exp) * 0.15 + 1e-9:
            arrow = "▲" if (gap > 0) == good_high else "▼"
        print(f"  {label:<22}{fmt(live):>12}   vs {fmt(exp):>10}   {arrow}")

    pct = lambda x: f"{x*100:.1f}%"
    usd = lambda x: f"${x:,.0f}"
    tk = lambda x: f"{x:.2f}t"
    pfx = lambda x: ("inf" if x == float("inf") else f"{x:.2f}")

    print(f"Completed trades: {n}   |   total P&L: {usd(total_pnl)}\n")
    print(f"  {'METRIC':<22}{'LIVE':>12}   vs {'BACKTEST':>10}")
    line("Win rate", win_rate, EXPECT["win_rate"], pct, good_high=True)
    line("Avg $/trade", avg_trade, EXPECT["avg_trade_usd"], usd, good_high=True)
    line("Profit factor", pf, EXPECT["profit_factor"], pfx, good_high=True)
    line("Entry slippage", avg_entry_slip, EXPECT["entry_slip_ticks"], tk, good_high=False)
    print(f"  {'Exit slippage':<22}{tk(avg_exit_slip):>12}   (informational)")

    # ---- verdict ----
    print()
    alarms = []
    if avg_entry_slip > SLIP_ALARM_TICKS or avg_exit_slip > SLIP_ALARM_TICKS:
        alarms.append(f"slippage over {SLIP_ALARM_TICKS} ticks — the bot auto-halts on this; investigate fills")
    if n >= 30 and win_rate < WR_ALARM_BELOW:
        alarms.append(f"win rate {pct(win_rate)} below {pct(WR_ALARM_BELOW)} over {n} trades — re-validate the edge")

    if n < MIN_TRADES_TO_JUDGE:
        print(f"⏳ Only {n} trades — too few to judge (need ~{MIN_TRADES_TO_JUDGE}). Informational only; "
              "a losing streak here is normal.")
    elif alarms:
        print("🔴 DRIFT ALARM — stop and investigate:")
        for a in alarms:
            print(f"   • {a}")
    else:
        print(f"🟢 Live is tracking the backtest within tolerance over {n} trades. Keep going.")
    print("\nReminder: judge on ~30+ trades, not a day or a week (see PLAYBOOK.md).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
