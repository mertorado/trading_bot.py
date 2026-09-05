import json
import math
import time
import urllib.request
from datetime import datetime

# ==========================================
# BOT VE RİSK YÖNETİMİ AYARLARI (DENGELİ SCALPER)
# ==========================================
INITIAL_BALANCE = 1000.0        # Başlangıç sanal bakiyesi (USDT)
TRADE_ALLOCATION_PCT = 0.10     # Her işleme kasanın %10'u
TAKE_PROFIT_PCT = 0.020         # %2.0 Sabit TP (Yedek)
STOP_LOSS_PCT = 0.010           # %1.0 Zarar Durdur
COMMISSION_RATE = 0.001         # %0.1 Komisyon
MAX_OPEN_POSITIONS = 5          # Aynı anda en fazla açık işlem
SCAN_INTERVAL_SEC = 15          # Tarama sıklığı (saniye)

MIN_PRICE = 1.0                 # Taranacak min coin fiyatı ($)
MAX_PRICE = 50.0                # Taranacak max coin fiyatı ($)
MIN_24H_VOLUME_USDT = 5_000_000 # 24s hacmi 5M USDT'nin altındaki coinleri ele
MIN_1M_BAR_VOLUME_USDT = 8_000  # Kapanmış mumda en az 8.000 USDT hacim
MIN_TRADES_PER_1M_CANDLE = 8    # Kapanmış mumda en az 8 işlem
MIN_PREVIOUS_BAR_VOLUME_RATIO = 0.5  # Önceki mum esnek hacim şartı

# === DENGELİ TRAILING STOP AYARLARI ===
TRAILING_ACTIVATION_PCT = 0.010   # %1.0 kârda trailing aktifleşir
TRAILING_STOP_PCT = 0.007         # Zirveden %0.7 geri çekilirse kârı kilitler

API_ENDPOINTS = [
    "https://data-api.binance.vision",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://api.binance.com"
]

def http_get(path):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json'
    }
    for base_url in API_ENDPOINTS:
        url = f"{base_url}{path}"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=8) as response:
                return json.loads(response.read().decode('utf-8'))
        except Exception:
            continue
    raise Exception("Binance API sunucularına erişilemedi.")

# ==========================================
# TEKNİK İNDİKATÖRLER
# ==========================================
def calculate_ema(data, period):
    if len(data) < period:
        return []
    multiplier = 2 / (period + 1)
    ema = [sum(data[:period]) / period]
    for price in data[period:]:
        ema.append((price - ema[-1]) * multiplier + ema[-1])
    return ema

def calculate_macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return None, None
    fast_ema = calculate_ema(closes, fast)
    slow_ema = calculate_ema(closes, slow)

    offset = slow - fast
    aligned_fast = fast_ema[offset:]
    macd_line = [f - s for f, s in zip(aligned_fast, slow_ema)]

    if len(macd_line) < signal:
        return None, None
    signal_line = calculate_ema(macd_line, signal)

    return macd_line[-len(signal_line):], signal_line

# ==========================================
# PİYASA VERİLERİ (YÜKSEK HACİMLİ COINLER)
# ==========================================
def get_candidate_symbols():
    try:
        tickers = http_get("/api/v3/ticker/24hr")
        candidates = []
        for t in tickers:
            sym = t['symbol']
            if not sym.endswith('USDT'):
                continue
            if any(sym.startswith(skip) for skip in ['UP', 'DOWN', 'BEAR', 'BULL', 'USDC', 'BUSD', 'TUSD', 'FDUSD', 'DAI', 'EUR']):
                continue

            price = float(t['lastPrice'])
            vol = float(t['quoteVolume'])

            if MIN_PRICE <= price <= MAX_PRICE and vol >= MIN_24H_VOLUME_USDT:
                candidates.append((sym, vol, price))

        candidates.sort(key=lambda x: x[1], reverse=True)
        return [c[0] for c in candidates[:30]]
    except Exception as e:
        print(f"[HATA] Aday semboller çekilemedi: {e}")
        return []

def get_klines(symbol, interval="1m", limit=65):
    raw = http_get(f"/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}")
    closes = [float(k[4]) for k in raw]
    quote_volumes = [float(k[7]) for k in raw]
    trade_counts = [int(k[8]) for k in raw]
    return closes, quote_volumes, trade_counts

