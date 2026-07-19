"""
EUROPEAN-SESSION TEST — the frozen strategy, unchanged, on hours it has never
seen. Purest out-of-sample test possible: same rules, new data.

PRE-REGISTERED BAR (set before results; STRICTER than RTH because EU liquidity
is ~19% of RTH, so real-world slippage will be worse than modeled):
  n >= 100 trades / 7yr        (enough to judge)
  baseline PF >= 2.0           (RTH bar is ~1.5; demand more headroom here)
  slippage x3 PF >= 1.5 AND net > 0   (the real judge for a thin book)
  positive net in ALL 3 periods (dev/val1/val2)  (out-of-sample)
  worst trade >= -983          (no worse tail than the current live config)
If it clears ALL of these, it earns the FULL gauntlet + a live-latency handicap
study. If not, it's dead and logged.
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np
import pandas as pd

import src.bot as bot

EURO_CSV = os.path.join(os.path.dirname(__file__), "euro_5m.csv")


def M(trades):
    pnls = [t.pnl_usd for t in trades]
    if not pnls:
        return dict(n=0, net=0, pf=0, wr=0, worst=0, avg=0)
    wins = [p for p in pnls if p > 0]; losses = [p for p in pnls if p <= 0]
    return dict(n=len(pnls), net=sum(pnls),
                pf=(sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else float("inf")),
                wr=len(wins) / len(pnls) * 100, worst=min(pnls), avg=sum(pnls) / len(pnls))


def sl(tr, d0, d1):
    return M([t for t in tr if d0 <= str(t.date)[:10] <= d1])


def load_euro():
    df = pd.read_csv(EURO_CSV, index_col=0)
    df.index = pd.to_datetime(df.index, utc=True).tz_convert("US/Eastern")
    return df[["open", "high", "low", "close", "volume"]]


def run(df, **flags):
    saved = {k: getattr(bot, k) for k in flags}
    for k, v in flags.items():
        setattr(bot, k, v)
    try:
        di = bot.add_indicators(df.copy())
        di = bot.generate_signals(di)
        slv = bot.compute_session_levels(di)
        _, trades, _, _ = bot.run_backtest(di, slv)
    finally:
        for k, v in saved.items():
            setattr(bot, k, v)
    return trades


def main():
    df = load_euro()
    print(f"EU dataset: {len(df):,} bars, {str(df.index[0])[:10]} -> {str(df.index[-1])[:10]}")

    tr = run(df)
    m = M(tr)
    print("\n" + "=" * 70)
    print("EUROPEAN SESSION — frozen strategy, baseline fills")
    print("=" * 70)
    print(f"  trades {m['n']}  |  net ${m['net']:,.0f}  |  PF {m['pf']:.2f}  |  "
          f"WR {m['wr']:.1f}%  |  avg ${m['avg']:.0f}  |  worst ${m['worst']:,.0f}")
    print(f"  per day: {m['n']/df.index.normalize().nunique():.2f} trades/session")

    print("\n  by period (out-of-sample):")
    for name, d0, d1 in (("dev 19-22", "2019-01-01", "2022-12-31"),
                         ("val1 23-24", "2023-01-01", "2024-12-31"),
                         ("val2 25-26", "2025-01-01", "2026-12-31")):
        s = sl(tr, d0, d1)
        print(f"    {name:<12} net ${s['net']:>8,.0f} | PF {s['pf']:4.2f} | n {s['n']:>4} | WR {s['wr']:4.1f}%")

    print("\n  stress (thin book -> this is the real judge):")
    x3 = [(c, t * 3) for c, t in bot.SLIPPAGE_SCALE_TIERS]
    for lbl, m2 in (("slip x2", M(run(df, SLIPPAGE_SCALE_TIERS=[(c, t*2) for c, t in bot.SLIPPAGE_SCALE_TIERS]))),
                    ("slip x3", M(run(df, SLIPPAGE_SCALE_TIERS=x3)))):
        print(f"    {lbl:<8} net ${m2['net']:>8,.0f} | PF {m2['pf']:4.2f} | n {m2['n']}")

    # pre-registered verdict
    s3 = M(run(df, SLIPPAGE_SCALE_TIERS=x3))
    periods = [sl(tr, a, b) for a, b in (("2019-01-01","2022-12-31"),("2023-01-01","2024-12-31"),("2025-01-01","2026-12-31"))]
    checks = [
        ("n >= 100", m["n"] >= 100),
        ("baseline PF >= 2.0", m["pf"] >= 2.0),
        ("slip x3 PF >= 1.5 & net>0", s3["pf"] >= 1.5 and s3["net"] > 0),
        ("positive in all 3 periods", all(p["net"] > 0 for p in periods)),
        ("worst trade >= -983", m["worst"] >= -983),
    ]
    print("\n  --- PRE-REGISTERED BAR ---")
    for name, ok in checks:
        print(f"    {'PASS' if ok else 'FAIL'}  {name}")
    print("  VERDICT:", "SURVIVES -> earns full gauntlet" if all(o for _, o in checks) else "DEAD")


if __name__ == "__main__":
    main()
