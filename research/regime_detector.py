"""
Regime Detector
===============
Classifies market conditions and maps them to V29 FVG strategy performance.
Answers: in markets that look like the current one, how does the strategy
historically perform?

Two new regime metrics inspired by Vibe-Trading's correlation-regime skill:
  - VWAP crossing frequency (bars/day where price flips above/below VWAP).
    High count = choppy/mean-reverting day. Low count = directional day.
  - ATR percentile rank (rolling 20-day window). High = elevated volatility.

Combined with the bot's built-in ADX regime and session phase, this gives a
multi-factor view of when the FVG edge is strongest and weakest.

Run from repo root:
    python -m research.regime_detector
"""

import json
import sys
import os
import pytz
import numpy as np
import pandas as pd
from collections import defaultdict
from typing import List, Dict

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import src.bot as bot

_EASTERN = pytz.timezone("US/Eastern")

SIGNAL_JSON_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "src", "exports", "v29_topstep_order_plan.json"
)

# ── REGIME METRICS ────────────────────────────────────────────────────────────

def compute_daily_regime_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """Build a daily regime summary from the 5-minute bar DataFrame."""
    if df.index.tzinfo is None:
        df.index = df.index.tz_localize("US/Eastern")

    df = df.copy()
    df["session_date"] = df.index.date

    # VWAP crossing frequency
    df["above_vwap"] = (df["close"] > df["vwap"]).astype(int)
    # Detect side-changes within each day only (no cross-day spillover)
    df["_prev_above"] = df.groupby("session_date")["above_vwap"].shift(1)
    df["vwap_cross"] = (df["above_vwap"] != df["_prev_above"]).astype(int)
    df.loc[df["_prev_above"].isna(), "vwap_cross"] = 0
    df.drop(columns=["_prev_above"], inplace=True)

    daily = df.groupby("session_date").agg(
        vwap_crossings=("vwap_cross", "sum"),
        mean_atr=("atr", "mean"),
        mean_adx=("adx", "mean"),
        bar_count=("close", "count"),
    ).reset_index()
    daily["session_date"] = pd.to_datetime(daily["session_date"])

    # Rolling percentile ranks (20-day window)
    daily["atr_pctile_20d"] = daily["mean_atr"].rolling(20, min_periods=5).apply(
        lambda x: 100.0 * (x[-1] >= x).mean(), raw=True
    )
    daily["vwap_cross_pctile_20d"] = daily["vwap_crossings"].rolling(20, min_periods=5).apply(
        lambda x: 100.0 * (x[-1] >= x).mean(), raw=True
    )

    # Composite regime label
    def classify_day(row):
        adx = row["mean_adx"]
        cp = row["vwap_cross_pctile_20d"]
        ap = row["atr_pctile_20d"]
        if pd.isna(cp) or pd.isna(ap):
            return "unknown"
        if ap >= 80:
            return "volatile"
        if adx >= 25 and cp <= 40:
            return "trending"
        if adx < 20 or cp >= 65:
            return "choppy"
        return "mixed"

    daily["day_regime"] = daily.apply(classify_day, axis=1)
    return daily


# ── TABLE HELPERS ─────────────────────────────────────────────────────────────

def _perf_by_key(trades: List, key_fn) -> List[Dict]:
    groups = defaultdict(list)
    for t in trades:
        groups[key_fn(t)].append(t)
    rows = []
    for grp_label, grp in sorted(groups.items(), key=lambda x: str(x[0])):
        n = len(grp)
        wins = sum(1 for t in grp if t.won)
        net = sum(t.pnl_usd for t in grp)
        gp = sum(t.pnl_usd for t in grp if t.pnl_usd > 0)
        gl = abs(sum(t.pnl_usd for t in grp if t.pnl_usd < 0))
        pf = gp / gl if gl > 0 else float("inf")
        rows.append({"label": grp_label, "n": n, "wr": 100*wins/n,
                     "net": net, "avg": net/n, "pf": pf})
    return rows


def _print_table(rows: List[Dict], title: str):
    print(f"\n  {title}")
    print(f"  {'-'*68}")
    print(f"  {'Slice':<20} {'N':>5} {'WR%':>5} {'Avg $':>8} {'Net $':>10} {'PF':>6}")
    print(f"  {'-'*20} {'-'*5} {'-'*5} {'-'*8} {'-'*10} {'-'*6}")
    for r in rows:
        pf_s = f"{r['pf']:.2f}" if r['pf'] != float("inf") else "  inf"
        print(f"  {str(r['label']):<20} {r['n']:>5} {r['wr']:>4.1f}%"
              f"  {r['avg']:>8.0f}  {r['net']:>10.0f}  {pf_s:>6}")


