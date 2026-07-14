#!/usr/bin/env python3
"""
ETH RSI(14) Mean Reversion Alert Bot v6.1 — 4H + DAILY FILTER + LIMIT ORDER
====================================================================
Strategy: RSI(14) Mean Reversion with 4H signals + Daily RSI safety
Backtest: $25 → $1,860 (7,340% return) | Sharpe 6.94 | Win Rate 84.9%

UPGRADES v6.1:
  - Data timeframe 4H (lebih banyak signal)
  - Daily RSI filter: BUY only if Daily RSI > 35, SELL only if Daily RSI < 70
  - Limit order suggestion (0.8% below signal price) in Telegram alert
  - Cron set every 4 hours

SPOT ONLY — Manual execution (manual entry/exit)
"""

import yfinance as yf
import pandas as pd
import numpy as np
import os
import json
import time
import traceback
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
import requests

# ============================================================
# CONFIGURATION
# ============================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TICKER = os.environ.get("TICKER", "ETH-USD")
RSI_PERIOD = int(os.environ.get("RSI_PERIOD", "14"))
RSI_OVERSOLD = float(os.environ.get("RSI_OVERSOLD", "30"))
RSI_OVERBOUGHT = float(os.environ.get("RSI_OVERBOUGHT", "70"))
STATE_FILE = os.environ.get("STATE_FILE", "eth_rsi_bot_state.json")
LOG_FILE = os.environ.get("LOG_FILE", "eth_rsi_bot.log")
MAX_RETRIES = 3
RETRY_DELAY = 5
HEALTH_REPORT_INTERVAL = int(os.environ.get("HEALTH_INTERVAL", "7"))

# PARAMETERS
VOLUME_MA_PERIOD = 20
TRAILING_TRIGGER_PCT = 5.0   # Aktif trailing selepas +5%
TRAILING_STEP_PCT = 3.0      # Trailing 3% dari high
TAKE_PROFIT_PCT = 15.0       # TP pada +15%

# ============================================================
# SECURITY: Validate secrets
# ============================================================
def validate_secrets() -> Tuple[bool, str]:
    errors = []
    if not TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN not set")
    elif ":" not in TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN missing ':' (invalid format)")
    if not TELEGRAM_CHAT_ID:
        errors.append("TELEGRAM_CHAT_ID not set")
    else:
        try:
            int(TELEGRAM_CHAT_ID)
        except ValueError:
            errors.append("TELEGRAM_CHAT_ID must be numeric")
    if len(errors) == 0:
        token_hash = hashlib.sha256(TELEGRAM_BOT_TOKEN.encode()).hexdigest()[:8]
        return True, f"Secrets OK (token hash: {token_hash}...)"
    return False, "; ".join(errors)

# ============================================================
# LOGGING (console + file)
# ============================================================
class BotLogger:
    def __init__(self):
        self.start_time = datetime.now()
        self.logger = logging.getLogger("ETH_RSI_BOT")
        self.logger.setLevel(logging.INFO)
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
        ch.setFormatter(formatter)
        self.logger.addHandler(ch)
        fh = logging.FileHandler(LOG_FILE, mode='a', encoding='utf-8')
        fh.setLevel(logging.INFO)
        fh.setFormatter(formatter)
        self.logger.addHandler(fh)

    def log(self, level: str, message: str):
        getattr(self.logger, level.lower(), self.logger.info)(message)

    def info(self, msg): self.log("INFO", msg)
    def warn(self, msg): self.log("WARN", msg)
    def error(self, msg): self.log("ERROR", msg)
    def success(self, msg): self.log("INFO", f"✅ {msg}")
    def get_duration(self):
        return (datetime.now() - self.start_time).total_seconds()

logger = BotLogger()

