"""
Telegram EMA Crossover Alert Bot
---------------------------------
Monitors the top USDT spot pairs on Binance (by 24h quote volume, up to 500)
and alerts on Telegram whenever a CLOSED candle crosses ABOVE a
user-configurable EMA, on a user-configurable timeframe (1h / 4h / 1d).

Fully interactive via Telegram commands:
    /start                 Welcome + command list
    /settings               Show current EMA period, timeframe, next scan
    /set_ema <int>           Change EMA period (default 21)
    /set_tf <1h|4h|1d>       Change timeframe (default 1d)
    /scan                    Trigger an immediate manual scan

Designed to run on Render.com as a Web Service (binds to $PORT) with a
self-ping keep-alive loop so the free-tier instance doesn't idle out.
"""

import os
import json
import time
import asyncio
import logging
import threading
from datetime import datetime, timezone, timedelta

import requests
import pandas as pd
import ccxt.async_support as ccxt_async
from flask import Flask, jsonify

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
log = logging.getLogger("ema_bot")

# --------------------------------------------------------------------------
# Environment / secrets
# --------------------------------------------------------------------------
# NOTE: Do NOT hardcode real secrets here. Set these in Render's
# "Environment" tab. If they are missing the bot will log a clear error
# and refuse to start rather than silently failing.
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
PORT = int(os.environ.get("PORT", 8080))
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "")  # auto-set by Render

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

DEFAULT_CONFIG = {
    "ema_period": 21,
    "timeframe": "1d",   # one of: 1h, 4h, 1d
    "top_n": 500,
}

VALID_TIMEFRAMES = {"1h", "4h", "1d"}

# --------------------------------------------------------------------------
# Config persistence
# --------------------------------------------------------------------------
_config_lock = threading.Lock()


def load_config() -> dict:
    with _config_lock:
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r") as f:
                    data = json.load(f)
                cfg = {**DEFAULT_CONFIG, **data}
                return cfg
            except Exception as e:
                log.error(f"Failed to load config, using defaults: {e}")
        return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    with _config_lock:
        try:
            with open(CONFIG_PATH, "w") as f:
                json.dump(cfg, f)
        except Exception as e:
            log.error(f"Failed to save config: {e}")


CONFIG = load_config()

# Tracks the last candle open-time (ms) we already alerted on, per symbol,
# so we don't spam the same crossover every scan cycle.
LAST_ALERTED = {}

# --------------------------------------------------------------------------
# Flask dummy web server (required so Render's Web Service port check passes)
# --------------------------------------------------------------------------
flask_app = Flask(__name__)


@flask_app.route("/")
def index():
    return jsonify(
        status="ok",
        service="ema-crossover-telegram-bot",
        ema_period=CONFIG["ema_period"],
        timeframe=CONFIG["timeframe"],
        time_utc=datetime.now(timezone.utc).isoformat(),
    )


@flask_app.route("/health")
def health():
    return "OK", 200


def run_flask():
    log.info(f"Starting Flask dummy server on 0.0.0.0:{PORT}")
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


def self_ping_loop():
    """Pings the service's own public URL periodically so Render's free
    web-service tier does not spin the instance down for inactivity."""
    if not RENDER_EXTERNAL_URL:
        log.warning(
            "RENDER_EXTERNAL_URL not set - skipping self-ping keep-alive loop. "
            "Render sets this automatically on Web Services, so this should "
            "only happen in local testing."
        )
        return
    url = RENDER_EXTERNAL_URL.rstrip("/") + "/health"
    while True:
        try:
            r = requests.get(url, timeout=10)
            log.info(f"Self-ping {url} -> {r.status_code}")
        except Exception as e:
            log.warning(f"Self-ping failed: {e}")
        time.sleep(600)  # every 10 minutes


# --------------------------------------------------------------------------
# Market data helpers
# --------------------------------------------------------------------------
EXCLUDE_TOKEN_SUBSTRINGS = ("UP/", "DOWN/", "BULL/", "BEAR/")


