import os
import time
import threading
from datetime import datetime, timezone

import pandas as pd
import requests
from flask import Flask, jsonify, render_template_string


# ============================================================
# AYARLAR
# ============================================================

MARKET_DATA_BASE_URL = "https://api.exchange.coinbase.com"

TIMEFRAME = "3m"
COINBASE_GRANULARITY_SECONDS = 60
RESAMPLE_MINUTES = 3

SCAN_INTERVAL_SECONDS = 20

INITIAL_BALANCE = 1000.0

# İşlem başına maksimum allocation (%10)
TRADE_SIZE_PERCENT = 0.10

MAX_OPEN_POSITIONS = 10

# Alış ve satışta simüle edilen komisyon oranı
# 0.001 = %0.10
COMMISSION_RATE = 0.001

MAX_TRADES_PER_HOUR = 15
MAX_TRADES_PER_DAY = 50

SYMBOL_COOLDOWN_SECONDS = 900

MIN_24H_VOLUME_USDT = 5_000_000
VOLUME_SPIKE_FACTOR = 1.8

MIN_PRICE = 0.05
MAX_PRICE = 100.0

MAX_SYMBOLS_TO_SCAN = 40


# ============================================================
# ATR BAZLI DİNAMİK RİSK SİSTEMİ
# ============================================================

# Her işlemde hesabın yalnızca %0.5'i riske girer
RISK_PER_TRADE_PCT = 0.005

# ATR hesaplama periyodu (tamamlanmış mumlar üzerinde)
ATR_PERIOD = 14

# Stop mesafesi = ATR × ATR_STOP_MULTIPLIER
ATR_STOP_MULTIPLIER = 1.5

# Take-profit mesafesi = stop mesafesi × RISK_REWARD_RATIO
RISK_REWARD_RATIO = 1.8

# Trailing stop, kâr bu R katına ulaşınca aktifleşir
TRAILING_TRIGGER_R_MULTIPLE = 1.0

# Trailing stop mesafesi (R cinsinden)
TRAILING_DISTANCE_R_MULTIPLE = 0.75

# Stop mesafesi yüzde olarak bu sınırlar arasında tutulur
MIN_STOP_DISTANCE_PCT = 0.006
MAX_STOP_DISTANCE_PCT = 0.030


# ============================================================
# HYBRID MOMENTUM AYARLARI
# ============================================================

HYBRID_ENABLED = True

# Aynı anda en fazla iki momentum pozisyonu
MAX_MOMENTUM_POSITIONS = 2

# 1 dakikalık mum hacmi, son 20 mum ortalamasının
# en az bu katı olmalı
MOMENTUM_VOLUME_SPIKE_FACTOR = 2.0

# Kırılma için geriye bakılacak 1 dakikalık mum sayısı
MOMENTUM_BREAKOUT_LOOKBACK = 5

# Kırılma fiyatı eski seviyeden bu orandan fazla uzaklaşmışsa
# bot hareketi kovalamaz
MOMENTUM_MAX_EXTENSION_PCT = 0.015


# ============================================================
# UYGULAMA DURUMU
# ============================================================

balance = INITIAL_BALANCE

open_positions = {}
trade_log = []
trade_history_timestamps = []
symbol_cooldowns = {}

state_lock = threading.RLock()

app = Flask(__name__)


# ============================================================
# YARDIMCI FONKSİYONLAR
# ============================================================

def utc_time_string():
    return datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def candle_time_string(value):
    try:
        return pd.Timestamp(value).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
    except Exception:
        return utc_time_string()


def is_valid_product(product):
    if not isinstance(product, dict):
        return False

    product_id = product.get("id", "")
    base_currency = product.get("base_currency", "")
    quote_currency = product.get("quote_currency", "")
    status = product.get("status", "")

    if not product_id or not base_currency:
        return False

    if quote_currency != "USD":
        return False

    if status not in ("online", "active"):
        return False

    forbidden_assets = {
        "UP",
        "DOWN",
        "BEAR",
        "BULL",
        "3L",
        "3S",
        "5L",
        "5S",
    }

    base_currency = base_currency.upper()

    # Stablecoinleri kesin olarak dışarıda bırak
    forbidden_stablecoins = {
        "USDT",
        "USDC",
        "DAI",
    }

    if base_currency in forbidden_stablecoins:
        return False

    if base_currency in forbidden_assets:
        return False

    for forbidden in forbidden_assets:
        if base_currency.endswith(forbidden):
            return False

    return True


def cleanup_old_trade_timestamps():
    one_day_ago = time.time() - 86400

    with state_lock:
        trade_history_timestamps[:] = [
            timestamp
            for timestamp in trade_history_timestamps
            if timestamp >= one_day_ago
        ]


def count_strategy_trades(strategy):
    return sum(
        1
        for trade in trade_log
        if trade.get("strategy", "NORMAL") == strategy
    )


def strategy_summary(strategy):
    trades = [
        trade
        for trade in trade_log
        if trade.get("strategy", "NORMAL") == strategy
    ]

    wins = [
        trade
        for trade in trades
        if safe_float(trade.get("pnl")) >= 0
    ]

    losses = [
        trade
        for trade in trades
        if safe_float(trade.get("pnl")) < 0
    ]

    total_pnl = sum(
        safe_float(trade.get("pnl"))
        for trade in trades
    )

    win_rate = (
        len(wins) / len(trades) * 100
        if trades
        else 0.0
    )

    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 2),
        "total_pnl": round(total_pnl, 4),
    }



# ============================================================
# COINBASE PİYASA VERİSİ
# ============================================================

def get_top_symbols():
    """
    Coinbase USD paritelerini alır.

    Coinbase ticker verisindeki 24 saatlik base hacim,
    fiyatla çarpılarak yaklaşık USD hacmine çevrilir.
    """

    url = f"{MARKET_DATA_BASE_URL}/products"

    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()

        products = response.json()

        if not isinstance(products, list):
            print("[!] Coinbase ürün API'si liste döndürmedi")
            return []

        candidates = []

        for product in products:
            if not is_valid_product(product):
                continue

            symbol = product.get("id")

            ticker_url = (
                f"{MARKET_DATA_BASE_URL}"
                f"/products/{symbol}/ticker"
            )

            try:
                ticker_response = requests.get(
                    ticker_url,
                    timeout=10
                )
                ticker_response.raise_for_status()

                ticker = ticker_response.json()

                price = safe_float(ticker.get("price"))
                base_volume = safe_float(ticker.get("volume"))

                if price <= 0 or base_volume <= 0:
                    continue

                volume_usd = base_volume * price

                if volume_usd < MIN_24H_VOLUME_USDT:
                    continue

                if not MIN_PRICE <= price <= MAX_PRICE:
                    continue

                candidates.append((symbol, volume_usd))

            except requests.exceptions.RequestException:
                continue

            except (TypeError, ValueError):
                continue

        candidates.sort(
            key=lambda item: item[1],
            reverse=True
        )

        symbols = [
            symbol
            for symbol, volume in candidates[:MAX_SYMBOLS_TO_SCAN]
        ]

        print(f"[+] {len(symbols)} Coinbase aday sembol bulundu")

        return symbols

    except requests.exceptions.HTTPError as error:
        print(f"[!] Coinbase ürün API HTTP hatası: {error}")

        try:
            print(f"[!] API cevabı: {response.text[:300]}")
        except Exception:
            pass

        return []

    except requests.exceptions.RequestException as error:
        print(f"[!] Coinbase bağlantı hatası: {error}")
        return []

    except ValueError as error:
        print(f"[!] Coinbase JSON hatası: {error}")
        return []

    except Exception as error:
        print(f"[!] Sembol listesi hatası: {error}")
        return []


