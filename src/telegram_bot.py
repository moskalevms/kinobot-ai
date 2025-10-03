# telegram_bot.py
import os
import logging
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes
)
from src.llm.dialog_agent import DialogMovieAgent
from dotenv import load_dotenv

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Инициализация агента один раз при запуске
agent = DialogMovieAgent()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка команды /start"""
    welcome_text = (
        "🎬 Привет! Я — кинопомощник.\n\n"
        "Напиши, что хочешь посмотреть:\n"
        "• «Лёгкая комедия»\n"
        "• «Фильмы с ДиКаприо»\n"
        "• «Лучшие боевики 2005 года»\n"
        "• «Расскажи о Титанике»\n\n"
        "Я подберу фильмы по твоему вкусу! 🍿"
    )
    await update.message.reply_text(welcome_text)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текстовых сообщений от пользователя"""
    user_message = update.message.text.strip()
    user_id = update.effective_user.id

    if not user_message:
        await update.message.reply_text("Пожалуйста, напиши запрос текстом.")
        return

    logger.info(f"[Telegram] Пользователь {user_id}: {user_message}")

    try:
        # Вызываем ваш агент
        result = agent.chat(user_message, history=[])
        response = result.get("response", "Извини, что-то пошло не так 😔")

        # Отправляем ответ
        await update.message.reply_text(response, parse_mode="HTML")

    except Exception as e:
        logger.error(f"[Telegram] Ошибка при обработке сообщения: {e}", exc_info=True)
        await update.message.reply_text(
            "Произошла ошибка при обработке запроса. Попробуй позже."
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Логирование ошибок"""
    logger.error(f"Update {update} вызвал ошибку {context.error}")


def main():
    """Запуск бота"""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError(
            "Токен бота не найден. Установите TELEGRAM_BOT_TOKEN в файле .env"
        )

    app = Application.builder().token(token).build()

    # Обработчики
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Обработчик ошибок
    app.add_error_handler(error_handler)

    logger.info("Telegram-бот запущен (long polling)...")
    app.run_polling()


if __name__ == "__main__":
    main()