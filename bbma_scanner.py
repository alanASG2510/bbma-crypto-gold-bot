import os
import pandas as pd
import numpy as np
import requests
from datetime import datetime
from typing import Dict, Optional, List
from enum import Enum

# ==========================================
# CONFIGURATION
# ==========================================
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
TWELVEDATA_API_KEY = os.environ.get('TWELVEDATA_API_KEY')

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise ValueError("Missing Telegram credentials in GitHub Secrets!")

GOLD_PAIR = 'XAU/USD'

STYLES = {
    'Intraday': {'big': '4h', 'small': '1h', 'period_big': 300, 'period_small': 300},
    'Swing': {'big': '1d', 'small': '4h', 'period_big': 730, 'period_small': 730}
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
# DATA FETCHER
# ==========================================
def fetch_twelvedata_data(symbol: str, interval: str, outputsize: int = 300) -> pd.DataFrame:
    if not TWELVEDATA_API_KEY:
        print("❌ TwelveData API key not found!")
        return pd.DataFrame()
    
    try:
        interval_map = {'1h': '1h', '4h': '4h', '1d': '1day'}
        td_interval = interval_map.get(interval, interval)
        
        url = "https://api.twelvedata.com/time_series"
        params = {
            'symbol': symbol, 'interval': td_interval,
            'outputsize': outputsize, 'apikey': TWELVEDATA_API_KEY, 'format': 'JSON'
        }
        
        response = requests.get(url, params=params, timeout=30)
        data = response.json()
        
        if 'values' not in data or not data['values']:
            print(f"❌ TwelveData error {symbol}: {data.get('message', 'No data')}")
            return pd.DataFrame()
        
        df = pd.DataFrame(data['values'])
        df = df.iloc[::-1]
        df['datetime'] = pd.to_datetime(df['datetime'])
        
        df = df.rename(columns={'datetime': 'timestamp'})
        
        numeric_cols = ['open', 'high', 'low', 'close', 'volume']
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        
        required = ['timestamp', 'open', 'high', 'low', 'close']
        df = df[[c for c in required if c in df.columns]]
        if 'volume' in df.columns:
            df['volume'] = pd.to_numeric(df['volume'], errors='coerce')
        else:
            df['volume'] = 0
        
        df.set_index('timestamp', inplace=True)
        
        if len(df) < 60:
            return pd.DataFrame()
        
        return df
    except Exception as e:
        print(f"❌ TwelveData error {symbol}: {e}")
        return pd.DataFrame()

# ==========================================
# BBMA STATE MACHINE (STRICT OMA ALLY)
# ==========================================
class BBMACycleTracker:
    """
    Tracks BBMA cycle state sequentially per Oma Ally:
    Extreme → TPW → MHV → CSA → Re-Entry
    Re-Entry ONLY valid after CSA. Resets on new Extreme/MHV.
    """
    
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
        """
        Process one candle and return setup dict if valid Re-Entry detected.
        Returns None otherwise.
        """
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
        
        # --- CHECK EXTREME BUY ---
        # MA5/10 Low keluar BB Lower + Reverse Candle (Bullish after Bearish)
        extreme_buy = (
            (ma5_low < bb_lower or ma10_low < bb_lower) and
            is_bullish and prev_bearish
        )
        
        # --- CHECK EXTREME SELL ---
        # MA5/10 High keluar BB Upper + Reverse Candle (Bearish after Bullish)
        extreme_sell = (
            (ma5_high > bb_upper or ma10_high > bb_upper) and
            is_bearish and prev_bullish
        )
        
        # New Extreme resets cycle
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
        
        # --- CHECK MHV (after Extreme) ---
        if self.state == BBMAState.EXTREME_BUY:
            # MHV Buy: Price tak close bawah BB Lower + Reverse Candle (Bearish)
            mhv_valid = (close >= bb_lower) and is_bearish and prev_bullish
            if mhv_valid:
                self.state = BBMAState.MHV_BUY
                self.mhv_price = high
                return None
            # MHV batal jika close luar BB Lower
            if close < bb_lower:
                self.reset()
                return None
        
        if self.state == BBMAState.EXTREME_SELL:
            # MHV Sell: Price tak close atas BB Upper + Reverse Candle (Bullish)
            mhv_valid = (close <= bb_upper) and is_bullish and prev_bearish
            if mhv_valid:
                self.state = BBMAState.MHV_SELL
                self.mhv_price = low
                return None
            # MHV batal jika close luar BB Upper
            if close > bb_upper:
                self.reset()
                return None
        
        # --- CHECK CSA (after MHV) ---
        if self.state == BBMAState.MHV_BUY:
            # CSA Buy: Close atas MA5/10 Low (early) atau atas Mid BB (strong)
            csa_early = close > ma5_low and close > ma10_low
            csa_strong = csa_early and close > bb_mid
            if csa_early:
                self.state = BBMAState.CSA_BUY
                self.csa_confirmed = True
                return None
        
        if self.state == BBMAState.MHV_SELL:
            # CSA Sell: Close bawah MA5/10 High (early) atau bawah Mid BB (strong)
            csa_early = close < ma5_high and close < ma10_high
            csa_strong = csa_early and close < bb_mid
            if csa_early:
                self.state = BBMAState.CSA_SELL
                self.csa_confirmed = True
                return None
        
        # --- CHECK RE-ENTRY (only after CSA confirmed) ---
        if self.state == BBMAState.CSA_BUY and self.csa_confirmed:
            # Re-Entry Buy: Price retrace ke MA5/10 Low zone
            # Close TIDAK boleh melebihi MA5/10 High atau Mid BB
            in_zone = (low <= ma5_low * 1.003) or (low <= ma10_low * 1.003)
            valid_reentry = (
                in_zone and
                close <= ma5_high and
                close <= ma10_high and
                close <= bb_mid and
                is_bullish and prev_bearish  # Reverse candle confirmation
            )
            if valid_reentry:
                self.state = BBMAState.REENTRY_BUY
                return {
                    'type': 'BUY',
                    'ma5_low': ma5_low, 'ma10_low': ma10_low,
                    'bb_lower': bb_lower, 'bb_upper': bb_upper,
                    'ma5_high': ma5_high, 'ma10_high': ma10_high,
                    'bb_mid': bb_mid
                }
        
        if self.state == BBMAState.CSA_SELL and self.csa_confirmed:
            # Re-Entry Sell: Price retrace ke MA5/10 High zone
            # Close TIDAK boleh melebihi MA5/10 Low atau Mid BB
            in_zone = (high >= ma5_high * 0.997) or (high >= ma10_high * 0.997)
            valid_reentry = (
                in_zone and
                close >= ma5_low and
                close >= ma10_low and
                close >= bb_mid and
                is_bearish and prev_bullish  # Reverse candle confirmation
            )
            if valid_reentry:
                self.state = BBMAState.REENTRY_SELL
                return {
                    'type': 'SELL',
                    'ma5_high': ma5_high, 'ma10_high': ma10_high,
                    'bb_upper': bb_upper, 'bb_lower': bb_lower,
                    'ma5_low': ma5_low, 'ma10_low': ma10_low,
                    'bb_mid': bb_mid
                }
        
        return None


# ==========================================
# LEVEL CALCULATION (STRICT BBMA)
# ==========================================
def calculate_levels_buy(setup: Dict) -> Dict:
    """
    BUY Levels (Oma Ally):
    - Entry: MA5 Low (aggressive) / MA10 Low (conservative)
    - SL: Below BB Lower (bukan percentage)
    - TP1: MA5/10 High | TP2: BB Upper | TP3: BB Upper + buffer
    """
    entry_aggressive = setup['ma5_low']
    entry_conservative = setup['ma10_low']
    entry_moderate = (entry_aggressive + entry_conservative) / 2
    
    sl = setup['bb_lower']  # SL strictly below BB Lower
    
    tp1 = setup['ma5_high']
    tp2 = setup['bb_upper']
    tp3 = setup['bb_upper'] * 1.02
    
    return {
        'conservative': {'entry': entry_conservative, 'sl': sl},
        'moderate': {'entry': entry_moderate, 'sl': sl},
        'aggressive': {'entry': entry_aggressive, 'sl': sl},
        'tp1': tp1, 'tp2': tp2, 'tp3': tp3
    }

def calculate_levels_sell(setup: Dict) -> Dict:
    """
    SELL Levels (Oma Ally):
    - Entry: MA5 High (aggressive) / MA10 High (conservative)
    - SL: Above BB Upper (bukan percentage)
    - TP1: MA5/10 Low | TP2: BB Lower | TP3: BB Lower - buffer
    """
    entry_aggressive = setup['ma5_high']
    entry_conservative = setup['ma10_high']
    entry_moderate = (entry_aggressive + entry_conservative) / 2
    
    sl = setup['bb_upper']  # SL strictly above BB Upper
    
    tp1 = setup['ma5_low']
    tp2 = setup['bb_lower']
    tp3 = setup['bb_lower'] * 0.98
    
    return {
        'conservative': {'entry': entry_conservative, 'sl': sl},
        'moderate': {'entry': entry_moderate, 'sl': sl},
        'aggressive': {'entry': entry_aggressive, 'sl': sl},
        'tp1': tp1, 'tp2': tp2, 'tp3': tp3
    }

# ==========================================
# SCANNER WITH STATE TRACKING
# ==========================================
def scan_xauusd():
    tracker = BBMACycleTracker()
    
    for style, tfs in STYLES.items():
        print(f"Scanning XAU/USD ({style})...")
        
        try:
            df_big = fetch_twelvedata_data(GOLD_PAIR, tfs['big'], tfs['period_big'])
            df_small = fetch_twelvedata_data(GOLD_PAIR, tfs['small'], tfs['period_small'])
            
            if df_big.empty or len(df_big) < 60:
                continue
            df_big = get_indicators(df_big)
            
            if df_small.empty or len(df_small) < 60:
                continue
            df_small = get_indicators(df_small)
            
            # Determine trend from big TF using EMA50 vs Mid BB
            last_big = df_big.iloc[-1]
            uptrend = last_big['ema50'] < last_big['bb_mid']
            downtrend = last_big['ema50'] > last_big['bb_mid']
            
            if not uptrend and not downtrend:
                print(f"ℹ️ XAU/USD ({style}) - No clear trend (EMA50 near BB Mid)")
                continue
            
            # Reset tracker per style/tf combination
            tracker.reset()
            
            # Walk through small TF candles sequentially to track BBMA cycle
            setup_found = None
            for i in range(1, len(df_small)):
                result = tracker.update(df_small.iloc[i], df_small.iloc[i-1])
                if result is not None:
                    setup_found = result
                    break  # First valid setup in current cycle
            
            if setup_found is None:
                print(f"ℹ️ XAU/USD ({style}) - No valid BBMA setup (cycle incomplete)")
                continue
            
            # Validate setup aligns with big TF trend
            if setup_found['type'] == 'BUY' and not uptrend:
                print(f"ℹ️ XAU/USD ({style}) - BUY setup ignored (big TF downtrend)")
                continue
            if setup_found['type'] == 'SELL' and not downtrend:
                print(f"ℹ️ XAU/USD ({style}) - SELL setup ignored (big TF uptrend)")
                continue
            
            # Calculate levels and send alert
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
            
            msg = f"""
{emoji} <b>BBMA {direction} SETUP DETECTED</b>

📊 Pair: XAU/USD (Gold)
⏱️ Style: {style}
📈 Pattern: {pattern}
✅ Cycle: Extreme → MHV → CSA → Re-Entry CONFIRMED

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

⚠️ <i>Pilih 1 level je ikut risk appetite kau! Verify live price on exchange.</i>
⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}
            """
            send_telegram(msg)
            print(f"🚨 {direction} SETUP FOUND: XAU/USD ({style})")
                
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

