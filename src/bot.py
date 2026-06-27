"""
╔══════════════════════════════════════════════════════════════╗
║         ADVANCED MNQ TRADING BOT — V29                       ║
║                                                              ║
║  Changes from V26:                                           ║
║  - Instrument: MES → MNQ (4x volatility, $2/point)          ║
║  - Dual entry: ORB (9:30-10:00 CT) + FVG (all session)      ║
║  - ORB: dynamic target max(1x range, 20 ticks), 6-bar range  ║
║  - Max 4 trades/day (1 ORB + 3 FVG)                         ║
║  - ATR ratio hard cap at 2.0 (removes losing explosive zone) ║
║  - July disabled (36.3% WR, net negative over 7 years)       ║
║  - All V26 analytics layer preserved                         ║
║  Strategy logic: FVG + EMA bias + VWAP + ATR regime          ║
╚══════════════════════════════════════════════════════════════╝
"""

import sys, os
import argparse
import json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Tuple, Optional, Dict
import warnings
warnings.filterwarnings("ignore")

from src.load_data import load_mes_data

# ── CONFIG ────────────────────────────────────────────────────────────────────
DATA_PATH       = r"C:\Users\kyawz\Downloads\GLBX-20260331-885WT5W7KA\glbx-mdp3-20100606-20260329.ohlcv-1m.csv"
INIT_CASH       = 50_000.0
RUN_MODE        = "BACKTEST"
EXECUTION_PROFILE = "CONSERVATIVE"
LIVE_EXECUTION_ENABLED = False
PRODUCTION_AUDIT_ENABLED = True

# Strategy params
EMA_FAST        = 9
EMA_SLOW        = 21
RSI_PERIOD      = 14
RSI_OVERSOLD    = 30
RSI_OVERBOUGHT  = 55
ATR_PERIOD      = 14
ATR_REGIME_MULT = 0.8

# Risk params
KELLY_FRACTION  = 0.25
PRIOR_TRADES    = 20
PRIOR_WIN_RATE  = 0.45

# ── MNQ CONTRACT SPECS (changed from MES) ────────────────────────────────────
COMMISSION_PER_CONTRACT = 1.34
SLIPPAGE_TICKS          = 1.0

MNQ_TICK_SIZE   = 0.25          # same tick size as MES
MNQ_TICK_VALUE  = 0.50          # $0.50/tick (MES was $1.25)
MNQ_POINT_VALUE = 2.00          # $2.00/point (MES was $5.00)

# Stop fallback — scaled for MNQ (32 ticks = 8 NQ points, equiv to 2 ES points)
STOP_TICKS      = 32

# ── TOPSTEP $50K RULES ────────────────────────────────────────────────────────
import pytz
TIMEZONE             = pytz.timezone("America/Chicago")
SESSION_OPEN_H       = 17
SESSION_OPEN_M       = 0
ENTRY_CUTOFF_H       = 15
ENTRY_CUTOFF_M       = 8
HARD_FLATTEN_H       = 15
HARD_FLATTEN_M       = 8
BOT_DAILY_LOSS_LIMIT = -750.0
EOD_LOSS_BUFFER      = 2_000.0

# Topstep Combine rules
COMBINE_PROFIT_TARGET    = 3_000.0
COMBINE_DAILY_LOSS_LIMIT = -1_000.0

# Topstep XFA rules
XFA_QUALIFYING_DAY_MIN    = 150.0
XFA_PAYOUT_CAP            = 5_000.0
XFA_QUALIFYING_DAYS_NEEDED = 5

# Topstep Scaling Plan (TopstepX, 1 MNQ = 1 lot)
SCALING_TIER_1_CONTRACTS = 2
SCALING_TIER_2_CONTRACTS = 3
SCALING_TIER_3_CONTRACTS = 5
SCALING_TIER_2_THRESHOLD = 1_500.0
SCALING_TIER_3_THRESHOLD = 2_000.0

# ── DYNAMIC RISK PROFILES ────────────────────────────────────────────────────
CALM_ATR_RATIO   = 1.00
STRONG_ATR_RATIO = 0.70

NORMAL_CONTRACTS    = 4
NORMAL_TARGET_TICKS = 64  # 16 NQ points = equiv to 4 ES points
NORMAL_MAX_TRADES   = 5

STRONG_CONTRACTS    = 5          # V28: capped at Topstep max
STRONG_TARGET_TICKS = 100  # 20 NQ points = equiv to 5 ES points at 4x scale
STRONG_MAX_TRADES   = 4          # V28: 1 ORB + 3 FVG

# V28: Explosive regime disabled (33% WR, consistently loses)
# ATR ratio hard cap at 2.0 enforced in get_risk_profile()
EXPLOSIVE_ATR_RATIO    = 2.0     # kept for reference — but blocked
EXPLOSIVE_ATR_POINTS   = 5.0
EXPLOSIVE_CONTRACTS    = 5
EXPLOSIVE_TARGET_TICKS = 24
EXPLOSIVE_MAX_TRADES   = 0       # V28: explosive = 0 trades allowed

# ADX thresholds
ADX_STRONG_THRESHOLD    = 10
ADX_EXPLOSIVE_THRESHOLD = 30

# ── REGIME DETECTOR (clock + ADX) ──────────────────────────────────────────────
# The "is the owner sitting or moving?" detector.
#   - Clock  = time of day rhythm (AM rush / lunch bench / PM rush)
#   - ADX    = are the steps pointing one way (trend) or all over (chop)?
#   - CHOP   = optional Choppiness Index confirmation (squiggly vs straight path)
# detect_regime() uses ADX when REGIME_USE_ADX is True; set False to fall back to
# the legacy volatility-only (ATR) rule for A/B comparison in the backtest.
REGIME_USE_ADX        = True
ADX_CHOP_MAX          = 20.0   # ADX below this  -> ranging / chop
ADX_TREND_MIN         = 25.0   # ADX above this  -> trending; 20-25 = neutral (stand aside)
CHOP_INDEX_ENABLED    = False  # require Choppiness Index confirmation for "choppy"
CHOP_INDEX_PERIOD     = 14
CHOP_INDEX_RANGE_MIN  = 61.8   # Choppiness Index above this confirms a range
# E3: regime router — route entries by detected regime instead of winner-take-all score
REGIME_ROUTER_ENABLED = False
# E5: explicit neutral no-trade zone (ADX 20-25 stands aside completely)
NEUTRAL_NO_TRADE      = False

# Clock windows (US/Central). The lunch "bench" window is when fades work best.
MR_WINDOW_START_CT    = (11, 0)    # mean-reversion (bounce-catching) window start
MR_WINDOW_END_CT      = (13, 30)   # mean-reversion window end
TREND_AM_WINDOW_CT    = ((8, 30), (10, 0))   # morning rush -> trend strategies
TREND_PM_WINDOW_CT    = ((14, 0), (14, 50))  # afternoon push -> trend strategies

# EMA Spread filter
EMA_SPREAD_MIN = 0.0010

# FVG settings
FVG_MAX_AGE_BARS   = 20
FVG_MIN_SIZE_TICKS = 2
FVG_FRESH_ONLY     = False
FVG_LEVEL_ALIGNMENT_BONUS_ENABLED = True
FVG_LEVEL_ALIGNMENT_TICKS         = 8
FVG_BOS_BONUS_ENABLED             = True
FVG_BOS_LOOKBACK_BARS             = 5
FVG_REACTION_CLOSE_BONUS_ENABLED  = True

# ── V28: ORB SETTINGS ─────────────────────────────────────────────────────────
ORB_RANGE_BARS         = 3       # 6 bars = 30 minutes (9:30-10:00 CT)
ORB_ENTRY_WINDOW_START = (9, 30) # CT hour, minute — range starts forming
ORB_ENTRY_WINDOW_END   = (10, 30)# CT hour, minute — last valid ORB entry
ORB_MIN_TARGET_TICKS   = 80      # 20 NQ points floor = equiv to 5 ES points
ORB_MAX_RANGE_TICKS    = 0       # skip if range is too wide (chaotic open)
ORB_MAX_TRADES_PER_DAY = 1       # only 1 ORB trade per session
ORB_STOP_BUFFER_TICKS  = 1       # ticks beyond ORB boundary for stop

# V28: ORB fires independently of ATR regime
ORB_INDEPENDENT_CONTRACTS = 3    # contracts when ORB fires without ATR confirmation

# V28: Partial profit — take 60% off at 40 ticks, let 40% run to full target
PARTIAL_PROFIT_ENABLED  = False
PARTIAL_PROFIT_TICKS    = 40     # 10 NQ points — lock in profit here
PARTIAL_PROFIT_FRACTION = 0.60   # close 60% of contracts, let 40% run

# One-account optimization filters
SKIP_FOMC_ENTRIES              = False
SKIP_HOUR_11_ENTRIES           = False
SKIP_LONG_HOUR_10_11           = False
LONG_QUALITY_FILTERS_ENABLED   = True
LONG_FILTER_START_HOUR         = 10
LONG_FILTER_END_HOUR           = 11
LONG_FILTER_MIN_ADX            = 10
LONG_FILTER_MIN_EMA_SPREAD_PTS = 10.0
FVG_LONG_EMA_FILTER_ENABLED    = False
FVG_LONG_MIN_EMA_SPREAD_PTS    = 10.0
FVG_LONG_EMA_FILTER_MIN_AGE_BARS = 8
FVG_LONG_AGE_FILTER_ENABLED    = False
FVG_LONG_MAX_AGE_BARS          = 7
FVG_QUALITY_SCORE_ENABLED      = True
FVG_SCORE_SIZING_ENABLED       = True
FVG_SCORE_MEDIUM_THRESHOLD     = 3
FVG_SCORE_MEDIUM_SIZE_STEP     = 1
FVG_SCORE_HIGH_THRESHOLD       = 4
FVG_SCORE_HIGH_SIZE_STEP       = 2
LATE_LONG_TARGET_ENABLED       = False
LATE_LONG_TARGET_START_HOUR    = 10
LATE_LONG_TARGET_END_HOUR      = 11
LATE_LONG_TARGET_TICKS         = 80

# Complementary module: VWAP mean reversion V2a
VWAP_MR_ENABLED                = False
VWAP_MR_WINDOW_START           = (12, 0)
VWAP_MR_WINDOW_END             = (14, 0)
VWAP_MR_CONTRACTS              = 2
VWAP_MR_MAX_TRADES_PER_DAY     = 1
VWAP_MR_MAX_ADX                = 20
VWAP_MR_MAX_ATR_RATIO          = 1.10
VWAP_MR_MIN_DISTANCE_POINTS    = 16.0    # used when VWAP_MR_SIGMA_BAND=False
VWAP_MR_MAX_DISTANCE_POINTS    = 24.0    # used when VWAP_MR_SIGMA_BAND=False
VWAP_MR_FAILED_FVG_MAX_AGE     = 8
VWAP_MR_RSI_SHORT_MIN          = 60
VWAP_MR_STOP_TICKS             = 24      # fallback stop when sigma-band=False
VWAP_MR_MIN_TARGET_TICKS       = 20
VWAP_MR_MAX_TARGET_TICKS       = 60
VWAP_MR_TARGET_BUFFER_TICKS    = 4
VWAP_MR_DEBUG_PRINT_LIMIT      = 25
# E1: two-sided σ-band VWAP reversion enhancements
VWAP_MR_TWO_SIDED              = True    # also fade below VWAP (long side)
VWAP_MR_SIGMA_BAND             = True    # use rolling σ-band instead of fixed pts
VWAP_MR_SIGMA_MULT             = 2.0     # entry threshold: price ≥ ±N*σ from VWAP
VWAP_MR_SIGMA_STOP_MULT        = 3.0     # stop: ±N*σ from VWAP (gravity weakens past 3σ)
VWAP_MR_SIGMA_PERIOD           = 20      # bars for rolling std(close - vwap)
VWAP_MR_FVG_AS_BONUS           = True    # failed-FVG is a score bonus, not a gate

# Complementary module: opening-drive first pullback continuation
OD_PULLBACK_ENABLED             = False
OD_DRIVE_WINDOW_START           = (9, 30)
OD_DRIVE_WINDOW_END             = (9, 45)
OD_ENTRY_WINDOW_START           = (9, 45)
OD_ENTRY_WINDOW_END             = (10, 20)
OD_MAX_TRADES_PER_DAY           = 1
OD_CONTRACTS                    = 3
OD_MIN_DRIVE_TICKS              = 12
OD_MAX_DRIVE_TICKS              = 48
OD_MIN_PULLBACK_PCT             = 0.40
OD_MAX_PULLBACK_PCT             = 0.60
OD_MIN_ADX                      = 10
OD_MIN_ATR_RATIO                = 0.90
OD_STOP_CAP_TICKS               = 28
OD_TARGET_TICKS                 = 80
OD_DEBUG_PRINT_LIMIT            = 25
OD_SHORT_ONLY                   = True

# Complementary module: failed breakout / liquidity sweep reversal
FAILED_BREAKOUT_ENABLED         = True
FB_WINDOW_START                 = (10, 0)
FB_WINDOW_END                   = (13, 30)
FB_CONTRACTS                    = 3
FB_MAX_TRADES_PER_DAY           = 1
FB_MIN_ADX                      = 10
FB_MAX_ADX                      = 25
FB_MAX_ATR_RATIO                = 1.50
FB_MIN_SWEEP_TICKS              = 4
FB_MAX_SWEEP_TICKS              = 24
FB_MAX_REENTRY_TICKS            = 24
FB_STOP_CAP_TICKS               = 28
FB_TARGET_TICKS                 = 52
FB_QUALITY_SIZING_ENABLED       = True
FB_GLOBEX_SIZE_STEP             = 1
FB_DEBUG_PRINT_LIMIT            = 25

ANALYSIS_HIGHLIGHT_MIN_TRADES  = 40

# V28: Disable July (36.3% WR, net negative over 7yr)
SKIP_JULY = True

# Monte Carlo
MC_SIMULATIONS  = 10_000
MC_TRADE_COUNT  = 100

# ── EXPORT CONFIG ─────────────────────────────────────────────────────────────
EXPORT_DIR        = os.path.join(os.path.dirname(__file__), "exports")
EXPORT_TABLES_DIR = os.path.join(EXPORT_DIR, "analysis_tables")
RUN_AUDIT_PATH    = os.path.join(EXPORT_DIR, "v29_run_audit.json")

# ── NEWS CALENDAR (FOMC / CPI / NFP 2019-2026) ────────────────────────────────
FOMC_DATES = {
    "2019-01-30","2019-03-20","2019-05-01","2019-06-19","2019-07-31",
    "2019-09-18","2019-10-30","2019-12-11","2020-01-29","2020-03-03",
    "2020-03-15","2020-04-29","2020-06-10","2020-07-29","2020-09-16",
    "2020-11-05","2020-12-16","2021-01-27","2021-03-17","2021-04-28",
    "2021-06-16","2021-07-28","2021-09-22","2021-11-03","2021-12-15",
    "2022-01-26","2022-03-16","2022-05-04","2022-06-15","2022-07-27",
    "2022-09-21","2022-11-02","2022-12-14","2023-02-01","2023-03-22",
    "2023-05-03","2023-06-14","2023-07-26","2023-09-20","2023-11-01",
    "2023-12-13","2024-01-31","2024-03-20","2024-05-01","2024-06-12",
    "2024-07-31","2024-09-18","2024-11-07","2024-12-18","2025-01-29",
    "2025-03-19","2025-05-07","2025-06-18","2025-07-30","2025-09-17",
    "2025-11-05","2025-12-17","2026-01-28","2026-03-18",
}
CPI_DATES = {
    "2019-01-11","2019-02-13","2019-03-12","2019-04-10","2019-05-10",
    "2019-06-12","2019-07-11","2019-08-13","2019-09-12","2019-10-10",
    "2019-11-13","2019-12-11","2020-01-14","2020-02-13","2020-03-11",
    "2020-04-10","2020-05-12","2020-06-10","2020-07-14","2020-08-12",
    "2020-09-11","2020-10-13","2020-11-12","2020-12-10","2021-01-13",
    "2021-02-10","2021-03-10","2021-04-13","2021-05-12","2021-06-10",
    "2021-07-13","2021-08-11","2021-09-14","2021-10-13","2021-11-10",
    "2021-12-10","2022-01-12","2022-02-10","2022-03-10","2022-04-12",
    "2022-05-11","2022-06-10","2022-07-13","2022-08-10","2022-09-13",
    "2022-10-13","2022-11-10","2022-12-13","2023-01-12","2023-02-14",
    "2023-03-14","2023-04-12","2023-05-10","2023-06-13","2023-07-12",
    "2023-08-10","2023-09-13","2023-10-12","2023-11-14","2023-12-12",
    "2024-01-11","2024-02-13","2024-03-12","2024-04-10","2024-05-15",
    "2024-06-12","2024-07-11","2024-08-14","2024-09-11","2024-10-10",
    "2024-11-13","2024-12-11","2025-01-15","2025-02-12","2025-03-12",
    "2025-04-10","2025-05-13","2025-06-11","2025-07-15","2025-08-12",
    "2025-09-10","2025-10-15","2025-11-13","2025-12-10","2026-01-14",
    "2026-02-11","2026-03-11",
}
NFP_DATES = {
    "2019-01-04","2019-02-01","2019-03-08","2019-04-05","2019-05-03",
    "2019-06-07","2019-07-05","2019-08-02","2019-09-06","2019-10-04",
    "2019-11-01","2019-12-06","2020-01-10","2020-02-07","2020-03-06",
    "2020-04-03","2020-05-08","2020-06-05","2020-07-02","2020-08-07",
    "2020-09-04","2020-10-02","2020-11-06","2020-12-04","2021-01-08",
    "2021-02-05","2021-03-05","2021-04-02","2021-05-07","2021-06-04",
    "2021-07-02","2021-08-06","2021-09-03","2021-10-08","2021-11-05",
    "2021-12-03","2022-01-07","2022-02-04","2022-03-04","2022-04-01",
    "2022-05-06","2022-06-03","2022-07-08","2022-08-05","2022-09-02",
    "2022-10-07","2022-11-04","2022-12-02","2023-01-06","2023-02-03",
    "2023-03-10","2023-04-07","2023-05-05","2023-06-02","2023-07-07",
    "2023-08-04","2023-09-01","2023-10-06","2023-11-03","2023-12-08",
    "2024-01-05","2024-02-02","2024-03-08","2024-04-05","2024-05-03",
    "2024-06-07","2024-07-05","2024-08-02","2024-09-06","2024-10-04",
    "2024-11-01","2024-12-06","2025-01-10","2025-02-07","2025-03-07",
    "2025-04-04","2025-05-02","2025-06-06","2025-07-03","2025-08-01",
    "2025-09-05","2025-10-03","2025-11-07","2025-12-05","2026-01-09",
    "2026-02-06","2026-03-06",
}
ALL_NEWS_DATES = FOMC_DATES | CPI_DATES | NFP_DATES

# ── DATA CLASSES ──────────────────────────────────────────────────────────────

@dataclass
class FVG:
    direction: str
    top: float
    bottom: float
    created_bar: int
    session_date: object
    tested: bool = False


@dataclass
class ORBState:
    """Tracks the Opening Range Breakout state for the current session."""
    formed: bool = False          # has the range fully formed?
    high: float = 0.0             # ORB high
    low: float = 0.0              # ORB low
    range_ticks: float = 0.0      # range width in ticks
    bars_seen: int = 0            # bars counted toward range formation
    fired_today: bool = False     # has ORB trade fired today?
    session_date: object = None   # which session this belongs to


@dataclass
class ODPullbackState:
    formed: bool = False
    eligible: bool = False
    fired_today: bool = False
    session_date: object = None
    bars_seen: int = 0
    direction: str = ""
    drive_open: float = 0.0
    drive_close: float = 0.0
    drive_high: float = 0.0
    drive_low: float = 0.0
    drive_range_ticks: float = 0.0
    drive_size_ticks: float = 0.0
    drive_midpoint: float = 0.0
    drive_end_bar: int = -1
    pullback_low: float = 0.0
    pullback_high: float = 0.0
    touched_ema21: bool = False


@dataclass
class TradeRecord:
    # Original fields
    date: pd.Timestamp
    direction: str
    entry: float
    exit: float
    pnl_pct: float
    gross_pnl_usd: float
    costs_usd: float
    pnl_usd: float
    won: bool
    position_size: float
    contracts: int
    regime: str
    exit_reason: str
    fvg_stop: float

    # V28: entry type
    entry_type: str = "FVG"       # "FVG", "ORB", "OD_PULLBACK", "VWAP_MR", or "FAILED_BREAKOUT"

    # Timing
    entry_hour: int = 0
    session_minute: int = 0
    day_of_week: str = ""
    month: int = 0
    year: int = 0
    quarter: int = 0

    # Market context
    atr_at_entry: float = 0.0
    atr_ratio_at_entry: float = 0.0
    adx_at_entry: float = 0.0
    vwap_at_entry: float = 0.0
    price_distance_from_vwap: float = 0.0
    ema_fast_at_entry: float = 0.0
    ema_slow_at_entry: float = 0.0
    ema_spread_at_entry: float = 0.0

    # FVG quality (0 for ORB trades)
    fvg_size_ticks: float = 0.0
    fvg_age_bars: int = 0
    fvg_type: str = ""
    fvg_quality_score: int = 0
    fvg_quality_flags: str = ""

    # ORB quality (0 for FVG trades)
    orb_range_ticks: float = 0.0
    orb_high: float = 0.0
    orb_low: float = 0.0

    # Stop / target
    stop_price: float = 0.0
    target_price: float = 0.0
    stop_width_ticks: float = 0.0

    # Sequence
    trade_number_today: int = 0
    trade_number_overall: int = 0
    consecutive_wins_before: int = 0
    consecutive_losses_before: int = 0
    daily_pnl_at_entry: float = 0.0
    drawdown_pct_at_entry: float = 0.0
    buffer_above_floor_at_entry: float = 0.0
    scaling_tier_at_entry: int = 2

    # Prev session / globex
    prev_day_high: float = 0.0
    prev_day_low: float = 0.0
    prev_day_range: float = 0.0
    globex_high: float = 0.0
    globex_low: float = 0.0
    opened_above_prev_close: bool = False
    opening_gap_points: float = 0.0

    # News
    is_fomc_day: bool = False
    is_cpi_day: bool = False
    is_nfp_day: bool = False
    is_news_day: bool = False

    # Exit quality
    mae: float = 0.0
    mfe: float = 0.0
    bars_to_exit: int = 0

    # Derived
    r_multiple: float = 0.0
    edge_ratio: float = 0.0
    efficiency_ratio: float = 0.0

    # VWAP MR diagnostics
    vwap_mr_distance_points: float = 0.0
    vwap_mr_rsi: float = 0.0
    vwap_mr_failed_fvg_age: int = 0
    vwap_mr_target_ticks: int = 0

    # OD pullback diagnostics
    od_drive_direction: str = ""
    od_drive_size_ticks: float = 0.0
    od_drive_range_ticks: float = 0.0
    od_pullback_depth_ticks: float = 0.0
    od_pullback_depth_pct: float = 0.0
    od_touched_ema21: bool = False
    od_bars_from_drive_to_entry: int = 0
    od_target_ticks: int = 0

    # Failed breakout diagnostics
    fb_level_type: str = ""
    fb_level_price: float = 0.0
    fb_sweep_distance_ticks: float = 0.0
    fb_reentry_distance_ticks: float = 0.0
    fb_target_ticks: int = 0
    fb_bars_from_sweep_to_entry: int = 0

    # Topstep compliance
    combine_profit_at_entry: float = 0.0
    combine_target_remaining: float = 0.0
    daily_loss_used_pct: float = 0.0
    mll_used_pct: float = 0.0
    qualifying_days_banked: int = 0


