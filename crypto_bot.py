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
from flask import Flask, jsonify, render_template_string, abort

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
_scanner_state = {"running": False, "last_scan": None, "scans": 0, "last_prices": {}}

def scanner_loop() -> None:
    _scanner_state["running"] = True
    log(f"scanner starting — {len(SYMBOLS)} symbols, interval={SCAN_INTERVAL_SECONDS}s")
    while True:
        try:
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

        pos = positions.get(symbol)
        if pos:
            holdings_value += pos["qty"] * price
            if check_tp_sl(symbol, price, pos):
                positions = get_positions()  # refresh after sell
                continue

        signal = evaluate(symbol, prices, have_position=symbol in positions)
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
<html><head><title>Crypto Paper Trading Bot</title>
<meta http-equiv="refresh" content="10">
<style>
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0b0d10;color:#e6e6e6;margin:0;padding:24px;}
 h1{margin:0 0 12px;font-size:22px}
 .row{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:24px}
 .card{background:#15181d;border:1px solid #23272e;border-radius:10px;padding:18px;min-width:200px;flex:1}
 .label{color:#8a9099;font-size:12px;text-transform:uppercase;letter-spacing:.05em}
 .value{font-size:24px;font-weight:600;margin-top:4px}
 .pos{color:#36d399}.neg{color:#f87272}
 table{width:100%;border-collapse:collapse;background:#15181d;border-radius:10px;overflow:hidden}
 th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #23272e;font-size:14px}
 th{background:#1a1e24;color:#8a9099;font-weight:500;text-transform:uppercase;font-size:11px;letter-spacing:.05em}
 tr:last-child td{border-bottom:none}
 .nav{margin-bottom:20px}
 .nav a{color:#7aa2f7;margin-right:18px;text-decoration:none;font-size:14px}
 .nav a:hover{text-decoration:underline}
 .pill{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600}
 .pill.buy{background:#193c2a;color:#36d399}
 .pill.sell{background:#3c1919;color:#f87272}
 .muted{color:#6b7280;font-size:12px}
</style></head><body>
<div class="nav">
  <a href="/">Dashboard</a><a href="/reports">Reports</a><a href="/api/state">Raw state (JSON)</a>
</div>
<h1>Crypto Paper Trading Bot</h1>
<div class="muted">Scans every {{scan_interval}}s · {{symbols|length}} pairs · Binance public klines · last scan {{last_scan or 'pending'}}Z · {{scans}} scans so far</div>

<div class="row">
 <div class="card"><div class="label">Total value</div>
   <div class="value {{ 'pos' if total>=starting else 'neg' }}">${{ '%.2f'|format(total) }}</div>
   <div class="muted">P/L ${{ '%+.2f'|format(total-starting) }} ({{ '%+.2f'|format((total/starting-1)*100) }}%)</div>
 </div>
 <div class="card"><div class="label">Cash</div><div class="value">${{ '%.2f'|format(cash) }}</div></div>
 <div class="card"><div class="label">Holdings value</div><div class="value">${{ '%.2f'|format(holdings_value) }}</div></div>
 <div class="card"><div class="label">Open positions</div><div class="value">{{ positions|length }}</div></div>
 <div class="card"><div class="label">Trades total</div><div class="value">{{ trade_count }}</div></div>
</div>

<h2 style="font-size:16px;margin:8px 0">Profit by symbol <span class="muted" style="font-weight:normal">· realized ${{ '%+.2f'|format(realized_total) }}</span></h2>
<table>
 <tr><th>Symbol</th><th>Trades</th><th>Wins / Losses</th><th>Win rate</th><th>Realized P/L</th><th>Unrealized P/L</th><th>Total P/L</th><th>Status</th></tr>
 {% for r in profit_rows %}
 <tr>
   <td><b>{{ r.symbol }}</b></td>
   <td>{{ r.trades }} <span class="muted">({{ r.buys }}B / {{ r.sells }}S)</span></td>
   <td><span class="pos">{{ r.wins }}</span> / <span class="neg">{{ r.losses }}</span></td>
   <td>{% if r.win_rate is not none %}{{ '%.0f'|format(r.win_rate) }}%{% else %}<span class="muted">—</span>{% endif %}</td>
   <td class="{{ 'pos' if r.realized>=0 else 'neg' }}">${{ '%+.2f'|format(r.realized) }}</td>
   <td class="{{ 'pos' if r.unrealized>=0 else 'neg' }}">{% if r.open %}${{ '%+.2f'|format(r.unrealized) }}{% else %}<span class="muted">—</span>{% endif %}</td>
   <td class="{{ 'pos' if r.total>=0 else 'neg' }}"><b>${{ '%+.2f'|format(r.total) }}</b></td>
   <td>{% if r.open %}<span class="pill buy">OPEN</span>{% else %}<span class="muted">closed</span>{% endif %}</td>
 </tr>
 {% else %}
 <tr><td colspan="8" class="muted">No trades yet — the strategy is still watching {{ symbols|length }} pairs.</td></tr>
 {% endfor %}
</table>

<h2 style="font-size:16px;margin:24px 0 8px">Open positions</h2>
<table>
 <tr><th>Symbol</th><th>Qty</th><th>Avg price</th><th>Current</th><th>Value</th><th>Unrealized P/L</th></tr>
 {% for p in open_positions %}
 <tr>
   <td><b>{{p.symbol}}</b></td>
   <td>{{ '%.6f'|format(p.qty) }}</td>
   <td>${{ '%.4f'|format(p.avg_price) }}</td>
   <td>${{ '%.4f'|format(p.current) }}</td>
   <td>${{ '%.2f'|format(p.value) }}</td>
   <td class="{{ 'pos' if p.upnl>=0 else 'neg' }}">${{ '%+.2f'|format(p.upnl) }} ({{ '%+.2f'|format(p.upnl_pct) }}%)</td>
 </tr>
 {% else %}
 <tr><td colspan="6" class="muted">No open positions yet — strategy is watching.</td></tr>
 {% endfor %}
</table>

<h2 style="font-size:16px;margin:24px 0 8px">Recent trades</h2>
<table>
 <tr><th>Time (UTC)</th><th>Side</th><th>Symbol</th><th>Qty</th><th>Price</th><th>Value</th><th>Reason</th><th>Realized P/L</th></tr>
 {% for t in trades %}
 <tr>
   <td>{{ t.ts[:19].replace('T',' ') }}</td>
   <td><span class="pill {{ t.side|lower }}">{{ t.side }}</span></td>
   <td>{{ t.symbol }}</td>
   <td>{{ '%.6f'|format(t.qty) }}</td>
   <td>${{ '%.4f'|format(t.price) }}</td>
   <td>${{ '%.2f'|format(t.value) }}</td>
   <td class="muted">{{ t.reason }}</td>
   <td class="{{ 'pos' if t.realized_pnl>=0 else 'neg' }}">{% if t.side=='SELL' %}${{ '%+.2f'|format(t.realized_pnl) }}{% endif %}</td>
 </tr>
 {% else %}
 <tr><td colspan="8" class="muted">No trades yet.</td></tr>
 {% endfor %}
</table>
</body></html>
"""

REPORTS_HTML = """
<!doctype html>
<html><head><title>Reports</title>
<style>
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0b0d10;color:#e6e6e6;margin:0;padding:24px;max-width:900px}
 h1{margin:0 0 12px;font-size:22px}
 .nav a{color:#7aa2f7;margin-right:18px;text-decoration:none;font-size:14px}
 ul{list-style:none;padding:0}
 li{padding:10px;border-bottom:1px solid #23272e}
 li a{color:#e6e6e6;text-decoration:none;font-weight:500}
 li a:hover{color:#7aa2f7}
 .muted{color:#6b7280;font-size:12px;margin-left:8px}
 .content{background:#15181d;border:1px solid #23272e;border-radius:10px;padding:24px;margin-top:16px;line-height:1.6}
 .content h1,.content h2,.content h3{color:#7aa2f7}
 .content code{background:#1a1e24;padding:2px 6px;border-radius:4px;font-size:13px}
 .content pre{background:#1a1e24;padding:12px;border-radius:6px;overflow-x:auto}
 .content table{border-collapse:collapse;margin:12px 0}
 .content th,.content td{border:1px solid #23272e;padding:6px 10px}
</style></head><body>
<div class="nav"><a href="/">← Dashboard</a><a href="/reports">Reports</a></div>
<h1>{{ title }}</h1>
{% if report_html %}
  <div class="content">{{ report_html|safe }}</div>
{% else %}
  <ul>
  {% for r in reports %}
    <li><a href="/reports/{{r.name}}">{{r.name}}</a><span class="muted">{{r.mtime}}</span></li>
  {% else %}
    <li class="muted">No reports yet. The daily / weekly Claude Code routines will drop markdown files into <code>reports/</code>.</li>
  {% endfor %}
  </ul>
{% endif %}
</body></html>
"""

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
    return render_template_string(
        DASHBOARD_HTML,
        cash=cash, holdings_value=holdings_value, total=cash + holdings_value,
        starting=starting, positions=positions, open_positions=open_positions,
        trades=trades, trade_count=trade_count, symbols=SYMBOLS,
        profit_rows=profit_rows, realized_total=realized_total,
        scan_interval=SCAN_INTERVAL_SECONDS,
        last_scan=_scanner_state["last_scan"][:19] if _scanner_state["last_scan"] else None,
        scans=_scanner_state["scans"],
    )

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
