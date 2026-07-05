import pandas as pd, numpy as np
from datetime import datetime, timedelta
from dotenv import load_dotenv; load_dotenv('.env')
import src.bot as bot, src.topstepx_client as c, src.topstepx_runtime as tr

DB = r'C:\Users\kyawz\Downloads\databento_mnq_2026_apr_jul.csv'

print("=== A. STRATEGY ON DATABENTO FEED (independent of ProjectX) ===")
df = bot.load_ohlcv_csv(DB)
df = bot.filter_phantom_bars(df)
di = bot.add_indicators(df); ds = bot.generate_signals(di); sl = bot.compute_session_levels(di)
_, trades, daily, _ = bot.run_backtest(ds, sl)
print(f"Databento range {df.index[0]} -> {df.index[-1]} | {len(df)} bars | {len(trades)} total trades")
ext = [t for t in trades if str(t.date)[:10] > '2026-03-27']
print(f"\nOUT-OF-SAMPLE trades (03-28 -> 07-02, never in the CSV backtest): {len(ext)}")
net = sum(t.pnl_usd for t in ext); wins = sum(1 for t in ext if t.won)
print(f"  net ${net:,.0f} | WR {wins/len(ext)*100:.0f}% | avg ${net/len(ext):.0f}" if ext else "  none")
print("\nDROUGHT WINDOW on DATABENTO (06-22 -> 07-02):")
dw = [t for t in trades if '2026-06-22' <= str(t.date)[:10] <= '2026-07-02']
for t in dw:
    print(f"  {str(t.date)[:16]} {t.direction:<5} ctr {t.contracts:>2} {t.exit_reason:<11} ${t.pnl_usd:,.0f}")
# quiet-session count on databento in the drought
dbdays = sorted(set(str(t.date)[:10] for t in trades))
alldays = sorted(set(str(i.date())[:10] for i in di.index if '2026-06-22' <= str(i.date()) <= '2026-07-02'))
quiet = [d for d in alldays if d not in dbdays]
print(f"  quiet sessions 06-22..07-02 on Databento: {quiet}")

print("\n=== B. BAR-BY-BAR FEED COMPARISON (Databento vs ProjectX, June) ===")
cfg=c.TopstepXConfig.from_env(); cl=c.TopstepXClient(cfg); cl.authenticate()
ct=cl.resolve_contract(cfg.contract_search_text, live=cfg.live_data)
bars=cl.retrieve_bars(contract_id=str(ct['id']), start_time='2026-06-17T00:00:00Z', end_time='2026-07-02T23:00:00Z',
                      live=cfg.live_data, unit=2, unit_number=5, limit=2500, include_partial_bar=False)
px=tr._bars_to_strategy_df(bars)          # Central tz, 5m RTH front-month
dbc = df.copy(); dbc.index = dbc.index.tz_convert('UTC')
px2 = px.copy(); px2.index = px2.index.tz_convert('UTC')
j = dbc.join(px2, how='inner', lsuffix='_db', rsuffix='_px')
j = j[(j.index >= '2026-06-17') & (j.index <= '2026-07-03')]
print(f"aligned bars: {len(j)}")
for col in ('open','high','low','close'):
    d = (j[col+'_db'] - j[col+'_px']).abs()
    print(f"  {col}: mean|diff| {d.mean():.4f} pts | max {d.max():.2f} | bars>1pt {int((d>1).sum())} | exact {int((d<0.01).sum())}/{len(j)}")
vd=(j['volume_db']-j['volume_px']).abs()
print(f"  volume: mean|diff| {vd.mean():.0f} | median {vd.median():.0f}")

print("\n=== C. VERDICT DATA ===")
print(f"Databento OOS extension made ${net:,.0f} over {len(ext)} trades (fresh, unseen 3 months)")
print("Done.")
