import logging
import os
import time
import re # Import regex for basic URL validation
from pytube import YouTube, Playlist
from pytube.exceptions import PytubeError, RegexMatchError, VideoUnavailable

from telegram import Update, ForceReply
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Enable logging for better debugging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Configuration ---
# Retrieve values from environment variables
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID_STR = os.getenv("TELEGRAM_ADMIN_ID") # Get as string first

# Validate essential configuration
if not TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN not found in .env file. Please set it.")
    exit("TELEGRAM_BOT_TOKEN is missing. Exiting.")

ADMIN_ID = None
if ADMIN_ID_STR:
    try:
        ADMIN_ID = int(ADMIN_ID_STR)
        logger.info(f"Admin ID set to: {ADMIN_ID}")
    except ValueError:
        logger.warning(f"Invalid TELEGRAM_ADMIN_ID in .env file: '{ADMIN_ID_STR}'. It should be an integer.")
else:
    logger.info("TELEGRAM_ADMIN_ID not set in .env file.")

# Directory to save downloaded videos temporarily
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True) # Ensure the directory exists

# Telegram's general file upload limit is around 50 MB (50 * 1024 * 1024 bytes)
# While some video types might allow larger, 50MB is a safe general threshold for direct sends.
MAX_TELEGRAM_FILE_SIZE_MB = 50
MAX_TELEGRAM_FILE_SIZE_BYTES = MAX_TELEGRAM_FILE_SIZE_MB * 1024 * 1024

# Regex for basic YouTube URL validation
YOUTUBE_URL_REGEX = r"(?:https?:\/\/)?(?:www\.)?(?:m\.)?(?:youtube\.com|youtu\.be)\/(?:watch\?v=|playlist\?list=|shorts\/|embed\/|v\/|)([a-zA-Z0-9_-]{11}|[a-zA-Z0-9_-]+)"

