# Choppy-Market Strategy Candidates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add four genuinely different choppy-market entry strategies to the candidate menu and judge them once through the unchanged honest pipeline.

**Architecture:** Each strategy is an entry-only class implementing the existing `Strategy` interface in `src/strategies.py` (`reset_session`/`observe`/`entry`/`confirm`). They reuse the shared harness for exits, sizing, costs, and Topstep rules. Unit tests exercise each `entry()` trigger in isolation with hand-built bar dicts. A final manual run of `python -m src.evaluate` produces the edge grid / rulebook / holdout / combine verdict.

**Tech Stack:** Python 3.13, pandas, numpy, pytest. Data is clean front-month 5-min RTH MNQ (2019-2026) loaded via the already-fixed `src/load_data.py`.

**Ground rules:** No tuning against the sealed holdout. No changes to the harness, sizing, exits, or combine judge. No live trading. All params are locked first-principles conventions.

---

## File Structure

- **Modify:** `src/strategies.py` — add four classes after the existing choppy specialists (near `TurtleSoup`/`VWAPCross`), and append them to `build_candidates()`.
- **Create:** `tests/test_choppy_strategies.py` — unit tests for each new `entry()` trigger plus a registration test.

### Shared facts the code relies on (already in `src/strategies.py`)
- `TICK = bot.MNQ_TICK_SIZE` (0.25), `STOP_CAP_TICKS = 40`.
- Helpers: `_cap_long_stop(entry, raw)`, `_cap_short_stop(entry, raw)`, `_ok(*vals)`.
- `EntrySignal(direction, stop_price, entry_type, meta={}, exit_on_trend_flip=True, target_price=None)`.
- `from datetime import time as dtime` and `import pandas as pd` are already imported.
- The harness passes `row`/`prev_row`/`bar_2` as **dicts**, and `ctx` as a `SimpleNamespace` with `ts_ct` (a `datetime` in America/Chicago), `session_date`, and `tick`.
- Triggers read the just-CLOSED bar `prev_row`; entry price is `row["open"]`.

### Preflight: confirm pytest is available

- [ ] **Step 0: Verify pytest**

Run: `python -m pytest --version`
Expected: prints a pytest version. If it errors with "No module named pytest", run `pip install pytest` first.

---

## Task 1: `RangeEdgeFade` — failed break of yesterday's RTH extreme

**Files:**
- Create: `tests/test_choppy_strategies.py`
- Modify: `src/strategies.py` (add class after `TurtleSoup`)

- [ ] **Step 1: Write the failing test**

Create `tests/test_choppy_strategies.py`:

```python
from types import SimpleNamespace
from datetime import datetime, date

import pandas as pd
from src import strategies


def _ctx(hh=11, mm=0):
    return SimpleNamespace(
        ts_ct=datetime(2024, 1, 2, hh, mm),
        session_date=date(2024, 1, 2),
        tick=strategies.TICK,
    )


def test_range_edge_fade_short_on_failed_high_break():
    s = strategies.RangeEdgeFade()
    s.prior_high, s.prior_low = 100.0, 90.0
    prev = {"high": 101.0, "low": 99.0, "close": 99.5, "vwap": 95.0}
    row = {"open": 100.0}
    sig = s.entry(1, row, prev, prev, _ctx())
    assert sig is not None
    assert sig.direction == "short"
    assert sig.target_price == 95.0
    assert sig.exit_on_trend_flip is False


def test_range_edge_fade_none_without_poke():
    s = strategies.RangeEdgeFade()
    s.prior_high, s.prior_low = 100.0, 90.0
    prev = {"high": 99.0, "low": 95.0, "close": 98.0, "vwap": 96.0}
    row = {"open": 97.0}
    assert s.entry(1, row, prev, prev, _ctx()) is None


def test_range_edge_fade_none_without_prior_day():
    s = strategies.RangeEdgeFade()
    prev = {"high": 101.0, "low": 99.0, "close": 99.5, "vwap": 95.0}
    row = {"open": 100.0}
    assert s.entry(1, row, prev, prev, _ctx()) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_choppy_strategies.py -k range_edge -v`
Expected: FAIL with `AttributeError: module 'src.strategies' has no attribute 'RangeEdgeFade'`.

