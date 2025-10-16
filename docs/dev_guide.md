# Запустите как модуль
python -m src.telegram_bot

### 1. Собрать образ
docker build -t kinobot-ai .

### 2. Логин в registry Cloud.ru
docker login kinobot-ai.cr.cloud.ru

### 3. Перетегирование образ
docker tag kinobot-ai kinobot-ai.cr.cloud.ru/beta/kinobot-ai:1.0.0

### 4. Пуш
docker push kinobot-ai.cr.cloud.ru/beta/kinobot-ai:1.0.0

### 5. Подключиться к VM:
ssh user1@176.108.252.72

scp docker-compose.yml user1@176.108.252.72:~/kinobot-deploy/
scp .env.production user1@176.108.252.72:~/kinobot-deploy/


docker compose -f docker-compose.prod.yml up -d




