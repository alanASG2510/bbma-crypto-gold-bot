import os
import pandas as pd
import numpy as np
import requests
from datetime import datetime
from typing import Dict, Optional
from enum import Enum

# ==========================================
# CONFIGURATION
# ==========================================
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise ValueError("Missing Telegram credentials!")

GOLD_PAIR = 'XAUUSD'

# Intraday: H1 (big) + M15 (small) | Swing: H4 (big) + H1 (small)
STYLES = {
    'Intraday': {'big': '1h', 'small': '15m'},
    'Swing': {'big': '4h', 'small': '1h'}
}

BB_PERIOD = 20
BB_STD = 2.0

class BBMAState(Enum):
    NONE = 0
    EXTREME_BUY = 1
    EXTREME_SELL = 2
    MHV_BUY = 3
    MHV_SELL = 4
    CSA_BUY = 5
    CSA_SELL = 6
    REENTRY_BUY = 7
    REENTRY_SELL = 8

# ==========================================
# INDICATORS (Exact BBMA Settings)
# ==========================================
def calculate_lwma(series: pd.Series, period: int) -> pd.Series:
    weights = np.arange(1, period + 1)
    def lwma(window):
        return np.sum(window * weights) / np.sum(weights)
    return series.rolling(window=period).apply(lwma, raw=True)

def get_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    
    # Bollinger Bands (20, 2, Close)
    df['bb_mid'] = df['close'].rolling(BB_PERIOD).mean()
    bb_std = df['close'].rolling(BB_PERIOD).std()
    df['bb_upper'] = df['bb_mid'] + (bb_std * BB_STD)
    df['bb_lower'] = df['bb_mid'] - (bb_std * BB_STD)
    
    # LWMA 5/10 High & Low (Oma Ally exact)
    df['ma5_high'] = calculate_lwma(df['high'], 5)
    df['ma10_high'] = calculate_lwma(df['high'], 10)
    df['ma5_low'] = calculate_lwma(df['low'], 5)
    df['ma10_low'] = calculate_lwma(df['low'], 10)
    
    # EMA 50 for trend
    df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()
    
    return df.dropna()

# ==========================================
# TELEGRAM
# ==========================================
def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': message, 'parse_mode': 'HTML'}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            print("✅ Alert sent")
        else:
            print(f"❌ TG Error: {resp.text}")
    except Exception as e:
        print(f"❌ Failed: {e}")

# ==========================================
# YAHOO FINANCE FETCHER (Primary & Only Source)
# ==========================================
def fetch_yahoo_data(interval: str) -> pd.DataFrame:
    """
    Yahoo Finance for XAU/USD spot
    Symbol: GC=F (Gold Futures) - most reliable free source
    """
    try:
        import yfinance as yf
        
        # Gold Futures symbol on Yahoo
        symbol = "GC=F"
        
        # Map BBMA intervals to Yahoo intervals
        interval_map = {
            '15m': '15m',
            '1h': '1h', 
            '4h': '1h'  # Yahoo doesn't have 4h, use 1h and resample
        }
        yf_interval = interval_map.get(interval, '1h')
        
        # Period mapping (need enough data for 50 EMA + 20 BB)
        period_map = {
            '15m': '5d',
            '1h': '30d',
            '4h': '60d'
        }
        yf_period = period_map.get(interval, '30d')
        
        print(f"📡 Fetching {symbol} {interval} from Yahoo Finance...")
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=yf_period, interval=yf_interval)
        
        if df.empty:
            print(f"❌ No data from Yahoo")
            return pd.DataFrame()
        
        # Reset index to get datetime column
        df = df.reset_index()
        
        # Rename columns
        df = df.rename(columns={
            'Datetime': 'timestamp',
            'Open': 'open',
            'High': 'high',
            'Low': 'low',
            'Close': 'close',
            'Volume': 'volume'
        })
        
        df.set_index('timestamp', inplace=True)
        
        # Ensure numeric
        for col in ['open', 'high', 'low', 'close']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        
        # Resample 1h to 4h if needed
        if interval == '4h':
            df = df.resample('4h').agg({
                'open': 'first',
                'high': 'max',
                'low': 'min',
                'close': 'last',
                'volume': 'sum'
            }).dropna()
        
        df = df[['open', 'high', 'low', 'close', 'volume']].dropna()
        
        if len(df) < 60:
            print(f"❌ Not enough data: {len(df)} candles")
            return pd.DataFrame()
        
        latest = df['close'].iloc[-1]
        print(f"✅ Yahoo {interval}: {len(df)} candles, latest: {latest:.2f}")
        return df
        
    except Exception as e:
        print(f"❌ Yahoo error: {e}")
        return pd.DataFrame()

