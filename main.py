import logging
import os
import time
import re
from pathlib import Path
import json # For serializing/deserializing callback data

import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError, SameFileError, UnsupportedError

from telegram import Update, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Configuration ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID_STR = os.getenv("TELEGRAM_ADMIN_ID")

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

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_TELEGRAM_FILE_SIZE_MB = 50
MAX_TELEGRAM_FILE_SIZE_BYTES = MAX_TELEGRAM_FILE_SIZE_MB * 1024 * 1024

YOUTUBE_URL_REGEX = r"(?:https?:\/\/)?(?:www\.)?(?:m\.)?(?:youtube\.com|youtu\.be)\/(?:watch\?v=|playlist\?list=|shorts\/|embed\/|v\/|)([a-zA-Z0-9_-]{11}|[a-zA-Z0-9_-]+)"

# --- User State Management ---
# Stores {chat_id: {'url': video_url, 'formats': [...], 'video_title': '...'}}
# This is an in-memory dictionary. For persistent state across restarts or
# for a highly concurrent bot, a database (e.g., SQLite, Redis) would be better.
user_states = {}

# --- Helper to map yt-dlp formats to human-readable options ---
def get_human_readable_formats(formats: list) -> list:
    """
    Analyzes yt-dlp formats and returns a list of human-readable options
    suitable for inline keyboard buttons.
    Prioritizes MP4 and common resolutions, plus audio-only.
    Returns a list of dictionaries: [{'label': '720p HD MP4', 'format_string': 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]'}, ...]
    """
    options = []
    seen_resolutions = set()
    
    # Sort formats to prioritize higher quality progressive streams
    formats.sort(key=lambda f: (f.get('height', 0), f.get('tbr', 0), f.get('ext', '')), reverse=True)

    # 1. Audio Only
    audio_formats = [f for f in formats if f.get('vcodec') == 'none']
    if audio_formats:
        # Prioritize m4a if available, otherwise best audio
        best_audio = next((f for f in audio_formats if f.get('ext') == 'm4a' and f.get('acodec')), None)
        if not best_audio:
            best_audio = max(audio_formats, key=lambda f: f.get('abr', 0), default=None)
        
        if best_audio:
            # Use 'bestaudio' for yt-dlp to pick the best audio available
            options.append({'label': '🎧 Audio Only', 'format_string': 'bestaudio'})


    # 2. Video Formats (MP4 preferred)
    for f in formats:
        if f.get('vcodec') != 'none' and f.get('ext') == 'mp4':
            height = f.get('height')
            if height and height not in seen_resolutions:
                label = f"{height}p MP4"
                format_string = f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4][height={height}]/best"
                
                if height >= 1080:
                    label += " (FHD)"
                elif height >= 720:
                    label += " (HD)"
                else: # typically 480p, 360p, 240p
                    label += " (SD)"
                
                options.append({'label': label, 'format_string': format_string})
                seen_resolutions.add(height)
    
    # If no MP4 options or just for general fallback, add a "Best Quality" option
    options.append({'label': '⚡️ Best Overall Quality', 'format_string': 'bestvideo+bestaudio/best'})

    return options


# --- Bot Command Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_html(
        f"Hi {user.mention_html()}! 👋 Send me a **YouTube video or playlist URL** and I'll try to download it for you. "
        f"Please note: For files larger than {MAX_TELEGRAM_FILE_SIZE_MB} MB, I might not be able to send them directly via Telegram.",
        reply_markup=ForceReply(selective=True),
    )
    if ADMIN_ID and update.effective_user.id == ADMIN_ID:
        await update.message.reply_text("You are recognized as the admin.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Simply send me a YouTube video or playlist URL. I'll download it and send it back if the size permits."
    )

# --- yt-dlp Progress Hook ---
def progress_hook(d: dict, update: Update, chat_id: int, logger: logging.Logger) -> None:
    if d['status'] == 'downloading':
        if d.get('total_bytes'):
            total_mb = d['total_bytes'] / (1024 * 1024)
            downloaded_mb = d['downloaded_bytes'] / (1024 * 1024)
            logger.info(f"[Download Progress] {downloaded_mb:.2f}MB / {total_mb:.2f}MB")
        elif d.get('total_bytes_estimate'):
            total_mb = d['total_bytes_estimate'] / (1024 * 1024)
            downloaded_mb = d['downloaded_bytes'] / (1024 * 1024)
            logger.info(f"[Download Progress] {downloaded_mb:.2f}MB / ~{total_mb:.2f}MB (estimated)")
        else:
            logger.info(f"[Download Progress] {d.get('downloaded_bytes', 'Unknown')} bytes downloaded.")
    elif d['status'] == 'finished':
        logger.info(f"[Download Complete] {d['filename']}")
    elif d['status'] == 'error':
        logger.error(f"[Download Error] {d.get('error', 'Unknown error')}")


