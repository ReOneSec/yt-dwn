import logging
import os
from pytube import YouTube, Playlist
from pytube.exceptions import PytubeError

from telegram import Update, ForceReply
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# Replace with your actual bot token obtained from BotFather
TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"

# Directory to save downloaded videos temporarily
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /start is issued."""
    user = update.effective_user
    await update.message.reply_html(
        f"Hi {user.mention_html()}! Send me a YouTube video or playlist URL and I'll try to download it for you.",
        reply_markup=ForceReply(selective=True),
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /help is issued."""
    await update.message.reply_text("Just send me a YouTube video or playlist URL.")

async def download_youtube_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Downloads YouTube video/playlist from a given URL."""
    url = update.message.text
    chat_id = update.effective_chat.id

    await update.message.reply_text("Received your URL! Checking...")

    try:
        if "playlist?list=" in url:
            # Handle playlist download
            p = Playlist(url)
            await update.message.reply_text(f"Found playlist: *{p.title}* with {len(p.videos)} videos. Starting download...", parse_mode='Markdown')
            
            # This can be a long-running operation, consider sending progress updates
            for i, video in enumerate(p.videos):
                try:
                    await update.message.reply_text(f"Downloading video {i+1}/{len(p.videos)}: *{video.title}*", parse_mode='Markdown')
                    stream = video.streams.get_highest_resolution()
                    if stream:
                        file_path = os.path.join(DOWNLOAD_DIR, stream.default_filename)
                        stream.download(output_path=DOWNLOAD_DIR)
                        await update.message.reply_text(f"Finished downloading *{video.title}*. Attempting to send...", parse_mode='Markdown')
                        
                        # Telegram has file size limits. For large files, you'd need to upload elsewhere.
                        # For simplicity, we'll try to send if size permits.
                        # This part needs careful handling for large files.
                        if os.path.getsize(file_path) < 50 * 1024 * 1024: # 50 MB limit for general files
                             with open(file_path, 'rb') as f:
                                 await context.bot.send_video(chat_id=chat_id, video=f, caption=f"Downloaded: {video.title}")
                             os.remove(file_path) # Clean up
                        else:
                            await update.message.reply_text(f"Video *{video.title}* is too large to send directly via Telegram ({os.path.getsize(file_path)/(1024*1024):.2f} MB). You'll need to download it manually.", parse_mode='Markdown')

                    else:
                        await update.message.reply_text(f"No suitable stream found for video: *{video.title}*", parse_mode='Markdown')
                except PytubeError as e:
                    await update.message.reply_text(f"Error downloading video *{video.title}*: {e}", parse_mode='Markdown')
                except Exception as e:
                    await update.message.reply_text(f"An unexpected error occurred for video *{video.title}*: {e}", parse_mode='Markdown')
            await update.message.reply_text("Playlist download complete!")

        else:
            # Handle single video download
            yt = YouTube(url)
            await update.message.reply_text(f"Found video: *{yt.title}*. Starting download...", parse_mode='Markdown')
            
            # Get the highest resolution progressive stream
            stream = yt.streams.get_highest_resolution()
            
            if stream:
                file_path = os.path.join(DOWNLOAD_DIR, stream.default_filename)
                stream.download(output_path=DOWNLOAD_DIR)
                await update.message.reply_text(f"Finished downloading *{yt.title}*. Attempting to send...", parse_mode='Markdown')
                
                # Check file size before sending
                if os.path.getsize(file_path) < 50 * 1024 * 1024: # Example: 50 MB limit for direct send
                    with open(file_path, 'rb') as f:
                        await context.bot.send_video(chat_id=chat_id, video=f, caption=f"Downloaded: {yt.title}")
                    os.remove(file_path) # Clean up the downloaded file
                else:
                    await update.message.reply_text(f"Video *{yt.title}* is too large to send directly via Telegram ({os.path.getsize(file_path)/(1024*1024):.2f} MB). You'll need to download it manually or I can provide a link if you set up cloud storage.", parse_mode='Markdown')
            else:
                await update.message.reply_text("No suitable stream found for this video.")

    except PytubeError as e:
        await update.message.reply_text(f"Error with YouTube content: {e}")
    except Exception as e:
        await update.message.reply_text(f"An unexpected error occurred: {e}. Please check the URL or try again later.")

def main() -> None:
    """Start the bot."""
    # Create the Application and pass your bot's token.
    application = Application.builder().token(TOKEN).build()

    # On different commands - answer in Telegram
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))

    # On non-command messages - echo the message text
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download_youtube_content))

    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
