# CLAUDE.md — MNQ V29 Trading Bot

Instructions for any AI session working in this repo. Read before making changes.

## 🔒 Production vs. Research — keep them separate

This is a **live trading bot**. Production code and research must not mix.

- **`src/` is production only.** The live bot runs `python -m src.topstepx_runtime
  run-loop --auto-submit`. Core files: `bot.py` (strategy + backtest engine),
  `topstepx_runtime.py` (live execution), `topstepx_client.py`, `load_data.py`.
  Do **not** add exploratory scripts, one-off analyses, or throwaway sweeps here.
- **`research/` holds everything exploratory** — backtest/sweep scripts
  (`run_*.py`), data-prep utilities, and research notes. It is a Python package;
  run its scripts from the repo root as modules, e.g.
  `python -m research.run_bias_validation`. Nothing in `src/` may import from
  `research/`.
- **`src/exports/` is operational runtime output** (live state file, logs, HALT.txt,
  fill forensics) and is gitignored. Never relocate it or move the live bot's paths.

## 📓 The Research Ledger is mandatory

**Every new backtest, hypothesis, or parameter experiment MUST be documented in
[`research/RESEARCH_LEDGER.md`](research/RESEARCH_LEDGER.md)** — with hypothesis,
method, result (net / PF / out-of-sample / walk-forward / stress / tail), and the
decision (shipped / rejected / parked). This includes **rejections**: a documented
dead end stops the next session from re-running it.

- Do **not** bury experiment results in code comments, commit messages, or scattered
  `.txt` outputs. The ledger is the single source of truth for research history.
- Do **not** change a live strategy parameter in `src/bot.py` without a corresponding
  ledger entry showing it passed the validation gauntlet (full backtest + out-of-sample
  + walk-forward + stress + combine sim). The config is frozen for a reason.

## ⚠️ Safety rules

- The bot trades real money on a Topstep combine. Any change touching entry logic,
  sizing, or live execution must be verified against **both** the backtest path and the
  live signal path, and covered by `tests/test_risk_and_safety.py`.
- Never commit `.env` (live API keys / Telegram token). Never send test output to the
  live Telegram — tests neutralize it via an autouse fixture; keep it that way.
- Operating guidance for the live bot is in [`PLAYBOOK.md`](PLAYBOOK.md).
