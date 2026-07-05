"""
Proper train / holdout validation for the combined E4a + E2a strategy.

Protocol (anti-rules §3, §5):
  DEV    2019-05-06 → 2023-12-29  (~4.6 yr)  used for all tuning / comparison
  HOLDOUT 2024-01-02 → 2026-03-27 (~2.25 yr) looked at ONCE, at the end, no further tuning

Run order:
  1. Baseline on dev
  2. E4a alone on dev
  3. E2a alone on dev
  4. E4a + E2a combined on dev   ← does the stack actually add up?
  5. E4a + E2a combined on HOLDOUT ← the one honest verdict

Indicators and session levels are computed on the FULL dataset so that the
first bars of the holdout period have proper ATR/ADX warmup and correct
prev-day price levels — both are genuine historical facts, not leakage.
"""
import io
import sys

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import src.bot as bot

# ── Date split ────────────────────────────────────────────────────────────────
DEV_END       = "2023-12-31"   # dev: everything before this date
HOLDOUT_START = "2024-01-01"   # holdout: everything from this date onward

# ── Config definitions ────────────────────────────────────────────────────────
BASELINE = {}   # all defaults

E4A = {
    "FAILED_BREAKOUT_ENABLED": True,
    "FB_MIN_SWEEP_TICKS":      6,
    "FB_MAX_SWEEP_TICKS":      20,
    "FB_TARGET_TICKS":         64,
}

E2A = {
    "VWAP_MR_ENABLED":       True,
    "VWAP_MR_TWO_SIDED":     True,
    "VWAP_MR_SIGMA_BAND":    True,
    "VWAP_MR_FVG_AS_BONUS":  True,
    "VWAP_MR_WINDOW_START":  (10, 30),
    "VWAP_MR_WINDOW_END":    (12, 30),
}

COMBINED = {**E4A, **E2A}   # both together

# ── Helpers ───────────────────────────────────────────────────────────────────

def _apply(overrides):
    for k, v in overrides.items():
        setattr(bot, k, v)

def _restore(saved):
    for k, v in saved.items():
        setattr(bot, k, v)

GLOBALS_TO_SAVE = list(COMBINED.keys())


def _run(label, overrides, df_indicators, session_levels, period_label):
    saved = {k: getattr(bot, k) for k in GLOBALS_TO_SAVE}
    _apply(overrides)

    df_sig = bot.generate_signals(df_indicators.copy())
    df_out, trades, daily_records, state = bot.run_backtest(df_sig, session_levels)
    stats = bot.compute_stats(df_out, trades, daily_records, state)

    _restore(saved)

    chop_trades = [t for t in trades if t.bar_adx_regime == "choppy"]
    avg_cost    = stats.get("avg_cost", 0.0)
    chop_sc     = bot._compute_consistency_scorecard(chop_trades, daily_records, avg_cost)

    return {
        "label":        label,
        "period":       period_label,
        "n_total":      len(trades),
        "n_chop":       len(chop_trades),
        "net_pnl":      stats["total_net"],
        "pf":           stats["profit_factor"],
        "sharpe":       stats["sharpe"],
        "max_dd":       stats["max_dd"],
        "chop_sc":      chop_sc,
    }


def _print_result(r):
    sc = r["chop_sc"]
    viable   = "VIABLE"   if sc.get("viable")   else "not-viable"
    fundable = " | FUNDABLE" if sc.get("fundable") else ""
    print(f"\n  [{r['period']}]  {r['label']}")
    print(f"  Total trades: {r['n_total']}  |  Chop trades: {r['n_chop']}")
    print(f"  Net P&L: ${r['net_pnl']:,.0f}  |  PF={r['pf']:.2f}  |  Sharpe={r['sharpe']:.2f}  |  MaxDD={r['max_dd']:.1f}%")
    if sc.get("n_trades"):
        print(f"  Chop scorecard:")
        print(f"    PF={sc['profit_factor']:.2f}  SQN={sc['sqn']:.2f}  "
              f"E/cost={sc['expectancy_in_cost_units']:.1f}x  "
              f"ProfDays={sc['pct_profitable_days']:.0f}%  "
              f"BestDay%={sc['best_day_pct_of_net']:.0f}%  "
              f"MaxLoss={sc['max_consec_losses']}")
        print(f"  Verdict: {viable}{fundable}")
    else:
        print("  Chop scorecard: no chop trades")


