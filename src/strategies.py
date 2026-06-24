"""
╔══════════════════════════════════════════════════════════════╗
║  STRATEGY CANDIDATES — locked-param, first-principles ideas   ║
║                                                              ║
║  Each candidate is ONLY an entry trigger ("what to trade").  ║
║  Everything else — exits, sizing, costs, Topstep risk,       ║
║  accounting — is shared by the harness in src/evaluate.py so ║
║  the candidates compete on a level field.                    ║
║                                                              ║
║  Interface:                                                  ║
║    reset_session(session_date)   once per trading day        ║
║    observe(i,row,prev,bar_2,ctx) every bar (maintain state)  ║
║    entry(i,row,prev,bar_2,ctx)   when flat -> EntrySignal?   ║
║    confirm(signal)               harness took the trade       ║
║                                                              ║
║  A signal supplies only direction + stop_price; the harness  ║
║  derives the target (a fixed R-multiple) so every candidate  ║
║  uses identical exit/target logic.                           ║
╚══════════════════════════════════════════════════════════════╝
"""
from dataclasses import dataclass, field
from datetime import time as dtime
from typing import Optional

import pandas as pd

from src import bot

TICK = bot.MNQ_TICK_SIZE
STOP_CAP_TICKS = 40            # 10 NQ points — hard risk ceiling (shared, first-principles)


@dataclass
class EntrySignal:
    direction: str             # "long" | "short"
    stop_price: float          # harness derives the target from this
    entry_type: str            # label for reporting
    meta: dict = field(default_factory=dict)
    # Trend strategies ride until the trend flips; mean-reversion fades must NOT
    # (they enter counter-trend), so they opt out.
    exit_on_trend_flip: bool = True
    # Fades aim at the mean (VWAP / band middle), not a fixed 2R. If set, the
    # harness uses this target instead of deriving 2R from the stop.
    target_price: float = None


def _cap_long_stop(entry_price: float, raw_stop: float) -> float:
    return max(raw_stop, entry_price - STOP_CAP_TICKS * TICK)


def _cap_short_stop(entry_price: float, raw_stop: float) -> float:
    return min(raw_stop, entry_price + STOP_CAP_TICKS * TICK)


class Strategy:
    name = "base"

    def reset_session(self, session_date):  # noqa: D401 - simple hook
        pass

    def observe(self, i, row, prev_row, bar_2, ctx):
        pass

    def entry(self, i, row, prev_row, bar_2, ctx) -> Optional[EntrySignal]:
        return None

    def confirm(self, signal: EntrySignal):
        pass


# ── A & B: FVG in the direction of trend (B adds a displacement filter) ─────────
class FVGTrend(Strategy):
    """Trade a fair-value gap in the direction of trend.

    require_displacement=True ("better FVG"): only keep gaps whose middle
    candle was a strong displacement bar (range >= mult x ATR). Thesis: gaps
    left by real institutional displacement continue more reliably than
    incidental ones.
    """

    def __init__(self, require_displacement: bool = False, displacement_mult: float = 1.5):
        self.require_displacement = require_displacement
        self.displacement_mult = displacement_mult
        self.name = "FVG-displacement" if require_displacement else "FVG-trend"
        self.active = []

    def reset_session(self, session_date):
        self.active = [f for f in self.active if f.session_date == session_date]

    def observe(self, i, row, prev_row, bar_2, ctx):
        fvg_min = bot.FVG_MIN_SIZE_TICKS * TICK
        # The middle candle (prev_row) is the displacement bar.
        disp_ok = True
        if self.require_displacement:
            atr = float(row["atr"]) if not pd.isna(row["atr"]) else 0.0
            disp_range = float(prev_row["high"]) - float(prev_row["low"])
            disp_ok = atr > 0 and disp_range >= self.displacement_mult * atr

        if disp_ok and bar_2["high"] < row["low"] and (row["low"] - bar_2["high"]) >= fvg_min:
            self.active.append(bot.FVG("bullish", row["low"], bar_2["high"], i, ctx.session_date))
        if disp_ok and bar_2["low"] > row["high"] and (bar_2["low"] - row["high"]) >= fvg_min:
            self.active.append(bot.FVG("bearish", bar_2["low"], row["high"], i, ctx.session_date))

        self.active = [f for f in self.active
                       if bot.is_fvg_valid(f, i, ctx.session_date, row["high"], row["low"])]

    def entry(self, i, row, prev_row, bar_2, ctx) -> Optional[EntrySignal]:
        price = float(row["open"])
        if prev_row["long_bias"]:
            for f in self.active:
                if f.direction == "bullish" and bot.price_in_fvg(f, price):
                    stop = _cap_long_stop(price, f.bottom - TICK)
                    if price - stop <= 0:
                        return None
                    return EntrySignal("long", stop, self.name, {"fvg": f})
        if prev_row["short_bias"]:
            for f in self.active:
                if f.direction == "bearish" and bot.price_in_fvg(f, price):
                    stop = _cap_short_stop(price, f.top + TICK)
                    if stop - price <= 0:
                        return None
                    return EntrySignal("short", stop, self.name, {"fvg": f})
        return None

    def confirm(self, signal: EntrySignal):
        f = signal.meta.get("fvg")
        if f is not None:
            self.active = [x for x in self.active if x is not f]


