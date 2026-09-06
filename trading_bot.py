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
# Not: TRADE_SIZE_PERCENT geriye dönük uyumluluk için
# MAX_ALLOCATION_PCT ile aynı değeri taşır.
# %10 x 10 pozisyon = en fazla %100 toplam maruziyet (aşırı
# yüklenmeyi önler; önceki %15 ayarı %150'ye kadar çıkabiliyordu).
MAX_ALLOCATION_PCT = 0.10
TRADE_SIZE_PERCENT = MAX_ALLOCATION_PCT

MAX_OPEN_POSITIONS = 10

# Alış ve satışta simüle edilen komisyon oranı
# 0.001 = %0.10 (tek taraf). Alış + satış = %0.20 toplam.
COMMISSION_RATE = 0.001

# Toplam round-trip komisyon oranı (alış + satış)
TOTAL_COMMISSION_RATE = COMMISSION_RATE * 2  # %0.20

MAX_TRADES_PER_HOUR = 15
MAX_TRADES_PER_DAY = 50

SYMBOL_COOLDOWN_SECONDS = 900

# Tarama sırasında minimum 24s USD hacmi (daha agresif eşik)
MIN_24H_VOLUME_USDT = 500_000

# İşlem öncesi likidite kontrolü için minimum 24s hacim
MIN_24H_VOLUME = 500_000

# Hacim, ortalamanın en az bu katı olmalı. Önceki 1.8 değeri
# "hacim patlaması" (exhaustion) anında giriş yapıp tepeden
# alınmasına yol açıyordu. 1.15 = sağlıklı/ortalama üstü hacim
# ister ama patlama tepesini kovalamaz.
VOLUME_SPIKE_FACTOR = 1.15

MIN_PRICE = 0.05
MAX_PRICE = 100.0

MAX_SYMBOLS_TO_SCAN = 40


# ============================================================
# ATR BAZLI DİNAMİK RİSK SİSTEMİ (KOMİSYON DOSTU)
# ============================================================

# Her işlemde hesabın %1.0'i riske girer. Kanıtlanmamış bir
# stratejiyi büyük risklerle çalıştırmamak için %1.5'ten düşürüldü.
RISK_PER_TRADE_PCT = 0.010

# ATR hesaplama periyodu (tamamlanmış mumlar üzerinde)
ATR_PERIOD = 14

# Stop mesafesi = ATR × ATR_STOP_MULTIPLIER. 2.0 -> 2.5:
# stop'a normal piyasa gürültüsünün değmemesi için genişletildi
# (önceki dar stoplar giriş sonrası salınımda hemen vuruluyordu).
ATR_STOP_MULTIPLIER = 2.5

# Take-profit mesafesi = stop mesafesi × RISK_REWARD_RATIO.
# 2.5 -> 2.0: daha ulaşılabilir hedef (2.5R bu zaman diliminde
# nadiren görülüyordu). Geniş stop ile 2.0R hâlâ komisyonu
# fazlasıyla karşılıyor.
RISK_REWARD_RATIO = 2.0

# Kâr bu R katına ulaşınca stop girişe (+ komisyon payı) çekilir.
# Veri kanıtı: işlemler sık sık +0.5R–1R kâr görüp geri dönüp TAM
# stop yiyordu. Breakeven stop bu işlemleri tam kayıp yerine
# sıfıra yakın kapatır (ödül/risk asimetrisini düzeltir).
BREAKEVEN_TRIGGER_R_MULTIPLE = 0.5

# Trailing stop, kâr bu R katına ulaşınca aktifleşir.
# 1.0 -> 0.7: sub-1R tepeleri de yakala (çoğu işlem 1R'ye
# ulaşamadan dönüyordu, trailing hiç devreye girmiyordu).
TRAILING_TRIGGER_R_MULTIPLE = 0.7

# Trailing stop mesafesi (R cinsinden). 0.7 -> 0.5: aktif olunca
# kilitlenen kârı artırır.
TRAILING_DISTANCE_R_MULTIPLE = 0.5

