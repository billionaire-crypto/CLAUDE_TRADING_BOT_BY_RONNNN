# Choppy-Market Strategy Candidates — Design

**Date:** 2026-06-24
**Status:** Approved (pending spec review)

## Problem

The strategy lab (`src/evaluate.py`) now runs on clean, volume-based front-month
5-min RTH MNQ data (2019-2026). On that clean data, the existing "choppy
specialist" fades — `MeanReversionVWAP`, `KeltnerRSIFade`, `TurtleSoup` — show
**no genuine edge in the chop regime**. They all share one thesis ("price
stretched too far from a statistical mean, bet it snaps back") and that thesis
does not survive honest costs once data-glitch spikes are removed.

We want to test genuinely *different* choppy-market ideas through the same honest
pipeline, with no tuning and no peeking at the sealed holdout.

## Goal

Add four new entry-only candidate strategies to `strategies.build_candidates()`
that compete on the existing level playing field (shared exits, sizing, costs,
Topstep rules, edge grid, and combine judge). Run the pipeline once and report
honestly which — if any — clear costs and lift P(pass).

## Non-Goals

- No live trading.
- No tuning parameters against the sealed holdout.
- No changes to the shared harness, exit logic, sizing, or combine judge.
- No new regime features unless a candidate strictly needs one (none do — each
  computes its own state in `observe`).

## Design Constraints (from the existing harness)

All candidates implement the `Strategy` interface in `src/strategies.py`:
`reset_session(session_date)`, `observe(i, row, prev_row, bar_2, ctx)`,
`entry(...) -> Optional[EntrySignal]`, `confirm(signal)`.

Shared facts the candidates must respect:
- Entry price is the current bar's `open`; triggers read the just-CLOSED bar
  (`prev_row`) — no look-ahead.
- Stops are capped to 40 ticks (10 NQ points) via `_cap_long_stop` /
  `_cap_short_stop`.
- Fades (counter-move bets) set `exit_on_trend_flip=False` and supply their own
  `target_price` (the mean). Breakouts use the shared 2R target.
- `ctx.ts_ct` is **America/Chicago** time. RTH open 09:30 ET = 08:30 CT. All
  time windows below are expressed in CT to match `ctx.ts_ct`.
- Candidates do NOT self-gate on regime. The edge grid measures each per regime;
  the `RegimeRouter` does the gating.
- Available per-bar columns include: `open/high/low/close/volume`, `atr`,
  `vwap`, `ema_fast`, `ema_slow`, `long_bias`, `short_bias`,
  `long_signal`, `short_signal`, and regime features `bb_upper/lower/mid`,
  `kc_upper/lower/mid`, `squeeze`, `rsi2`, `vwap`, `regime_class`.

## The Four Candidates

Parameters are locked, first-principles conventions — never fit to the holdout.

### 1. `RangeEdgeFade` — failed break of yesterday's RTH extreme
- **Thesis:** Reversals are reliable only at levels the market respects.
  Yesterday's RTH high/low are such levels; a poke past them that fails is a
  trapped-breakout fade.
- **State (`observe`/`reset_session`):** Track the current session's running
  high/low; at each new session, roll current → `prior_high`/`prior_low`.
- **Entry (`prev_row` trigger):**
  - Failed upside break: `prev.high > prior_high` AND `prev.close < prior_high`
    → **short**, `target_price = vwap`, stop = `prev.high + tick` (capped).
  - Failed downside break: `prev.low < prior_low` AND `prev.close > prior_low`
    → **long**, `target_price = vwap`, stop = `prev.low - tick` (capped).
- **Exit:** `exit_on_trend_flip=False`. Target VWAP or stop.
- **Skip** until a prior day exists (`prior_high`/`prior_low` set).

### 2. `SqueezeBreakout` — expansion out of a volatility coil
- **Thesis:** A low-volatility coil (Bollinger inside Keltner = `squeeze`)
  precedes expansion; trade the breakout direction.
- **State:** Track whether the recent bars were in a squeeze (e.g. squeeze was
  True within the last N bars; N locked at 6).
- **Entry (`prev_row` trigger), only if recently squeezed:**
  - `prev.close > prev.bb_upper` → **long**, stop = `prev.bb_lower` (capped),
    shared 2R target.
  - `prev.close < prev.bb_lower` → **short**, stop = `prev.bb_upper` (capped),
    shared 2R target.
- **Exit:** `exit_on_trend_flip=False` (breakout from chop has no reliable EMA
  trend signal); rely on 2R target or stop.
- Picks its own direction from the breakout side; not gated on `long/short_bias`.

### 3. `OpeningRangeFade` — failed poke outside the first-hour range
- **Thesis:** Pokes outside the established opening range that fail tend to
  revert into it.
- **State:** Build opening range high/low from **08:30-09:30 CT** (first RTH
  hour). Mark formed at 09:30 CT. Allow at most one fade per side per day.
- **Entry (after range formed, `prev_row` trigger):**
  - `prev.high > OR_high` AND `prev.close < OR_high` → **short**,
    `target_price = OR_mid`, stop = `prev.high + tick` (capped).
  - `prev.low < OR_low` AND `prev.close > OR_low` → **long**,
    `target_price = OR_mid`, stop = `prev.low - tick` (capped).
- **Exit:** `exit_on_trend_flip=False`. Target OR midpoint or stop.

### 4. `LunchLullReversion` — VWAP reversion gated to midday
- **Thesis:** Reversion is most reliable when the market is calmest (midday).
  This is the old VWAP fade trigger, but only allowed in the lull window — the
  time gate is the experiment.
- **State:** None beyond the time check.
- **Entry, only when `10:30 CT <= ts_ct.time() < 12:30 CT`** (≈ 11:30-13:30 ET):
  - `(vwap - prev.close) >= 1.5*atr` AND `prev.rsi2 < 15` AND `vwap > price`
    → **long**, `target_price = vwap`, stop = `prev.low - tick` (capped).
  - `(prev.close - vwap) >= 1.5*atr` AND `prev.rsi2 > 85` AND `vwap < price`
    → **short**, `target_price = vwap`, stop = `prev.high + tick` (capped).
- **Exit:** `exit_on_trend_flip=False`. Target VWAP or stop.

## Registration

Add all four to `strategies.build_candidates()`. `build_strategy(name)` resolves
by `.name`, so each needs a unique `name`: `Range-edge-fade`, `Squeeze-breakout`,
`OR-fade`, `Lunch-reversion`.

## Testing & Acceptance

Run `python -m src.evaluate` (with UTF-8 output) **once** on the clean data.
Inspect:
- **Edge grid:** each candidate's cost-adjusted $/trade per regime.
- **Rulebook:** any candidate that wins a regime cell (≥25 trades, positive).
- **Sealed holdout:** router result (net, PF, payoff, maxDD vs $2,000 limit).
- **Combine judge:** P(pass) / P(payout) / P(blowup), E[net] per account.

**Acceptance:** Keep a candidate only if it is positive + year-consistent on the
development window, still positive on the holdout, respects Topstep limits, AND
raises P(pass). A trustworthy "none of these work" is an acceptable, reportable
outcome — no result will be dressed up.

## Risks / Honest Caveats

- Choppy-market edges are thin; costs ($2.34 round-turn) may swamp all four.
- `OpeningRangeFade` and `RangeEdgeFade` may produce few trades (wide gate),
  risking sub-25-trade cells that the rulebook correctly ignores.
- Daily-resolution combine sim still under-counts intraday blow-ups (pre-existing
  limitation, unchanged).
