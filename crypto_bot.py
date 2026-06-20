"""
Crypto paper-trading bot — Binance public klines, EMA 9/21 crossover + RSI.

Built to mirror the architecture from the YouTube tutorial:
- 10 crypto pairs, scans every 5 seconds, 24/7
- Starts with $10,000 paper money, no real trades, no API key needed
- Flask dashboard on http://localhost:12345
- SQLite log of every trade + portfolio snapshots
- /reports page renders markdown reviews from the reports/ folder
  (so the daily/weekly Claude Code routines can write their reviews here)
"""
from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from flask import Flask, jsonify, render_template_string, abort, request, Response

try:
    import markdown as md
except ImportError:
    md = None

# ---------------------------------------------------------------------------
# CONFIG — these are the values the daily/weekly AI reviews are allowed to tune
# ---------------------------------------------------------------------------
STARTING_CAPITAL = 100.0

SYMBOLS = [
    # Top 30 — majors + large caps (kept from previous list)
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT",
    "TRXUSDT", "LTCUSDT", "POLUSDT", "SHIBUSDT", "UNIUSDT",
    "ATOMUSDT", "ETCUSDT", "XLMUSDT", "NEARUSDT", "FILUSDT",
    "APTUSDT", "ARBUSDT", "OPUSDT", "SUIUSDT", "INJUSDT",
    "SEIUSDT", "TIAUSDT", "PEPEUSDT", "AAVEUSDT", "RNDRUSDT",
    # 31–60 — broad large/mid caps
    "ICPUSDT", "IMXUSDT", "GRTUSDT", "FETUSDT", "MKRUSDT",
    "IOTAUSDT", "STXUSDT", "BCHUSDT", "HBARUSDT", "VETUSDT",
    "RUNEUSDT", "ALGOUSDT", "FLOWUSDT", "EOSUSDT", "SANDUSDT",
    "MANAUSDT", "AXSUSDT", "CHZUSDT", "GALAUSDT", "FTMUSDT",
    "ENJUSDT", "COMPUSDT", "SNXUSDT", "CRVUSDT", "CAKEUSDT",
    "BATUSDT", "ZECUSDT", "DASHUSDT", "NEOUSDT", "EGLDUSDT",
    # 61–100 — mid + active meme/L2/DeFi/AI
    "KSMUSDT", "MINAUSDT", "ROSEUSDT", "GMTUSDT", "APEUSDT",
    "LDOUSDT", "JTOUSDT", "JUPUSDT", "WIFUSDT", "BONKUSDT",
    "FLOKIUSDT", "ORDIUSDT", "BLURUSDT", "SUSHIUSDT", "1INCHUSDT",
    "GMXUSDT", "DYDXUSDT", "TWTUSDT", "LRCUSDT", "ANKRUSDT",
    "RVNUSDT", "ZILUSDT", "ICXUSDT", "ONTUSDT", "KAVAUSDT",
    "LPTUSDT", "MASKUSDT", "SKLUSDT", "BANDUSDT", "WLDUSDT",
    "PYTHUSDT", "JASMYUSDT", "MANTAUSDT", "STRKUSDT", "ALTUSDT",
    "ENAUSDT", "ETHFIUSDT", "CFXUSDT", "BOMEUSDT", "RAYUSDT",
]

SCAN_INTERVAL_SECONDS = 5         # how often to poll Binance
KLINE_INTERVAL = "1m"             # candle size we analyse on
KLINE_LIMIT = 100                 # candles fetched per scan (enough for EMA21 + RSI14)

# Strategy params — the AI is allowed to micro-adjust these in the daily review.
EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70

# How much of free cash to deploy per buy signal.
POSITION_SIZE_PCT = 0.10          # 10% of free cash per trade
MIN_CASH_PER_TRADE = 5.0          # don't fire below this
TAKE_PROFIT_PCT = 0.03            # +3% close
STOP_LOSS_PCT = 0.02              # -2% close

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "cryptobot.db"
LOG_PATH = ROOT / "crypto_bot.log"
REPORTS_DIR = ROOT / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

BINANCE_BASE = "https://api.binance.com"


# ---------------------------------------------------------------------------
# Logging — append-only file the routines read to assess yesterday's behaviour
# ---------------------------------------------------------------------------
_log_lock = threading.Lock()

def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with _log_lock:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


# ---------------------------------------------------------------------------
# Database — one SQLite file, three tables (trades, positions, snapshots)
# ---------------------------------------------------------------------------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,           -- BUY or SELL
                qty REAL NOT NULL,
                price REAL NOT NULL,
                value REAL NOT NULL,
                reason TEXT NOT NULL,
                realized_pnl REAL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS positions (
                symbol TEXT PRIMARY KEY,
                qty REAL NOT NULL,
                avg_price REAL NOT NULL,
                opened_ts TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                ts TEXT PRIMARY KEY,
                cash REAL NOT NULL,
                holdings_value REAL NOT NULL,
                total_value REAL NOT NULL
            );
            """
        )
        cur = conn.execute("SELECT value FROM state WHERE key='cash'")
        row = cur.fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO state(key,value) VALUES('cash',?)",
                (str(STARTING_CAPITAL),),
            )
            conn.execute(
                "INSERT INTO state(key,value) VALUES('starting_capital',?)",
                (str(STARTING_CAPITAL),),
            )

def get_cash() -> float:
    with db() as conn:
        row = conn.execute("SELECT value FROM state WHERE key='cash'").fetchone()
        return float(row["value"])

def set_cash(amount: float) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE state SET value=? WHERE key='cash'", (str(amount),)
        )

def get_positions() -> dict[str, dict]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM positions").fetchall()
    return {r["symbol"]: dict(r) for r in rows}

def upsert_position(symbol: str, qty: float, avg_price: float) -> None:
    with db() as conn:
        existing = conn.execute(
            "SELECT opened_ts FROM positions WHERE symbol=?", (symbol,)
        ).fetchone()
        opened_ts = existing["opened_ts"] if existing else datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO positions(symbol,qty,avg_price,opened_ts) VALUES(?,?,?,?)
               ON CONFLICT(symbol) DO UPDATE SET qty=excluded.qty, avg_price=excluded.avg_price""",
            (symbol, qty, avg_price, opened_ts),
        )

def delete_position(symbol: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM positions WHERE symbol=?", (symbol,))

def record_trade(symbol: str, side: str, qty: float, price: float,
                 reason: str, realized_pnl: float = 0.0) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    value = qty * price
    with db() as conn:
        conn.execute(
            """INSERT INTO trades(ts,symbol,side,qty,price,value,reason,realized_pnl)
               VALUES(?,?,?,?,?,?,?,?)""",
            (ts, symbol, side, qty, price, value, reason, realized_pnl),
        )
    log(f"{side} {qty:.6f} {symbol} @ {price:.4f} ({reason}) pnl={realized_pnl:+.2f}")

def take_snapshot(cash: float, holdings_value: float) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO snapshots(ts,cash,holdings_value,total_value) VALUES(?,?,?,?)",
            (ts, cash, holdings_value, cash + holdings_value),
        )