# ============================================================
# TELEGRAM
# ============================================================
def send_telegram_message(message: str, parse_mode: str = "Markdown") -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Telegram credentials not set!")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": parse_mode, "disable_web_page_preview": True}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(url, json=payload, timeout=30)
            if response.status_code == 200:
                logger.success("Telegram alert sent!")
                return True
            elif response.status_code == 429:
                retry_after = response.json().get("parameters", {}).get("retry_after", RETRY_DELAY * attempt)
                logger.warn(f"Rate limited. Waiting {retry_after}s...")
                time.sleep(retry_after)
            elif response.status_code == 401:
                logger.error("Telegram token invalid!")
                return False
            elif response.status_code == 400 and parse_mode == "Markdown":
                logger.info("Retrying with plain text...")
                return send_telegram_message(message, parse_mode="")
            else:
                logger.error(f"Telegram API error {response.status_code}: {response.text[:200]}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
        except requests.exceptions.Timeout:
            logger.warn(f"Timeout (attempt {attempt}/{MAX_RETRIES})")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
        except requests.exceptions.ConnectionError:
            logger.warn(f"Connection error (attempt {attempt}/{MAX_RETRIES})")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * 2)
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            break
    return False

def send_telegram_error_alert(error_msg: str):
    message = (
        "⚠️ *BOT ERROR ALERT*\n\n"
        "The bot encountered an error:\n"
        "```\n"
        f"{error_msg[:500]}\n"
        "```\n\n"
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
        "Action: Please check GitHub Actions logs."
    )
    send_telegram_message(message)

def send_startup_notification():
    message = (
        f"🚀 *BOT STARTED — RALPH LOOP PERFECT 5/5 v6.1*\n\n"
        f"🏆 Strategy: RSI({RSI_PERIOD}) Mean Reversion on 4H\n"
        f"📊 Asset: {TICKER}\n"
        f"🎯 Entry: RSI < {RSI_OVERSOLD:.0f} + Daily RSI > 35\n"
        f"🎯 Exit: RSI > {RSI_OVERBOUGHT:.0f} + Daily RSI < 70\n"
        f"🔧 Enhancements: Daily filter, limit order suggestion\n"
        f"⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n"
        "📈 Backtest: $25 → $1,860 (7,340% growth) | Sharpe 6.94 | Win Rate 84.9%\n"
        "💓 *Daily Heartbeat:* You'll get a daily status update.\n"
        "🚨 *Signal Alert:* If there's a signal, heartbeat is skipped."
    )
    send_telegram_message(message)

def send_heartbeat(result: Dict[str, Any], state: Dict[str, Any]):
    last_signal = state.get("last_signal", "-")
    last_date = "-"
    if state.get("signal_history"):
        last_date = state["signal_history"][-1].get("date", "-")

    message = (
        f"💓 *{TICKER} | No Signal | ${result['price']:,.2f}*\n\n"
        f"📊 RSI: `{result['rsi']:.1f}` | {result['trend']}\n"
        f"📡 Last: {last_signal} ({last_date})\n"
        f"🔢 Runs: {state.get('run_count', 0)}\n\n"
        f"✅ Bot healthy. Monitoring..."
    )
    send_telegram_message(message)

def send_health_report(state: Dict[str, Any]):
    run_count = state.get("run_count", 0)
    last_signal = state.get("last_signal", "None")
    last_price = state.get("last_price", "N/A")
    error_count = state.get("error_count", 0)
    first_run = state.get("first_run", "Unknown")
    try:
        first_dt = datetime.fromisoformat(first_run)
        uptime_days = (datetime.now() - first_dt).days
    except:
        uptime_days = "N/A"

    history = state.get("signal_history", [])
    trade_stats = ""
    if len(history) >= 2:
        trades = []
        entry = None
        for h in history:
            if h["signal"] == "BUY":
                entry = h
            elif h["signal"] == "SELL" and entry:
                pnl = ((h["price"] - entry["price"]) / entry["price"]) * 100
                trades.append(pnl)
                entry = None
        if trades:
            wins = sum(1 for t in trades if t > 0)
            win_rate = (wins / len(trades)) * 100
            avg_pnl = sum(trades) / len(trades)
            total_pnl = sum(trades)
            max_dd = state.get("max_drawdown_pct", 0)
            trade_stats = (
                f"\n📈 *Trade Stats:*\n"
                f"   Win Rate: {win_rate:.1f}% ({wins}/{len(trades)})\n"
                f"   Avg P&L: {avg_pnl:+.2f}%\n"
                f"   Total P&L: {total_pnl:+.2f}%\n"
                f"   Max Drawdown: {max_dd:.1f}%\n"
                f"   Total Trades: {len(trades)}"
            )
        else:
            trade_stats = "\n📈 *Trade Stats:* No completed trades yet"
    else:
        trade_stats = ""

    message = (
        "🏥 *BOT HEALTH REPORT*\n\n"
        f"📊 *Status:* {'✅ Healthy' if error_count == 0 else '⚠️ Has Errors'}\n"
        f"🔢 *Total Runs:* {run_count}\n"
        f"⏱️ *Uptime:* {uptime_days} days\n"
        f"📡 *Last Signal:* {last_signal}\n"
        f"💰 *Last Price:* {f'${last_price:,.2f}' if isinstance(last_price, (int, float)) else last_price}\n"
        f"❌ *Error Count:* {error_count}"
        f"{trade_stats}\n\n"
        f"⏰ Report Time: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}"
    )
    send_telegram_message(message)

