"""
Supply-decay study — is the core starving, and if so, at which gate?

Question: the ledger's 2026-07-07 entry noted "a secular decline in pullback-day
supply (idle rate 12.9%->31.8%, corr 0.96)" in passing, as context for a
CALM_ATR_RATIO decision. It was never measured as a subject in its own right.
This script does that: it separates FVG *supply* (how many gaps the market
makes) from *conversion* (how many of them the engine actually trades), by year
and by half-year.

Why it matters: those two decay in opposite directions, which points at a very
different remedy than "the edge is fading."

Input: research/day_type_study_results.csv — the 1,847-session census produced
by run_day_type_study.py (committed, so this is reproducible without the
Databento CSV). Columns: date, year, label, bars, regime_open, fvgs, trades.

Run from the repo root:  python -m research.run_supply_decay_study
"""
import os

import numpy as np
import pandas as pd

CENSUS_PATH = os.path.join(os.path.dirname(__file__), "day_type_study_results.csv")


def load_census() -> pd.DataFrame:
    d = pd.read_csv(CENSUS_PATH)
    d["date"] = pd.to_datetime(d["date"])
    return d


def by_year(d: pd.DataFrame) -> pd.DataFrame:
    g = d.groupby("year").agg(
        sessions=("date", "count"),
        fvgs_per_day=("fvgs", "mean"),
        trades_per_day=("trades", "mean"),
    )
    # Conversion = share of detected gaps that became entries. This is the
    # number that actually drives trade frequency.
    g["conversion_pct"] = (
        100 * d.groupby("year")["trades"].sum() / d.groupby("year")["fvgs"].sum()
    )
    g["zero_trade_day_pct"] = 100 * d.assign(z=d["trades"].eq(0)).groupby("year")["z"].mean()
    # Share of in-session bars where the ATR regime gate was open. Isolates
    # "the gate closed more" from "the retrace stopped coming."
    g["regime_open_pct"] = 100 * d.groupby("year")["regime_open"].sum() / d.groupby("year")["bars"].sum()
    return g


def by_half_year(d: pd.DataFrame) -> pd.DataFrame:
    hy = d["date"].dt.year.astype(str) + "H" + ((d["date"].dt.month > 6).astype(int) + 1).astype(str)
    h = d.assign(hy=hy).groupby("hy").agg(
        sessions=("date", "count"), fvgs=("fvgs", "sum"), trades=("trades", "sum")
    )
    h["conversion_pct"] = 100 * h["trades"] / h["fvgs"]
    h["trades_per_day"] = h["trades"] / h["sessions"]
    return h


def trend(g: pd.DataFrame, cols) -> pd.DataFrame:
    """Correlation with the calendar year + OLS slope. A monotone decay shows up
    as |corr| near 1; noise does not."""
    x = g.index.values.astype(float)
    rows = []
    for c in cols:
        y = g[c].values.astype(float)
        rows.append({"metric": c,
                     "corr_with_year": np.corrcoef(x, y)[0, 1],
                     "slope_per_year": np.polyfit(x, y, 1)[0],
                     "first": y[0], "last": y[-1]})
    return pd.DataFrame(rows).set_index("metric")


def main() -> None:
    d = load_census()
    print(f"census: {d['date'].min().date()} -> {d['date'].max().date()}  n={len(d)} sessions\n")

    g = by_year(d)
    print("── BY YEAR ─────────────────────────────────────────────────────────")
    print(g.round(2).to_string(), "\n")

    print("── TREND (corr with calendar year; |corr|~1 = monotone, not noise) ──")
    print(trend(g, ["fvgs_per_day", "conversion_pct", "trades_per_day",
                    "zero_trade_day_pct", "regime_open_pct"]).round(3).to_string(), "\n")

    print("── BY HALF-YEAR (recency check) ────────────────────────────────────")
    print(by_half_year(d).round(2).to_string())


if __name__ == "__main__":
    main()