def get_klines_data(symbol, resample_minutes=3):
    """
    Coinbase'den 1 dakikalık mumları alır ve istenen zaman
    dilimine yeniden örnekler.

    resample_minutes=3:
        Ana strateji için 3 dakikalık mumlar

    resample_minutes=1:
        Hybrid momentum stratejisi için 1 dakikalık mumlar

    Coinbase candle formatı:

    [timestamp, low, high, open, close, volume]

    Coinbase mumları en yeni mum ilk sırada olacak şekilde
    döner; bu yüzden veriler datetime'a göre artan sıraya
    getirilir. Sinyal ve pozisyon yönetimi her zaman
    tamamlanmış mumlar (iloc[-2] ve öncesi) üzerinden yapılır;
    en son satır (iloc[-1]) henüz açık olan mumdur ve
    kullanılmaz.
    """

    url = (
        f"{MARKET_DATA_BASE_URL}"
        f"/products/{symbol}/candles"
    )

    params = {
        "granularity": COINBASE_GRANULARITY_SECONDS
    }

    try:
        response = requests.get(
            url,
            params=params,
            timeout=10
        )
        response.raise_for_status()

        data = response.json()

        if not isinstance(data, list):
            return None

        rows = []

        for candle in data:
            if not isinstance(candle, list):
                continue

            if len(candle) < 6:
                continue

            rows.append(
                {
                    "timestamp": safe_float(candle[0]),
                    "low": safe_float(candle[1]),
                    "high": safe_float(candle[2]),
                    "open": safe_float(candle[3]),
                    "close": safe_float(candle[4]),
                    "volume": safe_float(candle[5]),
                }
            )

        if len(rows) < 30:
            return None

        dataframe = pd.DataFrame(rows)

        dataframe["datetime"] = pd.to_datetime(
            dataframe["timestamp"],
            unit="s",
            utc=True
        )

        # Coinbase mumları en yeni önce döner; artan sıraya al.
        dataframe = dataframe.sort_values("datetime")

        dataframe = dataframe.drop_duplicates(
            subset=["datetime"]
        )

        dataframe = dataframe.set_index("datetime")

        dataframe = dataframe.resample(
            f"{resample_minutes}min",
            label="left",
            closed="left"
        ).agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        )

        dataframe = dataframe.dropna().reset_index()

        minimum_rows = 65 if resample_minutes == 3 else 25

        if len(dataframe) < minimum_rows:
            return None

        dataframe["open_time"] = (
            dataframe["datetime"].astype("int64") // 10**9
        )

        dataframe["close_time"] = (
            dataframe["open_time"] + (resample_minutes * 60)
        )

        dataframe["quote_volume"] = (
            dataframe["close"] * dataframe["volume"]
        )

        return dataframe

    except requests.exceptions.HTTPError as error:
        print(
            f"[!] Coinbase mum HTTP hatası {symbol}: {error}"
        )
        return None

    except requests.exceptions.RequestException:
        return None

    except (ValueError, TypeError):
        return None

    except Exception as error:
        print(f"[!] Mum verisi hatası {symbol}: {error}")
        return None



# ============================================================
# TEKNİK İNDİKATÖRLER
# ============================================================

def calculate_atr(dataframe, period=ATR_PERIOD):
    """
    Wilder'ın ATR (Average True Range) hesaplaması.

    True Range = max(
        high - low,
        abs(high - önceki close),
        abs(low - önceki close)
    )

    ATR, True Range'in Wilder yumuşatmasıyla (alpha = 1/period)
    üstel hareketli ortalamasıdır. Sonuç dataframe'e "atr"
    kolonu olarak eklenir.
    """

    dataframe = dataframe.copy()

    high = dataframe["high"]
    low = dataframe["low"]
    close = dataframe["close"]

    previous_close = close.shift(1)

    high_low = high - low
    high_close = (high - previous_close).abs()
    low_close = (low - previous_close).abs()

    true_range = pd.concat(
        [high_low, high_close, low_close],
        axis=1
    ).max(axis=1)

    # Wilder yumuşatması: alpha = 1 / period
    dataframe["atr"] = true_range.ewm(
        alpha=1.0 / period,
        adjust=False,
        min_periods=period
    ).mean()

    return dataframe


def get_completed_atr(dataframe, period=ATR_PERIOD):
    """
    Tamamlanmış son mumun (iloc[-2]) ATR değerini döndürür.

    Açık olan son mum (iloc[-1]) kullanılmaz. Geçerli bir ATR
    yoksa None döner.
    """

    if dataframe is None or len(dataframe) < period + 2:
        return None

    atr_frame = calculate_atr(dataframe, period)

    atr_value = safe_float(atr_frame.iloc[-2]["atr"])

    if pd.isna(atr_value) or atr_value <= 0:
        return None

    return atr_value


def calculate_indicators(dataframe):
    dataframe = dataframe.copy()

    close = dataframe["close"]

    dataframe["ema_50"] = close.ewm(
        span=50,
        adjust=False
    ).mean()

    ema_12 = close.ewm(span=12, adjust=False).mean()
    ema_26 = close.ewm(span=26, adjust=False).mean()

    dataframe["macd"] = ema_12 - ema_26

    dataframe["macd_signal"] = dataframe["macd"].ewm(
        span=9,
        adjust=False
    ).mean()

    price_change = close.diff()

    gains = price_change.where(price_change > 0, 0)
    losses = -price_change.where(price_change < 0, 0)

    average_gain = gains.rolling(14).mean()
    average_loss = losses.rolling(14).mean()

    relative_strength = average_gain / (average_loss + 1e-9)

    dataframe["rsi"] = 100 - (100 / (1 + relative_strength))

    dataframe["volume_ma"] = dataframe["volume"].rolling(20).mean()

    return dataframe


