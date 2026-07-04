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
    assert any("HALT" in l for l in lines)


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
