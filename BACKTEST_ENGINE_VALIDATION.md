# Backtest Engine Validation

Status: **RESEARCH-READY; LIVE ROUTING NOT AUTHORIZED**

This status applies to the current working tree and the validated historical
dataset fingerprint printed by `validate_loaded_data()`. It does not certify a
strategy as profitable and it does not prove that software is error-free.

Validated dataset:

- Rows: `143,232`
- Range: `2019-05-06T09:30:00-04:00` through `2026-07-02T16:00:00-04:00`
- SHA-256: `d50572f9140bc962280328801368f3b1ebea3bbf5f87cc4e85c1d7c3cee1e0ca`

## Engine modes

- Evaluation mode is the default. It enforces the trailing account floor and
  rejects a trade when even one contract cannot fit inside both daily-loss and
  trailing-floor headroom.
- Causality-analysis mode must be requested explicitly with
  `enforce_evaluation_floor=False`. It exists only to exercise strategy
  decisions after a simulated evaluation would have stopped. It must not be
  used to report evaluation pass/fail results or by live routing.

## Verified invariants

- Entries priced at a bar's open use only that open and completed prior bars.
- An FVG created on bar `i` is unavailable until bar `i+1`.
- Entry-bar stop and target touches are processed.
- Stop and target touched in one OHLC bar resolves stop-first.
- A stop gapped through at the bar open fills at the worse open plus configured
  adverse stop slippage.
- Intrabar exit cannot be followed by retroactive same-open re-entry.
- Fifteen-minute indicators are shifted until the aggregate is complete.
- ORB and opening-drive state use completed bars only.
- Evaluation floor failure is absorbing.
- Partial and final exits reconcile gross P&L, costs, net P&L, cash, and initial
  contract count.
- Historical and live order planning use `determine_trade_size()` with explicit
  account context.
- Sizing is recorded as strategy candidate, account-scaled size, and final
  risk-sized contracts.
- Executed size cannot exceed account-scaled size, candidate size, modeled
  daily-loss headroom, or modeled trailing-floor headroom.
- Historical and ProjectX-shaped bar payloads use the same canonical strategy
  pipeline and canonical Chicago timestamps.
- The historical loader uses causal pre-open rollover selection, carries the
  chosen contract across extension-file boundaries, rejects missing selected
  contract timestamps, and rejects malformed/incomplete sessions.

## Required acceptance commands

```powershell
python -X utf8 -m pytest -q
python -m py_compile src/bot.py src/load_data.py src/topstepx_runtime.py research/run_lookahead_audit.py research/run_live_backtest_parity.py
git diff --check
python -X utf8 -m src.bot
python -X utf8 -m research.run_live_backtest_parity --lookback 727
python -X utf8 -m research.run_lookahead_audit --bars 5000 --cuts 12 --all-entry-bars --prefix-cuts 2 --horizon 400
```

For later-era causality windows, also run:

```powershell
python -X utf8 -m research.run_lookahead_audit --start-bar 46000 --bars 5000 --cuts 8 --prefix-cuts 1 --horizon 400
python -X utf8 -m research.run_lookahead_audit --start-bar 92000 --bars 5000 --cuts 8 --prefix-cuts 1 --horizon 400
python -X utf8 -m research.run_lookahead_audit --start-bar 138000 --bars 5000 --cuts 8 --prefix-cuts 1 --horizon 400
```

## Known model limits

- Five-minute OHLC cannot reveal the true sequence of events within a bar.
  Stop-first is deliberately conservative, not a reconstruction of tick order.
- A stop crossed intrabar is filled at the stop plus configured slippage. Only
  an opening gap is priced directly at the worse open. Tick or order-book data
  is required to validate fast-market stop fills more precisely.
- ProjectX partial-bar timestamp semantics are fail-closed in code but still
  require a read-only broker integration test before live routing.
- Historical causal rollover selection and the broker's active-contract switch
  can differ. This must be measured at each rollover before live routing.
- Every new strategy module requires its own behavioral causality fixtures.
  Passing the engine tests does not automatically certify newly added strategy
  code.

## Current retired strategy

V29 remains rejected. The hardened engine is intended for researching a new
strategy, not for re-enabling V29.
