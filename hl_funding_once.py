"""
Hyperliquid Funding Rate Monitor — GitHub Actions version
Runs once, appends to funding_log.csv, then exits.
Triggered every hour by GitHub Actions cron.
"""

import requests
import csv
import os
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
COINS         = ["BTC", "ETH", "HYPE"]
LOG_FILE      = "funding_log.csv"
TAKER_FEE     = 0.00035
ENTRY_EXIT    = TAKER_FEE * 2

THRESHOLD_LOW    = 0.05
THRESHOLD_MEDIUM = 0.15
THRESHOLD_HIGH   = 0.30

HL_API = "https://api.hyperliquid.xyz/info"

# ── Fetch ─────────────────────────────────────────────────────────────────────
def fetch_funding_rates():
    try:
        res = requests.post(
            HL_API,
            json={"type": "metaAndAssetCtxs"},
            timeout=10
        )
        res.raise_for_status()
        data   = res.json()
        assets = data[0]["universe"]
        ctxs   = data[1]

        result = {}
        for i, asset in enumerate(assets):
            if asset["name"] in COINS:
                ctx         = ctxs[i]
                funding_8h  = float(ctx["funding"])
                funding_apy = funding_8h * 3 * 365
                mark_px     = float(ctx["markPx"])
                oi          = float(ctx["openInterest"])
                result[asset["name"]] = {
                    "funding_8h":    funding_8h,
                    "funding_apy":   funding_apy,
                    "mark_px":       mark_px,
                    "open_interest": oi,
                }
        return result

    except Exception as e:
        print(f"[ERROR] API fetch failed: {e}")
        return None

# ── Signal ────────────────────────────────────────────────────────────────────
def evaluate_signal(funding_apy):
    fee_apy = (ENTRY_EXIT / 30) * 365
    net_apy = funding_apy - fee_apy
    if funding_apy < 0:
        return "NEGATIVE"
    elif funding_apy < THRESHOLD_LOW:
        return f"TOO LOW (net ~{net_apy:.1%})"
    elif funding_apy < THRESHOLD_MEDIUM:
        return f"MARGINAL (net ~{net_apy:.1%})"
    elif funding_apy < THRESHOLD_HIGH:
        return f"DECENT (net ~{net_apy:.1%})"
    else:
        return f"ATTRACTIVE (net ~{net_apy:.1%})"

# ── CSV ───────────────────────────────────────────────────────────────────────
def init_csv():
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp", "coin",
                "funding_8h", "funding_apy",
                "mark_px", "open_interest",
                "signal"
            ])

def append_row(timestamp, coin, data, signal):
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            timestamp,
            coin,
            f"{data['funding_8h']:.6f}",
            f"{data['funding_apy']:.4f}",
            f"{data['mark_px']:.4f}",
            f"{data['open_interest']:.2f}",
            signal
        ])

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    rates = fetch_funding_rates()

    if not rates:
        print("No data fetched, exiting.")
        return

    init_csv()

    print(f"[{now}]")
    for coin in COINS:
        if coin not in rates:
            print(f"  {coin}: not found")
            continue

        d      = rates[coin]
        signal = evaluate_signal(d["funding_apy"])
        append_row(now, coin, d, signal)

        print(
            f"  {coin:4s} | "
            f"8h: {d['funding_8h']:+.4%} | "
            f"APY: {d['funding_apy']:+.2%} | "
            f"Mark: ${d['mark_px']:,.2f} | "
            f"{signal}"
        )

    print(f"Appended to {LOG_FILE}")

if __name__ == "__main__":
    main()
