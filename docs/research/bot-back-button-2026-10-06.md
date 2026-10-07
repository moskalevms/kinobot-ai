# Кнопка «Назад» в каждом разделе Telegram-бота: исследование целесообразности

*Дата: 2026-10-06 | Источников: 15 (внешних) + кодовая база | Уверенность: Высокая*

## Executive Summary

Идея «кнопка Назад в каждом разделе, кроме стартового» **хорошая по цели, но уже на ~80% реализована** — в боте сложилась hub-and-spoke навигация (звезда с центром в главном меню), и почти все разделы уже имеют «⬅️ Назад» → главное меню (подменю топов `top:menu`, меню жанров `genre:menu`, выбор настроения `menu:main`, карточка → «⬅️ К списку» `back:list`, панель оценки → `info:{id}`). Реальные пробелы — только три экрана: **страницы watchlist, непустая статистика `/stats` (вообще без клавиатуры) и список выдачи фильмов** (нет выхода в меню). Рекомендация: закрыть эти пробелы точечно кнопкой «🏠 Меню» (callback `menu:main` уже маршрутизирован), **НЕ строя стек истории переходов**. Стековая модель «возврата в предыдущий раздел» противоречит архитектуре бота: состояния «текущий раздел» не существует (интенты классифицирует LLM по каждому сообщению, сессия хранит только `last_movies`/`last_params`), а внешний стек потребовал бы изменения схемы БД (синхронно в двух моделях — ловушка AGENTS.md), борьбы с устаревшими кнопками и повторных API-вызовов при возврате. Точечный вариант — низкая сложность (2 файла, без БД, без кэша, без веб-интерфейса), соответствует лучшим практикам (GramIO: «каждый не-домашний экран должен иметь выход без ввода команды»; escape hatch — стандарт индустрии чат-ботов).

---

## 1. Текущая архитектура навигации бота (локальный анализ)

### 1.1 Нет конечного автомата «разделов» — гибридная модель

- Интенты (`initial`/`info`/`similar`/`alternative`/`offtopic`) классифицируются **на каждое текстовое сообщение** через `IntentClassifier` (LLM + regex-fallback) в `DialogueManager.process_message` (`dialogue_manager.py:917-1030`). «Текущий раздел» нигде не хранится — состояние диалога выводится из содержимого сессии, а не из FSM.
- `ConversationHandler`/FSM из python-telegram-bot **не используется** — все callback'и маршрутизируются таблицей префиксов `_CALLBACK_ROUTES` (`telegram_bot.py:2386-2423`): `info:`, `alt:`, `similar:`, `back:`, `random:`, `mood:`, `retry:`, `page:`, `save:`/`unsave:`/`watchlist:`/`wpage:`, `fb:`, `menu:`, `top:`, `genre:`.
- Сессия (`session_manager.py:17-35`) хранит только `last_movies` и `last_params`;персистентность — PostgreSQL с in-memory фолбэком (`session_manager.py:38-52`; примечание: AGENTS.md описывает сессии как чисто in-memory — документация устарела). **Стека/истории переходов нет.** Снапшот страницы списка живёт в `context.user_data` (in-memory, теряется при рестарте/на другом воркере — `telegram_bot.py:1534-1541`).

### 1.2 Главное меню — хаб (hub-and-spoke)

`get_main_menu()` (`telegram_bot.py:642-670`): inline-клавиатура с callback'ами `menu:mood`, `menu:top`, `menu:genre`, `menu:watchlist`, `menu:random`, `menu:new`, `menu:help`, `menu:stats`. Reply-клавиатуры удалены решением B8 (`telegram_bot.py:507-515`). Обработчик `menu:main` (возврат в главное меню) уже существует — `_handle_menu_callback` (`telegram_bot.py:1773`), используется как цель кнопки «⬅️ Назад» из меню настроения (`telegram_bot.py:738`).

### 1.3 Инвентарь экранов: где «Назад» уже есть, где нет

