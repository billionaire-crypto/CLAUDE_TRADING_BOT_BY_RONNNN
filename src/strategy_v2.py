"""
╔══════════════════════════════════════════════════════════════╗
║  STRATEGY V2 — LEAN FVG TREND-CONTINUATION                    ║
║                                                              ║
║  A deliberate REBUILD of the overfit V29 bot. Design rules:  ║
║    1. Few parameters, each justifiable from first principles ║
║    2. No past-erasing filters (no skip-July, no "losing      ║
║       zone" deletions, no hour/month-specific rules)         ║
║    3. Longs and shorts treated identically                   ║
║    4. Trades every month and session                         ║
║    5. HONEST fills — ambiguous bars assume a STOP-OUT         ║
║                                                              ║
║  Reuses the good infrastructure from bot.py (data,           ║
║  indicators, FVG detection, costs, Topstep risk limits).     ║
║                                                              ║
║  Run it like the backtest:                                   ║
║    MNQ_DATA_PATH=/path/to/data.csv python -m src.strategy_v2 ║
║                                                              ║
║  ⚠️  UNTESTED EDGE: a clean, non-overfit strategy is NOT the  ║
║  same as a profitable one. This must be validated on real    ║
║  data (and forward-tested) before risking a cent.            ║
╚══════════════════════════════════════════════════════════════╝
"""
import sys
from collections import defaultdict
from dataclasses import dataclass

import pandas as pd

from src import bot

# ── THE ONLY KNOBS (5 of them, all defensible) ─────────────────────────────────
R_MULTIPLE        = 2.0   # target = this many times the risk taken. Standard, not fit.
STOP_CAP_TICKS    = 40    # 10 NQ points — a hard ceiling on risk per trade.
MAX_TRADES_PER_DAY = 4    # overtrading control, not an edge tweak.
# FVG size/age come from bot's mechanical defaults (FVG_MIN_SIZE_TICKS, FVG_MAX_AGE_BARS).
# Trend filter (EMA + VWAP) comes from bot.generate_signals — robust and standard.

TICK = bot.MNQ_TICK_SIZE
PT   = bot.MNQ_POINT_VALUE


@dataclass
class V2Trade:
    date: pd.Timestamp
    direction: str
    entry: float
    exit: float
    contracts: int
    pnl_usd: float
    won: bool
    exit_reason: str


def _position_size(net_profit: float, drawdown_pct: float) -> int:
    """Topstep scaling tier, reduced when in drawdown. Pure risk control."""
    base = bot.compute_scaling_tier(net_profit)   # 2 / 3 / 5
    if drawdown_pct <= -2.5:
        return 2
    if drawdown_pct <= -1.5:
        return max(2, base - 1)
    return base