def analyze_signal(dataframe):
    """
    Ana 3 dakikalık strateji.

    Sadece tamamlanmış mumlar kullanılır (iloc[-2] güncel
    tamamlanmış mum, iloc[-3] bir önceki tamamlanmış mum).
    """

    if dataframe is None or len(dataframe) < 60:
        return None

    dataframe = calculate_indicators(dataframe)

    previous_candle = dataframe.iloc[-3]
    current_candle = dataframe.iloc[-2]

    required_values = [
        previous_candle["close"],
        current_candle["close"],
        current_candle["ema_50"],
        previous_candle["macd"],
        previous_candle["macd_signal"],
        current_candle["macd"],
        current_candle["macd_signal"],
        current_candle["rsi"],
        current_candle["volume"],
        current_candle["volume_ma"],
    ]

    if any(pd.isna(value) for value in required_values):
        return None

    volume_is_strong = (
        current_candle["volume"]
        >= current_candle["volume_ma"] * VOLUME_SPIKE_FACTOR
    )

    long_signal = (
        current_candle["close"] > current_candle["ema_50"]
        and previous_candle["macd"] <= previous_candle["macd_signal"]
        and current_candle["macd"] > current_candle["macd_signal"]
        and 40 <= current_candle["rsi"] <= 65
        and volume_is_strong
    )

    if long_signal:
        return "LONG"

    short_signal = (
        current_candle["close"] < current_candle["ema_50"]
        and previous_candle["macd"] >= previous_candle["macd_signal"]
        and current_candle["macd"] < current_candle["macd_signal"]
        and 35 <= current_candle["rsi"] <= 60
        and volume_is_strong
    )

    if short_signal:
        return "SHORT"

    return None


def analyze_momentum_signal(dataframe_1m, dataframe_3m):
    """
    Hybrid stratejisi:

    - Ana yönü tamamlanmış 3 dakikalık mum belirler.
    - Giriş sinyalini tamamlanmış 1 dakikalık mum arar.
    - Hacim patlaması ve kısa vadeli kırılma aranır.
    - Çok uzamış hareketlerin tepesinden giriş yapılmaz.
    """

    if dataframe_1m is None or dataframe_3m is None:
        return None

    if len(dataframe_1m) < 25:
        return None

    if len(dataframe_3m) < 60:
        return None

    trend_data = calculate_indicators(dataframe_3m)

    momentum_data = dataframe_1m.copy()

    momentum_data["volume_ma"] = (
        momentum_data["volume"].rolling(20).mean()
    )

    trend_candle = trend_data.iloc[-2]
    current_candle = momentum_data.iloc[-2]

    lookback_start = -(MOMENTUM_BREAKOUT_LOOKBACK + 2)

    previous_candles = momentum_data.iloc[lookback_start:-2]

    required_values = [
        trend_candle["close"],
        trend_candle["ema_50"],
        trend_candle["macd"],
        trend_candle["macd_signal"],
        current_candle["open"],
        current_candle["close"],
        current_candle["high"],
        current_candle["low"],
        current_candle["volume"],
        current_candle["volume_ma"],
    ]

    if any(pd.isna(value) for value in required_values):
        return None

    if previous_candles.empty:
        return None

    previous_high = safe_float(previous_candles["high"].max())
    previous_low = safe_float(previous_candles["low"].min())

    current_open = safe_float(current_candle["open"])
    current_close = safe_float(current_candle["close"])
    current_volume = safe_float(current_candle["volume"])
    volume_average = safe_float(current_candle["volume_ma"])

    if (
        previous_high <= 0
        or previous_low <= 0
        or current_close <= 0
    ):
        return None

    volume_is_strong = (
        current_volume
        >= volume_average * MOMENTUM_VOLUME_SPIKE_FACTOR
    )

    long_trend = (
        trend_candle["close"] > trend_candle["ema_50"]
        and trend_candle["macd"] > trend_candle["macd_signal"]
    )

    short_trend = (
        trend_candle["close"] < trend_candle["ema_50"]
        and trend_candle["macd"] < trend_candle["macd_signal"]
    )

    long_breakout = (
        current_close > previous_high
        and current_close > current_open
    )

    short_breakout = (
        current_close < previous_low
        and current_close < current_open
    )

    if long_breakout:
        extension_pct = (
            current_close - previous_high
        ) / previous_high

        if extension_pct > MOMENTUM_MAX_EXTENSION_PCT:
            long_breakout = False

    if short_breakout:
        extension_pct = (
            previous_low - current_close
        ) / previous_low

        if extension_pct > MOMENTUM_MAX_EXTENSION_PCT:
            short_breakout = False

    if long_trend and long_breakout and volume_is_strong:
        return "LONG"

    if short_trend and short_breakout and volume_is_strong:
        return "SHORT"

    return None


# ============================================================
# İŞLEM KONTROLLERİ
# ============================================================

def can_open_new_trade(symbol):
    now = time.time()

    with state_lock:
        cooldown_until = symbol_cooldowns.get(symbol, 0)

        if now < cooldown_until:
            return False, "Coin soğuma süresinde"

        one_hour_ago = now - 3600
        one_day_ago = now - 86400

        hourly_count = sum(
            timestamp >= one_hour_ago
            for timestamp in trade_history_timestamps
        )

        daily_count = sum(
            timestamp >= one_day_ago
            for timestamp in trade_history_timestamps
        )

        if hourly_count >= MAX_TRADES_PER_HOUR:
            return False, "Saatlik işlem limiti"

        if daily_count >= MAX_TRADES_PER_DAY:
            return False, "Günlük işlem limiti"

        if len(open_positions) >= MAX_OPEN_POSITIONS:
            return False, "Açık pozisyon limiti"

        return True, "OK"



# ============================================================
# ATR BAZLI POZİSYON BÜYÜKLÜĞÜ VE RİSK HESABI
# ============================================================

