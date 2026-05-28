"""
Hyperliquid Funding Rate Monitor — GitHub Actions version
Runs once, appends to funding_log.csv, sends Telegram update, then exits.
Triggered every 15 mins by GitHub Actions cron.

Improvements over v1:
- Dynamic screener across ALL HL perps (not just BTC/ETH/HYPE)
- OI delta signal: flags when OI rising into negative funding (squeeze setup)
- Funding momentum filter: requires 3 consecutive readings trending more negative
- Top N screener by most negative / most positive funding
- OI delta stored in CSV for analysis
- Settlement countdown on every message
"""

import requests
import csv
import os
import json
import time
from datetime import datetime, timezone
from collections import defaultdict

# ── Config ────────────────────────────────────────────────────────────────────

LOG_FILE = "funding_log.csv"
OI_LOG_FILE = "oi_log.json"

TAKER_FEE = 0.00035
ENTRY_EXIT = TAKER_FEE * 2

THRESHOLD_LOW = 0.05
THRESHOLD_MEDIUM = 0.15
THRESHOLD_HIGH = 0.30
THRESHOLD_NEG_CONSIDER = -0.15
THRESHOLD_NEG_STRONG = -0.30

TOP_N = 5
MIN_OI_USD = 1_000_000
OI_SURGE_PCT = 0.05

PINNED_COINS = ["BTC", "ETH", "HYPE"]

SPOT_TARGETS = {
   "BTC": 40000,
   "ETH": 1200,
   "HYPE": 40,
}
NEAR_TARGET_PCT = 0.20

HL_API = "https://api.hyperliquid.xyz/info"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
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
       time.sleep(2)
   except Exception as e:
       print(f"[ERROR] Telegram send failed: {e}")

# ── Settlement countdown ──────────────────────────────────────────────────────

