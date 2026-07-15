#!/usr/bin/env python3
"""
ETH Momentum Snap - Signal Detector
Optimized for Luno Malaysia Spot Trading
Capital: RM50
Strategy: Buy on momentum/oversold, sell on overbought/trailing stop
"""

import os
import json
import requests
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Optional, Dict, List

# ─── CONFIG ─────────────────────────────────────────────────────────────
INITIAL_CAPITAL = 50.0        # RM50
STOP_LOSS_PCT = 12.0          # 12% trailing stop
TAKE_PROFIT_PCT = 20.0        # 20% take profit
MAX_HOLD_DAYS = 60            # Max 60 days hold
FEE_RATE = 0.006              # 0.6% round-trip (Luno Malaysia ~0.3% each way)

# State file (persisted between runs)
STATE_FILE = "trade_state.json"

@dataclass
class TradeState:
    in_position: bool = False
    entry_price: float = 0.0
    entry_date: str = ""
    peak_price: float = 0.0
    capital: float = INITIAL_CAPITAL
    eth_amount: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0

    def to_dict(self):
        return {
            "in_position": self.in_position,
            "entry_price": self.entry_price,
            "entry_date": self.entry_date,
            "peak_price": self.peak_price,
            "capital": self.capital,
            "eth_amount": self.eth_amount,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            in_position=d.get("in_position", False),
            entry_price=d.get("entry_price", 0.0),
            entry_date=d.get("entry_date", ""),
            peak_price=d.get("peak_price", 0.0),
            capital=d.get("capital", INITIAL_CAPITAL),
            eth_amount=d.get("eth_amount", 0.0),
            total_trades=d.get("total_trades", 0),
            winning_trades=d.get("winning_trades", 0),
        )

# ─── DATA FETCHING ──────────────────────────────────────────────────────

def fetch_eth_data() -> pd.DataFrame:
    """Fetch last 250 days of ETH-USD data from Yahoo Finance."""
    import pandas as pd
    import yfinance as yf

    ticker = yf.Ticker("ETH-USD")
    df = ticker.history(period="250d", interval="1d")
    df = df.reset_index()
    df.columns = [c.replace(" ", "_") for c in df.columns]
    return df

# ─── INDICATORS ─────────────────────────────────────────────────────────

def calculate_indicators(df):
    """Calculate all technical indicators."""
    data = df.copy()

    # Moving averages
    data['SMA20'] = data['Close'].rolling(window=20).mean()
    data['SMA50'] = data['Close'].rolling(window=50).mean()
    data['SMA200'] = data['Close'].rolling(window=200).mean()

    # EMA & MACD
    data['EMA12'] = data['Close'].ewm(span=12, adjust=False).mean()
    data['EMA26'] = data['Close'].ewm(span=26, adjust=False).mean()
    data['MACD'] = data['EMA12'] - data['EMA26']
    data['MACD_Signal'] = data['MACD'].ewm(span=9, adjust=False).mean()
    data['MACD_Hist'] = data['MACD'] - data['MACD_Signal']

    # RSI
    delta = data['Close'].diff()
    gain = delta.where(delta > 0, 0).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    data['RSI'] = 100 - (100 / (1 + rs))

    # Bollinger Bands
    data['BB_Middle'] = data['Close'].rolling(window=20).mean()
    bb_std = data['Close'].rolling(window=20).std()
    data['BB_Upper'] = data['BB_Middle'] + bb_std * 2
    data['BB_Lower'] = data['BB_Middle'] - bb_std * 2
    data['BB_Width'] = data['BB_Upper'] - data['BB_Lower']

    # Volume
    data['Volume_SMA20'] = data['Volume'].rolling(window=20).mean()
    data['Volume_Ratio'] = data['Volume'] / data['Volume_SMA20']

    # Drawdown
    data['Peak'] = data['Close'].cummax()
    data['Drawdown'] = (data['Close'] - data['Peak']) / data['Peak'] * 100

    data['Price_Change_Pct'] = data['Close'].pct_change() * 100

    return data

# ─── SIGNAL GENERATION ──────────────────────────────────────────────────

