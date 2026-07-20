# Research Ledger — MNQ V29 Bot

**This is the permanent laboratory notebook.** Every backtest, hypothesis, sweep,
and parameter experiment gets an entry here — *before or as* it's run, not after
it's forgotten. Production code (`src/`) stays clean; the thinking lives here.

Format for each entry: **Date · Hypothesis · Method · Result · Decision.** Record
rejections as carefully as wins — a documented dead end saves the next session
from re-running it.

Scripts referenced below live in this `research/` folder and are run from the repo
root as modules, e.g. `python -m research.run_bias_validation`.

---

## 2026-07-20 — PRE-REGISTRATION: Unfinished Inventory Hypothesis (DES study)

### Hypothesis (registered BEFORE any results were computed)
FVG retest expectancy is governed by the character of the retracement path into
the gap: passive, low-participation, high-impact-per-contract retracements
(institutional parent order still working) precede defended gaps; aggressive,
high-participation retracements precede failures. Measured as DES (Defense
Expectancy Score) = mean of trailing-1-year percentile ranks of three fixed
features computed on 1-minute bars:
- F1 Retrace Participation Ratio = mean(vol retrace)/mean(vol impulse)  [low=good]
- F2 BVC Counterflow Fraction = signed-volume flow against the FVG direction
  during retrace, signed via Bulk Volume Classification sv=V*(2*Phi(dP/sigma)-1),
  sigma = trailing 50-bar 1-min std  [low=good]
- F3 Impact Asymmetry = mean(|r|/V) retrace / mean(|r|/V) impulse  [high=good]
All windows FIXED by convention. NO weights, NO tuning, NO sweeps. DES requires
>=100 prior trades in the trailing year, else undefined. Trades entered on the
bar immediately after FVG creation (age=1) have an empty retrace window; their
DES is undefined by construction and they are reported as uncovered.

### Pre-registered kill criteria (any one -> REJECT)
1. No monotone increase of per-contract expectancy across DES quintiles in dev.
2. Q5-Q1 expectancy spread < $40/trade per contract... [NOTE: original proposal
   said $40/trade at full size; per-contract equivalent at ~4ct avg = $10/ct.
   Registered as: spread < $10 per contract.]
3. Spread sign flips between dev (2019-2022) and validation (2023+).
4. |correlation(DES, fvg_quality_score)| > 0.5 (re-measuring known information).
5. Permutation p >= 0.05 for the Q5-Q1 spread.

### Method
Script: research/run_inventory_echo_study.py. Deterministic re-run of the frozen
V29 backtest to reproduce the identical FVG trade set (no signal changes, no
entry/exit changes); features computed post-hoc from raw 1-minute bars; full
battery: distribution, quintiles, permutation, bootstrap CI, Spearman,
incremental value vs quality score (OLS + partial rank correlation), yearly,
regime splits, interactions, failure analysis, and a resize-only sizing
simulation (Q1 -1ct / Q5 +1ct, clipped [1,5]) with compact combine bootstrap.
Results appended below AFTER this entry was committed to disk.

### RESULT (2026-07-20, script run after pre-registration) — ❌ REJECT

Coverage: 2,877 FVG trades reproduced (100.0% entry-bar mapping validation).
Features defined for 2,054 (71.4%) — 823 trades (28.6%) enter at age 0-1 with an
EMPTY retrace window; the hypothesis is structurally untestable on the
strategy's fastest entries. DES computed for 1,954 after warm-up.

Kill criteria: **4 of 5 triggered.**
1. Monotonicity: FAILED. Expectancy by quintile ($/ct): 10.53 / 11.77 / 14.08 /
   9.30 / 9.81 — peak at Q3, no trend.
2. Spread: FAILED. Q5-Q1 = **-$0.72/ct** (registered floor: +$10).
3. Sign stability: FAILED. Yearly spreads: +13.3 (2020), -5.5 (2021), -3.8
   (2022), +8.7 (2023), +3.0 (2024), +2.8 (2025), -16.4 (2026). Noise.
4. Score overlap: NOT triggered — corr(DES, quality score) = 0.04. DES is
   genuinely orthogonal to the score… and orthogonal to profit too.
5. Significance: FAILED. Permutation p=0.59; bootstrap 95% CI [-6.74, +5.40]
   straddles zero; Spearman(DES, pnl/ct) = -0.012 (p=0.58); incremental OLS
   beta p=0.64; partial rank corr p=0.62. Nothing anywhere — no year, no
   regime, no session, no age/size/trend slice reached significance.

Falsification checks: DES uncorrelated with ATR ratio (0.03), ADX (-0.00), FVG
size (-0.01) — it is NOT a hidden volatility/trend proxy. It IS correlated with
FVG age (rho=0.29): window length mechanically leaks into the features. Part of
DES's variance was "how long did the retrace take," which the age gate already
caps.