# --- Core Download Logic with yt-dlp ---

async def download_youtube_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles incoming messages containing YouTube URLs and offers format selection."""
    url = update.message.text
    chat_id = update.effective_chat.id

    logger.info(f"Received URL: {url} from user {update.effective_user.id}")

    if not re.match(YOUTUBE_URL_REGEX, url):
        await update.message.reply_text(
            "❌ That doesn't look like a valid YouTube video or playlist URL. Please send a direct link to a YouTube video or playlist."
        )
        logger.warning(f"Invalid YouTube URL format: {url}")
        return

    await update.message.reply_text("⏳ Received your URL! Checking for valid YouTube content...")

    try:
        # Options to extract info without downloading
        info_ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True, # Important: just get info
            'force_generic_extractor': True, # Helps with some URLs
        }

        with yt_dlp.YoutubeDL(info_ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            if not info:
                await update.message.reply_text("❌ Could not retrieve video/playlist information. It might be unavailable or invalid.")
                logger.error(f"Failed to extract info for URL: {url}")
                return

            if info.get('_type') == 'playlist' or info.get('ie_key') == 'YoutubeTab':
                # Handle playlists: no format selection per video, just download best
                await update.message.reply_text(
                    f"🎶 Found playlist: *{info.get('title', 'Untitled Playlist')}* with {info.get('playlist_count', len(info.get('entries', [])))} videos.\n"
                    f"For playlists, I will attempt to download the best quality progressive MP4 (requires `ffmpeg` for optimal quality).",
                    parse_mode='Markdown'
                )
                await handle_download_selected_format(update, chat_id, url, is_playlist=True, format_string='bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best')
            else:
                # Handle single video: offer format selection
                available_formats = info.get('formats', [])
                video_title = info.get('title', 'Untitled Video')
                
                if not available_formats:
                    await update.message.reply_text(f"❌ No downloadable formats found for *{video_title}*.", parse_mode='Markdown')
                    logger.warning(f"No formats found for single video: {video_title} ({url})")
                    return

                human_formats = get_human_readable_formats(available_formats)
                if not human_formats:
                    await update.message.reply_text(f"❌ Could not find suitable download options for *{video_title}*.", parse_mode='Markdown')
                    logger.warning(f"No human-readable formats generated for: {video_title} ({url})")
                    return

                keyboard_buttons = []
                for fmt in human_formats:
                    # Callback data format: "format_choice|chat_id|original_url|format_string"
                    # We need chat_id to ensure the callback is from the right context
                    callback_data = json.dumps({
                        'action': 'download_format',
                        'chat_id': str(chat_id), # Store as string for JSON
                        'url': url,
                        'format': fmt['format_string']
                    })
                    if len(callback_data) > 64: # Telegram callback_data limit
                        logger.warning(f"Callback data too long, truncating: {callback_data}")
                        callback_data = callback_data[:60] + "..." # Truncate if too long (might cause issues)

                    keyboard_buttons.append([InlineKeyboardButton(fmt['label'], callback_data=callback_data)])
                
                reply_markup = InlineKeyboardMarkup(keyboard_buttons)
                
                # Store state for this user
                user_states[chat_id] = {
                    'url': url,
                    'formats_info': available_formats, # Store raw formats too if needed later
                    'video_title': video_title,
                    'message_id': None # Will be updated with the sent message_id
                }

                sent_message = await update.message.reply_text(
                    f"✨ Video found: *{video_title}*.\nPlease choose a download format:",
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
                user_states[chat_id]['message_id'] = sent_message.message_id
                logger.info(f"Sent format selection for {video_title} to chat {chat_id}")

    except DownloadError as e:
        logger.error(f"yt-dlp info extraction error for URL {url}: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ An error occurred retrieving video info: {e}. The video might be unavailable or invalid."
        )
    except ExtractorError as e:
        logger.error(f"yt-dlp Extractor Error during info extraction for URL {url}: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ Failed to extract video information: {e}. The URL might be invalid, or YouTube changed something."
        )
    except Exception as e:
        logger.error(f"An unexpected error occurred during info extraction for URL {url}: {e}", exc_info=True)
        await update.message.reply_text(
            f"🚫 An unexpected error occurred: {e}. Please try again later. "
            f"If you're the admin, check the server logs for more details."
        )


async def handle_format_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles callback queries from inline keyboard buttons for format selection."""
    query = update.callback_query
    await query.answer() # Acknowledge the query immediately

    chat_id = query.message.chat_id
    callback_data_str = query.data

    try:
        data = json.loads(callback_data_str)
        action = data.get('action')
        # Ensure the callback is for this chat's active request
        if action != 'download_format' or str(chat_id) != data.get('chat_id'):
            logger.warning(f"Mismatch in callback data or action for chat {chat_id}. Data: {data}")
            await query.edit_message_text("❌ Invalid selection or request expired.")
            return

        selected_url = data['url']
        selected_format_string = data['format']

        # Clear state immediately to prevent re-clicks
        if chat_id in user_states:
            # Try to delete the inline keyboard or edit the message
            if user_states[chat_id].get('message_id') == query.message.message_id:
                await query.edit_message_text(f"✅ You chose: `{selected_format_string}`. Starting download...", parse_mode='Markdown')
            else:
                await query.message.reply_text(f"✅ You chose: `{selected_format_string}`. Starting download...", parse_mode='Markdown')
            del user_states[chat_id]
        else:
            await query.message.reply_text("This download request has expired or was already processed. Please send a new URL.")
            return

        logger.info(f"User {chat_id} selected format: {selected_format_string} for {selected_url}")

        # Start the actual download
        await handle_download_selected_format(update, chat_id, selected_url, is_playlist=False, format_string=selected_format_string)

    except json.JSONDecodeError as e:
        logger.error(f"Failed to decode callback data: {callback_data_str}. Error: {e}")
        await query.edit_message_text("❌ An error occurred with your selection. Please try again or send a new URL.")
    except Exception as e:
        logger.error(f"Error in handle_format_selection for chat {chat_id}: {e}", exc_info=True)
        await query.edit_message_text(f"❌ An unexpected error occurred with your selection: {e}. Please try again.")


