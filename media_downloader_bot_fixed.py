import os
import re
import time
import shutil
import logging
import tempfile
import threading
import subprocess
import importlib.util
from pathlib import Path
from urllib.parse import urlparse

def ensure_package(module_name, package_name):
    if importlib.util.find_spec(module_name) is None:
        subprocess.check_call([
            __import__("sys").executable, "-m", "pip", "install",
            "--disable-pip-version-check", package_name
        ])

ensure_package("telebot", "pyTelegramBotAPI")

# Обновляем yt-dlp до последней версии при запуске
subprocess.check_call([
    __import__("sys").executable, "-m", "pip", "install", "--upgrade",
    "--no-cache-dir", "--disable-pip-version-check", "--pre", "yt-dlp[default]"
])
try:
    import yt_dlp_ejs  # noqa: F401
except ImportError:
    subprocess.check_call([
        __import__("sys").executable, "-m", "pip", "install", "--upgrade",
        "--no-cache-dir", "--disable-pip-version-check", "yt-dlp-ejs"
    ])

import telebot
from telebot import types
import yt_dlp

# ======================== CONFIG ========================
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError(
        "Не задан BOT_TOKEN. Добавь токен бота в переменные окружения облака."
    )

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "49"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
# Размер одного куска при нарезке (чуть меньше лимита Telegram)
SEGMENT_SIZE_MB = int(os.getenv("SEGMENT_SIZE_MB", "45"))

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", tempfile.gettempdir())) / "media_downloader_bot"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=8)
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("media-bot")

MODE_VIDEO = "video"
MODE_AUDIO = "audio"
user_modes = {}
user_jobs = {}
jobs_lock = threading.Lock()

VIDEO_QUALITIES = {
    "360": ("360p", "bestvideo[height<=360]+bestaudio/best[height<=360]/best"),
    "480": ("480p", "bestvideo[height<=480]+bestaudio/best[height<=480]/best"),
    "720": ("720p HD", "bestvideo[height<=720]+bestaudio/best[height<=720]/best"),
    "1080": ("1080p Full HD", "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"),
}

SUPPORTED_SCHEMES = {"http", "https"}
URL_PATTERN = re.compile(r"https?://[^\s<>()]+")


def main_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("🎬 Скачать видео", callback_data="mode:video"),
        types.InlineKeyboardButton("🎵 Скачать музыку (MP3)", callback_data="mode:audio"),
        types.InlineKeyboardButton("ℹ️ Возможности бота", callback_data="help"),
    )
    return markup


def quality_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("360p", callback_data="quality:360"),
        types.InlineKeyboardButton("480p", callback_data="quality:480"),
        types.InlineKeyboardButton("720p HD", callback_data="quality:720"),
        types.InlineKeyboardButton("1080p Full HD", callback_data="quality:1080"),
    )
    markup.add(types.InlineKeyboardButton("⬅️ В главное меню", callback_data="home"))
    return markup


def back_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⬅️ В главное меню", callback_data="home"))
    return markup


def valid_url(text):
    match = URL_PATTERN.search((text or "").strip())
    if not match:
        return None
    url = match.group(0).rstrip(".,!?;:")
    try:
        parsed = urlparse(url)
        if parsed.scheme not in SUPPORTED_SCHEMES or not parsed.hostname:
            return None
        if parsed.username or parsed.password:
            return None
        return url
    except ValueError:
        return None


def get_mode(chat_id):
    value = user_modes.get(chat_id)
    return value.get("mode") if isinstance(value, dict) else value


def is_ffmpeg_available():
    return shutil.which("ffmpeg") is not None


def clean_title(title, max_len=120):
    title = re.sub(r"[\r\n\t]+", " ", title or "media")
    title = re.sub(r'[\\/:*?"<>|]+', "_", title).strip(" ._")
    return (title or "media")[:max_len]


def progress_hook(chat_id, message_id):
    last_update = {"time": 0, "percent": ""}

    def hook(data):
        if data.get("status") != "downloading":
            return
        now = time.time()
        if now - last_update["time"] < 3:
            return
        last_update["time"] = now
        downloaded = data.get("downloaded_bytes") or 0
        total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
        if total:
            percent = f"{downloaded * 100 / total:.0f}%"
            if percent == last_update["percent"]:
                return
            last_update["percent"] = percent
            text = f"⏬ Загружаю файл… {percent}"
        else:
            text = "⏬ Загружаю файл…"
        try:
            bot.edit_message_text(text, chat_id, message_id)
        except Exception:
            pass
    return hook