# ==========================================
# LONG & SHORT DAY TRADING MOTORU
# ==========================================
class DualTradingBot:
    def __init__(self):
        self.balance = INITIAL_BALANCE
        self.positions = {}
        print("=== DENGELİ DİNAMİK DAY TRADE BOTU BAŞLATILDI ===")
        print(f"Kasa: {self.balance:.2f} USDT | Min 24s Hacim: ${MIN_24H_VOLUME_USDT/1_000_000:.0f}M | Min 1m Hacim: ${MIN_1M_BAR_VOLUME_USDT:,}")
        print(f"Trailing: %{TRAILING_ACTIVATION_PCT*100} kârda aktif | Takip Mesafesi: %{TRAILING_STOP_PCT*100} | SL: %{STOP_LOSS_PCT*100}\n")

    def analyze_symbol(self, symbol):
        try:
            closes, volumes, trade_counts = get_klines(symbol, interval="1m", limit=65)
            if len(closes) < 60:
                return None, 0.0

            closed_closes = closes[:-1]
            closed_volumes = volumes[:-1]
            closed_trade_counts = trade_counts[:-1]
            current_price = closed_closes[-1]
            current_vol_usdt = closed_volumes[-1]
            current_trade_count = closed_trade_counts[-1]
            previous_vol_usdt = closed_volumes[-2]
            avg_vol_usdt = sum(closed_volumes[-22:-2]) / 20

            # 1. Hacim ve işlem sayısı taban kontrolleri
            if current_vol_usdt < MIN_1M_BAR_VOLUME_USDT:
                return None, current_price
            if current_trade_count < MIN_TRADES_PER_1M_CANDLE:
                return None, current_price

            # 2. Hacim artış çarpanı (2.0x)
            if avg_vol_usdt == 0 or (current_vol_usdt / avg_vol_usdt) < 2.0:
                return None, current_price

            # 3. Önceki mum kontrolü
            if previous_vol_usdt < avg_vol_usdt * MIN_PREVIOUS_BAR_VOLUME_RATIO:
                return None, current_price

            # 4. EMA50 trend göstergesi
            ema50 = calculate_ema(closed_closes, 50)
            if not ema50:
                return None, current_price
            current_ema50 = ema50[-1]

            # 5. MACD kesişim göstergesi
            macd_line, signal_line = calculate_macd(closed_closes)
            if not macd_line or len(macd_line) < 2:
                return None, current_price

            prev_macd, prev_sig = macd_line[-2], signal_line[-2]
            curr_macd, curr_sig = macd_line[-1], signal_line[-1]

            # LONG Sinyali: MACD yukarı kesti VE Fiyat > EMA50
            if (prev_macd <= prev_sig) and (curr_macd > curr_sig) and (current_price > current_ema50):
                print(f"\n  [+] TEYİTLİ LONG: {symbol} | Fiyat: ${current_price:.4f} | 1m Hacim: ${current_vol_usdt:,.0f} ({current_vol_usdt/avg_vol_usdt:.1f}x) | İşlem: {current_trade_count}")
                return "LONG", current_price

            # SHORT Sinyali: MACD aşağı kesti VE Fiyat < EMA50
            if (prev_macd >= prev_sig) and (curr_macd < curr_sig) and (current_price < current_ema50):
                print(f"\n  [-] YÜKSEK HACİMLİ SHORT: {symbol} | Fiyat: ${current_price:.4f} | 1m Hacim: ${current_vol_usdt:,.0f} ({current_vol_usdt/avg_vol_usdt:.1f}x)")
                return "SHORT", current_price

            return None, current_price
        except Exception:
            return None, 0.0

    def check_open_positions(self):
        for symbol, pos in list(self.positions.items()):
            try:
                closes, _, _ = get_klines(symbol, interval="1m", limit=2)
                current_price = closes[-1]
                entry_price = pos['entry_price']
                side = pos['side']

                if side == "LONG":
                    pnl_pct = (current_price - entry_price) / entry_price
                else:
                    pnl_pct = (entry_price - current_price) / entry_price

                # Trailing stop aktivasyonu
                if pnl_pct >= TRAILING_ACTIVATION_PCT and not pos['trailing_activated']:
                    pos['trailing_activated'] = True

                    if side == "LONG":
                        pos['trailing_stop_price'] = current_price * (1 - TRAILING_STOP_PCT)
                        pos['highest_price'] = current_price
                    else:
                        pos['trailing_stop_price'] = current_price * (1 + TRAILING_STOP_PCT)
                        pos['lowest_price'] = current_price

                    print(f"  [TRAILING AKTİF] {symbol} | Trailing Stop: ${pos['trailing_stop_price']:.4f}")

                # Trailing stop takibi
                if pos['trailing_activated']:
                    if side == "LONG":
                        if current_price > pos['highest_price']:
                            pos['highest_price'] = current_price
                            pos['trailing_stop_price'] = current_price * (1 - TRAILING_STOP_PCT)

                        if current_price <= pos['trailing_stop_price']:
                            self.close_position(symbol, current_price, "TRAILING STOP")
                            continue
                    else:
                        if current_price < pos['lowest_price']:
                            pos['lowest_price'] = current_price
                            pos['trailing_stop_price'] = current_price * (1 + TRAILING_STOP_PCT)

                        if current_price >= pos['trailing_stop_price']:
                            self.close_position(symbol, current_price, "TRAILING STOP")
                            continue

                # Trailing aktif olmadan önceki yedek TP ve SL
                if not pos['trailing_activated']:
                    if pnl_pct >= TAKE_PROFIT_PCT:
                        self.close_position(symbol, current_price, "KÂR AL (TP)")
                    elif pnl_pct <= -STOP_LOSS_PCT:
                        self.close_position(symbol, current_price, "ZARAR DURDUR (SL)")

            except Exception as e:
                print(f"[HATA] Pozisyon takip hatası ({symbol}): {e}")

    def open_position(self, symbol, side, price):
        if len(self.positions) >= MAX_OPEN_POSITIONS or symbol in self.positions:
            return

        trade_amount = self.balance * TRADE_ALLOCATION_PCT
        if trade_amount < 10.0:
            return

        commission = trade_amount * COMMISSION_RATE
        self.balance -= (trade_amount + commission)
        quantity = trade_amount / price

        if side == "LONG":
            tp_price = price * (1 + TAKE_PROFIT_PCT)
            sl_price = price * (1 - STOP_LOSS_PCT)
        else:
            tp_price = price * (1 - TAKE_PROFIT_PCT)
            sl_price = price * (1 + STOP_LOSS_PCT)

        self.positions[symbol] = {
            'side': side,
            'entry_price': price,
            'quantity': quantity,
            'cost_usdt': trade_amount,
            'tp_price': tp_price,
            'sl_price': sl_price,
            'timestamp': time.time(),
            'entry_time': datetime.now().strftime("%H:%M:%S"),
            'trailing_activated': False,
            'trailing_stop_price': None,
            'highest_price': price if side == "LONG" else None,
            'lowest_price': price if side == "SHORT" else None
        }

        icon = "🟢 LONG" if side == "LONG" else "🔴 SHORT"
        print(f"\n[POZİSYON AÇILDI] >> {icon} {symbol}")
        print(f"  Giriş Fiyatı : ${price:.4f}")
        print(f"  Tutar        : {trade_amount:.2f} USDT")
        print(f"  Hedef TP     : ${tp_price:.4f}")
        print(f"  Stop SL      : ${sl_price:.4f}")
        print(f"  Kalan Bakiye : {self.balance:.2f} USDT\n")

    def close_position(self, symbol, exit_price, reason):
        pos = self.positions.pop(symbol)
        side = pos['side']
        entry_price = pos['entry_price']

        if side == "LONG":
            raw_pnl = pos['quantity'] * (exit_price - entry_price)
        else:
            raw_pnl = pos['quantity'] * (entry_price - exit_price)

        exit_value = pos['cost_usdt'] + raw_pnl
        commission = exit_value * COMMISSION_RATE
        net_return = exit_value - commission
        net_pnl = net_return - pos['cost_usdt']
        pnl_pct = (net_pnl / pos['cost_usdt']) * 100

        self.balance += net_return

        print(f"\n[POZİSYON KAPATILDI] << {side} {symbol} ({reason})")
        print(f"  Giriş / Çıkış: ${entry_price:.4f} -> ${exit_price:.4f}")
        print(f"  Net K/Z      : {net_pnl:+.2f} USDT ({pnl_pct:+.2f}%)")
        print(f"  Güncel Bakiye: {self.balance:.2f} USDT\n")

    def run(self):
        print("Piyasa taranıyor (Sadece aktif ve yüksek likiditeye sahip coinler izleniyor)...\n")
        while True:
            try:
                if self.positions:
                    self.check_open_positions()

                if len(self.positions) < MAX_OPEN_POSITIONS:
                    candidates = get_candidate_symbols()
                    for sym in candidates:
                        if sym in self.positions:
                            continue
                        side, price = self.analyze_symbol(sym)
                        if side:
                            self.open_position(sym, side, price)
                            if len(self.positions) >= MAX_OPEN_POSITIONS:
                                break
                        time.sleep(0.05)

                now = datetime.now().strftime("%H:%M:%S")
                status = "[{}] Tarama tamamlandı. Açık: {}/{} | Bakiye: {:.2f} USDT".format(
                    now, len(self.positions), MAX_OPEN_POSITIONS, self.balance
                )
                print(status)
                time.sleep(SCAN_INTERVAL_SEC)

            except Exception as e:
                print(f"[DÖNGÜ HATASI]: {e}")
                time.sleep(SCAN_INTERVAL_SEC)

bot = DualTradingBot()
bot.run()