- [ ] **Step 3: Write minimal implementation**

In `src/strategies.py`, after the `TurtleSoup` class, add:

```python
# ── Choppy candidate 1: failed break of YESTERDAY's RTH extreme ──────────────
class RangeEdgeFade(Strategy):
    """Fade a failed poke beyond yesterday's RTH high/low (a level the market
    respects) back toward VWAP. Trapped-breakout reversal — only at proven
    levels, unlike the statistical-stretch fades."""

    name = "Range-edge-fade"

    def __init__(self):
        self.prior_high = None
        self.prior_low = None
        self._cur_high = None
        self._cur_low = None

    def reset_session(self, session_date):
        if self._cur_high is not None:
            self.prior_high = self._cur_high
            self.prior_low = self._cur_low
        self._cur_high = None
        self._cur_low = None

    def observe(self, i, row, prev_row, bar_2, ctx):
        h, l = float(row["high"]), float(row["low"])
        self._cur_high = h if self._cur_high is None else max(self._cur_high, h)
        self._cur_low = l if self._cur_low is None else min(self._cur_low, l)

    def entry(self, i, row, prev_row, bar_2, ctx):
        if self.prior_high is None or pd.isna(prev_row["vwap"]):
            return None
        price = float(row["open"])
        vwap = float(prev_row["vwap"])
        if float(prev_row["high"]) > self.prior_high and float(prev_row["close"]) < self.prior_high and vwap < price:
            stop = _cap_short_stop(price, float(prev_row["high"]) + TICK)
            if stop - price <= 0:
                return None
            return EntrySignal("short", stop, self.name, exit_on_trend_flip=False, target_price=vwap)
        if float(prev_row["low"]) < self.prior_low and float(prev_row["close"]) > self.prior_low and vwap > price:
            stop = _cap_long_stop(price, float(prev_row["low"]) - TICK)
            if price - stop <= 0:
                return None
            return EntrySignal("long", stop, self.name, exit_on_trend_flip=False, target_price=vwap)
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_choppy_strategies.py -k range_edge -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/test_choppy_strategies.py src/strategies.py
git commit -m "Add RangeEdgeFade choppy candidate (failed break of prior-day extreme)"
```

---

## Task 2: `SqueezeBreakout` — expansion out of a volatility coil

**Files:**
- Modify: `tests/test_choppy_strategies.py` (add tests)
- Modify: `src/strategies.py` (add class after `RangeEdgeFade`)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_choppy_strategies.py`:

```python
def test_squeeze_breakout_long_on_close_above_band():
    s = strategies.SqueezeBreakout()
    s._since_squeeze = 0  # just came out of a squeeze
    prev = {"bb_upper": 100.0, "bb_lower": 95.0, "close": 101.0}
    row = {"open": 100.5}
    sig = s.entry(1, row, prev, prev, _ctx())
    assert sig is not None
    assert sig.direction == "long"
    assert sig.target_price is None  # uses shared 2R target
    assert sig.exit_on_trend_flip is False


