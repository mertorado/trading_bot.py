import os
import time
from datetime import datetime, timedelta
import requests
import pandas as pd
import numpy as np
from flask import Flask, jsonify, render_template_string
from threading import Thread

# ================= STRATEJİ VE ZAMANLAMA AYARLARI =================
TIMEFRAME = "3m"               # Güvenilir 3 dakikalık mumlar
SCAN_INTERVAL = 20             # 20 saniyede bir tarama
TRADE_SIZE_PERCENT = 0.10      # Her işlemde bakiyenin %10'u
MAX_OPEN_POSITIONS = 10        # Aynı anda maksimum 10 açık pozisyon
INITIAL_BALANCE = 1000.0       # Başlangıç paper trading kasası

# ================= KÂR / ZARAR VE RİSK YÖNETİMİ =================
TAKE_PROFIT_PCT = 0.025        # %2.5 Sabit Kâr Al hedefi
STOP_LOSS_PCT = 0.015          # %1.5 Sabit Stop Loss (pozisyon bazlı)
TRAILING_TRIGGER_PCT = 0.012   # %1.2 kâra ulaşınca Trailing Stop aktif olur
TRAILING_DISTANCE_PCT = 0.006  # Zirveden %0.6 geri çekilirse satış yapılır
COMMISSION_RATE = 0.001        # %0.1 Binance Spot komisyon oranı

# ================= OVERTRADING KORUMALARI =================
MAX_TRADES_PER_HOUR = 15
MAX_TRADES_PER_DAY = 50
SYMBOL_COOLDOWN_SECONDS = 900  # Pozisyon kapandıktan sonra aynı coine 15 dk yasak

# Filtreler
MIN_24H_VOLUME_USDT = 5000000  # 24s hacmi en az 5M USDT olan likit coinler
VOLUME_SPIKE_FACTOR = 1.8      # Son mum hacmi ortalamanın en az 1.8 katı olmalı

# ================= GLOBAL DURUM DEĞİŞKENLERİ =================
balance = INITIAL_BALANCE
open_positions = {}            # {symbol: pos_data}
symbol_cooldowns = {}          # {symbol: unlock_timestamp}
trade_history_timestamps = []  # [timestamp, ...]
trade_log = []                 # Kapanan işlemlerin listesi (dashboard için, son 100 tanesi)

app = Flask(__name__)

# ================= BINANCE API FONKSİYONLARI =================
def get_top_symbols():
    try:
        url = "https://api.binance.com/api/v3/ticker/24hr"
        res = requests.get(url, timeout=10).json()
        valid = []
        for item in res:
            sym = item.get("symbol", "")
            if sym.endswith("USDT") and not any(x in sym for x in ["UP", "DOWN", "BEAR", "BULL"]):
                vol = float(item.get("quoteVolume", 0))
                price = float(item.get("lastPrice", 0))
                if vol >= MIN_24H_VOLUME_USDT and 0.05 <= price <= 100.0:
                    valid.append((sym, vol))
        valid.sort(key=lambda x: x[1], reverse=True)
        return [x[0] for x in valid[:40]]
    except Exception as e:
        print(f"[!] Sembol listesi hatası: {e}")
        return []

def get_klines_data(symbol):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={TIMEFRAME}&limit=60"
        res = requests.get(url, timeout=5).json()
        if not isinstance(res, list) or len(res) < 50:
            return None

        df = pd.DataFrame(res, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "q_vol", "trades", "tb_base", "tb_quote", "ignore"
        ])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        return df
    except Exception:
        return None

# ================= İNDİKATÖR VE SİNYAL ANALİZİ =================
def calculate_indicators(df):
    close = df["close"]

    df["ema_trend"] = close.ewm(span=50, adjust=False).mean()

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()

    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / (loss + 1e-9)
    df["rsi"] = 100 - (100 / (1 + rs))

    df["vol_ma"] = df["volume"].rolling(window=20).mean()
    return df

