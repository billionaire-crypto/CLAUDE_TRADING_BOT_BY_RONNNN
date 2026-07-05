# MNQ Bot — Operating Playbook

**Read this before you touch anything. Especially when you're stressed.**
Last updated: 2026-07-04. Config: V29 FVG, `vwap_only` bias, ATR targets (2.0×, cap 240t), breakeven live, trail OFF.

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
| Trades per day | **~1.5** | ~1 in 4 days: **zero trades** (normal). |
| Combine pass | **~17 trading days** (~3.5 weeks) | ~98% likely *if* live matches testing. |
| Profit factor | 4.10 backtest → **plan for ~3 live** | Real fills are worse than backtests. |

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
- **Live win rate far below ~25% over 30+ trades**, or **live slippage averaging >3 ticks** → the bot auto-halts on the slippage one; investigate before resuming.
- If a tripwire fires: **halt, don't tinker.** Diagnose first.

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
