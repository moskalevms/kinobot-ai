# src/telegram_bot.py
import os
import logging
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler
)
from dotenv import load_dotenv
from session_manager import SessionManager
from dialogue_manager import DialogueManager
import aiohttp

load_dotenv()
from log_setup import setup_logging
logger = setup_logging("telegram")

# Инициализация менеджеров (для polling-режима)
session_manager = SessionManager()
dialogue_manager = DialogueManager(session_manager)
CURRENT_YEAR = 2025

def get_main_menu():
    keyboard = [
        [KeyboardButton("🎭 Фильм по настроению"), KeyboardButton("🏆 Топ фильмов")],
        [KeyboardButton("🎬 Поиск по жанру")],
        [KeyboardButton("🔄 Другие варианты"), KeyboardButton("💡 Помощь")],
        [KeyboardButton("🆕 Новый диалог")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

def get_top_menu():
    keyboard = [
        [KeyboardButton("🏆 Топ 50 фильмов"), KeyboardButton(f"🏆 Топ фильмов {CURRENT_YEAR}")],
        [KeyboardButton("📺 Топ 50 сериалов"), KeyboardButton(f"📺 Топ сериалов {CURRENT_YEAR}")],
        [KeyboardButton("⬅️ Назад")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "🎬 <b>Добро пожаловать в Kinobot!</b>\n"
        "Я ваш персональный кинопомощник с поддержкой живого диалога! 🤖\n"
        "<b>Что я умею:</b>\n"
        "• Подбирать фильмы по вашему настроению\n"
        "• Показывать топы и рейтинги\n"
        "• Искать по жанрам, актерам, годам\n"
        "• Запоминать контекст разговора\n"
        "• Уточнять рекомендации\n"
        "Просто напишите, что хотите посмотреть! 🍿\n"
        "Или используйте меню ниже 👇"
    )
    await update.message.reply_text(
        welcome_text,
        reply_markup=get_main_menu(),
        parse_mode='HTML'
    )

async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "💡 <b>Примеры запросов:</b>\n"
        "• <i>По настроению:</i>\n"
        "  «мне грустно», «хочу что-то веселое»\n"
        "  «посоветуй фильм когда устал»\n"
        "• <i>Топы и рейтинги:</i>\n"
        "  «топ комедий», «лучшие фильмы 2020-х»\n"
        "  «популярные триллеры»\n"
        "• <i>Поиск по параметрам:</i>\n"
        "  «комедии с Джимом Керри»\n"
        "  «французские драмы 1990-х»\n"
        "  «фильмы про космос»\n"
        "• <i>Уточнения:</i>\n"
        "  «другие варианты», «похожие фильмы»\n"
        "  «нет, это не то»\n"
        "Я запоминаю контекст нашего разговора! 🧠"
    )
    await update.message.reply_text(help_text, parse_mode='HTML')

async def handle_top_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Выберите категорию топа:", reply_markup=get_top_menu())

async def handle_genre_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Какой жанр вас интересует?\n"
        "Например: комедия, драма, боевик, триллер, ужасы, фантастика, мелодрама, приключения..."
    )

async def handle_mood_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎭 Какое у вас сейчас настроение?\n"
        "Например: *грустное*, *весёлое*, *устал*, *скучно*, *хочу адреналина*, *страшно*, *романтическое*.",
        parse_mode='Markdown'
    )

async def _process_and_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: str, query: str):
    try:
        await update.message.chat.send_action(action="typing")
        async with aiohttp.ClientSession() as http_session:
            result = await dialogue_manager.process_message(http_session, user_id, query)
        reply_markup = result.get("reply_markup")
        await update.message.reply_text(
            result["response"],
            parse_mode='HTML',
            reply_markup=reply_markup
        )
        if "movie" in result and result["movie"].get("poster_url"):
            poster_url = result["movie"]["poster_url"]
            if poster_url and poster_url.startswith("http"):
                try:
                    await update.message.reply_photo(photo=poster_url)
                except Exception as photo_err:
                    logger.warning(f"Не удалось отправить постер: {photo_err}")
    except Exception as e:
        logger.error(f"Ошибка обработки запроса '{query}': {e}", exc_info=True)
        await update.message.reply_text(
            "⚠️ Произошла ошибка. Попробуйте ещё раз или начните новый диалог.",
            reply_markup=get_main_menu()
        )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text.strip()
    user_id = str(update.effective_user.id)
    if not user_message:
        await update.message.reply_text("Пожалуйста, введите запрос.")
        return
    logger.info(f"Сообщение от {user_id}: {user_message}")

    if user_message == "🎭 Фильм по настроению":
        await update.message.reply_text(
            "🎭 Какое у вас сейчас настроение?\n"
            "Например: *грустное*, *весёлое*, *устал*, *скучно*, *хочу адреналина*, *страшно*, *романтическое*.",
            parse_mode='Markdown'
        )
        return
    elif user_message == "🏆 Топ фильмов":
        await update.message.reply_text("Выберите категорию топа:", reply_markup=get_top_menu())
        return
    elif user_message == "⬅️ Назад":
        await update.message.reply_text("Вернулись в главное меню", reply_markup=get_main_menu())
        return
    elif user_message == "🎬 Поиск по жанру":
        await update.message.reply_text(
            "Какой жанр вас интересует?\n"
            "Например: комедия, драма, боевик, триллер, ужасы, фантастика, мелодрама, приключения..."
        )
        return
    elif user_message == "🔄 Другие варианты":
        user_query = "посоветуй другие фильмы"
    elif user_message == "💡 Помощь":
        await handle_help(update, context)
        return
    elif user_message == "🆕 Новый диалог":
        dialogue_manager.clear_user_session(user_id)
        context.user_data.clear()
        await update.message.reply_text(
            "🆕 Начинаем новый диалог! История очищена.\nЧто хотите посмотреть?",
            reply_markup=get_main_menu()
        )
        return
    elif user_message == "🏆 Топ 50 фильмов":
        user_query = "топ 50 фильмов"
    elif user_message == f"🏆 Топ фильмов {CURRENT_YEAR}":
        user_query = f"топ фильмов {CURRENT_YEAR}"
    elif user_message == "📺 Топ 50 сериалов":
        user_query = "топ 50 сериалов"
    elif user_message == f"📺 Топ сериалов {CURRENT_YEAR}":
        user_query = f"топ сериалов {CURRENT_YEAR}"
    else:
        user_query = user_message

    await _process_and_reply(update, context, user_id, user_query)

