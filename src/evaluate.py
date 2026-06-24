"""
╔══════════════════════════════════════════════════════════════╗
║  STRATEGY LAB — honest candidate evaluation                  ║
║                                                              ║
║  Runs every candidate (src/strategies.py) through ONE shared ║
║  backtest engine, then picks a winner WITHOUT fooling us:    ║
║                                                              ║
║   1. Score all candidates on a DEVELOPMENT window (older 70%)║
║   2. Pick the single most robust one (not the highest return)║
║   3. Confirm it ONCE on a SEALED HOLDOUT (recent 30%) that   ║
║      was never used for selection — that is the honest number║
║                                                              ║
║  Shared & identical for all candidates: exits, 2R targets,   ║
║  position sizing, costs, Topstep daily-loss / flatten rules. ║
║  Fills are PESSIMISTIC: a bar touching both stop & target is ║
║  scored as a stop-out.                                       ║
║                                                              ║
║  Run:  MNQ_DATA_PATH=/path/to/data.csv python -m src.evaluate║
║                                                              ║
║  ⚠️  Meaningless on random/fake data — proves only that the  ║
║  machinery runs. Real verdict needs your real history.       ║
╚══════════════════════════════════════════════════════════════╝
"""
import sys
from collections import defaultdict
from dataclasses import dataclass
from types import SimpleNamespace

import pandas as pd

from src import bot
from src import strategies

# ── Shared, locked knobs (identical for every candidate) ───────────────────────
R_MULTIPLE = 2.0            # target = 2x the risk taken
MAX_TRADES_PER_DAY = 4
TICK = bot.MNQ_TICK_SIZE
PT = bot.MNQ_POINT_VALUE

# Robustness bar a candidate must clear on the development window to be eligible.
MIN_DEV_TRADES = 150
MIN_PROFIT_FACTOR = 1.3
MIN_PROFITABLE_YEAR_FRACTION = 0.5


@dataclass
class Trade:
    date: pd.Timestamp
    direction: str
    entry: float
    exit: float
    contracts: int
    pnl_usd: float
    won: bool
    exit_reason: str
    entry_type: str


def _position_size(net_profit: float, drawdown_pct: float) -> int:
    """Topstep scaling tier, trimmed in drawdown. Shared risk control, not edge."""
    base = bot.compute_scaling_tier(net_profit)   # 2 / 3 / 5
    if drawdown_pct <= -2.5:
        return 2
    if drawdown_pct <= -1.5:
        return max(2, base - 1)
    return base


def _settle(trades, ts, direction, entry, exit_px, contracts, entry_type, reason):
    pts = (exit_px - entry) if direction == "long" else (entry - exit_px)
    net = pts * PT * contracts - bot.round_turn_cost(contracts)
    trades.append(Trade(ts, direction, entry, exit_px, contracts, net, net > 0, reason, entry_type))
    return net


def backtest(df: pd.DataFrame, strategy, start=None, end=None):
    """Shared honest backtest. start inclusive, end exclusive (session-date objects)."""
    cash = bot.INIT_CASH
    peak_cash = cash
    daily_pnl = 0.0
    trades_today = 0
    current_day = None

    in_trade = False
    direction = entry_price = stop_px = target_px = 0.0
    contracts = 0
    entry_type = ""

    trades = []
    ctx = SimpleNamespace(ts_ct=None, session_date=None, tick=TICK)

    for i in range(2, len(df)):
        row, prev_row, bar_2 = df.iloc[i], df.iloc[i - 1], df.iloc[i - 2]
        ts = df.index[i]
        ts_ct = ts.astimezone(bot.TIMEZONE)
        session_date = ts_ct.date()

        if start is not None and session_date < start:
            continue
        if end is not None and session_date >= end:
            break

        ctx.ts_ct = ts_ct
        ctx.session_date = session_date

        if current_day != session_date:
            peak_cash = max(peak_cash, cash)
            current_day = session_date
            daily_pnl = 0.0
            trades_today = 0
            strategy.reset_session(session_date)

        strategy.observe(i, row, prev_row, bar_2, ctx)

        # Hard flatten outside the Topstep session window.
        if in_trade and not bot._is_in_session(ts_ct):
            cash += _settle(trades, ts, direction, entry_price, float(row["open"]),
                            contracts, entry_type, "flatten")
            in_trade = False
            peak_cash = max(peak_cash, cash)
            continue

        # Manage an open trade — HONEST: stop is checked before target.
        if in_trade:
            if direction == "long":
                hit_stop = row["low"] <= stop_px
                hit_target = row["high"] >= target_px
            else:
                hit_stop = row["high"] >= stop_px
                hit_target = row["low"] <= target_px

            exit_px = reason = None
            if hit_stop:
                exit_px, reason = stop_px, "stop"
            elif hit_target:
                exit_px, reason = target_px, "target"
            elif (direction == "long" and not prev_row["long_signal"]) or \
                 (direction == "short" and not prev_row["short_signal"]):
                exit_px, reason = float(row["open"]), "trend_flip"

            if exit_px is not None:
                net = _settle(trades, ts, direction, entry_price, exit_px,
                              contracts, entry_type, reason)
                cash += net
                daily_pnl += net
                in_trade = False
                peak_cash = max(peak_cash, cash)
            continue

        # Look for a new entry.
        if not bot._is_entry_allowed(ts_ct):
            continue
        if daily_pnl <= bot.BOT_DAILY_LOSS_LIMIT:
            continue
        if trades_today >= MAX_TRADES_PER_DAY:
            continue

        sig = strategy.entry(i, row, prev_row, bar_2, ctx)
        if sig is None:
            continue

        price = float(row["open"])
        if sig.direction == "long":
            risk = price - sig.stop_price
            if risk <= 0:
                continue
            target = price + R_MULTIPLE * risk
        else:
            risk = sig.stop_price - price
            if risk <= 0:
                continue
            target = price - R_MULTIPLE * risk

        drawdown_pct = (cash - peak_cash) / peak_cash * 100
        size = _position_size(cash - bot.INIT_CASH, drawdown_pct)

        strategy.confirm(sig)
        in_trade = True
        direction, entry_price, stop_px, target_px = sig.direction, price, sig.stop_price, target
        contracts, entry_type = size, sig.entry_type
        trades_today += 1

    return trades, cash


