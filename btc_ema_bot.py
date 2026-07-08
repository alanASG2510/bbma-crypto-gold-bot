#!/usr/bin/env python3
"""
BTC EMA Crossover Alert Bot v4.0 - FINAL (FIXED: daily close signal)
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
    """Validate secrets exist and look valid without logging them"""
    errors = []

    if not TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN not set")
    elif not TELEGRAM_BOT_TOKEN.replace(":", "").isalnum():
        errors.append("TELEGRAM_BOT_TOKEN format invalid")

    if not TELEGRAM_CHAT_ID:
        errors.append("TELEGRAM_CHAT_ID not set")
    else:
        try:
            int(TELEGRAM_CHAT_ID)
        except ValueError:
            errors.append("TELEGRAM_CHAT_ID must be numeric")

    if len(errors) == 0:
        # Log hash of token for debugging without exposing it
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
    message = f"""⚠️ *BOT ERROR ALERT*

The bot encountered an error:
