"""
SHADOW ENGINE — isolated, asynchronous analytical layer for the MNQ V29 bot.

Isolation guarantees (hard, by construction):
  - ZERO writes to src/ or any production file. Writes ONLY inside shadow/.
  - Read-only ingestion: fill forensics CSV, runtime state, daily logs, and
    (for the funnel pipeline) read-only market bars via the same API call the
    bot uses. No order routing, no state mutation, no parameter staging.
  - NO strategy changes ever. Pipeline 3 is a diagnostic decomposition of the
    day's entry funnel, not a tuner: staging "V30 candidates" from one day of
    data is curve-fitting and is disabled by design (see research ledger).

Pipelines (per Shadow Engine template):
  P1 DRIFT      — live fills vs frozen 7-yr baseline (V29SystemShield)
  P2 FUNNEL     — zero-trade-session census vs the 21.9% historical base rate
  P3 GHOST      — which entry gate was binding today (regime / FVG formation /
                  pullback-into-gap / VWAP bias), quantified on today's bars
  P4 TELEMETRY  — log ERROR/WARN vs whitelist, data gaps, hub restarts,
                  in-session loop latency, state freshness

Outputs (three tiers, written to shadow/shadow_reports/):
  latest.json + YYYYMMDD.json       — machine metrics
  daily_diagnostic.md + YYYYMMDD.md — scientist-notebook markdown
  Telegram alert                    — SENT ONLY on tolerance breach

Run:
  python shadow\\shadow_engine.py --once     one diagnostic now
  python shadow\\shadow_engine.py --daemon   background: daily at 15:35 CT
"""
import csv
import glob
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

REPORT_DIR = os.path.join(ROOT, "shadow", "shadow_reports")
EXPORTS = os.path.join(ROOT, "src", "exports")
FORENSICS = os.path.join(EXPORTS, "v29_fill_forensics.csv")
STATE = os.path.join(EXPORTS, "v29_topstep_runtime_state.json")
GO_LIVE = "2026-07-06"
DAEMON_RUN_AT = (15, 35)   # America/Chicago (CT), after EOD flatten + summary


def _now_ct():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/Chicago"))


def shield():
    from research.bot_v30_draft import V29SystemShield
    return V29SystemShield


def _send_telegram(text):
    """Minimal sender (mirrors watchdog) — no runtime import, no test leakage."""
    from urllib import parse, request
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(ROOT, ".env"))
    except Exception:
        pass
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        return False
    data = parse.urlencode({"chat_id": chat, "text": text}).encode()
    try:
        request.urlopen(request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data, method="POST"), timeout=15)
        return True
    except Exception:
        return False


