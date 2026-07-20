# Feedback Loop Lab — research only

**Question:** can a feedback loop make the bot better, or does it overreact (hunt)
and make performance worse?

**Thermostat framing:** Mode A = heater on a timer (never adjusts). Mode B = twitchy
thermostat (narrow deadband, reacts to every wiggle — built to expose hunting).
Mode C = disciplined thermostat (wide deadband, statistical evidence + cooldowns).

## Safety guarantees
- Production V29 untouched. This lab only **reads** `src/bot.py` (frozen) once to
  produce a canonical trade stream, then replays it. Writes stay inside this folder.
- All modes replay the **same** stream (same data/fills/costs/equity/seed=42);
  only the feedback overlay differs. No lookahead — decisions use completed trades only.
- Every knob is bounded: default / min / max / max-step / cooldown / min-sample /
  deadband, and every change is logged with a reason code + plain-English explanation
  (`adjustments_log.csv`).
- Knobs that would change FILLS (stop/target aggressiveness, delayed entries/exits)
  are **excluded by design** — they'd break the shared-stream rule and need a
  bar-level re-sim. Time-of-day/news multipliers: redundant (core already gates them).
  Slippage-drift cooldown: untestable in replay (no slippage variance to sense).

## How to run (Windows)
```
cd "C:\CLAUDE TRADING BOT"
python -X utf8 -m experiments.feedback_loop_lab.feedback_lab
```
First run builds `trade_stream.csv` from the frozen core (~1–2 min); re-runs are instant.

## Files
- `feedback_lab.py` — the whole experiment (modes, knobs, controller, metrics,
  hunting score, period splits, walk-forward, stress battery)
- `trade_stream.csv` — canonical frozen-core trade stream (2,697 trades, 2019–2026)
- `adjustments_log.csv` — every adjustment: trade index, mode, param, old/new,
  reason, sample size, recent P&L/expectancy/WR/drawdown, deadband, direction,
  thermostat explanation

## Verdict (2026-07-06 run, fixed harness)
- **Mode B hunted, confirmed:** 1,172 adjustments, 425 reversals, net −40% vs open
  loop, killed 70% of trades. Its "better" PF/Sharpe is trade-count amputation.
- **Mode C did NOT beat open loop on net** (−20%) but cut max drawdown ~15–23%
  **consistently in all three periods** with only 68 calm adjustments.
- **Only promising knob:** rolling-drawdown cooldown at wide deadband
  (−1.2% net for −20% maxDD, 17 adjustments, hunting 2.6). Candidate for a
  combine-breach-risk study; advisory at most.
- **Permanently rejected:** score-based skip as feedback (deletes profitable
  low-score trades), size risk-UP (leverage in a costume: +$115k net but +33%
  drawdown), streak-based session caps (pure hunting).
- **Stays research-only.** The human-facing equivalent already exists in
  production: PLAYBOOK tripwires + drift_report.py = a wide-deadband thermostat
  with the human as the actuator.
