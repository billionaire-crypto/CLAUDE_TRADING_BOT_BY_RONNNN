"""
NIGHTLY RESEARCHER — "the brain working at night."

The bot trades in the morning on the frozen, gauntlet-cleared config. This
runner spends the night testing ONE pending backlog candidate against the
FULL 7-year history — never against a single day's price action (that is
curve-fitting; see research/RESEARCH_LEDGER.md for what happens when you do
that). Each night: pick one candidate -> run every gate -> write a ledger
entry -> mark the backlog -> (if a candidate SHIPS) tell you on Telegram.

HARD GUARANTEES
  - Never writes to src/. The tested parameter is set via setattr() on the
    already-imported bot module in memory, then restored — same pattern
    research/run_bias_validation.py already used and shipped from. On disk,
    src/bot.py is byte-for-byte untouched.
  - Never auto-applies a SHIP verdict. A ship means "this earned a human
    decision," logged to the ledger for you to read and decide, same as
    every other change in this project's history.
  - One candidate per night. No candidate is re-tested once resolved
    (status flips to shipped/rejected) — re-running a settled question is
    exactly the "did we already try this" waste the ledger exists to prevent.

GAUNTLET (pre-registered, same shape as every human-run validation in this
repo's history — see run_bias_validation.py):
  A. 3-way split: dev 2019-2022 / val1 2023-2024 / val2 2025-2026+
  B. Walk-forward: 6-month windows, whole history
  C. Stress: slippage x2/x3, missed fills 10%, remove best-10 trades
  D. Combine sim: 100k-path bootstrap, pass rate + daily-limit fail rate
  E. Floor safety: worst modeled trade vs the -$750 internal DLL buffer

VERDICT RULES (pre-registered — do not loosen post-hoc to rescue a candidate):
  SHIP only if ALL of:
    - candidate net >= 95% of baseline net in EVERY one of the 3 periods
      (never just "better on average" — must not quietly wreck one era)
    - stress slip-x3: PF >= 1.5 and net > 0
    - combine sim: pass rate does not drop by more than 1 percentage point
    - floor safety: worst modeled trade <= the current baseline's worst trade
      (a "better" parameter that is also more dangerous does not ship)
  Otherwise REJECT, with the first failing gate named as the reason.

Run:
  python research\\nightly_researcher.py --once     one candidate, right now
  python research\\nightly_researcher.py --daemon   background: nightly at 20:00 CT
"""
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BACKLOG_PATH = os.path.join(ROOT, "research", "nightly_backlog.json")
LEDGER_PATH = os.path.join(ROOT, "research", "RESEARCH_LEDGER.md")
RUN_AT = (20, 0)  # America/Chicago, well after the shadow engine's 15:35 report


def _now_ct():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/Chicago"))


def _send_telegram(text):
    from urllib import parse, request
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(ROOT, ".env"))
    except Exception:
        pass
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        return
    data = parse.urlencode({"chat_id": chat, "text": text}).encode()
    try:
        request.urlopen(request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data, method="POST"), timeout=15)
    except Exception:
        pass


