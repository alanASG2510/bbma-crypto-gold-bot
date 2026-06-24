import os
import pandas as pd
import numpy as np
import requests
import yfinance as yf
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Tuple
from enum import Enum

# ==========================================
# CONFIGURATION
# ==========================================
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
GOLDAPI_KEY = os.environ.get('GOLDAPI_KEY')

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise ValueError("Missing Telegram credentials! Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in environment variables.")

GOLD_PAIR = 'XAU/USD'

# Intraday & Swing sahaja
STYLES = {
    'Intraday': {'big': '4h', 'small': '1h', 'yahoo_big': '1h', 'yahoo_small': '1h', 'period': '30d'},
    'Swing': {'big': '1d', 'small': '4h', 'yahoo_big': '1d', 'yahoo_small': '1h', 'period': '90d'}
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
# TELEGRAM ALERT
# ==========================================
def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': message, 'parse_mode': 'HTML'}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            print(f"✅ Alert sent to Telegram")
        else:
            print(f"❌ TG Error: {resp.status_code} - {resp.text}")
    except Exception as e:
        print(f"❌ Failed to send Telegram: {e}")

# ==========================================
# REAL-TIME PRICE FETCHER (GoldAPI.io)
# ==========================================
def fetch_goldapi_realtime():
    """Fetch real-time XAU/USD dari GoldAPI.io (1-5 min delay)"""
    if not GOLDAPI_KEY:
        return None
    
    try:
        url = "https://www.goldapi.io/api/XAU/USD"
        headers = {"x-access-token": GOLDAPI_KEY}
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()
        
        price = data.get('price')
        if price and 2000 <= price <= 5000:
            return {
                'price': float(price),
                'bid': float(data.get('bid', price)),
                'ask': float(data.get('ask', price)),
                'timestamp': data.get('timestamp'),
                'source': 'GoldAPI'
            }
    except Exception as e:
        print(f"❌ GoldAPI error: {e}")
    return None

def fetch_yahoo_realtime():
    """Fallback: Yahoo Finance (delayed ~15min)"""
    try:
        ticker = yf.Ticker("XAUUSD=X")
        data = ticker.history(period="1d", interval="1m")
        if not data.empty:
            latest = data.iloc[-1]
            price = float(latest['Close'])
            if 2000 <= price <= 5000:
                return {
                    'price': price,
                    'bid': price,
                    'ask': price,
                    'timestamp': int(datetime.now().timestamp()),
                    'source': 'Yahoo'
                }
    except Exception as e:
        print(f"❌ Yahoo realtime error: {e}")
    return None

def fetch_realtime_price():
    """Get real-time price: GoldAPI first, Yahoo fallback"""
    result = fetch_goldapi_realtime()
    if result:
        print(f"💰 GoldAPI Real-time: {result['price']:.2f} (Bid: {result['bid']:.2f}, Ask: {result['ask']:.2f})")
        return result
    
    result = fetch_yahoo_realtime()
    if result:
        print(f"💰 Yahoo Fallback: {result['price']:.2f} (delayed ~15min)")
        return result
    
    print("❌ Cannot fetch real-time price!")
    return None

# ==========================================
# HISTORICAL DATA (Yahoo Finance)
# ==========================================
def fetch_yahoo_historical(yahoo_interval='1h', period='60d'):
    """Fetch historical OHLCV dari Yahoo Finance (GC=F futures)"""
    try:
        ticker = yf.Ticker("GC=F")
        df = ticker.history(period=period, interval=yahoo_interval)
        
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
        
        # Remove timezone info
        if df['timestamp'].dt.tz is not None:
            df['timestamp'] = df['timestamp'].dt.tz_localize(None)
        
        df.set_index('timestamp', inplace=True)
        
        # Ensure numeric
        for col in ['open', 'high', 'low', 'close', 'volume']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        
        print(f"📊 Yahoo historical: {len(df)} candles ({yahoo_interval})")
        return df
        
    except Exception as e:
        print(f"❌ Yahoo historical error: {e}")
        return pd.DataFrame()

# ==========================================
# INDICATOR CALCULATIONS (BBMA Settings)
# ==========================================
def calculate_lwma(series: pd.Series, period: int) -> pd.Series:
    weights = np.arange(1, period + 1)
    def lwma(window):
        return np.sum(window * weights) / np.sum(weights)
    return series.rolling(window=period).apply(lwma, raw=True)

def get_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    
    # Bollinger Bands (Period 20, Deviation 2, Apply to Close)
    df['bb_mid'] = df['close'].rolling(BB_PERIOD).mean()
    bb_std = df['close'].rolling(BB_PERIOD).std()
    df['bb_upper'] = df['bb_mid'] + (bb_std * BB_STD)
    df['bb_lower'] = df['bb_mid'] - (bb_std * BB_STD)
    
    # LWMA 5/10 High & Low (Linear Weighted, Apply to High/Low)
    df['ma5_high'] = calculate_lwma(df['high'], 5)
    df['ma10_high'] = calculate_lwma(df['high'], 10)
    df['ma5_low'] = calculate_lwma(df['low'], 5)
    df['ma10_low'] = calculate_lwma(df['low'], 10)
    
    # EMA 50 (Exponential, Apply to Close)
    df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()
    
    return df.dropna()

# ==========================================
# BBMA STATE MACHINE (Oma Ally Rules)
# ==========================================
class BBMACycleTracker:
    """
    BBMA Cycle: Extreme -> MHV -> CSA -> Re-Entry
    Strict Oma Ally rules from PDF
    """
    
    def __init__(self):
        self.state = BBMAState.NONE
        self.extreme_price = None
        self.mhv_zone_high = None
        self.mhv_zone_low = None
        self.csa_confirmed = False
        self.csa_type = None
    
    def reset(self):
        self.state = BBMAState.NONE
        self.extreme_price = None
        self.mhv_zone_high = None
        self.mhv_zone_low = None
        self.csa_confirmed = False
        self.csa_type = None
    
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
        
        # ==========================================
        # 1. EXTREME (Signal - bukan setup)
        # ==========================================
        # Extreme Buy: MA5/10 Low keluar BB Lower + Reverse Candle
        extreme_buy = (
            (ma5_low < bb_lower or ma10_low < bb_lower) and
            is_bullish and prev_bearish
        )
        
        # Extreme Sell: MA5/10 High keluar BB Upper + Reverse Candle
        extreme_sell = (
            (ma5_high > bb_upper or ma10_high > bb_upper) and
            is_bearish and prev_bullish
        )
        
        if extreme_buy:
            self.state = BBMAState.EXTREME_BUY
            self.extreme_price = low
            self.mhv_zone_high = None
            self.mhv_zone_low = None
            self.csa_confirmed = False
            self.csa_type = None
            return None
        
        if extreme_sell:
            self.state = BBMAState.EXTREME_SELL
            self.extreme_price = high
            self.mhv_zone_high = None
            self.mhv_zone_low = None
            self.csa_confirmed = False
            self.csa_type = None
            return None
        
        # ==========================================
        # 2. MHV (Market Hilang Volume)
        # ==========================================
        # MHV valid: Body candle tak close luar BB, shadow tak kira
        if self.state == BBMAState.EXTREME_BUY:
            mhv_valid = (close >= bb_lower) and is_bearish and prev_bullish
            if mhv_valid:
                self.state = BBMAState.MHV_BUY
                self.mhv_zone_high = high
                return None
            if close < bb_lower:
                self.reset()
                return None
        
        if self.state == BBMAState.EXTREME_SELL:
            mhv_valid = (close <= bb_upper) and is_bullish and prev_bearish
            if mhv_valid:
                self.state = BBMAState.MHV_SELL
                self.mhv_zone_low = low
                return None
            if close > bb_upper:
                self.reset()
                return None
        
        # ==========================================
        # 3. CSA (Candlestick Direction / Arah)
        # ==========================================
        # CSA = Signal arah, bukan setup entry
        if self.state == BBMAState.MHV_BUY:
            # Early CSA: Close atas MA5/10 Low sahaja
            # Strong CSA: Close atas MA5/10 Low + atas Mid BB
            csa_early = close > ma5_low and close > ma10_low
            csa_strong = csa_early and close > bb_mid
            
            if csa_strong:
                self.state = BBMAState.CSA_BUY
                self.csa_confirmed = True
                self.csa_type = 'strong'
                return None
            elif csa_early:
                self.state = BBMAState.CSA_BUY
                self.csa_confirmed = True
                self.csa_type = 'early'
                return None
        
        if self.state == BBMAState.MHV_SELL:
            # Early CSA: Close bawah MA5/10 High sahaja
            # Strong CSA: Close bawah MA5/10 High + bawah Mid BB
            csa_early = close < ma5_high and close < ma10_high
            csa_strong = csa_early and close < bb_mid
            
            if csa_strong:
                self.state = BBMAState.CSA_SELL
                self.csa_confirmed = True
                self.csa_type = 'strong'
                return None
            elif csa_early:
                self.state = BBMAState.CSA_SELL
                self.csa_confirmed = True
                self.csa_type = 'early'
                return None
        
        # ==========================================
        # 4. RE-ENTRY (Setup Entry)
        # ==========================================
        # Re-Entry: Price retrace ke zone MA5/10, close tak melepasi MA lawan & Mid BB
        if self.state == BBMAState.CSA_BUY and self.csa_confirmed:
            # Re-Entry Buy: Price retrace ke MA5/10 Low zone
            in_zone = (low <= ma5_low * 1.002) or (low <= ma10_low * 1.002)
            not_beyond = close <= ma5_high and close <= ma10_high and close <= bb_mid
            valid_reentry = in_zone and not_beyond and is_bullish and prev_bearish
            
            if valid_reentry:
                self.state = BBMAState.REENTRY_BUY
                return {
                    'type': 'BUY',
                    'current_price': close,
                    'ma5_low': ma5_low,
                    'ma10_low': ma10_low,
                    'bb_lower': bb_lower,
                    'bb_upper': bb_upper,
                    'ma5_high': ma5_high,
                    'ma10_high': ma10_high,
                    'bb_mid': bb_mid,
                    'csa_type': self.csa_type
                }
        
        if self.state == BBMAState.CSA_SELL and self.csa_confirmed:
            # Re-Entry Sell: Price retrace ke MA5/10 High zone
            in_zone = (high >= ma5_high * 0.998) or (high >= ma10_high * 0.998)
            not_beyond = close >= ma5_low and close >= ma10_low and close >= bb_mid
            valid_reentry = in_zone and not_beyond and is_bearish and prev_bullish
            
            if valid_reentry:
                self.state = BBMAState.REENTRY_SELL
                return {
                    'type': 'SELL',
                    'current_price': close,
                    'ma5_high': ma5_high,
                    'ma10_high': ma10_high,
                    'bb_upper': bb_upper,
                    'bb_lower': bb_lower,
                    'ma5_low': ma5_low,
                    'ma10_low': ma10_low,
                    'bb_mid': bb_mid,
                    'csa_type': self.csa_type
                }
        
        return None

# ==========================================
# LEVEL CALCULATION (2 ENTRY LEVELS)
# ==========================================
def calculate_levels_buy(setup: Dict, latest_price: float) -> Dict:
    """
    BUY Levels:
    - Conservative: MA10 Low (lebih selamat)
    - Aggressive: MA5 Low (lebih awal, risiko tinggi)
    - SL: Below BB Lower
    - TP1: MA5/10 High | TP2: BB Upper | TP3: BB Upper + 1%
    """
    entry_conservative = setup['ma10_low']
    entry_aggressive = setup['ma5_low']
    
    # SL: Below BB Lower (ikut Oma Ally - bukan percentage)
    sl = setup['bb_lower'] - (latest_price * 0.001)
    
    # TP levels
    tp1 = max(setup['ma5_high'], setup['ma10_high'])
    tp2 = setup['bb_upper']
    tp3 = setup['bb_upper'] + (latest_price * 0.01)
    
    return {
        'conservative': {'entry': entry_conservative, 'sl': sl},
        'aggressive': {'entry': entry_aggressive, 'sl': sl},
        'tp1': tp1,
        'tp2': tp2,
        'tp3': tp3,
        'current_price': latest_price,
        'csa_type': setup.get('csa_type', 'early')
    }

def calculate_levels_sell(setup: Dict, latest_price: float) -> Dict:
    """
    SELL Levels:
    - Conservative: MA10 High (lebih selamat)
    - Aggressive: MA5 High (lebih awal, risiko tinggi)
    - SL: Above BB Upper
    - TP1: MA5/10 Low | TP2: BB Lower | TP3: BB Lower - 1%
    """
    entry_conservative = setup['ma10_high']
    entry_aggressive = setup['ma5_high']
    
    # SL: Above BB Upper (ikut Oma Ally - bukan percentage)
    sl = setup['bb_upper'] + (latest_price * 0.001)
    
    # TP levels
    tp1 = min(setup['ma5_low'], setup['ma10_low'])
    tp2 = setup['bb_lower']
    tp3 = setup['bb_lower'] - (latest_price * 0.01)
    
    return {
        'conservative': {'entry': entry_conservative, 'sl': sl},
        'aggressive': {'entry': entry_aggressive, 'sl': sl},
        'tp1': tp1,
        'tp2': tp2,
        'tp3': tp3,
        'current_price': latest_price,
        'csa_type': setup.get('csa_type', 'early')
    }

# ==========================================
# MAIN SCANNER
# ==========================================
def scan_xauusd():
    tracker = BBMACycleTracker()
    
    # 1. Get real-time price first
    rt = fetch_realtime_price()
    if not rt:
        print("❌ Cannot fetch real-time price. Aborting scan.")
        return
    
    current_price = rt['price']
    
    for style, tfs in STYLES.items():
        print(f"\n{'='*50}")
        print(f"🔍 Scanning XAU/USD ({style})")
        print(f"{'='*50}")
        
        try:
            # 2. Fetch historical data for big TF (trend direction)
            df_big = fetch_yahoo_historical(
                yahoo_interval=tfs['yahoo_big'],
                period=tfs['period']
            )
            
            if df_big.empty or len(df_big) < 60:
                print(f"❌ Big TF ({tfs['yahoo_big']}) data insufficient: {len(df_big)} candles")
                continue
            
            df_big = get_indicators(df_big)
            
            # 3. Fetch historical data for small TF (entry signals)
            df_small = fetch_yahoo_historical(
                yahoo_interval=tfs['yahoo_small'],
                period=tfs['period']
            )
            
            if df_small.empty or len(df_small) < 60:
                print(f"❌ Small TF ({tfs['yahoo_small']}) data insufficient: {len(df_small)} candles")
                continue
            
            df_small = get_indicators(df_small)
            
            # ==========================================
            # TREND DETECTION (EMA50 vs Mid BB)
            # ==========================================
            last_big = df_big.iloc[-1]
            
            # EMA50 below Mid BB = Uptrend (Bullish)
            # EMA50 above Mid BB = Downtrend (Bearish)
            uptrend = last_big['ema50'] < last_big['bb_mid']
            downtrend = last_big['ema50'] > last_big['bb_mid']
            
            trend_str = 'UP' if uptrend else 'DOWN' if downtrend else 'SIDEWAYS'
            print(f"📈 Trend: {trend_str}")
            print(f"   EMA50: {last_big['ema50']:.2f} | Mid BB: {last_big['bb_mid']:.2f}")
            
            if not uptrend and not downtrend:
                print(f"ℹ️ No clear trend - skipping")
                continue
            
            print(f"💰 Current Price: {current_price:.2f}")
            
            # ==========================================
            # SCAN FOR BBMA SETUP
            # ==========================================
            tracker.reset()
            setup_found = None
            
            for i in range(1, len(df_small)):
                result = tracker.update(df_small.iloc[i], df_small.iloc[i-1])
                if result is not None:
                    setup_found = result
                    setup_candle = df_small.iloc[i]
                    print(f"\n🎯 Setup found at candle {i}: {setup_candle.name}")
                    break
            
            if setup_found is None:
                print(f"ℹ️ No valid BBMA setup (cycle incomplete)")
                continue
            
            # ==========================================
            # VALIDATE TREND ALIGNMENT
            # ==========================================
            if setup_found['type'] == 'BUY' and not uptrend:
                print(f"ℹ️ BUY setup ignored (big TF downtrend)")
                continue
            if setup_found['type'] == 'SELL' and not downtrend:
                print(f"ℹ️ SELL setup ignored (big TF uptrend)")
                continue
            
            # ==========================================
            # VALIDATE PRICE REASONABLENESS
            # ==========================================
            entry_ref = setup_found['ma5_low'] if setup_found['type'] == 'BUY' else setup_found['ma5_high']
            price_diff_pct = abs(entry_ref - current_price) / current_price * 100
            
            print(f"📊 Entry reference: {entry_ref:.2f}")
            print(f"📊 Price diff from current: {price_diff_pct:.2f}%")
            
            if price_diff_pct > 2.0:
                print(f"⚠️ WARNING: Entry too far from current price ({price_diff_pct:.1f}%)")
                print(f"❌ Skipping alert - verify data source!")
                continue
            
            # ==========================================
            # CALCULATE LEVELS
            # ==========================================
            if setup_found['type'] == 'BUY':
                levels = calculate_levels_buy(setup_found, current_price)
                emoji = "🟢"
                direction = "BUY"
                pattern = "Bullish Re-Entry (After CSA)"
                zone = "MA5/10 Low zone"
            else:
                levels = calculate_levels_sell(setup_found, current_price)
                emoji = "🔴"
                direction = "SELL"
                pattern = "Bearish Re-Entry (After CSA)"
                zone = "MA5/10 High zone"
            
            # ==========================================
            # BUILD TELEGRAM MESSAGE
            # ==========================================
            csa_label = "STRONG" if levels['csa_type'] == 'strong' else "EARLY"
            source_label = rt.get('source', 'Unknown')
            
            msg = f"""
{emoji} <b>BBMA {direction} SETUP DETECTED</b>

📊 Pair: XAU/USD (Gold)
⏱️ Style: {style}
📈 Pattern: {pattern}
✅ Cycle: Extreme → MHV → CSA ({csa_label}) → Re-Entry CONFIRMED
🎯 Zone: {zone}
💰 <b>Current Price: {levels['current_price']:.2f}</b> (Source: {source_label})

━━━━━━━━━━━━━━━━━━━━

🟢 <b>CONSERVATIVE ENTRY</b>
Paling selamat, tunggu confirmation penuh
• Entry: {levels['conservative']['entry']:.2f}
• SL: {levels['conservative']['sl']:.2f}
• TP1: {levels['tp1']:.2f} | TP2: {levels['tp2']:.2f} | TP3: {levels['tp3']:.2f}

🔴 <b>AGGRESSIVE ENTRY</b>
Entry awal, harga terbaik, risiko tinggi
• Entry: {levels['aggressive']['entry']:.2f}
• SL: {levels['aggressive']['sl']:.2f}
• TP1: {levels['tp1']:.2f} | TP2: {levels['tp2']:.2f} | TP3: {levels['tp3']:.2f}

━━━━━━━━━━━━━━━━━━━━

⚠️ <i>Pilih 1 level je ikut risk appetite kau!</i>
⚠️ <i>Verify: Harga semasa ~{levels['current_price']:.2f}</i>
⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}
            """
            
            send_telegram(msg)
            print(f"\n🚨 {direction} SETUP SENT for XAU/USD ({style})!")
            
        except Exception as e:
            print(f"❌ Error scanning XAU/USD {style}: {e}")
            import traceback
            traceback.print_exc()

# ==========================================
# MAIN
# ==========================================
def main():
    print(f"=== BBMA XAU/USD Scanner ===")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"GoldAPI Key: {'✅ Set' if GOLDAPI_KEY else '❌ Not set (using Yahoo fallback)'}")
    print(f"Telegram: {'✅ Ready' if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID else '❌ Not configured'}")
    print("=" * 50)
    
    try:
        scan_xauusd()
    except Exception as e:
        print(f"❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
    
    print(f"\n=== Scan Complete: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")

if __name__ == "__main__":
    main()
