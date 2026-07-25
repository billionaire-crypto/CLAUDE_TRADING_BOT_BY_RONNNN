# MNQ Bot — Operating Playbook

**Read this before you touch anything. Especially when you're stressed.**
Last updated: 2026-07-24. Config: V29 FVG, `vwap_only` bias, ATR targets (2.0×, cap 240t), breakeven live, trail OFF, CALM_ATR_RATIO 0.70 (gauntlet-validated change, 07-07).
All config values above re-verified against `src/bot.py` on 2026-07-24.
Backtest baseline re-generated 2026-07-24 from the live config:
**2,877 trades | net $429,763.60 | PF 4.06 | win rate 29.06% | worst trade −$983.40 | 2019-05-06 → 2026-07-01.**

---

## 🚨 EMERGENCY — stop the bot RIGHT NOW
- **Telegram:** send `/halt` → it stops trading and closes any open trade next cycle.
- **Or** create a file named `HALT.txt` in `src\exports\`.
- To restart trading: `/resume` (or delete `HALT.txt`).
- The halt **survives restarts** — the bot stays stopped until you clear it.

---

## What to EXPECT live (so normal doesn't feel like broken)

| Thing | Number | Note |
|---|---|---|
| Win rate | **~29%** (recent ~31–36%) | You LOSE ~7 of 10 trades. **This is by design.** |
| Trades per day | **~1.2–1.3 in the current regime** (7-yr avg 1.5) | ~**1 in 3** days: **zero trades** (normal NOW — the market supplies fewer pullback days each year: idle rate 13%→32% from 2019→2026. Measured, not a malfunction). |
| Combine pass | **~17–25 trading days** (slower than the 17-day median, which assumed the older, busier regime) | ~98% *per the simulator* — but see the warning below. Treat as an upper bound, not a forecast. |
| Profit factor | **4.06 backtest** → **plan for ~3 live** | Real fills are worse than backtests. Per-trade edge has RISEN as frequency fell. |

> ⚠️ **The 97.7% pass rate is optimistic and you should not plan around it.**
> `research/run_combine_sim.py:59` draws each simulated day independently
> (`RNG.integers(0, n)`). Real losing days cluster — regimes persist — so an
> independent-draw bootstrap under-samples exactly the losing streak that
> breaches the trailing drawdown. A block bootstrap (contiguous 5–20 day blocks)
> would give a lower and more honest number; that rewrite has **not** been done
> yet, so the size of the gap is unquantified.
> For contrast, the separate clean-data study in `[[project_strategy_lab]]`
> judged the same strategy family at **P(pass) 22.5%, P(payout) 0.6%**. The truth
> is very likely between the two, and nobody has yet resolved which is closer.

**The single most important mindset:** this is a *"lose small, win big"* machine. A few huge winners pay for everything (top 10% of trades = 85% of profit). So:
- **A losing streak of 5–7 in a row is NORMAL. Do not panic-halt.**
- **Missing a big-winner day is the worst thing that can happen** — keep the PC on and the bot healthy, especially on trending days.

---

## Daily discipline (the rules that keep you from wrecking it)

1. **Never loosen entry rules to "trade more."** Every frequency shortcut has been tested and rejected. The selectivity IS the edge.
2. **Don't change the config** until you have ~30 live trades of real evidence. It's frozen for a reason.
3. **Don't judge the bot on one day, or one week.** Judge it on ~30+ trades.
4. **A quiet day is the strategy working, not failing.**
5. **Check the PC every morning** — it must be ON and LOGGED IN, or the bot can't run (auto-starts 6:20 AM PT).

---

## 🚧 TRIPWIRES — when to ACTUALLY worry (not before)

- **8 quiet sessions in a row** (no trades) → beyond the 7-year record. Stop, investigate (feed vs. market), don't just wait.
- **Trade rate stays below ~0.6/day over 30 sessions** → the backtest overstates live opportunity. Stop, re-validate, resize expectations.
- **Live win rate far below ~25% over 30+ trades** → investigate before resuming. **This one is NOT automatic — you have to notice it.**
- If a tripwire fires: **halt, don't tinker.** Diagnose first.

### What the slippage guards actually do (corrected 2026-07-24)

Three separate guards, in the order they can fire:

| Guard | Threshold | Action | Fires after |
|---|---|---|---|
| Single-fill halt | **≥ 32 ticks** adverse (= `STOP_TICKS`) | **Auto-halts** | 1 fill |
| Single-fill alert | **≥ 8 ticks** adverse | Telegram only, keeps trading | 1 fill |
| Rolling-average halt | avg **> 3 ticks** adverse | **Auto-halts** | 20 fills |

**Read the word "adverse" literally.** A fill that lands *better* than intended
records a NEGATIVE number and pulls the average down. Until 2026-07-24 this used
`abs()`, so price improvement counted as slippage — two real Jul 22 fills were
logged as "19 ticks" and "3 ticks" of slippage when both were actually better
than asked. Any slippage figure in a log dated before 2026-07-24 is inflated and
should not be quoted.

**Previous versions of this file claimed the bot auto-halts at a >3 tick average.
That was wrong in practice** — the rolling guard needs a full 20-fill window, and
at the live rate (~1.3 trades/day) that takes 15+ trading days to arm. One
catastrophic fill passed through unnoticed on 2026-07-22 for exactly this reason.
The single-fill guards above were added to close that gap.

**Known reality check on slippage:** as of 2026-07-24 only **one** fill pair has
ever been measured on trustworthy (single-instance, post-parser-fix) data — the
Jul 24 trade, at 3 ticks each way, one favorable and one adverse. Everything
earlier is either unmeasured (the 11 trades of Jul 15-20, when the hub parser was
blind) or corrupted (Jul 22, when multiple bot processes overwrote each other's
state). **There is not yet enough data to state a live slippage average.** Do not
quote one until ~10-20 clean fills have accumulated.

---

## 💰 PAYOUT FRAMEWORK (for when you're FUNDED — not yet!)

**You're on a Combine now. There are NO payouts until you pass and get a funded account.** This is for later.

### The core mechanic (Topstep $50k)
- Your loss floor (MLL) **trails up** as you profit, until your end-of-day balance hits **~$52,000** — then it **LOCKS at $50,000** permanently. Below $50k you can never go.

### When to withdraw
1. **Before the floor locks (balance < $52k):** ❌ don't withdraw. You're still building the cushion; pulling money keeps the floor chasing you.
2. **After it locks (balance > $52k) AND you have your 5 qualifying days (≥$150 profit each):** ✅ safe to withdraw — but keep a buffer.

### How much to withdraw
> **Withdraw = current balance − $50,000 floor − ~$2,500 buffer**
> (i.e. keep ~$52,500 in the account, take the rest)

- The **~$2,500 buffer** = your worst realistic losing streak (measured worst day ~-$983; a rough patch can stack to ~-$2,000). It's the shock absorber that stops a bad week from hitting the floor.
- **Never withdraw down to the floor.** A single normal drawdown would then blow the account.

### ⚠️ Before your first real payout
**Confirm the exact current rules on your Topstep dashboard** (qualifying-day amount, minimum buffer, first-payout caps — these change). Then ping me and we build a proper, rule-accurate payout calculator. Do NOT trust any hardcoded thresholds until verified against Topstep's live rules.

---

## 📈 SCALING (also for later)
- **Before increasing size on a funded account: bank the first ~$2,000** to lock the MLL at $50k first. Only scale from a locked floor.
- The strategy's catastrophic tail (a rare gap-through) only bites at *large* size. Small = safe; big = respect it.
- Prove ~30+ clean live trades before any size increase.

---

## The one-line summary
**Lose small, win big; a quiet day is fine; never loosen the rules; keep the PC awake; and when funded, keep ~$52.5k and withdraw the rest.**

*All numbers above are from backtest + simulation. Zero live trades have happened yet. Treat them as expectations to verify, not results in the bank.*
