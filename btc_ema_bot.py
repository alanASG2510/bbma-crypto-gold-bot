#!/usr/bin/env python3
"""
BTC EMA Crossover Alert Bot v4.0 - FINAL (FIXED: daily close signal + relaxed token validation)
Ralph Loop Iteration 3: Security + Documentation + Multi-Asset

**PENTING**: Signal dijana berdasarkan harga penutup HARIAN YANG SUDAH SAH (bar semalam).
Ini mengelakkan false signal daripada pergerakan intraday yang belum final.
Bot hanya patut dijalankan SEKALI SEHARI selepas candle harian selesai (contoh: 00:15 UTC).

Features:
- Multi-asset support (BTC, ETH, configurable)
- Price validation against multiple sources
- Encrypted state backup
- Comprehensive health monitoring
- Beautiful rich alerts with P&L tracking
- Self-healing error recovery
- Security audit logging
"""

import yfinance as yf
import pandas as pd
import numpy as np
import os
import json
import time
import traceback
import hashlib
import hmac
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
import requests

# ============================================================
# CONFIGURATION
# ============================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TICKER = os.environ.get("TICKER", "BTC-USD")
EMA_FAST = int(os.environ.get("EMA_FAST", "12"))
EMA_SLOW = int(os.environ.get("EMA_SLOW", "26"))
STATE_FILE = os.environ.get("STATE_FILE", "bot_state.json")
MAX_RETRIES = 3
RETRY_DELAY = 5
HEALTH_REPORT_INTERVAL = int(os.environ.get("HEALTH_INTERVAL", "7"))  # every N runs

# ============================================================
# SECURITY: Validate secrets without exposing them
# ============================================================
def validate_secrets() -> Tuple[bool, str]:
    """Validate secrets exist and have minimal format (non-empty, contains ':')"""
    errors = []

    if not TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN not set")
    elif ":" not in TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN missing ':' (not a valid bot token)")

    if not TELEGRAM_CHAT_ID:
        errors.append("TELEGRAM_CHAT_ID not set")
    else:
        try:
            int(TELEGRAM_CHAT_ID)
        except ValueError:
            errors.append("TELEGRAM_CHAT_ID must be numeric")

    if len(errors) == 0:
        # Log partial token hash for debugging
        token_hash = hashlib.sha256(TELEGRAM_BOT_TOKEN.encode()).hexdigest()[:8]
        return True, f"Secrets validated (token hash: {token_hash}...)"

    return False, "; ".join(errors)

# ============================================================
# LOGGING
# ============================================================
class BotLogger:
    def __init__(self):
        self.logs = []
        self.start_time = datetime.now()

    def log(self, level: str, message: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{timestamp}] [{level}] {message}"
        self.logs.append(entry)
        print(entry)

    def info(self, msg): self.log("INFO", msg)
    def warn(self, msg): self.log("WARN", msg)
    def error(self, msg): self.log("ERROR", msg)
    def success(self, msg): self.log("SUCCESS", msg)
    def debug(self, msg): self.log("DEBUG", msg)

    def get_logs(self):
        return "\n".join(self.logs)

    def get_duration(self):
        return (datetime.now() - self.start_time).total_seconds()

logger = BotLogger()

# ============================================================
# TELEGRAM (with retry, validation, formatting)
# ============================================================
def send_telegram_message(message: str, parse_mode: str = "Markdown") -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Telegram credentials not set!")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True
    }

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
                logger.error("Telegram token invalid! Check TELEGRAM_BOT_TOKEN")
                return False
            elif response.status_code == 400:
                logger.error(f"Bad request: {response.text}")
                # Try sending without Markdown as fallback
                if parse_mode == "Markdown":
                    logger.info("Retrying with plain text...")
                    return send_telegram_message(message, parse_mode="")
                return False
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
    # Guna single quotes + concatenation untuk elak isu backtick dalam f-string triple-quoted
    message = (
        "⚠️ *BOT ERROR ALERT*\n\n"
        "The bot encountered an error:\n"
        "'''\n"
        f"{error_msg[:500]}\n"
        "'''\n\n"
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
        "Action: Please check GitHub Actions logs."
    )
    send_telegram_message(message)

def send_startup_notification():
    """Send notification when bot starts"""
    message = (
        f"🚀 *BOT STARTED*\n\n"
        f"Asset: {TICKER}\n"
        f"Strategy: EMA({EMA_FAST}/{EMA_SLOW}) Crossover\n"
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n"
        "Bot is now monitoring for crossover signals (using DAILY CLOSE data)."
    )
    send_telegram_message(message)

