"""
Topstep $50k combine simulator, v2 — corrected rules + block bootstrap.

Supersedes research/run_combine_sim.py. That version had three defects; this one
fixes them and DECOMPOSES the effect of each so the change in P(pass) is
attributable rather than asserted:

  1. FAILURE THRESHOLD WAS THE WRONG NUMBER (this is the big one).
     v1 failed an attempt whenever a day was worse than BOT_DAILY_LOSS_LIMIT
     (-$750) — the bot's own internal brake. Hitting that brake is a normal risk
     event: the bot stops trading for the day and resumes tomorrow. Topstep only
     fails you below COMBINE_DAILY_LOSS_LIMIT (-$1,000). Measured on the current
     backtest: 2 of 1,848 days breach -$750, ZERO breach -$1,000. So v1 was
     manufacturing failures that cannot happen.

  2. TRAILING DRAWDOWN NEVER LOCKED.
     Topstep's floor trails the peak by $2,000 until the floor would exceed the
     $50,000 starting balance, then it locks there permanently. v1 trailed
     forever, so at a $52.9k peak it enforced a $50.9k floor instead of $50k.
     Since the combine target is $53,000, the peak always crosses the lock point
     on a winning path — this mattered on exactly the paths that were passing.

  3. IID DAY SAMPLING.
     v1 drew each day independently, which breaks up losing streaks. This was the
     defect I expected to dominate. IT DOES NOT — see the measurement below. It is
     fixed anyway so the objection is closed by evidence rather than argument.

WHY THE BLOCK BOOTSTRAP BARELY MOVES THE ANSWER (measured, not assumed):
On the 1,848-day series the daily P&L autocorrelation is ~0.00-0.04 at every lag
tested, and so is the autocorrelation of |P&L| (volatility clustering). A 5,000-run
permutation test — which destroys any real ordering — cannot distinguish the true
day order from shuffled:
    max losing streak : real 10      vs shuffled mean 8.6      p = 0.218
    max drawdown      : real -$1,857 vs shuffled mean -$1,798  p = 0.363
Neither is significant. For THIS strategy's daily series, independent sampling is
close to harmless. That is an empirical property of this data, not a general
licence — re-run `--clustering` after any strategy change before trusting IID.

Run:
    python -X utf8 -m research.run_combine_sim_v2
    python -X utf8 -m research.run_combine_sim_v2 --clustering
"""
import argparse
import io
import sys

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import numpy as np
import src.bot as bot

N_SIMS = 100_000
SEED = 42

PROFIT_TARGET = bot.COMBINE_PROFIT_TARGET       # $3,000
BOT_BRAKE     = bot.BOT_DAILY_LOSS_LIMIT        # -$750  (internal, NOT a failure)
TOPSTEP_DLL   = bot.COMBINE_DAILY_LOSS_LIMIT    # -$1,000 (real failure)
TRAIL_DD      = bot.EOD_LOSS_BUFFER             # $2,000
QUAL_MIN      = bot.XFA_QUALIFYING_DAY_MIN      # $150
QUAL_NEEDED   = bot.XFA_QUALIFYING_DAYS_NEEDED  # 5
INIT          = bot.INIT_CASH                   # $50,000