# ── C: VWAP trend-pullback (non-FVG) ────────────────────────────────────────────
class VWAPPullback(Strategy):
    """In an established trend, enter the first pullback that tags VWAP and holds.

    Thesis: trends resume from fair value. Robust, widely used, no fitted knobs.
    """

    name = "VWAP-pullback"

    def entry(self, i, row, prev_row, bar_2, ctx) -> Optional[EntrySignal]:
        if pd.isna(prev_row["vwap"]):
            return None
        price = float(row["open"])
        vwap = float(prev_row["vwap"])

        # Long: uptrend, prior bar dipped to/under VWAP but closed above it, now holding.
        if prev_row["long_bias"] and prev_row["low"] <= vwap and prev_row["close"] > vwap and price >= vwap:
            raw_stop = min(float(prev_row["low"]), float(bar_2["low"])) - TICK
            stop = _cap_long_stop(price, raw_stop)
            if price - stop <= 0:
                return None
            return EntrySignal("long", stop, self.name)

        # Short: downtrend, prior bar popped to/over VWAP but closed below it, now holding.
        if prev_row["short_bias"] and prev_row["high"] >= vwap and prev_row["close"] < vwap and price <= vwap:
            raw_stop = max(float(prev_row["high"]), float(bar_2["high"])) + TICK
            stop = _cap_short_stop(price, raw_stop)
            if stop - price <= 0:
                return None
            return EntrySignal("short", stop, self.name)
        return None


# ── D: Opening-range breakout, done right (non-FVG) ─────────────────────────────
class ORBreakout(Strategy):
    """Trend-aligned breakout of the 9:30-10:00 CT opening range.

    A REAL range filter (8-60 ticks) replaces the original bot's broken
    ORB_MAX_RANGE_TICKS=0 that made it impossible to fire. One ORB per day.
    """

    name = "ORB-breakout"
    FORM_START = dtime(9, 30)
    FORM_END = dtime(10, 0)
    ENTRY_END = dtime(11, 0)
    MIN_RANGE_TICKS = 8
    MAX_RANGE_TICKS = 60

    def reset_session(self, session_date):
        self.high = None
        self.low = None
        self.formed = False
        self.fired = False

    def observe(self, i, row, prev_row, bar_2, ctx):
        t = ctx.ts_ct.time()
        if self.FORM_START <= t < self.FORM_END:
            h, l = float(row["high"]), float(row["low"])
            self.high = h if self.high is None else max(self.high, h)
            self.low = l if self.low is None else min(self.low, l)
        elif t >= self.FORM_END and not self.formed and self.high is not None:
            self.formed = True
            self.range_ticks = (self.high - self.low) / TICK

    def entry(self, i, row, prev_row, bar_2, ctx) -> Optional[EntrySignal]:
        t = ctx.ts_ct.time()
        if not self.formed or self.fired or not (self.FORM_END <= t < self.ENTRY_END):
            return None
        if not (self.MIN_RANGE_TICKS <= self.range_ticks <= self.MAX_RANGE_TICKS):
            return None
        price = float(row["open"])
        if prev_row["long_bias"] and price > self.high:
            stop = _cap_long_stop(price, self.low - TICK)
            if price - stop <= 0:
                return None
            return EntrySignal("long", stop, self.name, {"orb": True})
        if prev_row["short_bias"] and price < self.low:
            stop = _cap_short_stop(price, self.high + TICK)
            if stop - price <= 0:
                return None
            return EntrySignal("short", stop, self.name, {"orb": True})
        return None

    def confirm(self, signal: EntrySignal):
        self.fired = True


