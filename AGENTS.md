# AGENTS.md

# 🚨 CRITICAL RULES FOR TOKEN EFFICIENCY (STRICT ENFORCEMENT)

1. **NO BULK READS**: don't dump entire directories or files >300 lines.
2. **SEARCH FIRST**: `grep`/`find` exact file+line before reading.
3. **TARGETED READS ONLY**: request specific line ranges of large files.
4. **TREE DEPTH LIMIT**: `tree -L 2` max; never list `node_modules`/`dist`.
5. **DIFF-ONLY OUTPUT**: output SEARCH/REPLACE blocks or diffs, never whole files.
6. **CONCISE TOOL REASONING**: cite files as name+line (`auth.ts:45`), no big code quotes.
7. **HISTORY COMPRESSION**: don't repeat successful output; on errors quote only failing lines.

Бот рекомендаций фильмов: Telegram-бот + Flask веб-интерфейс,
данные — Kinopoisk API, интенты/ответы — LLM (GigaChat/DeepSeek),
PostgreSQL только для админки и статистики.
Весь код и тексты — на русском, сохраняй это.
README.md — черновик-концепция; рабочие заметки в `docs/`.

## Запуск

Интерпретатор: `.venv` (Python 3.10.9); Docker — 3.11.

- Бот (polling): `python src\telegram_bot.py`.
  НЕ `python -m src.telegram_bot` — импорты в `src/` плоские,
  `-m` падает ModuleNotFoundError.
- Веб (порт 5000, админка `/admin/login`): `python src\app.py`
  (app.py сам делает chdir в src/).
- Консоль cp1251: код с эмодзи в `print()` падает UnicodeEncodeError —
  запускай с `PYTHONIOENCODING=utf-8`.
- Тесты/линтер/тайпчекер (конфиг `pyproject.toml`, зависимости
  `requirements-dev.txt`): `pytest` (tests/), `ruff check .`, `mypy`
  интерпретатором `.venv`. «Готово» = все три зелёные. CI проверяет
  только деплой (`.github/workflows/deploy.yml`).
- Smoke: веб — `GET /health`; бот — только импорт-чек, НЕ запускать
  polling (реальный Telegram, dry-run нет).

## Окружение

- Нужен `.env` в корне (состав — `.env.example`). Без `GIGACHAT_AUTH_KEY`
  LLMRouter кидает ValueError при старте.
  `KINOPOISK_API_KEY` (api.kinopoisk.dev) — для поиска,
  `TELEGRAM_BOT_TOKEN` — для бота.
- PostgreSQL: `docker compose up -d postgres`
  (postgres/postgres, БД kinobot_db). Таблицы и админ:
  `python init_db.py`.
- LLM: сначала GigaChat, запасной DeepSeek только при `ENABLE_DEEPSEEK=true`.

## Архитектурные ловушки

- app.py chdir в src/ → веб-логи в `src/logs/`, не в корневом `logs/`.
- Модели БД продублированы: `src/models/database.py` и `init_db.py`
  (+ flask-команда `init_db` в app.py). Меняешь схему — синхронизируй оба.
- Сессии (`session_manager.py`) и кэш поиска (`MovieAgent._search_cache`,
  TTL `CACHE_TTL`, по умолчанию 45 с) — in-memory: теряются при рестарте,
  не разделяются между воркерами.
- Интенты (initial/info/similar/alternative) и извлечение параметров —
  промпты `src/prompts/*.txt`; поведение меняется без правки кода.
- Текущий год в 3 местах: `CURRENT_YEAR` (telegram_bot.py),
  `current_year` (dialogue_manager.py, movie_agent.py).
- `gigachat_client.py` намеренно отключает проверку SSL (специфика
  API Сбера) — не «чинить».
- Не используется кодом (легаси): `data/processed/*.csv`,
  `src/models/movie.py`, `src/utils/code_collector.py`.

## Деплой

Продакшен — VPS Ubuntu 24 (38.180.228.133), CI/CD — GitHub Actions:
релиз пушем тега `v*`, ручной запуск и откат — `workflow_dispatch`
с тегом; образ собирается на VPS, секреты — в `~/kinobot/.env.production`.
Порядок и команды — `docs/dev_guide.md`.

## Коммиты и пуш

Коммит/пуш — ВСЕГДА только ПОСЛЕ локального запуска и прогона тестов,
никаких коммитов «вслепую». Порядок: изменение → запуск → тесты →
коммит/пуш.

## Управление изменениями

Проект работает через OpenSpec (`openspec/`), команды `/opsx-*`.

## Автономная разработка (autodev)

Спецификация процесса — скилл `autodev-pipeline`
(`.opencode/skills/autodev-pipeline/SKILL.md`); точка входа — агент `autodev`.

- `/research <тема>` — отчёт в `docs/research/` → бэклог в `backlog/`;
  `/backlog <описание|отчёт>` — бэклог P0/P1/P2 с чекбоксами;
  `/make-task <ID|P0|P1|P2|all>` — реализация по группам приоритета
  с гейтом (P0 → верификация → P1); `/resume` — возобновление по
  чекпоинту `backlog/state/autodev-state.json`.
- `autodev` — диспетчер, на каждую задачу спавнит НОВУЮ сессию-воркера
  `autodev-worker`; сабагенты: researcher, backlog-planner, implementer,
  verifier, reviewer.
- Пути: исследования — `docs/research/`, бэклог — `backlog/`,
  рантайм-состояние/логи/отчёты — `backlog/state/`. Пайплайн НЕ делает
  git commit/push.

## Контекст и токены

Отчёт `docs/research/token-economy-llm-2026-09-18.md` §2.4/§2.8/§3.1/§6:
1. Новая сессия на каждую задачу (в т.ч. ручные; в autodev уже так).
2. Контекст заполнен >70% → `/compact`, >80% → только новая сессия.
3. Не править AGENTS.md/конфиги в активной серии: кэш префиксный
   (правка инвалидирует дальше), TTL explicit-кэша Qwen 5 мин;
   правки — между сессиями.
4. Статический контент — в начало, динамический — в конец.
5. Еженедельно `opencode stats`; нормативы — `docs/token-budget.md`.
