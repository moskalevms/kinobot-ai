# src/log_setup.py
import os
import logging
from logging.handlers import RotatingFileHandler
import glob
from datetime import datetime, timedelta

LOG_DIR = "logs"
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 МБ
MAX_DAYS = 4

def setup_logging(service_name: str = "app") -> logging.Logger:
    """Настраивает ротацию логов: до 50 МБ на файл, хранение 4 дня"""
    os.makedirs(LOG_DIR, exist_ok=True)
    log_file = os.path.join(LOG_DIR, f"{service_name}.log")

    # Ротация по размеру: максимум 50 МБ, 1 backup-файл (итого 2 файла = 100 МБ максимум)
    # Но мы будем удалять старые файлы по дате, так что backup можно увеличить
    handler = RotatingFileHandler(
        log_file,
        maxBytes=MAX_FILE_SIZE,
        backupCount=10,  # достаточно для 4 дней при активной нагрузке
        encoding='utf-8'
    )
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    handler.setFormatter(formatter)

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)

    # Удаляем логи старше MAX_DAYS
    cleanup_old_logs()

    return logger

def cleanup_old_logs():
    """Удаляет лог-файлы старше MAX_DAYS"""
    cutoff = datetime.now() - timedelta(days=MAX_DAYS)
    log_files = glob.glob(os.path.join(LOG_DIR, "*.log*"))
    for file_path in log_files:
        try:
            file_time = datetime.fromtimestamp(os.path.getctime(file_path))
            if file_time < cutoff:
                os.remove(file_path)
                print(f"Удалён старый лог-файл: {file_path}")
        except Exception as e:
            print(f"Ошибка при удалении лога {file_path}: {e}")