@dataclass
class DailyRecord:
    session_date: object
    day_of_week: str = ""
    month: int = 0
    year: int = 0
    quarter: int = 0
    daily_pnl_gross: float = 0.0
    daily_pnl_net: float = 0.0
    daily_costs: float = 0.0
    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    orb_trade_count: int = 0
    fvg_trade_count: int = 0
    orb_pnl: float = 0.0
    fvg_pnl: float = 0.0
    morning_trade_count: int = 0
    afternoon_trade_count: int = 0
    morning_pnl: float = 0.0
    afternoon_pnl: float = 0.0
    long_trade_count: int = 0
    short_trade_count: int = 0
    long_pnl: float = 0.0
    short_pnl: float = 0.0
    strong_regime_count: int = 0
    explosive_regime_count: int = 0
    strong_regime_pnl: float = 0.0
    explosive_regime_pnl: float = 0.0
    first_trade_time: str = ""
    last_trade_time: str = ""
    daily_high: float = 0.0
    daily_low: float = 0.0
    daily_range_points: float = 0.0
    daily_range_vs_avg: float = 0.0
    is_trend_day: bool = False
    opening_gap_points: float = 0.0
    prev_day_range: float = 0.0
    globex_range: float = 0.0
    max_intraday_drawdown: float = 0.0
    max_intraday_peak: float = 0.0
    hit_daily_loss_limit: bool = False
    is_qualifying_day: bool = False
    is_news_day: bool = False
    is_fomc_day: bool = False
    is_cpi_day: bool = False
    is_nfp_day: bool = False
    contracts_tier: int = 2


@dataclass
class MonthlyRecord:
    year_month: str = ""
    year: int = 0
    month: int = 0
    total_net_pnl: float = 0.0
    total_gross_pnl: float = 0.0
    total_costs: float = 0.0
    trade_count: int = 0
    orb_trade_count: int = 0
    fvg_trade_count: int = 0
    orb_pnl: float = 0.0
    fvg_pnl: float = 0.0
    trading_days: int = 0
    winning_days: int = 0
    losing_days: int = 0
    qualifying_days: int = 0
    win_rate: float = 0.0
    avg_daily_pnl: float = 0.0
    best_day_pnl: float = 0.0
    worst_day_pnl: float = 0.0
    trend_days_count: int = 0
    range_days_count: int = 0
    avg_atr: float = 0.0
    avg_adx: float = 0.0
    morning_pnl: float = 0.0
    afternoon_pnl: float = 0.0
    long_pnl: float = 0.0
    short_pnl: float = 0.0
    strong_regime_pnl: float = 0.0
    explosive_regime_pnl: float = 0.0
    news_day_count: int = 0
    news_day_pnl: float = 0.0


@dataclass
class LiveSignalSnapshot:
    as_of: str = ""
    entry_timestamp: str = ""
    session_date: str = ""
    entry_type: str = ""
    direction: str = ""
    regime: str = ""
    contracts: int = 0
    entry_price: float = 0.0
    stop_price: float = 0.0
    target_price: float = 0.0
    stop_width_ticks: float = 0.0
    target_ticks: int = 0
    reward_risk_ratio: float = 0.0
    entry_hour: int = 0
    session_minute: int = 0
    day_of_week: str = ""
    month: int = 0
    year: int = 0
    atr_at_entry: float = 0.0
    atr_ratio_at_entry: float = 0.0
    adx_at_entry: float = 0.0
    vwap_at_entry: float = 0.0
    price_distance_from_vwap: float = 0.0
    ema_fast_at_entry: float = 0.0
    ema_slow_at_entry: float = 0.0
    ema_spread_at_entry: float = 0.0
    fvg_size_ticks: float = 0.0
    fvg_age_bars: int = 0
    fvg_type: str = ""
    fvg_quality_score: int = 0
    fvg_quality_flags: str = ""
    fb_level_type: str = ""
    fb_level_price: float = 0.0
    fb_sweep_distance_ticks: float = 0.0
    fb_reentry_distance_ticks: float = 0.0
    prev_day_high: float = 0.0
    prev_day_low: float = 0.0
    prev_day_range: float = 0.0
    globex_high: float = 0.0
    globex_low: float = 0.0
    opening_gap_points: float = 0.0
    is_news_day: bool = False
    is_fomc_day: bool = False
    is_cpi_day: bool = False
    is_nfp_day: bool = False
    drawdown_pct_at_entry: float = 0.0
    daily_pnl_at_entry: float = 0.0
    buffer_above_floor_at_entry: float = 0.0
    scaling_tier_at_entry: int = 0
    combine_profit_at_entry: float = 0.0
    combine_target_remaining: float = 0.0
    daily_loss_used_pct: float = 0.0
    mll_used_pct: float = 0.0
    thesis: str = ""


@dataclass
class RiskState:
    wins: int = 0
    losses: int = 0
    prior_wins: int = int(PRIOR_WIN_RATE * PRIOR_TRADES)
    prior_total: int = PRIOR_TRADES
    daily_pnl_usd: float = 0.0
    daily_trades: int = 0
    trade_returns: List[float] = field(default_factory=list)
    fb_cap_skips: List[dict] = field(default_factory=list)
    eod_high_balance: float = INIT_CASH
    max_loss_floor: float = INIT_CASH - EOD_LOSS_BUFFER
    floor_breached: bool = False
    min_buffer_over_floor: float = float("inf")

    @property
    def bayesian_win_rate(self) -> float:
        total_wins   = self.wins + self.prior_wins
        total_trades = self.wins + self.losses + self.prior_total
        return total_wins / total_trades if total_trades > 0 else PRIOR_WIN_RATE

    def kelly_size(self, reward_risk: float = 2.0) -> float:
        w = self.bayesian_win_rate
        l = 1 - w
        kelly = w - (l / reward_risk)
        return max(0.0, kelly) * KELLY_FRACTION

    def update(self, won: bool, pnl_pct: float, pnl_usd: float):
        if won:
            self.wins += 1
        else:
            self.losses += 1
        self.daily_pnl_usd += pnl_usd
        self.daily_trades  += 1
        self.trade_returns.append(pnl_pct)

    def end_of_day(self, cash: float):
        if cash > self.eod_high_balance:
            self.eod_high_balance = cash
        self.max_loss_floor = min(INIT_CASH, self.eod_high_balance - EOD_LOSS_BUFFER)
        self.daily_pnl_usd  = 0.0
        self.daily_trades   = 0


# ── COST CALCULATION ──────────────────────────────────────────────────────────

def round_turn_cost(contracts: int) -> float:
    commission = COMMISSION_PER_CONTRACT * contracts
    slippage   = SLIPPAGE_TICKS * MNQ_TICK_VALUE * contracts * 2
    return commission + slippage


# ── SCALING TIER ──────────────────────────────────────────────────────────────

def compute_scaling_tier(net_profit: float) -> int:
    if net_profit >= SCALING_TIER_3_THRESHOLD:
        return SCALING_TIER_3_CONTRACTS   # 5
    elif net_profit >= SCALING_TIER_2_THRESHOLD:
        return SCALING_TIER_2_CONTRACTS   # 3
    else:
        return SCALING_TIER_1_CONTRACTS   # 2


# ── REGIME PROFILE ────────────────────────────────────────────────────────────

def get_risk_profile(row) -> dict:
    """
    V28 changes:
    - ATR ratio hard cap at 2.0 (removes explosive losing zone)
    - Explosive max_trades = 0 (disabled)
    - Strong contracts capped at 5 (Topstep legal max)
    """
    if pd.isna(row["atr"]) or pd.isna(row["atr_avg_20"]) or row["atr_avg_20"] == 0:
        return {"regime": "unknown", "contracts": 0, "target_ticks": 0, "max_trades": 0}

    ratio = row["atr"] / row["atr_avg_20"]

    # V28: hard cap at 2.0 — above this is losing territory
    if ratio >= 2.0:
        return {"regime": "calm", "contracts": 0, "target_ticks": 0, "max_trades": 0}

    if ratio < CALM_ATR_RATIO:
        return {"regime": "calm", "contracts": 0, "target_ticks": 0, "max_trades": 0}

    elif (ratio >= STRONG_ATR_RATIO
          and row["atr"] >= 1.5
          and row["adx"] >= ADX_STRONG_THRESHOLD):
        return {
            "regime": "strong",
            "contracts": STRONG_CONTRACTS,       # 5 max
            "target_ticks": STRONG_TARGET_TICKS, # 20
            "max_trades": STRONG_MAX_TRADES,     # 4
        }
    else:
        return {"regime": "normal", "contracts": 0, "target_ticks": 0, "max_trades": 0}


# ── DATA ──────────────────────────────────────────────────────────────────────

def _serialize_audit_value(value):
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    return value


def build_run_audit() -> Dict[str, object]:
    return {
        "version": "V29",
        "generated_at": datetime.now(TIMEZONE).isoformat(),
        "run_mode": RUN_MODE,
        "execution_profile": EXECUTION_PROFILE,
        "live_execution_enabled": LIVE_EXECUTION_ENABLED,
        "data_path": DATA_PATH,
        "commission_per_contract_rt": COMMISSION_PER_CONTRACT,
        "slippage_ticks_per_side": SLIPPAGE_TICKS,
        "init_cash": INIT_CASH,
        "max_contracts_cap": STRONG_CONTRACTS,
        "daily_loss_limit": BOT_DAILY_LOSS_LIMIT,
        "combine_profit_target": COMBINE_PROFIT_TARGET,
        "combine_daily_loss_limit": COMBINE_DAILY_LOSS_LIMIT,
        "entry_cutoff_ct": f"{ENTRY_CUTOFF_H:02d}:{ENTRY_CUTOFF_M:02d}",
        "hard_flatten_ct": f"{HARD_FLATTEN_H:02d}:{HARD_FLATTEN_M:02d}",
        "fvg_score_sizing_enabled": FVG_SCORE_SIZING_ENABLED,
        "failed_breakout_enabled": FAILED_BREAKOUT_ENABLED,
    }


def run_startup_audit() -> Dict[str, object]:
    audit = build_run_audit()
    checks = []

    def add_check(name: str, passed: bool, detail: str) -> None:
        checks.append({"check": name, "passed": passed, "detail": detail})

    add_check("run_mode_explicit", RUN_MODE == "BACKTEST",
              f"RUN_MODE={RUN_MODE}")
    add_check("live_execution_disabled", not LIVE_EXECUTION_ENABLED,
              f"LIVE_EXECUTION_ENABLED={LIVE_EXECUTION_ENABLED}")
    add_check("data_path_exists", os.path.exists(DATA_PATH),
              DATA_PATH)
    add_check("commission_positive", COMMISSION_PER_CONTRACT > 0,
              f"commission_rt={COMMISSION_PER_CONTRACT}")
    add_check("slippage_positive", SLIPPAGE_TICKS > 0,
              f"slippage_ticks_side={SLIPPAGE_TICKS}")
    add_check("slippage_conservative", SLIPPAGE_TICKS >= 1.0,
              f"slippage_ticks_side={SLIPPAGE_TICKS}")
    add_check("contract_cap_valid", STRONG_CONTRACTS <= SCALING_TIER_3_CONTRACTS,
              f"max_contracts={STRONG_CONTRACTS}, scaling_cap={SCALING_TIER_3_CONTRACTS}")
    add_check("stop_positive", STOP_TICKS > 0, f"STOP_TICKS={STOP_TICKS}")
    add_check("target_positive", STRONG_TARGET_TICKS > 0 and FB_TARGET_TICKS > 0,
              f"STRONG_TARGET_TICKS={STRONG_TARGET_TICKS}, FB_TARGET_TICKS={FB_TARGET_TICKS}")
    add_check("daily_loss_limit_negative", BOT_DAILY_LOSS_LIMIT < 0,
              f"BOT_DAILY_LOSS_LIMIT={BOT_DAILY_LOSS_LIMIT}")
    add_check("combine_daily_loss_limit_negative", COMBINE_DAILY_LOSS_LIMIT < 0,
              f"COMBINE_DAILY_LOSS_LIMIT={COMBINE_DAILY_LOSS_LIMIT}")
    add_check("entry_before_flatten",
              (ENTRY_CUTOFF_H, ENTRY_CUTOFF_M) <= (HARD_FLATTEN_H, HARD_FLATTEN_M),
              f"entry_cutoff={ENTRY_CUTOFF_H:02d}:{ENTRY_CUTOFF_M:02d}, flatten={HARD_FLATTEN_H:02d}:{HARD_FLATTEN_M:02d}")

    failed = [c for c in checks if not c["passed"]]
    audit["checks"] = checks
    audit["status"] = "PASS" if not failed else "FAIL"

    print("  Run mode audit:")
    print(f"    mode={RUN_MODE} | profile={EXECUTION_PROFILE} | live={'ON' if LIVE_EXECUTION_ENABLED else 'OFF'}")
    print(f"    costs=commission ${COMMISSION_PER_CONTRACT:.2f} RT | slippage {SLIPPAGE_TICKS:.2f} ticks/side")
    print(f"    startup audit={audit['status']}")

    if failed:
        for item in failed:
            print(f"    FAIL {item['check']}: {item['detail']}")
        raise ValueError("Startup audit failed. Fix the failed checks before running.")

    return audit


def validate_loaded_data(df: pd.DataFrame) -> Dict[str, object]:
    required_cols = ["open", "high", "low", "close", "volume"]
    for col in required_cols:
        if col not in df.columns:
            raise ValueError(f"Data validation failed: missing required column '{col}'")
        if not pd.api.types.is_numeric_dtype(df[col]):
            raise ValueError(f"Data validation failed: column '{col}' is not numeric")

    if df.empty:
        raise ValueError("Data validation failed: dataset is empty after load")
    if (df["volume"] < 0).any():
        raise ValueError("Data validation failed: negative volume detected")
    if df.index.tz is None:
        raise ValueError("Data validation failed: timestamps are not timezone-aware")
    if not df.index.is_monotonic_increasing:
        raise ValueError("Data validation failed: timestamps are not sorted ascending")
    if df.index.has_duplicates:
        raise ValueError("Data validation failed: duplicate timestamps remain after resample")

    bad_ohlc = (
        (df["high"] < df[["open", "close", "low"]].max(axis=1)) |
        (df["low"] > df[["open", "close", "high"]].min(axis=1))
    )
    if bad_ohlc.any():
        bad_ts = df.index[bad_ohlc][0]
        raise ValueError(f"Data validation failed: OHLC bounds invalid at {bad_ts}")

    summary = {
        "rows": int(len(df)),
        "start": df.index[0].isoformat(),
        "end": df.index[-1].isoformat(),
        "timezone": str(df.index.tz),
    }
    print(
        "  Data validation passed:"
        f" rows={summary['rows']:,}"
        f" | range={summary['start']} -> {summary['end']}"
        f" | tz={summary['timezone']}"
    )
    return summary


def save_run_audit(audit: Dict[str, object], data_summary: Optional[Dict[str, object]] = None) -> None:
    os.makedirs(EXPORT_DIR, exist_ok=True)
    payload = dict(audit)
    if data_summary is not None:
        payload["data_summary"] = data_summary
    with open(RUN_AUDIT_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=_serialize_audit_value)
    print("  Exported: v29_run_audit.json")

def fetch_data() -> pd.DataFrame:
    print("Loading data...")
    if not os.path.exists(DATA_PATH):
        raise FileNotFoundError(f"Data path does not exist: {DATA_PATH}")
    df = load_mes_data(DATA_PATH)
    print(f"Loaded {len(df):,} 5-minute bars.")
    return df


# ── INDICATORS ────────────────────────────────────────────────────────────────

def compute_choppiness_index(high, low, tr, period: int):
    """Choppiness Index over `period` bars (0-100).

    High values (~> 61.8) = lots of motion but little net progress -> ranging/chop;
    low values (~< 38.2) = efficient, straight-line travel -> trending. `tr` is the
    per-bar true range (so gap moves are counted consistently with ATR)."""
    atr_sum    = tr.rolling(period).sum()
    chop_range = (high.rolling(period).max() - low.rolling(period).min()).replace(0, np.nan)
    return 100 * np.log10(atr_sum / chop_range) / np.log10(period)


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    close = df["close"]
    high  = df["high"]
    low   = df["low"]

    df["ema_fast"] = close.ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_slow"] = close.ewm(span=EMA_SLOW, adjust=False).mean()

    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(RSI_PERIOD).mean()
    loss  = (-delta.clip(upper=0)).rolling(RSI_PERIOD).mean()
    rs    = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    df["_date"]  = df.index.date
    df["_tp"]    = (high + low + close) / 3
    df["_tpvol"] = df.groupby("_date", group_keys=False).apply(
        lambda g: (g["_tp"] * g["volume"]).cumsum())
    df["_vcum"]  = df.groupby("_date", group_keys=False)["volume"].cumsum()
    df["vwap"]   = df["_tpvol"] / df["_vcum"].replace(0, np.nan)
    df.drop(columns=["_date", "_tp", "_tpvol", "_vcum"], inplace=True)

    # Rolling σ of (close − VWAP): used by the σ-band VWAP reversion entry.
    close_minus_vwap = (df["close"] - df["vwap"]).fillna(0)
    df["vwap_sigma"] = close_minus_vwap.rolling(VWAP_MR_SIGMA_PERIOD).std()

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs()
    ], axis=1).max(axis=1)
    df["atr"]        = tr.rolling(ATR_PERIOD).mean()
    df["atr_avg_20"] = df["atr"].rolling(20).mean()

    high_diff  = high.diff()
    low_diff   = (-low.diff())
    plus_dm    = high_diff.where((high_diff > low_diff) & (high_diff > 0), 0.0)
    minus_dm   = low_diff.where((low_diff > high_diff) & (low_diff > 0), 0.0)
    tr_smooth  = tr.rolling(14).mean()
    plus_di    = 100 * (plus_dm.rolling(14).mean() / tr_smooth)
    minus_di   = 100 * (minus_dm.rolling(14).mean() / tr_smooth)
    dx         = 100 * (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan))
    df["adx"]  = dx.rolling(14).mean()

    # Choppiness Index: high = squiggly/range-bound, low = straight/trending.
    df["chop_index"] = compute_choppiness_index(high, low, tr, CHOP_INDEX_PERIOD)

    df["ema_spread"] = (df["ema_fast"] - df["ema_slow"]).abs() / df["close"]
    df["structure_high"] = df["high"].rolling(FVG_BOS_LOOKBACK_BARS).max().shift(1)
    df["structure_low"]  = df["low"].rolling(FVG_BOS_LOOKBACK_BARS).min().shift(1)

    df_15m       = df["close"].resample("15min").last().dropna()
    ema_fast_15m = df_15m.ewm(span=EMA_FAST, adjust=False).mean()
    ema_slow_15m = df_15m.ewm(span=EMA_SLOW, adjust=False).mean()
    df["mtf_15m_bull"] = (ema_fast_15m > ema_slow_15m).reindex(
        df.index, method="ffill").fillna(False)
    df["mtf_15m_bear"] = (ema_fast_15m < ema_slow_15m).reindex(
        df.index, method="ffill").fillna(False)

    return df


# ── SIGNALS ───────────────────────────────────────────────────────────────────

def classify_regime_adx(row) -> str:
    """ADX-based regime: 'trending' | 'choppy' | 'neutral' | 'unknown'.

    Reads the *direction* of the steps (ADX) rather than just their speed (ATR):
      ADX >= ADX_TREND_MIN          -> trending  (owner is moving)
      ADX <  ADX_CHOP_MAX (+ CHOP)  -> choppy    (owner is sitting)
      in between                    -> neutral   (unsure -> stand aside)
    """
    adx = row.get("adx") if hasattr(row, "get") else row["adx"]
    if adx is None or pd.isna(adx):
        return "unknown"
    adx = float(adx)
    if adx >= ADX_TREND_MIN:
        return "trending"
    if adx < ADX_CHOP_MAX:
        if CHOP_INDEX_ENABLED:
            chop = row.get("chop_index") if hasattr(row, "get") else row["chop_index"]
            if chop is None or pd.isna(chop) or float(chop) < CHOP_INDEX_RANGE_MIN:
                return "neutral"
        return "choppy"
    return "neutral"


def detect_regime(row) -> str:
    """Regime label for the bar. Uses the ADX detector unless REGIME_USE_ADX is
    off, in which case it falls back to the legacy volatility-only (ATR) rule."""
    if REGIME_USE_ADX:
        return classify_regime_adx(row)
    if pd.isna(row["atr"]) or pd.isna(row["atr_avg_20"]):
        return "unknown"
    if row["atr"] >= row["atr_avg_20"] * ATR_REGIME_MULT:
        return "trending"
    return "choppy"


def _in_window_ct(dt_ct, start, end) -> bool:
    """True if dt_ct's clock time is within [start, end] (CT tuples)."""
    from datetime import time as dtime
    t = dt_ct.time()
    return dtime(start[0], start[1]) <= t <= dtime(end[0], end[1])


def session_phase(dt_ct) -> str:
    """Clock-based market rhythm:
    'am_trend' (morning rush) | 'lunch_chop' (bench) | 'pm_trend' (afternoon push)
    | 'other'. The clock alone tells you which game is *likely* on."""
    if _in_window_ct(dt_ct, TREND_AM_WINDOW_CT[0], TREND_AM_WINDOW_CT[1]):
        return "am_trend"
    if _in_window_ct(dt_ct, MR_WINDOW_START_CT, MR_WINDOW_END_CT):
        return "lunch_chop"
    if _in_window_ct(dt_ct, TREND_PM_WINDOW_CT[0], TREND_PM_WINDOW_CT[1]):
        return "pm_trend"
    return "other"


def is_mean_reversion_window(row, dt_ct) -> bool:
    """Green light for bounce-catching (mean reversion):
    the clock says lunch 'bench' AND ADX confirms the owner is actually sitting.
    Both must agree — a busy-news lunch that keeps trending is excluded."""
    return session_phase(dt_ct) == "lunch_chop" and classify_regime_adx(row) == "choppy"


def generate_signals(df: pd.DataFrame) -> pd.DataFrame:
    df["regime"] = df.apply(detect_regime, axis=1)

    trend_up   = df["ema_fast"] > df["ema_slow"]
    trend_down = df["ema_fast"] < df["ema_slow"]
    above_vwap = df["close"]   > df["vwap"]
    below_vwap = df["close"]   < df["vwap"]

    df["long_bias"]  = trend_up   & above_vwap
    df["short_bias"] = trend_down & below_vwap
    df["long_signal"]  = df["long_bias"]
    df["short_signal"] = df["short_bias"]

    return df


# ── FVG HELPERS ───────────────────────────────────────────────────────────────

def is_fvg_valid(fvg: FVG, current_bar: int, current_date: object,
                 current_high: float, current_low: float) -> bool:
    if fvg.session_date != current_date:
        return False
    if (current_bar - fvg.created_bar) > FVG_MAX_AGE_BARS:
        return False
    if fvg.direction == "bullish" and current_low < fvg.bottom:
        return False
    if fvg.direction == "bearish" and current_high > fvg.top:
        return False
    return True


def price_in_fvg(fvg: FVG, price: float) -> bool:
    return fvg.bottom <= price <= fvg.top


