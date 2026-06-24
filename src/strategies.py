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


# ── Candidate registry: fresh instances per run (state is stateful) ─────────────
def build_candidates():
    return [
        FVGTrend(require_displacement=False),
        FVGTrend(require_displacement=True),
        VWAPPullback(),
        ORBreakout(),
    ]