# ════════════════════════════════════════════════════════════════════════════════
#  REGIME-MATCHED STRATEGIES (read regime features from src/regime.py)
#  Fades enter COUNTER-trend and target the mean, so exit_on_trend_flip=False and
#  they supply their own target_price. They do NOT self-gate on regime — the edge
#  grid measures how each behaves per regime, and the RegimeRouter does the gating.
# ════════════════════════════════════════════════════════════════════════════════
MR_STRETCH_ATR = 1.5           # how far from VWAP counts as "stretched"
RSI2_OVERSOLD  = 15
RSI2_OVERBOUGHT = 85
KC_RSI2_OS     = 10            # tighter for the band-touch variant
KC_RSI2_OB     = 90


def _ok(*vals) -> bool:
    return not any(pd.isna(v) for v in vals)


# ── Choppy specialist 1: mean-reversion to VWAP (value) ─────────────────────────
class MeanReversionVWAP(Strategy):
    """In a balanced session, fade price that has stretched far from VWAP back to it.
    Anchored to VWAP (fair value), not a band. High-win-rate / modest-target profile."""

    name = "MeanRev-VWAP"

    def entry(self, i, row, prev_row, bar_2, ctx):
        vwap, close, atr, rsi2 = prev_row["vwap"], prev_row["close"], prev_row["atr"], prev_row["rsi2"]
        if not _ok(vwap, close, atr, rsi2) or atr <= 0:
            return None
        price = float(row["open"])
        if (vwap - close) >= MR_STRETCH_ATR * atr and rsi2 < RSI2_OVERSOLD and vwap > price:
            stop = _cap_long_stop(price, float(prev_row["low"]) - TICK)
            if price - stop <= 0:
                return None
            return EntrySignal("long", stop, self.name, exit_on_trend_flip=False, target_price=float(vwap))
        if (close - vwap) >= MR_STRETCH_ATR * atr and rsi2 > RSI2_OVERBOUGHT and vwap < price:
            stop = _cap_short_stop(price, float(prev_row["high"]) + TICK)
            if stop - price <= 0:
                return None
            return EntrySignal("short", stop, self.name, exit_on_trend_flip=False, target_price=float(vwap))
        return None


# ── Choppy specialist 2: Keltner band touch + RSI(2) extreme ────────────────────
class KeltnerRSIFade(Strategy):
    """Fade a close beyond the Keltner band confirmed by an RSI(2) extreme, back to
    the band middle. (Connors-style mean reversion, band-anchored.)"""

    name = "Keltner-RSI"

    def entry(self, i, row, prev_row, bar_2, ctx):
        kc_lo, kc_hi, kc_mid = prev_row["kc_lower"], prev_row["kc_upper"], prev_row["kc_mid"]
        close, rsi2 = prev_row["close"], prev_row["rsi2"]
        if not _ok(kc_lo, kc_hi, kc_mid, close, rsi2):
            return None
        price = float(row["open"])
        if close < kc_lo and rsi2 < KC_RSI2_OS and kc_mid > price:
            stop = _cap_long_stop(price, float(prev_row["low"]) - TICK)
            if price - stop <= 0:
                return None
            return EntrySignal("long", stop, self.name, exit_on_trend_flip=False, target_price=float(kc_mid))
        if close > kc_hi and rsi2 > KC_RSI2_OB and kc_mid < price:
            stop = _cap_short_stop(price, float(prev_row["high"]) + TICK)
            if stop - price <= 0:
                return None
            return EntrySignal("short", stop, self.name, exit_on_trend_flip=False, target_price=float(kc_mid))
        return None


