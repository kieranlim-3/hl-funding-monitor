"""
Hyperliquid Funding Rate + Day Trading Signal + HMM Regime Monitor
GitHub Actions version — runs every 15 mins.

Three layers:
  Layer 1 — Funding arb signals (passive, Strategy 1)
  Layer 2 — OI + price momentum setup signals (active, Strategy 2)
  Layer 3 — HMM regime detection (filter — only show signals in right regime)
"""

import requests
import csv
import os
import json
import numpy as np
from datetime import datetime, timezone
from hmmlearn.hmm import GaussianHMM

# ── Config ────────────────────────────────────────────────────────────────────
COINS         = ["BTC", "ETH", "HYPE"]
LOG_FILE      = "funding_log.csv"
OI_FILE       = "oi_log.json"
PRICE_FILE    = "price_history.json"  # stores price history for HMM training

TAKER_FEE     = 0.00035
ENTRY_EXIT    = TAKER_FEE * 2

# Funding thresholds
THRESHOLD_LOW            = 0.05
THRESHOLD_MEDIUM         = 0.15
THRESHOLD_HIGH           = 0.30
THRESHOLD_NEG_CONSIDER   = -0.15
THRESHOLD_NEG_STRONG     = -0.30

# Day trading thresholds
PRICE_MOVE_THRESHOLD     = 0.01
OI_CHANGE_THRESHOLD      = 0.02

# HMM config
HMM_STATES        = 3    # trending up, ranging, trending down
HMM_MIN_HISTORY   = 30   # minimum readings before HMM kicks in

HL_API = "https://api.hyperliquid.xyz/info"

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

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
                    "funding_8h":    funding_8h,
                    "funding_apy":   funding_8h * 3 * 365,
                    "mark_px":       float(ctx["markPx"]),
                    "open_interest": float(ctx["openInterest"]),
                }
        return result
    except Exception as e:
        print(f"[ERROR] HL fetch failed: {e}")
        return {}

# ── Persistence ───────────────────────────────────────────────────────────────
def load_json(filepath):
    if not os.path.exists(filepath):
        return {}
    try:
        with open(filepath, "r") as f:
            return json.load(f)
    except:
        return {}

def save_json(filepath, data):
    with open(filepath, "w") as f:
        json.dump(data, f)

# ── Price history management ──────────────────────────────────────────────────
def update_price_history(history, coin, price, max_len=200):
    if coin not in history:
        history[coin] = []
    history[coin].append(price)
    if len(history[coin]) > max_len:
        history[coin] = history[coin][-max_len:]
    return history

# ── HMM Regime Detection ──────────────────────────────────────────────────────
REGIME_LABELS = {
    0: ("TRENDING UP 📈",   "bullish"),
    1: ("RANGING ↔️",       "neutral"),
    2: ("TRENDING DOWN 📉", "bearish"),
}

def detect_regime(prices):
    """
    Fits a 3-state Gaussian HMM on log returns.
    Returns (regime_label, regime_type, confidence)
    States are relabelled by mean return: highest = trending up, lowest = trending down.
    """
    if len(prices) < HMM_MIN_HISTORY:
        return "INSUFFICIENT DATA ⏳", "unknown", 0.0

    prices_arr = np.array(prices)
    log_returns = np.diff(np.log(prices_arr)).reshape(-1, 1)

    try:
        model = GaussianHMM(
            n_components=HMM_STATES,
            covariance_type="full",
            n_iter=100,
            random_state=42
        )
        model.fit(log_returns)

        # Predict current regime
        hidden_states = model.predict(log_returns)
        current_state = hidden_states[-1]

        # Relabel states by mean return (ascending)
        means = model.means_.flatten()
        state_order = np.argsort(means)  # lowest to highest mean return
        # state_order[0] = trending down, [1] = ranging, [2] = trending up
        rank_map = {state_order[2]: 0, state_order[1]: 1, state_order[0]: 2}
        ranked_state = rank_map[current_state]

        # Confidence = posterior probability of current state
        log_posteriors = model.predict_proba(log_returns)
        confidence = log_posteriors[-1][current_state]

        label, regime_type = REGIME_LABELS[ranked_state]
        return label, regime_type, confidence

    except Exception as e:
        print(f"[WARN] HMM failed: {e}")
        return "HMM ERROR ⚠️", "unknown", 0.0

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