Sizing simulation: INVALID — implementation bug (sizes clipped to [1,5] but the
score-map runs 8-50 contracts on 819 trades carrying $291k of $430k net; the
clip, not DES, produced the sim's net difference). Not re-run: with the signal
dead upstream, a corrected sim cannot change the verdict.

### Why it failed (mechanism, worth remembering)
(a) Sample-size floor: retrace windows are 5-15 one-minute bars — BVC and
impact estimates from so few bars are noise-dominated. (b) The age<=4 cap means
all retraces are <=20 minutes; institutional re-engagement footprints, if they
exist, likely need longer horizons. (c) 29% of trades have no retrace window at
all. (d) The strategy's upstream filters (regime, bias, score) may already
harvest whatever conditioning information exists.

### Decision — REJECT. Do not revisit retrace-character features on 1-min OHLCV
for this strategy without materially different data (true tick/order flow) or a
materially different trade horizon. A null this clean is a closed door, not an
invitation to tune.

---

## 2026-07-19 — Port: FVG_BLOCK_LEVEL_SWEEP_ENABLED (candidate, not shipped)

### Background
Auditing `codex/topstepx-hardening-checkpoint` (main) against this branch found main
has one commit this branch never absorbed: `f4d76cd` (2026-04-19), "Add post-sweep
FVG filter blocking globex_high sweep entries." That commit found FVGs preceded by a
globex_high liquidity sweep (wick 3-8 NQ pts beyond the level, closing back inside,
within 10 bars) win at 39.1% vs 46% baseline — a real structural weakness. Main forked
from this branch's lineage on 2026-04-07, before that fix landed, so it was never
carried forward. The other main-only commit (EOD flatten, `b74cf48`) was independently
re-implemented on this branch already — not a gap.

### What was done
Ported ONLY the detection mechanism into `src/bot.py`, as a new OFF-by-default
experiment flag (`FVG_BLOCK_LEVEL_SWEEP_ENABLED = False`), matching the existing
pattern of `FVG_LUNCH_FILTER_ENABLED` / `FVG_VWAP_RUBBER_BAND_ENABLED`:
- `_detect_prior_sweep()` — looks back up to 10 bars in-session for a wick-sweep
  (3-8 pts) of prev_day_high/low or globex_high/low that closed back inside.
- Wired into `run_backtest`'s FVG entry gate, right after `_passes_strategy_filters`:
  when `FVG_BLOCK_LEVEL_SWEEP_ENABLED` is True and a swept level is in
  `FVG_BLOCK_LEVEL_SWEEP_LEVELS` (default `{"globex_high"}`), the entry is skipped.
- **Not ported:** the old commit's TradeRecord/LiveSignalSnapshot diagnostic fields
  and quality-score bonus point — not needed to make the candidate testable, and
  skipping them keeps the footprint minimal.

Confirmed inert: `FVG_BLOCK_LEVEL_SWEEP_ENABLED` defaults to `False`, so
`_detect_prior_sweep()` is never called and `run_backtest`/live signal generation are
byte-identical to before this port. 129/129 tests pass (no test exercises this flag
yet — the nightly gauntlet is the test).

### Decision — PARKED, seeded to nightly backlog as `fvg_block_level_sweep_on`
The April validation is 3+ months stale against the current config (`vwap_only` bias,
ATR-scaled targets, `FVG_MAX_AGE_BARS=4`, `CALM_ATR_RATIO=0.70` all postdate it). Do
**not** trust the old 39.1%/46% numbers as current truth — the nightly researcher will
re-run the full gauntlet against today's baseline and report SHIP or REJECT on its own
evidence, same as every other candidate.

### Notes for the next session
If the nightly researcher SHIPs this, it still needs a human decision per the standing
rule — automated SHIP is never auto-applied to `src/bot.py`.

---

## 2026-07-04 — 🏆 BREAKTHROUGH: the FVG frequency handbrake was the EMA trend-bias

### Background — the question that started it
The strategy is *signal-limited*, not size-limited: it trades ~1.3–1.5 times/day and
sizing up barely speeds the combine. So the only lever that materially grows the edge
is **trading more of the good setups** — without loosening quality. Across the weekend
we tested and **rejected ~10 frequency ideas** with evidence (age-gate relaxation,
concurrency, asset expansion to MES/MCL, fractal timeframes, dual-directional, extra
sessions, order-flow/OBI, NQ-ES residual, session-reset, latency work). Every shortcut
either re-exposed a tail risk or added correlated leverage, not diversification. That
left one place we hadn't audited: **the entry filter itself.**

### Hypothesis
The FVG *detection* is not the bottleneck — something *downstream* of detection is
silently discarding valid, high-quality setups.

### Method — code-level filter audit (not a parameter sweep)
Instead of sweeping numbers, we read the entry gating in `bot.generate_signals`
line by line to find where detected FVGs get dropped. Two candidate gates:
1. **FVG detection** — gap ≥ 2 ticks, `FVG_BODY_QUALITY_ENABLED = False`, all optional
   displacement filters off → **maximally loose already. Not the handbrake.**
2. **Directional bias filter** — entries required
   `long_bias = (ema_fast > ema_slow) AND (close > vwap)` (mirror for shorts).

The **EMA-trend leg** (`ema_fast > ema_slow`) was the culprit. The slow EMAs lag price,
so on VWAP-aligned pullbacks — price reclaiming institutional fair value *before* the
moving averages cross back — the EMA leg vetoed the entry. It was deleting **380
above-average trades** (marginal avg **$193** vs the **$148** book average): we were
throwing away *better-than-typical* trades purely because a lagging indicator hadn't
caught up.

### The change
Added a `BIAS_MODE` toggle to `bot.generate_signals` and shipped **`BIAS_MODE =
"vwap_only"`**: drop the EMA-trend leg, keep the VWAP leg
(`long_bias = close > vwap`, `short_bias = close < vwap`). VWAP = institutional fair
value; that's the leg carrying the real edge.

### Validation — full gauntlet (`research/run_bias_validation.py`)
Backtest window extended with **Databento GLBX.MDP3** 1-minute data; feeds verified
**byte-identical** to ProjectX (715/715 June bars matched) — so the numbers are honest,
not feed artifacts.

| Metric | Shipped (EMA+VWAP) | **vwap_only** | Verdict |
|---|---|---|---|
| Net P&L (7yr) | $343,004 | **$416,436** | **+21%** |
| Profit factor | 3.97 | **4.10** | up |
| Sharpe | 6.22 | **6.45** | up |
| Avg trade | $148 | **$154** | up |
| Trades | ~2,317 | **2,697** | +380 (the recovered ones) |

**Out-of-sample holds (the part that matters):**
- **3-way split (dev / val / UNTOUCHED 2026Q2):** vwap_only beats shipped in **every**
  period — including the never-touched Apr–Jul 2026 quarter (**PF 6.02 → 6.14**). The
  edge did not come from fitting the recent data.
- **Walk-forward:** **0 failed windows.**
- **Stress (slippage ×3):** PF **3.19** — still above the shipped config's baseline.
  The edge survives pessimistic fills.
- **Combine bootstrap sim:** pass rate **97.2% → 97.7%**, median days-to-pass
  **20 → 17**, daily-limit failures **2.39% → 2.03%** (lower is better).

### Tail check (the thing that can end a combine)
Worst modeled trade at scaled size **-$983** (was -$918) — slightly larger but the
*combine* tail improved (fewer daily-limit fails). Still well inside the -$1,000 DLL
buffer. `FVG_MAX_AGE_BARS = 4` was **kept** — relaxing it re-exposes the gap-through
tail (age-12 worst trade **-$5,037**). Single-position kept (concurrency = same-asset
leverage, not diversification).

### Decision — ✅ SHIPPED
`BIAS_MODE = "vwap_only"` is the live, frozen config as of 2026-07-04. **New baseline:
$416,436 / PF 4.10 / 2,697 trades.** Do **not** quote the old $343,004 anymore.

### Notes for the next session
- The April–July 2026 "drought" investigated alongside this was **real market
  behavior**, not a bug or feed gap (Databento vs ProjectX byte-identical). Don't chase
  it as a defect.
- **Detection is already maximally loose** — do not go hunting there for more frequency.
- The remaining known dial is `CALM_ATR_RATIO` (currently **0.75**; April sweep floor
  was 0.70). Loosening 0.75 → 0.70 *might* add a few trades, but it's a "later, only
  with a full gauntlet re-run" item — not to be tuned casually.
- **Lesson:** the biggest edge of the weekend came from *reading the code*, not
  sweeping parameters. Audit the filters before you optimize the numbers.

---

## 2026-07-17 — Shadow Analysis + Regime Detector (live performance audit)

### Hypothesis
After 9 consecutive live losses (-$485.70), determine whether: (a) the losing
streak is statistically normal, (b) live fills are worse than backtest, and
(c) the current market regime is favourable or hostile to the strategy.

### Method
Two new research scripts: `research/shadow_analysis.py` and
`research/regime_detector.py`. Shadow analysis compares live P&L per contract
to the historical distribution of same-direction / same-session-phase trades.
Regime detector slices 2,877 backtest FVG trades by ADX regime, session phase,
quality score, composite day regime (VWAP-crossing frequency + ADX + ATR
percentile), and month.

### Result

**Shadow analysis (9 live trades):**
- Live avg loss per contract: **-$13.21** vs historical avg loss **-$9.65**
  → **$3.57/ct additional drag** (not alarming at n=9; within noise)
- Most live trades sit at the 20–50th pctile of historical losses (normal
  variance). Two outliers: 07-16 09:30 long (6th pctile) and 07-16 13:35
  short (5th pctile) — tail outcomes, not execution failures.
- A 9-trade losing streak at 70.9% loss rate has P=4.55% and is expected
  **~131 times** over the 7-year backtest. No statistical red flag.

**Regime detector key findings:**
- **July is the worst month**: 24.8% WR, $62/trade, **PF 2.26** vs 4.10
  overall. Still net positive but the weakest calendar slot by far.
- **FVG quality score 4 is the weakest viable tier**: 22.7% WR, $50/trade,
  PF 2.42. All 9 live trades were score-4 setups.
- **Session phase**: am best (PF 4.62), lunch worst (PF 2.84), pm good (PF 4.53).
- **ADX regime**: trending best (2,102 trades, PF 4.26), choppy worst (PF ~2.5).
- **Composite day regime**: trending days (PF 5.14) >> choppy (PF 2.53).
- Current live regime (Jul 17): strong ATR, ADX 43 (trending), VWAP crossings
  falling (directional) → expected PF 4.06 in 'strong' regime. Normal regime.
- ADX falling from 56.8 → 38.5 over last 20 days (trending but softening).

**Root cause of current losing streak:**
The combination of July (worst month) + score-4 setups (weakest tier) + a
statistically routine losing streak fully explains the -$485.70. No execution
failure, no regime breakdown, no edge degradation signal.

### Decision — INFORMATIONAL (no config change)
Losing streak is within normal parameters. The bot's edge is intact. Monitor
for the first win to confirm the target-side mechanics work live. Reassess
at 30 live trades for meaningful win-rate signal.

Do NOT loosen entry rules or lower score thresholds to "trade more" — score
4 is already the weakest tier and further loosening only adds noise.

### Notes for the next session
- Update `LIVE_TRADES` in `shadow_analysis.py` after each trading day.
- `regime_detector.py` current-regime section will be stale until Databento
  CSV is extended past 2026-07-02.
- Score 7–8 setups are dramatically better (41–73% WR, $792–$1793/trade);
  keep watching for these — they carry the bulk of the strategy's value.

---

## 2026-07-18 — Payload forensics + auto-journal + backlog seeds (infrastructure)

### What was found (the important part)
**The live slippage monitor has never fired.** `v29_fill_forensics.csv` does not
exist despite 9 live fills — meaning the `GatewayUserTrade` branch of
`_process_user_hub_events` has never successfully parsed a fill payload. Live
hub logs corroborate: every hub event logs `status=None orderId=None size=0`,
i.e. the payload shape ProjectX actually sends does not match the keys the
parser expects. The slippage tripwire (a PLAYBOOK safety feature) is currently
blind; bracket verification is unaffected (it uses REST polling, which works).

### What was built
1. **Payload forensics capture** (`src/topstepx_runtime.py`): raw broker
   payloads (hub events + REST position/order snapshots) are appended to
   `src/exports/payload_forensics.jsonl`, capped at 5 per kind per day.
   Captures resolve audit Findings 1 (signed short sizes), 8 (status enum
   format), B (Auto-OCO bracket duplication) — and will show the real hub
   payload shape so the fill parser can be fixed from evidence, not guesses.
   Covered by 6 new tests (113 total, all passing). Tests write to tmp only.
2. **Auto-journal** (`research/live_journal.py`): parses live logs into
   `research/live_trade_journal.csv` (P&L attribution via flat-snapshot
   deltas; validated — reproduces all 9 known trades to the cent, total
   -$485.70). `shadow_analysis.py` now consumes it automatically; the
   hardcoded trade list is gone.
3. **Nightly backlog seeded** with two pending TIGHTENING candidates from the
   07-17 regime study: `fvg_lunch_filter_on` (lunch = weakest phase, PF 2.84)
   and `fvg_vwap_rubber_band_on` (causal intraday proxy for choppy days,
   PF 2.53 on chop). Both use existing wired-in experiment flags; the
   gauntlet adjudicates. Runner tests one per night at 20:00 CT.

### Next step once a capture lands
Read `payload_forensics.jsonl` after the next live fill; fix the hub payload
parsing in `_process_user_hub_events` against the real shape; close audit
Findings 1/8/B in the ledger.

---

## Template for future entries

```
## YYYY-MM-DD — <short title>
### Hypothesis
### Method  (script: research/run_xxx.py)
### Result  (net / PF / OOS / walk-forward / stress / tail)
### Decision  (SHIPPED / REJECTED / PARKED — and why)
### Notes for the next session
```

---

## 2026-07-06 — ❌ DEAD: Candidate A "Trend-Drift Continuation" (TDC v1)

### Hypothesis
The core is structurally blind to trend-drift days (e.g. first live day: 13 FVGs formed,
zero pullbacks). A continuation edge — enter the drift direction mid-morning when the core
is idle — should monetize those days with near-zero correlation to the core.

### Method (script: research/run_tdc_candidate.py — spec FROZEN before results)
Arm 10:30–14:00 CT when: core has no FVG entry yet that day (causal), ≥80% of bars one
side of VWAP (that side = direction), core regime gate open. Enter next bar open, 3
contracts, stop 1.5×ATR clamp [8,40]pts, target = core's exact ATR machinery (2×ATR,
[15,60]pts), breakeven via bot.desired_stop_price (parity), honest gap-through, EOD flatten,
tiered costs. Dev period ONLY (2019–2022). Pre-registered kills: n<15, PF<1.5, worst<-$600,
one year >60% of net.

