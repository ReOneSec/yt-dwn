import logging
import os
import time
import re
from pathlib import Path # For better path handling

import yt_dlp # The new library!
from yt_dlp.utils import DownloadError, ExtractorError, SameFileError, UnsupportedError

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
ADMIN_ID_STR = os.getenv("TELEGRAM_ADMIN_ID")

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
DOWNLOAD_DIR = Path("downloads") # Using pathlib for cleaner path operations
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True) # Ensure the directory exists

# Telegram's general file upload limit is around 50 MB (50 * 1024 * 1024 bytes)
MAX_TELEGRAM_FILE_SIZE_MB = 50
MAX_TELEGRAM_FILE_SIZE_BYTES = MAX_TELEGRAM_FILE_SIZE_MB * 1024 * 1024

# Regex for YouTube URL validation (yt-dlp is very robust, but a basic check is good)
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

# --- Core Download Logic with yt-dlp ---

async def download_youtube_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles incoming messages containing YouTube URLs and initiates download."""
    url = update.message.text
    chat_id = update.effective_chat.id

    logger.info(f"Received URL: {url} from user {update.effective_user.id}")

    # Basic URL validation for YouTube URLs. yt-dlp is very robust, but a quick check helps.
    if not re.match(YOUTUBE_URL_REGEX, url):
        await update.message.reply_text(
            "❌ That doesn't look like a valid YouTube video or playlist URL. Please send a direct link to a YouTube video or playlist."
        )
        logger.warning(f"Invalid YouTube URL format: {url}")
        return

    await update.message.reply_text("⏳ Received your URL! Checking for valid YouTube content...")

    try:
        # Check if it's a playlist URL
        # yt-dlp automatically detects playlists, but we can pre-check for better messaging
        if "playlist?list=" in url or "/shorts/" in url: # Shorts can also be part of a "playlist" in yt-dlp's view
             # yt-dlp handles shorts better than pytube too
            await handle_download(update, chat_id, url, is_playlist=True)
        else:
            await handle_download(update, chat_id, url, is_playlist=False)

    except DownloadError as e:
        logger.error(f"yt-dlp Download Error for URL {url}: {e}", exc_info=True)
        # Specific check for ffmpeg error
        if "ffmpeg is not installed" in str(e):
            await update.message.reply_text(
                "❌ Download failed: `ffmpeg` is not installed on the server. "
                "For the bot to download and merge video/audio streams (which is needed for high quality), "
                "please ensure `ffmpeg` is installed. If you are running this on Termux, you can install it using:\n"
                "```bash\npkg install ffmpeg\n```",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                f"❌ A download error occurred: {e}. The video/playlist might be unavailable, private, or geo-restricted."
            )
    except ExtractorError as e:
        logger.error(f"yt-dlp Extractor Error for URL {url}: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ Failed to extract video information: {e}. The URL might be invalid, or YouTube changed something."
        )
    except Exception as e:
        logger.error(f"An unexpected error occurred processing URL {url}: {e}", exc_info=True)
        await update.message.reply_text(
            f"🚫 An unexpected error occurred: {e}. Please check the URL or try again later. "
            f"If you're the admin, check the server logs for more details."
        )
        if ADMIN_ID and chat_id != ADMIN_ID:
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"🚨 Bot Error for user {update.effective_user.mention_html()} ({update.effective_user.id}) with URL:\n`{url}`\nError: `{e}`",
                    parse_mode='HTML'
                )
            except Exception as admin_notify_err:
                logger.error(f"Failed to notify admin about error: {admin_notify_err}")


async def handle_download(update: Update, chat_id: int, url: str, is_playlist: bool) -> None:
    """Generic handler for single video or playlist download using yt-dlp."""

    # Define common yt-dlp options
    ydl_opts = {
        # Prioritize best video + best audio (requires ffmpeg for merging)
        # Fallback to best progressive MP4, then any best format
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': str(DOWNLOAD_DIR / '%(title)s.%(ext)s'), # Save to DOWNLOAD_DIR
        'noplaylist': True, # Default to single video download behavior
        'progress_hooks': [lambda d: progress_hook(d, update, chat_id, logger)], # Custom progress hook
        'quiet': True, # Suppress console output from yt-dlp unless error
        'no_warnings': True, # Suppress warnings
        'merge_output_format': 'mp4', # Ensure merged formats are mp4
    }

    if is_playlist:
        await update.message.reply_text("Retrieving playlist information...")
        ydl_opts['extract_flat'] = True # Get info without downloading individual videos yet
        ydl_opts['noplaylist'] = False # Allow playlist processing
        ydl_opts['playlistend'] = 20 # Limit playlist videos for testing/resource management (remove for full download)

    downloaded_files_paths = [] # To keep track of files for cleanup

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            if is_playlist:
                info = ydl.extract_info(url, download=False) # Extract info for the whole playlist
                if info and 'entries' in info:
                    await update.message.reply_text(
                        f"🎶 Found playlist: *{info.get('title', 'Untitled Playlist')}* with {len(info['entries'])} videos (showing up to {ydl_opts.get('playlistend', 'all')} if limited).\nStarting individual downloads...",
                        parse_mode='Markdown'
                    )
                    logger.info(f"Processing playlist: {info.get('title', 'Untitled Playlist')} ({len(info['entries'])} videos)")

                    for i, entry in enumerate(info['entries']):
                        if not entry or not entry.get('url'):
                            logger.warning(f"Skipping empty or invalid entry in playlist: {entry}")
                            continue
                        
                        video_url = entry['url']
                        video_title = entry.get('title', f"Video {i+1}")

                        await update.message.reply_text(
                            f"\n🎬 Downloading video {i+1}/{len(info['entries'])}: *{video_title}*",
                            parse_mode='Markdown'
                        )
                        logger.info(f"Attempting to download playlist video {i+1}/{len(info['entries'])}: {video_title}")

                        # Download individual video from playlist
                        # Need to create a new ydl_opts for individual video download template
                        individual_ydl_opts = ydl_opts.copy()
                        individual_ydl_opts['noplaylist'] = True # Ensure only this one video is downloaded
                        # outtmpl needs to be a callable or string. If string, it's relative to CWD, so ensure it's absolute
                        individual_ydl_opts['outtmpl'] = str(DOWNLOAD_DIR / '%(title)s.%(ext)s')

                        with yt_dlp.YoutubeDL(individual_ydl_opts) as individual_ydl:
                            # Use extract_info with download=True for individual video
                            # This will return info for the single downloaded video
                            single_video_info = individual_ydl.extract_info(video_url, download=True)
                            
                            if not single_video_info:
                                await update.message.reply_text(f"❌ Could not get info or download *{video_title}*. Skipping.", parse_mode='Markdown')
                                logger.error(f"Failed to get info or download playlist video: {video_title}")
                                time.sleep(1)
                                continue
                            
                            # Get the path of the downloaded file
                            downloaded_file_path = None
                            if single_video_info.get('filepath'):
                                downloaded_file_path = Path(single_video_info['filepath'])
                            elif single_video_info.get('_format_filepath'):
                                downloaded_file_path = Path(single_video_info['_format_filepath'])

                            if not downloaded_file_path or not downloaded_file_path.exists() or downloaded_file_path.stat().st_size == 0:
                                await update.message.reply_text(f"❌ Downloaded file for *{video_title}* is missing or empty. Skipping.", parse_mode='Markdown')
                                logger.error(f"Downloaded file for {video_title} not found or empty: {downloaded_file_path}")
                                time.sleep(1)
                                continue

                            downloaded_files_paths.append(downloaded_file_path) # Add to cleanup list
                            await process_and_send_file(update, chat_id, video_title, downloaded_file_path)
                            time.sleep(2) # Delay between videos for user experience and API limits

                    await update.message.reply_text("🥳 Playlist download attempt complete!")

                else:
                    await update.message.reply_text("❌ Could not extract any videos from the playlist.")
                    logger.error(f"No entries found in playlist info for URL: {url}")
            else:
                # Single video download process
                info = ydl.extract_info(url, download=True) # Download and get info
                if info:
                    video_title = info.get('title', 'Untitled Video')
                    # yt-dlp returns the path in 'filepath' or '_format_filepath'
                    downloaded_file_path = None
                    if info.get('filepath'):
                        downloaded_file_path = Path(info['filepath'])
                    elif info.get('_format_filepath'):
                        downloaded_file_path = Path(info['_format_filepath'])

                    if not downloaded_file_path or not downloaded_file_path.exists() or downloaded_file_path.stat().st_size == 0:
                        await update.message.reply_text(f"❌ Downloaded file for *{video_title}* is missing or empty.", parse_mode='Markdown')
                        logger.error(f"Downloaded file for {video_title} not found or empty: {downloaded_file_path}")
                        return

                    downloaded_files_paths.append(downloaded_file_path) # Add to cleanup list
                    await process_and_send_file(update, chat_id, video_title, downloaded_file_path)
                else:
                    await update.message.reply_text("❌ Could not download video or retrieve its information.")
                    logger.error(f"No info returned after downloading single video from URL: {url}")

    finally:
        # Clean up all downloaded files at the end
        for file_path in downloaded_files_paths:
            if file_path.exists():
                os.remove(file_path)
                logger.info(f"Cleaned up temporary file: {file_path}")

async def process_and_send_file(update: Update, chat_id: int, title: str, file_path: Path) -> None:
    """Handles file size check and sending the video to Telegram."""
    file_size = file_path.stat().st_size
    file_size_mb = file_size / (1024 * 1024)

    await update.message.reply_text(
        f"✅ Finished downloading *{title}* (Size: {file_size_mb:.2f} MB).\nAttempting to send via Telegram...",
        parse_mode='Markdown'
    )
    logger.info(f"Processing file for sending: {title}, Size: {file_size_mb:.2f} MB, Path: {file_path}")

    if file_size <= MAX_TELEGRAM_FILE_SIZE_BYTES:
        try:
            with open(file_path, 'rb') as f:
                await context.bot.send_video(
                    chat_id=chat_id,
                    video=f,
                    caption=f"Downloaded: {title}",
                    supports_streaming=True
                )
            await update.message.reply_text(f"🎉 Successfully sent *{title}*!", parse_mode='Markdown')
            logger.info(f"Successfully sent {title} to chat_id {chat_id}")
        except Exception as e:
            logger.error(f"Error sending video {title} to Telegram (chat_id {chat_id}): {e}", exc_info=True)
            await update.message.reply_text(
                f"⚠️ Failed to send *{title}* via Telegram: {e}.\n"
                f"This can happen if the video format is not supported by Telegram, or due to a network issue. "
                f"Please try again or download manually.",
                parse_mode='Markdown'
            )
    else:
        await update.message.reply_text(
            f"⚠️ Video *{title}* (Size: {file_size_mb:.2f} MB) is too large to send directly via Telegram (max {MAX_TELEGRAM_FILE_SIZE_MB} MB).\n"
            f"You'll need to download it manually from YouTube.",
            parse_mode='Markdown'
        )

# --- Main Bot Application Setup ---

def main() -> None:
    """Start the bot."""
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download_youtube_content))

    logger.info("Bot started and polling for updates...")
    logger.info(f"Admin ID (if set): {ADMIN_ID if ADMIN_ID else 'Not set'}")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
            
