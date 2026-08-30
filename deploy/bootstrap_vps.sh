#!/usr/bin/env bash
# Первичная подготовка VPS (Ubuntu 24) для деплоя Kinobot.
# Идемпотентен: можно запускать повторно. Секретов не содержит.
# Запуск: на VPS от root — bash bootstrap_vps.sh
set -euo pipefail

DEPLOY_USER="kinobot"
DEPLOY_DIR="/home/${DEPLOY_USER}/kinobot"

if [ "$(id -u)" -ne 0 ]; then
  echo "Запускайте скрипт от root (например: sudo bash bootstrap_vps.sh)" >&2
  exit 1
fi

echo "==> Установка пакетов (Docker, Compose-плагин, rsync)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq docker.io docker-compose-v2 rsync

systemctl enable --now docker

echo "==> Пользователь ${DEPLOY_USER}"
if ! id -u "${DEPLOY_USER}" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "${DEPLOY_USER}"
  echo "Создан пользователь ${DEPLOY_USER}"
fi
usermod -aG docker "${DEPLOY_USER}"

echo "==> Структура каталогов ${DEPLOY_DIR}"
mkdir -p "${DEPLOY_DIR}/app"
chown -R "${DEPLOY_USER}:${DEPLOY_USER}" "${DEPLOY_DIR}"

echo "==> Шаблон ${DEPLOY_DIR}/.env.production"
if [ ! -f "${DEPLOY_DIR}/.env.production" ]; then
  cat > "${DEPLOY_DIR}/.env.production" <<'EOF'
# Продакшен-окружение Kinobot. Заполните значения вручную.
# FLASK
FLASK_ENV=production
FLASK_SECRET_KEY=

# LLM (GigaChat; DeepSeek включается отдельно через ENABLE_DEEPSEEK)
GIGACHAT_AUTH_KEY=
GIGACHAT_BASE_URL=https://gigachat.devices.sberbank.ru

# Kinopoisk API
KINOPOISK_API_KEY=

# Telegram
TELEGRAM_BOT_TOKEN=
BOT_MODE=polling

# Параметры выдачи
MIN_VOTES_IMDB=10000
MIN_VOTES_KP=1000
CACHE_TTL=45
LOG_LEVEL=INFO

# PostgreSQL (используется compose-файлом)
DB_USER=postgres
DB_PASSWORD=
DB_NAME=kinobot_db

# Админка (нужен для init_db.py)
ADMIN_PASSWORD=
EOF
  chmod 600 "${DEPLOY_DIR}/.env.production"
  chown "${DEPLOY_USER}:${DEPLOY_USER}" "${DEPLOY_DIR}/.env.production"
  echo "Создан шаблон .env.production — заполните значения!"
else
  echo ".env.production уже существует — не трогаем."
fi

echo "==> Готово."
echo "Дальше: заполните ${DEPLOY_DIR}/.env.production,"
echo "добавьте публичный ssh-ключ CI в /home/${DEPLOY_USER}/.ssh/authorized_keys"
echo "и ssh-ключ разработчика туда же (если ещё не добавлен)."
