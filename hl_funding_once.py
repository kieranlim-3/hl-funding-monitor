"""
Hyperliquid Funding Rate + Day Trading Signal Monitor
GitHub Actions version — runs every 15 mins.

Two strategies:
  Strategy 1 (passive)  — funding arb, spot price targets
  Strategy 2 (active)   — short term setup signals via OI + funding + price momentum
"""

import requests
import csv
import os
import json
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
COINS         = ["BTC", "ETH", "HYPE"]
LOG_FILE      = "funding_log.csv"
OI_FILE       = "oi_log.json"   # stores previous OI for comparison
TAKER_FEE     = 0.00035
ENTRY_EXIT    = TAKER_FEE * 2

# Funding thresholds (annualised)
THRESHOLD_LOW            = 0.05
THRESHOLD_MEDIUM         = 0.15
THRESHOLD_HIGH           = 0.30
THRESHOLD_NEG_CONSIDER   = -0.15
THRESHOLD_NEG_STRONG     = -0.30

# Day trading signal thresholds
PRICE_MOVE_THRESHOLD     = 0.01    # 1% price move in 15 mins = notable
OI_CHANGE_THRESHOLD      = 0.02    # 2% OI change in 15 mins = notable

HL_API = "https://api.hyperliquid.xyz/info"

# Telegram
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Spot targets
SPOT_TARGETS = {
    "BTC":  40000,
    "ETH":  1200,
    "HYPE": 40,
}
NEAR_TARGET_PCT = 0.20

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
                    "open_interest": float(ctx["openInterest"]),
                }
        return result
    except Exception as e:
        print(f"[ERROR] HL fetch failed: {e}")
        return {}

# ── OI history (persist between runs via JSON) ────────────────────────────────
def load_prev_data():
    if not os.path.exists(OI_FILE):
        return {}
    try:
        with open(OI_FILE, "r") as f:
            return json.load(f)
    except:
        return {}

def save_prev_data(data):
    with open(OI_FILE, "w") as f:
        json.dump(data, f)

# ── Funding signal ────────────────────────────────────────────────────────────
def evaluate_funding(funding_apy):
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

# ── Day trading signal ────────────────────────────────────────────────────────
def evaluate_setup(coin, current, prev):
    """
    Combines OI change + price move + funding to flag setups.
    Returns (signal_text, should_alert) or (None, False)
    """
    if not prev:
        return None, False

    price_now  = current["mark_px"]
    price_prev = prev.get("mark_px", price_now)
    oi_now     = current["open_interest"]
    oi_prev    = prev.get("open_interest", oi_now)
    funding    = current["funding_apy"]

    price_chg = (price_now - price_prev) / price_prev  # % move this period
    oi_chg    = (oi_now - oi_prev) / oi_prev if oi_prev else 0

    # Need meaningful moves to signal
    price_notable = abs(price_chg) >= PRICE_MOVE_THRESHOLD
    oi_notable    = abs(oi_chg) >= OI_CHANGE_THRESHOLD

    if not price_notable and not oi_notable:
        return None, False

    # Classify the setup
    signal = None
    alert  = False

    # ── Bullish setups ──
    if price_chg < -PRICE_MOVE_THRESHOLD and oi_chg < -OI_CHANGE_THRESHOLD:
        # Price down + OI down = longs closing/liquidated = exhaustion
        # Best bounce setup, especially if funding also negative
        quality = "⭐⭐⭐" if funding < 0 else "⭐⭐"
        signal  = (
            f"📉➡️📈 <b>{coin} BOUNCE SETUP {quality}</b>\n"
            f"Price: {price_chg:+.2%} | OI: {oi_chg:+.2%} (longs washed out)\n"
            f"Funding: {funding:+.2%} APY\n"
            f"<i>Long exhaustion — check 15min chart for entry</i>"
        )
        alert = True

    elif price_chg > PRICE_MOVE_THRESHOLD and oi_chg > OI_CHANGE_THRESHOLD:
        # Price up + OI up = new longs entering = momentum
        quality = "⭐⭐⭐" if funding < THRESHOLD_MEDIUM else "⭐⭐"
        signal  = (
            f"📈 <b>{coin} MOMENTUM SETUP {quality}</b>\n"
            f"Price: {price_chg:+.2%} | OI: {oi_chg:+.2%} (new longs entering)\n"
            f"Funding: {funding:+.2%} APY\n"
            f"<i>Trend continuation — check 15min chart for pullback entry</i>"
        )
        alert = True

    elif price_chg > PRICE_MOVE_THRESHOLD and oi_chg < -OI_CHANGE_THRESHOLD:
        # Price up + OI down = shorts covering, not new longs = weak move
        signal  = (
            f"⚠️ <b>{coin} WEAK RALLY</b>\n"
            f"Price: {price_chg:+.2%} | OI: {oi_chg:+.2%} (shorts covering only)\n"
            f"Funding: {funding:+.2%} APY\n"
            f"<i>No new conviction — potential reversal, avoid chasing</i>"
        )
        alert = True

    elif price_chg < -PRICE_MOVE_THRESHOLD and oi_chg > OI_CHANGE_THRESHOLD:
        # Price down + OI up = new shorts entering = bearish continuation
        signal  = (
            f"📉 <b>{coin} BEARISH CONTINUATION</b>\n"
            f"Price: {price_chg:+.2%} | OI: {oi_chg:+.2%} (new shorts entering)\n"
            f"Funding: {funding:+.2%} APY\n"
            f"<i>Trend down with conviction — wait for flush before longing</i>"
        )
        alert = True

    elif oi_notable and not price_notable:
        # Large OI change without price move = positioning quietly
        direction = "building longs" if oi_chg > 0 else "reducing longs"
        signal    = (
            f"👁 <b>{coin} QUIET POSITIONING</b>\n"
            f"Price: {price_chg:+.2%} | OI: {oi_chg:+.2%} ({direction})\n"
            f"Funding: {funding:+.2%} APY\n"
            f"<i>Market positioning without price move — watch closely</i>"
        )
        alert = True

    return signal, alert

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
                "mark_px", "open_interest", "signal"
            ])

