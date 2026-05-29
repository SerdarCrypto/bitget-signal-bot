# -*- coding: utf-8 -*-
"""
======================================================================
  BITGET FUTURES MULTI-TIMEFRAME SIGNAL BOT
======================================================================
  Strateji:
    1) Makro trend filtresi: 4H + 1H + 15M  (EMA200 + EMA9/21)
       -> Hepsi AYNI yon (LONG ya da SHORT) ise devam.
    2) 5M Sniper: 7 indikatorluk scoring matrix (MIN_SCORE esigi)
    3) EMA 9/21 onaylanmis cross (gercek cross, sadece pozisyon degil)
    4) Sadece KAPALI mum analiz edilir -> iloc[-2]
    5) Dinamik SL (SuperTrend) + R:R bazli TP1 (1.5x) / TP2 (3x)

  Kutuphaneler:
    pip install ccxt pandas pandas_ta pyTelegramBotAPI numpy
    (NOT: 'ta' yerine 'pandas_ta' kullaniyoruz cunku SuperTrend ve
     rolling VWAP icin daha guvenilir.)
======================================================================
"""

import os
import sys
import time
import logging
import traceback
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pandas_ta as pta
import ccxt
import telebot

# =====================================================================
#  >>>>>>>>>>  GIZLI BILGILER (ENVIRONMENT VARIABLES)  <<<<<<<<<<
#  Bu degerler KODA YAZILMAZ. Railway panelinden "Variables" kismina
#  asagidaki isimlerle girilir:
#    BITGET_API_KEY, BITGET_SECRET_KEY, BITGET_PASSPHRASE,
#    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
# =====================================================================
API_KEY        = os.environ.get("BITGET_API_KEY", "")
SECRET_KEY     = os.environ.get("BITGET_SECRET_KEY", "")
PASSPHRASE     = os.environ.get("BITGET_PASSPHRASE", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID        = os.environ.get("TELEGRAM_CHAT_ID", "")

# Baslangicta eksik degisken kontrolu
_required = {
    "BITGET_API_KEY": API_KEY,
    "BITGET_SECRET_KEY": SECRET_KEY,
    "BITGET_PASSPHRASE": PASSPHRASE,
    "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
    "TELEGRAM_CHAT_ID": CHAT_ID,
}
_missing = [k for k, v in _required.items() if not v]
if _missing:
    print("HATA: Su environment variable'lar eksik: " + ", ".join(_missing))
    print("Railway -> Variables kismindan bunlari ekleyin.")
    sys.exit(1)

# =====================================================================
#  STRATEJI PARAMETRELERI (gerekirse degistir)
# =====================================================================
MIN_SCORE        = 6      # 7 indikatorden kac tanesi onaylamali (7 = en kati)
COOLDOWN_HOURS   = 4      # Ayni coin icin tekrar sinyal vermeden once bekleme
MIN_QUOTE_VOLUME = 0      # Min 24s hacim filtresi (USDT). 0 = kapali. Orn: 5_000_000
LOOP_SLEEP       = 60     # Ana dongu bekleme (saniye) - PythonAnywhere icin
API_SLEEP        = 0.35   # Her API cagrisi arasi bekleme (rate-limit korumasi)
CANDLE_LIMIT     = 250    # EMA200 icin yeterli mum sayisi

# R:R carpanlari
TP1_MULT = 1.5
TP2_MULT = 3.0

# Indikator periyotlari
EMA_FAST, EMA_MID, EMA_SLOW = 9, 21, 200
RSI_LEN          = 9
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
VWAP_LEN         = 20
VOL_SMA_LEN      = 20
ST_LEN, ST_MULT  = 10, 2.0

# Takip edilecek coinler (Bitget USDT-M Swap)
COINS = [
    "STX", "RENDER", "RUNE", "INJ", "AKT", "TON", "AR", "TIA", "DOGE", "SUI",
    "AAVE", "KAS", "TAO", "MON", "BERA", "HYPE", "PENGU", "VIRTUAL", "ARB", "PENDLE",
]

TIMEFRAMES_MACRO = ["4h", "1h", "15m"]
TIMEFRAME_ENTRY  = "5m"

# =====================================================================
#  LOGGING
# =====================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("signal_bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("bot")


# =====================================================================
#  BOT SINIFI
# =====================================================================
class BitgetSignalBot:

    def __init__(self):
        self.exchange = ccxt.bitget({
            "apiKey": API_KEY,
            "secret": SECRET_KEY,
            "password": PASSPHRASE,
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        })
        self.tg = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

        self.symbols = {}          # ticker -> "STX/USDT:USDT"
        self.last_signal = {}      # symbol -> datetime (cooldown takibi)
        self.markets = {}

    # ----------------------------------------------------------------
    #  KURULUM: Marketleri yukle, coin formatlarini dogrula
    # ----------------------------------------------------------------
    def setup(self):
        log.info("Marketler yukleniyor...")
        self.markets = self.exchange.load_markets()

        valid, skipped = [], []
        for coin in COINS:
            sym = f"{coin}/USDT:USDT"
            if sym in self.markets and self.markets[sym].get("swap"):
                self.symbols[coin] = sym
                valid.append(coin)
            else:
                skipped.append(coin)

        log.info("Gecerli coinler (%d): %s", len(valid), ", ".join(valid))
        if skipped:
            log.warning("Atlanan coinler (Bitget'te yok): %s", ", ".join(skipped))

        # Hacim filtresi (opsiyonel)
        if MIN_QUOTE_VOLUME > 0:
            self._apply_volume_filter()

        self._send_startup_message(valid, skipped)

    def _apply_volume_filter(self):
        try:
            tickers = self.exchange.fetch_tickers(list(self.symbols.values()))
            filtered = {}
            for coin, sym in self.symbols.items():
                t = tickers.get(sym, {})
                qv = t.get("quoteVolume") or 0
                if qv >= MIN_QUOTE_VOLUME:
                    filtered[coin] = sym
                else:
                    log.info("Dusuk hacim, atlandi: %s (%.0f USDT)", coin, qv)
            self.symbols = filtered
        except Exception as e:
            log.warning("Hacim filtresi basarisiz, devam ediliyor: %s", e)

    # ----------------------------------------------------------------
    #  VERI CEKME
    # ----------------------------------------------------------------
    def fetch_df(self, symbol, timeframe):
        """OHLCV cek -> DataFrame. Hata olursa None."""
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=CANDLE_LIMIT)
            time.sleep(API_SLEEP)
            if not ohlcv or len(ohlcv) < EMA_SLOW + 5:
                return None
            df = pd.DataFrame(
                ohlcv, columns=["ts", "open", "high", "low", "close", "volume"]
            )
            df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
            for c in ["open", "high", "low", "close", "volume"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df.dropna(inplace=True)
            df.reset_index(drop=True, inplace=True)
            return df
        except ccxt.RateLimitExceeded:
            log.warning("Rate limit! 10sn bekleniyor...")
            time.sleep(10)
            return None
        except Exception as e:
            log.error("Veri cekme hatasi %s %s: %s", symbol, timeframe, e)
            return None

    # ----------------------------------------------------------------
    #  INDIKATOR HESAPLAMA
    # ----------------------------------------------------------------
    def add_indicators(self, df):
        """Tum indikatorleri DataFrame'e ekler."""
        df["ema9"]   = pta.ema(df["close"], length=EMA_FAST)
        df["ema21"]  = pta.ema(df["close"], length=EMA_MID)
        df["ema200"] = pta.ema(df["close"], length=EMA_SLOW)

        df["rsi"] = pta.rsi(df["close"], length=RSI_LEN)

        macd = pta.macd(df["close"], fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)
        if macd is not None and not macd.empty:
            df["macd"]   = macd.iloc[:, 0]   # MACD line
            df["macd_s"] = macd.iloc[:, 2]   # Signal line
        else:
            df["macd"] = df["macd_s"] = np.nan

        # Rolling VWAP (manuel - tam dogru hesap)
        tp = (df["high"] + df["low"] + df["close"]) / 3.0
        pv = tp * df["volume"]
        df["vwap"] = (
            pv.rolling(VWAP_LEN).sum() / df["volume"].rolling(VWAP_LEN).sum()
        )

        df["vol_sma"] = df["volume"].rolling(VOL_SMA_LEN).mean()

        # SuperTrend
        st = pta.supertrend(df["high"], df["low"], df["close"],
                            length=ST_LEN, multiplier=ST_MULT)
        if st is not None and not st.empty:
            df["st_line"] = st.iloc[:, 0]   # SuperTrend deger
            df["st_dir"]  = st.iloc[:, 1]   # Yon: 1 = LONG, -1 = SHORT
        else:
            df["st_line"] = np.nan
            df["st_dir"]  = np.nan

        return df

    # ----------------------------------------------------------------
    #  MAKRO TREND (tek timeframe icin yon dondurur)
    # ----------------------------------------------------------------
    def macro_direction(self, df):
        """
        Kapali muma gore makro yon dondurur: 'LONG', 'SHORT' veya None.
        Kosul: fiyat EMA200 ustunde + EMA9 > EMA21  -> LONG (tersi SHORT)
        """
        if df is None:
            return None
        c = df.iloc[-2]  # KAPALI mum
        if pd.isna(c["ema200"]) or pd.isna(c["ema9"]) or pd.isna(c["ema21"]):
            return None

        if c["close"] > c["ema200"] and c["ema9"] > c["ema21"]:
            return "LONG"
        if c["close"] < c["ema200"] and c["ema9"] < c["ema21"]:
            return "SHORT"
        return None

    # ----------------------------------------------------------------
    #  EMA CROSS ONAYI (gercek cross)
    # ----------------------------------------------------------------
    @staticmethod
    def ema_cross(df, direction):
        """
        Onceki mumda ters, son kapali mumda dogru pozisyon -> gercek cross.
        prev = iloc[-3], last = iloc[-2]
        """
        prev = df.iloc[-3]
        last = df.iloc[-2]
        if any(pd.isna(x) for x in
               [prev["ema9"], prev["ema21"], last["ema9"], last["ema21"]]):
            return False

        if direction == "LONG":
            return prev["ema9"] <= prev["ema21"] and last["ema9"] > last["ema21"]
        else:  # SHORT
            return prev["ema9"] >= prev["ema21"] and last["ema9"] < last["ema21"]

    # ----------------------------------------------------------------
    #  5M SCORING MATRIX (7 indikator)
    # ----------------------------------------------------------------
    def score_5m(self, df, direction):
        """
        7 kosulu kontrol eder, (score, detay_dict) dondurur.
        Son KAPALI mum (iloc[-2]) kullanilir.
        """
        c = df.iloc[-2]
        score = 0
        d = {}

        long_ = direction == "LONG"

        # 1) EMA9 vs EMA21
        ok = (c["ema9"] > c["ema21"]) if long_ else (c["ema9"] < c["ema21"])
        d["EMA9/21"] = ok; score += ok

        # 2) Fiyat vs EMA200
        ok = (c["close"] > c["ema200"]) if long_ else (c["close"] < c["ema200"])
        d["EMA200"] = ok; score += ok

        # 3) MACD line vs signal
        ok = (c["macd"] > c["macd_s"]) if long_ else (c["macd"] < c["macd_s"])
        d["MACD"] = ok; score += ok

        # 4) RSI yonu (50 referans)
        ok = (c["rsi"] > 50) if long_ else (c["rsi"] < 50)
        d["RSI"] = ok; score += ok

        # 5) Fiyat vs VWAP
        ok = (c["close"] > c["vwap"]) if long_ else (c["close"] < c["vwap"])
        d["VWAP"] = ok; score += ok

        # 6) Hacim > Hacim SMA (momentum teyidi - yon bagimsiz)
        ok = bool(c["volume"] > c["vol_sma"])
        d["Volume"] = ok; score += ok

        # 7) SuperTrend yonu
        ok = (c["st_dir"] == 1) if long_ else (c["st_dir"] == -1)
        d["SuperTrend"] = bool(ok); score += bool(ok)

        return int(score), d

    # ----------------------------------------------------------------
    #  SL / TP HESABI
    # ----------------------------------------------------------------
    def calc_levels(self, df, direction):
        """
        Giris = son kapali mum close.
        SL = SuperTrend cizgisi (dinamik destek/direnc).
        TP1/TP2 = R:R bazli.
        """
        c = df.iloc[-2]
        entry = float(c["close"])
        sl    = float(c["st_line"])

        risk = abs(entry - sl)
        if risk <= 0:
            return None

        if direction == "LONG":
            tp1 = entry + risk * TP1_MULT
            tp2 = entry + risk * TP2_MULT
        else:
            tp1 = entry - risk * TP1_MULT
            tp2 = entry - risk * TP2_MULT

        return {
            "entry": entry, "sl": sl,
            "tp1": tp1, "tp2": tp2,
            "risk_pct": (risk / entry) * 100,
        }

    # ----------------------------------------------------------------
    #  COOLDOWN
    # ----------------------------------------------------------------
    def in_cooldown(self, symbol):
        last = self.last_signal.get(symbol)
        if last is None:
            return False
        elapsed = (datetime.now(timezone.utc) - last).total_seconds() / 3600
        return elapsed < COOLDOWN_HOURS

    # ----------------------------------------------------------------
    #  TELEGRAM
    # ----------------------------------------------------------------
    def _fmt(self, val):
        """Akilli fiyat formatlama (kucuk fiyatli coinler icin)."""
        if val >= 100:   return f"{val:,.2f}"
        if val >= 1:     return f"{val:.4f}"
        if val >= 0.01:  return f"{val:.5f}"
        return f"{val:.7f}"

    def send_signal(self, coin, direction, lv, score, detail):
        emoji = "🟢📈" if direction == "LONG" else "🔴📉"
        side  = "LONG" if direction == "LONG" else "SHORT"

        checks = "\n".join(
            f"  {'✅' if v else '❌'} {k}" for k, v in detail.items()
        )

        msg = (
            f"{emoji} <b>{side} SINYALI</b> {emoji}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🪙 <b>Coin:</b> <code>{coin}/USDT</code>\n"
            f"⏱ <b>Zaman dilimi:</b> 5M (4H/1H/15M onayli)\n"
            f"🎯 <b>Skor:</b> {score}/7\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📍 <b>Giris:</b> <code>{self._fmt(lv['entry'])}</code>\n"
            f"🛑 <b>Stop Loss:</b> <code>{self._fmt(lv['sl'])}</code>  "
            f"(<i>%{lv['risk_pct']:.2f} risk</i>)\n"
            f"🥇 <b>TP1 (1.5R):</b> <code>{self._fmt(lv['tp1'])}</code>\n"
            f"🥈 <b>TP2 (3.0R):</b> <code>{self._fmt(lv['tp2'])}</code>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"<b>Indikator Matrisi:</b>\n{checks}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"⚠️ <i>Yatirim tavsiyesi degildir. Risk yonetimi senin sorumlulugunda.</i>\n"
            f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )
        try:
            self.tg.send_message(CHAT_ID, msg)
            log.info("SINYAL GONDERILDI: %s %s (%d/7)", coin, side, score)
        except Exception as e:
            log.error("Telegram gonderim hatasi: %s", e)

    def _send_startup_message(self, valid, skipped):
        try:
            txt = (
                "🤖 <b>Bitget Sinyal Botu BASLADI</b>\n"
                f"📊 Takip: {len(valid)} coin\n"
                f"🎯 Min skor: {MIN_SCORE}/7\n"
                f"⏱ Tarama araligi: {LOOP_SLEEP}sn\n"
            )
            if skipped:
                txt += f"⚠️ Atlanan: {', '.join(skipped)}\n"
            self.tg.send_message(CHAT_ID, txt)
        except Exception as e:
            log.warning("Baslangic mesaji gonderilemedi: %s", e)

    # ----------------------------------------------------------------
    #  TEK COIN ANALIZI
    # ----------------------------------------------------------------
    def analyze_coin(self, coin, symbol):
        if self.in_cooldown(symbol):
            return

        # 1) MAKRO TREND - uc timeframe ayni yonde mi?
        dirs = []
        for tf in TIMEFRAMES_MACRO:
            df = self.fetch_df(symbol, tf)
            if df is None:
                return
            df = self.add_indicators(df)
            dirs.append(self.macro_direction(df))

        if None in dirs or len(set(dirs)) != 1:
            return  # uc timeframe ayni yonde degil -> ele

        macro = dirs[0]

        # 2) 5M GIRIS ANALIZI
        df5 = self.fetch_df(symbol, TIMEFRAME_ENTRY)
        if df5 is None:
            return
        df5 = self.add_indicators(df5)

        # NaN guard (son kapali mum gecerli mi)
        c = df5.iloc[-2]
        need = ["ema9", "ema21", "ema200", "macd", "macd_s",
                "rsi", "vwap", "vol_sma", "st_line", "st_dir"]
        if any(pd.isna(c[x]) for x in need):
            return

        # 3) SCORING
        score, detail = self.score_5m(df5, macro)
        if score < MIN_SCORE:
            return

        # 4) GERCEK EMA CROSS ONAYI
        if not self.ema_cross(df5, macro):
            return

        # 5) SL / TP
        lv = self.calc_levels(df5, macro)
        if lv is None:
            return

        # 6) SINYAL
        self.send_signal(coin, macro, lv, score, detail)
        self.last_signal[symbol] = datetime.now(timezone.utc)

    # ----------------------------------------------------------------
    #  ANA DONGU
    # ----------------------------------------------------------------
    def run(self):
        self.setup()
        log.info("Tarama dongusu basladi.")
        while True:
            start = time.time()
            for coin, symbol in self.symbols.items():
                try:
                    self.analyze_coin(coin, symbol)
                except Exception as e:
                    log.error("Analiz hatasi %s: %s\n%s",
                            coin, e, traceback.format_exc())
            elapsed = time.time() - start
            wait = max(LOOP_SLEEP - elapsed, 5)
            log.info("Tur bitti (%.1fsn). %.1fsn bekleniyor...", elapsed, wait)
            time.sleep(wait)


# =====================================================================
#  GIRIS NOKTASI
# =====================================================================
if __name__ == "__main__":
    bot = BitgetSignalBot()
    while True:
        try:
            bot.run()
        except KeyboardInterrupt:
            log.info("Bot durduruldu (kullanici).")
            break
        except Exception as e:
            log.critical("KRITIK HATA, 60sn sonra yeniden baslatiliyor: %s\n%s",
                        e, traceback.format_exc())
            time.sleep(60)
