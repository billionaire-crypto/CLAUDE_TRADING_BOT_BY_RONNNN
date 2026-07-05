"""
Research batch 1: baseline diagnostics + first experiment sweep.

Diagnostics (baseline, post-bugfix):
  - ATR strong-gate saturation by year (raw 1.5-pt floor vs pct floor)
  - Loss clustering (hour / dow / direction / regime / month) with per-year signs
  - Intraday low-water trough report (realized path + open-trade MAE)
  - Worst 10 trades / worst 10 days / down months

Sweep (each toggle alone, default params):
  BASE   : all research toggles off
  TS     : time stop (24 bars, <8 ticks MFE)
  TRAIL  : trail after 60-tick MFE, 40-tick gap
  BE4    : breakeven offset +4 ticks
  ATRPCT : strong-gate ATR floor normalized by price (0.02%)
"""
import numpy as np
import pandas as pd
from collections import defaultdict
import src.bot as bot


def trough_by_day(trades):
    """Worst intraday daily-P&L trough per day: realized-so-far at entry minus
    the trade's max adverse excursion (with costs). Conservative."""
    troughs = defaultdict(float)
    closes = defaultdict(float)
    for t in trades:
        day = str(t.date)[:10]
        open_trough = float(t.daily_pnl_at_entry) - (float(t.mae) * bot.MNQ_POINT_VALUE * int(t.contracts) + float(t.costs_usd))
        realized_after = float(t.daily_pnl_at_entry) + float(t.pnl_usd)
        troughs[day] = min(troughs[day], open_trough, realized_after)
        closes[day] = realized_after
    return troughs, closes