def append_row(timestamp, coin, data, signal):
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            timestamp, coin,
            f"{data['funding_8h']:.6f}",
            f"{data['funding_apy']:.4f}",
            f"{data['mark_px']:.4f}",
            f"{data['open_interest']:.2f}",
            signal
        ])

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now      = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    hl       = fetch_hl_rates()
    prev_data = load_prev_data()

    if not hl:
        send_telegram("⚠️ HL Monitor: Failed to fetch funding rates.")
        return

    init_csv()

    lines        = [f"<b>📊 HL Funding — {now} UTC</b>\n"]
    alerts       = []
    price_alerts = []
    setup_alerts = []

    print(f"[{now}]")
    for coin in COINS:
        if coin not in hl:
            continue

        d              = hl[coin]
        prev           = prev_data.get(coin, {})
        funding_signal, funding_notify = evaluate_funding(d["funding_apy"])
        setup_signal, setup_alert      = evaluate_setup(coin, d, prev)
        append_row(now, coin, d, funding_signal)

        target   = SPOT_TARGETS.get(coin, 0)
        pct_away = (d["mark_px"] - target) / target * 100
        oi_chg   = (d["open_interest"] - prev.get("open_interest", d["open_interest"])) / prev.get("open_interest", d["open_interest"]) * 100 if prev else 0

        print(
            f"  {coin:4s} | "
            f"APY: {d['funding_apy']:+.2%} | "
            f"Mark: ${d['mark_px']:,.2f} | "
            f"OI chg: {oi_chg:+.1f}% | "
            f"Target: {pct_away:+.1f}% away | "
            f"{funding_signal}"
        )

        lines.append(
            f"<b>{coin}</b> | APY: {d['funding_apy']:+.2%} | "
            f"${d['mark_px']:,.2f} ({pct_away:+.1f}% to target)\n"
            f"OI: {oi_chg:+.1f}% | {funding_signal}"
        )

        # Funding alerts
        if funding_notify:
            if d["funding_apy"] < THRESHOLD_NEG_CONSIDER:
                alerts.append(
                    f"🟢 <b>{coin}</b> funding NEGATIVE!\n"
                    f"APY: {d['funding_apy']:+.2%} | Mark: ${d['mark_px']:,.2f}\n"
                    f"Long perp on HL to collect funding."
                )
            else:
                alerts.append(
                    f"🚨 <b>{coin}</b> funding ATTRACTIVE!\n"
                    f"APY: {d['funding_apy']:+.2%} | Mark: ${d['mark_px']:,.2f}\n"
                    f"Short perp on HL, buy spot on Coinhako."
                )

        # Setup alerts
        if setup_alert and setup_signal:
            setup_alerts.append(setup_signal)

        # Price alerts
        price_msg = evaluate_price(coin, d["mark_px"])
        if price_msg:
            price_alerts.append(price_msg)

    if not alerts and not price_alerts and not setup_alerts:
        lines.append("\n<i>No signals right now.</i>")

    # Save current data for next run comparison
    save_prev_data({coin: hl[coin] for coin in COINS if coin in hl})

    # Send messages
    send_telegram("\n\n".join(lines))
    for alert in setup_alerts:
        send_telegram(alert)
    for alert in alerts:
        send_telegram(alert)
    for alert in price_alerts:
        send_telegram(alert)

    print(f"Appended to {LOG_FILE}")

if __name__ == "__main__":
    main()