# Stop mesafesi yüzde olarak bu sınırlar arasında tutulur.
# Alt sınır %0.8 -> %1.0: giriş gürültüsüne dayanacak minimum alan.
MIN_STOP_DISTANCE_PCT = 0.010
MAX_STOP_DISTANCE_PCT = 0.040

# Minimum pozisyon büyüklüğü (USDT). Bunun altındaki
# pozisyonlar komisyon açısından verimsiz olduğu için açılmaz.
MIN_POSITION_SIZE = 50

# Komisyonu karşılamak için gereken minimum brüt kâr yüzdesi
# (round-trip komisyon %0.2 + güvenlik marjı)
MIN_GROSS_PROFIT_PCT = 0.005


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
# bot hareketi kovalamaz. 0.015 -> 0.008: uzamış kırılımların
# tepesinden girmeyi engeller (spike kovalamayı azaltır).
MOMENTUM_MAX_EXTENSION_PCT = 0.008


# ============================================================
# UYGULAMA DURUMU
# ============================================================

balance = INITIAL_BALANCE

open_positions = {}
trade_log = []
trade_history_timestamps = []
symbol_cooldowns = {}

# Genel çalışma istatistikleri (dashboard'da gösterilir)
stats = {
    "total_commission": 0.0,       # Ödenen toplam komisyon (USDT)
    "gross_pnl": 0.0,              # Komisyon öncesi toplam brüt K/Z
    "net_pnl": 0.0,               # Komisyon sonrası toplam net K/Z
    "volume_rejected": 0,          # Düşük hacim nedeniyle reddedilen
    "commission_rejected": 0,      # Komisyon kârlılığı nedeniyle reddedilen
    "min_position_rejected": 0,    # Minimum pozisyon altı reddedilen
    "position_size_sum": 0.0,      # Açılan pozisyon büyüklükleri toplamı
    "position_size_count": 0,      # Açılan pozisyon sayısı
    "position_size_min": None,     # En küçük açılan pozisyon (USDT)
    "position_size_max": None,     # En büyük açılan pozisyon (USDT)
}

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

