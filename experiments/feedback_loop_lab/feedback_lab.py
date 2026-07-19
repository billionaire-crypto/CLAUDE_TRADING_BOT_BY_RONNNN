"""
FEEDBACK LOOP LAB — research-only, isolated. Production V29 stays frozen.

QUESTION: can a feedback loop make the bot better, or does it overreact
(hunt) and make things worse?

THERMOSTAT FRAMING
  Mode A = heater on a timer (open loop): never adjusts. Stable but dumb.
  Mode B = twitchy thermostat (narrow deadband): reacts to every wiggle.
           Built to EXPOSE hunting if hunting exists.
  Mode C = disciplined thermostat (wide deadband): ignores noise, only
           moves on statistical evidence, cools down between changes.

HARD RULES HONORED
  - All modes replay the SAME canonical trade stream produced by ONE run of
    the frozen core (same data, seed, fills, costs, equity). Only the
    feedback overlay differs.
  - No lookahead: every decision uses only trades already completed.
  - Bounded knobs: default/min/max/max-step/cooldown/min-sample/deadband.
  - Research-only: reads src/bot.py (frozen), writes only inside this dir.

KNOBS EXCLUDED BY DESIGN (controller rejected them, with reasons):
  - stop/target aggressiveness, delayed entries/exits: change FILLS ->
    violates the shared-trade-stream hard rule; needs bar-level re-sim.
  - time-of-day & news risk multipliers: redundant — the frozen core
    already gates session window and news blackouts.
  - slippage-drift cooldown: untestable here — the backtest cost model has
    no slippage variance to react to (live-only concept).

Run (Windows):
  cd "C:\\CLAUDE TRADING BOT"
  python -X utf8 -m experiments.feedback_loop_lab.feedback_lab
"""
import io
import json
import os
import random
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import numpy as np
import pandas as pd

LAB_DIR = os.path.dirname(os.path.abspath(__file__))
STREAM_CSV = os.path.join(LAB_DIR, "trade_stream.csv")
ADJ_LOG = os.path.join(LAB_DIR, "adjustments_log.csv")
SEED = 42

# ────────────────────────────────────────────────────────────────────────────
# 1. Canonical trade stream (frozen core, run once, cached)
# ────────────────────────────────────────────────────────────────────────────
def build_trade_stream() -> pd.DataFrame:
    if os.path.exists(STREAM_CSV):
        df = pd.read_csv(STREAM_CSV, parse_dates=["date"])
        return df
    import src.bot as b
    data = b.fetch_data()
    data = b.add_indicators(data)
    data = b.generate_signals(data)
    lv = b.compute_session_levels(data)
    _so = sys.stdout
    sys.stdout = io.StringIO()
    try:
        _, trades, _, _ = b.run_backtest(data, lv)
    finally:
        sys.stdout = _so
    rows = [{
        "date": t.date, "day": str(t.date)[:10], "year": int(str(t.date)[:4]),
        "direction": t.direction, "contracts": int(t.contracts),
        "gross": float(t.gross_pnl_usd), "costs": float(t.costs_usd),
        "pnl": float(t.pnl_usd), "won": bool(t.won),
        "score": int(t.fvg_quality_score), "n_today": int(t.trade_number_today),
        "exit_reason": t.exit_reason,
    } for t in trades if t.entry_type == "FVG"]
    df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    df.to_csv(STREAM_CSV, index=False)
    return df


def round_turn_cost(contracts: int) -> float:
    # Mirror of bot.round_turn_cost (kept local so replays don't re-import the
    # heavy module; verified identical tiering in tests/test_risk_and_safety.py)
    import src.bot as b
    return b.round_turn_cost(contracts)


_COST_CACHE = {}
def cost_of(c: int) -> float:
    if c not in _COST_CACHE:
        _COST_CACHE[c] = round_turn_cost(c)
    return _COST_CACHE[c]


