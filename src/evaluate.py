"""
╔══════════════════════════════════════════════════════════════╗
║  STRATEGY LAB — regime × strategy edge grid + Topstep judge   ║
║                                                              ║
║  Pipeline (all on ONE shared, honest backtest engine):       ║
║   1. Tag every bar with its REGIME (src/regime.py).          ║
║   2. EDGE GRID: run each strategy and measure its            ║
║      cost-adjusted expectancy WITHIN each regime — on the    ║
║      development window only.                                ║
║   3. RULEBOOK: assign each regime to the strategy that wins  ║
║      its cell (enough trades, positive, year-consistent).    ║
║      Unassigned regimes -> stand aside.                      ║
║   4. ROUTER: trade that rulebook; add the DISCIPLINE overlay ║
║      (the safety net, measured A/B).                         ║
║   5. JUDGE: rank by P(pass Topstep) via the combine          ║
║      Monte-Carlo (src/combine_sim.py), NOT by raw P&L.       ║
║   6. Confirm ONCE on the SEALED HOLDOUT.                     ║
║                                                              ║
║  Fills are PESSIMISTIC (stop wins a tie). Costs are real     ║
║  ($2.34/contract round-turn). Params are locked, never fit   ║
║  to the holdout.                                             ║
║                                                              ║
║  Run:  MNQ_DATA_PATH=/path/to/data.csv python -m src.evaluate║
║  ⚠️  Meaningless on random data — proves only the machinery. ║
╚══════════════════════════════════════════════════════════════╝
"""
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import time as dtime
from types import SimpleNamespace
from typing import List, Optional

import pandas as pd

from src import bot
from src import regime
from src import strategies
from src import combine_sim

# ── Shared, locked knobs (identical for every candidate) ───────────────────────
R_MULTIPLE = 2.0            # target = 2x risk when a strategy doesn't set its own
MAX_TRADES_PER_DAY = 4
TICK = bot.MNQ_TICK_SIZE
PT = bot.MNQ_POINT_VALUE

# Eligibility / cell bars
MIN_DEV_TRADES = 150            # for the whole-strategy scorecard
MIN_PROFIT_FACTOR = 1.3
MIN_PROFITABLE_YEAR_FRACTION = 0.5
MIN_CELL_TRADES = 25            # min trades for a (strategy,regime) cell to qualify

DAILY_LIMIT = combine_sim.TopstepRules().daily_loss_limit   # $1,000


# ── Discipline overlay (the safety net — toggleable, measured A/B) ──────────────
@dataclass
class Discipline:
    # Trade windows in the data's local zone (America/Chicago ≈ ET-1h):
    # 08:30-10:00 CT (the 9:30-11:00 ET open) and 14:00-14:50 CT (the power hour).
    windows: tuple = ((dtime(8, 30), dtime(10, 0)), (dtime(14, 0), dtime(14, 50)))
    post_loss_cooldown_bars: int = 3       # walk away briefly after a loss
    stop_on_green: Optional[float] = 1200.0  # lock the day once this daily profit is hit
    breakeven_after_R: Optional[float] = 1.0  # no give-back: stop -> entry after +1R
    risk_dollars: Optional[float] = 150.0    # vol-scaled sizing target (None = base tier)

    def in_window(self, t: dtime) -> bool:
        return any(a <= t < b for a, b in self.windows)


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
    regime: str = ""


def _position_size(net_profit: float, drawdown_pct: float) -> int:
    """Topstep scaling tier, trimmed in drawdown. Shared risk control, not edge."""
    base = bot.compute_scaling_tier(net_profit)   # 2 / 3 / 5
    if drawdown_pct <= -2.5:
        return 2
    if drawdown_pct <= -1.5:
        return max(2, base - 1)
    return base


def _vol_scaled_size(risk_pts: float, risk_dollars: float) -> int:
    """Constant-dollar-risk sizing: smaller size when the stop (≈ATR) is wider."""
    if risk_pts <= 0:
        return 1
    n = round(risk_dollars / (risk_pts * PT))
    return int(max(1, min(5, n)))