def compute_risk_parameters(side, price, atr_value, current_balance):
    """
    ATR bazlı dinamik risk hesaplaması.

    Adımlar:
      1. Stop mesafesi = ATR × ATR_STOP_MULTIPLIER
      2. Stop mesafesi yüzde olarak MIN_STOP_DISTANCE_PCT ve
         MAX_STOP_DISTANCE_PCT arasında sınırlandırılır.
      3. Long / short'a göre stop_loss ve take_profit belirlenir.
         take_profit mesafesi = stop mesafesi × RISK_REWARD_RATIO
      4. Pozisyon büyüklüğü, hesabın yalnızca
         RISK_PER_TRADE_PCT (%0.5) kadarı riske girecek şekilde
         hesaplanır: trade_amount = risk_tutarı / stop_mesafesi_yüzdesi
      5. Pozisyon büyüklüğü %10 allocation sınırını (TRADE_SIZE_PERCENT)
         aşamaz.

    Geçersiz girdi durumunda None döner.
    """

    if price <= 0 or atr_value <= 0 or current_balance <= 0:
        return None

    # 1) ATR bazlı ham stop mesafesi (fiyat cinsinden)
    raw_stop_distance = atr_value * ATR_STOP_MULTIPLIER

    # 2) Yüzdeye çevir ve sınırlar arasında tut
    stop_distance_pct = raw_stop_distance / price

    stop_distance_pct = max(
        MIN_STOP_DISTANCE_PCT,
        min(stop_distance_pct, MAX_STOP_DISTANCE_PCT)
    )

    stop_distance = price * stop_distance_pct

    # 3) Long / short'a göre stop ve hedef
    take_profit_distance = stop_distance * RISK_REWARD_RATIO

    if side == "LONG":
        stop_loss = price - stop_distance
        take_profit = price + take_profit_distance
    else:
        stop_loss = price + stop_distance
        take_profit = price - take_profit_distance

    # 4) Sadece hesabın %0.5'i riske girecek şekilde büyüklük
    risk_amount = current_balance * RISK_PER_TRADE_PCT
    trade_amount = risk_amount / stop_distance_pct

    # 5) %10 allocation sınırı
    max_allocation = current_balance * TRADE_SIZE_PERCENT
    trade_amount = min(trade_amount, max_allocation)

    return {
        "stop_distance": stop_distance,
        "stop_distance_pct": stop_distance_pct,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "risk_amount": risk_amount,
        "trade_amount": trade_amount,
    }


# ============================================================
# POZİSYON AÇMA
# ============================================================

def execute_trade(
    symbol,
    side,
    price,
    entry_candle_time,
    atr_value,
    strategy="NORMAL"
):
    global balance

    with state_lock:
        if symbol in open_positions:
            return False

        if len(open_positions) >= MAX_OPEN_POSITIONS:
            return False

        if strategy == "MOMENTUM":
            momentum_count = sum(
                position.get("strategy") == "MOMENTUM"
                for position in open_positions.values()
            )

            if momentum_count >= MAX_MOMENTUM_POSITIONS:
                return False

        if atr_value is None or atr_value <= 0:
            print(f"[!] {symbol} için geçerli ATR yok, işlem atlandı")
            return False

        risk = compute_risk_parameters(
            side,
            price,
            atr_value,
            balance
        )

        if risk is None:
            print(f"[!] {symbol} için risk parametreleri hesaplanamadı")
            return False

        trade_amount = risk["trade_amount"]

        if trade_amount < 10:
            print("[!] İşlem tutarı 10 USDT altında")
            return False

        entry_fee = trade_amount * COMMISSION_RATE

        if balance < trade_amount + entry_fee:
            print("[!] Yeterli paper trading bakiyesi yok")
            return False

        stop_loss = risk["stop_loss"]
        take_profit = risk["take_profit"]
        stop_distance = risk["stop_distance"]
        stop_distance_pct = risk["stop_distance_pct"]

        balance -= trade_amount + entry_fee

        entry_time = candle_time_string(entry_candle_time)

        open_positions[symbol] = {
            "symbol": symbol,
            "side": side,
            "strategy": strategy,
            "entry_price": price,
            "amount": trade_amount,
            "entry_fee": entry_fee,
            "atr": atr_value,
            "stop_distance": stop_distance,
            "stop_distance_pct": stop_distance_pct,
            "take_profit": take_profit,
            "stop_loss": stop_loss,
            "peak_price": price,
            "trailing_active": False,
            "open_time": utc_time_string(),
            "entry_candle_time": entry_time,
            "last_price": price,
            "unrealized_pnl": 0.0,
            "post_entry_high": price,
            "post_entry_low": price,
            "post_entry_high_time": entry_time,
            "post_entry_low_time": entry_time,
        }

        trade_history_timestamps.append(time.time())

        risk_amount = risk["risk_amount"]

        print()
        print(f"[POZİSYON AÇILDI] {strategy} {side} {symbol}")
        print(f"  Strateji         : {strategy}")
        print(f"  Giriş fiyatı     : ${price:.8f}")
        print(f"  ATR              : {atr_value:.8f}")
        print(
            f"  Stop mesafesi    : ${stop_distance:.8f} "
            f"(%{stop_distance_pct * 100:.3f})"
        )
        print(f"  İşlem tutarı     : {trade_amount:.2f} USDT")
        print(
            f"  Riske edilen     : {risk_amount:.4f} USDT "
            f"(%{RISK_PER_TRADE_PCT * 100:.2f})"
        )
        print(f"  TP (1R x {RISK_REWARD_RATIO}) : ${take_profit:.8f}")
        print(f"  SL               : ${stop_loss:.8f}")
        print(f"  Kalan bakiye     : {balance:.2f} USDT")
        print()

        return True



# ============================================================
# POZİSYON YÖNETİMİ
# ============================================================

def calculate_unrealized_pnl(position, current_price):
    entry_price = position["entry_price"]
    amount = position["amount"]
    side = position["side"]

    if side == "LONG":
        gross_value = amount * (current_price / entry_price)
    else:
        gross_value = amount * (
            1 + (entry_price - current_price) / entry_price
        )

    estimated_exit_fee = gross_value * COMMISSION_RATE

    return (
        gross_value
        - estimated_exit_fee
        - amount
        - position["entry_fee"]
    )


