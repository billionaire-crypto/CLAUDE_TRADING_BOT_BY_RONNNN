"""
Research batch 2:
  - CORRECTED intraday trough (open-trade MAE capped at stop risk: a stop fill
    exits the position; the exit bar's wick beyond the stop never marks against
    the account)
  - TRAIL sensitivity (80/50 vs 60/40)
  - TS sensitivity (12 bars / 16 ticks) -- expect near-no-op
  - ATR-scaled targets (2.0x and 3.0x ATR)
"""
import numpy as np
from collections import defaultdict
import src.bot as bot


def trough_by_day(trades):
    troughs = defaultdict(float)
    for t in trades:
        day = str(t.date)[:10]
        stop_risk = float(t.stop_width_ticks) * bot.MNQ_TICK_VALUE * int(t.contracts)
        open_mark = min(float(t.mae) * bot.MNQ_POINT_VALUE * int(t.contracts), stop_risk)
        trough_open = float(t.daily_pnl_at_entry) - open_mark - float(t.costs_usd)
        realized_after = float(t.daily_pnl_at_entry) + float(t.pnl_usd)
        troughs[day] = min(troughs[day], trough_open, realized_after)
    return troughs


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
    tv = list(trough_by_day(trades).values())
    mon = defaultdict(float)
    yr = defaultdict(float)
    for day, p in d.items():
        mon[day[:7]] += p
        yr[day[:4]] += p
    return {
        "label": label, "net": sum(pnls), "pf": (sum(wins) / abs(sum(losses))) if losses else 0,
        "sharpe": sharpe, "maxdd": dd, "wr": len(wins) / len(pnls) * 100 if pnls else 0,
        "n": len(pnls), "avg": sum(pnls) / len(pnls) if pnls else 0,
        "wtrade": min(pnls) if pnls else 0, "wday": min(days) if days else 0,
        "wtrough": min(tv) if tv else 0,
        "tr750": sum(1 for x in tv if x <= -750), "tr1000": sum(1 for x in tv if x <= -1000),
        "downmo": sum(1 for v in mon.values() if v < 0), "nmo": len(mon),
        "years": {k: round(v) for k, v in sorted(yr.items())},
    }


def prow(m):
    print(f"{m['label']:<10} net ${m['net']:>9,.0f} | PF {m['pf']:.2f} | Sh {m['sharpe']:.2f} | DD ${m['maxdd']:>7,.0f} | "
          f"WR {m['wr']:.1f}% | n {m['n']} | avg ${m['avg']:.0f} | wT ${m['wtrade']:,.0f} | wD ${m['wday']:,.0f} | "
          f"wTr ${m['wtrough']:,.0f} | tr<=750/1k {m['tr750']}/{m['tr1000']} | dnMo {m['downmo']}/{m['nmo']}")
    print(f"           years: {m['years']}")


def main():
    print("Loading once...")
    df_raw = bot.fetch_data()
    bot.validate_loaded_data(df_raw)
    di = bot.add_indicators(df_raw)
    sl = bot.compute_session_levels(di)
    ds = bot.generate_signals(di.copy())

    def run(label, **flags):
        saved = {k: getattr(bot, k) for k in flags}
        for k, v in flags.items():
            setattr(bot, k, v)
        _, tr, _, _ = bot.run_backtest(ds, sl)
        for k, v in saved.items():
            setattr(bot, k, v)
        m = metrics(tr, label)
        prow(m)
        return m, tr

    m0, tr0 = run("BASE")
    tv = sorted(trough_by_day(tr0).items(), key=lambda kv: kv[1])[:10]
    print("  corrected worst troughs:", [(d, round(v)) for d, v in tv])

    run("TRAIL8050", TRAIL_AFTER_MFE_ENABLED=True, TRAIL_TRIGGER_TICKS=80, TRAIL_GAP_TICKS=50)
    run("TS1216", TIME_STOP_ENABLED=True, TIME_STOP_BARS=12, TIME_STOP_MIN_MFE_TICKS=16)
    run("ATRT2", ATR_TARGET_ENABLED=True, ATR_TARGET_MULT=2.0)
    run("ATRT3", ATR_TARGET_ENABLED=True, ATR_TARGET_MULT=3.0)
    print("\nDone.")


if __name__ == "__main__":
    main()