# ── Day trading setup signal ──────────────────────────────────────────────────
def evaluate_setup(coin, current, prev, regime_type):
    if not prev:
        return None, False

    price_now  = current["mark_px"]
    price_prev = prev.get("mark_px", price_now)
    oi_now     = current["open_interest"]
    oi_prev    = prev.get("open_interest", oi_now)
    funding    = current["funding_apy"]

    price_chg = (price_now - price_prev) / price_prev
    oi_chg    = (oi_now - oi_prev) / oi_prev if oi_prev else 0

    price_notable = abs(price_chg) >= PRICE_MOVE_THRESHOLD
    oi_notable    = abs(oi_chg) >= OI_CHANGE_THRESHOLD

    if not price_notable and not oi_notable:
        return None, False

    signal = None
    alert  = False

    if price_chg < -PRICE_MOVE_THRESHOLD and oi_chg < -OI_CHANGE_THRESHOLD:
        quality = "⭐⭐⭐" if funding < 0 and regime_type != "bearish" else "⭐⭐"
        regime_note = "✅ regime confirms" if regime_type in ["bullish", "neutral"] else "⚠️ regime bearish — caution"
        signal = (
            f"📉➡️📈 <b>{coin} BOUNCE SETUP {quality}</b>\n"
            f"Price: {price_chg:+.2%} | OI: {oi_chg:+.2%} (longs washed out)\n"
            f"Funding: {funding:+.2%} APY\n"
            f"Regime: {regime_note}\n"
            f"<i>Check 15min chart for entry</i>"
        )
        alert = True

    elif price_chg > PRICE_MOVE_THRESHOLD and oi_chg > OI_CHANGE_THRESHOLD:
        quality = "⭐⭐⭐" if regime_type == "bullish" else "⭐⭐"
        regime_note = "✅ regime confirms" if regime_type == "bullish" else "⚠️ counter-trend — caution"
        signal = (
            f"📈 <b>{coin} MOMENTUM SETUP {quality}</b>\n"
            f"Price: {price_chg:+.2%} | OI: {oi_chg:+.2%} (new longs entering)\n"
            f"Funding: {funding:+.2%} APY\n"
            f"Regime: {regime_note}\n"
            f"<i>Check 15min chart for pullback entry</i>"
        )
        alert = True

    elif price_chg > PRICE_MOVE_THRESHOLD and oi_chg < -OI_CHANGE_THRESHOLD:
        signal = (
            f"⚠️ <b>{coin} WEAK RALLY</b>\n"
            f"Price: {price_chg:+.2%} | OI: {oi_chg:+.2%} (shorts covering only)\n"
            f"Funding: {funding:+.2%} APY\n"
            f"<i>No new conviction — avoid chasing</i>"
        )
        alert = True

    elif price_chg < -PRICE_MOVE_THRESHOLD and oi_chg > OI_CHANGE_THRESHOLD:
        signal = (
            f"📉 <b>{coin} BEARISH CONTINUATION</b>\n"
            f"Price: {price_chg:+.2%} | OI: {oi_chg:+.2%} (new shorts entering)\n"
            f"Funding: {funding:+.2%} APY\n"
            f"<i>Wait for flush before longing</i>"
        )
        alert = True

    elif oi_notable and not price_notable:
        direction = "building longs" if oi_chg > 0 else "reducing longs"
        signal = (
            f"👁 <b>{coin} QUIET POSITIONING</b>\n"
            f"Price: {price_chg:+.2%} | OI: {oi_chg:+.2%} ({direction})\n"
            f"Funding: {funding:+.2%} APY\n"
            f"<i>Watch closely</i>"
        )
        alert = True

    return signal, alert

# ── Price target alert ────────────────────────────────────────────────────────
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
                "mark_px", "open_interest",
                "regime", "signal"
            ])

def append_row(timestamp, coin, data, regime, signal):
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            timestamp, coin,
            f"{data['funding_8h']:.6f}",
            f"{data['funding_apy']:.4f}",
            f"{data['mark_px']:.4f}",
            f"{data['open_interest']:.2f}",
            regime,
            signal
        ])

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now           = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    hl            = fetch_hl_rates()
    prev_data     = load_json(OI_FILE)
    price_history = load_json(PRICE_FILE)

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

        d    = hl[coin]
        prev = prev_data.get(coin, {})

        # Update price history and detect regime
        price_history = update_price_history(price_history, coin, d["mark_px"])
        regime_label, regime_type, confidence = detect_regime(price_history.get(coin, []))

        # Signals
        funding_signal, funding_notify        = evaluate_funding(d["funding_apy"])
        setup_signal, setup_alert             = evaluate_setup(coin, d, prev, regime_type)

        append_row(now, coin, d, regime_label, funding_signal)

        target   = SPOT_TARGETS.get(coin, 0)
        pct_away = (d["mark_px"] - target) / target * 100
        oi_chg   = (d["open_interest"] - prev.get("open_interest", d["open_interest"])) / prev.get("open_interest", d["open_interest"]) * 100 if prev else 0
        conf_str = f"{confidence:.0%}" if confidence > 0 else "—"

        print(
            f"  {coin:4s} | "
            f"APY: {d['funding_apy']:+.2%} | "
            f"Mark: ${d['mark_px']:,.2f} | "
            f"OI: {oi_chg:+.1f}% | "
            f"Regime: {regime_label} ({conf_str}) | "
            f"{funding_signal}"
        )

        lines.append(
            f"<b>{coin}</b> | APY: {d['funding_apy']:+.2%} | ${d['mark_px']:,.2f} ({pct_away:+.1f}% to target)\n"
            f"OI: {oi_chg:+.1f}% | Regime: {regime_label} {conf_str}\n"
            f"{funding_signal}"
        )

        if funding_notify:
            if d["funding_apy"] < THRESHOLD_NEG_CONSIDER:
                alerts.append(
                    f"🟢 <b>{coin}</b> funding NEGATIVE!\n"
                    f"APY: {d['funding_apy']:+.2%} | Mark: ${d['mark_px']:,.2f}\n"
                    f"Regime: {regime_label}\n"
                    f"Long perp on HL to collect funding."
                )
            else:
                alerts.append(
                    f"🚨 <b>{coin}</b> funding ATTRACTIVE!\n"
                    f"APY: {d['funding_apy']:+.2%} | Mark: ${d['mark_px']:,.2f}\n"
                    f"Regime: {regime_label}\n"
                    f"Short perp on HL, buy spot on Coinhako."
                )

        if setup_alert and setup_signal:
            setup_alerts.append(setup_signal)

        price_msg = evaluate_price(coin, d["mark_px"])
        if price_msg:
            price_alerts.append(price_msg)

    if not alerts and not price_alerts and not setup_alerts:
        lines.append("\n<i>No signals right now.</i>")

    # Save state
    save_json(OI_FILE, {coin: hl[coin] for coin in COINS if coin in hl})
    save_json(PRICE_FILE, price_history)

    # Send
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
