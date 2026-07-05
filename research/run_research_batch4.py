"""
Research batch 4 — FINAL VALIDATION of ATRT2_TRAIL (2.0x ATR target capped at
240 ticks + trail 60/40):
  1. Holdout: IS (2019-2023) vs OOS (2024-2026) quality metrics with flags ON.
  2. Combine survival: bootstrap sim (run_combine_sim._simulate) BASE vs COMBO.
"""
import numpy as np
from collections import defaultdict
import src.bot as bot
from src.run_combine_sim import _simulate

FLAGS = dict(ATR_TARGET_ENABLED=True, ATR_TARGET_MULT=2.0, ATR_TARGET_MAX_TICKS=240,
             TRAIL_AFTER_MFE_ENABLED=True, TRAIL_TRIGGER_TICKS=60, TRAIL_GAP_TICKS=40)


def qmetrics(trades):
    pnls = [t.pnl_usd for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    d = defaultdict(float)
    for t in trades:
        d[str(t.date)[:10]] += t.pnl_usd
    days = list(d.values())
    arr = np.array(days)
    sharpe = arr.mean() / arr.std(ddof=1) * np.sqrt(252) if len(days) > 1 and arr.std(ddof=1) > 0 else 0
    return dict(n=len(pnls), wr=len(wins) / len(pnls) * 100 if pnls else 0,
                net=sum(pnls), avg=(sum(pnls) / len(pnls)) if pnls else 0,
                pf=(sum(wins) / abs(sum(losses))) if losses else 0, sharpe=sharpe,
                wtrade=min(pnls) if pnls else 0, wday=min(days) if days else 0)


def main():
    print("Loading once...")
    df_raw = bot.fetch_data()
    bot.validate_loaded_data(df_raw)
    di = bot.add_indicators(df_raw)
    sl = bot.compute_session_levels(di)
    ds = bot.generate_signals(di.copy())

    saved = {k: getattr(bot, k) for k in FLAGS}
    for k, v in FLAGS.items():
        setattr(bot, k, v)

    print("HOLDOUT with combo flags ON...")
    df_is = ds[ds.index < "2024-01-01"]
    df_oos = ds[ds.index >= "2024-01-01"]
    _, tis, _, _ = bot.run_backtest(df_is, sl)
    _, tos, _, _ = bot.run_backtest(df_oos, sl)
    mi, mo = qmetrics(tis), qmetrics(tos)
    print(f"{'metric':<12}{'IS 19-23':>12}{'OOS 24-26':>12}")
    for k in ("n", "wr", "net", "avg", "pf", "sharpe", "wtrade", "wday"):
        print(f"{k:<12}{mi[k]:>12,.2f}{mo[k]:>12,.2f}")
    print(f"OOS/IS avg {mo['avg']/mi['avg']:.2f} | wr {mo['wr']/mi['wr']:.2f} | pf {mo['pf']/mi['pf']:.2f}")

    print("\nCOMBINE SIM (100k bootstrap runs each)...")
    for label, flags_on in (("BASE", False), ("COMBO", True)):
        if flags_on:
            for k, v in FLAGS.items():
                setattr(bot, k, v)
        else:
            for k, v in saved.items():
                setattr(bot, k, v)
        _, tr, dr, _ = bot.run_backtest(ds, sl)
        dp = np.array([d.daily_pnl_net for d in dr], dtype=float)
        ip = np.array([d.max_intraday_peak for d in dr], dtype=float)
        qf = np.array([d.is_qualifying_day for d in dr], dtype=bool)
        p_nl, days_nl, fails = _simulate(dp, ip, qf, None)
        p_30, _, _ = _simulate(dp, ip, qf, 30)
        med = np.median(days_nl) if len(days_nl) else 0
        print(f"  {label}: P(pass) {p_nl:.1f}% | median {med:.0f}d | P(30d) {p_30:.1f}% | "
              f"fails trailDD {fails['trail_dd']/1000:.1f}% dailyLim {fails['daily_limit']/1000:.1f}%")

    for k, v in saved.items():
        setattr(bot, k, v)
    print("\nDone.")


if __name__ == "__main__":
    main()
