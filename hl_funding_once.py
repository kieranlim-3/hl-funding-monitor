"""
Hyperliquid + Binance Funding Rate Monitor — GitHub Actions version
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

HL_API      = "https://api.hyperliquid.xyz/info"
BINANCE_API = "https://fapi.binance.com/fapi/v1/premiumIndex"

# Telegram
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Binance symbol mapping
BINANCE_SYMBOLS = {
    "BTC":  "BTCUSDT",
    "ETH":  "ETHUSDT",
    "HYPE": "HYPEUSDT",
}

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

# ── Fetch HL ─────────────────────────────────────────────────────────────────
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

# ── Fetch Binance ─────────────────────────────────────────────────────────────
def fetch_binance_rates():
    result = {}
    for coin, symbol in BINANCE_SYMBOLS.items():
        try:
            res = requests.get(
                BINANCE_API,
                params={"symbol": symbol},
                timeout=10
            )
            res.raise_for_status()
            data       = res.json()
            funding_8h = float(data["lastFundingRate"])
            result[coin] = {
                "funding_8h":  funding_8h,
                "funding_apy": funding_8h * 3 * 365,
                "mark_px":     float(data["markPrice"]),
            }
        except Exception as e:
            print(f"[ERROR] Binance fetch failed for {coin}: {e}")
    return result

# ── Signal ────────────────────────────────────────────────────────────────────
def evaluate_signal(funding_apy):
    fee_apy = (ENTRY_EXIT / 30) * 365
    net_apy = funding_apy - fee_apy
    if funding_apy < 0:
        return "NEGATIVE ❌", False
    elif funding_apy < THRESHOLD_LOW:
        return f"TOO LOW (net ~{net_apy:.1%}) ⬇️", False
    elif funding_apy < THRESHOLD_MEDIUM:
        return f"MARGINAL (net ~{net_apy:.1%}) 😐", False
    elif funding_apy < THRESHOLD_HIGH:
        return f"DECENT (net ~{net_apy:.1%}) 👀", True
    else:
        return f"ATTRACTIVE (net ~{net_apy:.1%}) 🚨", True

# ── Best exchange picker ──────────────────────────────────────────────────────
def best_exchange(hl_apy, bn_apy):
    if hl_apy >= bn_apy:
        return "HL", hl_apy
    return "Binance", bn_apy

# ── CSV ───────────────────────────────────────────────────────────────────────
def init_csv():
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp", "coin",
                "hl_funding_8h", "hl_funding_apy",
                "bn_funding_8h", "bn_funding_apy",
                "mark_px", "best_exchange", "signal"
            ])

def append_row(timestamp, coin, hl, bn, mark_px, best_ex, signal):
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            timestamp, coin,
            f"{hl['funding_8h']:.6f}",
            f"{hl['funding_apy']:.4f}",
            f"{bn['funding_8h']:.6f}" if bn else "N/A",
            f"{bn['funding_apy']:.4f}" if bn else "N/A",
            f"{mark_px:.4f}",
            best_ex,
            signal
        ])

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    hl   = fetch_hl_rates()
    bn   = fetch_binance_rates()

    if not hl:
        send_telegram("⚠️ HL Monitor: Failed to fetch HL funding rates.")
        return

    init_csv()

    lines  = [f"<b>📊 Funding Update — {now} UTC</b>\n"]
    alerts = []

    print(f"[{now}]")
    for coin in COINS:
        if coin not in hl:
            continue

        hl_data = hl[coin]
        bn_data = bn.get(coin)
        mark_px = hl_data["mark_px"]

        hl_apy  = hl_data["funding_apy"]
        bn_apy  = bn_data["funding_apy"] if bn_data else 0

        best_ex, best_apy = best_exchange(hl_apy, bn_apy)
        signal, notify    = evaluate_signal(best_apy)

        append_row(now, coin, hl_data, bn_data, mark_px, best_ex, signal)

        # Console
        bn_str = f"BN: {bn_apy:+.2%}" if bn_data else "BN: N/A"
        print(
            f"  {coin:4s} | HL: {hl_apy:+.2%} | {bn_str} | "
            f"Best: {best_ex} {best_apy:+.2%} | Mark: ${mark_px:,.2f} | {signal}"
        )

        # Telegram line
        bn_line = f"Binance: {bn_apy:+.2%}" if bn_data else "Binance: N/A"
        lines.append(
            f"<b>{coin}</b> | ${mark_px:,.2f}\n"
            f"HL: {hl_apy:+.2%} | {bn_line}\n"
            f"Best: <b>{best_ex}</b> → {signal}"
        )

        if notify:
            alerts.append(
                f"🚨 <b>{coin}</b> funding attractive on {best_ex}!\n"
                f"APY: {best_apy:+.2%} | Mark: ${mark_px:,.2f}\n"
                f"Short perp on {best_ex}, buy spot on Coinhako."
            )

    if not alerts:
        lines.append("\n<i>No positions recommended right now.</i>")

    send_telegram("\n\n".join(lines))
    for alert in alerts:
        send_telegram(alert)

    print(f"Appended to {LOG_FILE}")

if __name__ == "__main__":
    main()
