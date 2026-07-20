"""
FVG_MAX_AGE_BARS gradient walk — 5, 6, 7 through the pre-registered gauntlet.

Decision rule (unchanged): ship age 5 ONLY if it passes. 6 and 7 are shown to
reveal the CURVE SHAPE (smooth hill = real; jagged spike = noise), NOT to
cherry-pick a winner. Baseline (current age 4) computed once and reused.

Reuses nightly_researcher's exact verdict logic (decide) and metrics so this
sweep and the automated nightly runs are identical in rules.
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
PARAM = "FVG_MAX_AGE_BARS"
CANDIDATES = [5, 6, 7]


def main():
    df = bot.fetch_data(); bot.validate_loaded_data(df)
    di = bot.add_indicators(df); sl = bot.compute_session_levels(di)
    base_age = getattr(bot, PARAM)

    def backtest(value, **stress):
        saved = {PARAM: getattr(bot, PARAM)}
        for k in stress:
            saved[k] = getattr(bot, k)
        setattr(bot, PARAM, value)
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

    print(f"baseline: {PARAM} = {base_age}")
    tr_base, dr_base = backtest(base_age)
    m_base = _metrics(tr_base)
    combine_base = combine(dr_base)
    x3 = [(c, t * 3) for c, t in bot.SLIPPAGE_SCALE_TIERS]

    print("=" * 92)
    print(f"{'age':>4}{'net':>12}{'PF':>6}{'n':>7}{'WR':>7}{'worst':>9}"
          f"{'slipx3 PF':>11}{'combine%':>10}{'verdict':>9}")
    print("=" * 92)
    # baseline row
    print(f"{base_age:>4}{m_base['net']:>12,.0f}{m_base['pf']:>6.2f}{m_base['n']:>7}"
          f"{m_base['wr']:>6.1f}%{m_base['worst']:>9,.0f}{'--':>11}"
          f"{combine_base['pass_rate']:>9.1f}%{'BASELINE':>9}")

    for age in CANDIDATES:
        tr, dr = backtest(age)
        m = _metrics(tr)
        s3, _ = backtest(age, SLIPPAGE_SCALE_TIERS=x3)
        s3m = _metrics(s3)
        cc = combine(dr)
        g = {"split": {name: {"base": _slice(tr_base, d0, d1), "cand": _slice(tr, d0, d1)}
                       for name, d0, d1 in PERIODS},
             "stress": {"slip_x3": s3m},
             "combine_base": combine_base, "combine_cand": cc,
             "worst_trade_base": m_base["worst"], "worst_trade_cand": m["worst"]}
        verdict, reasons = decide(g)
        print(f"{age:>4}{m['net']:>12,.0f}{m['pf']:>6.2f}{m['n']:>7}"
              f"{m['wr']:>6.1f}%{m['worst']:>9,.0f}{s3m['pf']:>11.2f}"
              f"{cc['pass_rate']:>9.1f}%{verdict:>9}")
        if reasons:
            print(f"      -> {'; '.join(reasons)}")

    print("=" * 92)
    print("Read the SHAPE: monotonic improve-then-flatten = real edge; jagged = noise.")
    print("Anchor: age 12 is a known corpse (worst trade -$5,037). The cliff is between here and there.")


if __name__ == "__main__":
    main()
