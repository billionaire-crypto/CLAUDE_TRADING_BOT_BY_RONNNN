import re
from datetime import time as dtime, timedelta
from typing import Optional

import pandas as pd


_MONTH_CODES = {
    "F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
    "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
}
_SYMBOL_RE = re.compile(
    r"^(?P<root>[A-Z0-9]+?)(?P<month>[FGHJKMNQUVXZ])(?P<year>\d{1,2})$"
)


def contract_sort_key(symbol: str, reference_year: Optional[int] = None):
    """Return an expiry key, resolving one-digit years around reference_year."""
    match = _SYMBOL_RE.match(str(symbol).strip().upper())
    if not match:
        return (9999, 99)
    token = match.group("year")
    year_digit = int(token)
    if len(token) == 2:
        year = 2000 + year_digit
    elif reference_year is None:
        year = 2010 + year_digit if year_digit >= 9 else 2020 + year_digit
    else:
        decade = (int(reference_year) // 10) * 10
        candidates = (
            decade - 10 + year_digit,
            decade + year_digit,
            decade + 10 + year_digit,
        )
        year = min(
            candidates,
            key=lambda value: (
                abs(value - int(reference_year)),
                value < int(reference_year) - 1,
                value,
            ),
        )
    return (year, _MONTH_CODES[match.group("month")])


def select_front_month(
    df_5m: pd.DataFrame,
    initial_contract: Optional[str] = None,
    return_selected_contract: bool = False,
):
    """Choose one contract at each RTH open using only completed Globex volume.

    For RTH date D, the decision uses the futures session from 17:00 CT on D-1
    through 08:25 CT on D. Later volume cannot rewrite the morning's contract.
    The expiry may advance but never move backward.
    """
    work = df_5m.reset_index() if df_5m.index.name else df_5m.copy()
    required = {"ts_event", "symbol", "open", "high", "low", "close", "volume"}
    missing_columns = sorted(required - set(work.columns))
    if missing_columns:
        raise ValueError(f"front-month input is missing columns: {missing_columns}")
    if work.empty:
        raise ValueError("front-month selection received no 5-minute bars")
    if work["ts_event"].dt.tz is None:
        raise ValueError("front-month selection requires timezone-aware timestamps")

    work["_ct"] = work["ts_event"].dt.tz_convert("America/Chicago")
    work["_ct_time"] = work["_ct"].dt.time
    work["_session_date"] = [
        (timestamp.date() + timedelta(days=1))
        if timestamp.time() >= dtime(17, 0)
        else timestamp.date()
        for timestamp in work["_ct"]
    ]
    rth_mask = (
        (work["_ct_time"] >= dtime(8, 30))
        & (work["_ct_time"] <= dtime(15, 0))
    )
    rth_dates = sorted(work.loc[rth_mask, "_session_date"].unique())
    if not rth_dates:
        raise ValueError("front-month selection found no RTH sessions")

    preopen_mask = (
        (work["_ct_time"] >= dtime(17, 0))
        | (work["_ct_time"] < dtime(8, 30))
    )
    preopen_volume = (
        work[preopen_mask]
        .groupby(["_session_date", "symbol"], as_index=False)["volume"]
        .sum()
    )
    volume_by_session = {
        session_date: group[["symbol", "volume"]]
        for session_date, group in preopen_volume.groupby("_session_date")
    }

    current = initial_contract
    chosen = {}
    for session_date in rth_dates:
        volume = volume_by_session.get(
            session_date,
            pd.DataFrame(columns=["symbol", "volume"]),
        )
        volume = volume[
            volume["symbol"].map(
                lambda symbol: contract_sort_key(symbol, session_date.year)
                != (9999, 99)
            )
        ]

        candidate = None
        if not volume.empty:
            maximum = volume["volume"].max()
            tied = volume.loc[volume["volume"] == maximum, "symbol"].tolist()
            if current in tied:
                candidate = current
            else:
                candidate = min(
                    tied,
                    key=lambda symbol: contract_sort_key(
                        symbol, session_date.year
                    ),
                )

        if current is None:
            if candidate is None:
                raise ValueError(
                    "no parseable pre-open contract volume for first RTH "
                    f"session {session_date}"
                )
            current = candidate
        elif (
            candidate is not None
            and contract_sort_key(candidate, session_date.year)
            > contract_sort_key(current, session_date.year)
        ):
            current = candidate
        chosen[session_date] = current

    work["_front"] = work["_session_date"].map(chosen)

    # Never splice another contract into a missing selected-contract timestamp.
    # A hole is safer as a hard failure than as a fabricated cross-contract bar.
    rth = work[rth_mask & work["_front"].notna()]
    for session_date, day in rth.groupby("_session_date"):
        expected = set(day["ts_event"])
        selected = set(day.loc[day["symbol"] == day["_front"], "ts_event"])
        missing = sorted(expected - selected)
        if missing:
            raise ValueError(
                f"selected contract {chosen[session_date]} is missing "
                f"{len(missing)} RTH timestamp(s) on {session_date}; "
                f"first={missing[0]}"
            )

    work = work[work["symbol"] == work["_front"]]
    output = (
        work.drop(columns=["_ct", "_ct_time", "_session_date", "_front"])
        .set_index("ts_event")
        .sort_index()
    )
    if return_selected_contract:
        return output, current
    return output


def _validate_raw_frame(df: pd.DataFrame) -> None:
    required = {"ts_event", "open", "high", "low", "close", "volume", "symbol"}
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(f"missing required CSV columns: {missing}")
    for column in ("open", "high", "low", "close", "volume"):
        df[column] = pd.to_numeric(df[column], errors="coerce")
    if df[["open", "high", "low", "close", "volume"]].isna().any().any():
        raise ValueError("OHLCV columns contain non-numeric or null values")
    if (df["volume"] < 0).any():
        raise ValueError("volume contains negative values")
    if df["symbol"].astype(str).str.strip().eq("").any():
        raise ValueError("symbol contains empty values")


def _validate_output_frame(df: pd.DataFrame) -> None:
    if df.empty:
        raise ValueError("no RTH bars remain after front-month selection")
    if not df.index.is_monotonic_increasing or df.index.has_duplicates:
        raise ValueError("output timestamps are not strictly increasing and unique")
    bad_high = df["high"] < df[["open", "close", "low"]].max(axis=1)
    bad_low = df["low"] > df[["open", "close", "high"]].min(axis=1)
    if bad_high.any() or bad_low.any():
        raise ValueError("OHLC price invariants failed after resampling")


def load_ohlcv_csv(
    filepath: str,
    initial_contract: Optional[str] = None,
    return_selected_contract: bool = False,
):
    """Load one source file and return a causal, single-contract 5-minute series."""
    print("Loading CSV...")
    frame = pd.read_csv(filepath)
    _validate_raw_frame(frame)
    frame = frame[
        ["ts_event", "open", "high", "low", "close", "volume", "symbol"]
    ]
    frame["ts_event"] = pd.to_datetime(
        frame["ts_event"], utc=True, errors="coerce"
    ).dt.tz_convert("US/Eastern")
    if frame["ts_event"].isna().any():
        raise ValueError("ts_event contains unparseable timestamps")
    frame = frame.sort_values("ts_event")

    frame = frame.set_index("ts_event")
    bars_5m = (
        frame.groupby("symbol")
        .resample("5min")
        .agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        )
        .dropna()
    )
    bars_5m, final_contract = select_front_month(
        bars_5m.reset_index(),
        initial_contract=initial_contract,
        return_selected_contract=True,
    )
    bars_5m = bars_5m.between_time("09:30", "16:00")
    bars_5m = bars_5m[["open", "high", "low", "close", "volume"]]
    _validate_output_frame(bars_5m)

    print(f"Done! {len(bars_5m):,} 5-minute bars loaded")
    print(f"Date range: {bars_5m.index[0]} to {bars_5m.index[-1]}")
    print(bars_5m.head())
    if return_selected_contract:
        return bars_5m, final_contract
    return bars_5m


if __name__ == "__main__":
    from src.bot import DATA_PATH

    loaded = load_ohlcv_csv(DATA_PATH)