def split_video(input_file: Path, workdir: Path, segment_mb: int = 45):
    """Режет видео на куски по segment_mb мегабайт без перекодирования."""
    output_pattern = str(workdir / "part_%03d.mp4")
    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_file),
        "-map", "0",
        "-c", "copy",
        "-f", "segment",
        "-segment_size", f"{segment_mb}M",
        "-reset_timestamps", "1",
        output_pattern,
    ]
    log.info("Нарезка видео: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error("FFmpeg split failed: %s", result.stderr[-1500:])
        raise RuntimeError(f"Не удалось нарезать видео: {result.stderr[-400:]}")
    parts = sorted(workdir.glob("part_*.mp4"))
    log.info("Получилось частей: %d", len(parts))
    return parts


def download_media(chat_id, url, mode, quality=None, status_message=None):
    workdir = Path(tempfile.mkdtemp(prefix=f"tg_{chat_id}_", dir=str(DOWNLOAD_DIR)))
    try:
        cookies_file = os.getenv("YTDLP_COOKIES_FILE", "").strip()
        if cookies_file and not Path(cookies_file).is_file():
            log.warning("YTDLP_COOKIES_FILE is set but the file does not exist: %s", cookies_file)
            cookies_file = ""
        if mode == MODE_AUDIO and not is_ffmpeg_available():
            bot.send_message(
                chat_id,
                "На сервере не найден FFmpeg. Установи FFmpeg на хостинге и перезапусти бота."
            )
            return

        if mode == MODE_VIDEO:
            _, format_selector = VIDEO_QUALITIES[quality]
            options = {
                "format": format_selector,
                "outtmpl": str(workdir / "%(title).120B_%(id)s.%(ext)s"),
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "restrictfilenames": True,
                "merge_output_format": "mp4",
                "socket_timeout": 30,
                "retries": 3,
                "fragment_retries": 3,
                "extractor_retries": 5,
                "file_access_retries": 3,
                "retry_sleep": {"http": 2, "fragment": 2, "extractor": 2},
                "progress_hooks": [progress_hook(chat_id, status_message.message_id)] if status_message else [],
            }
        else:
            options = {
                "format": "bestaudio/best",
                "outtmpl": str(workdir / "%(title).120B_%(id)s.%(ext)s"),
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "restrictfilenames": True,
                "socket_timeout": 30,
                "retries": 3,
                "fragment_retries": 3,
                "extractor_retries": 5,
                "file_access_retries": 3,
                "retry_sleep": {"http": 2, "fragment": 2, "extractor": 2},
                "progress_hooks": [progress_hook(chat_id, status_message.message_id)] if status_message else [],
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
            }

        if cookies_file:
            options["cookiefile"] = cookies_file

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            title = clean_title(info.get("title", "media"))

        files = [p for p in workdir.iterdir() if p.is_file() and p.stat().st_size > 0 and not p.name.startswith("part_")]
        if not files:
            raise RuntimeError("Загрузчик не создал итоговый файл.")
        media_file = max(files, key=lambda p: p.stat().st_size)
        size = media_file.stat().st_size
        size_mb = size / 1024 / 1024
        log.info("Скачано: %s (%.1f МБ)", media_file.name, size_mb)

        # ==== Если файл больше лимита — режем на части ====
        if size > MAX_UPLOAD_BYTES:
            if mode == MODE_AUDIO:
                bot.send_message(
                    chat_id,
                    f"⚠️ MP3 получился слишком большим ({size_mb:.1f} МБ) и превышает лимит Telegram. "
                    f"Попробуй другое видео или более низкое качество."
                )
                return

            if not is_ffmpeg_available():
                bot.send_message(chat_id, "⚠️ Файл большой, но FFmpeg не установлен — не могу нарезать.")
                return

            if status_message:
                try:
                    bot.edit_message_text(
                        f"✂️ Файл большой ({size_mb:.1f} МБ). Режу на части…",
                        chat_id, status_message.message_id
                    )
                except Exception:
                    pass

            parts = split_video(media_file, workdir, segment_mb=SEGMENT_SIZE_MB)
            if not parts:
                raise RuntimeError("Нарезка не дала ни одного файла.")

            total = len(parts)
            for i, part in enumerate(parts, 1):
                part_mb = part.stat().st_size / 1024 / 1024
                log.info("Отправляю часть %d/%d (%.1f МБ)", i, total, part_mb)
                if status_message:
                    try:
                        bot.edit_message_text(
                            f"📤 Отправляю часть {i}/{total}…",
                            chat_id, status_message.message_id
                        )
                    except Exception:
                        pass
                caption = f"🎬 {title}\n📦 Часть {i}/{total}"
                with part.open("rb") as file_obj:
                    bot.send_video(
                        chat_id, file_obj,
                        caption=caption[:1000],
                        supports_streaming=True,
                        timeout=300,
                    )
            bot.send_message(chat_id, "✅ Готово! Все части отправлены.", reply_markup=main_keyboard())
            return
        # ==== Конец блока нарезки ====

        if status_message:
            try:
                bot.edit_message_text("📤 Отправляю файл в Telegram…", chat_id, status_message.message_id)
            except Exception:
                pass

        caption = f"🎵 {title}" if mode == MODE_AUDIO else f"🎬 {title}\nКачество: {VIDEO_QUALITIES[quality][0]}"
        caption = caption[:1000]
        with media_file.open("rb") as file_obj:
            if mode == MODE_AUDIO:
                bot.send_audio(chat_id, file_obj, caption=caption, title=title[:64], timeout=120)
            else:
                bot.send_video(
                    chat_id, file_obj,
                    caption=caption,
                    supports_streaming=True,
                    timeout=120,
                )
        bot.send_message(chat_id, "✅ Готово! Можешь отправить ещё одну ссылку или выбрать режим.", reply_markup=main_keyboard())

    except yt_dlp.utils.DownloadError as exc:
        error_text = str(exc)
        log.warning("Download failed: %s", error_text)
        lower_error = error_text.lower()
        if "sign in to confirm" in lower_error or "not a bot" in lower_error:
            message = (
                "YouTube отклонил запрос и требует авторизацию.\n\n"
                "Это защита от ботов. Установи PO Token провайдер на сервере "
                "(bgutil-ytdlp-pot-provider) и убедись, что он запущен."
            )
        elif any(site in url.lower() for site in ("instagram.com", "tiktok.com")) and any(
            term in lower_error for term in ("login", "cookies", "private", "challenge", "403", "forbidden")
        ):
            message = (
                "Instagram/TikTok отказал в доступе. Эти сайты часто требуют авторизацию. "
                "Проверь, что ссылка на публичный ролик."
            )
        else:
            message = f"Не удалось скачать материал. Ошибка загрузчика: {error_text[:650]}"
        try:
            bot.send_message(chat_id, message, reply_markup=main_keyboard())
        except Exception:
            pass
    except Exception as exc:
        log.exception("Processing failed")
        try:
            bot.send_message(chat_id, f"⚠️ Не получилось обработать файл: {str(exc)[:700]}", reply_markup=main_keyboard())
        except Exception:
            pass
    finally:
        if status_message:
            try:
                bot.delete_message(chat_id, status_message.message_id)
            except Exception:
                pass
        shutil.rmtree(workdir, ignore_errors=True)
        with jobs_lock:
            user_jobs.pop(chat_id, None)


def start_job(chat_id, url, mode, quality=None):
    with jobs_lock:
        if chat_id in user_jobs:
            bot.send_message(chat_id, "⏳ У тебя уже выполняется загрузка. Дождись её завершения.")
            return
        user_jobs[chat_id] = True
    status = bot.send_message(chat_id, "🔎 Проверяю ссылку и доступные форматы…")
    thread = threading.Thread(
        target=download_media,
        args=(chat_id, url, mode, quality, status),
        daemon=True,
    )
    thread.start()


@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    intro = (
        "Привет! Я MediaBot — бот для скачивания видео и аудио по ссылке.\n\n"
        "Что я умею:\n"
        "🎬 Скачивать видео в доступном качестве до 1080p.\n"
        "🎚 Предлагать выбор качества: 360p, 480p, 720p HD или 1080p Full HD.\n"
        "🎵 Извлекать аудио из видео и отправлять его в MP3.\n"
        "✂️ Если видео больше 49 МБ — я разрежу его на части и отправлю по очереди "
        "(размер каждой части ~45 МБ, количество зависит от веса видео).\n"
        "🌐 Поддерживаемые сервисы:\n"
        "   • YouTube\n"
        "   • TikTok\n"
        "   • Instagram\n"
        "   • ВКонтакте (VK Видео)\n"
        "   • Rutube\n"
        "   • и другие сайты, которые поддерживает загрузчик yt-dlp.\n"
        "🧹 Удалять временные файлы после обработки.\n\n"
        "Как пользоваться:\n"
        "1. Выбери режим ниже (видео или музыка).\n"
        "2. Отправь ссылку.\n"
        "3. Для видео выбери качество, для музыки дождись готового MP3.\n\n"
        "⚠️ Внимание: если видео весит больше 49 МБ, бот нарежет его на части "
        "и отправит по очереди — количество частей зависит от размера видео.\n\n"
        "Важно: YouTube, TikTok и Instagram могут блокировать запросы "
        "и требовать авторизацию. Скачивай только те материалы, которые "
        "тебе разрешено сохранять."
    )
    bot.send_message(message.chat.id, intro, reply_markup=main_keyboard())


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message):
    bot.reply_to(
        message,
        "Текущая загрузка не может быть мгновенно остановлена этой командой. "
        "Если она уже идёт, дождись завершения.",
        reply_markup=main_keyboard(),
    )


