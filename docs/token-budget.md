# Бюджет токенов LLM — baseline и мониторинг

Контекст: источник — бэклог `backlog/backlog_2026-09-18_token-economy-llm.md` (задача A5) и отчёт исследования `docs/research/token-economy-llm-2026-09-18.md`. Дата и время baseline: **2026-09-22 23:59:57**. Версия opencode: **1.18.32**.

**Scope baseline — «текущий проект» (`--project ""`), т.е. kinobot-ai.** Все три окна (7д / 30д / 90д) сняты в этом ЕДИНОМ scope; замеры «ПОСЛЕ» обязаны сниматься в нём же (регламент, п. 3). Прежняя версия документа смешивала scope (30д/90д — «все проекты», 7д — «текущий проект»), из-за чего «ДО/ПОСЛЕ» были несопоставимы; числа ниже — переснятый единообразный baseline.

---

## Как воспроизвести замер

Все команды запускать **из корня проекта**. Вывод `opencode stats` идёт в stdout с псевдографикой (box-drawing) и ANSI-эскейпами. Кодировка консоли Windows может не содержать символов псевдографики (box-drawing) — сохранять вывод в UTF-8: `opencode stats ... | Out-File -Encoding utf8 <файл>`. В PowerShell 5.1 `Out-File -Encoding utf8` пишет BOM; в сыром дампе могут оставаться ANSI-эскейпы (напр. `[1A`) — на числа они не влияют.

```powershell
# Scope baseline — ТЕКУЩИЙ проект (kinobot-ai).
# Ловушка: литерал `--project kinobot-ai` возвращает 0 сессий!
# Фильтр сопоставляет по внутреннему id проекта, а не по имени папки,
# поэтому для текущего проекта используется пустая строка:
opencode stats --models --days 7  --tools 20 --project ""
opencode stats --models --days 30 --tools 20 --project ""
opencode stats --models --days 90 --tools 20 --project ""

# Только для справки — ВСЕ проекты (без фильтра --project). В baseline НЕ используется:
opencode stats --models --days 30 --tools 20
```

> Примечание о scope: 7д — текущий проект, **65** сессий; 30д/90д — текущий проект, **139** сессий. Замер «всех проектов» дал бы чуть больше (~5% сессий относятся к другим каталогам проектов). Baseline и «ПОСЛЕ» ОБЯЗАТЕЛЬНО снимать в ОДНОМ scope (`--project ""`), иначе метрики Avg/Median Tokens/Session (эффект A2/A3) несопоставимы.

---

## Baseline — «ДО» (scope: текущий проект kinobot-ai, `--project ""`; снят 2026-09-22 23:59:57, до правок A1–A4)

### Сводка по окнам (7д / 30д / 90д)

| Метрика | 7 дней | 30 дней | 90 дней |
|---|---|---|---|
| Sessions | 65 | 139 | 139 |
| Messages | 1 953 | 4 568 | 4 568 |
| Дней в окне (Days) | 7 | 30 | 90 |
| Avg Tokens/Session | 2.5M | 2.5M | 2.5M |
| Median Tokens/Session | 976.8K | 1.0M | 1.0M |
| Input | 11.2K | 26.0K | 26.0K |
| Output | 1.5M | 3.2M | 3.2M |
| Cache Read | 149.4M | 323.4M | 323.4M |
| Cache Write | 8.7M | 19.0M | 19.0M |
| Total Cost | $0.00 | $0.00 | $0.00 |
| Avg Cost/Day | $0.00 | $0.00 | $0.00\* |

\* В выводе 90-дневного окна строка `Avg Cost/Day` отсутствует; окно по всем прочим метрикам идентично 30-дневному, значение — $0.00.

### Разбивка по моделям

В scope текущего проекта модель **одна** — `bailian-token-plan-personal/qwen3.8-max`. Строки `alibaba-token-plan/qwen3.6-plus` и `alibaba-token-plan/qwen3.8-max` из прежнего замера УДАЛЕНЫ: они относились к другим проектам и попадали в таблицу из-за смешения scope.

| Окно | Модель | Messages | Input | Output | Cache Read | Cache Write | Cost |
|---|---|---|---|---|---|---|---|
| 7д | bailian-token-plan-personal/qwen3.8-max | 1 876 | 11.2K | 1.5M | 149.4M | 8.7M | $0.0000 |
| 30д | bailian-token-plan-personal/qwen3.8-max | 4 353 | 26.0K | 3.2M | 323.4M | 19.0M | $0.0000 |
| 90д | bailian-token-plan-personal/qwen3.8-max | 4 353 | 26.0K | 3.2M | 323.4M | 19.0M | $0.0000 |

