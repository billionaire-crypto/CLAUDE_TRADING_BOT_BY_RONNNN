"""
Combine speed vs safety sweep. Tests configs that trade for faster passing and
shows the trade-off: median days to pass, pass probability, and the metric that
actually matters -- expected attempts to get funded (1 / P(pass)).
"""
import numpy as np
import src.bot as bot
from src.run_combine_sim import _simulate  # sets up UTF-8 stdout


def _arrays():
    df = bot.fetch_data(); bot.validate_loaded_data(df)
    di = bot.add_indicators(df); sl = bot.compute_session_levels(di)
    ds = bot.generate_signals(di.copy())
    _, trades, dr, _ = bot.run_backtest(ds, sl)
    dp = np.array([d.daily_pnl_net for d in dr], dtype=float)
    ip = np.array([d.max_intraday_peak for d in dr], dtype=float)
    qf = np.array([d.is_qualifying_day for d in dr], dtype=bool)
    return dp, ip, qf, len(trades) / max(1, len(dr))


# (label, gap_aware, max_trades, headroom_frac)
CONFIGS = [
    ("SAFE: gap1.5x 4tr f.80", True,  4, 0.80),
    ("CURRENT-OFF: 4tr f.80",  False, 4, 0.80),
    ("MORE TRADES: 5tr f.80",  False, 5, 0.80),
    ("BIGGER: 4tr f1.00",      False, 4, 1.00),
    ("AGGRESSIVE: 5tr f1.00",  False, 5, 1.00),
]


def run():
    orig = (bot.GAP_AWARE_SIZING_ENABLED, bot.STRONG_MAX_TRADES, bot.HEADROOM_SAFETY_FRAC)
    rows = []
    for label, gap, mt, frac in CONFIGS:
        bot.GAP_AWARE_SIZING_ENABLED = gap
        bot.GAP_STOP_MULT = 1.5
        bot.STRONG_MAX_TRADES = mt
        bot.HEADROOM_SAFETY_FRAC = frac
        print(f"Running: {label} ...")
        dp, ip, qf, tpd = _arrays()
        p_nl, days_nl, _ = _simulate(dp, ip, qf, None)
        p_30, _, _ = _simulate(dp, ip, qf, 30)
        med = np.median(days_nl) if len(days_nl) else 0
        rows.append((label, dp.mean(), tpd, p_nl, med, p_30))
    (bot.GAP_AWARE_SIZING_ENABLED, bot.STRONG_MAX_TRADES, bot.HEADROOM_SAFETY_FRAC) = orig

    print("\n" + "=" * 86)
    print("COMBINE SPEED vs SAFETY  (100k sims each)")
    print("=" * 86)
    print(f"{'Config':<24}{'Avg/day':>9}{'Trd/day':>8}{'P(pass)':>9}{'Median':>9}{'P(30d)':>8}{'ExpAttempts':>12}")
    print("-" * 86)
    for label, avg, tpd, p_nl, med, p_30 in rows:
        exp_att = 100.0 / p_nl if p_nl > 0 else float("inf")
        print(f"{label:<24}${avg:>7,.0f}{tpd:>8.2f}{p_nl:>8.1f}%{med:>8.0f}d{p_30:>7.1f}%{exp_att:>11.2f}x")
    print("=" * 86)
    print("ExpAttempts = expected combines to pass once = 100/P(pass) (lower is better).")
    print("Faster median days is worthless if P(pass) drops -- retries cost days AND reset fees.")


if __name__ == "__main__":
    run()
