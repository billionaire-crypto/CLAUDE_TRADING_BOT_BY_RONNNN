"""
Random slippage Monte Carlo for the V29 FVG strategy.

Runs the full backtest 200 times, each time drawing a random slippage
level from a realistic distribution. Shows the range of outcomes so you
know the best-case, typical-case, and worst-case P&L under live conditions.

Slippage distribution used:
  40% of runs: 1 tick/side  (clean fills, normal market)
  35% of runs: 2 ticks/side (slightly fast market)
  20% of runs: 3 ticks/side (volatile / FVG fill conditions)
   5% of runs: 5 ticks/side (worst-case extreme slippage)
"""
import numpy as np
import src.bot as bot


SLIP_LEVELS = [1.0, 2.0, 3.0, 5.0]
SLIP_WEIGHTS = [0.40, 0.35, 0.20, 0.05]
N_RUNS = 200


def run_random_slippage():
    print(f"Loading data and running {N_RUNS} random-slippage backtests...")
    print("Slippage distribution: 1tk=40%  2tk=35%  3tk=20%  5tk=5%")
    print()

    df_raw = bot.fetch_data()
    bot.validate_loaded_data(df_raw)
    df_ind = bot.add_indicators(df_raw)
    sl     = bot.compute_session_levels(df_ind)
    df_sig = bot.generate_signals(df_ind.copy())

    np.random.seed(42)
    slip_draws = np.random.choice(SLIP_LEVELS, size=N_RUNS, p=SLIP_WEIGHTS)

    net_pnls, win_rates, profit_factors, sharpes = [], [], [], []

    original_scale = bot.SLIPPAGE_SCALE_ENABLED
    original_slip  = bot.SLIPPAGE_TICKS

    try:
        bot.SLIPPAGE_SCALE_ENABLED = False  # use flat slip for each run
        for i, slip in enumerate(slip_draws):
            bot.SLIPPAGE_TICKS = float(slip)
            _, trades, _, _ = bot.run_backtest(df_sig, sl)
            if not trades:
                continue
            pnls   = [t.pnl_usd for t in trades]
            wins   = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]
            net    = sum(pnls)
            wr     = len(wins) / len(pnls) * 100
            pf     = sum(wins) / abs(sum(losses)) if losses else 0.0

            from collections import defaultdict
            day_map = defaultdict(float)
            for t in trades:
                day_map[str(t.date)[:10]] += t.pnl_usd
            daily = list(day_map.values())
            if len(daily) > 1:
                arr = np.array(daily)
                sharpe = arr.mean() / arr.std(ddof=1) * np.sqrt(252)
            else:
                sharpe = 0.0

            net_pnls.append(net)
            win_rates.append(wr)
            profit_factors.append(pf)
            sharpes.append(sharpe)

            if (i + 1) % 50 == 0:
                print(f"  {i+1}/{N_RUNS} runs complete...")
    finally:
        bot.SLIPPAGE_SCALE_ENABLED = original_scale
        bot.SLIPPAGE_TICKS         = original_slip

    net_arr = np.array(net_pnls)
    wr_arr  = np.array(win_rates)
    pf_arr  = np.array(profit_factors)
    sh_arr  = np.array(sharpes)

    print()
    print("=" * 60)
    print("RANDOM SLIPPAGE RESULTS (200 runs)")
    print("=" * 60)
    print(f"{'':20} {'Net P&L':>10} {'Win Rate':>9} {'Prof Factor':>12} {'Sharpe':>7}")
    print("-" * 60)
    print(f"{'Best case (95th pct)':20} ${np.percentile(net_arr,95):>9,.0f} "
          f"{np.percentile(wr_arr,95):>8.1f}% {np.percentile(pf_arr,95):>11.2f} "
          f"{np.percentile(sh_arr,95):>7.2f}")
    print(f"{'Good case (75th pct)':20} ${np.percentile(net_arr,75):>9,.0f} "
          f"{np.percentile(wr_arr,75):>8.1f}% {np.percentile(pf_arr,75):>11.2f} "
          f"{np.percentile(sh_arr,75):>7.2f}")
    print(f"{'Median (50th pct)':20} ${np.percentile(net_arr,50):>9,.0f} "
          f"{np.percentile(wr_arr,50):>8.1f}% {np.percentile(pf_arr,50):>11.2f} "
          f"{np.percentile(sh_arr,50):>7.2f}")
    print(f"{'Bad case (25th pct)':20} ${np.percentile(net_arr,25):>9,.0f} "
          f"{np.percentile(wr_arr,25):>8.1f}% {np.percentile(pf_arr,25):>11.2f} "
          f"{np.percentile(sh_arr,25):>7.2f}")
    print(f"{'Worst case (5th pct)':20} ${np.percentile(net_arr,5):>9,.0f} "
          f"{np.percentile(wr_arr,5):>8.1f}% {np.percentile(pf_arr,5):>11.2f} "
          f"{np.percentile(sh_arr,5):>7.2f}")
    print("=" * 60)
    print(f"{'Mean across all runs':20} ${net_arr.mean():>9,.0f} "
          f"{wr_arr.mean():>8.1f}% {pf_arr.mean():>11.2f} {sh_arr.mean():>7.2f}")
    print()
    pct_positive = (net_arr > 0).mean() * 100
    print(f"  Profitable runs: {pct_positive:.1f}% of {N_RUNS}")
    print(f"  P&L range: ${net_arr.min():,.0f}  to  ${net_arr.max():,.0f}")
    print()


if __name__ == "__main__":
    run_random_slippage()