> Сноска: сумма Messages по моделям (7д: **1 876**; 30д: **4 353**) меньше итогового Messages сводки (7д: **1 953**; 30д: **4 568**) на ~4–5% — opencode учитывает в per-model разбивке только сообщения с атрибуцией модели. Расхождение штатное, не ошибка транскрипции.

### Топ инструментов — 7 дней (знаменатель: сумма всех tool-вызовов = 2 535)

| Инструмент | Вызовы | Доля |
|---|---|---|
| bash | 932 | 36.8% |
| read | 622 | 24.5% |
| edit | 441 | 17.4% |
| grep | 258 | 10.2% |
| write | 98 | 3.9% |
| task | 54 | 2.1% |
| skill | 53 | 2.1% |
| firecrawl_* | 22 | 0.9% |
| glob | 17 | 0.7% |
| todowrite | 16 | 0.6% |
| firecrawl_* | 11 | 0.4% |
| firecrawl_* | 7 | 0.3% |
| webfetch | 2 | 0.1% |
| context7_* | 2 | 0.1% |

### Топ инструментов — 30 дней (знаменатель: сумма всех tool-вызовов = 5 946; 90д идентично 30д)

| Инструмент | Вызовы | Доля |
|---|---|---|
| bash | 2 289 | 38.5% |
| read | 1 354 | 22.8% |
| edit | 1 005 | 16.9% |
| grep | 506 | 8.5% |
| write | 306 | 5.1% |
| skill | 130 | 2.2% |
| task | 105 | 1.8% |
| todowrite | 85 | 1.4% |
| glob | 48 | 0.8% |
| firecrawl_* | 45 | 0.8% |
| firecrawl_* | 27 | 0.5% |
| question | 22 | 0.4% |
| webfetch | 11 | 0.2% |
| firecrawl_* | 9 | 0.2% |
| firecrawl_* | 2 | 0.0% |
| context7_* | 2 | 0.0% |

> Сноска к таблицам инструментов: имена MCP-инструментов обрезаны выводом stats (`firecrawl_firecr..`, `context7_resolve..`); полные имена — `firecrawl_firecrawl_search` / `_scrape` / `_map` и `context7_resolve-library-id` и т.п., перечень — в блоке `mcp` конфига opencode. Несколько строк `firecrawl_*` — это разные инструменты firecrawl, а не дубли.

### Конфигурация opencode на дату baseline (2026-09-22, ДО правок A1–A4)

Перечислены ТОЛЬКО несекретные поля (API-ключи и переменные окружения в документ намеренно не копируются). Источник — глобальный конфиг `~/.config/opencode/opencode.jsonc`; проектный `opencode.json` содержит лишь `mcp.context7`.

| Поле конфига | Значение «ДО» | Связь с мерами |
|---|---|---|
| `provider.bailian-token-plan-personal.models.qwen3.8-max.options.effort` | **xhigh** | цель A1 → `medium` |
| `provider...qwen3.8-max.options.reasoning` | `true` | не менять |
| `provider...qwen3.8-max.limit.context` | **983616** | цель A2 → `200000` |
| `provider...qwen3.8-max.limit.output` | `131072` | не менять |
| `provider...qwen3.8-max.options.providerOptions.anthropic.cacheControl` | `true` | кэш настроен правильно — НЕ менять |
| блок `compaction` | **ОТСУТСТВУЕТ** (prune = default `false`) | цель A3 → `{ "auto": true, "prune": true }` |
| `mcp.firecrawl.enabled` | `true` (глобально) | цель B2 — ограничить/отключить в нересёрш-сессиях |

> Значения зафиксированы для привязки строк «ПОСЛЕ» к конкретному конфигу (риск №8 бэклога, строка 165).

### Конфигурация opencode ПОСЛЕ правок A1–A3 (применено 2026-09-23)

| Поле конфига | «ДО» | «ПОСЛЕ» | Задача |
|---|---|---|---|
| `provider.bailian-token-plan-personal.models.qwen3.8-max.options.effort` | xhigh | **medium** | A1 |
| `provider...qwen3.8-max.limit.context` | 983616 | **200000** | A2 |
| блок `compaction` (верхний уровень) | отсутствует | **`{ "auto": true, "prune": true }`** | A3 |