def bar_touches_fvg(fvg: FVG, bar_high: float, bar_low: float) -> bool:
    return bar_high >= fvg.bottom and bar_low <= fvg.top


def fvg_distance_to_level_ticks(fvg: FVG, level_price: float) -> float:
    if level_price <= 0:
        return float("inf")
    if fvg.bottom <= level_price <= fvg.top:
        return 0.0
    distance_points = min(abs(level_price - fvg.bottom), abs(level_price - fvg.top))
    return distance_points / MNQ_TICK_SIZE


# ── SESSION HELPERS ───────────────────────────────────────────────────────────

def _is_in_session(dt_ct) -> bool:
    from datetime import time as dtime
    t      = dt_ct.time()
    cutoff = dtime(HARD_FLATTEN_H, HARD_FLATTEN_M)
    reopen = dtime(SESSION_OPEN_H, SESSION_OPEN_M)
    return not (cutoff <= t < reopen)


def _is_entry_allowed(dt_ct) -> bool:
    from datetime import time as dtime
    t      = dt_ct.time()
    cutoff = dtime(ENTRY_CUTOFF_H, ENTRY_CUTOFF_M)
    reopen = dtime(SESSION_OPEN_H, SESSION_OPEN_M)
    return not (cutoff <= t < reopen)


def _passes_strategy_filters(
    prev_row,
    dt_ct,
    session_date,
    entry_dir: str,
    entry_type: str,
    fvg_age_bars: int = 0,
) -> bool:
    """Apply optional post-signal filters for one-account optimization."""
    if SKIP_FOMC_ENTRIES and str(session_date) in FOMC_DATES:
        return False

    if SKIP_HOUR_11_ENTRIES and dt_ct.hour == 11:
        return False

    if entry_type != "FVG":
        return True

    if SKIP_LONG_HOUR_10_11 and entry_dir == "long" and dt_ct.hour in (10, 11):
        return False

    if (FVG_LONG_AGE_FILTER_ENABLED
            and entry_dir == "long"
            and fvg_age_bars > FVG_LONG_MAX_AGE_BARS):
        return False

    if (FVG_LONG_EMA_FILTER_ENABLED
            and entry_dir == "long"
            and fvg_age_bars >= FVG_LONG_EMA_FILTER_MIN_AGE_BARS):
        ema_spread_pts = (
            abs(float(prev_row["ema_fast"]) - float(prev_row["ema_slow"]))
            if not pd.isna(prev_row["ema_fast"]) and not pd.isna(prev_row["ema_slow"])
            else 0.0
        )
        if ema_spread_pts < FVG_LONG_MIN_EMA_SPREAD_PTS:
            return False

    if (LONG_QUALITY_FILTERS_ENABLED
            and entry_dir == "long"
            and LONG_FILTER_START_HOUR <= dt_ct.hour <= LONG_FILTER_END_HOUR):
        adx = float(prev_row["adx"]) if not pd.isna(prev_row["adx"]) else 0.0
        ema_spread_pts = (
            abs(float(prev_row["ema_fast"]) - float(prev_row["ema_slow"]))
            if not pd.isna(prev_row["ema_fast"]) and not pd.isna(prev_row["ema_slow"])
            else 0.0
        )
        if adx < LONG_FILTER_MIN_ADX:
            return False
        if ema_spread_pts < LONG_FILTER_MIN_EMA_SPREAD_PTS:
            return False

    return True


def _compute_fvg_quality_score(
    prev_row,
    dt_ct,
    entry_dir: str,
    entry_fvg: Optional[FVG],
    current_bar: int,
    session_level_data: Optional[dict] = None,
) -> dict:
    if not FVG_QUALITY_SCORE_ENABLED or entry_fvg is None:
        return {"score": 0, "flags": ""}

    age_bars = max(0, current_bar - entry_fvg.created_bar)
    size_ticks = (entry_fvg.top - entry_fvg.bottom) / MNQ_TICK_SIZE
    adx = float(prev_row["adx"]) if not pd.isna(prev_row["adx"]) else 0.0
    ema_spread_pts = (
        abs(float(prev_row["ema_fast"]) - float(prev_row["ema_slow"]))
        if not pd.isna(prev_row["ema_fast"]) and not pd.isna(prev_row["ema_slow"])
        else 0.0
    )

    score = 0
    flags: List[str] = []

    if entry_dir == "short":
        score += 1
        flags.append("short")

    age_ok = (age_bars <= 4) if entry_dir == "long" else (age_bars <= 10)
    if age_ok:
        score += 1
        flags.append("age")

    if ema_spread_pts >= 20.0:
        score += 1
        flags.append("ema20")

    if adx >= 20.0:
        score += 1
        flags.append("adx20")

    size_ok = (
        (entry_dir == "long" and 8 <= size_ticks < 20)
        or (entry_dir == "short" and 12 <= size_ticks < 40)
    )
    if size_ok:
        score += 1
        flags.append("size")

    if not entry_fvg.tested:
        score += 1
        flags.append("fresh")

    if FVG_LEVEL_ALIGNMENT_BONUS_ENABLED and session_level_data:
        candidate_levels = (
            [session_level_data.get("globex_low", 0.0), session_level_data.get("prev_day_low", 0.0)]
            if entry_dir == "long" else
            [session_level_data.get("globex_high", 0.0), session_level_data.get("prev_day_high", 0.0)]
        )
        best_distance_ticks = min(
            fvg_distance_to_level_ticks(entry_fvg, float(level_price))
            for level_price in candidate_levels
        )
        if best_distance_ticks <= FVG_LEVEL_ALIGNMENT_TICKS:
            score += 1
            flags.append("level")

    if FVG_BOS_BONUS_ENABLED:
        structure_high = float(prev_row["structure_high"]) if not pd.isna(prev_row.get("structure_high", np.nan)) else np.nan
        structure_low = float(prev_row["structure_low"]) if not pd.isna(prev_row.get("structure_low", np.nan)) else np.nan
        close_px = float(prev_row["close"]) if not pd.isna(prev_row.get("close", np.nan)) else np.nan
        bos_ok = (
            entry_dir == "long" and not pd.isna(structure_high) and not pd.isna(close_px) and close_px > structure_high
        ) or (
            entry_dir == "short" and not pd.isna(structure_low) and not pd.isna(close_px) and close_px < structure_low
        )
        if bos_ok:
            score += 1
            flags.append("bos")

    if FVG_REACTION_CLOSE_BONUS_ENABLED:
        close_px = float(prev_row["close"]) if not pd.isna(prev_row.get("close", np.nan)) else np.nan
        if not pd.isna(close_px) and entry_fvg.bottom <= close_px <= entry_fvg.top:
            midpoint = (entry_fvg.top + entry_fvg.bottom) / 2.0
            favorable_half_ok = (
                entry_dir == "long" and close_px >= midpoint
            ) or (
                entry_dir == "short" and close_px <= midpoint
            )
            if favorable_half_ok:
                score += 1
                flags.append("react")

    return {"score": score, "flags": "|".join(flags)}


def _target_ticks_for_entry(
    base_target_ticks: int,
    dt_ct,
    entry_dir: str,
    entry_type: str,
) -> int:
    """Apply isolated target overrides for weaker late-session long setups."""
    if (LATE_LONG_TARGET_ENABLED
            and entry_type == "FVG"
            and entry_dir == "long"
            and LATE_LONG_TARGET_START_HOUR <= dt_ct.hour <= LATE_LONG_TARGET_END_HOUR):
        return min(base_target_ticks, LATE_LONG_TARGET_TICKS)
    return base_target_ticks


def _build_live_signal_snapshot(
    entry_snapshot: dict,
    as_of: pd.Timestamp,
    session_date,
    direction: str,
    entry_price: float,
    contracts: int,
    entry_regime: str,
    entry_type: str,
    target_ticks: int,
) -> LiveSignalSnapshot:
    stop_width_ticks = float(entry_snapshot.get("stop_width_ticks", 0.0))
    reward_risk_ratio = (float(target_ticks) / stop_width_ticks) if stop_width_ticks > 0 else 0.0
    quality_flags = str(entry_snapshot.get("fvg_quality_flags", ""))
    thesis_parts = [
        f"{entry_type} {direction}",
        f"regime={entry_regime}",
    ]
    if int(entry_snapshot.get("fvg_quality_score", 0)) > 0:
        thesis_parts.append(
            f"score={int(entry_snapshot.get('fvg_quality_score', 0))}"
        )
    if quality_flags:
        thesis_parts.append(f"flags={quality_flags}")
    if str(entry_snapshot.get("fb_level_type", "")):
        thesis_parts.append(f"fb_level={entry_snapshot.get('fb_level_type', '')}")
    thesis_parts.append(f"rr={reward_risk_ratio:.2f}")

    return LiveSignalSnapshot(
        as_of=as_of.isoformat(),
        entry_timestamp=as_of.isoformat(),
        session_date=str(session_date),
        entry_type=entry_type,
        direction=direction,
        regime=entry_regime,
        contracts=int(contracts),
        entry_price=float(entry_price),
        stop_price=float(entry_snapshot.get("stop_price", 0.0)),
        target_price=float(entry_snapshot.get("target_price", 0.0)),
        stop_width_ticks=stop_width_ticks,
        target_ticks=int(target_ticks),
        reward_risk_ratio=reward_risk_ratio,
        entry_hour=int(entry_snapshot.get("entry_hour", 0)),
        session_minute=int(entry_snapshot.get("session_minute", 0)),
        day_of_week=str(entry_snapshot.get("day_of_week", "")),
        month=int(entry_snapshot.get("month", 0)),
        year=int(entry_snapshot.get("year", 0)),
        atr_at_entry=float(entry_snapshot.get("atr_at_entry", 0.0)),
        atr_ratio_at_entry=float(entry_snapshot.get("atr_ratio_at_entry", 0.0)),
        adx_at_entry=float(entry_snapshot.get("adx_at_entry", 0.0)),
        vwap_at_entry=float(entry_snapshot.get("vwap_at_entry", 0.0)),
        price_distance_from_vwap=float(entry_snapshot.get("price_distance_from_vwap", 0.0)),
        ema_fast_at_entry=float(entry_snapshot.get("ema_fast_at_entry", 0.0)),
        ema_slow_at_entry=float(entry_snapshot.get("ema_slow_at_entry", 0.0)),
        ema_spread_at_entry=float(entry_snapshot.get("ema_spread_at_entry", 0.0)),
        fvg_size_ticks=float(entry_snapshot.get("fvg_size_ticks", 0.0)),
        fvg_age_bars=int(entry_snapshot.get("fvg_age_bars", 0)),
        fvg_type=str(entry_snapshot.get("fvg_type", "")),
        fvg_quality_score=int(entry_snapshot.get("fvg_quality_score", 0)),
        fvg_quality_flags=quality_flags,
        fb_level_type=str(entry_snapshot.get("fb_level_type", "")),
        fb_level_price=float(entry_snapshot.get("fb_level_price", 0.0)),
        fb_sweep_distance_ticks=float(entry_snapshot.get("fb_sweep_distance_ticks", 0.0)),
        fb_reentry_distance_ticks=float(entry_snapshot.get("fb_reentry_distance_ticks", 0.0)),
        prev_day_high=float(entry_snapshot.get("prev_day_high", 0.0)),
        prev_day_low=float(entry_snapshot.get("prev_day_low", 0.0)),
        prev_day_range=float(entry_snapshot.get("prev_day_range", 0.0)),
        globex_high=float(entry_snapshot.get("globex_high", 0.0)),
        globex_low=float(entry_snapshot.get("globex_low", 0.0)),
        opening_gap_points=float(entry_snapshot.get("opening_gap_points", 0.0)),
        is_news_day=bool(entry_snapshot.get("is_news_day", False)),
        is_fomc_day=bool(entry_snapshot.get("is_fomc_day", False)),
        is_cpi_day=bool(entry_snapshot.get("is_cpi_day", False)),
        is_nfp_day=bool(entry_snapshot.get("is_nfp_day", False)),
        drawdown_pct_at_entry=float(entry_snapshot.get("drawdown_pct_at_entry", 0.0)),
        daily_pnl_at_entry=float(entry_snapshot.get("daily_pnl_at_entry", 0.0)),
        buffer_above_floor_at_entry=float(entry_snapshot.get("buffer_above_floor_at_entry", 0.0)),
        scaling_tier_at_entry=int(entry_snapshot.get("scaling_tier_at_entry", 0)),
        combine_profit_at_entry=float(entry_snapshot.get("combine_profit_at_entry", 0.0)),
        combine_target_remaining=float(entry_snapshot.get("combine_target_remaining", 0.0)),
        daily_loss_used_pct=float(entry_snapshot.get("daily_loss_used_pct", 0.0)),
        mll_used_pct=float(entry_snapshot.get("mll_used_pct", 0.0)),
        thesis=" | ".join(thesis_parts),
    )


def _is_vwap_mr_window(dt_ct) -> bool:
    from datetime import time as dtime
    t = dt_ct.time()
    start = dtime(VWAP_MR_WINDOW_START[0], VWAP_MR_WINDOW_START[1])
    end = dtime(VWAP_MR_WINDOW_END[0], VWAP_MR_WINDOW_END[1])
    return start <= t <= end


def _build_vwap_mr_setup(prev_row, current_price: float, dt_ct, active_fvgs, current_bar: int):
    """VWAP mean-reversion setup.  Returns a setup dict or None.

    When VWAP_MR_TWO_SIDED=True  : fades both above (short) and below (long).
    When VWAP_MR_SIGMA_BAND=True : uses a rolling σ-band for entry/stop thresholds
                                   instead of the original fixed-points band.
    When VWAP_MR_FVG_AS_BONUS=True: a recent failed FVG improves the label but is
                                   no longer a hard gate (was the main filter pre-E1).
    """
    if not VWAP_MR_ENABLED or not _is_vwap_mr_window(dt_ct):
        return None

    required = ["vwap", "rsi", "adx", "atr", "atr_avg_20", "close", "high", "low", "open", "regime"]
    if VWAP_MR_SIGMA_BAND:
        required.append("vwap_sigma")
    for field_name in required:
        if field_name not in prev_row or pd.isna(prev_row[field_name]):
            return None

    if prev_row["regime"] != "choppy":
        return None
    if prev_row["vwap"] <= 0 or prev_row["atr_avg_20"] <= 0:
        return None

    atr_ratio = float(prev_row["atr"] / prev_row["atr_avg_20"])
    if atr_ratio > VWAP_MR_MAX_ATR_RATIO:
        return None
    adx = float(prev_row["adx"])
    if adx > VWAP_MR_MAX_ADX:
        return None

    vwap  = float(prev_row["vwap"])
    dist  = current_price - vwap          # positive = above VWAP, negative = below

    # ── Distance / entry threshold check ──────────────────────────────────────
    if VWAP_MR_SIGMA_BAND:
        sigma = float(prev_row["vwap_sigma"])
        if sigma <= 0 or pd.isna(sigma):
            return None
        entry_threshold = VWAP_MR_SIGMA_MULT * sigma
        stop_threshold  = VWAP_MR_SIGMA_STOP_MULT * sigma
        if abs(dist) < entry_threshold:
            return None
        # direction: price above VWAP = short fade; price below VWAP = long fade
        direction = "short" if dist > 0 else "long"
        if direction == "short" and not (VWAP_MR_TWO_SIDED or dist > 0):
            return None
        if direction == "long" and not VWAP_MR_TWO_SIDED:
            return None
    else:
        abs_dist_pts = abs(dist)
        if abs_dist_pts < VWAP_MR_MIN_DISTANCE_POINTS:
            return None
        if abs_dist_pts > VWAP_MR_MAX_DISTANCE_POINTS:
            return None
        direction = "short" if dist > 0 else "long"
        if direction == "long" and not VWAP_MR_TWO_SIDED:
            return None

    # ── Optional failed-FVG bonus / legacy gate ────────────────────────────────
    fvg_direction_sought = "bullish" if direction == "short" else "bearish"
    recent_failed_fvg = None
    for fvg in reversed(active_fvgs):
        if fvg.direction != fvg_direction_sought:
            continue
        age = current_bar - fvg.created_bar
        if age <= VWAP_MR_FAILED_FVG_MAX_AGE:
            recent_failed_fvg = fvg
            break
    if not VWAP_MR_FVG_AS_BONUS and recent_failed_fvg is None:
        return None   # legacy gate: require a failed FVG

    # ── RSI confirmation (short needs overbought; long needs oversold inverse) ─
    rsi = float(prev_row["rsi"])
    if direction == "short" and rsi < VWAP_MR_RSI_SHORT_MIN:
        return None
    if direction == "long" and rsi > (100 - VWAP_MR_RSI_SHORT_MIN):
        return None

    # ── Rejection-bar confirmation ─────────────────────────────────────────────
    prev_open  = float(prev_row["open"])
    prev_close = float(prev_row["close"])
    prev_high  = float(prev_row["high"])
    prev_low   = float(prev_row["low"])
    body       = abs(prev_close - prev_open)

    if direction == "short":
        upper_wick          = prev_high - max(prev_open, prev_close)
        bearish_rejection   = prev_close < prev_open and upper_wick >= max(body, MNQ_TICK_SIZE)
        breakdown_confirmed = current_price < prev_low
        if not (bearish_rejection or breakdown_confirmed):
            return None
    else:
        lower_wick         = min(prev_open, prev_close) - prev_low
        bullish_rejection  = prev_close > prev_open and lower_wick >= max(body, MNQ_TICK_SIZE)
        breakout_confirmed = current_price > prev_high
        if not (bullish_rejection or breakout_confirmed):
            return None

    # ── Target and stop ───────────────────────────────────────────────────────
    raw_target_ticks = int(abs(dist) / MNQ_TICK_SIZE) - VWAP_MR_TARGET_BUFFER_TICKS
    if raw_target_ticks < VWAP_MR_MIN_TARGET_TICKS:
        return None
    target_ticks = min(raw_target_ticks, VWAP_MR_MAX_TARGET_TICKS)

    if direction == "short":
        if VWAP_MR_SIGMA_BAND:
            stop_price = vwap + stop_threshold
        else:
            stop_price = prev_high + MNQ_TICK_SIZE
            stop_price = min(stop_price, current_price + VWAP_MR_STOP_TICKS * MNQ_TICK_SIZE)
        if stop_price <= current_price:
            return None
    else:
        if VWAP_MR_SIGMA_BAND:
            stop_price = vwap - stop_threshold
        else:
            stop_price = prev_low - MNQ_TICK_SIZE
            stop_price = max(stop_price, current_price - VWAP_MR_STOP_TICKS * MNQ_TICK_SIZE)
        if stop_price >= current_price:
            return None

    return {
        "direction":       direction,
        "target_ticks":    target_ticks,
        "stop_price":      stop_price,
        "regime":          "mean_revert",
        "distance_points": dist,
        "rsi":             rsi,
        "failed_fvg_age":  (current_bar - recent_failed_fvg.created_bar)
                           if recent_failed_fvg else -1,
    }


def _is_od_drive_bar(dt_ct) -> bool:
    from datetime import time as dtime
    t = dt_ct.time()
    start = dtime(OD_DRIVE_WINDOW_START[0], OD_DRIVE_WINDOW_START[1])
    end = dtime(OD_DRIVE_WINDOW_END[0], OD_DRIVE_WINDOW_END[1])
    return start <= t < end


def _is_od_entry_window(dt_ct) -> bool:
    from datetime import time as dtime
    t = dt_ct.time()
    start = dtime(OD_ENTRY_WINDOW_START[0], OD_ENTRY_WINDOW_START[1])
    end = dtime(OD_ENTRY_WINDOW_END[0], OD_ENTRY_WINDOW_END[1])
    return start <= t <= end


def _finalize_od_pullback_state(od: ODPullbackState, drive_row, current_bar: int) -> None:
    if od.formed or od.bars_seen == 0:
        return

    od.formed = True
    od.drive_size_ticks = abs(od.drive_close - od.drive_open) / MNQ_TICK_SIZE
    od.drive_range_ticks = (od.drive_high - od.drive_low) / MNQ_TICK_SIZE
    od.drive_midpoint = (od.drive_high + od.drive_low) / 2.0
    od.drive_end_bar = current_bar
    od.pullback_low = od.drive_high
    od.pullback_high = od.drive_low

    required = ["close", "vwap", "ema_fast", "ema_slow", "adx", "atr", "atr_avg_20"]
    for field_name in required:
        if field_name not in drive_row or pd.isna(drive_row[field_name]):
            return

    if od.drive_size_ticks < OD_MIN_DRIVE_TICKS or od.drive_size_ticks > OD_MAX_DRIVE_TICKS:
        return

    atr_avg = float(drive_row["atr_avg_20"])
    if atr_avg <= 0:
        return
    atr_ratio = float(drive_row["atr"] / atr_avg)
    if atr_ratio < OD_MIN_ATR_RATIO:
        return
    if float(drive_row["adx"]) < OD_MIN_ADX:
        return

    close = float(drive_row["close"])
    vwap = float(drive_row["vwap"])
    ema_fast = float(drive_row["ema_fast"])
    ema_slow = float(drive_row["ema_slow"])

    if close > od.drive_open and close > vwap and ema_fast > ema_slow:
        if OD_SHORT_ONLY:
            return
        od.direction = "long"
        od.eligible = True
        od.pullback_low = od.drive_high
        od.pullback_high = od.drive_high
    elif close < od.drive_open and close < vwap and ema_fast < ema_slow:
        od.direction = "short"
        od.eligible = True
        od.pullback_low = od.drive_low
        od.pullback_high = od.drive_low


def _update_od_pullback_state(od: ODPullbackState, prev_row) -> None:
    if not od.eligible or od.fired_today:
        return

    if od.direction == "long":
        od.pullback_low = min(od.pullback_low, float(prev_row["low"]))
        if not pd.isna(prev_row["ema_slow"]) and float(prev_row["low"]) <= float(prev_row["ema_slow"]):
            od.touched_ema21 = True
    elif od.direction == "short":
        od.pullback_high = max(od.pullback_high, float(prev_row["high"]))
        if not pd.isna(prev_row["ema_slow"]) and float(prev_row["high"]) >= float(prev_row["ema_slow"]):
            od.touched_ema21 = True