# ────────────────────────────────────────────────────────────────────────────
# 2. Knobs — every one bounded, stepped, cooled, deadbanded
# ────────────────────────────────────────────────────────────────────────────
@dataclass
class Knob:
    name: str
    default: float
    vmin: float
    vmax: float
    max_step: float
    cooldown: int          # min trades between changes of THIS knob
    value: float = None
    last_change_idx: int = -10 ** 9
    changes: list = field(default_factory=list)   # (idx, old, new, reason, direction)

    def __post_init__(self):
        self.value = self.default

    def set(self, idx, new, reason, log, ctx):
        new = min(self.vmax, max(self.vmin, new))
        if new == self.value or (idx - self.last_change_idx) < self.cooldown:
            return False
        step = np.sign(new - self.value) * min(abs(new - self.value), self.max_step)
        new = self.value + step
        direction = "risk_down" if (
            (self.name in ("size_mult",) and new < self.value) or
            (self.name in ("min_score",) and new > self.value) or
            (self.name in ("session_cap",) and new < self.value) or
            (self.name in ("cooldown_skips", "day_risk_mult") and True)
        ) else "risk_up"
        old = self.value
        self.value = new
        self.last_change_idx = idx
        self.changes.append((idx, old, new, reason, direction))
        log.append({
            "trade_idx": idx, "mode": ctx["mode"], "param": self.name,
            "old": old, "new": new, "reason": reason,
            "sample_size": ctx["n_window"], "recent_pnl": ctx["win_pnl"],
            "recent_expectancy": ctx["win_exp"], "recent_win_rate": ctx["win_wr"],
            "recent_drawdown": ctx["win_dd"], "deadband": ctx["deadband"],
            "direction": direction,
            "explain": ctx["explain"],
        })
        return True


# ────────────────────────────────────────────────────────────────────────────
# 3. Controller — modes differ ONLY in thresholds (fixed a priori, not tuned)
# ────────────────────────────────────────────────────────────────────────────
MODE_CFG = {
    # Narrow deadband: reacts inside normal noise. Built to expose hunting.
    "B": dict(window=10, exp_band=40.0, dd_trig=-400.0, wr_floor=0.25,
              hot_exp=150.0, allow_risk_up=True, day_loss=-200.0,
              streak_losses=4, knob_cooldown=5, sig_mult=0.0),
    # Wide deadband: needs statistical evidence (2x standard error), long
    # cooldowns, risk-down only. The disciplined thermostat.
    "C": dict(window=40, exp_band=0.0, dd_trig=-1200.0, wr_floor=0.18,
              hot_exp=None, allow_risk_up=False, day_loss=-500.0,
              streak_losses=None, knob_cooldown=25, sig_mult=2.0),
}


