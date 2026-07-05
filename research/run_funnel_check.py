"""Entry-funnel forensics: which pipeline stage collapsed in the live window?
Stages per session: signal-column bars -> strong-regime bars -> conjunction ->
FVG 3-bar patterns -> engine entries. Applied identically to CSV years and live."""
import numpy as np, pandas as pd
from datetime import datetime
from dotenv import load_dotenv; load_dotenv('.env')
import src.bot as bot, src.topstepx_client as c, src.topstepx_runtime as tr

def funnel(ds, di, trades, label, d0, d1):
    w = ds[(ds.index >= d0) & (ds.index <= d1 + ' 23:59')]
    wi = di[(di.index >= d0) & (di.index <= d1 + ' 23:59')]
    days = max(1, len(set(w.index.date)))
    sig = (w['long_signal'] | w['short_signal']).mean() * 100
    g = wi.dropna(subset=['atr', 'atr_avg_20', 'adx'])
    ratio = g['atr'] / g['atr_avg_20']
    strong = ((ratio >= bot.STRONG_ATR_RATIO) & (ratio < 2.0) & (g['adx'] >= bot.ADX_STRONG_THRESHOLD)).mean() * 100
    h, l = w['high'].values, w['low'].values
    fvg = sum(1 for i in range(2, len(w)) if l[i] > h[i-2] or h[i] < l[i-2]) / days
    nt = sum(1 for t in trades if d0 <= str(t.date)[:10] <= d1)
    print(f"  {label:<16} sig-bars {sig:5.1f}% | strong {strong:5.1f}% | FVG/day {fvg:5.1f} | entries {nt}")

print("=== CSV windows (06-17..07-03) ===")
df_raw = bot.fetch_data(); bot.validate_loaded_data(df_raw)
di = bot.add_indicators(df_raw); sl = bot.compute_session_levels(di)
ds = bot.generate_signals(di.copy())
_, tr_all, _, _ = bot.run_backtest(ds, sl)
for yr in (2022, 2024, 2025):
    funnel(ds, di, tr_all, f"{yr} CSV", f"{yr}-06-17", f"{yr}-07-03")
print("  -- and the LAST CSV weeks (2026-03-09..03-27, engine traded then): --")
funnel(ds, di, tr_all, "2026 CSV Mar", "2026-03-09", "2026-03-27")

print("\n=== LIVE feed ===")
cfg = c.TopstepXConfig.from_env(); cl = c.TopstepXClient(cfg); cl.authenticate()
ct = cl.resolve_contract(cfg.contract_search_text, live=cfg.live_data)
now = datetime.utcnow()
bars = cl.retrieve_bars(contract_id=str(ct['id']), start_time='2026-06-17T00:00:00Z',
                        end_time=now.replace(microsecond=0).isoformat() + 'Z',
                        live=cfg.live_data, unit=2, unit_number=5, limit=2500, include_partial_bar=False)
lf = tr._bars_to_strategy_df(bars)
lfi = bot.add_indicators(lf); lfs = bot.generate_signals(lfi); lsl = bot.compute_session_levels(lfi)
_, lt, _, _ = bot.run_backtest(lfs, lsl)
funnel(lfs, lfi, lt, "2026 LIVE full", "2026-06-18", "2026-07-03")
funnel(lfs, lfi, lt, "2026 LIVE 06-23..26", "2026-06-23", "2026-06-26")
funnel(lfs, lfi, lt, "2026 LIVE 06-29..07-03", "2026-06-29", "2026-07-03")
print("Done.")
