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

MARKET_DATA_BASE_URL = (
    "https://api.exchange.coinbase.com"
)

TIMEFRAME = "3m"
COINBASE_GRANULARITY_SECONDS = 60
RESAMPLE_MINUTES = 3

SCAN_INTERVAL_SECONDS = 20

INITIAL_BALANCE = 1000.0
TRADE_SIZE_PERCENT = 0.10
MAX_OPEN_POSITIONS = 10

TAKE_PROFIT_PCT = 0.025
STOP_LOSS_PCT = 0.015

TRAILING_TRIGGER_PCT = 0.012
TRAILING_DISTANCE_PCT = 0.006

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

                price = safe_float(
                    ticker.get("price")
                )

                base_volume = safe_float(
                    ticker.get("volume")
                )

                if price <= 0 or base_volume <= 0:
                    continue

                volume_usd = base_volume * price

                if volume_usd < MIN_24H_VOLUME_USDT:
                    continue

                if not MIN_PRICE <= price <= MAX_PRICE:
                    continue

                candidates.append(
                    (symbol, volume_usd)
                )

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
            for symbol, volume in candidates[
                :MAX_SYMBOLS_TO_SCAN
            ]
        ]

        print(
            f"[+] {len(symbols)} Coinbase aday sembol bulundu"
        )

        return symbols

    except requests.exceptions.HTTPError as error:
        print(
            f"[!] Coinbase ürün API HTTP hatası: {error}"
        )

        try:
            print(
                f"[!] API cevabı: {response.text[:300]}"
            )
        except Exception:
            pass

        return []

    except requests.exceptions.RequestException as error:
        print(
            f"[!] Coinbase bağlantı hatası: {error}"
        )
        return []

    except ValueError as error:
        print(
            f"[!] Coinbase JSON hatası: {error}"
        )
        return []

    except Exception as error:
        print(
            f"[!] Sembol listesi hatası: {error}"
        )
        return []


