# Сэкономит ли векторизация артефактов разработки токены (код, openspec, docs) — отчёт исследования (ред. 2)
*Дата: 2026-09-18 | Источников: 14 | Уверенность: Высокая | Исправленная версия: v1 ошибочно исследовала runtime-применение векторной БД в самом боте; v2 отвечает на вопрос «экономит ли токены векторизация кода, спецификаций openspec и прочих артефактов при разработке агентом opencode»*

## Executive Summary

**Для kinobot-ai в текущем масштабе векторизация кода и артефактов разработки токены практически НЕ сэкономит.** Проект мал: src + tests ≈ 190–200K токенов целиком (6K строк кода), т.е. весь код физически помещается в одно контекстное окно. Все замеры и бенчмарки показывают: выгода семантического индекса растёт с размером репозитория и на «одиночных сервисах» ~5K строк — пренебрежима; экономия возникает там, где grep вынуждает агента читать десятки целых файлов (моно-репо, миллионы строк). Основное потребление токенов opencode — это транскрипт сессии и tool-выводы, а не поиск по коду; против них векторная БД не работает вообще. Более того: векторный поиск подключается к opencode через MCP-сервер, а его схемы инструментов добавляются в КАЖДЫЙ ход — фиксированная токенная надбавка, которая на малом репо может превысить всю экономию. Экономить токены на поиске без векторов умеют: (1) agentic search (grep/glob — выбор Anthropic для Claude Code: «outperformed everything by a lot»), (2) repo-map паттерн Aider (AST + PageRank: 87 токенов карты вместо ~12 000 токенов чтения файлов; задачи за 8.5–13K токенов против 108–117K у Claude Code), (3) изоляция контекста в сабагентах (`explore` в opencode — уже настроен), (4) дисциплина точечных чтений (уже в вашем AGENTS.md). Для openspec/docs векторизация тоже не окупается: не-архивная часть ≈106K токенов, архив ≈216K, но к нему обращаются редко, а `*.md` уже исключены из индексации `.opencodeignore`. Пересматривать решение имеет смысл при триггерах из §6.

## 1. Количественная база: что вообще предлагается векторизовать

Замеры проекта (2026-09-18, оценка токенов ≈ chars/4):

| Артефакт | Файлов | Символов | ~Токенов | Комментарий |
|---|---|---|---|---|
| `src/` (код) | 26 | 372 101 | ~93K | 6 086 строк — малый репо |
| `tests/` | 28 | 401 277 | ~100K | |
| `docs/` | 16 | 198 080 | ~50K | исключены `.opencodeignore` |
| `openspec/` не-архив | 51 | 423 492 | ~106K | активные changes + specs |
| `openspec/archive/` | 135 | 864 796 | ~216K | самый большой корпус, но обращений мало |
| `backlog/` | 12 | 125 916 | ~31K | |
| `.opencode/` (agents, skills, commands) | 27 | 224 353 | ~56K | грузятся лениво (метаданные/по вызову) |
| **Итого** | | **~2.59M** | **~650K** | |