def _build_od_pullback_setup(
    prev_row,
    bar_2,
    current_price: float,
    dt_ct,
    od: ODPullbackState,
    current_bar: int,
):
    if not OD_PULLBACK_ENABLED or not _is_od_entry_window(dt_ct):
        return None
    if not od.eligible or od.fired_today or od.direction not in ("long", "short"):
        return None
    if current_bar <= od.drive_end_bar + 1:
        return None
    if any(pd.isna(prev_row[field_name]) for field_name in ["vwap", "ema_fast", "ema_slow", "high", "low"]):
        return None

    if od.direction == "long":
        pullback_depth_ticks = (od.drive_high - od.pullback_low) / MNQ_TICK_SIZE
        if pullback_depth_ticks <= 0:
            return None
        pullback_depth_pct = pullback_depth_ticks / max(od.drive_size_ticks, 1e-9)
        if pullback_depth_pct < OD_MIN_PULLBACK_PCT or pullback_depth_pct > OD_MAX_PULLBACK_PCT:
            return None
        if od.pullback_low < od.drive_midpoint:
            return None
        if current_price <= float(prev_row["vwap"]) or float(prev_row["ema_fast"]) <= float(prev_row["ema_slow"]):
            return None
        if pd.isna(bar_2["high"]):
            return None
        breakout_confirmed = float(prev_row["close"]) > float(bar_2["high"])
        if not breakout_confirmed:
            return None
        stop_price = od.pullback_low - MNQ_TICK_SIZE
        stop_price = max(stop_price, current_price - (OD_STOP_CAP_TICKS * MNQ_TICK_SIZE))
        if stop_price >= current_price:
            return None
    else:
        pullback_depth_ticks = (od.pullback_high - od.drive_low) / MNQ_TICK_SIZE
        if pullback_depth_ticks <= 0:
            return None
        pullback_depth_pct = pullback_depth_ticks / max(od.drive_size_ticks, 1e-9)
        if pullback_depth_pct < OD_MIN_PULLBACK_PCT or pullback_depth_pct > OD_MAX_PULLBACK_PCT:
            return None
        if od.pullback_high > od.drive_midpoint:
            return None
        if current_price >= float(prev_row["vwap"]) or float(prev_row["ema_fast"]) >= float(prev_row["ema_slow"]):
            return None
        if pd.isna(bar_2["low"]):
            return None
        breakout_confirmed = float(prev_row["close"]) < float(bar_2["low"])
        if not breakout_confirmed:
            return None
        stop_price = od.pullback_high + MNQ_TICK_SIZE
        stop_price = min(stop_price, current_price + (OD_STOP_CAP_TICKS * MNQ_TICK_SIZE))
        if stop_price <= current_price:
            return None

    return {
        "direction": od.direction,
        "target_ticks": OD_TARGET_TICKS,
        "stop_price": stop_price,
        "regime": "opening_drive",
        "drive_direction": od.direction,
        "drive_size_ticks": od.drive_size_ticks,
        "drive_range_ticks": od.drive_range_ticks,
        "pullback_depth_ticks": pullback_depth_ticks,
        "pullback_depth_pct": pullback_depth_pct,
        "touched_ema21": od.touched_ema21,
        "bars_from_drive_to_entry": current_bar - od.drive_end_bar,
    }


def _is_failed_breakout_window(dt_ct) -> bool:
    from datetime import time as dtime
    t = dt_ct.time()
    start = dtime(FB_WINDOW_START[0], FB_WINDOW_START[1])
    end = dtime(FB_WINDOW_END[0], FB_WINDOW_END[1])
    return start <= t <= end


def _build_failed_breakout_setup(
    prev_row,
    current_price: float,
    dt_ct,
    session_level_data: dict,
    excluded_levels=None,
):
    """Return a simple failed-breakout setup dict, or None."""
    if not FAILED_BREAKOUT_ENABLED or not _is_failed_breakout_window(dt_ct):
        return None
    if excluded_levels is None:
        excluded_levels = set()

    required = ["high", "low", "close", "vwap", "ema_fast", "ema_slow", "adx", "atr", "atr_avg_20"]
    for field_name in required:
        if field_name not in prev_row or pd.isna(prev_row[field_name]):
            return None

    atr_avg = float(prev_row["atr_avg_20"])
    if atr_avg <= 0:
        return None
    atr_ratio = float(prev_row["atr"] / atr_avg)
    if atr_ratio > FB_MAX_ATR_RATIO:
        return None
    adx = float(prev_row["adx"])
    if adx < FB_MIN_ADX or adx > FB_MAX_ADX:
        return None

    prev_high = float(prev_row["high"])
    prev_low = float(prev_row["low"])
    prev_close = float(prev_row["close"])
    vwap = float(prev_row["vwap"])
    ema_fast = float(prev_row["ema_fast"])
    ema_slow = float(prev_row["ema_slow"])

    level_candidates = [
        ("globex_high", float(session_level_data.get("globex_high", 0.0))),
        ("globex_low", float(session_level_data.get("globex_low", 0.0))),
        ("prev_day_high", float(session_level_data.get("prev_day_high", 0.0))),
        ("prev_day_low", float(session_level_data.get("prev_day_low", 0.0))),
    ]

    for level_type, level_price in level_candidates:
        if level_price <= 0:
            continue
        if level_type in excluded_levels:
            continue

        if level_type.endswith("_high"):
            sweep_distance_ticks = (prev_high - level_price) / MNQ_TICK_SIZE
            reentry_distance_ticks = (level_price - current_price) / MNQ_TICK_SIZE
            if sweep_distance_ticks < FB_MIN_SWEEP_TICKS or sweep_distance_ticks > FB_MAX_SWEEP_TICKS:
                continue
            if reentry_distance_ticks <= 0 or reentry_distance_ticks > FB_MAX_REENTRY_TICKS:
                continue
            if prev_close >= level_price or current_price >= level_price:
                continue
            if not (current_price < vwap or ema_fast <= ema_slow):
                continue

            stop_price = prev_high + MNQ_TICK_SIZE
            stop_price = min(stop_price, current_price + (FB_STOP_CAP_TICKS * MNQ_TICK_SIZE))
            if stop_price <= current_price:
                continue

            return {
                "direction": "short",
                "target_ticks": FB_TARGET_TICKS,
                "stop_price": stop_price,
                "regime": "failed_breakout",
                "level_type": level_type,
                "level_price": level_price,
                "sweep_distance_ticks": sweep_distance_ticks,
                "reentry_distance_ticks": reentry_distance_ticks,
                "bars_from_sweep_to_entry": 1,
            }

        sweep_distance_ticks = (level_price - prev_low) / MNQ_TICK_SIZE
        reentry_distance_ticks = (current_price - level_price) / MNQ_TICK_SIZE
        if sweep_distance_ticks < FB_MIN_SWEEP_TICKS or sweep_distance_ticks > FB_MAX_SWEEP_TICKS:
            continue
        if reentry_distance_ticks <= 0 or reentry_distance_ticks > FB_MAX_REENTRY_TICKS:
            continue
        if prev_close <= level_price or current_price <= level_price:
            continue
        if not (current_price > vwap or ema_fast >= ema_slow):
            continue

        stop_price = prev_low - MNQ_TICK_SIZE
        stop_price = max(stop_price, current_price - (FB_STOP_CAP_TICKS * MNQ_TICK_SIZE))
        if stop_price >= current_price:
            continue

        return {
            "direction": "long",
            "target_ticks": FB_TARGET_TICKS,
            "stop_price": stop_price,
            "regime": "failed_breakout",
            "level_type": level_type,
            "level_price": level_price,
            "sweep_distance_ticks": sweep_distance_ticks,
            "reentry_distance_ticks": reentry_distance_ticks,
            "bars_from_sweep_to_entry": 1,
        }

    return None


def _is_orb_window(dt_ct) -> bool:
    """Returns True if we are within the valid ORB entry window."""
    from datetime import time as dtime
    t     = dt_ct.time()
    start = dtime(ORB_ENTRY_WINDOW_START[0], ORB_ENTRY_WINDOW_START[1])
    end   = dtime(ORB_ENTRY_WINDOW_END[0],   ORB_ENTRY_WINDOW_END[1])
    return start <= t <= end


def _is_orb_forming_bar(dt_ct) -> bool:
    """Returns True if this bar is one of the range-forming bars (9:30–10:00)."""
    from datetime import time as dtime
    t     = dt_ct.time()
    start = dtime(ORB_ENTRY_WINDOW_START[0], ORB_ENTRY_WINDOW_START[1])
    end   = dtime(9, 55)  # last bar that ends at 10:00
    return start <= t <= end


# ── SESSION LEVELS PRE-COMPUTE ────────────────────────────────────────────────

def compute_session_levels(df: pd.DataFrame) -> Dict:
    if df.index.tzinfo is not None:
        idx_ct = df.index.tz_convert(TIMEZONE)
    else:
        idx_ct = df.index.tz_localize(TIMEZONE)

    df_ct = df.copy()
    df_ct.index = idx_ct
    df_ct["_ct_date"] = idx_ct.date

    session_data = {}
    dates = sorted(df_ct["_ct_date"].unique())

    from datetime import time as dtime
    reg_open    = dtime(9, 30)
    reg_close   = dtime(15, 8)
    globex_start = dtime(17, 0)

    range_list = []

    for i, d in enumerate(dates):
        day_bars = df_ct[df_ct["_ct_date"] == d]
        if len(day_bars) == 0:
            continue

        reg_mask = [(t.time() >= reg_open and t.time() <= reg_close)
                    for t in day_bars.index]
        reg_bars = day_bars[reg_mask]

        daily_high  = float(day_bars["high"].max())
        daily_low   = float(day_bars["low"].min())
        daily_range = daily_high - daily_low
        first_open  = float(day_bars["open"].iloc[0])
        day_close   = float(reg_bars["close"].iloc[-1]) if len(reg_bars) > 0 else first_open

        prev_high  = 0.0
        prev_low   = 0.0
        prev_close = 0.0
        prev_range = 0.0
        if i > 0:
            prev_d    = dates[i - 1]
            prev_bars = df_ct[df_ct["_ct_date"] == prev_d]
            prev_reg_mask = [(t.time() >= reg_open and t.time() <= reg_close)
                             for t in prev_bars.index]
            prev_reg = prev_bars[prev_reg_mask]
            if len(prev_reg) > 0:
                prev_high  = float(prev_reg["high"].max())
                prev_low   = float(prev_reg["low"].min())
                prev_close = float(prev_reg["close"].iloc[-1])
                prev_range = prev_high - prev_low

        globex_mask = [(t.time() >= globex_start or t.time() < reg_open)
                       for t in day_bars.index]
        globex_bars = day_bars[globex_mask]
        if len(globex_bars) > 0:
            globex_high  = float(globex_bars["high"].max())
            globex_low   = float(globex_bars["low"].min())
            globex_range = globex_high - globex_low
        else:
            globex_high  = first_open
            globex_low   = first_open
            globex_range = 0.0

        opening_gap    = first_open - prev_close if prev_close > 0 else 0.0
        opened_above   = (first_open > prev_close) if prev_close > 0 else False

        session_data[d] = {
            "daily_high":         daily_high,
            "daily_low":          daily_low,
            "daily_range":        daily_range,
            "daily_close":        day_close,
            "first_open":         first_open,
            "prev_day_high":      prev_high,
            "prev_day_low":       prev_low,
            "prev_day_close":     prev_close,
            "prev_day_range":     prev_range,
            "globex_high":        globex_high,
            "globex_low":         globex_low,
            "globex_range":       globex_range,
            "opening_gap_points": opening_gap,
            "opened_above_prev":  opened_above,
        }
        range_list.append((d, daily_range))

    for idx_i, (d, _) in enumerate(range_list):
        lookback = range_list[max(0, idx_i - 20): idx_i]
        if len(lookback) >= 5:
            avg_range = np.mean([r for _, r in lookback])
        else:
            avg_range = session_data[d]["daily_range"]
        cur_range = session_data[d]["daily_range"]
        session_data[d]["avg_20_range"] = avg_range
        session_data[d]["range_vs_avg"] = (cur_range / avg_range) if avg_range > 0 else 1.0
        session_data[d]["is_trend_day"] = (cur_range > 1.5 * avg_range)

    return session_data


# ── BACKTEST ENGINE ───────────────────────────────────────────────────────────