def analyze_signal(df):
    if df is None or len(df) < 50:
        return None

    df = calculate_indicators(df)
    curr = df.iloc[-1]
    prev = df.iloc[-2]

    has_vol = curr["volume"] > (curr["vol_ma"] * VOLUME_SPIKE_FACTOR)

    if (curr["close"] > curr["ema_trend"] and
        prev["macd"] <= prev["macd_signal"] and curr["macd"] > curr["macd_signal"] and
        40 <= curr["rsi"] <= 65 and
        has_vol):
        return "LONG"

    if (curr["close"] < curr["ema_trend"] and
        prev["macd"] >= prev["macd_signal"] and curr["macd"] < curr["macd_signal"] and
        35 <= curr["rsi"] <= 60 and
        has_vol):
        return "SHORT"

    return None

# ================= OVERTRADING KONTROLÜ =================
def can_open_new_trade(symbol):
    now = time.time()

    if symbol in symbol_cooldowns and now < symbol_cooldowns[symbol]:
        wait_sec = int(symbol_cooldowns[symbol] - now)
        return False, f"{symbol} SOĞUMA SÜRESİNDE ({wait_sec} sn)"

    one_hour_ago = now - 3600
    one_day_ago = now - 86400
    recent_hour_trades = [t for t in trade_history_timestamps if t >= one_hour_ago]
    recent_day_trades = [t for t in trade_history_timestamps if t >= one_day_ago]

    if len(recent_hour_trades) >= MAX_TRADES_PER_HOUR:
        return False, "SAATLİK İŞLEM LİMİTİNE ULAŞILDI"

    if len(recent_day_trades) >= MAX_TRADES_PER_DAY:
        return False, "GÜNLÜK İŞLEM LİMİTİNE ULAŞILDI"

    return True, "OK"

# ================= POZİSYON AÇMA VE KAPATMA =================
def execute_trade(symbol, side, price):
    global balance, open_positions, trade_history_timestamps

    trade_amt = balance * TRADE_SIZE_PERCENT
    if trade_amt < 10.0 or len(open_positions) >= MAX_OPEN_POSITIONS:
        return

    fee = trade_amt * COMMISSION_RATE
    balance -= (trade_amt + fee)
    trade_history_timestamps.append(time.time())

    if side == "LONG":
        tp = price * (1 + TAKE_PROFIT_PCT)
        sl = price * (1 - STOP_LOSS_PCT)
    else:
        tp = price * (1 - TAKE_PROFIT_PCT)
        sl = price * (1 + STOP_LOSS_PCT)

    open_positions[symbol] = {
        "side": side,
        "entry_price": price,
        "amount": trade_amt,
        "tp": tp,
        "sl": sl,
        "peak_price": price,
        "trailing_active": False,
        "open_time": time.time()
    }

    icon = "🟢" if side == "LONG" else "🔴"
    print(f"\n[POZİSYON AÇILDI] >> {icon} {side} {symbol}")
    print(f"  Giriş Fiyatı : ${price:.4f} | Bütçe: {trade_amt:.2f} USDT")
    print(f"  Hedef TP     : ${tp:.4f} | Stop SL: ${sl:.4f}")
    print(f"  Kalan Bakiye : {balance:.2f} USDT\n")

