"""
Build the European-session MNQ dataset from the SAME raw Databento file the
bot already uses — just keeping the hours the RTH loader throws away.

RTH loader keeps 09:30-16:00 ET. European session = ~London-open to US-pre-open,
which in ET is 03:00-09:30 ET (= 02:00-08:30 CT). Same resample/front-month
logic as src/load_data.load_ohlcv_csv; only the time filter differs.

FIRST GATE (before any strategy): liquidity. If European volume/bar is a tiny
fraction of RTH, slippage murders any edge -> stop here.

Output: research/euro_5m.csv  (+ prints RTH-vs-EU liquidity comparison)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pandas as pd

import src.bot as bot

EU_START, EU_END = "03:00", "09:30"     # ET (= 02:00-08:30 CT)
RTH_START, RTH_END = "09:30", "16:00"
OUT = os.path.join(os.path.dirname(__file__), "euro_5m.csv")


def resample_frontmonth(filepath):
    df = pd.read_csv(filepath, parse_dates=["ts_event"])
    df = df[["ts_event", "open", "high", "low", "close", "volume", "symbol"]]
    df["ts_event"] = pd.to_datetime(df["ts_event"], utc=True).dt.tz_convert("US/Eastern")
    df = df.sort_values("ts_event").set_index("ts_event")
    df5 = df.groupby("symbol").resample("5min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    df5 = df5.reset_index().sort_values(["ts_event", "volume"])
    df5 = df5.drop_duplicates(subset="ts_event", keep="last")     # front month = highest vol
    return df5.set_index("ts_event").sort_index()


def main():
    print(f"resampling raw file (this is the 422MB one, ~1-2 min)...")
    allbars = resample_frontmonth(bot.DATA_PATH)
    for ext in bot.DATA_PATH_EXTENSIONS:
        if os.path.exists(ext):
            allbars = pd.concat([allbars, resample_frontmonth(ext)])
            allbars = allbars[~allbars.index.duplicated(keep="last")].sort_index()

    # Apply the SAME phantom-bar filter the RTH path uses (fetch_data does this
    # after load_ohlcv_csv). Thin overnight data has more bad single-bar prints;
    # without this a garbage print produces an impossible -$75k "trade".
    allbars = bot.filter_phantom_bars(allbars)
    # Extra sanity cap for the thin book: drop any 5-min bar whose range exceeds
    # 3% of price (a real MNQ 5-min bar is a fraction of that; anything bigger is
    # a bad print, not a tradeable move).
    rng_frac = (allbars["high"] - allbars["low"]) / allbars["close"]
    bad = rng_frac > 0.03
    if bad.any():
        print(f"  dropped {int(bad.sum())} bars with >3% range (bad prints)")
        allbars = allbars[~bad]

    eu = allbars.between_time(EU_START, EU_END)[["open", "high", "low", "close", "volume"]]
    rth = allbars.between_time(RTH_START, RTH_END)

    print("=" * 66)
    print("LIQUIDITY GATE — European session vs RTH (MNQ 5-min bars)")
    print("=" * 66)
    print(f"  EU bars : {len(eu):,}   range {str(eu.index[0])[:10]} -> {str(eu.index[-1])[:10]}")
    print(f"  RTH bars: {len(rth):,}")
    print(f"  median vol/bar  EU: {eu['volume'].median():,.0f}   RTH: {rth['volume'].median():,.0f}"
          f"   -> EU is {eu['volume'].median()/rth['volume'].median()*100:.0f}% of RTH")
    print(f"  mean vol/bar    EU: {eu['volume'].mean():,.0f}   RTH: {rth['volume'].mean():,.0f}")
    # per-hour (ET) volume profile so we can see WHICH euro hours are liquid
    eu2 = eu.copy(); eu2["h"] = eu2.index.hour
    print("\n  volume by hour (ET) in the EU window:")
    for h, g in eu2.groupby("h"):
        print(f"    {h:02d}:00 ET ({(h-1)%24:02d}:00 CT)  median vol {g['volume'].median():>7,.0f}  bars {len(g):>6,}")

    eu.to_csv(OUT)
    print(f"\nsaved -> {OUT}")
    print("VERDICT: proceed to strategy test only if EU liquidity is a workable fraction of RTH")
    print("(rule of thumb: >20% median volume = tradeable with realistic slippage).")


if __name__ == "__main__":
    main()