def run_backtest(
    df: pd.DataFrame,
    session_levels: Dict,
    live_signal_sink: Optional[Dict[str, object]] = None,
) -> Tuple[pd.DataFrame, List[TradeRecord], List[DailyRecord], RiskState]:

    state   = RiskState()
    trades:        List[TradeRecord] = []
    daily_records: List[DailyRecord] = []

    cash          = INIT_CASH
    portfolio     = [INIT_CASH, INIT_CASH]
    in_trade      = False
    direction     = None
    entry_price   = 0.0
    fvg_stop      = 0.0
    contracts     = 0
    target_ticks  = NORMAL_TARGET_TICKS
    entry_regime  = "unknown"
    entry_type    = "FVG"
    entry_bar_idx = 0
    partial_taken      = False   # has partial profit fired for current trade?
    partial_pnl_banked = 0.0    # P&L locked in from partial exit

    trade_mfe = 0.0
    trade_mae = 0.0

    current_day   = None
    active_fvgs:  List[FVG] = []
    orb           = ORBState()
    od            = ODPullbackState()

    trade_number_overall  = 0
    trade_number_today    = 0
    consecutive_wins      = 0
    consecutive_losses    = 0
    fb_levels_seen_today  = set()

    day_open_cash   = INIT_CASH
    day_peak_cash   = INIT_CASH
    day_trough_cash = INIT_CASH
    day_trades_list: List[TradeRecord] = []

    entry_snapshot: dict = {}

    for i in range(2, len(df)):
        row      = df.iloc[i]
        prev_row = df.iloc[i - 1]
        bar_2    = df.iloc[i - 2]
        date     = df.index[i]

        if hasattr(date, "tzinfo") and date.tzinfo is not None:
            date_ct = date.astimezone(TIMEZONE)
        else:
            date_ct = TIMEZONE.localize(date)

        session_date = date_ct.date()
        sl = session_levels.get(session_date, {})

        # ── Day rollover ──────────────────────────────────────────────────────
        if current_day != session_date:
            if current_day is not None:
                state.end_of_day(cash)
                sl = session_levels.get(current_day, {})
                dr = _build_daily_record(
                    session_date    = current_day,
                    day_trades      = day_trades_list,
                    day_open_cash   = day_open_cash,
                    day_peak_cash   = day_peak_cash,
                    day_trough_cash = day_trough_cash,
                    session_levels  = sl,
                    cash            = cash,
                )
                daily_records.append(dr)

            current_day       = session_date
            trade_number_today = 0
            day_open_cash     = cash
            day_peak_cash     = cash
            day_trough_cash   = cash
            day_trades_list   = []

            # Reset ORB for new session
            orb = ORBState(session_date=session_date)
            od  = ODPullbackState(session_date=session_date)
            fb_levels_seen_today = set()

            # Clear FVGs from previous session
            active_fvgs = [f for f in active_fvgs if f.session_date == session_date]

        # V28: Skip July entirely
        if SKIP_JULY and date_ct.month == 7:
            portfolio.append(cash)
            continue

        if FVG_FRESH_ONLY:
            for fvg in active_fvgs:
                if not fvg.tested and fvg.created_bar < (i - 1):
                    if bar_touches_fvg(fvg, float(prev_row["high"]), float(prev_row["low"])):
                        fvg.tested = True

        # Track intraday cash extremes
        if cash > day_peak_cash:
            day_peak_cash = cash
        if cash < day_trough_cash:
            day_trough_cash = cash

        # Track min buffer over floor
        state.min_buffer_over_floor = min(
            state.min_buffer_over_floor,
            cash - state.max_loss_floor
        )

        # ── MAE/MFE update ────────────────────────────────────────────────────
        if in_trade:
            if direction == "long":
                favorable   = row["high"] - entry_price
                unfavorable = entry_price - row["low"]
            else:
                favorable   = entry_price - row["low"]
                unfavorable = row["high"] - entry_price
            if favorable  > trade_mfe: trade_mfe = favorable
            if unfavorable > trade_mae: trade_mae = unfavorable

        # ── ORB range formation ───────────────────────────────────────────────
        # Accumulate highs/lows for the first ORB_RANGE_BARS bars after 9:30
        if (not orb.formed
                and orb.session_date == session_date
                and _is_orb_forming_bar(date_ct)):
            if orb.bars_seen == 0:
                orb.high = float(row["high"])
                orb.low  = float(row["low"])
            else:
                if row["high"] > orb.high: orb.high = float(row["high"])
                if row["low"]  < orb.low:  orb.low  = float(row["low"])
            orb.bars_seen += 1
            if orb.bars_seen >= ORB_RANGE_BARS:
                orb.range_ticks = (orb.high - orb.low) / MNQ_TICK_SIZE
                orb.formed = True

        # Build opening-drive state from the first three 5m bars after 9:30
        if od.session_date == session_date and _is_od_drive_bar(date_ct):
            if od.bars_seen == 0:
                od.drive_open = float(row["open"])
                od.drive_high = float(row["high"])
                od.drive_low = float(row["low"])
            else:
                od.drive_high = max(od.drive_high, float(row["high"]))
                od.drive_low = min(od.drive_low, float(row["low"]))
            od.drive_close = float(row["close"])
            od.bars_seen += 1
        elif od.session_date == session_date and not od.formed and od.bars_seen > 0:
            _finalize_od_pullback_state(od, prev_row, i - 1)

        if (od.session_date == session_date
                and od.formed
                and i > od.drive_end_bar + 1
                and _is_od_entry_window(date_ct)):
            _update_od_pullback_state(od, prev_row)

        # ── Detect new FVG ────────────────────────────────────────────────────
        fvg_size_min = FVG_MIN_SIZE_TICKS * MNQ_TICK_SIZE

        if bar_2["high"] < row["low"]:
            gap_size = row["low"] - bar_2["high"]
            if gap_size >= fvg_size_min:
                active_fvgs.append(FVG(
                    direction="bullish",
                    top=row["low"],
                    bottom=bar_2["high"],
                    created_bar=i,
                    session_date=session_date,
                ))

        if bar_2["low"] > row["high"]:
            gap_size = bar_2["low"] - row["high"]
            if gap_size >= fvg_size_min:
                active_fvgs.append(FVG(
                    direction="bearish",
                    top=bar_2["low"],
                    bottom=row["high"],
                    created_bar=i,
                    session_date=session_date,
                ))

        # ── Prune stale FVGs ──────────────────────────────────────────────────
        active_fvgs = [
            f for f in active_fvgs
            if is_fvg_valid(f, i, session_date, row["high"], row["low"])
        ]

        # ── Hard flatten outside session ──────────────────────────────────────
        if in_trade and not _is_in_session(date_ct):
            exit_price    = row["open"]
            pnl_pts       = ((exit_price - entry_price) if direction == "long"
                             else (entry_price - exit_price))
            gross_pnl_usd = pnl_pts * MNQ_POINT_VALUE * contracts
            costs_usd     = round_turn_cost(contracts)
            pnl_usd       = gross_pnl_usd - costs_usd
            pnl_pct       = pnl_pts / entry_price
            won           = pnl_usd > 0
            cash          += pnl_usd

            state.update(won, pnl_pct, pnl_usd)
            if cash > day_peak_cash:   day_peak_cash   = cash
            if cash < day_trough_cash: day_trough_cash = cash

            tr = _build_trade_record(
                entry_snapshot=entry_snapshot,
                date=date, direction=direction,
                entry_price=entry_price, exit_price=exit_price,
                pnl_pts=pnl_pts, gross_pnl_usd=gross_pnl_usd,
                costs_usd=costs_usd, pnl_usd=pnl_usd,
                pnl_pct=pnl_pct, won=won, cash=cash,
                contracts=contracts, entry_regime=entry_regime,
                exit_reason="flatten", fvg_stop=fvg_stop,
                trade_mae=trade_mae, trade_mfe=trade_mfe,
                bars_to_exit=i - entry_bar_idx, state=state,
            )
            trades.append(tr)
            day_trades_list.append(tr)

            if won: consecutive_wins += 1;  consecutive_losses = 0
            else:   consecutive_losses += 1; consecutive_wins = 0

            in_trade           = False
            trade_mfe          = 0.0
            trade_mae          = 0.0
            partial_taken      = False
            partial_pnl_banked = 0.0

        # ── Exit logic ────────────────────────────────────────────────────────
        if in_trade:
            target_pts  = target_ticks * MNQ_TICK_SIZE
            partial_pts = PARTIAL_PROFIT_TICKS * MNQ_TICK_SIZE
            if direction == "long":
                stop_px       = fvg_stop
                target_px     = entry_price + target_pts
                partial_px    = entry_price + partial_pts
                hit_stop      = row["low"]  <= stop_px
                hit_target    = row["high"] >= target_px
                hit_partial   = (PARTIAL_PROFIT_ENABLED
                                 and not partial_taken
                                 and contracts > 1
                                 and row["high"] >= partial_px)
            else:
                stop_px       = fvg_stop
                target_px     = entry_price - target_pts
                partial_px    = entry_price - partial_pts
                hit_stop      = row["high"] >= stop_px
                hit_target    = row["low"]  <= target_px
                hit_partial   = (PARTIAL_PROFIT_ENABLED
                                 and not partial_taken
                                 and contracts > 1
                                 and row["low"] <= partial_px)

            exit_price  = None
            exit_reason = None

            # Partial profit fires first — close fraction, continue with rest
            if hit_partial:
                partial_contracts  = max(1, round(contracts * PARTIAL_PROFIT_FRACTION))
                remaining          = contracts - partial_contracts
                pp_pts             = ((partial_px - entry_price) if direction == "long"
                                      else (entry_price - partial_px))
                pp_gross           = pp_pts * MNQ_POINT_VALUE * partial_contracts
                pp_costs           = round_turn_cost(partial_contracts)
                pp_net             = pp_gross - pp_costs
                cash              += pp_net
                state.daily_pnl_usd += pp_net
                partial_pnl_banked += pp_net
                contracts          = remaining
                partial_taken      = True
                if cash > day_peak_cash:   day_peak_cash   = cash
                if cash < day_trough_cash: day_trough_cash = cash

            if hit_target:
                exit_price  = target_px
                exit_reason = "target"
            elif hit_stop:
                exit_price  = stop_px
                exit_reason = "stop"
            elif ((direction == "long"  and not prev_row["long_signal"]) or
                  (direction == "short" and not prev_row["short_signal"])):
                exit_price  = row["open"]
                exit_reason = "signal_flip"

            if exit_price is not None:
                pnl_pts       = ((exit_price - entry_price) if direction == "long"
                                 else (entry_price - exit_price))
                gross_pnl_usd = pnl_pts * MNQ_POINT_VALUE * contracts
                costs_usd     = round_turn_cost(contracts)
                pnl_usd       = gross_pnl_usd - costs_usd
                pnl_pct       = pnl_pts / entry_price
                # won = True if final leg OR banked partial makes total positive
                won           = (pnl_usd + partial_pnl_banked) > 0
                cash          += pnl_usd

                state.update(won, pnl_pct, pnl_usd)
                if cash > day_peak_cash:   day_peak_cash   = cash
                if cash < day_trough_cash: day_trough_cash = cash

                tr = _build_trade_record(
                    entry_snapshot=entry_snapshot,
                    date=date, direction=direction,
                    entry_price=entry_price, exit_price=exit_price,
                    pnl_pts=pnl_pts, gross_pnl_usd=gross_pnl_usd,
                    costs_usd=costs_usd, pnl_usd=pnl_usd,
                    pnl_pct=pnl_pct, won=won, cash=cash,
                    contracts=contracts, entry_regime=entry_regime,
                    exit_reason=exit_reason, fvg_stop=fvg_stop,
                    trade_mae=trade_mae, trade_mfe=trade_mfe,
                    bars_to_exit=i - entry_bar_idx, state=state,
                )
                trades.append(tr)
                day_trades_list.append(tr)

                if won: consecutive_wins += 1;  consecutive_losses = 0
                else:   consecutive_losses += 1; consecutive_wins = 0

                in_trade           = False
                trade_mfe          = 0.0
                trade_mae          = 0.0
                partial_taken      = False
                partial_pnl_banked = 0.0

        # ── Entry logic ───────────────────────────────────────────────────────
        if not in_trade:
            if not _is_entry_allowed(date_ct):
                portfolio.append(cash)
                continue

            if cash <= state.max_loss_floor:
                state.floor_breached = True

            if state.daily_pnl_usd <= BOT_DAILY_LOSS_LIMIT:
                portfolio.append(cash)
                continue

            current_price  = row["open"]
            entry_fvg      = None
            entry_dir      = None
            this_entry_type = None
            vwap_mr_setup  = None
            od_setup       = None
            fb_setup       = None

            # ── E3: Regime router ─────────────────────────────────────────────
            # When REGIME_ROUTER_ENABLED=True, decide which strategy stack is
            # allowed based on the current regime + session clock:
            #   trending session  →  FVG + ORB only (directional stack)
            #   choppy+MR window  →  VWAP_MR + FAILED_BREAKOUT only (fade stack)
            #   neutral           →  stand aside (no entries)
            # When disabled, all enabled modules compete as before (legacy).
            if REGIME_ROUTER_ENABLED:
                _bar_regime  = classify_regime_adx(prev_row)
                _bar_phase   = session_phase(date_ct)
                _in_mr_win   = is_mean_reversion_window(prev_row, date_ct)
                _in_trend_win = _bar_phase in ("am_trend", "pm_trend")
                if _bar_regime == "neutral":
                    # E5 neutral zone: stand aside regardless of NEUTRAL_NO_TRADE flag
                    portfolio.append(cash)
                    continue
                if _bar_regime == "trending" or _in_trend_win:
                    _router_allow_trend = True
                    _router_allow_mr    = False
                elif _in_mr_win:
                    _router_allow_trend = False
                    _router_allow_mr    = True
                else:
                    # choppy but outside MR window — stand aside
                    portfolio.append(cash)
                    continue
            elif NEUTRAL_NO_TRADE and classify_regime_adx(prev_row) == "neutral":
                # E5 standalone: neutral zone no-trade without full router
                portfolio.append(cash)
                continue
            else:
                _router_allow_trend = True
                _router_allow_mr    = True

            # Pre-compute regime profile for regime-gated modules such as FVG.
            # ORB sizing may still reference the strong regime, but ORB entry itself
            # should not be blocked by the FVG/ATR gate.
            profile       = get_risk_profile(prev_row)
            orb_regime_ok = False  # will be set True if ATR is strong at ORB entry

            # ── ORB ENTRY (priority, independent of ATR regime) ──────────────
            # ORB fires BEFORE regime gate — has its own quality filters
            # (VWAP confirmation + breakout confirmation)
            if (_router_allow_trend
                    and orb.formed
                    and not orb.fired_today
                    and _is_orb_window(date_ct)
                    and orb.range_ticks <= ORB_MAX_RANGE_TICKS
                    and orb.range_ticks >= 4
                    and not pd.isna(prev_row["vwap"])
                    and state.daily_trades < STRONG_MAX_TRADES):

                orb_long_signal  = (current_price > orb.high
                                    and prev_row["vwap"] > 0
                                    and current_price > prev_row["vwap"])

                orb_short_signal = (current_price < orb.low
                                    and prev_row["vwap"] > 0
                                    and current_price < prev_row["vwap"])

                if orb_long_signal:
                    entry_dir       = "long"
                    this_entry_type = "ORB"
                elif orb_short_signal:
                    entry_dir       = "short"
                    this_entry_type = "ORB"

            # ── Opening-drive first pullback (independent morning continuation module) ──
            if (entry_dir is None
                    and _router_allow_mr
                    and FAILED_BREAKOUT_ENABLED
                    and state.daily_trades < STRONG_MAX_TRADES):
                fb_trades_today = sum(1 for t in day_trades_list if t.entry_type == "FAILED_BREAKOUT")
                if fb_trades_today < FB_MAX_TRADES_PER_DAY:
                    fb_setup = _build_failed_breakout_setup(
                        prev_row=prev_row,
                        current_price=current_price,
                        dt_ct=date_ct,
                        session_level_data=sl,
                        excluded_levels=fb_levels_seen_today,
                    )
                    if fb_setup is not None:
                        fb_levels_seen_today.add(fb_setup["level_type"])
                        entry_dir = fb_setup["direction"]
                        this_entry_type = "FAILED_BREAKOUT"
                else:
                    skipped_fb_setup = _build_failed_breakout_setup(
                        prev_row=prev_row,
                        current_price=current_price,
                        dt_ct=date_ct,
                        session_level_data=sl,
                        excluded_levels=fb_levels_seen_today,
                    )
                    if skipped_fb_setup is not None:
                        fb_levels_seen_today.add(skipped_fb_setup["level_type"])
                        state.fb_cap_skips.append({
                            "date": date,
                            "entry_hour": date_ct.hour,
                            "direction": skipped_fb_setup["direction"],
                            "level_type": skipped_fb_setup["level_type"],
                            "sweep_distance_ticks": float(skipped_fb_setup["sweep_distance_ticks"]),
                            "reentry_distance_ticks": float(skipped_fb_setup["reentry_distance_ticks"]),
                        })

            if (entry_dir is None
                    and OD_PULLBACK_ENABLED
                    and state.daily_trades < STRONG_MAX_TRADES):
                od_trades_today = sum(1 for t in day_trades_list if t.entry_type == "OD_PULLBACK")
                if od_trades_today < OD_MAX_TRADES_PER_DAY:
                    od_setup = _build_od_pullback_setup(
                        prev_row=prev_row,
                        bar_2=bar_2,
                        current_price=current_price,
                        dt_ct=date_ct,
                        od=od,
                        current_bar=i,
                    )
                    if od_setup is not None:
                        entry_dir = od_setup["direction"]
                        this_entry_type = "OD_PULLBACK"

            # ── FVG ENTRY (requires ATR regime, fires if ORB/OD not triggered) ──
            if entry_dir is None and _router_allow_trend:
                profile = get_risk_profile(prev_row)
                if profile["contracts"] > 0 and state.daily_trades < profile["max_trades"]:
                    if prev_row["long_bias"]:
                        for fvg in active_fvgs:
                            if (fvg.direction == "bullish"
                                    and (not FVG_FRESH_ONLY or not fvg.tested)
                                    and price_in_fvg(fvg, current_price)):
                                entry_fvg       = fvg
                                entry_dir       = "long"
                                this_entry_type = "FVG"
                                break

                    if entry_dir is None and prev_row["short_bias"]:
                        for fvg in active_fvgs:
                            if (fvg.direction == "bearish"
                                    and (not FVG_FRESH_ONLY or not fvg.tested)
                                    and price_in_fvg(fvg, current_price)):
                                entry_fvg       = fvg
                                entry_dir       = "short"
                                this_entry_type = "FVG"
                                break

            if (entry_dir is None
                    and _router_allow_mr
                    and VWAP_MR_ENABLED
                    and state.daily_trades < STRONG_MAX_TRADES):
                vwap_mr_trades_today = sum(1 for t in day_trades_list if t.entry_type == "VWAP_MR")
                if vwap_mr_trades_today < VWAP_MR_MAX_TRADES_PER_DAY:
                    vwap_mr_setup = _build_vwap_mr_setup(
                        prev_row=prev_row,
                        current_price=current_price,
                        dt_ct=date_ct,
                        active_fvgs=active_fvgs,
                        current_bar=i,
                    )
                    if vwap_mr_setup is not None:
                        entry_dir = vwap_mr_setup["direction"]
                        this_entry_type = "VWAP_MR"

            if entry_dir is None:
                portfolio.append(cash)
                continue

            # ── Skip afternoon longs (45.3% WR — weakest segment) ────────────
            if not _passes_strategy_filters(
                prev_row=prev_row,
                dt_ct=date_ct,
                session_date=session_date,
                entry_dir=entry_dir,
                entry_type=this_entry_type,
                fvg_age_bars=(i - entry_fvg.created_bar if this_entry_type == "FVG" and entry_fvg else 0),
            ):
                portfolio.append(cash)
                continue

            # ── Drawdown scaling ──────────────────────────────────────────────
            # ORB with strong regime: 5 contracts
            # ORB without strong regime: 3 contracts (ORB_INDEPENDENT_CONTRACTS)
            # FVG: uses regime profile contracts
            fvg_quality = _compute_fvg_quality_score(
                prev_row=prev_row,
                dt_ct=date_ct,
                entry_dir=entry_dir,
                entry_fvg=entry_fvg,
                current_bar=i,
                session_level_data=sl,
            )

            peak_equity  = state.eod_high_balance
            drawdown_pct = (cash - peak_equity) / peak_equity * 100
            if this_entry_type == "ORB":
                orb_regime_ok = (not pd.isna(prev_row["atr"]) and
                                 not pd.isna(prev_row["atr_avg_20"]) and
                                 prev_row["atr_avg_20"] > 0 and
                                 prev_row["atr"] / prev_row["atr_avg_20"] >= STRONG_ATR_RATIO and
                                 prev_row["atr"] >= 1.5 and
                                 prev_row["adx"] >= ADX_STRONG_THRESHOLD)
                base_contracts = STRONG_CONTRACTS if orb_regime_ok else ORB_INDEPENDENT_CONTRACTS
            elif this_entry_type == "OD_PULLBACK":
                base_contracts = OD_CONTRACTS
            elif this_entry_type == "FAILED_BREAKOUT":
                base_contracts = FB_CONTRACTS
            elif this_entry_type == "VWAP_MR":
                base_contracts = VWAP_MR_CONTRACTS
            else:
                base_contracts = profile["contracts"]
            if drawdown_pct > -2.5:
                contracts = base_contracts
            elif drawdown_pct > -1.5:
                contracts = max(2, base_contracts - 2)
            else:
                contracts = 2

            # ── VWAP size modifier ────────────────────────────────────────────
            above_vwap_now = current_price > prev_row["vwap"]
            if entry_dir == "long"  and not above_vwap_now:
                contracts = max(2, contracts - 2)
            elif entry_dir == "short" and above_vwap_now:
                contracts = max(2, contracts - 2)

            if this_entry_type == "FVG" and FVG_SCORE_SIZING_ENABLED:
                fvg_score = int(fvg_quality["score"])
                if drawdown_pct > -2.5:
                    if fvg_score >= FVG_SCORE_HIGH_THRESHOLD:
                        contracts = min(STRONG_CONTRACTS, contracts + FVG_SCORE_HIGH_SIZE_STEP)
                    elif fvg_score >= FVG_SCORE_MEDIUM_THRESHOLD:
                        contracts = min(STRONG_CONTRACTS, contracts + FVG_SCORE_MEDIUM_SIZE_STEP)
            elif (this_entry_type == "FAILED_BREAKOUT"
                  and FB_QUALITY_SIZING_ENABLED
                  and drawdown_pct > -2.5
                  and fb_setup is not None
                  and str(fb_setup.get("level_type", "")).startswith("globex_")):
                contracts = min(STRONG_CONTRACTS, contracts + FB_GLOBEX_SIZE_STEP)

            # ── Stop placement ────────────────────────────────────────────────
            if this_entry_type == "ORB":
                # ORB stop: other side of the range + buffer
                if entry_dir == "long":
                    raw_stop = orb.low - ORB_STOP_BUFFER_TICKS * MNQ_TICK_SIZE
                    fvg_stop = max(raw_stop, current_price - (STOP_TICKS * MNQ_TICK_SIZE))
                else:
                    raw_stop = orb.high + ORB_STOP_BUFFER_TICKS * MNQ_TICK_SIZE
                    fvg_stop = min(raw_stop, current_price + (STOP_TICKS * MNQ_TICK_SIZE))

                # ORB target: max(1x range, 20 ticks)
                range_ticks = orb.range_ticks
                target_ticks_orb = max(int(range_ticks), ORB_MIN_TARGET_TICKS)
                target_ticks = target_ticks_orb
                orb.fired_today = True

            else:
                if this_entry_type == "OD_PULLBACK":
                    fvg_stop = od_setup["stop_price"]
                    target_ticks = int(od_setup["target_ticks"])
                    od.fired_today = True
                elif this_entry_type == "FAILED_BREAKOUT":
                    fvg_stop = fb_setup["stop_price"]
                    target_ticks = int(fb_setup["target_ticks"])
                elif this_entry_type == "VWAP_MR":
                    fvg_stop = vwap_mr_setup["stop_price"]
                    target_ticks = int(vwap_mr_setup["target_ticks"])
                else:
                    # FVG stop: beyond FVG boundary
                    if entry_dir == "long":
                        fvg_stop = entry_fvg.bottom - MNQ_TICK_SIZE
                        fvg_stop = max(fvg_stop, current_price - (STOP_TICKS * MNQ_TICK_SIZE))
                    else:
                        fvg_stop = entry_fvg.top + MNQ_TICK_SIZE
                        fvg_stop = min(fvg_stop, current_price + (STOP_TICKS * MNQ_TICK_SIZE))

                    target_ticks = _target_ticks_for_entry(
                        base_target_ticks=profile["target_ticks"],
                        dt_ct=date_ct,
                        entry_dir=entry_dir,
                        entry_type=this_entry_type,
                    )
                    active_fvgs  = [f for f in active_fvgs if f is not entry_fvg]

            if this_entry_type == "FVG":
                entry_regime = profile["regime"]
            elif this_entry_type == "ORB":
                entry_regime = "strong" if orb_regime_ok else "orb_independent"
            elif this_entry_type == "OD_PULLBACK":
                entry_regime = od_setup["regime"]
            elif this_entry_type == "FAILED_BREAKOUT":
                entry_regime = fb_setup["regime"]
            else:
                entry_regime = vwap_mr_setup["regime"]
            in_trade     = True
            direction    = entry_dir
            entry_price  = current_price
            entry_type   = this_entry_type
            entry_bar_idx = i
            trade_mfe    = 0.0
            trade_mae    = 0.0
            partial_taken      = False
            partial_pnl_banked = 0.0
            trade_number_overall += 1
            trade_number_today   += 1

            # ── Build entry snapshot ──────────────────────────────────────────
            net_profit_now = cash - INIT_CASH
            tier_now       = compute_scaling_tier(net_profit_now)

            tgt_pts = target_ticks * MNQ_TICK_SIZE
            tgt_px  = ((current_price + tgt_pts) if entry_dir == "long"
                       else (current_price - tgt_pts))
            stop_w_ticks = abs(current_price - fvg_stop) / MNQ_TICK_SIZE

            date_str_news = str(session_date)
            is_fomc_today = date_str_news in FOMC_DATES
            is_cpi_today  = date_str_news in CPI_DATES
            is_nfp_today  = date_str_news in NFP_DATES
            is_news_today = date_str_news in ALL_NEWS_DATES

            open_hour   = 9
            open_minute = 30
            entry_h     = date_ct.hour
            entry_m     = date_ct.minute
            sess_min    = max(0, (entry_h - open_hour) * 60 + (entry_m - open_minute))

            dow_map = {0:"Mon",1:"Tue",2:"Wed",3:"Thu",4:"Fri",5:"Sat",6:"Sun"}
            dow_str = dow_map.get(date_ct.weekday(), "")

            buf    = cash - state.max_loss_floor
            dl_pct = (state.daily_pnl_usd / abs(BOT_DAILY_LOSS_LIMIT) * 100
                      if BOT_DAILY_LOSS_LIMIT != 0 else 0.0)
            mll_distance = cash - state.max_loss_floor
            mll_pct = ((EOD_LOSS_BUFFER - mll_distance) / EOD_LOSS_BUFFER * 100
                       if EOD_LOSS_BUFFER > 0 else 0.0)

            combine_profit = cash - INIT_CASH
            combine_remain = max(0.0, COMBINE_PROFIT_TARGET - combine_profit)

            entry_snapshot = {
                "entry_type":                  this_entry_type,
                "entry_hour":                  entry_h,
                "session_minute":              sess_min,
                "day_of_week":                 dow_str,
                "month":                       date_ct.month,
                "year":                        date_ct.year,
                "quarter":                     (date_ct.month - 1) // 3 + 1,
                "atr_at_entry":                float(prev_row["atr"]) if not pd.isna(prev_row["atr"]) else 0.0,
                "atr_ratio_at_entry":          (float(prev_row["atr"] / prev_row["atr_avg_20"])
                                                if not pd.isna(prev_row["atr_avg_20"])
                                                and prev_row["atr_avg_20"] > 0 else 0.0),
                "adx_at_entry":                float(prev_row["adx"]) if not pd.isna(prev_row["adx"]) else 0.0,
                "vwap_at_entry":               float(prev_row["vwap"]) if not pd.isna(prev_row["vwap"]) else 0.0,
                "price_distance_from_vwap":    (current_price - float(prev_row["vwap"])
                                                if not pd.isna(prev_row["vwap"]) else 0.0),
                "ema_fast_at_entry":           float(prev_row["ema_fast"]) if not pd.isna(prev_row["ema_fast"]) else 0.0,
                "ema_slow_at_entry":           float(prev_row["ema_slow"]) if not pd.isna(prev_row["ema_slow"]) else 0.0,
                "ema_spread_at_entry":         (abs(float(prev_row["ema_fast"]) - float(prev_row["ema_slow"]))
                                                if not pd.isna(prev_row["ema_fast"])
                                                and not pd.isna(prev_row["ema_slow"]) else 0.0),
                "fvg_size_ticks":              ((entry_fvg.top - entry_fvg.bottom) / MNQ_TICK_SIZE
                                                if entry_fvg else 0.0),
                "fvg_age_bars":                (i - entry_fvg.created_bar if entry_fvg else 0),
                "fvg_type":                    (entry_fvg.direction if entry_fvg else ""),
                "fvg_quality_score":           (int(fvg_quality["score"]) if entry_fvg else 0),
                "fvg_quality_flags":           (str(fvg_quality["flags"]) if entry_fvg else ""),
                "orb_range_ticks":             orb.range_ticks if orb.formed else 0.0,
                "orb_high":                    orb.high if orb.formed else 0.0,
                "orb_low":                     orb.low  if orb.formed else 0.0,
                "stop_price":                  fvg_stop,
                "target_price":                tgt_px,
                "stop_width_ticks":            stop_w_ticks,
                "trade_number_today":          trade_number_today,
                "trade_number_overall":        trade_number_overall,
                "consecutive_wins_before":     consecutive_wins,
                "consecutive_losses_before":   consecutive_losses,
                "daily_pnl_at_entry":          state.daily_pnl_usd,
                "drawdown_pct_at_entry":       drawdown_pct,
                "buffer_above_floor_at_entry": buf,
                "scaling_tier_at_entry":       tier_now,
                "prev_day_high":               sl.get("prev_day_high", 0.0),
                "prev_day_low":                sl.get("prev_day_low", 0.0),
                "prev_day_range":              sl.get("prev_day_range", 0.0),
                "globex_high":                 sl.get("globex_high", 0.0),
                "globex_low":                  sl.get("globex_low", 0.0),
                "opened_above_prev_close":     sl.get("opened_above_prev", False),
                "opening_gap_points":          sl.get("opening_gap_points", 0.0),
                "is_fomc_day":                 is_fomc_today,
                "is_cpi_day":                  is_cpi_today,
                "is_nfp_day":                  is_nfp_today,
                "is_news_day":                 is_news_today,
                "vwap_mr_distance_points":     (float(vwap_mr_setup["distance_points"])
                                                if vwap_mr_setup else 0.0),
                "vwap_mr_rsi":                 (float(vwap_mr_setup["rsi"])
                                                if vwap_mr_setup else 0.0),
                "vwap_mr_failed_fvg_age":      (int(vwap_mr_setup["failed_fvg_age"])
                                                if vwap_mr_setup else 0),
                "vwap_mr_target_ticks":        (int(vwap_mr_setup["target_ticks"])
                                                if vwap_mr_setup else 0),
                "od_drive_direction":          (od_setup["drive_direction"]
                                                if od_setup else ""),
                "od_drive_size_ticks":         (float(od_setup["drive_size_ticks"])
                                                if od_setup else 0.0),
                "od_drive_range_ticks":        (float(od_setup["drive_range_ticks"])
                                                if od_setup else 0.0),
                "od_pullback_depth_ticks":     (float(od_setup["pullback_depth_ticks"])
                                                if od_setup else 0.0),
                "od_pullback_depth_pct":       (float(od_setup["pullback_depth_pct"])
                                                if od_setup else 0.0),
                "od_touched_ema21":            (bool(od_setup["touched_ema21"])
                                                if od_setup else False),
                "od_bars_from_drive_to_entry": (int(od_setup["bars_from_drive_to_entry"])
                                                if od_setup else 0),
                "od_target_ticks":             (int(od_setup["target_ticks"])
                                                if od_setup else 0),
                "fb_level_type":               (fb_setup["level_type"]
                                                if fb_setup else ""),
                "fb_level_price":              (float(fb_setup["level_price"])
                                                if fb_setup else 0.0),
                "fb_sweep_distance_ticks":     (float(fb_setup["sweep_distance_ticks"])
                                                if fb_setup else 0.0),
                "fb_reentry_distance_ticks":   (float(fb_setup["reentry_distance_ticks"])
                                                if fb_setup else 0.0),
                "fb_target_ticks":             (int(fb_setup["target_ticks"])
                                                if fb_setup else 0),
                "fb_bars_from_sweep_to_entry": (int(fb_setup["bars_from_sweep_to_entry"])
                                                if fb_setup else 0),
                "combine_profit_at_entry":     combine_profit,
                "combine_target_remaining":    combine_remain,
                "daily_loss_used_pct":         dl_pct,
                "mll_used_pct":                mll_pct,
                "qualifying_days_banked":      0,
            }

        portfolio.append(cash)

    # Final day close
    if current_day is not None:
        sl = session_levels.get(current_day, {})
        dr = _build_daily_record(
            session_date    = current_day,
            day_trades      = day_trades_list,
            day_open_cash   = day_open_cash,
            day_peak_cash   = day_peak_cash,
            day_trough_cash = day_trough_cash,
            session_levels  = sl,
            cash            = cash,
        )
        daily_records.append(dr)

    if live_signal_sink is not None:
        live_signal_sink.clear()
        if in_trade and entry_snapshot:
            live_signal = _build_live_signal_snapshot(
                entry_snapshot=entry_snapshot,
                as_of=df.index[-1],
                session_date=current_day,
                direction=direction,
                entry_price=entry_price,
                contracts=contracts,
                entry_regime=entry_regime,
                entry_type=entry_type,
                target_ticks=target_ticks,
            )
            live_signal_sink.update(vars(live_signal))

    while len(portfolio) < len(df):
        portfolio.append(portfolio[-1])

    df["portfolio_value"] = portfolio
    df["buy_hold_value"]  = INIT_CASH * (df["close"] / df["close"].iloc[0])

    return df, trades, daily_records, state


# ── TRADE RECORD BUILDER ──────────────────────────────────────────────────────