def test_squeeze_breakout_none_when_not_recently_squeezed():
    s = strategies.SqueezeBreakout()
    s._since_squeeze = 50  # no recent squeeze
    prev = {"bb_upper": 100.0, "bb_lower": 95.0, "close": 101.0}
    row = {"open": 100.5}
    assert s.entry(1, row, prev, prev, _ctx()) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_choppy_strategies.py -k squeeze -v`
Expected: FAIL with `AttributeError: ... has no attribute 'SqueezeBreakout'`.

- [ ] **Step 3: Write minimal implementation**

In `src/strategies.py`, after `RangeEdgeFade`, add:

```python
# ── Choppy candidate 2: expansion breakout out of a squeeze ──────────────────
class SqueezeBreakout(Strategy):
    """After a low-volatility coil (Bollinger inside Keltner = squeeze), trade the
    bar that closes outside the Bollinger band in the breakout direction. Flips
    the chop thesis: quiet precedes expansion. Shared 2R target."""

    name = "Squeeze-breakout"
    LOOKBACK = 6  # a squeeze within the last N closed bars still qualifies

    def __init__(self):
        self._since_squeeze = 999

    def reset_session(self, session_date):
        self._since_squeeze = 999

    def observe(self, i, row, prev_row, bar_2, ctx):
        sq = prev_row["squeeze"]
        if bool(sq):
            self._since_squeeze = 0
        else:
            self._since_squeeze += 1

    def entry(self, i, row, prev_row, bar_2, ctx):
        if self._since_squeeze > self.LOOKBACK:
            return None
        bb_u, bb_l, close = prev_row["bb_upper"], prev_row["bb_lower"], prev_row["close"]
        if not _ok(bb_u, bb_l, close):
            return None
        price = float(row["open"])
        if float(close) > float(bb_u):
            stop = _cap_long_stop(price, float(bb_l))
            if price - stop <= 0:
                return None
            return EntrySignal("long", stop, self.name, exit_on_trend_flip=False)
        if float(close) < float(bb_l):
            stop = _cap_short_stop(price, float(bb_u))
            if stop - price <= 0:
                return None
            return EntrySignal("short", stop, self.name, exit_on_trend_flip=False)
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_choppy_strategies.py -k squeeze -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/test_choppy_strategies.py src/strategies.py
git commit -m "Add SqueezeBreakout choppy candidate (expansion out of a coil)"
```

---

## Task 3: `OpeningRangeFade` — failed poke outside the first-hour range

**Files:**
- Modify: `tests/test_choppy_strategies.py` (add tests)
- Modify: `src/strategies.py` (add class after `SqueezeBreakout`)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_choppy_strategies.py`:

```python
def test_opening_range_fade_short_on_failed_high_break():
    s = strategies.OpeningRangeFade()
    s.reset_session(date(2024, 1, 2))
    s.or_high, s.or_low, s.or_mid, s.formed = 100.0, 90.0, 95.0, True
    prev = {"high": 101.0, "low": 99.0, "close": 99.5}
    row = {"open": 100.0}
    sig = s.entry(1, row, prev, prev, _ctx())
    assert sig is not None
    assert sig.direction == "short"
    assert sig.target_price == 95.0
    s.confirm(sig)
    assert s.faded_high is True
    # one fade per side: the same setup no longer fires
    assert s.entry(1, row, prev, prev, _ctx()) is None


def test_opening_range_fade_none_before_formed():
    s = strategies.OpeningRangeFade()
    s.reset_session(date(2024, 1, 2))
    prev = {"high": 101.0, "low": 99.0, "close": 99.5}
    row = {"open": 100.0}
    assert s.entry(1, row, prev, prev, _ctx()) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_choppy_strategies.py -k opening_range -v`
Expected: FAIL with `AttributeError: ... has no attribute 'OpeningRangeFade'`.

- [ ] **Step 3: Write minimal implementation**

In `src/strategies.py`, after `SqueezeBreakout`, add:

```python
# ── Choppy candidate 3: failed poke outside the first-hour range ─────────────
class OpeningRangeFade(Strategy):
    """Fade a failed break of the 08:30-09:30 CT opening range (= the first RTH
    hour, 09:30-10:30 ET), back to its midpoint. One fade per side per day."""

    name = "OR-fade"
    FORM_START = dtime(8, 30)
    FORM_END = dtime(9, 30)

    def reset_session(self, session_date):
        self.or_high = None
        self.or_low = None
        self.or_mid = None
        self.formed = False
        self.faded_high = False
        self.faded_low = False

    def observe(self, i, row, prev_row, bar_2, ctx):
        t = ctx.ts_ct.time()
        if self.FORM_START <= t < self.FORM_END:
            h, l = float(row["high"]), float(row["low"])
            self.or_high = h if self.or_high is None else max(self.or_high, h)
            self.or_low = l if self.or_low is None else min(self.or_low, l)
        elif t >= self.FORM_END and not self.formed and self.or_high is not None:
            self.formed = True
            self.or_mid = (self.or_high + self.or_low) / 2.0

    def entry(self, i, row, prev_row, bar_2, ctx):
        if not self.formed:
            return None
        price = float(row["open"])
        if (not self.faded_high and float(prev_row["high"]) > self.or_high
                and float(prev_row["close"]) < self.or_high and self.or_mid < price):
            stop = _cap_short_stop(price, float(prev_row["high"]) + TICK)
            if stop - price <= 0:
                return None
            return EntrySignal("short", stop, self.name, {"side": "high"},
                               exit_on_trend_flip=False, target_price=self.or_mid)
        if (not self.faded_low and float(prev_row["low"]) < self.or_low
                and float(prev_row["close"]) > self.or_low and self.or_mid > price):
            stop = _cap_long_stop(price, float(prev_row["low"]) - TICK)
            if price - stop <= 0:
                return None
            return EntrySignal("long", stop, self.name, {"side": "low"},
                               exit_on_trend_flip=False, target_price=self.or_mid)
        return None

    def confirm(self, signal):
        if signal.meta.get("side") == "high":
            self.faded_high = True
        elif signal.meta.get("side") == "low":
            self.faded_low = True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_choppy_strategies.py -k opening_range -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/test_choppy_strategies.py src/strategies.py
git commit -m "Add OpeningRangeFade choppy candidate (failed first-hour break)"
```

