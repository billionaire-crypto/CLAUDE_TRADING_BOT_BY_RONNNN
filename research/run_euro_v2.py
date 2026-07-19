"""
EUROPEAN SESSION v2 — overnight-safe DYNAMIC sizing via measured gap risk.

v1 verdict: edge REAL (PF>=2.15 in all 3 periods, survives slip x3, 1.76
trades/session) but worst trade -$2,249 breaches the combine DLL. Cause: the
sizing engine budgets each trade assuming a gap-through fills at most
GAP_STOP_MULT=1.5x the stop distance — calibrated on the thick RTH book. The
thin EU book gaps harder.

v2 approach (mechanism-fix, not a knob sweep, not a blanket size cap):
  1. MEASURE the actual gap-through severity in EU stop exits — but calibrate
     ONLY on the dev years (2019-2022). Val periods stay untouched judges.
  2. Set EU gap mult = dev-period worst overshoot ratio + 25% safety margin.
  3. Re-run with that ONE measured parameter. The existing risk-budget engine
     then sizes EVERY trade dynamically: wide-stop trades shrink, tight-stop
     trades keep size. No blanket restriction.
  4. Judge on the SAME pre-registered 5-gate bar as v1 (incl. worst >= -983).
     If val-period tails exceed the dev-calibrated model -> it fails honestly.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np
import pandas as pd

import src.bot as bot
from research.run_euro_test import M, sl, load_euro, run

def overshoot_ratios(trades, d0=None, d1=None):
    """For stop exits: how far beyond the stop did the fill land, as a multiple
    of the stop distance? ratio 1.0 = clean stop fill; >1 = gap-through."""
    out = []
    for t in trades:
        if "stop" not in str(t.exit_reason).lower():
            continue
        if d0 and not (d0 <= str(t.date)[:10] <= d1):
            continue
        stop_dist = abs(t.entry - t.stop_price)
        if stop_dist <= 0:
            continue
        beyond = (t.stop_price - t.exit) if t.direction == "long" else (t.exit - t.stop_price)
        out.append(1.0 + max(0.0, beyond) / stop_dist)
    return np.array(out)


def main():
    df = load_euro()
    ndays = df.index.normalize().nunique()

    print("step 1 — baseline run + measure EU gap violence (dev years only)")
    tr1 = run(df)
    r_dev = overshoot_ratios(tr1, "2019-01-01", "2022-12-31")
    r_all = overshoot_ratios(tr1)
    print(f"  stop exits dev: {len(r_dev)} | overshoot ratio p50 {np.percentile(r_dev,50):.2f} "
          f"p95 {np.percentile(r_dev,95):.2f} p99 {np.percentile(r_dev,99):.2f} max {r_dev.max():.2f}")
    print(f"  (all years, informational: p99 {np.percentile(r_all,99):.2f} max {r_all.max():.2f})")

    eu_mult = round(r_dev.max() * 1.25, 1)      # dev worst + 25% margin, pre-registered
    print(f"  -> EU_GAP_MULT = dev max x 1.25 = {eu_mult}  (RTH uses 1.5)")

    print("\nstep 2 — v2 run with measured gap mult (dynamic per-trade sizing)")
    tr2 = run(df, GAP_STOP_MULT=eu_mult)
    m1, m2 = M(tr1), M(tr2)
    print(f"  {'':12}{'v1 (RTH sizing)':>18}{'v2 (EU-safe)':>16}")
    for k, fmt in (("net", ",.0f"), ("pf", ".2f"), ("n", "d"), ("wr", ".1f"), ("avg", ".0f"), ("worst", ",.0f")):
        print(f"  {k:<12}{m1[k]:>18{fmt}}{m2[k]:>16{fmt}}")
    print(f"  trades/session: v1 {m1['n']/ndays:.2f} -> v2 {m2['n']/ndays:.2f}")

    print("\n  by period (v2):")
    for name, d0, d1 in (("dev 19-22", "2019-01-01", "2022-12-31"),
                         ("val1 23-24", "2023-01-01", "2024-12-31"),
                         ("val2 25-26", "2025-01-01", "2026-12-31")):
        s = sl(tr2, d0, d1)
        print(f"    {name:<12} net ${s['net']:>8,.0f} | PF {s['pf']:4.2f} | n {s['n']:>4} | worst ${s['worst']:>7,.0f}")

    x3 = [(c, t * 3) for c, t in bot.SLIPPAGE_SCALE_TIERS]
    s3 = M(run(df, GAP_STOP_MULT=eu_mult, SLIPPAGE_SCALE_TIERS=x3))
    print(f"\n  stress slip x3 (v2): net ${s3['net']:,.0f} | PF {s3['pf']:.2f} | worst ${s3['worst']:,.0f}")

    periods = [sl(tr2, a, b) for a, b in (("2019-01-01","2022-12-31"),("2023-01-01","2024-12-31"),("2025-01-01","2026-12-31"))]
    checks = [
        ("n >= 100", m2["n"] >= 100),
        ("baseline PF >= 2.0", m2["pf"] >= 2.0),
        ("slip x3 PF >= 1.5 & net>0", s3["pf"] >= 1.5 and s3["net"] > 0),
        ("positive in all 3 periods", all(p["net"] > 0 for p in periods)),
        ("worst trade >= -983 (incl. stress)", m2["worst"] >= -983 and s3["worst"] >= -983),
    ]
    print("\n  --- SAME PRE-REGISTERED BAR AS v1 ---")
    for name, ok in checks:
        print(f"    {'PASS' if ok else 'FAIL'}  {name}")
    print("  VERDICT:", "SURVIVES -> earns full gauntlet + combine sim" if all(o for _, o in checks) else "DEAD")

    # informational: where do the remaining big losers live (hour of day)?
    worst20 = sorted(tr2, key=lambda t: t.pnl_usd)[:20]
    hrs = pd.Series([t.entry_hour for t in worst20]).value_counts().sort_index()
    print("\n  20 worst v2 trades by entry hour (CT):")
    print("  " + "  ".join(f"{h:02d}h:{c}" for h, c in hrs.items()))


if __name__ == "__main__":
    main()
