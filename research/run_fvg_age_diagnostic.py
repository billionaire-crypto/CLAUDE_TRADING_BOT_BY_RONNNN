"""Measures the true cost of the FVG_MAX_AGE_BARS<=4 cliff, on the SHIPPED config.
Key questions: (a) does relaxing it add trades, (b) are the ADDED trades winners
or junk (marginal avg P&L), (c) does it relieve the FREQUENCY bottleneck (fewer
zero-trade days) or just pad busy days, (d) does the core edge (PF/avg) survive."""
import numpy as np
from collections import defaultdict
import src.bot as bot

def metrics(trades, ndays_total):
    pnls=[t.pnl_usd for t in trades]; wins=[p for p in pnls if p>0]; losses=[p for p in pnls if p<=0]
    d=defaultdict(float)
    for t in trades: d[str(t.date)[:10]]+=t.pnl_usd
    days=list(d.values()); arr=np.array(days)
    sh=arr.mean()/arr.std(ddof=1)*np.sqrt(252) if len(days)>1 and arr.std(ddof=1)>0 else 0
    return dict(net=sum(pnls), pf=(sum(wins)/abs(sum(losses)) if losses else 0), sharpe=sh,
                n=len(pnls), wr=len(wins)/len(pnls)*100 if pnls else 0,
                avg=sum(pnls)/len(pnls) if pnls else 0, wt=min(pnls) if pnls else 0,
                wd=min(days) if days else 0, active_days=len(d))

def main():
    df=bot.fetch_data(); bot.validate_loaded_data(df)
    di=bot.add_indicators(df); sl=bot.compute_session_levels(di)
    total_sessions=len(set(di.index.date))
    base=None
    print(f"total trading sessions in data: {total_sessions}\n")
    print(f"{'AGE':<5}{'net':>10}{'PF':>6}{'Sh':>6}{'trades':>8}{'WR':>7}{'avg':>7}"
          f"{'worstT':>9}{'worstD':>9}{'activeDays':>11}{'zero%':>7}{'marg.avg':>10}")
    print("-"*100)
    for age in (4,6,8,12,20):
        saved=bot.FVG_MAX_AGE_BARS; bot.FVG_MAX_AGE_BARS=age
        ds=bot.generate_signals(di.copy())
        _,tr,_,_=bot.run_backtest(ds,sl)
        bot.FVG_MAX_AGE_BARS=saved
        m=metrics(tr,total_sessions)
        zero=(total_sessions-m['active_days'])/total_sessions*100
        if base is None:
            base=m; marg='(baseline)'
        else:
            dn=m['n']-base['n']; dnet=m['net']-base['net']
            marg=f"${dnet/dn:,.0f}" if dn>0 else 'n/a'
        print(f"{age:<5}${m['net']:>8,.0f}{m['pf']:>6.2f}{m['sharpe']:>6.2f}{m['n']:>8}"
              f"{m['wr']:>6.1f}%{m['avg']:>7.0f}${m['wt']:>7,.0f}${m['wd']:>7,.0f}"
              f"{m['active_days']:>11}{zero:>6.1f}%{marg:>10}")
    print("\nmarg.avg = avg P&L of the EXTRA trades unlocked vs age=4 (baseline avg for ref: "
          f"${base['avg']:.0f}). If marg.avg << baseline or negative -> the cliff blocks JUNK.")
    print("Done.")

if __name__=="__main__":
    main()