### Result (dev 2019–2022, 323 trades — robust sample, not noise)
**PF 0.84 | WR 16.1% | net −$2,843 (−$711/yr) | worst −$247.** Exits: 84% died at the
stop (191 stop + 80 gap-stop), only 51 targets. 2020 = 93% of the losses (regime-fragile
too). FAILED PF gate and concentration gate → DEAD. Not rescued by tuning, per protocol.

### Why it fails (the mechanism, worth remembering)
1. **Buying the day's extreme:** after 80% one-sided price action, entering WITH the drift
   is buying top-of-range mid-day; intraday mean reversion eats the stop 5 times for every
   target.
2. **The core already owns these days more than assumed:** 185 of 323 TDC days (57%) saw
   the core fire LATER the same day — the "drifting" morning usually resolves into exactly
   the pullback the core waits for. TDC was front-running its own core and losing.

### Decision
DEAD. Do not resurrect with tuned thresholds/stops (that is the overfitting failure mode).
Any future drift-day idea must use a structurally different entry (e.g. pullback-based, not
extension-based) and re-clear the full gauntlet from scratch.

### Notes for the next session
- The 57% collision stat is the big lesson: "no trade by 10:30" ≠ "idle day."
- Remaining honest paths: Candidate B (opening-drive first pullback — await archaeology on
  the dormant OD_PULLBACK module), and the Phase-0 day-type census (still unrun) to size
  whether ANY idle-day prize exists before more effort is spent.