# ── P1 DRIFT ─────────────────────────────────────────────────────────────────
def p1_drift(S):
    out = {"fills_recorded": 0, "exits": 0, "observed_slippage_status": "Normal",
           "avg_entry_slip_ticks": None, "avg_exit_slip_ticks": None,
           "live_win_rate": None, "win_rate_convergence": 0.0,
           "live_avg_trade_usd": None, "sufficient_sample": False, "breaches": []}
    if not os.path.exists(FORENSICS):
        out["note"] = "no fills yet — drift unmeasurable (n=0)"
        return out
    with open(FORENSICS, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    entries = [r for r in rows if r.get("kind") == "entry"]
    exits = [r for r in rows if str(r.get("kind", "")).startswith("exit")]
    f = lambda r, k: float(r[k]) if r.get(k) not in (None, "",) else None
    es = [s for r in entries if (s := f(r, "slippage_ticks")) is not None]
    xs = [s for r in exits if (s := f(r, "slippage_ticks")) is not None]
    pnls = [p for r in exits if (p := f(r, "trade_pnl")) is not None]
    out["fills_recorded"] = len(rows)
    out["exits"] = len(exits)
    if es:
        out["avg_entry_slip_ticks"] = round(sum(es) / len(es), 2)
    if xs:
        out["avg_exit_slip_ticks"] = round(sum(xs) / len(xs), 2)
    worst_slip = max([out["avg_entry_slip_ticks"] or 0, out["avg_exit_slip_ticks"] or 0])
    if len(es) + len(xs) >= 5:
        if worst_slip > S.TOLERANCES["slippage_alarm_ticks"]:
            out["observed_slippage_status"] = "Variance Spike"
            out["breaches"].append(f"slippage {worst_slip:.1f}t > {S.TOLERANCES['slippage_alarm_ticks']}t")
        elif worst_slip > 2.0:
            out["observed_slippage_status"] = "Elevated"
    if pnls:
        wr = sum(1 for p in pnls if p > 0) / len(pnls)
        out["live_win_rate"] = round(wr, 3)
        out["live_avg_trade_usd"] = round(sum(pnls) / len(pnls), 0)
        out["win_rate_convergence"] = round(wr - S.BASELINE["win_rate"], 3)
        out["sufficient_sample"] = len(pnls) >= S.TOLERANCES["min_trades_to_judge"]
        if out["sufficient_sample"] and wr < S.TOLERANCES["wr_alarm_below"]:
            out["breaches"].append(f"WR {wr:.0%} < {S.TOLERANCES['wr_alarm_below']:.0%} over {len(pnls)} trades")
    return out


# ── P2 FUNNEL ────────────────────────────────────────────────────────────────
def p2_funnel(S):
    # sessions bot ran = dated live logs since go-live (weekdays)
    ran = sorted({os.path.basename(p)[9:17] for p in glob.glob(os.path.join(EXPORTS, "live_log_*.txt"))
                  if os.path.basename(p)[9:17] >= GO_LIVE.replace("-", "")})
    ran = [d for d in ran if datetime.strptime(d, "%Y%m%d").weekday() < 5]
    # today only counts as a session AFTER it has actually closed (15:00 CT) —
    # otherwise a pre-open run would report today as a zero-trade session.
    ct = _now_ct()
    if (ct.hour, ct.minute) < (15, 0):
        ran = [d for d in ran if d != ct.strftime("%Y%m%d")]
    traded = set()
    if os.path.exists(FORENSICS):
        with open(FORENSICS, newline="", encoding="utf-8") as fh:
            traded = {str(r.get("session_date", ""))[:10].replace("-", "") for r in csv.DictReader(fh)}
    zero = [d for d in ran if d not in traded]
    consec = 0
    for d in reversed(ran):
        if d in traded:
            break
        consec += 1
    out = {"live_sessions": len(ran), "zero_trade_sessions": len(zero),
           "consecutive_quiet": consec,
           "historical_idle_rate": S.BASELINE["zero_trade_day_rate"],
           "funnel_throughput_status": S.funnel_verdict(consec, len(ran)),
           "breaches": []}
    if consec >= S.TOLERANCES["quiet_sessions_alarm"]:
        out["breaches"].append(f"{consec} consecutive quiet sessions >= {S.TOLERANCES['quiet_sessions_alarm']} (beyond 7-yr record)")
    return out


# ── P3 GHOST (gate decomposition on today's completed bars) ─────────────────
def p3_ghost():
    out = {"session_bars": 0, "gate_regime_open": None, "fvgs_formed": None,
           "bars_price_in_gap": None, "binding_gate": None, "note": None}
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(ROOT, ".env"))
        import src.bot as b
        import src.topstepx_runtime as tr
        from src.topstepx_client import TopstepXConfig, TopstepXClient
        from datetime import timezone as tz
        cfg = TopstepXConfig.from_env()
        cl = TopstepXClient(cfg)
        cl.authenticate()
        c = cl.resolve_contract(cfg.contract_search_text, live=cfg.live_data)
        now = datetime.now(tz.utc).replace(tzinfo=None)
        bars = cl.retrieve_bars(contract_id=str(c["id"]),
                                start_time=(now - timedelta(days=4)).replace(microsecond=0).isoformat() + "Z",
                                end_time=now.replace(microsecond=0).isoformat() + "Z",
                                live=cfg.live_data, unit=2, unit_number=5, limit=600,
                                include_partial_bar=False)
        df = tr._bars_to_strategy_df(bars)
        df = b.add_indicators(df)
        df = b.generate_signals(df)
        today = str(datetime.now(b.TIMEZONE))[:10]
        ddf = df[df.index.map(lambda x: str(x)[:10] == today)]
        if len(ddf) < 6:
            out["note"] = f"no completed session bars for {today} yet — pipeline idle"
            return out
        gap_min = b.FVG_MIN_SIZE_TICKS * b.MNQ_TICK_SIZE
        active, regime_ok, in_gap, fvgs = [], 0, 0, 0
        for i in range(2, len(ddf)):
            row, prev, bar2 = ddf.iloc[i], ddf.iloc[i - 1], ddf.iloc[i - 2]
            if bar2["high"] < row["low"] and (row["low"] - bar2["high"]) >= gap_min:
                active.append(b.FVG(direction="bullish", top=row["low"], bottom=bar2["high"],
                                    created_bar=i, session_date=today, body_pct=0, sweep=False,
                                    vwap_dist_at_creation=0)); fvgs += 1
            if bar2["low"] > row["high"] and (bar2["low"] - row["high"]) >= gap_min:
                active.append(b.FVG(direction="bearish", top=bar2["low"], bottom=row["high"],
                                    created_bar=i, session_date=today, body_pct=0, sweep=False,
                                    vwap_dist_at_creation=0)); fvgs += 1
            active = [x for x in active if b.is_fvg_valid(x, i, today, row["high"], row["low"], row["close"])]
            if b.get_risk_profile(prev)["contracts"] > 0:
                regime_ok += 1
            if any(b.price_in_fvg(x, row["open"]) for x in active):
                in_gap += 1
        out.update(session_bars=len(ddf), gate_regime_open=regime_ok,
                   fvgs_formed=fvgs, bars_price_in_gap=in_gap)
        if regime_ok < len(ddf) * 0.2:
            out["binding_gate"] = "regime (volatility outside band most of day)"
        elif fvgs == 0:
            out["binding_gate"] = "fvg_formation (no gaps formed)"
        elif in_gap == 0:
            out["binding_gate"] = "pullback (gaps formed, price never returned)"
        else:
            out["binding_gate"] = "none (setups reached entry evaluation)"
    except Exception as exc:
        out["note"] = f"pipeline skipped: {str(exc)[:120]}"
    return out


