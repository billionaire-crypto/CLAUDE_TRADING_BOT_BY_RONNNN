"""Feature verification harness — H1: Overnight Extreme Sweep Failure at the RTH Transition.

STANDALONE research script. Imports nothing from `src/`; changes nothing in `src/`.
Run from the repo root:

    python3 ./research/verify_hypothesis.py
    python -m research.verify_hypothesis

------------------------------------------------------------------------------
HYPOTHESIS (H1)
------------------------------------------------------------------------------
The Globex range extreme (18:00 ET -> 09:29 ET high/low) is the most-referenced
resting-liquidity shelf on NQ. In the 09:30-11:00 ET window, RTH volume arrives
at a multiple of the Globex rate. A price extension *through* ONH/ONL that
fails to close beyond it, on a bar whose volume is high relative to its own
recent distribution, is a liquidity event rather than a trend event: the size
that had to be filled has been filled, and the extension has no follow-on.

Forced counterparty (explicit): breakout entrants who bought the ONH break, and
the resting buy-stops of overnight shorts. Both are filled at the extreme and
are underwater the instant the bar closes back inside the range. Their exit is
the reversion this feature tries to capture. (Mirror image at ONL.)

FEATURE (continuous, signed; positive = expect price up):
    score = -side * (penetration_depth / overnight_range) * volume_rank
where `side` is +1 for a failed break above ONH, -1 for a failed break below
ONL, `penetration_depth` is how far the bar's extreme travelled beyond the
level, and `volume_rank` is the bar's volume percentile within its own trailing
window. Non-event bars score 0.

------------------------------------------------------------------------------
METHODOLOGY GUARDS
------------------------------------------------------------------------------
1. NO LOOKAHEAD. Overnight levels are frozen at 09:29 and only consulted from
   09:30. The event is evaluated at a bar's close, and the whole feature series
   is `.shift(1)` before being aligned against forward returns — so the signal
   observed at bar t-1 is scored against the move that begins at bar t. Nothing
   reads the current bar's close to decide the current bar's entry.
2. FRICTION. $1.50 round-turn commission + 1 tick ($0.50) of slippage per side
   per contract. Total modelled cost = $2.50 round turn per contract, which is
   deliberately stricter than a 1-tick-total assumption.
3. NO SESSION-CROSSING RETURNS. Forward returns are masked out when the horizon
   would run past 16:00 into the next session.
4. NO TECHNICAL INDICATORS. Session extremes, bar penetration depth, and a
   rolling *percentile rank* of volume. A percentile rank is an order statistic
   used to normalise across volatility regimes — not a moving average, and not
   a signal generator in its own right.

------------------------------------------------------------------------------
DATA
------------------------------------------------------------------------------
Resolution order for the 1-minute OHLCV source:
    1. $MNQ_1M_CSV environment variable
    2. ./research/data/*.ohlcv-1m.csv  (any match)
    3. src.bot.DATA_PATH, if that file happens to exist on this machine
    4. SYNTHETIC fallback — a deterministic seeded random walk

The synthetic fallback exists so the harness is runnable and self-testing on a
machine with no market data attached. When it is used, every number printed is
PLUMBING VALIDATION ONLY and is loudly labelled as such. Do not enter anything
from a synthetic run in RESEARCH_LEDGER.md as evidence about the market.
"""

from __future__ import annotations

import glob
import os
import sys

import numpy as np
import pandas as pd
from scipy import stats

# ----------------------------------------------------------------------------
# Contract + friction constants (MNQ, CME Micro E-mini Nasdaq-100)
# ----------------------------------------------------------------------------
TICK_SIZE = 0.25            # NQ points per tick
TICK_VALUE = 0.50           # $ per tick per contract
POINT_VALUE = TICK_VALUE / TICK_SIZE   # $2.00 per NQ point per contract

COMMISSION_ROUND_TURN = 1.50           # $ per contract, all-in round turn
SLIPPAGE_TICKS_PER_SIDE = 1.0          # 1 tick = $0.50 per side
SLIPPAGE_COST_ROUND_TURN = 2.0 * SLIPPAGE_TICKS_PER_SIDE * TICK_VALUE
FRICTION_ROUND_TURN = COMMISSION_ROUND_TURN + SLIPPAGE_COST_ROUND_TURN  # $2.50

