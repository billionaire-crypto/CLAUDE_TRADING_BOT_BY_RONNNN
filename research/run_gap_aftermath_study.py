"""
GAP AFTERMATH STUDY — what actually happens to price around FVGs that never
get entered? Resolves the paradox: gaps go unvisited (price stays away) YET
trend-continuation entries lose (TDC/SPC both PF 0.84). Where does price GO?

For every FVG in 7 years, classify its fate during its 4-bar life:
  ENTERED     — a bar OPENED inside the zone (the core's entry condition)
  INVALIDATED — price CLOSED through the far side (violent blow-through)
  EXPIRED     — lived 4 bars untouched by any open -> the mystery population

For EXPIRED gaps, measure the aftermath (signed so + = continuation in the
gap's direction):
  - late same-day touch: does price wick back into the zone AFTER expiry?
  - forward signed move from expiry: +30min, +60min, end of day
  - CHOP TEST: over the next hour, does the ADVERSE excursion reach 1.5xATR
    (a TDC/SPC-style stop) BEFORE the favorable excursion reaches 2xATR
    (the target)? This is the trend-follower death mechanism, measured.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np
import pandas as pd

import src.bot as b


def main():
    df = b.fetch_data()
    df = b.add_indicators(df)
    gap_min = b.FVG_MIN_SIZE_TICKS * b.MNQ_TICK_SIZE
    AGE = b.FVG_MAX_AGE_BARS

    fates = {"entered": 0, "invalidated": 0, "expired": 0}
    fate_by_year = {}
    aft = []   # aftermath rows for EXPIRED gaps

    for day, ddf in df.groupby(df.index.normalize()):
        if len(ddf) < 12:
            continue
        year = str(day)[:4]
        o = ddf["open"].values; h = ddf["high"].values
        l = ddf["low"].values; c = ddf["close"].values
        atr = ddf["atr"].values
        n = len(ddf)
        for i in range(2, n):
            gaps = []
            if h[i - 2] < l[i] and (l[i] - h[i - 2]) >= gap_min:
                gaps.append(("bull", h[i - 2], l[i]))     # zone bottom, top
            if l[i - 2] > h[i] and (l[i - 2] - h[i]) >= gap_min:
                gaps.append(("bear", h[i], l[i - 2]))
            for kind, lo, hi in gaps:
                fate = "expired"
                end = min(i + 1 + AGE, n)
                for j in range(i + 1, end):
                    if lo <= o[j] <= hi:
                        fate = "entered"; break
                    if (kind == "bull" and c[j] < lo) or (kind == "bear" and c[j] > hi):
                        fate = "invalidated"; break
                fates[fate] += 1
                fy = fate_by_year.setdefault(year, {"entered": 0, "invalidated": 0, "expired": 0})
                fy[fate] += 1
                if fate != "expired" or end >= n - 2:
                    continue
                # ---- aftermath of an expired, untouched gap ----
                x = end - 1                      # expiry bar index
                sgn = 1.0 if kind == "bull" else -1.0
                a = float(atr[x]) if atr[x] and not np.isnan(atr[x]) else None
                if not a or a <= 0:
                    continue
                ref = c[x]
                def fwd(k):
                    j = min(x + k, n - 1)
                    return sgn * (c[j] - ref)
                # late same-day touch of the zone (wick counts)
                late, tbars = False, None
                for j in range(x + 1, n):
                    if (kind == "bull" and l[j] <= hi) or (kind == "bear" and h[j] >= lo):
                        late, tbars = True, j - i
                        break
                # chop test over next 12 bars: adverse 1.5*ATR before favorable 2*ATR?
                stop_d, tgt_d = 1.5 * a, 2.0 * a
                death = None
                for j in range(x + 1, min(x + 13, n)):
                    fav = sgn * ((h[j] if kind == "bull" else ref) - ref) if kind == "bull" else sgn * (l[j] - ref)
                    fav = sgn * ((h[j] - ref) if kind == "bull" else (l[j] - ref))
                    adv = sgn * ((l[j] - ref) if kind == "bull" else (h[j] - ref))
                    if adv <= -stop_d:
                        death = "stopped"; break
                    if fav >= tgt_d:
                        death = "target"; break
                aft.append({"year": year, "m30": fwd(6), "m60": fwd(12),
                            "eod": sgn * (c[n - 1] - ref), "atr": a,
                            "late_touch": late, "bars_to_touch": tbars,
                            "chop": death or "neither"})

    total = sum(fates.values())
    print("=" * 72)
    print(f"GAP FATES — {total:,} FVGs across 7 years (4-bar life, engine rules)")
    print("=" * 72)
    for k, v in fates.items():
        print(f"  {k:<12} {v:>7,}  ({v/total*100:4.1f}%)")

    print("\n--- fate mix by year (% of that year's gaps) ---")
    print(f"  {'year':<6}{'entered':>9}{'invalidated':>13}{'expired':>9}")
    for y in sorted(fate_by_year):
        fy = fate_by_year[y]; t = sum(fy.values())
        print(f"  {y:<6}{fy['entered']/t*100:>8.1f}%{fy['invalidated']/t*100:>12.1f}%{fy['expired']/t*100:>8.1f}%")

    A = pd.DataFrame(aft)
    print("\n" + "=" * 72)
    print(f"AFTERMATH of {len(A):,} EXPIRED (never-entered) gaps  [+ = continuation]")
    print("=" * 72)
    for col, label in (("m30", "next 30 min"), ("m60", "next 60 min"), ("eod", "to end of day")):
        s = A[col]
        print(f"  {label:<15} mean {s.mean():+6.2f} pts | median {s.median():+6.2f} | "
              f"continuation-wins {(s>0).mean()*100:4.1f}%")
    print(f"\n  in ATR units: mean 60-min move = {(A.m60/A.atr).mean():+.2f} ATR "
          f"(a 2x-ATR target needs +2.00)")
    lt = A["late_touch"]
    print(f"\n  LATE RETURN: {lt.mean()*100:.1f}% of expired gaps get touched later the SAME day")
    print(f"  median bars from creation to late touch: {A.loc[lt, 'bars_to_touch'].median():.0f} "
          f"(engine expires them at {b.FVG_MAX_AGE_BARS})")
    print("\n  CHOP TEST (enter continuation at expiry, stop 1.5xATR, target 2xATR, 1hr):")
    for k, v in A["chop"].value_counts(normalize=True).items():
        print(f"    {k:<9} {v*100:4.1f}%")


if __name__ == "__main__":
    main()
