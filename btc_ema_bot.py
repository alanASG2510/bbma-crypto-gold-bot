import yfinance as yf
import pandas as pd
import numpy as np
import os
import json
from datetime import datetime
import requests

# ============================================================
# CONFIGURATION
# ============================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TICKER = "BTC-USD"
EMA_FAST = 12
EMA_SLOW = 26
STATE_FILE = "bot_state.json"

# ============================================================
# TELEGRAM FUNCTIONS
# ============================================================
def send_telegram_message(message):
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
            print("✅ Telegram alert sent!")
            return True
        else:
            print(f"❌ Telegram error: {response.text}")
            return False
    except Exception as e:
        print(f"❌ Failed to send Telegram: {e}")
        return False

# ============================================================
# STATE MANAGEMENT (No git commit needed!)
# ============================================================
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {"last_signal": None, "last_price": None, "last_check": None, "signal_history": []}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ============================================================
# EMA CROSSOVER STRATEGY
# ============================================================
def get_btc_data():
    try:
        ticker = yf.Ticker(TICKER)
        df = ticker.history(period="60d", interval="1d")
        
        if df.empty:
            print("❌ No data received from yfinance")
            return None
            
        df = df.reset_index()
        df.columns = [c.lower().replace(" ", "_") for c in df.columns]
        
        print(f"✅ Fetched {len(df)} days of BTC data")
        print(f"   Latest price: ${df['close'].iloc[-1]:,.2f}")
        print(f"   Date: {df['date'].iloc[-1]}")
        
        return df
        
    except Exception as e:
        print(f"❌ Error fetching data: {e}")
        return None

def calculate_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def check_signal(df):
    df["ema_fast"] = calculate_ema(df["close"], EMA_FAST)
    df["ema_slow"] = calculate_ema(df["close"], EMA_SLOW)
    
    prev_fast = df["ema_fast"].iloc[-2]
    prev_slow = df["ema_slow"].iloc[-2]
    curr_fast = df["ema_fast"].iloc[-1]
    curr_slow = df["ema_slow"].iloc[-1]
    
    curr_price = df["close"].iloc[-1]
    curr_date = df["date"].iloc[-1]
    
    signal = None
    
    if prev_fast <= prev_slow and curr_fast > curr_slow:
        signal = "BUY"
    elif prev_fast >= prev_slow and curr_fast < curr_slow:
        signal = "SELL"
    
    return {
        "signal": signal,
        "price": curr_price,
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
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("-" * 60)
    
    state = load_state()
    print(f"📂 Last signal: {state.get('last_signal', 'None')}")
    print(f"📂 Last check: {state.get('last_check', 'Never')}")
    print("-" * 60)
    
    df = get_btc_data()
    if df is None:
        print("❌ Bot failed - could not fetch data")
        return
    
    result = check_signal(df)
    current_signal = result["signal"]
    
    print(f"\n📊 Current Price: ${result['price']:,.2f}")
    print(f"📊 EMA{EMA_FAST}: ${result['ema_fast']:,.2f}")
    print(f"📊 EMA{EMA_SLOW}: ${result['ema_slow']:,.2f}")
    print(f"📊 Signal: {current_signal if current_signal else 'NONE (no crossover)'}")
    
    should_alert = False
    
    if current_signal is not None:
        if current_signal != state.get("last_signal"):
            should_alert = True
            print(f"\n🚨 NEW {current_signal} SIGNAL DETECTED!")
        else:
            print(f"\n⏭️ Signal unchanged ({current_signal}) - no alert")
    else:
        print("\n⏭️ No crossover today - no alert")
    
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
        
        state["last_signal"] = current_signal
        state["last_price"] = result["price"]
        
        # Add to history
        if "signal_history" not in state:
            state["signal_history"] = []
        state["signal_history"].append({
            "signal": current_signal,
            "price": result["price"],
            "date": result["date"][:10],
            "time": datetime.now().isoformat()
        })
    
    state["last_check"] = datetime.now().isoformat()
    save_state(state)
    
    print("\n" + "=" * 60)
    print("✅ Bot run complete!")
    print("=" * 60)

if __name__ == "__main__":
    main()
