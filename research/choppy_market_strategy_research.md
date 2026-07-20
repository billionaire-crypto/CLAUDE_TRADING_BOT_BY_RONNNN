# Choppy-Market Strategy Research & Consistency-First Reframe

**Branch:** `claude/strategy-lab-research-2o9zmk`
**Date:** 2026-06-27
**Scope:** (1) Which strategies are actually viable in choppy/ranging MNQ sessions, and (2) replacing the fixed "$3k/month" objective with a consistency-first scorecard (steady win rate + steady profit).

> Backtesting is run in a separate remote session. This document is the **research plan + concrete specs** to run there. Every recommendation maps to existing functions/constants in `src/bot.py` so it is directly implementable.

---

## 0. TL;DR

1. **Stop optimizing for a monthly dollar target.** A fixed "$3k/month" goal rewards lumpy, high-variance behavior (one big day, then overtrading to "catch up"). Optimize instead for a **consistency scorecard** (profit factor, expectancy vs cost, SQN, % profitable days, daily-PnL stability, max consecutive losses). This is not just cleaner statistics — Topstep *itself* enforces a consistency rule (best day ≤ 50% of profit target), so a steady edge is what actually gets funded and keeps payouts. ([Topstep](https://help.topstep.com/en/articles/8284208-what-is-the-consistency-target))

2. **Fix the regime definition first.** `detect_regime()` currently calls a session "choppy" purely on **low volatility** (`ATR < 0.8 × ATR₂₀`, `bot.py:908`). Low volatility ≠ ranging. A quiet, slowly-trending tape is "choppy" under the current rule but is death for mean reversion. Add a true range/trend classifier (ADX and/or Choppiness Index). This single change is likely why earlier "choppy edge" results were unstable.

3. **The viable choppy strategies, ranked:**
   - **#1 VWAP band reversion** (fade ≥2σ extensions back to VWAP) — best evidence base; already half-built in `_build_vwap_mr_setup()` but disabled and over-narrow.
   - **#2 Failed-breakout / liquidity-sweep reversal** — already `ENABLED`; research strongly supports it (60–80% of range breakouts fail).
   - **#3 Lunch/early-afternoon reversion** (the "LunchLull" idea) — lunch chop is the textbook reversion window.
   - **#4 Established-range Bollinger fade** — only after a range is confirmed (≥2h, defined boundaries).
   - **#5 Opening-range-breakout failure fade** — fade the failed ORB retest.

4. **Data caveat that invalidates choppy backtests if unfixed:** `load_data.py` picks the contract **alphabetically** (`bot.py`/`load_data.py:32-33`), not by front-month/volume. Stale back-month prints create fake spike-and-revert bars — which *look exactly like* mean-reversion edge. Confirm the front-month fix is applied in the backtest env before trusting any chop result (patch in §4).

---

## 1. Reframe the objective: consistency over a monthly dollar target

### 1.1 Why "$3k/month" is the wrong objective function

A fixed profit target is an **outcome**, not an **edge**. Optimizing a strategy to hit it tends to produce exactly the wrong incentives:

- It rewards **lumpiness** — a strategy that makes $3k on two huge days and bleeds the rest scores the same as one that grinds $150/day. The first blows up out-of-sample; the second compounds.
- It encourages **overtrading / revenge trading** to "make the number" on slow days — the #1 killer of funded accounts.
- It is **fragile to regime**: a target tuned on a trending sample silently demands trend-sized wins in a choppy sample, so the system forces trades that aren't there.
- Prop firms **explicitly penalize** lumpy profits. Topstep's consistency rule caps the best single day at ≤50% of the profit target; most futures firms use a 20–40% best-day cap (30% common). ([Topstep](https://help.topstep.com/en/articles/8284208-what-is-the-consistency-target), [Tradeify](https://help.tradeify.co/en/articles/10468320-rules-consistency-rule))

The correct objective: **a stable, repeatable per-trade edge that produces a smooth equity curve.** Profit then follows as a byproduct, *and* it satisfies the consistency rule automatically.

### 1.2 The consistency scorecard (proposed selection criterion)

Replace any pass/fail on a dollar target with this weighted scorecard. A strategy variant is "viable in chop" only if it clears the **minimum** column on the chop-only trade subset.

| Metric | Minimum (viable) | Target (fundable) | Notes / source |
|---|---|---|---|
| **Profit factor** | ≥ 1.30 | ≥ 1.75 | Gross win / gross loss. <1.3 = no dependable edge. ([QuantifiedStrategies/JournalPlus](https://journalplus.co/metrics/profit-factor/)) |
| **Expectancy / trade** | ≥ 2× round-turn cost | ≥ 3× cost | Cost ≈ commission + 1 tick slip (`round_turn_cost()`). Edge must dwarf friction. ([LuxAlgo](https://www.luxalgo.com/blog/top-5-metrics-for-evaluating-trading-strategies/)) |
| **SQN (Van Tharp)** | ≥ 1.6 | ≥ 2.5 | `mean(pnl)/std(pnl) × √N`. Rewards edge **and** low variance **and** sample size in one number. ([JournalPlus](https://journalplus.co/metrics/system-quality-number/)) |
| **Win rate** | banded to R:R | — | At 1:1 R:R need >50%; at 2:1 need >40%. Report alongside avg win/avg loss, never alone. |
| **% profitable days** | ≥ 50% | ≥ 55% | Baseline prop expectation. ([TradesViz](https://www.tradesviz.com/blog/general-statistics-reference/)) |
| **Best-day concentration** | ≤ 40% of net | ≤ 30% of net | Directly mirrors prop consistency rules; the anti-lumpiness guard. ([Tradeify](https://help.tradeify.co/en/articles/10468320-rules-consistency-rule)) |
| **Sharpe / Sortino (daily)** | Sharpe ≥ 1.0 | Sortino ≥ 1.5 | Already computed in `compute_stats` (`bot.py:2816-2820`). |
| **Max consecutive losses** | survivable vs daily loss cap | — | Streak × avg loss must not breach `BOT_DAILY_LOSS_LIMIT`. |
| **Calmar** | ≥ 0.5 | ≥ 1.0 | Annual return / |max DD|. Already computed (`bot.py:2813`). |

**What already exists vs. what to add in `compute_stats()` (`bot.py:2794`):**
- ✅ Already there: `win_rate`, `profit_factor`, `sharpe`, `sortino`, `calmar`, `max_dd`, `avg_win`, `avg_loss`, per-`regime_stats`, per-`entry_type_stats`.
- ➕ Add (small, pure additions): `expectancy` (= `total_net / num_trades`), `expectancy_in_cost_units` (= `expectancy / avg_cost`), `sqn` (= `mean(pnl)/std(pnl) * sqrt(N)`), `pct_profitable_days` (from `daily_records`), `best_day_pct_of_net` (max daily PnL / total_net), `max_consec_losses`.
- ➕ Add a `consistency_score(stats)` helper that returns the scorecard table + a single boolean `viable` flag, and print it per regime so you can see "is this thing viable specifically in chop."

> Action for the backtest session: report the scorecard **on the chop-only subset** (`regime_stats["choppy"]` once the regime fix in §2 lands), not on the blended all-regime numbers. Blended numbers hide that the edge may be entirely trend-driven.

---

## 2. Fix the regime classifier (prerequisite for all chop research)

### 2.1 The current definition is volatility, not chop

```python
# bot.py:908
def detect_regime(row) -> str:
    if row["atr"] >= row["atr_avg_20"] * ATR_REGIME_MULT:   # 0.8
        return "trending"
    return "choppy"
```

This labels **any low-volatility bar** "choppy," including quiet grinding trends — the worst environment to fade. It says nothing about whether price is *directional* vs *range-bound*.

### 2.2 Recommended classifier: ADX (+ optional Choppiness Index)

Standard, well-documented thresholds:
- **ADX < 20 → chop / no-trend**; **ADX > 25 → trend**; 20–25 = neutral (stand aside). ([TradingView ADX](https://www.tradingview.com/scripts/averagedirectionalindex/), [ChoppinessIndex.com](https://choppinessindex.com/choppiness-index-vs-adx/))
- Optionally confirm with the **Choppiness Index**: CHOP > 61.8 → choppy/ranging; CHOP < 38.2 → trending; 38.2–61.8 = transition. ([Positioned glossary](https://positioned.app/traders-glossary/choppiness-index-indicator), [Morpher](https://www.morpher.com/blog/choppiness-index))
- "Trending vs chop" can also be cross-checked structurally: net price progress over the last 10–20 bars (range-bound = chop; net-progressing = trend). ([Tradetus](https://www.tradetus.com/learn/ms-trending-vs-chop/))

`adx` is **already computed** in `add_indicators()` (`bot.py:889`), so this is a drop-in. Proposed:

```python
ADX_CHOP_MAX   = 20      # ADX below this = ranging
ADX_TREND_MIN  = 25      # ADX above this = trending
# Choppiness Index = 100*log10(sum(TR,n)/(max(high,n)-min(low,n)))/log10(n); n=14
CHOP_RANGE_MIN = 61.8    # CHOP above this confirms range

def detect_regime(row) -> str:
    adx = row.get("adx")
    if pd.isna(adx): return "unknown"
    if adx >= ADX_TREND_MIN: return "trending"
    if adx <  ADX_CHOP_MAX:  return "choppy"
    return "neutral"        # stand aside — do not fade, do not chase
```

**This is the single highest-leverage change.** A real chop filter is what separates "mean reversion has 55–65% WR with filters" from "≈45% and the trend-day failures dominate." ([HorizonAI](https://www.horizontrading.ai/learn/mean-reversion-trading-strategies)) Add `neutral` as an explicit no-trade state — most false reversion signals occur in the 20–25 ADX transition zone.

---

## 3. Choppy-market strategy candidates (ranked, with specs)

General principles confirmed across sources: **mean reversion outperforms on range-bound days; it fails badly on trend days, and those failures dominate without a regime screen.** Fade stretched moves back to a defined mean (VWAP, prior close, range mid). Best reversion windows: first ~90 min and the lunch/early-afternoon lull. ([Tradewink](https://www.tradewink.com/learn/mean-reversion-strategy), [CrossTrade VWAP reversion](https://crosstrade.io/learn/trading-strategies/vwap-reversion))

### #1 — VWAP band reversion (highest priority)

**Mechanism:** When price extends ≥2σ (or a fixed % / ATR multiple) from session VWAP *in a confirmed chop regime*, fade back toward VWAP. VWAP is the intraday institutional fair-value anchor; in range conditions it exerts a gravitational pull. ([CrossTrade](https://crosstrade.io/learn/trading-strategies/vwap-reversion), [ChartsWatcher](https://chartswatcher.com/pages/blog/a-practical-guide-to-vwap-strategy-trading))

**Why it beats the current module:** `_build_vwap_mr_setup()` (`bot.py:1240`, currently `VWAP_MR_ENABLED=False`) is the right idea but over-constrained:
- **Short-only** (`if current_price <= vwap: return None`) — it ignores the entire long side of reversion. Mirror it for longs below VWAP.
- Requires a **failed bullish FVG** within 8 bars — a narrow co-occurrence that discards most valid fades. Make the FVG a *bonus*, not a gate.
- Uses a fixed **points** distance band (16–24 pts). Replace with a **VWAP σ-band** (rolling std of price−VWAP) or an **ATR-scaled** band so it adapts across years/volatility.

**Concrete spec to backtest:**
- Regime: `choppy` (new ADX classifier), ADX < 20, ATR ratio ≤ ~1.1.
- Entry: price ≥ +2σ above VWAP → short; ≤ −2σ below → long. (σ = rolling std of `close−vwap`, intraday.) ([HorizonAI](https://www.horizontrading.ai/learn/mean-reversion-trading-strategies))
- Confirmation (keep, but soft): rejection wick / breakdown bar (already in the code).
- Target: VWAP (or VWAP ± `TARGET_BUFFER`).
- Stop: beyond ±3σ (the "gravity weakens past 3σ" level) capped at `VWAP_MR_STOP_TICKS`. ([HorizonAI](https://www.horizontrading.ai/learn/mean-reversion-trading-strategies))
- Filters: skip `ALL_NEWS_DATES` (FOMC/CPI/NFP already in `bot.py:298`); avoid first 15 min after open.
- Sessions to A/B: first 90 min vs lunch (see #3).

### #2 — Failed-breakout / liquidity-sweep reversal (already enabled — keep & tune)

**Mechanism:** Price pokes just beyond a recent swing high/low (sweeps resting stops), fails to follow through, and reverses. Research puts breakout-failure rates at **60–80%** in range conditions — i.e., fading the failed break is the high-probability side. ([BuildAlpha ORB](https://www.buildalpha.com/opening-range-breakout/))

**Status:** `FAILED_BREAKOUT_ENABLED = True` (`bot.py:212`), `_build_failed_breakout_setup()` (`bot.py:1486`). Already ADX-bounded (10–25) and ATR-bounded (≤1.5) — well-aligned to chop. **This is your most credible existing chop edge.** Tune `FB_MIN/MAX_SWEEP_TICKS`, `FB_TARGET_TICKS` (currently 52), and test extending `FB_WINDOW` to include lunch.

### #3 — Lunch / early-afternoon reversion ("LunchLull")

**Mechanism:** Volume thins midday → directional conviction drops → price oscillates around VWAP. This is the canonical mean-reversion window. ([CrossTrade](https://crosstrade.io/learn/trading-strategies/vwap-reversion), [Tradewink](https://www.tradewink.com/learn/mean-reversion-strategy))

**Spec:** This is **not a separate strategy** — it's strategy #1 with a time gate. Implement as a session window (CT) on the VWAP-band reversion, e.g. test windows `10:30–12:30` and `11:00–13:30`. Reuse the existing window-helper pattern (`_is_vwap_mr_window`, `bot.py:1232`). The handoff noted LunchLull was consistently positive but lost the rulebook tiebreak to FVG-trend in chop — so the fix is **selection logic** (let it run when FVG-trend is absent), not the signal itself. See §3.6.

### #4 — Established-range Bollinger fade

**Mechanism:** Once a range is *confirmed* (price contained in a band for ≥ ~2h after open), buy the lower Bollinger Band (20, 2σ), sell the upper. Only fade **after** the range is established — not preemptively. ([HighStrike](https://highstrike.com/opening-range/) range-day rules cited in search)

**Spec:** Needs a "range established" detector: rolling high−low contained within X ticks for ≥ N bars. Then fade band touches with target = range mid / opposite band, stop beyond the band. Lowest priority because it requires the most new scaffolding (range-state tracking) — build only if #1–#3 underperform.

### #5 — Opening-range-breakout failure fade

**Mechanism:** ORB breaks the opening-range extreme, runs to grab liquidity, fails, and reverts. Fade the failed retest. On choppy/range days this is the productive way to use the ORB structure already present (`ORBState`, `bot.py:313`). ([ForexTester ORB](https://forextester.com/blog/opening-range-breakout-trading-strategies/), [BuildAlpha](https://www.buildalpha.com/opening-range-breakout/)) Note your current ORB module trades the breakout *with* trend; the fade is the chop-regime complement. Most valid ORB action is 09:35–10:15 CT.

### 3.6 Selection logic — the real blocker

The handoff's key finding: LunchLull was positive but the rulebook picked FVG-trend in chop and never fired it. **Don't make strategies compete on a single per-bar score across regimes.** Instead:
- Route by **regime** first: in `trending` → FVG-trend/ORB only; in `choppy` → reversion stack (#1–#3) only; in `neutral` → stand aside.
- Within chop, allow multiple reversion modules to coexist (they rarely signal the same bar) with a per-day cap, rather than a winner-take-all tiebreak.

This is a **router**, not a new strategy, and it's likely the change that finally lets the consistent-but-overlooked reversion edge show up in results.

---

## 4. Data integrity prerequisite (confirm before trusting chop results)

Mean-reversion backtests are **uniquely sensitive** to bad prints: a stale back-month tick creates a fake spike that instantly "reverts," manufacturing phantom edge. The loader currently selects the contract alphabetically:

```python
# load_data.py:31-33  (current — WRONG for front-month)
df_5m = df_5m.sort_values(['ts_event', 'symbol'])
df_5m = df_5m.drop_duplicates(subset='ts_event', keep='first')
```

Alphabetical-first ≠ front-month. Select the **most-liquid (highest-volume) contract per timestamp** instead:

```python
# Front-month = highest-volume symbol at each timestamp
df_5m = df_5m.sort_values(['ts_event', 'volume'])           # ascending volume
df_5m = df_5m.drop_duplicates(subset='ts_event', keep='last')  # keep highest-volume
```

(Or roll on volume crossover for a continuous series.) The handoff says this was fixed in the backtest env (`1d0ead9`) — **verify it is actually applied there**, because this branch still has the naive version. Any "choppy edge" measured on un-fixed data is suspect.

---

## 5. Backtest experiment matrix (run in the remote session)

Run each as a separate config; score every run with the **§1.2 consistency scorecard on the chop-only subset**. Select by SQN + profit factor + best-day concentration — **not** by total dollars.

| # | Change under test | Compare against | Success = |
|---|---|---|---|
| E0 | Apply §2 ADX regime classifier (baseline re-measure) | current ATR-only regime | chop subset stats stabilize / become interpretable |
| E1 | VWAP band reversion, **both sides**, σ-bands, FVG as bonus | current short-only failed-FVG module | chop PF ≥ 1.3, SQN ≥ 1.6 |
| E2 | E1 gated to lunch window (test 10:30–12:30, 11:00–13:30) | E1 all-session | higher % profitable days, lower best-day % |
| E3 | Regime **router** (§3.6): reversion stack in chop, trend stack in trend | winner-take-all rulebook | reversion trades actually fire; blended Sharpe ↑ |
| E4 | Failed-breakout tuning (sweep ticks, target, +lunch window) | current FB defaults | chop PF ↑ without WR collapse |
| E5 | Add `neutral` no-trade zone (ADX 20–25) | trading through transition | fewer trades, higher expectancy/trade |

**Stop rule for the lab:** a variant is "viable in chop" when, **on the chop subset**, it clears every Minimum in §1.2 across the full clean (front-month-fixed) sample. Promote to "fundable candidate" only when it clears the Target column *and* best-day ≤ 30% of net.

---

## 6. Honest status note

For transparency: the research thread described in the handoff (the `1d0ead9` data fix, the `69313da` candidates, `LunchLullReversion`, the discipline windows, and the `P(pass)`/`P(payout)` fundability harness) is **not present in this branch** — `git log` shows only `b789459` (TopstepX hardening), `load_data.py` still uses alphabetical contract selection, and `monte_carlo()` is a plain i.i.d. bootstrap with no combine pass/payout model. That work lives in your separate backtest environment. This document is written to be applied there; the file/line references above are to the code as it exists on this branch so they're easy to port.

---

## Sources

- Mean reversion in range/chop, VWAP fades, session timing: [HorizonAI](https://www.horizontrading.ai/learn/mean-reversion-trading-strategies), [Tradewink](https://www.tradewink.com/learn/mean-reversion-strategy), [CrossTrade — VWAP reversion](https://crosstrade.io/learn/trading-strategies/vwap-reversion), [ChartsWatcher](https://chartswatcher.com/pages/blog/a-practical-guide-to-vwap-strategy-trading), [edgeful](https://www.edgeful.com/blog/posts/mean-reversion-strategy), [MetroTrade](https://www.metrotrade.com/mean-reversion-trading-strategy/)
- Regime detection (ADX / Choppiness Index): [TradingView ADX](https://www.tradingview.com/scripts/averagedirectionalindex/), [ChoppinessIndex.com — CHOP vs ADX](https://choppinessindex.com/choppiness-index-vs-adx/), [Positioned glossary](https://positioned.app/traders-glossary/choppiness-index-indicator), [Morpher](https://www.morpher.com/blog/choppiness-index), [Tradetus](https://www.tradetus.com/learn/ms-trending-vs-chop/)
- Breakout-failure / ORB fade: [BuildAlpha](https://www.buildalpha.com/opening-range-breakout/), [ForexTester](https://forextester.com/blog/opening-range-breakout-trading-strategies/), [HighStrike](https://highstrike.com/opening-range/)
- Evaluation metrics (profit factor, expectancy, SQN): [JournalPlus — Profit Factor](https://journalplus.co/metrics/profit-factor/), [JournalPlus — SQN](https://journalplus.co/metrics/system-quality-number/), [LuxAlgo](https://www.luxalgo.com/blog/top-5-metrics-for-evaluating-trading-strategies/), [QuantifiedStrategies](https://www.quantifiedstrategies.com/trading-performance/)
- Consistency rules / % profitable days: [Topstep consistency target](https://help.topstep.com/en/articles/8284208-what-is-the-consistency-target), [Tradeify](https://help.tradeify.co/en/articles/10468320-rules-consistency-rule), [TradesViz](https://www.tradesviz.com/blog/general-statistics-reference/)
