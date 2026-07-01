import yfinance as yf
import pandas as pd
import numpy as np
import os
import json
from datetime import datetime, timedelta
import requests

# ============================================================
# CONFIGURATION - EDIT THESE VALUES
# ============================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TICKER = "BTC-USD"  # yfinance Bitcoin ticker
EMA_FAST = 12
EMA_SLOW = 26
STATE_FILE = "bot_state.json"

# ============================================================
# TELEGRAM FUNCTIONS
# ============================================================
def send_telegram_message(message):
    """Send message to Telegram"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Telegram credentials not set!")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }

    try:
        response = requests.post(url, json=payload, timeout=30)
        if response.status_code == 200:
            print(f"✅ Telegram alert sent!")
            return True
        else:
            print(f"❌ Telegram error: {response.text}")
            return False
    except Exception as e:
        print(f"❌ Failed to send Telegram: {e}")
        return False

# ============================================================
# STATE MANAGEMENT
# ============================================================
def load_state():
    """Load previous signal state from file"""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"last_signal": None, "last_price": None, "last_check": None}

def save_state(state):
    """Save current signal state to file"""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ============================================================
# EMA CROSSOVER STRATEGY
# ============================================================
def get_btc_data():
    """Fetch BTC price data from yfinance"""
    try:
        ticker = yf.Ticker(TICKER)
        # Get 60 days of daily data (enough for 26 EMA)
        df = ticker.history(period="60d", interval="1d")

        if df.empty:
            print("❌ No data received from yfinance")
            return None

        # Reset index to make Date a column
        df = df.reset_index()
        df.columns = [c.lower().replace(" ", "_") for c in df.columns]

        print(f"✅ Fetched {len(df)} days of BTC data")
        print(f"   Latest price: ${df['close'].iloc[-1]:,.2f}")
        print(f"   Date: {df['date'].iloc[-1]}")

        return df

    except Exception as e:
        print(f"❌ Error fetching data: {e}")
        return None

def calculate_ema(df, period):
    """Calculate Exponential Moving Average"""
    return df["close"].ewm(span=period, adjust=False).mean()

def check_signal(df):
    """Check for EMA crossover signal"""
    # Calculate EMAs
    df["ema_fast"] = calculate_ema(df, EMA_FAST)
    df["ema_slow"] = calculate_ema(df, EMA_SLOW)

    # Get last two rows for crossover detection
    prev_fast = df["ema_fast"].iloc[-2]
    prev_slow = df["ema_slow"].iloc[-2]
    curr_fast = df["ema_fast"].iloc[-1]
    curr_slow = df["ema_slow"].iloc[-1]

    prev_price = df["close"].iloc[-2]
    curr_price = df["close"].iloc[-1]
    curr_date = df["date"].iloc[-1]

    signal = None

    # Bullish crossover: EMA12 crosses ABOVE EMA26
    if prev_fast <= prev_slow and curr_fast > curr_slow:
        signal = "BUY"

    # Bearish crossover: EMA12 crosses BELOW EMA26
    elif prev_fast >= prev_slow and curr_fast < curr_slow:
        signal = "SELL"

    return {
        "signal": signal,
        "price": curr_price,
        "prev_price": prev_price,
        "date": str(curr_date),
        "ema_fast": curr_fast,
        "ema_slow": curr_slow,
        "prev_ema_fast": prev_fast,
        "prev_ema_slow": prev_slow
    }

# ============================================================
# MAIN BOT LOGIC
# ============================================================
def main():
    print("=" * 60)
    print("🤖 BTC EMA CROSSOVER ALERT BOT")
    print("=" * 60)
    print(f"Strategy: EMA({EMA_FAST}) / EMA({EMA_SLOW}) Crossover")
    print(f"Asset: {TICKER}")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("-" * 60)

    # Load previous state
    state = load_state()
    print(f"📂 Last signal: {state.get('last_signal', 'None')}")
    print(f"📂 Last check: {state.get('last_check', 'Never')}")
    print("-" * 60)

    # Fetch data
    df = get_btc_data()
    if df is None:
        print("❌ Bot failed - could not fetch data")
        return

    # Check for signal
    result = check_signal(df)
    current_signal = result["signal"]

    print(f"\n📊 Current Price: ${result['price']:,.2f}")
    print(f"📊 EMA{EMA_FAST}: ${result['ema_fast']:,.2f}")
    print(f"📊 EMA{EMA_SLOW}: ${result['ema_slow']:,.2f}")
    print(f"📊 Signal: {current_signal if current_signal else 'NONE (no crossover)'}")

    # Determine if we should send alert
    should_alert = False

    if current_signal is not None:
        # New signal detected
        if current_signal != state.get("last_signal"):
            should_alert = True
            print(f"\n🚨 NEW {current_signal} SIGNAL DETECTED!")
        else:
            print(f"\n⏭️ Signal unchanged ({current_signal}) - no alert")
    else:
        print("\n⏭️ No crossover today - no alert")

    # Send Telegram alert if needed
    if should_alert:
        emoji = "🟢" if current_signal == "BUY" else "🔴"
        action = "BELI / BUY" if current_signal == "BUY" else "JUAL / SELL"

        message = f"""{emoji} *BTC EMA CROSSOVER ALERT* {emoji}

*Signal:* {action}
*Asset:* Bitcoin (BTC-USD)
*Price:* `${result['price']:,.2f}`
*Date:* {result['date'][:10]}

*EMA{EMA_FAST}:* `${result['ema_fast']:,.2f}`
*EMA{EMA_SLOW}:* `${result['ema_slow']:,.2f}`

*Strategy:* EMA({EMA_FAST}/{EMA_SLOW}) Crossover
*Action:* {'Masuk position BELI sekarang!' if current_signal == 'BUY' else 'Keluar position JUAL sekarang!'}

⏰ Bot check time: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}
"""

        send_telegram_message(message)

        # Update state
        state["last_signal"] = current_signal
        state["last_price"] = result["price"]

    # Always update check time
    state["last_check"] = datetime.now().isoformat()
    save_state(state)

    print("\n" + "=" * 60)
    print("✅ Bot run complete!")
    print("=" * 60)

if __name__ == "__main__":
    main()
