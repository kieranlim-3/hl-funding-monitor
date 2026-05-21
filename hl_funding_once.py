"""
Hyperliquid Funding Rate Monitor — GitHub Actions version
Runs once, appends to funding_log.csv, sends Telegram update, then exits.
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

# Telegram
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram credentials not set, skipping.")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        print(f"[ERROR] Telegram send failed: {e}")

# ── Fetch HL ──────────────────────────────────────────────────────────────────
def fetch_hl_rates():
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
                ctx        = ctxs[i]
                funding_8h = float(ctx["funding"])
                result[asset["name"]] = {
                    "funding_8h":  funding_8h,
                    "funding_apy": funding_8h * 3 * 365,
                    "mark_px":     float(ctx["markPx"]),
                }
        return result
    except Exception as e:
        print(f"[ERROR] HL fetch failed: {e}")
        return {}

# ── Signal ────────────────────────────────────────────────────────────────────
def evaluate_signal(funding_apy):
    fee_apy = (ENTRY_EXIT / 30) * 365
    net_apy = funding_apy - fee_apy
    if funding_apy < -0.10:
        return "VERY NEGATIVE 🟢 LONG PERP!", True
    elif funding_apy < -0.05:
        return f"NEGATIVE (net ~{abs(net_apy):.1%}) 🟢 consider long perp", True
    elif funding_apy < 0:
        return "SLIGHTLY NEGATIVE ⚡ monitor", False
    elif funding_apy < THRESHOLD_LOW:
        return f"TOO LOW (net ~{net_apy:.1%}) ⬇️", False
    elif funding_apy < THRESHOLD_MEDIUM:
        return f"MARGINAL (net ~{net_apy:.1%}) 😐", False
    elif funding_apy < THRESHOLD_HIGH:
        return f"DECENT (net ~{net_apy:.1%}) 👀", True
    else:
        return f"ATTRACTIVE (net ~{net_apy:.1%}) 🚨", True

# ── CSV ───────────────────────────────────────────────────────────────────────
def init_csv():
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp", "coin",
                "funding_8h", "funding_apy",
                "mark_px", "signal"
            ])

def append_row(timestamp, coin, data, signal):
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            timestamp, coin,
            f"{data['funding_8h']:.6f}",
            f"{data['funding_apy']:.4f}",
            f"{data['mark_px']:.4f}",
            signal
        ])

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    hl   = fetch_hl_rates()

    if not hl:
        send_telegram("⚠️ HL Monitor: Failed to fetch funding rates.")
        return

    init_csv()

    lines  = [f"<b>📊 HL Funding — {now} UTC</b>\n"]
    alerts = []

    print(f"[{now}]")
    for coin in COINS:
        if coin not in hl:
            continue

        d              = hl[coin]
        signal, notify = evaluate_signal(d["funding_apy"])
        append_row(now, coin, d, signal)

        print(
            f"  {coin:4s} | "
            f"8h: {d['funding_8h']:+.4%} | "
            f"APY: {d['funding_apy']:+.2%} | "
            f"Mark: ${d['mark_px']:,.2f} | "
            f"{signal}"
        )

        lines.append(
            f"<b>{coin}</b> | APY: {d['funding_apy']:+.2%} | "
            f"${d['mark_px']:,.2f}\n{signal}"
        )

        if notify:
            alerts.append(
                f"🚨 <b>{coin}</b> funding attractive on HL!\n"
                f"APY: {d['funding_apy']:+.2%} | Mark: ${d['mark_px']:,.2f}\n"
                f"Short perp on HL, buy spot on Coinhako."
            )

    if not alerts:
        lines.append("\n<i>No positions recommended right now.</i>")

    send_telegram("\n\n".join(lines))
    for alert in alerts:
        send_telegram(alert)

    print(f"Appended to {LOG_FILE}")

if __name__ == "__main__":
    main()
