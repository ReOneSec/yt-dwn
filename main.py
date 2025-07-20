import logging
import os
import time
from pytube import YouTube, Playlist
from pytube.exceptions import PytubeError, RegexMatchError

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
ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID") # Admin ID (optional, for future admin features)

# Validate essential configuration
if not TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN not found in .env file. Please set it.")
    exit("TELEGRAM_BOT_TOKEN is missing. Exiting.")

try:
    ADMIN_ID = int(ADMIN_ID) if ADMIN_ID else None
except ValueError:
    logger.warning(f"Invalid TELEGRAM_ADMIN_ID in .env file: {ADMIN_ID}. It should be an integer.")
    ADMIN_ID = None

# Directory to save downloaded videos temporarily
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True) # Ensure the directory exists

# Telegram's general file upload limit is around 50 MB (50 * 1024 * 1024 bytes)
MAX_TELEGRAM_FILE_SIZE_MB = 50 
MAX_TELEGRAM_FILE_SIZE_BYTES = MAX_TELEGRAM_FILE_SIZE_MB * 1024 * 1024

# --- Bot Command Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a welcome message when the /start command is issued."""
    user = update.effective_user
    await update.message.reply_html(
        f"Hi {user.mention_html()}! 👋 Send me a **YouTube video or playlist URL** and I'll try to download it for you. "
        f"Please note: For very large files, I might not be able to send them directly via Telegram.",
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

    # Basic URL validation to avoid unnecessary pytube calls
    if not (url.startswith("http://") or url.startswith("https://")) or \
       not ("youtube.com/" in url or "youtu.be/" in url):
        await update.message.reply_text(
            "❌ That doesn't look like a valid YouTube URL. Please send a direct link to a YouTube video or playlist."
        )
        return

    await update.message.reply_text("⏳ Received your URL! Checking for valid YouTube content...")

    try:
        # Check if it's a playlist URL
        if "playlist?list=" in url:
            await handle_playlist_download(update, chat_id, url)
        else:
            # Assume it's a single video URL
            await handle_single_video_download(update, chat_id, url)

    except RegexMatchError:
        # Pytube raises RegexMatchError if the URL is not a valid YouTube URL pattern
        await update.message.reply_text(
            "❌ That doesn't look like a valid YouTube video or playlist URL. Please try again with a correct link."
        )
    except PytubeError as e:
        logger.error(f"Pytube Error for URL {url}: {e}")
        await update.message.reply_text(
            f"❌ An error occurred with the YouTube content: {e}. The video/playlist might be unavailable, private, or age-restricted."
        )
    except Exception as e:
        logger.error(f"Unexpected error processing URL {url}: {e}", exc_info=True)
        await update.message.reply_text(
            f"🚫 An unexpected error occurred: {e}. Please check the URL or try again later. "
            f"If you're the admin, check the server logs for more details."
        )
        # Optionally, notify admin if an unexpected error occurs
        if ADMIN_ID and chat_id != ADMIN_ID: # Avoid sending error to admin twice if admin is the one causing it
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

    # Get the highest resolution progressive stream (video and audio combined)
    stream = yt.streams.get_highest_resolution()

    if stream:
        file_path = os.path.join(DOWNLOAD_DIR, stream.default_filename)
        
        await update.message.reply_text(f"🚀 Downloading *{yt.title}* ({stream.resolution})...", parse_mode='Markdown')
        
        try:
            stream.download(output_path=DOWNLOAD_DIR)
        except Exception as download_err:
            logger.error(f"Error during download of {yt.title}: {download_err}", exc_info=True)
            await update.message.reply_text(f"❌ Failed to download *{yt.title}*: {download_err}", parse_mode='Markdown')
            return # Stop here if download failed

        # Verify file exists and is not empty before proceeding
        if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
            await update.message.reply_text(f"❌ Downloaded file for *{yt.title}* is corrupted or empty. Cannot send.", parse_mode='Markdown')
            return

        file_size = os.path.getsize(file_path)
        file_size_mb = file_size / (1024 * 1024)
        
        await update.message.reply_text(
            f"✅ Finished downloading *{yt.title}* (Size: {file_size_mb:.2f} MB).\nAttempting to send via Telegram...",
            parse_mode='Markdown'
        )

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
            except Exception as e:
                logger.error(f"Error sending video {yt.title} to Telegram: {e}", exc_info=True)
                await update.message.reply_text(
                    f"⚠️ Failed to send *{yt.title}* via Telegram: {e}.\n"
                    f"It might still be too large or there was a network issue.",
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
    else:
        await update.message.reply_text("❌ No suitable video stream found for this URL.")

async def handle_playlist_download(update: Update, chat_id: int, url: str) -> None:
    """Downloads and sends videos from a YouTube playlist."""
    p = Playlist(url)
    await update.message.reply_text(
        f"🎶 Found playlist: *{p.title}* with {len(p.videos)} videos.\nStarting download of each video...",
        parse_mode='Markdown'
    )

    for i, video in enumerate(p.videos):
        try:
            await update.message.reply_text(
                f"\n🎬 Downloading video {i+1}/{len(p.videos)}: *{video.title}*",
                parse_mode='Markdown'
            )
            
            stream = video.streams.get_highest_resolution()
            if stream:
                file_path = os.path.join(DOWNLOAD_DIR, stream.default_filename)
                
                await update.message.reply_text(f"🚀 Downloading *{video.title}* ({stream.resolution})...", parse_mode='Markdown')
                
                try:
                    stream.download(output_path=DOWNLOAD_DIR)
                except Exception as download_err:
                    logger.error(f"Error during download of {video.title}: {download_err}", exc_info=True)
                    await update.message.reply_text(f"❌ Failed to download *{video.title}*: {download_err}", parse_mode='Markdown')
                    # Continue to next video in playlist
                    time.sleep(1) # Small delay before next video attempt
                    continue 

                # Verify file exists and is not empty
                if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
                    await update.message.reply_text(f"❌ Downloaded file for *{video.title}* is corrupted or empty. Cannot send. Skipping.", parse_mode='Markdown')
                    time.sleep(1) # Small delay
                    continue

                file_size = os.path.getsize(file_path)
                file_size_mb = file_size / (1024 * 1024)
                
                await update.message.reply_text(
                    f"✅ Finished downloading *{video.title}* (Size: {file_size_mb:.2f} MB).\nAttempting to send...",
                    parse_mode='Markdown'
                )

                if file_size <= MAX_TELEGRAM_FILE_SIZE_BYTES:
                    try:
                        with open(file_path, 'rb') as f:
                            await context.bot.send_video(
                                chat_id=chat_id,
                                video=f,
                                caption=f"Downloaded: {video.title}",
                                supports_streaming=True
                            )
                        await update.message.reply_text(f"🎉 Successfully sent *{video.title}*!", parse_mode='Markdown')
                    except Exception as e:
                        logger.error(f"Error sending video {video.title} to Telegram: {e}", exc_info=True)
                        await update.message.reply_text(
                            f"⚠️ Failed to send *{video.title}* via Telegram: {e}.\n"
                            f"It might still be too large or there was a network issue. Skipping this video.",
                            parse_mode='Markdown'
                        )
                    finally:
                        if os.path.exists(file_path):
                            os.remove(file_path) # Clean up
                            logger.info(f"Deleted temporary file: {file_path}")
                else:
                    await update.message.reply_text(
                        f"⚠️ Video *{video.title}* (Size: {file_size_mb:.2f} MB) is too large to send directly via Telegram (max {MAX_TELEGRAM_FILE_SIZE_MB} MB).\n"
                        f"Skipping direct send for this video.",
                        parse_mode='Markdown'
                    )
            else:
                await update.message.reply_text(f"❌ No suitable stream found for video: *{video.title}*. Skipping.", parse_mode='Markdown')

        except PytubeError as e:
            logger.error(f"Error processing video '{video.title}' in playlist: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Error processing video *{video.title}* in playlist: {e}. Skipping.", parse_mode='Markdown')
        except Exception as e:
            logger.error(f"An unexpected error occurred for video '{video.title}' in playlist: {e}", exc_info=True)
            await update.message.reply_text(f"🚫 An unexpected error occurred for video *{video.title}* in playlist: {e}. Skipping.", parse_mode='Markdown')
        
        # Add a small delay between videos to avoid hammering YouTube/Telegram and for better user experience
        time.sleep(2) 

    await update.message.reply_text("🥳 Playlist download attempt complete!")

# --- Main Bot Application Setup ---

def main() -> None:
    """Start the bot."""
    # Create the Application and pass your bot's token.
    application = Application.builder().token(TOKEN).build()

    # Register handlers for different commands and message types
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))

    # Register handler for text messages that are not commands (i.e., YouTube URLs)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download_youtube_content))

    # Run the bot until the user presses Ctrl-C
    logger.info("Bot started and polling for updates...")
    logger.info(f"Admin ID (if set): {ADMIN_ID if ADMIN_ID else 'Not set'}")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
    