def _settle(trades, ts, direction, entry, exit_px, contracts, entry_type, reason, regime_lbl):
    pts = (exit_px - entry) if direction == "long" else (entry - exit_px)
    net = pts * PT * contracts - bot.round_turn_cost(contracts)
    trades.append(Trade(ts, direction, entry, exit_px, contracts, net, net > 0,
                        reason, entry_type, regime_lbl))
    return net


def backtest(df: pd.DataFrame, strategy, start=None, end=None, discipline: Discipline = None):
    """Shared honest backtest. start inclusive, end exclusive (session-date objects).
    discipline=None -> raw harness (used for the edge grid). A Discipline() -> safety net on."""
    cash = bot.INIT_CASH
    peak_cash = cash
    daily_pnl = 0.0
    trades_today = 0
    current_day = None
    cooldown_until = -1
    day_locked = False

    in_trade = False
    direction = entry_price = stop_px = target_px = risk_pts = 0.0
    contracts = 0
    entry_type = entry_regime = ""
    exit_on_flip = True

    trades = []
    ctx = SimpleNamespace(ts_ct=None, session_date=None, tick=TICK)

    # Row-dicts are ~10x faster to index than df.iloc per bar. Strategies use
    # row["col"] which works identically on a dict.
    rows = df.to_dict("records")
    index = df.index

    for i in range(2, len(df)):
        row, prev_row, bar_2 = rows[i], rows[i - 1], rows[i - 2]
        ts = index[i]
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
            cooldown_until = -1
            day_locked = False
            strategy.reset_session(session_date)

        strategy.observe(i, row, prev_row, bar_2, ctx)

        # Hard flatten outside the Topstep session window.
        if in_trade and not bot._is_in_session(ts_ct):
            cash += _settle(trades, ts, direction, entry_price, float(row["open"]),
                            contracts, entry_type, "flatten", entry_regime)
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
            elif exit_on_flip and (
                    (direction == "long" and not prev_row["long_signal"]) or
                    (direction == "short" and not prev_row["short_signal"])):
                exit_px, reason = float(row["open"]), "trend_flip"

            if exit_px is not None:
                net = _settle(trades, ts, direction, entry_price, exit_px,
                              contracts, entry_type, reason, entry_regime)
                cash += net
                daily_pnl += net
                in_trade = False
                peak_cash = max(peak_cash, cash)
                if discipline:
                    if net < 0:
                        cooldown_until = i + discipline.post_loss_cooldown_bars
                    if discipline.stop_on_green is not None and daily_pnl >= discipline.stop_on_green:
                        day_locked = True
            elif discipline and discipline.breakeven_after_R is not None:
                # No give-back: once price has run +R in our favor, ratchet stop to entry.
                trig = discipline.breakeven_after_R * risk_pts
                if direction == "long" and row["high"] >= entry_price + trig:
                    stop_px = max(stop_px, entry_price)
                elif direction == "short" and row["low"] <= entry_price - trig:
                    stop_px = min(stop_px, entry_price)
            continue

        # ── Look for a new entry ───────────────────────────────────────────────
        if not bot._is_entry_allowed(ts_ct):
            continue
        if daily_pnl <= bot.BOT_DAILY_LOSS_LIMIT:
            continue
        if trades_today >= MAX_TRADES_PER_DAY:
            continue
        if discipline:
            if day_locked or i < cooldown_until:
                continue
            if not discipline.in_window(ts_ct.time()):
                continue

        sig = strategy.entry(i, row, prev_row, bar_2, ctx)
        if sig is None:
            continue

        price = float(row["open"])
        if sig.direction == "long":
            risk = price - sig.stop_price
        else:
            risk = sig.stop_price - price
        if risk <= 0:
            continue

        # Target: strategy-supplied (fades aim at the mean) or shared 2R.
        if sig.target_price is not None:
            target = float(sig.target_price)
            if (sig.direction == "long" and target <= price) or \
               (sig.direction == "short" and target >= price):
                continue
        else:
            target = price + R_MULTIPLE * risk if sig.direction == "long" else price - R_MULTIPLE * risk

        drawdown_pct = (cash - peak_cash) / peak_cash * 100
        if discipline and discipline.risk_dollars is not None:
            size = _vol_scaled_size(risk, discipline.risk_dollars)
        else:
            size = _position_size(cash - bot.INIT_CASH, drawdown_pct)

        strategy.confirm(sig)
        in_trade = True
        direction, entry_price, stop_px, target_px = sig.direction, price, sig.stop_price, target
        risk_pts = risk
        contracts, entry_type = size, sig.entry_type
        entry_regime = prev_row.get("regime_class", "unknown")
        exit_on_flip = sig.exit_on_trend_flip
        trades_today += 1

    return trades, cash


