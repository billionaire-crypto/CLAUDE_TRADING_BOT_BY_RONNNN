# Session Resume — Strategy Lab (read me first)

**Branch:** `claude/bot-troubleshooting-vclkmv`
**Last updated:** 2026-06-26
**Purpose of this file:** let a fresh (e.g. cloud) Claude session resume without
this machine's local memory. Everything needed is in the repo.

---

## What this thread is

An honest "strategy lab" for the MNQ Topstep bot: find a strategy that survives
real costs and, above all, **raises the probability of PASSING the Topstep
combine** (not raw profit). Core files:
- `src/regime.py` — regime classifier (trend / chop / high_vol / low_vol).
- `src/strategies.py` — candidate entry strategies + `RegimeRouter`.
- `src/evaluate.py` — shared honest backtest, edge grid, rulebook, sealed-holdout, discipline overlay.
- `src/combine_sim.py` — Monte-Carlo "combine judge": P(pass) / P(payout) / P(blowup).
- `src/load_data.py` — loads the Databento GLBX MNQ CSV → clean front-month 5-min RTH bars.

## How to run the lab
```
python -X utf8 -m src.evaluate     # real-data verdict (the -X utf8 avoids a Windows cp1252 print crash)
python -X utf8 -m src.simulate     # synthetic plumbing check only
python -m pytest tests/ -q         # unit tests (pytest was pip-installed this session)
```
Data path defaults in `src/bot.py:38` to the local CSV; override with env var
`MNQ_DATA_PATH`. Data = clean front-month MNQ, 138,270 5-min RTH bars, 2019-05 → 2026-03.

---

## What was accomplished this session (commits on this branch)

1. **`1d0ead9` — Fixed the data loader (the big one).** `src/load_data.py` used to
   pick the alphabetically-first contract per timestamp (unrelated to liquidity),
   injecting stale back-month prints into ~6% of bars and fake 50-140pt jumps. Now
   picks the **highest-volume contract per day with a forward-only roll** (true
   front-month auto roll-over). Eliminated all volume<=3 stale bars; cut >50pt
   intraday jumps ~61%.
   - **This invalidated the previous "choppy edge."** The mean-reversion fades that
     looked great (+$84-126/trade in chop, $261k holdout) were feeding on the fake
     spikes; on clean data they collapse to ~$0. Lesson: suspect data before
     believing a too-good mean-reversion result.

2. **`b01913a` / `bbb2f24` / `69313da` — Four new choppy candidates + tests.**
   RangeEdgeFade, SqueezeBreakout, OpeningRangeFade, LunchLullReversion in
   `src/strategies.py` (10 passing tests in `tests/test_choppy_strategies.py`).
   Result: three are dead in chop. Only **LunchLullReversion** (VWAP fade gated to
   the 10:30-12:30 CT midday lull) is consistently positive across all regimes
   (+18/+31/+45/+10) — but it came SECOND to FVG-trend in chop (+31 vs +38), so the
   rulebook did NOT pick it and fundability was unchanged. NOT tuned in.

3. **Discipline-overlay diagnosis (in-sample ablation, not committed as code —
   throwaway script removed).** The discipline overlay LOWERS P(pass). Cause: its
   **trading windows** (08:30-10:00 + 14:00-14:50 CT) discard >50% of trades. For
   the combine, trade FREQUENCY (more green days) matters more than per-trade
   quality, so halving volume tanks P(pass). breakeven@1R / stop_on_green /
   vol-sizing were ~neutral (the "breakeven cuts winners" guess was WRONG).

4. **`6a4f3dc` — Scale-out + trailing runner exit spec** (see below).

## Current honest state of the lab (single clean run, no holdout tuning)
- Trend strategies (VWAP-cross/pullback): no edge.
- **Only real edge: FVG-trend** (trend-continuation). Rulebook: trend/chop/low_vol
  → FVG-trend, high_vol → Keltner-RSI.
- Holdout router (no discipline): $84.5k net, PF 2.75, 29% win, maxDD $1,881.
- Combine judge: **P(pass) 22.5%, P(payout) 0.6%, negative E[net] → NOT fundable.**
- No genuine choppy edge exists in the menu; "chop" is won by a trend strategy on
  thin data. The strategy is profitable but too **lumpy** (low win / high payoff) to
  reliably pass the combine.

---

## WHERE WE PAUSED — next step

User chose to **improve the one real edge (FVG-trend) via better EXITS** to smooth
the equity curve and raise P(pass). Design is approved and specced:

**Spec:** `docs/superpowers/specs/2026-06-24-scale-out-runner-exit-design.md`
- Opt-in `ScaleOutPlan` in `src/evaluate.backtest` (OFF by default → all prior
  results stay valid). Applies ONLY to trend trades (`target_price is None`).
- Bank 50% at +1R, then trail the runner with a 2×ATR chandelier stop (ratchets up
  only, floored at break-even), let it ride to trend-flip / trail / EOD.
- Judge once on the SEALED HOLDOUT: router with plan ON vs baseline OFF, compare
  P(pass). Acceptance: keep only if P(pass) clearly improves; else drop it.

**Immediate next action:** the spec was at the user-review gate. Next is to invoke
the `writing-plans` skill to produce the implementation plan, then implement TDD
(subagent-driven was the chosen execution style, kept in-session). Honor the
no-holdout-tuning rule.

## Open threads / ideas not yet pursued
- Time-gated reversion (LunchLullReversion) is the most promising unpicked thread.
- A "fixed discipline" (drop the harmful trading windows, keep the daily-loss
  limit) deserves a clean holdout test.
- Bigger question raised: is 5-min RTH the right battlefield given costs?

## Local-only context that will NOT transfer to a cloud clone
- Earlier TopstepX-hardening WIP from branch `codex/topstepx-hardening-checkpoint`
  is in a LOCAL `git stash` on this machine only (message: "topstepx-hardening WIP
  ... auto-stashed"). It is NOT on the remote and will not appear in a cloud
  session. Recover locally with `git stash list` / `git stash pop`.
- User preference (applies to all responses): explain BOTH code and trading in
  beginner-friendly terms; the user is new to coding and trading.
