import os
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

# ONLY XAU/USD
GOLD_PAIR = 'XAU/USD'

STYLES = {
    'Intraday': {'big': '4h', 'small': '1h', 'period_big': 300, 'period_small': 300},
    'Swing': {'big': '1d', 'small': '4h', 'period_big': 730, 'period_small': 730}
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
# DATA FETCHER - TWELVEDATA ONLY
# ==========================================
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
        
        # Convert to DataFrame
        df = pd.DataFrame(data['values'])
        df = df.iloc[::-1]  # Reverse to get chronological order
        df['datetime'] = pd.to_datetime(df['datetime'])
        
        # Rename columns to lowercase standard
        df = df.rename(columns={
            'datetime': 'timestamp',
            'open': 'open',
            'high': 'high',
            'low': 'low',
            'close': 'close'
        })
        
        # Handle volume - make it optional
        if 'volume' in df.columns:
            df = df[['timestamp', 'open', 'high', 'low', 'close', 'volume']]
        else:
            df = df[['timestamp', 'open', 'high', 'low', 'close']]
            df['volume'] = 0  # Add dummy volume
        
        df.set_index('timestamp', inplace=True)
        
        if len(df) < 60:
            print(f"⚠️ Insufficient data for {symbol}: only {len(df)} candles")
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
    
    touch_zone = (curr['low'] <= curr['ma5_low'] * 1.003) or \
                 (curr['low'] <= curr['ma10_low'] * 1.003)
    
    valid_close = curr['close'] >= curr['bb_lower']
    
    is_bullish = curr['close'] > curr['open']
    prev_bearish = prev['close'] < prev['open']
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

def scan_xauusd():
    for style, tfs in STYLES.items():
        print(f"Scanning XAU/USD ({style})...")
        
        try:
            # Fetch data from TwelveData
            df_big = fetch_twelvedata_data(GOLD_PAIR, tfs['big'], tfs['period_big'])
            df_small = fetch_twelvedata_data(GOLD_PAIR, tfs['small'], tfs['period_small'])
            
            if df_big.empty or len(df_big) < 60:
                print(f"⚠️ Insufficient big TF data for XAU/USD")
                continue
            
            df_big = get_indicators(df_big)
            
            if not check_uptrend(df_big):
                print(f"ℹ️ XAU/USD ({style}) not in uptrend")
                continue
            
            if df_small.empty or len(df_small) < 60:
                print(f"⚠️ Insufficient small TF data for XAU/USD")
                continue
            
            df_small = get_indicators(df_small)
            
            setup = find_reentry_buy(df_small)
            if not setup:
                print(f"ℹ️ No Re-Entry Buy setup for XAU/USD ({style})")
                continue
            
            levels = calculate_levels(setup)
            
            msg = f"""
🚨 <b>BBMA BUY SETUP DETECTED</b>

📊 Pair: XAU/USD (Gold)
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
            print(f"🚨 SETUP FOUND: XAU/USD ({style})")
        except Exception as e:
            print(f"❌ Error XAU/USD {style}: {e}")

# ==========================================
# MAIN
# ==========================================
def main():
    print(f"=== BBMA XAU/USD Scan Start: {datetime.now()} ===")
    
    try:
        scan_xauusd()
    except Exception as e:
        print(f"❌ Error scanning XAU/USD: {e}")
    
    print("=== Scan Complete ===")

if __name__ == "__main__":
    main()