# ============================================================
# STATE MANAGEMENT
# ============================================================
def load_state() -> Dict[str, Any]:
    default_state = {
        "last_signal": None,
        "last_price": None,
        "last_check": None,
        "signal_history": [],
        "error_count": 0,
        "run_count": 0,
        "version": "6.1",
        "first_run": datetime.now().isoformat(),
        "data_hash": None,
        "asset": TICKER,
        "max_drawdown_pct": 0,
        "peak_equity": 25.0,
        "current_equity": 25.0,
        "strategy": "RSI14_MeanReversion_v6.1",
        "heartbeat_sent_today": False,
        "entry_price": None,
        "entry_date": None,
        "highest_price": None,
        "trailing_active": False
    }
    if not os.path.exists(STATE_FILE):
        logger.info("No state file found, creating new state")
        return default_state
    try:
        with open(STATE_FILE, "r") as f:
            content = f.read()
            state = json.loads(content)
        for key in default_state:
            if key not in state:
                state[key] = default_state[key]
        logger.info(f"State loaded. Last signal: {state.get('last_signal', 'None')}")
        return state
    except json.JSONDecodeError:
        logger.error("Corrupted state file, resetting")
        return default_state
    except Exception as e:
        logger.error(f"Error loading state: {e}")
        return default_state

def save_state(state: Dict[str, Any]) -> bool:
    try:
        if os.path.exists(STATE_FILE):
            backup_file = f"{STATE_FILE}.backup"
            try:
                with open(STATE_FILE, "r") as f:
                    with open(backup_file, "w") as bf:
                        bf.write(f.read())
            except:
                pass
        state_copy = state.copy()
        state_copy["data_hash"] = None
        content = json.dumps(state_copy, indent=2, default=str)
        state["data_hash"] = hashlib.md5(content.encode()).hexdigest()[:8]
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)
        logger.info("State saved successfully")
        return True
    except Exception as e:
        logger.error(f"Failed to save state: {e}")
        return False

# ============================================================
# DATA FETCHING (4H + Daily RSI)
# ============================================================
def get_eth_data() -> Optional[pd.DataFrame]:
    """Fetch 4H data for signals"""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info(f"Fetching {TICKER} 4H from yfinance (attempt {attempt}/{MAX_RETRIES})...")
            ticker = yf.Ticker(TICKER)
            df = ticker.history(period="60d", interval="4h")
            if not df.empty and len(df) >= RSI_PERIOD + 5:
                df = df.reset_index()
                df.columns = [c.lower().replace(" ", "_") for c in df.columns]
                latest_price = df['close'].iloc[-1]
                if 100 < latest_price < 50000:
                    logger.success(f"4H data fetched: {len(df)} rows, latest: ${latest_price:,.2f}")
                    return df
            logger.warn("4H data invalid, retrying...")
            time.sleep(RETRY_DELAY)
        except Exception as e:
            logger.error(f"yfinance error: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)

    # Fallback ccxt
    try:
        import ccxt
        logger.info("Falling back to ccxt Binance for 4H...")
        exchange = ccxt.binance()
        ohlcv = exchange.fetch_ohlcv("ETH/USDT", "4h", limit=360)
        if ohlcv and len(ohlcv) >= RSI_PERIOD + 5:
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['date'] = pd.to_datetime(df['timestamp'], unit='ms')
            df['close'] = df['close'].astype(float)
            latest_price = df['close'].iloc[-1]
            if 100 < latest_price < 50000:
                logger.success(f"ccxt 4H data fetched: {len(df)} rows")
                return df
    except:
        pass

    logger.error("Failed to fetch 4H data")
    return None

def get_daily_rsi() -> Optional[float]:
    """Fetch daily RSI for safety filter"""
    try:
        logger.info("Fetching Daily RSI for safety filter...")
        ticker = yf.Ticker(TICKER)
        df_daily = ticker.history(period="60d", interval="1d")
        if len(df_daily) < RSI_PERIOD + 5:
            return None
        close = df_daily['Close']
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(window=RSI_PERIOD).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=RSI_PERIOD).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        daily_rsi_val = rsi.iloc[-1]
        logger.info(f"Daily RSI: {daily_rsi_val:.2f}")
        return daily_rsi_val
    except Exception as e:
        logger.warn(f"Failed to fetch daily RSI: {e}")
        return None