# ── Choppy specialist 3: failed-breakout fade (Turtle Soup, Raschke/Connors) ─────
class TurtleSoup(Strategy):
    """Fade a FAILED break of the prior 20-bar extreme: the prior bar swept a new
    20-bar low/high but closed back inside the range. Self-selects false breakouts."""

    name = "TurtleSoup"

    def entry(self, i, row, prev_row, bar_2, ctx):
        r_lo, r_hi, mid = prev_row["roll20_low"], prev_row["roll20_high"], prev_row["bb_mid"]
        if not _ok(r_lo, r_hi, mid):
            return None
        price = float(row["open"])
        # Failed downside break -> fade long back to the mean.
        if float(prev_row["low"]) < r_lo and float(prev_row["close"]) > r_lo and mid > price:
            stop = _cap_long_stop(price, float(prev_row["low"]) - TICK)
            if price - stop <= 0:
                return None
            return EntrySignal("long", stop, self.name, exit_on_trend_flip=False, target_price=float(mid))
        # Failed upside break -> fade short.
        if float(prev_row["high"]) > r_hi and float(prev_row["close"]) < r_hi and mid < price:
            stop = _cap_short_stop(price, float(prev_row["high"]) + TICK)
            if stop - price <= 0:
                return None
            return EntrySignal("short", stop, self.name, exit_on_trend_flip=False, target_price=float(mid))
        return None


# ── Choppy candidate 1: failed break of YESTERDAY's RTH extreme ──────────────
class RangeEdgeFade(Strategy):
    """Fade a failed poke beyond yesterday's RTH high/low (a level the market
    respects) back toward VWAP. Trapped-breakout reversal — only at proven
    levels, unlike the statistical-stretch fades."""

    name = "Range-edge-fade"

    def __init__(self):
        self.prior_high = None
        self.prior_low = None
        self._cur_high = None
        self._cur_low = None

    def reset_session(self, session_date):
        if self._cur_high is not None:
            self.prior_high = self._cur_high
            self.prior_low = self._cur_low
        self._cur_high = None
        self._cur_low = None

    def observe(self, i, row, prev_row, bar_2, ctx):
        h, l = float(row["high"]), float(row["low"])
        self._cur_high = h if self._cur_high is None else max(self._cur_high, h)
        self._cur_low = l if self._cur_low is None else min(self._cur_low, l)

    def entry(self, i, row, prev_row, bar_2, ctx):
        if self.prior_high is None or pd.isna(prev_row["vwap"]):
            return None
        price = float(row["open"])
        vwap = float(prev_row["vwap"])
        if float(prev_row["high"]) > self.prior_high and float(prev_row["close"]) < self.prior_high and vwap < price:
            stop = _cap_short_stop(price, float(prev_row["high"]) + TICK)
            if stop - price <= 0:
                return None
            return EntrySignal("short", stop, self.name, exit_on_trend_flip=False, target_price=vwap)
        if float(prev_row["low"]) < self.prior_low and float(prev_row["close"]) > self.prior_low and vwap > price:
            stop = _cap_long_stop(price, float(prev_row["low"]) - TICK)
            if price - stop <= 0:
                return None
            return EntrySignal("long", stop, self.name, exit_on_trend_flip=False, target_price=vwap)
        return None


# ── Choppy candidate 2: expansion breakout out of a squeeze ──────────────────
class SqueezeBreakout(Strategy):
    """After a low-volatility coil (Bollinger inside Keltner = squeeze), trade the
    bar that closes outside the Bollinger band in the breakout direction. Flips
    the chop thesis: quiet precedes expansion. Shared 2R target."""

    name = "Squeeze-breakout"
    LOOKBACK = 6  # a squeeze within the last N closed bars still qualifies

    def __init__(self):
        self._since_squeeze = 999

    def reset_session(self, session_date):
        self._since_squeeze = 999

    def observe(self, i, row, prev_row, bar_2, ctx):
        sq = prev_row["squeeze"]
        if bool(sq):
            self._since_squeeze = 0
        else:
            self._since_squeeze += 1

    def entry(self, i, row, prev_row, bar_2, ctx):
        if self._since_squeeze > self.LOOKBACK:
            return None
        bb_u, bb_l, close = prev_row["bb_upper"], prev_row["bb_lower"], prev_row["close"]
        if not _ok(bb_u, bb_l, close):
            return None
        price = float(row["open"])
        if float(close) > float(bb_u):
            stop = _cap_long_stop(price, float(bb_l))
            if price - stop <= 0:
                return None
            return EntrySignal("long", stop, self.name, exit_on_trend_flip=False)
        if float(close) < float(bb_l):
            stop = _cap_short_stop(price, float(bb_u))
            if stop - price <= 0:
                return None
            return EntrySignal("short", stop, self.name, exit_on_trend_flip=False)
        return None