def _compare_table(results):
    print("\n" + "=" * 72)
    print("  SIDE-BY-SIDE COMPARISON — CHOP SUBSET")
    print("=" * 72)
    print(f"  {'Config':<28} {'Period':<10} {'N':>4}  {'PF':>5}  {'SQN':>5}  "
          f"{'E/cost':>6}  {'ProfD%':>6}  {'BestD%':>6}  {'Verdict'}")
    print("  " + "-" * 70)
    for r in results:
        sc = r["chop_sc"]
        if not sc.get("n_trades"):
            print(f"  {r['label']:<28} {r['period']:<10}  — no chop trades")
            continue
        v = "FUNDABLE" if sc["fundable"] else ("VIABLE" if sc["viable"] else "---")
        print(
            f"  {r['label']:<28} {r['period']:<10} "
            f"{sc['n_trades']:>4}  "
            f"{sc['profit_factor']:>5.2f}  "
            f"{sc['sqn']:>5.2f}  "
            f"{sc['expectancy_in_cost_units']:>6.1f}x  "
            f"{sc['pct_profitable_days']:>6.0f}%  "
            f"{sc['best_day_pct_of_net']:>6.0f}%  "
            f"{v}"
        )
    print("=" * 72)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 72)
    print("  HOLDOUT VALIDATION — E4a + E2a combined")
    print(f"  DEV:     2019-05-06 → {DEV_END}")
    print(f"  HOLDOUT: {HOLDOUT_START} → 2026-03-27")
    print("  Rule: dev is for tuning. Holdout is looked at ONCE. No further changes.")
    print("=" * 72)

    # ── Load and prepare (full dataset for proper indicator warmup) ────────────
    print("\nLoading data...")
    df_raw = bot.fetch_data()
    bot.validate_loaded_data(df_raw)
    df_full_indicators = bot.add_indicators(df_raw)
    session_levels_full = bot.compute_session_levels(df_full_indicators)

    # ── Date split (after indicators so ADX/ATR are warmed up at split boundary) ─
    df_dev     = df_full_indicators[df_full_indicators.index < HOLDOUT_START].copy()
    df_holdout = df_full_indicators[df_full_indicators.index >= HOLDOUT_START].copy()

    # Session levels per period (backtest only needs levels for its own dates)
    dev_dates     = set(df_dev.index.normalize())
    hold_dates    = set(df_holdout.index.normalize())
    sl_dev        = {d: v for d, v in session_levels_full.items() if d in dev_dates}
    sl_holdout    = {d: v for d, v in session_levels_full.items() if d in hold_dates}

    print(f"\n  Dev bars: {len(df_dev):,}  |  Holdout bars: {len(df_holdout):,}")

    results = []

    # ── DEV runs ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("  DEV PERIOD RUNS (2019 → 2023)")
    print("=" * 72)

    for label, cfg in [
        ("Baseline",       BASELINE),
        ("E4a (FB tuned)", E4A),
        ("E2a (VWAP lunch)", E2A),
        ("E4a + E2a combined", COMBINED),
    ]:
        r = _run(label, cfg, df_dev, sl_dev, "DEV")
        _print_result(r)
        results.append(r)

    # ── HOLDOUT run — one look only ────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("  HOLDOUT (2024 → 2026) — ONE LOOK, NO FURTHER TUNING AFTER THIS")
    print("=" * 72)

    r_hold = _run("E4a + E2a combined", COMBINED, df_holdout, sl_holdout, "HOLDOUT")
    _print_result(r_hold)
    results.append(r_hold)

    # ── Side-by-side table ────────────────────────────────────────────────────
    _compare_table(results)

    # ── Honest verdict ────────────────────────────────────────────────────────
    dev_combined   = next(r for r in results if r["label"] == "E4a + E2a combined" and r["period"] == "DEV")
    hold_combined  = r_hold

    dev_pf   = dev_combined["chop_sc"].get("profit_factor", 0)
    hold_pf  = hold_combined["chop_sc"].get("profit_factor", 0)
    dev_sqn  = dev_combined["chop_sc"].get("sqn", 0)
    hold_sqn = hold_combined["chop_sc"].get("sqn", 0)

    print("\n  HONEST VERDICT")
    print("  " + "-" * 50)

    if hold_pf > dev_pf * 1.15:
        print("  WARNING (Rule 4): Holdout beats dev by >15%. Check for data leak.")
    elif hold_pf >= 1.30 and hold_combined["chop_sc"].get("viable"):
        decay = (dev_pf - hold_pf) / dev_pf * 100
        print(f"  Edge held in holdout. PF decay: {decay:.1f}%  (some decay is normal)")
        if hold_combined["chop_sc"].get("fundable"):
            print("  FUNDABLE in holdout — this is the real result.")
        else:
            print("  Viable but not fundable in holdout — edge exists, not yet strong enough.")
    else:
        print("  Edge did NOT hold in holdout. Do not move forward with this config.")

    n_chop_hold = hold_combined["n_chop"]
    if n_chop_hold < 50:
        print(f"  NOTE (Rule 7): Only {n_chop_hold} chop trades in holdout — interpret with caution.")

    print("=" * 72)


if __name__ == "__main__":
    main()
