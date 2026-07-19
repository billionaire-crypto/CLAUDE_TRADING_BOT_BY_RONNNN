"""
CANDIDATE B2 — Shallow-Pullback Continuation (SPC). Dev-stage gauntlet harness.

Structural difference vs the dead TDC v1: TDC bought the EXTENSION (entering
with the drift at the day's stretched extreme — PF 0.84, dead). SPC buys a
DIP TO FAIR VALUE: in an established trend, price dips to VWAP without
reaching the deeper FVG zones, holds it (close stays trend-side), and we
enter on the resumption. Better entry price, structural anchor.

FROZEN SPEC v1 (pre-registered before any results — do not tune):
  Arming window : entries 10:30-14:00 CT (same as TDC, comparability)
  Conditions (completed bars only; enter at next bar OPEN, core convention):
    1. CORE IDLE  — no core FVG trade entered earlier that session (causal)
    2. TREND      — >=70% of today's completed bars closed on ONE side of
                    VWAP (that side = direction)
    3. SHALLOW PULLBACK TRIGGER — previous bar's LOW touched/crossed VWAP
                    (low <= vwap) while its CLOSE held the trend side
                    (close > vwap). Mirror for shorts.
    4. REGIME     — get_risk_profile(prev)["contracts"] > 0 (same gate as core)
  Entry/exits   : identical machinery to TDC (imported, not re-implemented):
                  3 contracts, stop 1.5xATR clamp [8,40]pts, target = core ATR
                  machinery [15,60]pts, breakeven via bot.desired_stop_price,
                  honest gap-through, stop-first same bar, EOD exit, tiered
                  costs. Max 1 SPC trade/day.

KILL CRITERIA (dev 2019-2022, identical to TDC, pre-registered):
  n < 15 | PF < 1.5 | worst trade < -$600 | one year > 60% of |net|  -> DEAD

Run:  python -X utf8 -m research.run_spc_candidate            (dev)
      python -X utf8 -m research.run_spc_candidate val1|val2  (only after dev verdict)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pandas as pd

import src.bot as b
from research.run_tdc_candidate import (PERIODS, ARM_START, ARM_END, CONTRACTS,
                                        core_entry_times, simulate)

TREND_FRAC = 0.70   # pre-registered; looser than TDC's 0.80 because a dip-to-VWAP
                    # day is by definition less one-sided than a pure drift day


def main():
    period = sys.argv[1] if len(sys.argv) > 1 else "dev"
    start, end = PERIODS[period]

    df = b.fetch_data()
    df = b.add_indicators(df)
    df = b.generate_signals(df)
    lv = b.compute_session_levels(df)
    core_first = core_entry_times(df, lv)

    df = df.loc[start:end]
    cost = b.round_turn_cost(CONTRACTS)
    trades = []
    armed_days = 0

    for day, ddf in df.groupby(df.index.normalize()):
        if len(ddf) < 12:
            continue
        dkey = str(day)[:10]
        core_min = core_first.get(dkey)
        above = (ddf["close"] > ddf["vwap"]).values
        times = [ts.hour * 60 + ts.minute for ts in ddf.index]
        arm_lo, arm_hi = 10 * 60 + 30, 14 * 60
        took = False
        for i in range(3, len(ddf)):
            if took or times[i] < arm_lo:
                continue
            if times[i] > arm_hi:
                break
            if core_min is not None and core_min <= times[i]:
                break                                    # core active today
            frac = above[:i].mean()
            prev = ddf.iloc[i - 1]
            if frac >= TREND_FRAC and prev["low"] <= prev["vwap"] < prev["close"]:
                direction = "long"                       # dip to VWAP, held it
            elif frac <= (1 - TREND_FRAC) and prev["high"] >= prev["vwap"] > prev["close"]:
                direction = "short"
            else:
                continue
            if b.get_risk_profile(prev)["contracts"] <= 0:
                continue
            armed_days += 1
            pnl_pts, reason = simulate(ddf, direction, i)
            usd = pnl_pts * b.MNQ_POINT_VALUE * CONTRACTS - cost
            trades.append({"date": dkey, "year": dkey[:4], "dir": direction,
                           "pnl_pts": pnl_pts, "usd": usd, "reason": reason})
            took = True

    t = pd.DataFrame(trades)
    print("=" * 70)
    print(f"SPC CANDIDATE B2 — period={period} ({start} -> {end})  spec=FROZEN v1")
    print("=" * 70)
    if t.empty:
        print("0 trades. DEAD at sample-size gate.")
        return
    wins, losses = t[t.usd > 0], t[t.usd <= 0]
    pf = wins.usd.sum() / abs(losses.usd.sum()) if len(losses) and losses.usd.sum() != 0 else float("inf")
    yrs = max(1.0, (pd.Timestamp(end) - pd.Timestamp(start)).days / 365.25)
    print(f"trades: {len(t)}   WR: {len(wins)/len(t)*100:.1f}%   PF: {pf:.2f}")
    print(f"net: ${t.usd.sum():,.0f}   avg: ${t.usd.mean():,.0f}   worst: ${t.usd.min():,.0f}   "
          f"best: ${t.usd.max():,.0f}   ~${t.usd.sum()/yrs:,.0f}/yr")
    print(f"exits: {t.reason.value_counts().to_dict()}")
    print("\nby year:")
    for y, g in t.groupby("year"):
        share = g.usd.sum() / t.usd.sum() * 100 if t.usd.sum() != 0 else 0
        print(f"  {y}: n={len(g):>3}  net ${g.usd.sum():>8,.0f}  ({share:.0f}% of net)  "
              f"WR {len(g[g.usd>0])/len(g)*100:.0f}%")
    print("\n--- KILL CRITERIA ---")
    year_max = max(abs(g.usd.sum()) / abs(t.usd.sum()) for _, g in t.groupby("year")) if t.usd.sum() != 0 else 1
    checks = [("n >= 15", len(t) >= 15), ("PF >= 1.5", pf >= 1.5),
              ("worst trade <= $600 risk", t.usd.min() >= -600),
              ("no year > 60% of |net|", year_max <= 0.60)]
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print("VERDICT:", "SURVIVES this gate" if all(ok for _, ok in checks) else "DEAD")
    out = os.path.join(os.path.dirname(__file__), f"spc_{period}_trades.csv")
    t.to_csv(out, index=False)
    print(f"trades -> {out}")


if __name__ == "__main__":
    main()
