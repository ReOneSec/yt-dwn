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
            async with aiohttp.FormData() as form_data:
                form_data.add_field('file', open(file_path, 'rb'))
                async with session.post(url, data=form_data) as upload_response:
                    if upload_response.status != 200:
                        return None
                    upload_data = await upload_response.json()
                    return upload_data["data"]["downloadPage"]
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

    url = context.user_data['url']
    format_choice = query.data
    chat_id = update.effective_chat.id
    
    # Add job to the queue
    message = await query.edit_message_text(text=" M_Bot Is On Work")
    await download_queue.put((chat_id, message.message_id, url, format_choice))
    
    await context.bot.send_message(chat_id, text="Your request has been added to the queue. ⏳")
    return ConversationHandler.END

async def download_worker(app: Application):
    """A worker that processes download jobs from the queue one by one."""
    logger.info("Download worker started.")
    while True:
        chat_id, message_id, url, format_choice = await download_queue.get()
        try:
            await process_download(app.bot, chat_id, message_id, url, format_choice)
        except Exception as e:
            logger.error(f"Error processing download for {url}: {e}")
            await app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="❌ An unexpected error occurred during processing."
            )
        finally:
            download_queue.task_done()

# --- The Core Download Logic ---
async def process_download(bot, chat_id, message_id, url, format_choice):
    """The main download and processing function, called by the worker."""
    last_update_time = 0

    def progress_hook(d):
        nonlocal last_update_time
        if d['status'] == 'downloading':
            current_time = time.time()
            if current_time - last_update_time < 2: # Limit updates to every 2 seconds
                return

            total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate')
            downloaded_bytes = d.get('downloaded_bytes')
            if total_bytes and downloaded_bytes:
                percent = downloaded_bytes / total_bytes * 100
                progress = int(percent / 10)
                bar = '█' * progress + '─' * (10 - progress)
                speed = d.get('speed')
                eta = d.get('eta')
                
                # Use asyncio to run the async bot method from this sync hook
                asyncio.run_coroutine_threadsafe(
                    bot.edit_message_text(
                        f"Downloading...\n`[{bar}] {percent:.1f}%`\n\nETA: {eta}s",
                        chat_id=chat_id,
                        message_id=message_id,
                        parse_mode='MarkdownV2'
                    ),
                    asyncio.get_running_loop()
                )
                last_update_time = current_time

    # Setup yt-dlp options based on choice
    if not os.path.exists(DOWNLOAD_PATH):
        os.makedirs(DOWNLOAD_PATH)
        
    ydl_opts = {
        'outtmpl': os.path.join(DOWNLOAD_PATH, '%(title)s.%(ext)s'),
        'noplaylist': True, # For simplicity, we handle one video at a time
        'progress_hooks': [progress_hook],
        'quiet': True,
    }

    if format_choice == 'audio':
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    else: # video
        ydl_opts['format'] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'

    # Download
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            await bot.edit_message_text("Starting download process...", chat_id=chat_id, message_id=message_id)
            info = ydl.extract_info(url, download=True)
            file_path = ydl.prepare_filename(info)
            # For audio, the extension changes after post-processing
            if format_choice == 'audio':
                file_path = os.path.splitext(file_path)[0] + '.mp3'
    except Exception as e:
        logger.error(f"yt-dlp error: {e}")
        await bot.edit_message_text("❌ Download failed. The video might be private or unavailable.", chat_id=chat_id, message_id=message_id)
        return

    # Upload and send
    await bot.edit_message_text("✅ Download complete! Now uploading to Telegram...", chat_id=chat_id, message_id=message_id)
    
    if os.path.getsize(file_path) > MAX_FILE_SIZE_MB * 1024 * 1024:
        await bot.edit_message_text("File is >50MB. Uploading to gofile.io, this may take a while...", chat_id=chat_id, message_id=message_id)
        download_link = await upload_to_gofile(file_path)
        if download_link:
            await bot.send_message(chat_id, f"File was too large for Telegram.\n\nDownload it here: {download_link}")
        else:
            await bot.send_message(chat_id, "❌ Sorry, failed to upload the large file to a sharing service.")
    else:
        media_file = open(file_path, 'rb')
        caption_text = os.path.basename(file_path)
        try:
            if format_choice == 'audio':
                await bot.send_audio(chat_id=chat_id, audio=media_file, caption=caption_text)
            else:
                await bot.send_video(chat_id=chat_id, video=media_file, caption=caption_text, supports_streaming=True)
        except Exception as e:
            logger.error(f"Telegram upload failed: {e}")
            await bot.send_message(chat_id, "❌ Sorry, failed to upload the file to Telegram.")

    # Cleanup
    await bot.delete_message(chat_id=chat_id, message_id=message_id)
    os.remove(file_path)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels and ends the conversation."""
    await update.message.reply_text("Operation canceled.")
    return ConversationHandler.END


async def main() -> None:
    """Run the bot."""
    # The application object
    application = Application.builder().token(BOT_TOKEN).build()
    
    # This line sets up the worker to run in the background
    application.post_init = download_worker

    # --- Conversation Handler with the per_message fix ---
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start), MessageHandler(filters.TEXT & ~filters.COMMAND, ask_for_format)],
        states={
            CHOOSING_FORMAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_for_format)],
            DOWNLOADING: [CallbackQueryHandler(process_download_choice)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False  # Correct location for this argument
    )

    application.add_handler(conv_handler)
    
    logger.info("Bot starting...")

    # This startup sequence is crucial
    # 1. initialize() runs the post_init worker
    await application.initialize()
    # 2. start_polling() connects to Telegram
    await application.updater.start_polling(drop_pending_updates=True)
    # 3. start() begins processing updates
    await application.start()
    
    logger.info("Bot has started successfully and is polling.")


if __name__ == "__main__":
    asyncio.run(main())
    
