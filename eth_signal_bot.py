#!/usr/bin/env python3
"""
ETH Momentum Snap - Signal Detector
Optimized for Luno Malaysia Spot Trading
Capital: RM50
Strategy: Buy on momentum/oversold, sell on overbought/trailing stop
UPDATED: Same structure as SOL bot — single file, yfinance only, USD prices.
"""

import os
import json
import requests
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Optional

import pandas as pd
import yfinance as yf

# ─── CONFIG ─────────────────────────────────────────────────────────────
INITIAL_CAPITAL = 50.0
STOP_LOSS_PCT = 12.0
TAKE_PROFIT_PCT = 20.0
MAX_HOLD_DAYS = 60
FEE_RATE = 0.006

STATE_FILE = "eth_trade_state.json"
LOG_FILE = "eth_bot.log"

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

def fetch_daily_data() -> pd.DataFrame:
    """Fetch daily data for signals."""
    ticker = yf.Ticker("ETH-USD")
    df = ticker.history(period="250d", interval="1d")
    df = df.reset_index()
    df.columns = [c.replace(" ", "_") for c in df.columns]
    return df

def fetch_live_price() -> float:
    """Fetch live price from 1‑hour data."""
    try:
        ticker = yf.Ticker("ETH-USD")
        df = ticker.history(period="1d", interval="1h")
        if not df.empty:
            return float(df['Close'].iloc[-1])
    except:
        pass
    try:
        ticker = yf.Ticker("ETH-USD")
        df = ticker.history(period="1d", interval="5m")
        if not df.empty:
            return float(df['Close'].iloc[-1])
    except:
        pass
    try:
        ticker = yf.Ticker("ETH-USD")
        df = ticker.history(period="1d", interval="1d")
        if not df.empty:
            return float(df['Close'].iloc[-1])
    except:
        return 0.0

# ─── INDICATORS ─────────────────────────────────────────────────────────

def calculate_indicators(df):
    data = df.copy()

    data['SMA20'] = data['Close'].rolling(20).mean()
    data['SMA50'] = data['Close'].rolling(50).mean()
    data['SMA200'] = data['Close'].rolling(200).mean()

    data['EMA12'] = data['Close'].ewm(span=12, adjust=False).mean()
    data['EMA26'] = data['Close'].ewm(span=26, adjust=False).mean()
    data['MACD'] = data['EMA12'] - data['EMA26']
    data['MACD_Signal'] = data['MACD'].ewm(span=9, adjust=False).mean()
    data['MACD_Hist'] = data['MACD'] - data['MACD_Signal']

    delta = data['Close'].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    data['RSI'] = 100 - (100 / (1 + rs))

    data['BB_Middle'] = data['Close'].rolling(20).mean()
    bb_std = data['Close'].rolling(20).std()
    data['BB_Upper'] = data['BB_Middle'] + bb_std * 2
    data['BB_Lower'] = data['BB_Middle'] - bb_std * 2
    data['BB_Width'] = data['BB_Upper'] - data['BB_Lower']

    data['Volume_SMA20'] = data['Volume'].rolling(20).mean()
    data['Volume_Ratio'] = data['Volume'] / data['Volume_SMA20']

    data['Peak'] = data['Close'].cummax()
    data['Drawdown'] = (data['Close'] - data['Peak']) / data['Peak'] * 100

    return data

def generate_signals(df):
    data = df.copy()

    # Buy signals
    data['Buy_Blood'] = (
        (data['Drawdown'] < -25) &
        (data['RSI'] > 35) & (data['RSI'] < 50) &
        (data['Volume_Ratio'] > 1.3) &
        (data['Close'] < data['SMA200'] * 1.05) &
        (data['MACD_Hist'] > data['MACD_Hist'].shift(1))
    ).astype(int)

    data['Buy_Trend'] = (
        (data['SMA20'] > data['SMA20'].shift(5)) &
        (data['Close'] > data['SMA50']) &
        (data['RSI'] > 45) & (data['RSI'] < 65) &
        (data['MACD'] > data['MACD_Signal']) &
        (data['Volume_Ratio'] > 1.1)
    ).astype(int)

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