---

## Task 4: `LunchLullReversion` — VWAP reversion gated to midday

**Files:**
- Modify: `tests/test_choppy_strategies.py` (add tests)
- Modify: `src/strategies.py` (add class after `OpeningRangeFade`)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_choppy_strategies.py`:

```python
def test_lunch_lull_long_inside_window():
    s = strategies.LunchLullReversion()
    prev = {"vwap": 100.0, "close": 90.0, "atr": 5.0, "rsi2": 10.0,
            "low": 89.0, "high": 91.0}
    row = {"open": 95.0}
    sig = s.entry(1, row, prev, prev, _ctx(hh=11, mm=0))  # 11:00 CT = inside lull
    assert sig is not None
    assert sig.direction == "long"
    assert sig.target_price == 100.0
    assert sig.exit_on_trend_flip is False


def test_lunch_lull_none_outside_window():
    s = strategies.LunchLullReversion()
    prev = {"vwap": 100.0, "close": 90.0, "atr": 5.0, "rsi2": 10.0,
            "low": 89.0, "high": 91.0}
    row = {"open": 95.0}
    assert s.entry(1, row, prev, prev, _ctx(hh=9, mm=0)) is None  # 09:00 CT = outside
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_choppy_strategies.py -k lunch -v`
Expected: FAIL with `AttributeError: ... has no attribute 'LunchLullReversion'`.

- [ ] **Step 3: Write minimal implementation**

In `src/strategies.py`, after `OpeningRangeFade`, add:

```python
# ── Choppy candidate 4: VWAP reversion gated to the midday lull ──────────────
class LunchLullReversion(Strategy):
    """The VWAP-stretch fade, allowed ONLY in the calm midday window
    (10:30-12:30 CT ~ 11:30-13:30 ET). The time gate IS the experiment: does
    reversion that fails all-day work when the market is at its quietest?"""

    name = "Lunch-reversion"
    LULL_START = dtime(10, 30)
    LULL_END = dtime(12, 30)
    STRETCH_ATR = 1.5
    RSI_OS = 15
    RSI_OB = 85

    def entry(self, i, row, prev_row, bar_2, ctx):
        t = ctx.ts_ct.time()
        if not (self.LULL_START <= t < self.LULL_END):
            return None
        vwap, close, atr, rsi2 = prev_row["vwap"], prev_row["close"], prev_row["atr"], prev_row["rsi2"]
        if not _ok(vwap, close, atr, rsi2) or float(atr) <= 0:
            return None
        price = float(row["open"])
        vwap = float(vwap)
        if (vwap - float(close)) >= self.STRETCH_ATR * float(atr) and float(rsi2) < self.RSI_OS and vwap > price:
            stop = _cap_long_stop(price, float(prev_row["low"]) - TICK)
            if price - stop <= 0:
                return None
            return EntrySignal("long", stop, self.name, exit_on_trend_flip=False, target_price=vwap)
        if (float(close) - vwap) >= self.STRETCH_ATR * float(atr) and float(rsi2) > self.RSI_OB and vwap < price:
            stop = _cap_short_stop(price, float(prev_row["high"]) + TICK)
            if stop - price <= 0:
                return None
            return EntrySignal("short", stop, self.name, exit_on_trend_flip=False, target_price=vwap)
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_choppy_strategies.py -k lunch -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/test_choppy_strategies.py src/strategies.py
git commit -m "Add LunchLullReversion choppy candidate (midday-gated VWAP fade)"
```

---

## Task 5: Register the four candidates in `build_candidates()`

**Files:**
- Modify: `tests/test_choppy_strategies.py` (add registration test)
- Modify: `src/strategies.py:381-392` (the `build_candidates` function)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_choppy_strategies.py`:

