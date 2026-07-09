import os
import time
import requests
import pandas as pd
import ta
from flask import Flask
from threading import Thread
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# --- کنفیگریشن ---
BOT_TOKEN = "8693213483:AAGd0ueLdox6tBDG9mbAr2HnaSgJLdFWh_4"  # آپ کا ٹوکن اب بالکل صحیح فارمیٹ میں ہے
CHAT_ID = 7548033382                                       # آپ کی چیٹ آئی ڈی

# رینڈر (Render) کے لیے ویب سرور سیٹ اپ تاکہ بوٹ بند نہ ہو
app = Flask('')

@app.route('/')
def home():
    return "Bot is running!"

def run_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# یوزر کی ترجیحات محفوظ کرنے کے لیے گلوبل ویری ایبلز
user_ema = 50
user_timeframe = '1h'
is_scanning = False

# بائنانس سے تمام USDT اسپاٹ پیئرز حاصل کرنے کا فنکشن
def get_usdt_pairs():
    try:
        url = "https://api.binance.com/api/v3/exchangeInfo"
        response = requests.get(url).json()
        pairs = [symbols['symbol'] for symbols in response['symbols'] if symbols['symbol'].endswith('USDT') and symbols['status'] == 'TRADING']
        return pairs[:200]  # رینڈر کے فری اکاؤنٹ پر لوڈ کم رکھنے کے لیے ٹاپ 200 پیئرز
    except Exception as e:
        print(f"Error fetching pairs: {e}")
        return []

# کینڈل اسٹک ڈیٹا حاصل کرنے اور EMA چیک کرنے کا فنکشن
def check_crossover(symbol, timeframe, ema_period):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={timeframe}&limit={ema_period + 5}"
        data = requests.get(url).json()
        
        if not data or len(data) < (ema_period + 2):
            return False
            
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'qav', 'num_trades', 'taker_base', 'taker_quote', 'ignore'])
        df['close'] = df['close'].astype(float)
        
        # EMA کیلکولیٹ کرنا
        df['ema'] = ta.trend.ema_indicator(df['close'], window=int(ema_period))
        
        # آخری مکمل ہونے والی کینڈل اور اس سے پچھلی کینڈل کا ڈیٹا
        prev_close = df['close'].iloc[-3]
        prev_ema = df['ema'].iloc[-3]
        
        current_close = df['close'].iloc[-2]
        current_ema = df['ema'].iloc[-2]
        
        # کراس اوور لاجک: پچھلی کینڈل EMA سے نیچے تھی، اب والی اوپر بند ہوئی
        if prev_close < prev_ema and current_close > current_ema:
            return True
    except Exception as e:
        pass
    return False

# پس منظر (Background) میں اسکیننگ کرنے والا لوپ
async def scanning_loop(context: ContextTypes.DEFAULT_TYPE):
    global is_scanning, user_ema, user_timeframe
    if not is_scanning:
        return
        
    pairs = get_usdt_pairs()
    await context.bot.send_message(chat_id=CHAT_ID, text=f"🔍 اسکیننگ شروع ہو گئی ہے...\nEMA: {user_ema}\nٹائم فریم: {user_timeframe}\nکل کوائنز: {len(pairs)}")
    
    for symbol in pairs:
        if not is_scanning:
            break
        if check_crossover(symbol, user_timeframe, user_ema):
            message = f"🚀 **EMA CROSSOVER SIGNAL!** 🚀\n\nCoin: #{symbol}\nTimeframe: {user_timeframe}\nEMA: {user_ema}\n\nقیمت نے ای ایم اے کو نیچے سے اوپر کراس کر لیا ہے (Spot Buy)۔"
            await context.bot.send_message(chat_id=CHAT_ID, text=message, parse_mode="Markdown")
        time.sleep(0.5) # بائنانس کی API بلاک نہ ہو اس لیے تھوڑا وقفہ

# ٹیلی گرام کمانڈز
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
        is_scanning = True
        await update.message.reply_text(
            f"سیٹنگز محفوظ ہو گئی ہیں!\n\nEMA: {user_ema}\nTimeframe: {user_timeframe}\n\nبوٹ اب اسکیننگ شروع کر رہا ہے...",
            reply_markup=ReplyKeyboardRemove()
        )
        # اسکیننگ شروع کرنے کا ٹاسک شیڈول کریں
        context.job_queue.run_once(scanning_loop, when=0)
    elif text.lower() == '/stop':
        is_scanning = False
        await update.message.reply_text("اسکیننگ روک دی گئی ہے۔ دوبارہ شروع کرنے کے لیے /start لکھیں۔")

def main():
    # ویب سرور کو الگ تھریڈ میں چلائیں
    Thread(target=run_server).start()

    # ٹیلی گرام بوٹ سیٹ اپ
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stop", lambda u, c: handle_message(u, c)))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("Bot is starting...")
    application.run_polling()

if __name__ == '__main__':
    main()
