import os
import yfinance as yf
import pandas as pd
import numpy as np
import requests
from datetime import datetime
from typing import Dict, Optional

# ==========================================
# CONFIGURATION
# ==========================================
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
TWELVEDATA_API_KEY = os.environ.get('TWELVEDATA_API_KEY')

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise ValueError("Missing Telegram credentials in GitHub Secrets!")

# Crypto pairs use Yahoo Finance
CRYPTO_PAIRS = ['BTC-USD', 'ETH-USD', 'SOL-USD', 'BNB-USD', 'XRP-USD', 'RENDER-USD']
# XAU/USD use TwelveData
GOLD_PAIR = 'XAU/USD'

STYLES = {
    'Intraday': {'big': '4h', 'small': '1h', 'period_big': '300d', 'period_small': '300d'},
    'Swing': {'big': '1d', 'small': '4h', 'period_big': '730d', 'period_small': '730d'}
}

BB_PERIOD = 20
BB_STD = 2.0

# ==========================================
# INDICATOR CALCULATIONS (Strict BBMA PDF)
# ==========================================
def calculate_lwma(series: pd.Series, period: int) -> pd.Series:
    weights = np.arange(1, period + 1)
    def lwma(window):
        return np.sum(window * weights) / np.sum(weights)
    return series.rolling(window=period).apply(lwma, raw=True)

def get_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [col.capitalize() for col in df.columns]
    df = df.copy()
    
    df['bb_mid'] = df['Close'].rolling(BB_PERIOD).mean()
    bb_std = df['Close'].rolling(BB_PERIOD).std()
    df['bb_upper'] = df['bb_mid'] + (bb_std * BB_STD)
    df['bb_lower'] = df['bb_mid'] - (bb_std * BB_STD)
    
    df['ma5_high'] = calculate_lwma(df['High'], 5)
    df['ma10_high'] = calculate_lwma(df['High'], 10)
    df['ma5_low'] = calculate_lwma(df['Low'], 5)
    df['ma10_low'] = calculate_lwma(df['Low'], 10)
    
    df['ema50'] = df['Close'].ewm(span=50, adjust=False).mean()
    
    return df.dropna()

# ==========================================
# TELEGRAM ALERT
# ==========================================
def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': message, 'parse_mode': 'HTML'}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            print(f"✅ Alert sent")
        else:
            print(f"❌ TG Error: {resp.text}")
    except Exception as e:
        print(f"❌ Failed: {e}")

# ==========================================
# DATA FETCHERS
# ==========================================
def fetch_yfinance_data(symbol: str, interval: str, period: str) -> pd.DataFrame:
    try:
        df = yf.download(symbol, interval=interval, period=period, progress=False)
        if df.empty or len(df) < 60:
            return pd.DataFrame()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except Exception as e:
        print(f"❌ yfinance error {symbol}: {e}")
        return pd.DataFrame()

def fetch_twelvedata_data(symbol: str, interval: str, outputsize: int = 300) -> pd.DataFrame:
    if not TWELVEDATA_API_KEY:
        print("❌ TwelveData API key not found!")
        return pd.DataFrame()
    
    try:
        interval_map = {
            '1h': '1h',
            '4h': '4h',
            '1d': '1day'
        }
        td_interval = interval_map.get(interval, interval)
        
        url = f"https://api.twelvedata.com/time_series"
        params = {
            'symbol': symbol,
            'interval': td_interval,
            'outputsize': outputsize,
            'apikey': TWELVEDATA_API_KEY,
            'format': 'JSON'
        }
        
        response = requests.get(url, params=params, timeout=30)
        data = response.json()
        
        if 'values' not in data or not data['values']:
            print(f"❌ TwelveData error {symbol}: {data.get('message', 'No data')}")
            return pd.DataFrame()
        
        df = pd.DataFrame(data['values'])
        df = df.iloc[::-1]
        df['datetime'] = pd.to_datetime(df['datetime'])
        df = df.rename(columns={
            'datetime': 'timestamp',
            'open': 'Open',
            'high': 'High',
            'low': 'Low',
            'close': 'Close',
            'volume': 'Volume'
        })
        df = df[['timestamp', 'Open', 'High', 'Low', 'Close', 'Volume']]
        df.set_index('timestamp', inplace=True)
        
        if len(df) < 60:
            return pd.DataFrame()
        
        return df
    except Exception as e:
        print(f"❌ TwelveData error {symbol}: {e}")
        return pd.DataFrame()

# ==========================================
# BBMA SCANNER LOGIC
# ==========================================
def check_uptrend(df: pd.DataFrame) -> bool:
    last = df.iloc[-1]
    return last['ema50'] < last['bb_mid']

