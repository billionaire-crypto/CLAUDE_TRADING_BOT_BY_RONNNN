"""
Research batch 3: contain the ATR-target tail for combine compatibility.
Raw ATRT2 = +49% net but maxDD -$4,084 (kills the $2k MLL) and worst day
-$3,957 (kills the $1k DLL). Test whether capped/hybrid variants keep a
meaningful share of the gain with combine-survivable tails.

  BASE        : current shipping config
  ATRT2_C240  : 2.0x ATR target capped at 240 ticks
  ATRT2_C140  : 2.0x ATR target capped at 140 ticks (mild hybrid)
  ATRT15_C160 : 1.5x ATR target capped at 160 ticks
  ATRT2_TRAIL : 2.0x ATR (cap 240) + trail 60/40 to cut long-hold exposure

Also dumps the 10 worst trades of ATRT2_C240 to see what drives the tail.
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
    print(f"{m['label']:<12} net ${m['net']:>9,.0f} | PF {m['pf']:.2f} | Sh {m['sharpe']:.2f} | DD ${m['maxdd']:>7,.0f} | "
          f"WR {m['wr']:.1f}% | n {m['n']} | avg ${m['avg']:.0f} | wT ${m['wtrade']:,.0f} | wD ${m['wday']:,.0f} | "
          f"wTr ${m['wtrough']:,.0f} | tr<=750/1k {m['tr750']}/{m['tr1000']} | dnMo {m['downmo']}/{m['nmo']}")
    print(f"             years: {m['years']}")


def main():
    print("Loading once...")
    df_raw = bot.fetch_data()
    bot.validate_loaded_data(df_raw)
    di = bot.add_indicators(df_raw)
    sl = bot.compute_session_levels(di)
    ds = bot.generate_signals(di.copy())

    def run(label, dump_worst=False, **flags):
        saved = {k: getattr(bot, k) for k in flags}
        for k, v in flags.items():
            setattr(bot, k, v)
        _, tr, _, _ = bot.run_backtest(ds, sl)
        for k, v in saved.items():
            setattr(bot, k, v)
        m = metrics(tr, label)
        prow(m)
        if dump_worst:
            for t in sorted(tr, key=lambda t: t.pnl_usd)[:10]:
                print(f"    worst: {str(t.date)[:16]} {t.direction:<5} ctr {t.contracts:>2} "
                      f"stopW {t.stop_width_ticks:.0f}t {t.exit_reason:<11} ${t.pnl_usd:,.0f}")
        return m

    run("BASE")
    run("ATRT2_C240", dump_worst=True, ATR_TARGET_ENABLED=True, ATR_TARGET_MULT=2.0, ATR_TARGET_MAX_TICKS=240)
    run("ATRT2_C140", ATR_TARGET_ENABLED=True, ATR_TARGET_MULT=2.0, ATR_TARGET_MAX_TICKS=140)
    run("ATRT15_C160", ATR_TARGET_ENABLED=True, ATR_TARGET_MULT=1.5, ATR_TARGET_MAX_TICKS=160)
    run("ATRT2_TRAIL", ATR_TARGET_ENABLED=True, ATR_TARGET_MULT=2.0, ATR_TARGET_MAX_TICKS=240,
        TRAIL_AFTER_MFE_ENABLED=True, TRAIL_TRIGGER_TICKS=60, TRAIL_GAP_TICKS=40)
    print("\nDone.")


if __name__ == "__main__":
    main()