def get_ticker_data(symbol):
    """
    Belirli bir sembol için Coinbase ticker verisini döndürür.

    Dönen sözlükte "price" ve "volume" (24s base hacim) alanları
    bulunur. Hata durumunda None döner.
    """

    ticker_url = (
        f"{MARKET_DATA_BASE_URL}"
        f"/products/{symbol}/ticker"
    )

    try:
        ticker_response = requests.get(ticker_url, timeout=10)
        ticker_response.raise_for_status()

        ticker = ticker_response.json()

        if not isinstance(ticker, dict):
            return None

        return ticker

    except requests.exceptions.RequestException:
        return None

    except (ValueError, TypeError):
        return None


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

            ticker = get_ticker_data(symbol)

            if ticker is None:
                continue

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
    trend_ref_candle = dataframe.iloc[-7]  # ~5 mum önceki EMA50

    required_values = [
        previous_candle["close"],
        current_candle["close"],
        current_candle["ema_50"],
        trend_ref_candle["ema_50"],
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

    close_price = safe_float(current_candle["close"])
    ema_50 = safe_float(current_candle["ema_50"])
    ema_50_ref = safe_float(trend_ref_candle["ema_50"])
    rsi_value = safe_float(current_candle["rsi"])

    if ema_50 <= 0 or close_price <= 0:
        return None

    # YENİDEN TASARIM: Eski sürüm hacim patlamasında (spike) aşırı
    # alım/satım bölgesinden giriyor, tepeden alıp anında geri
    # dönüşe yakalanıyordu (kapanan işlemlerde "maks. olumlu
    # hareket %0" bunun kanıtıydı). Yeni mantık:
    #   1) EMA50 eğimiyle trend rejimini doğrula (trende karşı girme)
    #   2) RSI'ı aşırı bölgeden uzak tut (tepeden alma / dipten satma yok)
    #   3) Fiyat EMA50'den %2'den fazla uzaksa girme (kovalama yok)
    #   4) Hacim "patlama" değil, ortalama üstü (sağlıklı) olsun

    # Hacim: patlama değil, sağlıklı/ortalama üstü hacim
    volume_is_healthy = (
        current_candle["volume"]
        >= current_candle["volume_ma"] * VOLUME_SPIKE_FACTOR
    )

    # Trend rejimi: EMA50 eğimi
    ema_rising = ema_50 > ema_50_ref
    ema_falling = ema_50 < ema_50_ref

    # Fiyatın EMA50'den uzaklığı (kovalamayı önlemek için)
    distance_from_ema = abs(close_price - ema_50) / ema_50
    not_overextended = distance_from_ema <= 0.02  # %2

    macd_bullish = (
        current_candle["macd"] > current_candle["macd_signal"]
        and (
            previous_candle["macd"] <= previous_candle["macd_signal"]
            or current_candle["macd"] > previous_candle["macd"]
        )
    )

    macd_bearish = (
        current_candle["macd"] < current_candle["macd_signal"]
        and (
            previous_candle["macd"] >= previous_candle["macd_signal"]
            or current_candle["macd"] < previous_candle["macd"]
        )
    )

    # LONG: yükselen trendde, aşırı alımda değilken, kovalamadan
    long_signal = (
        close_price > ema_50
        and ema_rising
        and macd_bullish
        and 45 <= rsi_value <= 68
        and not_overextended
        and volume_is_healthy
    )

    if long_signal:
        return "LONG"

    # SHORT: düşen trendde, aşırı satımda değilken, kovalamadan
    short_signal = (
        close_price < ema_50
        and ema_falling
        and macd_bearish
        and 32 <= rsi_value <= 55
        and not_overextended
        and volume_is_healthy
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

    # calculate_indicators hem rsi hem volume_ma üretir; böylece
    # momentum girişinde RSI ile aşırı bölge filtresi uygulanır.
    momentum_data = calculate_indicators(dataframe_1m)

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
        current_candle["rsi"],
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
    current_rsi = safe_float(current_candle["rsi"])

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

    # RSI aşırı bölge filtresi: kırılım zaten aşırı alım/satıma
    # ulaşmışsa girme (tepeden alma / dipten satma engellenir).
    rsi_ok_for_long = current_rsi <= 72
    rsi_ok_for_short = current_rsi >= 28

    if long_trend and long_breakout and volume_is_strong and rsi_ok_for_long:
        return "LONG"

    if short_trend and short_breakout and volume_is_strong and rsi_ok_for_short:
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

def calculate_commission(position_size_usdt):
    """
    Round-trip komisyon: Coinbase %0.1 alış + %0.1 satış = %0.2.

    position_size_usdt: pozisyonun USDT cinsinden büyüklüğü.
    """
    return position_size_usdt * TOTAL_COMMISSION_RATE


def calculate_net_profit(entry_price, exit_price, position_size, side):
    """
    Komisyon dahil gerçek net kârı hesaplar.

    position_size: coin adedi (miktar). Notional büyüklük
    position_size * entry_price ile bulunur ve round-trip
    komisyon bu notional üzerinden uygulanır.

    Döner: (net_profit, commission)
    """
    position_size_usdt = position_size * entry_price
    commission = calculate_commission(position_size_usdt)

    if side == "LONG":
        gross_profit = (exit_price - entry_price) * position_size
    else:
        gross_profit = (entry_price - exit_price) * position_size

    net_profit = gross_profit - commission

    return net_profit, commission


def is_trade_worth_commission(
    entry_price,
    take_profit,
    position_size_usdt
):
    """
    İşlem komisyonları karşılayıp pozitif net kâr bırakıyor mu?

    Minimum brüt kâr yüzdesi MIN_GROSS_PROFIT_PCT (%0.5) olmalı
    (round-trip komisyon %0.2 + güvenlik marjı). Ayrıca hedefe
    ulaşıldığında net kârın pozitif olması gerekir.

    Döner: (uygun_mu, mesaj)
    """
    if entry_price <= 0 or position_size_usdt <= 0:
        return False, "Geçersiz giriş fiyatı veya pozisyon büyüklüğü"

    gross_profit_pct = abs(take_profit - entry_price) / entry_price

    if gross_profit_pct < MIN_GROSS_PROFIT_PCT:
        return (
            False,
            f"Brüt kâr %{gross_profit_pct * 100:.2f} < "
            f"minimum %{MIN_GROSS_PROFIT_PCT * 100:.2f}"
        )

    # Hedefe ulaşıldığındaki gerçek net kârı kontrol et
    gross_profit_usdt = gross_profit_pct * position_size_usdt
    commission = calculate_commission(position_size_usdt)
    net_profit = gross_profit_usdt - commission

    if net_profit <= 0:
        return (
            False,
            f"Net kâr ${net_profit:.4f} <= 0 (komisyon sonrası)"
        )

    return True, f"Net kâr ${net_profit:.4f} pozitif"


def has_sufficient_liquidity(symbol):
    """
    24 saatlik hacim kontrolü (düşük hacim = slippage riski).

    Döner: (yeterli_mi, mesaj)
    """
    ticker = get_ticker_data(symbol)

    if ticker is None:
        return False, "Volume bilgisi alınamadı"

    price = safe_float(ticker.get("price"))
    base_volume = safe_float(ticker.get("volume"))

    # 24s USD hacmi = base hacim × fiyat
    volume_24h_usd = base_volume * price

    if volume_24h_usd < MIN_24H_VOLUME:
        return (
            False,
            f"Volume ${volume_24h_usd:,.0f} < "
            f"minimum ${MIN_24H_VOLUME:,.0f}"
        )

    return True, f"Volume ${volume_24h_usd:,.0f} yeterli"


def get_total_equity():
    """
    Toplam öz sermaye = serbest nakit (balance) + açık
    pozisyonlara yatırılmış tutarların (amount) toplamı.

    Pozisyon büyüklüğü ve allocation limiti bu taban üzerinden
    hesaplanır. Aksi halde her yeni pozisyon açıldıkça serbest
    nakit erir ve %10 allocation limiti MIN_POSITION_SIZE'ın
    altına düşerek bot yeni işlem açamaz hale gelir (starvation).
    """
    with state_lock:
        deployed = sum(
            position.get("amount", 0.0)
            for position in open_positions.values()
        )
    return balance + deployed


def calculate_position_size_with_limits(
    account_balance,
    entry_price,
    atr_stop_distance
):
    """
    Pozisyon büyüklüğünü hem ATR riskine hem de allocation
    limitine göre hesaplar ve minimum pozisyon kontrolü yapar.

    account_balance : güncel bakiye (USDT)
    entry_price     : giriş fiyatı
    atr_stop_distance : stop mesafesi (fiyat cinsinden)

    Döner: (position_size_usdt, mesaj)
    position_size_usdt None ise pozisyon açılmamalıdır.
    """
    if (
        account_balance <= 0
        or entry_price <= 0
        or atr_stop_distance <= 0
    ):
        return None, "Geçersiz risk girdileri"

    # ATR bazlı pozisyon (risk = bakiye × RISK_PER_TRADE_PCT)
    risk_amount = account_balance * RISK_PER_TRADE_PCT
    position_size_coins = risk_amount / atr_stop_distance
    position_size_usdt_atr = position_size_coins * entry_price

    # Allocation bazlı üst sınır
    max_position_usdt = account_balance * MAX_ALLOCATION_PCT

    # İkisinden küçük olanı seç
    final_position_usdt = min(
        position_size_usdt_atr,
        max_position_usdt
    )

    # Minimum pozisyon kontrolü
    if final_position_usdt < MIN_POSITION_SIZE:
        return (
            None,
            f"Pozisyon ${final_position_usdt:.2f} < "
            f"minimum ${MIN_POSITION_SIZE}"
        )

    return (
        final_position_usdt,
        f"Pozisyon ${final_position_usdt:.2f} hesaplandı"
    )


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
         RISK_PER_TRADE_PCT (%1.0) kadarı riske girecek şekilde
         hesaplanır: trade_amount = risk_tutarı / stop_mesafesi_yüzdesi
      5. Pozisyon büyüklüğü MAX_ALLOCATION_PCT (%10) allocation
         sınırını (TRADE_SIZE_PERCENT) aşamaz.

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

    # 4) Sadece hesabın %1.0'i riske girecek şekilde büyüklük
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

        # ---- KONTROL 1: 24s hacim / likidite ----
        liquidity_ok, liquidity_msg = has_sufficient_liquidity(symbol)

        if not liquidity_ok:
            stats["volume_rejected"] += 1
            print(f"[❌ HACİM] {symbol} reddedildi: {liquidity_msg}")
            return False

        # Pozisyon büyüklüğü, serbest nakit değil TOPLAM öz sermaye
        # (nakit + açık pozisyonlar) üzerinden hesaplanır; böylece
        # allocation limiti MIN_POSITION_SIZE altına düşüp botu
        # aç bırakmaz (starvation fix).
        sizing_base = get_total_equity()

        risk = compute_risk_parameters(
            side,
            price,
            atr_value,
            sizing_base
        )

        if risk is None:
            print(f"[!] {symbol} için risk parametreleri hesaplanamadı")
            return False

        trade_amount = risk["trade_amount"]
        stop_distance = risk["stop_distance"]
        take_profit = risk["take_profit"]

        # ---- KONTROL 2: Pozisyon büyüklüğü + limitler ----
        position_size_usdt, size_msg = calculate_position_size_with_limits(
            sizing_base,
            price,
            stop_distance
        )

        if position_size_usdt is None:
            stats["min_position_rejected"] += 1
            print(f"[❌ POZİSYON] {symbol} reddedildi: {size_msg}")
            return False

        # compute_risk_parameters ile aynı allocation limitini
        # uygular; ikisinden küçük olanı kullanarak tutarlılık sağla.
        trade_amount = min(trade_amount, position_size_usdt)

        if trade_amount < MIN_POSITION_SIZE:
            stats["min_position_rejected"] += 1
            print(
                f"[❌ POZİSYON] {symbol} reddedildi: "
                f"işlem tutarı ${trade_amount:.2f} < "
                f"minimum ${MIN_POSITION_SIZE}"
            )
            return False

        # ---- KONTROL 3: Komisyon sonrası kâr uygunluğu ----
        worth_ok, worth_msg = is_trade_worth_commission(
            price,
            take_profit,
            trade_amount
        )

        if not worth_ok:
            stats["commission_rejected"] += 1
            print(f"[❌ KOMİSYON] {symbol} reddedildi: {worth_msg}")
            return False

        print(
            f"[✅ ONAY] {symbol} {side} tüm filtrelerden geçti "
            f"({liquidity_msg}; {worth_msg})"
        )

        entry_fee = trade_amount * COMMISSION_RATE

        if balance < trade_amount + entry_fee:
            print("[!] Yeterli paper trading bakiyesi yok")
            return False

        stop_loss = risk["stop_loss"]
        stop_distance_pct = risk["stop_distance_pct"]

        # Pozisyon büyüklüğü istatistikleri
        stats["position_size_sum"] += trade_amount
        stats["position_size_count"] += 1
        if stats["position_size_min"] is None:
            stats["position_size_min"] = trade_amount
        else:
            stats["position_size_min"] = min(
                stats["position_size_min"], trade_amount
            )
        if stats["position_size_max"] is None:
            stats["position_size_max"] = trade_amount
        else:
            stats["position_size_max"] = max(
                stats["position_size_max"], trade_amount
            )

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

    # Komisyon ve K/Z istatistikleri
    trade_commission = entry_fee + exit_fee
    gross_pnl = gross_return - amount  # komisyon öncesi brüt K/Z
    stats["total_commission"] += trade_commission
    stats["gross_pnl"] += gross_pnl
    stats["net_pnl"] += total_pnl

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
      2. Stop loss (breakeven'a çekilmiş olabilir)
      3. Take profit

    R-multiple (yüzde değil) ile çalışır:
      - R = giriş anındaki stop mesafesi (fiyat cinsinden)
      - Kâr BREAKEVEN_TRIGGER_R_MULTIPLE (0.5R) katına ulaşınca
        stop, giriş + komisyon yastığına çekilir. Böylece yeşile
        dönmüş bir işlem geri dönüp tam zarar yazmaz.
      - Kâr TRAILING_TRIGGER_R_MULTIPLE (0.7R) katına ulaşınca
        trailing aktifleşir.
      - Trailing mesafesi TRAILING_DISTANCE_R_MULTIPLE (0.5R)'dir.
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

                # 0.5R kâr görüldüğünde stop, giriş + komisyon
                # yastığına çekilir (breakeven). Böylece yeşile
                # dönen işlem tam stop'a geri düşüp zarar yazmaz.
                breakeven_trigger = (
                    stop_distance * BREAKEVEN_TRIGGER_R_MULTIPLE
                )
                if profit_from_entry >= breakeven_trigger:
                    breakeven_stop = entry_price + (
                        entry_price * TOTAL_COMMISSION_RATE
                    )
                    position["stop_loss"] = max(
                        position["stop_loss"],
                        breakeven_stop
                    )

                # 0.7R kâr görüldüğünde trailing aktifleşir
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

                # 0.5R kâr görüldüğünde stop, giriş - komisyon
                # yastığına çekilir (breakeven). Böylece yeşile
                # dönen işlem tam stop'a geri düşüp zarar yazmaz.
                breakeven_trigger = (
                    stop_distance * BREAKEVEN_TRIGGER_R_MULTIPLE
                )
                if profit_from_entry >= breakeven_trigger:
                    breakeven_stop = entry_price - (
                        entry_price * TOTAL_COMMISSION_RATE
                    )
                    position["stop_loss"] = min(
                        position["stop_loss"],
                        breakeven_stop
                    )

                # 0.7R kâr görüldüğünde trailing aktifleşir
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
                Round-trip: {{ "%.2f"|format(total_commission_percent) }}% |
                Hybrid: {{ "AÇIK" if hybrid_enabled else "KAPALI" }} |
                Momentum limiti: {{ momentum_limit }}
            </div>
        </div>
    </section>

    <h2>💰 Gerçek Para / Komisyon İstatistikleri</h2>

    <section class="summary">
        <div class="summary-item">
            <span class="summary-label">
                Ödenen toplam komisyon
            </span>

            <span class="summary-value negative">
                {{ "%.4f"|format(stats['total_commission']) }} USDT
            </span>
        </div>

        <div class="summary-item">
            <span class="summary-label">
                Brüt K/Z (komisyon öncesi)
            </span>

            <span class="summary-value
                {{ 'positive' if stats['gross_pnl'] >= 0
                   else 'negative' }}">
                {{ "%+.4f"|format(stats['gross_pnl']) }} USDT
            </span>
        </div>

        <div class="summary-item">
            <span class="summary-label">
                Net K/Z (komisyon sonrası)
            </span>

            <span class="summary-value
                {{ 'positive' if stats['net_pnl'] >= 0
                   else 'negative' }}">
                {{ "%+.4f"|format(stats['net_pnl']) }} USDT
            </span>
        </div>

        <div class="summary-item">
            <span class="summary-label">
                Pozisyon büyüklüğü (min/ort/maks)
            </span>

            <span class="summary-value">
                {{ "%.0f"|format(stats['position_size_min']) }} /
                {{ "%.0f"|format(stats['position_size_avg']) }} /
                {{ "%.0f"|format(stats['position_size_max']) }}
            </span>

            <div class="small">
                USDT | Açılan pozisyon: {{ stats['position_count'] }} |
                Min: ${{ min_position_size }} |
                Maks allocation: {{ "%.1f"|format(max_allocation_pct) }}%
            </div>
        </div>

        <div class="summary-item">
            <span class="summary-label">
                Düşük hacim reddi
            </span>

            <span class="summary-value">
                {{ stats['volume_rejected'] }}
            </span>

            <div class="small">
                Min 24s hacim: ${{ "{:,.0f}".format(min_24h_volume) }}
            </div>
        </div>

        <div class="summary-item">
            <span class="summary-label">
                Komisyon reddi
            </span>

            <span class="summary-value">
                {{ stats['commission_rejected'] }}
            </span>

            <div class="small">
                Komisyon sonrası kârsız işlemler engellendi
            </div>
        </div>

        <div class="summary-item">
            <span class="summary-label">
                Min pozisyon reddi
            </span>

            <span class="summary-value">
                {{ stats['min_position_rejected'] }}
            </span>

            <div class="small">
                ${{ min_position_size }} altı işlemler engellendi
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


def build_stats_snapshot():
    """
    Komisyon, brüt/net K/Z, pozisyon büyüklüğü ve filtre
    reddi istatistiklerinin türetilmiş özetini üretir.
    state_lock altında çağrılmalıdır.
    """
    count = stats["position_size_count"]

    if count > 0:
        avg_position = stats["position_size_sum"] / count
    else:
        avg_position = 0.0

    return {
        "total_commission": round(stats["total_commission"], 4),
        "gross_pnl": round(stats["gross_pnl"], 4),
        "net_pnl": round(stats["net_pnl"], 4),
        "volume_rejected": stats["volume_rejected"],
        "commission_rejected": stats["commission_rejected"],
        "min_position_rejected": stats["min_position_rejected"],
        "position_count": count,
        "position_size_avg": round(avg_position, 2),
        "position_size_min": (
            round(stats["position_size_min"], 2)
            if stats["position_size_min"] is not None else 0.0
        ),
        "position_size_max": (
            round(stats["position_size_max"], 2)
            if stats["position_size_max"] is not None else 0.0
        ),
    }


@app.route("/")
def dashboard():
    with state_lock:
        normal_stats = strategy_summary("NORMAL")
        momentum_stats = strategy_summary("MOMENTUM")

        total_pnl = sum(
            safe_float(trade.get("pnl"))
            for trade in trade_log
        )

        stats_snapshot = build_stats_snapshot()

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
            total_commission_percent=(TOTAL_COMMISSION_RATE * 100),
            risk_per_trade_pct=(RISK_PER_TRADE_PCT * 100),
            atr_period=ATR_PERIOD,
            atr_stop_multiplier=ATR_STOP_MULTIPLIER,
            risk_reward_ratio=RISK_REWARD_RATIO,
            trailing_trigger_r=TRAILING_TRIGGER_R_MULTIPLE,
            trailing_distance_r=TRAILING_DISTANCE_R_MULTIPLE,
            min_position_size=MIN_POSITION_SIZE,
            max_allocation_pct=(MAX_ALLOCATION_PCT * 100),
            min_24h_volume=MIN_24H_VOLUME,
            stats=stats_snapshot,
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

        stats_snapshot = build_stats_snapshot()

        return jsonify(
            {
                "balance": round(balance, 4),
                "open_positions": open_positions,
                "trade_history": trade_log,
                "total_pnl": round(total_pnl, 4),
                "normal_summary": normal_stats,
                "momentum_summary": momentum_stats,
                "hybrid_enabled": HYBRID_ENABLED,
                "commission_stats": stats_snapshot,
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
                    "min_position_size": MIN_POSITION_SIZE,
                    "max_allocation_pct": MAX_ALLOCATION_PCT,
                    "min_24h_volume": MIN_24H_VOLUME,
                    "total_commission_rate": TOTAL_COMMISSION_RATE,
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
    print(
        f"🎯 RİSK: İşlem başına %{RISK_PER_TRADE_PCT * 100:.2f} risk | "
        f"ATR x{ATR_STOP_MULTIPLIER} stop | "
        f"R/Ö {RISK_REWARD_RATIO}"
    )
    print(
        f"💰 KOMİSYON: Round-trip %{TOTAL_COMMISSION_RATE * 100:.2f} | "
        f"Min brüt kâr hedefi %{MIN_GROSS_PROFIT_PCT * 100:.2f}"
    )
    print(
        f"📊 POZİSYON: Min ${MIN_POSITION_SIZE} | "
        f"Maks allocation %{MAX_ALLOCATION_PCT * 100:.2f} bakiye"
    )
    print(
        f"📈 VOLUME: Minimum 24s hacim ${MIN_24H_VOLUME:,.0f} "
        f"(slippage koruması)"
    )
    print("⚠️  GERÇEK PARA MODU HAZIR: Her işlem komisyon sonrası "
          "pozitif net kâr için filtreleniyor")
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

    dashboard_thread.start()

    run_bot()