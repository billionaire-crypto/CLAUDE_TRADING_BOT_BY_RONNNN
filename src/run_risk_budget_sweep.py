"""
Risk-budget sizing sweep: find the calibration that keeps most of the P&L while
still capping every trade under the combine DLL (with room for live gap-through
slippage the backtest does not model).

Two levers:
  HEADROOM_SAFETY_FRAC -> caps fresh-day / low-headroom sizing (binds most often)
  RISK_BUDGET_MAP      -> caps max size once an intraday cushion is built

Loads data / builds signals once, then runs the backtest for each config.
Reports the metrics that matter for a Topstep combine plus the worst SINGLE
trade loss (so we can confirm the structural cap holds with gap slack).
"""
import numpy as np
import src.bot as bot


# (label, frac, budget_map, default_budget)  -- None frac => baseline (model off)
CONFIGS = [
    ("BASELINE",        None, None,                         None),
    ("A frac.50 tight", 0.50, {8: 600, 7: 450, 6: 200},     100),
    ("B frac.65 mid",   0.65, {8: 750, 7: 550, 6: 275},     125),
    ("C frac.80 loose", 0.80, {8: 900, 7: 675, 6: 350},     150),
    ("D frac.80 hi-bud",0.80, {8: 1200, 7: 900, 6: 450},    150),
]


def _metrics(trades, daily_records):
    pnls   = [t.pnl_usd for t in trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    day_pnls = [d.daily_pnl_net for d in daily_records]
    arr = np.array(day_pnls) if day_pnls else np.array([0.0])
    sharpe = (arr.mean() / arr.std(ddof=1) * np.sqrt(252)
              if len(arr) > 1 and arr.std(ddof=1) > 0 else 0.0)
    ctrs = [t.contracts for t in trades]
    return {
        "net":         sum(pnls),
        "wr":          len(wins) / len(pnls) * 100 if pnls else 0.0,
        "pf":          sum(wins) / abs(sum(losses)) if losses else 0.0,
        "sharpe":      sharpe,
        "worst_day":   min(day_pnls) if day_pnls else 0.0,
        "worst_trade": min(pnls) if pnls else 0.0,
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

    orig = (bot.RISK_BUDGET_SIZING_ENABLED, bot.RISK_BUDGET_MAP,
            bot.RISK_BUDGET_DEFAULT, bot.HEADROOM_SAFETY_FRAC)

    results = []
    for label, frac, bmap, dflt in CONFIGS:
        if frac is None:
            bot.RISK_BUDGET_SIZING_ENABLED = False
        else:
            bot.RISK_BUDGET_SIZING_ENABLED = True
            bot.HEADROOM_SAFETY_FRAC = frac
            bot.RISK_BUDGET_MAP = {int(k): float(v) for k, v in bmap.items()}
            bot.RISK_BUDGET_DEFAULT = float(dflt)
        print(f"Running: {label} ...")
        _, trades, daily, _ = bot.run_backtest(df_sig, sl)
        results.append((label, _metrics(trades, daily)))

    (bot.RISK_BUDGET_SIZING_ENABLED, bot.RISK_BUDGET_MAP,
     bot.RISK_BUDGET_DEFAULT, bot.HEADROOM_SAFETY_FRAC) = orig

    base_net = results[0][1]["net"]
    print()
    print("=" * 108)
    print("RISK-BUDGET SIZING SWEEP")
    print("=" * 108)
    hdr = (f"{'Config':<18}{'Net P&L':>12}{'vs base':>9}{'Sharpe':>8}"
           f"{'PF':>6}{'WorstDay':>10}{'WorstTrd':>10}{'<=-750':>8}"
           f"{'<=-1000':>9}{'MaxCtr':>8}{'AvgCtr':>8}")
    print(hdr)
    print("-" * 108)
    for label, m in results:
        vs = "" if m["net"] == base_net else f"{(m['net']-base_net)/base_net*100:+.0f}%"
        print(f"{label:<18}${m['net']:>10,.0f}{vs:>9}{m['sharpe']:>8.2f}"
              f"{m['pf']:>6.2f}${m['worst_day']:>8,.0f}${m['worst_trade']:>8,.0f}"
              f"{m['breach_750']:>8}{m['breach_1000']:>9}{m['max_ctr']:>8}{m['avg_ctr']:>8.1f}")
    print("=" * 108)
    print("Note: WorstTrd is the worst SINGLE trade; add live gap-through slippage")
    print("      on top of it to estimate the true live worst case vs the $1000 DLL.")


if __name__ == "__main__":
    run()
