import logging
import os
import asyncio
import time
import json
import aiohttp
import yt_dlp

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
)

# --- Configuration ---
BOT_TOKEN = "8107253903:AAFwss6BKmUhKdSpSThQWxAiLE0CzKLIdJA"
DOWNLOAD_PATH = "downloads"
MAX_FILE_SIZE_MB = 48  # Set slightly below 50MB for safety

# --- States for ConversationHandler ---
CHOOSING_FORMAT, DOWNLOADING = range(2)

# --- Logging Setup ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Job Queue ---
download_queue = asyncio.Queue()

# --- Helper Functions ---
async def upload_to_gofile(file_path):
    """Uploads a file to gofile.io and returns the download link."""
    try:
        async with aiohttp.ClientSession() as session:
            # 1. Get the best server
            async with session.get("https://api.gofile.io/getServer") as response:
                if response.status != 200:
                    return None
                server_data = await response.json()
                server = server_data["data"]["server"]

            # 2. Upload the file
            url = f"https://{server}.gofile.io/uploadFile"
            with open(file_path, 'rb') as f:
                async with session.post(url, data={'file': f}) as upload_response:
                    if upload_response.status != 200:
                        return None
                    upload_data = await upload_response.json()
                    return upload_data.get("data", {}).get("downloadPage")
    except Exception as e:
        logger.error(f"GoFile upload failed: {e}")
        return None

# --- Bot Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Sends a welcome message and prompts for a URL."""
    user_name = update.effective_user.first_name
    await update.message.reply_text(
        f"Hi {user_name}! 👋\n\nSend me a YouTube video or playlist link."
    )
    return CHOOSING_FORMAT

async def ask_for_format(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receives a URL and asks the user to choose a download format."""
    url = update.message.text
    if "youtube.com/" not in url and "youtu.be/" not in url:
        await update.message.reply_text("That doesn't look like a YouTube link. Please try again.")
        return CHOOSING_FORMAT

    context.user_data['url'] = url
    keyboard = [
        [InlineKeyboardButton("🎬 Video", callback_data='video')],
        [InlineKeyboardButton("🎵 Audio (MP3)", callback_data='audio')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Great! Choose your desired format:", reply_markup=reply_markup)
    return DOWNLOADING

async def process_download_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Processes the user's format choice and adds the job to the queue."""
    query = update.callback_query
    await query.answer()

    url = context.user_data.get('url')
    if not url:
        await query.edit_message_text(text="Sorry, something went wrong. Please send the link again.")
        return ConversationHandler.END

    format_choice = query.data
    chat_id = update.effective_chat.id

    message = await query.edit_message_text(text="Your request has been added to the queue... ⏳")
    await download_queue.put((chat_id, message.message_id, url, format_choice))

    return ConversationHandler.END

async def download_worker(app: Application):
    """A worker that processes download jobs from the queue one by one."""
    logger.info("Download worker started.")
    while True:
        chat_id, message_id, url, format_choice = await download_queue.get()
        try:
            await process_download(app.bot, chat_id, message_id, url, format_choice)
        except Exception as e:
            logger.error(f"Error processing download for {url}: {e}", exc_info=True)
            try:
                await app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text="❌ An unexpected error occurred during processing."
                )
            except Exception as e2:
                logger.error(f"Failed to even send error message: {e2}")
        finally:
            download_queue.task_done()

# --- The Core Download Logic ---
async def process_download(bot, chat_id, message_id, url, format_choice):
    """The main download and processing function, called by the worker."""
    last_update_time = [0] # Use a list to make it mutable inside the hook

    def progress_hook(d):
        if d['status'] == 'downloading':
            current_time = time.time()
            if current_time - last_update_time[0] < 2:
                return

            total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate')
            downloaded_bytes = d.get('downloaded_bytes')
            if total_bytes and downloaded_bytes:
                percent = downloaded_bytes / total_bytes * 100
                progress = int(percent / 10)
                bar = '█' * progress + '─' * (10 - progress)
                eta = d.get('eta', 'N/A')
                
                try:
                    asyncio.run_coroutine_threadsafe(
                        bot.edit_message_text(
                            f"Downloading\\.\\.\\.\n`[{bar}] {percent:.1f}%`\n\nETA: {eta}s",
                            chat_id=chat_id,
                            message_id=message_id,
                            parse_mode='MarkdownV2'
                        ),
                        asyncio.get_running_loop()
                    )
                    last_update_time[0] = current_time
                except Exception:
                    # Ignore errors if message editing fails (e.g., message too old)
                    pass

    if not os.path.exists(DOWNLOAD_PATH):
        os.makedirs(DOWNLOAD_PATH)
        
    ydl_opts = {
        'outtmpl': os.path.join(DOWNLOAD_PATH, '%(title)s.%(ext)s'),
        'noplaylist': True,
        'progress_hooks': [progress_hook],
        'quiet': True,
        'noprogress': True,
    }

    if format_choice == 'audio':
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    else:
        ydl_opts['format'] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            await bot.edit_message_text("Starting download...", chat_id=chat_id, message_id=message_id)
            info = await asyncio.to_thread(ydl.extract_info, url, download=True)
            file_path = ydl.prepare_filename(info)
            if format_choice == 'audio':
                file_path = os.path.splitext(file_path)[0] + '.mp3'
    except Exception as e:
        logger.error(f"yt-dlp error: {e}")
        await bot.edit_message_text("❌ Download failed. The video might be private or unavailable.", chat_id=chat_id, message_id=message_id)
        return

    await bot.edit_message_text("✅ Download complete! Uploading...", chat_id=chat_id, message_id=message_id)
    
    if not os.path.exists(file_path):
         await bot.edit_message_text("❌ Error: Downloaded file not found.", chat_id=chat_id, message_id=message_id)
         return

    if os.path.getsize(file_path) > MAX_FILE_SIZE_MB * 1024 * 1024:
        await bot.edit_message_text("File is >50MB. Uploading to file host...", chat_id=chat_id, message_id=message_id)
        download_link = await upload_to_gofile(file_path)
        if download_link:
            await bot.send_message(chat_id, f"File was too large for Telegram.\nDownload it here: {download_link}")
        else:
            await bot.send_message(chat_id, "❌ Sorry, failed to upload the large file.")
    else:
        try:
            if format_choice == 'audio':
                await bot.send_audio(chat_id=chat_id, audio=open(file_path, 'rb'), caption=os.path.basename(file_path))
            else:
                await bot.send_video(chat_id=chat_id, video=open(file_path, 'rb'), caption=os.path.basename(file_path), supports_streaming=True)
        except Exception as e:
            logger.error(f"Telegram upload failed: {e}")
            await bot.send_message(chat_id, "❌ Sorry, failed to upload the file to Telegram.")

    await bot.delete_message(chat_id=chat_id, message_id=message_id)
    if os.path.exists(file_path):
        os.remove(file_path)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Operation canceled.")
    return ConversationHandler.END

async def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()
    application.post_init = download_worker

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start), MessageHandler(filters.TEXT & ~filters.COMMAND, ask_for_format)],
        states={
            CHOOSING_FORMAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_for_format)],
            DOWNLOADING: [CallbackQueryHandler(process_download_choice)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False
    )

    application.add_handler(conv_handler)
    
    logger.info("Bot starting...")

    await application.initialize()
    await application.updater.start_polling(drop_pending_updates=True)
    await application.start()
    
    logger.info("Bot has started successfully and is polling.")

if __name__ == "__main__":
    asyncio.run(main())
    