@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    chat_id = call.message.chat.id
    data = call.data or ""
    try:
        if data == "home":
            user_modes.pop(chat_id, None)
            bot.answer_callback_query(call.id)
            bot.send_message(chat_id, "Главное меню. Что хочешь скачать?", reply_markup=main_keyboard())
        elif data == "help":
            bot.answer_callback_query(call.id)
            bot.send_message(
                chat_id,
                "🎬 Видео: отправь ссылку, затем выбери 360p / 480p / 720p / 1080p.\n"
                "🎵 Музыка: отправь ссылку, бот попробует извлечь аудио и преобразовать его в MP3.\n"
                "✂️ Большие видео (свыше 49 МБ) автоматически режутся на части по ~45 МБ. "
                "Количество частей зависит от размера видео.\n\n"
                "🌐 Поддерживаемые сервисы:\n"
                "• YouTube\n"
                "• TikTok\n"
                "• Instagram\n"
                "• ВКонтакте (VK Видео)\n"
                "• Rutube\n"
                "• и другие сайты, поддерживаемые yt-dlp.\n\n"
                "Если качество недоступно, выбери вариант ниже. Если файл превышает "
                "лимит отправки, попробуй более низкое качество. Для MP3 нужен FFmpeg.",
                reply_markup=back_keyboard(),
            )
        elif data == "mode:video":
            user_modes[chat_id] = MODE_VIDEO
            bot.answer_callback_query(call.id, "Режим видео включён")
            bot.send_message(chat_id, "🎬 Отправь ссылку на видео. После проверки я покажу варианты качества.")
        elif data == "mode:audio":
            user_modes[chat_id] = MODE_AUDIO
            bot.answer_callback_query(call.id, "Режим музыки включён")
            bot.send_message(chat_id, "🎵 Отправь ссылку на видео — я попробую извлечь аудио и отправить MP3.")
        elif data.startswith("quality:"):
            quality = data.split(":", 1)[1]
            if quality not in VIDEO_QUALITIES:
                bot.answer_callback_query(call.id, "Неизвестное качество", show_alert=True)
                return
            bot.answer_callback_query(call.id, f"Выбрано {VIDEO_QUALITIES[quality][0]}")
            pending = user_modes.get(chat_id)
            url = pending.get("url") if isinstance(pending, dict) else None
            if not url:
                bot.send_message(chat_id, "Сначала отправь ссылку на видео.", reply_markup=main_keyboard())
                return
            user_modes[chat_id] = MODE_VIDEO
            start_job(chat_id, url, MODE_VIDEO, quality)
        else:
            bot.answer_callback_query(call.id)
    except Exception:
        log.exception("Callback failed")
        try:
            bot.answer_callback_query(call.id, "Произошла ошибка", show_alert=True)
        except Exception:
            pass