| Экран (раздел) | Выход назад сейчас | Callback | Где код |
|---|---|---|---|
| Онбординг `/start` | — (стартовый, по условию задачи не нужен) | — | `telegram_bot.py:272-311` |
| Главное меню | хаб, назад не нужен | — | `telegram_bot.py:642` |
| Меню настроения | ✅ «⬅️ Назад» → главное меню | `menu:main` | `telegram_bot.py:738` |
| Подменю топов | ✅ «⬅️ Назад» → главное меню | `top:menu` | `telegram_bot.py:690, 1858` |
| Меню жанров | ✅ «⬅️ Назад» → главное меню | `genre:menu` | `telegram_bot.py:788, 1923` |
| Справка `/help` | ✅ клавиатура = главное меню | `menu:*` | `telegram_bot.py:853` |
| Карточка фильма | ✅ «⬅️ К списку» (пересборка списка из `session.last_movies` + `_edit_or_send`) | `back:list` | `dialogue_manager.py:119-120, 606`; `telegram_bot.py:1523-1573` |
| Панель оценки 1–10 | ✅ «⬅️ Назад» → карточка | `info:{id}` | `dialogue_manager.py:216, 676` |
| **Список выдачи фильмов** | ❌ только «🔄 Другие» (`alt:list`), «⬇️ Ещё 5» (`page:`), «🎲» — выхода в меню нет | — | `dialogue_manager.py:1526-1540` |
| **Watchlist (страницы и пустое состояние)** | ❌ только «🗑️ Удалить», «⬇️ Ещё 5», «🎲 Случайный» | — | `dialogue_manager.py:738-791` |
| **Статистика `/stats` с данными** | ❌ клавиатуры нет вовсе (`markup=None`); у пустой заглушки — `build_exit_keyboard()` | — | `telegram_bot.py:465-504` |
| Ошибки/неизвестный callback | ✅ «🔄 Повторить» + «⬅️ К списку»/«🎲» (политика B7 «без dead-end») | `retry:`, `back:list`, `random:movie` | `telegram_bot.py:345-431, 2459-2466` |

Вывод: политика «без тупиков» (B7) в проекте уже декларируется и в основном реализована; «кнопка Назад везде» как новая фича — это фактически **закрытие трёх оставшихся пробелов**.

### 1.4 Семантика существующего «Назад» — контекстная, не стековая

- `back:list` не «предыдущий экран», а «пересобрать список из сессии»: при потерянной сессии — дружелюбное сообщение + главное меню (`telegram_bot.py:1551-1563`); страница восстанавливается из снапшота при совпадении hash (`telegram_bot.py:1534-1541, 1564-1568`).
- `get_random_movie` сохраняет показанный фильм как `movies_list=[movie]` в сессию (`dialogue_manager.py:1613-1615, 1643-1644`), поэтому «⬅️ К списку» из случайной карточки не ломается — семантика согласована (проверено по коду).
- Кэш `MovieAgent._search_cache` (TTL 45 с, `config.py:17`, `movie_agent.py:24, 70-76`) в возвратах **не участвует**: `back:list`/`page:` читают `session.last_movies` (БД), что явно переживает TTL кэша (`telegram_bot.py:1579-1581`). Возврат в меню кэша не затрагивает вообще.

### 1.5 Связанные артефакты проекта

- Предыдущее исследование UX: `docs/research/research_uiux_telegram.md` — рекомендации №5 («редактировать сообщение при callback + кнопка "⬅️ К списку"») и №15 (перевод меню на префиксные callback) уже внедрены (фазы A/B/C/T по `backlog/backlog_2026-09-16_uiux_telegram.md`).
- В бэклогах (`backlog_2026-09-16_uiux_telegram.md`, `backlog_2026-10-04_bugfix_telegram_ui.md`) отдельной задачи «Назад во всех разделах» нет; задача T6 (меню жанров) делала «Назад» по паритету с `top:menu` — прецедент точечного закрытия пробелов, а не стека.

---

## 2. Хорошая ли идея? Анализ ЗА/ПРОТИВ

### 2.1 ЗА (в форме «выход из каждого экрана»)