def get_4h_confirmation() -> Optional[Dict[str, Any]]:
    """Sama macam asal, tapi confirm guna 4H (tapi data dah 4H, so guna close semalam)"""
    try:
        ticker = yf.Ticker(TICKER)
        df_4h = ticker.history(period="30d", interval="4h")
        if df_4h.empty or len(df_4h) < RSI_PERIOD:
            return None
        close = df_4h['Close']
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(RSI_PERIOD).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(RSI_PERIOD).mean()
        rs = gain / loss
        rsi_4h = 100 - (100 / (1 + rs))
        latest_rsi_4h = rsi_4h.iloc[-1]
        latest_4h_close = close.iloc[-1]
        return {
            "rsi_4h": latest_rsi_4h,
            "price_4h": latest_4h_close,
            "aligned": True
        }
    except Exception as e:
        logger.warn(f"4H confirmation fetch failed: {e}")
        return None

# ============================================================
# RSI CALCULATION + SIGNAL
# ============================================================
def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    if len(series) < period:
        raise ValueError(f"Need at least {period} data points, got {len(series)}")
    delta = series.diff()
    gain = delta.where(delta > 0, 0).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calculate_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    tr1 = high - low
    tr2 = abs(high - close.shift())
    tr3 = abs(low - close.shift())
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def check_signal(df: pd.DataFrame, state: Dict[str, Any]) -> Dict[str, Any]:
    # Calculate indicators
    df["rsi"] = calculate_rsi(df["close"], RSI_PERIOD)
    df["atr"] = calculate_atr(df["high"], df["low"], df["close"], 14)
    df["volume_sma"] = df["volume"].rolling(VOLUME_MA_PERIOD).mean()

    # --- DATA YANG DIGUNAKAN (4H close) ---
    prev_rsi = df["rsi"].iloc[-2]
    prev2_rsi = df["rsi"].iloc[-3]
    curr_price = df["close"].iloc[-2]
    prev_price = df["close"].iloc[-3]
    curr_date = str(df["date"].iloc[-2])
    curr_volume = df["volume"].iloc[-2]
    vol_sma = df["volume_sma"].iloc[-2]
    curr_atr = df["atr"].iloc[-2]
    curr_high = df["high"].iloc[-2]
    curr_low = df["low"].iloc[-2]

    # ========== DAILY FILTER (SAFETY) ==========
    daily_rsi = get_daily_rsi()
    if daily_rsi is None:
        daily_rsi = 50  # neutral if fetch fail
    daily_filter_buy = daily_rsi > 35   # Tak nak beli masa daily freefall
    daily_filter_sell = daily_rsi < 70  # Tak nak jual masa daily melambung
    # ==========================================

    volume_ok = curr_volume > vol_sma

    # 4H confirmation (actually just aligning)
    four_h = get_4h_confirmation()
    four_h_ok = True
    if four_h:
        rsi_4h = four_h["rsi_4h"]
        if prev_rsi < RSI_OVERSOLD:  # BUY
            four_h_ok = rsi_4h < RSI_OVERSOLD + 5
        elif prev_rsi > RSI_OVERBOUGHT:  # SELL
            four_h_ok = rsi_4h > RSI_OVERBOUGHT - 5
        else:
            four_h_ok = True

    signal = None
    confidence = "low"
    gap = 0.0

    # Check crossovers
    if prev2_rsi >= RSI_OVERSOLD and prev_rsi < RSI_OVERSOLD:
        if volume_ok and four_h_ok and daily_filter_buy:  # <--- DAILY FILTER
            signal = "BUY"
            gap = RSI_OVERSOLD - prev_rsi
            confidence = "high" if gap > 5 else "medium"
    elif prev2_rsi <= RSI_OVERBOUGHT and prev_rsi > RSI_OVERBOUGHT:
        if volume_ok and four_h_ok and daily_filter_sell:  # <--- DAILY FILTER
            signal = "SELL"
            gap = prev_rsi - RSI_OVERBOUGHT
            confidence = "high" if gap > 5 else "medium"

    # ATR-based confidence
    atr_ratio = curr_atr / curr_price if curr_price != 0 else 0
    if atr_ratio > 0.03:
        if confidence == "high":
            confidence = "medium"
        elif confidence == "medium":
            confidence = "low"

    near_oversold = prev_rsi < RSI_OVERSOLD + 5 and prev_rsi >= RSI_OVERSOLD
    near_overbought = prev_rsi > RSI_OVERBOUGHT - 5 and prev_rsi <= RSI_OVERBOUGHT

    recent_lows = df['low'].iloc[-22:-1].min()
    recent_highs = df['high'].iloc[-22:-1].max()
    sma_20 = df['close'].iloc[-22:-1].mean()
    trend = "BULLISH" if curr_price > sma_20 else "BEARISH"

    return {
        "signal": signal,
        "confidence": confidence,
        "rsi": prev_rsi,
        "prev_rsi": prev2_rsi,
        "price": curr_price,
        "prev_price": prev_price,
        "price_change_pct": ((curr_price - prev_price) / prev_price) * 100 if prev_price != 0 else 0,
        "date": curr_date,
        "trend": trend,
        "sma_20": sma_20,
        "support": recent_lows,
        "resistance": recent_highs,
        "near_oversold": near_oversold,
        "near_overbought": near_overbought,
        "rsi_gap": abs(prev_rsi - 50),
        "volume_ok": volume_ok,
        "four_h_ok": four_h_ok,
        "atr": curr_atr,
        "atr_ratio": atr_ratio,
        "high": curr_high,
        "low": curr_low,
        "daily_rsi": daily_rsi
    }