def run(df: pd.DataFrame):
    df = bot.add_indicators(df)
    df = bot.generate_signals(df)   # gives long_bias / short_bias (EMA + VWAP agree)

    cash = bot.INIT_CASH
    peak_cash = cash
    daily_pnl = 0.0
    trades_today = 0
    current_day = None

    in_trade = False
    direction = entry_price = stop_px = target_px = 0.0
    contracts = 0

    active_fvgs = []
    trades = []

    fvg_min = bot.FVG_MIN_SIZE_TICKS * TICK

    for i in range(2, len(df)):
        row, prev_row, bar_2 = df.iloc[i], df.iloc[i - 1], df.iloc[i - 2]
        ts = df.index[i]
        ts_ct = ts.astimezone(bot.TIMEZONE)
        session_date = ts_ct.date()

        # ── New session bookkeeping ────────────────────────────────────────────
        if current_day != session_date:
            if current_day is not None and cash > peak_cash:
                peak_cash = cash
            current_day = session_date
            daily_pnl = 0.0
            trades_today = 0
            active_fvgs = [f for f in active_fvgs if f.session_date == session_date]

        # ── Detect fresh FVGs (same mechanic as the original, kept) ────────────
        if bar_2["high"] < row["low"] and (row["low"] - bar_2["high"]) >= fvg_min:
            active_fvgs.append(bot.FVG("bullish", row["low"], bar_2["high"], i, session_date))
        if bar_2["low"] > row["high"] and (bar_2["low"] - row["high"]) >= fvg_min:
            active_fvgs.append(bot.FVG("bearish", bar_2["low"], row["high"], i, session_date))
        active_fvgs = [f for f in active_fvgs
                       if bot.is_fvg_valid(f, i, session_date, row["high"], row["low"])]

        # ── Hard flatten outside Topstep session window ────────────────────────
        if in_trade and not bot._is_in_session(ts_ct):
            cash, in_trade, daily_pnl = _settle(
                trades, ts, direction, entry_price, row["open"], contracts,
                cash, daily_pnl, "flatten")
            peak_cash = max(peak_cash, cash)
            continue

        # ── Manage an open trade (HONEST: stop checked before target) ──────────
        if in_trade:
            if direction == "long":
                hit_stop = row["low"] <= stop_px
                hit_target = row["high"] >= target_px
            else:
                hit_stop = row["high"] >= stop_px
                hit_target = row["low"] <= target_px

            exit_px = exit_reason = None
            if hit_stop:                      # pessimistic tie-break: stop wins
                exit_px, exit_reason = stop_px, "stop"
            elif hit_target:
                exit_px, exit_reason = target_px, "target"
            elif (direction == "long" and not prev_row["long_signal"]) or \
                 (direction == "short" and not prev_row["short_signal"]):
                exit_px, exit_reason = row["open"], "trend_flip"

            if exit_px is not None:
                cash, in_trade, daily_pnl = _settle(
                    trades, ts, direction, entry_price, exit_px, contracts,
                    cash, daily_pnl, exit_reason)
                peak_cash = max(peak_cash, cash)
            continue

        # ── Look for a new entry ───────────────────────────────────────────────
        if not bot._is_entry_allowed(ts_ct):
            continue
        if daily_pnl <= bot.BOT_DAILY_LOSS_LIMIT:   # done for the day
            continue
        if trades_today >= MAX_TRADES_PER_DAY:
            continue

        price = row["open"]
        chosen = None
        if prev_row["long_bias"]:
            for f in active_fvgs:
                if f.direction == "bullish" and bot.price_in_fvg(f, price):
                    chosen = ("long", f); break
        if chosen is None and prev_row["short_bias"]:
            for f in active_fvgs:
                if f.direction == "bearish" and bot.price_in_fvg(f, price):
                    chosen = ("short", f); break
        if chosen is None:
            continue

        side, fvg = chosen
        # Stop beyond the gap, capped; target = R_MULTIPLE x risk.
        if side == "long":
            raw_stop = fvg.bottom - TICK
            stop = max(raw_stop, price - STOP_CAP_TICKS * TICK)
            risk = price - stop
            if risk <= 0:
                continue
            target = price + R_MULTIPLE * risk
        else:
            raw_stop = fvg.top + TICK
            stop = min(raw_stop, price + STOP_CAP_TICKS * TICK)
            risk = stop - price
            if risk <= 0:
                continue
            target = price - R_MULTIPLE * risk

        drawdown_pct = (cash - peak_cash) / peak_cash * 100
        size = _position_size(cash - bot.INIT_CASH, drawdown_pct)

        in_trade = True
        direction, entry_price, stop_px, target_px, contracts = side, price, stop, target, size
        trades_today += 1
        active_fvgs = [f for f in active_fvgs if f is not fvg]

    return trades, cash


def _settle(trades, ts, direction, entry, exit_px, contracts, cash, daily_pnl, reason):
    pts = (exit_px - entry) if direction == "long" else (entry - exit_px)
    gross = pts * PT * contracts
    net = gross - bot.round_turn_cost(contracts)
    cash += net
    daily_pnl += net
    trades.append(V2Trade(ts, direction, entry, exit_px, contracts, net, net > 0, reason))
    return cash, False, daily_pnl


# ── Honest reporting (with year-by-year, like the diagnostic) ──────────────────
def _report(trades, final_cash):
    print("\n" + "=" * 64)
    print("  STRATEGY V2 — LEAN FVG TREND-CONTINUATION — RESULTS")
    print("=" * 64)
    if not trades:
        print("  No trades generated.")
        return
    n = len(trades)
    wins = sum(1 for t in trades if t.won)
    net = sum(t.pnl_usd for t in trades)
    gains = sum(t.pnl_usd for t in trades if t.pnl_usd > 0)
    losses = -sum(t.pnl_usd for t in trades if t.pnl_usd < 0)
    pf = (gains / losses) if losses > 0 else float("inf")
    months = len({(t.date.year, t.date.month) for t in trades}) or 1

    print(f"  Final account : ${final_cash:,.0f}   (start ${bot.INIT_CASH:,.0f})")
    print(f"  Net P&L       : ${net:,.0f}")
    print(f"  Trades        : {n}   ({n/months:.1f} per active month)")
    print(f"  Win rate      : {wins/n*100:.1f}%")
    print(f"  Profit factor : {pf:.2f}")
    print(f"  Avg / month   : ${net/months:,.0f}")

    by_year = defaultdict(list)
    for t in trades:
        by_year[t.date.year].append(t)
    print("\n  ── Year by year (same settings every year) ──")
    print(f"  {'Year':<6}{'Trades':>8}{'Win%':>8}{'Net $':>12}")
    for y in sorted(by_year):
        b = by_year[y]
        w = sum(1 for t in b if t.won)
        ny = sum(t.pnl_usd for t in b)
        print(f"  {y:<6}{len(b):>8}{w/len(b)*100:>7.0f}%{ny:>12,.0f}")

    print("\n  ⚠️  A clean strategy is not automatically a profitable one.")
    print("     Validate on YOUR data + forward-test before risking money.")
    print("=" * 64)


def main() -> None:
    df = bot.fetch_data()
    bot.validate_loaded_data(df)
    trades, final_cash = run(df)
    _report(trades, final_cash)


if __name__ == "__main__":
    sys.exit(main())
