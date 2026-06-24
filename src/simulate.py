"""
╔══════════════════════════════════════════════════════════════╗
║  REGIME SIMULATION — robustness scorecard (no real data)     ║
║                                                              ║
║  Generates many SYNTHETIC market paths across distinct       ║
║  regimes (trend up/down, choppy range, high/low volatility)  ║
║  and runs every candidate through the shared honest engine.  ║
║                                                              ║
║  WHAT THIS TELLS YOU (honest):                               ║
║   - ROBUSTNESS: does a strategy avoid blowing up across      ║
║     many different market conditions?                        ║
║   - BEHAVIOUR: does it trade enough, and act as intended?    ║
║                                                              ║
║  WHAT THIS DOES NOT TELL YOU:                                ║
║   - Real profitability. P&L on synthetic data mostly         ║
║     reflects the regime baked in, NOT a real market edge.    ║
║     Only YOUR 7-year data (src/evaluate.py) can judge edge.  ║
║                                                              ║
║  Run:  python -m src.simulate                                ║
╚══════════════════════════════════════════════════════════════╝
"""
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

from src import bot
from src import strategies
from src import evaluate

TICK = bot.MNQ_TICK_SIZE
DAYS_PER_SIM = 90            # ~4.5 months of trading days per simulation
SEEDS_PER_REGIME = 8        # independent random paths per regime
BARS_PER_DAY = 78          # 5-min RTH bars, 09:30-16:00 Eastern

# Each regime is a (drift, noise_sd, mean_revert_theta) recipe per 5-min bar.
REGIMES = {
    "trend_up":    dict(drift=+0.55, sd=4.0, theta=0.0),
    "trend_down":  dict(drift=-0.55, sd=4.0, theta=0.0),
    "choppy":      dict(drift=0.0,  sd=4.0, theta=0.06),   # mean-reverts to day open
    "high_vol":    dict(drift=0.0,  sd=10.0, theta=0.0),
    "low_vol":     dict(drift=0.0,  sd=1.5, theta=0.0),
}


def _gen_path(regime: dict, seed: int) -> pd.DataFrame:
    """Build a synthetic OHLCV df: tz-aware US/Eastern, 5-min RTH bars."""
    rng = np.random.default_rng(seed)
    start = pd.Timestamp("2020-01-02", tz="US/Eastern")
    days = pd.bdate_range(start, periods=DAYS_PER_SIM)

    idx = []
    o_, h_, l_, c_, v_ = [], [], [], [], []
    price = 18000.0
    for d in days:
        session = pd.date_range(d + pd.Timedelta(hours=9, minutes=30),
                                periods=BARS_PER_DAY, freq="5min", tz="US/Eastern")
        day_open = price
        for ts in session:
            mean_pull = regime["theta"] * (day_open - price)
            step = regime["drift"] + mean_pull + rng.normal(0, regime["sd"])
            o = price
            c = o + step
            wick = abs(rng.normal(0, regime["sd"] * 0.6))
            hi = max(o, c) + wick
            lo = min(o, c) - wick
            o, c, hi, lo = (round(x / TICK) * TICK for x in (o, c, hi, lo))
            hi = max(hi, o, c); lo = min(lo, o, c)
            idx.append(ts); o_.append(o); h_.append(hi); l_.append(lo); c_.append(c)
            v_.append(int(abs(rng.normal(500, 150))) + 1)
            price = c

    df = pd.DataFrame({"open": o_, "high": h_, "low": l_, "close": c_, "volume": v_},
                      index=pd.DatetimeIndex(idx, name="ts_event"))
    return df


def _run_one(df: pd.DataFrame, candidate) -> dict:
    df = bot.add_indicators(df)
    df = bot.generate_signals(df)
    trades, _ = evaluate.backtest(df, candidate)
    n = len(trades)
    net = sum(t.pnl_usd for t in trades)
    wins = sum(1 for t in trades if t.won)
    return {"n": n, "net": net, "win_rate": (wins / n * 100) if n else 0.0}


def main() -> None:
    print("=" * 78)
    print("  REGIME SIMULATION — robustness across synthetic market conditions")
    print("=" * 78)
    print(f"  {len(REGIMES)} regimes x {SEEDS_PER_REGIME} random paths x {DAYS_PER_SIM} days each")
    print("  NOTE: synthetic data. Reads ROBUSTNESS/behaviour, NOT real profitability.\n")

    candidate_names = [c.name for c in strategies.build_candidates()]
    # results[name][regime] = list of per-sim dicts
    results = {name: defaultdict(list) for name in candidate_names}

    for regime_name, recipe in REGIMES.items():
        regime_offset = list(REGIMES).index(regime_name) * 10_000
        for seed in range(SEEDS_PER_REGIME):
            df = _gen_path(recipe, regime_offset + seed * 101)
            for cand in strategies.build_candidates():
                results[cand.name][regime_name].append(_run_one(df, cand))
        print(f"  simulated regime: {regime_name}")

    # ── Per-regime scorecard: average NET per sim, and % of sims profitable ─────
    print("\n" + "=" * 78)
    print("  SCORECARD — average net $ per simulation (% of sims profitable)")
    print("=" * 78)
    header = f"  {'Candidate':<18}" + "".join(f"{r:>13}" for r in REGIMES)
    print(header)
    for name in candidate_names:
        cells = []
        for regime_name in REGIMES:
            sims = results[name][regime_name]
            avg_net = np.mean([s["net"] for s in sims]) if sims else 0.0
            pct_prof = np.mean([s["net"] > 0 for s in sims]) * 100 if sims else 0.0
            cells.append(f"{avg_net:>7,.0f}({pct_prof:>3.0f}%)")
        print(f"  {name:<18}" + "".join(f"{c:>13}" for c in cells))

    # ── Average trades/sim (frequency sanity) ──────────────────────────────────
    print("\n  Average trades per simulation (frequency check):")
    print(f"  {'Candidate':<18}" + "".join(f"{r:>13}" for r in REGIMES))
    for name in candidate_names:
        cells = [f"{np.mean([s['n'] for s in results[name][r]]):>11.0f}" for r in REGIMES]
        print(f"  {name:<18}" + "".join(f"{c:>13}" for c in cells))

    # ── Robustness summary: worst-regime avg net + regimes survived ────────────
    print("\n" + "=" * 78)
    print("  ROBUSTNESS SUMMARY")
    print("=" * 78)
    print(f"  {'Candidate':<18}{'Worst regime (avg net)':>28}{'Regimes net+':>14}{'Total trades':>14}")
    for name in candidate_names:
        regime_avgs = {r: np.mean([s["net"] for s in results[name][r]]) for r in REGIMES}
        worst_r = min(regime_avgs, key=regime_avgs.get)
        regimes_pos = sum(1 for r in REGIMES if regime_avgs[r] > 0)
        total_tr = sum(sum(s["n"] for s in results[name][r]) for r in REGIMES)
        print(f"  {name:<18}{worst_r + ' $' + format(regime_avgs[worst_r], ',.0f'):>28}"
              f"{str(regimes_pos) + '/' + str(len(REGIMES)):>14}{total_tr:>14}")

    print("\n  HOW TO READ THIS:")
    print("  - A robust strategy is profitable (or at least not badly negative) across")
    print("    MOST regimes and never catastrophic in its worst one.")
    print("  - A strategy that only wins in one regime is fragile — it needs that exact")
    print("    market to keep showing up, which it won't.")
    print("  - These are SYNTHETIC results. The real test is src/evaluate.py on your data.")
    print("=" * 78)


if __name__ == "__main__":
    sys.exit(main())
