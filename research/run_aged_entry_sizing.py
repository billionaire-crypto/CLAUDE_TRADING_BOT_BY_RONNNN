"""
C1 — aged FVG entries at tail-matched size. Spec FROZEN (ledger 2026-07-26).

The gap in the evidence this closes
-----------------------------------
run_age_sweep.py (2026-07-07) walked FVG_MAX_AGE_BARS 4->5->6->7 and found a
clean monotone trade-off: net +7.5% at age 5 (+288 trades) but worst trade
-$983 -> -$1,246 and combine pass 97.7% -> 95.9%. It was REJECTED on the SAFETY
gates only, never on P&L.

That sweep mutated exactly one attribute and inherited RISK_BUDGET_SIZING_ENABLED
intact -- so an age-5 entry was sized from the same per-score budget map
({8:900, 7:675, 6:350}) as a fresh age-1 entry. Under risk-budget sizing a
-$1,246 realized loss on a <=$900 budgeted stop IS the gap-through overshoot,
and it scales linearly with size. So: trim the budget on aged entries and the
tail should scale down with it, while most of the added frequency survives.

Why this needs its own script
-----------------------------
The candidate is a PAIR of parameters (FVG_MAX_AGE_BARS=5 AND the aged-entry
trim). nightly_researcher.py sets exactly one attribute, so running this through
it would leave the age gate at 4, admit no aged entries at all, and report a
no-op as a clean "no change" -- a misleading result in the ledger. Hence the
dedicated runner. Verdict logic is still imported from nightly_researcher so the
rules are byte-identical to every other candidate.

PRE-REGISTERED pass condition -- ALL FOUR, no exceptions:
  1. worst modeled trade <= -$983   (the BASELINE, not merely better than -1246)
  2. combine pass rate    >= baseline - 1.0pp
  3. net >= 95% of baseline in EVERY one of the three splits
  4. trade count strictly ABOVE baseline (if it isn't, the point is gone)

KILL CONDITION: if mult 0.50 fails the tail gate, do NOT walk the grid downward
hunting for a passing value. Two values, then stop. That is the tuning failure
mode TDC and SPC were killed for.

Run from the repo root:  python -m research.run_aged_entry_sizing
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np

import src.bot as bot
from research.run_combine_sim import _simulate
from research.nightly_researcher import _metrics, _slice, decide

PERIODS = [("dev_2019_2022", "2019-01-01", "2022-12-31"),
           ("val1_2023_2024", "2023-01-01", "2024-12-31"),
           ("val2_2025_2026", "2025-01-01", "2026-12-31")]

CANDIDATE_AGE = 5
MULT_GRID = [0.50, 0.35]          # frozen. Do not extend.
TAIL_GATE_ABSOLUTE = True         # worst trade must beat the BASELINE, not age-5


def main() -> None:
    df = bot.fetch_data()
    bot.validate_loaded_data(df)
    di = bot.add_indicators(df)
    sl = bot.compute_session_levels(di)

    def backtest(**overrides):
        saved = {k: getattr(bot, k) for k in overrides}
        for k, v in overrides.items():
            setattr(bot, k, v)
        try:
            ds = bot.generate_signals(di.copy())
            _, trades, days, _ = bot.run_backtest(ds, sl)
        finally:
            for k, v in saved.items():
                setattr(bot, k, v)     # ALWAYS restore, even on exception
        return trades, days

    def combine(days):
        dp = np.array([d.daily_pnl_net for d in days])
        ip = np.array([d.max_intraday_peak for d in days])
        qf = np.array([d.is_qualifying_day for d in days], dtype=bool)
        p, days_arr, fails = _simulate(dp, ip, qf, None)
        return {"pass_rate": p,
                "median_days": float(np.median(days_arr)) if len(days_arr) else 0,
                "daily_limit_fail_pct": fails["daily_limit"] / 1000}

    # ── baselines: shipped config, and age-5-without-trim (the 07-07 reject) ──
    tr_base, dr_base = backtest()
    m_base, c_base = _metrics(tr_base), combine(dr_base)

    tr_a5, dr_a5 = backtest(FVG_MAX_AGE_BARS=CANDIDATE_AGE)
    m_a5, c_a5 = _metrics(tr_a5), combine(dr_a5)

    x3 = [(c, t * 3) for c, t in bot.SLIPPAGE_SCALE_TIERS]

    hdr = (f"{'variant':>22}{'net':>12}{'PF':>6}{'n':>7}{'WR':>7}{'worst':>9}"
           f"{'slipx3 PF':>11}{'combine%':>10}{'DLLfail%':>10}")
    print("=" * len(hdr))
    print(f"C1 — aged-entry tail-matched sizing (age {CANDIDATE_AGE}, grid {MULT_GRID})")
    print("=" * len(hdr))
    print(hdr)
    print("-" * len(hdr))

    def row(label, m, c, s3pf="--"):
        s3 = f"{s3pf:>11.2f}" if isinstance(s3pf, float) else f"{s3pf:>11}"
        print(f"{label:>22}{m['net']:>12,.0f}{m['pf']:>6.2f}{m['n']:>7}{m['wr']:>6.1f}%"
              f"{m['worst']:>9,.0f}{s3}{c['pass_rate']:>9.1f}%"
              f"{c['daily_limit_fail_pct']:>9.2f}%")

    row("BASELINE (age 4)", m_base, c_base)
    row("age 5, no trim", m_a5, c_a5)

    results = []
    for mult in MULT_GRID:
        ov = {"FVG_MAX_AGE_BARS": CANDIDATE_AGE, "FVG_AGED_ENTRY_RISK_MULT": mult}
        tr, dr = backtest(**ov)
        m, c = _metrics(tr), combine(dr)
        s3, _ = backtest(**ov, SLIPPAGE_SCALE_TIERS=x3)
        s3m = _metrics(s3)
        row(f"age 5, mult {mult:.2f}", m, c, s3m["pf"])

        walk = [w for w in (_slice(tr, f"{y}-{a}", f"{y}-{b}")
                            for y in range(2019, 2027)
                            for a, b in (("01-01", "06-30"), ("07-01", "12-31")))
                if w["n"] > 0]

        g = {"param": f"FVG_AGED_ENTRY_RISK_MULT@age{CANDIDATE_AGE}",
             "baseline_value": 1.0, "candidate_value": mult,
             "split": {n: {"base": _slice(tr_base, d0, d1), "cand": _slice(tr, d0, d1)}
                       for n, d0, d1 in PERIODS},
             "walk_forward": walk,
             "stress": {"slip_x3": s3m},
             "combine_base": c_base, "combine_cand": c,
             "worst_trade_base": m_base["worst"], "worst_trade_cand": m["worst"]}
        verdict, reasons = decide(g)

        # Gate 4 is specific to this candidate and not part of decide()'s
        # generic rules: more frequency is the entire point of the change.
        if m["n"] <= m_base["n"]:
            verdict = "REJECT"
            reasons = reasons + [f"trade count {m['n']} not above baseline {m_base['n']} "
                                 f"— the frequency rationale is gone"]
        results.append((mult, verdict, reasons, m, c))

    print("=" * len(hdr))
    for mult, verdict, reasons, m, c in results:
        print(f"\nmult {mult:.2f} -> {verdict}")
        for r in reasons:
            print(f"    - {r}")
        if verdict == "SHIP":
            print(f"    net {m['net']:,.0f} ({m['n']} trades, +{m['n'] - m_base['n']} vs baseline), "
                  f"worst {m['worst']:,.0f}, combine {c['pass_rate']:.1f}%")

    print("\nReminder: a SHIP here is a CANDIDATE for human review. This script "
          "never edits src/bot.py, and the frozen grid stops at 0.35 — do not "
          "extend it hunting for a pass.")


if __name__ == "__main__":
    main()