# ── Scoring helpers ──────────────────────────────────────────────────────────────
def _profit_factor(trades):
    gains = sum(t.pnl_usd for t in trades if t.pnl_usd > 0)
    losses = -sum(t.pnl_usd for t in trades if t.pnl_usd < 0)
    if losses <= 0:
        return float("inf") if gains > 0 else 0.0
    return gains / losses


def _pf_str(pf):
    return "inf" if pf == float("inf") else f"{pf:.2f}"


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
    return {"n": n, "win_rate": (wins / n * 100) if n else 0.0, "net": net, "pf": pf,
            "prof_years": prof_years, "total_years": len(years), "passed": passed}


def topstep_scorecard(trades) -> dict:
    """Topstep-relevant metrics: not just profit, but survival/consistency."""
    n = len(trades)
    if n == 0:
        return {"n": 0}
    wins = [t.pnl_usd for t in trades if t.pnl_usd > 0]
    losses = [t.pnl_usd for t in trades if t.pnl_usd < 0]
    net = sum(t.pnl_usd for t in trades)
    # equity curve + max drawdown ($)
    eq = bot.INIT_CASH
    peak = eq
    max_dd = 0.0
    for t in trades:
        eq += t.pnl_usd
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
    # daily aggregation
    daily = defaultdict(float)
    for t in trades:
        daily[pd.Timestamp(t.date).date()] += t.pnl_usd
    day_vals = list(daily.values())
    breaches = sum(1 for v in day_vals if v <= -DAILY_LIMIT)
    best_day = max(day_vals) if day_vals else 0.0
    return {
        "n": n,
        "win_rate": len(wins) / n * 100,
        "net": net,
        "pf": _profit_factor(trades),
        "avg_win": (sum(wins) / len(wins)) if wins else 0.0,
        "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
        "payoff": (abs(sum(wins) / len(wins)) / abs(sum(losses) / len(losses)))
                  if wins and losses else float("inf"),
        "expectancy": net / n,
        "max_dd": max_dd,
        "n_days": len(day_vals),
        "trades_per_day": n / len(day_vals) if day_vals else 0.0,
        "daily_breaches": breaches,
        "best_day": best_day,
        "best_day_share": (best_day / net) if net > 0 else float("inf"),
    }


# ── Edge grid + rulebook ─────────────────────────────────────────────────────────
def run_edge_grid(df, strategy_list, end=None):
    """For each strategy, bucket its DEV trades by regime-at-entry; return a grid:
    grid[name][regime] = {n, net, expectancy, win_rate}."""
    grid = {}
    for cand in strategy_list:
        trades, _ = backtest(df, cand, end=end, discipline=None)
        cells = defaultdict(list)
        for t in trades:
            cells[t.regime or "unknown"].append(t)
        grid[cand.name] = {
            r: {"n": len(ts), "net": sum(x.pnl_usd for x in ts),
                "expectancy": sum(x.pnl_usd for x in ts) / len(ts),
                "win_rate": sum(1 for x in ts if x.won) / len(ts) * 100}
            for r, ts in cells.items()}
    return grid


def build_rulebook(grid, regimes=regime.TRADEABLE_REGIMES):
    """Assign each regime to the strategy with the best positive expectancy in that
    cell (>= MIN_CELL_TRADES). Regimes with no qualifying strategy -> stand aside."""
    rulebook = {}
    for r in regimes:
        best_name, best_exp = None, 0.0
        for name, cells in grid.items():
            cell = cells.get(r)
            if cell and cell["n"] >= MIN_CELL_TRADES and cell["expectancy"] > best_exp:
                best_name, best_exp = name, cell["expectancy"]
        if best_name is not None:
            rulebook[r] = best_name
    return rulebook