def replay(stream: pd.DataFrame, mode: str, enabled: set, log: list):
    """Replay the canonical stream under a feedback controller.
    Returns per-trade results dataframe (taken trades only)."""
    cfg = MODE_CFG.get(mode)
    knobs = {
        "size_mult":   Knob("size_mult", 1.0, 0.5, 1.5 if (cfg and cfg["allow_risk_up"]) else 1.0,
                            0.25, cfg["knob_cooldown"] if cfg else 0),
        # default 0 = take everything (matches the frozen core, which sizes
        # low scores small but does NOT skip them). Escalation: 0 -> 6 -> 7 -> 8.
        "min_score":   Knob("min_score", 0, 0, 8, 6, cfg["knob_cooldown"] if cfg else 0),
        "session_cap": Knob("session_cap", 4, 2, 4, 1, cfg["knob_cooldown"] if cfg else 0),
        "cooldown_skips": Knob("cooldown_skips", 0, 0, 3, 3, cfg["knob_cooldown"] if cfg else 0),
        "day_risk_mult": Knob("day_risk_mult", 1.0, 0.5, 1.0, 0.5, 0),
    } if cfg else {}

    results = []
    window = []                 # rolling adjusted pnl of TAKEN trades
    equity = peak = 0.0
    baseline_sum = baseline_n = 0.0
    pending_skips = 0
    cur_day, day_pnl, day_taken, day_losses = None, 0.0, 0, 0
    consec_losses = 0

    for idx, t in stream.iterrows():
        if t["day"] != cur_day:
            cur_day, day_pnl, day_taken, day_losses = t["day"], 0.0, 0, 0
            if knobs:
                knobs["day_risk_mult"].value = 1.0      # day-scoped, resets daily

        # ---- decide participation/size using ONLY past information ----
        take, skip_reason = True, ""
        if knobs:
            if pending_skips > 0:
                take, skip_reason = False, "dd_cooldown"
                pending_skips -= 1
            elif t["score"] < knobs["min_score"].value:
                take, skip_reason = False, "min_score"
            elif day_taken >= knobs["session_cap"].value:
                take, skip_reason = False, "session_cap"

        if take:
            size_mult = (knobs["size_mult"].value * knobs["day_risk_mult"].value) if knobs else 1.0
            c0 = max(1, int(t["contracts"]))
            c1 = max(1, int(round(c0 * size_mult)))
            gross = t["gross"] / c0 * c1
            pnl = gross - cost_of(c1)
            equity += pnl
            peak = max(peak, equity)
            day_pnl += pnl; day_taken += 1
            if pnl <= 0:
                consec_losses += 1; day_losses += 1
            else:
                consec_losses = 0
            results.append({"idx": idx, "date": t["date"], "day": t["day"],
                            "year": t["year"], "pnl": pnl, "contracts": c1,
                            "size_mult": size_mult, "equity": equity})
        else:
            results.append({"idx": idx, "date": t["date"], "day": t["day"],
                            "year": t["year"], "pnl": 0.0, "contracts": 0,
                            "size_mult": 0.0, "equity": equity,
                            "skipped": skip_reason})

        # SENSOR: the thermostat measures the ROOM (the raw strategy stream,
        # every signal, taken or skipped), not its own heater (the overlay).
        # This mirrors live shadow-tracking of all signals and prevents the
        # frozen-window death spiral (skip everything -> never observe
        # recovery -> stuck at max restriction forever).
        window.append(float(t["pnl"]))
        if cfg and len(window) > cfg["window"]:
            window.pop(0)
        baseline_sum += float(t["pnl"]); baseline_n += 1
        if not take:
            if t["pnl"] <= 0:
                consec_losses += 1
            else:
                consec_losses = 0

        # ---- feedback evaluation (after trade completes; affects FUTURE) ----
        if not knobs or len(window) < (cfg["window"] // 2):
            continue
        n = len(window)
        w = np.array(window)
        w_exp, w_wr = w.mean(), (w > 0).mean()
        w_eq = np.cumsum(w)
        w_dd = float((w_eq - np.maximum.accumulate(w_eq)).min())
        se = w.std(ddof=1) / np.sqrt(n) if n > 1 else 1e9
        base_exp = baseline_sum / baseline_n if baseline_n else 0.0
        band = cfg["exp_band"] + cfg["sig_mult"] * se
        ctx = dict(mode=mode, n_window=n, win_pnl=round(w.sum(), 0),
                   win_exp=round(w_exp, 1), win_wr=round(w_wr, 3),
                   win_dd=round(w_dd, 0), deadband=round(band, 1), explain="")

        def why(txt):
            ctx["explain"] = txt
            return ctx

        # risk-down: expectancy below deadband
        if "size_mult" in enabled and w_exp < base_exp - band and w_exp < 0:
            knobs["size_mult"].set(idx, knobs["size_mult"].value - 0.25, "EXP_NEG", log,
                why(f"Thermostat: room measurably colder than normal (exp ${w_exp:.0f} vs base ${base_exp:.0f}, band ${band:.0f}) -> turn heat down."))
        # risk-up / recovery
        if "size_mult" in enabled:
            if cfg["allow_risk_up"] and cfg["hot_exp"] and w_exp > cfg["hot_exp"]:
                knobs["size_mult"].set(idx, knobs["size_mult"].value + 0.25, "HOT", log,
                    why(f"Twitchy thermostat: brief warm spell (exp ${w_exp:.0f}) -> cranks heat UP (this is the hunting behavior)."))
            elif knobs["size_mult"].value < 1.0 and w_exp > base_exp - se:
                knobs["size_mult"].set(idx, knobs["size_mult"].value + 0.25, "RECOVER", log,
                    why("Temperature back inside the deadband -> step back toward default. Never above it."))
        # rolling drawdown -> cooldown skips
        if "cooldown_skips" in enabled and w_dd <= cfg["dd_trig"]:
            if knobs["cooldown_skips"].set(idx, 2, "DD", log,
                    why(f"Rolling drawdown ${w_dd:.0f} beyond trigger {cfg['dd_trig']} -> sit out 2 trades to confirm it's drift, not noise.")):
                pending_skips = 2
                knobs["cooldown_skips"].value = 0     # one-shot
        # selectivity on collapsed win rate
        if "min_score" in enabled:
            if w_wr < cfg["wr_floor"]:
                proposal = 6 if knobs["min_score"].value == 0 else knobs["min_score"].value + 1
                knobs["min_score"].set(idx, proposal, "WR_LOW", log,
                    why(f"Win rate {w_wr:.0%} below floor {cfg['wr_floor']:.0%} -> only take higher-quality setups."))
            elif knobs["min_score"].value > 0 and w_wr > 0.30:
                proposal = 0 if knobs["min_score"].value <= 6 else knobs["min_score"].value - 1
                knobs["min_score"].set(idx, proposal, "RECOVER", log,
                    why("Win rate normalized -> relax selectivity back toward default."))
        # session cap after streaks (narrow mode only — a twitch reaction)
        if "session_cap" in enabled and cfg["streak_losses"] and consec_losses >= cfg["streak_losses"]:
            knobs["session_cap"].set(idx, 2, "STREAK", log,
                why(f"{consec_losses} losses in a row (normal for 29% WR!) -> twitchy cap on trades/day."))
        if "session_cap" in enabled and knobs["session_cap"].value < 4 and consec_losses == 0:
            knobs["session_cap"].set(idx, 4, "RECOVER", log, why("Streak over -> cap back to default."))
        # daily loss risk-down (day-scoped)
        if "day_risk_mult" in enabled and day_pnl <= cfg["day_loss"] and knobs["day_risk_mult"].value == 1.0:
            knobs["day_risk_mult"].set(idx, 0.5, "DAY_LOSS", log,
                why(f"Day P&L ${day_pnl:.0f} below {cfg['day_loss']} -> half size for the rest of today."))

    return pd.DataFrame(results), knobs


# ────────────────────────────────────────────────────────────────────────────
# 4. Metrics + hunting score
# ────────────────────────────────────────────────────────────────────────────
def metrics(res: pd.DataFrame, knobs=None, label=""):
    taken = res[res["contracts"] > 0]
    pnl = taken["pnl"]
    daily = taken.groupby("day")["pnl"].sum()
    eq = pnl.cumsum()
    dd = float((eq - eq.cummax()).min()) if len(eq) else 0.0
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    pf = wins.sum() / abs(losses.sum()) if len(losses) and losses.sum() != 0 else float("inf")
    sharpe = daily.mean() / daily.std() * np.sqrt(252) if daily.std() else 0.0
    m = dict(label=label, net=pnl.sum(), n=len(taken), wr=(pnl > 0).mean(),
             pf=pf, sharpe=sharpe, maxdd=dd, avg=pnl.mean() if len(pnl) else 0,
             worst_trade=pnl.min() if len(pnl) else 0,
             worst_day=daily.min() if len(daily) else 0,
             skipped=int((res["contracts"] == 0).sum()))
    if knobs:
        changes = [c for k in knobs.values() for c in k.changes]
        m["adjustments"] = len(changes)
        m["risk_up"] = sum(1 for c in changes if c[4] == "risk_up")
        m["risk_down"] = sum(1 for c in changes if c[4] == "risk_down")
        # hunting score
        adj100 = len(changes) / max(1, len(taken)) * 100
        reversals = 0
        by_knob = {}
        for k in knobs.values():
            seq = k.changes
            for a, b2 in zip(seq, seq[1:]):
                if np.sign(b2[2] - b2[1]) == -np.sign(a[2] - a[1]) and (b2[0] - a[0]) <= 3 * max(1, k.cooldown):
                    reversals += 1
        m["reversals"] = reversals
        # post-adjustment degradation: avg pnl of the next 10 taken trades after each change
        post = []
        pnl_arr = taken.reset_index(drop=True)
        idx_map = {r["idx"]: i for i, r in pnl_arr.iterrows()}
        for c in changes:
            start = None
            for orig, pos in idx_map.items():
                if orig >= c[0]:
                    start = pos; break
            if start is not None:
                nxt = pnl_arr["pnl"].iloc[start:start + 10]
                if len(nxt) >= 5:
                    post.append(nxt.mean())
        base_avg = pnl.mean() if len(pnl) else 0
        m["post_adj_delta"] = (np.mean(post) - base_avg) if post else 0.0
        roll = pnl.rolling(20).mean().dropna()
        m["instability"] = roll.std()
        hs = (min(40, adj100 * 4)
              + 30 * (reversals / max(1, len(changes)))
              + min(20, max(0.0, -m["post_adj_delta"]) / 10)
              )
        m["hunting"] = round(hs, 1)
    else:
        m.update(adjustments=0, risk_up=0, risk_down=0, reversals=0,
                 post_adj_delta=0.0, hunting=0.0,
                 instability=pnl.rolling(20).mean().dropna().std() if len(pnl) > 20 else 0)
    return m


def fmt_row(m):
    return (f"{m['label']:<22}{m['net']:>10,.0f}{m['n']:>7}{m['wr']*100:>7.1f}%"
            f"{m['pf']:>7.2f}{m['sharpe']:>7.2f}{m['maxdd']:>10,.0f}"
            f"{m['adjustments']:>6}{m['hunting']:>8.1f}")


HDR = (f"{'mode':<22}{'net $':>10}{'n':>7}{'WR':>8}{'PF':>7}{'Sharpe':>7}"
       f"{'maxDD':>10}{'adj':>6}{'hunt':>8}")


# ────────────────────────────────────────────────────────────────────────────
# 5. Stress battery (replay-compatible subset; fill-changing stresses excluded)
# ────────────────────────────────────────────────────────────────────────────
def stress_variants(stream):
    rng = random.Random(SEED)
    v = {"base": stream}
    s = stream.copy(); s["gross"] = s["gross"] - (s["costs"])        # ~double slip+comm
    v["double_costs"] = s
    s = stream.copy(); s["gross"] = s["gross"] - 2 * s["costs"]
    v["triple_costs"] = s
    v["drop_best10_trades"] = stream.drop(stream.nlargest(10, "pnl").index).reset_index(drop=True)
    best_days = stream.groupby("day")["pnl"].sum().nlargest(10).index
    v["drop_best10_days"] = stream[~stream["day"].isin(best_days)].reset_index(drop=True)
    keep = [i for i in range(len(stream)) if rng.random() > 0.10]
    v["missed_fills_10pct"] = stream.iloc[keep].reset_index(drop=True)
    sh = stream.sample(frac=1.0, random_state=SEED).reset_index(drop=True)
    sh["day"] = stream["day"].values                                  # keep day structure
    v["shuffled_order"] = sh
    wf = stream.sort_values("pnl").reset_index(drop=True); wf["day"] = stream["day"].values
    v["worst_first"] = wf
    return v


# ────────────────────────────────────────────────────────────────────────────
def main():
    stream = build_trade_stream()
    print(f"canonical stream: {len(stream)} trades  {stream['day'].iloc[0]} -> {stream['day'].iloc[-1]}")
    all_knobs = {"size_mult", "min_score", "session_cap", "cooldown_skips", "day_risk_mult"}
    log = []

    runs = {}
    runs["A_open_loop"] = replay(stream, None, set(), log)
    runs["B_narrow_all"] = replay(stream, "B", all_knobs, log)
    runs["C_wide_all"] = replay(stream, "C", all_knobs, log)
    for k in sorted(all_knobs):
        runs[f"B_only_{k}"] = replay(stream, "B", {k}, log)
        runs[f"C_only_{k}"] = replay(stream, "C", {k}, log)

    print("\n===== FULL PERIOD (2019-2026) =====")
    print(HDR)
    ms = {}
    for name, (res, kn) in runs.items():
        m = metrics(res, kn if kn else None, name)
        ms[name] = m
        print(fmt_row(m))

    # out-of-sample: constants were fixed a priori — just report period splits
    print("\n===== PERIOD SPLITS (same fixed constants — honest OOS) =====")
    for lo, hi, tag in [(0, 2022, "IS 2019-2022"), (2023, 2024, "OOS1 2023-24"), (2025, 2026, "OOS2 2025-26")]:
        sub = stream[(stream["year"] >= (lo or 2019)) & (stream["year"] <= hi)].reset_index(drop=True)
        print(f"\n--- {tag} ---"); print(HDR)
        for mode, en in [(None, set()), ("B", all_knobs), ("C", all_knobs)]:
            res, kn = replay(sub, mode, en, [])
            print(fmt_row(metrics(res, kn if kn else None, f"{mode or 'A'}_{tag[:3]}")))

    print("\n===== WALK-FORWARD (net $ by year) =====")
    yr_rows = {}
    for name in ("A_open_loop", "B_narrow_all", "C_wide_all"):
        res, _ = runs[name]
        yr_rows[name] = res[res.contracts > 0].groupby("year")["pnl"].sum()
    wf = pd.DataFrame(yr_rows).round(0)
    print(wf.to_string())

    print("\n===== STRESS BATTERY (net $; delta vs mode A per stress) =====")
    print(f"{'stress':<22}{'A net':>10}{'B net':>10}{'C net':>10}{'C-A':>9}{'B-A':>9}")
    for sname, sv in stress_variants(stream).items():
        row = {}
        for mode, en, tag in [(None, set(), "A"), ("B", all_knobs, "B"), ("C", all_knobs, "C")]:
            res, kn = replay(sv, mode, en, [])
            row[tag] = metrics(res, kn if kn else None, tag)["net"]
        print(f"{sname:<22}{row['A']:>10,.0f}{row['B']:>10,.0f}{row['C']:>10,.0f}"
              f"{row['C']-row['A']:>9,.0f}{row['B']-row['A']:>9,.0f}")

    pd.DataFrame(log).to_csv(ADJ_LOG, index=False)
    print(f"\nadjustment log ({len(log)} rows) -> {ADJ_LOG}")

    print("\n===== ADJUSTMENT PROFILE =====")
    for name in ("B_narrow_all", "C_wide_all"):
        m = ms[name]
        print(f"{name}: adjustments={m['adjustments']} (up={m['risk_up']}, down={m['risk_down']}) "
              f"reversals={m['reversals']} post-adj delta=${m['post_adj_delta']:.0f}/trade "
              f"hunting={m['hunting']}")


if __name__ == "__main__":
    main()
