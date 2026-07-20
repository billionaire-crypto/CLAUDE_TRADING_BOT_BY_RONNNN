import pandas as pd

def load_ohlcv_csv(filepath: str) -> pd.DataFrame:
    """Generic 1-minute OHLCV CSV loader/resampler to 5-minute RTH bars.

    Instrument-agnostic: applies NO tick/point/dollar math — it only reshapes
    OHLCV. The active instrument is whatever `filepath` points to (currently the
    MNQ GLBX file set by bot.DATA_PATH). Formerly named load_mes_data.
    """
    print("Loading CSV...")
    df = pd.read_csv(filepath, parse_dates=['ts_event'])

    # Keep only relevant columns
    df = df[['ts_event', 'open', 'high', 'low', 'close', 'volume', 'symbol']]

    # Convert UTC to US/Eastern
    df['ts_event'] = pd.to_datetime(df['ts_event'], utc=True).dt.tz_convert('US/Eastern')

    # Filter to front-month continuous contract only.
    # Front-month symbols follow pattern: <ROOT> + month code + year (e.g. MNQM9).
    # We keep the most liquid contract at each point = lowest expiry still active.
    df = df.sort_values('ts_event')

    # Resample 1m -> 5m
    df = df.set_index('ts_event')
    df_5m = df.groupby('symbol').resample('5min').agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    }).dropna()

    # After resample, pick front month per bar.
    # Front month = highest-volume contract at each timestamp — avoids stale
    # back-month prints that create phantom spike-and-revert bars.
    df_5m = df_5m.reset_index()
    df_5m = df_5m.sort_values(['ts_event', 'volume'])          # ascending volume
    df_5m = df_5m.drop_duplicates(subset='ts_event', keep='last')  # keep highest

    # Filter to regular trading hours only (9:30 - 16:00 EST)
    

    df_5m = df_5m.set_index('ts_event').sort_index()
    df_5m = df_5m.between_time("09:30", "16:00")
    df_5m = df_5m[['open', 'high', 'low', 'close', 'volume']]

    print(f"Done! {len(df_5m):,} 5-minute bars loaded")
    print(f"Date range: {df_5m.index[0]} to {df_5m.index[-1]}")
    print(df_5m.head())

    return df_5m


if __name__ == "__main__":
    # Manual smoke test: point this at the active data file (see bot.DATA_PATH).
    from src.bot import DATA_PATH
    df = load_ohlcv_csv(DATA_PATH)