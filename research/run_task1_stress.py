import numpy as np
from collections import defaultdict
import src.bot as bot

def M(trades):
    pnls=[t.pnl_usd for t in trades]
    if not pnls: return dict(n=0,net=0,pf=0,avg=0)
    wins=[p for p in pnls if p>0]; losses=[p for p in pnls if p<=0]
    return dict(n=len(pnls), net=sum(pnls), pf=(sum(wins)/abs(sum(losses)) if losses else 0),
                avg=sum(pnls)/len(pnls))

def OOS(trades): return [t for t in trades if '2026-03-28' <= str(t.date)[:10] <= '2026-07-02']

def top10pct_removed(pnl_list):
    pnls=sorted(pnl_list, reverse=True)
    k=max(1,int(len(pnls)*0.10))
    return sum(pnls)-sum(pnls[:k]), k

df=bot.fetch_data(); bot.validate_loaded_data(df)
di=bot.add_indicators(df); sl=bot.compute_session_levels(di)
ds=bot.generate_signals(di.copy())
_,tr,_,_=bot.run_backtest(ds,sl)
m=M(tr); mo=M(OOS(tr))
print(f"BASELINE full: net ${m['net']:,.0f} PF {m['pf']:.2f} n {m['n']}")
print(f"BASELINE OOS(Apr-Jul26): net ${mo['net']:,.0f} PF {mo['pf']:.2f} n {mo['n']}")
x2=[(c,t*2) for c,t in bot.SLIPPAGE_SCALE_TIERS]
sv=bot.SLIPPAGE_SCALE_TIERS; bot.SLIPPAGE_SCALE_TIERS=x2
_,tr2,_,_=bot.run_backtest(ds,sl)
bot.SLIPPAGE_SCALE_TIERS=sv
m2=M(tr2); mo2=M(OOS(tr2))
print(f"SLIP x2 full: net ${m2['net']:,.0f} PF {m2['pf']:.2f}")
print(f"SLIP x2 OOS: net ${mo2['net']:,.0f} PF {mo2['pf']:.2f} n {mo2['n']}")
net10,k = top10pct_removed([t.pnl_usd for t in tr])
print(f"TOP-10% REMOVAL full: removed {k} best trades -> net ${net10:,.0f} (was ${m['net']:,.0f})")
net10o,ko = top10pct_removed([t.pnl_usd for t in OOS(tr)])
print(f"TOP-10% REMOVAL OOS: removed {ko} -> net ${net10o:,.0f} (was ${mo['net']:,.0f})")
print("GATE OOS PF>=3.3 baseline:", 'PASS' if mo['pf']>=3.3 else 'FAIL')
print("GATE OOS PF>=3.3 slip x2:", 'PASS' if mo2['pf']>=3.3 else 'FAIL')