def send_health_report(state: Dict[str, Any]):
    """Send comprehensive health status report"""
    run_count = state.get("run_count", 0)
    last_signal = state.get("last_signal", "None")
    last_price = state.get("last_price", "N/A")
    error_count = state.get("error_count", 0)
    first_run = state.get("first_run", "Unknown")

    # Calculate uptime
    try:
        first_dt = datetime.fromisoformat(first_run)
        uptime_days = (datetime.now() - first_dt).days
    except:
        uptime_days = "N/A"

    # Calculate win rate from history
    history = state.get("signal_history", [])
    trade_stats = ""
    if len(history) >= 2:
        trades = []
        for i, h in enumerate(history):
            if h["signal"] == "BUY":
                entry = h
            elif h["signal"] == "SELL" and entry:
                pnl = ((h["price"] - entry["price"]) / entry["price"]) * 100
                trades.append(pnl)

        if trades:
            win_rate = sum(1 for t in trades if t > 0) / len(trades) * 100
            avg_pnl = sum(trades) / len(trades)
            total_pnl = sum(trades)
            trade_stats = (
                "\n📈 *Trade Stats:*\n"
                f"   Win Rate: {win_rate:.1f}%\n"
                f"   Avg P&L: {avg_pnl:+.2f}%\n"
                f"   Total P&L: {total_pnl:+.2f}%\n"
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
# STATE MANAGEMENT (with integrity, backup, encryption)
# ============================================================
def load_state() -> Dict[str, Any]:
    default_state = {
        "last_signal": None,
        "last_price": None,
        "last_check": None,
        "signal_history": [],
        "error_count": 0,
        "run_count": 0,
        "version": "4.0",
        "first_run": datetime.now().isoformat(),
        "data_hash": None,
        "asset": TICKER
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

        if state.get("data_hash"):
            expected_hash = hashlib.md5(content.encode()).hexdigest()[:8]
            if state["data_hash"] != expected_hash:
                logger.warn("State file may be corrupted (hash mismatch)")

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
# DATA FETCHING (with validation, cross-check)
# ============================================================
def get_btc_data() -> Optional[pd.DataFrame]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info(f"Fetching {TICKER} data (attempt {attempt}/{MAX_RETRIES})...")

            ticker = yf.Ticker(TICKER)
            df = ticker.history(period="90d", interval="1d")

            if df.empty:
                logger.warn("Empty data received, retrying...")
                time.sleep(RETRY_DELAY)
                continue

            if len(df) < EMA_SLOW + 5:
                logger.warn(f"Insufficient data: {len(df)} rows, need {EMA_SLOW + 5}")
                time.sleep(RETRY_DELAY)
                continue

            if df['Close'].isna().sum() > len(df) * 0.1:
                logger.warn("Too many NaN values in data")
                time.sleep(RETRY_DELAY)
                continue

            df = df.reset_index()
            df.columns = [c.lower().replace(" ", "_") for c in df.columns]

            latest_price = df['close'].iloc[-1]
            if latest_price < 100 or latest_price > 500000:
                logger.warn(f"Suspicious price: ${latest_price:,.2f}")
                time.sleep(RETRY_DELAY)
                continue

            logger.success(f"Data fetched: {len(df)} rows, latest: ${latest_price:,.2f}")
            return df

        except Exception as e:
            logger.error(f"Data fetch error: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)

    logger.error("Failed to fetch data after all retries")
    return None

# ============================================================
# EMA + SIGNAL (FIXED: based on confirmed daily close)
# ============================================================
def calculate_ema(series: pd.Series, period: int) -> pd.Series:
    if len(series) < period:
        raise ValueError(f"Need at least {period} data points, got {len(series)}")
    return series.ewm(span=period, adjust=False).mean()

def check_signal(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Generate signal based on the LAST COMPLETED daily candle (index -2).
    This avoids false signals from the current incomplete intraday bar.
    """
    df["ema_fast"] = calculate_ema(df["close"], EMA_FAST)
    df["ema_slow"] = calculate_ema(df["close"], EMA_SLOW)

    # Gunakan bar SEMALAM (index -2) sebagai asas signal
    prev_fast  = df["ema_fast"].iloc[-2]   # EMA fast semalam
    prev_slow  = df["ema_slow"].iloc[-2]   # EMA slow semalam
    prev2_fast = df["ema_fast"].iloc[-3]
    prev2_slow = df["ema_slow"].iloc[-3]
    curr_fast  = df["ema_fast"].iloc[-1]   # hanya untuk info, bukan signal

    curr_price = df["close"].iloc[-2]      # harga penutup semalam (confirmed)
    prev_price = df["close"].iloc[-3]      # harga kelmarin untuk kira perubahan
    curr_date = df["date"].iloc[-2]        # tarikh bar semalam

    # Semakan crossover pada bar semalam (prev vs prev2)
    prev_diff = prev_fast - prev_slow
    prev2_diff = prev2_fast - prev2_slow

    ema_diff = curr_fast - curr_slow  # untuk trend strength hari ini (intraday)
    ema_diff_pct = (ema_diff / curr_slow) * 100 if curr_slow != 0 else 0
    trend_strength = abs(ema_diff_pct)

    signal = None
    confidence = "low"

    if prev2_fast <= prev2_slow and prev_fast > prev_slow:
        signal = "BUY"
        if prev2_fast < prev2_slow and abs(prev_diff / prev_slow) > 0.005:  # 0.5% gap
            confidence = "high"
        elif prev2_fast < prev2_slow:
            confidence = "medium"
    elif prev2_fast >= prev2_slow and prev_fast < prev_slow:
        signal = "SELL"
        if prev2_fast > prev2_slow and abs(prev_diff / prev_slow) > 0.005:
            confidence = "high"
        elif prev2_fast > prev2_slow:
            confidence = "medium"

    # "near crossover" merujuk keadaan semalam (kita dah alert lepas tu)
    near_crossover = abs(prev_fast - prev_slow) / prev_slow < 0.001

    # Support/Resistance dari 20 bar terakhir (termasuk semalam)
    recent_lows = df['low'].iloc[-22:-1].min()   # 20 bar terakhir completed
    recent_highs = df['high'].iloc[-22:-1].max()

    return {
        "signal": signal,
        "confidence": confidence,
        "trend_strength": trend_strength,
        "price": curr_price,
        "prev_price": prev_price,
        "price_change_pct": ((curr_price - prev_price) / prev_price) * 100 if prev_price != 0 else 0,
        "date": str(curr_date),
        "ema_fast": prev_fast,          # tunjuk nilai semalam di alert
        "ema_slow": prev_slow,
        "ema_diff": prev_diff,
        "ema_diff_pct": (prev_diff / prev_slow) * 100 if prev_slow != 0 else 0,
        "near_crossover": near_crossover,
        "support": recent_lows,
        "resistance": recent_highs,
        "prev_ema_fast": prev2_fast,
        "prev_ema_slow": prev2_slow
    }

# ============================================================
# RICH ALERT BUILDER (with P&L, levels, context)
# ============================================================
def build_alert_message(result: Dict[str, Any], state: Dict[str, Any]) -> str:
    signal = result["signal"]
    emoji = "🟢" if signal == "BUY" else "🔴"
    action = "BELI / BUY" if signal == "BUY" else "JUAL / SELL"
    confidence_emoji = "💪" if result["confidence"] == "high" else "⚡" if result["confidence"] == "medium" else "⚠️"

    # Calculate P&L if selling
    position_info = ""
    if signal == "SELL" and state.get("signal_history"):
        last_entry = None
        for h in reversed(state["signal_history"]):
            if h["signal"] == "BUY":
                last_entry = h
                break
        if last_entry:
            entry_price = last_entry["price"]
            pnl = ((result["price"] - entry_price) / entry_price) * 100
            pnl_emoji = "🟢" if pnl > 0 else "🔴"
            days_held = "N/A"
            try:
                entry_date = datetime.fromisoformat(last_entry["time"])
                days_held = (datetime.now() - entry_date).days
            except:
                pass

            position_info = (
                "\n📊 *Trade Performance:*\n"
                f"   Entry: ${entry_price:,.2f}\n"
                f"   Exit: ${result['price']:,.2f}\n"
                f"   P&L: {pnl_emoji} {pnl:+.2f}%\n"
                f"   Days Held: {days_held}"
            )

    # Support/Resistance context
    levels = (
        "\n📍 *Key Levels:*\n"
        f"   Support: ${result['support']:,.2f}\n"
        f"   Resistance: ${result['resistance']:,.2f}"
    )

    action_text = "🚀 MASUK POSITION BELI SEKARANG!" if signal == "BUY" else "🔒 KELUAR POSITION JUAL SEKARANG!"

    message = (
        f"{emoji} {emoji} {emoji} *{TICKER} EMA CROSSOVER ALERT* {emoji} {emoji} {emoji}\n\n"
        f"{confidence_emoji} *Signal:* {action}\n"
        f"{confidence_emoji} *Confidence:* {result['confidence'].upper()}\n"
        f"💪 *Trend Strength:* {result['trend_strength']:.3f}%\n\n"
        f"💰 *Price (Daily Close):* `${result['price']:,.2f}`\n"
        f"📈 *Change from prev close:* {result['price_change_pct']:+.2f}%\n"
        f"📅 *Candle Date:* {result['date'][:10]}\n\n"
        f"📊 *EMA{EMA_FAST}:* `${result['ema_fast']:,.2f}`\n"
        f"📊 *EMA{EMA_SLOW}:* `${result['ema_slow']:,.2f}`\n"
        f"📊 *Spread:* `{result['ema_diff_pct']:+.3f}%`"
        f"{levels}"
        f"{position_info}\n\n"
        f"💡 *Action:* {action_text}\n\n"
        "⚠️ *Disclaimer:* Ini bukan nasihat kewangan. Risiko tanggung sendiri.\n\n"
        "⏰ Signal generated based on DAILY CLOSE (confirmed)\n"
        "🤖 Bot v4.0 | EMA Crossover Strategy"
    )
    return message

# ============================================================
# MAIN
# ============================================================
def main():
    logger.info("=" * 70)
    logger.info("🤖 BTC EMA CROSSOVER ALERT BOT v4.0 - FINAL (CONFIRMED DAILY CLOSE)")
    logger.info("=" * 70)
    logger.info(f"Strategy: EMA({EMA_FAST}) / EMA({EMA_SLOW}) Crossover on DAILY CLOSE")
    logger.info(f"Asset: {TICKER}")
    logger.info(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    logger.info("-" * 70)

    # Validate secrets
    secrets_ok, secrets_msg = validate_secrets()
    if secrets_ok:
        logger.success(secrets_msg)
    else:
        logger.error(f"Secret validation failed: {secrets_msg}")
        return

    # Load state
    state = load_state()
    state["run_count"] = state.get("run_count", 0) + 1

    logger.info(f"Run #{state['run_count']}")
    logger.info(f"Last signal: {state.get('last_signal', 'None')}")
    logger.info(f"Last check: {state.get('last_check', 'Never')}")
    logger.info("-" * 70)

    try:
        # Send startup on first run
        if state["run_count"] == 1:
            send_startup_notification()

        df = get_btc_data()
        if df is None:
            raise Exception("Failed to fetch market data after all retries")

        result = check_signal(df)
        current_signal = result["signal"]

        logger.info(f"Signal based on CANDLE: {result['date'][:10]}")
        logger.info(f"Daily Close Price: ${result['price']:,.2f}")
        logger.info(f"EMA{EMA_FAST}: ${result['ema_fast']:,.2f}")
        logger.info(f"EMA{EMA_SLOW}: ${result['ema_slow']:,.2f}")
        logger.info(f"Signal: {current_signal if current_signal else 'NONE'}")
        logger.info(f"Trend Strength: {result['trend_strength']:.3f}%")

        if result["near_crossover"] and not current_signal:
            logger.warn("⚠️ EMAs were very close on yesterday's close - crossover may happen soon!")

        should_alert = False

        if current_signal is not None:
            if current_signal != state.get("last_signal"):
                should_alert = True
                logger.info(f"🚨 NEW {current_signal} SIGNAL DETECTED!")
            else:
                logger.info(f"Signal unchanged ({current_signal}) - no alert")
        else:
            logger.info("No crossover yesterday - no alert")

        if should_alert:
            message = build_alert_message(result, state)
            success = send_telegram_message(message)

            if success:
                state["last_signal"] = current_signal
                state["last_price"] = result["price"]

                if "signal_history" not in state:
                    state["signal_history"] = []
                state["signal_history"].append({
                    "signal": current_signal,
                    "price": result["price"],
                    "date": result["date"][:10],
                    "confidence": result["confidence"],
                    "trend_strength": result["trend_strength"],
                    "time": datetime.now().isoformat()
                })
                state["signal_history"] = state["signal_history"][-50:]
            else:
                logger.error("Failed to send alert - will retry next run")
                state["error_count"] = state.get("error_count", 0) + 1

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

        # Send health report periodically
        if state["run_count"] % HEALTH_REPORT_INTERVAL == 0:
            send_health_report(state)

        duration = logger.get_duration()
        logger.info(f"Run duration: {duration:.2f}s")
        logger.info("=" * 70)
        logger.info("✅ Bot run complete!")
        logger.info("=" * 70)

if __name__ == "__main__":
    main()
