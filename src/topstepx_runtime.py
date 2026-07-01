import argparse
import csv
import json
import os
import re
import socket
import ssl
import sys
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib import error, parse, request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Load .env before anything reads os.getenv()
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except ImportError:
    pass

from src import bot
from src.topstepx_client import (
    TopstepXAPIError,
    TopstepXClient,
    TopstepXConfig,
    TopstepXUserHubStream,
)


SIGNAL_EXPORT_PATH = os.path.join(bot.EXPORT_DIR, "v29_topstep_signal.json")
ORDER_PLAN_EXPORT_PATH = os.path.join(bot.EXPORT_DIR, "v29_topstep_order_plan.json")
TELEMETRY_JSONL_PATH = os.path.join(bot.EXPORT_DIR, "v29_topstep_telemetry.jsonl")
TRADE_NOTES_CSV_PATH = os.path.join(bot.EXPORT_DIR, "v29_topstep_trade_notes.csv")
STATE_PATH = os.path.join(bot.EXPORT_DIR, "v29_topstep_runtime_state.json")
KILL_SWITCH_PATH = os.path.join(bot.EXPORT_DIR, "HALT.txt")
ERROR_LOG_PATH = os.path.join(bot.EXPORT_DIR, "live_errors.txt")

SESSION_ROLLOVER_HOUR_CT = 17
DEFAULT_LOOP_INTERVAL_SECONDS = 5
DEFAULT_SESSION_VALIDATE_SECONDS = 60
MAX_RECONNECT_BACKOFF_SECONDS = 60
DATA_GAP_KILL_THRESHOLD = 5
DATA_GAP_AUTO_CLEAR_MINUTES = 30
MAX_TRANSIENT_RETRIES = 50
TRANSIENT_ERROR_KEYWORDS = (
    "network error", "urlopen error", "timed out", "timeout",
    "502", "503", "504", "connection", "ssl", "winerror", "handshake", "remote host",
)
CONSISTENCY_WARNING_FRACTION = 0.80
CONSISTENCY_THROTTLE_FRACTION = 0.90
LIVE_BAR_LOOKBACK_BARS = 2500
HUB_STALE_SECONDS = 30
HEAVY_RECONCILE_SECONDS = 300
ORDER_BRACKET_VERIFY_DELAY_SECONDS = 1.5
BRACKET_VERIFY_TIMEOUT_SECONDS = 8.0
BRACKET_VERIFY_REST_POLL_SECONDS = 1.0
CONSISTENCY_BUFFER = 0.02
STRATEGY_BAR_SECONDS = 300
# Live slippage monitoring: compare intended entry price vs actual fill price.
# The backtest assumes ~1 tick; if live fills are systematically worse the edge
# is not real, so halt. Rolling average over the last SLIPPAGE_WINDOW entries.
SLIPPAGE_WINDOW = 20
SLIPPAGE_ALERT_TICKS = 3.0
MANUAL_FLATTEN_COOLDOWN_MINUTES = 10   # block new entries for this long after a manual flatten

QUARTER_MONTH_CODES = {
    3: "H",
    6: "M",
    9: "U",
    12: "Z",
}

TRADE_NOTE_FIELDS = [
    "logged_at",
    "event_type",
    "signal_id",
    "account_name",
    "contract_name",
    "dry_run",
    "executed",
    "entry_timestamp",
    "session_date",
    "entry_type",
    "direction",
    "regime",
    "contracts",
    "entry_price",
    "stop_price",
    "target_price",
    "stop_width_ticks",
    "target_ticks",
    "reward_risk_ratio",
    "entry_hour",
    "day_of_week",
    "month",
    "year",
    "atr_at_entry",
    "atr_ratio_at_entry",
    "adx_at_entry",
    "vwap_at_entry",
    "price_distance_from_vwap",
    "ema_spread_at_entry",
    "fvg_type",
    "fvg_size_ticks",
    "fvg_age_bars",
    "fvg_quality_score",
    "fvg_quality_flags",
    "fb_level_type",
    "fb_level_price",
    "fb_sweep_distance_ticks",
    "fb_reentry_distance_ticks",
    "prev_day_high",
    "prev_day_low",
    "globex_high",
    "globex_low",
    "opening_gap_points",
    "is_news_day",
    "drawdown_pct_at_entry",
    "daily_pnl_at_entry",
    "buffer_above_floor_at_entry",
    "combine_profit_at_entry",
    "combine_target_remaining",
    "daily_loss_used_pct",
    "mll_used_pct",
    "startup_audit_status",
    "thesis",
    "notes",
    "broker_response",
]


def _current_live_log_path() -> str:
    return os.path.join(bot.EXPORT_DIR, f"live_log_{datetime.now(bot.TIMEZONE).strftime('%Y%m%d')}.txt")


def _current_ct_now() -> datetime:
    return datetime.now(bot.TIMEZONE)


def _current_session_date(now: Optional[datetime] = None) -> str:
    current = now or _current_ct_now()
    if (current.hour, current.minute) >= (SESSION_ROLLOVER_HOUR_CT, 0):
        current = current + timedelta(days=1)
    return current.date().isoformat()


def _is_transient_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(kw in msg for kw in TRANSIENT_ERROR_KEYWORDS)


def _write_log(level: str, message: str, *, error_only: bool = False) -> None:
    timestamp = datetime.now(bot.TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %Z")
    line = f"[{timestamp}] [{level}] {message}"
    if not error_only:
        os.makedirs(os.path.dirname(_current_live_log_path()), exist_ok=True)
        with open(_current_live_log_path(), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    if level == "ERROR" or error_only:
        os.makedirs(os.path.dirname(ERROR_LOG_PATH), exist_ok=True)
        with open(ERROR_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _default_state() -> Dict[str, Any]:
    return {
        "version": "V29",
        "updated_at": None,
        "session_date": _current_session_date(),
        "session_start_total_profit": None,
        "session_daily_pnl_usd": 0.0,
        "session_trade_count": 0,
        "in_trade": False,
        "current_position": 0,
        "current_contracts": 0,
        "open_order_count": 0,
        "open_position_count": 0,
        "topstep_max_contracts": bot.SCALING_TIER_3_CONTRACTS,
        "topstep_scaling_plan_limit_mnq": None,
        "last_routed_signal_id": "",
        "last_routed_signal_session_date": "",
        "last_dry_run_signal_id": "",
        "last_dry_run_session_date": "",
        "last_submit_response": None,
        "last_reconcile": {},
        "last_validate_at": None,
        "last_loop_minute": "",
        "last_hub_message_at": None,
        "last_user_trade_id": None,
        "last_order_submit_started_at": None,
        "last_order_submitted_at": None,
        "last_order_signal_id": "",
        "last_order_custom_tag": "",
        "last_signal_built_at": None,
        "last_signal_bar_close_at": None,
        "last_fill_logged_trade_id": None,
        "latency_bar_to_signal_ms": None,
        "latency_signal_to_submit_ms": None,
        "latency_submit_to_fill_ms": None,
        "last_signal_entry_price": None,
        "last_signal_stop_price": None,
        "last_signal_target_price": None,
        "awaiting_entry_fill": False,
        "awaiting_exit_fill": False,
        "rolling_entry_slippage_ticks": [],
        "rolling_exit_slippage_ticks": [],
        "last_data_gap_failure": "",
        "last_known_account_balance": None,
        "last_known_account_equity": None,
        "last_known_account_profit": 0.0,
        "last_seen_contract_name": "",
        "last_seen_contract_code": "",
        "last_contract_rollover_warning": "",
        "last_session_finalize_date": "",
        "payout_cycle_started_at": None,
        "payout_window_cumulative_pnl": 0.0,
        "payout_window_best_day_pnl": 0.0,
        "all_time_best_day_pnl": 0.0,
        "consistency_mode": "none",
        "consistency_limit": None,
        "consistency_ratio": 0.0,
        "consistency_status": "clear",
        "last_heavy_reconcile_at": None,
        "manual_flatten_cooldown_until": None,
        "manual_flatten_reason": "",
        "consecutive_data_gaps": 0,
        "last_data_gap_at": None,
        "peak_account_balance": None,
        "last_mll_alert_level": 0,  # 0=clear, 1=warning(<50% buffer), 2=critical(<25% buffer)
    }


def _normalize_state(state: Dict[str, Any]) -> Dict[str, Any]:
    normalized = _default_state()
    normalized.update(state or {})
    return normalized


def _load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_PATH):
        return _default_state()
    with open(STATE_PATH, "r", encoding="utf-8") as fh:
        return _normalize_state(json.load(fh))


def _save_state(state: Dict[str, Any]) -> None:
    state["updated_at"] = datetime.now(bot.TIMEZONE).isoformat()
    _write_json(STATE_PATH, state)


def _kill_switch_active() -> bool:
    return os.path.exists(KILL_SWITCH_PATH)


def _check_kill_switch_or_raise() -> None:
    if _kill_switch_active():
        raise TopstepXAPIError(f"HALT.txt is active at {KILL_SWITCH_PATH}. Execution is blocked.")


def engage_kill_switch(reason: str) -> None:
    os.makedirs(os.path.dirname(KILL_SWITCH_PATH), exist_ok=True)
    with open(KILL_SWITCH_PATH, "w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "engaged_at": datetime.now(bot.TIMEZONE).isoformat(),
                    "reason": reason or "manual",
                },
                indent=2,
            )
        )
    _write_log("WARN", f"Kill switch engaged: {reason or 'manual'}")
    print(f"Kill switch engaged: {KILL_SWITCH_PATH}")


def clear_kill_switch() -> None:
    if os.path.exists(KILL_SWITCH_PATH):
        os.remove(KILL_SWITCH_PATH)
        _write_log("INFO", "Kill switch cleared.")
        print(f"Kill switch cleared: {KILL_SWITCH_PATH}")
    else:
        print("Kill switch was not active.")


def _maybe_auto_clear_data_gap_kill_switch() -> bool:
    """Auto-clear kill switch if it was a data gap and enough time has passed."""
    if not _kill_switch_active():
        return False
    try:
        with open(KILL_SWITCH_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("reason") != "data_integrity_failure":
            return False
        engaged_at = _parse_iso_timestamp(data.get("engaged_at"))
        if engaged_at is None:
            return False
        elapsed_min = (_current_ct_now() - engaged_at.astimezone(bot.TIMEZONE)).total_seconds() / 60
        if elapsed_min >= DATA_GAP_AUTO_CLEAR_MINUTES:
            clear_kill_switch()
            _write_log("INFO", f"data_gap_kill_switch_auto_cleared after {elapsed_min:.0f} min")
            _send_telegram_lines([
                "MNQ Bot: Data-gap kill switch auto-cleared",
                f"Was active for {elapsed_min:.0f} min. Resuming normal trading.",
            ])
            return True
    except Exception as exc:
        # Fail-safe: on any error reading/parsing HALT.txt, leave the kill switch
        # ENGAGED (return False = not auto-cleared). Log it so the swallow is visible.
        _write_log("WARN", f"data_gap_auto_clear_check_failed (kill switch stays engaged): {exc}",
                   error_only=True)
    return False


def _write_json(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)


def _append_jsonl(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload) + "\n")


def _signal_id_from_signal(signal: Dict[str, Any]) -> str:
    return (
        f"V29-{signal.get('entry_type', 'NONE')}-{signal.get('direction', 'NONE')}-"
        f"{str(signal.get('entry_timestamp', '')).replace(':', '').replace('+', '_')}"
    )


def _parse_iso_timestamp(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value)
    try:
        if text.endswith("Z"):
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _duration_ms(start_value: Any, end_value: Any) -> Optional[float]:
    start_dt = _parse_iso_timestamp(start_value)
    end_dt = _parse_iso_timestamp(end_value)
    if start_dt is None or end_dt is None:
        return None
    return max(0.0, (end_dt - start_dt).total_seconds() * 1000.0)


