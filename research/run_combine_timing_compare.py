"""
Compare combine-passing TIMING and probability: baseline vs gap-aware 1.5x.
Reuses the bootstrap combine simulator from run_combine_sim.
"""
import numpy as np
import src.bot as bot
from src.run_combine_sim import _simulate  # note: this import sets up UTF-8 stdout


def _arrays():
    df = bot.fetch_data(); bot.validate_loaded_data(df)
    di = bot.add_indicators(df); sl = bot.compute_session_levels(di)
    ds = bot.generate_signals(di.copy())
    _, trades, daily_records, _ = bot.run_backtest(ds, sl)
    dp = np.array([d.daily_pnl_net for d in daily_records], dtype=float)
    ip = np.array([d.max_intraday_peak for d in daily_records], dtype=float)
    qf = np.array([d.is_qualifying_day for d in daily_records], dtype=bool)
    return dp, ip, qf, len(trades), len(daily_records)


def run():
    configs = [("BASELINE (gap-aware off)", False), ("GAP-AWARE 1.5x", True)]
    rows = []
    for label, gap in configs:
        bot.GAP_AWARE_SIZING_ENABLED = gap
        bot.GAP_STOP_MULT = 1.5
        print(f"Running backtest: {label} ...")
        dp, ip, qf, ntr, nd = _arrays()
        avg_day = dp.mean()
        out = {}
        for scen, md in [("no_limit", None), ("30d", 30), ("60d", 60)]:
            p, days, fails = _simulate(dp, ip, qf, md)
            out[scen] = (p, np.array(days))
        rows.append((label, avg_day, out))
    bot.GAP_AWARE_SIZING_ENABLED = False

    print("\n" + "=" * 78)
    print("COMBINE TIMING & PASS-RATE  —  baseline vs gap-aware 1.5x  (100k sims each)")
    print("=" * 78)
    print(f"{'Config':<26}{'Avg/day':>9}{'P(pass)':>9}{'Median':>9}{'25th':>7}{'75th':>7}{'P(30d)':>8}{'P(60d)':>8}")
    print("-" * 78)
    for label, avg_day, out in rows:
        p_nl, d_nl = out["no_limit"]
        med = np.median(d_nl) if len(d_nl) else 0
        p25 = np.percentile(d_nl, 25) if len(d_nl) else 0
        p75 = np.percentile(d_nl, 75) if len(d_nl) else 0
        p30 = out["30d"][0]
        p60 = out["60d"][0]
        print(f"{label:<26}${avg_day:>7,.0f}{p_nl:>8.1f}%{med:>8.0f}d{p25:>6.0f}d{p75:>6.0f}d{p30:>7.1f}%{p60:>7.1f}%")
    print("=" * 78)
    print("Median/25th/75th = trading days to reach +$3,000 (no time limit).")
    print("P(30d)/P(60d) = probability of passing within 30 / 60 trading days.")


if __name__ == "__main__":
    run()