- Резервная копия до правок: `~/.config/opencode/opencode.jsonc.bak-2026-09-23` (откат = вернуть её на место).
- Не изменены: `reasoning: true`, `limit.output: 131072`, `providerOptions.anthropic.cacheControl: true`, блоки `mcp`, `instructions`.
- Схема сверена с `https://opencode.ai/config.json` (публикуемая схема текущей версии opencode; на дату правки содержимое совпадает с установленной 1.18.32, подтверждено `opencode --version`): `compaction` допускает `{auto, prune, tail_turns, preserve_recent_tokens, reserved}` (`additionalProperties: false`) — блок `{auto, prune}` валиден; валидность конфига именно установленной версии подтверждена `opencode debug config` (exit 0). v2-миграции полей (`keep.tokens`/`buffer`) нет (допущение 2 бэклога закрыто).
- Валидация: `opencode debug config` (конфиг резолвится без ConfigInvalidError) + `opencode models`; новая конфигурация подхватывается сессиями, запущенными ПОСЛЕ правки (конфиг загружается один раз на старте — применять между сессиями, допущение 6).
- Правки A4 (per-agent effort) и A6 на эту дату НЕ применены.

### Правки B2 — firecrawl MCP (применено 2026-09-23)

**Замер overhead схем firecrawl** (закрывает пробел п.3 «Аналитические пометки»: `stats --tools` показывает только вызовы, не overhead схем). Метод: локальный запуск `npx -y firecrawl-mcp` по stdio, MCP-handshake, `tools/list`, сумма байт определений инструментов (детали замера сохранены в `%TEMP%\opencode\firecrawl-schema-size.json`). Результат: **27 инструментов, 40 199 байт JSON** (inputSchema 24 296 B + descriptions 11 006 B + имена/обвязка) ≈ **~10.0K токенов** (оценка chars/4) — в КАЖДОМ ходе КАЖДОЙ сессии с включённым firecrawl. Топ-5 по размеру:

| Инструмент | Байт JSON |
|---|---|
| `firecrawl_search` | 5 645 |
| `firecrawl_crawl` | 3 888 |
| `firecrawl_scrape` | 3 802 |
| `firecrawl_interact` | 3 459 |
| `firecrawl_parse` | 2 694 |

**Решение — per-agent permission** (поддерживается установленной opencode 1.18.32, подтверждено исходниками v1.18.32: `config/agent.ts` normalize — поле `tools` deprecated, используем `permission`; `agent/agent.ts` — глобальный `cfg.permission` входит в каждого агента через `Permission.merge(defaults, user)`, правила агента мержатся ПОСЛЕ; `permission/index.ts` — `findLast`: побеждает последнее совпавшее правило, deny с pattern "*" скрывает инструмент из контекста LLM). Глобально `"firecrawl_*": "deny"` (правило `user` у всех агентов), у агента `researcher` во frontmatter `"firecrawl_*": allow` — агентное правило добавляется ПОСЛЕ глобального, `findLast` → allow побеждает, firecrawl доступен только исследователю. Глоб-ключ `firecrawl_*` матчит все 27 инструментов firecrawl (имена регистрируются как `firecrawl_firecrawl_*`), НЕ затрагивает context7 (`context7_*`) и ресурсные MCP-тулы (`list_mcp_resources` и т.п.). `mcp.firecrawl.enabled` остаётся `true` — сервер нужен researcher'у. Fallback-вариант бэклога (`"enabled": false` + ручное включение перед `/research` + памятка в SKILL.md) НЕ понадобился: per-agent поддерживается, при статическом включении нет риска «забыть включить».

| Поле | «ДО» | «ПОСЛЕ» | Задача |
|---|---|---|---|
| `permission.firecrawl_*` (глобальный конфиг, верхний уровень) | отсутствует | **`"deny"`** | B2 |
| `researcher.md` frontmatter `permission."firecrawl_*"` | отсутствует | **`allow`** | B2 |
| `mcp.firecrawl.enabled` | `true` | `true` (без изменений) | B2 |

