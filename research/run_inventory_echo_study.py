"""
Unfinished Inventory Hypothesis (DES) study — pre-registered 2026-07-20.
See RESEARCH_LEDGER.md entry "PRE-REGISTRATION: Unfinished Inventory Hypothesis"
for the hypothesis and kill criteria, registered BEFORE this script produced
any numbers.

Rules of this study (verbatim from the mission):
  - No optimization. No threshold tuning. No fitted weights.
  - Formulas exactly as proposed. Fixed windows (50-bar BVC sigma, 1-year
    percentile history, >=100 min observations).
  - The identical FVG trade set from the frozen V29 backtest (deterministic
    re-run; no signal/entry/exit changes).
  - If the hypothesis fails, report that honestly.

Run from repo root:
    python -m research.run_inventory_echo_study
"""

import os
import sys
import math
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import src.bot as bot

RESEARCH_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_CSV = os.path.join(RESEARCH_DIR, "inventory_echo_trades.csv")

BVC_SIGMA_WINDOW = 50       # fixed, per pre-registration
PCTILE_WINDOW_DAYS = 365    # fixed
PCTILE_MIN_OBS = 100        # fixed
N_PERMUTATIONS = 10_000
N_BOOTSTRAP = 10_000
RNG = np.random.default_rng(42)   # reproducibility only; not a tuned choice


# ── 1-minute data loading (front-month by volume, RTH, US/Eastern) ────────────

def load_1min_rth() -> pd.DataFrame:
    frames = []
    paths = [bot.DATA_PATH] + [p for p in bot.DATA_PATH_EXTENSIONS if os.path.exists(p)]
    for path in paths:
        print(f"Loading 1-min CSV: {os.path.basename(path)} ...")
        df = pd.read_csv(path, parse_dates=["ts_event"])
        df = df[["ts_event", "open", "high", "low", "close", "volume", "symbol"]]
        df["ts_event"] = pd.to_datetime(df["ts_event"], utc=True).dt.tz_convert("US/Eastern")
        frames.append(df)
    df = pd.concat(frames, ignore_index=True)
    # Front month per minute = highest-volume contract at that timestamp
    df = df.sort_values(["ts_event", "volume"])
    df = df.drop_duplicates(subset="ts_event", keep="last")
    df = df.set_index("ts_event").sort_index()
    df = df.between_time("09:30", "16:00")
    df = df[~df.index.duplicated(keep="last")]
    print(f"1-min RTH bars: {len(df):,}  ({df.index[0]} -> {df.index[-1]})")
    return df[["open", "high", "low", "close", "volume"]]


# ── Feature computation ──────────────────────────────────────────────────────

def compute_bvc_signed_volume(m1: pd.DataFrame) -> pd.DataFrame:
    """Bulk Volume Classification on the 1-min series.
    sv_t = V_t * (2*Phi(dP_t / sigma) - 1), sigma = trailing 50-bar std of dP."""
    from scipy.stats import norm
    m1 = m1.copy()
    m1["dp"] = m1["close"].diff()
    m1["sigma"] = m1["dp"].rolling(BVC_SIGMA_WINDOW, min_periods=30).std()
    z = m1["dp"] / m1["sigma"]
    buy_frac = pd.Series(norm.cdf(z.fillna(0.0)), index=m1.index)
    buy_frac[m1["sigma"].isna() | (m1["sigma"] == 0)] = 0.5   # uninformative
    m1["sv"] = m1["volume"] * (2.0 * buy_frac - 1.0)
    # per-bar absolute return and impact |r|/V (guard zero volume)
    m1["absr"] = m1["dp"].abs()
    m1["impact"] = np.where(m1["volume"] > 0, m1["absr"] / m1["volume"], np.nan)
    return m1


def window_features(m1: pd.DataFrame, imp_start, imp_end, ret_start, ret_end,
                    direction: str):
    """F1, F2, F3 for one trade. Windows are [start, end) in Eastern time.
    Returns (f1, f2, f3) with NaN where undefined."""
    I = m1.loc[(m1.index >= imp_start) & (m1.index < imp_end)]
    R = m1.loc[(m1.index >= ret_start) & (m1.index < ret_end)]
    if len(I) == 0 or len(R) == 0:
        return np.nan, np.nan, np.nan, len(I), len(R)

    mean_vi = I["volume"].mean()
    mean_vr = R["volume"].mean()
    f1 = (mean_vr / mean_vi) if mean_vi > 0 else np.nan

    sum_v = R["volume"].sum()
    if sum_v > 0:
        net_sv = R["sv"].sum()
        # counterflow AGAINST the FVG direction: bullish -> selling; bearish -> buying
        f2 = (-net_sv / sum_v) if direction == "long" else (net_sv / sum_v)
    else:
        f2 = np.nan

    lam_i = I["impact"].mean()
    lam_r = R["impact"].mean()
    f3 = (lam_r / lam_i) if (lam_i and lam_i > 0 and not math.isnan(lam_i)) else np.nan
    return f1, f2, f3, len(I), len(R)