Ключевые факты:
- **Весь живой код (src+tests ≈ 193K токенов) помещается в контекст одной сессии** вашей модели (лимит 983K, рабочая рекомендация ~200K). Когда корпус целиком влезает в окно, главный аргумент в пользу RAG («данные больше контекста») не работает ([SitePoint](https://www.sitepoint.com/long-context-vs-rag-1m-token-windows/)).
- Крупнейший корпус — архив openspec (~216K токенов), но это «холодные» данные: агент читает оттуда единичные файлы по явной необходимости.
- `.opencodeignore` уже исключает `docs/` и `*.md` (кроме AGENTS.md) — markdown-артефакты не попадают в поиск/упоминания opencode по умолчанию.

## 2. Где opencode реально тратит токены (и при чём тут поиск)

Каждый ход opencode пересылает: промпт + AGENTS.md + схемы MCP + **весь транскрипт сессии со всеми tool-выводами** ([Tokenminning](https://tokenminning.ai/ides/opencode)). Расход на «поиск кода» — это цепочка `grep → read файла → ещё grep → ещё read`. Векторный индекс атакует только последнее звено (меньше целых файлов читается), но:

1. На малом репо grep даёт мало «шумовых» попаданий — файлы маленькие, чтения точечные (ваш AGENTS.md уже требует «SEARCH FIRST», «TARGETED READS ONLY», лимит 300 строк).
2. Основной расход — накопленный транскрипт (compounding), а не единичные поиски. Против него работают compaction/prune/новые сессии (см. отчёт `token-economy-llm-2026-09-18.md` §2), а не векторная БД.
3. Подключение векторного поиска к opencode = новый MCP-сервер: его схемы инструментов уходят в каждый ход постоянно. Пять «лишних» MCP-серверов могут съесть заметную часть окна ([Tokenminning: MCP](https://tokenminning.ai/ides/opencode/mcp)). Нагрузка постоянная, выгода — только в ходах с поиском.

## 3. Доказательная база: векторный индекс vs agentic search для кода

### 3.1. Против векторизации (для малых/средних репо)

- **Позиция Anthropic**: Claude Code сознательно НЕ использует RAG/эмбеддинги — только grep/glob/чтения. Цитата лида Claude Code: ранние версии с RAG пробовали, но «we landed on just agentic search… It outperformed everything. By a lot. And this was surprising» ([разбор](https://zerofilter.medium.com/why-claude-code-is-special-for-not-doing-rag-vector-search-agent-search-tool-calling-versus-41b9a6c0f4d9), [MindStudio](https://www.mindstudio.ai/blog/is-rag-dead-what-ai-coding-agents-use-instead)). Коду нужна структурная точность (точные имена символов), а не семантическая близость; эмбеддинги не ловят причинно-следственные связи кода ([Morph](https://www.morphllm.com/agentic-search)).
- **Академический замер (arXiv 2608.13568, «Does a Language Server Save Tokens for Coding Agents?»)**: даже LSP-поиск (более точный, чем эмбеддинги) при равной успешности задач **дороже grep на 6%**; выигрыш появляется только (а) в «лексически шумных» репозиториях, где grep тонет в ложных срабатываниях (−12%), и (б) у слабых моделей (Haiku: −19…−26%). Сильные модели компенсируют шум сами ([arXiv](https://arxiv.org/html/2608.13568v1)).
- **Масштабирование выгоды**: «экономия растёт с тем, сколько целых файлов grep иначе затянул бы в контекст… на моно-репо эффект большой, **на одиночном сервисе — пренебрежимый**. На 5 000-строчном сервисе подход read-everything достаточно дёшев, чтобы его игнорировать» ([Particula Tech](https://particula.tech/blog/semantic-code-search-vs-grep-coding-agents)). Ваш src — 6 086 строк.
- **Свежесть индекса**: эмбеддинги устаревают при каждом коммите; агентные сессии правят код непрерывно → постоянные ре-эмбеддинги и риск retrieval по устаревшим чанкам («stale embeddings persist» как отдельный риск) ([heyuan110: Agent Memory vs RAG](https://www.heyuan110.com/posts/ai/2026-02-21-ai-agent-memory-systems/)).

### 3.2. За векторизацию (когда она всё-таки экономит)

- **claude-context (Zilliz, 11K+ звёзд)**: гибрид BM25 + dense vectors, заявлено **~40% сокращения токенов при равном качестве retrieval** — но это самооценка на больших репозиториях ([Particula](https://particula.tech/blog/semantic-code-search-vs-grep-coding-agents), [Milvus blog](https://milvus.io/blog/why-im-against-claude-codes-grep-only-retrieval-it-just-burns-too-many-tokens.md)).
- **semble (MinishLab)**: ~98% экономии против НАИВНОГО паттерна «grep → читать целые файлы» (другой базлайн: не против грамотного точечного поиска); CPU-only, без векторной БД как сервиса ([Particula](https://particula.tech/blog/semantic-code-search-vs-grep-coding-agents)).
- **Codebase Memory MCP**: структурный обход 412K → 3.4K токенов (121x), на 31 репо — 83% качества при 10x меньших токенах; это граф знаний, не классические эмбеддинги ([Particula](https://particula.tech/blog/semantic-code-search-vs-grep-coding-agents)).
- Честный вывод из всех цифр: **«проверяй базлайн»**. 40–98% достижимы, когда агент сейчас читает много целых файлов; если поиск уже дисциплинирован (как предписывает ваш AGENTS.md), экономия схлопывается к нулю.

### 3.3. Главная альтернатива: repo map (структурный, НЕ векторный)

Сравнительное исследование кодинг-агентов: **Aider тратит 8.5–13K токенов на задачу против 108–117K у Claude Code** — за счёт graph-based repo map, а не эмбеддингов ([Preprints: Code Retrieval in Coding Agents](https://www.preprints.org/manuscript/202510.0924)). Механика Aider: tree-sitter извлекает сигнатуры символов → PageRank ранжирует файлы по графу ссылок → бинарный поиск вписывает карту в бюджет (**дефолт 1K токенов**); пример: карта репо — 87 токенов вместо ~12 000 токенов полного чтения ([Aider docs](https://aider.chat/docs/repomap.html), [AgentPatterns](https://agentpatterns.ai/context-engineering/repository-map-pattern/), [Anish Gandhi](https://anishgandhi.com/aider-pagerank-codebase-ranking/)). Для kinobot-ai аналог без всякой инфраструктуры — компактный `ARCHITECTURE.md`/карта модулей (файл → ключевые классы/функции → назначение), который агент читает один раз вместо серии grep/read.

## 4. Специфика openspec-спецификаций и docs

- Корпус спецификаций — это markdown, а не код: для ДОКУМЕНТОВ RAG уместнее, чем для кода («Coding Agents Skipped RAG — RAG Still Wins on Large Docs», [MindStudio](https://www.mindstudio.ai/blog/is-rag-dead-what-ai-coding-agents-use-instead)). Но «large» — это >500K токенов и частые семантические запросы. Ваш не-архив openspec ~106K токенов, запросы к нему редки и точечны (агент знает имя change/спеки из workflow OpenSpec).
- Для проектного уровня знаний **курируемые markdown-файлы эффективнее RAG**: выше точность, детерминизм, «same file → same context», ниже риск poisoning; RAG выигрывает только на тысячах документов ([heyuan110](https://www.heyuan110.com/posts/ai/2026-02-21-ai-agent-memory-systems/)).
- Прогрессивная выдача (метаданные → саммари ~50–100 токенов → полный фрагмент 500–1000 токенов только при необходимости) даёт ~10x экономию против наивного RAG-внедрения — тот же принцип, что lazy-loading скиллов в opencode.
- Практический аналог для openspec без векторов: индекс-оглавление (одна таблица «change → статус → путь → 1 строка саммари»), который агент читает вместо сканирования 186 файлов.

## 5. Итоговая оценка для kinobot-ai + opencode

| Вариант | Экономия токенов на разработке | Стоимость внедрения | Вердикт |
|---|---|---|---|
| Векторизация src/tests (claude-context/semble MCP) | ~0–10% (репо мал, поиск уже дисциплинирован); минус постоянная надбавка схем MCP в каждом ходе | Эмбеддинги, индексация, свежесть, новый сервис | ❌ Не окупается |
| Векторизация openspec/docs | Близко к нулю (редкие точечные обращения; *.md уже вне индексации) | То же | ❌ Не окупается |
| Repo map / ARCHITECTURE.md (паттерн Aider, без векторов) | Существенная: карта ~0.5–1K токенов вместо серий grep+read; ориентир Aider: 8–13K токенов/задача | Часы работы, нулевая инфраструктура | ✅ Рекомендовано |
| Сабагент `explore` для поиска (изоляция контекста) | Большая: поиск происходит в отдельной сессии, в основную возвращается только ответ | Уже настроено в проекте | ✅ Использовать активнее |
| LSP через Serena MCP | Спорно: +6% к токенам на чистом репо, −12% на шумном; полезен для точности рефакторинга | MCP-сервер + Python LSP | ⚠️ Опционально, не ради токенов |
| compaction/prune/сессии (из отчёта v «token-economy») | 30–60% на длинных сессиях — основной резерв | Конфиг | ✅ Приоритет |

## 6. Триггеры пересмотра (когда векторизация станет выгодной)

1. Кодовая база выросла до ~50K+ строк / нескольких сервисов (моно-репо) — grep начнёт затягивать десятки файлов.
2. Замер `opencode stats --tools` показывает, что >30% input-токенов сессии — это чтения целых файлов после поиска.
3. openspec-архив стал предметом частых семантических запросов («в каком change решали X?» — регулярно, а не разово).
4. Появилась команда из нескольких параллельных агентов/разработчиков, которым нужен общий семантический слой.
Тогда начинать НЕ с серверной векторной БД, а с CPU-only локальных решений (semble) или claude-context MCP с локальными эмбеддингами, и замерить A/B на своих задачах.

## Key Takeaways

1. **Не векторизуйте**: на репо в 6K строк экономия от семантического поиска ~0, а схемы vector-поиск-MCP-сервера облагают налогом каждый ход. Anthropic для Claude Code выбрала grep и не жалеет («by a lot»).
2. Токены разработки уходят на **транскрипт и tool-выводы** — лечится compaction/prune/новыми сессиями и `explore`-сабагентом, а не RAG.
3. Лучшая «не-векторная векторизация» — **repo map** (Aider): AST+PageRank, бюджет 1K токенов; для проекта достаточно курируемой карты модулей в markdown.
4. Для openspec/docs — **оглавление-индекс и прогрессивная выдача** (саммари → детали) вместо эмбеддингов; курируемые файлы точнее и детерминированнее RAG на проектном масштабе.
5. Даже LSP (структурный поиск) по замерам arXiv не экономит токены на чистых репо (+6% к grep) — что уж говорить про эмбеддинги на 6K строк.
6. Вернётесь к векторизации по триггерам §6 (рост репо, >30% токенов на whole-file reads).

## Sources

1. [Anthropic via zerofilter (Medium)](https://zerofilter.medium.com/why-claude-code-is-special-for-not-doing-rag-vector-search-agent-search-tool-calling-versus-41b9a6c0f4d9) — почему Claude Code без RAG; цитата «agentic search outperformed everything by a lot».
2. [MindStudio: Is RAG Dead](https://www.mindstudio.ai/blog/is-rag-dead-what-ai-coding-agents-use-instead) — что используют кодинг-агенты вместо RAG; «RAG still wins on large docs».
3. [arXiv 2608.13568: Does a Language Server Save Tokens for Coding Agents?](https://arxiv.org/html/2608.13568v1) — LSP +6% к токенам vs grep при равном успехе; выгода только на шумных репо и слабых моделях.
4. [Particula Tech: Semantic Code Search vs Grep 2026](https://particula.tech/blog/semantic-code-search-vs-grep-coding-agents) — разбор цифр claude-context (~40%), semble (~98%), Codebase Memory (121x) и их базлайнов; масштабирование выгоды с размером репо.
5. [Milvus/Zilliz: Against grep-only retrieval](https://milvus.io/blog/why-im-against-claude-codes-grep-only-retrieval-it-just-burns-too-many-tokens.md) — контраргумент: −40% токенов на больших репо; проект Claude Context.
6. [Morph: Agentic Search](https://www.morphllm.com/agentic-search) — сравнение agentic/semantic/lexical; почему эмбеддинги не ловят структуру кода.
7. [Preprints: Code Retrieval Techniques in Coding Agents](https://www.preprints.org/manuscript/202510.0924) — Aider 8.5–13K токенов/задача vs Claude Code 108–117K; transparency-efficiency trade-off.
8. [Aider docs: Repository map](https://aider.chat/docs/repomap.html) — tree-sitter + PageRank, бюджет `--map-tokens` 1K.
9. [AgentPatterns: Repository Map Pattern](https://agentpatterns.ai/context-engineering/repository-map-pattern/) — механика и кейс «87 токенов вместо ~12 000».
10. [Anish Gandhi: Aider PageRank repomap](https://anishgandhi.com/aider-pagerank-codebase-ranking/) — сравнение подходов Aider/Cursor/Claude Code.
11. [deska.dev: Aider Repo Map Explained](https://deska.dev/blog/aider-repo-map-explained) — адаптивный бюджет карты.
12. [heyuan110: Agent Memory vs RAG](https://www.heyuan110.com/posts/ai/2026-02-21-ai-agent-memory-systems/) — курируемый контекст vs RAG на проектном уровне; прогрессивная выдача ~10x.
13. [Tokenminning: OpenCode](https://tokenminning.ai/ides/opencode) — из чего складывается счёт opencode; MCP-схемы в каждом ходе; anti-patterns.
14. [SitePoint: Long Context vs RAG](https://www.sitepoint.com/long-context-vs-rag-1m-token-windows/) — точка пересечения: <200K токенов и <500 запросов/день → инфраструктура векторной БД не окупается.
15. [Serena MCP (GitHub)](https://github.com/oraios/serena) — LSP-инструменты для опencode/Claude Code (альтернатива векторам).

## Methodology

Замерены артефакты проекта (PowerShell: размеры src/tests/docs/openspec/backlog/.opencode; split openspec на архив/не-архив). 6 поисковых запросов firecrawl_search: grep vs эмбеддинги для кодинг-агентов; repo map Aider; Serena/LSP token savings; RAG для документации; точка пересечения RAG vs long context. Проанализировано 15 источников, включая академический замер (arXiv 2608.13568) и сравнительное исследование агентов (Preprints). Ограничения: (1) цифры «−40%/−98%» — самооценки авторов инструментов на больших репо, перенос на 6K-строчный проект некорректен — о чём прямо сказано в §3.2; (2) оценка токенов chars/4 приблизительна (±20% для кода); (3) точный профиль расходов ваших сессий opencode неизвестен — перед любыми решениями рекомендуется замер `opencode stats --tools --days 7` (в §6 триггер опирается на него). Отчёт v1 (runtime-применение векторной БД в самом боте — семантический кэш GigaChat, pgvector) остаётся валидным для своего вопроса; его краткий вердикт: pgvector в существующем PostgreSQL ради персистентного кэша, а не ради токенов.
