"""
Generic gradient sweep — walk any single bot.py constant through the
pre-registered gauntlet and print the SHAPE. Reuses nightly_researcher's exact
verdict rules. src/bot.py is never written (in-memory setattr + restore).

Usage:
  python -X utf8 -m research.run_param_sweep STRONG_MAX_TRADES 5 6
  python -X utf8 -m research.run_param_sweep FVG_MAX_AGE_BARS 5 6 7
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np

import src.bot as bot
from research.run_combine_sim import _simulate
from research.nightly_researcher import _metrics, _slice, decide

PERIODS = [("dev_2019_2022", "2019-01-01", "2022-12-31"),
           ("val1_2023_2024", "2023-01-01", "2024-12-31"),
           ("val2_2025_2026", "2025-01-01", "2026-12-31")]


def main():
    param = sys.argv[1]
    candidates = [int(x) if x.lstrip("-").isdigit() else float(x) for x in sys.argv[2:]]

    df = bot.fetch_data(); bot.validate_loaded_data(df)
    di = bot.add_indicators(df); sl = bot.compute_session_levels(di)
    base_val = getattr(bot, param)

    def backtest(value, **stress):
        saved = {param: getattr(bot, param)}
        for k in stress:
            saved[k] = getattr(bot, k)
        setattr(bot, param, value)
        for k, v in stress.items():
            setattr(bot, k, v)
        try:
            ds = bot.generate_signals(di.copy())
            _, trades, days, _ = bot.run_backtest(ds, sl)
        finally:
            for k, v in saved.items():
                setattr(bot, k, v)
        return trades, days

    def combine(days):
        dp = np.array([d.daily_pnl_net for d in days])
        ip = np.array([d.max_intraday_peak for d in days])
        qf = np.array([d.is_qualifying_day for d in days], dtype=bool)
        p, _, fails = _simulate(dp, ip, qf, None)
        return {"pass_rate": p, "daily_limit_fail_pct": fails["daily_limit"] / 1000}

    print(f"baseline: {param} = {base_val}")
    tr_base, dr_base = backtest(base_val)
    m_base = _metrics(tr_base)
    combine_base = combine(dr_base)
    x3 = [(c, t * 3) for c, t in bot.SLIPPAGE_SCALE_TIERS]

    print("=" * 92)
    print(f"{'val':>5}{'net':>12}{'PF':>6}{'n':>7}{'WR':>7}{'worst':>9}"
          f"{'slipx3 PF':>11}{'combine%':>10}{'verdict':>9}")
    print("=" * 92)
    print(f"{str(base_val):>5}{m_base['net']:>12,.0f}{m_base['pf']:>6.2f}{m_base['n']:>7}"
          f"{m_base['wr']:>6.1f}%{m_base['worst']:>9,.0f}{'--':>11}"
          f"{combine_base['pass_rate']:>9.1f}%{'BASELINE':>9}")

    for val in candidates:
        tr, dr = backtest(val)
        m = _metrics(tr)
        s3, _ = backtest(val, SLIPPAGE_SCALE_TIERS=x3)
        s3m = _metrics(s3)
        cc = combine(dr)
        g = {"split": {name: {"base": _slice(tr_base, d0, d1), "cand": _slice(tr, d0, d1)}
                       for name, d0, d1 in PERIODS},
             "stress": {"slip_x3": s3m}, "combine_base": combine_base, "combine_cand": cc,
             "worst_trade_base": m_base["worst"], "worst_trade_cand": m["worst"]}
        verdict, reasons = decide(g)
        print(f"{str(val):>5}{m['net']:>12,.0f}{m['pf']:>6.2f}{m['n']:>7}"
              f"{m['wr']:>6.1f}%{m['worst']:>9,.0f}{s3m['pf']:>11.2f}"
              f"{cc['pass_rate']:>9.1f}%{verdict:>9}")
        if reasons:
            print(f"      -> {'; '.join(reasons)}")
    print("=" * 92)
    print("Monotonic improve-then-flatten = real; jagged = noise. Combine pass rate is the")
    print("objective for a combine account, NOT raw net P&L.")


if __name__ == "__main__":
    main()
