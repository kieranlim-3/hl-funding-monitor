"""
Hyperliquid Funding Rate Analysis
Run this anytime to get a summary report from your funding_log.csv

Usage:
    python hl_funding_analysis.py
"""

import csv
import os
from datetime import datetime, timezone
from collections import defaultdict

LOG_FILE       = "funding_log.csv"
DEPLOY_CAPITAL = 10000   # USDC — change this to your actual amount
TAKER_FEE      = 0.00035 # per side
ENTRY_EXIT     = TAKER_FEE * 2

# ── Load CSV ──────────────────────────────────────────────────────────────────
def load_data():
    if not os.path.exists(LOG_FILE):
        print(f"[ERROR] {LOG_FILE} not found. Run hl_funding_monitor.py first.")
        return None

    rows = []
    with open(LOG_FILE, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row["funding_8h"]  = float(row["funding_8h"])
            row["funding_apy"] = float(row["funding_apy"])
            row["mark_px"]     = float(row["mark_px"])
            rows.append(row)

    if not rows:
        print("[ERROR] CSV is empty. Wait for monitor to collect some data.")
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
    return (s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2)

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

    coins = sorted(by_coin.keys())
    results = {}

    for coin in coins:
        data   = by_coin[coin]
        rates  = [r["funding_apy"] for r in data]
        rates8 = [r["funding_8h"]  for r in data]
        prices = [r["mark_px"]     for r in data]

        n_positive  = sum(1 for r in rates if r > 0)
        n_negative  = sum(1 for r in rates if r < 0)
        n_above_15  = sum(1 for r in rates if r > 0.15)
        n_above_30  = sum(1 for r in rates if r > 0.30)

        avg_apy     = mean(rates)
        med_apy     = median(rates)
        max_apy     = max(rates)
        min_apy     = min(rates)
        vol_apy     = stdev(rates)

        # Simulated yield: sum of 8h funding payments above fee threshold
        # Assumes you enter only when APY > 15% and exit when negative
        simulated_pnl = 0
        in_position   = False
        entries       = 0
        for r in data:
            apy = r["funding_apy"]
            f8h = r["funding_8h"]
            if not in_position and apy > 0.15:
                in_position = True
                entries += 1
                simulated_pnl -= ENTRY_EXIT * DEPLOY_CAPITAL  # entry cost
            if in_position:
                if apy < 0:
                    in_position = False
                    simulated_pnl -= ENTRY_EXIT * DEPLOY_CAPITAL  # exit cost
                else:
                    simulated_pnl += f8h * DEPLOY_CAPITAL  # collect funding

        # Close any open position at end
        if in_position:
            simulated_pnl -= ENTRY_EXIT * DEPLOY_CAPITAL

        # Time coverage
        first = data[0]["timestamp"]
        last  = data[-1]["timestamp"]
        n_hrs = len(data)

        results[coin] = {
            "n_readings":    n_hrs,
            "first":         first,
            "last":          last,
            "avg_apy":       avg_apy,
            "med_apy":       med_apy,
            "max_apy":       max_apy,
            "min_apy":       min_apy,
            "vol_apy":       vol_apy,
            "pct_positive":  n_positive / len(rates),
            "pct_negative":  n_negative / len(rates),
            "pct_above_15":  n_above_15 / len(rates),
            "pct_above_30":  n_above_30 / len(rates),
            "price_start":   prices[0],
            "price_end":     prices[-1],
            "price_chg":     (prices[-1] - prices[0]) / prices[0],
            "sim_pnl":       simulated_pnl,
            "sim_entries":   entries,
        }

    return results

# ── Print report ──────────────────────────────────────────────────────────────
def print_report(results):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print("\n" + "=" * 60)
    print("  HYPERLIQUID FUNDING RATE REPORT")
    print(f"  Generated: {now}")
    print(f"  Simulated capital: ${DEPLOY_CAPITAL:,}")
    print("=" * 60)

    for coin, r in results.items():
        days = r["n_readings"] / 24
        print(f"\n{'─'*60}")
        print(f"  {coin}")
        print(f"{'─'*60}")
        print(f"  Data:         {r['first']} → {r['last']}")
        print(f"  Readings:     {r['n_readings']} hrs ({days:.1f} days)")
        print()
        print(f"  FUNDING RATES (annualised)")
        print(f"    Average:    {r['avg_apy']:+.2%}")
        print(f"    Median:     {r['med_apy']:+.2%}")
        print(f"    Max:        {r['max_apy']:+.2%}")
        print(f"    Min:        {r['min_apy']:+.2%}")
        print(f"    Volatility: {r['vol_apy']:.2%}  (std dev)")
        print()
        print(f"  RATE DISTRIBUTION")
        print(f"    Positive:   {r['pct_positive']:.0%} of readings")
        print(f"    Negative:   {r['pct_negative']:.0%} of readings")
        print(f"    Above 15%:  {r['pct_above_15']:.0%} of readings")
        print(f"    Above 30%:  {r['pct_above_30']:.0%} of readings")
        print()
        print(f"  PRICE")
        print(f"    Start:      ${r['price_start']:,.2f}")
        print(f"    End:        ${r['price_end']:,.2f}")
        print(f"    Change:     {r['price_chg']:+.2%}")
        print()
        print(f"  SIMULATED P&L (enter >15% APY, exit when negative)")
        print(f"    Trades:     {r['sim_entries']} entries")
        print(f"    Net PnL:    ${r['sim_pnl']:,.2f}  on ${DEPLOY_CAPITAL:,}")
        print(f"    Net yield:  {r['sim_pnl']/DEPLOY_CAPITAL:.2%}")

        # Verdict
        avg = r["avg_apy"]
        if avg > 0.25:
            verdict = "STRONG — consistently high funding, worth entering"
        elif avg > 0.10:
            verdict = "DECENT — moderate funding, selective entry"
        elif avg > 0:
            verdict = "WEAK — low funding, fees eat most of the yield"
        else:
            verdict = "AVOID — negative average funding"

        print()
        print(f"  VERDICT: {verdict}")

    # Best coin recommendation
    print(f"\n{'='*60}")
    print("  RECOMMENDATION")
    print(f"{'='*60}")
    best = max(results.items(), key=lambda x: x[1]["avg_apy"])
    print(f"  Best avg funding: {best[0]} at {best[1]['avg_apy']:+.2%} APY")
    ranked = sorted(results.items(), key=lambda x: x[1]["avg_apy"], reverse=True)
    print(f"  Ranking: {' > '.join(c for c, _ in ranked)}")
    print(f"\n  Start with {best[0]} for your first position.")
    print("=" * 60 + "\n")

# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    rows = load_data()
    if not rows:
        return
    results = analyse(rows)
    print_report(results)

if __name__ == "__main__":
    main()
