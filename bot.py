import os
import asyncio
import requests
import pandas as pd
import ta
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# --- کنفیگریشن ---
BOT_TOKEN = "8693213483:AAGd0ueLdox6tBDG9mbAr2HnaSgJLdFWh_4"
CHAT_ID = "7548033382"

# یوزر کی ترجیحات
user_ema = 50
user_timeframe = '1h'
is_scanning = False

def get_usdt_pairs():
    try:
        url = "https://api.binance.com/api/v3/exchangeInfo"
        response = requests.get(url).json()
        return [s['symbol'] for s in response['symbols'] if s['symbol'].endswith('USDT') and s['status'] == 'TRADING'][:50]
    except Exception:
        return []

def check_crossover(symbol, timeframe, ema_period):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={timeframe}&limit={ema_period + 5}"
        data = requests.get(url).json()
        if not data or len(data) < (ema_period + 2):
            return False
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'qav', 'num_trades', 'taker_base', 'taker_quote', 'ignore'])
        df['close'] = df['close'].astype(float)
        df['ema'] = ta.trend.ema_indicator(df['close'], window=int(ema_period))
        if df['close'].iloc[-3] < df['ema'].iloc[-3] and df['close'].iloc[-2] > df['ema'].iloc[-2]:
            return True
    except Exception:
        pass
    return False

async def start_scanning(bot):
    global is_scanning, user_ema, user_timeframe
    pairs = get_usdt_pairs()
    await bot.send_message(chat_id=CHAT_ID, text=f"🔍 اسکیننگ شروع ہو گئی ہے...\nEMA: {user_ema}\nٹائم فریم: {user_timeframe}")
    
    while is_scanning:
        for symbol in pairs:
            if not is_scanning:
                break
            if check_crossover(symbol, user_timeframe, user_ema):
                await bot.send_message(chat_id=CHAT_ID, text=f"🚀 **EMA CROSSOVER!** 🚀\n\nCoin: #{symbol}\nEMA: {user_ema}", parse_mode="Markdown")
            await asyncio.sleep(1)
        if is_scanning:
            await asyncio.sleep(300)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply_keyboard = [['20', '50', '200']]
    await update.message.reply_text("کرپٹو بوٹ میں خوش آمدید۔ اپنا **EMA** منتخب کریں:", reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True, resize_keyboard=True))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global user_ema, user_timeframe, is_scanning
    text = update.message.text
    if text in ['20', '50', '200']:
        user_ema = int(text)
        await update.message.reply_text(f"آپ نے {user_ema} EMA منتخب کیا۔ اب **Timeframe** منتخب کریں:", reply_markup=ReplyKeyboardMarkup([['1h', '4h', '1d']], one_time_keyboard=True, resize_keyboard=True))
    elif text in ['1h', '4h', '1d']:
        user_timeframe = text
        is_scanning = True
        await update.message.reply_text("بوٹ اب اسکیننگ شروع کر رہا ہے...", reply_markup=ReplyKeyboardRemove())
        asyncio.create_task(start_scanning(context.bot))

def main():
    # ایپلیکیشن بلڈ کریں
    application = Application.builder().token(BOT_TOKEN).build()
    
    # ہینڈلرز شامل کریں
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # رینڈر کی پورٹ حاصل کریں
    port = int(os.environ.get("PORT", 8080))
    
    # یہ آفیشل فنکشن سرور بھی چلائے گا اور ویب ہک بھی خود سیٹ کرے گا
    application.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=BOT_TOKEN,
        webhook_url=f"https://trading-bot-1-hs5g.onrender.com/{BOT_TOKEN}"
    )

if __name__ == '__main__':
    main()