- Резервная копия до правок: `~/.config/opencode/opencode.jsonc.bak-2026-09-23-b2`. Откат = вернуть её на место + убрать строку `"firecrawl_*": allow` из frontmatter `.opencode/agent/researcher.md`; подхватывается только НОВЫМИ сессиями. Секреты (FIRECRAWL_API_KEY) в документ намеренно не копируются (практика baseline-раздела).
- Верификация (без запуска сессий, `opencode run` запрещён контрактом воркера): `opencode debug config` — exit 0 (конфиг валиден для 1.18.32); `opencode debug agent build` / `autodev-worker` — deny-правило `firecrawl_*` присутствует (схемы скрыты); `opencode debug agent researcher` — глобальное deny РАНЬШЕ агентного allow (порядок критичен). **Полный E2E-дым `/research` отложен до следующего запуска пайплайна**: конфиг грузится один раз на старте сессии, в текущей сессии эффект не наблюдается и не проверяется.
- Эффект: нересёрш-сессии экономят ≈10K токенов префикса на ход (в т.ч. в канале Cache Write — см. п.1 пометок). Издержки: процесс `npx firecrawl-mcp` по-прежнему стартует с каждой сессией — это latency/процесс, не токены; осознанно принято (экономия B2 — именно схемы в префиксе).
- Побочный эффект: `implementer` и прочие нересёрш-агенты теряют `firecrawl_developer_search` — остаётся context7 MCP, приемлемо (зафиксировано в OpenSpec-изменении `restrict-firecrawl-mcp-to-researcher`). Заметка для ресёрч-сессий добавлена в `.opencode/skills/deep-research/SKILL.md` (секция «MCP Requirements»).

### Правки B3 — дешёвая модель для read-only сабагентов (применено 2026-09-23)

**Решение — ветвь «доступна»**: verifier и reviewer переведены на `qwen3.8-flash` (та же генерация 3.8, flash-класс) через per-agent поле `model` во frontmatter; кодинг-агенты (`implementer`, `autodev-worker`, `autodev`, `backlog-planner`) и `researcher` остались на флагмане `qwen3.8-max` (качество синтеза важнее экономии). Подписка Token Plan Personal списывает Credits по «tiered deduction coefficients by model» — flash-класс дешевле флагмана (точные ставки публикуются только в консоли Model Studio, usage details).

**Доказательства доступности** (2026-09-23, детали — `openspec/changes/rightsize-readonly-agent-models/design.md` D1):

1. `GET {baseURL}/models` → HTTP 404 `InvalidParameter: Not support` — endpoint список моделей не отдаёт; источник истины — документация + зонд.
2. Документация Alibaba Model Studio «Token Plan (Personal Edition) Overview» (обновлена 2026-09-23): поддерживаемые текстовые модели Qwen — `auto`, `qwen3.8-max`, `qwen3.8-flash`, `qwen3.7-max`, `qwen3.7-plus`, `qwen3.6-flash` (+ сторонние deepseek-v4-*, glm-5.*); `qwen3.6-plus` в актуальном списке отсутствует (выведен из подписки — в старых замерах других проектов фигурировал, см. сноску «Разбивка по моделям»). Бонус: ночная скидка 60% Credits (22:00–08:00 UTC+8) на `qwen3.8-max` и `qwen3.8-flash`.
3. Эмпирические зонды `POST {baseURL}/messages` (`max_tokens:1`, ключ из конфига, в документ не копируется):

| Model id | HTTP | Вывод |
|---|---|---|
| `qwen3.8-max` | 200 | контроль — текущая модель работает |
| `qwen3.8-flash` | **200** | **доступна — выбрана** |
| `qwen3.6-flash` | 200 | доступна (прошлая генерация, резерв) |
| `qwen3.6-plus` | 403 | `AccessDenied` — недоступна подписке |
| `qwen3.7-plus` | 500 | `InternalError` — сервинг нестабилен, отброшен |

4. Каталог models.dev (кэш opencode): `qwen3.8-flash` — релиз 2026-08-26, `reasoning: true`, `effort: [low, medium, xhigh]`, `tool_call: true`, `structured_output: true`, ctx 1M / out 131072. Урок: каталог шире фактической доступности подписки (`qwen3.6-plus` в каталоге есть, но зонд — 403).

**Изменения (несекретные поля):**