# ── Choppy candidate 3: failed poke outside the first-hour range ─────────────
class OpeningRangeFade(Strategy):
    """Fade a failed break of the 08:30-09:30 CT opening range (= the first RTH
    hour, 09:30-10:30 ET), back to its midpoint. One fade per side per day."""

    name = "OR-fade"
    FORM_START = dtime(8, 30)
    FORM_END = dtime(9, 30)

    def reset_session(self, session_date):
        self.or_high = None
        self.or_low = None
        self.or_mid = None
        self.formed = False
        self.faded_high = False
        self.faded_low = False

    def observe(self, i, row, prev_row, bar_2, ctx):
        t = ctx.ts_ct.time()
        if self.FORM_START <= t < self.FORM_END:
            h, l = float(row["high"]), float(row["low"])
            self.or_high = h if self.or_high is None else max(self.or_high, h)
            self.or_low = l if self.or_low is None else min(self.or_low, l)
        elif t >= self.FORM_END and not self.formed and self.or_high is not None:
            self.formed = True
            self.or_mid = (self.or_high + self.or_low) / 2.0

    def entry(self, i, row, prev_row, bar_2, ctx):
        if not self.formed:
            return None
        price = float(row["open"])
        if (not self.faded_high and float(prev_row["high"]) > self.or_high
                and float(prev_row["close"]) < self.or_high and self.or_mid < price):
            stop = _cap_short_stop(price, float(prev_row["high"]) + TICK)
            if stop - price <= 0:
                return None
            return EntrySignal("short", stop, self.name, {"side": "high"},
                               exit_on_trend_flip=False, target_price=self.or_mid)
        if (not self.faded_low and float(prev_row["low"]) < self.or_low
                and float(prev_row["close"]) > self.or_low and self.or_mid > price):
            stop = _cap_long_stop(price, float(prev_row["low"]) - TICK)
            if price - stop <= 0:
                return None
            return EntrySignal("long", stop, self.name, {"side": "low"},
                               exit_on_trend_flip=False, target_price=self.or_mid)
        return None

    def confirm(self, signal):
        if signal.meta.get("side") == "high":
            self.faded_high = True
        elif signal.meta.get("side") == "low":
            self.faded_low = True


# ── Choppy candidate 4: VWAP reversion gated to the midday lull ──────────────
class LunchLullReversion(Strategy):
    """The VWAP-stretch fade, allowed ONLY in the calm midday window
    (10:30-12:30 CT ~ 11:30-13:30 ET). The time gate IS the experiment: does
    reversion that fails all-day work when the market is at its quietest?"""

    name = "Lunch-reversion"
    LULL_START = dtime(10, 30)
    LULL_END = dtime(12, 30)
    STRETCH_ATR = 1.5
    RSI_OS = 15
    RSI_OB = 85

    def entry(self, i, row, prev_row, bar_2, ctx):
        t = ctx.ts_ct.time()
        if not (self.LULL_START <= t < self.LULL_END):
            return None
        vwap, close, atr, rsi2 = prev_row["vwap"], prev_row["close"], prev_row["atr"], prev_row["rsi2"]
        if not _ok(vwap, close, atr, rsi2) or float(atr) <= 0:
            return None
        price = float(row["open"])
        vwap = float(vwap)
        if (vwap - float(close)) >= self.STRETCH_ATR * float(atr) and float(rsi2) < self.RSI_OS and vwap > price:
            stop = _cap_long_stop(price, float(prev_row["low"]) - TICK)
            if price - stop <= 0:
                return None
            return EntrySignal("long", stop, self.name, exit_on_trend_flip=False, target_price=vwap)
        if (float(close) - vwap) >= self.STRETCH_ATR * float(atr) and float(rsi2) > self.RSI_OB and vwap < price:
            stop = _cap_short_stop(price, float(prev_row["high"]) + TICK)
            if stop - price <= 0:
                return None
            return EntrySignal("short", stop, self.name, exit_on_trend_flip=False, target_price=vwap)
        return None


