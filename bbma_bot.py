import os
import pandas as pd
import numpy as np
import requests
from datetime import datetime, timedelta
from typing import Dict, Optional
from enum import Enum
import time

# ==========================================
# CONFIGURATION
# ==========================================
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
ALPHA_VANTAGE_KEY = os.environ.get('ALPHA_VANTAGE_KEY')

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise ValueError("Missing Telegram credentials!")
if not ALPHA_VANTAGE_KEY:
    print("⚠️ No Alpha Vantage key – will use Yahoo fallback only")

GOLD_PAIR = 'XAUUSD'

# Timeframes
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
# INDICATORS (exact Oma Ally)
# ==========================================
def calculate_lwma(series: pd.Series, period: int) -> pd.Series:
    weights = np.arange(1, period + 1)
    def lwma(window):
        return np.sum(window * weights) / np.sum(weights)
    return series.rolling(window=period).apply(lwma, raw=True)

def get_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df['bb_mid'] = df['close'].rolling(BB_PERIOD).mean()
    bb_std = df['close'].rolling(BB_PERIOD).std()
    df['bb_upper'] = df['bb_mid'] + (bb_std * BB_STD)
    df['bb_lower'] = df['bb_mid'] - (bb_std * BB_STD)
    df['ma5_high'] = calculate_lwma(df['high'], 5)
    df['ma10_high'] = calculate_lwma(df['high'], 10)
    df['ma5_low'] = calculate_lwma(df['low'], 5)
    df['ma10_low'] = calculate_lwma(df['low'], 10)
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
# DATA FETCHERS
# ==========================================
def fetch_alpha_vantage(interval: str) -> pd.DataFrame:
    """Fetch XAUUSD spot from Alpha Vantage"""
    if not ALPHA_VANTAGE_KEY:
        return pd.DataFrame()
    
    # Map BBMA interval to AV function & interval
    func_map = {
        '15m': 'FX_INTRADAY',
        '1h': 'FX_INTRADAY',
        '4h': 'FX_INTRADAY'   # we'll resample
    }
    interval_map = {
        '15m': '15min',
        '1h': '60min',
        '4h': '60min'
    }
    outputsize = 'full' if interval in ['15m', '1h'] else 'full'
    
    params = {
        'function': func_map[interval],
        'from_symbol': 'XAU',
        'to_symbol': 'USD',
        'interval': interval_map[interval],
        'apikey': ALPHA_VANTAGE_KEY,
        'datatype': 'json',
        'outputsize': outputsize
    }
    
    url = 'https://www.alphavantage.co/query'
    print(f"📡 Fetching XAUUSD spot from Alpha Vantage ({interval})...")
    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
        
        # Parse the time series
        key = f"Time Series FX ({interval_map[interval]})"
        if key not in data:
            print(f"❌ Alpha Vantage: no data for {interval}")
            return pd.DataFrame()
        
        ts = data[key]
        rows = []
        for dt_str, values in ts.items():
            rows.append({
                'timestamp': pd.to_datetime(dt_str),
                'open': float(values['1. open']),
                'high': float(values['2. high']),
                'low': float(values['3. low']),
                'close': float(values['4. close']),
            })
        df = pd.DataFrame(rows)
        df.set_index('timestamp', inplace=True)
        df = df.sort_index()
        
        # Resample 4h if needed
        if interval == '4h':
            df = df.resample('4h').agg({
                'open': 'first',
                'high': 'max',
                'low': 'min',
                'close': 'last'
            }).dropna()
        
        # Add volume column (dummy)
        df['volume'] = 0
        
        if len(df) < 60:
            print(f"❌ Alpha Vantage: only {len(df)} candles")
            return pd.DataFrame()
        
        latest = df['close'].iloc[-1]
        print(f"✅ Alpha Vantage {interval}: {len(df)} candles, latest: {latest:.2f}")
        return df[['open', 'high', 'low', 'close', 'volume']]
        
    except Exception as e:
        print(f"❌ Alpha Vantage error: {e}")
        return pd.DataFrame()