def time_to_settlement(now_dt):
   """Returns hours and minutes until next 8h funding settlement (00, 08, 16 UTC)."""
   hour = now_dt.hour
   minute = now_dt.minute
   next_settlement = ((hour // 8) + 1) * 8
   if next_settlement >= 24:
       next_settlement = 0
   hours_left = (next_settlement - hour - 1)
   mins_left = 60 - minute
   if mins_left == 60:
       mins_left = 0
       hours_left += 1
   return hours_left, mins_left

# ── Fetch ALL HL perps ────────────────────────────────────────────────────────

def fetch_hl_rates():
   try:
       res = requests.post(
           HL_API,
           json={"type": "metaAndAssetCtxs"},
           timeout=15
       )
       res.raise_for_status()
       data = res.json()
       assets = data[0]["universe"]
       ctxs = data[1]

       result = {}
       for i, asset in enumerate(assets):
           ctx = ctxs[i]
           mark_px = float(ctx.get("markPx", 0) or 0)
           oi = float(ctx.get("openInterest", 0) or 0)
           funding_8h = float(ctx.get("funding", 0) or 0)

           if mark_px == 0:
               continue

           oi_usd = oi * mark_px

           result[asset["name"]] = {
               "funding_8h": funding_8h,
               "funding_apy": funding_8h * 3 * 365,
               "mark_px": mark_px,
               "open_interest": oi,
               "oi_usd": oi_usd,
           }

       return result

   except Exception as e:
       print(f"[ERROR] HL fetch failed: {e}")
       return {}

# ── OI Delta ─────────────────────────────────────────────────────────────────

def load_oi_log():
   if not os.path.exists(OI_LOG_FILE):
       return {}
   try:
       with open(OI_LOG_FILE, "r") as f:
           return json.load(f)
   except Exception:
       return {}

def save_oi_log(oi_data):
   with open(OI_LOG_FILE, "w") as f:
       json.dump(oi_data, f)

def compute_oi_delta(coin, current_oi, prev_oi_log):
   if coin not in prev_oi_log:
       return None, False
   prev_oi = prev_oi_log[coin]
   if isinstance(prev_oi, dict):
       prev_oi = prev_oi.get("open_interest", 0)
   if not prev_oi or prev_oi == 0:
       return None, False
   delta_pct = (current_oi - prev_oi) / prev_oi
   surge = delta_pct > OI_SURGE_PCT
   return delta_pct, surge

# ── Funding Momentum ──────────────────────────────────────────────────────────

def load_funding_momentum():
   if not os.path.exists(LOG_FILE):
       return {}

   history = defaultdict(list)
   try:
       with open(LOG_FILE, "r") as f:
           reader = csv.DictReader(f)
           for row in reader:
               history[row["coin"]].append(float(row["funding_8h"]))
   except Exception:
       return {}

   return {coin: vals[-3:] for coin, vals in history.items()}

def is_funding_trending_negative(coin, current_funding_8h, momentum_history):
   hist = momentum_history.get(coin, [])
   series = hist + [current_funding_8h]
   if len(series) < 3:
       return False
   return all(series[i] < series[i - 1] for i in range(1, len(series)))

# ── Signal ────────────────────────────────────────────────────────────────────

def evaluate_signal(funding_apy, oi_surge=False, trending_negative=False):
   fee_apy = (ENTRY_EXIT / 30) * 365
   net_apy = funding_apy - fee_apy

   squeeze_flag = funding_apy < THRESHOLD_NEG_CONSIDER and oi_surge
   momentum_flag = trending_negative and funding_apy < 0

   if squeeze_flag:
       label = "🔥 SQUEEZE SETUP — neg funding + OI surge"
       notify = True
   elif funding_apy < THRESHOLD_NEG_STRONG:
       label = "VERY NEGATIVE 🟢 LONG PERP!"
       notify = True
   elif funding_apy < THRESHOLD_NEG_CONSIDER:
       label = f"NEGATIVE (net ~{abs(net_apy):.1%}) 🟢 consider long perp"
       notify = True
   elif momentum_flag:
       label = "⚡ FUNDING TRENDING NEG — watch for entry"
       notify = True
   elif funding_apy < 0:
       label = "SLIGHTLY NEGATIVE ⚡ monitor"
       notify = False
   elif funding_apy < THRESHOLD_LOW:
       label = f"TOO LOW (net ~{net_apy:.1%}) ⬇️"
       notify = False
   elif funding_apy < THRESHOLD_MEDIUM:
       label = f"MARGINAL (net ~{net_apy:.1%}) 😐"
       notify = False
   elif funding_apy < THRESHOLD_HIGH:
       label = f"DECENT (net ~{net_apy:.1%}) 👀"
       notify = True
   else:
       label = f"ATTRACTIVE (net ~{net_apy:.1%}) 🚨"
       notify = True

   return label, notify, squeeze_flag

# ── Screener ──────────────────────────────────────────────────────────────────

def screen_coins(hl_data, prev_oi_log, momentum_history):
   enriched = {}

   for coin, d in hl_data.items():
       if d["oi_usd"] < MIN_OI_USD:
           continue

       oi_delta_pct, oi_surge = compute_oi_delta(
           coin, d["open_interest"], prev_oi_log
       )
       trending_neg = is_funding_trending_negative(
           coin, d["funding_8h"], momentum_history
       )
       signal, notify, squeeze = evaluate_signal(
           d["funding_apy"], oi_surge, trending_neg
       )

       enriched[coin] = {
           **d,
           "oi_delta_pct": oi_delta_pct,
           "oi_surge": oi_surge,
           "trending_negative": trending_neg,
           "signal": signal,
           "notify": notify,
           "squeeze": squeeze,
       }

   liquid = [(c, v) for c, v in enriched.items()]
   top_neg = sorted(liquid, key=lambda x: x[1]["funding_apy"])[:TOP_N]
   top_pos = sorted(liquid, key=lambda x: x[1]["funding_apy"], reverse=True)[:TOP_N]
   squeeze_setups = [(c, v) for c, v in liquid if v["squeeze"]]

   return enriched, top_neg, top_pos, squeeze_setups

# ── Price alert ───────────────────────────────────────────────────────────────

def evaluate_price(coin, mark_px):
   if coin not in SPOT_TARGETS:
       return None
   target = SPOT_TARGETS[coin]
   near_line = target * (1 + NEAR_TARGET_PCT)
   pct_away = (mark_px - target) / target * 100

   if mark_px <= target:
       return (
           f"🎯 <b>{coin} HIT TARGET!</b>\n"
           f"Mark: ${mark_px:,.2f} | Target: ${target:,}\n"
           f"Tranche 1 entry."
       )
   elif mark_px <= near_line:
       return (
           f"👀 <b>{coin} approaching target</b>\n"
           f"Mark: ${mark_px:,.2f} | Target: ${target:,} ({pct_away:.1f}% away)\n"
           f"Prepare dry powder."
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
               "mark_px", "open_interest", "oi_usd",
               "oi_delta_pct", "oi_surge",
               "trending_negative", "signal"
           ])

def append_row(timestamp, coin, d):
   with open(LOG_FILE, "a", newline="") as f:
       writer = csv.writer(f)
       writer.writerow([
           timestamp, coin,
           f"{d['funding_8h']:.6f}",
           f"{d['funding_apy']:.4f}",
           f"{d['mark_px']:.4f}",
           f"{d['open_interest']:.2f}",
           f"{d['oi_usd']:.0f}",
           f"{d['oi_delta_pct']:.4f}" if d['oi_delta_pct'] is not None else "",
           str(d['oi_surge']),
           str(d['trending_negative']),
           d['signal']
       ])

# ── Report builders ───────────────────────────────────────────────────────────

def format_coin_line(coin, d):
   oi_tag = ""
   if d["oi_surge"]:
       oi_tag = " 📈OI↑"
   elif d["oi_delta_pct"] is not None and d["oi_delta_pct"] < -OI_SURGE_PCT:
       oi_tag = " 📉OI↓"

   momentum_tag = " 🔻trending" if d["trending_negative"] else ""

   return (
       f"<b>{coin}</b> | APY: {d['funding_apy']:+.2%} | "
       f"${d['mark_px']:,.2f} | OI: ${d['oi_usd']/1e6:.1f}M"
       f"{oi_tag}{momentum_tag}\n{d['signal']}"
   )

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
   now_dt = datetime.now(timezone.utc)
   now = now_dt.strftime("%Y-%m-%d %H:%M:%S")
   h, m = time_to_settlement(now_dt)
   settlement_tag = f"⏱ Next settlement in {h}h {m}m"

   hl = fetch_hl_rates()
   if not hl:
       send_telegram("⚠️ HL Monitor: Failed to fetch funding rates.")
       return

   prev_oi_log = load_oi_log()
   momentum_history = load_funding_momentum()

   enriched, top_neg, top_pos, squeeze_setups = screen_coins(
       hl, prev_oi_log, momentum_history
   )

   save_oi_log({coin: d["open_interest"] for coin, d in hl.items()})

   init_csv()

   for coin, d in enriched.items():
       append_row(now, coin, d)

   # 1. Squeeze setups
   if squeeze_setups:
       squeeze_msg = [f"🔥 <b>SQUEEZE SETUPS — {now} UTC</b>\n{settlement_tag}\n"]
       for coin, d in squeeze_setups:
           oi_delta_str = f"{d['oi_delta_pct']:+.1%}" if d['oi_delta_pct'] is not None else "n/a"
           squeeze_msg.append(
               f"<b>{coin}</b> | APY: {d['funding_apy']:+.2%} | "
               f"OI Δ: {oi_delta_str} | ${d['mark_px']:,.2f}\n"
               f"Negative funding + OI surge = shorts piling in → long perp"
           )
       send_telegram("\n\n".join(squeeze_msg))

   # 2. Screener report
   lines = [f"<b>📊 HL Screener — {now} UTC</b>\n{settlement_tag}\n"]
   lines.append(f"<b>🟢 TOP {TOP_N} MOST NEGATIVE FUNDING</b>")
   for coin, d in top_neg:
       lines.append(format_coin_line(coin, d))
   lines.append(f"\n<b>🔴 TOP {TOP_N} MOST POSITIVE FUNDING</b>")
   for coin, d in top_pos:
       lines.append(format_coin_line(coin, d))
   send_telegram("\n\n".join(lines))

   # 3. Pinned coins
   pinned_lines = [f"<b>📌 Pinned — {now} UTC</b>\n{settlement_tag}\n"]
   for coin in PINNED_COINS:
       if coin in enriched:
           pinned_lines.append(format_coin_line(coin, enriched[coin]))
   send_telegram("\n\n".join(pinned_lines))

   # 4. Price alerts
   for coin, d in enriched.items():
       price_msg = evaluate_price(coin, d["mark_px"])
       if price_msg:
           send_telegram(price_msg)

   print(f"\n[{now}] Screened {len(enriched)} liquid coins")
   print(f"Next settlement in {h}h {m}m")
   print(f"TOP {TOP_N} MOST NEGATIVE:")
   for coin, d in top_neg:
       oi_tag = " [OI SURGE]" if d["oi_surge"] else ""
       print(f"  {coin:8s} APY: {d['funding_apy']:+.2%}  OI: ${d['oi_usd']/1e6:.1f}M{oi_tag}")
   print(f"TOP {TOP_N} MOST POSITIVE:")
   for coin, d in top_pos:
       print(f"  {coin:8s} APY: {d['funding_apy']:+.2%}  OI: ${d['oi_usd']/1e6:.1f}M")
   if squeeze_setups:
       print(f"🔥 SQUEEZE SETUPS: {[c for c, _ in squeeze_setups]}")
   print(f"Logged {len(enriched)} coins to {LOG_FILE}")

if __name__ == "__main__":
   main()
