# Scale-Out + Trailing Runner Exit — Design

**Date:** 2026-06-24
**Status:** Approved (pending spec review)

## Problem

On clean front-month 5-min RTH MNQ data (2019-2026), the only strategy with a
real edge is FVG-trend. Its all-or-nothing 2R exit produces a low-win-rate
(~29-34%), high-payoff, **lumpy** equity curve. The Topstep combine judge
rewards steady accumulation to +$3,000 (target + consistency + time limits), so
lumpiness — not lack of profit — is why the best router config passes only
~22.5% of combines with negative expected value.

## Goal

Add an **opt-in** scale-out + trailing-runner exit policy to the shared backtest
engine that banks a partial profit early (steadier, higher win rate) while
keeping a trailing runner for the home-run moves that are the trend edge. Judge
whether it raises **P(pass)** on the sealed holdout versus the all-or-nothing
baseline. Build on the one proven edge rather than inventing new entries.

## Non-Goals

- No live trading.
- No tuning of scale-out parameters against the sealed holdout.
- No change to entry signals, the regime classifier, sizing, or the combine judge.
- No change to default behavior: with no plan passed, `backtest` behaves exactly
  as today (all earlier results stay valid and comparable).

## Design Constraints (existing harness)

- `evaluate.backtest(df, strategy, start, end, discipline)` manages one open
  position at a time. Stop is checked before target (pessimistic). Exits are
  owned by the harness, not strategies.
- A `Trade` record carries `pnl_usd`, `won`, `exit_reason`, etc. The combine
  judge consumes per-day P&L (`combine_sim.trades_to_daily_pnl`).
- Trend signals use the shared 2R target and `exit_on_trend_flip=True`, with no
  `target_price`. Fades set `target_price` (the mean) and `exit_on_trend_flip=False`.
- `bot.round_turn_cost(contracts)` is the per-round-turn cost for N contracts.

## The Scale-Out Plan

New dataclass in `src/evaluate.py`. All values are locked, documented conventions
— never fit to the holdout.

```
@dataclass
class ScaleOutPlan:
    scale_at_R: float = 1.0      # bank the partial when price reaches +1R
    scale_fraction: float = 0.5  # fraction of contracts banked at scale_at_R
    trail_atr_mult: float = 2.0  # chandelier trail distance for the runner
```

### Applicability
The plan applies **only to trend trades** — signals with `target_price is None`
(i.e. the shared 2R target style). Trades that carry a `target_price` (the
mean-reversion fades) keep the **existing fixed-target** exit unchanged, because
trailing a runner does not fit a "snap back to the mean" trade.

### Lifecycle of one trend trade when a plan is active (long; short mirrors)
Let `entry`, `stop0`, `risk = entry - stop0`, `size` contracts.
`scale_qty = int(size * scale_fraction)` (floor), `runner_qty = size - scale_qty`.
`scale_price = entry + scale_at_R * risk`. `atr_entry` = ATR at entry bar.

Per bar, **stop checked before profit targets** (pessimistic), in this order:

1. **Not yet scaled:**
   - If `low <= stop0` → whole position stops out at `stop0`. One Trade,
     `reason="stop"`, contracts `size`.
   - Else if `high >= scale_price`:
     - Bank `scale_qty` at `scale_price` (if `scale_qty > 0`) → records a
       partial settle (see Accounting).
     - Switch to **runner mode**: `runner_stop = entry` (break-even),
       `peak = high`.
     - (If `scale_qty == 0` because `size == 1`, nothing is banked; the single
       contract still switches to runner mode at break-even.)
2. **Scaled (runner mode):**
   - `peak = max(peak, high)`; `trail = peak - trail_atr_mult * atr_entry`;
     `runner_stop = max(runner_stop, trail)` (ratchets up only).
   - If `low <= runner_stop` → runner exits at `runner_stop`,
     `reason="trail"`, contracts `runner_qty`.
   - Else if `exit_on_trend_flip` and the trend flips (same condition the harness
     already uses) → runner exits at next open, `reason="trend_flip"`.
3. **End-of-session flatten** (existing rule): any open contracts (full or
   runner) exit at the open, `reason="flatten"`.

### Accounting
To keep win-rate and daily-P&L semantics intact, **one entry = one `Trade`
record** whose `pnl_usd` is the **sum** of the partial and runner legs, minus
costs for **both** legs:
- partial leg pnl = `scale_at_R * risk * PT * scale_qty - round_turn_cost(scale_qty)`
- runner leg pnl  = `(exit_px - entry) * PT * runner_qty - round_turn_cost(runner_qty)`
- `Trade.pnl_usd = partial_pnl + runner_pnl`; `won = pnl_usd > 0`;
  `exit_reason` = the runner's reason (or `"stop"` if stopped before scaling).
- Total contracts charged = `size` → identical total cost to the baseline.

Edge cases: if `scale_qty == 0`, partial_pnl = 0 and only the runner leg applies.
If the same bar would both stop and scale, the **stop wins** (pessimistic).

## How It Is Judged

In `src/evaluate.py` `main()`, after the existing holdout section, add a
**scale-out comparison** on the SEALED HOLDOUT only:
- Run the router with `scale_plan=None` (baseline) and with
  `scale_plan=ScaleOutPlan()`.
- Print each one's topstep scorecard and `combine_sim` report.
- The edge grid, rulebook, and the existing discipline A/B are unchanged (no plan
  passed there).

Run `python -m src.evaluate` **once**.

## Acceptance

Keep the scale-out exit only if, on the holdout, it **clearly raises P(pass)**
versus baseline while staying profitable and respecting Topstep limits (maxDD,
daily breaches). If P(pass) does not improve, report that plainly and drop it —
no tuning to rescue it.

## Risks / Honest Caveats

- Scaling caps the upside on half the position → lower raw net; the bet is that
  steadiness raises P(pass) more than the lost profit costs.
- A 2×ATR trail can exit the runner early in choppy pullbacks.
- Daily-resolution combine sim still under-counts intraday blow-ups (pre-existing).
- Adds real state to `backtest`; must be covered by unit tests for: stop before
  scale, scale then trail-exit, scale then trend-flip, size==1 path, and
  baseline-unchanged-when-plan-None.