def _build_trade_record(
    entry_snapshot: dict,
    date: pd.Timestamp,
    direction: str,
    entry_price: float,
    exit_price: float,
    pnl_pts: float,
    gross_pnl_usd: float,
    costs_usd: float,
    pnl_usd: float,
    pnl_pct: float,
    won: bool,
    cash: float,
    contracts: int,
    entry_regime: str,
    exit_reason: str,
    fvg_stop: float,
    trade_mae: float,
    trade_mfe: float,
    bars_to_exit: int,
    state: RiskState,
) -> TradeRecord:
    snap = entry_snapshot

    stop_w    = snap.get("stop_width_ticks", 0.0)
    risk_usd  = stop_w * MNQ_TICK_VALUE * contracts if stop_w > 0 else 1.0
    r_multiple = pnl_usd / risk_usd if risk_usd > 0 else 0.0
    edge_ratio = (trade_mfe / trade_mae) if trade_mae > 0 else trade_mfe
    mfe_usd    = trade_mfe * MNQ_POINT_VALUE * contracts
    efficiency = (pnl_usd / mfe_usd) if mfe_usd > 0 else 0.0

    return TradeRecord(
        date=date, direction=direction,
        entry=entry_price, exit=exit_price,
        pnl_pct=pnl_pct, gross_pnl_usd=gross_pnl_usd,
        costs_usd=costs_usd, pnl_usd=pnl_usd,
        won=won, position_size=cash,
        contracts=contracts, regime=entry_regime,
        exit_reason=exit_reason, fvg_stop=fvg_stop,
        entry_type             = snap.get("entry_type", "FVG"),
        entry_hour             = snap.get("entry_hour", 0),
        session_minute         = snap.get("session_minute", 0),
        day_of_week            = snap.get("day_of_week", ""),
        month                  = snap.get("month", 0),
        year                   = snap.get("year", 0),
        quarter                = snap.get("quarter", 0),
        atr_at_entry           = snap.get("atr_at_entry", 0.0),
        atr_ratio_at_entry     = snap.get("atr_ratio_at_entry", 0.0),
        adx_at_entry           = snap.get("adx_at_entry", 0.0),
        vwap_at_entry          = snap.get("vwap_at_entry", 0.0),
        price_distance_from_vwap = snap.get("price_distance_from_vwap", 0.0),
        ema_fast_at_entry      = snap.get("ema_fast_at_entry", 0.0),
        ema_slow_at_entry      = snap.get("ema_slow_at_entry", 0.0),
        ema_spread_at_entry    = snap.get("ema_spread_at_entry", 0.0),
        fvg_size_ticks         = snap.get("fvg_size_ticks", 0.0),
        fvg_age_bars           = snap.get("fvg_age_bars", 0),
        fvg_type               = snap.get("fvg_type", ""),
        fvg_quality_score      = snap.get("fvg_quality_score", 0),
        fvg_quality_flags      = snap.get("fvg_quality_flags", ""),
        orb_range_ticks        = snap.get("orb_range_ticks", 0.0),
        orb_high               = snap.get("orb_high", 0.0),
        orb_low                = snap.get("orb_low", 0.0),
        stop_price             = snap.get("stop_price", 0.0),
        target_price           = snap.get("target_price", 0.0),
        stop_width_ticks       = snap.get("stop_width_ticks", 0.0),
        trade_number_today     = snap.get("trade_number_today", 0),
        trade_number_overall   = snap.get("trade_number_overall", 0),
        consecutive_wins_before  = snap.get("consecutive_wins_before", 0),
        consecutive_losses_before= snap.get("consecutive_losses_before", 0),
        daily_pnl_at_entry     = snap.get("daily_pnl_at_entry", 0.0),
        drawdown_pct_at_entry  = snap.get("drawdown_pct_at_entry", 0.0),
        buffer_above_floor_at_entry = snap.get("buffer_above_floor_at_entry", 0.0),
        scaling_tier_at_entry  = snap.get("scaling_tier_at_entry", 2),
        prev_day_high          = snap.get("prev_day_high", 0.0),
        prev_day_low           = snap.get("prev_day_low", 0.0),
        prev_day_range         = snap.get("prev_day_range", 0.0),
        globex_high            = snap.get("globex_high", 0.0),
        globex_low             = snap.get("globex_low", 0.0),
        opened_above_prev_close= snap.get("opened_above_prev_close", False),
        opening_gap_points     = snap.get("opening_gap_points", 0.0),
        is_fomc_day            = snap.get("is_fomc_day", False),
        is_cpi_day             = snap.get("is_cpi_day", False),
        is_nfp_day             = snap.get("is_nfp_day", False),
        is_news_day            = snap.get("is_news_day", False),
        vwap_mr_distance_points = snap.get("vwap_mr_distance_points", 0.0),
        vwap_mr_rsi             = snap.get("vwap_mr_rsi", 0.0),
        vwap_mr_failed_fvg_age  = snap.get("vwap_mr_failed_fvg_age", 0),
        vwap_mr_target_ticks    = snap.get("vwap_mr_target_ticks", 0),
        od_drive_direction      = snap.get("od_drive_direction", ""),
        od_drive_size_ticks     = snap.get("od_drive_size_ticks", 0.0),
        od_drive_range_ticks    = snap.get("od_drive_range_ticks", 0.0),
        od_pullback_depth_ticks = snap.get("od_pullback_depth_ticks", 0.0),
        od_pullback_depth_pct   = snap.get("od_pullback_depth_pct", 0.0),
        od_touched_ema21        = snap.get("od_touched_ema21", False),
        od_bars_from_drive_to_entry = snap.get("od_bars_from_drive_to_entry", 0),
        od_target_ticks         = snap.get("od_target_ticks", 0),
        fb_level_type           = snap.get("fb_level_type", ""),
        fb_level_price          = snap.get("fb_level_price", 0.0),
        fb_sweep_distance_ticks = snap.get("fb_sweep_distance_ticks", 0.0),
        fb_reentry_distance_ticks = snap.get("fb_reentry_distance_ticks", 0.0),
        fb_target_ticks         = snap.get("fb_target_ticks", 0),
        fb_bars_from_sweep_to_entry = snap.get("fb_bars_from_sweep_to_entry", 0),
        mae=trade_mae, mfe=trade_mfe, bars_to_exit=bars_to_exit,
        r_multiple=r_multiple, edge_ratio=edge_ratio, efficiency_ratio=efficiency,
        combine_profit_at_entry  = snap.get("combine_profit_at_entry", 0.0),
        combine_target_remaining = snap.get("combine_target_remaining", 0.0),
        daily_loss_used_pct      = snap.get("daily_loss_used_pct", 0.0),
        mll_used_pct             = snap.get("mll_used_pct", 0.0),
        qualifying_days_banked   = snap.get("qualifying_days_banked", 0),
    )


# ── DAILY RECORD BUILDER ──────────────────────────────────────────────────────

def _build_daily_record(
    session_date: object,
    day_trades: List[TradeRecord],
    day_open_cash: float,
    day_peak_cash: float,
    day_trough_cash: float,
    session_levels: dict,
    cash: float,
) -> DailyRecord:
    import datetime
    d = session_date
    date_obj = datetime.date(d.year, d.month, d.day) if not isinstance(d, datetime.date) else d
    dow_map  = {0:"Mon",1:"Tue",2:"Wed",3:"Thu",4:"Fri",5:"Sat",6:"Sun"}
    dow_str  = dow_map.get(date_obj.weekday(), "")
    quarter  = (date_obj.month - 1) // 3 + 1

    total_net   = sum(t.pnl_usd for t in day_trades)
    total_gross = sum(t.gross_pnl_usd for t in day_trades)
    total_costs = sum(t.costs_usd for t in day_trades)
    wins        = sum(1 for t in day_trades if t.won)

    orb_trades  = [t for t in day_trades if t.entry_type == "ORB"]
    fvg_trades  = [t for t in day_trades if t.entry_type == "FVG"]
    morning     = [t for t in day_trades if t.entry_hour < 12]
    afternoon   = [t for t in day_trades if t.entry_hour >= 12]
    longs       = [t for t in day_trades if t.direction == "long"]
    shorts      = [t for t in day_trades if t.direction == "short"]
    strong      = [t for t in day_trades if t.regime == "strong"]
    explosive   = [t for t in day_trades if t.regime == "explosive"]

    first_time = ""
    last_time  = ""
    if day_trades:
        sorted_t = sorted(day_trades, key=lambda t: t.date)
        try:
            ft = sorted_t[0].date
            lt = sorted_t[-1].date
            if hasattr(ft, "astimezone"):
                ft = ft.astimezone(TIMEZONE)
                lt = lt.astimezone(TIMEZONE)
            first_time = ft.strftime("%H:%M")
            last_time  = lt.strftime("%H:%M")
        except Exception:
            pass

    date_str = str(session_date)
    is_fomc  = date_str in FOMC_DATES
    is_cpi   = date_str in CPI_DATES
    is_nfp   = date_str in NFP_DATES
    is_news  = date_str in ALL_NEWS_DATES

    net_profit_start = day_open_cash - INIT_CASH
    tier = compute_scaling_tier(net_profit_start)
    sl   = session_levels

    return DailyRecord(
        session_date              = session_date,
        day_of_week               = dow_str,
        month                     = date_obj.month,
        year                      = date_obj.year,
        quarter                   = quarter,
        daily_pnl_gross           = total_gross,
        daily_pnl_net             = total_net,
        daily_costs               = total_costs,
        trade_count               = len(day_trades),
        win_count                 = wins,
        loss_count                = len(day_trades) - wins,
        orb_trade_count           = len(orb_trades),
        fvg_trade_count           = len(fvg_trades),
        orb_pnl                   = sum(t.pnl_usd for t in orb_trades),
        fvg_pnl                   = sum(t.pnl_usd for t in fvg_trades),
        morning_trade_count       = len(morning),
        afternoon_trade_count     = len(afternoon),
        morning_pnl               = sum(t.pnl_usd for t in morning),
        afternoon_pnl             = sum(t.pnl_usd for t in afternoon),
        long_trade_count          = len(longs),
        short_trade_count         = len(shorts),
        long_pnl                  = sum(t.pnl_usd for t in longs),
        short_pnl                 = sum(t.pnl_usd for t in shorts),
        strong_regime_count       = len(strong),
        explosive_regime_count    = len(explosive),
        strong_regime_pnl         = sum(t.pnl_usd for t in strong),
        explosive_regime_pnl      = sum(t.pnl_usd for t in explosive),
        first_trade_time          = first_time,
        last_trade_time           = last_time,
        daily_high                = sl.get("daily_high", 0.0),
        daily_low                 = sl.get("daily_low", 0.0),
        daily_range_points        = sl.get("daily_range", 0.0),
        daily_range_vs_avg        = sl.get("range_vs_avg", 1.0),
        is_trend_day              = sl.get("is_trend_day", False),
        opening_gap_points        = sl.get("opening_gap_points", 0.0),
        prev_day_range            = sl.get("prev_day_range", 0.0),
        globex_range              = sl.get("globex_range", 0.0),
        max_intraday_drawdown     = day_trough_cash - day_open_cash,
        max_intraday_peak         = day_peak_cash   - day_open_cash,
        hit_daily_loss_limit      = (total_net <= BOT_DAILY_LOSS_LIMIT),
        is_qualifying_day         = (total_net >= XFA_QUALIFYING_DAY_MIN),
        is_news_day               = is_news,
        is_fomc_day               = is_fomc,
        is_cpi_day                = is_cpi,
        is_nfp_day                = is_nfp,
        contracts_tier            = tier,
    )


# ── MONTHLY RECORD BUILDER ────────────────────────────────────────────────────

def build_monthly_records(
    daily_records: List[DailyRecord],
    trades: List[TradeRecord],
) -> List[MonthlyRecord]:
    from collections import defaultdict

    monthly:       Dict[str, List[DailyRecord]]  = defaultdict(list)
    trade_monthly: Dict[str, List[TradeRecord]]  = defaultdict(list)

    for dr in daily_records:
        key = f"{dr.year}-{dr.month:02d}"
        monthly[key].append(dr)

    for t in trades:
        key = t.date.strftime("%Y-%m")
        trade_monthly[key].append(t)

    results: List[MonthlyRecord] = []
    for key in sorted(monthly.keys()):
        days         = monthly[key]
        month_trades = trade_monthly.get(key, [])
        wins_days    = sum(1 for d in days if d.daily_pnl_net > 0)
        loss_days    = sum(1 for d in days if d.daily_pnl_net <= 0)
        qual_days    = sum(1 for d in days if d.is_qualifying_day)
        net_pnls     = [d.daily_pnl_net for d in days]
        atrs  = [t.atr_at_entry for t in month_trades if t.atr_at_entry > 0]
        adxs  = [t.adx_at_entry for t in month_trades if t.adx_at_entry > 0]
        news_days = [d for d in days if d.is_news_day]
        orb_t = [t for t in month_trades if t.entry_type == "ORB"]
        fvg_t = [t for t in month_trades if t.entry_type == "FVG"]

        parts = key.split("-")
        yr, mo = int(parts[0]), int(parts[1])

        mr = MonthlyRecord(
            year_month          = key,
            year                = yr,
            month               = mo,
            total_net_pnl       = sum(d.daily_pnl_net for d in days),
            total_gross_pnl     = sum(d.daily_pnl_gross for d in days),
            total_costs         = sum(d.daily_costs for d in days),
            trade_count         = sum(d.trade_count for d in days),
            orb_trade_count     = len(orb_t),
            fvg_trade_count     = len(fvg_t),
            orb_pnl             = sum(t.pnl_usd for t in orb_t),
            fvg_pnl             = sum(t.pnl_usd for t in fvg_t),
            trading_days        = len(days),
            winning_days        = wins_days,
            losing_days         = loss_days,
            qualifying_days     = qual_days,
            win_rate            = (wins_days / len(days) * 100) if days else 0.0,
            avg_daily_pnl       = float(np.mean(net_pnls)) if net_pnls else 0.0,
            best_day_pnl        = float(max(net_pnls)) if net_pnls else 0.0,
            worst_day_pnl       = float(min(net_pnls)) if net_pnls else 0.0,
            trend_days_count    = sum(1 for d in days if d.is_trend_day),
            range_days_count    = sum(1 for d in days if not d.is_trend_day),
            avg_atr             = float(np.mean(atrs)) if atrs else 0.0,
            avg_adx             = float(np.mean(adxs)) if adxs else 0.0,
            morning_pnl         = sum(d.morning_pnl for d in days),
            afternoon_pnl       = sum(d.afternoon_pnl for d in days),
            long_pnl            = sum(d.long_pnl for d in days),
            short_pnl           = sum(d.short_pnl for d in days),
            strong_regime_pnl   = sum(d.strong_regime_pnl for d in days),
            explosive_regime_pnl= sum(d.explosive_regime_pnl for d in days),
            news_day_count      = len(news_days),
            news_day_pnl        = sum(d.daily_pnl_net for d in news_days),
        )
        results.append(mr)

    return results


# ── STATS ─────────────────────────────────────────────────────────────────────

def _compute_consistency_scorecard(
    trades: List[TradeRecord],
    daily_records: List[DailyRecord],
    avg_round_turn_cost: float = 0.0,
) -> dict:
    """Consistency-first scorecard (§1.2 of research spec).

    Returns a dict with every metric plus a 'viable' bool (cleared all minimums)
    and a 'fundable' bool (cleared all targets AND best_day_pct <= 30%).
    Designed to be called on a subset of trades (e.g. chop-only).
    """
    n = len(trades)
    if n == 0:
        return {k: None for k in (
            "n_trades", "profit_factor", "expectancy", "expectancy_in_cost_units",
            "sqn", "win_rate", "pct_profitable_days", "best_day_pct_of_net",
            "sharpe_daily", "sortino_daily", "max_consec_losses", "calmar",
            "viable", "fundable",
        )}

    pnls      = [t.pnl_usd for t in trades]
    wins      = [p for p in pnls if p > 0]
    losses    = [p for p in pnls if p <= 0]

    gross_win  = sum(wins)
    gross_loss = abs(sum(losses))
    pf         = gross_win / gross_loss if gross_loss > 0 else float("inf")

    total_net  = sum(pnls)
    expectancy = total_net / n

    cost_units = (expectancy / avg_round_turn_cost
                  if avg_round_turn_cost > 0 else 0.0)

    pnl_arr = np.array(pnls, dtype=float)
    std_pnl = float(np.std(pnl_arr))
    sqn     = (float(np.mean(pnl_arr)) / std_pnl * np.sqrt(n)) if std_pnl > 0 else 0.0

    win_rate = len(wins) / n * 100

    # Per-day metrics from daily_records that have at least one matching trade.
    trade_dates = set(t.date.date() if hasattr(t.date, "date") else t.date for t in trades)
    relevant_dr = [d for d in daily_records
                   if (d.session_date.date() if hasattr(d.session_date, "date")
                       else d.session_date) in trade_dates]
    if relevant_dr:
        prof_days = sum(1 for d in relevant_dr if d.daily_pnl_net > 0)
        pct_prof  = prof_days / len(relevant_dr) * 100
        day_pnls  = [d.daily_pnl_net for d in relevant_dr]
        best_day  = max(day_pnls)
        best_day_pct = (best_day / total_net * 100) if total_net > 0 else float("inf")
        # daily Sharpe/Sortino on subset days
        arr = np.array(day_pnls, dtype=float)
        mu  = float(arr.mean())
        sd  = float(arr.std())
        sharpe_d  = (mu / sd * np.sqrt(252)) if sd > 0 else 0.0
        down      = float(arr[arr < 0].std()) if (arr < 0).any() else 0.0
        sortino_d = (mu / down * np.sqrt(252)) if down > 0 else 0.0
    else:
        pct_prof      = 0.0
        best_day_pct  = float("inf")
        sharpe_d      = 0.0
        sortino_d     = 0.0

    # Max consecutive losses
    max_streak = streak = 0
    for p in pnls:
        if p <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    # Calmar: annualised return / max DD on these trades' equity curve
    eq = np.cumsum(pnl_arr)
    pk = np.maximum.accumulate(eq)
    dd = pk - eq
    max_dd_abs = float(dd.max()) if len(dd) > 0 else 0.0
    first_t = trades[0].date
    last_t  = trades[-1].date
    days_span = max(((last_t - first_t).days if hasattr(first_t, "__sub__") else 1), 1)
    ann_net  = total_net * 365.0 / days_span
    calmar   = ann_net / max_dd_abs if max_dd_abs > 0 else 0.0

    viable = (
        pf         >= 1.30
        and cost_units >= 2.0
        and sqn        >= 1.6
        and pct_prof   >= 50.0
        and best_day_pct <= 40.0
        and sharpe_d   >= 1.0
        and calmar     >= 0.5
    )
    fundable = viable and (
        pf         >= 1.75
        and cost_units >= 3.0
        and sqn        >= 2.5
        and pct_prof   >= 55.0
        and best_day_pct <= 30.0
        and sortino_d  >= 1.5
        and calmar     >= 1.0
    )

    return {
        "n_trades":               n,
        "profit_factor":          round(pf, 3),
        "expectancy":             round(expectancy, 2),
        "expectancy_in_cost_units": round(cost_units, 2),
        "sqn":                    round(sqn, 3),
        "win_rate":               round(win_rate, 1),
        "pct_profitable_days":    round(pct_prof, 1),
        "best_day_pct_of_net":    round(best_day_pct, 1),
        "sharpe_daily":           round(sharpe_d, 3),
        "sortino_daily":          round(sortino_d, 3),
        "max_consec_losses":      max_streak,
        "calmar":                 round(calmar, 3),
        "viable":                 viable,
        "fundable":               fundable,
    }


def compute_stats(
    df: pd.DataFrame,
    trades: List[TradeRecord],
    daily_records: List[DailyRecord],
    state: RiskState,
) -> dict:
    final      = df["portfolio_value"].iloc[-1]
    bh         = df["buy_hold_value"].iloc[-1]
    start_date = df.index[0]
    end_date   = df.index[-1]
    years      = max((end_date - start_date).days / 365.25, 1e-9)

    total_ret  = (final - INIT_CASH) / INIT_CASH * 100
    annual_ret = ((final / INIT_CASH) ** (1 / years) - 1) * 100
    bh_ret     = (bh - INIT_CASH) / INIT_CASH * 100

    roll_max   = df["portfolio_value"].cummax()
    drawdown   = (df["portfolio_value"] - roll_max) / roll_max
    max_dd     = drawdown.min() * 100
    calmar     = annual_ret / abs(max_dd) if max_dd != 0 else 0.0

    daily_ret  = df["portfolio_value"].pct_change().dropna()
    sharpe     = ((daily_ret.mean() / daily_ret.std()) * np.sqrt(252)
                  if daily_ret.std() > 0 else 0.0)
    downside   = daily_ret[daily_ret < 0].std()
    sortino    = ((daily_ret.mean() / downside) * np.sqrt(252)
                  if downside > 0 else 0.0)

    wins_usd   = [t.pnl_usd for t in trades if t.won]
    loss_usd   = [t.pnl_usd for t in trades if not t.won]
    win_rate   = len(wins_usd) / len(trades) * 100 if trades else 0.0
    avg_win    = float(np.mean(wins_usd)) if wins_usd else 0.0
    avg_loss   = float(np.mean(loss_usd)) if loss_usd else 0.0
    gross_win  = sum(wins_usd)
    gross_loss = abs(sum(loss_usd))
    pf         = gross_win / gross_loss if gross_loss > 0 else float("inf")

    total_gross = sum(t.gross_pnl_usd for t in trades)
    total_costs = sum(t.costs_usd for t in trades)
    total_net   = sum(t.pnl_usd for t in trades)
    avg_cost    = total_costs / len(trades) if trades else 0.0

    qualifying_days = sum(1 for d in daily_records if d.is_qualifying_day)
    total_days      = len(daily_records)
    avg_trades_per_day = len(trades) / total_days if total_days > 0 else 0.0

    entry_type_stats = {}
    for etype in sorted(set(t.entry_type for t in trades)):
        et = [t for t in trades if t.entry_type == etype]
        if et:
            ew = [t.pnl_usd for t in et if t.won]
            el = [t.pnl_usd for t in et if not t.won]
            entry_type_stats[etype] = {
                "count": len(et),
                "win_rate": len(ew) / len(et) * 100,
                "net_pnl": sum(t.pnl_usd for t in et),
                "avg_win": float(np.mean(ew)) if ew else 0.0,
                "avg_loss": float(np.mean(el)) if el else 0.0,
            }

    orb_stats = entry_type_stats.get("ORB", {"count": 0, "win_rate": 0.0, "net_pnl": 0.0})
    fvg_stats = entry_type_stats.get("FVG", {"count": 0, "win_rate": 0.0, "net_pnl": 0.0})

    # Full consistency scorecard across all trades
    consistency_scorecard = _compute_consistency_scorecard(trades, daily_records, avg_cost)

    regime_stats = {}
    for r in sorted(set(t.regime for t in trades)):
        rt = [t for t in trades if t.regime == r]
        if rt:
            rw = [t.pnl_usd for t in rt if t.won]
            rl = [t.pnl_usd for t in rt if not t.won]
            regime_stats[r] = {
                "count":    len(rt),
                "win_rate": len(rw) / len(rt) * 100,
                "net_pnl":  sum(t.pnl_usd for t in rt),
                "avg_win":  float(np.mean(rw)) if rw else 0.0,
                "avg_loss": float(np.mean(rl)) if rl else 0.0,
                "scorecard": _compute_consistency_scorecard(rt, daily_records, avg_cost),
            }

    return dict(
        final=final, total_ret=total_ret, annual_ret=annual_ret,
        bh_ret=bh_ret, max_dd=max_dd, calmar=calmar,
        sharpe=sharpe, sortino=sortino,
        profit_factor=pf, num_trades=len(trades),
        win_rate=win_rate, bayes_wr=state.bayesian_win_rate * 100,
        avg_win=avg_win, avg_loss=avg_loss,
        total_gross=total_gross, total_costs=total_costs,
        consistency_scorecard=consistency_scorecard,
        total_net=total_net, avg_cost=avg_cost,
        floor_breached=state.floor_breached,
        min_buffer_over_floor=state.min_buffer_over_floor,
        regime_stats=regime_stats,
        entry_type_stats=entry_type_stats,
        qualifying_days=qualifying_days,
        total_days=total_days,
        avg_trades_per_day=avg_trades_per_day,
        orb_count=orb_stats["count"], orb_wr=orb_stats["win_rate"], orb_pnl=orb_stats["net_pnl"],
        fvg_count=fvg_stats["count"], fvg_wr=fvg_stats["win_rate"], fvg_pnl=fvg_stats["net_pnl"],
    )