def get_klines_data(symbol):
    """
    Coinbase'den 1 dakikalık mumları alır ve 3 dakikalık
    mumlara dönüştürür.

    Coinbase candle formatı:

    [timestamp, low, high, open, close, volume]
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

        if len(rows) < 180:
            return None

        dataframe = pd.DataFrame(rows)

        dataframe["datetime"] = pd.to_datetime(
            dataframe["timestamp"],
            unit="s",
            utc=True
        )

        dataframe = dataframe.sort_values(
            "datetime"
        )

        dataframe = dataframe.drop_duplicates(
            subset=["datetime"]
        )

        dataframe = dataframe.set_index("datetime")

        dataframe = dataframe.resample(
            "3min",
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

        if len(dataframe) < 65:
            return None

        dataframe["open_time"] = (
            dataframe["datetime"].astype("int64")
            // 10**9
        )

        dataframe["close_time"] = (
            dataframe["open_time"] + 180
        )

        dataframe["quote_volume"] = (
            dataframe["close"]
            * dataframe["volume"]
        )

        if len(dataframe) < 60:
            return None

        return dataframe

    except requests.exceptions.HTTPError as error:
        print(
            f"[!] Coinbase mum HTTP hatası "
            f"{symbol}: {error}"
        )
        return None

    except requests.exceptions.RequestException:
        return None

    except (ValueError, TypeError):
        return None

    except Exception as error:
        print(
            f"[!] Mum verisi hatası {symbol}: {error}"
        )
        return None


# ============================================================
# TEKNİK İNDİKATÖRLER
# ============================================================

def calculate_indicators(dataframe):
    dataframe = dataframe.copy()

    close = dataframe["close"]

    dataframe["ema_50"] = close.ewm(
        span=50,
        adjust=False
    ).mean()

    ema_12 = close.ewm(
        span=12,
        adjust=False
    ).mean()

    ema_26 = close.ewm(
        span=26,
        adjust=False
    ).mean()

    dataframe["macd"] = ema_12 - ema_26

    dataframe["macd_signal"] = dataframe["macd"].ewm(
        span=9,
        adjust=False
    ).mean()

    price_change = close.diff()

    gains = price_change.where(
        price_change > 0,
        0
    )

    losses = -price_change.where(
        price_change < 0,
        0
    )

    average_gain = gains.rolling(14).mean()
    average_loss = losses.rolling(14).mean()

    relative_strength = average_gain / (
        average_loss + 1e-9
    )

    dataframe["rsi"] = 100 - (
        100 / (1 + relative_strength)
    )

    dataframe["volume_ma"] = dataframe["volume"].rolling(
        20
    ).mean()

    return dataframe


def analyze_signal(dataframe):
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
        >= current_candle["volume_ma"]
        * VOLUME_SPIKE_FACTOR
    )

    long_signal = (
        current_candle["close"]
        > current_candle["ema_50"]
        and previous_candle["macd"]
        <= previous_candle["macd_signal"]
        and current_candle["macd"]
        > current_candle["macd_signal"]
        and 40 <= current_candle["rsi"] <= 65
        and volume_is_strong
    )

    if long_signal:
        return "LONG"

    short_signal = (
        current_candle["close"]
        < current_candle["ema_50"]
        and previous_candle["macd"]
        >= previous_candle["macd_signal"]
        and current_candle["macd"]
        < current_candle["macd_signal"]
        and 35 <= current_candle["rsi"] <= 60
        and volume_is_strong
    )

    if short_signal:
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
            return (
                False,
                "Coin soğuma süresinde"
            )

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
# POZİSYON AÇMA
# ============================================================

def execute_trade(
    symbol,
    side,
    price,
    entry_candle_time
):
    global balance

    with state_lock:
        if symbol in open_positions:
            return False

        if len(open_positions) >= MAX_OPEN_POSITIONS:
            return False

        trade_amount = balance * TRADE_SIZE_PERCENT

        if trade_amount < 10:
            print(
                "[!] İşlem tutarı 10 USDT altında"
            )
            return False

        entry_fee = trade_amount * COMMISSION_RATE

        if balance < trade_amount + entry_fee:
            print(
                "[!] Yeterli paper trading bakiyesi yok"
            )
            return False

        if side == "LONG":
            take_profit = price * (
                1 + TAKE_PROFIT_PCT
            )

            stop_loss = price * (
                1 - STOP_LOSS_PCT
            )

        else:
            take_profit = price * (
                1 - TAKE_PROFIT_PCT
            )

            stop_loss = price * (
                1 + STOP_LOSS_PCT
            )

        balance -= trade_amount + entry_fee

        entry_time = candle_time_string(
            entry_candle_time
        )

        open_positions[symbol] = {
            "symbol": symbol,
            "side": side,
            "entry_price": price,
            "amount": trade_amount,
            "entry_fee": entry_fee,
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

        trade_history_timestamps.append(
            time.time()
        )

        print()
        print(
            f"[POZİSYON AÇILDI] "
            f"{side} {symbol}"
        )
        print(
            f"  Giriş fiyatı : ${price:.8f}"
        )
        print(
            f"  İşlem tutarı : {trade_amount:.2f} USDT"
        )
        print(
            f"  TP           : ${take_profit:.8f}"
        )
        print(
            f"  SL           : ${stop_loss:.8f}"
        )
        print(
            f"  Kalan bakiye : {balance:.2f} USDT"
        )
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
        gross_value = amount * (
            current_price / entry_price
        )

    else:
        gross_value = amount * (
            1 + (
                entry_price - current_price
            ) / entry_price
        )

    estimated_exit_fee = (
        gross_value * COMMISSION_RATE
    )

    return (
        gross_value
        - estimated_exit_fee
        - amount
        - position["entry_fee"]
    )


def close_position(
    symbol,
    position,
    exit_price,
    exit_reason
):
    global balance

    entry_price = position["entry_price"]
    amount = position["amount"]
    entry_fee = position["entry_fee"]
    side = position["side"]

    if side == "LONG":
        gross_return = amount * (
            exit_price / entry_price
        )

    else:
        gross_return = amount * (
            1 + (
                entry_price - exit_price
            ) / entry_price
        )

    exit_fee = gross_return * COMMISSION_RATE
    net_return = gross_return - exit_fee

    total_pnl = (
        net_return
        - amount
        - entry_fee
    )

    balance += net_return

    symbol_cooldowns[symbol] = (
        time.time() + SYMBOL_COOLDOWN_SECONDS
    )

    pnl_percent = (
        total_pnl / amount
    ) * 100

    post_entry_high = position.get(
        "post_entry_high",
        entry_price
    )

    post_entry_low = position.get(
        "post_entry_low",
        entry_price
    )

    high_time = position.get(
        "post_entry_high_time",
        position.get("open_time", "")
    )

    low_time = position.get(
        "post_entry_low_time",
        position.get("open_time", "")
    )

    if side == "LONG":
        max_favorable_move_pct = (
            (post_entry_high - entry_price)
            / entry_price
        ) * 100

        max_adverse_move_pct = (
            (post_entry_low - entry_price)
            / entry_price
        ) * 100

    else:
        max_favorable_move_pct = (
            (entry_price - post_entry_low)
            / entry_price
        ) * 100

        max_adverse_move_pct = (
            (entry_price - post_entry_high)
            / entry_price
        ) * 100

    trade_entry = {
        "symbol": symbol,
        "side": side,
        "reason": exit_reason,
        "entry": round(entry_price, 8),
        "exit": round(exit_price, 8),
        "pnl": round(total_pnl, 4),
        "pnl_pct": round(pnl_percent, 4),
        "time": utc_time_string(),

        "post_entry_high": round(
            post_entry_high,
            8
        ),

        "post_entry_low": round(
            post_entry_low,
            8
        ),

        "post_entry_high_time": high_time,
        "post_entry_low_time": low_time,

        "max_favorable_move_pct": round(
            max_favorable_move_pct,
            4
        ),

        "max_adverse_move_pct": round(
            max_adverse_move_pct,
            4
        ),
    }

    trade_log.insert(0, trade_entry)
    del trade_log[100:]

    result = "KAR" if total_pnl >= 0 else "ZARAR"

    print()
    print(
        f"[POZİSYON KAPATILDI] "
        f"{result} {side} {symbol}"
    )
    print(
        f"  Sebep              : {exit_reason}"
    )
    print(
        f"  Giriş/Çıkış        : "
        f"${entry_price:.8f} -> "
        f"${exit_price:.8f}"
    )
    print(
        f"  Net K/Z            : "
        f"{total_pnl:+.4f} USDT "
        f"({pnl_percent:+.4f}%)"
    )
    print(
        f"  Güncel bakiye      : "
        f"{balance:.4f} USDT"
    )
    print(
        f"  İşlem sonrası tavan: "
        f"${post_entry_high:.8f}"
    )
    print(
        f"  Tavan zamanı       : "
        f"{high_time}"
    )
    print(
        f"  İşlem sonrası taban: "
        f"${post_entry_low:.8f}"
    )
    print(
        f"  Taban zamanı       : "
        f"{low_time}"
    )
    print(
        f"  Maks. olumlu hareket: "
        f"{max_favorable_move_pct:+.4f}%"
    )
    print(
        f"  Maks. ters hareket : "
        f"{max_adverse_move_pct:+.4f}%"
    )
    print()


def manage_positions():
    with state_lock:
        symbols = list(open_positions.keys())

    for symbol in symbols:
        dataframe = get_klines_data(symbol)

        if dataframe is None or len(dataframe) < 2:
            continue

        completed_candle = dataframe.iloc[-2]

        current_price = safe_float(
            completed_candle["close"]
        )

        candle_high = safe_float(
            completed_candle["high"]
        )

        candle_low = safe_float(
            completed_candle["low"]
        )

        candle_time = completed_candle["datetime"]

        if current_price <= 0:
            continue

        with state_lock:
            if symbol not in open_positions:
                continue

            position = open_positions[symbol]

            side = position["side"]
            entry_price = position["entry_price"]

            position["last_price"] = current_price

            position["unrealized_pnl"] = round(
                calculate_unrealized_pnl(
                    position,
                    current_price
                ),
                4
            )

            entry_time = position.get(
                "entry_candle_time"
            )

            try:
                update_extremes = (
                    pd.Timestamp(candle_time)
                    > pd.Timestamp(entry_time)
                )
            except Exception:
                update_extremes = True

            if update_extremes:
                if candle_high > position[
                    "post_entry_high"
                ]:
                    position[
                        "post_entry_high"
                    ] = candle_high

                    position[
                        "post_entry_high_time"
                    ] = candle_time_string(
                        candle_time
                    )

                if candle_low < position[
                    "post_entry_low"
                ]:
                    position[
                        "post_entry_low"
                    ] = candle_low

                    position[
                        "post_entry_low_time"
                    ] = candle_time_string(
                        candle_time
                    )

            exit_reason = None
            exit_price = current_price

            if side == "LONG":
                position["peak_price"] = max(
                    position["peak_price"],
                    candle_high
                )

                profit_from_entry = (
                    position["peak_price"]
                    - entry_price
                ) / entry_price

                if (
                    profit_from_entry
                    >= TRAILING_TRIGGER_PCT
                ):
                    position["trailing_active"] = True

                if position["trailing_active"]:
                    trailing_stop = (
                        position["peak_price"]
                        * (1 - TRAILING_DISTANCE_PCT)
                    )

                    if candle_low <= trailing_stop:
                        exit_reason = "TRAILING STOP"
                        exit_price = trailing_stop

                elif candle_low <= position["stop_loss"]:
                    exit_reason = "STOP LOSS"
                    exit_price = position["stop_loss"]

                elif candle_high >= position["take_profit"]:
                    exit_reason = "TAKE PROFIT"
                    exit_price = position["take_profit"]

            else:
                position["peak_price"] = min(
                    position["peak_price"],
                    candle_low
                )

                profit_from_entry = (
                    entry_price
                    - position["peak_price"]
                ) / entry_price

                if (
                    profit_from_entry
                    >= TRAILING_TRIGGER_PCT
                ):
                    position["trailing_active"] = True

                if position["trailing_active"]:
                    trailing_stop = (
                        position["peak_price"]
                        * (1 + TRAILING_DISTANCE_PCT)
                    )

                    if candle_high >= trailing_stop:
                        exit_reason = "TRAILING STOP"
                        exit_price = trailing_stop

                elif candle_high >= position["stop_loss"]:
                    exit_reason = "STOP LOSS"
                    exit_price = position["stop_loss"]

                elif candle_low <= position["take_profit"]:
                    exit_reason = "TAKE PROFIT"
                    exit_price = position["take_profit"]

            if exit_reason is not None:
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

        .table-wrapper {
            border: 1px solid #29313d;
            overflow-x: auto;
            width: 100%;
        }

        table {
            background: #171b22;
            border-collapse: collapse;
            min-width: 1250px;
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
    </style>
</head>

<body>
    <h1>Paper Trading Dashboard</h1>

    <div class="subtitle">
        Veri kaynağı: Coinbase public market data |
        İşlemler sanal
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
                Kapanan işlem
            </span>

            <span class="summary-value">
                {{ trade_count }}
            </span>
        </div>

        <div class="summary-item">
            <span class="summary-label">
                Son güncelleme
            </span>

            <span class="summary-value"
                  style="font-size: 14px;">
                {{ current_time }}
            </span>
        </div>
    </section>

    <h2>Açık Pozisyonlar</h2>

    {% if positions %}
    <div class="table-wrapper">
        <table>
            <thead>
                <tr>
                    <th>Coin</th>
                    <th>Yön</th>
                    <th>Giriş</th>
                    <th>Son fiyat</th>
                    <th>TP</th>
                    <th>SL</th>
                    <th>Trailing</th>
                    <th>Anlık K/Z</th>
                    <th>İşlem sonrası tavan</th>
                    <th>İşlem sonrası taban</th>
                </tr>
            </thead>

            <tbody>
                {% for symbol, position in positions.items() %}
                <tr>
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
                        {{ trade['pnl'] }} USDT
                    </td>

                    <td class="
                        {{ 'profit'
                           if trade['pnl_pct'] >= 0
                           else 'loss' }}">
                        {{ trade['pnl_pct'] }}%
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
                        {{ trade['max_favorable_move_pct'] }}%
                    </td>

                    <td class="
                        {{ 'profit'
                           if trade['max_adverse_move_pct'] >= 0
                           else 'loss' }}">
                        {{ trade['max_adverse_move_pct'] }}%
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
        return render_template_string(
            DASHBOARD_HTML,
            balance=balance,
            open_count=len(open_positions),
            max_positions=MAX_OPEN_POSITIONS,
            trade_count=len(trade_log),
            current_time=utc_time_string(),
            positions=dict(open_positions),
            trades=list(trade_log),
        )


@app.route("/api/status")
def api_status():
    with state_lock:
        return jsonify(
            {
                "balance": round(balance, 4),
                "open_positions": open_positions,
                "trade_history": trade_log,
                "server_time": utc_time_string(),
            }
        )


def run_dashboard():
    port = int(
        os.environ.get("PORT", "8080")
    )

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
    print(
        f"Veri kaynağı       : "
        f"{MARKET_DATA_BASE_URL}"
    )
    print(
        f"Zaman dilimi       : {TIMEFRAME}"
    )
    print(
        f"Tarama aralığı     : "
        f"{SCAN_INTERVAL_SECONDS} saniye"
    )
    print(
        f"Maksimum pozisyon  : "
        f"{MAX_OPEN_POSITIONS}"
    )
    print(
        f"TP                 : "
        f"%{TAKE_PROFIT_PCT * 100:.2f}"
    )
    print(
        f"Pozisyon SL        : "
        f"%{STOP_LOSS_PCT * 100:.2f}"
    )
    print(
        f"Trailing tetikleme : "
        f"%{TRAILING_TRIGGER_PCT * 100:.2f}"
    )
    print(
        f"Trailing mesafe    : "
        f"%{TRAILING_DISTANCE_PCT * 100:.2f}"
    )
    print(
        f"Başlangıç bakiye   : "
        f"{INITIAL_BALANCE:.2f} USDT"
    )
    print("Günlük zarar kesme : YOK")
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

                    can_trade, reason = (
                        can_open_new_trade(symbol)
                    )

                    if not can_trade:
                        continue

                    dataframe = get_klines_data(symbol)

                    if dataframe is None:
                        continue

                    signal = analyze_signal(dataframe)

                    if signal is None:
                        continue

                    signal_candle = dataframe.iloc[-2]

                    signal_price = safe_float(
                        signal_candle["close"]
                    )

                    if signal_price <= 0:
                        continue

                    opened = execute_trade(
                        symbol,
                        signal,
                        signal_price,
                        signal_candle["datetime"]
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
            print(
                f"[!] Ana döngü hatası: {error}"
            )
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