def fetch_yahoo_futures(interval: str) -> pd.DataFrame:
    """Fallback: Gold Futures (GC=F)"""
    try:
        import yfinance as yf
        symbol = "GC=F"
        interval_map = {'15m': '15m', '1h': '1h', '4h': '1h'}
        period_map = {'15m': '5d', '1h': '30d', '4h': '60d'}
        yf_interval = interval_map.get(interval, '1h')
        yf_period = period_map.get(interval, '30d')
        
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=yf_period, interval=yf_interval)
        if df.empty:
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
        df = df[['open', 'high', 'low', 'close', 'volume']].dropna()
        if len(df) >= 60:
            latest = df['close'].iloc[-1]
            print(f"✅ Yahoo Futures {interval}: {len(df)} candles, latest: {latest:.2f}")
            return df
        return pd.DataFrame()
    except Exception as e:
        print(f"❌ Yahoo Futures error: {e}")
        return pd.DataFrame()

def fetch_data(interval: str) -> pd.DataFrame:
    """Primary: Alpha Vantage, Fallback: Yahoo Futures"""
    df = fetch_alpha_vantage(interval)
    if not df.empty and len(df) >= 60:
        return df
    print("⚠️ Alpha Vantage failed – using Yahoo Futures fallback...")
    return fetch_yahoo_futures(interval)

# ==========================================
# BBMA STATE MACHINE (exact Oma Ally)
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
        
        # --- EXTREME ---
        # PDF: MA5/10 outside BB + reverse candle
        if (ma5_low < bb_lower or ma10_low < bb_lower) and is_bullish and prev_bearish:
            self.state = BBMAState.EXTREME_BUY
            self.extreme_price = low
            self.mhv_price = None
            self.csa_confirmed = False
            return None
        
        if (ma5_high > bb_upper or ma10_high > bb_upper) and is_bearish and prev_bullish:
            self.state = BBMAState.EXTREME_SELL
            self.extreme_price = high
            self.mhv_price = None
            self.csa_confirmed = False
            return None
        
        # --- MHV ---
        # PDF: after extreme, price cannot close outside BB, reverse candle
        if self.state == BBMAState.EXTREME_BUY:
            if close < bb_lower:
                self.reset()
                return None
            if is_bearish and prev_bullish:
                self.state = BBMAState.MHV_BUY
                self.mhv_price = high
                return None
        
        if self.state == BBMAState.EXTREME_SELL:
            if close > bb_upper:
                self.reset()
                return None
            if is_bullish and prev_bearish:
                self.state = BBMAState.MHV_SELL
                self.mhv_price = low
                return None
        
        # --- CSA (Candle Stick Arah) ---
        # PDF: close below/above MA5/10 (or mid BB for stronger)
        if self.state == BBMAState.MHV_BUY:
            if close > ma5_low and close > ma10_low:
                self.state = BBMAState.CSA_BUY
                self.csa_confirmed = True
                return None
        
        if self.state == BBMAState.MHV_SELL:
            if close < ma5_high and close < ma10_high:
                self.state = BBMAState.CSA_SELL
                self.csa_confirmed = True
                return None
        
        # --- RE-ENTRY ---
        # PDF: after CSA, candle close in MA5/10 zone, not exceeding mid BB
        if self.state == BBMAState.CSA_BUY and self.csa_confirmed:
            in_zone = (low <= ma5_low * 1.002) or (low <= ma10_low * 1.002)
            if in_zone and is_bullish and prev_bearish and close <= ma5_high and close <= ma10_high and close <= bb_mid:
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
            if in_zone and is_bearish and prev_bullish and close >= ma5_low and close >= ma10_low and close >= bb_mid:
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
# LEVELS (Oma Ally rules)
# ==========================================
def calculate_levels_buy(setup: Dict) -> Dict:
    # Aggressive: MA5 Low, Conservative: MA10 Low
    entry_agg = setup['ma5_low']
    entry_con = setup['ma10_low']
    sl = setup['bb_lower'] - 0.50   # buffer
    tp1 = setup['ma5_high']
    tp2 = setup['bb_upper']
    tp3 = setup['bb_upper'] + (setup['bb_upper'] - setup['bb_mid']) * 0.5
    return {
        'aggressive': {'entry': entry_agg, 'sl': sl},
        'conservative': {'entry': entry_con, 'sl': sl},
        'tp1': tp1, 'tp2': tp2, 'tp3': tp3,
        'current_price': setup['current_price']
    }