def generate_signals(df):
    """Generate buy and sell signals."""
    data = df.copy()

    # Buy: "Blood in the Streets" - deep drawdown, recovering RSI, near support
    data['Buy_Blood'] = (
        (data['Drawdown'] < -25) &
        (data['RSI'] > 35) & (data['RSI'] < 50) &
        (data['Volume_Ratio'] > 1.3) &
        (data['Close'] < data['SMA200'] * 1.05) &
        (data['MACD_Hist'] > data['MACD_Hist'].shift(1))
    ).astype(int)

    # Buy: "Trend Kick" - early uptrend confirmation
    data['Buy_Trend'] = (
        (data['SMA20'] > data['SMA20'].shift(5)) &
        (data['Close'] > data['SMA50']) &
        (data['RSI'] > 45) & (data['RSI'] < 65) &
        (data['MACD'] > data['MACD_Signal']) &
        (data['Volume_Ratio'] > 1.1)
    ).astype(int)

    # Buy: "BB Squeeze Breakout"
    data['BB_Width_Pct'] = data['BB_Width'] / data['Close']
    data['BB_Squeeze'] = data['BB_Width_Pct'] < data['BB_Width_Pct'].rolling(50).quantile(0.2)
    data['Buy_Breakout'] = (
        data['BB_Squeeze'].shift(1) &
        (data['Close'] > data['BB_Upper']) &
        (data['Volume_Ratio'] > 1.5) &
        (data['RSI'] > 50) & (data['RSI'] < 75)
    ).astype(int)

    data['Signal_Buy'] = ((data['Buy_Blood'] == 1) | (data['Buy_Trend'] == 1) | (data['Buy_Breakout'] == 1)).astype(int)

    # Sell signals
    data['Signal_Sell_RSI'] = (
        (data['RSI'] > 75) & (data['Close'] > data['BB_Upper'] * 0.98)
    ).astype(int)

    data['Signal_Sell_MACD'] = (
        (data['MACD'] < data['MACD_Signal']) &
        (data['MACD'].shift(1) >= data['MACD_Signal'].shift(1)) &
        (data['RSI'] > 60)
    ).astype(int)

    return data

# ─── ALERT FUNCTIONS ────────────────────────────────────────────────────

def send_telegram_alert(message: str):
    """Send alert via Telegram bot."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        print("[ALERT - Telegram not configured]")
        print(message)
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}

    try:
        resp = requests.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        print("Telegram alert sent successfully!")
    except Exception as e:
        print(f"Failed to send Telegram alert: {e}")
        print(message)

def send_discord_alert(message: str):
    """Send alert via Discord webhook."""
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")

    if not webhook_url:
        return

    payload = {"content": message}
    try:
        resp = requests.post(webhook_url, json=payload, timeout=30)
        resp.raise_for_status()
        print("Discord alert sent!")
    except Exception as e:
        print(f"Discord alert failed: {e}")

# ─── MAIN LOGIC ─────────────────────────────────────────────────────────

def load_state() -> TradeState:
    """Load persisted trade state."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return TradeState.from_dict(json.load(f))
    return TradeState()

def save_state(state: TradeState):
    """Persist trade state."""
    with open(STATE_FILE, 'w') as f:
        json.dump(state.to_dict(), f, indent=2)

def format_alert(action: str, price: float, reason: str, state: TradeState) -> str:
    """Format alert message."""
    emoji = "🟢" if action == "BUY" else "🔴"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M UTC")

    msg = f"""{emoji} *ETH SIGNAL: {action}*

📅 *Time:* {timestamp}
💰 *ETH Price:* ${price:,.2f}
📊 *Reason:* {reason}

💼 *Portfolio:*
• Capital: RM{state.capital:.2f}
• ETH Held: {state.eth_amount:.6f}
• Total Trades: {state.total_trades}
• Win Rate: {state.winning_trades/max(state.total_trades,1)*100:.0f}%

⚙️ *Strategy:* Momentum Snap (SL: {STOP_LOSS_PCT}%, TP: {TAKE_PROFIT_PCT}%)
🏦 *Exchange:* Luno Malaysia (Spot Only)
💵 *Capital:* RM{INITIAL_CAPITAL:.0f}

_Go to Luno app to execute this trade manually._
"""
    return msg