def trailing_percentile(history: list, value: float) -> float:
    """Percentile rank of value among history (list of floats)."""
    arr = np.asarray(history)
    return float((arr <= value).mean())


# ── Analysis helpers ─────────────────────────────────────────────────────────

def quintile_stats(dfq: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for q in sorted(dfq["quintile"].unique()):
        g = dfq[dfq["quintile"] == q]
        pnl = g["pnl_ct"].values
        wins = g["won"].sum()
        gp = pnl[pnl > 0].sum()
        gl = -pnl[pnl < 0].sum()
        eq = np.cumsum(pnl)
        peak = np.maximum.accumulate(eq)
        maxdd = float((eq - peak).min()) if len(eq) else 0.0
        rows.append({
            "quintile": q, "n": len(g),
            "win_rate": 100.0 * wins / len(g),
            "avg_pnl_ct": pnl.mean(), "med_pnl_ct": float(np.median(pnl)),
            "pf": (gp / gl) if gl > 0 else float("inf"),
            "expectancy": pnl.mean(),
            "maxdd_ct": maxdd,
            "sharpe_trade": pnl.mean() / pnl.std() if pnl.std() > 0 else 0.0,
            "avg_mae": g["mae"].mean(), "avg_mfe": g["mfe"].mean(),
        })
    return pd.DataFrame(rows)


def spearman(x, y):
    from scipy.stats import spearmanr
    r, p = spearmanr(x, y)
    return float(r), float(p)


def ols_with_t(y, X):
    """OLS via numpy; returns (betas, t_stats, r2). X includes intercept col."""
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    n, k = X.shape
    s2 = (resid @ resid) / (n - k)
    cov = s2 * np.linalg.inv(X.T @ X)
    t = beta / np.sqrt(np.diag(cov))
    ss_tot = ((y - y.mean()) ** 2).sum()
    r2 = 1 - (resid @ resid) / ss_tot if ss_tot > 0 else 0.0
    return beta, t, r2


def t_to_p(t, dof):
    from scipy.stats import t as tdist
    return float(2 * (1 - tdist.cdf(abs(t), dof)))


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 72)
    print("UNFINISHED INVENTORY HYPOTHESIS -- DES STUDY (pre-registered)")
    print("=" * 72)

    # 1. Reproduce the frozen backtest trade set (deterministic).
    print("\n[1/6] Reproducing frozen V29 backtest ...")
    df5 = bot.fetch_data()
    df5 = bot.add_indicators(df5)
    df5 = bot.generate_signals(df5)
    levels = bot.compute_session_levels(df5)
    _, trades, _, _ = bot.run_backtest(df5, levels)
    fvg = [t for t in trades if t.entry_type == "FVG"]
    print(f"FVG trades reproduced: {len(fvg):,}")

    pos_of = {ts: i for i, ts in enumerate(df5.index)}

    # 2. Map each trade to entry/creation bars; validate mapping.
    print("\n[2/6] Mapping trades to bar positions ...")
    mapped, hour_ok = [], 0
    for t in fvg:
        p_exit = pos_of.get(t.date)
        if p_exit is None:
            continue
        p_entry = p_exit - int(t.bars_to_exit)
        if p_entry < 2 or p_entry >= len(df5):
            continue
        ts_entry = df5.index[p_entry]
        # validation: recorded entry_hour is CT = Eastern - 1
        if ts_entry.hour - 1 == int(t.entry_hour):
            hour_ok += 1
        age = int(t.fvg_age_bars)
        p_created = p_entry - age
        if p_created < 2:
            continue
        mapped.append((t, p_entry, p_created))
    print(f"Mapped {len(mapped):,} trades; entry-hour validation: "
          f"{100.0 * hour_ok / max(1, len(mapped)):.1f}% match")
    if hour_ok / max(1, len(mapped)) < 0.95:
        print("WARNING: entry-bar mapping validation below 95% -- results suspect.")

    # 3. Load 1-min data and compute BVC.
    print("\n[3/6] Loading 1-minute data + BVC ...")
    m1 = load_1min_rth()
    m1 = compute_bvc_signed_volume(m1)

    # 4. Per-trade features.
    print("\n[4/6] Computing F1/F2/F3 per trade ...")
    bar = pd.Timedelta(minutes=5)
    recs = []
    for t, p_entry, p_created in mapped:
        ts_created = df5.index[p_created]     # 5-min bar START of creation bar
        ts_entry = df5.index[p_entry]
        imp_start = df5.index[p_created - 2]
        imp_end = ts_created + bar
        ret_start = imp_end
        ret_end = ts_entry                     # entry bar excluded (no leakage)
        f1, f2, f3, ni, nr = window_features(
            m1, imp_start, imp_end, ret_start, ret_end, t.direction)
        recs.append({
            "ts_entry": ts_entry, "year": ts_entry.year,
            "direction": t.direction, "age": int(t.fvg_age_bars),
            "pnl_ct": t.pnl_usd / t.contracts if t.contracts else np.nan,
            "pnl_usd": t.pnl_usd, "contracts": t.contracts,
            "won": bool(t.won), "mae": t.mae, "mfe": t.mfe,
            "score": int(t.fvg_quality_score), "flags": t.fvg_quality_flags,
            "size_ticks": t.fvg_size_ticks, "adx": t.adx_at_entry,
            "atr_ratio": t.atr_ratio_at_entry, "entry_hour": int(t.entry_hour),
            "adx_regime": t.bar_adx_regime, "news": bool(t.is_news_day),
            "f1": f1, "f2": f2, "f3": f3, "n_imp_1m": ni, "n_ret_1m": nr,
        })
    d = pd.DataFrame(recs).sort_values("ts_entry").reset_index(drop=True)
    have_feat = d[["f1", "f2", "f3"]].notna().all(axis=1)
    print(f"Trades with defined features: {have_feat.sum():,} / {len(d):,} "
          f"({100.0 * have_feat.mean():.1f}%)")
    print("Age distribution of ALL FVG trades:")
    print(d["age"].value_counts().sort_index().to_string())

    # 5. DES via trailing 1-year percentiles (walk-forward, no lookahead).
    print("\n[5/6] Computing DES (trailing 1-yr percentiles, min 100 obs) ...")
    hist_ts, hist_f1, hist_f2, hist_f3 = [], [], [], []
    des_vals = []
    win = pd.Timedelta(days=PCTILE_WINDOW_DAYS)
    for _, row in d.iterrows():
        if not (np.isfinite(row["f1"]) and np.isfinite(row["f2"]) and np.isfinite(row["f3"])):
            des_vals.append(np.nan)
            continue
        cutoff = row["ts_entry"] - win
        idx0 = 0
        while idx0 < len(hist_ts) and hist_ts[idx0] < cutoff:
            idx0 += 1
        h1, h2, h3 = hist_f1[idx0:], hist_f2[idx0:], hist_f3[idx0:]
        if len(h1) >= PCTILE_MIN_OBS:
            p1 = trailing_percentile(h1, row["f1"])   # low F1 good -> invert
            p2 = trailing_percentile(h2, row["f2"])   # low F2 good -> invert
            p3 = trailing_percentile(h3, row["f3"])   # high F3 good
            des_vals.append(((1 - p1) + (1 - p2) + p3) / 3.0)
        else:
            des_vals.append(np.nan)
        hist_ts.append(row["ts_entry"]); hist_f1.append(row["f1"])
        hist_f2.append(row["f2"]); hist_f3.append(row["f3"])
    d["des"] = des_vals
    dv = d[d["des"].notna()].copy()
    print(f"Trades with DES: {len(dv):,} / {len(d):,}")
    d.to_csv(OUT_CSV, index=False)
    print(f"Per-trade output -> {OUT_CSV}")

    # 6. Analyses.
    print("\n[6/6] Analyses")
    print("\n--- (1) DES distribution ---")
    print(f"mean={dv['des'].mean():.4f}  median={dv['des'].median():.4f}  "
          f"std={dv['des'].std():.4f}  min={dv['des'].min():.3f}  max={dv['des'].max():.3f}")
    hist, edges = np.histogram(dv["des"], bins=20, range=(0, 1))
    for i, h in enumerate(hist):
        print(f"  {edges[i]:.2f}-{edges[i+1]:.2f}  {'#' * int(60 * h / max(1, hist.max()))} {h}")

    print("\n--- (2)+(3) Quintiles & monotonicity (per-contract P&L) ---")
    dv["quintile"] = pd.qcut(dv["des"], 5, labels=[1, 2, 3, 4, 5]).astype(int)
    qs = quintile_stats(dv)
    print(qs.to_string(index=False, float_format=lambda x: f"{x:.2f}"))
    exp_by_q = qs.set_index("quintile")["expectancy"]
    mono = all(exp_by_q[q] <= exp_by_q[q + 1] for q in range(1, 5))
    print(f"\nStrict monotone increase Q1->Q5: {mono}")
    spread = float(exp_by_q[5] - exp_by_q[1])
    print(f"Q5 - Q1 expectancy spread: ${spread:.2f} per contract")

    print("\n--- (4) Statistical significance ---")
    pnl = dv["pnl_ct"].values
    q_masks = [dv["quintile"].values == q for q in (1, 5)]
    obs_stat = pnl[q_masks[1]].mean() - pnl[q_masks[0]].mean()
    perm_stats = np.empty(N_PERMUTATIONS)
    for i in range(N_PERMUTATIONS):
        pp = RNG.permutation(pnl)
        perm_stats[i] = pp[q_masks[1]].mean() - pp[q_masks[0]].mean()
    p_perm = float((perm_stats >= obs_stat).mean())
    print(f"Permutation test (Q5-Q1 spread): observed ${obs_stat:.2f}, "
          f"one-sided p = {p_perm:.4f}")
    boots = np.empty(N_BOOTSTRAP)
    i5 = np.where(q_masks[1])[0]; i1 = np.where(q_masks[0])[0]
    for i in range(N_BOOTSTRAP):
        boots[i] = (pnl[RNG.choice(i5, len(i5))].mean()
                    - pnl[RNG.choice(i1, len(i1))].mean())
    lo, hi = np.percentile(boots, [2.5, 97.5])
    print(f"Bootstrap 95% CI for spread: [${lo:.2f}, ${hi:.2f}]")
    r_s, p_s = spearman(dv["des"], dv["pnl_ct"])
    print(f"Spearman(DES, pnl/ct): rho = {r_s:.4f}, p = {p_s:.4f}")
    r_sw, p_sw = spearman(dv["des"], dv["won"].astype(int))
    print(f"Spearman(DES, won):    rho = {r_sw:.4f}, p = {p_sw:.4f}")

    print("\n--- (5) Incremental value vs FVG quality score ---")
    from scipy.stats import pearsonr
    r_p, p_p = pearsonr(dv["des"], dv["score"])
    r_ss, p_ss = spearman(dv["des"], dv["score"])
    print(f"Pearson(DES, score) = {r_p:.4f} (p={p_p:.3g}); "
          f"Spearman = {r_ss:.4f} (p={p_ss:.3g})")
    y = dv["pnl_ct"].values
    Xa = np.column_stack([np.ones(len(dv)), dv["score"].values])
    Xb = np.column_stack([np.ones(len(dv)), dv["score"].values, dv["des"].values])
    ba, ta, r2a = ols_with_t(y, Xa)
    bb, tb, r2b = ols_with_t(y, Xb)
    p_des = t_to_p(tb[2], len(dv) - 3)
    print(f"Model A (score only):   R2 = {r2a:.5f}")
    print(f"Model B (score + DES):  R2 = {r2b:.5f}  "
          f"DES beta = {bb[2]:.2f}, t = {tb[2]:.2f}, p = {p_des:.4f}")
    # partial rank correlation: DES vs pnl controlling for score
    rk = lambda s: pd.Series(s).rank().values
    yr_, dr_, sr_ = rk(dv["pnl_ct"]), rk(dv["des"]), rk(dv["score"])
    Xs = np.column_stack([np.ones(len(dv)), sr_])
    ry = yr_ - Xs @ np.linalg.lstsq(Xs, yr_, rcond=None)[0]
    rd = dr_ - Xs @ np.linalg.lstsq(Xs, dr_, rcond=None)[0]
    r_part, p_part = pearsonr(ry, rd)
    print(f"Partial rank corr (DES vs pnl | score): r = {r_part:.4f}, p = {p_part:.4f}")

    print("\n--- (6) Year-by-year ---")
    print(f"{'year':<6} {'n':>5} {'spearman':>9} {'p':>8} {'Q5-Q1 $/ct':>11}")
    for yr in sorted(dv["year"].unique()):
        g = dv[dv["year"] == yr]
        if len(g) < 50:
            print(f"{yr:<6} {len(g):>5}  (too few trades)")
            continue
        r_y, p_y = spearman(g["des"], g["pnl_ct"])
        gq = pd.qcut(g["des"], 5, labels=False, duplicates="drop")
        s_y = g[gq == gq.max()]["pnl_ct"].mean() - g[gq == 0]["pnl_ct"].mean()
        print(f"{yr:<6} {len(g):>5} {r_y:>9.4f} {p_y:>8.4f} {s_y:>11.2f}")

    print("\n--- (7) Regime splits ---")
    for name, mask in [
        ("high vol (atr_ratio>=med)", dv["atr_ratio"] >= dv["atr_ratio"].median()),
        ("low vol",  dv["atr_ratio"] < dv["atr_ratio"].median()),
        ("trending (ADX regime)", dv["adx_regime"] == "trending"),
        ("not trending", dv["adx_regime"] != "trending"),
    ]:
        g = dv[mask]
        if len(g) < 100:
            continue
        r_g, p_g = spearman(g["des"], g["pnl_ct"])
        print(f"  {name:<28} n={len(g):>5}  spearman={r_g:>7.4f}  p={p_g:.4f}")

    print("\n--- (8) Failure analysis ---")
    hi_fail = dv[(dv["quintile"] == 5) & (~dv["won"])]
    lo_win = dv[(dv["quintile"] == 1) & (dv["won"])]
    print(f"High-DES losers: n={len(hi_fail)} "
          f"({100*len(hi_fail)/max(1,(dv['quintile']==5).sum()):.0f}% of Q5)")
    print(f"  vs all trades: news_day {100*hi_fail['news'].mean():.0f}% vs "
          f"{100*dv['news'].mean():.0f}% | avg ADX {hi_fail['adx'].mean():.0f} vs "
          f"{dv['adx'].mean():.0f} | avg atr_ratio {hi_fail['atr_ratio'].mean():.2f} vs "
          f"{dv['atr_ratio'].mean():.2f} | lunch-hour {100*(hi_fail['entry_hour'].isin([11,12])).mean():.0f}% "
          f"vs {100*(dv['entry_hour'].isin([11,12])).mean():.0f}%")
    print(f"Low-DES winners: n={len(lo_win)} "
          f"({100*len(lo_win)/max(1,(dv['quintile']==1).sum()):.0f}% of Q1)")
    print(f"  avg MFE {lo_win['mfe'].mean():.1f} vs Q1-losers "
          f"{dv[(dv['quintile']==1)&(~dv['won'])]['mfe'].mean():.1f} | "
          f"avg score {lo_win['score'].mean():.1f} vs {dv['score'].mean():.1f}")

    print("\n--- (9) Interactions (Spearman DES vs pnl/ct within slices) ---")
    slices = [
        ("age 2", dv["age"] == 2), ("age 3", dv["age"] == 3), ("age 4+", dv["age"] >= 4),
        ("small FVG (<med)", dv["size_ticks"] < dv["size_ticks"].median()),
        ("large FVG (>=med)", dv["size_ticks"] >= dv["size_ticks"].median()),
        ("strong trend (ADX>=med)", dv["adx"] >= dv["adx"].median()),
        ("weak trend", dv["adx"] < dv["adx"].median()),
        ("am (<11 CT)", dv["entry_hour"] < 11),
        ("lunch (11-12 CT)", dv["entry_hour"].isin([11, 12])),
        ("pm (>=13 CT)", dv["entry_hour"] >= 13),
        ("BOS flag", dv["flags"].str.contains("bos", na=False)),
        ("no BOS flag", ~dv["flags"].str.contains("bos", na=False)),
    ]
    for name, mask in slices:
        g = dv[mask]
        if len(g) < 80:
            print(f"  {name:<24} n={len(g):>5}  (too few)")
            continue
        r_g, p_g = spearman(g["des"], g["pnl_ct"])
        print(f"  {name:<24} n={len(g):>5}  spearman={r_g:>7.4f}  p={p_g:.4f}")

    print("\n--- (falsification checks) is DES secretly something simpler? ---")
    for name, col in [("atr_ratio (volatility)", "atr_ratio"), ("ADX (trend)", "adx"),
                      ("FVG size ticks", "size_ticks"), ("age", "age")]:
        r_c, p_c = spearman(dv["des"], dv[col])
        print(f"  Spearman(DES, {name:<22}) = {r_c:>7.4f}  (p={p_c:.3g})")

    print("\n--- (10) Sizing simulation (resize only, walk-forward quintiles) ---")
    # trailing DES quintile bounds (no lookahead): rank each trade's DES against
    # the PREVIOUS trailing-year DES values.
    des_hist_ts, des_hist = [], []
    adj = []
    for _, row in d.iterrows():
        a = 0
        if np.isfinite(row["des"]):
            cutoff = row["ts_entry"] - win
            j0 = 0
            while j0 < len(des_hist_ts) and des_hist_ts[j0] < cutoff:
                j0 += 1
            h = des_hist[j0:]
            if len(h) >= PCTILE_MIN_OBS:
                pr = trailing_percentile(h, row["des"])
                if pr < 0.2:
                    a = -1
                elif pr >= 0.8:
                    a = +1
            des_hist_ts.append(row["ts_entry"]); des_hist.append(row["des"])
        adj.append(a)
    d["ct_adj"] = adj
    d["ct_new"] = (d["contracts"] + d["ct_adj"]).clip(lower=1, upper=5)
    d["pnl_new"] = d["pnl_ct"] * d["ct_new"]
    base_net = d["pnl_usd"].sum()
    new_net = d["pnl_new"].sum()
    daily_base = d.groupby(d["ts_entry"].dt.date)["pnl_usd"].sum()
    daily_new = d.groupby(d["ts_entry"].dt.date)["pnl_new"].sum()

    def maxdd(s):
        eq = s.cumsum(); return float((eq - eq.cummax()).min())

    def combine_pass_rate(daily, n_paths=10_000, target=3000.0, dll=-1000.0,
                          mll=2000.0, max_days=60):
        vals = daily.values
        # include flat days at the observed idle rate
        n_days_range = np.busday_count(daily.index.min(), daily.index.max())
        n_flat = max(0, n_days_range - len(vals))
        pool = np.concatenate([vals, np.zeros(n_flat)])
        passes = 0
        for _ in range(n_paths):
            eq = 0.0; peak = 0.0; ok = True
            for day in RNG.choice(pool, size=max_days):
                if day <= dll:
                    ok = False; break
                eq += day; peak = max(peak, eq)
                if eq - peak <= -mll:
                    ok = False; break
                if eq >= target:
                    break
            passes += 1 if (ok and eq >= target) else 0
        return 100.0 * passes / n_paths

    adj_n = (d["ct_adj"] != 0).sum()
    print(f"Trades resized: {adj_n} of {len(d)} "
          f"(-1ct: {(d['ct_adj'] == -1).sum()}, +1ct: {(d['ct_adj'] == +1).sum()})")
    print(f"{'':<24} {'baseline':>12} {'DES-sized':>12}")
    print(f"{'net pnl':<24} {base_net:>12.0f} {new_net:>12.0f}")
    print(f"{'avg trade':<24} {d['pnl_usd'].mean():>12.2f} {d['pnl_new'].mean():>12.2f}")
    print(f"{'daily maxDD':<24} {maxdd(daily_base):>12.0f} {maxdd(daily_new):>12.0f}")
    sh_b = daily_base.mean() / daily_base.std() if daily_base.std() > 0 else 0
    sh_n = daily_new.mean() / daily_new.std() if daily_new.std() > 0 else 0
    print(f"{'daily Sharpe':<24} {sh_b:>12.3f} {sh_n:>12.3f}")
    print("computing combine pass rates (10k paths each) ...")
    pr_b = combine_pass_rate(daily_base)
    pr_n = combine_pass_rate(daily_new)
    print(f"{'combine pass rate %':<24} {pr_b:>12.1f} {pr_n:>12.1f}")

    # charts (best effort)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(1, 2, figsize=(11, 4))
        ax[0].hist(dv["des"], bins=30); ax[0].set_title("DES distribution")
        ax[1].bar(qs["quintile"], qs["expectancy"])
        ax[1].set_title("Expectancy ($/ct) by DES quintile"); ax[1].set_xlabel("quintile")
        fig.tight_layout()
        p = os.path.join(RESEARCH_DIR, "inventory_echo_charts.png")
        fig.savefig(p, dpi=110)
        print(f"charts -> {p}")
    except Exception as exc:
        print(f"(charts skipped: {exc})")

    print("\nDone.")


if __name__ == "__main__":
    main()
