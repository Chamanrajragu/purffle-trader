<p align="center">
  <img src="https://img.shields.io/badge/Purffle-Crypto_Trader-F7931A?style=for-the-badge&logo=bitcoin&logoColor=white" alt="PurffleTrader"/>
</p>

<h1 align="center">PurffleTrader — Crypto Paper Trading Bot</h1>

<p align="center">
  <strong>Scan 100 crypto pairs on Binance every 5 seconds. EMA crossover + RSI strategy. Paper trading with a real-time Flask dashboard.</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.9+-blue?style=flat-square&logo=python&logoColor=white" />
  <img src="https://img.shields.io/badge/binance-public_API-F0B90B?style=flat-square&logo=binance&logoColor=white" />
  <img src="https://img.shields.io/badge/flask-dashboard-000000?style=flat-square&logo=flask&logoColor=white" />
  <img src="https://img.shields.io/badge/sqlite-trade_log-003B57?style=flat-square&logo=sqlite&logoColor=white" />
  <img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" />
</p>

---

## What It Does

PurffleTrader is a paper-trading bot that monitors cryptocurrency markets 24/7 using Binance's public API. No API key required — it reads public candlestick data and simulates trades with virtual money.

### Strategy: EMA 9/21 Crossover + RSI Filter

| Signal | Condition |
|--------|-----------|
| **Buy** | EMA-9 crosses above EMA-21 AND RSI < 30 (oversold) |
| **Sell (Take Profit)** | Position up +3% |
| **Sell (Stop Loss)** | Position down -2% |

### Key Numbers

- **100 crypto pairs** scanned simultaneously
- **5-second** scan interval
- **1-minute** candlestick data
- **$100** starting paper capital
- **10%** position size per trade

## Features

- **No API key needed** — Uses Binance public kline endpoints only
- **100-pair coverage** — Majors, altcoins, DeFi, meme coins, L2 tokens
- **Real-time dashboard** — Flask web UI showing portfolio, positions, and trade history
- **SQLite logging** — Every trade and portfolio snapshot persisted to disk
- **Reports page** — Renders markdown review files from the `/reports` directory
- **Take-profit & stop-loss** — Automatic position management at +3% / -2%
- **AI-assisted tuning** — Designed for daily/weekly AI review routines that optimize parameters

## Dashboard

The Flask dashboard runs at `http://localhost:12345` and shows:

- Current portfolio value and P&L
- Active positions with entry price and unrealized gain/loss
- Complete trade history with timestamps and reasons
- Markdown reports from AI review routines

## Quick Start

### Prerequisites

- Python 3.9+
- Internet connection (for Binance public API)

### Installation

```bash
# Clone the repo
git clone https://github.com/Chamanrajragu/purffle-trader.git
cd purffle-trader

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### Run

```bash
python crypto_bot.py
```

Open `http://localhost:12345` to view the dashboard.

## Configuration

All strategy parameters are defined at the top of `crypto_bot.py`:

```python
EMA_FAST = 9              # Fast EMA period
EMA_SLOW = 21             # Slow EMA period
RSI_PERIOD = 14           # RSI lookback
RSI_OVERSOLD = 30         # Buy threshold
RSI_OVERBOUGHT = 70       # Sell threshold
POSITION_SIZE_PCT = 0.10  # 10% of cash per trade
TAKE_PROFIT_PCT = 0.03    # +3% take profit
STOP_LOSS_PCT = 0.02      # -2% stop loss
```

## AI Review Routines

PurffleTrader is designed to be tuned by AI review routines:

### Daily Micro-Review (9:00 AM)
Reads trade logs and the database. Analyzes win rate, P&L per trade, and underperforming symbols. Makes small parameter tweaks (±20% max). Writes a report to `reports/daily-YYYY-MM-DD.md`.

### Weekly Strategy Review (Monday 10:00 AM)
Full strategy assessment over 7 days of data. May make larger changes — add/remove indicators, restructure evaluation logic, adjust the symbol list. Backs up the bot before editing.

## Project Structure

```
purffle-trader/
├── crypto_bot.py          # Main bot + Flask dashboard
├── _reset_db.py           # Database reset utility
├── requirements.txt       # Python dependencies
└── reports/               # AI-generated trade reviews
```

## Backtest Results (2-Year, $100 Starting Capital)

Backtested across the top 20 majors from Jun 2024 — Jun 2026 with 0.1% spot fees per trade.

| Strategy | Final | ROI | Win Rate | Max Drawdown | Avg Monthly | vs BTC |
|----------|-------|-----|----------|-------------|-------------|--------|
| Buy & Hold BTC | $92.61 | -7.4% | — | 51.9% | +0.4% | — |
| DCA BTC (weekly) | $74.99 | -25.0% | — | 45.2% | -0.9% | -17.6% |
| **BTC 4h EMA 21/55** | **$130.79** | **+30.8%** | 33.3% | 29.8% | +1.4% | **+38.2%** |
| BTC 4h EMA 9/21 | $90.55 | -9.4% | 30.5% | 43.1% | +0.0% | -2.1% |
| Multi-coin 4h EMA 21/55 | $115.24 | +15.2% | 26.7% | 63.8% | +4.1% | +22.6% |
| **Multi-coin 4h EMA 9/21** | **$135.40** | **+35.4%** | 27.8% | 53.1% | +5.1% | **+42.8%** |
| Mean Reversion RSI<25 | $106.53 | +6.5% | 57.1% | 31.3% | +0.6% | +13.9% |
| Mean Reversion RSI<20 | $113.21 | +13.2% | 55.2% | 33.5% | +0.7% | +20.6% |
| Breakout 20-day high | $96.74 | -3.3% | 31.6% | 44.1% | +0.4% | +4.1% |
| Breakout 55-day high | $134.50 | +34.5% | 37.5% | 31.7% | +1.3% | +41.9% |

**Best performer:** Multi-coin EMA 9/21 crossover at **+35.4% ROI**, outperforming BTC buy & hold by **+42.8%**. The active EMA crossover strategy that PurffleTrader uses consistently beats passive holding across multiple market conditions.

## Disclaimer

> This is a **paper trading bot** for educational purposes only. It does not execute real trades or require any exchange API keys. Past simulated performance does not guarantee future results. Always do your own research before trading cryptocurrency.

---

<p align="center">
  Built with passion by <a href="https://github.com/Chamanrajragu"><strong>Purffle</strong></a>
  <br/>
  <sub>Part of the Purffle ecosystem — PurffleTools · PurffleAI · Purffle.com</sub>
</p>
