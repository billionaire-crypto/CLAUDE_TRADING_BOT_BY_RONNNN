"""
CANDIDATE A — Trend-Drift Continuation (TDC). Dev-stage gauntlet harness.

FROZEN SPEC v1 (pre-registered before any results were seen — do not tune):
  Arming window : entries 10:30-14:00 CT only
  Conditions    : all evaluated on completed bars (enter at next bar OPEN,
                  same convention as the core engine):
    1. CORE IDLE   — no core FVG trade entered earlier that session (causal)
    2. DRIFT       — >=80% of today's completed bars closed on ONE side of
                     VWAP (that side = direction) AND prev close on that side
    3. REGIME      — get_risk_profile(prev)["contracts"] > 0 (same gate as core)
  Entry         : market at bar open, drift direction, max 1 TDC trade/day
  Size          : 3 contracts fixed
  Stop          : 1.5 x ATR(prev), clamped [8, 40] pts   (own choice, no core
                  analogue — sensitivity MUST be reported, see --sweep-stop)
  Target        : core's exact ATR machinery: 2.0 x ATR clamped [15, 60] pts
  Stop mgmt     : bot.desired_stop_price (breakeven @20 ticks, trail OFF) — parity
  Exits         : honest gap-through (open beyond stop -> fill at open),
                  stop-before-target if both hit same bar (conservative),
                  EOD exit at last bar close
  Costs         : bot.round_turn_cost(3) (tiered slippage + commission)

KILL CRITERIA (dev 2019-2022, pre-registered):
  n < 15 trades          -> DEAD (too thin)
  PF < 1.5               -> DEAD
  worst trade > $600     -> DEAD (floor safety at 3c)
  one year > 60% of net  -> DEAD (fragile)

Run:  python -X utf8 -m research.run_tdc_candidate            (dev period)
      python -X utf8 -m research.run_tdc_candidate val1       (2023-2024, only after dev verdict)
      python -X utf8 -m research.run_tdc_candidate val2       (2025-2026, only after val1)
"""
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np
import pandas as pd

import src.bot as b

PERIODS = {
    "dev":  ("2019-01-01", "2022-12-31"),
    "val1": ("2023-01-01", "2024-12-31"),
    "val2": ("2025-01-01", "2026-12-31"),
}

# frozen spec constants
ARM_START, ARM_END = "10:30", "14:00"
DRIFT_FRAC = 0.80
CONTRACTS = 3
STOP_ATR_MULT = 1.5
STOP_MIN_PTS, STOP_MAX_PTS = 8.0, 40.0
TGT_MULT = b.ATR_TARGET_MULT                                  # 2.0 (core)
TGT_MIN_PTS = b.ATR_TARGET_MIN_TICKS * b.MNQ_TICK_SIZE        # 15 (core)
TGT_MAX_PTS = b.ATR_TARGET_MAX_TICKS * b.MNQ_TICK_SIZE        # 60 (core)


def core_entry_times(df, lv):
    """Run the core once; map session date -> earliest FVG entry minute-of-day."""
    _stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        _, trades, _, _ = b.run_backtest(df, lv)
    finally:
        sys.stdout = _stdout
    first = {}
    for t in trades:
        if t.entry_type != "FVG":
            continue
        d = str(t.date)[:10]
        m = int(t.entry_hour) * 60 + (int(t.session_minute) % 60 if t.session_minute else 0)
        # entry_hour is CT hour; minute-of-day is enough resolution for causality
        first[d] = min(first.get(d, 10 ** 6), m)
    return first


def simulate(day_df, direction, entry_i):
    """Simulate one TDC trade from bar entry_i to exit. Returns (pnl_pts, exit_reason)."""
    prev = day_df.iloc[entry_i - 1]
    entry = float(day_df.iloc[entry_i]["open"])
    atr = float(prev["atr"])
    stop_d = min(max(STOP_ATR_MULT * atr, STOP_MIN_PTS), STOP_MAX_PTS)
    tgt_d = min(max(TGT_MULT * atr, TGT_MIN_PTS), TGT_MAX_PTS)
    sgn = 1.0 if direction == "long" else -1.0
    stop = entry - sgn * stop_d
    target = entry + sgn * tgt_d

    mfe_prev = 0.0
    for j in range(entry_i, len(day_df)):
        bar = day_df.iloc[j]
        o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
        if j > entry_i:
            stop = b.desired_stop_price(direction, entry, stop, mfe_prev)  # parity
            # honest gap-through: bar opens beyond the stop -> filled at open
            if (direction == "long" and o <= stop) or (direction == "short" and o >= stop):
                return sgn * (o - entry), "stop_gap"
        # intrabar: conservative stop-first
        if (direction == "long" and l <= stop) or (direction == "short" and h >= stop):
            return sgn * (stop - entry), "stop"
        if (direction == "long" and h >= target) or (direction == "short" and l <= target):
            return sgn * (target - entry), "target"
        fav = (h - entry) if direction == "long" else (entry - l)
        mfe_prev = max(mfe_prev, fav)
    last = float(day_df.iloc[-1]["close"])
    return sgn * (last - entry), "eod"


