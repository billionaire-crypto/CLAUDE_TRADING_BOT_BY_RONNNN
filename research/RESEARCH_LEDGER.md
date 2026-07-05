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

## Template for future entries

```
## YYYY-MM-DD — <short title>
### Hypothesis
### Method  (script: research/run_xxx.py)
### Result  (net / PF / OOS / walk-forward / stress / tail)
### Decision  (SHIPPED / REJECTED / PARKED — and why)
### Notes for the next session
```