---

## 2026-07-06 — 📏 PHASE 0 CENSUS: idle days are common but carry NO harvestable prize — second-edge program KILLED

### Question
How many days does the FVG core sit idle, and is there money on those days for a
second edge? (script: research/run_day_type_study.py, pre-registered 10:30 split)

### Result (1,847 sessions, 2019-05 → 2026-07)
- **Census:** active (core traded) 1,443 days (78.1%) | idle-with-setups-but-no-pullback
  **404 days (21.9%, ~56/yr)**. Zero "dead regime" days — every idle day had regime open
  and FVGs formed; the pullback just never came.
- **Prize on idle days ≈ ZERO or negative:** naive continuation (morning direction →
  hold to EOD) on idle days: win% **51.5% (coin flip)**, avg **−2.0 pts**,
  **−$1,086/yr at 3 contracts**. On active days the same proxy is +4.7 pts — the
  directional material lives on days the core ALREADY trades.
- **No structure:** drift-strength terciles are non-monotonic (weak −12.3 / med +4.9 /
  strong +1.4 pts) — a real continuation edge would strengthen with drift. It doesn't.
- **Unstable sign by year:** 2020 −15.2, 2023 +10.9, 2025 −24.2, 2026 +10.4 pts. Noise.
- **Reversal reading rejected:** the only positive slice (fade weak-drift idle days,
  +12.3 pts) is (a) post-hoc slicing, (b) structurally VWAP mean-reversion on range
  days = the permanently-dead VWAP_MR corpse (42.95% WR, −$3,464). Not resurrected.

### Decision
**The "monetize idle days" program is DEAD at Phase 0.** Idle days are frequent (~1 in
5) but the market offers coin-flip, sign-unstable material on them — the core's sitting
out is CORRECT behavior, not missed money. Combined with TDC v1's autopsy (PF 0.84;
57% of "idle mornings" resolve into afternoon pullbacks the core catches), the honest
conclusion: there is no second intraday MNQ 5-min edge hiding next to this core.
Candidate B (opening-drive pullback) is de-prioritized to dormant — its idle-day
rationale is gone; firing on active days = correlated leverage, repeatedly rejected.

### Notes for the next session
Growth now comes from the remaining two legs only: (1) capital scaling once funded
(contracts within the 50-MNQ cap, payout-cycle math), and (2) letting the validated
core compound. Do not reopen the second-edge hunt without materially new information
(different instrument, different timeframe, or live data contradicting this census).

---

## 2026-07-06 — 🌡️ FEEDBACK LOOP LAB: narrow deadband hunts (−40%), wide deadband buys DD insurance at −20% net — stays research-only

### Question
Can a feedback loop (bot adjusts its own risk knobs from recent performance) beat the
frozen open-loop core, or does it overreact? (lab: experiments/feedback_loop_lab/)

