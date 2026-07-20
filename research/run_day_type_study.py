"""
PHASE 0 — Day-type opportunity study (pre-registered measurement, NOT tuning).

Question: how many sessions is the FVG core idle on, and is there actually a
continuation edge worth money on those days?

Labels per session (fixed, decided before running):
  active           — core took >=1 FVG trade
  idle_no_pullback — regime opened on >=1 bar AND >=1 FVG formed, but 0 trades
                     (Monday 2026-07-06's type: setups formed, price never
                     pulled back in — the day a Trend-Drift Continuation
                     edge would target)
  idle_no_fvg      — regime opened but no FVG even formed
  dead             — regime never opened (calm/explosive all day)

Prize measurement (fixed, pre-registered — NO parameter search):
  Split each day at 10:30 CT. morning = first bar open -> 10:30 close.
  afternoon = 10:30 close -> last bar close. The naive continuation proxy:
  "at 10:30, take the morning direction, hold to EOD."
  signed_pts = sign(morning_move) * afternoon_move.
  Reported per day-type, with costs at 3 contracts (round_turn_cost).
  Also split by |morning_move| tercile (a drift edge would arm only on
  strong drifts — tercile split is reporting, not optimization).

If the idle_no_pullback expectancy is ~0 or negative even BEFORE costs,
no continuation edge exists on those days and the program dies here.
"""
import io
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np
import pandas as pd

import src.bot as b


def main():
    df = b.fetch_data()
    df = b.add_indicators(df)
    df = b.generate_signals(df)
    lv = b.compute_session_levels(df)

    _stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        _, trades, _, _ = b.run_backtest(df, lv)
    finally:
        sys.stdout = _stdout

    fvg_trade_days = {}
    for t in trades:
        if t.entry_type == "FVG":
            d = str(t.date)[:10]
            fvg_trade_days[d] = fvg_trade_days.get(d, 0) + 1

    gap_min = b.FVG_MIN_SIZE_TICKS * b.MNQ_TICK_SIZE
    day_rows = []

    dates = df.index.normalize()
    for day, ddf in df.groupby(dates):
        if len(ddf) < 12:      # skip stub sessions (holidays/partial)
            continue
        dkey = str(day)[:10]

        # regime-open bars (same gate the entry router uses, on prev-bar basis)
        regime_open = 0
        for i in range(1, len(ddf)):
            prof = b.get_risk_profile(ddf.iloc[i - 1])
            if prof["contracts"] > 0:
                regime_open += 1

        # FVGs formed (exact 3-bar detection from the engine)
        fvgs = 0
        for i in range(2, len(ddf)):
            row, bar2 = ddf.iloc[i], ddf.iloc[i - 2]
            if bar2["high"] < row["low"] and (row["low"] - bar2["high"]) >= gap_min:
                fvgs += 1
            if bar2["low"] > row["high"] and (bar2["low"] - row["high"]) >= gap_min:
                fvgs += 1

        traded = fvg_trade_days.get(dkey, 0)
        if traded:
            label = "active"
        elif regime_open == 0:
            label = "dead"
        elif fvgs >= 1:
            label = "idle_no_pullback"
        else:
            label = "idle_no_fvg"

        # 10:30 CT split (pre-registered)
        try:
            am = ddf.between_time("08:30", "10:30")
            pm = ddf.between_time("10:35", "15:00")
        except TypeError:
            continue
        if len(am) < 3 or len(pm) < 3:
            continue
        morning = float(am["close"].iloc[-1] - am["open"].iloc[0])
        afternoon = float(pm["close"].iloc[-1] - am["close"].iloc[-1])
        direction = np.sign(morning) if morning != 0 else 0.0
        signed_pts = direction * afternoon

        day_rows.append({
            "date": dkey, "year": dkey[:4], "label": label,
            "bars": len(ddf), "regime_open": regime_open, "fvgs": fvgs,
            "trades": traded, "morning_pts": morning, "signed_pts": signed_pts,
        })

    days = pd.DataFrame(day_rows)
    n_years = max(1.0, (df.index[-1] - df.index[0]).days / 365.25)
    cost3 = b.round_turn_cost(3)

    print("=" * 74)
    print("PHASE 0 — DAY-TYPE OPPORTUNITY STUDY")
    print(f"sessions: {len(days)}   span: {days['date'].iloc[0]} -> {days['date'].iloc[-1]}   ({n_years:.1f} yrs)")
    print(f"cost assumption: 3 contracts round-turn = ${cost3:.0f}")
    print("=" * 74)

    print("\n--- day-type census ---")
    for label, g in days.groupby("label"):
        print(f"  {label:<18} {len(g):>5} days  ({len(g)/len(days)*100:4.1f}%)")

    print("\n--- naive 10:30 continuation proxy, by day type ---")
    print(f"  {'day type':<18}{'days':>6}{'win%':>7}{'avg pts':>9}{'med pts':>9}"
          f"{'$/day@3c':>10}{'$/yr@3c':>10}")
    for label, g in days.groupby("label"):
        pts = g["signed_pts"]
        win = (pts > 0).mean() * 100
        usd_day = pts.mean() * b.MNQ_POINT_VALUE * 3 - cost3
        usd_yr = usd_day * len(g) / n_years
        print(f"  {label:<18}{len(g):>6}{win:>6.1f}%{pts.mean():>9.1f}{pts.median():>9.1f}"
              f"{usd_day:>10.0f}{usd_yr:>10.0f}")

    print("\n--- idle_no_pullback only: by |morning drift| tercile ---")
    idle = days[days["label"] == "idle_no_pullback"].copy()
    if len(idle) >= 9:
        idle["strength"] = pd.qcut(idle["morning_pts"].abs(), 3,
                                   labels=["weak", "medium", "strong"])
        for s, g in idle.groupby("strength", observed=True):
            pts = g["signed_pts"]
            usd_day = pts.mean() * b.MNQ_POINT_VALUE * 3 - cost3
            print(f"  {s:<8} n={len(g):>4}  win {(pts>0).mean()*100:4.1f}%  "
                  f"avg {pts.mean():+7.1f} pts  ${usd_day:+7.0f}/day@3c  "
                  f"(~${usd_day*len(g)/n_years:+,.0f}/yr)")

    print("\n--- idle_no_pullback by year (stability check) ---")
    for y, g in idle.groupby("year"):
        pts = g["signed_pts"]
        print(f"  {y}: n={len(g):>3}  avg {pts.mean():+7.1f} pts  win {(pts>0).mean()*100:4.1f}%")

    out = os.path.join(os.path.dirname(__file__), "day_type_study_results.csv")
    days.to_csv(out, index=False)
    print(f"\nper-day rows -> {out}")


if __name__ == "__main__":
    main()