def main():
    period = sys.argv[1] if len(sys.argv) > 1 else "dev"
    start, end = PERIODS[period]

    df = b.fetch_data()
    df = b.add_indicators(df)
    df = b.generate_signals(df)
    lv = b.compute_session_levels(df)
    core_first = core_entry_times(df, lv)   # full-history run for causal idle check

    df = df.loc[start:end]
    cost = b.round_turn_cost(CONTRACTS)
    trades, collisions, armed_days = [], 0, 0

    for day, ddf in df.groupby(df.index.normalize()):
        if len(ddf) < 12:
            continue
        dkey = str(day)[:10]
        core_min = core_first.get(dkey)
        above = (ddf["close"] > ddf["vwap"]).values
        times = [ts.hour * 60 + ts.minute for ts in ddf.index]
        arm_lo = 10 * 60 + 30
        arm_hi = 14 * 60
        took = False
        for i in range(2, len(ddf)):
            if took or times[i] < arm_lo:
                continue
            if times[i] > arm_hi:
                break
            if core_min is not None and core_min <= times[i]:
                break                                # core already active today
            frac_above = above[:i].mean()
            if frac_above >= DRIFT_FRAC and above[i - 1]:
                direction = "long"
            elif frac_above <= (1 - DRIFT_FRAC) and not above[i - 1]:
                direction = "short"
            else:
                continue
            prev = ddf.iloc[i - 1]
            if b.get_risk_profile(prev)["contracts"] <= 0:
                continue
            armed_days += 1
            pnl_pts, reason = simulate(ddf, direction, i)
            usd = pnl_pts * b.MNQ_POINT_VALUE * CONTRACTS - cost
            trades.append({"date": dkey, "year": dkey[:4], "dir": direction,
                           "pnl_pts": pnl_pts, "usd": usd, "reason": reason})
            took = True
            if core_min is not None and core_min > times[i]:
                collisions += 1                      # core wanted in later same day

    t = pd.DataFrame(trades)
    print("=" * 70)
    print(f"TDC CANDIDATE A — period={period} ({start} -> {end})  spec=FROZEN v1")
    print("=" * 70)
    if t.empty:
        print("0 trades. DEAD at sample-size gate.")
        return
    wins = t[t.usd > 0]; losses = t[t.usd <= 0]
    pf = wins.usd.sum() / abs(losses.usd.sum()) if len(losses) and losses.usd.sum() != 0 else float("inf")
    yrs = max(1.0, (pd.Timestamp(end) - pd.Timestamp(start)).days / 365.25)
    print(f"trades: {len(t)}   WR: {len(wins)/len(t)*100:.1f}%   PF: {pf:.2f}")
    print(f"net: ${t.usd.sum():,.0f}   avg: ${t.usd.mean():,.0f}   "
          f"worst: ${t.usd.min():,.0f}   best: ${t.usd.max():,.0f}   ~${t.usd.sum()/yrs:,.0f}/yr")
    print(f"exits: {t.reason.value_counts().to_dict()}")
    print(f"collision days (core fired later, TDC was in first): {collisions}")
    print("\nby year:")
    for y, g in t.groupby("year"):
        share = g.usd.sum() / t.usd.sum() * 100 if t.usd.sum() != 0 else 0
        print(f"  {y}: n={len(g):>3}  net ${g.usd.sum():>8,.0f}  ({share:.0f}% of net)  WR {len(g[g.usd>0])/len(g)*100:.0f}%")
    print("\n--- KILL CRITERIA (dev) ---")
    year_max = max(abs(g.usd.sum()) / abs(t.usd.sum()) for _, g in t.groupby("year")) if t.usd.sum() != 0 else 1
    checks = [
        ("n >= 15", len(t) >= 15),
        ("PF >= 1.5", pf >= 1.5),
        ("worst trade <= $600 risk", t.usd.min() >= -600),
        ("no year > 60% of |net|", year_max <= 0.60),
    ]
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print("VERDICT:", "SURVIVES dev gate" if all(ok for _, ok in checks) else "DEAD")

    out = os.path.join(os.path.dirname(__file__), f"tdc_{period}_trades.csv")
    t.to_csv(out, index=False)
    print(f"trades -> {out}")


if __name__ == "__main__":
    main()
