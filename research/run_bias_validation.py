"""FULL validation gauntlet for BIAS_MODE='vwap_only' vs shipped 'ema_and_vwap'.
Same protocol the ATR targets cleared: 3-way split (dev/val/UNTOUCHED 2026Q2+),
16 half-year walk-forward windows, stress battery, combine bootstrap sim.
Ship ONLY if it holds out-of-sample AND survives stress."""
import numpy as np
from collections import defaultdict
import src.bot as bot
from src.run_combine_sim import _simulate

def M(trades):
    pnls=[t.pnl_usd for t in trades]
    if not pnls: return dict(n=0,net=0,pf=0,sharpe=0,dd=0,wr=0,avg=0,wt=0,wd=0)
    wins=[p for p in pnls if p>0]; losses=[p for p in pnls if p<=0]
    d=defaultdict(float)
    for t in trades: d[str(t.date)[:10]]+=t.pnl_usd
    days=list(d.values()); arr=np.array(days)
    sh=arr.mean()/arr.std(ddof=1)*np.sqrt(252) if len(days)>1 and arr.std(ddof=1)>0 else 0
    eq=np.cumsum(arr); dd=float((eq-np.maximum.accumulate(eq)).min())
    return dict(n=len(pnls),net=sum(pnls),pf=(sum(wins)/abs(sum(losses)) if losses else 0),
                sharpe=sh,dd=dd,wr=len(wins)/len(pnls)*100,avg=sum(pnls)/len(pnls),
                wt=min(pnls),wd=min(days))

def row(lbl,m):
    print(f"  {lbl:<22} net ${m['net']:>9,.0f} | PF {m['pf']:4.2f} | Sh {m['sharpe']:4.2f} | "
          f"DD ${m['dd']:>7,.0f} | n {m['n']:>4} | WR {m['wr']:4.1f}% | avg ${m['avg']:>4.0f} | "
          f"wD ${m['wd']:>6,.0f}")

def sl_m(tr,d0,d1): return M([t for t in tr if d0<=str(t.date)[:10]<=d1])

def main():
    df=bot.fetch_data(); bot.validate_loaded_data(df)
    di=bot.add_indicators(df); sl=bot.compute_session_levels(di)

    def run(mode, **flags):
        sv={k:getattr(bot,k) for k in list(flags)+["BIAS_MODE"]}
        bot.BIAS_MODE=mode
        for k,v in flags.items(): setattr(bot,k,v)
        ds=bot.generate_signals(di.copy())
        _,tr,dr,_=bot.run_backtest(ds,sl)
        for k,v in sv.items(): setattr(bot,k,v)
        return tr,dr

    tr_ship,_=run("ema_and_vwap")
    tr_v,dr_v=run("vwap_only")

    print("="*95)
    print("A. 3-WAY SPLIT (dev 19-22 / validation 23-24 / TEST 25-26 / UNTOUCHED 26Q2+)")
    print("="*95)
    for name,tr in (("SHIPPED",tr_ship),("VWAP_ONLY",tr_v)):
        row(f"{name} dev 19-22", sl_m(tr,"2019-01-01","2022-12-31"))
        row(f"{name} val 23-24", sl_m(tr,"2023-01-01","2024-12-31"))
        row(f"{name} test 25-26", sl_m(tr,"2025-01-01","2026-12-31"))
        row(f"{name} UNTOUCHED Q2+", sl_m(tr,"2026-03-28","2026-12-31"))
        print()

    print("="*95)
    print("B. WALK-FORWARD (vwap_only, 6-month windows)")
    print("="*95)
    wins=[]
    for y in range(2019,2027):
        for h,(a,b) in enumerate((("01-01","06-30"),("07-01","12-31"))):
            m=sl_m(tr_v,f"{y}-{a}",f"{y}-{b}")
            if m["n"]==0: continue
            wins.append(m)
            print(f"  {y}H{h+1}: net ${m['net']:>8,.0f} | PF {m['pf']:4.2f} | n {m['n']:>3} | wD ${m['wd']:>6,.0f}")
    print(f"  windows {len(wins)} | FAILED(net<0) {sum(1 for m in wins if m['net']<0)} | avg ${np.mean([m['net'] for m in wins]):,.0f}")

    print("="*95)
    print("C. STRESS BATTERY (vwap_only)")
    print("="*95)
    row("baseline", M(tr_v))
    x2=[(c,t*2) for c,t in bot.SLIPPAGE_SCALE_TIERS]; x3=[(c,t*3) for c,t in bot.SLIPPAGE_SCALE_TIERS]
    row("slip x2", M(run("vwap_only",SLIPPAGE_SCALE_TIERS=x2)[0]))
    row("slip x3", M(run("vwap_only",SLIPPAGE_SCALE_TIERS=x3)[0]))
    row("comm $2.50", M(run("vwap_only",COMMISSION_PER_CONTRACT=2.50)[0]))
    row("stopslip +4t", M(run("vwap_only",STOP_EXTRA_SLIP_TICKS=4)[0]))
    row("miss10% s1", M(run("vwap_only",MISS_FILL_PROB=0.10,MISS_FILL_SEED=1)[0]))
    row("miss10% s2", M(run("vwap_only",MISS_FILL_PROB=0.10,MISS_FILL_SEED=2)[0]))
    pn=sorted([t.pnl_usd for t in tr_v],reverse=True); net=sum(pn)
    print(f"  remove best 10 trades: net ${net-sum(pn[:10]):,.0f} (was ${net:,.0f})")

    print("="*95)
    print("D. COMBINE SIM (100k bootstrap each)")
    print("="*95)
    for name,tr,dr in (("SHIPPED",tr_ship,None),("VWAP_ONLY",tr_v,dr_v)):
        if dr is None:
            _t,dr=run("ema_and_vwap")[0],run("ema_and_vwap")[1]
        dp=np.array([d.daily_pnl_net for d in dr]); ip=np.array([d.max_intraday_peak for d in dr])
        qf=np.array([d.is_qualifying_day for d in dr],dtype=bool)
        p,days,f=_simulate(dp,ip,qf,None); p30,_,_=_simulate(dp,ip,qf,30)
        med=np.median(days) if len(days) else 0
        print(f"  {name}: P(pass) {p:.1f}% | median {med:.0f}d | P(30d) {p30:.1f}% | "
              f"fails trailDD {f['trail_dd']/1000:.2f}% dailyLim {f['daily_limit']/1000:.2f}%")
    print("Done.")

if __name__=="__main__": main()