async def handle_movie_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Использование: <code>/movie Название фильма</code>\n"
            "Пример: <code>/movie Начало</code>",
            parse_mode='HTML'
        )
        return
    movie_title = " ".join(context.args)
    user_id = str(update.effective_user.id)
    await _process_and_reply(update, context, user_id, f"расскажи о фильме {movie_title}")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Ошибка: {context.error}", exc_info=True)
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "⚠️ Произошла непредвиденная ошибка. Попробуйте еще раз.",
            reply_markup=get_main_menu()
        )

async def handle_movie_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("info:"):
        try:
            movie_id = int(data.split(":", 1)[1])
            async with aiohttp.ClientSession() as http_session:
                kinopoisk_client = dialogue_manager.movie_agent.kinopoisk_client
                url = f"{kinopoisk_client.base_url}/{movie_id}"
                async with http_session.get(url, headers=kinopoisk_client.headers) as resp:
                    if resp.status == 200:
                        raw = await resp.json()
                        genres = ', '.join([g['name'] for g in raw.get('genres', []) if g.get('name')])
                        countries = ', '.join([c['name'] for c in raw.get('countries', []) if c.get('name')])
                        rating_imdb = raw.get('rating', {}).get('imdb')
                        rating_kp = raw.get('rating', {}).get('kp')
                        poster_url = raw.get('poster', {}).get('url', '').strip()
                        movie = {
                            'id': raw.get('id'),
                            'title': raw.get('name') or '—',
                            'year': raw.get('year'),
                            'genre': genres,
                            'country': countries,
                            'rating': rating_imdb or rating_kp or '—',
                            'rating_imdb': rating_imdb,
                            'rating_kp': rating_kp,
                            'description': (raw.get('description') or '')[:500],
                            'poster_url': poster_url
                        }
                        desc = f"🎬 <strong>{movie['title']}</strong> ({movie['year']}) — {movie['genre']} с рейтингом {movie['rating']}.\n{movie['description']}"
                        await query.message.reply_text(desc, parse_mode='HTML')
                        if poster_url:
                            try:
                                await query.message.reply_photo(photo=poster_url)
                            except Exception as e:
                                logger.warning(f"Не удалось отправить постер: {e}")
                    else:
                        await query.message.reply_text("Не удалось загрузить информацию о фильме.")
        except (ValueError, IndexError, KeyError) as e:
            logger.warning(f"Ошибка обработки callback_data: {e}")
            await query.message.reply_text("Не удалось определить фильм.")
    else:
        await query.message.reply_text("Неизвестная команда.")

# === Функции для webhook-режима ===

def create_telegram_app() -> Application:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN не найден в .env")
    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", handle_help))
    application.add_handler(CommandHandler("movie", handle_movie_command))
    application.add_handler(CommandHandler("top", handle_top_command))
    application.add_handler(CommandHandler("genre", handle_genre_command))
    application.add_handler(CommandHandler("mood", handle_mood_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)
    application.add_handler(CallbackQueryHandler(handle_movie_detail))
    return application

def setup_webhook(app: Application, webhook_url: str):
    import asyncio
    async def _set():
        await app.bot.set_webhook(url=webhook_url)
        logger.info(f"Вебхук установлен: {webhook_url}")
    asyncio.run(_set())

# === Запуск в режиме polling ===

def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN не найден в .env")
    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", handle_help))
    application.add_handler(CommandHandler("movie", handle_movie_command))
    application.add_handler(CommandHandler("top", handle_top_command))
    application.add_handler(CommandHandler("genre", handle_genre_command))
    application.add_handler(CommandHandler("mood", handle_mood_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)
    application.add_handler(CallbackQueryHandler(handle_movie_detail))
    logger.info("Telegram бот запущен в режиме polling...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    main()