def _fmt_sep(c="=", w=72):
    print(c * w)


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    _fmt_sep()
    print("REGIME DETECTOR -- V29 FVG Strategy")
    _fmt_sep()

    # ── 1. Load data & run backtest ──────────────────────────────────────────
    print("\nLoading data and running backtest...")
    df = bot.fetch_data()
    df = bot.add_indicators(df)
    df = bot.generate_signals(df)
    session_levels = bot.compute_session_levels(df)
    _, bt_trades, _, _ = bot.run_backtest(df, session_levels)
    fvg_trades = [t for t in bt_trades if t.entry_type == "FVG"]
    n_wins = sum(1 for t in fvg_trades if t.won)
    print(f"Done: {len(fvg_trades):,} FVG trades, {n_wins:,} wins "
          f"({100*n_wins/len(fvg_trades):.1f}% WR)\n")

    data_end = df.index[-1]
    print(f"NOTE: backtest data covers to {data_end.strftime('%Y-%m-%d')}.")
    print("      Current regime uses today's signal JSON for supplemental data.")

    # ── 2. Compute daily regime metrics ─────────────────────────────────────
    print("\nComputing regime metrics...")
    daily = compute_daily_regime_metrics(df)
    daily_regime_map = {row["session_date"].date(): row["day_regime"]
                        for _, row in daily.iterrows()}

    def day_regime(t):
        return daily_regime_map.get(t.date.date(), "unknown")

    # ── 3. ATR regime (bot's built-in) ───────────────────────────────────────
    _fmt_sep("-")
    _print_table(_perf_by_key(fvg_trades, lambda t: t.regime),
                 "Performance by ATR regime (bot built-in: calm / strong)")

    # ── 4. ADX regime ────────────────────────────────────────────────────────
    _print_table(_perf_by_key(fvg_trades, lambda t: t.bar_adx_regime),
                 "Performance by ADX regime (trending / choppy / neutral)")

    # ── 5. Session phase ─────────────────────────────────────────────────────
    def phase(t):
        if t.entry_hour < 11:
            return "am  (9:30-11)"
        if t.entry_hour < 13:
            return "lunch (11-13)"
        return "pm  (13-15)"

    _print_table(_perf_by_key(fvg_trades, phase),
                 "Performance by session phase (CT hours)")

    # ── 6. Day of week ───────────────────────────────────────────────────────
    _print_table(_perf_by_key(fvg_trades, lambda t: t.day_of_week),
                 "Performance by day of week")

    # ── 7. FVG quality score ─────────────────────────────────────────────────
    _print_table(_perf_by_key(fvg_trades, lambda t: f"score {t.fvg_quality_score}"),
                 "Performance by FVG quality score")

    # ── 8. Composite day regime (VWAP crossings + ADX + ATR) ─────────────────
    _print_table(_perf_by_key(fvg_trades, day_regime),
                 "Performance by composite day regime (VWAP-cross + ADX + ATR)")

    # ── 9. Monthly seasonality ───────────────────────────────────────────────
    month_names = {1:"Jan",2:"Feb",3:"Mar",4:"Apr",5:"May",6:"Jun",
                   7:"Jul",8:"Aug",9:"Sep",10:"Oct",11:"Nov",12:"Dec"}
    _print_table(
        _perf_by_key(fvg_trades,
                     lambda t: f"{t.month:02d} {month_names.get(t.month,'')}"),
        "Seasonal performance by month"
    )

    # ── 10. Current regime assessment ────────────────────────────────────────
    _fmt_sep()
    print("CURRENT MARKET REGIME")
    _fmt_sep()

    # What does the last available day in backtest look like?
    recent_daily = daily[daily["day_regime"] != "unknown"].tail(20)
    if not recent_daily.empty:
        last_day = recent_daily.iloc[-1]
        print(f"\n  Last day in backtest CSV: {last_day['session_date'].date()}")
        print(f"    Regime:            {last_day['day_regime']}")
        print(f"    Mean ADX:          {last_day['mean_adx']:.1f}")
        print(f"    VWAP crossings:    {last_day['vwap_crossings']:.0f}  "
              f"(20d pctile: {last_day['vwap_cross_pctile_20d']:.0f}%)")
        print(f"    ATR 20d pctile:    {last_day['atr_pctile_20d']:.0f}%")

        regime_counts = recent_daily["day_regime"].value_counts()
        print(f"\n  Regime distribution (last {len(recent_daily)} trading days):")
        for reg, cnt in regime_counts.items():
            bar = "#" * int(20 * cnt / len(recent_daily))
            print(f"    {reg:<12} {cnt:>3}d  {bar}")

    # Supplement with live signal JSON for current snapshot
    print()
    if os.path.exists(SIGNAL_JSON_PATH):
        try:
            with open(SIGNAL_JSON_PATH, "r", encoding="utf-8") as fh:
                plan = json.load(fh)
            sig = plan.get("signal") or {}
            if sig:
                print("  LIVE signal snapshot (from most recent order plan):")
                atr_r = sig.get("atr_ratio_at_entry", 0)
                adx_v = sig.get("adx_at_entry", 0)
                dist_v = sig.get("price_distance_from_vwap", 0)
                score = sig.get("fvg_quality_score", "?")
                regime_live = sig.get("regime", "?")
                print(f"    Date/time:         {sig.get('as_of','?')}")
                print(f"    ATR ratio:         {atr_r:.3f}  "
                      f"(CALM_ATR_RATIO={bot.CALM_ATR_RATIO}  ->  "
                      f"regime={'strong' if atr_r >= bot.CALM_ATR_RATIO else 'calm'})")
                print(f"    ADX:               {adx_v:.1f}  "
                      f"({'trending' if adx_v >= 25 else 'weak trend'})")
                print(f"    VWAP distance:     {dist_v:.1f} pts")
                print(f"    FVG quality score: {score}")
                print(f"    Bot regime label:  {regime_live}")

                # Look up expected performance in this live regime
                live_regime_bucket = [t for t in fvg_trades if t.regime == regime_live]
                if live_regime_bucket:
                    n = len(live_regime_bucket)
                    wr = 100 * sum(1 for t in live_regime_bucket if t.won) / n
                    net = sum(t.pnl_usd for t in live_regime_bucket)
                    gp = sum(t.pnl_usd for t in live_regime_bucket if t.pnl_usd > 0)
                    gl = abs(sum(t.pnl_usd for t in live_regime_bucket if t.pnl_usd < 0))
                    pf = gp / gl if gl > 0 else float("inf")
                    print(f"\n    Expected performance in '{regime_live}' regime ({n} hist trades):")
                    print(f"      Win rate:          {wr:.1f}%")
                    print(f"      Avg trade:         ${net/n:+.0f}")
                    print(f"      Profit factor:     {pf:.2f}")
        except Exception as exc:
            print(f"  (could not read signal JSON: {exc})")
    else:
        print("  (signal JSON not found -- run bot first to generate it)")

    # ── 11. Regime trends (last 20d) ─────────────────────────────────────────
    if not recent_daily.empty and len(recent_daily) >= 4:
        adx_vals = recent_daily["mean_adx"].values
        cross_vals = recent_daily["vwap_crossings"].values
        adx_dir = "rising" if adx_vals[-1] > adx_vals[0] else "falling"
        cross_dir = ("rising (choppier)" if cross_vals[-1] > cross_vals[0]
                     else "falling (more directional)")
        print(f"\n  ADX trend (last {len(recent_daily)}d): "
              f"{adx_vals[0]:.1f} -> {adx_vals[-1]:.1f}  [{adx_dir}]")
        print(f"  VWAP crossings trend: "
              f"{cross_vals[0]:.0f} -> {cross_vals[-1]:.0f}  [{cross_dir}]")

    _fmt_sep()
    print("INTERPRETATION")
    _fmt_sep("-")
    print("  trending + low crossings  : ideal for FVG (price commits to direction)")
    print("  choppy   + high crossings : FVG edge weakest; accept lower WR")
    print("  volatile                  : wide spreads, stops get run -- check PF")
    print("  VWAP crossings pctile >70 : market churning; expect more losses")
    print("  ADX falling + crossings   ")
    print("    rising                  : regime deteriorating; watch for more chop")
    _fmt_sep()


if __name__ == "__main__":
    main()