async def get_top_symbols(exchange: "ccxt_async.binance", top_n: int) -> list:
    """Return the top_n USDT spot pairs on Binance ranked by 24h quote volume."""
    try:
        markets = await exchange.load_markets()
        tickers = await exchange.fetch_tickers()
    except Exception as e:
        log.error(f"Failed to fetch markets/tickers: {e}")
        return []

    candidates = []
    for symbol, market in markets.items():
        if not market.get("spot", False):
            continue
        if market.get("quote") != "USDT":
            continue
        if not market.get("active", True):
            continue
        if any(sub in symbol for sub in EXCLUDE_TOKEN_SUBSTRINGS):
            continue
        ticker = tickers.get(symbol)
        if not ticker:
            continue
        quote_volume = ticker.get("quoteVolume") or 0
        candidates.append((symbol, quote_volume))

    candidates.sort(key=lambda x: x[1], reverse=True)
    top_symbols = [s for s, _ in candidates[:top_n]]
    log.info(f"Selected top {len(top_symbols)} USDT spot pairs by volume")
    return top_symbols


async def fetch_and_check(exchange, symbol, ema_period, timeframe, semaphore):
    """Fetch OHLCV for one symbol and check for a fresh EMA crossover on the
    last fully-closed candle. Returns a dict if a crossover fired, else None."""
    limit = max(ema_period * 3, 60)
    limit = min(limit, 500)
    async with semaphore:
        for attempt in range(3):
            try:
                ohlcv = await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
                break
            except ccxt_async.RateLimitExceeded:
                await asyncio.sleep(1.5 * (attempt + 1))
            except Exception as e:
                if attempt == 2:
                    log.debug(f"{symbol}: fetch_ohlcv failed after retries: {e}")
                    return None
                await asyncio.sleep(1.0 * (attempt + 1))
        else:
            return None

    if not ohlcv or len(ohlcv) < ema_period + 2:
        return None

    # Drop the last candle - it may still be forming (incomplete).
    closed_candles = ohlcv[:-1]
    if len(closed_candles) < ema_period + 2:
        return None

    df = pd.DataFrame(
        closed_candles, columns=["ts", "open", "high", "low", "close", "volume"]
    )
    df["ema"] = df["close"].ewm(span=ema_period, adjust=False).mean()

    prev_close, prev_ema = df["close"].iloc[-2], df["ema"].iloc[-2]
    last_close, last_ema = df["close"].iloc[-1], df["ema"].iloc[-1]
    last_ts = int(df["ts"].iloc[-1])

    crossed_above = prev_close <= prev_ema and last_close > last_ema
    if not crossed_above:
        return None

    # Avoid re-alerting on the same closed candle across repeated scans.
    key = f"{symbol}|{timeframe}|{ema_period}"
    if LAST_ALERTED.get(key) == last_ts:
        return None
    LAST_ALERTED[key] = last_ts

    return {
        "symbol": symbol,
        "price": last_close,
        "ema": last_ema,
        "candle_time": last_ts,
    }