def print_stats(
    stats: dict,
    trades: List[TradeRecord],
    daily_records: List[DailyRecord],
    monthly_records: List[MonthlyRecord],
):
    print("\n" + "=" * 60)
    print("  TRADING BOT V29 — MNQ | MULTI-ENTRY | BACKTEST RESULTS")
    print("=" * 60)
    print(f"  {'Instrument':<30} {'MNQ (Micro NQ)':>12}")
    print(f"  {'Start Account':<30} ${INIT_CASH:>12,.2f}")
    print(f"  {'End Account Value':<30} ${stats['final']:>12,.2f}")
    print(f"  {'Total Return':<30} {stats['total_ret']:>11.2f}%")
    print(f"  {'Annualized Return':<30} {stats['annual_ret']:>11.2f}%")
    print(f"  {'Buy & Hold Return':<30} {stats['bh_ret']:>11.2f}%")
    print("-" * 60)
    print(f"  {'Max Drawdown':<30} {stats['max_dd']:>11.2f}%")
    print(f"  {'Calmar Ratio':<30} {stats['calmar']:>12.2f}")
    print(f"  {'Sharpe Ratio':<30} {stats['sharpe']:>12.2f}")
    print(f"  {'Sortino Ratio':<30} {stats['sortino']:>12.2f}")
    print(f"  {'Profit Factor':<30} {stats['profit_factor']:>12.2f}")
    print(f"  {'Floor Breached':<30} {'YES ⚠️' if stats['floor_breached'] else 'NO ✅':>12}")
    print(f"  {'Min Buffer Over Floor':<30} ${stats['min_buffer_over_floor']:>11,.2f}")
    print("-" * 60)
    print(f"  {'Total Trades':<30} {stats['num_trades']:>12}")
    print(f"  {'Avg Trades/Day':<30} {stats['avg_trades_per_day']:>12.2f}")
    print(f"  {'Win Rate':<30} {stats['win_rate']:>11.2f}%")
    print(f"  {'Bayesian Win Rate':<30} {stats['bayes_wr']:>11.2f}%")
    print(f"  {'Avg Win (net)':<30} ${stats['avg_win']:>11.2f}")
    print(f"  {'Avg Loss (net)':<30} ${stats['avg_loss']:>11.2f}")
    print("-" * 60)
    print(f"  {'Gross Trading P&L':<30} ${stats['total_gross']:>12,.2f}")
    print(f"  {'Total Costs':<30} ${stats['total_costs']:>12,.2f}")
    print(f"  {'Net Trading P&L':<30} ${stats['total_net']:>12,.2f}")
    print(f"  {'Avg Cost Per Trade':<30} ${stats['avg_cost']:>12.2f}")
    print("=" * 60)

    print("\n  ENTRY MECHANISM BREAKDOWN")
    print("=" * 60)
    if stats.get("entry_type_stats"):
        for etype, es in stats["entry_type_stats"].items():
            label = etype.replace("_", " ")
            print(f"  {label + ' trades':<30} {es['count']:>12}")
            print(f"  {label + ' win rate':<30} {es['win_rate']:>11.2f}%")
            print(f"  {label + ' total P&L':<30} ${es['net_pnl']:>12,.2f}")
    else:
        print(f"  {'ORB trades':<30} {stats['orb_count']:>12}")
        print(f"  {'ORB win rate':<30} {stats['orb_wr']:>11.2f}%")
        print(f"  {'ORB total P&L':<30} ${stats['orb_pnl']:>12,.2f}")
        print(f"  {'FVG trades':<30} {stats['fvg_count']:>12}")
        print(f"  {'FVG win rate':<30} {stats['fvg_wr']:>11.2f}%")
        print(f"  {'FVG total P&L':<30} ${stats['fvg_pnl']:>12,.2f}")
    print("=" * 60)

    print("\n  TOPSTEP XFA QUALIFICATION SUMMARY")
    print("=" * 60)
    total_days = stats["total_days"]
    qual_days  = stats["qualifying_days"]
    qual_rate  = qual_days / total_days * 100 if total_days > 0 else 0.0
    print(f"  {'Total Trading Days':<30} {total_days:>12}")
    print(f"  {'Qualifying Days ($150+)':<30} {qual_days:>12}")
    print(f"  {'Qualification Rate':<30} {qual_rate:>11.1f}%")
    cycles = qual_days // XFA_QUALIFYING_DAYS_NEEDED
    print(f"  {'Theoretical Payout Cycles':<30} {cycles:>12}")
    print(f"  {'Theoretical Max Payout':<30} ${cycles * XFA_PAYOUT_CAP:>12,.2f}")
    print("=" * 60)

    if stats["regime_stats"]:
        print("\n  REGIME BREAKDOWN")
        print("=" * 60)
        for regime, rs in stats["regime_stats"].items():
            print(f"  {regime.upper()}")
            print(f"    Trades:    {rs['count']}")
            print(f"    Win Rate:  {rs['win_rate']:.1f}%")
            print(f"    Net P&L:   ${rs['net_pnl']:,.2f}")
            print(f"    Avg Win:   ${rs['avg_win']:.2f}")
            print(f"    Avg Loss:  ${rs['avg_loss']:.2f}")
        print("=" * 60)

    if monthly_records:
        print("\n  MONTHLY P&L BREAKDOWN")
        print("=" * 60)
        print(f"  {'Month':<12} {'Net P&L':>10}  {'Qual Days':>9}  {'ORB P&L':>8}  Status")
        print("-" * 60)
        up_months = 0
        down_months = 0
        for mr in monthly_records:
            status  = "OK" if mr.total_net_pnl >= 0 else "DOWN"
            target  = " *" if mr.total_net_pnl >= 2_000 else ""
            q_mark  = f"Q:{mr.qualifying_days}" if mr.qualifying_days > 0 else "     "
            orb_str = f"${mr.orb_pnl:,.0f}" if mr.orb_trade_count > 0 else "   -"
            print(f"  {mr.year_month:<12} ${mr.total_net_pnl:>9,.2f}  {q_mark:>9}  {orb_str:>8}  {status}{target}")
            if mr.total_net_pnl >= 0: up_months += 1
            else: down_months += 1
        total_months = len(monthly_records)
        avg_monthly  = sum(mr.total_net_pnl for mr in monthly_records) / total_months
        best_mr      = max(monthly_records, key=lambda m: m.total_net_pnl)
        worst_mr     = min(monthly_records, key=lambda m: m.total_net_pnl)
        print("-" * 60)
        print(f"  {'Up months':<30} {up_months:>6} / {total_months}")
        print(f"  {'Down months':<30} {down_months:>6} / {total_months}")
        print(f"  {'Avg monthly P&L':<30} ${avg_monthly:>11,.2f}")
        print(f"  {'Best month':<30} {best_mr.year_month:>12}  ${best_mr.total_net_pnl:,.2f}")
        print(f"  {'Worst month':<30} {worst_mr.year_month:>12}  ${worst_mr.total_net_pnl:,.2f}")
        months_2k = sum(1 for mr in monthly_records if mr.total_net_pnl >= 2_000)
        months_3k = sum(1 for mr in monthly_records if mr.total_net_pnl >= 3_000)
        print(f"  {'Months >= $2K':<30} {months_2k:>6} / {total_months}")
        print(f"  {'Months >= $3K':<30} {months_3k:>6} / {total_months}")
        print("=" * 60)

    if daily_records:
        daily_pnls  = [d.daily_pnl_net for d in daily_records]
        breach_days = [d for d in daily_records if d.hit_daily_loss_limit]
        print("\n  DAILY P&L ANALYSIS")
        print("=" * 60)
        print(f"  {'Total trading days':<30} {len(daily_records):>12}")
        print(f"  {'Best day':<30} ${max(daily_pnls):>12,.2f}")
        print(f"  {'Worst day':<30} ${min(daily_pnls):>12,.2f}")
        print(f"  {'Avg day':<30} ${np.mean(daily_pnls):>12,.2f}")
        print(f"  {'Qualifying days ($150+)':<30} {stats['qualifying_days']:>12}")
        print(f"  {'Days breaching daily limit':<30} {len(breach_days):>12}")
        print("=" * 60)


# ── MONTE CARLO ───────────────────────────────────────────────────────────────

def monte_carlo(stats: dict) -> dict:
    win_rate   = stats["win_rate"] / 100
    avg_win    = stats["avg_win"]
    avg_loss   = abs(stats["avg_loss"])
    ruin_floor = INIT_CASH - EOD_LOSS_BUFFER

    final_values  = []
    max_drawdowns = []
    ruin_count    = 0

    for _ in range(MC_SIMULATIONS):
        equity = float(INIT_CASH)
        peak   = float(INIT_CASH)
        max_dd = 0.0
        ruined = False
        for _ in range(MC_TRADE_COUNT):
            won     = np.random.random() < win_rate
            equity += avg_win if won else -avg_loss
            peak    = max(peak, equity)
            dd      = (equity - peak) / peak
            max_dd  = min(max_dd, dd)
            if equity <= ruin_floor:
                ruined = True
                break
        final_values.append(equity)
        max_drawdowns.append(max_dd * 100)
        if ruined:
            ruin_count += 1

    final_arr = np.array(final_values)
    dd_arr    = np.array(max_drawdowns)

    results = dict(
        median_return = (np.median(final_arr) / INIT_CASH - 1) * 100,
        p10_return    = (np.percentile(final_arr, 10) / INIT_CASH - 1) * 100,
        p90_return    = (np.percentile(final_arr, 90) / INIT_CASH - 1) * 100,
        median_max_dd = np.median(dd_arr),
        worst_dd      = np.min(dd_arr),
        ruin_prob     = ruin_count / MC_SIMULATIONS * 100,
        final_arr     = final_arr,
        dd_arr        = dd_arr,
    )

    print("\n" + "-" * 45)
    print(f"  MONTE CARLO — {MC_TRADE_COUNT} trades, {MC_SIMULATIONS:,} runs")
    print("-" * 45)
    print(f"  {'Median Return':<28} {results['median_return']:>8.2f}%")
    print(f"  {'10th Pct Return':<28} {results['p10_return']:>8.2f}%")
    print(f"  {'90th Pct Return':<28} {results['p90_return']:>8.2f}%")
    print(f"  {'Median Max Drawdown':<28} {results['median_max_dd']:>8.2f}%")
    print(f"  {'Worst Case Drawdown':<28} {results['worst_dd']:>8.2f}%")
    print(f"  {'Prop Firm Ruin Prob':<28} {results['ruin_prob']:>8.2f}%")
    print("-" * 45)

    return results


# ── EXPORT ────────────────────────────────────────────────────────────────────

def print_vwap_mr_slice(trades: List[TradeRecord], limit: int = VWAP_MR_DEBUG_PRINT_LIMIT) -> None:
    vwap_trades = [t for t in trades if t.entry_type == "VWAP_MR"]

    print("\n  VWAP MR DIAGNOSTIC")
    print("=" * 60)
    if not vwap_trades:
        print("  No VWAP MR trades in this run.")
        print("=" * 60)
        return

    print(f"  {'VWAP MR trades':<24} {len(vwap_trades):>8}")
    print(f"  {'Win rate':<24} {np.mean([t.won for t in vwap_trades]) * 100:>7.2f}%")
    print(f"  {'Net P&L':<24} ${sum(t.pnl_usd for t in vwap_trades):>11,.2f}")
    print(f"  {'Avg P&L':<24} ${np.mean([t.pnl_usd for t in vwap_trades]):>11,.2f}")
    print(f"  {'Avg distance from VWAP':<24} {np.mean([t.vwap_mr_distance_points for t in vwap_trades]):>8.2f} pts")
    print(f"  {'Avg RSI':<24} {np.mean([t.vwap_mr_rsi for t in vwap_trades]):>8.2f}")
    print(f"  {'Avg failed FVG age':<24} {np.mean([t.vwap_mr_failed_fvg_age for t in vwap_trades]):>8.2f} bars")
    print("-" * 60)
    print("  By hour:")
    hour_groups = {}
    for t in vwap_trades:
        hour_groups.setdefault(t.entry_hour, []).append(t)
    for hour in sorted(hour_groups):
        grp = hour_groups[hour]
        print(
            f"    {hour:02d}:00  trades={len(grp):>3}  "
            f"wr={np.mean([x.won for x in grp]) * 100:>6.2f}%  "
            f"pnl=${sum(x.pnl_usd for x in grp):>8,.2f}"
        )
    print("-" * 60)
    print("  Sample trades:")
    print("    date               side   hr   dist   rsi  age  tgt   entry     exit     pnl     reason")
    for t in vwap_trades[:limit]:
        print(
            f"    {str(t.date)[:16]:<16} "
            f"{t.direction:<5} "
            f"{t.entry_hour:>2} "
            f"{t.vwap_mr_distance_points:>6.2f} "
            f"{t.vwap_mr_rsi:>5.1f} "
            f"{t.vwap_mr_failed_fvg_age:>4} "
            f"{t.vwap_mr_target_ticks:>4} "
            f"{t.entry:>8.2f} "
            f"{t.exit:>8.2f} "
            f"{t.pnl_usd:>8.2f} "
            f"{t.exit_reason}"
        )
    if len(vwap_trades) > limit:
        print(f"  ... showing first {limit} of {len(vwap_trades)} VWAP MR trades")
    print("=" * 60)


def print_od_pullback_slice(trades: List[TradeRecord], limit: int = OD_DEBUG_PRINT_LIMIT) -> None:
    od_trades = [t for t in trades if t.entry_type == "OD_PULLBACK"]

    print("\n  OD PULLBACK DIAGNOSTIC")
    print("=" * 60)
    if not od_trades:
        print("  No OD pullback trades in this run.")
        print("=" * 60)
        return

    print(f"  {'OD trades':<24} {len(od_trades):>8}")
    print(f"  {'Win rate':<24} {np.mean([t.won for t in od_trades]) * 100:>7.2f}%")
    print(f"  {'Net P&L':<24} ${sum(t.pnl_usd for t in od_trades):>11,.2f}")
    print(f"  {'Avg P&L':<24} ${np.mean([t.pnl_usd for t in od_trades]):>11,.2f}")
    print(f"  {'Avg drive size':<24} {np.mean([t.od_drive_size_ticks for t in od_trades]):>8.2f} ticks")
    print(f"  {'Avg pullback depth':<24} {np.mean([t.od_pullback_depth_pct for t in od_trades]) * 100:>8.2f}%")
    print("-" * 60)
    print("  By side:")
    side_groups = {}
    for t in od_trades:
        side_groups.setdefault(t.direction, []).append(t)
    for side in sorted(side_groups):
        grp = side_groups[side]
        print(
            f"    {side:<5} trades={len(grp):>3}  "
            f"wr={np.mean([x.won for x in grp]) * 100:>6.2f}%  "
            f"pnl=${sum(x.pnl_usd for x in grp):>8,.2f}"
        )
    print("-" * 60)
    print("  Sample trades:")
    print("    date               side   drive  pb%   bars  stop  tgt   entry     exit     pnl     reason")
    for t in od_trades[:limit]:
        print(
            f"    {str(t.date)[:16]:<16} "
            f"{t.direction:<5} "
            f"{t.od_drive_size_ticks:>5.1f} "
            f"{t.od_pullback_depth_pct * 100:>5.1f} "
            f"{t.od_bars_from_drive_to_entry:>5} "
            f"{t.stop_width_ticks:>5.1f} "
            f"{t.od_target_ticks:>4} "
            f"{t.entry:>8.2f} "
            f"{t.exit:>8.2f} "
            f"{t.pnl_usd:>8.2f} "
            f"{t.exit_reason}"
        )
    if len(od_trades) > limit:
        print(f"  ... showing first {limit} of {len(od_trades)} OD trades")
    print("=" * 60)


def print_failed_breakout_slice(trades: List[TradeRecord], limit: int = FB_DEBUG_PRINT_LIMIT) -> None:
    fb_trades = [t for t in trades if t.entry_type == "FAILED_BREAKOUT"]

    print("\n  FAILED BREAKOUT DIAGNOSTIC")
    print("=" * 60)
    if not fb_trades:
        print("  No failed-breakout trades in this run.")
        print("=" * 60)
        return

    print(f"  {'FB trades':<24} {len(fb_trades):>8}")
    print(f"  {'Win rate':<24} {np.mean([t.won for t in fb_trades]) * 100:>7.2f}%")
    print(f"  {'Net P&L':<24} ${sum(t.pnl_usd for t in fb_trades):>11,.2f}")
    print(f"  {'Avg P&L':<24} ${np.mean([t.pnl_usd for t in fb_trades]):>11,.2f}")
    print(f"  {'Avg sweep distance':<24} {np.mean([t.fb_sweep_distance_ticks for t in fb_trades]):>8.2f} ticks")
    print(f"  {'Avg reentry distance':<24} {np.mean([t.fb_reentry_distance_ticks for t in fb_trades]):>8.2f} ticks")
    print("-" * 60)
    print("  By level:")
    level_groups = {}
    for t in fb_trades:
        level_groups.setdefault(t.fb_level_type, []).append(t)
    for level in sorted(level_groups):
        grp = level_groups[level]
        print(
            f"    {level:<14} trades={len(grp):>3}  "
            f"wr={np.mean([x.won for x in grp]) * 100:>6.2f}%  "
            f"pnl=${sum(x.pnl_usd for x in grp):>8,.2f}"
        )
    print("-" * 60)
    print("  Sample trades:")
    print("    date               side   level           sweep  reent  stop  tgt   entry     exit     pnl     reason")
    for t in fb_trades[:limit]:
        print(
            f"    {str(t.date)[:16]:<16} "
            f"{t.direction:<5} "
            f"{t.fb_level_type:<14} "
            f"{t.fb_sweep_distance_ticks:>5.1f} "
            f"{t.fb_reentry_distance_ticks:>6.1f} "
            f"{t.stop_width_ticks:>5.1f} "
            f"{t.fb_target_ticks:>4} "
            f"{t.entry:>8.2f} "
            f"{t.exit:>8.2f} "
            f"{t.pnl_usd:>8.2f} "
            f"{t.exit_reason}"
        )
    if len(fb_trades) > limit:
        print(f"  ... showing first {limit} of {len(fb_trades)} failed-breakout trades")
    print("=" * 60)


def print_failed_breakout_cap_diagnostic(state: RiskState, limit: int = 15) -> None:
    skips = state.fb_cap_skips

    print("\n  FAILED BREAKOUT CAP DIAGNOSTIC")
    print("=" * 60)
    if not skips:
        print("  No valid failed-breakout setups were blocked by the daily cap.")
        print("=" * 60)
        return

    print(f"  {'Blocked valid setups':<24} {len(skips):>8}")
    print("-" * 60)

    print("  By level:")
    level_counts = {}
    for s in skips:
        level_counts[s["level_type"]] = level_counts.get(s["level_type"], 0) + 1
    for level, count in sorted(level_counts.items(), key=lambda x: (-x[1], x[0])):
        print(f"    {level:<14} skips={count:>3}")

    print("  By hour:")
    hour_counts = {}
    for s in skips:
        hour_counts[s["entry_hour"]] = hour_counts.get(s["entry_hour"], 0) + 1
    for hour in sorted(hour_counts):
        print(f"    {hour:02d}:00          skips={hour_counts[hour]:>3}")

    print("-" * 60)
    print("  Sample skipped setups:")
    print("    date               side   level           sweep  reent")
    for s in skips[:limit]:
        print(
            f"    {str(s['date'])[:16]:<16} "
            f"{s['direction']:<5} "
            f"{s['level_type']:<14} "
            f"{s['sweep_distance_ticks']:>5.1f} "
            f"{s['reentry_distance_ticks']:>6.1f}"
        )
    if len(skips) > limit:
        print(f"  ... showing first {limit} of {len(skips)} blocked setups")
    print("=" * 60)


def _prepare_trade_df(trades: List[TradeRecord]) -> pd.DataFrame:
    td = pd.DataFrame([vars(t) for t in trades]).copy()
    if td.empty:
        return td

    month_names = {
        1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr",
        5: "May", 6: "Jun", 7: "Jul", 8: "Aug",
        9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
    }
    td["month_name"] = td["month"].map(month_names).fillna(td["month"].astype(str))
    td["abs_price_distance_from_vwap"] = td["price_distance_from_vwap"].abs()
    td["abs_vwap_mr_distance_points"] = td["vwap_mr_distance_points"].abs()
    td["abs_fb_sweep_distance_ticks"] = td["fb_sweep_distance_ticks"].abs()
    td["abs_fb_reentry_distance_ticks"] = td["fb_reentry_distance_ticks"].abs()
    td["session_bucket"] = pd.cut(
        td["session_minute"],
        bins=[-1, 29, 59, 89, 119, 149, 999],
        labels=[
            "09:30-09:59",
            "10:00-10:29",
            "10:30-10:59",
            "11:00-11:29",
            "11:30-11:59",
            "12:00+",
        ],
        include_lowest=True,
        right=True,
    )
    return td


def _build_group_stats(td: pd.DataFrame, group_cols: List[str]) -> pd.DataFrame:
    if td.empty:
        return pd.DataFrame()

    rows = []
    for keys, sub in td.groupby(group_cols, dropna=False):
        if not isinstance(keys, tuple):
            keys = (keys,)

        wins = sub[sub["pnl_usd"] > 0]["pnl_usd"]
        losses = sub[sub["pnl_usd"] < 0]["pnl_usd"]
        gross_profit = float(wins.sum()) if len(wins) else 0.0
        gross_loss = abs(float(losses.sum())) if len(losses) else 0.0
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)

        row = {col: key for col, key in zip(group_cols, keys)}
        row.update({
            "trades": len(sub),
            "wins": int(sub["won"].sum()),
            "win_rate": round(float(sub["won"].mean() * 100), 2),
            "total_pnl": round(float(sub["pnl_usd"].sum()), 2),
            "avg_pnl": round(float(sub["pnl_usd"].mean()), 2),
            "avg_win": round(float(wins.mean()), 2) if len(wins) else 0.0,
            "avg_loss": round(float(losses.mean()), 2) if len(losses) else 0.0,
            "profit_factor": round(float(profit_factor), 2),
            "avg_r_multiple": round(float(sub["r_multiple"].mean()), 3),
            "avg_mfe": round(float(sub["mfe"].mean()), 2),
            "avg_mae": round(float(sub["mae"].mean()), 2),
            "avg_bars_to_exit": round(float(sub["bars_to_exit"].mean()), 2),
        })
        rows.append(row)

    return pd.DataFrame(rows)


def _build_bucket_stats(
    td: pd.DataFrame,
    value_col: str,
    bins: List[float],
    labels: List[str],
    group_cols: List[str] | None = None,
    filter_mask=None,
    bucket_name: str = "bucket",
) -> pd.DataFrame:
    if td.empty or value_col not in td.columns:
        return pd.DataFrame()

    sub = td.copy()
    if filter_mask is not None:
        sub = sub[filter_mask].copy()
    sub = sub[sub[value_col].notna()].copy()
    if sub.empty:
        return pd.DataFrame()

    sub[bucket_name] = pd.cut(
        sub[value_col],
        bins=bins,
        labels=labels,
        include_lowest=True,
        right=False,
    )
    sub = sub[sub[bucket_name].notna()].copy()
    if sub.empty:
        return pd.DataFrame()

    groups = list(group_cols or [])
    groups.append(bucket_name)
    return _build_group_stats(sub, groups)