### Method
Frozen core run ONCE → canonical 2,697-trade stream → replayed through 3 controllers on
identical data/fills/costs (seed 42, no lookahead; sensor = raw signal stream so skip
spirals can't freeze the window). Knobs (bounded, stepped, cooled, deadbanded, logged):
size_mult, min_score skip, session_cap, rolling-DD cooldown, daily risk-down. Fill-changing
knobs excluded by design. Mode B = narrow deadband (window 10, ±$40, twitchy). Mode C =
wide deadband (window 40, 2×SE significance, 25-trade cooldowns, risk-down only).
Constants fixed a priori, NOT tuned. Per-knob isolation runs + IS/OOS1/OOS2 + walk-forward
+ 8 stress variants. Harness bug found+fixed before trusting results (min_score default
silently deleted the 71% of trades scoring <6 — all profitable buckets).

### Result
| | net | n | PF | Sharpe | maxDD | adj | hunting |
|---|---|---|---|---|---|---|---|
| A open loop | $416,436 | 2697 | 4.10 | 6.45 | −$2,086 | 0 | 0 |
| B narrow | $248,660 (−40%) | 814 | 4.80 | 7.07 | −$2,143 | 1,172 (425 reversals) | 50.9 |
| C wide | $331,094 (−20%) | 1897 | 4.35 | 6.61 | −$1,763 | 68 | 21.0 |

- B hunted exactly as theory predicts: param ping-pong, killed 70% of trades; its
  prettier PF is amputation, not edge.
- C cut maxDD 15–23% in ALL THREE periods (consistent) but paid ~20% of net every period.
- Per-knob: **rolling-DD cooldown (C)** the only promising one: −1.2% net, −20% maxDD,
  17 adj, hunting 2.6. **size risk-UP** made +$115k but +33% DD = leverage, rejected.
  **min_score skip** deletes profitable low-score trades (score 2–5 = +$135k), rejected.
  **session-cap streak reaction** = pure hunting (61 score), rejected. daily risk-down ≈
  inert (redundant with production DLL stop).
- worst-first stress: feedback dominates (+$104k C, +$415k B) — feedback is INSURANCE
  against structural edge death, paid via −20% net in normal regimes.

### Decision
REJECTED for live/auto per pre-registered acceptance rule (kills trade count; net cost).
Research-only. The production human-in-the-loop already implements the wide-deadband
thermostat: PLAYBOOK 30-session tripwires + drift_report.py, human as actuator.
ONE follow-up permitted if ever needed: combine-sim breach-probability test of the
rolling-DD cooldown as an ADVISORY alert (never auto).

---

## 2026-07-07 — 🌙 NIGHTLY RESEARCHER: CALM_ATR_RATIO 0.75 -> 0.7 — **SHIP**

### Hypothesis
April sweep found 0.70 was the looser floor (monotonic improvement 0.80->0.70, identical at 0.65); shipped config uses 0.75. Parked in memory as 'possible minor future dial, needs full gauntlet re-run' since 2026-07-04. First real candidate for the nightly runner.

### Method (script: research/nightly_researcher.py — automated, unattended)
Full 7-year gauntlet: 3-way split (dev 19-22 / val1 23-24 / val2 25-26+), walk-forward (half-year windows), stress (slip x2/x3, 10% missed fills), 100k-path combine bootstrap, floor safety vs baseline worst trade. Parameter set via in-memory setattr on `bot.py`, restored after each run — **src/bot.py was not modified on disk.**

### Result

- **dev_2019_2022**: baseline net $209,706 (PF 4.04, n=1500) vs candidate net $217,509 (PF 4.03, n=1607)
- **val1_2023_2024**: baseline net $108,628 (PF 4.01, n=732) vs candidate net $109,435 (PF 3.92, n=768)
- **val2_2025_2026**: baseline net $98,102 (PF 4.35, n=465) vs candidate net $102,820 (PF 4.27, n=502)
- **Walk-forward**: 16 windows, 0 negative, avg $26,860/window
- **Stress slip_x2**: net $377,054, PF 3.59
- **Stress slip_x3**: net $338,349, PF 3.15
- **Stress miss_10pct**: net $390,512, PF 4.10
- **Combine sim**: pass rate 97.7% -> 97.8%, daily-limit fails 2.01% -> 1.97%
- **Floor safety**: worst trade $-983 -> $-983

### Decision
**SHIP** — cleared every pre-registered gate. Needs human review before shipping; the runner never edits src/bot.py itself.

### Notes for the next session
Automated overnight result. Verify independently before changing the live config — this is a candidate for human review, not an applied change.

**UPDATE 2026-07-07 14:20 CT:** Ron reviewed and approved — CALM_ATR_RATIO 0.70 **APPLIED to live config** (src/bot.py) and bot restarted mid-session (flat, no orders). First nightly-researcher candidate to complete the full loop: automated gauntlet -> SHIP verdict -> human review -> applied. Context for the decision: the same day's analysis found a secular decline in pullback-day supply (idle rate 12.9%->31.8% 2019->2026, corr 0.96), and this change adds ~7% trade frequency with better nets in all periods.

---

## 2026-07-07 — 🌙 NIGHTLY RESEARCHER: STRONG_MAX_TRADES 4 -> 5 — **SHIP**

### Hypothesis
2026-04-19 sweep: max_trades=5 added +$204/mo, +19 qual days, no WR/PF degradation vs the then-current 4. Never shipped. Needs re-validation on the current vwap_only baseline (that sweep predated the vwap_only/ATR-target changes).

### Method (script: research/nightly_researcher.py — automated, unattended)
Full 7-year gauntlet: 3-way split (dev 19-22 / val1 23-24 / val2 25-26+), walk-forward (half-year windows), stress (slip x2/x3, 10% missed fills), 100k-path combine bootstrap, floor safety vs baseline worst trade. Parameter set via in-memory setattr on `bot.py`, restored after each run — **src/bot.py was not modified on disk.**

### Result

- **dev_2019_2022**: baseline net $217,509 (PF 4.03, n=1607) vs candidate net $219,007 (PF 4.04, n=1625)
- **val1_2023_2024**: baseline net $109,435 (PF 3.92, n=768) vs candidate net $109,954 (PF 3.89, n=779)
- **val2_2025_2026**: baseline net $102,820 (PF 4.27, n=502) vs candidate net $102,461 (PF 4.23, n=506)
- **Walk-forward**: 16 windows, 0 negative, avg $26,964/window
- **Stress slip_x2**: net $378,187, PF 3.57
- **Stress slip_x3**: net $339,205, PF 3.13
- **Stress miss_10pct**: net $375,541, PF 3.87
- **Combine sim**: pass rate 97.7% -> 97.8%, daily-limit fails 1.96% -> 1.95%
- **Floor safety**: worst trade $-983 -> $-983

### Decision
**SHIP** — cleared every pre-registered gate. Needs human review before shipping; the runner never edits src/bot.py itself.

### Notes for the next session
Automated overnight result. Verify independently before changing the live config — this is a candidate for human review, not an applied change.

---

## 2026-07-07 — ❌ DEAD: Candidate B2 "Shallow-Pullback Continuation" (SPC v1) — the trend-day door is now closed from three sides

### Hypothesis
TDC died buying the EXTENSION of trend days. Maybe the entry was the problem, not the
days: buy the shallow DIP TO VWAP (fair value) in an established trend instead —
better price, structural anchor, dip must HOLD (close stays trend-side).

### Method (script: research/run_spc_candidate.py — spec FROZEN before results)
Same harness as TDC (imported simulate/gauntlet machinery — identical stops 1.5xATR
[8,40]pts, core ATR targets, breakeven parity, gap-through, costs, 1/day, 3 contracts).
Trigger: >=70% of bars one side of VWAP + prev bar low touched VWAP while close held
it + core idle (causal) + regime open. Dev 2019-2022 only. Same pre-registered kills.

### Result (dev, 203 trades)
**PF 0.84 | WR 14.3% | net −$1,536 | 85% of entries died at the stop (173/203).**
2020 alone was 111% of the net loss (concentration fail too). FAILED PF and
concentration gates → DEAD. Identical PF to TDC v1 (0.84) despite a structurally
opposite entry.

### Why this closes the book on trend-day continuation
Three independent measurements now agree:
1. Extension entry (TDC): PF 0.84, dead.
2. Fair-value-dip entry (SPC): PF 0.84, dead.
3. Entry-agnostic ceiling (day-type census): idle-day directional material is a
   coin flip (51.5% WR, −$1,086/yr, sign flips by year).
The failure is not entry mechanics — the days themselves carry no harvestable
directional edge. A shallow VWAP dip on these days is frequently the PRECURSOR to
the deeper FVG retrace (which the core already owns), so SPC enters early, gets
chopped, and the core's own later entry is the profitable one.

### Decision
DEAD. Do not revisit trend-day continuation on MNQ 5-min in ANY entry variant
without materially new information (different instrument/timeframe, or live data
contradicting the census). The core sitting out these days is correct behavior.

---

## 2026-07-07 — 🔬 GAP AFTERMATH STUDY: unvisited gaps don't trend — they LEVITATE; and 68% get revisited LATE

### Question (Ron's paradox)
If gaps never get revisited, price must be running away — so why did both continuation
candidates (TDC, SPC) lose with PF 0.84? Where does price actually GO?

### Method (research/run_gap_aftermath_study.py, 27,170 FVGs / 7 years, engine rules)
Classify every gap's fate in its 4-bar life (entered / invalidated / expired-untouched);
for the 15,892 expired ones, measure signed forward moves, late same-day returns, and a
chop test (continuation entry at expiry, 1.5xATR stop vs 2xATR target).

### Results
- Fates: entered 19.6% | blown-through 22.0% | expired untouched 58.5% (mix stable by year;
  entered share drifted 24.3%->~18.5%, consistent with the conversion decline).
- **Aftermath of expired gaps: price goes NOWHERE.** Next 60 min: mean +1.3 pts =
  **+0.06 ATR**, continuation-wins 51.4% (coin flip). EOD: +1.8 pts mean. The impulse that
  CREATES the gap is the whole move; afterwards the market stalls ("levitation", not trend).
- **Chop test:** stop-first 39.4% vs target-first 27.7% -> expectancy ≈ −0.04 ATR before
  costs. This IS the measured mechanism behind TDC/SPC's identical PF 0.84.
- **SURPRISE: 68.3% of expired gaps get touched later the SAME day** — median 8 bars from
  creation vs the engine's 4-bar expiry. "No pullback" is really "pullback arrives late."

### Implications
1. Trend-day continuation is now closed with the mechanism measured, not just the P&L.
2. The age dimension is the live question: FVG_MAX_AGE_BARS=5 is already queued in the
   nightly backlog — this study raises its prior. But late touches are NOT automatically
   good entries (staleness decays the resting-order logic; age-12 corpse: worst trade
   −$5,037). Let the gauntlet adjudicate age 5, then possibly 6. Never leap to 8 off a
   touch-rate statistic — touch rate ≠ profitable-entry rate.

---

## 2026-07-07 — ❌ REJECT (but instructive): FVG_MAX_AGE_BARS 5/6/7 — real money, wrong trade for a combine

### Question
Aftermath study found 68% of expired gaps get touched late (median bar 8 vs 4-bar expiry).
Does waiting longer (age 5, then 6, 7) safely capture those late returns? Walk the gradient,
pre-registered gauntlet rules, see the SHAPE (real hill vs noise spike). Tested against the
CURRENT live config (CALM_ATR_RATIO=0.70).

### Result (research/run_age_sweep.py — full gauntlet per step; src/bot.py md5 verified unchanged)
| age | net | PF | n | worst trade | slipx3 PF | combine pass | verdict |
|----|-----|----|----|----|----|----|----|
| **4 (live)** | $429,764 | 4.06 | 2877 | **−$983** | — | **97.7%** | baseline |
| 5 | $461,845 (+7.5%) | 3.90 | 3165 | −$1,246 | 3.05 | 95.9% | REJECT |
| 6 | $461,649 | 3.62 | 3359 | −$1,246 | 2.86 | 92.2% | REJECT |
| 7 | $473,371 (+10%) | 3.51 | 3517 | −$1,488 | 2.79 | 91.5% | REJECT |

**The shape is a clean, MONOTONIC tradeoff (real, not noise):** net P&L ↑, trade count ↑,
but PF ↓, worst trade ↓ (−983→−1246→−1488), combine pass rate ↓ (97.7→95.9→92.2→91.5).
All 3 REJECT on the SAFETY gates (worst trade worse than baseline; combine pass drop > 1pp) —
NOT on P&L, which improves. Ron's intuition that later entries = more money was CORRECT; the
catch is those extra trades are lower-quality and fatten the gap-through tail, and on a $50k
combine (−$1,000 DLL) a −$1,246 worst trade is a daily-limit breach, not just a bad day.

### Decision
FVG_MAX_AGE_BARS = 4 STAYS. Confirmed the cliff starts immediately (age 5 already breaches
the worst-trade gate), consistent with the age-12 corpse (−$5,037). Net P&L is the WRONG
objective for a combine — combine pass rate is, and age 4 maximizes it.
**Honest nuance for LATER:** once FUNDED with a locked $50k floor, the fatter tail matters
far less (big cushion) and age 5's +7.5% net could be worth revisiting THEN. Parked, not dead,
for the funded phase — but NEVER on the combine.

---

## 2026-07-07 — ✅ SHIP (but tiny): STRONG_MAX_TRADES 4→5 — and the lesson: the cap almost never binds

### Question
Raising the daily trade cap: does the bot leave good trades on the table by stopping at 4?
(research/run_param_sweep.py — new generic gradient tool; src/bot.py md5 verified unchanged.)

### Result (against current live config CALM_ATR_RATIO=0.70)
| cap | net | PF | n | worst | slipx3 PF | combine | verdict |
|----|-----|----|----|----|----|----|----|
| **4 (live)** | $429,764 | 4.06 | 2877 | −$983 | — | 97.7% | baseline |
| **5** | $431,422 | 4.04 | 2910 | **−$983** | 3.13 | **97.8%** | **SHIP** |
| 6 | $429,291 | 3.97 | 2918 | **−$2,318** | 3.05 | 96.7% | REJECT (cliff) |

### The real finding
Cap 5 SHIPS cleanly — worst trade UNCHANGED (−$983), combine pass ticks UP (97.8%),
survives stress. But it adds only **+33 trades across 7 YEARS** (~5/yr). **The 4-cap almost
never binds** — the market rarely offers a 5th qualifying setup in one strong-regime day,
especially in the current low-supply regime. Cap 6 has a hard cliff (worst trade −$2,318:
a 6th trade lets a bad day stack losses past the DLL). Confirms April's 5-good/6-bad finding
on the current baseline. **Lesson: trade count is limited by MARKET SUPPLY, not our caps.**

### Decision
SHIP-eligible (safe, free, combine-positive) — pending Ron's review. But near-zero frequency
impact. This closes the "are we self-limiting?" question: we are not. Every setting-based
frequency lever is now exhausted (vwap_only ✓, CALM 0.70 ✓, cap 5 ✓ trivial; continuation
✗✗, age ✗, timeframe ✗, second-edges ✗). The binding constraint is the market. Next real
optimization is LIVE DATA (does the edge survive execution), not more parameter tuning —
which now risks multiple-comparisons overfitting for diminishing return.

---

## 2026-07-08 — ❌ DEAD (mechanism measured): European session — real edge, uninsurable venue

### Summary of the arc (Option 1: trade EU hours 02:00-08:30 CT for +supply)
- Rules check: Topstep ALLOWS it (5PM->3:10PM trading day; our scalps comply). ✔
- Liquidity gate: EU volume = 19% of RTH — borderline, proceed. ✔
- v1 (frozen strategy, unseen hours): **edge is REAL** — PF 2.15/2.30/2.88 across all 3
  splits, survives slip x3 (PF 1.71), 1.76 trades/session. Failed ONLY the tail gate
  (worst -$2,249). [Also caught+fixed a data artifact first: 17 bad prints, one made a
  fake -$75k trade; phantom filter now applied in research/build_euro_data.py.]
- v2 (dynamic sizing from MEASURED gap risk, dev-calibrated 2019-22 + 25% margin):
  **the measurement is the verdict.** EU stop-fill overshoot: p95 = 1.00 (clean), but
  dev MAX = 14.4x stop distance; the val years contained a **51x** overshoot. Calibrating
  to dev max (mult 18 vs RTH's 1.5) crushed avg trade $58->$18, net $187k->$40k — and
  val2 STILL printed worst -$2,102 because 51x > the dev-calibrated 18x. Worst-20 trades
  spread across ALL EU hours (8 of 20 at London open 02:00 CT, the THICKEST hour) — no
  hour-filter rescue exists.

### The mechanism (why this is final, not tunable)
The overnight book has no liquidity floor. RTH gap-throughs stayed <=~2x stop distance
for 7 years (bounded tail -> insurable via GAP_STOP_MULT=1.5). The EU tail GREW between
sample periods (14x -> 51x): it is effectively unbounded, so any sizing honest about it
destroys the economics, and any sizing that preserves economics lies about the tail.
Same physics as the -$5,521 overnight gap disaster that motivated EOD flatten in March.
**Entry edge != tradeable edge. The venue, not the signal, is the problem.**

### Decision
DEAD for the combine, and NOT parked for the funded phase at size (a 51x event threatens
any account). At most a 1-contract toy after funding — do not revisit beyond that.
Scripts: research/build_euro_data.py, run_euro_test.py, run_euro_v2.py; data: euro_5m.csv.

---

## 2026-07-08 — 🌙 NIGHTLY RESEARCHER: BREAKEVEN_TRIGGER_TICKS 20 -> 16 — **SHIP**

### Hypothesis
Live breakeven trigger is 20 ticks. Never swept against the current vwap_only + ATR-target baseline. A lower trigger locks in break-even sooner (less give-back) but may cut winners short more often - exactly the kind of trade-off the gauntlet should adjudicate, not intuition.

### Method (script: research/nightly_researcher.py — automated, unattended)
Full 7-year gauntlet: 3-way split (dev 19-22 / val1 23-24 / val2 25-26+), walk-forward (half-year windows), stress (slip x2/x3, 10% missed fills), 100k-path combine bootstrap, floor safety vs baseline worst trade. Parameter set via in-memory setattr on `bot.py`, restored after each run — **src/bot.py was not modified on disk.**

### Result

- **dev_2019_2022**: baseline net $217,509 (PF 4.03, n=1607) vs candidate net $217,572 (PF 4.05, n=1607)
- **val1_2023_2024**: baseline net $109,435 (PF 3.92, n=768) vs candidate net $109,520 (PF 3.93, n=768)
- **val2_2025_2026**: baseline net $102,820 (PF 4.27, n=502) vs candidate net $102,810 (PF 4.27, n=502)
- **Walk-forward**: 16 windows, 0 negative, avg $26,869/window
- **Stress slip_x2**: net $377,183, PF 3.60
- **Stress slip_x3**: net $338,444, PF 3.15
- **Stress miss_10pct**: net $390,587, PF 4.11
- **Combine sim**: pass rate 97.7% -> 97.8%, daily-limit fails 1.96% -> 1.96%
- **Floor safety**: worst trade $-983 -> $-983

### Decision
**SHIP** — cleared every pre-registered gate. Needs human review before shipping; the runner never edits src/bot.py itself.

### Notes for the next session
Automated overnight result. Verify independently before changing the live config — this is a candidate for human review, not an applied change.

---

## 2026-07-18 — 🌙 NIGHTLY RESEARCHER: FVG_LUNCH_FILTER_ENABLED False -> True — **REJECT**

### Hypothesis
Regime study (ledger 2026-07-17): lunch phase 11-13 CT is the weakest slice of the book - 897 trades, 23.0% WR, PF 2.84 vs am 4.62 / pm 4.53. This existing experiment flag blocks entries 11:00-12:30 CT (note: slightly narrower than the studied 11-13 bucket). A TIGHTENING, not a loosening. Open question for the gauntlet: lunch still contributed +$78,620/7yr, so cutting it likely lowers net - but does removing the weakest, most stop-out-prone slice improve the combine pass rate / daily-limit fail rate enough to justify it? The combine-sim gate is the interesting one, not raw net.

### Method (script: research/nightly_researcher.py — automated, unattended)
Full 7-year gauntlet: 3-way split (dev 19-22 / val1 23-24 / val2 25-26+), walk-forward (half-year windows), stress (slip x2/x3, 10% missed fills), 100k-path combine bootstrap, floor safety vs baseline worst trade. Parameter set via in-memory setattr on `bot.py`, restored after each run — **src/bot.py was not modified on disk.**

### Result

- **dev_2019_2022**: baseline net $217,509 (PF 4.03, n=1607) vs candidate net $196,842 (PF 4.50, n=1271)
- **val1_2023_2024**: baseline net $109,435 (PF 3.92, n=768) vs candidate net $96,994 (PF 4.33, n=609)
- **val2_2025_2026**: baseline net $102,820 (PF 4.27, n=502) vs candidate net $81,048 (PF 4.51, n=371)
- **Walk-forward**: 16 windows, 0 negative, avg $23,430/window
- **Stress slip_x2**: net $330,477, PF 3.96
- **Stress slip_x3**: net $299,364, PF 3.48
- **Stress miss_10pct**: net $333,016, PF 4.40
- **Combine sim**: pass rate 97.7% -> 96.4%, daily-limit fails 1.96% -> 3.38%
- **Floor safety**: worst trade $-983 -> $-983

### Decision
**REJECT** — dev_2019_2022: candidate net $196,842 < 95% of baseline $217,509; val1_2023_2024: candidate net $96,994 < 95% of baseline $109,435; val2_2025_2026: candidate net $81,048 < 95% of baseline $102,820; combine pass rate -1.3pp (worse than -1pp tolerance)

### Notes for the next session
Automated overnight result. Verify independently before changing the live config — this is a candidate for human review, not an applied change.

---

## 2026-07-19 — 🌙 NIGHTLY RESEARCHER: FVG_VWAP_RUBBER_BAND_ENABLED False -> True — **REJECT**

### Hypothesis
Regime study (ledger 2026-07-17): composite choppy days (high VWAP-crossing frequency) run PF 2.53 vs 5.14 on trending days. A day-level filter is not causal (day type known only in hindsight), but this existing flag is a causal intraday proxy: it requires price to be extended >= 12 pts from VWAP when the FVG is created, which is exactly what churny VWAP-hugging days fail. A TIGHTENING. Gauntlet decides whether the chop it removes is worth the trades it costs.

### Method (script: research/nightly_researcher.py — automated, unattended)
Full 7-year gauntlet: 3-way split (dev 19-22 / val1 23-24 / val2 25-26+), walk-forward (half-year windows), stress (slip x2/x3, 10% missed fills), 100k-path combine bootstrap, floor safety vs baseline worst trade. Parameter set via in-memory setattr on `bot.py`, restored after each run — **src/bot.py was not modified on disk.**

### Result

- **dev_2019_2022**: baseline net $217,509 (PF 4.03, n=1607) vs candidate net $194,532 (PF 3.85, n=1386)
- **val1_2023_2024**: baseline net $109,435 (PF 3.92, n=768) vs candidate net $100,622 (PF 3.88, n=713)
- **val2_2025_2026**: baseline net $102,820 (PF 4.27, n=502) vs candidate net $90,962 (PF 3.97, n=481)
- **Walk-forward**: 16 windows, 0 negative, avg $24,132/window
- **Stress slip_x2**: net $339,286, PF 3.47
- **Stress slip_x3**: net $305,503, PF 3.06
- **Stress miss_10pct**: net $339,658, PF 3.83
- **Combine sim**: pass rate 97.7% -> 95.3%, daily-limit fails 1.96% -> 3.25%
- **Floor safety**: worst trade $-983 -> $-2,318

### Decision
**REJECT** — dev_2019_2022: candidate net $194,532 < 95% of baseline $217,509; val1_2023_2024: candidate net $100,622 < 95% of baseline $109,435; val2_2025_2026: candidate net $90,962 < 95% of baseline $102,820; combine pass rate -2.4pp (worse than -1pp tolerance); worst trade $-2,318 worse than baseline $-983

### Notes for the next session
Automated overnight result. Verify independently before changing the live config — this is a candidate for human review, not an applied change.