| Поле | «ДО» | «ПОСЛЕ» | Задача |
|---|---|---|---|
| `provider.bailian-token-plan-personal.models` | только `qwen3.8-max` | `qwen3.8-max` (БЕЗ изменений) + **`qwen3.8-flash`**: name «Qwen3.8 Flash», reasoning true, limit.context **200000** (единообразно с A2; лимит компакции, не физический — в каталоге 1M), limit.output 131072, modalities text+image→text, options.effort medium, cacheControl true | B3 |
| `verifier.md` frontmatter | `model` отсутствует (наследует модель сессии) | **`model: bailian-token-plan-personal/qwen3.8-flash`** | B3 |
| `reviewer.md` frontmatter | `model` отсутствует | **`model: bailian-token-plan-personal/qwen3.8-flash`** | B3 |
| `options.effort` verifier/reviewer | `medium` (A4) | `medium` — СОХРАНЁН: flash поддерживает effort (low/medium/xhigh), матрица A4 (`autodev-agent-effort`) не нарушена | B3 |
| `researcher.md` и прочие агенты | `model` отсутствует | без изменений (наследуют флагман сессии) | B3 |

- Резервная копия до правок: `~/.config/opencode/opencode.jsonc.bak-2026-09-23-b3`. Откат (полный) = вернуть её на место + удалить строку `model:` из frontmatter `verifier.md` и `reviewer.md`; откат (частичный, «пилот на verifier» по риску бэклога) = удалить `model:` только из `reviewer.md`. Подхватывается только НОВЫМИ сессиями. Секреты (apiKey провайдера) в документ намеренно не копируются.
- Верификация (без запуска сессий, `opencode run` запрещён контрактом воркера): `opencode debug config` — exit 0; `opencode models` — содержит `bailian-token-plan-personal/qwen3.8-flash` наряду с `qwen3.8-max`; `opencode debug agent verifier` / `reviewer` — резолвят `model.providerID=bailian-token-plan-personal, modelID=qwen3.8-flash` и `options.effort=medium` без ошибок схемы; `opencode debug agent researcher` — поля `model` нет (наследует сессию). **E2E (реальный прогон verifier/reviewer на flash) — только в НОВЫХ сессиях**: конфиг грузится один раз на старте, проверка отложена до следующего запуска `/make-task` (критерий приёмки B3: гейт проходит без деградации; при деградации ревью — частичный откат reviewer).
- Эффект и мониторинг: в разбивке `opencode stats --models` появится вторая строка модели (`bailian-token-plan-personal/qwen3.8-flash`) — доля токенов рутинных сабагентов уходит с флагманского коэффициента списания на flash-коэффициент; долларовая тарификация по-прежнему $0.00 (подписка), эффект измеряем в токенах и в Credits консоли. Издержки: префиксный кэш per-model — первые сессии на flash платят Cache Write заново (разовый эффект, `cacheControl: true` в блоке новой модели).

### Аналитические пометки

1. **Cost = $0.00 — долларовая тарификация не ведётся.** Аккаунт — личный token-plan (`bailian-token-plan-personal`). Поэтому эффект мер A1–A4 / B1–B3 измерять **в токенах, а не в $**. Ключевые метрики эффекта:
   - **Output** — канал thinking-токенов, на который бьёт A1 (effort `xhigh` → `medium`);
   - **Cache Write** — объём записи в префиксный кэш (на него влияют A1, B1);
   - **Avg / Median Tokens/Session** — на них влияют A2 (`limit.context`) и A3 (compaction / prune).
   Cache Read огромен (149.4M за 7д) — префиксный кэш работает; Input крошечный (11.2K) — почти всё уходит в cache-read.

2. **Текущий effort = xhigh (глобально).** Output 1.5M за 7д / 3.2M за 30д — это значение «ДО» для меры A1. Глобальный xhigh — главный антипаттерн (отчёт §2.1).

3. **firecrawl (связь с B2).** За 7д `firecrawl_*` вызовы = 22 + 11 + 7 = **40**, доля = 40 / 2 535 = **1.6%**; за 30д = 45 + 27 + 9 + 2 = **83**, доля = 83 / 5 946 = **1.4%**. Знаменатели — сумма всех tool-вызовов окна: 7д = **2 535**; 30д = **5 946**. Это **число вызовов**, а не overhead JSON-схем в каждом ходе — `stats --tools` НЕ показывает overhead схем. Для B2 потребуется **отдельная оценка доли схем** в префиксе каждого запроса.

4. **Воспроизводимость.** Команды запускать из корня проекта, в одном и том же scope (`--project ""`), теми же окнами; вывод сохранять в UTF-8 (см. «Как воспроизвести замер»).

> ⚠️ **90д ПОЛНОСТЬЮ идентично 30д** (Sessions 139 = 139, Messages 4 568 = 4 568) → в scope текущего проекта НЕТ сессий старше 30 дней; локальная история opencode покрывает ровно ~30 дней. Baseline по 90-дневному окну — частичный (риск бэклога, строка 72: `stats` учитывает только локальную историю opencode).