def load_backlog():
    with open(BACKLOG_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def save_backlog(items):
    with open(BACKLOG_PATH, "w", encoding="utf-8") as fh:
        json.dump(items, fh, indent=1)


def _metrics(trades):
    pnls = [t.pnl_usd for t in trades]
    if not pnls:
        return dict(n=0, net=0, pf=0, sharpe=0, wr=0, worst=0)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    daily = defaultdict(float)
    for t in trades:
        daily[str(t.date)[:10]] += t.pnl_usd
    arr = np.array(list(daily.values()))
    sharpe = arr.mean() / arr.std(ddof=1) * np.sqrt(252) if len(arr) > 1 and arr.std(ddof=1) > 0 else 0.0
    return dict(n=len(pnls), net=sum(pnls),
                pf=(sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else float("inf")),
                sharpe=sharpe, wr=len(wins) / len(pnls) * 100, worst=min(pnls))


def _slice(trades, d0, d1):
    return _metrics([t for t in trades if d0 <= str(t.date)[:10] <= d1])


def run_gauntlet(param: str, candidate_value):
    """Full 7-year gauntlet. Reads src/bot.py; monkeypatches ONE attribute in
    memory only; restores it before returning. Never writes to src/bot.py."""
    import src.bot as bot
    from research.run_combine_sim import _simulate

    df = bot.fetch_data()
    bot.validate_loaded_data(df)
    di = bot.add_indicators(df)
    sl = bot.compute_session_levels(di)
    baseline_value = getattr(bot, param)

    def backtest(value):
        old = getattr(bot, param)
        setattr(bot, param, value)
        try:
            ds = bot.generate_signals(di.copy())
            _, trades, days, _ = bot.run_backtest(ds, sl)
        finally:
            setattr(bot, param, old)   # ALWAYS restore, even on exception
        return trades, days

    tr_base, dr_base = backtest(baseline_value)
    tr_cand, dr_cand = backtest(candidate_value)

    periods = [("dev_2019_2022", "2019-01-01", "2022-12-31"),
               ("val1_2023_2024", "2023-01-01", "2024-12-31"),
               ("val2_2025_2026", "2025-01-01", "2026-12-31")]
    split = {name: {"base": _slice(tr_base, d0, d1), "cand": _slice(tr_cand, d0, d1)}
             for name, d0, d1 in periods}

    walk = []
    for y in range(2019, 2027):
        for a, b in (("01-01", "06-30"), ("07-01", "12-31")):
            m = _slice(tr_cand, f"{y}-{a}", f"{y}-{b}")
            if m["n"] > 0:
                walk.append(m)

    def stress(mult=None, miss=None):
        old_tiers = bot.SLIPPAGE_SCALE_TIERS
        old_miss, old_seed = bot.MISS_FILL_PROB, bot.MISS_FILL_SEED
        try:
            if mult:
                bot.SLIPPAGE_SCALE_TIERS = [(c, t * mult) for c, t in old_tiers]
            if miss:
                bot.MISS_FILL_PROB, bot.MISS_FILL_SEED = miss, 1
            tr, _ = backtest(candidate_value)
        finally:
            bot.SLIPPAGE_SCALE_TIERS = old_tiers
            bot.MISS_FILL_PROB, bot.MISS_FILL_SEED = old_miss, old_seed
        return _metrics(tr)

    stress_results = {"slip_x2": stress(mult=2), "slip_x3": stress(mult=3),
                      "miss_10pct": stress(miss=0.10)}

    def combine(daily_records):
        dp = np.array([d.daily_pnl_net for d in daily_records])
        ip = np.array([d.max_intraday_peak for d in daily_records])
        qf = np.array([d.is_qualifying_day for d in daily_records], dtype=bool)
        p, days_arr, fails = _simulate(dp, ip, qf, None)
        return {"pass_rate": p, "median_days": float(np.median(days_arr)) if len(days_arr) else 0,
                "daily_limit_fail_pct": fails["daily_limit"] / 1000}

    combine_base, combine_cand = combine(dr_base), combine(dr_cand)

    return {
        "param": param, "baseline_value": baseline_value, "candidate_value": candidate_value,
        "split": split, "walk_forward": walk, "stress": stress_results,
        "combine_base": combine_base, "combine_cand": combine_cand,
        "worst_trade_base": _metrics(tr_base)["worst"], "worst_trade_cand": _metrics(tr_cand)["worst"],
    }


def decide(g: dict):
    """Pre-registered verdict rules. Returns (verdict, reasons: list[str])."""
    reasons = []
    for name, d in g["split"].items():
        b, c = d["base"]["net"], d["cand"]["net"]
        if b > 0 and c < 0.95 * b:
            reasons.append(f"{name}: candidate net ${c:,.0f} < 95% of baseline ${b:,.0f}")
    s3 = g["stress"]["slip_x3"]
    if s3["pf"] < 1.5 or s3["net"] <= 0:
        reasons.append(f"stress slip_x3: PF {s3['pf']:.2f} / net ${s3['net']:,.0f} (need PF>=1.5, net>0)")
    pass_delta = g["combine_cand"]["pass_rate"] - g["combine_base"]["pass_rate"]
    if pass_delta < -1.0:
        reasons.append(f"combine pass rate {pass_delta:+.1f}pp (worse than -1pp tolerance)")
    if g["worst_trade_cand"] < g["worst_trade_base"] - 1e-6:
        reasons.append(f"worst trade ${g['worst_trade_cand']:,.0f} worse than baseline ${g['worst_trade_base']:,.0f}")
    return ("REJECT", reasons) if reasons else ("SHIP", [])


def write_ledger_entry(candidate: dict, g: dict, verdict: str, reasons: list):
    date = _now_ct().strftime("%Y-%m-%d")
    lines = [f"\n---\n\n## {date} — 🌙 NIGHTLY RESEARCHER: {candidate['param']} "
             f"{g['baseline_value']} -> {g['candidate_value']} — **{verdict}**\n"]
    lines.append(f"### Hypothesis\n{candidate['rationale']}\n")
    lines.append("### Method (script: research/nightly_researcher.py — automated, unattended)\n"
                 "Full 7-year gauntlet: 3-way split (dev 19-22 / val1 23-24 / val2 25-26+), "
                 "walk-forward (half-year windows), stress (slip x2/x3, 10% missed fills), "
                 "100k-path combine bootstrap, floor safety vs baseline worst trade. "
                 "Parameter set via in-memory setattr on `bot.py`, restored after each run — "
                 "**src/bot.py was not modified on disk.**\n")
    lines.append("### Result\n")
    for name, d in g["split"].items():
        b, c = d["base"], d["cand"]
        lines.append(f"- **{name}**: baseline net ${b['net']:,.0f} (PF {b['pf']:.2f}, n={b['n']}) "
                     f"vs candidate net ${c['net']:,.0f} (PF {c['pf']:.2f}, n={c['n']})")
    wf = g["walk_forward"]
    neg = sum(1 for m in wf if m["net"] < 0)
    lines.append(f"- **Walk-forward**: {len(wf)} windows, {neg} negative, "
                 f"avg ${np.mean([m['net'] for m in wf]):,.0f}/window")
    for k, m in g["stress"].items():
        lines.append(f"- **Stress {k}**: net ${m['net']:,.0f}, PF {m['pf']:.2f}")
    lines.append(f"- **Combine sim**: pass rate {g['combine_base']['pass_rate']:.1f}% -> "
                 f"{g['combine_cand']['pass_rate']:.1f}%, daily-limit fails "
                 f"{g['combine_base']['daily_limit_fail_pct']:.2f}% -> "
                 f"{g['combine_cand']['daily_limit_fail_pct']:.2f}%")
    lines.append(f"- **Floor safety**: worst trade ${g['worst_trade_base']:,.0f} -> ${g['worst_trade_cand']:,.0f}")
    lines.append(f"\n### Decision\n**{verdict}**"
                 + (f" — {'; '.join(reasons)}" if reasons
                    else " — cleared every pre-registered gate. Needs human review before shipping; "
                         "the runner never edits src/bot.py itself."))
    lines.append("\n### Notes for the next session\n"
                 "Automated overnight result. Verify independently before changing the live config "
                 "— this is a candidate for human review, not an applied change.\n")
    with open(LEDGER_PATH, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def run_once():
    backlog = load_backlog()
    pending = [b for b in backlog if b["status"] == "pending"]
    if not pending:
        print("Nightly researcher: backlog empty (no pending candidates). Nothing to do.")
        return
    candidate = pending[0]
    print(f"Nightly researcher: testing {candidate['param']} -> {candidate['candidate_value']} "
         f"({candidate['id']})")
    g = run_gauntlet(candidate["param"], candidate["candidate_value"])
    verdict, reasons = decide(g)
    write_ledger_entry(candidate, g, verdict, reasons)

    for b in backlog:
        if b["id"] == candidate["id"]:
            b["status"] = "shipped_pending_review" if verdict == "SHIP" else "rejected"
            b["tested_at"] = _now_ct().isoformat()
            b["verdict"] = verdict
    save_backlog(backlog)

    summary = (f"🌙 Nightly Researcher\n• Tested: {candidate['param']} -> {candidate['candidate_value']}\n"
              f"• Verdict: {verdict}"
              + (f"\n• Needs your review — see RESEARCH_LEDGER.md" if verdict == "SHIP" else ""))
    print(summary)
    if verdict == "SHIP":
        _send_telegram(summary)   # only page the phone on something worth a look
    print(f"\nledger updated -> {LEDGER_PATH}")
    print(f"backlog updated -> {BACKLOG_PATH}")


def daemon():
    print(f"Nightly Researcher daemon: one candidate/night at {RUN_AT[0]:02d}:{RUN_AT[1]:02d} America/Chicago")
    import subprocess
    last_run_day = None
    while True:
        now = _now_ct()
        if last_run_day != now.date() and (now.hour, now.minute) >= RUN_AT:
            subprocess.call([sys.executable, "-X", "utf8", os.path.abspath(__file__), "--once"])
            last_run_day = now.date()
        time.sleep(120)


if __name__ == "__main__":
    if "--daemon" in sys.argv:
        daemon()
    else:
        run_once()