# ----------------------------------------------------------------------------
# Session + feature parameters
# ----------------------------------------------------------------------------
TZ = "US/Eastern"
GLOBEX_OPEN = "18:00"       # prior calendar day
GLOBEX_CLOSE = "09:29"
RTH_OPEN = "09:30"
RTH_CLOSE = "16:00"
SIGNAL_WINDOW_END = "11:00"  # sweeps are only scored in the open-liquidity window

MIN_PENETRATION_TICKS = 2.0   # noise floor: must clear the level by >= 2 ticks
MAX_PENETRATION_TICKS = 60.0  # beyond this it is a range break, not a sweep
VOLUME_RANK_WINDOW = 60       # bars, trailing, for the volume percentile rank
MIN_OVERNIGHT_RANGE_TICKS = 20.0  # discard degenerate/holiday overnight sessions

FORWARD_HORIZONS = {"15m": 15, "60m": 60}  # in 1-minute bars

SESSION_ROLL_HOURS = 6  # 18:00 ET + 6h -> next calendar day = that day's session


# ============================================================================
# Data loading
# ============================================================================
def _read_csv_1m(path: str) -> pd.DataFrame:
    """Read a Databento-style 1m OHLCV CSV into an ET-indexed frame."""
    df = pd.read_csv(path)
    ts_col = "ts_event" if "ts_event" in df.columns else df.columns[0]
    df[ts_col] = pd.to_datetime(df[ts_col], utc=True).dt.tz_convert(TZ)

    needed = ["open", "high", "low", "close", "volume"]
    missing = [c for c in needed if c not in df.columns]
    if missing:
        raise ValueError(f"{path}: missing required columns {missing}")

    df = df.sort_values(ts_col)

    # Front-month selection: when several contract months print on the same
    # minute, keep the highest-volume one. Mirrors src/load_data.py's rule.
    if "symbol" in df.columns:
        df = df.sort_values([ts_col, "volume"])
        df = df.drop_duplicates(subset=ts_col, keep="last")

    df = df.set_index(ts_col)[needed].sort_index()
    return df[~df.index.duplicated(keep="last")]


def _synthetic_1m(n_days: int = 90, seed: int = 20260726) -> pd.DataFrame:
    """Deterministic synthetic 1m tape: 18:00 -> 16:00 ET, weekdays only.

    Volatility clusters and volume follows a U-shaped intraday profile so the
    rolling volume rank and the sweep detector both exercise realistically.
    This is PLUMBING DATA. It contains no market information.
    """
    rng = np.random.default_rng(seed)

    end = pd.Timestamp("2026-06-30 16:00", tz=TZ)
    start = end - pd.Timedelta(days=int(n_days * 1.6))
    idx = pd.date_range(start, end, freq="1min", tz=TZ)
    idx = idx[idx.dayofweek < 5]
    # Globex 18:00-16:00 with the 17:00-18:00 maintenance halt removed.
    minute_of_day = idx.hour * 60 + idx.minute
    idx = idx[(minute_of_day >= 18 * 60) | (minute_of_day <= 16 * 60)]

    n = len(idx)
    # Vol clustering: slow-moving log-vol, no averaging of price used as signal.
    log_vol = np.cumsum(rng.normal(0.0, 0.02, n))
    log_vol -= log_vol.mean()
    sigma = 1.10 * np.exp(np.clip(log_vol, -1.0, 1.0))   # NQ points per minute

    steps = rng.normal(0.0, 1.0, n) * sigma
    close = 21000.0 + np.cumsum(steps)

    wick = np.abs(rng.normal(0.0, 1.0, n)) * sigma
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) + wick
    low = np.minimum(open_, close) - wick

    mod = idx.hour * 60 + idx.minute
    rth = (mod >= 9 * 60 + 30) & (mod <= 16 * 60)
    base = np.where(rth, 900.0, 90.0)
    # U-shape inside RTH: heaviest at the open and into the close.
    from_open = np.abs(mod - (9 * 60 + 30))
    to_close = np.abs(mod - 16 * 60)
    edge = np.minimum(from_open, to_close)
    shape = np.where(rth, 1.0 + 2.5 * np.exp(-edge / 25.0), 1.0)
    volume = np.maximum(1.0, base * shape * rng.lognormal(0.0, 0.45, n)).round()

    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