def metrics(trades, label):
    pnls = [t.pnl_usd for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    d = defaultdict(float)
    for t in trades:
        d[str(t.date)[:10]] += t.pnl_usd
    days = list(d.values())
    arr = np.array(days)
    sharpe = arr.mean() / arr.std(ddof=1) * np.sqrt(252) if len(days) > 1 and arr.std(ddof=1) > 0 else 0
    eq = np.cumsum(arr)
    dd = float((eq - np.maximum.accumulate(eq)).min()) if len(eq) else 0.0
    troughs, _ = trough_by_day(trades)
    tv = list(troughs.values())
    mon = defaultdict(float)
    for day, p in d.items():
        mon[day[:7]] += p
    yr = defaultdict(float)
    for day, p in d.items():
        yr[day[:4]] += p
    longs = sum(t.pnl_usd for t in trades if t.direction == "long")
    shorts = sum(t.pnl_usd for t in trades if t.direction == "short")
    return {
        "label": label, "net": sum(pnls), "pf": (sum(wins) / abs(sum(losses))) if losses else 0,
        "sharpe": sharpe, "maxdd": dd, "wr": len(wins) / len(pnls) * 100 if pnls else 0,
        "n": len(pnls), "avg": sum(pnls) / len(pnls) if pnls else 0,
        "wtrade": min(pnls) if pnls else 0, "wday": min(days) if days else 0,
        "wtrough": min(tv) if tv else 0,
        "tr750": sum(1 for x in tv if x <= -750), "tr1000": sum(1 for x in tv if x <= -1000),
        "cl750": sum(1 for x in days if x <= -750), "cl1000": sum(1 for x in days if x <= -1000),
        "downmo": sum(1 for v in mon.values() if v < 0), "nmo": len(mon),
        "long": longs, "short": shorts,
        "years": {k: round(v) for k, v in sorted(yr.items())},
    }


def prow(m):
    print(f"{m['label']:<8} net ${m['net']:>9,.0f} | PF {m['pf']:.2f} | Sh {m['sharpe']:.2f} | DD ${m['maxdd']:>7,.0f} | "
          f"WR {m['wr']:.1f}% | n {m['n']} | avg ${m['avg']:.0f} | wT ${m['wtrade']:,.0f} | wD ${m['wday']:,.0f} | "
          f"wTr ${m['wtrough']:,.0f} | tr<=750/1k {m['tr750']}/{m['tr1000']} | dnMo {m['downmo']}/{m['nmo']} | "
          f"L ${m['long']:,.0f} S ${m['short']:,.0f}")
    print(f"         years: {m['years']}")


def main():
    print("Loading once...")
    df_raw = bot.fetch_data()
    bot.validate_loaded_data(df_raw)
    di = bot.add_indicators(df_raw)
    sl = bot.compute_session_levels(di)
    ds = bot.generate_signals(di.copy())

    # ---------- diagnostics on indicators ----------
    print("\n=== ATR STRONG-GATE SATURATION BY YEAR ===")
    tmp = di.dropna(subset=["atr"]).copy()
    tmp["yr"] = tmp.index.year
    for yr, g in tmp.groupby("yr"):
        raw = (g["atr"] >= 1.5).mean() * 100
        pct = (g["atr"] >= g["close"] * bot.ATR_FLOOR_PCT).mean() * 100
        print(f"  {yr}: atr>=1.5pts {raw:5.1f}% of bars | atr>=0.02%*px {pct:5.1f}%  (avg px {g['close'].mean():,.0f})")

    # ---------- baseline ----------
    print("\nRunning BASE...")
    _, tr0, _, _ = bot.run_backtest(ds, sl)
    m0 = metrics(tr0, "BASE")

    tdf = pd.DataFrame([{
        "pnl": t.pnl_usd, "dir": t.direction, "hour": t.entry_hour,
        "dow": t.day_of_week, "mon": t.month, "yr": t.year,
        "regime": t.bar_adx_regime, "stopw": t.stop_width_ticks,
        "atr_ratio": t.atr_ratio_at_entry, "won": t.won,
    } for t in tr0])

    print("\n=== LOSS CLUSTERS (net P&L by bucket; worst first; n>=40) ===")
    for col in ("hour", "dow", "regime", "dir", "mon"):
        g = tdf.groupby(col)["pnl"].agg(["sum", "count", "mean"])
        g = g[g["count"] >= 40].sort_values("sum")
        worst = g.head(3)
        print(f"  by {col}:")
        for idx, r in worst.iterrows():
            sub = tdf[tdf[col] == idx]
            ys = sub.groupby("yr")["pnl"].sum()
            signs = "".join("+" if v > 0 else "-" for _, v in sorted(ys.items()))
            print(f"    {idx}: net ${r['sum']:>8,.0f}  n={int(r['count'])}  avg ${r['mean']:>6.1f}  yearsigns {signs}")

    troughs, _ = trough_by_day(tr0)
    print("\n=== WORST 10 INTRADAY TROUGHS (BASE) ===")
    for day, v in sorted(troughs.items(), key=lambda kv: kv[1])[:10]:
        print(f"  {day}: trough ${v:,.0f}")
    print("\n=== WORST 10 TRADES (BASE) ===")
    for t in sorted(tr0, key=lambda t: t.pnl_usd)[:10]:
        print(f"  {str(t.date)[:16]} {t.direction:<5} ctr {t.contracts:>2} {t.exit_reason:<11} ${t.pnl_usd:,.0f}")

    # ---------- sweep ----------
    print("\n=== SWEEP ===")
    prow(m0)
    results = [m0]

    def run(label, **flags):
        saved = {k: getattr(bot, k) for k in flags}
        for k, v in flags.items():
            setattr(bot, k, v)
        _, tr, _, _ = bot.run_backtest(ds, sl)
        for k, v in saved.items():
            setattr(bot, k, v)
        m = metrics(tr, label)
        prow(m)
        return m

    results.append(run("TS", TIME_STOP_ENABLED=True))
    results.append(run("TRAIL", TRAIL_AFTER_MFE_ENABLED=True))
    results.append(run("BE4", BE_OFFSET_TICKS=4))
    results.append(run("ATRPCT", ATR_FLOOR_PCT_ENABLED=True))
    print("\nDone.")


if __name__ == "__main__":
    main()
