"""
Walk-forward validation for the V29 FVG strategy.

Splits the 7-year dataset into annual periods and measures performance
in each slice independently using fixed parameters, no re-fitting.
If later years perform similarly to early years, the edge is real.
If later years collapse, the strategy is overfitted to early data.
"""
import numpy as np
from collections import defaultdict
import src.bot as bot


def _metrics(trades: list) -> dict:
    if not trades:
        return dict(trades=0, wr=0.0, net=0.0, avg=0.0, pf=0.0, sharpe=0.0)
    pnls   = [t.pnl_usd for t in trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    net    = sum(pnls)
    wr     = len(wins) / len(pnls) * 100
    pf     = sum(wins) / abs(sum(losses)) if losses else float("inf")
    avg    = net / len(pnls)
    day_map = defaultdict(float)
    for t in trades:
        day_map[str(t.date)[:10]] += t.pnl_usd
    daily = list(day_map.values())
    if len(daily) > 1:
        arr = np.array(daily)
        sharpe = arr.mean() / arr.std(ddof=1) * np.sqrt(252)
    else:
        sharpe = 0.0
    return dict(trades=len(trades), wr=round(wr, 1), net=round(net, 0),
                avg=round(avg, 2), pf=round(pf, 2), sharpe=round(sharpe, 2))


def run_walkforward():
    print("Loading and processing data (once)...")
    df_raw = bot.fetch_data()
    bot.validate_loaded_data(df_raw)
    df_ind = bot.add_indicators(df_raw)
    sl     = bot.compute_session_levels(df_ind)
    df_sig = bot.generate_signals(df_ind.copy())

    print("Running full backtest (baseline)...")
    _, trades_all, _, _ = bot.run_backtest(df_sig, sl)
    base = _metrics(trades_all)

    windows = [
        ("2019 (partial)", "2019-01-01", "2020-01-01"),
        ("2020",           "2020-01-01", "2021-01-01"),
        ("2021",           "2021-01-01", "2022-01-01"),
        ("2022",           "2022-01-01", "2023-01-01"),
        ("2023",           "2023-01-01", "2024-01-01"),
        ("2024",           "2024-01-01", "2025-01-01"),
        ("2025",           "2025-01-01", "2026-01-01"),
        ("2026 (partial)", "2026-01-01", "2027-01-01"),
    ]

    print()
    print(f"{'Period':<20} {'Trades':>6} {'WR':>7} {'Net P&L':>11} {'Avg/trade':>10} {'PF':>6} {'Sharpe':>7}")
    print("-" * 72)

    results = []
    for label, start, end in windows:
        df_w = df_sig[(df_sig.index >= start) & (df_sig.index < end)]
        if len(df_w) < 10:
            continue
        _, trades_w, _, _ = bot.run_backtest(df_w, sl)
        m = _metrics(trades_w)
        m["period"] = label
        results.append(m)
        print(f"{label:<20} {m['trades']:>6} {m['wr']:>6.1f}% "
              f"${m['net']:>10,.0f} {m['avg']:>10.2f} {m['pf']:>6.2f} {m['sharpe']:>7.2f}")

    print("=" * 72)
    print(f"{'FULL BASELINE':<20} {base['trades']:>6} {base['wr']:>6.1f}% "
          f"${base['net']:>10,.0f} {base['avg']:>10.2f} {base['pf']:>6.2f} {base['sharpe']:>7.2f}")
    print("=" * 72)

    early = [r for r in results if r["period"] in ("2019 (partial)", "2020", "2021", "2022")]
    late  = [r for r in results if r["period"] in ("2023", "2024", "2025")]

    if early and late:
        early_avg = sum(r["avg"] for r in early) / len(early)
        late_avg  = sum(r["avg"] for r in late)  / len(late)
        early_wr  = sum(r["wr"]  for r in early) / len(early)
        late_wr   = sum(r["wr"]  for r in late)  / len(late)
        early_pf_vals = [r["pf"] for r in early if r["pf"] != float("inf")]
        late_pf_vals  = [r["pf"] for r in late  if r["pf"] != float("inf")]
        early_pf  = sum(early_pf_vals) / len(early_pf_vals) if early_pf_vals else 0
        late_pf   = sum(late_pf_vals)  / len(late_pf_vals)  if late_pf_vals  else 0
        ratio = late_avg / early_avg if early_avg > 0 else 0

        print()
        print(f"  Early years (2019-2022):  WR={early_wr:.1f}%  avg/trade=${early_avg:.2f}  PF={early_pf:.2f}")
        print(f"  Later years (2023-2025):  WR={late_wr:.1f}%  avg/trade=${late_avg:.2f}  PF={late_pf:.2f}")
        print(f"  OOS/IS ratio: {ratio:.2f}")
        print()
        if ratio >= 0.80:
            print("  VERDICT: ROBUST — later years within 20% of early years. Edge is real.")
        elif ratio >= 0.60:
            print("  VERDICT: ACCEPTABLE — some decay but edge clearly persists.")
        else:
            print("  VERDICT: WARNING — significant decay. Possible overfitting to early data.")
    print()


if __name__ == "__main__":
    run_walkforward()