def _simulate(daily_pnls, intraday_peaks, qual_flags, max_days, *,
              sampler="iid", block_len=10, daily_limit=TOPSTEP_DLL,
              trail_lock=True, consistency_limit=None, n_sims=N_SIMS, seed=SEED):
    """Run n_sims combine attempts.

    sampler          : "iid" | "block" (circular, fixed length) | "stationary" (geometric)
    daily_limit      : day P&L strictly below this fails the attempt
    trail_lock       : if True the trailing floor stops rising once it reaches INIT
    consistency_limit: Topstep's rule that the best single day must be <= this
                       fraction of total profit. None disables it (what v1 did).
                       This is NOT cosmetic for V29: the strategy's edge is
                       concentrated in rare huge days, which is precisely the
                       shape this rule penalises. The LIVE bot enforces it
                       (state.consistency_limit = 0.5) — the simulator did not,
                       so v1 was scoring passes the real account would refuse.
    """
    rng = np.random.default_rng(seed)
    n = len(daily_pnls)
    passes = 0
    fails = {"daily_limit": 0, "trail_dd": 0, "time": 0}
    days_taken = []
    final_equities = []
    troughs = []

    for _ in range(n_sims):
        equity = float(INIT)
        peak_equity = float(INIT)
        worst_equity = float(INIT)
        best_day = 0.0
        qual_days = 0
        day_num = 0
        # block-sampler cursor
        start = int(rng.integers(0, n))
        offset = 0

        while True:
            if sampler == "iid":
                idx = int(rng.integers(0, n))
            elif sampler == "block":
                if offset >= block_len:
                    start = int(rng.integers(0, n))
                    offset = 0
                idx = (start + offset) % n
                offset += 1
            elif sampler == "stationary":
                # Politis-Romano: restart a block with prob 1/block_len,
                # giving geometric block lengths with mean block_len.
                if rng.random() < (1.0 / block_len):
                    start = int(rng.integers(0, n))
                    offset = 0
                else:
                    offset += 1
                idx = (start + offset) % n
            else:
                raise ValueError(f"unknown sampler: {sampler}")

            pnl = float(daily_pnls[idx])
            peak_gain = float(intraday_peaks[idx])
            is_qual = bool(qual_flags[idx])
            day_num += 1

            # Intraday high can lift the trailing peak before the day settles.
            intraday_hi = equity + peak_gain
            if intraday_hi > peak_equity:
                peak_equity = intraday_hi

            equity += pnl
            worst_equity = min(worst_equity, equity)
            best_day = max(best_day, pnl)
            if is_qual:
                qual_days += 1

            # Trailing floor. Topstep stops raising it once it reaches the
            # starting balance (i.e. peak >= INIT + TRAIL_DD).
            floor = peak_equity - TRAIL_DD
            if trail_lock:
                floor = min(floor, INIT)
            if equity < floor:
                fails["trail_dd"] += 1
                final_equities.append(equity)
                troughs.append(worst_equity - INIT)
                break

            if pnl < daily_limit:
                fails["daily_limit"] += 1
                final_equities.append(equity)
                troughs.append(worst_equity - INIT)
                break

            profit = equity - INIT
            # Consistency gate: one outsized day cannot carry the account. If it
            # does, you are not eligible yet and must keep trading to dilute it —
            # so this shows up as extra days, not as an immediate failure.
            consistent = (
                consistency_limit is None
                or profit <= 0
                or best_day <= consistency_limit * profit
            )
            if profit >= PROFIT_TARGET and qual_days >= QUAL_NEEDED and consistent:
                passes += 1
                days_taken.append(day_num)
                final_equities.append(equity)
                troughs.append(worst_equity - INIT)
                break

            if max_days is not None and day_num >= max_days:
                fails["time"] += 1
                final_equities.append(equity)
                troughs.append(worst_equity - INIT)
                break

    return {
        "p_pass": passes / n_sims * 100,
        "days": np.array(days_taken) if days_taken else np.array([0]),
        "fails": fails,
        "final": np.array(final_equities),
        "troughs": np.array(troughs),
        "n_sims": n_sims,
    }


def _load_daily():
    print("Loading data and running backtest...")
    df_raw = bot.fetch_data()
    bot.validate_loaded_data(df_raw)
    df_ind = bot.add_indicators(df_raw)
    sl = bot.compute_session_levels(df_ind)
    df_sig = bot.generate_signals(df_ind.copy())
    _, trades, daily_records, _ = bot.run_backtest(df_sig, sl)
    print(f"  {len(daily_records)} trading days | {len(trades)} trades\n")
    return (
        np.array([d.daily_pnl_net for d in daily_records], dtype=float),
        np.array([d.max_intraday_peak for d in daily_records], dtype=float),
        np.array([d.is_qualifying_day for d in daily_records], dtype=bool),
    )


def _clustering_report(pnls):
    """Measure whether daily P&L actually clusters. Justifies (or refutes) the
    block bootstrap empirically instead of assuming it matters."""
    rng = np.random.default_rng(7)

    def acf(x, k):
        x = x - x.mean()
        return float((x[:-k] * x[k:]).sum() / (x * x).sum())

    def max_lose(x):
        best = cur = 0
        for v in x:
            cur = cur + 1 if v < 0 else 0
            best = max(best, cur)
        return best

    def max_dd(x):
        eq = np.cumsum(x)
        return float((eq - np.maximum.accumulate(eq)).min())

    print("=" * 66)
    print("  CLUSTERING CHECK — is IID sampling actually wrong here?")
    print("=" * 66)
    print(f"  n={len(pnls)}  mean=${pnls.mean():.2f}  sd=${pnls.std():.2f}")
    print("\n  Autocorrelation (0 = no memory):")
    for k in (1, 2, 3, 5, 10):
        print(f"    lag {k:<2}  P&L {acf(pnls, k):+.4f}   |P&L| {acf(np.abs(pnls), k):+.4f}")

    real_streak, real_dd = max_lose(pnls), max_dd(pnls)
    S, D = [], []
    for _ in range(5000):
        q = rng.permutation(pnls)
        S.append(max_lose(q))
        D.append(max_dd(q))
    S, D = np.array(S), np.array(D)
    print("\n  Permutation test (5,000 shuffles destroy any real ordering):")
    print(f"    max losing streak : real {real_streak:<6} shuffled mean {S.mean():.1f}"
          f"    p={(S >= real_streak).mean():.3f}")
    print(f"    max drawdown      : real ${real_dd:<8,.0f} shuffled mean ${D.mean():,.0f}"
          f"  p={(D <= real_dd).mean():.3f}")
    print("\n  p > 0.05 on both => the real day order is indistinguishable from")
    print("  shuffled => IID sampling is close to harmless FOR THIS SERIES.")
    print("=" * 66 + "\n")


