import os
import time
from datetime import datetime, timedelta
import requests
import pandas as pd
import numpy as np

# ================= STRATEJİ VE ZAMANLAMA AYARLARI =================
TIMEFRAME = "3m"               # Güvenilir 3 dakikalık mumlar
SCAN_INTERVAL = 20             # 20 saniyede bir tarama
TRADE_SIZE_PERCENT = 0.10      # Her işlemde bakiyenin %10'u
MAX_OPEN_POSITIONS = 10        # Aynı anda maksimum 10 açık pozisyon
INITIAL_BALANCE = 1000.0       # Başlangıç paper trading kasası

# ================= KÂR / ZARAR VE RİSK YÖNETİMİ =================
TAKE_PROFIT_PCT = 0.025        # %2.5 Sabit Kâr Al hedefi
STOP_LOSS_PCT = 0.015          # %1.5 Sabit Stop Loss (pozisyon bazlı, kalıyor)
TRAILING_TRIGGER_PCT = 0.012   # %1.2 kâra ulaşınca Trailing Stop aktif olur
TRAILING_DISTANCE_PCT = 0.006  # Zirveden %0.6 geri çekilirse satış yapılır
COMMISSION_RATE = 0.001        # %0.1 Binance Spot komisyon oranı

# ================= OVERTRADING KORUMALARI (Zarar kesme kaldırıldı) =================
MAX_TRADES_PER_HOUR = 15       # 10 pozisyona kadar rahatça açabilsin diye artırıldı
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
        prev["macd"] <= prev["macd_signal"