def _print_segment_lines(title: str, seg_df: pd.DataFrame, label_cols: List[str]) -> None:
    print(f"  {title}")
    if seg_df.empty:
        print("    none")
        return

    for _, row in seg_df.iterrows():
        label = " | ".join(str(row[col]) for col in label_cols)
        print(
            f"    {label:<18} trades={int(row['trades']):>4}  "
            f"wr={row['win_rate']:>6.2f}%  pnl=${row['total_pnl']:>9,.2f}  "
            f"avg=${row['avg_pnl']:>7.2f}"
        )


def print_analysis_highlights(trades: List[TradeRecord]) -> None:
    td = _prepare_trade_df(trades)
    if td.empty:
        return

    overall_hour = _build_group_stats(td, ["entry_hour"])
    overall_hour = overall_hour[overall_hour["trades"] >= ANALYSIS_HIGHLIGHT_MIN_TRADES]
    overall_hour = overall_hour.sort_values("avg_pnl", ascending=False)

    fvg_hour = _build_group_stats(td[td["entry_type"] == "FVG"], ["entry_hour"])
    fvg_hour = fvg_hour[fvg_hour["trades"] >= ANALYSIS_HIGHLIGHT_MIN_TRADES]
    fvg_hour = fvg_hour.sort_values("avg_pnl", ascending=False)

    dow_df = _build_group_stats(td, ["day_of_week"])
    dow_df = dow_df[dow_df["trades"] >= ANALYSIS_HIGHLIGHT_MIN_TRADES]
    dow_df = dow_df.sort_values("avg_pnl", ascending=False)

    print("\n  ANALYSIS HIGHLIGHTS")
    print("=" * 60)
    _print_segment_lines("Best overall hours", overall_hour.head(3), ["entry_hour"])
    _print_segment_lines("Worst overall hours", overall_hour.tail(3).sort_values("avg_pnl"), ["entry_hour"])
    _print_segment_lines("Best FVG hours", fvg_hour.head(3), ["entry_hour"])
    _print_segment_lines("Worst FVG hours", fvg_hour.tail(3).sort_values("avg_pnl"), ["entry_hour"])
    _print_segment_lines("Best days", dow_df.head(3), ["day_of_week"])
    _print_segment_lines("Worst days", dow_df.tail(3).sort_values("avg_pnl"), ["day_of_week"])
    print("=" * 60)


def print_fvg_quality_score_diagnostic(trades: List[TradeRecord]) -> None:
    td = _prepare_trade_df(trades)
    fvg = td[td["entry_type"] == "FVG"].copy() if not td.empty else pd.DataFrame()

    print("\n  FVG QUALITY SCORE DIAGNOSTIC")
    print("=" * 60)
    if fvg.empty or "fvg_quality_score" not in fvg.columns:
        print("  No FVG trades in this run.")
        print("=" * 60)
        return

    print(f"  Score model                 {'ON' if FVG_QUALITY_SCORE_ENABLED else 'OFF'}")
    score_df = _build_group_stats(fvg, ["fvg_quality_score"]).sort_values("fvg_quality_score")
    _print_segment_lines("By score", score_df, ["fvg_quality_score"])
    print("=" * 60)


def export_all_csvs(
    trades: List[TradeRecord],
    daily_records: List[DailyRecord],
    monthly_records: List[MonthlyRecord],
) -> None:
    os.makedirs(EXPORT_DIR, exist_ok=True)
    os.makedirs(EXPORT_TABLES_DIR, exist_ok=True)

    td = _prepare_trade_df(trades) if trades else pd.DataFrame()

    if trades:
        td.to_csv(
            os.path.join(EXPORT_DIR, "v29_trades_full.csv"), index=False)
        print(f"  Exported: v29_trades_full.csv ({len(trades)} rows)")

    if daily_records:
        pd.DataFrame([vars(d) for d in daily_records]).to_csv(
            os.path.join(EXPORT_DIR, "v29_daily_summary.csv"), index=False)
        print(f"  Exported: v29_daily_summary.csv ({len(daily_records)} rows)")

    if monthly_records:
        pd.DataFrame([vars(m) for m in monthly_records]).to_csv(
            os.path.join(EXPORT_DIR, "v29_monthly_summary.csv"), index=False)
        print(f"  Exported: v29_monthly_summary.csv ({len(monthly_records)} rows)")

    # Entry mechanism breakdown table
    if trades:
        rows = []
        entry_types = []
        for etype in ["ORB", "FAILED_BREAKOUT", "OD_PULLBACK", "FVG", "VWAP_MR"]:
            if etype not in entry_types:
                entry_types.append(etype)
        for etype in sorted(td["entry_type"].dropna().unique()):
            if etype not in entry_types:
                entry_types.append(etype)

        for etype in entry_types:
            sub = td[td["entry_type"] == etype]
            if len(sub) == 0:
                rows.append({
                    "entry_type": etype, "trades": 0, "wins": 0,
                    "win_rate": 0.0, "total_pnl": 0.0,
                    "avg_pnl": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
                })
            else:
                rows.append({
                    "entry_type": etype,
                    "trades":     len(sub),
                    "wins":       int(sub["won"].sum()),
                    "win_rate":   round(sub["won"].mean() * 100, 2),
                    "total_pnl":  round(sub["pnl_usd"].sum(), 2),
                    "avg_pnl":    round(sub["pnl_usd"].mean(), 2),
                    "avg_win":    round(sub[sub["pnl_usd"] > 0]["pnl_usd"].mean(), 2) if sub["won"].any() else 0.0,
                    "avg_loss":   round(sub[sub["pnl_usd"] < 0]["pnl_usd"].mean(), 2) if (~sub["won"]).any() else 0.0,
                })
        pd.DataFrame(rows).to_csv(
            os.path.join(EXPORT_TABLES_DIR, "v29_orb_vs_fvg.csv"), index=False)
        print(f"  Exported: analysis_tables/v29_orb_vs_fvg.csv")

        analysis_exports = {
            "v29_by_hour.csv": _build_group_stats(td, ["entry_hour"]).sort_values("entry_hour"),
            "v29_by_day_of_week.csv": _build_group_stats(td, ["day_of_week"]),
            "v29_by_month.csv": _build_group_stats(td, ["month", "month_name"]).sort_values("month"),
            "v29_by_entry_type_hour.csv": _build_group_stats(td, ["entry_type", "entry_hour"]).sort_values(["entry_type", "entry_hour"]),
            "v29_by_entry_type_side.csv": _build_group_stats(td, ["entry_type", "direction"]).sort_values(["entry_type", "direction"]),
            "v29_by_exit_reason.csv": _build_group_stats(td, ["entry_type", "exit_reason"]).sort_values(["entry_type", "exit_reason"]),
            "v29_by_session_bucket.csv": _build_group_stats(td, ["entry_type", "session_bucket"]).sort_values(["entry_type", "session_bucket"]),
            "v29_adx_bins.csv": _build_bucket_stats(
                td,
                value_col="adx_at_entry",
                bins=[0, 10, 20, 30, 40, 60, 1_000],
                labels=["00-10", "10-20", "20-30", "30-40", "40-60", "60+"],
                group_cols=["entry_type"],
                bucket_name="adx_bucket",
            ),
            "v29_atr_ratio_bins.csv": _build_bucket_stats(
                td,
                value_col="atr_ratio_at_entry",
                bins=[0, 1.0, 1.1, 1.25, 1.5, 2.0, 10.0],
                labels=["<1.0", "1.0-1.1", "1.1-1.25", "1.25-1.5", "1.5-2.0", "2.0+"],
                group_cols=["entry_type"],
                bucket_name="atr_ratio_bucket",
            ),
            "v29_fvg_age_bins.csv": _build_bucket_stats(
                td,
                value_col="fvg_age_bars",
                bins=[0, 1, 2, 4, 7, 11, 21, 10_000],
                labels=["0", "1", "2-3", "4-6", "7-10", "11-20", "21+"],
                filter_mask=(td["entry_type"] == "FVG"),
                bucket_name="fvg_age_bucket",
            ),
            "v29_fvg_size_bins.csv": _build_bucket_stats(
                td,
                value_col="fvg_size_ticks",
                bins=[0, 4, 8, 12, 20, 40, 10_000],
                labels=["0-3", "4-7", "8-11", "12-19", "20-39", "40+"],
                filter_mask=(td["entry_type"] == "FVG"),
                bucket_name="fvg_size_bucket",
            ),
            "v29_fvg_quality_score_bins.csv": _build_bucket_stats(
                td,
                value_col="fvg_quality_score",
                bins=[0, 1, 2, 3, 4, 5, 6],
                labels=["0", "1", "2", "3", "4", "5"],
                filter_mask=(td["entry_type"] == "FVG"),
                bucket_name="fvg_quality_score_bucket",
            ),
            "v29_vwap_distance_bins.csv": _build_bucket_stats(
                td,
                value_col="abs_price_distance_from_vwap",
                bins=[0, 8, 16, 24, 40, 80, 10_000],
                labels=["0-7", "8-15", "16-23", "24-39", "40-79", "80+"],
                group_cols=["entry_type"],
                bucket_name="vwap_distance_bucket",
            ),
            "v29_vwap_mr_distance_bins.csv": _build_bucket_stats(
                td,
                value_col="abs_vwap_mr_distance_points",
                bins=[0, 12, 16, 20, 24, 32, 10_000],
                labels=["0-11", "12-15", "16-19", "20-23", "24-31", "32+"],
                filter_mask=(td["entry_type"] == "VWAP_MR"),
                bucket_name="vwap_mr_distance_bucket",
            ),
            "v29_vwap_mr_rsi_bins.csv": _build_bucket_stats(
                td,
                value_col="vwap_mr_rsi",
                bins=[0, 55, 60, 65, 70, 100],
                labels=["<55", "55-59", "60-64", "65-69", "70+"],
                filter_mask=(td["entry_type"] == "VWAP_MR"),
                bucket_name="vwap_mr_rsi_bucket",
            ),
            "v29_vwap_mr_failed_fvg_age_bins.csv": _build_bucket_stats(
                td,
                value_col="vwap_mr_failed_fvg_age",
                bins=[0, 1, 3, 5, 8, 100],
                labels=["0", "1-2", "3-4", "5-7", "8+"],
                filter_mask=(td["entry_type"] == "VWAP_MR"),
                bucket_name="vwap_mr_failed_fvg_age_bucket",
            ),
            "v29_od_drive_size_bins.csv": _build_bucket_stats(
                td,
                value_col="od_drive_size_ticks",
                bins=[0, 16, 24, 32, 48, 10_000],
                labels=["0-15", "16-23", "24-31", "32-47", "48+"],
                filter_mask=(td["entry_type"] == "OD_PULLBACK"),
                bucket_name="od_drive_size_bucket",
            ),
            "v29_od_pullback_depth_pct_bins.csv": _build_bucket_stats(
                td,
                value_col="od_pullback_depth_pct",
                bins=[0, 0.25, 0.40, 0.50, 0.60, 10.0],
                labels=["<25%", "25-39%", "40-49%", "50-59%", "60%+"],
                filter_mask=(td["entry_type"] == "OD_PULLBACK"),
                bucket_name="od_pullback_depth_pct_bucket",
            ),
            "v29_od_bars_from_drive_bins.csv": _build_bucket_stats(
                td,
                value_col="od_bars_from_drive_to_entry",
                bins=[0, 2, 4, 6, 20],
                labels=["0-1", "2-3", "4-5", "6+"],
                filter_mask=(td["entry_type"] == "OD_PULLBACK"),
                bucket_name="od_bars_from_drive_bucket",
            ),
            "v29_fb_level_type.csv": _build_group_stats(
                td[td["entry_type"] == "FAILED_BREAKOUT"],
                ["fb_level_type", "direction"],
            ).sort_values(["fb_level_type", "direction"]),
            "v29_fb_sweep_distance_bins.csv": _build_bucket_stats(
                td,
                value_col="abs_fb_sweep_distance_ticks",
                bins=[0, 2, 4, 8, 12, 24, 10_000],
                labels=["0-1", "2-3", "4-7", "8-11", "12-23", "24+"],
                filter_mask=(td["entry_type"] == "FAILED_BREAKOUT"),
                bucket_name="fb_sweep_distance_bucket",
            ),
            "v29_fb_reentry_distance_bins.csv": _build_bucket_stats(
                td,
                value_col="abs_fb_reentry_distance_ticks",
                bins=[0, 2, 4, 8, 12, 24, 10_000],
                labels=["0-1", "2-3", "4-7", "8-11", "12-23", "24+"],
                filter_mask=(td["entry_type"] == "FAILED_BREAKOUT"),
                bucket_name="fb_reentry_distance_bucket",
            ),
        }

        for filename, df_out in analysis_exports.items():
            if df_out is None or df_out.empty:
                continue
            df_out.to_csv(os.path.join(EXPORT_TABLES_DIR, filename), index=False)
            print(f"  Exported: analysis_tables/{filename}")

    print(f"\n  All V29 exports saved to: {EXPORT_DIR}")


# ── MAIN ──────────────────────────────────────────────────────────────────────

def _parse_cli_args():
    parser = argparse.ArgumentParser(description="Run the V29 MNQ backtest and analytics pipeline.")
    parser.add_argument(
        "--slippage-ticks",
        type=float,
        default=None,
        help="Override the default slippage assumption (ticks per side) for stress testing.",
    )
    return parser.parse_args()


def _apply_cli_overrides(args) -> None:
    global SLIPPAGE_TICKS
    if getattr(args, "slippage_ticks", None) is not None:
        SLIPPAGE_TICKS = float(args.slippage_ticks)


def main(args=None):
    if args is None:
        args = _parse_cli_args()
    _apply_cli_overrides(args)
    print("=" * 60)
    print("  TRADING BOT V29 — MNQ | MULTI-ENTRY")
    print(f"  Run mode: {RUN_MODE}  |  Profile: {EXECUTION_PROFILE}  |  Live execution: {'ON' if LIVE_EXECUTION_ENABLED else 'OFF'}")
    print(f"  Instrument: MNQ  |  Point value: ${MNQ_POINT_VALUE}/pt")
    print(f"  ORB: {ORB_RANGE_BARS} bars (9:30-10:00 CT) | FVG: all session")
    print(f"  ATR cap: 2.0  |  July: DISABLED")
    print(f"  Max trades/day: {STRONG_MAX_TRADES} (shared cap across modules)")
    print(f"  Max contracts: {STRONG_CONTRACTS} (Topstep scaling plan)")
    print(
        "  One-account filters:"
        f" late-longs={'ON' if LONG_QUALITY_FILTERS_ENABLED else 'OFF'}"
        f" | long-age={'ON' if FVG_LONG_AGE_FILTER_ENABLED else 'OFF'}"
        f" (max {FVG_LONG_MAX_AGE_BARS} bars)"
        f" | long-ema-aged={'ON' if FVG_LONG_EMA_FILTER_ENABLED else 'OFF'}"
        f" (age>={FVG_LONG_EMA_FILTER_MIN_AGE_BARS}, {FVG_LONG_MIN_EMA_SPREAD_PTS:.0f}pts)"
        f" | skip_fomc={'ON' if SKIP_FOMC_ENTRIES else 'OFF'}"
        f" | skip_hour11={'ON' if SKIP_HOUR_11_ENTRIES else 'OFF'}"
        f" | skip_long_10_11={'ON' if SKIP_LONG_HOUR_10_11 else 'OFF'}"
    )
    print(
        "  FVG scoring:"
        f" {'ON' if FVG_QUALITY_SCORE_ENABLED else 'OFF'}"
        f" | sizing={'ON' if FVG_SCORE_SIZING_ENABLED else 'OFF'}"
        f" | med>={FVG_SCORE_MEDIUM_THRESHOLD}:+{FVG_SCORE_MEDIUM_SIZE_STEP}"
        f" | high>={FVG_SCORE_HIGH_THRESHOLD}:+{FVG_SCORE_HIGH_SIZE_STEP}"
        " | features=side,age,ema,adx,size,fresh,level,bos,react"
    )
    print(
        "  FVG freshness:"
        f" filter={'ON' if FVG_FRESH_ONLY else 'OFF'}"
        " | score_bonus=+1"
    )
    print(
        "  FVG level alignment:"
        f" bonus={'+1' if FVG_LEVEL_ALIGNMENT_BONUS_ENABLED else 'OFF'}"
        f" | tol<={FVG_LEVEL_ALIGNMENT_TICKS}t"
    )
    print(
        "  FVG BOS:"
        f" bonus={'+1' if FVG_BOS_BONUS_ENABLED else 'OFF'}"
        f" | lookback={FVG_BOS_LOOKBACK_BARS} bars"
    )
    print(
        "  FVG reaction close:"
        f" bonus={'+1' if FVG_REACTION_CLOSE_BONUS_ENABLED else 'OFF'}"
        " | prior close inside favorable half"
    )
    print(
        "  Exit optimization:"
        f" late-long-target={'ON' if LATE_LONG_TARGET_ENABLED else 'OFF'}"
        f" ({LATE_LONG_TARGET_TICKS} ticks from {LATE_LONG_TARGET_START_HOUR}:00"
        f"-{LATE_LONG_TARGET_END_HOUR}:59)"
    )
    print(
        "  Failed Breakout:"
        f" {'ON' if FAILED_BREAKOUT_ENABLED else 'OFF'}"
        f" | window={FB_WINDOW_START[0]:02d}:{FB_WINDOW_START[1]:02d}"
        f"-{FB_WINDOW_END[0]:02d}:{FB_WINDOW_END[1]:02d}"
        f" | levels=globex+prev-day"
        f" | sweep={FB_MIN_SWEEP_TICKS}-{FB_MAX_SWEEP_TICKS}t"
        f" | reentry<= {FB_MAX_REENTRY_TICKS}t"
        f" | adx={FB_MIN_ADX}-{FB_MAX_ADX}"
        f" | tgt={FB_TARGET_TICKS}t"
        f" | ctr={FB_CONTRACTS}"
        f" | globex:+{FB_GLOBEX_SIZE_STEP if FB_QUALITY_SIZING_ENABLED else 0}"
        f" | max/day={FB_MAX_TRADES_PER_DAY}"
    )
    print(
        "  OD Pullback:"
        f" {'ON' if OD_PULLBACK_ENABLED else 'OFF'}"
        f" | drive={OD_DRIVE_WINDOW_START[0]:02d}:{OD_DRIVE_WINDOW_START[1]:02d}"
        f"-{OD_DRIVE_WINDOW_END[0]:02d}:{OD_DRIVE_WINDOW_END[1]:02d}"
        f" | entry={OD_ENTRY_WINDOW_START[0]:02d}:{OD_ENTRY_WINDOW_START[1]:02d}"
        f"-{OD_ENTRY_WINDOW_END[0]:02d}:{OD_ENTRY_WINDOW_END[1]:02d}"
        f" | side={'short-only' if OD_SHORT_ONLY else 'both'}"
        f" | drive>={OD_MIN_DRIVE_TICKS}t"
        f" | pb={int(OD_MIN_PULLBACK_PCT * 100)}-{int(OD_MAX_PULLBACK_PCT * 100)}%"
    )
    print(
        "  VWAP MR:"
        f" {'ON' if VWAP_MR_ENABLED else 'OFF'}"
        f" | window={VWAP_MR_WINDOW_START[0]:02d}:{VWAP_MR_WINDOW_START[1]:02d}"
        f"-{VWAP_MR_WINDOW_END[0]:02d}:{VWAP_MR_WINDOW_END[1]:02d}"
        f" | dist={VWAP_MR_MIN_DISTANCE_POINTS:.0f}-{VWAP_MR_MAX_DISTANCE_POINTS:.0f}pts"
        f" | max_adx={VWAP_MR_MAX_ADX}"
        f" | failed-bullish-FVG short-only"
    )
    print(f"  Commission: ${COMMISSION_PER_CONTRACT}/contract RT | Slippage: {SLIPPAGE_TICKS} ticks/side")
    if getattr(args, "slippage_ticks", None) is not None:
        print(f"  Stress override: slippage set by CLI to {SLIPPAGE_TICKS:.2f} ticks/side")
    print(f"  Exports: {EXPORT_DIR}")
    print("=" * 60)

    audit = run_startup_audit() if PRODUCTION_AUDIT_ENABLED else None
    df = fetch_data()
    data_summary = validate_loaded_data(df)
    if audit is not None:
        save_run_audit(audit, data_summary)
    df = add_indicators(df)
    df = generate_signals(df)

    print("Computing session levels...")
    session_levels = compute_session_levels(df)
    print(f"  Session levels computed for {len(session_levels)} sessions.")

    print("Running backtest...")
    df, trades, daily_records, state = run_backtest(df, session_levels)
    print(f"  Backtest complete: {len(trades)} trades, {len(daily_records)} sessions.")

    orb_count = sum(1 for t in trades if t.entry_type == "ORB")
    fb_count = sum(1 for t in trades if t.entry_type == "FAILED_BREAKOUT")
    od_count = sum(1 for t in trades if t.entry_type == "OD_PULLBACK")
    fvg_count = sum(1 for t in trades if t.entry_type == "FVG")
    vwap_count = sum(1 for t in trades if t.entry_type == "VWAP_MR")
    print(
        f"  ORB trades: {orb_count}"
        f"  |  FB trades: {fb_count}"
        f"  |  OD trades: {od_count}"
        f"  |  FVG trades: {fvg_count}"
        f"  |  VWAP trades: {vwap_count}"
    )

    monthly_records = build_monthly_records(daily_records, trades)
    stats = compute_stats(df, trades, daily_records, state)
    print_stats(stats, trades, daily_records, monthly_records)

    mc = monte_carlo(stats)
    print_failed_breakout_slice(trades)
    print_failed_breakout_cap_diagnostic(state)
    print_od_pullback_slice(trades)
    print_vwap_mr_slice(trades)
    print_fvg_quality_score_diagnostic(trades)
    print_analysis_highlights(trades)

    print("\n  RISK INSIGHTS")
    print("-" * 45)
    rr    = STRONG_TARGET_TICKS / STOP_TICKS
    kelly = state.kelly_size(rr) * 100
    print(f"  Kelly size estimate:    {kelly:.2f}% of account")
    print(f"  Bayesian win rate:      {state.bayesian_win_rate * 100:.1f}%")
    print(f"  Calmar ratio:           {stats['calmar']:.2f}")
    print(f"  Prop firm ruin risk:    {mc['ruin_prob']:.2f}%")
    if mc["ruin_prob"] > 10:
        print("  WARNING: Ruin probability high — consider reducing size")
    elif mc["ruin_prob"] > 5:
        print("  CAUTION: Ruin probability elevated")
    else:
        print("  Ruin probability within acceptable range")

    print("\nExporting CSVs...")
    export_all_csvs(trades, daily_records, monthly_records)
    if audit is not None:
        audit["result_summary"] = {
            "final_account_value": round(stats["final"], 2),
            "net_trading_pnl": round(stats["total_net"], 2),
            "max_drawdown_pct": round(stats["max_dd"], 2),
            "profit_factor": round(stats["profit_factor"], 2),
            "qualifying_days": int(stats["qualifying_days"]),
            "total_trades": int(stats["num_trades"]),
            "orb_trades": int(orb_count),
            "fb_trades": int(fb_count),
            "fvg_trades": int(fvg_count),
        }
        save_run_audit(audit, data_summary)


if __name__ == "__main__":
    main()