---

## Регламент мониторинга

1. **Повторный замер — через 7 дней после применения A1–A4** (даты фиксируются в якоре над таблицей «ПОСЛЕ»).
2. **Далее — раз в неделю.**
3. Каждый замер выполнять **теми же командами, окнами и всегда в одном и том же scope**, что и baseline (`--models --days 7 --tools 20 --project ""`, то же для `--days 30` и `--days 90`). Смена scope (например, «все проекты» вместо `--project ""`) делает «ДО/ПОСЛЕ» несопоставимыми — особенно по Avg/Median Tokens/Session.
4. После недельного замера добавить строку «ПОСЛЕ» в таблицу ниже и сформулировать вывод об эффекте: **Δ Output**, **Δ Avg / Median Tokens/Session**, **Δ Cache Write** — в процентах относительно baseline **того же окна и того же scope**.
5. Правки конфигов применять **МЕЖДУ сессиями** (префиксный кэш, TTL explicit-кэша 5 мин — допущение 6 бэклога). Иначе invalidation кэша «смазывает» замер.
6. **Окна `--days N` — скользящие.** Через 7 дней после A1–A4 в 30д-окне ~23 дня данных «ДО» (~77%), поэтому Δ по 30д будет занижен. Основной индикатор эффекта — окно 7д (полностью post-change); 30д становится полностью сопоставимым лишь через 30 дней после применения мер — до этого Δ интерпретировать с поправкой на долю pre-change дней.

---

## Замеры «ПОСЛЕ» (заполнять еженедельно)

Дата применения мер: **A1–A3 — 2026-09-23**; A4 — ожидается. Контрольный замер T+7 — через 7 дней после применения A4 (окно 7д полностью post-change только для A1–A3 до тех пор).

> На момент baseline (**2026-09-22 23:59:57**) правки A1–A4 ещё не были применены; **A1–A3 применены 2026-09-23** (см. таблицу «ПОСЛЕ правок A1–A3»). Числа baseline ниже остаются точкой отсчёта. Δ считать относительно baseline **того же окна и scope** (7д: Output 1.5M, Avg 2.5M, Cache Write 8.7M; 30д: Output 3.2M, Avg 2.5M, Cache Write 19.0M).

| Дата | Окно | Sessions | Messages | Input | Output | Cache Read | Cache Write | Avg Tokens/Session | Median | Δ Output, % | Δ Avg/Session, % | Δ Cache Write, % | Примечание (применённые меры) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| _ожидается_ | _7д_ | _—_ | _—_ | _—_ | _—_ | _—_ | _—_ | _—_ | _—_ | _—_ | _—_ | _—_ | _A1–A4_ |

---

## Связанные задачи

- **A1** — effort `xhigh` → `medium` (глобально). Цель: снизить **Output** (thinking-токены).
- **A2** — `limit.context` → `200000`. Цель: снизить **Avg Tokens/Session**.
- **A3** — `compaction` / `prune`. Цель: снизить пересылку старых tool-выводов (`read` / `bash` / `grep`).
- **A4** — per-agent effort (высокий только там, где нужен).
- **B1** — сокращение AGENTS.md до ≤2K токенов. Цель: снизить размер префикса каждого хода → **Cache Write** и input-токены.
- **B2** — overhead firecrawl-схем: `stats --tools` его **не показывает**, нужна отдельная оценка доли JSON-схем в префиксе. **ВЫПОЛНЕНО 2026-09-23**: замер (~10.0K токенов/ход) и per-agent отключение firecrawl — см. раздел «Правки B2 — firecrawl MCP (применено 2026-09-23)» выше.
- **B3** — дешёвая модель для verifier/reviewer (если поддерживается per-agent model). Цель: снизить стоимость/токены рутинных сабагентов → видно в **разбивке по моделям**. **ВЫПОЛНЕНО 2026-09-23**: per-agent `model` поддерживается (opencode 1.18.32, `opencode debug agent`); verifier/reviewer назначен `qwen3.8-flash` (доступность подписке подтверждена зондом endpoint'а — HTTP 200; `qwen3.6-plus` — 403, `qwen3.7-plus` — 500) — см. раздел «Правки B3 — дешёвая модель для read-only сабагентов (применено 2026-09-23)» выше.