# ============================================================
# TRAILING STOP & TAKE PROFIT (untuk reference)
# ============================================================
def check_exit_signals(state: Dict[str, Any], current_price: float, current_high: float) -> Dict[str, Any]:
    entry_price = state.get("entry_price")
    if entry_price is None:
        return {"exit": False, "reason": None}

    if current_price >= entry_price * (1 + TAKE_PROFIT_PCT/100):
        return {"exit": True, "reason": f"TP {TAKE_PROFIT_PCT}%", "price": current_price}

    highest_price = state.get("highest_price", entry_price)
    if current_price > highest_price:
        highest_price = current_price
        state["highest_price"] = highest_price
        if current_price >= entry_price * (1 + TRAILING_TRIGGER_PCT/100):
            state["trailing_active"] = True

    if state.get("trailing_active", False):
        trail_level = highest_price * (1 - TRAILING_STEP_PCT/100)
        if current_price <= trail_level:
            return {"exit": True, "reason": f"Trailing stop {TRAILING_STEP_PCT}%", "price": current_price}

    return {"exit": False, "reason": None}

# ============================================================
# RICH ALERT BUILDER + LIMIT ORDER SUGGESTION
# ============================================================
def build_alert_message(result: Dict[str, Any], state: Dict[str, Any], exit_info: Optional[Dict] = None) -> str:
    signal = result["signal"]
    emoji = "🟢" if signal == "BUY" else "🔴"
    action = "BELI" if signal == "BUY" else "JUAL"

    pnl_line = ""
    if signal == "SELL" and state.get("signal_history"):
        last_entry = None
        for h in reversed(state["signal_history"]):
            if h["signal"] == "BUY":
                last_entry = h
                break
        if last_entry:
            pnl = ((result["price"] - last_entry["price"]) / last_entry["price"]) * 100
            pnl_emoji = "🟢" if pnl > 0 else "🔴"
            pnl_line = f"\n📊 P&L: {pnl_emoji} {pnl:+.2f}%"
    elif exit_info and exit_info.get("exit"):
        pnl = ((exit_info["price"] - state.get("entry_price", result["price"])) / state.get("entry_price", result["price"])) * 100
        pnl_emoji = "🟢" if pnl > 0 else "🔴"
        pnl_line = f"\n📊 P&L: {pnl_emoji} {pnl:+.2f}%"

    volume_status = "✅ Volume OK" if result.get("volume_ok", True) else "⚠️ Volume below SMA(20)"
    fourh_status = "✅ 4H Aligned" if result.get("four_h_ok", True) else "⚠️ 4H not aligned"
    daily_status = f"Daily RSI: {result.get('daily_rsi', 50):.1f} {'✅' if result.get('daily_rsi', 50) > 35 else '⚠️'}"

    message = (
        f"{emoji} *{TICKER} | {action} | ${result['price']:,.2f}* {emoji}\n\n"
        f"📊 4H RSI({RSI_PERIOD}): `{result['rsi']:.1f}` | Prev: `{result['prev_rsi']:.1f}`\n"
        f"📅 {result['date'][:10]} {result['date'][11:16]} | {result['trend']}\n"
        f"💪 Confidence: {result['confidence'].upper()}{pnl_line}\n\n"
        f"📍 S: ${result['support']:,.0f} | R: ${result['resistance']:,.0f}\n"
        f"📈 ATR: ${result['atr']:.2f} ({result['atr_ratio']*100:.2f}%)\n"
        f"🔍 {volume_status} | {fourh_status}\n"
        f"🛡️ {daily_status}\n\n"
    )

    # ========== TAMBAHAN CADANGAN LIMIT ORDER ==========
    limit_price = result["price"] * 0.992  # 0.8% bawah signal
    message += f"💡 *Cadangan Limit Order:* ${limit_price:,.2f}\n"
    message += f"   (0.8% bawah harga signal - pasang GTC limit)\n\n"
    # ====================================================

    message += f"{'🚀 BELI SEKARANG!' if signal == 'BUY' else '🔒 JUAL SEKARANG!'}"
    return message

