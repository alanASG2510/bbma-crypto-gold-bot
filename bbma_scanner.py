import os
import pandas as pd
import numpy as np
import requests
from datetime import datetime, timedelta
from typing import Dict, Optional
from enum import Enum

# ==========================================
# CONFIGURATION
# ==========================================
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
GOLDAPI_KEY = os.environ.get('GOLDAPI_KEY')

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise ValueError("Missing Telegram credentials in GitHub Secrets!")
if not GOLDAPI_KEY:
    raise ValueError("Missing GoldAPI key in GitHub Secrets!")

GOLD_PAIR = 'XAUUSD'

STYLES = {
    'Intraday': {'big': '1h', 'small': '15m', 'period_big': 300, 'period_small': 300},
    'Swing': {'big': '4h', 'small': '1h', 'period_big': 500, 'period_small': 500}
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
# INDICATOR CALCULATIONS
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
# DATA FETCHER - GOLDAPI.IO (REAL GOLD PRICE)
# ==========================================
def fetch_goldapi_ohlc(symbol: str, interval: str, limit: int = 300) -> pd.DataFrame:
    """
    Fetch real gold price dari GoldAPI.io
    Interval: 1min, 5min, 15min, 30min, 45min, 1h, 2h, 4h, 1day
    """
    try:
        # GoldAPI format symbol: XAUUSD (bukan XAU/USD)
        url = f"https://www.goldapi.io/api/{symbol}/{interval}"
        
        headers = {
            'x-access-token': GOLDAPI_KEY,
            'Content-Type': 'application/json'
        }
        
        # GoldAPI free tier ada limit, kita fetch latest data
        response = requests.get(url, headers=headers, timeout=30)
        data = response.json()
        
        if 'error' in data:
            print(f"❌ GoldAPI error: {data.get('error', 'Unknown')}")
            return pd.DataFrame()
        
        # GoldAPI return format berbeza — kita convert
        items = data if isinstance(data, list) else data.get('items', [])
        
        if not items:
            print(f"❌ No data from GoldAPI for {symbol} {interval}")
            return pd.DataFrame()
        
        # Convert ke DataFrame
        df = pd.DataFrame(items)
        
        # Rename columns GoldAPI → standard
        col_map = {
            'date': 'timestamp',
            'open_price': 'open',
            'high_price': 'high', 
            'low_price': 'low',
            'close_price': 'close'
        }
        df = df.rename(columns=col_map)
        
        # Parse timestamp
        if 'timestamp' in df.columns:
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            df.set_index('timestamp', inplace=True)
        
        # Ensure numeric
        for col in ['open', 'high', 'low', 'close']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        
        df['volume'] = 0  # GoldAPI takde volume
        
        df = df[['open', 'high', 'low', 'close', 'volume']].dropna()
        df = df.sort_index()
        
        if len(df) < 60:
            print(f"❌ Not enough data: {len(df)} candles")
            return pd.DataFrame()
        
        print(f"✅ GoldAPI {symbol} {interval}: {len(df)} candles, latest close: {df['close'].iloc[-1]:.2f}")
        return df
        
    except Exception as e:
        print(f"❌ GoldAPI error {symbol}: {e}")
        return pd.DataFrame()

# ==========================================
# BACKUP: Fetch dari Yahoo Finance (kalau GoldAPI fail)
# ==========================================
def fetch_yahoo_backup(symbol: str, interval: str, period_days: int = 60) -> pd.DataFrame:
    """Backup pakai Yahoo Finance untuk XAUUSD"""
    try:
        import yfinance as yf
        
        # Yahoo symbol untuk gold: GC=F (Gold Futures)
        yf_symbol = "GC=F"
        
        # Map interval
        interval_map = {'15m': '15m', '1h': '1h', '4h': '1h', '1day': '1d'}
        yf_interval = interval_map.get(interval, '1h')
        
        ticker = yf.Ticker(yf_symbol)
        df = ticker.history(period=f"{period_days}d", interval=yf_interval)
        
        if df.empty:
            return pd.DataFrame()
        
        df = df.reset_index()
        df = df.rename(columns={
            'Date': 'timestamp',
            'Datetime': 'timestamp',
            'Open': 'open',
            'High': 'high',
            'Low': 'low',
            'Close': 'close',
            'Volume': 'volume'
        })
        df.set_index('timestamp', inplace=True)
        
        print(f"✅ Yahoo Backup: {len(df)} candles, latest: {df['close'].iloc[-1]:.2f}")
        return df[['open', 'high', 'low', 'close', 'volume']]
        
    except Exception as e:
        print(f"❌ Yahoo backup failed: {e}")
        return pd.DataFrame()

def fetch_data(symbol: str, interval: str, limit: int = 300) -> pd.DataFrame:
    """Try GoldAPI dulu, kalau fail guna Yahoo"""
    df = fetch_goldapi_ohlc(symbol, interval, limit)
    if not df.empty and len(df) >= 60:
        return df
    
    print("⚠️ GoldAPI fail, trying Yahoo Finance backup...")
    return fetch_yahoo_backup(symbol, interval)

# ==========================================
# BBMA STATE MACHINE
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
        
        # --- EXTREME BUY ---
        extreme_buy = (
            (ma5_low < bb_lower or ma10_low < bb_lower) and
            is_bullish and prev_bearish
        )
        
        # --- EXTREME SELL ---
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
        
        # --- MHV ---
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
        
        # --- CSA ---
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
        
        # --- RE-ENTRY ---
        if self.state == BBMAState.CSA_BUY and self.csa_confirmed:
            in_zone = (low <= ma5_low * 1.003) or (low <= ma10_low * 1.003)
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
            in_zone = (high >= ma5_high * 0.997) or (high >= ma10_high * 0.997)
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
# LEVEL CALCULATION
# ==========================================
def calculate_levels_buy(setup: Dict) -> Dict:
    entry_aggressive = setup['ma5_low']
    entry_conservative = setup['ma10_low']
    entry_moderate = (entry_aggressive + entry_conservative) / 2
    
    sl = setup['bb_lower'] * 0.998  # SL sikit bawah BB Lower
    
    tp1 = setup['ma5_high']
    tp2 = setup['bb_upper']
    tp3 = setup['bb_upper'] * 1.015
    
    return {
        'conservative': {'entry': entry_conservative, 'sl': sl},
        'moderate': {'entry': entry_moderate, 'sl': sl},
        'aggressive': {'entry': entry_aggressive, 'sl': sl},
        'tp1': tp1, 'tp2': tp2, 'tp3': tp3,
        'current_price': setup['current_price']
    }

def calculate_levels_sell(setup: Dict) -> Dict:
    entry_aggressive = setup['ma5_high']
    entry_conservative = setup['ma10_high']
    entry_moderate = (entry_aggressive + entry_conservative) / 2
    
    sl = setup['bb_upper'] * 1.002  # SL sikit atas BB Upper
    
    tp1 = setup['ma5_low']
    tp2 = setup['bb_lower']
    tp3 = setup['bb_lower'] * 0.985
    
    return {
        'conservative': {'entry': entry_conservative, 'sl': sl},
        'moderate': {'entry': entry_moderate, 'sl': sl},
        'aggressive': {'entry': entry_aggressive, 'sl': sl},
        'tp1': tp1, 'tp2': tp2, 'tp3': tp3,
        'current_price': setup['current_price']
    }

# ==========================================
# SCANNER
# ==========================================
def scan_xauusd():
    tracker = BBMACycleTracker()
    
    for style, tfs in STYLES.items():
        print(f"\n🔍 Scanning XAU/USD ({style})...")
        
        try:
            df_big = fetch_data(GOLD_PAIR, tfs['big'], tfs['period_big'])
            df_small = fetch_data(GOLD_PAIR, tfs['small'], tfs['period_small'])
            
            if df_big.empty or len(df_big) < 60:
                print(f"❌ Big TF data insufficient")
                continue
            df_big = get_indicators(df_big)
            
            if df_small.empty or len(df_small) < 60:
                print(f"❌ Small TF data insufficient")
                continue
            df_small = get_indicators(df_small)
            
            # Trend check
            last_big = df_big.iloc[-1]
            uptrend = last_big['ema50'] < last_big['bb_mid']
            downtrend = last_big['ema50'] > last_big['bb_mid']
            
            if not uptrend and not downtrend:
                print(f"ℹ️ No clear trend")
                continue
            
            print(f"📊 Big TF Trend: {'UP' if uptrend else 'DOWN'}")
            print(f"📊 Latest Big TF: EMA50={last_big['ema50']:.2f}, BB Mid={last_big['bb_mid']:.2f}")
            
            tracker.reset()
            
            setup_found = None
            for i in range(1, len(df_small)):
                result = tracker.update(df_small.iloc[i], df_small.iloc[i-1])
                if result is not None:
                    setup_found = result
                    break
            
            if setup_found is None:
                print(f"ℹ️ No BBMA setup found")
                continue
            
            # Validate trend alignment
            if setup_found['type'] == 'BUY' and not uptrend:
                print(f"ℹ️ BUY setup ignored (downtrend)")
                continue
            if setup_found['type'] == 'SELL' and not downtrend:
                print(f"ℹ️ SELL setup ignored (uptrend)")
                continue
            
            # Build alert
            if setup_found['type'] == 'BUY':
                levels = calculate_levels_buy(setup_found)
                emoji = "🟢"
                direction = "BUY"
                pattern = "Bullish Re-Entry (After CSA)"
            else:
                levels = calculate_levels_sell(setup_found)
                emoji = "🔴"
                direction = "SELL"
                pattern = "Bearish Re-Entry (After CSA)"
            
            current = levels['current_price']
            
            msg = f"""
{emoji} <b>BBMA {direction} SETUP DETECTED</b>

📊 Pair: XAU/USD (Gold)
⏱️ Style: {style}
📈 Pattern: {pattern}
✅ Cycle: Extreme → MHV → CSA → Re-Entry CONFIRMED
💰 Current Price: {current:.2f}

━━━━━━━━━━━━━━━━━━━━

🟢 <b>CONSERVATIVE ENTRY</b>
Paling selamat, tunggu confirmation penuh
• Entry: {levels['conservative']['entry']:.2f}
• SL: {levels['conservative']['sl']:.2f}
• TP1: {levels['tp1']:.2f} | TP2: {levels['tp2']:.2f} | TP3: {levels['tp3']:.2f}

🟡 <b>MODERATE ENTRY</b>
Balance risk & reward
• Entry: {levels['moderate']['entry']:.2f}
• SL: {levels['moderate']['sl']:.2f}
• TP1: {levels['tp1']:.2f} | TP2: {levels['tp2']:.2f} | TP3: {levels['tp3']:.2f}

🔴 <b>AGGRESSIVE ENTRY</b>
Entry awal, harga terbaik, risiko tinggi
• Entry: {levels['aggressive']['entry']:.2f}
• SL: {levels['aggressive']['sl']:.2f}
• TP1: {levels['tp1']:.2f} | TP2: {levels['tp2']:.2f} | TP3: {levels['tp3']:.2f}

━━━━━━━━━━━━━━━━━━━━

⚠️ <i>Pilih 1 level je ikut risk appetite. Verify live price!</i>
⏰ {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}
            """
            send_telegram(msg)
            print(f"🚨 {direction} SETUP FOUND: XAU/USD ({style}) @ {current:.2f}")
                
        except Exception as e:
            print(f"❌ Error {style}: {e}")

# ==========================================
# MAIN
# ==========================================
def main():
    print(f"=== BBMA XAU/USD Scan Start: {datetime.now()} ===")
    try:
        scan_xauusd()
    except Exception as e:
        print(f"❌ Error: {e}")
    print("=== Scan Complete ===")

if __name__ == "__main__":
    main()
