import pandas as pd

def load_mes_data(filepath: str) -> pd.DataFrame:
    print("Loading CSV...")
    df = pd.read_csv(filepath, parse_dates=['ts_event'])

    # Keep only relevant columns
    df = df[['ts_event', 'open', 'high', 'low', 'close', 'volume', 'symbol']]

    # Convert UTC to US/Eastern
    df['ts_event'] = pd.to_datetime(df['ts_event'], utc=True).dt.tz_convert('US/Eastern')

    # Filter to front-month continuous contract only
    # Front month symbols follow pattern: MES + month code + year (e.g. MESM9, MESH5)
    # We keep the most liquid contract at each point = lowest expiry still active
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

    # After resample, pick front month per bar
    # Keep only rows where symbol is the first alphabetically per timestamp
    # (Databento continuous contract handles this, just deduplicate)
    df_5m = df_5m.reset_index()
    df_5m = df_5m.sort_values(['ts_event', 'symbol'])
    df_5m = df_5m.drop_duplicates(subset='ts_event', keep='first')

    # Filter to regular trading hours only (9:30 - 16:00 EST)
    

    df_5m = df_5m.set_index('ts_event').sort_index()
    df_5m = df_5m.between_time("09:30", "16:00")
    df_5m = df_5m[['open', 'high', 'low', 'close', 'volume']]

    print(f"Done! {len(df_5m):,} 5-minute bars loaded")
    print(f"Date range: {df_5m.index[0]} to {df_5m.index[-1]}")
    print(df_5m.head())

    return df_5m


if __name__ == "__main__":
    filepath = r"C:\Users\kyawz\Downloads\GLBX-20260315-XECEHWSFA6\MES_1m_bars.csv"
    df = load_mes_data(filepath)