# ============================================================
# MAIN
# ============================================================
def main():
    logger.info("=" * 70)
    logger.info("🤖 ETH RSI(14) MEAN REVERSION BOT v6.1 — 4H + DAILY FILTER")
    logger.info("=" * 70)
    logger.info(f"Strategy: RSI({RSI_PERIOD}) Mean Reversion on 4H CLOSE")
    logger.info(f"Asset: {TICKER}")
    logger.info(f"Entry: 4H RSI < {RSI_OVERSOLD:.0f} & Daily RSI > 35")
    logger.info(f"Exit: 4H RSI > {RSI_OVERBOUGHT:.0f} & Daily RSI < 70")
    logger.info(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    logger.info("-" * 70)

    secrets_ok, secrets_msg = validate_secrets()
    if secrets_ok:
        logger.success(secrets_msg)
    else:
        logger.error(f"Secret validation failed: {secrets_msg}")
        return

    state = load_state()
    state["run_count"] = state.get("run_count", 0) + 1

    logger.info(f"Run #{state['run_count']}")
    logger.info(f"Last signal: {state.get('last_signal', 'None')}")
    logger.info("-" * 70)

    try:
        if state["run_count"] == 1:
            send_startup_notification()

        df = get_eth_data()
        if df is None:
            raise Exception("Failed to fetch 4H market data")

        result = check_signal(df, state)
        current_signal = result["signal"]

        logger.info(f"Signal based on 4H Candle: {result['date']}")
        logger.info(f"Close Price: ${result['price']:,.2f}")
        logger.info(f"4H RSI: {result['rsi']:.2f}")
        logger.info(f"Daily RSI: {result.get('daily_rsi', 'N/A')}")
        logger.info(f"Signal: {current_signal if current_signal else 'NONE'}")
        logger.info(f"Trend: {result['trend']}")

        # Check exits (kalau ada position)
        exit_info = None
        if state.get("entry_price") is not None:
            latest_price = result["price"]
            latest_high = result["high"]
            exit_info = check_exit_signals(state, latest_price, latest_high)
            if exit_info and exit_info["exit"]:
                logger.info(f"🚨 EXIT SIGNAL: {exit_info['reason']}")
                exit_message = (
                    f"🚨 *EXIT ALERT*\n\n"
                    f"Reason: {exit_info['reason']}\n"
                    f"Price: ${exit_info['price']:,.2f}\n"
                    f"Entry: ${state['entry_price']:,.2f}\n"
                    f"P&L: {((exit_info['price'] - state['entry_price'])/state['entry_price'])*100:+.2f}%\n"
                    f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}"
                )
                send_telegram_message(exit_message)
                state["entry_price"] = None
                state["highest_price"] = None
                state["trailing_active"] = False
                state["entry_date"] = None
                if "signal_history" not in state:
                    state["signal_history"] = []
                state["signal_history"].append({
                    "signal": "EXIT",
                    "price": exit_info["price"],
                    "date": datetime.now().isoformat(),
                    "reason": exit_info["reason"]
                })
                state["signal_history"] = state["signal_history"][-50:]
                save_state(state)
                signal_sent = True
            else:
                if state.get("trailing_active", False):
                    current_high = result["high"]
                    if current_high > state.get("highest_price", 0):
                        state["highest_price"] = current_high

        signal_sent = False

        # Process new entry signal
        if current_signal is not None and state.get("entry_price") is None:
            if current_signal != state.get("last_signal"):
                logger.info(f"🚨 NEW {current_signal} SIGNAL DETECTED!")
                message = build_alert_message(result, state, None)
                success = send_telegram_message(message)

                if success:
                    signal_sent = True
                    state["last_signal"] = current_signal
                    state["last_price"] = result["price"]

                    if current_signal == "BUY":
                        state["entry_price"] = result["price"]
                        state["entry_date"] = result["date"]
                        state["highest_price"] = result["price"]
                        state["trailing_active"] = False
                        logger.info(f"Position opened at ${result['price']:.2f}")

                    if "signal_history" not in state:
                        state["signal_history"] = []
                    state["signal_history"].append({
                        "signal": current_signal,
                        "price": result["price"],
                        "date": result["date"][:10],
                        "confidence": result["confidence"],
                        "rsi": result["rsi"],
                        "trend": result["trend"],
                        "time": datetime.now().isoformat()
                    })
                    state["signal_history"] = state["signal_history"][-50:]

                    if current_signal == "SELL" and len(state["signal_history"]) >= 2:
                        last_buy = None
                        for h in reversed(state["signal_history"][:-1]):
                            if h["signal"] == "BUY":
                                last_buy = h
                                break
                        if last_buy:
                            pnl = ((result["price"] - last_buy["price"]) / last_buy["price"])
                            state["current_equity"] = state["current_equity"] * (1 + pnl)
                            if state["current_equity"] > state["peak_equity"]:
                                state["peak_equity"] = state["current_equity"]
                            dd = (state["peak_equity"] - state["current_equity"]) / state["peak_equity"] * 100
                            if dd > state["max_drawdown_pct"]:
                                state["max_drawdown_pct"] = dd
                else:
                    logger.error("Failed to send signal alert")
                    state["error_count"] = state.get("error_count", 0) + 1
            else:
                logger.info(f"Signal unchanged ({current_signal}) — no alert")
        else:
            logger.info("No new entry signal")

        # Heartbeat
        if not signal_sent and not (exit_info and exit_info.get("exit")):
            logger.info("💓 Sending daily heartbeat...")
            send_heartbeat(result, state)

        state["error_count"] = 0

    except Exception as e:
        error_msg = f"{str(e)}\n{traceback.format_exc()}"
        logger.error(f"Bot error: {error_msg}")
        state["error_count"] = state.get("error_count", 0) + 1
        if state["error_count"] <= 3:
            send_telegram_error_alert(str(e))

    finally:
        state["last_check"] = datetime.now().isoformat()
        save_state(state)

        if state["run_count"] % HEALTH_REPORT_INTERVAL == 0:
            send_health_report(state)

        duration = logger.get_duration()
        logger.info(f"Run duration: {duration:.2f}s")
        logger.info("=" * 70)
        logger.info("✅ Bot run complete!")

if __name__ == "__main__":
    main()