def _safe_lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _build_trade_note_row(
    event_type: str,
    signal_payload: Dict[str, Any],
    signal: Optional[Dict[str, Any]],
    account_name: str = "",
    contract_name: str = "",
    dry_run: bool = True,
    executed: bool = False,
    notes: str = "",
    broker_response: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    signal = signal or {}
    return {
        "logged_at": datetime.now(bot.TIMEZONE).isoformat(),
        "event_type": event_type,
        "signal_id": _signal_id_from_signal(signal) if signal else "",
        "account_name": account_name,
        "contract_name": contract_name,
        "dry_run": dry_run,
        "executed": executed,
        "entry_timestamp": signal.get("entry_timestamp", ""),
        "session_date": signal.get("session_date", ""),
        "entry_type": signal.get("entry_type", ""),
        "direction": signal.get("direction", ""),
        "regime": signal.get("regime", ""),
        "contracts": signal.get("contracts", 0),
        "entry_price": signal.get("entry_price", 0.0),
        "stop_price": signal.get("stop_price", 0.0),
        "target_price": signal.get("target_price", 0.0),
        "stop_width_ticks": signal.get("stop_width_ticks", 0.0),
        "target_ticks": signal.get("target_ticks", 0),
        "reward_risk_ratio": signal.get("reward_risk_ratio", 0.0),
        "entry_hour": signal.get("entry_hour", 0),
        "day_of_week": signal.get("day_of_week", ""),
        "month": signal.get("month", 0),
        "year": signal.get("year", 0),
        "atr_at_entry": signal.get("atr_at_entry", 0.0),
        "atr_ratio_at_entry": signal.get("atr_ratio_at_entry", 0.0),
        "adx_at_entry": signal.get("adx_at_entry", 0.0),
        "vwap_at_entry": signal.get("vwap_at_entry", 0.0),
        "price_distance_from_vwap": signal.get("price_distance_from_vwap", 0.0),
        "ema_spread_at_entry": signal.get("ema_spread_at_entry", 0.0),
        "fvg_type": signal.get("fvg_type", ""),
        "fvg_size_ticks": signal.get("fvg_size_ticks", 0.0),
        "fvg_age_bars": signal.get("fvg_age_bars", 0),
        "fvg_quality_score": signal.get("fvg_quality_score", 0),
        "fvg_quality_flags": signal.get("fvg_quality_flags", ""),
        "fb_level_type": signal.get("fb_level_type", ""),
        "fb_level_price": signal.get("fb_level_price", 0.0),
        "fb_sweep_distance_ticks": signal.get("fb_sweep_distance_ticks", 0.0),
        "fb_reentry_distance_ticks": signal.get("fb_reentry_distance_ticks", 0.0),
        "prev_day_high": signal.get("prev_day_high", 0.0),
        "prev_day_low": signal.get("prev_day_low", 0.0),
        "globex_high": signal.get("globex_high", 0.0),
        "globex_low": signal.get("globex_low", 0.0),
        "opening_gap_points": signal.get("opening_gap_points", 0.0),
        "is_news_day": signal.get("is_news_day", False),
        "drawdown_pct_at_entry": signal.get("drawdown_pct_at_entry", 0.0),
        "daily_pnl_at_entry": signal.get("daily_pnl_at_entry", 0.0),
        "buffer_above_floor_at_entry": signal.get("buffer_above_floor_at_entry", 0.0),
        "combine_profit_at_entry": signal.get("combine_profit_at_entry", 0.0),
        "combine_target_remaining": signal.get("combine_target_remaining", 0.0),
        "daily_loss_used_pct": signal.get("daily_loss_used_pct", 0.0),
        "mll_used_pct": signal.get("mll_used_pct", 0.0),
        "startup_audit_status": signal_payload.get("startup_audit_status", "SKIPPED"),
        "thesis": signal.get("thesis", ""),
        "notes": notes,
        "broker_response": json.dumps(broker_response) if broker_response else "",
    }


def _append_trade_note(row: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(TRADE_NOTES_CSV_PATH), exist_ok=True)
    file_exists = os.path.exists(TRADE_NOTES_CSV_PATH)
    with open(TRADE_NOTES_CSV_PATH, "a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=TRADE_NOTE_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow({field: row.get(field, "") for field in TRADE_NOTE_FIELDS})


def _log_trade_event(
    event_type: str,
    signal_payload: Dict[str, Any],
    signal: Optional[Dict[str, Any]] = None,
    account_name: str = "",
    contract_name: str = "",
    dry_run: bool = True,
    executed: bool = False,
    notes: str = "",
    broker_response: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    signal = signal or signal_payload.get("signal") or {}
    event = {
        "logged_at": datetime.now(bot.TIMEZONE).isoformat(),
        "event_type": event_type,
        "signal_id": _signal_id_from_signal(signal) if signal else "",
        "run_mode": signal_payload.get("run_mode", bot.RUN_MODE),
        "execution_profile": signal_payload.get("execution_profile", bot.EXECUTION_PROFILE),
        "signal": signal,
        "account_name": account_name,
        "contract_name": contract_name,
        "dry_run": dry_run,
        "executed": executed,
        "notes": notes,
        "broker_response": broker_response,
    }
    if extra:
        event["extra"] = extra
    _append_jsonl(TELEMETRY_JSONL_PATH, event)
    _append_trade_note(
        _build_trade_note_row(
            event_type=event_type,
            signal_payload=signal_payload,
            signal=signal,
            account_name=account_name,
            contract_name=contract_name,
            dry_run=dry_run,
            executed=executed,
            notes=notes,
            broker_response=broker_response,
        )
    )
    _write_log(
        "INFO" if event_type not in {"submit_blocked_open_orders", "submit_blocked_open_positions"} else "WARN",
        f"{event_type} signal_id={event.get('signal_id', '')} account={account_name or '-'} contract={contract_name or '-'} notes={notes}",
    )


def _ensure_not_killed() -> None:
    if _kill_switch_active():
        raise TopstepXAPIError(f"Kill switch is active at {KILL_SWITCH_PATH}. Remove it to resume routing.")


def _extract_numeric(record: Dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        if key not in record:
            continue
        value = record.get(key)
        try:
            if value is None or value == "":
                continue
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _extract_position_size(position: Dict[str, Any]) -> int:
    for key in ("size", "netPos", "quantity", "qty", "positionSize"):
        value = position.get(key)
        try:
            return abs(int(float(value)))
        except (TypeError, ValueError):
            continue
    return 0


def _extract_signed_position_size(position: Dict[str, Any]) -> int:
    for key in ("size", "netPos", "quantity", "qty", "positionSize"):
        value = position.get(key)
        try:
            return int(float(value))
        except (TypeError, ValueError):
            continue
    return 0


def _order_matches_contract(order: Dict[str, Any], contract_id: str) -> bool:
    return str(order.get("contractId", "")) == str(contract_id)


def _has_matching_position(open_positions: List[Dict[str, Any]], contract_id: str) -> bool:
    return any(_order_matches_contract(position, contract_id) for position in open_positions)


def _is_protective_stop_like_order(order: Dict[str, Any], entry_side: int, contract_id: str) -> bool:
    if not _order_matches_contract(order, contract_id):
        return False

    descriptor = " ".join(
        _safe_lower(order.get(key))
        for key in ("type", "typeName", "name", "orderType", "status", "customTag")
    )
    stop_price = order.get("stopPrice")
    try:
        opposite_side = int(order.get("side")) != int(entry_side)
    except (TypeError, ValueError):
        opposite_side = False

    has_stop_descriptor = any(token in descriptor for token in ("stop", "loss", "sl"))
    has_stop_price = stop_price not in (None, "", 0, 0.0)
    # ProjectX uses numeric order-type codes; 4 = stop / stop-loss bracket. The
    # string descriptor may be empty when only a numeric code is returned, so
    # recognize the code directly to avoid a false "unprotected" flatten.
    order_type_code = str(order.get("type", order.get("orderType", ""))).strip()
    has_stop_type_code = order_type_code in ("4", "stop", "stop_limit", "stoplimit")
    return bool(opposite_side and (has_stop_descriptor or has_stop_price or has_stop_type_code))


def _user_hub_confirms_protective_order(event: Dict[str, Any], order_payload: Dict[str, Any]) -> bool:
    if event.get("event_type") != "GatewayUserOrder":
        return False
    payload = event.get("payload") or {}
    return _is_protective_stop_like_order(
        payload,
        entry_side=int(order_payload["side"]),
        contract_id=str(order_payload["contractId"]),
    )


def _extract_account_metrics(account: Dict[str, Any], config: TopstepXConfig) -> Dict[str, Optional[float]]:
    balance = _extract_numeric(
        account,
        "balance",
        "accountBalance",
        "cashBalance",
        "cash",
        "netLiq",
        "netLiquidation",
        "netLiquidatingValue",
        "equity",
        "equityValue",
    )
    equity = _extract_numeric(
        account,
        "netLiq",
        "netLiquidation",
        "netLiquidatingValue",
        "equity",
        "equityValue",
        "balance",
        "accountBalance",
        "cashBalance",
    )
    profit = _extract_numeric(
        account,
        "netProfit",
        "profitAndLoss",
        "pnl",
        "accountPnl",
        "realizedPnl",
        "realizedPnL",
        "closedPnl",
        "totalPnL",
        "totalPnl",
    )
    day_pnl = _extract_numeric(
        account,
        "dayPnl",
        "dailyPnl",
        "todayPnl",
        "sessionPnl",
    )
    if profit is None:
        for candidate in (equity, balance):
            if candidate is None:
                continue
            if candidate > (config.topstep_account_size_usd * 0.5):
                profit = candidate - float(config.topstep_account_size_usd)
            else:
                profit = candidate
            break

    scaling_profit = None
    if balance is not None:
        scaling_profit = balance - float(config.topstep_account_size_usd) if balance > (config.topstep_account_size_usd * 0.5) else balance
    elif profit is not None:
        scaling_profit = profit

    return {
        "balance": balance,
        "equity": equity if equity is not None else balance,
        "total_profit": profit,
        "day_pnl": day_pnl,
        "scaling_profit": scaling_profit,
    }


def _consistency_profile(config: TopstepXConfig) -> Dict[str, Optional[float]]:
    stage = str(config.topstep_account_stage or "").strip().lower()
    if stage == "combine":
        return {"mode": "combine", "limit": 0.50}
    if stage in {"xfa_consistency", "xfa-consistency", "consistency"}:
        return {"mode": "xfa_consistency", "limit": 0.40}
    return {"mode": "none", "limit": None}


def _reset_payout_window(state: Dict[str, Any], reason: str) -> None:
    state["payout_cycle_started_at"] = _current_ct_now().isoformat()
    state["payout_window_cumulative_pnl"] = 0.0
    state["payout_window_best_day_pnl"] = 0.0
    state["consistency_ratio"] = 0.0
    state["consistency_status"] = "clear"
    _write_log("WARN", f"Payout window reset: {reason}")


def _finalize_session_if_needed(state: Dict[str, Any]) -> None:
    session_date = str(state.get("session_date", ""))
    if not session_date or state.get("last_session_finalize_date") == session_date:
        return
    session_pnl = float(state.get("session_daily_pnl_usd", 0.0) or 0.0)
    state["payout_window_cumulative_pnl"] = float(state.get("payout_window_cumulative_pnl", 0.0) or 0.0) + session_pnl
    state["payout_window_best_day_pnl"] = max(
        float(state.get("payout_window_best_day_pnl", 0.0) or 0.0),
        session_pnl,
    )
    state["all_time_best_day_pnl"] = max(
        float(state.get("all_time_best_day_pnl", 0.0) or 0.0),
        session_pnl,
    )
    state["last_session_finalize_date"] = session_date


def _start_new_session(state: Dict[str, Any], session_date: str, current_profit: Optional[float]) -> None:
    state["session_date"] = session_date
    state["session_start_total_profit"] = float(current_profit) if current_profit is not None else 0.0
    state["session_daily_pnl_usd"] = 0.0
    state["session_trade_count"] = 0
    state["last_loop_minute"] = ""
    _write_log("INFO", f"Session reset for {session_date}.")


def _live_consistency_snapshot(state: Dict[str, Any], config: TopstepXConfig) -> Dict[str, Any]:
    profile = _consistency_profile(config)
    limit = profile["limit"]
    cumulative = float(state.get("payout_window_cumulative_pnl", 0.0) or 0.0)
    best_day = float(state.get("payout_window_best_day_pnl", 0.0) or 0.0)
    current_day = float(state.get("session_daily_pnl_usd", 0.0) or 0.0)
    live_cumulative = cumulative + current_day
    live_best_day = max(best_day, current_day)
    ratio = (live_best_day / live_cumulative) if limit and live_cumulative > 0 and live_best_day > 0 else 0.0
    warning_threshold = (limit * CONSISTENCY_WARNING_FRACTION) if limit else None
    throttle_threshold = (limit * CONSISTENCY_THROTTLE_FRACTION) if limit else None

    status = "clear"
    if limit:
        protected_limit = max(0.0, limit - CONSISTENCY_BUFFER)
        if ratio >= protected_limit:
            status = "block"
        elif throttle_threshold is not None and ratio >= throttle_threshold:
            status = "throttle"
        elif warning_threshold is not None and ratio >= warning_threshold:
            status = "warn"

    return {
        "mode": profile["mode"],
        "limit": limit,
        "warning_threshold": warning_threshold,
        "throttle_threshold": throttle_threshold,
        "live_cumulative_pnl": live_cumulative,
        "live_best_day_pnl": live_best_day,
        "ratio": ratio,
        "status": status,
        "protected_limit": (max(0.0, limit - CONSISTENCY_BUFFER) if limit else None),
    }


def _update_consistency_state(state: Dict[str, Any], config: TopstepXConfig) -> Dict[str, Any]:
    snapshot = _live_consistency_snapshot(state, config)
    state["consistency_mode"] = snapshot["mode"]
    state["consistency_limit"] = snapshot["limit"]
    state["consistency_ratio"] = snapshot["ratio"]
    state["consistency_status"] = snapshot["status"]
    state["consistency_protected_limit"] = snapshot.get("protected_limit")
    return snapshot


def _quarter_month_code(month: int) -> str:
    if month <= 3:
        return "H"
    if month <= 6:
        return "M"
    if month <= 9:
        return "U"
    return "Z"


def _extract_contract_code(contract: Optional[Dict[str, Any]]) -> str:
    if not contract:
        return ""
    joined = " ".join(str(contract.get(key, "")) for key in ("name", "symbol", "description"))
    match = re.search(r"\b(?:MNQ|NQ)([HMUZ])(\d{1,2})\b", joined.upper())
    if match:
        return f"{match.group(1)}{match.group(2)}"
    return ""


def _contract_rollover_warning(contract: Optional[Dict[str, Any]]) -> str:
    contract_code = _extract_contract_code(contract)
    if not contract_code:
        return ""

    now = _current_ct_now()
    current_quarter_code = _quarter_month_code(now.month)
    current_quarter_month = {v: k for k, v in QUARTER_MONTH_CODES.items()}.get(current_quarter_code, now.month)
    contract_month_code = contract_code[0]

    if now.month in QUARTER_MONTH_CODES and now.day >= 10 and contract_month_code == current_quarter_code:
        return (
            f"Contract {contract_code} is still on the current quarterly month during a rollover window. "
            "Review the active MNQ contract before the next session."
        )
    if now.month > current_quarter_month and contract_month_code == current_quarter_code:
        return (
            f"Contract {contract_code} appears stale relative to the current calendar quarter. "
            "Review TOPSTEPX_CONTRACT before routing orders."
        )
    return ""


def _bars_to_strategy_df(bars: List[Dict[str, Any]]):
    if not bars:
        raise TopstepXAPIError("ProjectX returned no historical bars for the selected contract.")

    records: List[Dict[str, Any]] = []
    for bar_record in bars:
        timestamp = bar_record.get("t")
        if not timestamp:
            continue
        records.append(
            {
                "ts_event": timestamp,
                "open": float(bar_record.get("o", 0.0)),
                "high": float(bar_record.get("h", 0.0)),
                "low": float(bar_record.get("l", 0.0)),
                "close": float(bar_record.get("c", 0.0)),
                "volume": float(bar_record.get("v", 0.0)),
            }
        )
    if not records:
        raise TopstepXAPIError("ProjectX returned bar payloads without timestamps.")

    df = bot.pd.DataFrame.from_records(records)
    df["ts_event"] = bot.pd.to_datetime(df["ts_event"], utc=True).dt.tz_convert("America/Chicago")
    df = df.sort_values("ts_event")
    df = df.drop_duplicates(subset="ts_event", keep="last")
    df = df.set_index("ts_event")
    df = df.between_time("08:30", "15:15")
    df = df[["open", "high", "low", "close", "volume"]]
    if df.empty:
        raise TopstepXAPIError("ProjectX live bar retrieval produced an empty RTH dataset after filtering.")
    session_dates = bot.pd.Series(df.index.date, index=df.index)
    diffs = df.index.to_series().diff()
    bad_gaps = diffs[(session_dates == session_dates.shift(1)) & (diffs > bot.pd.Timedelta(minutes=5))]
    if not bad_gaps.empty:
        gap_at = bad_gaps.index[0]
        gap_value = bad_gaps.iloc[0]
        raise TopstepXAPIError(
            f"DATA_INTEGRITY_FAILURE: missing strategy bars detected near {gap_at.isoformat()} gap={gap_value}."
        )
    return df


def build_live_strategy_signal(
    client: TopstepXClient,
    config: TopstepXConfig,
    lookback_bars: int = LIVE_BAR_LOOKBACK_BARS,
) -> Dict[str, Any]:
    audit = bot.run_startup_audit() if bot.PRODUCTION_AUDIT_ENABLED else {}
    contract = client.resolve_contract(config.contract_search_text, live=config.live_data)
    if contract is None:
        raise TopstepXAPIError(f"Could not resolve contract for search text '{config.contract_search_text}'.")

    now_utc = datetime.utcnow()
    start_utc = now_utc - timedelta(days=45)
    bars = client.retrieve_bars(
        contract_id=str(contract["id"]),
        start_time=start_utc.replace(microsecond=0).isoformat() + "Z",
        end_time=now_utc.replace(microsecond=0).isoformat() + "Z",
        live=config.live_data,
        unit=2,
        unit_number=5,
        limit=lookback_bars,
        include_partial_bar=False,
    )
    df = _bars_to_strategy_df(bars)
    data_summary = bot.validate_loaded_data(df)
    df = bot.add_indicators(df)
    df = bot.generate_signals(df)
    session_levels = bot.compute_session_levels(df)
    live_signal_sink: Dict[str, Any] = {}
    _, _, _, _ = bot.run_backtest(df, session_levels, live_signal_sink=live_signal_sink)

    payload = {
        "version": "V29",
        "generated_at": datetime.now(bot.TIMEZONE).isoformat(),
        "bar_close_at": df.index[-1].isoformat(),
        "run_mode": bot.RUN_MODE,
        "execution_profile": bot.EXECUTION_PROFILE,
        "data_source": "projectx_history_retrieveBars",
        "data_summary": data_summary,
        "contract": contract,
        "signal": live_signal_sink or None,
        "startup_audit_status": audit.get("status", "SKIPPED"),
    }
    _write_json(SIGNAL_EXPORT_PATH, payload)
    _write_log(
        "INFO",
        f"live_signal_snapshot_built contract={contract.get('name', '')} bars={data_summary.get('rows', 0)} path={SIGNAL_EXPORT_PATH}",
    )
    _log_trade_event(
        event_type="live_signal_snapshot",
        signal_payload=payload,
        contract_name=str(contract.get("name", "")),
        notes="Strategy signal snapshot built from ProjectX historical bars.",
    )
    return payload


def _apply_hub_account_update(state: Dict[str, Any], payload: Dict[str, Any]) -> None:
    metrics = _extract_account_metrics(payload, TopstepXConfig.from_env())
    if metrics.get("balance") is not None:
        _bal = metrics["balance"]
        state["last_known_account_balance"] = _bal
        _peak = state.get("peak_account_balance")
        if _peak is None or float(_bal) > float(_peak):
            state["peak_account_balance"] = _bal
    if metrics.get("equity") is not None:
        state["last_known_account_equity"] = metrics["equity"]
    if metrics.get("total_profit") is not None:
        state["last_known_account_profit"] = metrics["total_profit"]


def _record_slippage(window: Optional[List[float]], intended: float, actual: float) -> List[float]:
    """Append |actual-intended| in ticks to a rolling window (last SLIPPAGE_WINDOW)."""
    w = list(window or [])
    w.append(round(abs(float(actual) - float(intended)) / bot.MNQ_TICK_SIZE, 2))
    return w[-SLIPPAGE_WINDOW:]


def _emit_slippage_log_and_halt(kind: str, window: List[float], intended: float,
                                actual: float, halt_reason: str) -> None:
    """Log the latest slippage sample + rolling average; halt if the average
    exceeds SLIPPAGE_ALERT_TICKS over a full window."""
    slip_ticks = window[-1] if window else 0.0
    avg_slip = sum(window) / len(window) if window else 0.0
    _write_log(
        "INFO",
        f"{kind}_slippage ticks={slip_ticks} rolling_avg={avg_slip:.2f} "
        f"n={len(window)} intended={intended} actual={actual}",
    )
    if len(window) >= SLIPPAGE_WINDOW and avg_slip > SLIPPAGE_ALERT_TICKS:
        _write_log(
            "ERROR",
            f"{halt_reason} rolling_avg={avg_slip:.2f} ticks > {SLIPPAGE_ALERT_TICKS}; "
            f"engaging kill switch.",
            error_only=True,
        )
        engage_kill_switch(halt_reason)


def _process_user_hub_events(events: List[Dict[str, Any]], state: Dict[str, Any], config: TopstepXConfig) -> None:
    if not events:
        return
    account_name = str(state.get("last_reconcile", {}).get("account_name", config.account_name))
    for event in events:
        event_type = str(event.get("event_type", ""))
        payload = event.get("payload", {}) or {}
        state["last_hub_message_at"] = event.get("logged_at")
        if event_type == "GatewayUserAccount":
            _apply_hub_account_update(state, payload)
            _write_log("INFO", f"user_hub_account_update balance={payload.get('balance')}")
        elif event_type == "GatewayUserPosition":
            try:
                signed_size = int(float(payload.get("size", 0)))
            except (TypeError, ValueError):
                signed_size = 0
            state["in_trade"] = bool(signed_size)
            state["current_position"] = signed_size
            try:
                state["current_contracts"] = abs(signed_size)
            except (TypeError, ValueError):
                state["current_contracts"] = 0
            _write_log(
                "INFO",
                f"user_hub_position_update contract={payload.get('contractId', '')} size={payload.get('size', 0)}",
            )
        elif event_type == "GatewayUserOrder":
            order_status = str(payload.get("status", "")).lower()
            if order_status in {"working", "open", "accepted", "new"}:
                state["open_order_count"] = max(1, int(state.get("open_order_count", 0) or 0))
            elif order_status in {"filled", "cancelled", "canceled", "rejected", "complete", "done"}:
                state["open_order_count"] = max(0, int(state.get("open_order_count", 0) or 0) - 1)
            _log_trade_event(
                event_type="user_hub_order",
                signal_payload={"signal": {}, "run_mode": bot.RUN_MODE, "execution_profile": bot.EXECUTION_PROFILE},
                account_name=account_name,
                contract_name=str(payload.get("contractId", "")),
                dry_run=False,
                executed=False,
                notes=f"User hub order event status={payload.get('status')} orderId={payload.get('id')}",
                broker_response=payload,
            )
        elif event_type == "GatewayUserTrade":
            trade_id = payload.get("id")
            if trade_id == state.get("last_user_trade_id"):
                continue
            state["last_user_trade_id"] = trade_id
            state["session_trade_count"] = int(state.get("session_trade_count", 0) or 0) + 1
            state["in_trade"] = True
            trade_pnl = _extract_numeric(payload, "profitAndLoss", "pnl", "profit")
            if trade_pnl is not None:
                state["session_daily_pnl_usd"] = float(state.get("session_daily_pnl_usd", 0.0) or 0.0) + float(trade_pnl)
            try:
                signed_trade_size = int(float(payload.get("size", state.get("current_position", 0))))
                state["current_position"] = signed_trade_size
                state["current_contracts"] = abs(signed_trade_size)
            except (TypeError, ValueError):
                pass
            fill_timestamp = (
                payload.get("fillTime")
                or payload.get("timestamp")
                or payload.get("tradeTime")
                or event.get("logged_at")
            )
            latency_submit_to_fill_ms = _duration_ms(state.get("last_order_submitted_at"), fill_timestamp)
            if latency_submit_to_fill_ms is not None:
                state["latency_submit_to_fill_ms"] = latency_submit_to_fill_ms

            # ── Live slippage monitoring ───────────────────────────────────────
            # Compare actual fill price to intended. Entry fills (awaiting_entry_fill,
            # no realized P&L) are compared to the intended entry price. Exit fills
            # (awaiting_exit_fill, realized P&L present) are compared to the NEARER of
            # the intended stop / target price. The realized-P&L gate keeps entry
            # partial-fills from being mistaken for exits. The backtest assumes ~1
            # tick; if a rolling average exceeds SLIPPAGE_ALERT_TICKS the edge
            # assumption is broken -> halt.
            actual = _extract_numeric(payload, "price", "fillPrice", "averagePrice", "avgPrice")
            if state.get("awaiting_entry_fill") and trade_pnl is None:
                intended = state.get("last_signal_entry_price")
                if intended and actual:
                    state["rolling_entry_slippage_ticks"] = _record_slippage(
                        state.get("rolling_entry_slippage_ticks"), intended, actual)
                    state["awaiting_entry_fill"] = False
                    state["awaiting_exit_fill"] = True   # arm exit monitoring
                    _emit_slippage_log_and_halt(
                        "entry", state["rolling_entry_slippage_ticks"], intended, actual,
                        "slippage_systematic_excess")
            elif state.get("awaiting_exit_fill") and trade_pnl is not None:
                _stop_px = state.get("last_signal_stop_price")
                _tgt_px = state.get("last_signal_target_price")
                candidates = [(k, float(v)) for k, v in
                              (("stop", _stop_px), ("target", _tgt_px)) if v]
                if actual and candidates:
                    exit_kind, intended = min(candidates, key=lambda c: abs(float(actual) - c[1]))
                    state["rolling_exit_slippage_ticks"] = _record_slippage(
                        state.get("rolling_exit_slippage_ticks"), intended, actual)
                    state["awaiting_exit_fill"] = False
                    _emit_slippage_log_and_halt(
                        f"exit_{exit_kind}", state["rolling_exit_slippage_ticks"], intended, actual,
                        "exit_slippage_systematic_excess")
            _write_log(
                "INFO",
                "execution_latency "
                f"bar_to_signal_ms={state.get('latency_bar_to_signal_ms')} "
                f"signal_to_submit_ms={state.get('latency_signal_to_submit_ms')} "
                f"submit_to_fill_ms={state.get('latency_submit_to_fill_ms')}",
            )
            _log_trade_event(
                event_type="user_hub_trade",
                signal_payload={"signal": {}, "run_mode": bot.RUN_MODE, "execution_profile": bot.EXECUTION_PROFILE},
                account_name=account_name,
                contract_name=str(payload.get("contractId", "")),
                dry_run=False,
                executed=True,
                notes=(
                    f"User hub trade fill id={trade_id} size={payload.get('size')} "
                    f"pnl={payload.get('profitAndLoss')} "
                    f"bar_to_signal_ms={state.get('latency_bar_to_signal_ms')} "
                    f"signal_to_submit_ms={state.get('latency_signal_to_submit_ms')} "
                    f"submit_to_fill_ms={state.get('latency_submit_to_fill_ms')}"
                ),
                broker_response=payload,
            )
        elif event_type == "user_hub_connected":
            _write_log("INFO", "user_hub_connected")
        elif event_type == "user_hub_error":
            _write_log("ERROR", f"user_hub_error {payload.get('message', '')}", error_only=True)


def _symbol_fallback_max_contracts(symbol: str) -> int:
    """Symbol-aware fallback max position size when the broker API does not
    return a maxContracts field.

    Topstep expresses the $50K scaling limit as NQ-equivalent lots
    (SCALING_TIER_3_CONTRACTS = 5). One NQ lot = 10 MNQ, so:
      - MNQ (micro):  5 lots x 10 = 50 MNQ
      - NQ  (mini) :  5 lots      =  5 NQ
    MUST be checked MNQ-first because the substring "NQ" is contained in "MNQ".
    """
    s = (symbol or "").upper()
    if "MNQ" in s or "MICRO" in s:
        return bot.SCALING_TIER_3_CONTRACTS * 10   # 50 MNQ
    return bot.SCALING_TIER_3_CONTRACTS            # 5 NQ (also the safe default)


def _infer_topstep_max_contracts(account: Dict[str, Any], signal: Dict[str, Any],
                                 symbol: str = "") -> int:
    candidate_keys = (
        "maxContracts",
        "maxContractSize",
        "maxPositionSize",
        "maximumPositionSize",
        "allowedContracts",
    )
    for key in candidate_keys:
        value = account.get(key)
        if value is None:
            continue
        try:
            parsed = int(value)
            if parsed > 0:
                return parsed
        except (TypeError, ValueError):
            continue
    # API did not provide a max-contracts field. Fall back to the symbol-aware
    # maximum (NOT the raw NQ-lot count of 5, which would cap MNQ 10x too low).
    fallback = _symbol_fallback_max_contracts(symbol)
    _write_log(
        "WARN",
        f"maxContracts absent from broker API; using symbol-aware fallback "
        f"symbol={symbol or 'unknown'} fallback_max_contracts={fallback}",
    )
    return fallback


def _evaluate_signal_risk_halts(
    signal: Dict[str, Any],
    runtime_state: Optional[Dict[str, Any]],
    config: TopstepXConfig,
) -> Optional[str]:
    now = _current_ct_now()
    session_date = str(signal.get("session_date", ""))
    try:
        month = int(str(session_date).split("-")[1]) if session_date else int(signal.get("month", 0))
    except (ValueError, IndexError, TypeError):
        month = int(signal.get("month", 0) or 0)
    if bot.SKIP_JULY and month == 7:
        return "Signal blocked because July trading is disabled in the locked strategy."
    if (now.hour, now.minute) >= (bot.HARD_FLATTEN_H, bot.HARD_FLATTEN_M):
        return (
            f"Signal blocked because the hard flatten time {bot.HARD_FLATTEN_H:02d}:{bot.HARD_FLATTEN_M:02d} CT "
            "has already passed."
        )
    if (now.hour, now.minute) > (bot.ENTRY_CUTOFF_H, bot.ENTRY_CUTOFF_M):
        return (
            f"Signal blocked because the entry cutoff time {bot.ENTRY_CUTOFF_H:02d}:{bot.ENTRY_CUTOFF_M:02d} CT "
            "has already passed."
        )

    runtime_state = _normalize_state(runtime_state or {})
    try:
        daily_pnl = float(runtime_state.get("session_daily_pnl_usd", signal.get("daily_pnl_at_entry", 0.0)))
        if daily_pnl <= float(bot.BOT_DAILY_LOSS_LIMIT):
            return (
                f"Signal blocked because daily P&L {daily_pnl:.2f} is beyond the daily loss limit "
                f"{bot.BOT_DAILY_LOSS_LIMIT:.2f}."
            )
    except (TypeError, ValueError):
        pass
    try:
        combine_profit = float(runtime_state.get("last_known_account_profit", signal.get("combine_profit_at_entry", 0.0)))
        if config.topstep_account_stage == "combine" and combine_profit >= bot.COMBINE_PROFIT_TARGET:
            return (
                f"Signal blocked because combine profit {combine_profit:.2f} has already reached the target "
                f"{bot.COMBINE_PROFIT_TARGET:.2f}."
            )
    except (TypeError, ValueError):
        pass
    if runtime_state.get("consistency_status") == "block":
        return (
            f"Signal blocked because the live consistency ratio {float(runtime_state.get('consistency_ratio', 0.0)):.2%} "
            f"has reached the protected limit for stage '{config.topstep_account_stage}'."
        )
    return None


def _topstepx_scaling_plan_limit_mnq(
    config: TopstepXConfig,
    signal: Optional[Dict[str, Any]] = None,
    current_profit: Optional[float] = None,
) -> Optional[int]:
    stage = str(config.topstep_account_stage or "").lower()
    if stage not in {"xfa", "xfa_standard", "xfa_consistency", "xfa-consistency"}:
        return None

    account_size = int(config.topstep_account_size_usd)
    profit_basis = float(current_profit) if current_profit is not None else 0.0
    if account_size == 50_000:
        if profit_basis:
            tier_lots = int(bot.compute_scaling_tier(profit_basis))
        else:
            tier_lots = int((signal or {}).get("scaling_tier_at_entry", bot.compute_scaling_tier(0.0)))
        if tier_lots <= 0:
            tier_lots = bot.SCALING_TIER_1_CONTRACTS
        return tier_lots * 10

    if account_size == 100_000:
        if profit_basis < 1_500.0:
            return 30
        if profit_basis < 2_000.0:
            return 40
        if profit_basis < 3_000.0:
            return 50
        return 100

    if account_size == 150_000:
        if profit_basis < 1_500.0:
            return 30
        if profit_basis < 2_000.0:
            return 40
        if profit_basis < 3_000.0:
            return 50
        if profit_basis < 4_500.0:
            return 100
        return 150

    return None


def _unprotected_position_detected(state: Dict[str, Any], config: TopstepXConfig) -> bool:
    """True when live routing holds an open position with zero working orders
    (i.e. no protective stop). Dry-run and routing-disabled modes never qualify."""
    if not config.enable_order_routing or config.dry_run:
        return False
    try:
        positions = int(state.get("open_position_count", 0) or 0)
        orders = int(state.get("open_order_count", 0) or 0)
    except (TypeError, ValueError):
        return False
    return positions > 0 and orders == 0


def _orphan_orders_detected(state: Dict[str, Any], config: TopstepXConfig) -> bool:
    """True when live routing is FLAT (no position) but working orders remain --
    e.g. a take-profit filled and its sibling stop was never cancelled. Such an
    orphan order can later fill and open an unintended, unprotected position."""
    if not config.enable_order_routing or config.dry_run:
        return False
    try:
        positions = int(state.get("open_position_count", 0) or 0)
        orders = int(state.get("open_order_count", 0) or 0)
    except (TypeError, ValueError):
        return False
    return positions == 0 and orders > 0


def reconcile_state(client: TopstepXClient, config: TopstepXConfig) -> Dict[str, Any]:
    state = _load_state()
    now = _current_ct_now()
    session_date = _current_session_date(now)
    previous_session = str(state.get("session_date", ""))
    if previous_session and previous_session != session_date:
        _finalize_session_if_needed(state)

    account = _require_single_account(client.search_accounts(True), config.account_name)
    account_id = int(account["id"])
    contract = client.resolve_contract(config.contract_search_text, live=config.live_data)
    open_orders = client.search_open_orders(account_id)
    open_positions = client.search_open_positions(account_id)
    metrics = _extract_account_metrics(account, config)

    if not previous_session or previous_session != session_date:
        _start_new_session(state, session_date, metrics.get("total_profit"))

    previous_total_profit = state.get("last_known_account_profit")
    current_total_profit = metrics.get("total_profit")
    stage = str(config.topstep_account_stage or "").lower()
    if (
        stage.startswith("xfa")
        and previous_total_profit is not None
        and current_total_profit is not None
        and float(previous_total_profit) - float(current_total_profit) >= 250.0
    ):
        _reset_payout_window(
            state,
            reason=(
                f"Detected account profit drop from {float(previous_total_profit):.2f} "
                f"to {float(current_total_profit):.2f}; treating it as a payout/reset event."
            ),
        )

    if state.get("session_start_total_profit") is None:
        state["session_start_total_profit"] = float(current_total_profit) if current_total_profit is not None else 0.0

    if current_total_profit is not None and state.get("session_start_total_profit") is not None:
        state["session_daily_pnl_usd"] = float(current_total_profit) - float(state.get("session_start_total_profit", 0.0))
    elif metrics.get("day_pnl") is not None:
        state["session_daily_pnl_usd"] = float(metrics["day_pnl"])

    prior_in_trade = bool(state.get("in_trade"))
    state["in_trade"] = bool(open_positions)
    state["current_position"] = sum(_extract_signed_position_size(position) for position in open_positions)
    state["current_contracts"] = sum(_extract_position_size(position) for position in open_positions)
    state["open_order_count"] = len(open_orders)
    state["open_position_count"] = len(open_positions)
    state["topstep_max_contracts"] = _infer_topstep_max_contracts(
        account, {}, symbol=config.contract_search_text)
    state["topstep_scaling_plan_limit_mnq"] = _topstepx_scaling_plan_limit_mnq(
        config,
        current_profit=metrics.get("scaling_profit"),
    )
    _bal = metrics.get("balance")
    state["last_known_account_balance"] = _bal
    if _bal is not None:
        _peak = state.get("peak_account_balance")
        if _peak is None or float(_bal) > float(_peak):
            state["peak_account_balance"] = _bal
    state["last_known_account_equity"] = metrics.get("equity")
    state["last_known_account_profit"] = metrics.get("total_profit")
    state["last_seen_contract_name"] = str(contract.get("name", "")) if contract else ""
    state["last_seen_contract_code"] = _extract_contract_code(contract)
    rollover_warning = _contract_rollover_warning(contract)
    if rollover_warning:
        if state.get("last_contract_rollover_warning") != rollover_warning:
            _write_log("WARN", rollover_warning)
        state["last_contract_rollover_warning"] = rollover_warning
    else:
        state["last_contract_rollover_warning"] = ""

    if prior_in_trade != state["in_trade"]:
        _write_log(
            "WARN",
            f"Broker position state changed during reconciliation. prior_in_trade={prior_in_trade} current_in_trade={state['in_trade']}",
        )

    # Unprotected-position guard: a live position with ZERO working orders has no
    # protective stop attached — the dangerous account/bot state mismatch. Halt
    # and flatten rather than leave an unbracketed position exposed. Reconcile is
    # periodic (outside the entry race that bracket-verification already covers),
    # so positions>0 with orders==0 here is a genuine red flag, not a timing blip.
    if _unprotected_position_detected(state, config):
        _write_log(
            "ERROR",
            f"account_state_mismatch: {state['open_position_count']} open position(s) "
            f"with 0 working orders (no protective stop). Engaging kill switch + flatten.",
            error_only=True,
        )
        _send_telegram_lines([
            "MNQ Bot: UNPROTECTED POSITION detected",
            f"{state['open_position_count']} position(s), 0 working orders. Flattening + halting.",
        ])
        engage_kill_switch("unprotected_position_state_mismatch")
        try:
            _flatten_account_internal(client, config, reason="unprotected_position_state_mismatch")
        except Exception as exc:
            _write_log("ERROR", f"flatten_after_state_mismatch_failed: {exc}", error_only=True)

    # Orphan-order cleanup (E4): account is FLAT but working orders remain (e.g. a
    # take-profit filled and its sibling stop was left working). Cancel the
    # leftovers so a stale order cannot fill and open an unintended position.
    # Guards against a mid-entry race: skip while awaiting an entry fill or within
    # 120s of a submit, when working orders may belong to a position about to open.
    if _orphan_orders_detected(state, config) and not state.get("awaiting_entry_fill"):
        _since_submit = _duration_ms(state.get("last_order_submitted_at"),
                                     datetime.now(bot.TIMEZONE).isoformat())
        if _since_submit is None or _since_submit > 120_000:
            cancelled = 0
            for _o in open_orders:
                _oid = _o.get("id") if _o.get("id") is not None else _o.get("orderId")
                if _oid is None:
                    continue
                try:
                    client.cancel_order(account_id, int(_oid))
                    cancelled += 1
                except Exception as exc:
                    _write_log("ERROR", f"orphan_order_cancel_failed id={_oid}: {exc}", error_only=True)
            if cancelled:
                _write_log("WARN", f"orphan_orders_cancelled count={cancelled} "
                                   f"(account flat with working orders)")
                _send_telegram_lines([
                    "MNQ Bot: orphan orders cancelled",
                    f"{cancelled} working order(s) with no open position — cleaned up.",
                ])
                state["open_order_count"] = 0

    consistency = _update_consistency_state(state, config)
    reconcile = {
        "reconciled_at": datetime.now(bot.TIMEZONE).isoformat(),
        "account_id": account_id,
        "account_name": str(account.get("name", account_id)),
        "account_snapshot": account,
        "account_metrics": metrics,
        "contract_snapshot": contract,
        "open_orders": open_orders,
        "open_positions": open_positions,
        "topstep_max_contracts": state["topstep_max_contracts"],
        "topstep_scaling_plan_limit_mnq": state["topstep_scaling_plan_limit_mnq"],
        "topstep_account_stage": config.topstep_account_stage,
        "topstep_account_size_usd": config.topstep_account_size_usd,
        "session_date": session_date,
        "consistency": consistency,
        "contract_rollover_warning": state["last_contract_rollover_warning"],
    }
    state["last_reconcile"] = reconcile
    state["last_heavy_reconcile_at"] = reconcile["reconciled_at"]
    _save_state(state)
    _write_log(
        "INFO",
        f"reconcile_state account={reconcile['account_name']} open_orders={len(open_orders)} "
        f"open_positions={len(open_positions)} session_pnl={float(state.get('session_daily_pnl_usd', 0.0)):.2f}",
    )
    return reconcile


def build_strategy_signal() -> Dict[str, Any]:
    live_signal_sink: Dict[str, Any] = {}

    audit = bot.run_startup_audit() if bot.PRODUCTION_AUDIT_ENABLED else {}
    df = bot.fetch_data()
    data_summary = bot.validate_loaded_data(df)
    df = bot.add_indicators(df)
    df = bot.generate_signals(df)
    session_levels = bot.compute_session_levels(df)
    _, _, _, _ = bot.run_backtest(df, session_levels, live_signal_sink=live_signal_sink)

    payload = {
        "version": "V29",
        "generated_at": datetime.now(bot.TIMEZONE).isoformat(),
        "run_mode": bot.RUN_MODE,
        "execution_profile": bot.EXECUTION_PROFILE,
        "data_summary": data_summary,
        "signal": live_signal_sink or None,
        "startup_audit_status": audit.get("status", "SKIPPED"),
    }
    _write_json(SIGNAL_EXPORT_PATH, payload)
    _write_log("INFO", f"signal_snapshot_built path={SIGNAL_EXPORT_PATH}")
    _log_trade_event(
        event_type="signal_snapshot",
        signal_payload=payload,
        notes="Strategy signal snapshot built from frozen V29 logic.",
    )
    print(f"Signal export saved to: {SIGNAL_EXPORT_PATH}")
    if live_signal_sink:
        print(
            "Current live signal:"
            f" {live_signal_sink['entry_type']} {live_signal_sink['direction']}"
            f" {live_signal_sink['contracts']} MNQ"
            f" @ {live_signal_sink['entry_price']}"
        )
    else:
        print("Current live signal: none")
    return payload


def _require_single_account(accounts, requested_name: str) -> Dict[str, Any]:
    if requested_name:
        matches = [a for a in accounts if str(a.get("name", "")).strip().lower() == requested_name.strip().lower()]
        if not matches:
            raise TopstepXAPIError(f"Account '{requested_name}' was not found.")
        return matches[0]
    if len(accounts) != 1:
        names = ", ".join(str(a.get("name", a.get("id"))) for a in accounts)
        raise TopstepXAPIError(
            f"Multiple accounts found ({names}). Set TOPSTEPX_ACCOUNT_NAME explicitly."
        )
    return accounts[0]


def build_order_plan(
    signal_payload: Dict[str, Any],
    config: TopstepXConfig,
    client: Optional[TopstepXClient],
    runtime_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    _check_kill_switch_or_raise()
    signal = signal_payload.get("signal")
    if not signal:
        raise TopstepXAPIError("No current strategy signal is available to route.")

    state = _normalize_state(runtime_state or _load_state())
    halt_reason = _evaluate_signal_risk_halts(signal, state, config)
    if halt_reason:
        _log_trade_event(
            event_type="signal_blocked_risk_halt",
            signal_payload=signal_payload,
            dry_run=bool(config.dry_run),
            notes=halt_reason,
        )
        raise TopstepXAPIError(halt_reason)

    account = None
    contract = None
    order_symbol = config.contract_search_text
    # Default to the symbol-aware fallback (MNQ->50) rather than the raw NQ-lot
    # count (5), so no-client/dry-run plans don't show a misleading 5-contract cap.
    # Replaced with the API/account value below when a client is present.
    topstep_max_contracts = _symbol_fallback_max_contracts(order_symbol)
    scaling_plan_limit_mnq: Optional[int] = state.get("topstep_scaling_plan_limit_mnq")
    if client is not None:
        accounts = client.search_accounts(True)
        account = _require_single_account(accounts, config.account_name)
        contract = client.resolve_contract(config.contract_search_text, live=config.live_data)
        if contract is None:
            raise TopstepXAPIError(f"Could not resolve contract for search text '{config.contract_search_text}'.")
        order_symbol = str(contract.get("name") or contract.get("symbol") or config.contract_search_text)
        topstep_max_contracts = _infer_topstep_max_contracts(account, signal, symbol=order_symbol)
        if scaling_plan_limit_mnq is None:
            metrics = _extract_account_metrics(account, config)
            scaling_plan_limit_mnq = _topstepx_scaling_plan_limit_mnq(
                config,
                signal=signal,
                current_profit=metrics.get("scaling_profit"),
            )

    stop_ticks = round(abs(float(signal["entry_price"]) - float(signal["stop_price"])) / bot.MNQ_TICK_SIZE)
    target_ticks = int(signal["target_ticks"])
    side = 0 if signal["direction"] == "long" else 1
    final_size = max(1, min(int(signal["contracts"]), int(topstep_max_contracts)))
    if scaling_plan_limit_mnq is not None:
        final_size = min(final_size, int(scaling_plan_limit_mnq))

    consistency_note = ""
    if state.get("consistency_status") == "throttle":
        throttled_size = min(final_size, bot.SCALING_TIER_1_CONTRACTS)
        if throttled_size < final_size:
            consistency_note = (
                f"Consistency throttle active at {float(state.get('consistency_ratio', 0.0)):.2%}; "
                f"size reduced from {final_size} to {throttled_size}."
            )
            final_size = throttled_size

    # Dynamic MLL proximity guard: scale contracts proportionally to remaining trailing
    # drawdown buffer. Buffer = current_balance - (peak_balance - $2,000).
    # At 100% buffer → full contracts. At 50% → half contracts. At 0% → 1 contract.
    mll_note = ""
    if client is not None:
        _cur_bal = float(state.get("last_known_account_balance") or 0.0)
        _peak_bal = float(state.get("peak_account_balance") or _cur_bal)
        _mll_total = float(bot.EOD_LOSS_BUFFER)
        if _cur_bal > 0 and _peak_bal > 0 and _mll_total > 0:
            _buffer = _cur_bal - (_peak_bal - _mll_total)
            _fraction = max(0.0, min(1.0, _buffer / _mll_total))
            _mll_size = max(1, int(final_size * _fraction))
            if _mll_size < final_size:
                mll_note = (
                    f"MLL guard: buffer ${_buffer:.0f}/{_mll_total:.0f} "
                    f"({_fraction:.0%}) — size {final_size}→{_mll_size}"
                )
                final_size = _mll_size
                _last_lvl = int(state.get("last_mll_alert_level") or 0)
                _new_lvl = 2 if _fraction < 0.25 else 1 if _fraction < 0.50 else 0
                if _new_lvl > _last_lvl:
                    _send_telegram_lines([
                        f"MNQ Bot: MLL BUFFER {'CRITICAL' if _new_lvl == 2 else 'WARNING'}",
                        f"Buffer: ${_buffer:.0f} remaining ({_fraction:.0%} of ${_mll_total:.0f})",
                        f"Contracts scaled: {final_size} (normal: {int(signal['contracts'])})",
                        f"Account: ${_cur_bal:.0f}  Peak: ${_peak_bal:.0f}",
                    ])
                    state["last_mll_alert_level"] = _new_lvl
                elif _new_lvl < _last_lvl:
                    state["last_mll_alert_level"] = _new_lvl

    # Size audit trail before every order: requested vs broker max vs final.
    _write_log(
        "INFO",
        f"order_sizing symbol={order_symbol} requested={int(signal['contracts'])} "
        f"max_allowed={int(topstep_max_contracts)} "
        f"scaling_plan_limit_mnq={scaling_plan_limit_mnq} final_size={int(final_size)}",
    )

    payload: Dict[str, Any] = {
        "version": "V29",
        "generated_at": datetime.now(bot.TIMEZONE).isoformat(),
        "dry_run": config.dry_run,
        "signal": signal,
        "projectx_order": {
            "accountId": account.get("id") if account else None,
            "accountName": account.get("name") if account else config.account_name,
            "contractId": contract.get("id") if contract else None,
            "contractName": contract.get("name") if contract else config.contract_search_text,
            "type": 2,
            "side": side,
            "size": final_size,
            "requestedSize": int(signal["contracts"]),
            "topstepMaxContracts": int(topstep_max_contracts),
            "topstepScalingPlanLimitMnq": scaling_plan_limit_mnq,
            "topstepAccountStage": config.topstep_account_stage,
            "topstepAccountSizeUsd": int(config.topstep_account_size_usd),
            "consistencyMode": state.get("consistency_mode"),
            "consistencyRatio": state.get("consistency_ratio"),
            "consistencyStatus": state.get("consistency_status"),
            "customTag": (
                f"V29-{signal['entry_type']}-{signal['direction']}-"
                f"{signal['entry_timestamp'].replace(':', '').replace('+', '_')}"
            ),
            "stopLossBracket": {
                "ticks": stop_ticks,
                "type": 4,
            },
            "takeProfitBracket": {
                "ticks": target_ticks,
                "type": 1,
            },
        },
    }
    _write_json(ORDER_PLAN_EXPORT_PATH, payload)
    _log_trade_event(
        event_type="order_plan_built",
        signal_payload=signal_payload,
        account_name=str(payload["projectx_order"].get("accountName", "")),
        contract_name=str(payload["projectx_order"].get("contractName", "")),
        dry_run=bool(config.dry_run),
        notes=consistency_note or "ProjectX order plan built from current strategy signal.",
        extra={"order_plan": payload["projectx_order"]},
    )
    print(f"Order plan saved to: {ORDER_PLAN_EXPORT_PATH}")
    return payload


def smoke_test() -> None:
    config = TopstepXConfig.from_env()
    client = TopstepXClient(config)
    client.authenticate()
    session = client.validate_session()
    accounts = client.search_accounts(True)
    contract = client.resolve_contract(config.contract_search_text, live=config.live_data)

    print("TopstepX smoke test passed.")
    print(f"  session_success={session.get('success', True)}")
    print(f"  active_accounts={len(accounts)}")
    if accounts:
        print("  accounts=" + ", ".join(str(a.get("name", a.get("id"))) for a in accounts))
    if contract:
        print(f"  resolved_contract={contract.get('name', '')} ({contract.get('id', '')})")
    else:
        print(f"  resolved_contract=none for search '{config.contract_search_text}'")
    _write_log("INFO", f"smoke_test_passed accounts={len(accounts)} contract={contract.get('name', '') if contract else 'none'}")
    _append_jsonl(
        TELEMETRY_JSONL_PATH,
        {
            "logged_at": datetime.now(bot.TIMEZONE).isoformat(),
            "event_type": "smoke_test",
            "account_names": [str(a.get("name", a.get("id"))) for a in accounts],
            "resolved_contract": contract.get("name", "") if contract else "",
            "resolved_contract_id": contract.get("id", "") if contract else "",
            "dry_run": config.dry_run,
        },
    )


def _submit_order_plan(
    client: TopstepXClient,
    config: TopstepXConfig,
    signal_payload: Dict[str, Any],
    plan: Dict[str, Any],
    state: Dict[str, Any],
    execute: bool,
    user_stream: Optional[TopstepXUserHubStream] = None,
) -> Dict[str, Any]:
    _check_kill_switch_or_raise()
    order_payload = dict(plan["projectx_order"])
    account_id = order_payload["accountId"]
    contract_id = order_payload["contractId"]
    signal = signal_payload.get("signal") or {}
    signal_id = _signal_id_from_signal(signal) if signal else ""

    if account_id is None or contract_id is None:
        raise TopstepXAPIError("Order plan is missing account or contract resolution.")

    reconcile = reconcile_state(client, config)
    open_orders = reconcile.get("open_orders", []) or []
    open_positions = reconcile.get("open_positions", []) or []
    if open_orders:
        _log_trade_event(
            event_type="submit_blocked_open_orders",
            signal_payload=signal_payload,
            account_name=str(order_payload.get("accountName", "")),
            contract_name=str(order_payload.get("contractName", "")),
            dry_run=bool(config.dry_run),
            notes="Signal routing blocked because open orders already exist on the selected account.",
            extra={"open_orders": open_orders},
        )
        raise TopstepXAPIError("Refusing to route signal: open orders already exist on the selected account.")
    if open_positions:
        _log_trade_event(
            event_type="submit_blocked_open_positions",
            signal_payload=signal_payload,
            account_name=str(order_payload.get("accountName", "")),
            contract_name=str(order_payload.get("contractName", "")),
            dry_run=bool(config.dry_run),
            notes="Signal routing blocked because open positions already exist on the selected account.",
            extra={"open_positions": open_positions},
        )
        raise TopstepXAPIError("Refusing to route signal: open positions already exist on the selected account.")
    if (
        state.get("last_routed_signal_id") == signal_id
        and state.get("last_routed_signal_session_date") == signal.get("session_date")
    ):
        _log_trade_event(
            event_type="submit_blocked_duplicate_signal",
            signal_payload=signal_payload,
            account_name=str(order_payload.get("accountName", "")),
            contract_name=str(order_payload.get("contractName", "")),
            dry_run=bool(config.dry_run),
            notes="Signal routing blocked because this signal ID was already processed for the same session.",
        )
        raise TopstepXAPIError("Refusing to route signal: duplicate signal detected for the same session.")

    cooldown_until_raw = state.get("manual_flatten_cooldown_until")
    if cooldown_until_raw:
        try:
            cooldown_until_dt = datetime.fromisoformat(str(cooldown_until_raw))
            if _current_ct_now() < cooldown_until_dt:
                cooldown_msg = (
                    f"Signal blocked: manual flatten cooldown active until {cooldown_until_raw} "
                    f"(reason={state.get('manual_flatten_reason', '?')}). "
                    f"Cooldown prevents immediate re-entry after a manual close."
                )
                _log_trade_event(
                    event_type="submit_blocked_manual_flatten_cooldown",
                    signal_payload=signal_payload,
                    account_name=str(order_payload.get("accountName", "")),
                    contract_name=str(order_payload.get("contractName", "")),
                    dry_run=bool(config.dry_run),
                    notes=cooldown_msg,
                )
                raise TopstepXAPIError(cooldown_msg)
        except ValueError:
            pass  # malformed timestamp — let the trade through

    if not execute or config.dry_run or not config.enable_order_routing:
        state["last_dry_run_signal_id"] = signal_id
        state["last_dry_run_session_date"] = signal.get("session_date", "")
        _save_state(state)
        _log_trade_event(
            event_type="order_dry_run",
            signal_payload=signal_payload,
            account_name=str(order_payload.get("accountName", "")),
            contract_name=str(order_payload.get("contractName", "")),
            dry_run=True,
            notes="Dry-run order plan generated; no order routed.",
            extra={"order_plan": order_payload},
        )
        print("Dry run only. Order was not sent.")
        print(json.dumps(order_payload, indent=2))
        return {"dry_run": True, "order_plan": order_payload}

    submit_started_at = datetime.now(bot.TIMEZONE).isoformat()
    _check_kill_switch_or_raise()
    response = client.place_order(
        account_id=int(account_id),
        contract_id=str(contract_id),
        side=int(order_payload["side"]),
        size=int(order_payload["size"]),
        order_type=int(order_payload["type"]),
        custom_tag=str(order_payload["customTag"]),
        stop_loss_bracket=dict(order_payload["stopLossBracket"]),
        take_profit_bracket=dict(order_payload["takeProfitBracket"]),
    )
    submitted_at = datetime.now(bot.TIMEZONE).isoformat()
    state["last_order_submit_started_at"] = submit_started_at
    state["last_order_submitted_at"] = submitted_at
    # Capture intended entry / stop / target prices so the entry fill and the
    # exit fill can each be compared to intended for live slippage monitoring
    # (see GatewayUserTrade handler).
    try:
        _entry_px = float(signal.get("entry_price", 0.0)) or None
        _stop_px = float(signal.get("stop_price", 0.0)) or None
        _tgt_ticks = float(signal.get("target_ticks", 0.0))
        if _entry_px and _tgt_ticks:
            _tgt_off = _tgt_ticks * bot.MNQ_TICK_SIZE
            _tgt_px = (_entry_px + _tgt_off) if signal.get("direction") == "long" else (_entry_px - _tgt_off)
        else:
            _tgt_px = None
    except (TypeError, ValueError):
        _entry_px = _stop_px = _tgt_px = None
    state["last_signal_entry_price"] = _entry_px
    state["last_signal_stop_price"] = _stop_px
    state["last_signal_target_price"] = _tgt_px
    state["awaiting_entry_fill"] = _entry_px is not None
    state["awaiting_exit_fill"] = False   # armed only after the entry fill is seen
    state["last_order_signal_id"] = signal_id
    state["last_order_custom_tag"] = str(order_payload.get("customTag", ""))
    state["latency_signal_to_submit_ms"] = _duration_ms(signal_payload.get("generated_at"), submitted_at)
    state["last_routed_signal_id"] = signal_id
    state["last_routed_signal_session_date"] = signal.get("session_date", "")
    state["last_submit_response"] = response
    _save_state(state)
    _log_trade_event(
        event_type="order_submitted",
        signal_payload=signal_payload,
        account_name=str(order_payload.get("accountName", "")),
        contract_name=str(order_payload.get("contractName", "")),
        dry_run=False,
        executed=True,
        notes=f"ProjectX order submitted successfully. signal_to_submit_ms={state.get('latency_signal_to_submit_ms')}",
        broker_response=response,
        extra={"order_plan": order_payload},
    )
    _verify_brackets_after_submit(client, config, signal_payload, state, order_payload, user_stream=user_stream)
    print("Order submitted successfully.")
    print(json.dumps(response, indent=2))
    return response


def _verify_brackets_after_submit(
    client: TopstepXClient,
    config: TopstepXConfig,
    signal_payload: Dict[str, Any],
    state: Dict[str, Any],
    order_payload: Dict[str, Any],
    user_stream: Optional[TopstepXUserHubStream] = None,
) -> None:
    if config.dry_run or not config.enable_order_routing:
        return
    time.sleep(ORDER_BRACKET_VERIFY_DELAY_SECONDS)
    _check_kill_switch_or_raise()
    account_id = int(order_payload["accountId"])
    contract_id = str(order_payload["contractId"])
    entry_side = int(order_payload["side"])
    deadline = time.monotonic() + BRACKET_VERIFY_TIMEOUT_SECONDS
    next_rest_poll = 0.0
    open_orders: List[Dict[str, Any]] = []
    open_positions: List[Dict[str, Any]] = []
    saw_hub_confirmation = False

    while time.monotonic() < deadline:
        _check_kill_switch_or_raise()

        if user_stream is not None:
            remaining = max(0.1, min(0.5, deadline - time.monotonic()))
            event = user_stream.get_event(timeout=remaining)
            events: List[Dict[str, Any]] = []
            if event is not None:
                events.append(event)
                events.extend(user_stream.drain_events())
            if events:
                _process_user_hub_events(events, state, config)
                _save_state(state)
                if any(_user_hub_confirms_protective_order(item, order_payload) for item in events):
                    saw_hub_confirmation = True

        now_monotonic = time.monotonic()
        if now_monotonic < next_rest_poll:
            continue

        open_orders = client.search_open_orders(account_id)
        open_positions = client.search_open_positions(account_id)
        next_rest_poll = now_monotonic + BRACKET_VERIFY_REST_POLL_SECONDS

        if not _has_matching_position(open_positions, contract_id):
            return

        has_protective_stop = any(
            _is_protective_stop_like_order(order, entry_side=entry_side, contract_id=contract_id)
            for order in open_orders
        )
        if has_protective_stop or saw_hub_confirmation:
            _write_log("INFO", f"bracket_verification_passed signal_id={state.get('last_order_signal_id', '')}")
            return

    _log_trade_event(
        event_type="bracket_verification_failed",
        signal_payload=signal_payload,
        account_name=str(order_payload.get("accountName", "")),
        contract_name=str(order_payload.get("contractName", "")),
        dry_run=False,
        executed=True,
        notes="Position was open after submit but no protective stop was found; emergency flatten triggered.",
        extra={"open_orders": open_orders, "open_positions": open_positions},
    )
    _flatten_account_internal(client, config, reason="unprotected_position_after_entry")
    # Engage the persistent kill switch: a bracket failure means the protective
    # stop is not reliably being attached. Halt fully rather than let the run
    # loop route another potentially-unprotected trade on the next bar. Requires
    # a manual clear-kill-switch after the cause is understood.
    engage_kill_switch("bracket_failure_unprotected_position")
    raise TopstepXAPIError("Bracket verification failed: open position detected without protective stop. Emergency flatten triggered.")


def submit_signal(execute: bool) -> None:
    _ensure_not_killed()
    signal_payload = build_strategy_signal()
    config = TopstepXConfig.from_env()
    client = TopstepXClient(config)
    client.authenticate()
    client.validate_session()
    reconcile_state(client, config)
    state = _load_state()
    plan = build_order_plan(signal_payload, config, client, runtime_state=state)
    _submit_order_plan(client, config, signal_payload, plan, state, execute)


def _flatten_account_internal(
    client: TopstepXClient,
    config: TopstepXConfig,
    reason: str,
) -> Dict[str, Any]:
    account = _require_single_account(client.search_accounts(True), config.account_name)
    open_orders = client.search_open_orders(int(account["id"]))
    positions = client.search_open_positions(int(account["id"]))
    payload = {
        "account_name": str(account.get("name", account.get("id"))),
        "reason": reason,
        "open_orders": open_orders,
        "positions": positions,
    }
    if not positions and not open_orders:
        _log_trade_event(
            event_type="flatten_no_positions",
            signal_payload={"signal": {}, "run_mode": bot.RUN_MODE, "execution_profile": bot.EXECUTION_PROFILE},
            account_name=payload["account_name"],
            dry_run=bool(config.dry_run),
            notes=f"No open orders or positions to flatten. reason={reason}",
        )
        return payload
    if config.dry_run or not config.enable_order_routing:
        _log_trade_event(
            event_type="flatten_dry_run",
            signal_payload={"signal": {}, "run_mode": bot.RUN_MODE, "execution_profile": bot.EXECUTION_PROFILE},
            account_name=payload["account_name"],
            dry_run=True,
            notes=f"Dry-run flatten requested. reason={reason}",
            extra=payload,
        )
        return payload
    responses = []
    for order in open_orders:
        order_id = order.get("id")
        if order_id is None:
            continue
        response = client.cancel_order(int(account["id"]), int(order_id))
        responses.append({"cancel_order_id": int(order_id), "response": response})
        _log_trade_event(
            event_type="flatten_cancel_order",
            signal_payload={"signal": {}, "run_mode": bot.RUN_MODE, "execution_profile": bot.EXECUTION_PROFILE},
            account_name=payload["account_name"],
            dry_run=False,
            executed=True,
            notes=f"Flatten cancelled order {int(order_id)}. reason={reason}",
            broker_response=response,
        )
    for position in positions:
        response = client.close_contract(int(account["id"]), str(position["contractId"]))
        responses.append({"close_contract_id": str(position["contractId"]), "response": response})
        _log_trade_event(
            event_type="flatten_executed",
            signal_payload={"signal": {}, "run_mode": bot.RUN_MODE, "execution_profile": bot.EXECUTION_PROFILE},
            account_name=payload["account_name"],
            contract_name=str(position.get("contractId", "")),
            dry_run=False,
            executed=True,
            notes=f"Flattened contract {position.get('contractId', '')}. reason={reason}",
            broker_response=response,
        )
    payload["responses"] = responses

    # After a manual flatten (telegram or CLI), block new bot entries for a cooldown window.
    # This prevents the bot from immediately re-entering the same signal the user just closed.
    if reason in {"telegram_command", "manual"} and any(
        r.get("close_contract_id") for r in responses
    ):
        cooldown_state = _load_state()
        cooldown_until = (
            _current_ct_now() + timedelta(minutes=MANUAL_FLATTEN_COOLDOWN_MINUTES)
        ).isoformat()
        cooldown_state["manual_flatten_cooldown_until"] = cooldown_until
        cooldown_state["manual_flatten_reason"] = reason
        _save_state(cooldown_state)
        _write_log(
            "INFO",
            f"manual_flatten_cooldown set until {cooldown_until} reason={reason}",
        )

    return payload


def flatten_account(reason: str = "manual") -> None:
    config = TopstepXConfig.from_env()
    client = TopstepXClient(config)
    client.authenticate()
    client.validate_session()
    result = _flatten_account_internal(client, config, reason=reason)
    print(json.dumps(result, indent=2))


def reset_payout_window(reason: str = "manual") -> None:
    state = _load_state()
    _reset_payout_window(state, reason=reason)
    _save_state(state)
    print(f"Payout window reset in state file. reason={reason}")


def _ensure_authenticated(client: TopstepXClient, state: Dict[str, Any]) -> None:
    last_validate_at = state.get("last_validate_at")
    now = _current_ct_now()
    if not client.token:
        client.authenticate()
        client.validate_session()
        state["last_validate_at"] = now.isoformat()
        _save_state(state)
        return
    if not last_validate_at:
        client.validate_session()
        state["last_validate_at"] = now.isoformat()
        _save_state(state)
        return
    try:
        last_ts = datetime.fromisoformat(str(last_validate_at))
    except ValueError:
        last_ts = now - timedelta(seconds=DEFAULT_SESSION_VALIDATE_SECONDS + 1)
    if (now - last_ts).total_seconds() >= DEFAULT_SESSION_VALIDATE_SECONDS:
        client.validate_session()
        state["last_validate_at"] = now.isoformat()
        _save_state(state)


def _should_run_heavy_reconcile(state: Dict[str, Any]) -> bool:
    last_heavy = _parse_iso_timestamp(state.get("last_heavy_reconcile_at"))
    if last_heavy is None:
        return True
    return (_current_ct_now() - last_heavy.astimezone(bot.TIMEZONE)).total_seconds() >= HEAVY_RECONCILE_SECONDS


def _restart_user_stream(
    client: TopstepXClient,
    config: TopstepXConfig,
    user_stream: Optional[TopstepXUserHubStream],
) -> Optional[TopstepXUserHubStream]:
    if not config.enable_user_hub:
        return None
    if user_stream is not None:
        user_stream.stop()
    account = _require_single_account(client.search_accounts(True), config.account_name)
    restarted = TopstepXUserHubStream(token=str(client.token), account_id=int(account["id"]))
    restarted.start()
    _write_log("WARN", f"user_hub_stream_restarted account={account.get('name', account.get('id'))}")
    return restarted


TELEGRAM_REQUEST_TIMEOUT_SECONDS = 15
TELEGRAM_SEND_RETRIES = 3


def _telegram_settings() -> Dict[str, str]:
    return {
        "token": os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        "chat_id": os.getenv("TELEGRAM_CHAT_ID", "").strip(),
    }


def _telegram_ready() -> bool:
    s = _telegram_settings()
    return bool(s["token"] and s["chat_id"])


def _send_telegram_message(text: str) -> None:
    s = _telegram_settings()
    endpoint = f"https://api.telegram.org/bot{s['token']}/sendMessage"
    payload = parse.urlencode(
        {"chat_id": s["chat_id"], "text": text, "disable_web_page_preview": "true"}
    ).encode("utf-8")
    req = request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    for attempt in range(1, TELEGRAM_SEND_RETRIES + 1):
        try:
            with request.urlopen(req, timeout=TELEGRAM_REQUEST_TIMEOUT_SECONDS):
                return
        except (error.URLError, TimeoutError, socket.timeout, ssl.SSLError) as exc:
            if attempt == TELEGRAM_SEND_RETRIES:
                raise TopstepXAPIError(f"Telegram network error after {attempt} attempts: {exc}") from exc
            time.sleep(2.0)


def _send_telegram_lines(lines: List[str]) -> bool:
    if not _telegram_ready():
        return False
    try:
        _send_telegram_message("\n".join(str(l) for l in lines if str(l).strip()))
        _write_log("INFO", "telegram_alert_sent key=startup")
        return True
    except Exception as exc:
        _write_log("ERROR", f"telegram_alert_failed key=startup: {exc}", error_only=True)
        return False


def _send_startup_telegram_alert(config: TopstepXConfig, *, auto_submit: bool) -> None:
    _send_telegram_lines(
        [
            "MNQ Bot Started",
            f"Session date: {_current_session_date()}",
            f"Account: {config.account_name}",
            f"Auto-submit: {auto_submit}",
            f"Dry run: {config.dry_run}",
            f"Routing enabled: {config.enable_order_routing}",
        ]
    )


def run_loop(auto_submit: bool, interval_seconds: int, max_cycles: int) -> None:
    config = TopstepXConfig.from_env()
    client = TopstepXClient(config)
    user_stream: Optional[TopstepXUserHubStream] = None
    attempts = 0
    cycles = 0
    backoff = interval_seconds
    _write_log(
        "INFO",
        f"run_loop_started auto_submit={auto_submit} interval_seconds={interval_seconds} dry_run={config.dry_run}",
    )
    _send_startup_telegram_alert(config, auto_submit=auto_submit)

    while True:
        state = _load_state()
        _maybe_auto_clear_data_gap_kill_switch()
        try:
            _ensure_authenticated(client, state)
            if config.enable_user_hub and user_stream is None:
                user_stream = _restart_user_stream(client, config, user_stream)
            if _should_run_heavy_reconcile(state):
                before_reconcile = _normalize_state(state)
                reconcile_state(client, config)
                state = _load_state()
                if (
                    before_reconcile.get("current_contracts") != state.get("current_contracts")
                    or before_reconcile.get("session_daily_pnl_usd") != state.get("session_daily_pnl_usd")
                ):
                    _write_log(
                        "WARN",
                        "heavy_reconcile_overwrote_local_state "
                        f"contracts {before_reconcile.get('current_contracts')}->{state.get('current_contracts')} "
                        f"session_pnl {before_reconcile.get('session_daily_pnl_usd')}->{state.get('session_daily_pnl_usd')}",
                    )
            if user_stream is not None:
                first_event = user_stream.get_event(timeout=1.0)
                events: List[Dict[str, Any]] = []
                if first_event is not None:
                    events.append(first_event)
                    events.extend(user_stream.drain_events())
                if events:
                    _process_user_hub_events(events, state, config)
                    _save_state(state)
                last_hub_message_at = state.get("last_hub_message_at")
                if last_hub_message_at:
                    try:
                        hub_ts = datetime.fromisoformat(str(last_hub_message_at).replace("Z", "+00:00")).astimezone(bot.TIMEZONE)
                        if (_current_ct_now() - hub_ts).total_seconds() > HUB_STALE_SECONDS:
                            _write_log("WARN", f"user_hub_stale last_message_at={last_hub_message_at}")
                            _ensure_authenticated(client, state)
                            user_stream = _restart_user_stream(client, config, user_stream)
                    except ValueError:
                        pass
                elif not user_stream.is_connected:
                    _write_log("WARN", "user_hub_not_connected")
                    _ensure_authenticated(client, state)
                    user_stream = _restart_user_stream(client, config, user_stream)
            else:
                time.sleep(1)

            if _kill_switch_active():
                _write_log("WARN", "HALT.txt detected during run loop; new entries are blocked.")
                if (state.get("open_order_count") or state.get("open_position_count")) and (config.enable_order_routing and not config.dry_run):
                    _flatten_account_internal(client, config, reason="kill_switch")
                cycles += 1
                if max_cycles and cycles >= max_cycles:
                    break
                continue

            now = _current_ct_now()
            minute_key = now.strftime("%Y-%m-%d %H:%M")

            if (now.hour, now.minute) >= (bot.HARD_FLATTEN_H, bot.HARD_FLATTEN_M):
                if state.get("open_order_count") or state.get("open_position_count"):
                    _flatten_account_internal(client, config, reason="hard_flatten_time")
                state["last_loop_minute"] = minute_key
                _save_state(state)
                time.sleep(interval_seconds)
                cycles += 1
                if max_cycles and cycles >= max_cycles:
                    break
                continue

            if state.get("last_loop_minute") != minute_key:
                signal_payload = build_live_strategy_signal(client, config)
                state = _load_state()
                state["last_signal_built_at"] = signal_payload.get("generated_at")
                state["last_signal_bar_close_at"] = signal_payload.get("bar_close_at")
                state["latency_bar_to_signal_ms"] = _duration_ms(
                    signal_payload.get("bar_close_at"),
                    signal_payload.get("generated_at"),
                )
                state["last_loop_minute"] = minute_key
                state["consecutive_data_gaps"] = 0
                _save_state(state)
                try:
                    plan = build_order_plan(signal_payload, config, client, runtime_state=state)
                    if auto_submit:
                        _submit_order_plan(client, config, signal_payload, plan, state, execute=True, user_stream=user_stream)
                    else:
                        _write_log(
                            "INFO",
                            f"run_loop_minute_processed minute={minute_key} "
                            f"signal_id={_signal_id_from_signal(signal_payload.get('signal') or {})} "
                            f"bar_to_signal_ms={state.get('latency_bar_to_signal_ms')}",
                        )
                except TopstepXAPIError as exc:
                    if "No current strategy signal" in str(exc):
                        _write_log("INFO", f"run_loop_no_signal minute={minute_key}")
                    elif "DATA_INTEGRITY_FAILURE" in str(exc):
                        state["last_data_gap_failure"] = str(exc)
                        _save_state(state)
                        engage_kill_switch("data_integrity_failure")
                        raise
                    else:
                        raise

            attempts = 0
            backoff = interval_seconds
            cycles += 1
            if max_cycles and cycles >= max_cycles:
                break
        except KeyboardInterrupt:
            _write_log("WARN", "run_loop interrupted by user.")
            raise
        except TopstepXAPIError as exc:
            error_str = str(exc)
            if "DATA_INTEGRITY_FAILURE" in error_str:
                state = _load_state()
                gap_count = int(state.get("consecutive_data_gaps", 0) or 0) + 1
                state["consecutive_data_gaps"] = gap_count
                state["last_data_gap_at"] = _current_ct_now().isoformat()
                state["last_data_gap_failure"] = error_str
                _save_state(state)
                _write_log("WARN", f"data_gap_soft_skip gap={gap_count}/{DATA_GAP_KILL_THRESHOLD}: {error_str[:120]}")
                if gap_count >= DATA_GAP_KILL_THRESHOLD:
                    _send_telegram_lines([
                        "MNQ Bot: Data gap kill switch engaged",
                        f"{gap_count} consecutive missing-bar errors.",
                        f"Will auto-clear in {DATA_GAP_AUTO_CLEAR_MINUTES} min if data recovers.",
                    ])
                    engage_kill_switch("data_integrity_failure")
                    raise
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_RECONNECT_BACKOFF_SECONDS)
            else:
                attempts += 1
                max_retries = MAX_TRANSIENT_RETRIES if _is_transient_error(exc) else 10
                _write_log("ERROR", f"run_loop broker error attempt={attempts}/{max_retries}: {exc}", error_only=True)
                if attempts > max_retries:
                    raise
                if user_stream is not None:
                    user_stream.stop()
                    user_stream = None
                client = TopstepXClient(config)
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_RECONNECT_BACKOFF_SECONDS)
        except Exception as exc:  # pragma: no cover - defensive operational guard
            attempts += 1
            max_retries = MAX_TRANSIENT_RETRIES if _is_transient_error(exc) else 10
            _write_log("ERROR", f"run_loop unexpected error attempt={attempts}/{max_retries}: {exc}", error_only=True)
            if attempts > max_retries:
                raise
            if user_stream is not None:
                user_stream.stop()
                user_stream = None
            client = TopstepXClient(config)
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_RECONNECT_BACKOFF_SECONDS)
    if user_stream is not None:
        user_stream.stop()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TopstepX runtime shell for the frozen V29 strategy.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("smoke-test", help="Authenticate and verify account/contract access.")
    sub.add_parser("build-signal", help="Run the frozen strategy and export the current live signal snapshot.")
    sub.add_parser("build-live-signal", help="Build the current live signal snapshot from ProjectX historical bars.")
    sub.add_parser("show-state", help="Print the current persisted TopstepX runtime state.")
    sub.add_parser("reconcile-state", help="Refresh account, open-order, and open-position state.")
    engage = sub.add_parser("engage-kill-switch", help="Create HALT.txt to block new routing.")
    engage.add_argument("--reason", default="manual", help="Short reason stored in the halt file.")
    sub.add_parser("clear-kill-switch", help="Remove HALT.txt and allow routing again.")
    payout = sub.add_parser("reset-payout-window", help="Reset the tracked payout-cycle metrics in local state.")
    payout.add_argument("--reason", default="manual", help="Short reason recorded in the log.")

    submit = sub.add_parser("submit-signal", help="Build the current signal and prepare or submit a ProjectX order.")
    submit.add_argument("--execute", action="store_true", help="Actually send the order if routing is enabled.")

    flatten = sub.add_parser("flatten-account", help="Close all open positions on the selected account.")
    flatten.add_argument("--reason", default="manual", help="Short reason recorded in the log.")

    loop = sub.add_parser("run-loop", help="Continuously reconcile broker state and process the frozen signal once per minute.")
    loop.add_argument("--auto-submit", action="store_true", help="Submit orders when routing is enabled.")
    loop.add_argument(
        "--interval-seconds",
        type=int,
        default=DEFAULT_LOOP_INTERVAL_SECONDS,
        help="Polling interval for broker reconciliation.",
    )
    loop.add_argument(
        "--max-cycles",
        type=int,
        default=0,
        help="Stop after this many polling cycles. Use 0 to run until interrupted.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        if args.command == "smoke-test":
            smoke_test()
        elif args.command == "build-signal":
            build_strategy_signal()
        elif args.command == "build-live-signal":
            config = TopstepXConfig.from_env()
            client = TopstepXClient(config)
            client.authenticate()
            client.validate_session()
            payload = build_live_strategy_signal(client, config)
            print(f"Signal export saved to: {SIGNAL_EXPORT_PATH}")
            if payload.get("signal"):
                live_signal = payload["signal"]
                print(
                    "Current live signal:"
                    f" {live_signal['entry_type']} {live_signal['direction']}"
                    f" {live_signal['contracts']} MNQ"
                    f" @ {live_signal['entry_price']}"
                )
            else:
                print("Current live signal: none")
        elif args.command == "show-state":
            print(json.dumps(_load_state(), indent=2))
        elif args.command == "reconcile-state":
            config = TopstepXConfig.from_env()
            client = TopstepXClient(config)
            client.authenticate()
            client.validate_session()
            snapshot = reconcile_state(client, config)
            print(json.dumps(snapshot, indent=2))
        elif args.command == "engage-kill-switch":
            engage_kill_switch(reason=str(args.reason))
        elif args.command == "clear-kill-switch":
            clear_kill_switch()
        elif args.command == "reset-payout-window":
            reset_payout_window(reason=str(args.reason))
        elif args.command == "submit-signal":
            submit_signal(execute=bool(args.execute))
        elif args.command == "flatten-account":
            flatten_account(reason=str(args.reason))
        elif args.command == "run-loop":
            run_loop(
                auto_submit=bool(args.auto_submit),
                interval_seconds=max(1, int(args.interval_seconds)),
                max_cycles=max(0, int(args.max_cycles)),
            )
        else:
            raise TopstepXAPIError(f"Unknown command: {args.command}")
    except TopstepXAPIError as exc:
        _write_log("ERROR", str(exc), error_only=True)
        print(f"TopstepX integration error: {exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
