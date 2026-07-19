"""
SHADOW ENGINE draft artifact — research-only, advisory-only. NOT a strategy.

Produced by the Shadow Engine run of 2026-07-07 (pre-open). Per the frozen-core
mandate (CLAUDE.md + RESEARCH_LEDGER), NO strategy-parameter candidate was
staged: ghost-testing on one day's price action is curve-fitting by definition,
and every parameter the template proposed touching is either already validated
with its current value load-bearing (ATR clamp), already maximally loose
(FVG_MIN_SIZE_TICKS=2), or demonstrably not the bottleneck (CALM_ATR_RATIO —
regime was OPEN on 58/79 bars of the last zero-trade session).

What this file IS: a machine-checkable "shield" — the frozen baseline
tolerances a future shadow run compares live forensics against. Import it in
research scripts; never from src/.
"""


class V29SystemShield:
    """Frozen baseline + drift tolerances for shadow-engine comparisons."""

    # 7-year honest-fill baseline (vwap_only, commit-frozen 2026-07-04)
    BASELINE = {
        "net_usd": 416_436,
        "profit_factor": 4.10,
        "win_rate": 0.296,
        "avg_trade_usd": 154.0,
        "trades_per_day": 1.5,
        "zero_trade_day_rate": 0.219,     # 404/1847 sessions — normal, no prize on them
    }

    # Drift tolerances (PLAYBOOK tripwires — judge at sample, not per-day)
    TOLERANCES = {
        "min_trades_to_judge": 30,
        "wr_alarm_below": 0.25,           # over 30+ trades
        "slippage_alarm_ticks": 3.0,      # rolling avg; bot auto-halts here too
        "quiet_sessions_alarm": 8,        # beyond 7-year record
        "trade_rate_alarm_per_day": 0.6,  # over 30 sessions
    }

    # Telemetry expectations (infrastructure audit whitelist)
    TELEMETRY = {
        # Overnight artifact: bar->signal latency measures against yesterday's
        # 15:00 CT close outside RTH. Only meaningful 08:30-15:00 CT.
        # CALIBRATION (verified 2026-07-07): ProjectX labels bars by OPEN time,
        # so the metric includes the bar's own 300s span. Healthy in-session
        # readings run ~360-430s (span + minute cadence + feed publish delay).
        # Alert only above 600s = true bar-close latency > ~5 min (stalled).
        "latency_bar_to_signal_valid_window_ct": ("08:30", "15:00"),
        "latency_bar_to_signal_max_ms_in_session": 600_000,
        # Known-benign WARN: broker omits maxContracts -> symbol fallback 50.
        "whitelisted_warns": ("maxContracts absent from broker API",),
        "max_errors_per_session": 0,
        "max_hub_restarts_per_session": 3,
    }

    @classmethod
    def funnel_verdict(cls, zero_trade_sessions: int, total_sessions: int) -> str:
        """Gates are 'Choked' only with statistical evidence, never one quiet day."""
        if total_sessions == 0:
            return "Baseline"
        if zero_trade_sessions >= cls.TOLERANCES["quiet_sessions_alarm"]:
            return "Choked"
        return "Baseline"
