"""
╔══════════════════════════════════════════════════════════════╗
║  COMBINE MONTE-CARLO — the selection judge                   ║
║                                                              ║
║  Money on Topstep comes from PASSING the combine, staying    ║
║  funded, and collecting payouts — NOT from raw backtest P&L. ║
║  A strategy with positive average profit can still fail most ║
║  combines if a losing streak hits the −$2,000 trailing       ║
║  drawdown before the +$3,000 target.                         ║
║                                                              ║
║  So we bootstrap a strategy's REAL daily-P&L distribution    ║
║  through thousands of simulated combine attempts under the   ║
║  actual rules, and report:                                   ║
║    P(pass) · P(reach payout) · P(blow up) · median days      ║
║  plus the N-account expected-value math (the scaling plan).  ║
║                                                              ║
║  HONEST LIMITS: daily-resolution (no intraday path), so the  ║
║  trailing-floor breach is checked at end-of-day — it slightly ║
║  UNDER-counts intraday blow-ups. Bootstrap assumes days are  ║
║  independent (ignores autocorrelation/streak clustering).    ║
║  Treat P(pass) as an optimistic-ish estimate, not a promise. ║
╚══════════════════════════════════════════════════════════════╝
"""
import sys
from dataclasses import dataclass

import numpy as np


@dataclass
class TopstepRules:
    """Defaults = Topstep 50K Combine / Express Funded (2025-2026)."""
    target: float = 3000.0          # combine profit target
    trailing_mll: float = 2000.0    # max loss limit, trails EOD highs, locks at start
    daily_loss_limit: float = 1000.0
    consistency: float = 0.50       # best single day <= 50% of profit
    min_days: int = 2               # combine minimum trading days
    max_days: int = 60              # give-up horizon for the sim
    eval_fee: float = 49.0          # ~monthly combine cost (50K)
    # Funded-phase payout proxy: build a buffer over >= N winning days.
    payout_target: float = 2000.0
    payout_min_win_days: int = 5
    payout_amount: float = 2000.0   # modeled cash collected on a successful payout


def trades_to_daily_pnl(trades) -> np.ndarray:
    """Collapse a list of Trade objects into net P&L per trading day (date-ordered)."""
    by_day = {}
    for t in trades:
        d = getattr(t, "date", None)
        key = d.date() if hasattr(d, "date") else d
        by_day[key] = by_day.get(key, 0.0) + t.pnl_usd
    return np.array([by_day[k] for k in sorted(by_day)], dtype=float)


def _run_phase(rng, daily_pool, rules, start_profit, target, max_days,
               need_win_days=0):
    """Simulate one funding phase. Returns ('pass'|'blowup'|'timeout', days, best_day)."""
    profit = start_profit
    hwm = max(0.0, start_profit)
    floor = min(hwm - rules.trailing_mll, 0.0)
    best_day = 0.0
    win_days = 0
    for day in range(1, max_days + 1):
        pnl = float(rng.choice(daily_pool))
        pnl = max(pnl, -rules.daily_loss_limit)     # daily loss limit caps the loss
        profit += pnl
        if pnl > 0:
            win_days += 1
        best_day = max(best_day, pnl)
        if profit <= floor:                          # trailing drawdown breached (EOD)
            return "blowup", day, best_day
        hwm = max(hwm, profit)
        floor = min(hwm - rules.trailing_mll, 0.0)
        consistent = best_day <= rules.consistency * target if target > 0 else True
        if profit >= target and day >= rules.min_days and consistent and win_days >= need_win_days:
            return "pass", day, best_day
    return "timeout", max_days, best_day


def monte_carlo(daily_pnls, n_iter=5000, rules: TopstepRules = None, seed=7) -> dict:
    rules = rules or TopstepRules()
    pool = np.asarray(daily_pnls, dtype=float)
    if pool.size == 0:
        return {"error": "no trading days", "n_days": 0}
    rng = np.random.default_rng(seed)

    passes = blowups = timeouts = payouts = 0
    days_to_pass = []
    for _ in range(n_iter):
        outcome, days, _ = _run_phase(rng, pool, rules, 0.0, rules.target, rules.max_days)
        if outcome == "pass":
            passes += 1
            days_to_pass.append(days)
            # Funded phase: build the payout buffer without blowing up.
            f_outcome, _, _ = _run_phase(rng, pool, rules, 0.0, rules.payout_target,
                                         rules.max_days, need_win_days=rules.payout_min_win_days)
            if f_outcome == "pass":
                payouts += 1
        elif outcome == "blowup":
            blowups += 1
        else:
            timeouts += 1

    p_pass = passes / n_iter
    p_payout = payouts / n_iter
    return {
        "n_days": int(pool.size),
        "avg_daily": float(pool.mean()),
        "p_pass": p_pass,
        "p_payout": p_payout,
        "p_blowup": blowups / n_iter,
        "p_timeout": timeouts / n_iter,
        "median_days_to_pass": float(np.median(days_to_pass)) if days_to_pass else None,
        "rules": rules,
    }


def n_account_table(stats: dict, accounts=(1, 2, 5, 10)) -> list:
    """Expected value of running the SAME proven config across N accounts."""
    rules = stats["rules"]
    rows = []
    for n in accounts:
        exp_passes = n * stats["p_pass"]
        exp_payout_cash = n * stats["p_payout"] * rules.payout_amount
        cost = n * rules.eval_fee
        rows.append({
            "accounts": n,
            "exp_passes": exp_passes,
            "exp_payout_cash": exp_payout_cash,
            "eval_cost": cost,
            "exp_net": exp_payout_cash - cost,
        })
    return rows


def print_report(name: str, stats: dict) -> None:
    if stats.get("error"):
        print(f"  {name}: {stats['error']}")
        return
    print(f"\n  ── Combine Monte-Carlo: {name} ──")
    print(f"  trading days sampled: {stats['n_days']}   avg/day: ${stats['avg_daily']:,.0f}")
    print(f"  P(pass combine):  {stats['p_pass']*100:5.1f}%")
    print(f"  P(reach payout):  {stats['p_payout']*100:5.1f}%")
    print(f"  P(blow up):       {stats['p_blowup']*100:5.1f}%")
    print(f"  P(timeout):       {stats['p_timeout']*100:5.1f}%")
    mdtp = stats["median_days_to_pass"]
    print(f"  median days to pass: {mdtp:.0f}" if mdtp else "  median days to pass: n/a")
    print(f"  {'accts':>6}{'E[passes]':>12}{'E[payout $]':>14}{'eval cost':>12}{'E[net $]':>12}")
    for r in n_account_table(stats):
        print(f"  {r['accounts']:>6}{r['exp_passes']:>12.2f}{r['exp_payout_cash']:>14,.0f}"
              f"{r['eval_cost']:>12,.0f}{r['exp_net']:>12,.0f}")


def main() -> None:
    # Standalone demo on synthetic daily P&L (proves the machinery; not real data).
    rng = np.random.default_rng(1)
    # A modest-edge daily distribution: ~58% green days, small wins, occasional bad day.
    demo = np.where(rng.random(400) < 0.58,
                    rng.normal(180, 90, 400),
                    rng.normal(-220, 110, 400))
    print("=" * 70)
    print("  COMBINE MONTE-CARLO — standalone demo (SYNTHETIC daily P&L)")
    print("=" * 70)
    print_report("demo-strategy", monte_carlo(demo, n_iter=5000))
    print("\n  (Synthetic demo only — real numbers come from a strategy's backtested days.)")
    print("=" * 70)


if __name__ == "__main__":
    sys.exit(main())