```python
def test_new_candidates_registered():
    names = {c.name for c in strategies.build_candidates()}
    for expected in ("Range-edge-fade", "Squeeze-breakout", "OR-fade", "Lunch-reversion"):
        assert expected in names
        assert strategies.build_strategy(expected).name == expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_choppy_strategies.py -k registered -v`
Expected: FAIL on the first `assert expected in names` (new names not yet in the menu).

- [ ] **Step 3: Add the four to `build_candidates()`**

In `src/strategies.py`, change the `build_candidates` return list to append the new classes:

```python
def build_candidates():
    """Full menu of individual strategies — what the edge grid evaluates per regime."""
    return [
        VWAPCross(),
        VWAPPullback(),
        ORBreakout(),
        FVGTrend(require_displacement=False),
        FVGTrend(require_displacement=True),
        MeanReversionVWAP(),
        KeltnerRSIFade(),
        TurtleSoup(),
        RangeEdgeFade(),
        SqueezeBreakout(),
        OpeningRangeFade(),
        LunchLullReversion(),
    ]
```

- [ ] **Step 4: Run the FULL test file to verify everything passes**

Run: `python -m pytest tests/test_choppy_strategies.py -v`
Expected: all tests passed (3 + 2 + 2 + 2 + 1 = 10 passed).

- [ ] **Step 5: Commit**

```bash
git add tests/test_choppy_strategies.py src/strategies.py
git commit -m "Register four choppy-market candidates in build_candidates()"
```

---

## Task 6: Run the honest pipeline once and report

**Files:** none (analysis only).

- [ ] **Step 1: Sanity-check the synthetic pipeline still wires up**

Run: `python -X utf8 -m src.simulate 2>&1 | tail -40`
Expected: completes without error; the edge grid / robustness scorecard prints and now includes the four new candidate names. (Synthetic data — proves plumbing only, not edge.)

- [ ] **Step 2: Run the real-data evaluation ONCE**

Run: `python -X utf8 -m src.evaluate`
Expected: prints the edge grid (now 12 strategies), the rulebook, the sealed-holdout router result, and the combine Monte-Carlo report. Do NOT re-run with tweaked params — a single honest run only.

- [ ] **Step 3: Report the results honestly**

Summarise for the user, in plain language:
- **Edge grid:** did any of the four new candidates earn positive cost-adjusted $/trade in the `chop` (or `low_vol`) regime, with ≥25 trades?
- **Rulebook:** did any new candidate win a regime cell and get assigned?
- **Holdout + combine:** if the rulebook changed, did P(pass) / P(payout) improve vs the current baseline (no-discipline router: ~22.5% P(pass), ~0.6% P(payout), negative E[net])?
- **Verdict:** state plainly whether any new idea clears costs and lifts P(pass). A trustworthy "none of these beat the costs" is an acceptable, reportable outcome — do not dress up a weak result, and do not tune to improve it.

- [ ] **Step 4: Commit any notes (optional)**

If you capture a short results summary in a markdown note, commit it. Otherwise no commit — Task 6 is analysis.

---

## Self-Review Notes (author check)

- **Spec coverage:** All four spec candidates → Tasks 1-4; registration → Task 5; single honest run + report → Task 6. Acceptance bar carried into Task 6 Step 3.
- **Type/name consistency:** Class names and `.name` strings match across tests, classes, and `build_candidates()` / `build_strategy()` (`Range-edge-fade`, `Squeeze-breakout`, `OR-fade`, `Lunch-reversion`).
- **Interface consistency:** All classes use the existing `_cap_long_stop`/`_cap_short_stop`/`_ok`/`EntrySignal`/`TICK`/`dtime` already defined in `src/strategies.py`. Fades set `exit_on_trend_flip=False` + `target_price`; the breakout uses the shared 2R target. The harness already skips a signal whose `target_price` is on the wrong side of entry, so no extra guard is needed.
- **No harness changes:** Confirmed — only `src/strategies.py` and a new test file are touched.