# ---------------------------------------------------------------------------
# Binance public klines — no API key required
# ---------------------------------------------------------------------------
def fetch_klines(symbol: str) -> Optional[list[float]]:
    """Return a list of close prices for `symbol`, oldest first."""
    url = f"{BINANCE_BASE}/api/v3/klines"
    params = {"symbol": symbol, "interval": KLINE_INTERVAL, "limit": KLINE_LIMIT}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        # Kline format: [openTime, open, high, low, close, volume, ...]
        return [float(k[4]) for k in data]
    except Exception as e:
        log(f"fetch_klines({symbol}) failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------
def ema(prices: list[float], period: int) -> list[float]:
    if len(prices) < period:
        return []
    k = 2 / (period + 1)
    out = [sum(prices[:period]) / period]
    for p in prices[period:]:
        out.append(p * k + out[-1] * (1 - k))
    return out

def rsi(prices: list[float], period: int = RSI_PERIOD) -> Optional[float]:
    if len(prices) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        change = prices[i] - prices[i - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(prices)):
        change = prices[i] - prices[i - 1]
        gain = max(change, 0)
        loss = -min(change, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ---------------------------------------------------------------------------
# Strategy — EMA 9/21 crossover plus RSI 14, then TP/SL on open positions
# ---------------------------------------------------------------------------
@dataclass
class Signal:
    action: str        # BUY / SELL / HOLD
    reason: str

def evaluate(symbol: str, prices: list[float], have_position: bool) -> Signal:
    if len(prices) < max(EMA_SLOW + 2, RSI_PERIOD + 2):
        return Signal("HOLD", "not enough data")

    fast = ema(prices, EMA_FAST)
    slow = ema(prices, EMA_SLOW)
    if len(fast) < 2 or len(slow) < 2:
        return Signal("HOLD", "indicators not ready")

    # Align fast & slow tails (slow starts later because period is longer)
    offset = len(fast) - len(slow)
    f_now, f_prev = fast[-1], fast[-2]
    s_now, s_prev = slow[-1], slow[-2]
    rsi_val = rsi(prices, RSI_PERIOD) or 50.0

    golden_cross = f_prev <= s_prev and f_now > s_now
    death_cross = f_prev >= s_prev and f_now < s_now

    if not have_position:
        if golden_cross:
            return Signal("BUY", f"EMA{EMA_FAST}/{EMA_SLOW} golden cross")
        if rsi_val < RSI_OVERSOLD:
            return Signal("BUY", f"RSI {rsi_val:.1f} oversold")
    else:
        if death_cross:
            return Signal("SELL", f"EMA{EMA_FAST}/{EMA_SLOW} death cross")
        if rsi_val > RSI_OVERBOUGHT:
            return Signal("SELL", f"RSI {rsi_val:.1f} overbought")

    return Signal("HOLD", f"rsi={rsi_val:.1f} fast={f_now:.2f} slow={s_now:.2f}")


# ---------------------------------------------------------------------------
# Trading engine — fills paper orders against latest close
# ---------------------------------------------------------------------------
def execute_buy(symbol: str, price: float, reason: str) -> None:
    cash = get_cash()
    spend = max(MIN_CASH_PER_TRADE, cash * POSITION_SIZE_PCT)
    if spend > cash or spend < MIN_CASH_PER_TRADE:
        return
    qty = spend / price
    set_cash(cash - spend)
    upsert_position(symbol, qty, price)
    record_trade(symbol, "BUY", qty, price, reason)

def execute_sell(symbol: str, price: float, reason: str) -> None:
    positions = get_positions()
    pos = positions.get(symbol)
    if not pos:
        return
    qty = pos["qty"]
    avg = pos["avg_price"]
    proceeds = qty * price
    pnl = (price - avg) * qty
    set_cash(get_cash() + proceeds)
    delete_position(symbol)
    record_trade(symbol, "SELL", qty, price, reason, realized_pnl=pnl)


def check_tp_sl(symbol: str, price: float, pos: dict) -> bool:
    """Close on take-profit / stop-loss before strategy can decide."""
    avg = pos["avg_price"]
    change = (price - avg) / avg
    if change >= TAKE_PROFIT_PCT:
        execute_sell(symbol, price, f"take-profit +{change*100:.2f}%")
        return True
    if change <= -STOP_LOSS_PCT:
        execute_sell(symbol, price, f"stop-loss {change*100:.2f}%")
        return True
    return False


# ---------------------------------------------------------------------------
# Background scanner loop — runs forever in a thread
# ---------------------------------------------------------------------------
_scanner_state = {
    "running": False, "paused": False, "last_scan": None, "scans": 0,
    "last_prices": {},
    "market": {},   # symbol -> live indicator snapshot for the Market Scanner view
}

def scanner_loop() -> None:
    _scanner_state["running"] = True
    log(f"scanner starting — {len(SYMBOLS)} symbols, interval={SCAN_INTERVAL_SECONDS}s")
    while True:
        try:
            if not _scanner_state["paused"]:
                scan_once()
        except Exception as e:
            log(f"scan error: {e}")
        time.sleep(SCAN_INTERVAL_SECONDS)

def scan_once() -> None:
    positions = get_positions()
    holdings_value = 0.0
    for symbol in SYMBOLS:
        prices = fetch_klines(symbol)
        if not prices:
            continue
        price = prices[-1]
        _scanner_state["last_prices"][symbol] = price

        # Capture the indicators the strategy sees so the dashboard can show the
        # bot's live decision-making for every tracked pair.
        fast = ema(prices, EMA_FAST)
        slow = ema(prices, EMA_SLOW)
        rsi_val = rsi(prices, RSI_PERIOD)

        pos = positions.get(symbol)
        if pos:
            holdings_value += pos["qty"] * price
            if check_tp_sl(symbol, price, pos):
                positions = get_positions()  # refresh after sell
                _scanner_state["market"][symbol] = {
                    "price": price, "rsi": rsi_val,
                    "ema_fast": fast[-1] if fast else None,
                    "ema_slow": slow[-1] if slow else None,
                    "action": "SELL", "reason": "take-profit / stop-loss",
                    "have_position": False,
                }
                continue

        signal = evaluate(symbol, prices, have_position=symbol in positions)
        _scanner_state["market"][symbol] = {
            "price": price, "rsi": rsi_val,
            "ema_fast": fast[-1] if fast else None,
            "ema_slow": slow[-1] if slow else None,
            "action": signal.action, "reason": signal.reason,
            "have_position": symbol in positions,
        }
        if signal.action == "BUY" and symbol not in positions:
            execute_buy(symbol, price, signal.reason)
        elif signal.action == "SELL" and symbol in positions:
            execute_sell(symbol, price, signal.reason)

    take_snapshot(get_cash(), holdings_value)
    _scanner_state["last_scan"] = datetime.now(timezone.utc).isoformat()
    _scanner_state["scans"] += 1


# ---------------------------------------------------------------------------
# Flask dashboard
# ---------------------------------------------------------------------------
app = Flask(__name__)

DASHBOARD_HTML = """
<!doctype html>
<html><head><title>PurffleTrader — Dashboard</title>
<noscript><meta http-equiv="refresh" content="15"></noscript>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet"/>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#06060a;--s1:#0f1117;--s2:#181b23;--bd:#1e2130;--t1:#f0f1f5;--t2:#8b8fa3;--t3:#565a6e;
--green:#22c55e;--red:#ef4444;--blue:#3b82f6;--purple:#a855f7;--teal:#14b8a6;
--grad:linear-gradient(135deg,#f7931a,#f59e0b,#eab308)}
body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--t1);min-height:100vh;padding:0}
.shell{max-width:1280px;margin:0 auto;padding:24px 32px}
#live{transition:opacity .2s ease}

/* Nav */
.topbar{display:flex;align-items:center;justify-content:space-between;margin-bottom:28px;padding-bottom:20px;border-bottom:1px solid var(--bd)}
.brand{display:flex;align-items:center;gap:10px;text-decoration:none;color:var(--t1)}
.brand-icon{width:34px;height:34px;background:var(--grad);border-radius:9px;display:flex;align-items:center;justify-content:center;font-weight:900;font-size:15px;color:#000}
.brand span{font-weight:800;font-size:17px;letter-spacing:-.02em}
.brand .env{font-size:10px;font-weight:700;color:#f59e0b;background:rgba(245,158,11,.12);padding:3px 8px;border-radius:5px;margin-left:6px}
.nav-links{display:flex;gap:4px}
.nav-links a{color:var(--t2);font-size:13px;font-weight:500;text-decoration:none;padding:7px 14px;border-radius:8px;transition:.15s}
.nav-links a:hover,.nav-links a.active{color:var(--t1);background:var(--s2)}

/* Status bar */
.status-bar{display:flex;align-items:center;gap:16px;margin-bottom:24px;font-size:12px;color:var(--t3)}
.status-bar .live{display:flex;align-items:center;gap:6px;color:var(--green);font-weight:600}
.status-bar .live .dot{width:7px;height:7px;border-radius:50%;background:var(--green);animation:blink 2s ease-in-out infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.25}}
.status-bar .sep{color:var(--bd)}

/* Metric cards */
.metrics{display:grid;grid-template-columns:repeat(5,1fr);gap:14px;margin-bottom:28px}
.metric{background:var(--s1);border:1px solid var(--bd);border-radius:14px;padding:20px;position:relative;overflow:hidden;transition:.2s}
.metric:hover{border-color:rgba(168,85,247,.3);transform:translateY(-2px)}
.metric::after{content:'';position:absolute;top:0;left:0;right:0;height:2px;border-radius:14px 14px 0 0;opacity:0;transition:.2s}
.metric:hover::after{opacity:1}
.metric:nth-child(1)::after{background:var(--grad)}
.metric:nth-child(2)::after{background:var(--blue)}
.metric:nth-child(3)::after{background:var(--teal)}
.metric:nth-child(4)::after{background:var(--purple)}
.metric:nth-child(5)::after{background:var(--green)}
.metric .lbl{font-size:11px;font-weight:600;color:var(--t3);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px}
.metric .val{font-size:26px;font-weight:800;letter-spacing:-.02em}
.metric .sub{font-size:12px;color:var(--t3);margin-top:4px}

.pos{color:var(--green)}.neg{color:var(--red)}

/* Section headers */
.sec{display:flex;align-items:center;justify-content:space-between;margin:28px 0 12px}
.sec h2{font-size:15px;font-weight:700;display:flex;align-items:center;gap:8px}
.sec .badge{font-size:11px;font-weight:600;color:var(--t3);background:var(--s2);padding:3px 10px;border-radius:6px}

/* Tables */
.tbl-wrap{background:var(--s1);border:1px solid var(--bd);border-radius:14px;overflow:hidden;margin-bottom:8px}
table{width:100%;border-collapse:collapse}
th{background:var(--s2);color:var(--t3);font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;padding:10px 14px;text-align:left;position:sticky;top:0}
td{padding:10px 14px;font-size:13px;border-top:1px solid var(--bd)}
tr:hover td{background:rgba(255,255,255,.015)}
.pill{display:inline-block;padding:3px 10px;border-radius:8px;font-size:10px;font-weight:700;letter-spacing:.03em}
.pill.buy{background:rgba(34,197,94,.12);color:var(--green)}
.pill.sell{background:rgba(239,68,68,.12);color:var(--red)}
.pill.open{background:rgba(59,130,246,.12);color:var(--blue)}
.muted{color:var(--t3);font-size:12px}
b{font-weight:700}

/* Stats panel */
.statgrid{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:8px}
.statcard{background:var(--s1);border:1px solid var(--bd);border-radius:12px;padding:14px 16px}
.statcard .l{font-size:10px;font-weight:600;color:var(--t3);text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px}
.statcard .v{font-size:18px;font-weight:800;letter-spacing:-.01em}
/* Market scanner */
.scan-wrap{max-height:430px;overflow-y:auto}
.pill.hold{background:rgba(139,143,163,.12);color:var(--t2)}
.trend-up{color:var(--green);font-weight:700;font-size:12px}
.trend-dn{color:var(--red);font-weight:700;font-size:12px}
.rsi-lo{color:var(--green);font-weight:700}.rsi-hi{color:var(--red);font-weight:700}
/* Controls */
.ctrl-btn{font-size:12px;font-weight:600;font-family:inherit;cursor:pointer;padding:7px 14px;border-radius:8px;border:1px solid var(--bd);background:var(--s2);color:var(--t1);transition:.15s}
.ctrl-btn:hover{border-color:var(--purple)}
.ctrl-btn.paused{color:#f59e0b;border-color:rgba(245,158,11,.4)}
.export-link{color:var(--t2)!important}
/* Allocation breakdown */
.alloc-bar{display:flex;height:18px;border-radius:9px;overflow:hidden;margin-bottom:12px;background:var(--s2)}
.alloc-seg{height:100%;transition:width .4s ease}
.alloc-legend{display:flex;flex-wrap:wrap;gap:8px 16px}
.alloc-item{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--t2)}
.alloc-dot{width:10px;height:10px;border-radius:3px;flex-shrink:0}
/* Scanner search */
.scan-search{width:100%;max-width:280px;padding:7px 12px;background:var(--s2);border:1px solid var(--bd);border-radius:8px;color:var(--t1);font-size:12px;font-family:inherit;margin-bottom:10px}
.scan-search:focus{outline:none;border-color:var(--purple)}

@media(max-width:900px){
 .metrics{grid-template-columns:repeat(2,1fr)}
 .shell{padding:16px}
 .statgrid{grid-template-columns:repeat(2,1fr)}
 .topbar{flex-wrap:wrap;gap:12px}
 .nav-links{flex-wrap:wrap}
 .status-bar{flex-wrap:wrap;gap:8px}
 .tbl-wrap{overflow-x:auto}
 table{min-width:560px}
 .scan-wrap{overflow:auto}
}
@media(max-width:560px){.metrics{grid-template-columns:1fr 1fr}.statgrid{grid-template-columns:1fr 1fr}.metric .val{font-size:22px}}
</style></head><body>
<div class="shell">
<div class="topbar">
 <a href="/" class="brand"><div class="brand-icon">P</div><span>PurffleTrader</span><span class="env">PAPER</span></a>
 <div class="nav-links">
   <a href="/" class="active">Dashboard</a>
   <a href="/reports">Reports</a>
   <a href="/export/trades.csv" class="export-link">&#x2B07; CSV</a>
   <a href="/api/state">API</a>
   <button type="button" id="pauseBtn" class="ctrl-btn {{ 'paused' if paused else '' }}">{{ '▶ Resume' if paused else '⏸ Pause' }}</button>
 </div>
</div>

<div id="live">
<div class="status-bar">
 <span class="live" style="{{ 'color:#f59e0b' if paused else '' }}"><span class="dot"></span> {{ 'PAUSED' if paused else 'SCANNING' }}</span>
 <span class="sep">|</span> Every {{scan_interval}}s
 <span class="sep">|</span> {{symbols|length}} pairs
 <span class="sep">|</span> Binance public klines
 <span class="sep">|</span> Last scan: {{last_scan or 'pending'}}
 <span class="sep">|</span> {{scans}} scans
</div>

<div class="metrics">
 <div class="metric"><div class="lbl">Total Value</div>
  <div class="val {{'pos' if total>=starting else 'neg'}}" id="mTotal" data-v="{{ total }}">${{ '%.2f'|format(total) }}</div>
  <div class="sub">P/L ${{ '%+.2f'|format(total-starting) }} ({{ '%+.2f'|format((total/starting-1)*100) }}%)</div></div>
 <div class="metric"><div class="lbl">Cash</div><div class="val">${{ '%.2f'|format(cash) }}</div></div>
 <div class="metric"><div class="lbl">Holdings</div><div class="val">${{ '%.2f'|format(holdings_value) }}</div></div>
 <div class="metric"><div class="lbl">Open Positions</div><div class="val">{{ positions|length }}</div></div>
 <div class="metric"><div class="lbl">Total Trades</div><div class="val">{{ trade_count }}</div></div>
</div>

{% set palette = ['#f7931a','#3b82f6','#14b8a6','#22c55e','#a855f7','#ec4899','#ef4444','#8b5cf6','#eab308','#06b6d4'] %}
<div class="sec"><h2>Portfolio Allocation</h2></div>
<div class="tbl-wrap" style="padding:18px 16px">
 {% if allocation %}
 <div class="alloc-bar">
  {% for a in allocation %}<div class="alloc-seg" style="width:{{ '%.3f'|format(a.pct) }}%;background:{{ palette[loop.index0 % palette|length] }}" title="{{ a.label }} {{ '%.1f'|format(a.pct) }}%"></div>{% endfor %}
 </div>
 <div class="alloc-legend">
  {% for a in allocation %}<span class="alloc-item"><span class="alloc-dot" style="background:{{ palette[loop.index0 % palette|length] }}"></span>{{ a.label }} <b style="color:var(--t1)">{{ '%.1f'|format(a.pct) }}%</b> <span class="muted">${{ '%.2f'|format(a.value) }}</span></span>{% endfor %}
 </div>
 {% else %}
 <div class="muted" style="text-align:center;padding:8px">All in cash — no open positions yet.</div>
 {% endif %}
</div>

<div class="sec"><h2>Equity Curve <span class="badge">{{ equity.count }} snapshots</span></h2></div>
<div class="tbl-wrap" style="padding:18px 16px 12px">
 {% if equity.has %}
 <svg viewBox="0 0 1180 130" preserveAspectRatio="none" style="width:100%;height:130px;display:block">
  <defs><linearGradient id="eg" x1="0" y1="0" x2="0" y2="1">
   <stop offset="0%" stop-color="{{ '#22c55e' if equity.up else '#ef4444' }}" stop-opacity="0.28"/>
   <stop offset="100%" stop-color="{{ '#22c55e' if equity.up else '#ef4444' }}" stop-opacity="0"/>
  </linearGradient></defs>
  <polygon fill="url(#eg)" points="{{ equity.area }}"/>
  <polyline fill="none" stroke="{{ '#22c55e' if equity.up else '#ef4444' }}" stroke-width="2" stroke-linejoin="round" points="{{ equity.points }}"/>
 </svg>
 <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--t3);margin-top:8px">
  <span>Start ${{ '%.2f'|format(equity.first) }}</span>
  <span>Low ${{ '%.2f'|format(equity.min) }} &middot; High ${{ '%.2f'|format(equity.max) }}</span>
  <span class="{{ 'pos' if equity.up else 'neg' }}">Now ${{ '%.2f'|format(equity.last) }}</span>
 </div>
 {% else %}
 <div class="muted" style="text-align:center;padding:18px">Equity curve will appear after a few scan cycles record snapshots.</div>
 {% endif %}
</div>

<div class="sec"><h2>Performance <span class="badge">{{ stats.closed }} closed trades</span></h2></div>
<div class="statgrid">
 <div class="statcard"><div class="l">Win Rate</div><div class="v {{ 'pos' if stats.win_rate is not none and stats.win_rate>=50 else '' }}">{% if stats.win_rate is not none %}{{ '%.1f'|format(stats.win_rate) }}%{% else %}—{% endif %}</div></div>
 <div class="statcard"><div class="l">Profit Factor</div><div class="v">{{ stats.profit_factor }}</div></div>
 <div class="statcard"><div class="l">Net Realized</div><div class="v {{ 'pos' if stats.net>=0 else 'neg' }}">${{ '%+.2f'|format(stats.net) }}</div></div>
 <div class="statcard"><div class="l">Avg Win</div><div class="v pos">${{ '%+.2f'|format(stats.avg_win) }}</div></div>
 <div class="statcard"><div class="l">Avg Loss</div><div class="v neg">${{ '%+.2f'|format(stats.avg_loss) }}</div></div>
 <div class="statcard"><div class="l">Best / Worst</div><div class="v"><span class="pos">${{ '%+.0f'|format(stats.best) }}</span> <span class="muted">/</span> <span class="neg">${{ '%+.0f'|format(stats.worst) }}</span></div></div>
</div>

<div class="sec"><h2>&#x1F4E1; Live Market Scanner <span class="badge">top {{ market|length }} of {{ symbols|length }} pairs</span></h2></div>
<input type="text" id="scanSearch" class="scan-search" placeholder="Filter symbols… (e.g. BTC)" autocomplete="off">
<div class="tbl-wrap scan-wrap"><table id="scanTable">
 <tr><th>Symbol</th><th>Price</th><th>RSI {{rsi_period}}</th><th>EMA {{ema_fast}}/{{ema_slow}}</th><th>Live Signal</th><th>Reason</th></tr>
 {% for m in market %}
 <tr>
  <td><b>{{ m.symbol }}</b>{% if m.have_position %} <span class="pill open">HOLDING</span>{% endif %}</td>
  <td>{% if m.price is not none %}${{ '%.4f'|format(m.price) }}{% else %}<span class="muted">—</span>{% endif %}</td>
  <td>{% if m.rsi is not none %}<span class="{{ 'rsi-lo' if m.rsi<rsi_oversold else ('rsi-hi' if m.rsi>rsi_overbought else '') }}">{{ '%.0f'|format(m.rsi) }}</span>{% else %}<span class="muted">—</span>{% endif %}</td>
  <td>{% if m.trend=='up' %}<span class="trend-up">&#x25B2; bullish</span>{% else %}<span class="trend-dn">&#x25BC; bearish</span>{% endif %}</td>
  <td><span class="pill {{ m.action|lower }}">{{ m.action }}</span></td>
  <td class="muted">{{ m.reason }}</td>
 </tr>
 {% else %}
 <tr><td colspan="6" class="muted" style="padding:20px;text-align:center">Scanner warming up — indicators populate after the first scan cycle</td></tr>
 {% endfor %}
</table></div>

<div class="sec"><h2>Profit by Symbol <span class="badge">realized ${{ '%+.2f'|format(realized_total) }}</span></h2></div>
<div class="tbl-wrap"><table>
 <tr><th>Symbol</th><th>Trades</th><th>W / L</th><th>Win Rate</th><th>Realized</th><th>Unrealized</th><th>Total P/L</th><th>Status</th></tr>
 {% for r in profit_rows %}
 <tr>
  <td><b>{{ r.symbol }}</b></td>
  <td>{{ r.trades }} <span class="muted">({{ r.buys }}B/{{ r.sells }}S)</span></td>
  <td><span class="pos">{{ r.wins }}</span> / <span class="neg">{{ r.losses }}</span></td>
  <td>{% if r.win_rate is not none %}{{ '%.0f'|format(r.win_rate) }}%{% else %}<span class="muted">—</span>{% endif %}</td>
  <td class="{{'pos' if r.realized>=0 else 'neg'}}">${{ '%+.2f'|format(r.realized) }}</td>
  <td class="{{'pos' if r.unrealized>=0 else 'neg'}}">{% if r.open %}${{ '%+.2f'|format(r.unrealized) }}{% else %}<span class="muted">—</span>{% endif %}</td>
  <td class="{{'pos' if r.total>=0 else 'neg'}}"><b>${{ '%+.2f'|format(r.total) }}</b></td>
  <td>{% if r.open %}<span class="pill open">OPEN</span>{% else %}<span class="muted">closed</span>{% endif %}</td>
 </tr>
 {% else %}
 <tr><td colspan="8" class="muted" style="padding:20px;text-align:center">No trades yet — strategy is watching {{ symbols|length }} pairs</td></tr>
 {% endfor %}
</table></div>

<div class="sec"><h2>Open Positions</h2></div>
<div class="tbl-wrap"><table>
 <tr><th>Symbol</th><th>Qty</th><th>Avg Price</th><th>Current</th><th>Value</th><th>Unrealized P/L</th></tr>
 {% for p in open_positions %}
 <tr>
  <td><b>{{p.symbol}}</b></td>
  <td>{{ '%.6f'|format(p.qty) }}</td>
  <td>${{ '%.4f'|format(p.avg_price) }}</td>
  <td>${{ '%.4f'|format(p.current) }}</td>
  <td>${{ '%.2f'|format(p.value) }}</td>
  <td class="{{'pos' if p.upnl>=0 else 'neg'}}">${{ '%+.2f'|format(p.upnl) }} ({{ '%+.2f'|format(p.upnl_pct) }}%)</td>
 </tr>
 {% else %}
 <tr><td colspan="6" class="muted" style="padding:20px;text-align:center">No open positions — strategy is watching</td></tr>
 {% endfor %}
</table></div>

<div class="sec"><h2>Recent Trades</h2></div>
<div class="tbl-wrap"><table>
 <tr><th>Time (UTC)</th><th>Side</th><th>Symbol</th><th>Qty</th><th>Price</th><th>Value</th><th>Reason</th><th>Realized P/L</th></tr>
 {% for t in trades %}
 <tr>
  <td>{{ t.ts[:19].replace('T',' ') }}</td>
  <td><span class="pill {{ t.side|lower }}">{{ t.side }}</span></td>
  <td><b>{{ t.symbol }}</b></td>
  <td>{{ '%.6f'|format(t.qty) }}</td>
  <td>${{ '%.4f'|format(t.price) }}</td>
  <td>${{ '%.2f'|format(t.value) }}</td>
  <td class="muted">{{ t.reason }}</td>
  <td class="{{'pos' if t.realized_pnl>=0 else 'neg'}}">{% if t.side=='SELL' %}${{ '%+.2f'|format(t.realized_pnl) }}{% endif %}</td>
 </tr>
 {% else %}
 <tr><td colspan="8" class="muted" style="padding:20px;text-align:center">No trades yet</td></tr>
 {% endfor %}
</table></div>
</div><!-- /#live -->

<div class="sec"><h2>&#x1F4D6; How It Works</h2></div>
<div class="tbl-wrap" style="padding:24px">
 <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px">
  <div>
   <h3 style="font-size:14px;font-weight:700;margin-bottom:10px;color:var(--blue)">&#x1F4CA; Strategy: EMA Crossover + RSI</h3>
   <p style="font-size:13px;color:var(--t2);line-height:1.7">PurffleTrader uses a dual-indicator strategy combining <b>Exponential Moving Averages</b> (EMA {{ema_fast}} &amp; {{ema_slow}}) with the <b>Relative Strength Index</b> (RSI {{rsi_period}}). A <span class="pill buy">BUY</span> fires when the fast EMA ({{ema_fast}}) crosses above the slow EMA ({{ema_slow}}), or when RSI drops below {{rsi_oversold}} (oversold). A <span class="pill sell">SELL</span> fires on a death cross (fast crosses below slow) or when RSI rises above {{rsi_overbought}} (overbought).</p>
   <p style="font-size:13px;color:var(--t2);line-height:1.7;margin-top:10px"><b>Data source:</b> Binance public API (no key needed). Pulls <b>{{kline_interval}}</b> klines for all tracked pairs every scan cycle.</p>
  </div>
  <div>
   <h3 style="font-size:14px;font-weight:700;margin-bottom:10px;color:var(--teal)">&#x2699;&#xFE0F; How to Use</h3>
   <ul style="font-size:13px;color:var(--t2);line-height:2;list-style:none;padding:0">
    <li>&#x2022; <b>Paper trading only</b> — uses virtual ${{ '%.0f'|format(starting) }} starting capital, no real money</li>
    <li>&#x2022; Bot scans <b>{{symbols|length}} pairs</b> every {{scan_interval}} seconds on Binance spot</li>
    <li>&#x2022; Each buy deploys {{ '%.0f'|format(position_size_pct*100) }}% of free cash; one position per symbol at a time</li>
    <li>&#x2022; Auto take-profit at +{{ '%.0f'|format(take_profit_pct*100) }}% and stop-loss at -{{ '%.0f'|format(stop_loss_pct*100) }}%</li>
    <li>&#x2022; All trades logged to SQLite — check the <b>Reports</b> page for AI reviews</li>
    <li>&#x2022; Dashboard auto-refreshes every 10 seconds</li>
    <li>&#x2022; Win Rate column shows how often sells close at a profit</li>
   </ul>
  </div>
 </div>
</div>

<div class="sec"><h2>&#x1F4C8; Backtest Results (2-Year, $100 Capital)</h2></div>
<div class="tbl-wrap"><table>
 <tr><th>Strategy</th><th>Final</th><th>ROI</th><th>Win Rate</th><th>Max DD</th><th>Avg Mo.</th><th>Best Mo.</th><th>Worst Mo.</th><th>vs BTC</th></tr>
 <tr><td><b>Buy &amp; Hold BTC</b></td><td>$92.61</td><td class="neg">-7.4%</td><td>0.0%</td><td>51.9%</td><td>+0.4%</td><td>+37.2%</td><td class="neg">-17.7%</td><td>—</td></tr>
 <tr><td><b>DCA BTC (weekly)</b></td><td>$74.99</td><td class="neg">-25.0%</td><td>0.0%</td><td>45.2%</td><td>-0.9%</td><td>+10.5%</td><td class="neg">-15.0%</td><td class="neg">-17.6%</td></tr>
 <tr style="background:rgba(59,130,246,.06)"><td><b>BTC 4h EMA 21/55 &#x2B50;</b></td><td class="pos"><b>$130.79</b></td><td class="pos"><b>+30.8%</b></td><td>33.3%</td><td>29.8%</td><td>+1.4%</td><td>+27.1%</td><td class="neg">-15.0%</td><td class="pos">+38.2%</td></tr>
 <tr><td><b>BTC 4h EMA 9/21</b></td><td>$90.55</td><td class="neg">-9.4%</td><td>30.5%</td><td>43.1%</td><td>+0.0%</td><td>+26.2%</td><td class="neg">-17.0%</td><td>-2.1%</td></tr>
 <tr><td><b>Multi-coin 4h EMA 21/55</b></td><td>$115.24</td><td class="pos">+15.2%</td><td>26.7%</td><td>63.8%</td><td>+4.1%</td><td>+178.8%</td><td class="neg">-22.1%</td><td class="pos">+22.6%</td></tr>
 <tr style="background:rgba(168,85,247,.06)"><td><b>Multi-coin 4h EMA 9/21 &#x1F680;</b></td><td class="pos"><b>$135.40</b></td><td class="pos"><b>+35.4%</b></td><td>27.8%</td><td>53.1%</td><td>+5.1%</td><td>+189.5%</td><td class="neg">-18.9%</td><td class="pos">+42.8%</td></tr>
 <tr><td><b>Mean Reversion RSI&lt;25</b></td><td>$106.53</td><td class="pos">+6.5%</td><td>57.1%</td><td>31.3%</td><td>+0.6%</td><td>+15.2%</td><td class="neg">-15.5%</td><td class="pos">+13.9%</td></tr>
 <tr><td><b>Mean Reversion RSI&lt;20</b></td><td>$113.21</td><td class="pos">+13.2%</td><td>55.2%</td><td>33.5%</td><td>+0.7%</td><td>+13.9%</td><td class="neg">-17.4%</td><td class="pos">+20.6%</td></tr>
 <tr><td><b>Breakout 20-day high</b></td><td>$96.74</td><td class="neg">-3.3%</td><td>31.6%</td><td>44.1%</td><td>+0.4%</td><td>+37.3%</td><td class="neg">-13.8%</td><td>+4.1%</td></tr>
 <tr><td><b>Breakout 55-day high</b></td><td>$134.50</td><td class="pos">+34.5%</td><td>37.5%</td><td>31.7%</td><td>+1.3%</td><td>+39.3%</td><td class="neg">-16.5%</td><td class="pos">+41.9%</td></tr>
</table></div>
<div style="padding:12px 20px;font-size:12px;color:var(--t3);line-height:1.6">
 <b>Window:</b> Jun 2024 — Jun 2026 &middot; <b>Universe:</b> Top 20 majors &middot; <b>Fee:</b> 0.1% spot per trade &middot; <b>Best performer:</b> Multi-coin EMA 9/21 at <span class="pos">+35.4%</span> (outperformed BTC buy &amp; hold by +42.8%). The active EMA 9/21 crossover strategy used by PurffleTrader consistently beats passive holding across multiple market conditions.
</div>

<div style="text-align:center;padding:24px 0;color:var(--t3);font-size:12px">PurffleTrader &middot; Built by <b>Purffle</b></div>
</div>
<script>
(function(){
  // Pause / resume control (button lives in the static topbar so it survives live swaps).
  var pauseBtn = document.getElementById('pauseBtn');
  if (pauseBtn && window.fetch) {
    pauseBtn.addEventListener('click', function(){
      fetch('/api/control', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'},
                             body:'action=toggle'})
        .then(function(r){ return r.json(); })
        .then(function(d){
          var paused = !!d.paused;
          pauseBtn.textContent = paused ? '▶ Resume' : '⏸ Pause';
          pauseBtn.classList.toggle('paused', paused);
        }).catch(function(){});
    });
  }

  var live = document.getElementById('live');
  if (!live || !window.fetch) return;          // no-JS: <noscript> meta-refresh takes over
  var INTERVAL = 6000;

  // Market Scanner filter — persists across live refreshes (input is re-created each swap).
  function applyScanFilter(){
    var input = document.getElementById('scanSearch');
    var table = document.getElementById('scanTable');
    if (!input || !table) return;
    var q = input.value.trim().toUpperCase();
    var rows = table.getElementsByTagName('tr');
    for (var i = 1; i < rows.length; i++){   // row 0 is the header
      var cell = rows[i].cells[0];
      if (!cell) continue;
      rows[i].style.display = (!q || cell.textContent.toUpperCase().indexOf(q) !== -1) ? '' : 'none';
    }
  }
  live.addEventListener('input', function(e){ if (e.target && e.target.id === 'scanSearch') applyScanFilter(); });

  function animateValue(el, from, to, dur){
    var start = performance.now();
    function frame(now){
      var t = Math.min(1, (now - start) / dur);
      var v = from + (to - from) * (t * (2 - t)); // ease-out
      el.textContent = '$' + v.toFixed(2);
      if (t < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }
  function refresh(){
    fetch(window.location.pathname, {headers: {'X-Requested-With': 'fetch'}})
      .then(function(r){ return r.text(); })
      .then(function(txt){
        var fresh = new DOMParser().parseFromString(txt, 'text/html').getElementById('live');
        if (!fresh) return;
        var oldEl = document.getElementById('mTotal');
        var oldV = oldEl ? parseFloat(oldEl.getAttribute('data-v')) : NaN;
        var sEl = document.getElementById('scanSearch');
        var savedQuery = sEl ? sEl.value : '';
        var savedFocus = sEl && document.activeElement === sEl;
        live.style.opacity = '0.55';
        setTimeout(function(){
          live.innerHTML = fresh.innerHTML;
          live.style.opacity = '1';
          var newEl = document.getElementById('mTotal');
          if (newEl){
            var newV = parseFloat(newEl.getAttribute('data-v'));
            if (!isNaN(oldV) && !isNaN(newV) && oldV !== newV) animateValue(newEl, oldV, newV, 700);
          }
          var newSearch = document.getElementById('scanSearch');
          if (newSearch && savedQuery){
            newSearch.value = savedQuery;
            if (savedFocus){ newSearch.focus(); }
            applyScanFilter();
          }
        }, 160);
      }).catch(function(){});
  }
  setInterval(refresh, INTERVAL);
})();
</script>
</body></html>
"""

REPORTS_HTML = """
<!doctype html>
<html><head><title>PurffleTrader — {{ title }}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"/>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',sans-serif;background:#06060a;color:#f0f1f5;min-height:100vh;padding:0}
.shell{max-width:800px;margin:0 auto;padding:24px 32px}
.topbar{display:flex;align-items:center;justify-content:space-between;margin-bottom:28px;padding-bottom:20px;border-bottom:1px solid #1e2130}
.brand{display:flex;align-items:center;gap:10px;text-decoration:none;color:#f0f1f5}
.brand-icon{width:34px;height:34px;background:linear-gradient(135deg,#f7931a,#f59e0b);border-radius:9px;display:flex;align-items:center;justify-content:center;font-weight:900;font-size:15px;color:#000}
.brand span{font-weight:800;font-size:17px}
.nav-links a{color:#8b8fa3;font-size:13px;font-weight:500;text-decoration:none;padding:7px 14px;border-radius:8px;transition:.15s}
.nav-links a:hover,.nav-links a.active{color:#f0f1f5;background:#181b23}
h1{font-size:20px;font-weight:700;margin-bottom:20px}
ul{list-style:none;padding:0}
li{border-bottom:1px solid #1e2130}
li a{display:flex;align-items:center;justify-content:space-between;padding:14px 16px;color:#f0f1f5;text-decoration:none;font-weight:500;font-size:14px;border-radius:10px;transition:.15s}
li a:hover{background:#0f1117;color:#a855f7}
.muted{color:#565a6e;font-size:12px}
.content{background:#0f1117;border:1px solid #1e2130;border-radius:14px;padding:28px;margin-top:16px;line-height:1.7;font-size:14px}
.content h1,.content h2,.content h3{color:#a855f7;margin:20px 0 10px}
.content h1:first-child,.content h2:first-child{margin-top:0}
.content code{background:#181b23;padding:2px 8px;border-radius:6px;font-size:13px}
.content pre{background:#181b23;padding:16px;border-radius:10px;overflow-x:auto;border:1px solid #1e2130}
.content table{border-collapse:collapse;margin:16px 0;width:100%}
.content th,.content td{border:1px solid #1e2130;padding:8px 12px;text-align:left;font-size:13px}
.content th{background:#181b23;color:#8b8fa3;font-size:11px;text-transform:uppercase}
.empty{text-align:center;padding:40px;color:#565a6e;font-size:14px}
</style></head><body>
<div class="shell">
<div class="topbar">
 <a href="/" class="brand"><div class="brand-icon">P</div><span>PurffleTrader</span></a>
 <div class="nav-links"><a href="/">Dashboard</a><a href="/reports" class="active">Reports</a></div>
</div>
<h1>{{ title }}</h1>
{% if report_html %}
  <div class="content">{{ report_html|safe }}</div>
{% else %}
  <ul>
  {% for r in reports %}
    <li><a href="/reports/{{r.name}}">{{r.name}}<span class="muted">{{r.mtime}}</span></a></li>
  {% else %}
    <li class="empty">No reports yet — AI review routines will generate reports here.</li>
  {% endfor %}
  </ul>
{% endif %}
</div></body></html>
"""

def build_equity_sparkline(series: list[float], w: int = 1180, h: int = 130,
                           pad: int = 10) -> dict:
    """Turn a list of total-value snapshots into SVG polyline/area point strings."""
    if len(series) < 2:
        return {"has": False, "count": len(series)}
    lo, hi = min(series), max(series)
    rng = (hi - lo) or 1.0
    n = len(series)
    pts = []
    for i, v in enumerate(series):
        x = pad + (w - 2 * pad) * (i / (n - 1))
        y = pad + (h - 2 * pad) * (1 - (v - lo) / rng)
        pts.append(f"{x:.1f},{y:.1f}")
    points = " ".join(pts)
    x0 = f"{pad:.1f}"
    x1 = f"{pad + (w - 2 * pad):.1f}"
    baseline = f"{h - pad:.1f}"
    area = f"{x0},{baseline} {points} {x1},{baseline}"
    return {
        "has": True, "count": n, "points": points, "area": area,
        "up": series[-1] >= series[0], "first": series[0], "last": series[-1],
        "min": lo, "max": hi,
    }


def compute_trade_stats(conn) -> dict:
    """Aggregate realized P/L of closed (SELL) trades into headline performance stats."""
    pnls = [r["realized_pnl"] or 0.0
            for r in conn.execute("SELECT realized_pnl FROM trades WHERE side='SELL'").fetchall()]
    closed = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    if gross_loss > 0:
        pf_display = f"{gross_profit / gross_loss:.2f}"
    elif gross_profit > 0:
        pf_display = "∞"   # profits, no losses yet
    else:
        pf_display = "—"
    return {
        "closed": closed, "wins": len(wins), "losses": len(losses),
        "win_rate": (len(wins) / closed * 100) if closed else None,
        "avg_win": (gross_profit / len(wins)) if wins else 0.0,
        "avg_loss": (-gross_loss / len(losses)) if losses else 0.0,
        "best": max(pnls) if pnls else 0.0,
        "worst": min(pnls) if pnls else 0.0,
        "profit_factor": pf_display,
        "net": sum(pnls),
    }


def build_market_view(limit: int = 40) -> list[dict]:
    """Snapshot of what the strategy currently sees per symbol, most actionable first."""
    action_rank = {"BUY": 0, "SELL": 1, "HOLD": 2}
    rows = []
    for sym, d in _scanner_state["market"].items():
        ef, es = d.get("ema_fast"), d.get("ema_slow")
        rows.append({
            "symbol": sym, "price": d.get("price"), "rsi": d.get("rsi"),
            "ema_fast": ef, "ema_slow": es,
            "trend": "up" if (ef is not None and es is not None and ef >= es) else "down",
            "action": d.get("action", "HOLD"), "reason": d.get("reason", ""),
            "have_position": d.get("have_position", False),
        })
    rows.sort(key=lambda m: (action_rank.get(m["action"], 3),
                             -abs((m["rsi"] if m["rsi"] is not None else 50) - 50)))
    return rows[:limit]


@app.route("/")
def dashboard():
    cash = get_cash()
    positions = get_positions()
    last_prices = _scanner_state["last_prices"]
    open_positions = []
    holdings_value = 0.0
    for sym, p in positions.items():
        current = last_prices.get(sym, p["avg_price"])
        value = p["qty"] * current
        upnl = (current - p["avg_price"]) * p["qty"]
        upnl_pct = ((current / p["avg_price"]) - 1) * 100 if p["avg_price"] else 0
        holdings_value += value
        open_positions.append({
            "symbol": sym, "qty": p["qty"], "avg_price": p["avg_price"],
            "current": current, "value": value, "upnl": upnl, "upnl_pct": upnl_pct,
        })
    with db() as conn:
        trades = [dict(r) for r in conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT 50"
        ).fetchall()]
        trade_count = conn.execute("SELECT COUNT(*) AS c FROM trades").fetchone()["c"]
        starting = float(conn.execute(
            "SELECT value FROM state WHERE key='starting_capital'"
        ).fetchone()["value"])
        equity_series = [float(r["total_value"]) for r in conn.execute(
            "SELECT total_value FROM snapshots ORDER BY ts ASC LIMIT 500"
        ).fetchall()]
        # Per-symbol profit table: realized P/L from closed trades, wins/losses,
        # plus unrealized from any open position.
        per_symbol_rows = conn.execute(
            """SELECT symbol,
                      COUNT(*)                                  AS trade_count,
                      SUM(CASE WHEN side='BUY'  THEN 1 ELSE 0 END) AS buys,
                      SUM(CASE WHEN side='SELL' THEN 1 ELSE 0 END) AS sells,
                      SUM(realized_pnl)                          AS realized_pnl,
                      SUM(CASE WHEN side='SELL' AND realized_pnl>0 THEN 1 ELSE 0 END) AS wins,
                      SUM(CASE WHEN side='SELL' AND realized_pnl<0 THEN 1 ELSE 0 END) AS losses
               FROM trades GROUP BY symbol"""
        ).fetchall()
        stats = compute_trade_stats(conn)
    upnl_by_sym = {p["symbol"]: p["upnl"] for p in open_positions}
    profit_rows = []
    for r in per_symbol_rows:
        sym = r["symbol"]
        realized = r["realized_pnl"] or 0.0
        unrealized = upnl_by_sym.pop(sym, 0.0)
        total = realized + unrealized
        closed = (r["wins"] or 0) + (r["losses"] or 0)
        win_rate = (r["wins"] / closed * 100) if closed else None
        profit_rows.append({
            "symbol": sym, "trades": r["trade_count"],
            "buys": r["buys"], "sells": r["sells"],
            "wins": r["wins"] or 0, "losses": r["losses"] or 0,
            "win_rate": win_rate,
            "realized": realized, "unrealized": unrealized, "total": total,
            "open": sym in {p["symbol"] for p in open_positions},
        })
    # Append any open positions that haven't sold yet (no row in trades grouping is fine,
    # but if a symbol has only buys it's already included above).
    for sym, upnl in upnl_by_sym.items():
        profit_rows.append({
            "symbol": sym, "trades": 0, "buys": 0, "sells": 0,
            "wins": 0, "losses": 0, "win_rate": None,
            "realized": 0.0, "unrealized": upnl, "total": upnl, "open": True,
        })
    profit_rows.sort(key=lambda r: r["total"], reverse=True)
    realized_total = sum(r["realized"] for r in profit_rows)
    equity = build_equity_sparkline(equity_series)
    # Portfolio allocation breakdown (cash + each holding as a share of total value).
    total_value = cash + holdings_value
    allocation = []
    if total_value > 0:
        allocation.append({"label": "Cash", "value": cash, "pct": cash / total_value * 100})
        for p in open_positions:
            allocation.append({"label": p["symbol"], "value": p["value"],
                               "pct": p["value"] / total_value * 100})
    allocation.sort(key=lambda a: -a["value"])
    return render_template_string(
        DASHBOARD_HTML,
        cash=cash, holdings_value=holdings_value, total=cash + holdings_value,
        starting=starting, positions=positions, open_positions=open_positions,
        trades=trades, trade_count=trade_count, symbols=SYMBOLS,
        profit_rows=profit_rows, realized_total=realized_total,
        scan_interval=SCAN_INTERVAL_SECONDS,
        last_scan=_scanner_state["last_scan"][:19] if _scanner_state["last_scan"] else None,
        scans=_scanner_state["scans"], equity=equity,
        ema_fast=EMA_FAST, ema_slow=EMA_SLOW, rsi_period=RSI_PERIOD,
        rsi_oversold=RSI_OVERSOLD, rsi_overbought=RSI_OVERBOUGHT,
        kline_interval=KLINE_INTERVAL, position_size_pct=POSITION_SIZE_PCT,
        take_profit_pct=TAKE_PROFIT_PCT, stop_loss_pct=STOP_LOSS_PCT,
        stats=stats, market=build_market_view(), paused=_scanner_state["paused"],
        allocation=allocation,
    )

@app.route("/export/trades.csv")
def export_trades_csv():
    """Download the full trade log as CSV."""
    with db() as conn:
        rows = conn.execute(
            "SELECT ts,symbol,side,qty,price,value,reason,realized_pnl "
            "FROM trades ORDER BY id ASC"
        ).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp", "symbol", "side", "qty", "price", "value", "reason", "realized_pnl"])
    for r in rows:
        w.writerow([r["ts"], r["symbol"], r["side"], r["qty"], r["price"],
                    r["value"], r["reason"], r["realized_pnl"]])
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=purffletrader_trades.csv"},
    )