async def handle_download_selected_format(update: Update, chat_id: int, url: str, is_playlist: bool, format_string: str) -> None:
    """Handles the actual download and sending of the video/audio with the chosen format."""

    ydl_opts = {
        'format': format_string,
        'outtmpl': str(DOWNLOAD_DIR / '%(title)s.%(ext)s'),
        'noplaylist': True, # Default for single video download via selected format
        'progress_hooks': [lambda d: progress_hook(d, update, chat_id, logger)],
        'quiet': True,
        'no_warnings': True,
        'merge_output_format': 'mp4', # Default for merged video files
        'postprocessors': []
    }

    is_audio_only = False
    if 'bestaudio' in format_string or 'audio-only' in format_string: # Heuristic for audio only
        ydl_opts['format'] = 'bestaudio/best' # Ensure best audio is picked
        ydl_opts['extract_audio'] = True
        ydl_opts['audio_format'] = 'mp3' # or 'm4a'
        ydl_opts['audio_quality'] = 0 # best audio quality
        ydl_opts['postprocessors'].append({
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3', # Convert to mp3
            'preferredquality': '192', # High quality audio
        })
        ydl_opts['outtmpl'] = str(DOWNLOAD_DIR / '%(title)s.%(ext)s') # Ensure audio gets correct ext
        is_audio_only = True
        logger.info(f"Preparing to download audio only for URL: {url}")
        
    if is_playlist:
        ydl_opts['noplaylist'] = False # Allow playlist download
        ydl_opts['extract_flat'] = True # Extract playlist info first
        ydl_opts['playlistend'] = 20 # Limit for testing, remove for full playlist
        # For playlists, apply the selected format to each video (or a default best if not applicable)
        # This part of the logic needs to iterate through playlist entries and download each.
        # The current handle_download_selected_format expects a single file download context.
        # We need to replicate the playlist iteration logic here or pass the playlist object.

    downloaded_files_paths = [] # To keep track of files for cleanup

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            if is_playlist:
                # Re-extract playlist info to get entry URLs if extract_flat was used
                playlist_info = ydl.extract_info(url, download=False)
                if not playlist_info or 'entries' not in playlist_info:
                    await update.message.reply_text("❌ Could not extract videos from the playlist.")
                    logger.error(f"Failed to get entries for playlist URL: {url}")
                    return

                await update.message.reply_text(
                    f"Starting download of {len(playlist_info['entries'])} videos from *{playlist_info.get('title', 'Untitled Playlist')}*...",
                    parse_mode='Markdown'
                )

                for i, entry in enumerate(playlist_info['entries']):
                    if not entry or not entry.get('url'):
                        logger.warning(f"Skipping invalid entry in playlist: {entry}")
                        continue
                    
                    video_url = entry['url']
                    video_title = entry.get('title', f"Video {i+1}")
                    
                    await update.message.reply_text(
                        f"\n🎬 Downloading video {i+1}/{len(playlist_info['entries'])}: *{video_title}*",
                        parse_mode='Markdown'
                    )
                    logger.info(f"Attempting download for playlist item: {video_title} from {video_url}")

                    # Individual download options for playlist item
                    item_ydl_opts = ydl_opts.copy() # Start with overall options
                    item_ydl_opts['outtmpl'] = str(DOWNLOAD_DIR / f'{video_title}.%(ext)s') # Ensure unique filenames
                    item_ydl_opts['noplaylist'] = True # Only download this specific video

                    with yt_dlp.YoutubeDL(item_ydl_opts) as item_ydl:
                        item_info = item_ydl.extract_info(video_url, download=True)

                        if item_info:
                            file_path = None
                            if item_info.get('filepath'):
                                file_path = Path(item_info['filepath'])
                            elif item_info.get('_format_filepath'):
                                file_path = Path(item_info['_format_filepath'])

                            if not file_path or not file_path.exists() or file_path.stat().st_size == 0:
                                await update.message.reply_text(
                                    f"❌ Downloaded file for *{video_title}* is missing or empty. Skipping. "
                                    f"This often happens if `ffmpeg` is not installed and needed for merging video/audio. "
                                    f"If you are running this on Termux, try `pkg install ffmpeg`.",
                                    parse_mode='Markdown'
                                )
                                logger.error(f"Playlist video {video_title} file missing or empty: {file_path}")
                            else:
                                downloaded_files_paths.append(file_path)
                                await process_and_send_file(update, chat_id, video_title, file_path, is_audio_only)
                        else:
                            await update.message.reply_text(f"❌ Could not download playlist video *{video_title}*. Skipping.", parse_mode='Markdown')
                            logger.error(f"Failed to download playlist video: {video_title} from {video_url}")
                    time.sleep(2) # Delay between videos
                await update.message.reply_text("🥳 Playlist download attempt complete!")

            else: # Single video download logic
                info = ydl.extract_info(url, download=True)
                if info:
                    video_title = info.get('title', 'Untitled Video')
                    downloaded_file_path = None
                    if info.get('filepath'):
                        downloaded_file_path = Path(info['filepath'])
                    elif info.get('_format_filepath'):
                        downloaded_file_path = Path(info['_format_filepath'])

                    if not downloaded_file_path or not downloaded_file_path.exists() or downloaded_file_path.stat().st_size == 0:
                        await update.message.reply_text(f"❌ Downloaded file for *{video_title}* is missing or empty. "
                                                        f"This often happens if `ffmpeg` is not installed and needed for merging video/audio. "
                                                        f"If you are running this on Termux, try `pkg install ffmpeg`.", parse_mode='Markdown')
                        logger.error(f"Downloaded file for {video_title} not found or empty: {downloaded_file_path}")
                        return

                    downloaded_files_paths.append(downloaded_file_path)
                    await process_and_send_file(update, chat_id, video_title, downloaded_file_path, is_audio_only)
                else:
                    await update.message.reply_text("❌ Could not download video or retrieve its information.")
                    logger.error(f"No info returned after downloading single video from URL: {url}")

    finally:
        for file_path in downloaded_files_paths:
            if file_path.exists():
                os.remove(file_path)
                logger.info(f"Cleaned up temporary file: {file_path}")

