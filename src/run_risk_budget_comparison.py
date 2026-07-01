"""
A/B comparison: baseline (inert CDR / score-map sizing) vs risk-budget sizing
with a DLL-headroom cap.

Runs the full backtest twice on identical signals and reports the metrics that
matter for a Topstep combine: net P&L, win rate, profit factor, Sharpe, best/
worst day, and -- most importantly -- how many days would breach the -$750 bot
limit and the -$1,000 combine daily loss limit under each sizing scheme.

The point of the experiment: quantify what DLL-safe risk control costs in P&L,
and confirm the risk-budget model eliminates single-trade DLL breaches.
"""
import numpy as np
import src.bot as bot


def _metrics(trades, daily_records):
    pnls   = [t.pnl_usd for t in trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    net    = sum(pnls)
    wr     = len(wins) / len(pnls) * 100 if pnls else 0.0
    pf     = sum(wins) / abs(sum(losses)) if losses else 0.0

    day_pnls = [d.daily_pnl_net for d in daily_records]
    arr = np.array(day_pnls) if day_pnls else np.array([0.0])
    sharpe = (arr.mean() / arr.std(ddof=1) * np.sqrt(252)
              if len(arr) > 1 and arr.std(ddof=1) > 0 else 0.0)

    ctrs = [t.contracts for t in trades]
    return {
        "net":         net,
        "wr":          wr,
        "pf":          pf,
        "sharpe":      sharpe,
        "n_trades":    len(trades),
        "n_days":      len(daily_records),
        "best_day":    max(day_pnls) if day_pnls else 0.0,
        "worst_day":   min(day_pnls) if day_pnls else 0.0,
        "breach_750":  sum(1 for p in day_pnls if p <= -750.0),
        "breach_1000": sum(1 for p in day_pnls if p <= -1000.0),
        "max_ctr":     max(ctrs) if ctrs else 0,
        "avg_ctr":     float(np.mean(ctrs)) if ctrs else 0.0,
    }


def run():
    print("Loading data / building signals once...")
    df_raw = bot.fetch_data()
    bot.validate_loaded_data(df_raw)
    df_ind = bot.add_indicators(df_raw)
    sl     = bot.compute_session_levels(df_ind)
    df_sig = bot.generate_signals(df_ind.copy())

    runs = [("BASELINE", False), ("RISK-BUDGET", True)]
    results = {}
    for label, flag in runs:
        bot.RISK_BUDGET_SIZING_ENABLED = flag
        print(f"Running: {label} (RISK_BUDGET_SIZING_ENABLED={flag}) ...")
        _, trades, daily, _ = bot.run_backtest(df_sig, sl)
        results[label] = _metrics(trades, daily)
    bot.RISK_BUDGET_SIZING_ENABLED = False  # restore module default

    a, b = results["BASELINE"], results["RISK-BUDGET"]

    def row(name, key, fmt):
        print(f"  {name:<26}{fmt.format(a[key]):>18}{fmt.format(b[key]):>18}")

    print()
    print("=" * 62)
    print("RISK-BUDGET SIZING  --  A/B COMPARISON")
    print("=" * 62)
    print(f"  {'Metric':<26}{'BASELINE':>18}{'RISK-BUDGET':>18}")
    print("-" * 62)
    row("Net P&L",              "net",         "${:,.0f}")
    row("Win rate",             "wr",          "{:.1f}%")
    row("Profit factor",        "pf",          "{:.2f}")
    row("Sharpe (ann.)",        "sharpe",      "{:.2f}")
    row("Trades",               "n_trades",    "{:,}")
    row("Trading days",         "n_days",      "{:,}")
    row("Best day",             "best_day",    "${:,.0f}")
    row("Worst day",            "worst_day",   "${:,.0f}")
    row("Days <= -$750",        "breach_750",  "{:,}")
    row("Days <= -$1000 (DLL)", "breach_1000", "{:,}")
    row("Max contracts/trade",  "max_ctr",     "{:,}")
    row("Avg contracts/trade",  "avg_ctr",     "{:.1f}")
    print("=" * 62)

    delta = b["net"] - a["net"]
    pct   = (delta / a["net"] * 100) if a["net"] else 0.0
    print(f"\n  Net P&L change: ${delta:,.0f} ({pct:+.1f}%)")
    print(f"  Combine-fatal days removed: {a['breach_1000'] - b['breach_1000']} "
          f"(baseline {a['breach_1000']} -> risk-budget {b['breach_1000']})")


if __name__ == "__main__":
    run()
