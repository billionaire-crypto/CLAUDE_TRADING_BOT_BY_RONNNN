"""Batch 5: combine sims for the LIVE-COMPATIBLE ATR-target variants (no trail)."""
import numpy as np
import src.bot as bot
from src.run_combine_sim import _simulate

def main():
    print("Loading once...")
    df_raw = bot.fetch_data(); bot.validate_loaded_data(df_raw)
    di = bot.add_indicators(df_raw); sl = bot.compute_session_levels(di)
    ds = bot.generate_signals(di.copy())
    for label, mult, cap in (("ATRT2_C240", 2.0, 240), ("ATRT2_C140", 2.0, 140)):
        bot.ATR_TARGET_ENABLED = True; bot.ATR_TARGET_MULT = mult; bot.ATR_TARGET_MAX_TICKS = cap
        _, tr, dr, _ = bot.run_backtest(ds, sl)
        dp = np.array([d.daily_pnl_net for d in dr]); ip = np.array([d.max_intraday_peak for d in dr])
        qf = np.array([d.is_qualifying_day for d in dr], dtype=bool)
        p_nl, days_nl, fails = _simulate(dp, ip, qf, None)
        p_30, _, _ = _simulate(dp, ip, qf, 30)
        med = np.median(days_nl) if len(days_nl) else 0
        print(f"  {label}: P(pass) {p_nl:.1f}% | median {med:.0f}d | P(30d) {p_30:.1f}% | "
              f"fails trailDD {fails['trail_dd']/1000:.2f}% dailyLim {fails['daily_limit']/1000:.2f}%")
    bot.ATR_TARGET_ENABLED = False
    print("Done.")

if __name__ == "__main__":
    main()
