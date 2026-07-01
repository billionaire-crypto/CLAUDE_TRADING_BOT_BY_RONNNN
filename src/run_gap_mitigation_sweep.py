"""
Gap-risk mitigation sweep. All runs use the honest backtest (gap-through stops +
phantom filter + EOD flatten already active). Compares the current config against
the two targeted gap-mitigation options and their combination:

  BASELINE          : no extra mitigation (accept the rare gap tail)
  NEWS-CAP          : cap size to NEWS_DAY_MAX_CONTRACTS on scheduled news days
  GAP-BUDGET 1.5x   : size the risk budget against a 1.5x-wider stop
  GAP-BUDGET 2.0x   : ... against a 2.0x-wider stop
  NEWS + GAP 1.5x   : both together

Reports the P&L-vs-safety trade-off: net P&L, PF, Sharpe, worst trade, worst day,
and how many trades/days breach the -$1,000 combine daily limit.
"""
import numpy as np
from collections import defaultdict
import src.bot as bot


def _metrics(trades):
    pnls = [t.pnl_usd for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    d = defaultdict(float)
    for t in trades:
        d[str(t.date)[:10]] += t.pnl_usd
    days = list(d.values())
    arr = np.array(days) if len(days) > 1 else np.array([0.0])
    sharpe = (arr.mean() / arr.std(ddof=1) * np.sqrt(252)
              if len(days) > 1 and arr.std(ddof=1) > 0 else 0.0)
    return {
        "net": sum(pnls),
        "wr": len(wins) / len(pnls) * 100 if pnls else 0.0,
        "pf": sum(wins) / abs(sum(losses)) if losses else 0.0,
        "sharpe": sharpe,
        "trades": len(trades),
        "worst_trade": min(pnls) if pnls else 0.0,
        "worst_day": min(days) if days else 0.0,
        "tr_le_1000": sum(1 for p in pnls if p <= -1000),
        "days_le_1000": sum(1 for x in days if x <= -1000),
    }


# (label, gap_aware, gap_mult, news_cap)
CONFIGS = [
    ("BASELINE",        False, 1.5, False),
    ("NEWS-CAP 10",     False, 1.5, True),
    ("GAP-BUDGET 1.5x", True,  1.5, False),
    ("GAP-BUDGET 2.0x", True,  2.0, False),
    ("NEWS + GAP 1.5x", True,  1.5, True),
]


def run():
    print("Loading data / building signals once (honest backtest)...")
    df_raw = bot.fetch_data()
    bot.validate_loaded_data(df_raw)
    df_ind = bot.add_indicators(df_raw)
    sl = bot.compute_session_levels(df_ind)
    df_sig = bot.generate_signals(df_ind.copy())

    orig = (bot.GAP_AWARE_SIZING_ENABLED, bot.GAP_STOP_MULT, bot.NEWS_DAY_SIZE_CAP_ENABLED)
    results = []
    for label, gap_aware, gap_mult, news_cap in CONFIGS:
        bot.GAP_AWARE_SIZING_ENABLED = gap_aware
        bot.GAP_STOP_MULT = gap_mult
        bot.NEWS_DAY_SIZE_CAP_ENABLED = news_cap
        print(f"Running: {label} ...")
        _, trades, _, _ = bot.run_backtest(df_sig, sl)
        results.append((label, _metrics(trades)))
    (bot.GAP_AWARE_SIZING_ENABLED, bot.GAP_STOP_MULT, bot.NEWS_DAY_SIZE_CAP_ENABLED) = orig

    base = results[0][1]["net"]
    print()
    print("=" * 100)
    print("GAP-RISK MITIGATION SWEEP  (honest backtest: gap-through + phantom filter + EOD flatten)")
    print("=" * 100)
    print(f"{'Config':<18}{'Net P&L':>11}{'vs base':>9}{'PF':>6}{'Sharpe':>8}"
          f"{'Trades':>8}{'WorstTrd':>10}{'WorstDay':>10}{'trd<=-1k':>9}{'day<=-1k':>9}")
    print("-" * 100)
    for label, m in results:
        vs = "" if m["net"] == base else f"{(m['net']-base)/base*100:+.1f}%"
        print(f"{label:<18}${m['net']:>9,.0f}{vs:>9}{m['pf']:>6.2f}{m['sharpe']:>8.2f}"
              f"{m['trades']:>8}${m['worst_trade']:>8,.0f}${m['worst_day']:>8,.0f}"
              f"{m['tr_le_1000']:>9}{m['days_le_1000']:>9}")
    print("=" * 100)
    print("trd<=-1k / day<=-1k = single trades / days breaching the -$1,000 combine daily limit")


if __name__ == "__main__":
    run()
