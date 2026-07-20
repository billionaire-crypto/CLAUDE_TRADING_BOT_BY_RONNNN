"""
Topstep $50k combine simulator.

Uses bootstrap resampling of actual backtest daily returns to estimate the
probability of passing under three time-limit scenarios.

Combine rules (TopstepX $50k):
  Profit target          $3,000  (equity reaches $53,000)
  Daily loss limit       -$1,000 (bot's internal brake is -$750, so rarely hit)
  Trailing max drawdown  -$2,000 from highest intraday equity peak
  Min qualifying days    5 days with at least $150 profit
  Time limit             varies by plan (simulated below at None / 30 / 60 days)

A "trading day" is a day the market is open.
30 trading days  ~= 6 calendar weeks
60 trading days  ~= 3 calendar months
"""
import io
import sys

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import numpy as np
import src.bot as bot

N_SIMS  = 100_000
RNG     = np.random.default_rng(seed=42)

PROFIT_TARGET   = bot.COMBINE_PROFIT_TARGET      # $3,000
DAILY_LIMIT     = bot.BOT_DAILY_LOSS_LIMIT       # -$750  (bot's real brake)
TRAIL_DD        = bot.EOD_LOSS_BUFFER            # $2,000
QUAL_MIN        = bot.XFA_QUALIFYING_DAY_MIN     # $150
QUAL_NEEDED     = bot.XFA_QUALIFYING_DAYS_NEEDED # 5
INIT            = bot.INIT_CASH                  # $50,000


def _simulate(daily_pnls, intraday_peaks, qual_flags, max_days):
    """
    Run N_SIMS combine attempts by bootstrapping from real daily outcomes.

    daily_pnls     : array of EOD daily net P&L values from backtest
    intraday_peaks : array of max intraday equity GAIN for each day
    qual_flags     : bool array — True when that day was a qualifying day
    max_days       : int or None — trading-day time limit (None = unlimited)
    """
    n = len(daily_pnls)
    passes = 0
    fails = {"daily_limit": 0, "trail_dd": 0, "time": 0}
    days_taken = []

    for _ in range(N_SIMS):
        equity      = float(INIT)
        peak_equity = float(INIT)   # tracks highest intraday equity seen so far
        qual_days   = 0
        day_num     = 0

        while True:
            idx = int(RNG.integers(0, n))
            pnl         = float(daily_pnls[idx])
            peak_gain   = float(intraday_peaks[idx])
            is_qual     = bool(qual_flags[idx])
            day_num    += 1

            # Intraday peak could be higher than EOD — update peak first
            intraday_hi = equity + peak_gain
            if intraday_hi > peak_equity:
                peak_equity = intraday_hi

            equity += pnl
            if is_qual:
                qual_days += 1

            # Check failure: trailing drawdown (uses intraday peak, like Topstep does)
            if equity < peak_equity - TRAIL_DD:
                fails["trail_dd"] += 1
                break

            # Check failure: bot's internal daily brake triggered
            if pnl < DAILY_LIMIT:
                fails["daily_limit"] += 1
                break

            # Check pass: profit target + qualifying days
            if equity - INIT >= PROFIT_TARGET and qual_days >= QUAL_NEEDED:
                passes += 1
                days_taken.append(day_num)
                break

            # Check failure: time limit expired
            if max_days is not None and day_num >= max_days:
                fails["time"] += 1
                break

    p_pass = passes / N_SIMS * 100
    return p_pass, days_taken, fails


def main():
    # ── Load and run full 7-year backtest once ─────────────────────────────────
    print("Loading data and running backtest...")
    df_raw = bot.fetch_data()
    bot.validate_loaded_data(df_raw)
    df_ind = bot.add_indicators(df_raw)
    sl     = bot.compute_session_levels(df_ind)
    df_sig = bot.generate_signals(df_ind.copy())
    _, trades, daily_records, state = bot.run_backtest(df_sig, sl)

    print(f"  {len(daily_records)} trading days  |  {len(trades)} trades\n")

    # Build arrays from actual backtest daily records
    daily_pnls     = np.array([d.daily_pnl_net      for d in daily_records], dtype=float)
    intraday_peaks = np.array([d.max_intraday_peak   for d in daily_records], dtype=float)
    qual_flags     = np.array([d.is_qualifying_day   for d in daily_records], dtype=bool)

    # Summary of the raw daily distribution
    positive = daily_pnls[daily_pnls > 0]
    negative = daily_pnls[daily_pnls < 0]
    print("  Daily P&L distribution (from backtest):")
    print(f"    Green days : {len(positive):>4}  avg +${np.mean(positive):>7,.0f}  best  +${np.max(positive):>7,.0f}")
    print(f"    Red days   : {len(negative):>4}  avg  ${np.mean(negative):>7,.0f}  worst  ${np.min(negative):>7,.0f}")
    print(f"    Flat days  : {sum(daily_pnls == 0):>4}")
    print(f"    Qual days  : {qual_flags.sum():>4}  ({qual_flags.mean()*100:.0f}% of all days)")
    print(f"    Avg trades/day: {len(trades)/len(daily_records):.2f}\n")

    # ── Run three scenarios ────────────────────────────────────────────────────
    scenarios = [
        ("No time limit  (TopstepX style)", None),
        ("30 trading days (~6 weeks)",       30),
        ("60 trading days (~3 months)",      60),
    ]

    print("=" * 62)
    print("  COMBINE SIMULATION  —  100,000 runs each")
    print(f"  Target: +${PROFIT_TARGET:,.0f}  |  Daily brake: ${DAILY_LIMIT:,.0f}  "
          f"|  Trail DD: ${TRAIL_DD:,.0f}  |  Qual days: {QUAL_NEEDED}")
    print("=" * 62)

    for label, max_days in scenarios:
        p_pass, days_taken, fails = _simulate(
            daily_pnls, intraday_peaks, qual_flags, max_days
        )
        n_passed = len(days_taken)
        print(f"\n  Scenario: {label}")
        print(f"    P(pass)          : {p_pass:.1f}%")
        if n_passed > 0:
            arr = np.array(days_taken)
            print(f"    Median days      : {np.median(arr):.0f} trading days  "
                  f"(~{np.median(arr)*7/5:.0f} calendar days)")
            print(f"    25th pct         : {np.percentile(arr,25):.0f} days")
            print(f"    75th pct         : {np.percentile(arr,75):.0f} days")
            print(f"    Fastest 10%      : {np.percentile(arr,10):.0f} days")
        fail_total = sum(fails.values())
        if fail_total > 0:
            print(f"    Fail: trail DD   : {fails['trail_dd']/N_SIMS*100:.1f}%")
            print(f"    Fail: daily limit: {fails['daily_limit']/N_SIMS*100:.1f}%")
            if max_days:
                print(f"    Fail: time limit : {fails['time']/N_SIMS*100:.1f}%")

    print("\n" + "=" * 62)
    print("  What the numbers mean:")
    print("    P(pass) = % of attempts that hit the profit target")
    print("              before breaching any limit.")
    print("    Trail DD failure = cumulative equity fell $2,000 from")
    print("              its highest intraday peak during the combine.")
    print("=" * 62)


if __name__ == "__main__":
    main()
