import logging
import os
import time
import re
from pathlib import Path
import json
import uuid

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

# --- Configure httpx logger to be less verbose ---
# This stops the "HTTP Request: POST..." messages from httpx
logging.getLogger("httpx").setLevel(logging.WARNING)

# --- Configuration ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID_STR = os.getenv("TELEGRAM_ADMIN_ID")
FFMPEG_PATH = os.getenv("FFMPEG_PATH") # Get ffmpeg path from .env

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
user_states = {}

# --- YouTube Standard Resolutions for Selection ---
TARGET_RESOLUTIONS = [144, 240, 360, 480, 720, 1080, 1440, 2160]

# --- Helper to map yt-dlp formats to human-readable options ---
def get_human_readable_formats(formats: list) -> list:
    options = []
    seen_resolutions = set()
    
    formats.sort(key=lambda f: (f.get('height') or 0, f.get('tbr') or 0), reverse=True)

    # 1. Audio Only Option
    audio_formats = [f for f in formats if f.get('vcodec') == 'none']
    if audio_formats:
        best_audio_format = next((f for f in audio_formats if f.get('ext') == 'm4a' and f.get('acodec')), None)
        if not best_audio_format:
            best_audio_format = max(audio_formats, key=lambda f: f.get('abr') or 0, default=None)
        
        if best_audio_format:
            options.append({
                'callback_id': 'audio',
                'label': '🎧 Audio Only (MP3)',
                'format_string': 'bestaudio[ext=m4a]/bestaudio',
                'is_audio': True
            })

    # 2. Video Formats (prioritizing MP4 and specific resolutions)
    for target_height in TARGET_RESOLUTIONS[::-1]:
        format_string = (
            f"bestvideo[height<={target_height}][ext=mp4]+bestaudio[ext=m4a]/"
            f"best[ext=mp4][height<={target_height}]/"
            f"best[ext=mp4]"
        )
        
        if target_height not in seen_resolutions:
            label = f"{target_height}p MP4"
            if target_height >= 1080:
                label += " (FHD)"
            elif target_height >= 720:
                label += " (HD)"
            else:
                label += " (SD)"
            
            options.append({
                'callback_id': f'v{target_height}',
                'label': label,
                'format_string': format_string,
                'is_audio': False
            })
            seen_resolutions.add(target_height)

    # 3. Best Overall Quality Option
    options.append({
        'callback_id': 'best',
        'label': '⚡️ Best Overall Quality',
        'format_string': 'bestvideo+bestaudio/best',
        'is_audio': False
    })

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
        if d.get('total_bytes') and d.get('total_bytes_estimate'):
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
    """Handles incoming messages containing YouTube URLs and offers format selection for single videos."""
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
            'skip_download': True,
            'force_generic_extractor': True,
            'simulate': True,
            'get_formats': True,
            'ffmpeg_location': FFMPEG_PATH, # Explicitly set ffmpeg location
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
                    f"For playlists, I will attempt to download the best quality MP4 (requires `ffmpeg`).",
                    parse_mode='Markdown'
                )
                await handle_download_selected_format(context, chat_id, url, is_playlist=True, format_string='bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best', is_audio_only=False)
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
                current_format_options = {opt['callback_id']: opt for opt in human_formats}

                for fmt_option in human_formats:
                    callback_data = fmt_option['callback_id']
                    
                    if len(callback_data.encode('utf-8')) > 64:
                        logger.warning(f"Callback data for format '{fmt_option['label']}' is still too long ({len(callback_data.encode('utf-8'))} bytes). Skipping this option.")
                        continue

                    keyboard_buttons.append([InlineKeyboardButton(fmt_option['label'], callback_data=callback_data)])
                
                if not keyboard_buttons:
                    await update.message.reply_text(f"❌ No valid download options could be generated for *{video_title}* due to Telegram's data limits or other issues. Please try another video.", parse_mode='Markdown')
                    logger.warning(f"No keyboard buttons generated for {video_title} due to callback data length issues.")
                    return

                reply_markup = InlineKeyboardMarkup(keyboard_buttons)
                
                user_states[chat_id] = {
                    'url': url,
                    'video_title': video_title,
                    'message_id': None,
                    'format_options': current_format_options
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
        if "ffmpeg is not installed" in str(e):
            await update.message.reply_text(
                "❌ Download failed: `ffmpeg` is not installed on the server. "
                "For the bot to download and merge video/audio streams (which is needed for high quality), "
                "please ensure `ffmpeg` is installed and accessible in the system's PATH.",
                parse_mode='Markdown'
            )
        else:
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
        if ADMIN_ID and chat_id != ADMIN_ID:
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"🚨 Bot Error for user {update.effective_user.mention_html()} ({update.effective_user.id}) with URL:\n`{url}`\nError: `{e}`",
                    parse_mode='HTML'
                )
            except Exception as admin_notify_err:
                logger.error(f"Failed to notify admin about error: {admin_notify_err}")