def close_position(symbol, position, exit_price, exit_reason):
    global balance

    entry_price = position["entry_price"]
    amount = position["amount"]
    entry_fee = position["entry_fee"]
    side = position["side"]

    if side == "LONG":
        gross_return = amount * (exit_price / entry_price)
    else:
        gross_return = amount * (
            1 + (entry_price - exit_price) / entry_price
        )

    exit_fee = gross_return * COMMISSION_RATE
    net_return = gross_return - exit_fee

    total_pnl = net_return - amount - entry_fee

    balance += net_return

    symbol_cooldowns[symbol] = (
        time.time() + SYMBOL_COOLDOWN_SECONDS
    )

    pnl_percent = (total_pnl / amount) * 100

    post_entry_high = position.get("post_entry_high", entry_price)
    post_entry_low = position.get("post_entry_low", entry_price)

    high_time = position.get(
        "post_entry_high_time",
        position.get("open_time", "")
    )

    low_time = position.get(
        "post_entry_low_time",
        position.get("open_time", "")
    )

    # Çıkış fiyatını da ekstrem takibine dahil et.
    # Böylece hızlı stoplarda veri 0% kalmaz.
    if exit_price > post_entry_high:
        post_entry_high = exit_price
        high_time = utc_time_string()

    if exit_price < post_entry_low:
        post_entry_low = exit_price
        low_time = utc_time_string()

    if side == "LONG":
        max_favorable_move_pct = (
            (post_entry_high - entry_price) / entry_price
        ) * 100

        max_adverse_move_pct = (
            (post_entry_low - entry_price) / entry_price
        ) * 100
    else:
        max_favorable_move_pct = (
            (entry_price - post_entry_low) / entry_price
        ) * 100

        max_adverse_move_pct = (
            (entry_price - post_entry_high) / entry_price
        ) * 100

    strategy = position.get("strategy", "NORMAL")

    trade_entry = {
        "symbol": symbol,
        "strategy": strategy,
        "side": side,
        "reason": exit_reason,
        "entry": round(entry_price, 8),
        "exit": round(exit_price, 8),
        "pnl": round(total_pnl, 4),
        "pnl_pct": round(pnl_percent, 4),
        "time": utc_time_string(),
        "post_entry_high": round(post_entry_high, 8),
        "post_entry_low": round(post_entry_low, 8),
        "post_entry_high_time": high_time,
        "post_entry_low_time": low_time,
        "max_favorable_move_pct": round(max_favorable_move_pct, 4),
        "max_adverse_move_pct": round(max_adverse_move_pct, 4),
    }

    trade_log.insert(0, trade_entry)
    del trade_log[100:]

    result = "KAR" if total_pnl >= 0 else "ZARAR"

    print()
    print(
        f"[POZİSYON KAPATILDI] "
        f"{result} {strategy} {side} {symbol}"
    )
    print(f"  Strateji             : {strategy}")
    print(f"  Sebep                : {exit_reason}")
    print(
        f"  Giriş/Çıkış          : "
        f"${entry_price:.8f} -> ${exit_price:.8f}"
    )
    print(
        f"  Net K/Z              : "
        f"{total_pnl:+.4f} USDT ({pnl_percent:+.4f}%)"
    )
    print(f"  Güncel bakiye        : {balance:.4f} USDT")
    print(f"  İşlem sonrası tavan  : ${post_entry_high:.8f}")
    print(f"  Tavan zamanı         : {high_time}")
    print(f"  İşlem sonrası taban  : ${post_entry_low:.8f}")
    print(f"  Taban zamanı         : {low_time}")
    print(f"  Maks. olumlu hareket : {max_favorable_move_pct:+.4f}%")
    print(f"  Maks. ters hareket   : {max_adverse_move_pct:+.4f}%")
    print()


def update_position_extremes(
    position,
    candle_high,
    candle_low,
    candle_time
):
    if candle_high > position["post_entry_high"]:
        position["post_entry_high"] = candle_high
        position["post_entry_high_time"] = (
            candle_time_string(candle_time)
        )

    if candle_low < position["post_entry_low"]:
        position["post_entry_low"] = candle_low
        position["post_entry_low_time"] = (
            candle_time_string(candle_time)
        )


def manage_positions():
    """
    Açık pozisyonları tamamlanmış 3 dakikalık mumlarla yönetir.

    Çıkış öncelik sırası (aynı mumda birden fazla seviye
    görülürse STOP önceliklidir):
      1. Trailing stop (aktifse)
      2. Stop loss
      3. Take profit

    Trailing stop, R-multiple (yüzde değil) ile çalışır:
      - R = giriş anındaki stop mesafesi (fiyat cinsinden)
      - Kâr TRAILING_TRIGGER_R_MULTIPLE (1R) katına ulaşınca
        trailing aktifleşir.
      - Trailing mesafesi TRAILING_DISTANCE_R_MULTIPLE (0.75R)'dir.
    """

    with state_lock:
        symbols = list(open_positions.keys())

    for symbol in symbols:
        dataframe = get_klines_data(symbol, resample_minutes=3)

        if dataframe is None or len(dataframe) < 2:
            continue

        # Sadece tamamlanmış mum (iloc[-2]); iloc[-1] açık mum.
        completed_candle = dataframe.iloc[-2]

        current_price = safe_float(completed_candle["close"])
        candle_high = safe_float(completed_candle["high"])
        candle_low = safe_float(completed_candle["low"])
        candle_time = completed_candle["datetime"]

        if current_price <= 0:
            continue

        with state_lock:
            if symbol not in open_positions:
                continue

            position = open_positions[symbol]

            side = position["side"]
            entry_price = position["entry_price"]
            stop_distance = position["stop_distance"]

            position["last_price"] = current_price

            position["unrealized_pnl"] = round(
                calculate_unrealized_pnl(position, current_price),
                4
            )

            entry_time = position.get("entry_candle_time")

            try:
                update_extremes = (
                    pd.Timestamp(candle_time)
                    > pd.Timestamp(entry_time)
                )
            except Exception:
                update_extremes = True

            if update_extremes:
                update_position_extremes(
                    position,
                    candle_high,
                    candle_low,
                    candle_time
                )

            exit_reason = None
            exit_price = current_price

            trailing_distance = (
                stop_distance * TRAILING_DISTANCE_R_MULTIPLE
            )

            trailing_trigger = (
                stop_distance * TRAILING_TRIGGER_R_MULTIPLE
            )

            if side == "LONG":
                if update_extremes:
                    position["peak_price"] = max(
                        position["peak_price"],
                        candle_high
                    )

                profit_from_entry = (
                    position["peak_price"] - entry_price
                )

                # 1R kâr görüldüğünde trailing aktifleşir
                if profit_from_entry >= trailing_trigger:
                    position["trailing_active"] = True

                # Stop önceliği: önce trailing, sonra stop, en son TP
                if position["trailing_active"]:
                    trailing_stop = (
                        position["peak_price"] - trailing_distance
                    )

                    if candle_low <= trailing_stop:
                        exit_reason = "TRAILING STOP"
                        exit_price = trailing_stop

                if exit_reason is None:
                    if candle_low <= position["stop_loss"]:
                        exit_reason = "STOP LOSS"
                        exit_price = position["stop_loss"]
                    elif candle_high >= position["take_profit"]:
                        exit_reason = "TAKE PROFIT"
                        exit_price = position["take_profit"]

            else:
                if update_extremes:
                    position["peak_price"] = min(
                        position["peak_price"],
                        candle_low
                    )

                profit_from_entry = (
                    entry_price - position["peak_price"]
                )

                # 1R kâr görüldüğünde trailing aktifleşir
                if profit_from_entry >= trailing_trigger:
                    position["trailing_active"] = True

                # Stop önceliği: önce trailing, sonra stop, en son TP
                if position["trailing_active"]:
                    trailing_stop = (
                        position["peak_price"] + trailing_distance
                    )

                    if candle_high >= trailing_stop:
                        exit_reason = "TRAILING STOP"
                        exit_price = trailing_stop

                if exit_reason is None:
                    if candle_high >= position["stop_loss"]:
                        exit_reason = "STOP LOSS"
                        exit_price = position["stop_loss"]
                    elif candle_low <= position["take_profit"]:
                        exit_reason = "TAKE PROFIT"
                        exit_price = position["take_profit"]

            if exit_reason is not None:
                # Çıkış fiyatını kapatmadan önce ekstrem değerlere
                # dahil ediyoruz.
                if exit_price > position["post_entry_high"]:
                    position["post_entry_high"] = exit_price
                    position["post_entry_high_time"] = (
                        utc_time_string()
                    )

                if exit_price < position["post_entry_low"]:
                    position["post_entry_low"] = exit_price
                    position["post_entry_low_time"] = (
                        utc_time_string()
                    )

                close_position(
                    symbol,
                    position,
                    exit_price,
                    exit_reason
                )

                del open_positions[symbol]



