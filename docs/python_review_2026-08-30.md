# Python Code Review — весь проект Kinobot

Дата: 2026-08-30
Объём: все 26 `.py` файлов проекта (src/, tests/, scripts/, init_db.py)

## Статический анализ и тесты

- ruff 0.16.4: 2 замечания (`scripts/cleanup_stats.py` — неиспользуемый импорт `sys`, импорт не вверху файла)
- mypy 2.3.1 (`--ignore-missing-imports`): ошибок нет
- pytest: **28 passed**

## CRITICAL

Нет: SQL только через ORM/параметризованный `text()`, секретов в коде нет,
eval/exec/pickle отсутствуют.

## HIGH

### 1. Нет CSRF-защиты форм админки
`src/admin_routes.py:31,131,154`

Логин, смена пароля и email — обычные POST-формы без токена (нет Flask-WTF).
Возможны login-CSRF и смена пароля через поддельную страницу.
Решение: добавить `Flask-WTF` + `csrf_token` во все формы.

### 2. Dev-режим Flask на 0.0.0.0 с debug
`src/app.py:191`

`app.run(debug=True, host='0.0.0.0')` при `FLASK_ENV != 'production'` —
Werkzeug-отладчик даёт RCE. Защищено только значением переменной окружения.
Решение: убрать `debug` или слушать 127.0.0.1.

## MEDIUM

### 3. Лимит длины сообщения не применяется в вебе
`MESSAGE_MAX_LENGTH` проверяется только в `src/telegram_bot.py:146`;
`/chat` (`src/app.py:113`) и `dialogue_manager.process_message` лимит не проверяют.
Решение: перенести проверку в `process_message`.

### 4. Дублирование списка исключаемых жанров
`src/recommendation_engine.py:29-35` и `src/utils/movie_filter.py:58-64`.
Решение: вынести в общую константу.

### 5. Мёртвая ветка в приоритизации стран
`src/utils/movie_filter.py:17`: `elif len(country_names) > 1 and any(...)`
недостижим — первый `if` уже ловит любое совпадение с `high_priority`.
Приоритет 1 никогда не возвращается.

### 6. `print()` вместо логгера
`src/llm_router.py` (10 мест), `src/log_setup.py:64-66`.
В `llm_router` особенно критично: сообщения уходят в stdout мимо ротации логов.

### 7. `__import__('uuid')` внутри функции
`src/gigachat_client.py:46`. Решение: обычный `import uuid`.

### 8. Блокирующие синхронные вызовы БД в event loop бота
`SessionManager.get_session/save_session` (`src/session_manager.py:75,108`)
выполняются синхронно внутри `process_message`. Плюс `get_session` делает
`commit` на каждый запрос. При росте нагрузки бот будет тормозить.
Решение: `asyncio.to_thread` или асинхронный драйвер БД.

## LOW

- `datetime.utcnow()` (deprecated с Python 3.12): `src/admin_routes.py:39`,
  `src/session_manager.py`, `src/models/database.py`.
- `User.query.get()` deprecated в SQLAlchemy 2.0 — `src/app.py:65`
  (заменить на `db.session.get`).
- Нет rate-limit / блокировки после нескольких неудачных попыток на `/admin/login`.
- `_get_cache_key` (`src/movie_agent.py:20`) склеивает ключ через `_` —
  теоретические коллизии; без тайп-хинтов.
- `UserSession.dialogue_history` нигде не используется и не сохраняется в БД.
- Смешение `parse_mode='Markdown'` и `'HTML'` — `src/telegram_bot.py:111,156`
  (тексты статичны, риск минимальный).
- `scripts/cleanup_stats.py` — замечания ruff (см. выше).

## Что хорошо

- Тесты осмысленные (28, покрытие ключевых веток диалога/фильтров/кэша),
  `tests/conftest.py` изолирует секреты.
- Модели БД больше не дублируются (`init_db.py` использует `src/models/database.py`).
- `CURRENT_YEAR` централизован в `src/config.py`.
- Guardrails (`src/guardrails.py`): многоуровневая защита, закрытый перечень
  интентов, нераскрытие причины блокировки пользователю, журналирование
  фрагментами (до 120 символов).
- Секретный ключ Flask обязателен в продакшене (`src/app.py:34`).
- Ошибки статистики/БД не прерывают диалог пользователя.

## Итог

| Уровень | Количество |
|---------|-----------|
| CRITICAL | 0 |
| HIGH | 2 |
| MEDIUM | 6 |
| LOW | 6 |

**Вердикт:** до коммита в продакшен закрыть HIGH (CSRF, debug-режим);
остальное можно вести бэклогом.
