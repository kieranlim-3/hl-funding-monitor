"""
Hyperliquid Funding Rate Analysis
Run anytime to get a full report from your funding_log.csv

Usage:
    python hl_funding_analysis.py
"""

import csv
import os
from datetime import datetime, timezone
from collections import defaultdict

LOG_FILE = "funding_log.csv"
DEPLOY_CAPITAL = 10000  # USDC — change to your actual amount
TAKER_FEE = 0.00035
ENTRY_EXIT = TAKER_FEE * 2

# ── Load CSV ──────────────────────────────────────────────────────────────────

def load_data():
    if not os.path.exists(LOG_FILE):
        print(f"[ERROR] {LOG_FILE} not found.")
        return None

    rows = []
    with open(LOG_FILE, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row["funding_8h"]  = float(row["funding_8h"])
            row["funding_apy"] = float(row["funding_apy"])
            row["mark_px"]     = float(row["mark_px"])
            row["oi_usd"]      = float(row["oi_usd"]) if row.get("oi_usd") else 0.0

            # New columns — graceful fallback for old CSV rows
            raw_delta = row.get("oi_delta_pct", "")
            row["oi_delta_pct"]      = float(raw_delta) if raw_delta not in ("", None) else None
            row["oi_surge"]          = row.get("oi_surge", "False") == "True"
            row["trending_negative"] = row.get("trending_negative", "False") == "True"
            rows.append(row)

    if not rows:
        print("[ERROR] CSV is empty.")
        return None
    return rows

# ── Stats helpers ─────────────────────────────────────────────────────────────

def mean(values):
    return sum(values) / len(values) if values else 0

def median(values):
    if not values:
        return 0
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

def stdev(values):
    if len(values) < 2:
        return 0
    m = mean(values)
    return (sum((x - m) ** 2 for x in values) / (len(values) - 1)) ** 0.5

# ── Analysis ──────────────────────────────────────────────────────────────────

def analyse(rows):
    by_coin = defaultdict(list)
    for row in rows:
        by_coin[row["coin"]].append(row)

    results = {}

    for coin, data in sorted(by_coin.items()):
        rates    = [r["funding_apy"] for r in data]
        rates8   = [r["funding_8h"]  for r in data]
        prices   = [r["mark_px"]     for r in data]
        oi_usds  = [r["oi_usd"]      for r in data]

        # ── OI delta stats ────────────────────────────────────────────────────
        oi_deltas   = [r["oi_delta_pct"] for r in data if r["oi_delta_pct"] is not None]
        n_oi_surge  = sum(1 for r in data if r["oi_surge"])
        n_trending  = sum(1 for r in data if r["trending_negative"])

        # ── Squeeze co-occurrence: negative funding AND OI surge ──────────────
        squeeze_events = [
            r for r in data
            if r["funding_apy"] < -0.15 and r["oi_surge"]
        ]

        # ── Funding stats ─────────────────────────────────────────────────────
        n_positive   = sum(1 for r in rates if r > 0)
        n_negative   = sum(1 for r in rates if r < 0)
        n_above_15   = sum(1 for r in rates if r > 0.15)
        n_above_30   = sum(1 for r in rates if r > 0.30)

        avg_apy = mean(rates)
        med_apy = median(rates)
        max_apy = max(rates)
        min_apy = min(rates)
        vol_apy = stdev(rates)

        # ── Simulated P&L ─────────────────────────────────────────────────────
        # Strategy: enter long perp when APY < -15%, exit when APY turns positive
        # Squeeze variant: also enter on squeeze_event (neg funding + OI surge)
        simulated_pnl        = 0
        squeeze_pnl          = 0
        in_position          = False
        in_squeeze_position  = False
        entries              = 0
        squeeze_entries      = 0

        for r in data:
            apy = r["funding_apy"]
            f8h = r["funding_8h"]

            # --- Standard neg funding strategy ---
            if not in_position and apy < -0.15:
                in_position = True
                entries += 1
                simulated_pnl -= ENTRY_EXIT * DEPLOY_CAPITAL
            if in_position:
                if apy >= 0:
                    in_position = False
                    simulated_pnl -= ENTRY_EXIT * DEPLOY_CAPITAL
                else:
                    simulated_pnl += abs(f8h) * DEPLOY_CAPITAL  # collect funding

            # --- Squeeze setup strategy ---
            if not in_squeeze_position and r["oi_surge"] and apy < -0.15:
                in_squeeze_position = True
                squeeze_entries += 1
                squeeze_pnl -= ENTRY_EXIT * DEPLOY_CAPITAL
            if in_squeeze_position:
                if apy >= 0:
                    in_squeeze_position = False
                    squeeze_pnl -= ENTRY_EXIT * DEPLOY_CAPITAL
                else:
                    squeeze_pnl += abs(f8h) * DEPLOY_CAPITAL

        # Close open positions at end
        if in_position:
            simulated_pnl -= ENTRY_EXIT * DEPLOY_CAPITAL
        if in_squeeze_position:
            squeeze_pnl -= ENTRY_EXIT * DEPLOY_CAPITAL

        results[coin] = {
            "n_readings":       len(data),
            "first":            data[0]["timestamp"],
            "last":             data[-1]["timestamp"],
            "avg_apy":          avg_apy,
            "med_apy":          med_apy,
            "max_apy":          max_apy,
            "min_apy":          min_apy,
            "vol_apy":          vol_apy,
            "pct_positive":     n_positive / len(rates),
            "pct_negative":     n_negative / len(rates),
            "pct_above_15":     n_above_15 / len(rates),
            "pct_above_30":     n_above_30 / len(rates),
            "price_start":      prices[0],
            "price_end":        prices[-1],
            "price_chg":        (prices[-1] - prices[0]) / prices[0] if prices[0] else 0,
            "avg_oi_usd":       mean(oi_usds),
            "avg_oi_delta":     mean(oi_deltas) if oi_deltas else None,
            "n_oi_surge":       n_oi_surge,
            "n_trending_neg":   n_trending,
            "n_squeeze_events": len(squeeze_events),
            "sim_pnl":          simulated_pnl,
            "sim_entries":      entries,
            "squeeze_pnl":      squeeze_pnl,
            "squeeze_entries":  squeeze_entries,
        }

    return results

# ── Print report ──────────────────────────────────────────────────────────────

def print_report(results):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sep = "=" * 65
    thin = "─" * 65

    print(f"\n{sep}")
    print(f"  HYPERLIQUID FUNDING RATE REPORT")
    print(f"  Generated : {now}")
    print(f"  Capital   : ${DEPLOY_CAPITAL:,}")
    print(sep)

    for coin, r in results.items():
        days = r["n_readings"] / 24
        print(f"\n{thin}")
        print(f"  {coin}")
        print(thin)
        print(f"  Data      : {r['first']} → {r['last']}")
        print(f"  Readings  : {r['n_readings']} hrs ({days:.1f} days)")

        print(f"\n  FUNDING RATES (annualised)")
        print(f"    Average    : {r['avg_apy']:+.2%}")
        print(f"    Median     : {r['med_apy']:+.2%}")
        print(f"    Max        : {r['max_apy']:+.2%}")
        print(f"    Min        : {r['min_apy']:+.2%}")
        print(f"    Volatility : {r['vol_apy']:.2%}  (std dev)")

        print(f"\n  RATE DISTRIBUTION")
        print(f"    Positive   : {r['pct_positive']:.0%} of readings")
        print(f"    Negative   : {r['pct_negative']:.0%} of readings")
        print(f"    Above 15%  : {r['pct_above_15']:.0%} of readings")
        print(f"    Above 30%  : {r['pct_above_30']:.0%} of readings")

        print(f"\n  OPEN INTEREST")
        if r["avg_oi_usd"]:
            print(f"    Avg OI     : ${r['avg_oi_usd']/1e6:.1f}M")
        if r["avg_oi_delta"] is not None:
            print(f"    Avg OI Δ   : {r['avg_oi_delta']:+.2%} per reading")
        print(f"    OI Surges  : {r['n_oi_surge']} readings (>5% jump)")
        print(f"    Neg Trend  : {r['n_trending_neg']} readings (3-bar momentum)")
        print(f"    Squeeze    : {r['n_squeeze_events']} events (neg funding + OI surge)")

        print(f"\n  PRICE")
        print(f"    Start      : ${r['price_start']:,.2f}")
        print(f"    End        : ${r['price_end']:,.2f}")
        print(f"    Change     : {r['price_chg']:+.2%}")

        print(f"\n  SIMULATED P&L  (enter when APY < -15%, exit when positive)")
        print(f"    Entries    : {r['sim_entries']}")
        print(f"    Net PnL    : ${r['sim_pnl']:,.2f}  ({r['sim_pnl']/DEPLOY_CAPITAL:.2%})")

        print(f"\n  SQUEEZE STRATEGY P&L  (enter on neg funding + OI surge)")
        print(f"    Entries    : {r['squeeze_entries']}")
        print(f"    Net PnL    : ${r['squeeze_pnl']:,.2f}  ({r['squeeze_pnl']/DEPLOY_CAPITAL:.2%})")

        # Verdict
        avg = r["avg_apy"]
        sq  = r["n_squeeze_events"]
        if avg > 0.25:
            verdict = "STRONG — consistently high funding, worth entering short perp"
        elif avg > 0.10:
            verdict = "DECENT — moderate funding, selective entry"
        elif avg > 0:
            verdict = "WEAK — low funding, fees eat most of yield"
        elif avg < -0.15 and sq > 0:
            verdict = "SQUEEZE CANDIDATE — negative funding with OI surges observed"
        elif avg < 0:
            verdict = "NEGATIVE BIAS — monitor for long perp entries"
        else:
            verdict = "NEUTRAL"

        print(f"\n  VERDICT: {verdict}")

    # ── Summary ranking ───────────────────────────────────────────────────────
    print(f"\n{sep}")
    print("  SCREENER SUMMARY")
    print(sep)

    ranked_pos = sorted(results.items(), key=lambda x: x[1]["avg_apy"], reverse=True)
    ranked_neg = sorted(results.items(), key=lambda x: x[1]["avg_apy"])
    ranked_sq  = sorted(results.items(), key=lambda x: x[1]["n_squeeze_events"], reverse=True)

    print(f"\n  Best avg positive funding (short perp candidates):")
    for coin, r in ranked_pos[:5]:
        print(f"    {coin:8s}  avg APY {r['avg_apy']:+.2%}")

    print(f"\n  Best avg negative funding (long perp / squeeze candidates):")
    for coin, r in ranked_neg[:5]:
        print(f"    {coin:8s}  avg APY {r['avg_apy']:+.2%}  squeeze events: {r['n_squeeze_events']}")

    print(f"\n  Most squeeze events observed:")
    for coin, r in ranked_sq[:5]:
        if r["n_squeeze_events"] > 0:
            print(f"    {coin:8s}  {r['n_squeeze_events']} events")

    print(f"\n{sep}\n")

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    rows = load_data()
    if not rows:
        return
    results = analyse(rows)
    print_report(results)

if __name__ == "__main__":
    main()