def manage_positions():
    global balance, open_positions, symbol_cooldowns, trade_log
    closed = []

    for symbol, pos in open_positions.items():
        df = get_klines_data(symbol)
        if df is None:
            continue

        curr_price = df.iloc[-1]["close"]
        high_price = df.iloc[-1]["high"]
        low_price = df.iloc[-1]["low"]

        side = pos["side"]
        entry = pos["entry_price"]
        amt = pos["amount"]

        exit_reason = None
        exit_price = curr_price

        if side == "LONG":
            pos["peak_price"] = max(pos["peak_price"], high_price)
            peak_pct = (pos["peak_price"] - entry) / entry

            if peak_pct >= TRAILING_TRIGGER_PCT:
                pos["trailing_active"] = True

            if pos["trailing_active"]:
                trail_stop = pos["peak_price"] * (1 - TRAILING_DISTANCE_PCT)
                if low_price <= trail_stop:
                    exit_reason = "TRAILING STOP"
                    exit_price = trail_stop
            elif low_price <= pos["sl"]:
                exit_reason = "STOP LOSS (SL)"
                exit_price = pos["sl"]
            elif high_price >= pos["tp"]:
                exit_reason = "TAKE PROFIT (TP)"
                exit_price = pos["tp"]

        else:  # SHORT
            pos["peak_price"] = min(pos["peak_price"], low_price)
            peak_pct = (entry - pos["peak_price"]) / entry

            if peak_pct >= TRAILING_TRIGGER_PCT:
                pos["trailing_active"] = True

            if pos["trailing_active"]:
                trail_stop = pos["peak_price"] * (1 + TRAILING_DISTANCE_PCT)
                if high_price >= trail_stop:
                    exit_reason = "TRAILING STOP"
                    exit_price = trail_stop
            elif high_price >= pos["sl"]:
                exit_reason = "STOP LOSS (SL)"
                exit_price = pos["sl"]
            elif low_price <= pos["tp"]:
                exit_reason = "TAKE PROFIT (TP)"
                exit_price = pos["tp"]

        if exit_reason:
            if side == "LONG":
                gross_return = amt * (exit_price / entry)
            else:
                gross_return = amt * (1 + (entry - exit_price) / entry)

            fee = gross_return * COMMISSION_RATE
            net_return = gross_return - fee
            pnl = net_return - amt
            balance += net_return
            closed.append(symbol)

            # Kapanan coine 15 dk soğuma süresi
            symbol_cooldowns[symbol] = time.time() + SYMBOL_COOLDOWN_SECONDS

            icon = "✅" if pnl > 0 else "❌"
            print(f"\n[POZİSYON KAPATILDI] << {icon} {side} {symbol} ({exit_reason})")
            print(f"  Giriş/Çıkış: ${entry:.4f} -> ${exit_price:.4f}")
            print(f"  Net K/Z    : {pnl:+.2f} USDT ({((net_return/amt)-1)*100:+.2f}%)")
            print(f"  Güncel Bakiye: {balance:.2f} USDT\n")

            trade_log.insert(0, {
                "symbol": symbol,
                "side": side,
                "reason": exit_reason,
                "entry": round(entry, 4),
                "exit": round(exit_price, 4),
                "pnl": round(pnl, 2),
                "pnl_pct": round((net_return / amt - 1) * 100, 2),
                "time": datetime.utcnow().strftime("%H:%M:%S")
            })
            trade_log[:] = trade_log[:100]

    for sym in closed:
        del open_positions[sym]

