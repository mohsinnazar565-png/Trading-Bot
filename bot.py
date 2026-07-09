import os
import time
import requests
import pandas as pd
import ta
import asyncio
from flask import Flask
from threading import Thread
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# --- کنفیگریشن ---
BOT_TOKEN = "8693213483:AAGd0ueLdox6tBDG9mbAr2HnaSgJLdFWh_4"  # یہاں صرف ٹوکن آئے گا، لائبریری 'bot' خود لگا لیتی ہے
CHAT_ID = "7548033382"                                       # آپ کی چیٹ آئی ڈی

# رینڈر (Render) کے لیے ویب سرور سیٹ اپ
app = Flask('')

@app.route('/')
def home():
    return "Bot is running!"

def run_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# یوزر کی ترجیحات
user_ema = 50
user_timeframe = '1h'
is_scanning = False

def get_usdt_pairs():
    try:
        url = "https://api.binance.com/api/v3/exchangeInfo"
        response = requests.get(url).json()
        pairs = [symbols['symbol'] for symbols in response['symbols'] if symbols['symbol'].endswith('USDT') and symbols['status'] == 'TRADING']
        return pairs[:50]  # رینڈر کے فری اکاؤنٹ پر لوڈ کم رکھنے کے لیے ٹاپ 50 پیئرز (بہترین پرفارمنس کے لیے)
    except Exception as e:
        print(f"Error fetching pairs: {e}")
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
        
        prev_close = df['close'].iloc[-3]
        prev_ema = df['ema'].iloc[-3]
        
        current_close = df['close'].iloc[-2]
        current_ema = df['ema'].iloc[-2]
        
        if prev_close < prev_ema and current_close > current_ema:
            return True
    except Exception:
        pass
    return False

# اسکیننگ کا نیا اور محفوظ طریقہ جو بوٹ کو کریش نہیں ہونے دے گا
async def start_scanning(bot):
    global is_scanning, user_ema, user_timeframe
    pairs = get_usdt_pairs()
    await bot.send_message(chat_id=CHAT_ID, text=f"🔍 اسکیننگ شروع ہو گئی ہے...\nEMA: {user_ema}\nٹائم فریم: {user_timeframe}\nکل کوائنز: {len(pairs)}")
    
    while is_scanning:
        for symbol in pairs:
            if not is_scanning:
                break
            if check_crossover(symbol, user_timeframe, user_ema):
                message = f"🚀 **EMA CROSSOVER SIGNAL!** 🚀\n\nCoin: #{symbol}\nTimeframe: {user_timeframe}\nEMA: {user_ema}\n\nقیمت نے ای ایم اے کو نیچے سے اوپر کراس کر لیا ہے (Spot Buy)۔"
                await bot.send_message(chat_id=CHAT_ID, text=message, parse_mode="Markdown")
            await asyncio.sleep(1)  # رینڈر پر لوڈ اور بائنانس بلاکنگ سے بچنے کے لیے محفوظ وقفہ
        
        # ایک چکر پورا ہونے کے بعد 5 منٹ کا آرام تاکہ سرور ہینگ نہ ہو
        if is_scanning:
            await asyncio.sleep(300)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply_keyboard = [['20', '50', '200']]
    await update.message.reply_text(
        "سلام! کرپٹو اسپاٹ ٹریڈنگ بوٹ میں خوش آمدید۔\n\nسب سے پہلے نیچے دیے گئے آپشنز میں سے اپنا **EMA** منتخب کریں:",
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True, resize_keyboard=True)
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global user_ema, user_timeframe, is_scanning
    text = update.message.text

    if text in ['20', '50', '200']:
        user_ema = int(text)
        reply_keyboard = [['1h', '4h', '1d']]
        await update.message.reply_text(
            f"آپ نے {user_ema} EMA منتخب کیا ہے۔\n\nاب اپنا پسندیدہ **ٹائم فریم (Timeframe)** منتخب کریں:",
            reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True, resize_keyboard=True)
        )
    elif text in ['1h', '4h', '1d']:
        user_timeframe = text
        if not is_scanning:
            is_scanning = True
            await update.message.reply_text(
                f"سیٹنگز محفوظ ہو گئی ہیں!\n\nEMA: {user_ema}\nTimeframe: {user_timeframe}\n\nبوٹ اب اسکیننگ شروع کر رہا ہے...",
                reply_markup=ReplyKeyboardRemove()
            )
            # پس منظر میں اسکیننگ شروع کرنے کے لیے اسینکرونس ٹاسک بنائیں
            asyncio.create_task(start_scanning(context.bot))
    elif text == '/stop':
        is_scanning = False
        await update.message.reply_text("اسکیننگ روک دی گئی ہے۔ دوبارہ شروع کرنے کے لیے /start لکھیں۔")

def main():
    # ویب سرور کو الگ تھریڈ میں چلائیں
    Thread(target=run_server).start()

    # ٹیلی گرام بوٹ سیٹ اپ
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stop", handle_message))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("Bot is starting...")
    application.run_polling()

if __name__ == '__main__':
    main()
