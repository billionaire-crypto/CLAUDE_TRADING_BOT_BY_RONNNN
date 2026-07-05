"""
FVG strategy experiment runner.
Tests 5 first-principles filters against the base model.
Each test enables ONE flag only; all others stay False.
"""
import src.bot as bot

# ── helpers ───────────────────────────────────────────────────────────────────

def _fvg_stats(trades):
    fvg = [t for t in trades if t.entry_type == "FVG"]
    if not fvg:
        return {"trades": 0, "wr": 0.0, "net": 0.0, "avg": 0.0}
    wins = sum(1 for t in fvg if t.won)
    net  = sum(t.pnl_usd for t in fvg)
    return {
        "trades": len(fvg),
        "wr":     round(wins / len(fvg) * 100, 1),
        "net":    round(net, 0),
        "avg":    round(net / len(fvg), 1),
    }


def _run(label: str, **overrides) -> dict:
    # apply overrides
    original = {}
    for k, v in overrides.items():
        original[k] = getattr(bot, k)
        setattr(bot, k, v)

    try:
        df_raw  = bot.fetch_data()
        bot.validate_loaded_data(df_raw)
        df_ind  = bot.add_indicators(df_raw)
        sl      = bot.compute_session_levels(df_ind)
        df_sig  = bot.generate_signals(df_ind.copy())
        _, trades, _, _ = bot.run_backtest(df_sig, sl)
        stats = _fvg_stats(trades)
        stats["label"] = label
        return stats
    finally:
        for k, v in original.items():
            setattr(bot, k, v)


# ── run all tests ─────────────────────────────────────────────────────────────

print("Loading data once …")
results = []

print("Running base model …")
results.append(_run("Base (no filter)"))

print("Running idea 1: lunch dead zone filter …")
results.append(_run("1. Lunch filter (11–12:30 CT)", FVG_LUNCH_FILTER_ENABLED=True))

print("Running idea 2: VWAP rubber band (>=12 pts) …")
results.append(_run("2. VWAP rubber band (>=12 pts)", FVG_VWAP_RUBBER_BAND_ENABLED=True, FVG_VWAP_MIN_EXTENSION_PTS=12.0))

print("Running idea 3: candle body quality (>=60%) …")
results.append(_run("3. Body quality (>=60%)", FVG_BODY_QUALITY_ENABLED=True, FVG_BODY_MIN_PCT=0.60))

print("Running idea 4: liquidity sweep required …")
results.append(_run("4. Sweep required", FVG_SWEEP_REQUIRED=True))

print("Running idea 5: overlapping FVG zones …")
results.append(_run("5. Overlap required", FVG_OVERLAP_REQUIRED=True))

# ── print comparison table ────────────────────────────────────────────────────

base = results[0]

print()
print("=" * 82)
print(f"{'Filter':<38} {'Trades':>7} {'  vs base':>9} {'WR%':>6} {'Net P&L':>10} {'Avg/trade':>10}")
print("-" * 82)
for r in results:
    delta_trades = r["trades"] - base["trades"]
    delta_str    = f"{delta_trades:+d}" if delta_trades != 0 else "—"
    delta_net    = r["net"] - base["net"]
    net_str      = f"${r['net']:,.0f}"
    delta_net_str = f"({delta_net:+,.0f})" if delta_net != 0 else ""
    print(
        f"{r['label']:<38} {r['trades']:>7} {delta_str:>9} {r['wr']:>5.1f}%"
        f" {net_str:>10} {r['avg']:>8.1f}  {delta_net_str}"
    )
print("=" * 82)
