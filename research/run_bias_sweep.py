"""Trend-bias relaxation sweep. Tests whether the long_bias/short_bias AND
(EMA-trend AND VWAP-position) is a frequency handbrake or load-bearing edge.
Full metrics incl. the tail and zero-day frequency. Default mode must reproduce
the shipped net ($343,004) exactly."""
import numpy as np
from collections import defaultdict
import src.bot as bot

def metrics(trades, total_sessions):
    pnls=[t.pnl_usd for t in trades]; wins=[p for p in pnls if p>0]; losses=[p for p in pnls if p<=0]
    d=defaultdict(float)
    for t in trades: d[str(t.date)[:10]]+=t.pnl_usd
    days=list(d.values()); arr=np.array(days)
    sh=arr.mean()/arr.std(ddof=1)*np.sqrt(252) if len(days)>1 and arr.std(ddof=1)>0 else 0
    eq=np.cumsum(arr); dd=float((eq-np.maximum.accumulate(eq)).min()) if len(eq) else 0
    mon=defaultdict(float)
    for k,v in d.items(): mon[k[:7]]+=v
    return dict(net=sum(pnls), pf=(sum(wins)/abs(sum(losses)) if losses else 0), sharpe=sh, dd=dd,
                n=len(pnls), wr=len(wins)/len(pnls)*100 if pnls else 0, avg=sum(pnls)/len(pnls) if pnls else 0,
                wt=min(pnls) if pnls else 0, wd=min(days) if days else 0, active=len(d),
                dnmo=sum(1 for v in mon.values() if v<0), nmo=len(mon),
                lng=sum(p for t,p in zip(trades,pnls) if t.direction=='long'),
                sht=sum(p for t,p in zip(trades,pnls) if t.direction=='short'))

def main():
    df=bot.fetch_data(); bot.validate_loaded_data(df)
    di=bot.add_indicators(df); sl=bot.compute_session_levels(di)
    total=len(set(di.index.date))
    print(f"{'MODE':<16}{'net':>10}{'PF':>6}{'Sh':>6}{'DD':>9}{'trades':>8}{'WR':>7}{'avg':>7}"
          f"{'worstT':>9}{'worstD':>9}{'zero%':>7}{'dnMo':>6}{'marg':>9}")
    print("-"*112)
    base=None
    for mode in ("ema_and_vwap","ema_only","vwap_only","ema_or_vwap"):
        saved=bot.BIAS_MODE; bot.BIAS_MODE=mode
        ds=bot.generate_signals(di.copy())
        _,tr,_,_=bot.run_backtest(ds,sl)
        bot.BIAS_MODE=saved
        m=metrics(tr,total)
        zero=(total-m['active'])/total*100
        if base is None: base=m; marg='base'
        else:
            dn=m['n']-base['n']; marg=f"${(m['net']-base['net'])/dn:,.0f}" if dn>0 else 'n/a'
        print(f"{mode:<16}${m['net']:>8,.0f}{m['pf']:>6.2f}{m['sharpe']:>6.2f}${m['dd']:>7,.0f}"
              f"{m['n']:>8}{m['wr']:>6.1f}%{m['avg']:>7.0f}${m['wt']:>7,.0f}${m['wd']:>7,.0f}"
              f"{zero:>6.1f}%{m['dnmo']:>4}/{m['nmo']}{marg:>9}")
    print("\nmarg = avg P&L of the EXTRA trades each looser mode adds vs shipped (base avg ~$148).")
    print("Accept only if trades UP, PF stays >=~3.3, tail (worstT/D) NOT worse, zero% DOWN.")
    print("Done.")

if __name__=="__main__": main()
