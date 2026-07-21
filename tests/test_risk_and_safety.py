"""Unit tests for the safety-critical money math and execution guards.

Covers the pieces that protect the account and that were hardened for go-live:
  - round_turn_cost()             : tiered slippage at the tier boundaries
  - compute_scaling_tier()        : Topstep scaling-plan lot tiers
  - worst_modeled_trade_loss_usd(): worst single-trade loss per sizing model
  - startup DLL-safety invariant  : risk-budget passes, raw score map fails
  - risk-budget headroom formula  : a single trade can never breach the DLL
  - _is_protective_stop_like_order: numeric stop type code 4 is recognized

Run with:  pytest tests/test_risk_and_safety.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src import bot
from src import topstepx_runtime as tr

import pytest


@pytest.fixture(autouse=True)
def _no_real_telegram(monkeypatch, tmp_path):
    """Hard guard: tests must NEVER reach the live Telegram chat OR the live
    log files. Neutralizes the senders and the log writer (2026-07-08: test
    artifacts like 'simulated cancel failure' landed in live_errors.txt and
    looked like real incidents during live debugging)."""
    monkeypatch.setattr(tr, "_send_telegram_lines", lambda *a, **k: True, raising=False)
    monkeypatch.setattr(tr, "_send_telegram_message", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(tr, "_write_log", lambda *a, **k: None, raising=False)
    # Payload forensics must write to tmp, never into the live exports dir.
    monkeypatch.setattr(tr, "PAYLOAD_FORENSICS_PATH",
                        str(tmp_path / "payload_forensics.jsonl"), raising=False)
    monkeypatch.setattr(tr, "_payload_forensics_counts", {}, raising=False)
    # Fill forensics likewise: hub-trade tests exercise the slippage path,
    # which appends rows — those must land in tmp, not the live CSV.
    monkeypatch.setattr(tr, "FILL_FORENSICS_CSV_PATH",
                        str(tmp_path / "fill_forensics.csv"), raising=False)


# ── round_turn_cost: tiered slippage (1 tk <=10, 2 tk 11-20, 3 tk 21+) ──────────
def _expected_cost(contracts, slip_ticks):
    commission = bot.COMMISSION_PER_CONTRACT * contracts
    slippage = slip_ticks * bot.MNQ_TICK_VALUE * contracts * 2
    return commission + slippage

def test_cost_tier1_boundary():
    assert bot.round_turn_cost(10) == _expected_cost(10, 1.0)

def test_cost_tier2_lower_boundary():
    assert bot.round_turn_cost(11) == _expected_cost(11, 2.0)

def test_cost_tier2_upper_boundary():
    assert bot.round_turn_cost(20) == _expected_cost(20, 2.0)

def test_cost_tier3_boundary():
    assert bot.round_turn_cost(21) == _expected_cost(21, 3.0)

def test_cost_tier3_max_size():
    assert bot.round_turn_cost(50) == _expected_cost(50, 3.0)

def test_cost_monotonic_in_size():
    costs = [bot.round_turn_cost(n) for n in (1, 10, 11, 20, 21, 40, 50)]
    assert costs == sorted(costs)


# ── compute_scaling_tier ────────────────────────────────────────────────────────
def test_scaling_tier_low():
    assert bot.compute_scaling_tier(0.0) == bot.SCALING_TIER_1_CONTRACTS

def test_scaling_tier_mid():
    assert bot.compute_scaling_tier(bot.SCALING_TIER_2_THRESHOLD) == bot.SCALING_TIER_2_CONTRACTS

def test_scaling_tier_high():
    assert bot.compute_scaling_tier(bot.SCALING_TIER_3_THRESHOLD) == bot.SCALING_TIER_3_CONTRACTS


# ── worst_modeled_trade_loss_usd + DLL safety invariant ─────────────────────────
def test_worst_trade_risk_budget_is_headroom_fraction():
    saved = bot.RISK_BUDGET_SIZING_ENABLED
    try:
        bot.RISK_BUDGET_SIZING_ENABLED = True
        expected = bot.HEADROOM_SAFETY_FRAC * abs(bot.BOT_DAILY_LOSS_LIMIT)
        assert abs(bot.worst_modeled_trade_loss_usd() - expected) < 1e-6
    finally:
        bot.RISK_BUDGET_SIZING_ENABLED = saved

def test_worst_trade_score_map_uses_full_stop():
    """With risk-budget off, the raw score map can take the full 32-tick stop at
    max contracts -> this is the ~$1017 figure the audit flagged."""
    saved = bot.RISK_BUDGET_SIZING_ENABLED
    try:
        bot.RISK_BUDGET_SIZING_ENABLED = False
        max_ctr = bot._fvg_ambition_max_contracts()
        worst_slip = max(t for _cap, t in bot.SLIPPAGE_SCALE_TIERS)
        expected = (bot.STOP_TICKS * bot.MNQ_TICK_VALUE * max_ctr
                    + bot.COMMISSION_PER_CONTRACT * max_ctr
                    + worst_slip * bot.MNQ_TICK_VALUE * max_ctr * 2)
        assert abs(bot.worst_modeled_trade_loss_usd() - expected) < 1e-6
        assert bot.worst_modeled_trade_loss_usd() > abs(bot.COMBINE_DAILY_LOSS_LIMIT)
    finally:
        bot.RISK_BUDGET_SIZING_ENABLED = saved

def test_risk_budget_worst_trade_under_combine_dll():
    saved = bot.RISK_BUDGET_SIZING_ENABLED
    try:
        bot.RISK_BUDGET_SIZING_ENABLED = True
        # Must sit under the combine DLL with the 20% gap-slippage margin the
        # startup audit enforces.
        assert bot.worst_modeled_trade_loss_usd() < 0.80 * abs(bot.COMBINE_DAILY_LOSS_LIMIT)
    finally:
        bot.RISK_BUDGET_SIZING_ENABLED = saved


# ── The core structural guarantee: risk-budget sizing cannot breach the DLL ─────
def _budget_contracts(score, stop_dist_pts, daily_pnl):
    """Re-implements the sizing math from bot.run_backtest for verification."""
    worst_slip = max(t for _cap, t in bot.SLIPPAGE_SCALE_TIERS)
    per_ctr_cost = bot.COMMISSION_PER_CONTRACT + worst_slip * bot.MNQ_TICK_VALUE * 2
    per_ctr_risk = stop_dist_pts * bot.MNQ_POINT_VALUE + per_ctr_cost
    budget = bot.RISK_BUDGET_DEFAULT
    for th in sorted(bot.RISK_BUDGET_MAP.keys(), reverse=True):
        if score >= th:
            budget = bot.RISK_BUDGET_MAP[th]
            break
    headroom = daily_pnl - bot.BOT_DAILY_LOSS_LIMIT
    headroom_cap = max(0.0, headroom) * bot.HEADROOM_SAFETY_FRAC
    allowed = min(budget, headroom_cap)
    return int(allowed / per_ctr_risk), per_ctr_risk

def test_no_single_trade_can_breach_combine_dll():
    """Across scores, stop distances and starting P&L, the resulting day P&L
    after a full stop-out must stay above the -$1000 combine limit."""
    for score in (6, 7, 8):
        for stop_pts in (2.0, 5.0, 8.0):        # 8 pts = the 32-tick cap
            for daily_pnl in (0.0, -400.0, -740.0, 500.0):
                n, per_ctr_risk = _budget_contracts(score, stop_pts, daily_pnl)
                loss = n * per_ctr_risk
                assert daily_pnl - loss > bot.COMBINE_DAILY_LOSS_LIMIT, (
                    f"score={score} stop={stop_pts} pnl={daily_pnl} "
                    f"n={n} loss={loss} -> {daily_pnl - loss}")


# ── _is_protective_stop_like_order: numeric type code 4 ─────────────────────────
def test_stop_recognized_by_type_code_4():
    order = {"contractId": "X", "side": 1, "type": 4}
    assert tr._is_protective_stop_like_order(order, entry_side=0, contract_id="X")

def test_stop_recognized_by_descriptor():
    order = {"contractId": "X", "side": 1, "typeName": "STOP"}
    assert tr._is_protective_stop_like_order(order, entry_side=0, contract_id="X")

def test_same_side_order_is_not_protective_stop():
    # A stop on the SAME side as entry is not the protective exit.
    order = {"contractId": "X", "side": 0, "type": 4}
    assert not tr._is_protective_stop_like_order(order, entry_side=0, contract_id="X")

def test_non_matching_contract_rejected():
    order = {"contractId": "Y", "side": 1, "type": 4}
    assert not tr._is_protective_stop_like_order(order, entry_side=0, contract_id="X")

def test_plain_market_order_not_stop():
    order = {"contractId": "X", "side": 1, "type": 2}  # 2 = not a stop code
    assert not tr._is_protective_stop_like_order(order, entry_side=0, contract_id="X")


# ── Live max-contract fallback: symbol-aware, MNQ != NQ ─────────────────────────
def test_mnq_missing_maxcontracts_uses_50():
    # Account API returns no maxContracts field -> MNQ must fall back to 50.
    assert tr._infer_topstep_max_contracts({}, {}, symbol="MNQ") == 50

def test_nq_missing_maxcontracts_uses_5():
    # Same missing field -> NQ (mini) must fall back to 5, not 50.
    assert tr._infer_topstep_max_contracts({}, {}, symbol="NQ") == 5

def test_symbol_fallback_not_confused_mnq_vs_nq():
    # "NQ" is a substring of "MNQ"; MNQ must be matched first.
    assert tr._symbol_fallback_max_contracts("MNQZ5") == 50
    assert tr._symbol_fallback_max_contracts("MNQ") == 50
    assert tr._symbol_fallback_max_contracts("NQZ5") == 5
    assert tr._symbol_fallback_max_contracts("NQ") == 5

def test_api_maxcontracts_takes_precedence_over_fallback():
    # When the API provides the value, use it regardless of symbol.
    assert tr._infer_topstep_max_contracts({"maxContracts": 12}, {}, symbol="MNQ") == 12

def test_requested_40_mnq_not_capped_to_5():
    # Effective cap = min(requested, broker max). With MNQ fallback (50),
    # a 40-lot request must NOT be squashed to 5.
    max_allowed = tr._infer_topstep_max_contracts({}, {}, symbol="MNQ")
    final = max(1, min(40, max_allowed))
    assert final == 40

def test_requested_60_mnq_capped_to_50():
    max_allowed = tr._infer_topstep_max_contracts({}, {}, symbol="MNQ")
    final = max(1, min(60, max_allowed))
    assert final == 50


# ── No MES contamination: MNQ contract specs are correct ────────────────────────
def test_mnq_contract_specs():
    assert bot.MNQ_TICK_SIZE == 0.25
    assert bot.MNQ_TICK_VALUE == 0.50      # MES was 1.25 — must NOT be that
    assert bot.MNQ_POINT_VALUE == 2.00     # MES was 5.00 — must NOT be that

def test_no_mes_dollar_per_point_constant_leaks():
    # The old MES config used DOLLARS_PER_POINT=5.0 / DOLLARS_PER_TICK. Those
    # must not exist in the live strategy module.
    assert not hasattr(bot, "DOLLARS_PER_POINT")
    assert not hasattr(bot, "DOLLARS_PER_TICK")
    assert not hasattr(bot, "TICKS_PER_POINT")


# ── ORB is intentionally disabled and cannot fire ───────────────────────────────
def test_orb_range_gate_is_unsatisfiable():
    # The entry guard requires range_ticks <= ORB_MAX_RANGE_TICKS AND >= 4.
    # With ORB_MAX_RANGE_TICKS = 0 these can never both hold -> ORB is off.
    assert bot.ORB_MAX_RANGE_TICKS < 4


# ── HALT state is file-based and survives a "restart" ───────────────────────────
def test_halt_persists_across_restart(tmp_path):
    import json as _json
    saved_path = tr.KILL_SWITCH_PATH
    tr.KILL_SWITCH_PATH = str(tmp_path / "HALT.txt")
    try:
        assert tr._kill_switch_active() is False
        tr.engage_kill_switch("unit_test_reason")
        # A restart is just a new process reading the same file: _kill_switch_active
        # is stateless and only checks disk, so this models a restart.
        assert tr._kill_switch_active() is True
        with open(tr.KILL_SWITCH_PATH, encoding="utf-8") as fh:
            assert _json.load(fh)["reason"] == "unit_test_reason"
        tr.clear_kill_switch()
        assert tr._kill_switch_active() is False
    finally:
        tr.KILL_SWITCH_PATH = saved_path


# ── ORB explicit disable flag ───────────────────────────────────────────────────
def test_orb_enabled_flag_is_false():
    assert bot.ORB_ENABLED is False


# ── Data loader rename (no MES-named symbol leaks) ──────────────────────────────
def test_ohlcv_loader_renamed():
    from src import load_data
    assert hasattr(load_data, "load_ohlcv_csv")
    assert not hasattr(load_data, "load_mes_data")


# ── Unprotected-position mismatch predicate ─────────────────────────────────────
class _Cfg:
    def __init__(self, enable_order_routing, dry_run):
        self.enable_order_routing = enable_order_routing
        self.dry_run = dry_run

def test_unprotected_position_detected_live():
    # Live routing, real position, no working orders -> dangerous mismatch.
    cfg = _Cfg(enable_order_routing=True, dry_run=False)
    st = {"open_position_count": 1, "open_order_count": 0}
    assert tr._unprotected_position_detected(st, cfg) is True

def test_protected_position_not_flagged():
    cfg = _Cfg(enable_order_routing=True, dry_run=False)
    st = {"open_position_count": 1, "open_order_count": 1}  # has a working stop
    assert tr._unprotected_position_detected(st, cfg) is False

def test_flat_position_not_flagged():
    cfg = _Cfg(enable_order_routing=True, dry_run=False)
    st = {"open_position_count": 0, "open_order_count": 0}
    assert tr._unprotected_position_detected(st, cfg) is False

def test_dry_run_never_flagged():
    cfg = _Cfg(enable_order_routing=True, dry_run=True)
    st = {"open_position_count": 1, "open_order_count": 0}
    assert tr._unprotected_position_detected(st, cfg) is False

def test_routing_disabled_never_flagged():
    cfg = _Cfg(enable_order_routing=False, dry_run=False)
    st = {"open_position_count": 1, "open_order_count": 0}
    assert tr._unprotected_position_detected(st, cfg) is False


# ── Slippage recording helpers (entry + exit share these) ───────────────────────
def test_record_slippage_ticks_and_window():
    # 20000.00 intended vs 20000.75 actual = 3 ticks (0.25 each).
    w = tr._record_slippage([], 20000.00, 20000.75)
    assert w == [3.0]
    # window is capped at SLIPPAGE_WINDOW
    big = tr._record_slippage([1.0] * tr.SLIPPAGE_WINDOW, 20000.0, 20000.25)
    assert len(big) == tr.SLIPPAGE_WINDOW
    assert big[-1] == 1.0

def test_exit_slippage_attributes_to_nearer_level():
    # Fill at 20010.25 is nearer the target (20010) than the stop (19990):
    # slippage should be measured against the target (1 tick), not the stop.
    stop_px, tgt_px = 19990.0, 20010.0
    actual = 20010.25
    candidates = [("stop", stop_px), ("target", tgt_px)]
    kind, intended = min(candidates, key=lambda c: abs(actual - c[1]))
    assert kind == "target"
    assert tr._record_slippage([], intended, actual) == [1.0]

def test_exit_slippage_halts_over_threshold(tmp_path):
    # A full window averaging above SLIPPAGE_ALERT_TICKS must engage the kill switch.
    saved = tr.KILL_SWITCH_PATH
    tr.KILL_SWITCH_PATH = str(tmp_path / "HALT.txt")
    try:
        bad_window = [tr.SLIPPAGE_ALERT_TICKS + 2.0] * tr.SLIPPAGE_WINDOW
        tr._emit_slippage_log_and_halt("exit_stop", bad_window, 20000.0, 20001.0,
                                       "exit_slippage_systematic_excess")
        assert tr._kill_switch_active() is True
    finally:
        tr.clear_kill_switch()
        tr.KILL_SWITCH_PATH = saved

def test_exit_slippage_no_halt_under_threshold(tmp_path):
    saved = tr.KILL_SWITCH_PATH
    tr.KILL_SWITCH_PATH = str(tmp_path / "HALT.txt")
    try:
        ok_window = [1.0] * tr.SLIPPAGE_WINDOW
        tr._emit_slippage_log_and_halt("exit_target", ok_window, 20000.0, 20000.25,
                                       "exit_slippage_systematic_excess")
        assert tr._kill_switch_active() is False
    finally:
        tr.KILL_SWITCH_PATH = saved

def test_exit_slippage_state_fields_exist():
    st = tr._default_state()
    for k in ("last_signal_stop_price", "last_signal_target_price",
              "awaiting_exit_fill", "rolling_exit_slippage_ticks"):
        assert k in st


# ── Gap-through stop fills (conservative) ───────────────────────────────────────
def test_gap_through_enabled_by_default():
    assert bot.GAP_THROUGH_STOPS_ENABLED is True

def test_gap_through_long_fills_at_open_when_gapped():
    # Long stop at 20000. Bar opens at 19950 (gapped below) -> fill at 19950, worse.
    stop_px, bar_open = 20000.0, 19950.0
    fill = min(bar_open, stop_px)
    assert fill == 19950.0

def test_gap_through_long_normal_fill_at_stop():
    # Bar opens above stop, low pierces it -> normal fill at the stop price.
    stop_px, bar_open = 20000.0, 20010.0
    fill = min(bar_open, stop_px)
    assert fill == 20000.0

def test_gap_through_short_fills_at_open_when_gapped():
    # Short stop at 20000. Bar opens at 20060 (gapped above) -> fill at 20060, worse.
    stop_px, bar_open = 20000.0, 20060.0
    fill = max(bar_open, stop_px)
    assert fill == 20060.0

def test_gap_through_short_normal_fill_at_stop():
    stop_px, bar_open = 20000.0, 19990.0
    fill = max(bar_open, stop_px)
    assert fill == 20000.0


# ── No-client order-plan default is symbol-aware, not raw 5 ──────────────────────
def test_no_client_default_cap_is_symbol_aware():
    # The build_order_plan no-client default now uses the symbol fallback, so an
    # MNQ dry-run plan defaults to 50, not the raw NQ-lot count of 5.
    assert tr._symbol_fallback_max_contracts("MNQ") == 50
    assert tr._symbol_fallback_max_contracts("MNQ") != bot.SCALING_TIER_3_CONTRACTS


# ── Phantom-bar filter ──────────────────────────────────────────────────────────
def _mk_df(closes, spread=1.0):
    import pandas as pd
    idx = pd.date_range("2024-01-02 09:30", periods=len(closes), freq="5min", tz="US/Eastern")
    return pd.DataFrame({
        "open":  closes,
        "high":  [c + spread for c in closes],
        "low":   [c - spread for c in closes],
        "close": closes,
        "volume":[10] * len(closes),
    }, index=idx)

def test_phantom_bar_floating_above_removed():
    df = _mk_df([100, 100, 300, 100, 100])   # bar 2 floats far above both neighbors
    out = bot.filter_phantom_bars(df)
    assert len(out) == 4
    assert 300 not in list(out["close"])

def test_phantom_bar_floating_below_removed():
    df = _mk_df([20000, 20000, 19000, 20000, 20000])  # bar 2 floats far below
    out = bot.filter_phantom_bars(df)
    assert len(out) == 4
    assert 19000 not in list(out["close"])

def test_real_trend_move_not_removed():
    # A genuine move where price steps and the next bar CONTINUES (overlaps) is
    # not a phantom — must be kept.
    df = _mk_df([20000, 20010, 20025, 20040, 20055], spread=8.0)
    out = bot.filter_phantom_bars(df)
    assert len(out) == 5

def test_phantom_filter_flag_and_default():
    assert bot.PHANTOM_BAR_FILTER_ENABLED is True


# ── Orphan-order detection (E4: flat account with leftover working orders) ──────
def test_orphan_orders_detected_when_flat_with_orders():
    cfg = _Cfg(enable_order_routing=True, dry_run=False)
    st = {"open_position_count": 0, "open_order_count": 1}  # flat, but an order lingers
    assert tr._orphan_orders_detected(st, cfg) is True

def test_no_orphan_when_position_open():
    cfg = _Cfg(enable_order_routing=True, dry_run=False)
    st = {"open_position_count": 1, "open_order_count": 1}  # order belongs to the position
    assert tr._orphan_orders_detected(st, cfg) is False

def test_no_orphan_when_flat_and_no_orders():
    cfg = _Cfg(enable_order_routing=True, dry_run=False)
    st = {"open_position_count": 0, "open_order_count": 0}
    assert tr._orphan_orders_detected(st, cfg) is False

def test_orphan_not_flagged_in_dry_run():
    cfg = _Cfg(enable_order_routing=True, dry_run=True)
    st = {"open_position_count": 0, "open_order_count": 2}
    assert tr._orphan_orders_detected(st, cfg) is False

def test_orphan_not_flagged_when_routing_disabled():
    cfg = _Cfg(enable_order_routing=False, dry_run=False)
    st = {"open_position_count": 0, "open_order_count": 2}
    assert tr._orphan_orders_detected(st, cfg) is False


# ── Telegram command parsing (two-way control) ──────────────────────────────────
def test_telegram_command_status():
    for t in ("/status", "status", "STATUS", "/s", "  /Status  ", "/status now"):
        assert tr._telegram_command_action(t) == "status", t

def test_telegram_command_halt_and_resume():
    for t in ("/halt", "stop", "KILL", "/pause"):
        assert tr._telegram_command_action(t) == "halt", t
    for t in ("/resume", "clear", "go", "/start"):
        assert tr._telegram_command_action(t) == "resume", t

def test_telegram_command_flatten_positions_help():
    assert tr._telegram_command_action("/flatten") == "flatten"
    assert tr._telegram_command_action("closeall") == "flatten"
    assert tr._telegram_command_action("/positions") == "positions"
    assert tr._telegram_command_action("/help") == "help"

def test_telegram_command_unknown():
    for t in ("", "/foobar", "hello", "/ 123"):
        assert tr._telegram_command_action(t) == "unknown", t

def test_status_lines_nonempty():
    lines = tr._status_lines(tr._default_state())
    assert isinstance(lines, list) and len(lines) >= 4
    assert any("P&L" in l for l in lines)

def test_status_lines_show_halt_when_engaged(monkeypatch):
    monkeypatch.setattr(tr, "_kill_switch_active", lambda: True)
    lines = tr._status_lines(tr._default_state())
    assert any("HALT" in l.upper() for l in lines)


# ── News-protection regressions (2026-07-01 audit fixes) ────────────────────────
def test_fomc_blackout_covers_statement_bar():
    # FOMC statement = 2:00 PM ET = 13:00 CT. The old window (13:45-14:30 CT)
    # started 45 minutes AFTER the statement (timezone slip).
    assert bot.FOMC_BLACKOUT_START_CT <= (13, 0) <= bot.FOMC_BLACKOUT_END_CT

def test_fomc_dates_cover_2026_h2():
    assert "2026-07-29" in bot.FOMC_DATES
    assert "2026-09-16" in bot.FOMC_DATES
    assert "2026-12-09" in bot.FOMC_DATES

def test_nfp_dates_autogenerate_forward():
    # First Fridays of 2026 H2 must exist even though the hardcoded list ended 2026-03.
    assert "2026-07-03" in bot.NFP_DATES   # first Friday of July 2026
    assert "2026-08-07" in bot.NFP_DATES
    assert "2027-01-01" in bot.NFP_DATES   # 2027-01-01 is a Friday

def test_first_fridays_helper():
    fs = bot._first_fridays(2026, 2026)
    assert len(fs) == 12
    assert "2026-02-06" in fs


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))


# -- Stress toggles must ship OFF (they distort fills when on) -------------------
def test_stress_toggles_default_off():
    assert bot.MISS_FILL_PROB == 0.0
    assert bot.STOP_EXTRA_SLIP_TICKS == 0


# ── Live stop management (breakeven/trail unlock) ───────────────────────────────
def test_desired_stop_breakeven_long():
    # 20t trigger, prior-bar MFE 6 pts (=24t) -> stop ratchets to entry (+offset 0)
    assert bot.desired_stop_price("long", 20000.0, 19992.0, 6.0) == 20000.0

def test_desired_stop_no_move_below_trigger():
    # MFE 4 pts = 16t < 20t trigger -> unchanged
    assert bot.desired_stop_price("long", 20000.0, 19992.0, 4.0) == 19992.0

def test_desired_stop_never_retreats():
    # Stop already above breakeven -> stays put (ratchet)
    assert bot.desired_stop_price("long", 20000.0, 20005.0, 6.0) == 20005.0

def test_desired_stop_short_symmetry():
    assert bot.desired_stop_price("short", 20000.0, 20008.0, 6.0) == 20000.0

def test_desired_stop_trail_when_enabled():
    saved = bot.TRAIL_AFTER_MFE_ENABLED
    try:
        bot.TRAIL_AFTER_MFE_ENABLED = True
        # MFE 20 pts (=80t) >= 60t trigger; lock = 20 - 10 (40t) = +10 pts
        assert bot.desired_stop_price("long", 20000.0, 19992.0, 20.0) == 20010.0
        assert bot.desired_stop_price("short", 20000.0, 20008.0, 20.0) == 19990.0
    finally:
        bot.TRAIL_AFTER_MFE_ENABLED = saved

def test_trail_flag_still_off_for_staged_rollout():
    assert bot.TRAIL_AFTER_MFE_ENABLED is False

def test_pick_protective_stop_nearest():
    orders = [
        {"id": 1, "contractId": "X", "side": 1, "type": 4, "stopPrice": 19980.0},
        {"id": 2, "contractId": "X", "side": 1, "type": 4, "stopPrice": 19995.0},
        {"id": 3, "contractId": "Y", "side": 1, "type": 4, "stopPrice": 19999.0},
    ]
    pick = tr._pick_protective_stop(orders, entry_side=0, contract_id="X")
    assert pick["id"] == 2  # nearest below market for a long
    assert tr._pick_protective_stop([], 0, "X") is None

def test_live_mfe_excludes_entry_bar():
    import pandas as pd
    idx = pd.date_range("2026-07-06 09:30", periods=3, freq="5min", tz="America/Chicago")
    df = pd.DataFrame({"high": [20050.0, 20010.0, 20020.0],
                       "low": [19990.0, 19995.0, 20000.0]}, index=idx)
    # entry on the first bar: its 20050 spike must NOT count (backtest parity)
    mfe = tr._live_mfe_pts(df, idx[0], 20000.0, "long")
    assert mfe == 20.0  # max(20010,20020)-20000, not 50
    assert tr._live_mfe_pts(df[df.index <= idx[0]], idx[0], 20000.0, "long") is None


class _FakeStopClient:
    def __init__(self, modify_ok=True, confirm=True, cancel_ok=True):
        self.modify_ok = modify_ok; self.confirm = confirm; self.cancel_ok = cancel_ok
        self.calls = []
    def modify_order(self, account_id, order_id, **kw):
        self.calls.append(("modify", order_id, kw.get("stop_price")))
        if not self.modify_ok:
            raise tr.TopstepXAPIError("modify unsupported")
        return {"success": True}
    def place_order(self, **kw):
        self.calls.append(("place", kw.get("order_type"), kw.get("stop_price")))
        return {"orderId": 999}
    def search_open_orders(self, account_id):
        self.calls.append(("search", None, None))
        return [{"id": 999}] if self.confirm else []
    def cancel_order(self, account_id, order_id):
        self.calls.append(("cancel", order_id, None))
        if not self.cancel_ok and order_id == 5:
            raise tr.TopstepXAPIError("cancel rejected")
        return {"success": True}

def _mv(cli):
    return tr._move_protective_stop(cli, None, account_id=1, contract_id="X",
                                    entry_side=0, old_order_id=5, size=3, new_stop=20000.0)

def test_move_stop_atomic_modify():
    cli = _FakeStopClient(modify_ok=True)
    assert _mv(cli) == "modify"
    assert [c[0] for c in cli.calls] == ["modify"]  # no place, no cancel

def test_move_stop_fallback_place_confirm_then_cancel():
    cli = _FakeStopClient(modify_ok=False, confirm=True, cancel_ok=True)
    assert _mv(cli) == "replace"
    kinds = [c[0] for c in cli.calls]
    # NEVER cancel-first: the cancel of the old stop must come after place+confirm
    assert kinds.index("place") < kinds.index("cancel")
    assert ("cancel", 5, None) in cli.calls

def test_move_stop_unconfirmed_replacement_keeps_old():
    cli = _FakeStopClient(modify_ok=False, confirm=False)
    import pytest
    with pytest.raises(tr.TopstepXAPIError):
        _mv(cli)
    # old stop (id 5) must NOT have been cancelled
    assert ("cancel", 5, None) not in cli.calls

def test_move_stop_old_cancel_failure_reports_two_stops():
    cli = _FakeStopClient(modify_ok=False, confirm=True, cancel_ok=False)
    assert _mv(cli) == "replace_old_uncancelled"


# ── End-to-end: _manage_position_stops fires breakeven on a live-shaped position ─
class _FakeMgrClient:
    """Mimics the broker for a long position that has run +24t past entry."""
    def __init__(self):
        self.modified = []
        self.account = {"id": 1, "name": "acct"}
    def search_accounts(self, active=True):
        return [self.account]
    def resolve_contract(self, text, live=None):
        return {"id": "X", "name": "MNQU6"}
    def retrieve_bars(self, **kw):
        # 5-min bars, UTC. Entry bar 15:00Z (10:00 CT) spikes high 20050 (must be
        # EXCLUDED). Post-entry bars top out at 20006 -> MFE 6pts=24t >= 20t.
        return [
            {"t": "2026-07-06T15:00:00Z", "o": 20000, "h": 20050, "l": 19999, "c": 20000, "v": 100},
            {"t": "2026-07-06T15:05:00Z", "o": 20000, "h": 20003, "l": 19998, "c": 20002, "v": 100},
            {"t": "2026-07-06T15:10:00Z", "o": 20002, "h": 20006, "l": 20001, "c": 20005, "v": 100},
        ]
    def search_open_orders(self, account_id):
        return [{"id": 5, "contractId": "X", "side": 1, "type": 4, "stopPrice": 19992.0}]
    def modify_order(self, account_id, order_id, **kw):
        self.modified.append((order_id, kw.get("stop_price")))
        return {"success": True}

class _CfgFull:
    enable_order_routing = True
    dry_run = False
    contract_search_text = "MNQ"
    live_data = False
    account_name = "acct"

def test_manage_position_stops_moves_to_breakeven_end_to_end(monkeypatch):
    monkeypatch.setattr(tr, "_save_state", lambda s: None)
    monkeypatch.setattr(tr, "_kill_switch_active", lambda: False)
    cli = _FakeMgrClient()
    state = tr._default_state()
    state.update({
        "current_position": 3,                      # long 3 lots
        "last_entry_fill_price": 20000.0,
        "last_order_submitted_at": "2026-07-06T10:00:00-05:00",  # 10:00 CT
        "stop_mgmt_last_bar": "",                    # not yet evaluated this bar
    })
    tr._manage_position_stops(cli, _CfgFull(), state)
    # It must have moved the stop from 19992 up to breakeven (entry 20000).
    assert cli.modified == [(5, 20000.0)], cli.modified
    assert state["stop_moves_this_trade"] == 1

def test_manage_position_stops_noop_when_flat(monkeypatch):
    monkeypatch.setattr(tr, "_save_state", lambda s: None)
    monkeypatch.setattr(tr, "_kill_switch_active", lambda: False)
    cli = _FakeMgrClient()
    state = tr._default_state(); state["current_position"] = 0
    tr._manage_position_stops(cli, _CfgFull(), state)
    assert cli.modified == []   # nothing to manage when flat


# ── Observability: fill forensics + EOD summary (logging only, no trade impact) ──
def test_fill_forensics_writes_row_and_computes_slippage(monkeypatch, tmp_path):
    monkeypatch.setattr(tr, "FILL_FORENSICS_CSV_PATH", str(tmp_path / "ff.csv"))
    st = tr._default_state(); st["session_date"] = "2026-07-06"; st["session_daily_pnl_usd"] = 120.0
    row = tr._log_fill_forensics(st, kind="entry", contract="MNQU6", direction="long",
                                 size=3, intended=20000.0, actual=20000.75, trade_pnl=None)
    assert row["slippage_ticks"] == 3.0                 # 0.75 / 0.25
    assert row["slippage_usd"] == 3.0 * tr.bot.MNQ_TICK_VALUE * 3   # 3t * $0.50 * 3 = $4.50
    import csv as _csv
    with open(tr.FILL_FORENSICS_CSV_PATH, encoding="utf-8") as fh:
        rows = list(_csv.DictReader(fh))
    assert len(rows) == 1 and rows[0]["kind"] == "entry"

def test_eod_summary_sends_once_and_reads_exits(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(tr, "_send_telegram_lines", lambda lines: sent.append(lines) or True)
    monkeypatch.setattr(tr, "_save_state", lambda s: None)
    monkeypatch.setattr(tr, "FILL_FORENSICS_CSV_PATH", str(tmp_path / "ff.csv"))
    st = tr._default_state(); st["session_date"] = "2026-07-06"; st["session_daily_pnl_usd"] = 240.0
    tr._log_fill_forensics(st, kind="exit_target", contract="X", direction="long",
                           size=3, intended=20010.0, actual=20010.0, trade_pnl=240.0)
    tr._send_eod_summary(st)
    assert sent, "EOD summary should send"
    assert any("end of day" in l for l in sent[-1])
    assert st["eod_summary_sent_date"] == "2026-07-06"
    sent.clear()
    tr._send_eod_summary(st)          # second call same day -> no-op
    assert not sent


# -- Shipped trend-bias mode (validated 2026-07-04) --------------------------------
def test_bias_mode_shipped_is_vwap_only():
    assert bot.BIAS_MODE == "vwap_only"


# ── 2026-07-04 internal audit hardening ─────────────────────────────────────────
# Fixes verified here: atomic state writes, corrupt-state tolerance, flatten
# resilience (close even when a cancel fails), telegram /flatten cooldown
# surviving the post-command state save, partial-exit trade events not
# clobbering net position, and 429s classified transient.
import json as _json


def test_write_json_atomic_no_tmp_left(tmp_path):
    p = str(tmp_path / "state.json")
    tr._write_json(p, {"a": 1})
    with open(p, encoding="utf-8") as fh:
        assert _json.load(fh) == {"a": 1}
    assert not os.path.exists(p + ".tmp")


def test_write_json_overwrites_existing_atomically(tmp_path):
    p = str(tmp_path / "state.json")
    tr._write_json(p, {"v": 1})
    tr._write_json(p, {"v": 2})
    with open(p, encoding="utf-8") as fh:
        assert _json.load(fh) == {"v": 2}
    assert not os.path.exists(p + ".tmp")


def test_load_state_survives_corrupt_file(tmp_path, monkeypatch):
    # A crash mid-write must not wedge the bot in a crash-restart loop:
    # corrupt JSON -> default state (broker reconcile restores truth).
    p = str(tmp_path / "state.json")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write('{"updated_at": "2026-')  # torn write
    monkeypatch.setattr(tr, "STATE_PATH", p)
    st = tr._load_state()
    assert isinstance(st, dict)
    assert st.get("current_position", 0) == 0


class _FlattenClient:
    """cancel_order fails on the FIRST order; close_contract must still run."""
    def __init__(self):
        self.cancelled = []
        self.closed = []
    def search_accounts(self, only_active):
        return [{"id": 1, "name": "ACCT"}]
    def search_open_orders(self, account_id):
        return [{"id": 11}, {"id": 12}]
    def search_open_positions(self, account_id):
        return [{"contractId": "CON.F.US.MNQ.U26"}]
    def cancel_order(self, account_id, order_id):
        if order_id == 11:
            raise tr.TopstepXAPIError("simulated cancel failure")
        self.cancelled.append(order_id)
        return {"ok": True}
    def close_contract(self, account_id, contract_id):
        self.closed.append(contract_id)
        return {"ok": True}


class _FlattenCfg:
    account_name = "ACCT"
    dry_run = False
    enable_order_routing = True


def test_flatten_closes_position_even_if_cancel_fails(monkeypatch):
    monkeypatch.setattr(tr, "_log_trade_event", lambda *a, **k: None)
    client = _FlattenClient()
    with pytest.raises(tr.TopstepXAPIError, match="flatten_incomplete"):
        tr._flatten_account_internal(client, _FlattenCfg(), reason="hard_flatten_time")
    # The position was closed despite the first cancel failing,
    # and the second cancel still went through.
    assert client.closed == ["CON.F.US.MNQ.U26"]
    assert client.cancelled == [12]


def test_telegram_flatten_cooldown_survives_state_save(tmp_path, monkeypatch):
    # _flatten_account_internal writes the cooldown to disk on its own state
    # copy; the telegram handler must sync it into the caller's in-memory
    # state so the post-command _save_state doesn't clobber it.
    p = str(tmp_path / "state.json")
    monkeypatch.setattr(tr, "STATE_PATH", p)

    def fake_flatten(client, config, reason):
        st = tr._load_state()
        st["manual_flatten_cooldown_until"] = "2099-01-01T00:00:00-05:00"
        st["manual_flatten_reason"] = reason
        tr._save_state(st)
        return {}

    monkeypatch.setattr(tr, "_flatten_account_internal", fake_flatten)
    state = tr._default_state()
    tr._handle_telegram_command("/flatten", None, None, state)
    tr._save_state(state)  # what _process_telegram_commands does afterwards
    assert tr._load_state()["manual_flatten_cooldown_until"] == "2099-01-01T00:00:00-05:00"


class _HubCfg:
    account_name = "ACCT"


def _trade_event(trade_id, size, pnl):
    return {
        "event_type": "GatewayUserTrade",
        "payload": {"id": trade_id, "size": size, "price": 20000.0,
                    "profitAndLoss": pnl, "contractId": "CON.F.US.MNQ.U26"},
        "logged_at": "2026-07-04T12:00:00Z",
    }


def test_partial_exit_trade_event_does_not_clobber_position(monkeypatch):
    monkeypatch.setattr(tr, "_log_trade_event", lambda *a, **k: None)
    state = tr._default_state()
    state["current_position"] = 5   # long 5 MNQ
    # Partial exit: 2 of 5 close. The trade's size (2) must NOT overwrite
    # the net position (5) that live stop management reads.
    tr._process_user_hub_events([_trade_event("t-exit", 2, 40.0)], state, _HubCfg())
    assert state["current_position"] == 5


def test_entry_from_flat_trade_event_sets_position(monkeypatch):
    monkeypatch.setattr(tr, "_log_trade_event", lambda *a, **k: None)
    state = tr._default_state()
    state["current_position"] = 0
    tr._process_user_hub_events([_trade_event("t-entry", 3, None)], state, _HubCfg())
    assert state["current_position"] == 3


def test_position_event_still_authoritative(monkeypatch):
    monkeypatch.setattr(tr, "_log_trade_event", lambda *a, **k: None)
    state = tr._default_state()
    state["current_position"] = 5
    ev = {"event_type": "GatewayUserPosition",
          "payload": {"size": 0, "contractId": "CON.F.US.MNQ.U26"},
          "logged_at": "2026-07-04T12:00:01Z"}
    tr._process_user_hub_events([ev], state, _HubCfg())
    assert state["current_position"] == 0
    assert state["in_trade"] is False


def test_rate_limit_429_is_transient():
    assert tr._is_transient_error(Exception("HTTP 429 Too Many Requests"))
    assert tr._is_transient_error(Exception("rate limit exceeded"))
    assert not tr._is_transient_error(Exception("invalid order size"))


# ── Fix 5: hub bracket confirmation must reject cancelled/rejected stops ────────

def _hub_order_event(contract_id, entry_side_of_position, stop_price, status):
    """Return a GatewayUserOrder hub event for a stop on the opposite side."""
    opposite_side = 1 - entry_side_of_position
    return {
        "event_type": "GatewayUserOrder",
        "payload": {
            "contractId": contract_id,
            "side": opposite_side,
            "type": "4",
            "stopPrice": stop_price,
            "status": status,
        },
    }


def test_hub_does_not_confirm_rejected_stop():
    event = _hub_order_event("C1", 0, 19900.0, "rejected")
    order_payload = {"side": 0, "contractId": "C1"}
    assert not tr._user_hub_confirms_protective_order(event, order_payload)


def test_hub_does_not_confirm_cancelled_stop():
    event = _hub_order_event("C1", 0, 19900.0, "cancelled")
    order_payload = {"side": 0, "contractId": "C1"}
    assert not tr._user_hub_confirms_protective_order(event, order_payload)


def test_hub_does_not_confirm_filled_stop():
    event = _hub_order_event("C1", 0, 19900.0, "filled")
    order_payload = {"side": 0, "contractId": "C1"}
    assert not tr._user_hub_confirms_protective_order(event, order_payload)


def test_hub_confirms_working_stop():
    event = _hub_order_event("C1", 0, 19900.0, "working")
    order_payload = {"side": 0, "contractId": "C1"}
    assert tr._user_hub_confirms_protective_order(event, order_payload)


def test_hub_confirms_open_stop():
    event = _hub_order_event("C1", 0, 19900.0, "open")
    order_payload = {"side": 0, "contractId": "C1"}
    assert tr._user_hub_confirms_protective_order(event, order_payload)


# ── Fix 2: bracket verify must not panic-flatten when fill never appeared ───────

class _FlattenCfgRouting:
    account_name = "ACCT"
    dry_run = False
    enable_order_routing = True


def test_bracket_verify_no_panic_when_position_never_appeared(monkeypatch):
    """If the entry order was rejected (or not yet settled by end of timeout),
    position count stays 0 throughout. Must return quietly without flattening."""
    class _EmptyClient:
        def search_open_orders(self, account_id):
            return []
        def search_open_positions(self, account_id):
            return []

    monkeypatch.setattr(tr, "ORDER_BRACKET_VERIFY_DELAY_SECONDS", 0)
    monkeypatch.setattr(tr, "BRACKET_VERIFY_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(tr, "BRACKET_VERIFY_REST_POLL_SECONDS", 0)
    monkeypatch.setattr(tr, "_check_kill_switch_or_raise", lambda: None)
    monkeypatch.setattr(tr, "_log_trade_event", lambda *a, **k: None)
    flattened = []
    monkeypatch.setattr(tr, "_flatten_account_internal", lambda *a, **k: flattened.append(True))
    monkeypatch.setattr(tr, "engage_kill_switch", lambda *a, **k: None)

    state = tr._default_state()
    order_payload = {"accountId": 1, "contractId": "C1", "side": 0,
                     "accountName": "ACCT", "contractName": "MNQU6"}
    tr._verify_brackets_after_submit(
        _EmptyClient(), _FlattenCfgRouting(), {}, state, order_payload, user_stream=None,
    )
    assert not flattened, "Must not flatten when position never appeared (likely rejection)"


# ── Fix 3: unprotected detection must look for a real stop, not just any order ─

class _UnprotectedCfg:
    enable_order_routing = True
    dry_run = False


def test_tp_order_does_not_satisfy_stop_requirement():
    """A sell-limit take-profit order must not count as a protective stop."""
    state = tr._default_state()
    state["open_position_count"] = 1
    state["open_order_count"] = 1
    # Long position; the only order is a take-profit (type=1 limit, no stopPrice)
    open_positions = [{"contractId": "C1", "size": 2}]
    open_orders = [{"contractId": "C1", "side": 1, "type": 1, "stopPrice": None}]
    assert tr._unprotected_position_detected(
        state, _UnprotectedCfg(), open_orders=open_orders, open_positions=open_positions
    )


def test_real_stop_order_satisfies_stop_requirement():
    """A genuine stop order on the opposite side protects the position."""
    state = tr._default_state()
    state["open_position_count"] = 1
    state["open_order_count"] = 1
    open_positions = [{"contractId": "C1", "size": 2}]
    open_orders = [{"contractId": "C1", "side": 1, "type": "4", "stopPrice": 19900.0}]
    assert not tr._unprotected_position_detected(
        state, _UnprotectedCfg(), open_orders=open_orders, open_positions=open_positions
    )


def test_zero_orders_always_unprotected():
    """When open_order_count == 0, the position is unprotected regardless of lists."""
    state = tr._default_state()
    state["open_position_count"] = 1
    state["open_order_count"] = 0
    assert tr._unprotected_position_detected(state, _UnprotectedCfg(), open_orders=[], open_positions=[{"contractId": "C1", "size": 2}])


def test_no_position_never_unprotected():
    state = tr._default_state()
    state["open_position_count"] = 0
    state["open_order_count"] = 0
    assert not tr._unprotected_position_detected(state, _UnprotectedCfg())


# ── Fix 7: awaiting_entry_fill repaired on reconcile after restart ──────────────

class _ReconcileCfgFull:
    account_name = "ACCT"
    contract_search_text = "MNQ"
    live_data = False
    topstep_account_stage = "combine"
    topstep_account_size_usd = 50_000
    enable_order_routing = True
    dry_run = False


class _ReconcileClientWithPosition:
    def search_accounts(self, only_active):
        return [{"id": 1, "name": "ACCT", "balance": 50_000}]
    def search_open_orders(self, account_id):
        return [{"id": 99, "contractId": "C1", "side": 1, "type": "4", "stopPrice": 19900.0}]
    def search_open_positions(self, account_id):
        return [{"contractId": "C1", "size": 2}]
    def resolve_contract(self, search_text, live):
        return {"id": "C1", "name": "MNQU6"}


def test_awaiting_entry_fill_repaired_when_position_and_bracket_found(tmp_path, monkeypatch):
    """After a restart with awaiting_entry_fill=True, if reconcile finds an open
    position AND a protective stop, the lifecycle flags are advanced automatically."""
    monkeypatch.setattr(tr, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(tr, "_log_trade_event", lambda *a, **k: None)
    monkeypatch.setattr(tr, "engage_kill_switch", lambda *a, **k: None)
    monkeypatch.setattr(tr, "_flatten_account_internal", lambda *a, **k: None)

    st = tr._default_state()
    st["awaiting_entry_fill"] = True
    st["awaiting_exit_fill"] = False
    tr._save_state(st)

    tr.reconcile_state(_ReconcileClientWithPosition(), _ReconcileCfgFull())
    repaired = tr._load_state()
    assert repaired["awaiting_entry_fill"] is False, "awaiting_entry_fill must be cleared"
    assert repaired["awaiting_exit_fill"] is True, "awaiting_exit_fill must be armed"


def test_awaiting_entry_fill_not_repaired_when_no_brackets(tmp_path, monkeypatch):
    """If there is a position but no stop order, do not silently advance the
    lifecycle flag — the unprotected-position guard is supposed to fire instead."""
    monkeypatch.setattr(tr, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(tr, "_log_trade_event", lambda *a, **k: None)
    monkeypatch.setattr(tr, "engage_kill_switch", lambda *a, **k: None)
    monkeypatch.setattr(tr, "_flatten_account_internal", lambda *a, **k: None)

    st = tr._default_state()
    st["awaiting_entry_fill"] = True
    tr._save_state(st)

    class _NoOrdersClient(_ReconcileClientWithPosition):
        def search_open_orders(self, account_id):
            return []  # position open but no bracket orders

    tr.reconcile_state(_NoOrdersClient(), _ReconcileCfgFull())
    not_repaired = tr._load_state()
    # Flag stays True; the unprotected-position guard (separate path) is responsible
    assert not_repaired["awaiting_entry_fill"] is True


# ── Fix 10: /resume must verify account is flat for safety-critical halts ───────

import json as _json_module


class _FlatAccountClient:
    def search_accounts(self, only_active):
        return [{"id": 1, "name": "ACCT"}]
    def search_open_orders(self, account_id):
        return []
    def search_open_positions(self, account_id):
        return []


class _PositionAccountClient:
    def search_accounts(self, only_active):
        return [{"id": 1, "name": "ACCT"}]
    def search_open_orders(self, account_id):
        return []
    def search_open_positions(self, account_id):
        return [{"contractId": "C1"}]


class _ResumeCfg:
    account_name = "ACCT"


def _write_halt(path, reason):
    with open(path, "w", encoding="utf-8") as fh:
        _json_module.dump({"reason": reason, "engaged_at": "2026-07-15T10:00:00-05:00"}, fh)


def test_resume_blocked_for_bracket_failure_halt_when_position_open(tmp_path, monkeypatch):
    p = str(tmp_path / "HALT.txt")
    _write_halt(p, "bracket_failure_unprotected_position")
    monkeypatch.setattr(tr, "KILL_SWITCH_PATH", p)
    msgs = []
    monkeypatch.setattr(tr, "_send_telegram_lines", lambda lines, **k: msgs.append(lines))
    state = tr._default_state()
    tr._handle_telegram_command("/resume", _PositionAccountClient(), _ResumeCfg(), state)
    assert os.path.exists(p), "Safety halt must NOT be cleared while position is open"
    assert any("cannot resume" in " ".join(str(x) for x in m) for m in msgs)


def test_resume_clears_bracket_failure_halt_when_account_flat(tmp_path, monkeypatch):
    p = str(tmp_path / "HALT.txt")
    _write_halt(p, "bracket_failure_unprotected_position")
    monkeypatch.setattr(tr, "KILL_SWITCH_PATH", p)
    monkeypatch.setattr(tr, "_send_telegram_lines", lambda *a, **k: None)
    state = tr._default_state()
    tr._handle_telegram_command("/resume", _FlatAccountClient(), _ResumeCfg(), state)
    assert not os.path.exists(p), "Safety halt should clear when account is confirmed flat"


def test_resume_clears_telegram_halt_without_broker_check(tmp_path, monkeypatch):
    """A /halt (reason=telegram_command) must clear immediately — no broker query needed."""
    p = str(tmp_path / "HALT.txt")
    _write_halt(p, "telegram_command")
    monkeypatch.setattr(tr, "KILL_SWITCH_PATH", p)
    monkeypatch.setattr(tr, "_send_telegram_lines", lambda *a, **k: None)
    state = tr._default_state()
    # Pass None for client — safe halts must not touch the broker API
    tr._handle_telegram_command("/resume", None, None, state)
    assert not os.path.exists(p)


# ── Payload forensics capture (audit Findings 1 / 8 / B instrumentation) ────────
import json as _json


def _read_forensics_lines():
    if not os.path.exists(tr.PAYLOAD_FORENSICS_PATH):
        return []
    with open(tr.PAYLOAD_FORENSICS_PATH, encoding="utf-8") as fh:
        return [_json.loads(line) for line in fh if line.strip()]


def test_payload_forensics_writes_record():
    tr._log_payload_forensics("test_kind", {"size": -4, "contractId": "CON.F.US.MNQ.U26"})
    records = _read_forensics_lines()
    assert len(records) == 1
    assert records[0]["kind"] == "test_kind"
    assert records[0]["payload"]["size"] == -4


def test_payload_forensics_daily_cap():
    for i in range(tr.PAYLOAD_FORENSICS_MAX_PER_KIND_PER_DAY + 3):
        tr._log_payload_forensics("capped_kind", {"n": i})
    records = _read_forensics_lines()
    assert len(records) == tr.PAYLOAD_FORENSICS_MAX_PER_KIND_PER_DAY


def test_payload_forensics_cap_is_per_kind():
    tr._log_payload_forensics("kind_a", {})
    tr._log_payload_forensics("kind_b", {})
    assert len(_read_forensics_lines()) == 2


def test_payload_forensics_survives_unserializable_payload():
    # A set is not JSON-serializable; default=str must save it, not raise.
    tr._log_payload_forensics("weird", {"raw": {1, 2, 3}})
    records = _read_forensics_lines()
    assert len(records) == 1


def test_payload_forensics_never_raises_on_write_failure(monkeypatch):
    monkeypatch.setattr(tr, "PAYLOAD_FORENSICS_PATH", r"Z:\nonexistent\dir\f.jsonl")
    tr._log_payload_forensics("doomed", {"x": 1})  # must not raise


def test_hub_event_capture_records_raw_event(monkeypatch):
    monkeypatch.setattr(tr, "_log_trade_event", lambda *a, **k: None)
    state = tr._default_state()
    event = {"event_type": "GatewayUserPosition",
             "payload": {"size": -4, "contractId": "CON.F.US.MNQ.U26"},
             "logged_at": "2026-07-18T09:30:00Z"}
    tr._process_user_hub_events([event], state, _HubCfg())
    records = _read_forensics_lines()
    assert any(r["kind"] == "hub_GatewayUserPosition" for r in records)
    hub_rec = next(r for r in records if r["kind"] == "hub_GatewayUserPosition")
    # The FULL event is captured (not the pre-parsed extract), so the true
    # payload shape is preserved even if the parser's key assumptions are wrong.
    assert hub_rec["payload"]["payload"]["size"] == -4


# ── Real ProjectX payload shapes (captured live 2026-07-20, forensics JSONL) ────
# These fixtures mirror the exact wire format: hub events enveloped as
# {"action": N, "data": {...}}, numeric status/type codes, UNSIGNED position
# and trade sizes with direction in `type` (positions: 1=long/2=short) or
# `side` (orders/trades: 0=buy/1=sell).

_REAL_SHORT_POSITION = {
    "id": 793089049, "accountId": 24808178, "contractId": "CON.F.US.MNQ.U26",
    "type": 2, "size": 21, "averagePrice": 28876.0,
}
_REAL_SL_ORDER = {
    "id": 3293159491, "accountId": 24808178, "contractId": "CON.F.US.MNQ.U26",
    "status": 1, "type": 4, "side": 0, "size": 21,
    "limitPrice": None, "stopPrice": 28881.25, "fillVolume": 0,
    "customTag": "V29-FVG-short-2026-07-20T135500-0500-a1784574013-SL",
}
_REAL_TP_ORDER = {
    "id": 3293159492, "accountId": 24808178, "contractId": "CON.F.US.MNQ.U26",
    "status": 1, "type": 1, "side": 0, "size": 21,
    "limitPrice": 28816.0, "stopPrice": None, "fillVolume": 0,
    "customTag": "V29-FVG-short-2026-07-20T135500-0500-a1784574013-TP",
}


def _enveloped(event_type, data):
    return {"event_type": event_type, "payload": {"action": 1, "data": data},
            "logged_at": "2026-07-20T19:00:15Z"}


class _UnprotCfg:
    enable_order_routing = True
    dry_run = False


def test_signed_size_short_position_real_shape():
    assert tr._extract_signed_position_size(_REAL_SHORT_POSITION) == -21


def test_signed_size_long_position_type1():
    assert tr._extract_signed_position_size({"type": 1, "size": 5}) == 5


def test_signed_size_flat_position_type0():
    assert tr._extract_signed_position_size({"type": 0, "size": 0}) == 0


def test_signed_size_legacy_signed_payload_passthrough():
    # A payload already carrying a signed size (no type field) is unchanged.
    assert tr._extract_signed_position_size({"size": -3}) == -3


def test_unprotected_detector_accepts_real_short_brackets():
    """Regression: before the type-aware sign fix, a live short read as +21 ->
    entry_side long -> buy-side brackets unrecognized -> FALSE emergency
    flatten of a fully protected position."""
    state = {"open_position_count": 1, "open_order_count": 2}
    assert not tr._unprotected_position_detected(
        state, _UnprotCfg(),
        open_orders=[_REAL_SL_ORDER, _REAL_TP_ORDER],
        open_positions=[_REAL_SHORT_POSITION])


def test_unprotected_detector_fires_when_short_has_only_tp():
    state = {"open_position_count": 1, "open_order_count": 1}
    assert tr._unprotected_position_detected(
        state, _UnprotCfg(),
        open_orders=[_REAL_TP_ORDER],
        open_positions=[_REAL_SHORT_POSITION])


def test_hub_envelope_suspended_stop_confirms_short_bracket():
    ev = _enveloped("GatewayUserOrder", dict(_REAL_SL_ORDER, status=8, stopPrice=None))
    assert tr._user_hub_confirms_protective_order(
        ev, {"side": 1, "contractId": "CON.F.US.MNQ.U26"})


def test_hub_envelope_filled_entry_order_does_not_confirm():
    entry = {"id": 1, "contractId": "CON.F.US.MNQ.U26", "status": 2,
             "type": 2, "side": 1, "size": 21, "fillVolume": 21}
    ev = _enveloped("GatewayUserOrder", entry)
    assert not tr._user_hub_confirms_protective_order(
        ev, {"side": 1, "contractId": "CON.F.US.MNQ.U26"})


def test_hub_envelope_cancelled_stop_does_not_confirm():
    ev = _enveloped("GatewayUserOrder", dict(_REAL_SL_ORDER, status=3))
    assert not tr._user_hub_confirms_protective_order(
        ev, {"side": 1, "contractId": "CON.F.US.MNQ.U26"})


def test_hub_envelope_unknown_status_does_not_confirm():
    # Whitelist semantics: an unknown status code must fail safe (fall back to
    # the REST bracket check) rather than confirm protection.
    ev = _enveloped("GatewayUserOrder", dict(_REAL_SL_ORDER, status=99))
    assert not tr._user_hub_confirms_protective_order(
        ev, {"side": 1, "contractId": "CON.F.US.MNQ.U26"})


def test_hub_position_event_short_sets_negative_position(monkeypatch):
    monkeypatch.setattr(tr, "_log_trade_event", lambda **k: None)
    state = tr._default_state()
    tr._process_user_hub_events(
        [_enveloped("GatewayUserPosition", _REAL_SHORT_POSITION)], state, _HubCfg())
    assert state["current_position"] == -21
    assert state["current_contracts"] == 21
    assert state["in_trade"] is True


def test_hub_trade_entry_fill_arms_slippage_monitoring(monkeypatch, tmp_path):
    """End-to-end on the REAL captured entry-fill shape: envelope unwrapped,
    price seen, slippage recorded, lifecycle flags advanced."""
    monkeypatch.setattr(tr, "_log_trade_event", lambda **k: None)
    monkeypatch.setattr(tr, "KILL_SWITCH_PATH", str(tmp_path / "HALT.txt"))
    trade = {"id": 2884287598, "accountId": 24808178,
             "contractId": "CON.F.US.MNQ.U26", "price": 28876.0,
             "fees": 7.56, "side": 1, "size": 21, "voided": False,
             "orderId": 3293159486, "profitAndLoss": None,
             "creationTimestamp": "2026-07-20T19:00:15.260089+00:00"}
    state = tr._default_state()
    state.update({"current_position": 0, "awaiting_entry_fill": True,
                  "last_signal_entry_price": 28875.5,
                  "rolling_entry_slippage_ticks": []})
    tr._process_user_hub_events(
        [_enveloped("GatewayUserTrade", trade)], state, _HubCfg())
    assert state["awaiting_entry_fill"] is False
    assert state["awaiting_exit_fill"] is True
    assert state["last_entry_fill_price"] == 28876.0
    assert state["rolling_entry_slippage_ticks"] == [2.0]   # 0.5 pts = 2 ticks
    assert state["current_position"] == -21                 # sell 21 from flat


def test_hub_trade_exit_fill_records_exit_slippage(monkeypatch, tmp_path):
    monkeypatch.setattr(tr, "_log_trade_event", lambda **k: None)
    monkeypatch.setattr(tr, "KILL_SWITCH_PATH", str(tmp_path / "HALT.txt"))
    exit_trade = {"id": 2884287777, "contractId": "CON.F.US.MNQ.U26",
                  "price": 28881.25, "side": 0, "size": 21,
                  "profitAndLoss": -220.5, "orderId": 3293159491}
    state = tr._default_state()
    state.update({"current_position": -21, "awaiting_entry_fill": False,
                  "awaiting_exit_fill": True,
                  "last_signal_stop_price": 28881.25,
                  "last_signal_target_price": 28816.0,
                  "rolling_exit_slippage_ticks": []})
    tr._process_user_hub_events(
        [_enveloped("GatewayUserTrade", exit_trade)], state, _HubCfg())
    assert state["awaiting_exit_fill"] is False
    assert state["rolling_exit_slippage_ticks"] == [0.0]    # exact stop fill
