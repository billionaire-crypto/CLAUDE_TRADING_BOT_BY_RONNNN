"""
Holdout (in-sample / out-of-sample) validation for the CURRENT live V29 config
(FVG + risk-budget sizing). Distinct from run_holdout.py, which validates the
older E4a+E2a chop research (FB / VWAP_MR modules that are disabled live).

The score map and risk-budget parameters were hand-tuned on the FULL 2019-2026
dataset. This is the honest test of whether they generalize: freeze the live
config and compare a clean split --

    IN-SAMPLE (train era):  2019 .. 2024-01-01
    OUT-OF-SAMPLE (test):   2024-01-01 .. end

Indicators and session levels are computed on the FULL dataset so the first
holdout bars get proper ATR/ADX warmup and correct prev-day levels (historical
facts, not leakage). If OOS per-trade quality (win rate, avg/trade, PF, Sharpe)
holds up near IS, the edge is real; if it collapses, the params are curve-fit.

Runs with whatever sizing config is live in bot.py (currently risk-budget on).
"""
import io
import sys

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import numpy as np
from collections import defaultdict
import src.bot as bot


SPLIT_DATE = "2024-01-01"


def _metrics(trades):
    if not trades:
        return dict(trades=0, wr=0.0, net=0.0, avg=0.0, pf=0.0, sharpe=0.0,
                    worst_day=0.0, worst_trade=0.0, days=0)
    pnls   = [t.pnl_usd for t in trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    net    = sum(pnls)
    day_map = defaultdict(float)
    for t in trades:
        day_map[str(t.date)[:10]] += t.pnl_usd
    daily = list(day_map.values())
    sharpe = 0.0
    if len(daily) > 1:
        arr = np.array(daily)
        if arr.std(ddof=1) > 0:
            sharpe = arr.mean() / arr.std(ddof=1) * np.sqrt(252)
    return dict(
        trades=len(trades),
        wr=len(wins) / len(pnls) * 100,
        net=net,
        avg=net / len(pnls),
        pf=sum(wins) / abs(sum(losses)) if losses else float("inf"),
        sharpe=sharpe,
        worst_day=min(daily),
        worst_trade=min(pnls),
        days=len(daily),
    )


def _ratio(oos, is_, key):
    a, b = oos[key], is_[key]
    return (a / b) if b else 0.0


def run():
    print(f"Sizing model: {'risk_budget' if bot.RISK_BUDGET_SIZING_ENABLED else 'score_map'}")
    print("Loading and processing data (once, full set for warmup)...")
    df_raw = bot.fetch_data()
    bot.validate_loaded_data(df_raw)
    df_ind = bot.add_indicators(df_raw)
    sl     = bot.compute_session_levels(df_ind)
    df_sig = bot.generate_signals(df_ind.copy())

    df_is  = df_sig[df_sig.index < SPLIT_DATE]
    df_oos = df_sig[df_sig.index >= SPLIT_DATE]

    print(f"Running IN-SAMPLE  (< {SPLIT_DATE}) ...")
    _, tr_is,  _, _ = bot.run_backtest(df_is, sl)
    print(f"Running OUT-OF-SAMPLE (>= {SPLIT_DATE}) ...")
    _, tr_oos, _, _ = bot.run_backtest(df_oos, sl)

    m_is, m_oos = _metrics(tr_is), _metrics(tr_oos)

    def line(name, key, fmt):
        print(f"  {name:<22}{fmt.format(m_is[key]):>16}{fmt.format(m_oos[key]):>16}")

    print()
    print("=" * 56)
    print(f"HOLDOUT VALIDATION  (split {SPLIT_DATE})")
    print("=" * 56)
    print(f"  {'Metric':<22}{'IN-SAMPLE':>16}{'OUT-SAMPLE':>16}")
    print(f"  {'':<22}{'2019-2023':>16}{'2024-2026':>16}")
    print("-" * 56)
    line("Trades",         "trades",      "{:,}")
    line("Trading days",   "days",        "{:,}")
    line("Win rate",       "wr",          "{:.1f}%")
    line("Net P&L",        "net",         "${:,.0f}")
    line("Avg / trade",    "avg",         "${:.2f}")
    line("Profit factor",  "pf",          "{:.2f}")
    line("Sharpe (ann.)",  "sharpe",      "{:.2f}")
    line("Worst day",      "worst_day",   "${:,.0f}")
    line("Worst trade",    "worst_trade", "${:,.0f}")
    print("=" * 56)

    # Judge generalization on per-trade quality, not totals (blocks differ in
    # length). Win rate, avg/trade and PF should hold up out-of-sample.
    wr_keep  = _ratio(m_oos, m_is, "wr")
    avg_keep = _ratio(m_oos, m_is, "avg")
    pf_keep  = _ratio(m_oos, m_is, "pf") if m_is["pf"] != float("inf") else 0.0
    print(f"\n  OOS/IS win rate : {wr_keep:.2f}")
    print(f"  OOS/IS avg/trade: {avg_keep:.2f}")
    print(f"  OOS/IS PF       : {pf_keep:.2f}")

    quality = [x for x in (wr_keep, avg_keep, pf_keep) if x > 0]
    worst = min(quality) if quality else 0.0
    print()
    if m_oos["avg"] <= 0:
        print("  VERDICT: FAIL — not profitable out-of-sample. Curve-fit.")
    elif worst >= 0.80:
        print("  VERDICT: ROBUST — OOS within 20% of IS on all quality metrics.")
    elif worst >= 0.60:
        print("  VERDICT: ACCEPTABLE — some decay but the edge clearly persists OOS.")
    else:
        print("  VERDICT: WARNING — meaningful OOS decay. Treat parameters as suspect.")
    print()


if __name__ == "__main__":
    run()
