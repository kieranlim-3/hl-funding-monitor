# hl-funding-monitor

A live funding rate monitor and screener for Hyperliquid perpetuals, running on GitHub Actions with Telegram alerts.

## What it does

Runs every 15 minutes via GitHub Actions cron. Each run:

- Fetches funding rates across **all active Hyperliquid perps** (~100+ coins)
- Filters out illiquid markets (OI < $1M USD)
- Surfaces the **top 5 most negative** and **top 5 most positive** funding coins
- Detects **squeeze setups**: negative funding + rising OI (shorts piling in)
- Detects **funding momentum**: 3 consecutive readings trending more negative
- Sends structured alerts to Telegram
- Logs everything to `funding_log.csv` for offline analysis

## Why funding rates matter

On perpetual futures, funding is paid between longs and shorts every 8 hours to keep the perp price anchored to spot. When funding goes deeply negative, longs are being paid by shorts — a signal that the market is overcrowded short. If open interest is simultaneously rising, new shorts are still entering despite paying to hold the position. This combination — negative funding + OI surge — historically precedes short squeezes.

This bot monitors for that setup across the full Hyperliquid universe in real time.

## Signal logic

|Signal             |Condition                                              |
|-------------------|-------------------------------------------------------|
|🔥 Squeeze setup    |APY < -15% AND OI jumped >5% since last reading        |
|🟢 Very negative    |APY < -30% — strong long perp candidate                |
|🟢 Negative         |APY < -15% — consider long perp                        |
|⚡ Trending negative|3 consecutive readings each more negative than previous|
|👀 Decent positive  |APY 15–30% — short perp / basis trade candidate        |
|🚨 Attractive       |APY > 30% — high funding, strong short perp signal     |

## Project structure

```
hl_funding_once.py      # Main monitor — runs once per cron trigger
hl_funding_analysis.py  # Offline analysis script — run locally anytime
funding_log.csv         # Appended every 15 min by GitHub Actions
oi_log.json             # OI snapshot from last run (used for delta calc)
.github/workflows/      # GitHub Actions cron config
```

## Setup

### 1. Fork this repo

### 2. Add GitHub Secrets

Go to **Settings → Secrets and variables → Actions** and add:

|Secret            |Value                                    |
|------------------|-----------------------------------------|
|`TELEGRAM_TOKEN`  |Your Telegram bot token (from @BotFather)|
|`TELEGRAM_CHAT_ID`|Your chat ID (from @userinfobot)         |

### 3. Enable GitHub Actions

The workflow runs every 15 minutes automatically once enabled. Check the **Actions** tab to verify.

### 4. Run analysis locally

```bash
pip install requests
python hl_funding_analysis.py
```

Reads from `funding_log.csv` and prints a full report including simulated P&L for two strategies: standard negative funding entry, and squeeze-setup-only entry.

## CSV schema

|Column             |Description                               |
|-------------------|------------------------------------------|
|`timestamp`        |UTC time of reading                       |
|`coin`             |Asset name                                |
|`funding_8h`       |Raw 8h funding rate                       |
|`funding_apy`      |Annualised rate (funding_8h × 3 × 365)    |
|`mark_px`          |Mark price at time of reading             |
|`open_interest`    |OI in coin terms                          |
|`oi_usd`           |OI in USD (open_interest × mark_px)       |
|`oi_delta_pct`     |% change in OI vs previous reading        |
|`oi_surge`         |True if OI jumped >5%                     |
|`trending_negative`|True if last 3 readings each more negative|
|`signal`           |Human-readable signal label               |

## Configuration

Edit the top of `hl_funding_once.py`:

```python
TOP_N = 5              # Coins to surface per direction
MIN_OI_USD = 1_000_000 # Liquidity filter
OI_SURGE_PCT = 0.05    # OI delta threshold for surge flag
PINNED_COINS = ["BTC", "ETH", "HYPE"]  # Always included in report
DEPLOY_CAPITAL = 10000 # For simulated P&L in analysis script
```

## Tech stack

- Python 3.x, stdlib only (csv, json, os, datetime)
- `requests` for HL API and Telegram
- GitHub Actions for scheduling (free tier sufficient)
- Hyperliquid public API — no API key required

-----

Built to monitor funding rate conditions on Hyperliquid as part of a discretionary futures trading workflow.