def load_1m_data() -> tuple[pd.DataFrame, bool]:
    """Return (1m OHLCV in ET, is_synthetic)."""
    candidates: list[str] = []

    env_path = os.environ.get("MNQ_1M_CSV")
    if env_path:
        candidates.append(env_path)

    here = os.path.dirname(os.path.abspath(__file__))
    candidates.extend(sorted(glob.glob(os.path.join(here, "data", "*ohlcv-1m*.csv"))))

    try:  # optional: only fires on a machine that has the production data mounted
        sys.path.insert(0, os.path.dirname(here))
        from src.bot import DATA_PATH  # noqa: PLC0415

        candidates.append(DATA_PATH)
    except Exception:
        pass

    for path in candidates:
        if path and os.path.exists(path):
            print(f"[data] real 1m source: {path}")
            return _read_csv_1m(path), False

    print("[data] no 1m CSV found (set $MNQ_1M_CSV) -> SYNTHETIC fallback")
    return _synthetic_1m(), True


# ============================================================================
# Sessionisation
# ============================================================================
def add_session_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Tag each bar with its trading-session date and its session phase."""
    out = df.copy()
    # 18:00 ET rolls into the *next* day's session.
    out["session_date"] = (out.index + pd.Timedelta(hours=SESSION_ROLL_HOURS)).date

    mod = out.index.hour * 60 + out.index.minute
    rth_open = 9 * 60 + 30
    rth_close = 16 * 60
    out["is_rth"] = (mod >= rth_open) & (mod <= rth_close)
    out["is_globex"] = ~out["is_rth"]
    return out


def overnight_levels(df: pd.DataFrame) -> pd.DataFrame:
    """Per-session Globex high/low, frozen at 09:29 — known before RTH opens."""
    globex = df[df["is_globex"]]
    lv = globex.groupby("session_date").agg(
        on_high=("high", "max"),
        on_low=("low", "min"),
        on_bars=("close", "size"),
    )
    lv["on_range"] = lv["on_high"] - lv["on_low"]
    lv["valid"] = (
        (lv["on_range"] >= MIN_OVERNIGHT_RANGE_TICKS * TICK_SIZE)
        & (lv["on_bars"] >= 120)
    )
    return lv


# ============================================================================
# Feature construction
# ============================================================================
def build_feature(df: pd.DataFrame) -> pd.DataFrame:
    """Build the signed sweep-failure score on RTH bars.

    Every input to the score is fully observable at the close of the bar it is
    computed on. The `.shift(1)` that makes it tradeable is applied downstream
    in `align_signal_and_returns`, once, so it is impossible to double-shift or
    forget it.
    """
    d = add_session_columns(df)
    lv = overnight_levels(d)

    rth = d[d["is_rth"]].copy()
    rth = rth.join(lv[["on_high", "on_low", "on_range", "valid"]], on="session_date")
    rth = rth[rth["valid"].fillna(False)].copy()

    # Volume percentile rank within the trailing window, current bar included.
    # This bar's own volume is knowable at its close, so including it is not
    # lookahead. `min_periods` keeps early-session bars from ranking on 2 samples.
    rth["vol_rank"] = (
        rth.groupby("session_date")["volume"]
        .transform(
            lambda s: s.rolling(VOLUME_RANK_WINDOW, min_periods=15).rank(pct=True)
        )
    )

    mod = rth.index.hour * 60 + rth.index.minute
    in_window = mod <= (11 * 60)  # SIGNAL_WINDOW_END

    min_pen = MIN_PENETRATION_TICKS * TICK_SIZE
    max_pen = MAX_PENETRATION_TICKS * TICK_SIZE

    # Failed break ABOVE the overnight high -> bearish (side = +1).
    pen_up = rth["high"] - rth["on_high"]
    swept_up = (
        in_window
        & (pen_up >= min_pen)
        & (pen_up <= max_pen)
        & (rth["close"] < rth["on_high"])   # closed back inside the range
        & (rth["open"] <= rth["on_high"])   # the bar did the sweeping, not a gap
    )

    # Failed break BELOW the overnight low -> bullish (side = -1).
    pen_dn = rth["on_low"] - rth["low"]
    swept_dn = (
        in_window
        & (pen_dn >= min_pen)
        & (pen_dn <= max_pen)
        & (rth["close"] > rth["on_low"])
        & (rth["open"] >= rth["on_low"])
    )

    depth_up = (pen_up / rth["on_range"]).clip(upper=1.0)
    depth_dn = (pen_dn / rth["on_range"]).clip(upper=1.0)
    vr = rth["vol_rank"].fillna(0.0)

    score = pd.Series(0.0, index=rth.index)
    score = score.mask(swept_up, -(depth_up * vr))
    score = score.mask(swept_dn, (depth_dn * vr))
    # A bar that sweeps both extremes is an outside bar, not a liquidity event.
    score = score.mask(swept_up & swept_dn, 0.0)

    rth["sweep_score"] = score.fillna(0.0)
    rth["is_event"] = rth["sweep_score"].abs() > 0.0
    return rth


def add_forward_returns(rth: pd.DataFrame) -> pd.DataFrame:
    """Forward close-to-close returns in NQ points, masked at session edges."""
    out = rth.copy()
    for label, bars in FORWARD_HORIZONS.items():
        fwd_close = out["close"].shift(-bars)
        fwd_session = pd.Series(out["session_date"].values, index=out.index).shift(-bars)
        same_session = fwd_session.values == out["session_date"]
        pts = (fwd_close - out["close"]).where(pd.Series(same_session, index=out.index))
        out[f"fwd_pts_{label}"] = pts
    return out


def align_signal_and_returns(rth: pd.DataFrame) -> pd.DataFrame:
    """THE no-lookahead join.

    `signal` is the feature shifted forward one bar: the score computed at the
    close of bar t-1 is what you can act on at bar t. It is scored against the
    forward return measured from bar t onward. There is exactly one shift in
    this file and it lives here.
    """
    out = rth.copy()
    out["signal"] = out["sweep_score"].shift(1)
    out["signal_side"] = np.sign(out["signal"])
    # Entry price: the open of the bar you can actually reach.
    out["entry_px"] = out["open"]
    # Don't let a signal from the previous session leak across the 09:30 boundary.
    prev_session = pd.Series(out["session_date"].values, index=out.index).shift(1)
    out.loc[prev_session.values != out["session_date"], "signal"] = np.nan
    return out.dropna(subset=["signal"])


# ============================================================================
# Evaluation
# ============================================================================
def rank_ic(signal: pd.Series, fwd: pd.Series) -> tuple[float, float, int]:
    """Spearman rank IC between a signal and a forward return."""
    ok = signal.notna() & fwd.notna()
    s, f = signal[ok], fwd[ok]
    if len(s) < 30 or s.nunique() < 3:
        return float("nan"), float("nan"), int(len(s))
    rho, p = stats.spearmanr(s.values, f.values)
    return float(rho), float(p), int(len(s))


def event_study(aligned: pd.DataFrame, horizon_label: str) -> dict:
    """Fixed-horizon event study on signal bars only, net of friction.

    Entry is the open of the signal bar (the first reachable price), exit is
    `horizon` bars later at the close. One contract. Friction is charged once
    per round turn, in dollars, not modelled as a spread on the price.
    """
    bars = FORWARD_HORIZONS[horizon_label]
    ev = aligned[aligned["signal"].abs() > 0].copy()
    if ev.empty:
        return {"n": 0}

    # Exit is derived on the FULL RTH grid, not on the sparse event index —
    # shifting inside `ev` would jump to the next *event*, not the next bar.
    exit_px = aligned["close"].shift(-(bars - 1)).reindex(ev.index)
    exit_session = (
        pd.Series(aligned["session_date"].values, index=aligned.index)
        .shift(-(bars - 1))
        .reindex(ev.index)
    )
    entry_session = pd.Series(ev["session_date"].values, index=ev.index)
    exit_px = exit_px.where(exit_session.values == entry_session.values)

    side = np.sign(ev["signal"])
    gross_pts = side * (exit_px - ev["entry_px"])
    gross_usd = gross_pts * POINT_VALUE
    net_usd = gross_usd - FRICTION_ROUND_TURN

    net_usd = net_usd.dropna()
    if net_usd.empty:
        return {"n": 0}

    wins = net_usd[net_usd > 0]
    losses = net_usd[net_usd <= 0]
    gross_win = float(wins.sum())
    gross_loss = float(-losses.sum())

    return {
        "n": int(len(net_usd)),
        "net_usd": float(net_usd.sum()),
        "avg_usd": float(net_usd.mean()),
        "median_usd": float(net_usd.median()),
        "win_rate": float((net_usd > 0).mean()),
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "best": float(net_usd.max()),
        "worst": float(net_usd.min()),
        "friction_usd": float(FRICTION_ROUND_TURN * len(net_usd)),
    }


def _fmt(x: float, nd: int = 4) -> str:
    return "n/a" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:,.{nd}f}"


def main() -> int:
    df, synthetic = load_1m_data()
    print(f"[data] {len(df):,} 1m bars | {df.index[0]} -> {df.index[-1]}")

    rth = build_feature(df)
    rth = add_forward_returns(rth)
    aligned = align_signal_and_returns(rth)

    n_events = int((aligned["signal"].abs() > 0).sum())
    n_sessions = aligned["session_date"].nunique()
    print(
        f"[feature] {len(aligned):,} tradeable RTH bars | {n_sessions} sessions | "
        f"{n_events:,} sweep-failure events "
        f"({n_events / max(n_sessions, 1):.2f} per session)"
    )

    print("\n" + "=" * 74)
    print("H1 — OVERNIGHT EXTREME SWEEP FAILURE @ RTH TRANSITION")
    print("=" * 74)
    print(
        f"Friction modelled: ${COMMISSION_ROUND_TURN:.2f} commission RT + "
        f"{SLIPPAGE_TICKS_PER_SIDE:.0f} tick/side slippage "
        f"= ${FRICTION_ROUND_TURN:.2f} RT per contract"
    )

    print("\n--- RANK IC (Spearman) ---")
    print(f"{'horizon':<10}{'scope':<14}{'rho':>10}{'p':>12}{'n':>10}")
    for label in FORWARD_HORIZONS:
        fwd = aligned[f"fwd_pts_{label}"]
        for scope, mask in (
            ("all bars", pd.Series(True, index=aligned.index)),
            ("events only", aligned["signal"].abs() > 0),
        ):
            rho, p, n = rank_ic(aligned["signal"][mask], fwd[mask])
            print(f"{label:<10}{scope:<14}{_fmt(rho):>10}{_fmt(p):>12}{n:>10,}")

    print(
        "\nNote: the all-bars IC is dominated by the ~0-valued non-event bars and is\n"
        "reported only for completeness. The events-only IC is the decision number."
    )

    print("\n--- EVENT STUDY (1 contract, net of friction) ---")
    for label in FORWARD_HORIZONS:
        r = event_study(aligned, label)
        print(f"\n[{label} hold]")
        if not r.get("n"):
            print("  no completed events")
            continue
        print(f"  trades         : {r['n']:,}")
        print(f"  net            : ${r['net_usd']:,.2f}")
        print(f"  avg / trade    : ${r['avg_usd']:,.2f}")
        print(f"  median / trade : ${r['median_usd']:,.2f}")
        print(f"  win rate       : {r['win_rate'] * 100:,.1f}%")
        print(f"  profit factor  : {_fmt(r['profit_factor'], 2)}")
        print(f"  best / worst   : ${r['best']:,.2f} / ${r['worst']:,.2f}")
        print(f"  friction paid  : ${r['friction_usd']:,.2f}")

    print("\n" + "=" * 74)
    if synthetic:
        print("!! SYNTHETIC DATA — PLUMBING VALIDATION ONLY.")
        print("!! Every number above is noise. It proves the harness runs; it")
        print("!! proves nothing about MNQ. Re-run with MNQ_1M_CSV=<path to 1m CSV>")
        print("!! before recording anything in research/RESEARCH_LEDGER.md.")
    else:
        print("Real data run. Record hypothesis / method / result / decision in")
        print("research/RESEARCH_LEDGER.md before acting on any of it.")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