# ================= WEB DASHBOARD =================
DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
<title>Trading Bot Panel</title>
<meta http-equiv="refresh" content="10">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
body { background:#0f1117; color:#e5e7eb; font-family: monospace; padding:20px; }
h2 { color:#22c55e; margin-top:30px; }
table { width:100%; border-collapse: collapse; margin-bottom:20px; }
th, td { padding:8px 12px; border-bottom:1px solid #262b36; text-align:left; white-space:nowrap; }
th { color:#9ca3af; text-transform:uppercase; font-size:11px; }
.long { color:#22c55e; } .short { color:#ef4444; }
.pos { color:#22c55e; } .neg { color:#ef4444; }
.balance { font-size:22px; margin-bottom:20px; }
.wrap { overflow-x:auto; }
.empty { color:#6b7280; padding:10px 0; }
</style>
</head>
<body>
<div class="balance">💰 Bakiye: {{ balance }} USDT &nbsp; | &nbsp; Açık Pozisyon: {{ open_count }}</div>

<h2>Açık Pozisyonlar</h2>
<div class="wrap">
<table>
<tr><th>Coin</th><th>Yön</th><th>Giriş</th><th>TP</th><th>SL</th><th>Trailing</th></tr>
{% for sym, p in positions.items() %}
<tr>
<td>{{ sym }}</td>
<td class="{{ 'long' if p.side=='LONG' else 'short' }}">{{ p.side }}</td>
<td>${{ '%.4f'|format(p.entry_price) }}</td>
<td>${{ '%.4f'|format(p.tp) }}</td>
<td>${{ '%.4f'|format(p.sl) }}</td>
<td>{{ 'Aktif' if p.trailing_active else '-' }}</td>
</tr>
{% endfor %}
</table>
{% if not positions %}<div class="empty">Şu anda açık pozisyon yok.</div>{% endif %}
</div>

<h2>Son İşlemler</h2>
<div class="wrap">
<table>
<tr><th>Saat</th><th>Coin</th><th>Yön</th><th>Sonuç</th><th>Giriş→Çıkış</th><th>K/Z</th></tr>
{% for t in trades %}
<tr>
<td>{{ t.time }}</td>
<td>{{ t.symbol }}</td>
<td class="{{ 'long' if t.side=='LONG' else 'short' }}">{{ t.side }}</td>
<td>{{ t.reason }}</td>
<td>${{ t.entry }} → ${{ t.exit }}</td>
<td class="{{ 'pos' if t.pnl >= 0 else 'neg' }}">{{ t.pnl }} USDT ({{ t.pnl_pct }}%)</td>
</tr>
{% endfor %}
</table>
{% if not trades %}<div class="empty">Henüz kapanan işlem yok.</div>{% endif %}
</div>
</body>
</html>
"""

@app.route("/")
def dashboard():
    return render_template_string(
        DASHBOARD_HTML,
        balance=round(balance, 2),
        open_count=len(open_positions),
        positions=open_positions,
        trades=trade_log
    )

@app.route("/api/status")
def api_status():
    return jsonify({
        "balance": round(balance, 2),
        "open_positions": open_positions,
        "trade_history": trade_log
    })

def run_dashboard():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# ================= ANA ÇALIŞMA DÖNGÜSÜ =================
def run_bot():
    print("=" * 55)
    print("🚀 GELİŞMİŞ AL-SAT BOTU BAŞLATILDI (Dashboard Aktif, Max 10 Pozisyon)")
    print(f"  Zaman Dilimi   : {TIMEFRAME}")
    print(f"  Hedefler       : %{TAKE_PROFIT_PCT*100} TP | %{STOP_LOSS_PCT*100} SL")
    print(f"  Trailing Stop  : %{TRAILING_TRIGGER_PCT*100} Kar -> %{TRAILING_DISTANCE_PCT*100} Takip")
    print(f"  Max Pozisyon   : {MAX_OPEN_POSITIONS}")
    print(f"  Başlangıç Kasa : {balance:.2f} USDT")
    print("=" * 55)

    while True:
        try:
            manage_positions()

            if len(open_positions) < MAX_OPEN_POSITIONS:
                symbols = get_top_symbols()
                for sym in symbols:
                    if sym in open_positions:
                        continue

                    can_trade, reason = can_open_new_trade(sym)
                    if not can_trade:
                        continue

                    df = get_klines_data(sym)
                    signal = analyze_signal(df)
                    if signal:
                        curr_price = df.iloc[-1]["close"]
                        execute_trade(sym, signal, curr_price)
                        if len(open_positions) >= MAX_OPEN_POSITIONS:
                            break
                        time.sleep(0.5)

            time.sleep(SCAN_INTERVAL)
        except Exception as e:
            print(f"[!] Döngü hatası: {e}")
            time.sleep(5)

if __name__ == "__main__":
    Thread(target=run_dashboard, daemon=True).start()
    run_bot()