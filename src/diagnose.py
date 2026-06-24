"""
╔══════════════════════════════════════════════════════════════╗
║  EDGE DIAGNOSTIC — is the backtest edge real or curve-fit?    ║
║                                                              ║
║  Runs the exact V29 backtest, then breaks the results down   ║
║  over TIME to expose two classic overfitting symptoms:       ║
║    1. Profit concentrated in a few early years               ║
║    2. Signal frequency drying up in recent data              ║
║                                                              ║
║  Run it the same way as the backtest:                        ║
║    MNQ_DATA_PATH=/path/to/your.csv python -m src.diagnose    ║
╚══════════════════════════════════════════════════════════════╝
"""
import sys
from collections import defaultdict

import pandas as pd

from src import bot


def _year_of(trade) -> int:
    return pd.Timestamp(trade.date).year


def _profit_factor(trades) -> float:
    gains = sum(t.pnl_usd for t in trades if t.pnl_usd > 0)
    losses = -sum(t.pnl_usd for t in trades if t.pnl_usd < 0)
    if losses <= 0:
        return float("inf") if gains > 0 else 0.0
    return gains / losses


def _block_stats(trades) -> dict:
    n = len(trades)
    wins = sum(1 for t in trades if t.won)
    net = sum(t.pnl_usd for t in trades)
    return {
        "trades": n,
        "win_rate": (wins / n * 100) if n else 0.0,
        "net": net,
        "pf": _profit_factor(trades),
    }


def main() -> None:
    print("=" * 64)
    print("  EDGE DIAGNOSTIC — V29")
    print("=" * 64)

    # Reuse the exact backtest pipeline so the numbers match the real bot.
    df = bot.fetch_data()
    bot.validate_loaded_data(df)
    df = bot.add_indicators(df)
    df = bot.generate_signals(df)
    session_levels = bot.compute_session_levels(df)
    df, trades, daily_records, state = bot.run_backtest(df, session_levels)

    if not trades:
        print("\n  No trades were generated. Nothing to diagnose.")
        return

    total_net = sum(t.pnl_usd for t in trades)
    print(f"\n  Total trades: {len(trades)}   Total net P&L: ${total_net:,.0f}")

    # ── 1. YEAR-BY-YEAR ────────────────────────────────────────────────
    by_year = defaultdict(list)
    for t in trades:
        by_year[_year_of(t)].append(t)

    years = sorted(by_year)
    span_years = max(1, len(years))
    months_total = max(1, len({(pd.Timestamp(t.date).year, pd.Timestamp(t.date).month) for t in trades}))

    print("\n  ── YEAR-BY-YEAR (same settings every year) ──")
    print(f"  {'Year':<6}{'Trades':>8}{'Win%':>8}{'Net $':>12}{'PF':>7}  Verdict")
    profitable_years = 0
    for y in years:
        s = _block_stats(by_year[y])
        if s["net"] > 0:
            profitable_years += 1
        verdict = "profit" if s["net"] > 0 else "LOSS"
        pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        print(f"  {y:<6}{s['trades']:>8}{s['win_rate']:>7.0f}%{s['net']:>12,.0f}{pf:>7}  {verdict}")

    # ── 2. SIGNAL FREQUENCY TREND ──────────────────────────────────────
    print("\n  ── SIGNAL FREQUENCY (trades per year) ──")
    first_half = years[: len(years) // 2] or years[:1]
    second_half = years[len(years) // 2 :]
    fh_rate = sum(len(by_year[y]) for y in first_half) / max(1, len(first_half))
    sh_rate = sum(len(by_year[y]) for y in second_half) / max(1, len(second_half))
    print(f"  Earlier years avg: {fh_rate:.0f} trades/yr")
    print(f"  Recent  years avg: {sh_rate:.0f} trades/yr")
    if sh_rate < fh_rate * 0.6:
        print("  >>> Signal frequency has DROPPED sharply in recent years.")
        print("      This matches a bot that 'barely fires' when traded live.")

    # ── 3. IN-SAMPLE vs OUT-OF-SAMPLE (last ~30% of the timeline) ──────
    sorted_trades = sorted(trades, key=lambda t: pd.Timestamp(t.date))
    split_idx = int(len(sorted_trades) * 0.70)
    in_sample = sorted_trades[:split_idx]
    out_sample = sorted_trades[split_idx:]
    print("\n  ── IN-SAMPLE vs OUT-OF-SAMPLE ──")
    print("  (Out-of-sample = most recent 30% of trades = closest thing to 'live')")
    for label, block in (("In-sample (older 70%)", in_sample), ("Out-of-sample (recent 30%)", out_sample)):
        s = _block_stats(block)
        pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        print(f"  {label:<30} trades={s['trades']:>4}  win={s['win_rate']:>4.0f}%  net=${s['net']:>9,.0f}  PF={pf}")

    # ── 4. PROFIT CONCENTRATION ────────────────────────────────────────
    best_year = max(years, key=lambda y: sum(t.pnl_usd for t in by_year[y]))
    best_year_net = sum(t.pnl_usd for t in by_year[best_year])
    concentration = (best_year_net / total_net * 100) if total_net > 0 else 0.0

    print("\n" + "=" * 64)
    print("  PLAIN-ENGLISH VERDICT")
    print("=" * 64)
    print(f"  Profitable years: {profitable_years} of {len(years)}")
    if total_net > 0:
        print(f"  Best single year ({best_year}) = {concentration:.0f}% of ALL profit")
    os_stats = _block_stats(out_sample)
    print(f"  Recent (out-of-sample) result: ${os_stats['net']:,.0f} over {os_stats['trades']} trades, "
          f"{os_stats['win_rate']:.0f}% win rate")
    avg_month = total_net / months_total
    print(f"  Avg per active month (whole period): ${avg_month:,.0f}")

    print("\n  HOW TO READ THIS:")
    print("  - Edge is likely REAL if: most years profitable, out-of-sample still")
    print("    positive, and no single year dominates the profit.")
    print("  - Edge is likely OVERFIT if: profit concentrated in a few early years,")
    print("    out-of-sample weak/negative, and signal frequency dropping.")
    print("=" * 64)


if __name__ == "__main__":
    sys.exit(main())
