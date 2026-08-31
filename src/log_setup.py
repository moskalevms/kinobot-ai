# src/log_setup.py
import os
import logging
from logging.handlers import RotatingFileHandler
import glob
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

LOG_DIR = os.getenv("LOG_DIR", "logs")
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 МБ
MAX_DAYS = 4

# Файловый хендлер настраивается один раз на процесс (идемпотентно)
_handler_configured = False
# Активные лог-файлы процесса — их нельзя удалять при ротации
_active_log_files: set = set()


def setup_logging(service_name: str = "app") -> logging.Logger:
    """Настраивает ротацию логов: до 50 МБ на файл, хранение 4 дня.

    Хендлер корневого логгера создаётся один раз на процесс, повторные
    вызовы не добавляют дублей. Возвращает именованный логгер сервиса.
    Каталог логов задаётся переменной окружения LOG_DIR (по умолчанию
    «logs», разрешается в абсолютный путь на момент старта).
    """
    global _handler_configured
    log_dir = os.path.abspath(LOG_DIR)
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"{service_name}.log")
    _active_log_files.add(log_file)

    if not _handler_configured:
        handler = RotatingFileHandler(
            log_file,
            maxBytes=MAX_FILE_SIZE,
            backupCount=10,
            encoding='utf-8'
        )
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        handler.setFormatter(formatter)
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(handler)
        _handler_configured = True

    cleanup_old_logs(log_dir)

    return logging.getLogger(service_name)


def cleanup_old_logs(log_dir: str):
    """Удаляет лог-файлы старше MAX_DAYS, пропуская активные файлы процесса"""
    cutoff = datetime.now() - timedelta(days=MAX_DAYS)
    log_files = glob.glob(os.path.join(log_dir, "*.log*"))
    for file_path in log_files:
        if os.path.abspath(file_path) in _active_log_files:
            continue
        try:
            file_time = datetime.fromtimestamp(os.path.getctime(file_path))
            if file_time < cutoff:
                os.remove(file_path)
                logger.info(f"Удалён старый лог-файл: {file_path}")
        except Exception as e:
            logger.warning(f"Ошибка при удалении лога {file_path}: {e}")