# ── P4 TELEMETRY ─────────────────────────────────────────────────────────────
def p4_telemetry(S):
    out = {"errors": 0, "non_whitelisted_warns": 0, "data_gaps": 0,
           "hub_restarts": 0, "state_fresh_seconds": None,
           "in_session_latency_ok": True, "breaches": []}
    day = datetime.now().strftime("%Y%m%d")
    out["hub_restarts_all_day"] = 0
    # ERROR lines are written to live_errors.txt (error_only=True never reaches
    # the main log). The 2026-07-08 auth-death storm was invisible here because
    # this pipeline read only live_log_*.txt. Count today's errors from the
    # errors file — the authoritative source.
    errfile = os.path.join(EXPORTS, "live_errors.txt")
    if os.path.exists(errfile):
        with open(errfile, encoding="utf-8", errors="replace") as fh:
            day_tag = f"[{day[:4]}-{day[4:6]}-{day[6:]}"
            out["errors"] = sum(1 for ln in fh if ln.startswith(day_tag) and "[ERROR]" in ln)
    log = os.path.join(EXPORTS, f"live_log_{day}.txt")
    if os.path.exists(log):
        with open(log, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                if "[ERROR]" in ln:
                    pass  # counted from live_errors.txt above (superset)
                elif "[WARN]" in ln:
                    if not any(w in ln for w in S.TELEMETRY["whitelisted_warns"]):
                        out["non_whitelisted_warns"] += 1
                if "data_gap" in ln:
                    out["data_gaps"] += 1
                if "user_hub_stream_restarted" in ln or "user_hub_stale" in ln:
                    out["hub_restarts_all_day"] += 1
                    # Only IN-SESSION reconnects matter. Overnight connection
                    # cycling and manual bot restarts are benign noise (they
                    # produced 2 false phone alarms). Parse the HH:MM off the
                    # log line and count only 08:30-15:00 CT.
                    hm = ln[12:17] if ln.startswith("[") and ln[11] == " " else ""
                    if "08:30" <= hm <= "15:00":
                        out["hub_restarts"] += 1
    try:
        d = json.load(open(STATE, encoding="utf-8"))
        ts = datetime.fromisoformat(str(d.get("updated_at")))
        out["state_fresh_seconds"] = round((datetime.now(ts.tzinfo) - ts).total_seconds())
        lat = d.get("latency_bar_to_signal_ms")
        lo, hi = S.TELEMETRY["latency_bar_to_signal_valid_window_ct"]
        now_hm = datetime.now(ts.tzinfo).strftime("%H:%M")
        if lat and lo <= now_hm <= hi and lat > S.TELEMETRY["latency_bar_to_signal_max_ms_in_session"]:
            out["in_session_latency_ok"] = False
            out["breaches"].append(f"in-session bar->signal latency {lat / 1000:.0f}s")
    except Exception:
        pass
    if out["errors"] > S.TELEMETRY["max_errors_per_session"]:
        out["breaches"].append(f"{out['errors']} ERROR lines today")
    # A reconnect only matters if it actually broke data flow. In-session
    # restarts alone (with zero data gaps) = the connection self-healed = fine.
    if out["hub_restarts"] > S.TELEMETRY["max_hub_restarts_per_session"] and out["data_gaps"] > 0:
        out["breaches"].append(f"{out['hub_restarts']} in-session hub restarts WITH {out['data_gaps']} data gaps")
    if out["state_fresh_seconds"] is not None and out["state_fresh_seconds"] > 360:
        out["breaches"].append(f"bot state stale {out['state_fresh_seconds']}s")
    return out


# ── compose + emit ───────────────────────────────────────────────────────────
def run_once():
    S = shield()
    p1, p2, p3, p4 = p1_drift(S), p2_funnel(S), p3_ghost(), p4_telemetry(S)
    breaches = p1["breaches"] + p2["breaches"] + p4["breaches"]
    stamp = datetime.now().strftime("%Y%m%d")

    machine = {"generated_at": datetime.now().isoformat(), "drift": p1,
               "funnel": p2, "ghost": p3, "telemetry": p4,
               "breaches": breaches, "v30_staged": False}

    md = [f"# SHADOW ENGINE DIAGNOSTIC — {datetime.now():%Y-%m-%d %H:%M}", "---",
          "## I. Metric drift",
          (f"Fills: {p1['fills_recorded']} ({p1['exits']} exits). "
           + (f"WR {p1['live_win_rate']:.0%} (baseline 29.6%), avg ${p1['live_avg_trade_usd']}, "
              f"entry slip {p1['avg_entry_slip_ticks']}t / exit {p1['avg_exit_slip_ticks']}t."
              if p1["exits"] else "Drift UNMEASURABLE — no live fills yet (n=0)."))
          + (" Sample sufficient to judge." if p1["sufficient_sample"] else " Sample below 30 — informational only."),
          "## II. Funnel",
          f"{p2['zero_trade_sessions']}/{p2['live_sessions']} zero-trade sessions "
          f"({p2['consecutive_quiet']} consecutive) vs 21.9% historical idle rate -> "
          f"**{p2['funnel_throughput_status']}**.",
          "## III. Ghost (today's binding gate — diagnostic only, never a tuner)",
          (f"{p3['session_bars']} bars: regime open {p3['gate_regime_open']}, FVGs {p3['fvgs_formed']}, "
           f"price-in-gap bars {p3['bars_price_in_gap']} -> binding gate: **{p3['binding_gate']}**"
           if p3["session_bars"] else f"_{p3['note']}_"),
          "## IV. Telemetry",
          f"ERRORs {p4['errors']}, non-whitelisted WARNs {p4['non_whitelisted_warns']}, "
          f"data gaps {p4['data_gaps']}, hub restarts {p4['hub_restarts']}, "
          f"state fresh {p4['state_fresh_seconds']}s.",
          "## Verdict",
          ("🔴 BREACHES: " + "; ".join(breaches)) if breaches else "🟢 All clear — within tolerances.",
          "\n_V30 staging permanently disabled: parameter candidates require the full validation gauntlet._"]
    md_text = "\n\n".join(md)

    os.makedirs(REPORT_DIR, exist_ok=True)
    for name, payload in ((f"{stamp}.json", machine), ("latest.json", machine)):
        with open(os.path.join(REPORT_DIR, name), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1)
    for name in (f"{stamp}.md", "daily_diagnostic.md"):
        with open(os.path.join(REPORT_DIR, name), "w", encoding="utf-8") as fh:
            fh.write(md_text + "\n")

    if breaches:
        _send_telegram("🤖 Shadow Engine ALERT\n• " + "\n• ".join(breaches)
                       + "\nDetails: shadow/shadow_reports/daily_diagnostic.md")
    print(md_text)
    print(f"\nreports -> {REPORT_DIR}  |  telegram: {'SENT (breach)' if breaches else 'silent (all clear)'}")


def _daemon_log(msg):
    line = f"[{_now_ct():%Y-%m-%d %H:%M:%S} CT] {msg}\n"
    try:
        os.makedirs(REPORT_DIR, exist_ok=True)
        with open(os.path.join(REPORT_DIR, "daemon.log"), "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass
    print(line, end="")


def daemon():
    _daemon_log(f"daemon started (fires {DAEMON_RUN_AT[0]:02d}:{DAEMON_RUN_AT[1]:02d} CT Mon-Fri)")
    import subprocess
    last_run_day = None
    while True:
        try:
            now = _now_ct()
            if (now.weekday() < 5 and last_run_day != now.date()
                    and (now.hour, now.minute) >= DAEMON_RUN_AT):
                _daemon_log("scheduled run FIRING")
                rc = subprocess.call([sys.executable, "-X", "utf8", os.path.abspath(__file__), "--once"])
                _daemon_log(f"scheduled run finished, exit={rc}")
                last_run_day = now.date()
        except Exception as exc:      # never let the scheduler loop die silently
            _daemon_log(f"loop error (continuing): {exc}")
        time.sleep(60)


if __name__ == "__main__":
    if "--daemon" in sys.argv:
        daemon()
    else:
        run_once()