# ── Scoring ────────────────────────────────────────────────────────────────────
def _profit_factor(trades):
    gains = sum(t.pnl_usd for t in trades if t.pnl_usd > 0)
    losses = -sum(t.pnl_usd for t in trades if t.pnl_usd < 0)
    if losses <= 0:
        return float("inf") if gains > 0 else 0.0
    return gains / losses


def _score(trades):
    n = len(trades)
    wins = sum(1 for t in trades if t.won)
    net = sum(t.pnl_usd for t in trades)
    by_year = defaultdict(float)
    for t in trades:
        by_year[pd.Timestamp(t.date).year] += t.pnl_usd
    years = sorted(by_year)
    prof_years = sum(1 for y in years if by_year[y] > 0)
    pf = _profit_factor(trades)
    frac = (prof_years / len(years)) if years else 0.0
    passed = (net > 0 and n >= MIN_DEV_TRADES and pf >= MIN_PROFIT_FACTOR
              and frac >= MIN_PROFITABLE_YEAR_FRACTION)
    return {
        "n": n, "win_rate": (wins / n * 100) if n else 0.0, "net": net, "pf": pf,
        "prof_years": prof_years, "total_years": len(years), "by_year": dict(by_year),
        "passed": passed,
    }


def _pf_str(pf):
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def _split_date(df):
    cut_ts = df.index[int(len(df) * 0.70)]
    return cut_ts.astimezone(bot.TIMEZONE).date()


def main() -> None:
    df = bot.fetch_data()
    bot.validate_loaded_data(df)
    df = bot.add_indicators(df)
    df = bot.generate_signals(df)

    split = _split_date(df)
    print("\n" + "=" * 70)
    print("  STRATEGY LAB — sealed-holdout candidate evaluation")
    print("=" * 70)
    print(f"  Development window:  start .. {split} (exclusive)")
    print(f"  Sealed holdout:      {split} .. end   (never used for selection)")
    print(f"  Eligibility bar:  >= {MIN_DEV_TRADES} dev trades, PF >= {MIN_PROFIT_FACTOR}, "
          f">= {int(MIN_PROFITABLE_YEAR_FRACTION*100)}% years green")

    # ── Phase 1: score every candidate on DEVELOPMENT only ─────────────────────
    print("\n  ── DEVELOPMENT SCORECARD ──")
    print(f"  {'Candidate':<18}{'Trades':>8}{'Win%':>7}{'Net $':>12}{'PF':>7}{'Yrs+':>7}  Eligible")
    results = []
    for idx, cand in enumerate(strategies.build_candidates()):
        trades, _ = backtest(df, cand, end=split)
        s = _score(trades)
        results.append((cand.name, idx, s))
        yrs = f"{s['prof_years']}/{s['total_years']}"
        print(f"  {cand.name:<18}{s['n']:>8}{s['win_rate']:>6.0f}%{s['net']:>12,.0f}"
              f"{_pf_str(s['pf']):>7}{yrs:>7}  {'YES' if s['passed'] else 'no'}")

    eligible = [r for r in results if r[2]["passed"]]

    # ── Phase 2: pick the single most robust winner ────────────────────────────
    print("\n" + "=" * 70)
    print("  VERDICT")
    print("=" * 70)
    if not eligible:
        print("  No candidate cleared the eligibility bar on development data.")
        print("  Honest outcome: NO durable edge found — do not trade. Iterate on ideas.")
        print("=" * 70)
        return

    # Rank eligible by profit factor, then net. (Robustness over raw return.)
    eligible.sort(key=lambda r: (r[2]["pf"], r[2]["net"]), reverse=True)
    win_name, win_idx, win_dev = eligible[0]
    print(f"  Selected winner (on development data only): {win_name}")
    print(f"    dev: {win_dev['n']} trades, {win_dev['win_rate']:.0f}% win, "
          f"${win_dev['net']:,.0f} net, PF {_pf_str(win_dev['pf'])}, "
          f"{win_dev['prof_years']}/{win_dev['total_years']} years green")

    # ── Phase 3: confirm the winner ONCE on the sealed holdout ─────────────────
    winner = strategies.build_candidates()[win_idx]
    hold_trades, _ = backtest(df, winner, start=split)
    h = _score(hold_trades)
    print(f"\n  SEALED-HOLDOUT result for {win_name} (the number that matters):")
    print(f"    {h['n']} trades, {h['win_rate']:.0f}% win, ${h['net']:,.0f} net, "
          f"PF {_pf_str(h['pf'])}, {h['prof_years']}/{h['total_years']} years green")

    holds_up = h["net"] > 0 and h["pf"] >= 1.2 and h["n"] >= 30
    print()
    if holds_up:
        print(f"  ✅ {win_name} held up out-of-sample. WORTH forward-testing in Topstep")
        print("     practice mode before any real money. Not a guarantee — a green light to test.")
    else:
        print(f"  ❌ {win_name} looked good in development but FELL APART on the sealed holdout.")
        print("     That is overfitting caught in the act. Do not trade it. Iterate.")
    print("=" * 70)
    print("  Reminder: a passed test means 'worth forward-testing', NOT 'guaranteed money'.")
    print("=" * 70)


if __name__ == "__main__":
    sys.exit(main())