def find_reentry_buy(df: pd.DataFrame) -> Optional[Dict]:
    if len(df) < 3:
        return None
    
    curr = df.iloc[-1]
    prev = df.iloc[-2]
    
    touch_zone = (curr['Low'] <= curr['ma5_low'] * 1.003) or \
                 (curr['Low'] <= curr['ma10_low'] * 1.003)
    
    valid_close = curr['Close'] >= curr['bb_lower']
    
    is_bullish = curr['Close'] > curr['Open']
    prev_bearish = prev['Close'] < prev['Open']
    reverse = is_bullish and prev_bearish
    
    if touch_zone and valid_close and reverse:
        return {
            'ma5_low': curr['ma5_low'],
            'ma10_low': curr['ma10_low'],
            'bb_lower': curr['bb_lower'],
            'bb_upper': curr['bb_upper'],
            'ma5_high': curr['ma5_high'],
            'ma10_high': curr['ma10_high'],
            'bb_mid': curr['bb_mid']
        }
    return None

def calculate_levels(setup: Dict) -> Dict:
    entry_high_risk = setup['ma5_low']
    entry_mid_risk = (setup['ma5_low'] + setup['ma10_low']) / 2
    entry_low_risk = setup['ma10_low']
    
    sl_base = setup['bb_lower']
    sl_high_risk = sl_base * 0.999
    sl_mid_risk = sl_base * 0.998
    sl_low_risk = sl_base * 0.997
    
    tp1 = setup['ma5_high']
    tp2 = setup['bb_upper']
    tp3 = setup['bb_upper'] * 1.02
    
    return {
        'high_risk': {'entry': entry_high_risk, 'sl': sl_high_risk},
        'mid_risk': {'entry': entry_mid_risk, 'sl': sl_mid_risk},
        'low_risk': {'entry': entry_low_risk, 'sl': sl_low_risk},
        'tp1': tp1, 'tp2': tp2, 'tp3': tp3
    }

def scan_pair(ticker: str, is_gold: bool = False):
    for style, tfs in STYLES.items():
        print(f"Scanning {ticker} ({style})...")
        
        try:
            if is_gold:
                df_big = fetch_twelvedata_data(ticker, tfs['big'], 300)
                df_small = fetch_twelvedata_data(ticker, tfs['small'], 300)
            else:
                df_big = fetch_yfinance_data(ticker, tfs['big'], tfs['period_big'])
                df_small = fetch_yfinance_data(ticker, tfs['small'], tfs['period_small'])
            
            if df_big.empty or len(df_big) < 60:
                continue
            
            df_big = get_indicators(df_big)
            
            if not check_uptrend(df_big):
                continue
            
            if df_small.empty or len(df_small) < 60:
                continue
            
            df_small = get_indicators(df_small)
            
            setup = find_reentry_buy(df_small)
            if not setup:
                continue
            
            levels = calculate_levels(setup)
            pair_name = ticker.replace('-USD', '/USDT') if not is_gold else 'XAU/USD'
            
            msg = f"""
🚨 <b>BBMA BUY SETUP DETECTED</b>

📊 Pair: {pair_name}
⏱️ Style: {style}
📈 Pattern: Bullish Rejection (Pinbar)

━━━━━━━━━━━━━━━━━━━━

🟢 <b>LOW RISK ENTRY (Konservatif)</b>
Paling selamat, tunggu confirmation penuh
• Entry: {levels['low_risk']['entry']:.2f}
• SL: {levels['low_risk']['sl']:.2f}
• TP1: {levels['tp1']:.2f} | TP2: {levels['tp2']:.2f} | TP3: {levels['tp3']:.2f}

🟡 <b>MID RISK ENTRY (Moderate)</b>
Balance risk & reward
• Entry: {levels['mid_risk']['entry']:.2f}
• SL: {levels['mid_risk']['sl']:.2f}
• TP1: {levels['tp1']:.2f} | TP2: {levels['tp2']:.2f} | TP3: {levels['tp3']:.2f}

🔴 <b>HIGH RISK ENTRY (Agresif)</b>
Entry awal, harga terbaik, risiko tinggi
• Entry: {levels['high_risk']['entry']:.2f}
• SL: {levels['high_risk']['sl']:.2f}
• TP1: {levels['tp1']:.2f} | TP2: {levels['tp2']:.2f} | TP3: {levels['tp3']:.2f}

━━━━━━━━━━━━━━━━━━━━

⚠️ <i>Pilih 1 level je ikut risk appetite kau! Verify live price on exchange.</i>
⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}
            """
            send_telegram(msg)
            print(f"🚨 SETUP FOUND: {ticker} ({style})")
        except Exception as e:
            print(f"❌ Error {ticker} {style}: {e}")

# ==========================================
# MAIN
# ==========================================
def main():
    print(f"=== BBMA Scan Start: {datetime.now()} ===")
    
    for ticker in CRYPTO_PAIRS:
        try:
            scan_pair(ticker, is_gold=False)
        except Exception as e:
            print(f"❌ Error {ticker}: {e}")
    
    try:
        scan_pair(GOLD_PAIR, is_gold=True)
    except Exception as e:
        print(f"❌ Error {GOLD_PAIR}: {e}")
    
    print("=== Scan Complete ===")

if __name__ == "__main__":
    main()