# ── Trend engine: VWAP cross with EMA-trend filter (highest-evidence, low-param) ─
class VWAPCross(Strategy):
    """Enter on a close-confirmed cross of VWAP in the direction of the EMA trend.
    Rides with the trend (harness 2R target, trend-flip exit)."""

    name = "VWAP-cross"

    def entry(self, i, row, prev_row, bar_2, ctx):
        vwap_p, vwap_2 = prev_row["vwap"], bar_2["vwap"]
        ef, es = prev_row["ema_fast"], prev_row["ema_slow"]
        if not _ok(vwap_p, vwap_2, ef, es):
            return None
        price = float(row["open"])
        c_prev, c_2 = float(prev_row["close"]), float(bar_2["close"])
        # Cross confirmed on two closed bars: was below VWAP, just closed above it.
        if ef > es and c_2 <= vwap_2 and c_prev > vwap_p:
            raw = min(float(prev_row["low"]), float(bar_2["low"])) - TICK
            stop = _cap_long_stop(price, raw)
            if price - stop <= 0:
                return None
            return EntrySignal("long", stop, self.name)
        if ef < es and c_2 >= vwap_2 and c_prev < vwap_p:
            raw = max(float(prev_row["high"]), float(bar_2["high"])) + TICK
            stop = _cap_short_stop(price, raw)
            if stop - price <= 0:
                return None
            return EntrySignal("short", stop, self.name)
        return None


# ── The router: trade only the (regime -> strategy) cells the edge grid proved ──
class RegimeRouter(Strategy):
    """Delegates each bar to the strategy assigned to the CURRENT regime (read from
    the just-closed bar's `regime_class`). Unassigned regimes -> stand aside."""

    name = "RegimeRouter"

    def __init__(self, rulebook: dict):
        # rulebook: {regime_label: Strategy instance}
        self.rulebook = rulebook
        self._pending = None

    def reset_session(self, session_date):
        for sub in self.rulebook.values():
            sub.reset_session(session_date)

    def observe(self, i, row, prev_row, bar_2, ctx):
        for sub in self.rulebook.values():
            sub.observe(i, row, prev_row, bar_2, ctx)

    def entry(self, i, row, prev_row, bar_2, ctx):
        regime = prev_row.get("regime_class") if hasattr(prev_row, "get") else prev_row["regime_class"]
        sub = self.rulebook.get(regime)
        if sub is None:
            return None
        sig = sub.entry(i, row, prev_row, bar_2, ctx)
        if sig is not None:
            self._pending = sub
            sig.entry_type = f"{regime}:{sig.entry_type}"
        return sig

    def confirm(self, signal: EntrySignal):
        if self._pending is not None:
            self._pending.confirm(signal)
            self._pending = None


# ── Registries ──────────────────────────────────────────────────────────────────
def build_candidates():
    """Full menu of individual strategies — what the edge grid evaluates per regime."""
    return [
        VWAPCross(),
        VWAPPullback(),
        ORBreakout(),
        FVGTrend(require_displacement=False),
        FVGTrend(require_displacement=True),
        MeanReversionVWAP(),
        KeltnerRSIFade(),
        TurtleSoup(),
        RangeEdgeFade(),
        SqueezeBreakout(),
        OpeningRangeFade(),
        LunchLullReversion(),
    ]


def build_strategy(name: str):
    """Fresh instance of one strategy by name (used to assemble a router rulebook)."""
    for s in build_candidates():
        if s.name == name:
            return s
    raise KeyError(f"unknown strategy: {name}")