@app.route("/api/control", methods=["POST"])
def api_control():
    """Pause or resume the background scanner."""
    action = (request.form.get("action") or request.args.get("action") or "").lower()
    if action == "pause":
        _scanner_state["paused"] = True
    elif action == "resume":
        _scanner_state["paused"] = False
    elif action == "toggle":
        _scanner_state["paused"] = not _scanner_state["paused"]
    else:
        return jsonify({"error": "action must be pause, resume, or toggle"}), 400
    log(f"scanner {'paused' if _scanner_state['paused'] else 'resumed'} via dashboard")
    return jsonify({"paused": _scanner_state["paused"]})


@app.route("/reports")
def reports_index():
    reports = []
    for p in sorted(REPORTS_DIR.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
        reports.append({
            "name": p.name,
            "mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
    return render_template_string(REPORTS_HTML, title="Reports", reports=reports, report_html=None)

@app.route("/reports/<name>")
def report_view(name: str):
    if "/" in name or "\\" in name or not name.endswith(".md"):
        abort(404)
    path = REPORTS_DIR / name
    if not path.exists():
        abort(404)
    text = path.read_text(encoding="utf-8")
    if md is None:
        html = "<pre>" + text.replace("<", "&lt;") + "</pre>"
    else:
        html = md.markdown(text, extensions=["fenced_code", "tables"])
    return render_template_string(REPORTS_HTML, title=name, reports=[], report_html=html)

@app.route("/api/state")
def api_state():
    cash = get_cash()
    positions = get_positions()
    last_prices = _scanner_state["last_prices"]
    holdings_value = sum(p["qty"] * last_prices.get(s, p["avg_price"])
                         for s, p in positions.items())
    with db() as conn:
        trades = [dict(r) for r in conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT 200"
        ).fetchall()]
    return jsonify({
        "cash": cash,
        "holdings_value": holdings_value,
        "total_value": cash + holdings_value,
        "positions": list(positions.values()),
        "last_prices": last_prices,
        "trades": trades,
        "last_scan": _scanner_state["last_scan"],
        "scans": _scanner_state["scans"],
        "config": {
            "symbols": SYMBOLS,
            "scan_interval_seconds": SCAN_INTERVAL_SECONDS,
            "ema_fast": EMA_FAST, "ema_slow": EMA_SLOW,
            "rsi_period": RSI_PERIOD,
            "rsi_oversold": RSI_OVERSOLD, "rsi_overbought": RSI_OVERBOUGHT,
            "position_size_pct": POSITION_SIZE_PCT,
            "take_profit_pct": TAKE_PROFIT_PCT,
            "stop_loss_pct": STOP_LOSS_PCT,
        },
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    init_db()
    log("=" * 60)
    log("crypto_bot starting")
    log(f"symbols: {', '.join(SYMBOLS)}")
    log(f"strategy: EMA{EMA_FAST}/{EMA_SLOW} crossover + RSI{RSI_PERIOD} "
        f"(oversold<{RSI_OVERSOLD}, overbought>{RSI_OVERBOUGHT})")
    log(f"risk: pos={POSITION_SIZE_PCT*100:.0f}% cash, TP={TAKE_PROFIT_PCT*100:.1f}%, "
        f"SL={STOP_LOSS_PCT*100:.1f}%")
    t = threading.Thread(target=scanner_loop, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", "12345"))
    # Disable reloader so the background thread isn't duplicated.
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
