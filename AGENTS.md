# AGENTS.md

# 🚨 CRITICAL RULES FOR TOKEN EFFICIENCY (STRICT ENFORCEMENT)

1. **NO BULK READS**: You are STRICTLY FORBIDDEN from reading entire directories (e.g., `ls -R`, `tree` without limits) or large files (>300 lines) in one go. 
2. **SEARCH FIRST**: Before reading any file, you MUST use `grep`, `search`, or `find` to locate the exact file and line numbers relevant to the task.
3. **TARGETED READS ONLY**: When using `read_file`, you MUST request specific line ranges if the file is large (e.g., "read lines 45-80 of auth.ts"). Do not dump entire files into context.
4. **TREE DEPTH LIMIT**: If you must list a directory, you MUST limit depth to 2 (e.g., `tree -L 2`). Never list `node_modules` or `dist`.
5. **DIFF-ONLY OUTPUT**: When modifying code, you MUST output ONLY `SEARCH/REPLACE` blocks or unified diffs. NEVER output the entire file content.
6. **CONCISE TOOL REASONING**: Do not quote large chunks of code in your `` `` `` thought process. Refer to files by name and line numbers (e.g., "The bug is in `auth.ts:45`").
7. **HISTORY COMPRESSION**: If a tool output (like a test result or file read) was successful, do not repeat its content in your next response. Simply state: "[Tool X succeeded, moving to next step]". If an error occurred, quote ONLY the specific error lines, not the whole stack trace.

Бот рекомендаций фильмов: Telegram-бот + Flask веб-интерфейс,
данные — Kinopoisk API, интенты/ответы — LLM (GigaChat/DeepSeek),
PostgreSQL только для админки и статистики.
Весь код, комментарии и тексты для пользователя — на русском. Сохраняй это.

README.md — черновик-концепция, а не документация; рабочие заметки в `docs/`.

## Запуск

Локальный интерпретатор: `.venv` (Python 3.10.9); Docker-образ — 3.11.

- Бот (polling): `python src\telegram_bot.py`
  НЕ `python -m src.telegram_bot` — внутри `src/` импорты плоские
  (`from dialogue_manager import ...`), с `-m` из корня падают с
  ModuleNotFoundError.
- Веб (порт 5000, админка `/admin/login`): `python src\app.py`
  (app.py сам делает chdir в src/ и правит sys.path).
- На этой машине консоль в cp1251: код с эмодзи в `print()` падает
  UnicodeEncodeError — запускай с `PYTHONIOENCODING=utf-8`.
- Тесты/линтер/тайпчекер ЕСТЬ (конфиг в `pyproject.toml`, зависимости в
  `requirements-dev.txt`): `pytest` (tests/, ~38 тестов), `ruff check .`,
  `mypy`. Запускать интерпретатором `.venv`. «Готово» = все три зелёные.
  CI проверяет только деплой (`.github/workflows/deploy.yml`), код в CI не
  тестируется. Smoke: для веба `GET /health` (`python src\app.py`,
  `PYTHONIOENCODING=utf-8`); бот — только импорт-чек, НЕ запускать polling.
- Запуск бота сразу подключается к реальному Telegram; dry-run режима нет.

## Окружение

- Нужен `.env` в корне (состав — `.env.example`). Без `GIGACHAT_AUTH_KEY`
  LLMRouter кидает ValueError при старте — приложение не поднимется.
  `KINOPOISK_API_KEY` (api.kinopoisk.dev) нужен для любого поиска,
  `TELEGRAM_BOT_TOKEN` — для бота.
- PostgreSQL: `docker compose up -d postgres`
  (postgres/postgres, БД kinobot_db). Создание таблиц и админа:
  `python init_db.py`.
- LLM: сначала GigaChat, запасной DeepSeek только при `ENABLE_DEEPSEEK=true`.

## Архитектурные ловушки

- Т.к. app.py делает chdir в src/, веб-приложение пишет логи в `src/logs/`,
  а не в корневой `logs/`.
- Модели БД продублированы: `src/models/database.py` и `init_db.py`
  (плюс flask-команда `init_db` в app.py). Меняешь схему — синхронизируй оба
  файла.
- Сессии пользователей (`session_manager.py`) и кэш поиска
  (`MovieAgent._search_cache`, TTL из `CACHE_TTL`, по умолчанию 45 с) —
  in-memory: теряются при рестарте, не разделяются между воркерами.
- Интенты (initial / info / similar / alternative) и извлечение параметров
  управляются текстовыми промптами `src/prompts/*.txt` — поведение меняется
  без правки кода.
- Текущий год захардкожен в трёх местах: `CURRENT_YEAR` в telegram_bot.py,
  `current_year` в dialogue_manager.py и movie_agent.py.
- `gigachat_client.py` намеренно отключает проверку SSL-сертификатов
  (специфика API Сбера) — не «чинить».
- Не используется кодом (легаси/утилиты): `data/processed/*.csv`,
  `src/models/movie.py`, `src/utils/code_collector.py` (дамп кода в
  «код.txt» на рабочем столе для вставки в LLM).

## Деплой

Продакшен — VPS с Ubuntu 24 (38.180.228.133), CI/CD через GitHub Actions
(`.github/workflows/deploy.yml`): релиз пушем тега `v*`, ручной запуск
и откат — `workflow_dispatch` с тегом. Образ `kinobot-ai:<тег>` собирается
на самом VPS (реестр не используется), исходники доставляются rsync,
запуск `docker compose -f docker-compose.prod.yml` (файл в репо —
`deploy/`, копируется на VPS при деплое); секреты — только на VPS
в `~/kinobot/.env.production`. Порядок и команды — `docs/dev_guide.md`.
Локальный `docker-compose.yml` с образом из реестра — только для
разработки, в продакшене не используется.

## Коммиты и пуш

Коммит и/или пуш в репозиторий — ВСЕГДА, во всех случаях, только ПОСЛЕ
локального запуска проекта и прогона тестов (верификации). Никаких
коммитов «вслепую». Порядок: изменение → локальный запуск → прогон
тестов/верификация → только затем коммит/пуш.

## Управление изменениями

Проект работает через OpenSpec (`openspec/`), команды `/opsx-*`.

## Автономная разработка (autodev)

Сквозной пайплайн без участия человека; единая точка входа — агент `autodev`
(`.opencode/agent/autodev.md`), полная спецификация процесса — скилл
`autodev-pipeline` (`.opencode/skills/autodev-pipeline/SKILL.md`).

- `/research <тема>` — deep-research → отчёт в `docs/research/<slug>-<дата>.md`
  → приоритизированный бэклог в `backlog/backlog_<дата>_<slug>.md`.
- `/backlog <описание|путь к отчёту>` — собрать бэклог (P0/P1/P2, чекбоксы) из
  пользовательского ввода или готового отчёта, без исследования и реализации.
- `/make-task <ID|P0|P1|P2|all|описание>` — итеративная реализация бэклога по
  группам приоритета со строгим гейтом (P0 → верификация → только потом P1):
  OpenSpec-спека → реализация → сборка+тесты (ruff/mypy/pytest) → ревью →
  повторные сборка+тесты; отметка чекбоксов `- [x]`/`✅` после верификации.
- Сабагенты: `researcher`, `backlog-planner`, `implementer`, `verifier`
  (только проверки, не правит код), `reviewer` (только ревью).
- Конвенции: исследования — в `docs/research/`, бэклог — в `backlog/`.
  Пайплайн НЕ делает git commit/push (см. «Коммиты и пуш»).
- MCP: `firecrawl` (глобально, исследование) и `context7` (`opencode.json`,
  документация библиотек для реализации).

