"""
Experiment runner for the choppy-market strategy research (E0-E5).

Each experiment runs a full backtest over the clean (volume-based front-month) sample
and reports the §1.2 consistency scorecard on the CHOP-ONLY trade subset.

Usage:
    python -m src.run_experiments
or:
    python src/run_experiments.py
"""
import io
import sys

# Force UTF-8 output on Windows consoles that default to cp1252
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import copy
import textwrap
from typing import List

import src.bot as bot


# ── Helpers ───────────────────────────────────────────────────────────────────

def _apply(overrides: dict) -> None:
    """Patch bot module globals for the current experiment."""
    for k, v in overrides.items():
        setattr(bot, k, v)


def _restore(saved: dict) -> None:
    for k, v in saved.items():
        setattr(bot, k, v)


def _scorecard_row(label: str, sc: dict) -> str:
    if sc is None or sc.get("n_trades") is None:
        return f"  {label:<40} — no trades"
    viable   = "VIABLE"   if sc["viable"]   else "not-viable"
    fundable = " FUNDABLE" if sc["fundable"] else ""
    return (
        f"  {label:<40} N={sc['n_trades']:>4}  "
        f"PF={sc['profit_factor']:.2f}  "
        f"SQN={sc['sqn']:.2f}  "
        f"E/cost={sc['expectancy_in_cost_units']:.1f}x  "
        f"ProfDays={sc['pct_profitable_days']:.0f}%  "
        f"BestDay%={sc['best_day_pct_of_net']:.0f}%  "
        f"Sharpe={sc['sharpe_daily']:.2f}  "
        f"Calmar={sc['calmar']:.2f}  "
        f"MaxLoss={sc['max_consec_losses']}  "
        f"→ {viable}{fundable}"
    )


