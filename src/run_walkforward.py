"""
Walk-forward validation for the FVG strategy.

Rolls a 4-year in-sample window forward by 1 year and reports the
out-of-sample (OOS) result for each step. No parameters are re-tuned
between windows — the same bot config is used throughout.

If the OOS/IS net ratio stays above ~0.70 across all windows, the edge is
genuine. If it collapses, the strategy is curve-fitted to specific years.

Windows (each IS = 4 yr, OOS = 1 yr):
  1. IS 2019–2022  →  OOS 2023
  2. IS 2020–2023  →  OOS 2024
  3. IS 2021–2024  →  OOS 2025
"""
import io
import sys

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import src.bot as bot

WINDOWS = [
    ("2019-05-06", "2022-12-31", "2023-01-01", "2023-12-31"),
    ("2020-01-01", "2023-12-31", "2024-01-01", "2024-12-31"),
    ("2021-01-01", "2024-12-31", "2025-01-01", "2025-12-31"),
]


def _run_period(df_indicators, sl_full, start, end):
    df_slice = df_indicators[
        (df_indicators.index >= start) & (df_indicators.index <= end)
    ].copy()
    dates = set(df_slice.index.normalize())
    sl = {d: v for d, v in sl_full.items() if d in dates}
    df_sig = bot.generate_signals(df_slice)
    _, trades, daily, _ = bot.run_backtest(df_sig, sl)
    fvg = [t for t in trades if t.entry_type == "FVG"]
    n   = len(fvg)
    wr  = sum(1 for t in fvg if t.won) / n * 100 if n else 0
    net = sum(t.pnl_usd for t in fvg)
    years = max((df_slice.index[-1] - df_slice.index[0]).days / 365.25, 0.01)
    # basic Sharpe from daily returns
    import numpy as np
    dpnl = [d.daily_pnl_net for d in daily]
    sharpe = (np.mean(dpnl) / np.std(dpnl) * (252 ** 0.5)) if np.std(dpnl) > 0 else 0
    return {"n": n, "wr": wr, "net": net, "net_yr": net / years, "sharpe": sharpe, "years": years}


def main():
    print("=" * 68)
    print("  WALK-FORWARD VALIDATION  —  4-year IS → 1-year OOS rolling")
    print("  No re-tuning between windows. Same bot config throughout.")
    print("=" * 68)

    print("\nLoading data...")
    df_raw = bot.fetch_data()
    bot.validate_loaded_data(df_raw)
    df_ind = bot.add_indicators(df_raw)
    sl_full = bot.compute_session_levels(df_ind)

    # Disable non-FVG strategies for a clean FVG-only walk-forward
    bot.HTF_FVG_ENABLED = False
    bot.SWEEP_REV_ENABLED = False
    bot.VPOC_ENABLED = False

    results = []
    for is_start, is_end, oos_start, oos_end in WINDOWS:
        is_r  = _run_period(df_ind, sl_full, is_start,  is_end)
        oos_r = _run_period(df_ind, sl_full, oos_start, oos_end)
        results.append((is_start, is_end, oos_start, oos_end, is_r, oos_r))

    print(f"\n  {'Window':<22} {'Period':<10} {'Trades':>7} {'WR':>6} {'Net/yr':>10} {'Sharpe':>7}  OOS/IS")
    print("  " + "-" * 66)

    for is_s, is_e, oos_s, oos_e, is_r, oos_r in results:
        ratio = oos_r["net_yr"] / is_r["net_yr"] if is_r["net_yr"] > 0 else float("nan")
        label = f"{is_s[:4]}–{is_e[:4]}"
        print(f"  {label:<22} {'IS':<10} {is_r['n']:>7} {is_r['wr']:>5.1f}% "
              f"${is_r['net_yr']:>9,.0f} {is_r['sharpe']:>7.2f}")
        print(f"  {'':22} {'OOS '+oos_s[:4]:<10} {oos_r['n']:>7} {oos_r['wr']:>5.1f}% "
              f"${oos_r['net_yr']:>9,.0f} {oos_r['sharpe']:>7.2f}  {ratio:.2f}")
        print()

    oos_nets = [oos_r["net_yr"] for _, _, _, _, _, oos_r in results]
    is_nets  = [is_r["net_yr"]  for _, _, _, _, is_r, _ in results]
    avg_ratio = sum(o / i for o, i in zip(oos_nets, is_nets) if i > 0) / len(results)

    print("  " + "=" * 66)
    print(f"  Mean OOS/IS net ratio: {avg_ratio:.2f}")
    if avg_ratio >= 0.80:
        verdict = "STRONG — edge holds robustly out-of-sample"
    elif avg_ratio >= 0.60:
        verdict = "ACCEPTABLE — some decay but edge persists"
    else:
        verdict = "WARNING — significant decay; possible curve-fit"
    print(f"  Verdict: {verdict}")
    print("  " + "=" * 66)
    print("\n  What this means:")
    print("  OOS/IS ratio = 1.0 means OOS earns as much as IS (no decay).")
    print("  Ratio = 0.70 means OOS earns 70% of IS (mild, normal decay).")
    print("  Ratio < 0.50 means the edge mostly came from memorizing IS data.")


if __name__ == "__main__":
    main()
