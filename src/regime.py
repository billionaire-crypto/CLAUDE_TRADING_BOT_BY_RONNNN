"""
╔══════════════════════════════════════════════════════════════╗
║  REGIME CLASSIFIER — "what mood is the market in right now?"  ║
║                                                              ║
║  Adds the regime features the base indicators lack, then     ║
║  labels every bar: trend / chop / high_vol / low_vol /       ║
║  transition. The router (src/strategies.py) reads the LABEL  ║
║  of the just-CLOSED bar to pick which strategy may fire —    ║
║  so there is no look-ahead.                                  ║
║                                                              ║
║  Thresholds are research-backed and FEW (ER~0.35, CHOP       ║
║  61.8/38.2 Fibonacci lines, ATR percentile 20/85). They are  ║
║  documented conventions, NOT values fit to the data, and     ║
║  must never be tuned against the sealed holdout.             ║
║                                                              ║
║  Signals used (per the research):                            ║
║   - Kaufman Efficiency Ratio  -> trend vs noise              ║
║   - Choppiness Index(14)      -> range vs trend              ║
║   - ATR percentile            -> volatility regime           ║
║   - VWAP slope                -> flat (fade) vs sloped (trend)║
║   - Bollinger / Keltner squeeze -> low-vol coil flag         ║
║   - RSI(2)                    -> mean-reversion trigger       ║
║   ADX is used by strategies only to EXCLUDE trend trades     ║
║   (it lags — never as confirmation).                         ║
╚══════════════════════════════════════════════════════════════╝
"""
import numpy as np
import pandas as pd

# ── Thresholds (documented conventions — do NOT fit to the holdout) ─────────────
ER_PERIOD          = 20
ER_TREND           = 0.35      # efficiency ratio above this == directional
CHOP_PERIOD        = 14
CHOP_RANGE         = 61.8      # Choppiness Index above this == range/chop
CHOP_TREND         = 38.2      # below this == strong trend
ATR_PCT_WINDOW     = 100       # trailing bars for the ATR percentile rank
ATR_PCT_HIGH       = 0.85      # >= == high-vol regime
ATR_PCT_LOW        = 0.20      # <= == low-vol regime
VWAP_SLOPE_PERIOD  = 6         # bars
VWAP_SLOPE_FLAT    = 0.15      # |slope| (in ATRs) below this == flat / rotational
BB_PERIOD          = 20
BB_STD             = 2.0
KC_PERIOD          = 20
KC_ATR_MULT        = 1.5
RSI2_PERIOD        = 2


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def add_regime_features(df: pd.DataFrame) -> pd.DataFrame:
    """Attach regime features + a `regime_class` label column. Expects `atr`/`vwap`
    already present (from bot.add_indicators)."""
    close, high, low = df["close"], df["high"], df["low"]

    # Kaufman Efficiency Ratio: net move / sum of absolute moves.
    direction = (close - close.shift(ER_PERIOD)).abs()
    volatility = close.diff().abs().rolling(ER_PERIOD).sum()
    df["er"] = direction / volatility.replace(0, np.nan)

    # Choppiness Index: how much of the range was "wasted" oscillating.
    tr = pd.concat([high - low,
                    (high - close.shift()).abs(),
                    (low - close.shift()).abs()], axis=1).max(axis=1)
    tr_sum = tr.rolling(CHOP_PERIOD).sum()
    span = high.rolling(CHOP_PERIOD).max() - low.rolling(CHOP_PERIOD).min()
    df["chop"] = 100 * np.log10((tr_sum / span.replace(0, np.nan))) / np.log10(CHOP_PERIOD)

    # ATR percentile rank over a trailing window (0..1). Vectorised rolling rank.
    df["atr_pct"] = df["atr"].rolling(ATR_PCT_WINDOW).rank(pct=True)

    # VWAP slope, normalised by ATR so it is comparable across price levels.
    df["vwap_slope"] = (df["vwap"] - df["vwap"].shift(VWAP_SLOPE_PERIOD)) / (
        VWAP_SLOPE_PERIOD * df["atr"].replace(0, np.nan))

    # Bollinger + Keltner -> squeeze flag (low-vol coil).
    bb_mid = close.rolling(BB_PERIOD).mean()
    bb_std = close.rolling(BB_PERIOD).std()
    df["bb_upper"] = bb_mid + BB_STD * bb_std
    df["bb_lower"] = bb_mid - BB_STD * bb_std
    df["bb_mid"] = bb_mid
    kc_mid = close.ewm(span=KC_PERIOD, adjust=False).mean()
    df["kc_upper"] = kc_mid + KC_ATR_MULT * df["atr"]
    df["kc_lower"] = kc_mid - KC_ATR_MULT * df["atr"]
    df["kc_mid"] = kc_mid
    df["squeeze"] = (df["bb_upper"] < df["kc_upper"]) & (df["bb_lower"] > df["kc_lower"])

    df["rsi2"] = _rsi(close, RSI2_PERIOD)

    # Prior 20-bar extremes (shifted to exclude the current bar) — for failed-breakout fades.
    df["roll20_high"] = high.rolling(20).max().shift(1)
    df["roll20_low"] = low.rolling(20).min().shift(1)

    df["regime_class"] = df.apply(classify, axis=1)
    return df


def classify(row) -> str:
    """Label one bar. Priority: volatility extremes first, then structure."""
    er, chop, atr_pct = row.get("er"), row.get("chop"), row.get("atr_pct")
    if pd.isna(er) or pd.isna(chop) or pd.isna(atr_pct):
        return "unknown"
    if atr_pct >= ATR_PCT_HIGH:
        return "high_vol"
    if atr_pct <= ATR_PCT_LOW:
        return "low_vol"
    if er >= ER_TREND or chop <= CHOP_TREND:
        return "trend"
    if chop >= CHOP_RANGE:
        return "chop"
    return "transition"


# Regimes the router can hold a rulebook entry for (transition/unknown -> stand aside).
TRADEABLE_REGIMES = ("trend", "chop", "high_vol", "low_vol")