def _run_experiment(name: str, overrides: dict, saved_defaults: dict,
                    df_base, session_levels):
    """Apply overrides, run backtest, restore, return (name, chop_sc, result_dict)."""
    print(f"\n{'='*70}")
    print(f"  {name}")
    print(f"{'='*70}")
    print("  Config overrides:", {k: v for k, v in overrides.items()})

    _apply(overrides)

    # Re-generate signals so regime column reflects current REGIME_USE_ADX setting.
    df_work = bot.generate_signals(df_base.copy())

    df_out, trades, daily_records, state = bot.run_backtest(df_work, session_levels)

    # Restore immediately so the next experiment starts clean.
    _restore(saved_defaults)

    n_total = len(trades)
    n_adx_chop = sum(1 for t in trades if t.bar_adx_regime == "choppy")
    n_mr    = sum(1 for t in trades if t.entry_type in ("VWAP_MR", "FAILED_BREAKOUT"))
    n_trend = sum(1 for t in trades if t.entry_type in ("FVG", "ORB"))

    print(f"  Trades total={n_total}  ADX-chop={n_adx_chop}  MR-entries={n_mr}  trend-entries={n_trend}")

    stats = bot.compute_stats(df_out, trades, daily_records, state)

    # Chop-only trades: filter by the ADX bar regime stored at entry time.
    chop_trades = [t for t in trades if t.bar_adx_regime == "choppy"]
    avg_cost    = stats.get("avg_cost", 0.0)
    chop_sc     = bot._compute_consistency_scorecard(chop_trades, daily_records, avg_cost)

    overall_sc  = stats.get("consistency_scorecard", {})

    print(f"  Net P&L: ${stats['total_net']:,.0f}  |  PF={stats['profit_factor']:.2f}  "
          f"|  Sharpe={stats['sharpe']:.2f}  |  MaxDD={stats['max_dd']:.1f}%")
    print(f"\n  CHOP-SUBSET SCORECARD:")
    print("  " + "-"*66)
    for metric, val in chop_sc.items():
        if val is None:
            continue
        print(f"    {metric:<32} {val}")
    print("  " + "-"*66)
    viable_str   = "VIABLE"   if chop_sc.get("viable")   else "not-viable"
    fundable_str = "FUNDABLE" if chop_sc.get("fundable") else "not-fundable"
    print(f"  VERDICT: {viable_str}  |  {fundable_str}")

    return name, chop_sc, stats


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Loading and preparing data (once for all experiments)…")
    df_raw = bot.fetch_data()
    bot.validate_loaded_data(df_raw)
    df_indicators = bot.add_indicators(df_raw)
    session_levels = bot.compute_session_levels(df_indicators)

    # Snapshot all relevant defaults so we can restore after each run.
    GLOBALS_TO_TRACK = [
        "REGIME_USE_ADX", "REGIME_ROUTER_ENABLED", "NEUTRAL_NO_TRADE",
        "VWAP_MR_ENABLED", "VWAP_MR_TWO_SIDED", "VWAP_MR_SIGMA_BAND",
        "VWAP_MR_FVG_AS_BONUS", "VWAP_MR_WINDOW_START", "VWAP_MR_WINDOW_END",
        "FAILED_BREAKOUT_ENABLED", "FB_WINDOW_START", "FB_WINDOW_END",
        "FB_MIN_SWEEP_TICKS", "FB_MAX_SWEEP_TICKS", "FB_TARGET_TICKS",
        "MR_WINDOW_START_CT", "MR_WINDOW_END_CT",
    ]
    saved = {k: getattr(bot, k) for k in GLOBALS_TO_TRACK}

    results = []

    # ── E0: ADX regime baseline vs legacy ATR ─────────────────────────────────
    # E0a: ADX on (new default)
    r = _run_experiment(
        "E0a — ADX regime (REGIME_USE_ADX=True)",
        {"REGIME_USE_ADX": True},
        saved, df_indicators, session_levels,
    )
    results.append(r)

    # E0b: Legacy ATR-only (comparison)
    r = _run_experiment(
        "E0b — Legacy ATR regime (REGIME_USE_ADX=False)",
        {"REGIME_USE_ADX": False},
        saved, df_indicators, session_levels,
    )
    results.append(r)

    # ── E1: Two-sided sigma-band VWAP reversion (both sides, sigma entry) ───────
    r = _run_experiment(
        "E1 — Two-sided sigma-band VWAP MR (TWO_SIDED+SIGMA+FVG_BONUS)",
        {
            "VWAP_MR_ENABLED":    True,
            "VWAP_MR_TWO_SIDED":  True,
            "VWAP_MR_SIGMA_BAND": True,
            "VWAP_MR_FVG_AS_BONUS": True,
            "VWAP_MR_WINDOW_START": (9, 45),   # avoid first 15 min
            "VWAP_MR_WINDOW_END":   (14, 30),
        },
        saved, df_indicators, session_levels,
    )
    results.append(r)

    # ── E2a: E1 gated to 10:30-12:30 lunch window ─────────────────────────────
    r = _run_experiment(
        "E2a — VWAP MR gated 10:30-12:30 CT (lunch window A)",
        {
            "VWAP_MR_ENABLED":    True,
            "VWAP_MR_TWO_SIDED":  True,
            "VWAP_MR_SIGMA_BAND": True,
            "VWAP_MR_FVG_AS_BONUS": True,
            "VWAP_MR_WINDOW_START": (10, 30),
            "VWAP_MR_WINDOW_END":   (12, 30),
        },
        saved, df_indicators, session_levels,
    )
    results.append(r)

    # ── E2b: E1 gated to 11:00-13:30 lunch window ─────────────────────────────
    r = _run_experiment(
        "E2b — VWAP MR gated 11:00-13:30 CT (lunch window B)",
        {
            "VWAP_MR_ENABLED":    True,
            "VWAP_MR_TWO_SIDED":  True,
            "VWAP_MR_SIGMA_BAND": True,
            "VWAP_MR_FVG_AS_BONUS": True,
            "VWAP_MR_WINDOW_START": (11, 0),
            "VWAP_MR_WINDOW_END":   (13, 30),
            "MR_WINDOW_START_CT":   (11, 0),
            "MR_WINDOW_END_CT":     (13, 30),
        },
        saved, df_indicators, session_levels,
    )
    results.append(r)

    # ── E3: Regime router (reversion in chop, trend stack in trending) ─────────
    r = _run_experiment(
        "E3 — Regime router ON (reversion vs trend by regime)",
        {
            "REGIME_ROUTER_ENABLED": True,
            "VWAP_MR_ENABLED":       True,
            "VWAP_MR_TWO_SIDED":     True,
            "VWAP_MR_SIGMA_BAND":    True,
            "VWAP_MR_FVG_AS_BONUS":  True,
            "FAILED_BREAKOUT_ENABLED": True,
        },
        saved, df_indicators, session_levels,
    )
    results.append(r)

    # ── E4a: Failed-breakout tuning — tighten sweep, larger target ────────────
    r = _run_experiment(
        "E4a — Failed-breakout: sweep 6-20t, target 64t",
        {
            "FAILED_BREAKOUT_ENABLED": True,
            "FB_MIN_SWEEP_TICKS":      6,
            "FB_MAX_SWEEP_TICKS":      20,
            "FB_TARGET_TICKS":         64,
        },
        saved, df_indicators, session_levels,
    )
    results.append(r)

    # ── E4b: Failed-breakout + extend window to cover lunch ───────────────────
    r = _run_experiment(
        "E4b — Failed-breakout: sweep 6-20t, target 64t, window to 14:00",
        {
            "FAILED_BREAKOUT_ENABLED": True,
            "FB_MIN_SWEEP_TICKS":      6,
            "FB_MAX_SWEEP_TICKS":      20,
            "FB_TARGET_TICKS":         64,
            "FB_WINDOW_START":         (10, 0),
            "FB_WINDOW_END":           (14, 0),
        },
        saved, df_indicators, session_levels,
    )
    results.append(r)

    # ── E5: Neutral no-trade zone (ADX 20-25 stands aside) ────────────────────
    r = _run_experiment(
        "E5 — Neutral no-trade zone (ADX 20-25, NEUTRAL_NO_TRADE=True)",
        {"NEUTRAL_NO_TRADE": True},
        saved, df_indicators, session_levels,
    )
    results.append(r)

    # ── Final summary ─────────────────────────────────────────────────────────
    print("\n\n" + "="*70)
    print("  EXPERIMENT SUMMARY — CHOP-SUBSET CONSISTENCY SCORECARD")
    print("="*70)
    print(f"  {'Experiment':<40}  N    PF    SQN   E/cost  ProfDays  BestDay%  Verdict")
    print("  " + "-"*68)
    fundable_candidates = []
    for name, sc, stats in results:
        if sc.get("n_trades") is None or sc["n_trades"] == 0:
            print(f"  {name:<40}  — no chop trades")
            continue
        verdict = "VIABLE" if sc["viable"] else "---"
        if sc["fundable"]:
            verdict = "FUNDABLE"
            fundable_candidates.append((name, sc, stats))
        print(
            f"  {name:<40}  "
            f"{sc['n_trades']:>4}  "
            f"{sc['profit_factor']:>4.2f}  "
            f"{sc['sqn']:>5.2f}  "
            f"{sc['expectancy_in_cost_units']:>6.1f}x  "
            f"{sc['pct_profitable_days']:>8.0f}%  "
            f"{sc['best_day_pct_of_net']:>8.0f}%  "
            f"{verdict}"
        )

    print("\n" + "="*70)
    if fundable_candidates:
        print("  FUNDABLE CANDIDATES (viable + best-day ≤ 30% of net):")
        print("="*70)
        for name, sc, stats in fundable_candidates:
            print(f"\n  {name}")
            for k, v in sc.items():
                if v is not None:
                    print(f"    {k:<32} {v}")
    else:
        print("  No configs cleared the FUNDABLE bar on the chop subset.")
        print("  Highest SQN viable candidate:")
        viable = [(n, sc, st) for n, sc, st in results
                  if sc.get("viable") and sc.get("n_trades", 0) > 0]
        if viable:
            best = max(viable, key=lambda x: x[1]["sqn"])
            print(f"    {best[0]}  →  SQN={best[1]['sqn']:.2f}")
    print("="*70)


if __name__ == "__main__":
    main()