async def scan_market(ema_period: int, timeframe: str, top_n: int) -> list:
    """Run a full scan across the top_n symbols and return crossover hits."""
    exchange = ccxt_async.binance({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    try:
        symbols = await get_top_symbols(exchange, top_n)
        if not symbols:
            return []

        semaphore = asyncio.Semaphore(15)
        tasks = [
            fetch_and_check(exchange, sym, ema_period, timeframe, semaphore)
            for sym in symbols
        ]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        hits = [r for r in results if r]
        hits.sort(key=lambda x: x["symbol"])
        return hits
    finally:
        await exchange.close()


# --------------------------------------------------------------------------
# Message formatting
# --------------------------------------------------------------------------
def format_hit(hit: dict, timeframe: str, ema_period: int) -> str:
    symbol = hit["symbol"]
    base = symbol.split("/")[0]
    price = hit["price"]
    ema = hit["ema"]
    price_str = f"{price:,.8f}".rstrip("0").rstrip(".") if price < 1 else f"{price:,.4f}"
    ema_str = f"{ema:,.8f}".rstrip("0").rstrip(".") if ema < 1 else f"{ema:,.4f}"
    tv_link = f"https://www.tradingview.com/chart/?symbol=BINANCE:{base}USDT"
    return (
        f"🟢 *{base}/USDT*\n"
        f"Price: `{price_str}` closed above EMA{ema_period} (`{ema_str}`)\n"
        f"Timeframe: {timeframe}\n"
        f"[View Chart on TradingView]({tv_link})"
    )


def chunk_messages(header: str, blocks: list, max_len: int = 4000) -> list:
    """Splits a list of message blocks into Telegram-safe chunks (<4096 chars)."""
    chunks = []
    current = header
    for block in blocks:
        if len(current) + len(block) + 2 > max_len:
            chunks.append(current)
            current = block
        else:
            current += "\n\n" + block
    chunks.append(current)
    return chunks


def next_scan_eta(timeframe: str) -> str:
    now = datetime.now(timezone.utc)
    if timeframe == "1h":
        nxt = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)) + timedelta(minutes=1)
    elif timeframe == "4h":
        hour_block = (now.hour // 4 + 1) * 4
        nxt = now.replace(minute=0, second=0, microsecond=0)
        nxt += timedelta(hours=(hour_block - now.hour))
        nxt += timedelta(minutes=1)
    else:  # 1d
        nxt = now.replace(hour=0, minute=5, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
    return nxt.strftime("%Y-%m-%d %H:%M UTC")


# --------------------------------------------------------------------------
# Authorization guard
# --------------------------------------------------------------------------
def is_authorized(update: Update) -> bool:
    if not TELEGRAM_CHAT_ID:
        return True  # no restriction configured
    return str(update.effective_chat.id) == str(TELEGRAM_CHAT_ID)


# --------------------------------------------------------------------------
# Telegram command handlers
# --------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    text = (
        "👋 *EMA Crossover Alert Bot*\n\n"
        "I scan the top USDT spot pairs on Binance and alert you whenever "
        "a *closed candle crosses above* your chosen EMA.\n\n"
        "*Commands:*\n"
        "`/settings` — show current EMA, timeframe & next scan\n"
        "`/set_ema <value>` — change EMA period, e.g. `/set_ema 50`\n"
        "`/set_tf <1h|4h|1d>` — change candle timeframe, e.g. `/set_tf 4h`\n"
        "`/scan` — run an immediate manual scan\n\n"
        f"Current settings: EMA {CONFIG['ema_period']} on {CONFIG['timeframe']} "
        f"across top {CONFIG['top_n']} pairs."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    cfg = CONFIG
    text = (
        "⚙️ *Current Settings*\n\n"
        f"EMA Period: `{cfg['ema_period']}`\n"
        f"Timeframe: `{cfg['timeframe']}`\n"
        f"Universe: top `{cfg['top_n']}` USDT spot pairs (by volume)\n"
        f"Next scheduled scan: `{next_scan_eta(cfg['timeframe'])}`"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_set_ema(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/set_ema 50`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        value = int(context.args[0])
        if value < 2 or value > 400:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Please provide an integer between 2 and 400, e.g. `/set_ema 50`", parse_mode=ParseMode.MARKDOWN)
        return

    CONFIG["ema_period"] = value
    save_config(CONFIG)
    LAST_ALERTED.clear()
    await update.message.reply_text(f"✅ EMA period updated to *{value}*.", parse_mode=ParseMode.MARKDOWN)
    log.info(f"EMA period changed to {value}")


async def cmd_set_tf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    if not context.args or context.args[0].lower() not in VALID_TIMEFRAMES:
        await update.message.reply_text(
            "Usage: `/set_tf 1h` (options: `1h`, `4h`, `1d`)", parse_mode=ParseMode.MARKDOWN
        )
        return

    tf = context.args[0].lower()
    CONFIG["timeframe"] = tf
    save_config(CONFIG)
    LAST_ALERTED.clear()
    reschedule_job(context.application, tf)
    await update.message.reply_text(f"✅ Timeframe updated to *{tf}*.", parse_mode=ParseMode.MARKDOWN)
    log.info(f"Timeframe changed to {tf}")


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text("🔍 Scanning top pairs, this can take a minute...")
    try:
        hits = await scan_market(CONFIG["ema_period"], CONFIG["timeframe"], CONFIG["top_n"])
        await send_scan_results(context.application, update.effective_chat.id, hits, manual=True)
    except Exception as e:
        log.exception("Manual scan failed")
        await update.message.reply_text(f"❌ Scan failed: {e}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error(f"Unhandled exception: {context.error}", exc_info=context.error)


# --------------------------------------------------------------------------
# Sending results
# --------------------------------------------------------------------------
async def send_scan_results(app: Application, chat_id, hits: list, manual: bool = False):
    cfg = CONFIG
    if not hits:
        note = "Manual scan complete." if manual else "Scheduled scan complete."
        await app.bot.send_message(
            chat_id=chat_id,
            text=f"ℹ️ {note} No EMA{cfg['ema_period']} crossovers found on {cfg['timeframe']}.",
        )
        return

    header = (
        f"📊 *{'Manual' if manual else 'Scheduled'} Scan Results*\n"
        f"EMA{cfg['ema_period']} • {cfg['timeframe']} • {len(hits)} crossover(s) found"
    )
    blocks = [format_hit(h, cfg["timeframe"], cfg["ema_period"]) for h in hits]
    for chunk in chunk_messages(header, blocks):
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
        except Exception as e:
            log.error(f"Failed to send message chunk: {e}")


async def scheduled_scan_job(context: ContextTypes.DEFAULT_TYPE):
    log.info("Running scheduled scan...")
    try:
        hits = await scan_market(CONFIG["ema_period"], CONFIG["timeframe"], CONFIG["top_n"])
        target_chat = TELEGRAM_CHAT_ID or None
        if target_chat:
            await send_scan_results(context.application, target_chat, hits, manual=False)
        else:
            log.warning("TELEGRAM_CHAT_ID not set - scheduled results not sent anywhere.")
    except Exception:
        log.exception("Scheduled scan failed")


# --------------------------------------------------------------------------
# Job scheduling
# --------------------------------------------------------------------------
JOB_NAME = "scheduled_scan"


def reschedule_job(app: Application, timeframe: str):
    current_jobs = app.job_queue.get_jobs_by_name(JOB_NAME)
    for job in current_jobs:
        job.schedule_removal()

    if timeframe == "1h":
        app.job_queue.run_repeating(scheduled_scan_job, interval=3600, first=60, name=JOB_NAME)
    elif timeframe == "4h":
        app.job_queue.run_repeating(scheduled_scan_job, interval=14400, first=60, name=JOB_NAME)
    else:  # 1d
        from datetime import time as dtime
        app.job_queue.run_daily(scheduled_scan_job, time=dtime(hour=0, minute=5, tzinfo=timezone.utc), name=JOB_NAME)

    log.info(f"Scheduled job rescheduled for timeframe: {timeframe}")


async def post_init(app: Application):
    reschedule_job(app, CONFIG["timeframe"])
    log.info("Bot initialized and job scheduled.")


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------
def main():
    if not TELEGRAM_TOKEN:
        log.error(
            "TELEGRAM_TOKEN environment variable is not set. "
            "Set it in Render's Environment tab before deploying."
        )
        raise SystemExit(1)
    if not TELEGRAM_CHAT_ID:
        log.warning(
            "TELEGRAM_CHAT_ID is not set. The bot will still respond to commands "
            "from anyone, and scheduled scans will not be delivered anywhere "
            "until you send /start once (or set the env var)."
        )

    # Flask dummy server thread (Render port binding requirement)
    threading.Thread(target=run_flask, daemon=True).start()
    # Keep-alive self-ping thread
    threading.Thread(target=self_ping_loop, daemon=True).start()

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("settings", cmd_settings))
    application.add_handler(CommandHandler("set_ema", cmd_set_ema))
    application.add_handler(CommandHandler("set_tf", cmd_set_tf))
    application.add_handler(CommandHandler("scan", cmd_scan))
    application.add_error_handler(error_handler)

    log.info("Starting Telegram bot polling...")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