async def handle_format_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles callback queries from inline keyboard buttons for format selection."""
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    selected_callback_id = query.data

    try:
        if chat_id not in user_states or 'format_options' not in user_states[chat_id] or selected_callback_id not in user_states[chat_id]['format_options']:
            await query.edit_message_text("This download request has expired or the format option is no longer available. Please send a new URL.")
            return

        selected_option = user_states[chat_id]['format_options'][selected_callback_id]
        selected_url = user_states[chat_id]['url']
        selected_format_string = selected_option['format_string']
        is_audio_only = selected_option['is_audio']

        if user_states[chat_id].get('message_id') == query.message.message_id:
            await query.edit_message_text(f"✅ You chose: `{selected_option['label']}`. Starting download...", parse_mode='Markdown')
        else:
            await query.message.reply_text(f"✅ You chose: `{selected_option['label']}`. Starting download...", parse_mode='Markdown')
        
        del user_states[chat_id]

        logger.info(f"User {chat_id} selected format: {selected_format_string} (Audio Only: {is_audio_only}) for {selected_url}")

        await handle_download_selected_format(context, chat_id, selected_url, is_playlist=False, format_string=selected_format_string, is_audio_only=is_audio_only)

    except Exception as e:
        logger.error(f"Error in handle_format_selection for chat {chat_id}: {e}", exc_info=True)
        await query.edit_message_text(f"❌ An unexpected error occurred with your selection: {e}. Please try again.")


async def handle_download_selected_format(context: ContextTypes.DEFAULT_TYPE, chat_id: int, url: str, is_playlist: bool, format_string: str, is_audio_only: bool) -> None:
    """Handles the actual download and sending of the video/audio with the chosen format."""

    ydl_opts = {
        'format': format_string,
        'outtmpl': str(DOWNLOAD_DIR / '%(title)s.%(ext)s'),
        'noplaylist': True,
        'progress_hooks': [lambda d: progress_hook(d, None, chat_id, logger)],
        'quiet': True, # Changed to quiet for general operation, will be overridden by verbose in some cases
        'no_warnings': True,
        'merge_output_format': 'mp4',
        'postprocessors': [],
        'ffmpeg_location': FFMPEG_PATH, # Explicitly set ffmpeg location from .env
        # Set verbose: True ONLY for debugging this specific issue. Make sure to remove it later.
        'verbose': True # TEMPORARY: Set to True for debugging. REMOVE AFTER FIXING!
    }

    if is_audio_only:
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['extract_audio'] = True
        ydl_opts['audio_format'] = 'mp3'
        ydl_opts['audio_quality'] = 0
        ydl_opts['postprocessors'].append({
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        })
        ydl_opts['outtmpl'] = str(DOWNLOAD_DIR / '%(title)s.%(ext)s')
        logger.info(f"Preparing to download audio only for URL: {url}")
        
    if is_playlist:
        ydl_opts['noplaylist'] = False
        ydl_opts['extract_flat'] = True
        ydl_opts['playlistend'] = 20
        ydl_opts['format'] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
        is_audio_only = False

    downloaded_files_paths = []

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            if is_playlist:
                playlist_info = ydl.extract_info(url, download=False)
                if not playlist_info or 'entries' not in playlist_info:
                    await context.bot.send_message(chat_id=chat_id, text="❌ Could not extract videos from the playlist.")
                    logger.error(f"Failed to get entries for playlist URL: {url}")
                    return

                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Starting download of {len(playlist_info['entries'])} videos from *{playlist_info.get('title', 'Untitled Playlist')}*...",
                    parse_mode='Markdown'
                )

                for i, entry in enumerate(playlist_info['entries']):
                    if not entry or not entry.get('url'):
                        logger.warning(f"Skipping invalid entry in playlist: {entry}")
                        continue
                    
                    video_url = entry['url']
                    video_title = entry.get('title', f"Video {i+1}")
                    
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"\n🎬 Downloading video {i+1}/{len(playlist_info['entries'])}: *{video_title}*",
                        parse_mode='Markdown'
                    )
                    logger.info(f"Attempting download for playlist item: {video_title} from {video_url}")

                    item_ydl_opts = ydl_opts.copy()
                    sanitized_title = re.sub(r'[\\/:*?"<>|]', '', video_title)
                    item_ydl_opts['outtmpl'] = str(DOWNLOAD_DIR / f'{sanitized_title}_{entry.get("id", "")}.%(ext)s') 
                    item_ydl_opts['noplaylist'] = True

                    with yt_dlp.YoutubeDL(item_ydl_opts) as item_ydl:
                        item_info = item_ydl.extract_info(video_url, download=True)

                        if item_info:
                            file_path = None
                            if item_info.get('filepath'):
                                file_path = Path(item_info['filepath'])
                            elif item_info.get('_format_filepath'):
                                file_path = Path(item_info['_format_filepath'])

                            if not file_path or not file_path.exists() or file_path.stat().st_size == 0:
                                logger.error(f"Playlist video {video_title} file missing or empty. Full yt-dlp info: {json.dumps(item_info, indent=2)}")
                                await context.bot.send_message(
                                    chat_id=chat_id,
                                    text=f"❌ Downloaded file for *{video_title}* is missing or empty. "
                                    f"This often happens if `ffmpeg` is not installed and needed for merging video/audio. "
                                    f"Please ensure `ffmpeg` is installed and accessible in your system's PATH.",
                                    parse_mode='Markdown'
                                )
                            else:
                                downloaded_files_paths.append(file_path)
                                await context.bot.send_message(chat_id=chat_id, text=f"❌ Could not download playlist video *{video_title}*. Skipping.", parse_mode='Markdown')
                            logger.error(f"Failed to download playlist video: {video_title} from {video_url}")
                    time.sleep(2)
                await context.bot.send_message(chat_id=chat_id, text="🥳 Playlist download attempt complete!")

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
                        logger.error(f"Downloaded file for {video_title} not found or empty. Full yt-dlp info: {json.dumps(info, indent=2)}")
                        await context.bot.send_message(chat_id=chat_id, text=f"❌ Downloaded file for *{video_title}* is missing or empty. "
                                                        f"This often happens if `ffmpeg` is not installed and needed for merging video/audio. "
                                                        f"Please ensure `ffmpeg` is installed and accessible in your system's PATH.", parse_mode='Markdown')
                        return

                    downloaded_files_paths.append(downloaded_file_path)
                    await process_and_send_file(context, chat_id, video_title, downloaded_file_path, is_audio_only)
                else:
                    await context.bot.send_message(chat_id=chat_id, text="❌ Could not download video or retrieve its information.")
                    logger.error(f"No info returned after downloading single video from URL: {url}")

    finally:
        for file_path in downloaded_files_paths:
            if file_path.exists():
                os.remove(file_path)
                logger.info(f"Cleaned up temporary file: {file_path}")

async def process_and_send_file(context: ContextTypes.DEFAULT_TYPE, chat_id: int, title: str, file_path: Path, is_audio_only: bool) -> None:
    """Handles file size check and sending the video/audio to Telegram."""
    file_size = file_path.stat().st_size
    file_size_mb = file_size / (1024 * 1024)

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✅ Finished downloading *{title}* (Size: {file_size_mb:.2f} MB).\nAttempting to send via Telegram...",
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
            await context.bot.send_message(chat_id=chat_id, text=f"🎉 Successfully sent *{title}*!", parse_mode='Markdown')
            logger.info(f"Successfully sent {title} to chat_id {chat_id}")
        except Exception as e:
            logger.error(f"Error sending file {title} to Telegram (chat_id {chat_id}): {e}", exc_info=True)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ Failed to send *{title}* via Telegram: {e}.\n"
                f"This can happen if the file format is not supported by Telegram, or due to a network issue. "
                f"Please try again or download manually.",
                parse_mode='Markdown'
            )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ File *{title}* (Size: {file_size_mb:.2f} MB) is too large to send directly via Telegram (max {MAX_TELEGRAM_FILE_SIZE_MB} MB).\n"
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