# ============================================================
# DASHBOARD
# ============================================================

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">

    <meta name="viewport"
          content="width=device-width, initial-scale=1.0">

    <meta http-equiv="refresh" content="10">

    <title>Paper Trading Dashboard</title>

    <style>
        * {
            box-sizing: border-box;
        }

        body {
            margin: 0;
            padding: 24px;
            background: #101318;
            color: #e5e7eb;
            font-family: Arial, sans-serif;
        }

        h1 {
            color: #f9fafb;
            font-size: 26px;
            margin: 0 0 8px;
        }

        h2 {
            color: #60a5fa;
            font-size: 20px;
            margin-top: 32px;
        }

        .subtitle {
            color: #9ca3af;
            margin-bottom: 24px;
        }

        .summary {
            display: grid;
            grid-template-columns:
                repeat(auto-fit, minmax(180px, 1fr));
            gap: 12px;
            margin-bottom: 24px;
        }

        .summary-item {
            background: #171b22;
            border: 1px solid #29313d;
            padding: 16px;
        }

        .summary-label {
            color: #9ca3af;
            display: block;
            font-size: 12px;
            margin-bottom: 8px;
            text-transform: uppercase;
        }

        .summary-value {
            color: #f9fafb;
            font-size: 22px;
            font-weight: bold;
        }

        .strategy-normal {
            color: #60a5fa;
            font-weight: bold;
        }

        .strategy-momentum {
            color: #fbbf24;
            font-weight: bold;
        }

        .table-wrapper {
            border: 1px solid #29313d;
            overflow-x: auto;
            width: 100%;
        }

        table {
            background: #171b22;
            border-collapse: collapse;
            min-width: 1450px;
            width: 100%;
        }

        th,
        td {
            border-bottom: 1px solid #29313d;
            padding: 11px;
            text-align: left;
            white-space: nowrap;
        }

        th {
            color: #9ca3af;
            font-size: 11px;
            text-transform: uppercase;
        }

        td {
            color: #e5e7eb;
            font-size: 13px;
        }

        .long,
        .profit {
            color: #4ade80;
            font-weight: bold;
        }

        .short,
        .loss {
            color: #f87171;
            font-weight: bold;
        }

        .empty {
            background: #171b22;
            border: 1px solid #29313d;
            color: #9ca3af;
            padding: 18px;
        }

        .footer {
            color: #6b7280;
            font-size: 12px;
            margin-top: 28px;
        }

        .small {
            color: #9ca3af;
            font-size: 11px;
            line-height: 1.5;
        }

        .positive {
            color: #4ade80;
        }

        .negative {
            color: #f87171;
        }
    </style>
</head>