@bot.message_handler(content_types=["text"])
def handle_text(message):
    text = (message.text or "").strip()
    if text.startswith("/"):
        return
    url = valid_url(text)
    if not url:
        bot.send_message(
            message.chat.id,
            "Отправь полную ссылку, начинающуюся с http:// или https://, или выбери режим в меню.",
            reply_markup=main_keyboard(),
        )
        return

    mode = get_mode(message.chat.id)
    if not mode:
        bot.send_message(
            message.chat.id,
            "Сначала выбери, что нужно скачать:",
            reply_markup=main_keyboard(),
        )
        return

    if mode == MODE_VIDEO:
        user_modes[message.chat.id] = {"mode": MODE_VIDEO, "url": url}
        bot.send_message(
            message.chat.id,
            "Выбери максимальное качество. Если оно недоступно, загрузчик подберёт доступный формат.",
            reply_markup=quality_keyboard(),
        )
    elif mode == MODE_AUDIO:
        start_job(message.chat.id, url, MODE_AUDIO)
    else:
        bot.send_message(message.chat.id, "Выбери режим в главном меню.", reply_markup=main_keyboard())


def run_bot():
    log.info("MediaBot запущен; yt-dlp version: %s", yt_dlp.version.__version__)
    while True:
        try:
            bot.infinity_polling(timeout=25, long_polling_timeout=25, skip_pending=True)
            log.warning("Polling завершился; повторное подключение через 3 секунды")
        except KeyboardInterrupt:
            log.info("Бот остановлен вручную")
            break
        except Exception:
            log.exception("Ошибка Telegram-соединения; повтор через 5 секунд")
            time.sleep(5)


if __name__ == "__main__":
    run_bot()
