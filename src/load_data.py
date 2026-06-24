"""
Load a Databento GLBX 1-minute OHLCV CSV and build a clean, continuous
front-month MNQ series resampled to 5-minute RTH bars.

Why this file matters
---------------------
A futures "market" is really many separate contracts (Mar/Jun/Sep/Dec, each
year) trading at once. Only ONE — the highest-volume one — is the real, liquid
market at any time; the others are nearly dead and quote stale, off-market
prices. The original loader picked the contract whose symbol came FIRST
alphabetically, which has nothing to do with liquidity. That injected stale
back-month prints into ~6% of bars and created fake 50-140pt spike-and-revert
jumps that mean-reversion backtests would feast on.

Fix: pick the highest-VOLUME contract each day (the genuine front month) and
never roll backward to an already-expiring contract. This is the standard
volume-based continuous-contract construction and gives the bot automatic
contract roll-over for backtests.
"""
import pandas as pd

# CME month codes -> calendar month number (1..12).
MONTH_CODES = {
    "F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
    "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
}

# Outright MNQ contracts only: MNQ + month code + single year digit (e.g. MNQH4).
# Excludes calendar spreads (e.g. "MNQH4-MNQM4") and any other instruments.
_OUTRIGHT_RE = r"^MNQ[FGHJKMNQUVXZ]\d$"


def _expiry_rank(symbol: str, ref_year: int) -> int:
    """Chronological sort key for a contract = expiry_year*12 + expiry_month.

    The symbol carries only a single year DIGIT (MNQH4 -> "4"), which is
    ambiguous across decades. We disambiguate using ref_year: a year drawn from
    the contract's PEAK-volume day, which always falls within a few months of
    expiry. The expiry year is whichever of {ref_year, ref_year+1} ends in that
    digit.
    """
    month = MONTH_CODES[symbol[-2]]
    year_digit = int(symbol[-1])
    for y in (ref_year, ref_year + 1):
        if y % 10 == year_digit:
            return y * 12 + month
    # Fallback (shouldn't happen for clean data): assume same decade as ref.
    return (ref_year - ref_year % 10 + year_digit) * 12 + month


def load_mes_data(filepath: str) -> pd.DataFrame:
    print("Loading CSV...")
    df = pd.read_csv(filepath, parse_dates=["ts_event"])
    df = df[["ts_event", "open", "high", "low", "close", "volume", "symbol"]]

    # Keep only outright MNQ contracts (drop spreads / stray instruments).
    df = df[df["symbol"].str.match(_OUTRIGHT_RE)]

    # UTC -> US/Eastern.
    df["ts_event"] = pd.to_datetime(df["ts_event"], utc=True).dt.tz_convert("US/Eastern")
    df = df.sort_values("ts_event").set_index("ts_event")

    # Resample each contract separately to 5-minute OHLCV.
    d5 = df.groupby("symbol").resample("5min").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna().reset_index()

    d5["date"] = d5["ts_event"].dt.date

    # Expiry rank per contract, year-digit disambiguated via its peak-volume day.
    daily_sym_vol = d5.groupby(["symbol", "date"])["volume"].sum()
    peak_day = daily_sym_vol.groupby("symbol").idxmax()       # -> (symbol, date)
    peak_year = peak_day.map(lambda k: k[1].year)
    ranks = {s: _expiry_rank(s, int(peak_year[s])) for s in peak_year.index}
    d5["rank"] = d5["symbol"].map(ranks)

    # Front month per day = highest-volume contract, with a FORWARD-ONLY roll
    # (never switch back to an earlier/expiring contract once we've rolled).
    day_vol = (d5.groupby(["date", "symbol"])
                 .agg(volume=("volume", "sum"), rank=("rank", "first"))
                 .reset_index())
    front = {}
    cur_rank = -1
    for date, grp in day_vol.groupby("date"):
        eligible = grp[grp["rank"] >= cur_rank]
        if eligible.empty:                      # all candidates already expired
            eligible = grp
        pick = eligible.sort_values("volume").iloc[-1]
        front[date] = pick["symbol"]
        cur_rank = max(cur_rank, int(pick["rank"]))

    # Keep only the chosen front-month contract's bars on each day.
    d5["front_symbol"] = d5["date"].map(front)
    d5 = d5[d5["symbol"] == d5["front_symbol"]]

    # Regular trading hours only (09:30-16:00 ET).
    d5 = d5.set_index("ts_event").sort_index()
    d5 = d5.between_time("09:30", "16:00")
    d5 = d5[["open", "high", "low", "close", "volume"]]

    print(f"Done! {len(d5):,} 5-minute bars loaded")
    print(f"Date range: {d5.index[0]} -> {d5.index[-1]}")
    print(d5.head())

    return d5


if __name__ == "__main__":
    import os
    filepath = os.environ.get(
        "MNQ_DATA_PATH",
        r"C:\Users\kyawz\Downloads\GLBX-20260331-885WT5W7KA\glbx-mdp3-20100606-20260329.ohlcv-1m.csv",
    )
    df = load_mes_data(filepath)