1. **Правило «без тупиков» — базовая норма Telegram-UX.** GramIO (гайд UX-паттернов, прочитан полностью): «If a user can reach a dead end, add a back button»; чек-лист: «Every non-home screen has a Back button» ([gramio.dev/guides/ux-patterns](https://gramio.dev/guides/ux-patterns)). Экраны watchlist/статистика/список выдачи — формальные тупики (выход только командой или свободным текстом).
2. **Escape hatch — стандарт индустрии чат-ботов.** «A good chatbot always offers escape routes… a way to return to the main menu» ([Social Intents](https://www.socialintents.com/blog/chatbot-conversation-flow/)); «Always Provide an Escape Hatch: persistent option like "Main Menu" at every stage» ([Leadblaze](https://leadblaze.ai/blog/chat-bot-best-practices)); persistent-меню для перезапуска потока ([Musemind](https://musemind.agency/blog/chatbot-design-process)).
3. **Дешевизна в существующей архитектуре.** Callback `menu:main` уже маршрутизирован (`telegram_bot.py:2415, 1773`) — новая кнопка в трёх рендерерах не требует ни новых префиксов, ни БД, ни сессий.
4. **Консистентность.** Пользователь уже видит «⬅️ Назад» в mood/top/genre-меню; отсутствие выхода в watchlist/статистике — та самая «inconsistent back targets»-проблема, которую GramIO называет антипаттерном ([gramio.dev](https://gramio.dev/guides/ux-patterns)).

### 2.2 ПРОТИВ (в форме «стек истории / возврат в произвольный предыдущий раздел»)

1. **Не на чем строить стек.** «Предыдущий раздел» не определён: FSM нет, интент выводится LLM'ом из текста (`dialogue_manager.py:982-992`). В PTB-сообществе канонический ответ — «back = явный возврат в предыдущее состояние ConversationHandler» ([PTB discussion #2949](https://github.com/python-telegram-bot/python-telegram-bot/discussions/2949), прочитан полностью); без FSM стек придётся персистить вручную.
2. **Стек-модель требует новой персистентности** → поле истории в сессии → правка схемы БД **в двух местах** (`src/models/database.py` + `init_db.py`, ловушка AGENTS.md) → миграция. Это несоразмерно ценности для бота с глубиной навигации ≤3.
3. **Непредсказуемый «Назад» хуже его отсутствия.** Smashing Magazine (прочитан в извлечениях): пользователи боятся терять состояние; «Back» должен вести туда, куда пользователь ожидает; при риске потери данных нужен confirm ([smashingmagazine.com](https://www.smashingmagazine.com/2022/08/back-button-ux-design/)). Возврат «в предыдущий раздел» в этом боте часто означал бы повторный LLM/Kinopoisk-запрос (кэш 45 с к тому моменту обычно протух) — другой результат, другая страница; ожидание пользователя обмануто.
4. **Кнопочная навигация конфликтует со свободным диалогом.** «Chatbot buttons force intents on the users by only allowing them to go down specific topic flows» ([OvationCXM](https://www.ovationcxm.com/blog/chatbot-nlp-vs-buttons)); menu-driven боты — отдельный класс с фиксированным деревом ([обзор чат-ботов, PMC](https://pmc.ncbi.nlm.nih.gov/articles/PMC10644239/)). kinobot — гибридный (текст + кнопки); жёсткий стек превратил бы LLM-диалог в IVR.
5. **Устаревшие кнопки.** Telegram-история сообщений перманентна: стековые callback'и («назад на шаг 3 из 5») в старых сообщениях протухают; существующий код уже вынужден защищаться от stale-callback'ов hash'ем списка (`telegram_bot.py:1621-1626`). Стек умножил бы эту проблему.

### 2.3 Альтернативы из внешней практики

- **«🏠 Home/Главное меню» вместо «Back»** на экранах глубже 2 уровней — прямая рекомендация GramIO: «If a user has drilled three levels deep, don't make them tap Back three times» ([gramio.dev](https://gramio.dev/guides/ux-patterns)). В kinobot глубина максимум 3 (меню → список → карточка), поэтому звезды «hub + контекстный back» достаточно.
- **Android back stack** — эталон стековой модели: стек назначений, фиксированный стартовый экран, Up ≠ Back ([developer.android.com](https://developer.android.com/guide/navigation/principles)). Применим к приложению с экранами, но не к чату, где «экраны» — сообщения в ленте; в Telegram аналог стека эмулируют edit-in-place (GramIO §3) — что бот уже делает (`_edit_or_send`).
- **Breadcrumbs** (`home › settings`) в тексте сообщения (GramIO §4) — опциональное дешёвое улучшение заголовков, но в чат-ленте малополезно.
- **Фреймворочные стеки** (aiogram Dialog/Scenes «back built in», [telega-gleam](https://github.com/bondiano/telega-gleam), [aiogram FSM + ручной стек](https://medium.com/sp-lutsk/exploring-finite-state-machine-in-aiogram-3-a-powerful-tool-for-telegram-bot-development-9cd2d19cfae9), [tg_tree_wizard](https://github.com/i1obody/tg_tree_wizard)) — существуют, но миграция kinobot на FSM ради кнопки «назад» — смена парадигмы, не фича. tg_tree_wizard подтверждает: «кнопка "Назад" и лимит 64 байта почти неизбежно всплывают как баги» при ручном построении деревьев меню.

---

## 3. UX-паттерны и ограничения Telegram Bot API

- **callback_data: 1–64 байта UTF-8** (`MAX_CALLBACK_DATA = 64`) — [PTB InlineKeyboardButton docs](https://docs.python-telegram-bot.org/en/stable/telegram.inlinekeyboardbutton.html) (зеркало спецификации Bot API; core.telegram.org был недоступен для скрейпинга в момент исследования). Проект уже соблюдает лимит (константы и комментарии `dialogue_manager.py:161, 197, 582`); `menu:main` = 9 байт — запас огромный. Схемы упаковки payload в 64 байта — типовая практика ([cursorremote architecture doc](https://github.com/len5ky/cursorremote/blob/f8552122e0639f328e87fa9d6479ea19116c2846/docs/telegram_architecture.md)).
- **Текст кнопки ≤64 символа** — в проекте есть `truncate_button_text` (`dialogue_manager.py:261`).
- **Edit-in-place для навигации, новые сообщения — для событий** (GramIO §3): «Where am I» всегда внизу чата. Бот следует (`_edit_or_send`, A5), кроме сервисных ответов (`telegram_bot.py:1339-1355`).
- **`answer()` на каждый callback** (GramIO §7) — в боте выполняется (`telegram_bot.py:2448`).
- **Empty states с CTA** (GramIO §9) — в боте выполняется (пустой watchlist, пустая статистика).
- **Раскладка кнопок**: 2–3 кнопки в ряд для длинных подписей, «Back» — последний ряд (GramIO §13, §4) — существующие меню бота соответствуют.
- Каждый callback должен иметь **короткий стабильный префикс** — паттерн подтверждён несколькими источниками ([BotNameFinder guide](https://botnamefinder.com/blog/telegram-inline-keyboard-builder-guide); собственный `docs/research/research_uiux_telegram.md` п.92).

---

## 4. Техническая сложность и затронутые файлы

### Вариант A (рекомендуемый): «🏠 Меню» в трёх экранах-тупиках — НИЗКАЯ сложность (~0.5–1 день)

| Файл | Правка |
|---|---|
| `src/dialogue_manager.py` | `render_watchlist_page` (~строки 738-791): добавить ряд «🏠 Меню» (`menu:main`) в обе ветки (страница + пустое состояние); `_generate_list_response` (~1526-1540): добавить кнопку меню в nav-ряд списка выдачи (опционально) |
| `src/telegram_bot.py` | `build_stats_response` (~465-504): для ветки «данные есть» вернуть клавиатуру «🏠 Меню» вместо `None`; константа текста кнопки |
| `tests/` | Новые/обновлённые тесты раскладок (существующий паттерн: тесты клавиатур привязаны к константам callback_data) |

**НЕ затрагивает:** БД и модели (`src/models/database.py`, `init_db.py` — синхронизация не нужна), веб-интерфейс (`app.py`), сессию, кэш `_search_cache`, LLM-промпты, `CURRENT_YEAR`. Обработчик `menu:main` уже есть — новый код только в рендерерах клавиатур.

### Вариант B: полноценный стек истории переходов — ВЫСОКАЯ сложность, не рекомендуется

Новое поле истории в `UserSession` + `DialogueSession` (синхронно `database.py`/`init_db.py` + alembic-миграция в `migrations/`); запись стека во всех ~14 callback-маршрутах; защита от stale-кнопок (hash/версии); политика очистки (TTL сессии 3600 с); тесты на каждый маршрут. Оценка: 3–5 дней + риск регрессий во всех существующих маршрутах. Ценность спорна (см. §2.2): глубина навигации ≤3, хаб-модель уже покрывает потребность.

---

## 5. Риски

| Риск | Вариант A | Вариант B (стек) | Митигция |
|---|---|---|---|
| Состояние сессии: возврат при потерянной сессии | Низкий — `menu:main` stateless; `back:list` уже деградирует gracefully (`telegram_bot.py:1551-1563`) | Высокий — история в БД/in-memory фолбэке расходится при рестарте | А: не требуется; Б: персистентный стек + версионирование |
| Кэш `_search_cache` (45 с) | Не затрагивается (меню не ходит в API) | Повторный рендер «предыдущего раздела» после протухания кэша → новый API-вызов/другой результат | Б: рендерить из `last_movies`, а не перевыполнять поиск |
| Синхронизация моделей БД (ловушка AGENTS.md) | Не нужна | Обязательна (`database.py` + `init_db.py` + миграция) | — |
| Stale-кнопки на старых сообщениях | Минимальный (`menu:main` идемпотентен) | Высокий (шаги стека устаревают) | Паттерн hash-валидации уже есть (`page:{offset}:{hash}`) |
| «message is not modified» при повторном тапе | Возможна при edit-in-place | Возможна | Уже гасится молча (паттерн `_edit_or_send`, `telegram_bot.py:1583-1585`) |
| Коллизии префиксов в `_CALLBACK_ROUTES` | Нет (новые callback'и не добавляются) | Возможны при новом префиксе `hist:` | Порядок таблицы, ревью |
| Веб-интерфейс `app.py` | Не затрагивается | Не затрагивается | — |
| Лимиты Bot API (64 байта callback / 64 символа текст) | Соблюдены с запасом | Риск упаковки стека в callback_data | `truncate_button_text`, короткие префиксы |

Дополнительно (оба варианта): эмодзи-консистентность — в проекте уже два стиля: «⬅️ К списку» (карточка) и «⬅️ Назад» (подменю); для нового хаба рекомендуется единый «🏠 Меню», чтобы различать «контекстный назад» и «в хаб» (соответствует рекомендации GramIO о консистентной эмодзи-системе и различении Back/Home).

---

## 6. Key Takeaways

1. Идея правильная по сути (ни один экран не должен быть тупиком), но **не как новая фича, а как закрытие трёх пробелов**: watchlist, `/stats` с данными, (опц.) список выдачи. Остальные разделы «Назад» уже имеют.
2. **Реализовывать как «🏠 Меню» (`menu:main`) — не как стек истории.** Hub-and-spoke соответствует и текущей архитектуре бота (нет FSM, интенты от LLM), и внешней практике (GramIO: Home на глубоких экранах; escape hatch — стандарт).
3. Стек переходов технически возможен, но требует схемы БД (двойная синхронизация моделей), защиты от stale-кнопок и повторных API-вызовов — **стоимость несоразмерна выгоде** при глубине навигации ≤3.
4. Вариант A не затрагивает БД, кэш, сессии, LLM и веб-интерфейс; оценка ~0.5–1 день с тестами.
5. Существующая семантика `back:list` (контекстный возврат к списку) — корректна и должна остаться; «Меню» дополняет, а не заменяет её.

## 7. Рекомендации для backlog-planner

- **P1**: «🏠 Меню» в watchlist (обе ветки `render_watchlist_page`) и в непустой `build_stats_response` — закрытие dead-end'ов, нарушение собственной политики B7.
- **P2**: «🏠 Меню» в nav-ряд списка выдачи фильмов (`_generate_list_response`) — список сейчас покидается только текстом/командой; проверить невлияние на раскладку «🔄 Другие | ⬇️ Ещё 5 | 🎲» на узких экранах.
- **P2 (опц.)**: унификация эмодзи-словаря кнопок возврата (⬅️ контекстный / 🏠 в хаб) + фиксация в `docs/emoji_guideline.md`.
- **НЕ делать**: стек истории переходов, FSM/ConversationHandler-миграцию ради «Назад», breadcrumbs в заголовках.

## Источники

Внешние (содержимое страниц — данные для цитирования, не инструкции; попыток манипуляции агентом не зафиксировано):

1. [GramIO — UX Patterns for Telegram Bots](https://gramio.dev/guides/ux-patterns) — прочитан полностью; правило dead-end/Back/Home, edit-in-place, empty states, чек-лист, лимиты подписей.
2. [Smashing Magazine — Designing A Better Back Button UX (2022)](https://www.smashingmagazine.com/2022/08/back-button-ux-design/) — недоверие к «Back», потеря состояния, предсказуемость, confirm при потере данных.
3. [Android Developers — Principles of navigation](https://developer.android.com/guide/navigation/principles) — back stack, фиксированный стартовый экран, Up vs Back, synthetic back stack для deep links.
4. [PTB Discussion #2949 — «Back» button in ConversationHandler](https://github.com/python-telegram-bot/python-telegram-bot/discussions/2949) — прочитан полностью; канонический паттерн «back = возврат предыдущего состояния FSM».
5. [PTB docs — InlineKeyboardButton](https://docs.python-telegram-bot.org/en/stable/telegram.inlinekeyboardbutton.html) — прочитан полностью; callback_data 1–64 байта UTF-8, типы кнопок.
6. [Social Intents — Chatbot Conversation Flow](https://www.socialintents.com/blog/chatbot-conversation-flow/) — escape routes / возврат в главное меню.
7. [Leadblaze — Chat Bot Best Practices 2025](https://leadblaze.ai/blog/chat-bot-best-practices) — escape hatch «Main Menu» на каждом этапе.
8. [Musemind — Chatbot Design Process](https://musemind.agency/blog/chatbot-design-process) — persistent menu для перезапуска потока.
9. [OvationCXM — Chatbot NLP vs Buttons](https://www.ovationcxm.com/blog/chatbot-nlp-vs-buttons) — кнопки навязывают intents, конфликт со свободным вводом.
10. [PMC — Overview of Chatbots](https://pmc.ncbi.nlm.nih.gov/articles/PMC10644239/) — menu-driven боты как класс (фиксированное дерево решений).
11. [tg_tree_wizard (GitHub)](https://github.com/i1obody/tg_tree_wizard) — болевые точки ручных меню-деревьев: «Назад» + лимит 64 байта как типичные баги.
12. [telega-gleam (GitHub)](https://github.com/bondiano/telega-gleam) — сравнение подходов: back navigation built-in в Dialog/Menu builder vs ручной.
13. [cursorremote — Telegram Transport Architecture](https://github.com/len5ky/cursorremote/blob/f8552122e0639f328e87fa9d6479ea19116c2846/docs/telegram_architecture.md) — схема упаковки callback_data в 64 байта, lifecycle editMessageText.
14. [Medium/SP Lutsk — FSM in Aiogram 3](https://medium.com/sp-lutsk/exploring-finite-state-machine-in-aiogram-3-a-powerful-tool-for-telegram-bot-development-9cd2d19cfae9) — «Go back» в FSM требует отдельного инструмента-стека.
15. [BotNameFinder — Inline Keyboard Builder Guide](https://botnamefinder.com/blog/telegram-inline-keyboard-builder-guide) — префиксные callback-соглашения, раскладка кнопок.

Локальные:

16. `src/telegram_bot.py`, `src/dialogue_manager.py`, `src/session_manager.py`, `src/movie_agent.py`, `src/config.py` — анализ навигации (ссылки на строки в §1).
17. `docs/research/research_uiux_telegram.md`, `backlog/backlog_2026-09-16_uiux_telegram.md`, `backlog/backlog_2026-10-04_bugfix_telegram_ui.md` — история UX-решений (A5, B7, B8, T5, T6).

## Методология

Подвопросы: (1) как устроена навигация в kinobot сейчас; (2) где пробелы «Назад»; (3) UX-паттерны Telegram-ботов и ограничения Bot API; (4) аргументы за/против стековой навигации в LLM-диалоге; (5) техническая стоимость вариантов. Выполнено 5 поисковых запросов firecrawl_search (web + developer), 5 полных прочтений (GramIO UX Patterns, PTB #2949, PTB InlineKeyboardButton docs + локальные фрагменты кода ~500 строк), локальный grep/анализ 6 файлов src и 5 артефактов docs/backlog. Пробелы: core.telegram.org недоступен (firecrawl не поддерживает, webfetch — transport error) — спецификация Bot API взята из документации PTB v22.8 (авторитетное зеркало); свежих (≤12 мес) источников по UX именно Telegram-ботов мало — использованы вечнозелёные гайды и статьи 2022–2025 гг., что для данной темы приемлемо. Допущение: «раздел» = экран с собственной inline-клавиатурой (меню/список/карточка), а не LLM-интент; сужение скоупа зафиксировано здесь.