<body>
    <h1>Paper Trading Dashboard</h1>

    <div class="subtitle">
        Veri kaynağı: Coinbase public market data |
        İşlemler sanal |
        Ana strateji: 3 dakika |
        Momentum: 1 dakika |
        Risk sistemi: ATR bazlı dinamik
    </div>

    <section class="summary">
        <div class="summary-item">
            <span class="summary-label">
                Kullanılabilir bakiye
            </span>

            <span class="summary-value">
                {{ "%.2f"|format(balance) }} USDT
            </span>
        </div>

        <div class="summary-item">
            <span class="summary-label">
                Açık pozisyon
            </span>

            <span class="summary-value">
                {{ open_count }} / {{ max_positions }}
            </span>
        </div>

        <div class="summary-item">
            <span class="summary-label">
                Toplam kapanan
            </span>

            <span class="summary-value">
                {{ trade_count }}
            </span>
        </div>

        <div class="summary-item">
            <span class="summary-label">
                Toplam net K/Z
            </span>

            <span class="summary-value
                {{ 'positive' if total_pnl >= 0 else 'negative' }}">
                {{ "%+.4f"|format(total_pnl) }} USDT
            </span>
        </div>
    </section>

    <section class="summary">
        <div class="summary-item">
            <span class="summary-label">
                Normal işlemler
            </span>

            <span class="summary-value">
                {{ normal_summary['trades'] }}
            </span>

            <div class="small">
                Kazanç: {{ normal_summary['wins'] }} |
                Zarar: {{ normal_summary['losses'] }} |
                Başarı: {{ normal_summary['win_rate'] }}% |
                Net:
                <span class="
                    {{ 'positive'
                       if normal_summary['total_pnl'] >= 0
                       else 'negative' }}">
                    {{ "%+.4f"|format(
                        normal_summary['total_pnl']
                    ) }}
                </span>
            </div>
        </div>

        <div class="summary-item">
            <span class="summary-label">
                Momentum işlemleri
            </span>

            <span class="summary-value">
                {{ momentum_summary['trades'] }}
            </span>

            <div class="small">
                Kazanç: {{ momentum_summary['wins'] }} |
                Zarar: {{ momentum_summary['losses'] }} |
                Başarı: {{ momentum_summary['win_rate'] }}% |
                Net:
                <span class="
                    {{ 'positive'
                       if momentum_summary['total_pnl'] >= 0
                       else 'negative' }}">
                    {{ "%+.4f"|format(
                        momentum_summary['total_pnl']
                    ) }}
                </span>
            </div>
        </div>

        <div class="summary-item">
            <span class="summary-label">
                ATR risk sistemi
            </span>

            <span class="summary-value">
                {{ "%.2f"|format(risk_per_trade_pct) }}% risk
            </span>

            <div class="small">
                ATR periyodu: {{ atr_period }} |
                Stop çarpanı: {{ atr_stop_multiplier }} |
                R/R: {{ risk_reward_ratio }} |
                Trailing: {{ trailing_trigger_r }}R tetik /
                {{ trailing_distance_r }}R mesafe
            </div>
        </div>

        <div class="summary-item">
            <span class="summary-label">
                Komisyon varsayımı
            </span>

            <span class="summary-value">
                {{ commission_percent }}%
            </span>

            <div class="small">
                Alış ve satış tarafında uygulanır. |
                Hybrid: {{ "AÇIK" if hybrid_enabled else "KAPALI" }} |
                Momentum limiti: {{ momentum_limit }}
            </div>
        </div>
    </section>

    <h2>Açık Pozisyonlar</h2>

    {% if positions %}
    <div class="table-wrapper">
        <table>
            <thead>
                <tr>
                    <th>Strateji</th>
                    <th>Coin</th>
                    <th>Yön</th>
                    <th>Giriş</th>
                    <th>Son fiyat</th>
                    <th>TP</th>
                    <th>SL</th>
                    <th>Stop mesafesi</th>
                    <th>Trailing</th>
                    <th>Anlık K/Z</th>
                    <th>İşlem sonrası tavan</th>
                    <th>İşlem sonrası taban</th>
                </tr>
            </thead>

            <tbody>
                {% for symbol, position in positions.items() %}
                <tr>
                    <td class="
                        {{ 'strategy-momentum'
                           if position.get('strategy')
                              == 'MOMENTUM'
                           else 'strategy-normal' }}">
                        {{ position.get('strategy', 'NORMAL') }}
                    </td>

                    <td>{{ symbol }}</td>

                    <td class="
                        {{ 'long'
                           if position['side'] == 'LONG'
                           else 'short' }}">
                        {{ position['side'] }}
                    </td>

                    <td>
                        ${{ "%.8f"|format(
                            position['entry_price']
                        ) }}
                    </td>

                    <td>
                        ${{ "%.8f"|format(
                            position['last_price']
                        ) }}
                    </td>

                    <td>
                        ${{ "%.8f"|format(
                            position['take_profit']
                        ) }}
                    </td>

                    <td>
                        ${{ "%.8f"|format(
                            position['stop_loss']
                        ) }}
                    </td>

                    <td>
                        %{{ "%.3f"|format(
                            position['stop_distance_pct'] * 100
                        ) }}
                    </td>

                    <td>
                        {{ "AKTİF"
                           if position['trailing_active']
                           else "Bekliyor" }}
                    </td>

                    <td class="
                        {{ 'profit'
                           if position['unrealized_pnl'] >= 0
                           else 'loss' }}">
                        {{ "%.4f"|format(
                            position['unrealized_pnl']
                        ) }} USDT
                    </td>

                    <td>
                        ${{ "%.8f"|format(
                            position['post_entry_high']
                        ) }}

                        <div class="small">
                            {{ position['post_entry_high_time'] }}
                        </div>
                    </td>

                    <td>
                        ${{ "%.8f"|format(
                            position['post_entry_low']
                        ) }}

                        <div class="small">
                            {{ position['post_entry_low_time'] }}
                        </div>
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
    {% else %}
        <div class="empty">
            Şu anda açık pozisyon yok.
        </div>
    {% endif %}

    <h2>Son Kapanan İşlemler</h2>

    {% if trades %}
    <div class="table-wrapper">
        <table>
            <thead>
                <tr>
                    <th>Zaman</th>
                    <th>Strateji</th>
                    <th>Coin</th>
                    <th>Yön</th>
                    <th>Sebep</th>
                    <th>Giriş</th>
                    <th>Çıkış</th>
                    <th>Net K/Z</th>
                    <th>Yüzde</th>
                    <th>Tavan</th>
                    <th>Tavan zamanı</th>
                    <th>Taban</th>
                    <th>Taban zamanı</th>
                    <th>Maks. olumlu</th>
                    <th>Maks. ters</th>
                </tr>
            </thead>

            <tbody>
                {% for trade in trades %}
                <tr>
                    <td>{{ trade['time'] }}</td>

                    <td class="
                        {{ 'strategy-momentum'
                           if trade.get('strategy')
                              == 'MOMENTUM'
                           else 'strategy-normal' }}">
                        {{ trade.get('strategy', 'NORMAL') }}
                    </td>

                    <td>{{ trade['symbol'] }}</td>

                    <td class="
                        {{ 'long'
                           if trade['side'] == 'LONG'
                           else 'short' }}">
                        {{ trade['side'] }}
                    </td>

                    <td>{{ trade['reason'] }}</td>

                    <td>${{ trade['entry'] }}</td>
                    <td>${{ trade['exit'] }}</td>

                    <td class="
                        {{ 'profit'
                           if trade['pnl'] >= 0
                           else 'loss' }}">
                        {{ "%+.4f"|format(trade['pnl']) }} USDT
                    </td>

                    <td class="
                        {{ 'profit'
                           if trade['pnl_pct'] >= 0
                           else 'loss' }}">
                        {{ "%+.4f"|format(trade['pnl_pct']) }}%
                    </td>

                    <td>
                        ${{ trade['post_entry_high'] }}
                    </td>

                    <td>
                        {{ trade['post_entry_high_time'] }}
                    </td>

                    <td>
                        ${{ trade['post_entry_low'] }}
                    </td>

                    <td>
                        {{ trade['post_entry_low_time'] }}
                    </td>

                    <td class="
                        {{ 'profit'
                           if trade['max_favorable_move_pct'] >= 0
                           else 'loss' }}">
                        {{ "%+.4f"|format(
                            trade['max_favorable_move_pct']
                        ) }}%
                    </td>

                    <td class="
                        {{ 'profit'
                           if trade['max_adverse_move_pct'] >= 0
                           else 'loss' }}">
                        {{ "%+.4f"|format(
                            trade['max_adverse_move_pct']
                        ) }}%
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
    {% else %}
        <div class="empty">
            Henüz kapanmış işlem yok.
        </div>
    {% endif %}

    <div class="footer">
        Sayfa 10 saniyede bir yenilenir.
    </div>