# ==========================================
# BACKUP: FOREX API (if Yahoo fails)
# ==========================================
def fetch_forex_api(interval: str) -> pd.DataFrame:
    """Backup using forex API"""
    try:
        # Alpha Vantage or similar could go here
        # For now, try alternative Yahoo symbol
        import yfinance as yf
        
        # Try XAUUSD spot via different route
        symbol = "XAUUSD=X"  # Yahoo forex symbol
        
        yf_interval = {'15m': '15m', '1h': '1h', '4h': '1h'}.get(interval, '1h')
        yf_period = {'15m': '5d', '1h': '30d', '4h': '60d'}.get(interval, '30d')
        
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=yf_period, interval=yf_interval)
        
        if df.empty or len(df) < 10:
            return pd.DataFrame()
        
        df = df.reset_index()
        df = df.rename(columns={
            'Datetime': 'timestamp',
            'Open': 'open', 'High': 'high',
            'Low': 'low', 'Close': 'close',
            'Volume': 'volume'
        })
        df.set_index('timestamp', inplace=True)
        
        if interval == '4h':
            df = df.resample('4h').agg({
                'open': 'first', 'high': 'max',
                'low': 'min', 'close': 'last',
                'volume': 'sum'
            }).dropna()
        
        return df[['open', 'high', 'low', 'close', 'volume']].dropna()
        
    except Exception as e:
        print(f"❌ Backup failed: {e}")
        return pd.DataFrame()

def fetch_data(interval: str) -> pd.DataFrame:
    """Fetch with fallback"""
    df = fetch_yahoo_data(interval)
    if not df.empty and len(df) >= 60:
        return df
    
    print("⚠️ Trying backup source...")
    return fetch_forex_api(interval)