def _row(label, r):
    d, f, fin = r["days"], r["fails"], r["final"]
    ev = fin.mean() - INIT
    return (f"  {label:<34} {r['p_pass']:>6.1f}%  {np.median(d):>5.0f}d  "
            f"{f['trail_dd']/r['n_sims']*100:>6.2f}%  {f['daily_limit']/r['n_sims']*100:>6.2f}%  "
            f"${ev:>+8,.0f}")


def main():
    ap = argparse.ArgumentParser(description="Corrected Topstep combine simulator.")
    ap.add_argument("--clustering", action="store_true",
                    help="Only run the clustering diagnostic.")
    ap.add_argument("--sims", type=int, default=N_SIMS)
    args = ap.parse_args()

    pnls, peaks, quals = _load_daily()

    if args.clustering:
        _clustering_report(pnls)
        return
    _clustering_report(pnls)

    ns = args.sims
    print("=" * 90)
    print(f"  DECOMPOSITION — what each correction does  ({ns:,} runs each, no time limit)")
    print("=" * 90)
    print(f"  {'configuration':<34} {'P(pass)':>7}  {'med':>6}  {'trailDD':>7}  {'dayLim':>7}  {'E[$/attempt]':>12}")
    print("  " + "-" * 86)

    a = _simulate(pnls, peaks, quals, None, sampler="iid",
                  daily_limit=BOT_BRAKE, trail_lock=False, n_sims=ns)
    print(_row("A  v1 LEGACY (reproduces old #)", a))

    b = _simulate(pnls, peaks, quals, None, sampler="iid",
                  daily_limit=TOPSTEP_DLL, trail_lock=False, n_sims=ns)
    print(_row("B  + real Topstep DLL (-$1000)", b))

    c = _simulate(pnls, peaks, quals, None, sampler="iid",
                  daily_limit=TOPSTEP_DLL, trail_lock=True, n_sims=ns)
    print(_row("C  + trailing-DD lock at $50k", c))

    d = _simulate(pnls, peaks, quals, None, sampler="block", block_len=10,
                  daily_limit=TOPSTEP_DLL, trail_lock=True, n_sims=ns)
    print(_row("D  + block bootstrap (L=10)", d))

    e = _simulate(pnls, peaks, quals, None, sampler="block", block_len=10,
                  daily_limit=TOPSTEP_DLL, trail_lock=True,
                  consistency_limit=0.50, n_sims=ns)
    print(_row("E  + CONSISTENCY RULE (50%)  <=", e))

    f = _simulate(pnls, peaks, quals, 60, sampler="block", block_len=10,
                  daily_limit=TOPSTEP_DLL, trail_lock=True,
                  consistency_limit=0.50, n_sims=ns)
    print(_row("F  + 60-day cap (lab-judge rules)", f))
    print("  " + "-" * 86)
    print(f"  A -> D (my predicted fix) : {d['p_pass'] - a['p_pass']:+.1f} pp")
    print(f"  D -> E (consistency rule) : {e['p_pass'] - d['p_pass']:+.1f} pp"
          f"   <- the rule v1 ignored entirely")
    print(f"  A -> F total              : {f['p_pass'] - a['p_pass']:+.1f} pp")

    print("\n  Block-length sensitivity (all with corrected rules):")
    for L in (5, 10, 20, 40):
        r = _simulate(pnls, peaks, quals, None, sampler="block", block_len=L,
                      daily_limit=TOPSTEP_DLL, trail_lock=True, n_sims=ns)
        print(_row(f"   block L={L}", r))
    r = _simulate(pnls, peaks, quals, None, sampler="stationary", block_len=10,
                  daily_limit=TOPSTEP_DLL, trail_lock=True, n_sims=ns)
    print(_row("   stationary (mean L=10)", r))

    print("\n" + "=" * 90)
    print("  DOLLAR VIEW — most complete config (F: corrected rules + consistency + 60d)")
    print("=" * 90)
    fin, tr, dd = f["final"], f["troughs"], f["days"]
    print(f"  P(pass)                      : {f['p_pass']:.1f}%")
    print(f"  Expected attempts to pass    : {100/f['p_pass']:.2f}")
    print(f"  E[account change per attempt]: ${fin.mean()-INIT:+,.0f}")
    print(f"  Median days to pass          : {np.median(dd):.0f} trading days"
          f"  (~{np.median(dd)*7/5:.0f} calendar)")
    print(f"  25th / 75th pct days to pass : {np.percentile(dd,25):.0f} / {np.percentile(dd,75):.0f}")
    print("\n  Final account value distribution:")
    for p in (5, 25, 50, 75, 95):
        print(f"    {p:>2}th pct : ${np.percentile(fin,p):>10,.0f}"
              f"   ({np.percentile(fin,p)-INIT:>+8,.0f})")
    print("\n  Worst equity trough reached during an attempt:")
    for p in (5, 50, 95):
        print(f"    {p:>2}th pct : ${np.percentile(tr,p):>+8,.0f}")
    print(f"    absolute worst : ${tr.min():+,.0f}   (Topstep floor is -$2,000)")
    print("\n  Failure modes:")
    for k, v in f["fails"].items():
        print(f"    {k:<12}: {v/ns*100:>6.2f}%")
    print("=" * 90)


if __name__ == "__main__":
    main()