async def process_and_send_file(update: Update, chat_id: int, title: str, file_path: Path, is_audio_only: bool) -> None:
    """Handles file size check and sending the video/audio to Telegram."""
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
                if is_audio_only:
                    await context.bot.send_audio(
                        chat_id=chat_id,
                        audio=f,
                        caption=f"Downloaded Audio: {title}"
                    )
                else:
                    await context.bot.send_video(
                        chat_id=chat_id,
                        video=f,
                        caption=f"Downloaded Video: {title}",
                        supports_streaming=True
                    )
            await update.message.reply_text(f"🎉 Successfully sent *{title}*!", parse_mode='Markdown')
            logger.info(f"Successfully sent {title} to chat_id {chat_id}")
        except Exception as e:
            logger.error(f"Error sending file {title} to Telegram (chat_id {chat_id}): {e}", exc_info=True)
            await update.message.reply_text(
                f"⚠️ Failed to send *{title}* via Telegram: {e}.\n"
                f"This can happen if the file format is not supported by Telegram, or due to a network issue. "
                f"Please try again or download manually.",
                parse_mode='Markdown'
            )
    else:
        await update.message.reply_text(
            f"⚠️ File *{title}* (Size: {file_size_mb:.2f} MB) is too large to send directly via Telegram (max {MAX_TELEGRAM_FILE_SIZE_MB} MB).\n"
            f"You'll need to download it manually from YouTube.",
            parse_mode='Markdown'
        )

# --- Main Bot Application Setup ---

def main() -> None:
    """Start the bot."""
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    
    # Handle incoming YouTube URLs (not commands)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download_youtube_content))
    # Handle inline keyboard button presses
    application.add_handler(CallbackQueryHandler(handle_format_selection))

    logger.info("Bot started and polling for updates...")
    logger.info(f"Admin ID (if set): {ADMIN_ID if ADMIN_ID else 'Not set'}")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