# ==========================================
# BBMA STATE MACHINE (Strict Oma Ally)
# ==========================================
class BBMACycleTracker:
    def __init__(self):
        self.state = BBMAState.NONE
        self.extreme_price = None
        self.mhv_price = None
        self.csa_confirmed = False
    
    def reset(self):
        self.state = BBMAState.NONE
        self.extreme_price = None
        self.mhv_price = None
        self.csa_confirmed = False
    
    def update(self, row: pd.Series, prev_row: pd.Series) -> Optional[Dict]:
        close = row['close']
        open_ = row['open']
        high = row['high']
        low = row['low']
        bb_upper = row['bb_upper']
        bb_lower = row['bb_lower']
        bb_mid = row['bb_mid']
        ma5_high = row['ma5_high']
        ma10_high = row['ma10_high']
        ma5_low = row['ma5_low']
        ma10_low = row['ma10_low']
        
        is_bullish = close > open_
        is_bearish = close < open_
        prev_bullish = prev_row['close'] > prev_row['open']
        prev_bearish = prev_row['close'] < prev_row['open']
        
        # --- EXTREME BUY (PDF: MA5/10 Low outside BB Lower + Reverse Candle) ---
        extreme_buy = (
            (ma5_low < bb_lower or ma10_low < bb_lower) and
            is_bullish and prev_bearish
        )
        
        # --- EXTREME SELL (PDF: MA5/10 High outside BB Upper + Reverse Candle) ---
        extreme_sell = (
            (ma5_high > bb_upper or ma10_high > bb_upper) and
            is_bearish and prev_bullish
        )
        
        if extreme_buy:
            self.state = BBMAState.EXTREME_BUY
            self.extreme_price = low
            self.mhv_price = None
            self.csa_confirmed = False
            return None
        
        if extreme_sell:
            self.state = BBMAState.EXTREME_SELL
            self.extreme_price = high
            self.mhv_price = None
            self.csa_confirmed = False
            return None
        
        # --- MHV (After Extreme) ---
        # PDF: "Price cannot close outside BB" / "Candle close dalam BB"
        if self.state == BBMAState.EXTREME_BUY:
            mhv_valid = (close >= bb_lower) and is_bearish and prev_bullish
            if mhv_valid:
                self.state = BBMAState.MHV_BUY
                self.mhv_price = high
                return None
            if close < bb_lower:
                self.reset()
                return None
        
        if self.state == BBMAState.EXTREME_SELL:
            mhv_valid = (close <= bb_upper) and is_bullish and prev_bearish
            if mhv_valid:
                self.state = BBMAState.MHV_SELL
                self.mhv_price = low
                return None
            if close > bb_upper:
                self.reset()
                return None
        
        # --- CSA (After MHV) ---
        # PDF: "CS Arah - Body CS Close bawah/atas MA5/10 atau MID BB"
        if self.state == BBMAState.MHV_BUY:
            csa_early = close > ma5_low and close > ma10_low
            if csa_early:
                self.state = BBMAState.CSA_BUY
                self.csa_confirmed = True
                return None
        
        if self.state == BBMAState.MHV_SELL:
            csa_early = close < ma5_high and close < ma10_high
            if csa_early:
                self.state = BBMAState.CSA_SELL
                self.csa_confirmed = True
                return None
        
        # --- RE-ENTRY (Only after CSA confirmed) ---
        # PDF: "Re-Entry - Selepas CS Arah, ada Candle Close di zone MA5/10"
        # "Close tidak melebihi MID BB (lebih kuat)"
        if self.state == BBMAState.CSA_BUY and self.csa_confirmed:
            in_zone = (low <= ma5_low * 1.002) or (low <= ma10_low * 1.002)
            valid_reentry = (
                in_zone and
                close <= ma5_high and
                close <= ma10_high and
                close <= bb_mid and
                is_bullish and prev_bearish
            )
            if valid_reentry:
                self.state = BBMAState.REENTRY_BUY
                return {
                    'type': 'BUY',
                    'ma5_low': ma5_low, 'ma10_low': ma10_low,
                    'bb_lower': bb_lower, 'bb_upper': bb_upper,
                    'ma5_high': ma5_high, 'ma10_high': ma10_high,
                    'bb_mid': bb_mid,
                    'current_price': close
                }
        
        if self.state == BBMAState.CSA_SELL and self.csa_confirmed:
            in_zone = (high >= ma5_high * 0.998) or (high >= ma10_high * 0.998)
            valid_reentry = (
                in_zone and
                close >= ma5_low and
                close >= ma10_low and
                close >= bb_mid and
                is_bearish and prev_bullish
            )
            if valid_reentry:
                self.state = BBMAState.REENTRY_SELL
                return {
                    'type': 'SELL',
                    'ma5_high': ma5_high, 'ma10_high': ma10_high,
                    'bb_upper': bb_upper, 'bb_lower': bb_lower,
                    'ma5_low': ma5_low, 'ma10_low': ma10_low,
                    'bb_mid': bb_mid,
                    'current_price': close
                }
        
        return None

# ==========================================
# LEVELS (Exact Oma Ally Rules - 2 Prices Only)
# ==========================================
def calculate_levels_buy(setup: Dict) -> Dict:
    """
    BUY Levels (Oma Ally):
    - Aggressive Entry: MA5 Low
    - Conservative Entry: MA10 Low  
    - SL: Below BB Lower
    - TP1: MA5/10 High | TP2: BB Upper | TP3: Extended
    """
    entry_aggressive = setup['ma5_low']
    entry_conservative = setup['ma10_low']
    
    # SL: Below BB Lower (PDF: "SL below BB Lower")
    sl = setup['bb_lower'] - 0.50  # 50 cents buffer for gold
    
    tp1 = setup['ma5_high']
    tp2 = setup['bb_upper']
    tp3 = setup['bb_upper'] + (setup['bb_upper'] - setup['bb_mid']) * 0.5
    
    return {
        'aggressive': {'entry': entry_aggressive, 'sl': sl},
        'conservative': {'entry': entry_conservative, 'sl': sl},
        'tp1': tp1, 'tp2': tp2, 'tp3': tp3,
        'current_price': setup['current_price']
    }

def calculate_levels_sell(setup: Dict) -> Dict:
    """
    SELL Levels (Oma Ally):
    - Aggressive Entry: MA5 High
    - Conservative Entry: MA
