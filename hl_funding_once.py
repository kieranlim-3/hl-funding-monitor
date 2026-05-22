"""
Hyperliquid Funding Rate Monitor — GitHub Actions version
Runs once, appends to funding_log.csv, sends Telegram update, then exits.
Triggered every 15 mins by GitHub Actions cron.
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

THRESHOLD_NEG_CONSIDER = -0.15  # -15% APY to consider long perp
THRESHOLD_NEG_STRONG   = -0.30  # -30% APY strong signal

HL_API = "https://api.hyperliquid.xyz/info"

# Telegram
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# ── Spot target prices ────────────────────────────────────────────────────────
SPOT_TARGETS = {
    "BTC":  40000,
    "ETH":  1200,
    "HYPE": 40,
}
NEAR_TARGET_PCT = 0.20  # alert when within 20% of target

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
    if funding_apy < THRESHOLD_NEG_STRONG:
        return "VERY NEGATIVE 🟢 LONG PERP!", True
    elif funding_apy < THRESHOLD_NEG_CONSIDER:
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

# ── Price alert ───────────────────────────────────────────────────────────────
def evaluate_price(coin, mark_px):
    if coin not in SPOT_TARGETS:
        return None
    target    = SPOT_TARGETS[coin]
    near_line = target * (1 + NEAR_TARGET_PCT)
    pct_away  = (mark_px - target) / target * 100

    if mark_px <= target:
        return (
            f"🎯 <b>{coin} HIT TARGET!</b>\n"
            f"Mark: ${mark_px:,.2f} | Target: ${target:,}\n"
            f"Consider buying spot on Coinhako now.\n"
            f"Tranche 1 entry."
        )
    elif mark_px <= near_line:
        return (
            f"👀 <b>{coin} approaching target</b>\n"
            f"Mark: ${mark_px:,.2f} | Target: ${target:,} ({pct_away:.1f}% away)\n"
            f"Prepare dry powder — getting close."
        )
    return None

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
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    hl  = fetch_hl_rates()

    if not hl:
        send_telegram("⚠️ HL Monitor: Failed to fetch funding rates.")
        return

    init_csv()

    lines        = [f"<b>📊 HL Funding — {now} UTC</b>\n"]
    alerts       = []
    price_alerts = []

    print(f"[{now}]")
    for coin in COINS:
        if coin not in hl:
            continue

        d              = hl[coin]
        signal, notify = evaluate_signal(d["funding_apy"])
        append_row(now, coin, d, signal)

        target   = SPOT_TARGETS.get(coin, 0)
        pct_away = (d["mark_px"] - target) / target * 100

        print(
            f"  {coin:4s} | "
            f"8h: {d['funding_8h']:+.4%} | "
            f"APY: {d['funding_apy']:+.2%} | "
            f"Mark: ${d['mark_px']:,.2f} | "
            f"Target: ${target:,} ({pct_away:+.1f}%) | "
            f"{signal}"
        )

        lines.append(
            f"<b>{coin}</b> | APY: {d['funding_apy']:+.2%} | "
            f"${d['mark_px']:,.2f} ({pct_away:+.1f}% to target)\n{signal}"
        )

        # Funding alerts
        if notify:
            if d["funding_apy"] < THRESHOLD_NEG_CONSIDER:
                alerts.append(
                    f"🟢 <b>{coin}</b> funding NEGATIVE on HL!\n"
                    f"APY: {d['funding_apy']:+.2%} | Mark: ${d['mark_px']:,.2f}\n"
                    f"Long perp on HL to collect funding.\n"
                    f"Consider accumulating spot on Coinhako."
                )
            else:
                alerts.append(
                    f"🚨 <b>{coin}</b> funding ATTRACTIVE on HL!\n"
                    f"APY: {d['funding_apy']:+.2%} | Mark: ${d['mark_px']:,.2f}\n"
                    f"Short perp on HL, buy spot on Coinhako."
                )

        # Price alerts
        price_msg = evaluate_price(coin, d["mark_px"])
        if price_msg:
            price_alerts.append(price_msg)

    if not alerts and not price_alerts:
        lines.append("\n<i>No positions recommended right now.</i>")

    send_telegram("\n\n".join(lines))
    for alert in alerts:
        send_telegram(alert)
    for alert in price_alerts:
        send_telegram(alert)

    print(f"Appended to {LOG_FILE}")

if __name__ == "__main__":
    main()