def main():
    import pandas as pd

    print("=" * 60)
    print("ETH Momentum Snap - Signal Detector")
    print(f"Running at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Load state
    state = load_state()
    print(f"\nCurrent State:")
    print(f"  In Position: {state.in_position}")
    print(f"  Capital: RM{state.capital:.2f}")
    print(f"  ETH Amount: {state.eth_amount:.6f}")
    print(f"  Total Trades: {state.total_trades}")

    # Fetch and analyze data
    print("\nFetching ETH data...")
    df = fetch_eth_data()
    df = calculate_indicators(df)
    df = generate_signals(df)

    latest = df.iloc[-1]
    price = float(latest['Close'])
    date = str(latest['Date']) if hasattr(latest['Date'], 'strftime') else str(latest['Date'])

    print(f"\nLatest Data:")
    print(f"  Date: {date}")
    print(f"  Price: ${price:,.2f}")
    print(f"  RSI: {latest['RSI']:.1f}")
    print(f"  Drawdown: {latest['Drawdown']:.1f}%")
    print(f"  MACD: {latest['MACD']:.2f}")

    # ─── SELL CHECK ─────────────────────────────────────────────────────
    if state.in_position:
        state.peak_price = max(state.peak_price, price)

        trailing_stop = state.peak_price * (1 - STOP_LOSS_PCT / 100)
        take_profit = state.entry_price * (1 + TAKE_PROFIT_PCT / 100)
        entry_dt = datetime.fromisoformat(state.entry_date.replace('Z', '+00:00'))
        max_hold_dt = entry_dt + timedelta(days=MAX_HOLD_DAYS)

        sell_reason = None

        if price <= trailing_stop:
            sell_reason = f"Trailing Stop (${trailing_stop:,.2f})"
        elif price >= take_profit:
            sell_reason = f"Take Profit (${take_profit:,.2f})"
        elif datetime.now() >= max_hold_dt:
            sell_reason = f"Max Hold ({MAX_HOLD_DAYS} days)"
        elif latest['Signal_Sell_RSI'] == 1:
            sell_reason = "RSI Overbought (>75)"
        elif latest['Signal_Sell_MACD'] == 1:
            sell_reason = "MACD Bearish Cross"

        if sell_reason:
            # Execute sell
            sell_value = state.eth_amount * price
            fee = sell_value * FEE_RATE / 2
            state.capital = sell_value - fee
            pnl_pct = ((price / state.entry_price) - 1) * 100

            state.total_trades += 1
            if pnl_pct > 0:
                state.winning_trades += 1

            alert = format_alert("SELL", price, sell_reason, state)
            print(f"\n🚨 SELL SIGNAL! {sell_reason}")
            print(f"   P&L: {pnl_pct:+.1f}%")
            print(f"   Capital after: RM{state.capital:.2f}")

            send_telegram_alert(alert)
            send_discord_alert(alert)

            # Reset position
            state.in_position = False
            state.entry_price = 0.0
            state.entry_date = ""
            state.peak_price = 0.0
            state.eth_amount = 0.0

    # ─── BUY CHECK ──────────────────────────────────────────────────────
    elif not state.in_position and state.capital >= 10:
        if latest['Signal_Buy'] == 1:
            buy_reasons = []
            if latest['Buy_Blood'] == 1:
                buy_reasons.append("Blood in the Streets (deep drawdown recovery)")
            if latest['Buy_Trend'] == 1:
                buy_reasons.append("Trend Kick (momentum building)")
            if latest['Buy_Breakout'] == 1:
                buy_reasons.append("BB Squeeze Breakout")

            buy_reason = " + ".join(buy_reasons)

            # Execute buy
            fee = state.capital * FEE_RATE / 2
            buy_amount = (state.capital - fee) / price

            state.in_position = True
            state.entry_price = price
            state.entry_date = datetime.now().isoformat()
            state.peak_price = price
            state.eth_amount = buy_amount
            state.capital = 0.0

            alert = format_alert("BUY", price, buy_reason, state)
            print(f"\n🚨 BUY SIGNAL! {buy_reason}")
            print(f"   Entry: ${price:,.2f}")
            print(f"   ETH bought: {buy_amount:.6f}")

            send_telegram_alert(alert)
            send_discord_alert(alert)
        else:
            print("\n📊 No buy signal today.")
            print(f"   Buy_Blood: {latest['Buy_Blood']}, Buy_Trend: {latest['Buy_Trend']}, Buy_Breakout: {latest['Buy_Breakout']}")
    else:
        print(f"\n⏳ Waiting... (In position: {state.in_position}, Capital: RM{state.capital:.2f})")

    # Save state
    save_state(state)
    print("\nState saved. Done!")

if __name__ == "__main__":
    main()