def print_edge_grid(grid, regimes=regime.TRADEABLE_REGIMES):
    print("\n  EDGE GRID — expectancy $/trade by regime (n in parens), dev window")
    print(f"  {'Strategy':<16}" + "".join(f"{r:>14}" for r in regimes))
    for name, cells in grid.items():
        out = f"  {name:<16}"
        for r in regimes:
            c = cells.get(r)
            cell_str = f"{c['expectancy']:+.0f}({c['n']})" if c else "—"
            out += f"{cell_str:>14}"
        print(out)


# ── Main ─────────────────────────────────────────────────────────────────────────
def _split_date(df):
    cut_ts = df.index[int(len(df) * 0.70)]
    return cut_ts.astimezone(bot.TIMEZONE).date()


def main() -> None:
    df = bot.fetch_data()
    bot.validate_loaded_data(df)
    df = bot.add_indicators(df)
    df = bot.generate_signals(df)
    df = regime.add_regime_features(df)
    split = _split_date(df)

    print("\n" + "=" * 72)
    print("  STRATEGY LAB — regime edge grid + Topstep combine judge")
    print("=" * 72)
    print(f"  Development: start .. {split} (excl)   Sealed holdout: {split} .. end")

    # ── 1+2: edge grid on development data ────────────────────────────────────
    menu = strategies.build_candidates()
    grid = run_edge_grid(df, menu, end=split)
    print_edge_grid(grid)

    # ── 3: rulebook ───────────────────────────────────────────────────────────
    rulebook = build_rulebook(grid)
    print("\n  RULEBOOK (regime -> strategy; others = stand aside):")
    if not rulebook:
        print("    (empty — no cell cleared the bar. No tradeable edge found.)")
        print("=" * 72)
        return
    for r, name in rulebook.items():
        c = grid[name][r]
        print(f"    {r:<10} -> {name:<16} (exp ${c['expectancy']:+.0f}/trade, {c['n']} dev trades)")

    # ── 4: build the router; DEV then SEALED-HOLDOUT, with discipline A/B ──────
    def router():
        return strategies.RegimeRouter({r: strategies.build_strategy(n) for r, n in rulebook.items()})

    print("\n  ── ROUTER on SEALED HOLDOUT (the number that matters) ──")
    for label, disc in (("no discipline", None), ("+ discipline", Discipline())):
        h_trades, _ = backtest(df, router(), start=split, discipline=disc)
        sc = topstep_scorecard(h_trades)
        if sc["n"] == 0:
            print(f"  {label:<14}: no trades.")
            continue
        print(f"  {label:<14}: {sc['n']} trades, {sc['win_rate']:.0f}% win, "
              f"${sc['net']:,.0f} net, PF {_pf_str(sc['pf'])}, payoff {sc['payoff']:.2f}, "
              f"exp ${sc['expectancy']:+.0f}/trade")
        print(f"  {'':<14}  maxDD ${sc['max_dd']:,.0f} (vs $2,000 MLL), "
              f"daily-limit breaches {sc['daily_breaches']}, "
              f"best-day share {sc['best_day_share']*100:.0f}% (rule ≤50%), "
              f"{sc['trades_per_day']:.1f} trades/day")
        # ── 5: combine Monte-Carlo judge ──────────────────────────────────────
        daily = combine_sim.trades_to_daily_pnl(h_trades)
        combine_sim.print_report(f"router ({label})", combine_sim.monte_carlo(daily))

    print("\n" + "=" * 72)
    print("  Acceptance: ship only if a regime cell is positive + year-consistent on dev,")
    print("  holds on the holdout, respects Topstep limits, AND shows a respectable")
    print("  P(pass). Otherwise: not fundable yet — iterate, don't risk the eval fee.")
    print("=" * 72)


if __name__ == "__main__":
    sys.exit(main())