# --- Bot Command Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a welcome message when the /start command is issued."""
    user = update.effective_user
    await update.message.reply_html(
        f"Hi {user.mention_html()}! 👋 Send me a **YouTube video or playlist URL** and I'll try to download it for you. "
        f"Please note: For files larger than {MAX_TELEGRAM_FILE_SIZE_MB} MB, I might not be able to send them directly via Telegram.",
        reply_markup=ForceReply(selective=True),
    )
    if ADMIN_ID and update.effective_user.id == ADMIN_ID:
        await update.message.reply_text("You are recognized as the admin.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a help message when the /help command is issued."""
    await update.message.reply_text(
        "Simply send me a YouTube video or playlist URL. I'll download it and send it back if the size permits."
    )

# --- Core Download Logic ---

async def download_youtube_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles incoming messages containing YouTube URLs and initiates download."""
    url = update.message.text
    chat_id = update.effective_chat.id

    # Basic URL validation before calling pytube
    if not re.match(YOUTUBE_URL_REGEX, url):
        await update.message.reply_text(
            "❌ That doesn't look like a valid YouTube video or playlist URL. Please send a direct link to a YouTube video or playlist."
        )
        return

    await update.message.reply_text("⏳ Received your URL! Checking for valid YouTube content...")
    logger.info(f"Received URL: {url} from user {update.effective_user.id}")

    try:
        # Check if it's a playlist URL
        if "playlist?list=" in url:
            await handle_playlist_download(update, chat_id, url)
        else:
            # Assume it's a single video URL (including shorts)
            await handle_single_video_download(update, chat_id, url)

    except RegexMatchError:
        # Pytube raises RegexMatchError if the URL is not a valid YouTube URL pattern
        logger.warning(f"RegexMatchError for URL: {url}")
        await update.message.reply_text(
            "❌ That doesn't look like a valid YouTube video or playlist URL. Please try again with a correct link."
        )
    except VideoUnavailable:
        logger.error(f"VideoUnavailable error for URL {url}: The video might be private, deleted, or age-restricted.")
        await update.message.reply_text(
            "❌ The video/playlist is unavailable or private. It might be deleted, restricted, or simply not accessible."
        )
    except PytubeError as e:
        logger.error(f"Pytube Error for URL {url}: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ An error occurred with the YouTube content: {e}. The video/playlist might be unavailable, private, or age-restricted."
        )
    except Exception as e:
        logger.error(f"Unexpected error processing URL {url}: {e}", exc_info=True)
        await update.message.reply_text(
            f"🚫 An unexpected error occurred: {e}. Please check the URL or try again later. "
            f"If you're the admin, check the server logs for more details."
        )
        # Optionally, notify admin if an unexpected error occurs for a non-admin user
        if ADMIN_ID and chat_id != ADMIN_ID:
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID, 
                    text=f"🚨 Bot Error for user {update.effective_user.mention_html()} ({update.effective_user.id}) with URL:\n`{url}`\nError: `{e}`", 
                    parse_mode='HTML'
                )
            except Exception as admin_notify_err:
                logger.error(f"Failed to notify admin about error: {admin_notify_err}")


async def handle_single_video_download(update: Update, chat_id: int, url: str) -> None:
    """Downloads and sends a single YouTube video."""
    yt = YouTube(url)
    await update.message.reply_text(
        f"🔍 Found video: *{yt.title}*.\nStarting download...", parse_mode='Markdown'
    )
    logger.info(f"Processing single video: {yt.title} ({url})")

    # --- Stream Selection: Prioritize MP4 progressive streams ---
    stream = yt.streams.filter(progressive=True, file_extension='mp4').order_by('resolution').desc().first()
    
    if not stream:
        logger.warning(f"No progressive MP4 stream found for {yt.title}. Falling back to highest resolution.")
        stream = yt.streams.get_highest_resolution() # Fallback if no suitable MP4

    if not stream:
        await update.message.reply_text("❌ No suitable video stream found for this URL.")
        logger.error(f"No streams found at all for {yt.title} ({url})")
        return

    # Construct file path
    # Ensure filename is safe (pytube usually handles this, but good to be aware)
    safe_filename = re.sub(r'[\\/:*?"<>|]', '', stream.default_filename) 
    file_path = os.path.join(DOWNLOAD_DIR, safe_filename)
    
    await update.message.reply_text(f"🚀 Downloading *{yt.title}* ({stream.resolution}, {stream.mime_type})...", parse_mode='Markdown')
    logger.info(f"Attempting to download {yt.title} to {file_path}")

    try:
        stream.download(output_path=DOWNLOAD_DIR, filename=safe_filename)
        logger.info(f"Download completed for {yt.title}")
    except Exception as download_err:
        logger.error(f"Error during download of {yt.title}: {download_err}", exc_info=True)
        await update.message.reply_text(f"❌ Failed to download *{yt.title}*: {download_err}", parse_mode='Markdown')
        return # Stop here if download failed

    # --- File Verification after Download ---
    if not os.path.exists(file_path):
        logger.error(f"Downloaded file for {yt.title} does not exist at {file_path} after download.")
        await update.message.reply_text(f"❌ Downloaded file for *{yt.title}* not found on disk. Download likely failed or path issue.", parse_mode='Markdown')
        return

    file_size = os.path.getsize(file_path)
    if file_size == 0:
        logger.error(f"Downloaded file for {yt.title} is empty: {file_path}")
        await update.message.reply_text(f"❌ Downloaded file for *{yt.title}* is empty (0 bytes). Download likely failed. Please try again.", parse_mode='Markdown')
        if os.path.exists(file_path):
            os.remove(file_path) # Clean up empty file
            logger.info(f"Cleaned up empty file: {file_path}")
        return

    file_size_mb = file_size / (1024 * 1024)
    logger.info(f"Verified downloaded file for {yt.title}. Size: {file_size_mb:.2f} MB, Path: {file_path}")
    
    await update.message.reply_text(
        f"✅ Finished downloading *{yt.title}* (Size: {file_size_mb:.2f} MB).\nAttempting to send via Telegram...",
        parse_mode='Markdown'
    )

    # --- Sending to Telegram ---
    if file_size <= MAX_TELEGRAM_FILE_SIZE_BYTES:
        try:
            with open(file_path, 'rb') as f:
                await context.bot.send_video(
                    chat_id=chat_id,
                    video=f,
                    caption=f"Downloaded: {yt.title}",
                    supports_streaming=True # Helps Telegram stream larger videos
                )
            await update.message.reply_text(f"🎉 Successfully sent *{yt.title}*!", parse_mode='Markdown')
            logger.info(f"Successfully sent {yt.title} to chat_id {chat_id}")
        except Exception as e:
            logger.error(f"Error sending video {yt.title} to Telegram (chat_id {chat_id}): {e}", exc_info=True)
            await update.message.reply_text(
                f"⚠️ Failed to send *{yt.title}* via Telegram: {e}.\n"
                f"This can happen if the video format is not supported by Telegram, or due to a network issue.",
                parse_mode='Markdown'
            )
        finally:
            if os.path.exists(file_path):
                os.remove(file_path) # Clean up the downloaded file
                logger.info(f"Deleted temporary file: {file_path}")
    else:
        await update.message.reply_text(
            f"⚠️ Video *{yt.title}* (Size: {file_size_mb:.2f} MB) is too large to send directly via Telegram (max {MAX_TELEGRAM_FILE_SIZE_MB} MB).\n"
            f"You'll need to download it manually from YouTube.",
            parse_mode='Markdown'
        )
        if os.path.exists(file_path): # Clean up even if too large to send
            os.remove(file_path)
            logger.info(f"Deleted temporary file (too large): {file_path}")


async def handle_playlist_download(update: Update, chat_id: int, url: str) -> None:
    """Downloads and sends videos from a YouTube playlist."""
    p = Playlist(url)
    await update.message.reply_text(
        f"🎶 Found playlist: *{p.title}* with {len(p.videos)} videos.\nStarting download of each video...",
        parse_mode='Markdown'
    )
    logger.info(f"Processing playlist: {p.title} ({len(p.videos)} videos)")

    for i, video in enumerate(p.videos):
        video_title = video.title # Store title for consistent logging/messages
        try:
            await update.message.reply_text(
                f"\n🎬 Downloading video {i+1}/{len(p.videos)}: *{video_title}*",
                parse_mode='Markdown'
            )
            logger.info(f"Starting download for playlist video {i+1}/{len(p.videos)}: {video_title}")
            
            # --- Stream Selection: Prioritize MP4 progressive streams ---
            stream = video.streams.filter(progressive=True, file_extension='mp4').order_by('resolution').desc().first()
            
            if not stream:
                logger.warning(f"No progressive MP4 stream found for {video_title}. Falling back to highest resolution.")
                stream = video.streams.get_highest_resolution() # Fallback

            if not stream:
                await update.message.reply_text(f"❌ No suitable stream found for video: *{video_title}*. Skipping.", parse_mode='Markdown')
                logger.error(f"No streams found at all for playlist video {video_title}")
                time.sleep(1) # Small delay before next video attempt
                continue

            # Construct safe filename and file path
            safe_filename = re.sub(r'[\\/:*?"<>|]', '', stream.default_filename)
            file_path = os.path.join(DOWNLOAD_DIR, safe_filename)
            
            await update.message.reply_text(f"🚀 Downloading *{video_title}* ({stream.resolution}, {stream.mime_type})...", parse_mode='Markdown')
            logger.info(f"Attempting to download {video_title} to {file_path}")

            try:
                stream.download(output_path=DOWNLOAD_DIR, filename=safe_filename)
                logger.info(f"Download completed for {video_title}")
            except Exception as download_err:
                logger.error(f"Error during download of {video_title}: {download_err}", exc_info=True)
                await update.message.reply_text(f"❌ Failed to download *{video_title}*: {download_err}. Skipping.", parse_mode='Markdown')
                time.sleep(1) # Small delay before next video attempt
                continue 

            # --- File Verification after Download ---
            if not os.path.exists(file_path):
                logger.error(f"Downloaded file for {video_title} does not exist at {file_path} after download.")
                await update.message.reply_text(f"❌ Downloaded file for *{video_title}* not found on disk. Download likely failed. Skipping.", parse_mode='Markdown')
                time.sleep(1)
                continue

            file_size = os.path.getsize(file_path)
            if file_size == 0:
                logger.error(f"Downloaded file for {video_title} is empty: {file_path}")
                await update.message.reply_text(f"❌ Downloaded file for *{video_title}* is empty (0 bytes). Download likely failed. Skipping.", parse_mode='Markdown')
                if os.path.exists(file_path):
                    os.remove(file_path) # Clean up empty file
                    logger.info(f"Cleaned up empty file: {file_path}")
                time.sleep(1)
                continue

            file_size_mb = file_size / (1024 * 1024)
            logger.info(f"Verified downloaded file for {video_title}. Size: {file_size_mb:.2f} MB, Path: {file_path}")
            
            await update.message.reply_text(
                f"✅ Finished downloading *{video_title}* (Size: {file_size_mb:.2f} MB).\nAttempting to send...",
                parse_mode='Markdown'
            )

            # --- Sending to Telegram ---
            if file_size <= MAX_TELEGRAM_FILE_SIZE_BYTES:
                try:
                    with open(file_path, 'rb') as f:
                        await context.bot.send_video(
                            chat_id=chat_id,
                            video=f,
                            caption=f"Downloaded: {video_title}",
                            supports_streaming=True
                        )
                    await update.message.reply_text(f"🎉 Successfully sent *{video_title}*!", parse_mode='Markdown')
                    logger.info(f"Successfully sent {video_title} to chat_id {chat_id}")
                except Exception as e:
                    logger.error(f"Error sending video {video_title} to Telegram (chat_id {chat_id}): {e}", exc_info=True)
                    await update.message.reply_text(
                        f"⚠️ Failed to send *{video_title}* via Telegram: {e}.\n"
                        f"This can happen if the video format is not supported by Telegram, or due to a network issue. Skipping this video.",
                        parse_mode='Markdown'
                    )
                finally:
                    if os.path.exists(file_path):
                        os.remove(file_path) # Clean up
                        logger.info(f"Deleted temporary file: {file_path}")
            else:
                await update.message.reply_text(
                    f"⚠️ Video *{video_title}* (Size: {file_size_mb:.2f} MB) is too large to send directly via Telegram (max {MAX_TELEGRAM_FILE_SIZE_MB} MB).\n"
                    f"Skipping direct send for this video.",
                    parse_mode='Markdown'
                )
                if os.path.exists(file_path): # Clean up even if too large to send
                    os.remove(file_path)
                    logger.info(f"Deleted temporary file (too large): {file_path}")

        except VideoUnavailable:
            logger.error(f"Playlist video '{video_title}' is unavailable: {video.watch_url}")
            await update.message.reply_text(
                f"❌ Video *{video_title}* is unavailable or private. Skipping.", parse_mode='Markdown'
            )
        except PytubeError as e:
            logger.error(f"Error processing video '{video_title}' in playlist: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Error processing video *{video_title}* in playlist: {e}. Skipping.", parse_mode='Markdown')
        except Exception as e:
            logger.error(f"An unexpected error occurred for video '{video_title}' in playlist: {e}", exc_info=True)
            await update.message.reply_text(f"🚫 An unexpected error occurred for video *{video_title}* in playlist: {e}. Skipping.", parse_mode='Markdown')
        
        # Add a small delay between videos to avoid hammering YouTube/Telegram and for better user experience
        time.sleep(2) 

    await update.message.reply_text("🥳 Playlist download attempt complete!")

# --- Main Bot Application Setup ---

def main() -> None:
    """Start the bot."""
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download_youtube_content))

    logger.info("Bot started and polling for updates...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
    
