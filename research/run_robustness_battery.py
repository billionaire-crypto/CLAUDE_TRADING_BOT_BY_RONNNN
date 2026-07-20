"""
Robustness-first battery. Adjudicates the SHIPPED config (ATR-target 2.0x/240)
against BASE (fixed 100t target) under a no-curve-fit protocol:

  - 3-way split: dev <=2022 / validation 2023-24 / final test 2025..2026-07
    (Apr-Jul 2026 additionally marked: data arrived AFTER all parameter
    selection -> true temporally-untouched test)
  - 6-month walk-forward windows (fixed rules, unseen-window evaluation)
  - Stress: slippage x2/x3, commission +87%, extra stop slip 2t/4t,
    10% random missed fills x3 seeds, remove best 10 trades/days,
    news-day shock fills, worst-days-first sequencing, 10k reshuffle DD
  - Trade-count guard and combine-survival metrics throughout
"""
import numpy as np
from collections import defaultdict
import src.bot as bot

MLL = 2000.0


def trough_by_day(trades):
    troughs = defaultdict(float)
    for t in trades:
        day = str(t.date)[:10]
        stop_risk = float(t.stop_width_ticks) * bot.MNQ_TICK_VALUE * int(t.contracts)
        open_mark = min(float(t.mae) * bot.MNQ_POINT_VALUE * int(t.contracts), stop_risk)
        troughs[day] = min(troughs[day],
                           float(t.daily_pnl_at_entry) - open_mark - float(t.costs_usd),
                           float(t.daily_pnl_at_entry) + float(t.pnl_usd))
    return troughs


def metrics(trades, label):
    pnls = [t.pnl_usd for t in trades]
    if not pnls:
        return {"label": label, "net": 0, "n": 0}
    wins = [p for p in pnls if p > 0]; losses = [p for p in pnls if p <= 0]
    d = defaultdict(float)
    for t in trades:
        d[str(t.date)[:10]] += t.pnl_usd
    days = list(d.values()); arr = np.array(days)
    sharpe = arr.mean()/arr.std(ddof=1)*np.sqrt(252) if len(days) > 1 and arr.std(ddof=1) > 0 else 0
    eq = np.cumsum(arr); dd = float((eq - np.maximum.accumulate(eq)).min())
    tv = list(trough_by_day(trades).values())
    mon = defaultdict(float)
    for k, v in d.items(): mon[k[:7]] += v
    return {"label": label, "net": sum(pnls), "pf": sum(wins)/abs(sum(losses)) if losses else 0,
            "sharpe": sharpe, "dd": dd, "wr": len(wins)/len(pnls)*100, "n": len(pnls),
            "avg": sum(pnls)/len(pnls), "wt": min(pnls), "wd": min(days), "wtr": min(tv),
            "d750": sum(1 for x in tv if x <= -750), "d1000": sum(1 for x in tv if x <= -1000),
            "dnmo": sum(1 for v in mon.values() if v < 0), "nmo": len(mon)}


def prow(m):
    if m["n"] == 0:
        print(f"{m['label']:<22} NO TRADES"); return
    print(f"{m['label']:<22} net ${m['net']:>9,.0f} | PF {m['pf']:4.2f} | Sh {m['sharpe']:4.2f} | DD ${m['dd']:>7,.0f} | "
          f"WR {m['wr']:4.1f}% | n {m['n']:>4} | avg ${m['avg']:>4.0f} | wT ${m['wt']:>6,.0f} | wD ${m['wd']:>6,.0f} | "
          f"wTr ${m['wtr']:>6,.0f} | tr750/1k {m['d750']}/{m['d1000']} | dnMo {m['dnmo']}/{m['nmo']}")


def slice_m(trades, d0, d1, label):
    return metrics([t for t in trades if d0 <= str(t.date)[:10] <= d1], label)


