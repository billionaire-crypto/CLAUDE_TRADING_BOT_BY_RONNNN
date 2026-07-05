"""Deep live-vs-backtest alignment check (no overlapping window exists, so:
A. season-matched transplant: same engine on 06-17..07-03 windows of every CSV year
B. rolling 11-session trade-count percentile of the live window's rate
C. feed character: range/volume/FVG-pattern frequency, live feed vs CSV years"""
import numpy as np, pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv; load_dotenv('.env')
import src.bot as bot, src.topstepx_client as c, src.topstepx_runtime as tr

def fvg_patterns_per_day(df):
    h,l = df['high'].values, df['low'].values
    n=0
    for i in range(2,len(df)):
        if l[i] > h[i-2] or h[i] < l[i-2]: n+=1
    days=len(set(df.index.date))
    return n/days if days else 0

def charac(df, label):
    rng=(df['high']-df['low'])
    pct=(rng/df['close']*100)
    days=len(set(df.index.date))
    print(f"  {label:<22} days {days:>3} | bar range {rng.mean():6.2f} pts ({pct.mean():.3f}%) | vol {df['volume'].mean():7.0f} | FVG-patterns/day {fvg_patterns_per_day(df):5.1f}")

print("=== A+C: CSV season windows (same engine, same dates) ===")
df_raw=bot.fetch_data(); bot.validate_loaded_data(df_raw)
di=bot.add_indicators(df_raw); sl=bot.compute_session_levels(di)
ds=bot.generate_signals(di.copy())
_,all_trades,daily,_=bot.run_backtest(ds,sl)
td={}
for t in all_trades: td.setdefault(str(t.date)[:10],0); td[str(t.date)[:10]]+=1
for yr in range(2019,2026):
    w=ds[(ds.index>=f'{yr}-06-17')&(ds.index<=f'{yr}-07-03 23:59')]
    if len(w)<100: continue
    wt=sum(v for k,v in td.items() if f'{yr}-06-17'<=k<=f'{yr}-07-03')
    charac(di[(di.index>=f'{yr}-06-17')&(di.index<=f'{yr}-07-03 23:59')], f'{yr} 06-17..07-03')
    print(f"      -> trades in window: {wt}")

print("\n=== live feed 2026 ===")
cfg=c.TopstepXConfig.from_env(); cl=c.TopstepXClient(cfg); cl.authenticate()
ct=cl.resolve_contract(cfg.contract_search_text, live=cfg.live_data)
now=datetime.utcnow()
bars=cl.retrieve_bars(contract_id=str(ct['id']), start_time='2026-06-17T00:00:00Z', end_time=now.replace(microsecond=0).isoformat()+'Z', live=cfg.live_data, unit=2, unit_number=5, limit=2500, include_partial_bar=False)
lf=tr._bars_to_strategy_df(bars)
charac(lf, '2026 live 06-18..07-03')
lfi=bot.add_indicators(lf); lfs=bot.generate_signals(lfi); lsl=bot.compute_session_levels(lfi)
_,lt,_,_=bot.run_backtest(lfs,lsl)
print(f"      -> trades in window: {len(lt)}")

print("\n=== B: rolling 11-session trade-count distribution (7yr) ===")
sess=[str(d.session_date)[:10] for d in daily]
counts=[sum(td.get(s,0) for s in sess[i:i+11]) for i in range(len(sess)-10)]
cs=np.array(counts)
live_n=len(lt)
print(f"  11-session windows: mean {cs.mean():.1f} trades | p5 {np.percentile(cs,5):.0f} | p10 {np.percentile(cs,10):.0f} | min {cs.min()}")
print(f"  windows with <= {live_n} trades: {(cs<=live_n).mean()*100:.1f}%")
print("Done.")