def calculate_levels_sell(setup: Dict) -> Dict:
    entry_agg = setup['ma5_high']
    entry_con = setup['ma10_high']
    sl = setup['bb_upper'] + 0.50
    tp1 = setup['ma5_low']
    tp2 = setup['bb_lower']
    tp3 = setup['bb_lower'] - (setup['bb_mid'] - setup['bb_lower']) * 0.5
    return {
        'aggressive': {'entry': entry_agg, 'sl': sl},
        'conservative': {'entry': entry_con, 'sl': sl},
        'tp1': tp1, 'tp2': tp2, 'tp3': tp3,
        'current_price': setup['current_price']
    }

# ==========================================
# MAIN
# ==========================================
def run_analysis():
    print("\n" + "="*60)
    print(f"🔍 BBMA Gold Analyzer - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("="*60)
    
    for style_name, timeframes in STYLES.items():
        print(f"\n📊 {style_name} Analysis")
        print("-"*40)
        big_tf = timeframes['big']
        small_tf = timeframes['small']
        print(f"⏰ Big TF: {big_tf} | Small TF: {small_tf}")
        
        df_small = fetch_data(small_tf)
        if df_small.empty:
            print(f"❌ No data for {small_tf}")
            continue
        df_big = fetch_data(big_tf)
        if df_big.empty:
            print(f"❌ No data for {big_tf}")
            continue
        
        df_small = get_indicators(df_small)
        df_big = get_indicators(df_big)
        
        latest_price = df_small['close'].iloc[-1]
        big_latest = df_big['close'].iloc[-1]
        print(f"💰 Current Price (small): {latest_price:.2f}")
        print(f"💰 Current Price (big):   {big_latest:.2f}")
        
        tracker = BBMACycleTracker()
        setups = []
        for i in range(20, len(df_small)):
            result = tracker.update(df_small.iloc[i], df_small.iloc[i-1])
            if result:
                setups.append(result)
        
        if setups:
            # Use the latest setup
            setup = setups[-1]
            print(f"\n📈 Latest Setup: {setup['type']} at {setup['current_price']:.2f}")
            
            if setup['type'] == 'BUY':
                levels = calculate_levels_buy(setup)
                msg = f"""
📊 <b>BBMA SETUP DETECTED - {style_name}</b>

Pair: XAU/USD (Gold)
Type: BUY
Current: {setup['current_price']:.2f}

<b>🔥 AGGRESSIVE ENTRY</b>
Entry: {levels['aggressive']['entry']:.2f}
SL:   {levels['aggressive']['sl']:.2f}

<b>🛡️ CONSERVATIVE ENTRY</b>
Entry: {levels['conservative']['entry']:.2f}
SL:   {levels['conservative']['sl']:.2f}

<b>🎯 TAKE PROFITS</b>
TP1: {levels['tp1']:.2f}
TP2: {levels['tp2']:.2f}
TP3: {levels['tp3']:.2f}

⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}
"""
                send_telegram(msg)
                
            elif setup['type'] == 'SELL':
                levels = calculate_levels_sell(setup)
                msg = f"""
📊 <b>BBMA SETUP DETECTED - {style_name}</b>

Pair: XAU/USD (Gold)
Type: SELL
Current: {setup['current_price']:.2f}

<b>🔥 AGGRESSIVE ENTRY</b>
Entry: {levels['aggressive']['entry']:.2f}
SL:   {levels['aggressive']['sl']:.2f}

<b>🛡️ CONSERVATIVE ENTRY</b>
Entry: {levels['conservative']['entry']:.2f}
SL:   {levels['conservative']['sl']:.2f}

<b>🎯 TAKE PROFITS</b>
TP1: {levels['tp1']:.2f}
TP2: {levels['tp2']:.2f}
TP3: {levels['tp3']:.2f}

⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}
"""
                send_telegram(msg)
        else:
            print("   No setup found")
            print(f"   Current state: {tracker.state}")

if __name__ == "__main__":
    run_analysis()