def main():
    print("Loading extended dataset once...")
    df = bot.fetch_data(); bot.validate_loaded_data(df)
    di = bot.add_indicators(df); sl = bot.compute_session_levels(di)
    ds = bot.generate_signals(di.copy())

    FLAG_KEYS = ("ATR_TARGET_ENABLED", "SLIPPAGE_SCALE_TIERS", "COMMISSION_PER_CONTRACT",
                 "STOP_EXTRA_SLIP_TICKS", "MISS_FILL_PROB", "MISS_FILL_SEED")
    saved = {k: getattr(bot, k) for k in FLAG_KEYS}

    def run(label, **flags):
        for k, v in saved.items(): setattr(bot, k, v)
        for k, v in flags.items(): setattr(bot, k, v)
        _, tr, _, _ = bot.run_backtest(ds, sl)
        for k, v in saved.items(): setattr(bot, k, v)
        m = metrics(tr, label); prow(m)
        return tr, m

    print("\n===== ENGINE RUNS =====")
    tr_ship, m_ship = run("SHIPPED baseline")
    tr_base, m_base = run("BASE (fixed 100t)", ATR_TARGET_ENABLED=False)
    x2 = [(c, t*2) for c, t in saved["SLIPPAGE_SCALE_TIERS"]]
    x3 = [(c, t*3) for c, t in saved["SLIPPAGE_SCALE_TIERS"]]
    run("SHIP slip x2", SLIPPAGE_SCALE_TIERS=x2)
    run("SHIP slip x3", SLIPPAGE_SCALE_TIERS=x3)
    run("SHIP comm $2.50", COMMISSION_PER_CONTRACT=2.50)
    run("SHIP stopslip +2t", STOP_EXTRA_SLIP_TICKS=2)
    run("SHIP stopslip +4t", STOP_EXTRA_SLIP_TICKS=4)
    for seed in (1, 2, 3):
        run(f"SHIP miss10% s{seed}", MISS_FILL_PROB=0.10, MISS_FILL_SEED=seed)
    run("BASE slip x2", ATR_TARGET_ENABLED=False, SLIPPAGE_SCALE_TIERS=x2)

    print("\n===== 3-WAY SPLIT (dev / validation / FINAL TEST) =====")
    for name, tr in (("SHIPPED", tr_ship), ("BASE", tr_base)):
        prow(slice_m(tr, "2019-01-01", "2022-12-31", f"{name} DEV 19-22"))
        prow(slice_m(tr, "2023-01-01", "2024-12-31", f"{name} VAL 23-24"))
        prow(slice_m(tr, "2025-01-01", "2026-12-31", f"{name} TEST 25-26"))
        prow(slice_m(tr, "2026-03-28", "2026-12-31", f"{name} UNTOUCHED 26Q2+"))

    print("\n===== 6-MONTH WALK-FORWARD WINDOWS (SHIPPED, fixed rules) =====")
    wins = []
    for y in range(2019, 2027):
        for h, (a, b) in enumerate((("01-01", "06-30"), ("07-01", "12-31"))):
            m = slice_m(tr_ship, f"{y}-{a}", f"{y}-{b}", f"{y}H{h+1}")
            if m["n"] == 0: continue
            wins.append(m)
            print(f"  {m['label']}: net ${m['net']:>8,.0f} | PF {m['pf']:4.2f} | n {m['n']:>3} | wD ${m['wd']:>6,.0f}")
    fails = sum(1 for m in wins if m["net"] < 0)
    print(f"  windows: {len(wins)} | FAILED (net<0): {fails} | avg net ${np.mean([m['net'] for m in wins]):,.0f}")

    print("\n===== POST-HOC STRESSES (SHIPPED) =====")
    pn = sorted([t.pnl_usd for t in tr_ship], reverse=True)
    net = sum(pn)
    print(f"  remove best 10 trades: net ${net - sum(pn[:10]):,.0f} (was ${net:,.0f})")
    d = defaultdict(float)
    for t in tr_ship: d[str(t.date)[:10]] += t.pnl_usd
    dv = sorted(d.values(), reverse=True)
    print(f"  remove best 10 days  : net ${net - sum(dv[:10]):,.0f}")
    news = bot.ALL_NEWS_DATES
    shock = sum(4 * bot.MNQ_TICK_VALUE * t.contracts for t in tr_ship if str(t.date)[:10] in news)
    n_news = sum(1 for t in tr_ship if str(t.date)[:10] in news)
    print(f"  news-day shock (+2t/side on {n_news} news-day trades): net ${net - shock:,.0f}")
    days_sorted = np.sort(np.array(list(d.values())))
    worst_first_dd = float(np.min(np.cumsum(days_sorted)))
    print(f"  worst-days-FIRST sequencing: max cumulative loss ${worst_first_dd:,.0f} "
          f"(MLL breach at -$2,000: {'YES' if worst_first_dd <= -MLL else 'no'} - pathological ordering)")
    rng = np.random.default_rng(7)
    dds = []
    arr = np.array(list(d.values()))
    for _ in range(10000):
        e = np.cumsum(rng.permutation(arr))
        dds.append(float((e - np.maximum.accumulate(e)).min()))
    dds = np.array(dds)
    print(f"  10k reshuffle maxDD: p50 ${np.percentile(dds,50):,.0f} | p95 ${np.percentile(dds,5):,.0f} "
          f"| p99 ${np.percentile(dds,1):,.0f} | P(DD<=-2000) {(dds<=-2000).mean()*100:.2f}%")
    print("\nDone.")


if __name__ == "__main__":
    main()