# ─── TELEGRAM ──────────────────────────────────────────────────────────

def send_telegram(message: str):
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        print(message)
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=30).raise_for_status()
        print("Telegram sent.")
    except Exception as e:
        print(f"Telegram error: {e}")

# ─── STATE MANAGEMENT ──────────────────────────────────────────────────

def load_state() -> TradeState:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return TradeState.from_dict(json.load(f))
    return TradeState()

def save_state(state: TradeState):
    with open(STATE_FILE, 'w') as f:
        json.dump(state.to_dict(), f, indent=2)

# ─── MAIN ──────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("ETH Momentum Snap (Single File — Same as SOL)")
    print(f"Run: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    state = load_state()
    print(f"State: In pos={state.in_position}, Capital=RM{state.capital:.2f}")

    # Get signals from daily
    df = fetch_daily_data()
    df = calculate_indicators(df)
    df = generate_signals(df)
    latest = df.iloc[-1]

    # Get live price
    live_price = fetch_live_price()
    if live_price == 0.0:
        live_price = float(latest['Close'])

    print(f"Live Price: ${live_price:,.2f}")
    print(f"Daily Close: ${latest['Close']:,.2f}")
    print(f"RSI: {latest['RSI']:.1f}, Drawdown: {latest['Drawdown']:.1f}%")

    # ─── SELL LOGIC ──────────────────────────────────────────────────────
    if state.in_position:
        state.peak_price = max(state.peak_price, live_price)
        trailing_stop = state.peak_price * (1 - STOP_LOSS_PCT / 100)
        take_profit = state.entry_price * (1 + TAKE_PROFIT_PCT / 100)

        sell_reason = None
        if live_price <= trailing_stop:
            sell_reason = f"Trailing Stop (${trailing_stop:,.2f})"
        elif live_price >= take_profit:
            sell_reason = f"Take Profit (${take_profit:,.2f})"
        elif latest['Signal_Sell_RSI'] == 1:
            sell_reason = "RSI Overbought (>75)"
        elif latest['Signal_Sell_MACD'] == 1:
            sell_reason = "MACD Bearish Cross"

        if sell_reason:
            sell_value = state.eth_amount * live_price
            fee = sell_value * FEE_RATE / 2
            state.capital = sell_value - fee
            pnl = ((live_price / state.entry_price) - 1) * 100
            state.total_trades += 1
            if pnl > 0:
                state.winning_trades += 1

            msg = f"🔴 *SELL ETH* @ ${live_price:,.2f}\nReason: {sell_reason}\nP&L: {pnl:+.1f}%\nCapital: RM{state.capital:.2f}"
            send_telegram(msg)

            state.in_position = False
            state.entry_price = 0.0
            state.peak_price = 0.0
            state.eth_amount = 0.0
            state.entry_date = ""

    # ─── BUY LOGIC ──────────────────────────────────────────────────────
    elif not state.in_position and state.capital >= 10:
        if latest['Signal_Buy'] == 1:
            reasons = []
            if latest['Buy_Blood'] == 1:
                reasons.append("Blood")
            if latest['Buy_Trend'] == 1:
                reasons.append("Trend")
            if latest['Buy_Breakout'] == 1:
                reasons.append("BB Breakout")

            fee = state.capital * FEE_RATE / 2
            buy_amount = (state.capital - fee) / live_price

            state.in_position = True
            state.entry_price = live_price
            state.entry_date = datetime.now().isoformat()
            state.peak_price = live_price
            state.eth_amount = buy_amount
            state.capital = 0.0

            msg = f"🟢 *BUY ETH* @ ${live_price:,.2f}\nReason: {', '.join(reasons)}\nETH: {buy_amount:.6f}"
            send_telegram(msg)
        else:
            print("No buy signal.")

    save_state(state)
    print("Done.")

if __name__ == "__main__":
    main()