</body>
</html>
"""


@app.route("/")
def dashboard():
    with state_lock:
        normal_stats = strategy_summary("NORMAL")
        momentum_stats = strategy_summary("MOMENTUM")

        total_pnl = sum(
            safe_float(trade.get("pnl"))
            for trade in trade_log
        )

        return render_template_string(
            DASHBOARD_HTML,
            balance=balance,
            open_count=len(open_positions),
            max_positions=MAX_OPEN_POSITIONS,
            trade_count=len(trade_log),
            total_pnl=round(total_pnl, 4),
            normal_summary=normal_stats,
            momentum_summary=momentum_stats,
            hybrid_enabled=HYBRID_ENABLED,
            momentum_limit=MAX_MOMENTUM_POSITIONS,
            commission_percent=(COMMISSION_RATE * 100),
            risk_per_trade_pct=(RISK_PER_TRADE_PCT * 100),
            atr_period=ATR_PERIOD,
            atr_stop_multiplier=ATR_STOP_MULTIPLIER,
            risk_reward_ratio=RISK_REWARD_RATIO,
            trailing_trigger_r=TRAILING_TRIGGER_R_MULTIPLE,
            trailing_distance_r=TRAILING_DISTANCE_R_MULTIPLE,
            current_time=utc_time_string(),
            positions=dict(open_positions),
            trades=list(trade_log),
        )


@app.route("/api/status")
def api_status():
    with state_lock:
        normal_stats = strategy_summary("NORMAL")
        momentum_stats = strategy_summary("MOMENTUM")

        total_pnl = sum(
            safe_float(trade.get("pnl"))
            for trade in trade_log
        )

        return jsonify(
            {
                "balance": round(balance, 4),
                "open_positions": open_positions,
                "trade_history": trade_log,
                "total_pnl": round(total_pnl, 4),
                "normal_summary": normal_stats,
                "momentum_summary": momentum_stats,
                "hybrid_enabled": HYBRID_ENABLED,
                "risk_system": {
                    "risk_per_trade_pct": RISK_PER_TRADE_PCT,
                    "atr_period": ATR_PERIOD,
                    "atr_stop_multiplier": ATR_STOP_MULTIPLIER,
                    "risk_reward_ratio": RISK_REWARD_RATIO,
                    "trailing_trigger_r_multiple": (
                        TRAILING_TRIGGER_R_MULTIPLE
                    ),
                    "trailing_distance_r_multiple": (
                        TRAILING_DISTANCE_R_MULTIPLE
                    ),
                    "min_stop_distance_pct": MIN_STOP_DISTANCE_PCT,
                    "max_stop_distance_pct": MAX_STOP_DISTANCE_PCT,
                },
                "server_time": utc_time_string(),
            }
        )


def run_dashboard():
    port = int(os.environ.get("PORT", "8080"))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False,
    )



# ============================================================
# ANA BOT DÖNGÜSÜ
# ============================================================

def run_bot():
    print("=" * 60)
    print("PAPER TRADING BOT BAŞLATILDI")
    print("=" * 60)
    print(f"Veri kaynağı        : {MARKET_DATA_BASE_URL}")
    print(f"Ana zaman dilimi    : {TIMEFRAME}")
    print(f"Momentum zaman dil. : 1m")
    print(f"Tarama aralığı      : {SCAN_INTERVAL_SECONDS} saniye")
    print(f"Maksimum pozisyon   : {MAX_OPEN_POSITIONS}")
    print(f"Maks. allocation    : %{TRADE_SIZE_PERCENT * 100:.2f}")
    print("-" * 60)
    print("ATR BAZLI DİNAMİK RİSK SİSTEMİ")
    print(f"  İşlem başına risk  : %{RISK_PER_TRADE_PCT * 100:.2f}")
    print(f"  ATR periyodu       : {ATR_PERIOD}")
    print(f"  ATR stop çarpanı   : {ATR_STOP_MULTIPLIER}")
    print(f"  Risk/Ödül oranı    : {RISK_REWARD_RATIO}")
    print(
        f"  Trailing tetik     : "
        f"{TRAILING_TRIGGER_R_MULTIPLE}R"
    )
    print(
        f"  Trailing mesafe    : "
        f"{TRAILING_DISTANCE_R_MULTIPLE}R"
    )
    print(
        f"  Min stop mesafesi  : "
        f"%{MIN_STOP_DISTANCE_PCT * 100:.3f}"
    )
    print(
        f"  Maks stop mesafesi : "
        f"%{MAX_STOP_DISTANCE_PCT * 100:.3f}"
    )
    print("-" * 60)
    print(
        f"Hybrid momentum     : "
        f"{'AÇIK' if HYBRID_ENABLED else 'KAPALI'}"
    )
    print(f"Momentum poz. limiti: {MAX_MOMENTUM_POSITIONS}")
    print(f"Komisyon varsayımı  : %{COMMISSION_RATE * 100:.3f}")
    print(f"Başlangıç bakiye    : {INITIAL_BALANCE:.2f} USDT")
    print("Günlük zarar kesme  : YOK")
    print("=" * 60)

    while True:
        try:
            cleanup_old_trade_timestamps()

            manage_positions()

            with state_lock:
                position_count = len(open_positions)

            if position_count < MAX_OPEN_POSITIONS:
                symbols = get_top_symbols()

                for symbol in symbols:
                    with state_lock:
                        if symbol in open_positions:
                            continue

                    can_trade, reason = can_open_new_trade(symbol)

                    if not can_trade:
                        continue

                    dataframe_3m = get_klines_data(
                        symbol,
                        resample_minutes=3
                    )

                    if dataframe_3m is None:
                        continue

                    signal = analyze_signal(dataframe_3m)

                    strategy = "NORMAL"
                    signal_dataframe = dataframe_3m

                    # Önce mevcut 3 dakikalık strateji denenir.
                    # Sinyal yoksa hybrid momentum denenir.
                    if signal is None and HYBRID_ENABLED:
                        dataframe_1m = get_klines_data(
                            symbol,
                            resample_minutes=1
                        )

                        momentum_signal = analyze_momentum_signal(
                            dataframe_1m,
                            dataframe_3m
                        )

                        if momentum_signal is not None:
                            signal = momentum_signal
                            strategy = "MOMENTUM"
                            signal_dataframe = dataframe_1m

                    if signal is None:
                        continue

                    # Sadece tamamlanmış mumla giriş yapılır.
                    signal_candle = signal_dataframe.iloc[-2]

                    signal_price = safe_float(
                        signal_candle["close"]
                    )

                    if signal_price <= 0:
                        continue

                    # Her işlemden önce ATR hesaplanır.
                    # ATR, girişin yapılacağı zaman dilimindeki
                    # tamamlanmış mumlar üzerinden alınır.
                    atr_value = get_completed_atr(
                        signal_dataframe,
                        ATR_PERIOD
                    )

                    if atr_value is None or atr_value <= 0:
                        continue

                    opened = execute_trade(
                        symbol,
                        signal,
                        signal_price,
                        signal_candle["datetime"],
                        atr_value,
                        strategy=strategy
                    )

                    if opened:
                        with state_lock:
                            if (
                                len(open_positions)
                                >= MAX_OPEN_POSITIONS
                            ):
                                break

                        time.sleep(0.5)

            time.sleep(SCAN_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            print("\nBot durduruldu.")
            break

        except Exception as error:
            print(f"[!] Ana döngü hatası: {error}")
            time.sleep(5)


# ============================================================
# BAŞLAT
# ============================================================

if __name__ == "__main__":
    dashboard_thread = threading.Thread(
        target=run_dashboard,
        daemon=True
    )