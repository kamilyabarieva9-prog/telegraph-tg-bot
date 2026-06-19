import logging
import os
import re
import sys
import time
import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
TELEGRAPH_TOKEN = os.environ.get("TELEGRAPH_TOKEN", "")

STYLES = {
    "vikings": {
        "label": "Викинги",
        "title_prefix": "ᚠ ",
        "tag": "blockquote",
    },
    "medieval": {
        "label": "Средневековье",
        "title_prefix": "⚔ ",
        "tag": "em",
    },
    "official": {
        "label": "Официальный",
        "title_prefix": "",
        "tag": "p",
    },
}

sessions: dict[int, dict] = {}


def get_telegraph_token() -> str:
    global TELEGRAPH_TOKEN
    if TELEGRAPH_TOKEN:
        return TELEGRAPH_TOKEN

    log.warning("TELEGRAPH_TOKEN не задан — создаю новый аккаунт Telegraph...")
    resp = requests.get(
        "https://api.telegra.ph/createAccount",
        params={"short_name": "TgBot", "author_name": "Telegram Bot"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("ok"):
        TELEGRAPH_TOKEN = data["result"]["access_token"]
        log.warning(
            "Сохраните TELEGRAPH_TOKEN в переменные окружения: %s",
            TELEGRAPH_TOKEN,
        )
        return TELEGRAPH_TOKEN

    raise RuntimeError(f"Не удалось создать аккаунт Telegraph: {data}")


def telegraph_upload(image_bytes: bytes, filename: str = "image.jpg") -> str:
    resp = requests.post(
        "https://telegra.ph/upload",
        files={"file": (filename, image_bytes)},
        timeout=60,
    )
    resp.raise_for_status()
    result = resp.json()
    if not result or "src" not in result[0]:
        raise RuntimeError(f"Ошибка загрузки картинки: {result}")
    return "https://telegra.ph" + result[0]["src"]


def split_paragraphs(text: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]
    return parts if parts else [text.strip()]


def build_content(
    text: str,
    image_urls: list[str],
    style_key: str,
    bullet_points: bool,
) -> list[dict]:
    style = STYLES[style_key]
    paragraphs = split_paragraphs(text)
    tag = style["tag"]

    items: list[dict] = []
    img_count = len(image_urls)
    para_count = len(paragraphs)

    if img_count > 0 and para_count > 0:
        insert_after = set()
        for i in range(img_count):
            idx = min(para_count - 1, round((i + 1) * para_count / (img_count + 1)) - 1)
            if idx < 0:
                idx = 0
            insert_after.add(idx)
        insert_list = sorted(insert_after)[:img_count]
    else:
        insert_list = []

    img_idx = 0
    for i, paragraph in enumerate(paragraphs):
        if bullet_points:
            items.append({"tag": "li", "children": [paragraph]})
        else:
            items.append({"tag": tag, "children": [paragraph]})

        if img_idx < len(image_urls) and i in insert_list:
            items.append(
                {
                    "tag": "figure",
                    "children": [
                        {
                            "tag": "img",
                            "attrs": {"src": image_urls[img_idx]},
                        }
                    ],
                }
            )
            img_idx += 1

    while img_idx < len(image_urls):
        items.append(
            {
                "tag": "figure",
                "children": [
                    {
                        "tag": "img",
                        "attrs": {"src": image_urls[img_idx]},
                    }
                ],
            }
        )
        img_idx += 1

    if bullet_points:
        return [{"tag": "ul", "children": items}]
    return items


def telegraph_publish(title: str, content: list[dict]) -> str:
    token = get_telegraph_token()
    resp = requests.post(
        "https://api.telegra.ph/createPage",
        json={
            "access_token": token,
            "title": title[:256],
            "author_name": "Telegram Bot",
            "content": content,
            "return_content": False,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Ошибка публикации: {data}")
    return "https://telegra.ph/" + data["result"]["path"]


def make_title(text: str, style_key: str) -> str:
    first_line = text.strip().split("\n")[0].strip()
    prefix = STYLES[style_key]["title_prefix"]
    title = (prefix + first_line)[:256]
    return title or "Статья"


def clear_session(chat_id: int) -> None:
    sessions.pop(chat_id, None)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    clear_session(chat_id)
    sessions[chat_id] = {"text": None, "images": [], "state": "waiting_text"}
    await update.message.reply_text(
        "Привет! Отправьте текст статьи одним сообщением.\n"
        "Отмена: /cancel"
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_session(update.effective_chat.id)
    await update.message.reply_text("Отменено. Начните снова: /start")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    session = sessions.get(chat_id)

    if not session or session["state"] != "waiting_text":
        await update.message.reply_text("Сначала нажмите /start")
        return

    session["text"] = update.message.text
    session["state"] = "waiting_images"
    await update.message.reply_text(
        "Текст принят.\n\n"
        "Теперь отправьте картинки (по одной или альбомом).\n"
        "Когда всё готово — напишите /done"
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    session = sessions.get(chat_id)

    if not session or session["state"] != "waiting_images":
        await update.message.reply_text(
            "Сначала /start, затем текст, потом картинки."
        )
        return

    photo = update.message.photo[-1]
    tg_file = await context.bot.get_file(photo.file_id)
    image_bytes = await tg_file.download_as_bytearray()

    session["images"].append(bytes(image_bytes))
    count = len(session["images"])
    await update.message.reply_text(f"Картинка {count} получена. Ещё или /done")


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    session = sessions.get(chat_id)

    if not session or session["state"] != "waiting_images":
        await update.message.reply_text("Сейчас нечего завершать. Начните с /start")
        return

    if not session["text"]:
        await update.message.reply_text("Сначала отправьте текст.")
        return

    session["state"] = "waiting_style"
    keyboard = [
        [
            InlineKeyboardButton(STYLES["vikings"]["label"], callback_data="style:vikings"),
            InlineKeyboardButton(STYLES["medieval"]["label"], callback_data="style:medieval"),
        ],
        [InlineKeyboardButton(STYLES["official"]["label"], callback_data="style:official")],
    ]
    await update.message.reply_text(
        "Выберите оформление:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def on_style(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    session = sessions.get(chat_id)

    if not session or session["state"] != "waiting_style":
        await query.edit_message_text("Сессия устарела. Начните с /start")
        return

    style_key = query.data.split(":", 1)[1]
    session["style"] = style_key
    session["state"] = "waiting_bullets"

    keyboard = [
        [
            InlineKeyboardButton("Да", callback_data="bullets:yes"),
            InlineKeyboardButton("Нет", callback_data="bullets:no"),
        ]
    ]
    await query.edit_message_text(
        f"Стиль: {STYLES[style_key]['label']}\n\n"
        "Разбить текст на пункты?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def on_bullets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    session = sessions.get(chat_id)

    if not session or session["state"] != "waiting_bullets":
        await query.edit_message_text("Сессия устарела. Начните с /start")
        return

    bullet_points = query.data == "bullets:yes"
    style_key = session["style"]
    text = session["text"]
    images = session["images"]

    await query.edit_message_text("Публикую статью, подождите...")

    try:
        image_urls = []
        for i, img in enumerate(images):
            ext = "jpg"
            url = telegraph_upload(img, f"photo_{i}.{ext}")
            image_urls.append(url)

        content = build_content(text, image_urls, style_key, bullet_points)
        title = make_title(text, style_key)
        link = telegraph_publish(title, content)

        await context.bot.send_message(
            chat_id,
            f"Готово!\n\n{link}",
            disable_web_page_preview=False,
        )
    except Exception as exc:
        log.exception("Ошибка публикации")
        await context.bot.send_message(
            chat_id,
            f"Ошибка: {exc}\n\nПопробуйте снова: /start",
        )
    finally:
        clear_session(chat_id)


WEBHOOK_PATH = "tg-webhook"


def verify_telegram_token() -> str:
    resp = requests.get(
        f"https://api.telegram.org/bot{BOT_TOKEN}/getMe",
        timeout=30,
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Неверный BOT_TOKEN: {data.get('description', data)}")
    username = data["result"]["username"]
    log.info("Telegram-бот: @%s", username)
    return username


def clear_telegram_webhook() -> None:
    requests.get(
        f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook",
        params={"drop_pending_updates": True},
        timeout=30,
    )


def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(on_style, pattern=r"^style:"))
    app.add_handler(CallbackQueryHandler(on_bullets, pattern=r"^bullets:"))
    return app


def main() -> None:
    if not BOT_TOKEN:
        log.error("BOT_TOKEN не задан в переменных окружения Render")
        raise SystemExit(1)

    log.info("Python: %s", sys.version.split()[0])
    base_url = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    log.info("RENDER_EXTERNAL_URL: %s", base_url or "нет")

    try:
        verify_telegram_token()
    except Exception:
        log.exception("Проверка BOT_TOKEN не прошла")
        raise SystemExit(1)

    port = int(os.environ.get("PORT", "10000"))

    try:
        if base_url:
            webhook_url = base_url.rstrip("/") + f"/{WEBHOOK_PATH}"
            clear_telegram_webhook()

            for attempt in range(1, 6):
                app = build_app()
                try:
                    log.info(
                        "Запуск webhook, попытка %s/5, порт %s, url %s",
                        attempt,
                        port,
                        webhook_url,
                    )
                    app.run_webhook(
                        listen="0.0.0.0",
                        port=port,
                        url_path=WEBHOOK_PATH,
                        webhook_url=webhook_url,
                        drop_pending_updates=True,
                    )
                    return
                except Exception as exc:
                    log.error("Попытка %s не удалась: %s", attempt, exc)
                    if attempt < 5:
                        time.sleep(15)
                    else:
                        raise
        else:
            log.info("Режим polling (локально)")
            build_app().run_polling(drop_pending_updates=True)
    except Exception:
        log.exception("Бот упал при запуске")
        raise SystemExit(1)

if __name__ == "__main__":
    main()
