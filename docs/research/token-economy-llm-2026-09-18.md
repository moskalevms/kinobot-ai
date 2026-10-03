# Экономия токенов при работе с LLM (opencode + Qwen3.8 Max, Anthropic, OpenAI): отчёт исследования
*Дата: 2026-09-18 | Источников: 20+ | Уверенность: Высокая (официальные доки провайдеров corroborated несколькими независимыми обзорами)*

## Executive Summary

Главные рычаги экономии: (1) промпт-кэширование провайдера — скидка 80–90% на повторяющийся префикс (системный промпт, схемы инструментов, история); (2) маршрутизация моделей — дешёвая модель для рутинных операций, флагман только для сложного кода (40–70% экономии); (3) гигиена контекста — короткий AGENTS.md, отключение неиспользуемых MCP, новые сессии на каждую задачу, лимиты выходных токенов; (4) асинхронный Batch API — плоские 50% скидки; (5) управление reasoning-токенами — у вашей модели `effort: xhigh` стоит глобально, это главный источник перерасхода на рутинных операциях. Комбинация методов даёт 50–90% сокращения затрат без потери качества. Ваша конфигурация уже включает ключевой механизм (`cacheControl: true` для Anthropic-совместимого endpoint Alibaba), но содержит и антипаттерны (глобальный xhigh reasoning, AGENTS.md ~2.5–3K токенов в каждой сессии).

## 1. Аудит текущей конфигурации opencode

Что изучено локально:

| Компонент | Текущее состояние | Влияние на токены |
|---|---|---|
| Провайдер | Qwen3.8 Max через Anthropic-совместимый endpoint Alibaba token-plan (`~/.config/opencode/opencode.jsonc`) | Кэширование по Anthropic-механике доступно |
| `cacheControl: true` | Включён в `providerOptions.anthropic` | ✅ Правильно: активирует cache breakpoints (~10% цены на попадания) |
| `effort: xhigh` + `reasoning: true` | Глобально для модели | ⚠️ Антипаттерн: thinking-токены (тарифицируются как output, который в ~5 раз дороже input) тратятся на каждый grep/read |
| `limit.context: 983616` | Почти 1M | ⚠️ Компакция сработает очень поздно; транскрипт успевает раздуться. Рекомендация ниже — уменьшить |
| AGENTS.md проекта | 10.5 КБ (~2.5–3K токенов) | Пересылается в КАЖДОМ ходе сессии; уже содержит правила token efficiency — хорошо, но сам объём можно сократить |
| MCP | `context7` (проект) + `firecrawl` (глобально) | Схемы инструментов обоих серверов уходят в каждый ход, даже когда не вызываются |
| `.opencodeignore` | Исключает `docs/`, `*.md` (кроме AGENTS.md), node_modules и пр. | ✅ Правильно: не даёт мусору попадать в контекст |
| Скиллы | 10 шт. | Метаданные скиллов в каждом ходе; тело грузится только при вызове — приемлемо |
| Агенты | 7 кастомных (autodev и пр.) | Воркеры в отдельных сессиях — уже хорошая практика изоляции контекста |

## 2. Быстрые победы для ВАШЕЙ конфигурации (по убыванию эффекта)

1. **Снизить `effort` с `xhigh` до `medium`/`high` глобально, а `xhigh` оставить точечно.** Reasoning-токены — самая дорогая статья (output-тариф). В opencode можно задавать модель/параметры на агента: для `plan`/`general`/рутинных сабагентов — низкий effort, для `implementer`/сложных задач — высокий. Ожидаемый эффект: −30–60% output-токенов на рутине ([Tokenminning: reasoning](https://tokenminning.ai/ides/opencode/reasoning), [Simon Willison о reasoning_effort=minimal](https://simonw.substack.com/p/gpt-5-key-characteristics-pricing)).
2. **Уменьшить `limit.context` до ~128–200K.** Пользовательский `limit.context` переопределяет каталожное значение и управляет порогом автокомпакции. Окно в 983K означает, что каждый ход пересылает гигантский транскрипт по полной цене до самого конца. Компакция раньше = дешевле каждый последующий ход ([Tokenminning: context](https://tokenminning.ai/ides/opencode/context)).
3. **Включить `compaction.prune: true`.** По умолчанию `false`. Prune вырезает устаревшие tool-выводы (`read`/`bash`/`grep` — главный раздуватель транскрипта в кодинг-агентах) без полного саммари. Плюс `compaction.auto: true` уже по умолчанию ([Tokenminning: context](https://tokenminning.ai/ides/opencode/context)).
4. **Новая сессия на каждую задачу.** Транскрипт компаундится: сообщение №201 может стоить как сообщения 1–200 вместе. Правило: context fill в сайдбаре >70% → `/compact` или новая сессия; >80% → только новая сессия ([Amnic/Anthropic](https://amnic.com/blogs/how-to-reduce-anthropic-api-costs), [Tokenminning](https://tokenminning.ai/ides/opencode/context)).
5. **Сократить AGENTS.md до ~1–1.5K токенов**, вынеся редкие детали в `docs/` и подключая их через `instructions` или ленивое чтение. Прецедент: 331 КБ AGENTS.md съедал 81% окна 128K ([GitHub issue #18037](https://github.com/anomalyco/opencode/issues/18037)). Ваши 10.5 КБ — умеренно, но это ~3K токенов × каждый ход × каждая сессия.
6. **Держать MCP-серверы выключенными, когда не нужны.** Схемы firecrawl (20+ инструментов с длинными JSON-схемами) уходят в каждый ход даже в сессиях, где веб-поиск не используется. Вариант: включать по необходимости или ограничить per-agent. Замерить накладные расходы: `opencode stats --tools` ([Tokenminning: MCP](https://tokenminning.ai/ides/opencode/mcp)).
7. **Мониторинг: `opencode stats --models --days 7`** — регулярная сверка, где реально уходят токены (input vs output, по моделям, по инструментам).
8. **Стабильный префикс для кэша.** Кэш — префиксный: любое изменение в начале (системный промпт, AGENTS.md, порядок схем инструментов) инвалидирует весь кэш дальше по тексту. Не правьте AGENTS.md/конфиг посреди активной рабочей серии запросов; группировать статический контент в начало.

Пример конфигурации (итог рекомендаций 1–3):

```jsonc
{
  "compaction": { "auto": true, "prune": true },
  "provider": {
    "bailian-token-plan-personal": {
      "models": {
        "qwen3.8-max": {
          "limit": { "context": 200000 },
          "options": { "effort": "medium" }
        }
      }
    }
  }
}
```

## 3. Универсальные методы (работают у любого провайдера)

### 3.1. Промпт-кэширование (префиксное)
Самый эффективный метод «без усилий»: повторяющийся префикс (системный промпт + схемы инструментов + начало истории) кэшируется, попадания тарифицируются в 5–10 раз дешевле. Экономия 40–90% на input-токенах. Ключевые правила: статический контент — в начало запроса, динамический — в конец; не менять модель посреди сессии (каждая модель держит свой кэш); окупается примерно со 2-го попадания (запись в кэш дороже чтения) ([NeuralTrust](https://neuraltrust.ai/blog/llm-cost-reduction-guide), [Morph](https://www.morphllm.com/llm-cost-optimization), [Amnic](https://amnic.com/blogs/how-to-reduce-anthropic-api-costs)).

### 3.2. Маршрутизация моделей (model routing / right-sizing)
Дешёвая модель (mini/flash-класс) для классификации, саммари, поиска по коду, рутинных правок; флагман — только когда качество критично. Разница в цене на порядок. Типовой микс из практики: 70% запросов на дешёвой модели, 25% на средней, 5% на флагмане ([NeuralTrust](https://neuraltrust.ai/blog/llm-cost-reduction-guide), [PremAI](https://www.premai.io/blog/llm-cost-optimization-8-strategies-that-cut-api-spend-by-80-2026-guide/)). В opencode это `small_model`, per-agent `model`, агенты plan/build.

### 3.3. Ограничение и структурирование вывода
Output-токены стоят в 4–5 раз дороже input. Методы: `max_tokens`/`max_output_tokens`, явные инструкции «отвечай кратко, только diff», structured outputs (JSON-схема вместо прозы). Практика: −50% output-токенов без потери качества ([Amnic](https://amnic.com/blogs/how-to-reduce-openai-api-costs), [PremAI](https://www.premai.io/blog/llm-cost-optimization-8-strategies-that-cut-api-spend-by-80-2026-guide/)). Ваш AGENTS.md с правилами «DIFF-ONLY OUTPUT», «TARGETED READS ONLY» — ровно этот метод, он работает.

### 3.4. Batch API (асинхронная обработка)
−50% на input и output для задач, терпящих до 24 часов: оценочные прогоны (evals), массовая обработка, генерация данных. Стекуется с кэшированием: batch + cache hit ≈ −95% на кэшированном префиксе ([Morph](https://www.morphllm.com/llm-cost-optimization)).

### 3.5. Компрессия промптов
- **LLMLingua / LLMLingua-2** (Microsoft): сжатие динамического контекста малой LM, 2–20x при минимальной потере качества; production-ready для RAG-контекстов ([LLMLingua](https://llmlingua.com/llmlingua.html), [AWS Token Efficiency Playbook](https://builder.aws.com/content/3FRlppwY0rQsApCRxEksJP0s6hX/the-token-efficiency-playbook-10-methods-to-spend-less-on-llm-inference)).
- **Сжатие tool-выводов** для кодинг-агентов: Headroom, snip, Tokenade, lean-ctx — 60–95% сокращения на логах/выводах команд ([awesome-llm-token-optimization](https://github.com/pleasedodisturb/awesome-llm-token-optimization)).
- **TOON** вместо JSON для однородных массивов данных: −30–60% токенов.
- Реалистичный эффект на уже аккуратных промптах кодинг-агентов: 12–21% ([PointFive](https://www.pointfive.co/guides/top-prompt-compression-solutions-2026)).

### 3.6. Семантическое кэширование
Кэш ответов на похожие запросы (GPTCache, Portkey и т.п.): в production-трафике значимая доля запросов — почти дубликаты. Hit rate ~45% в кейсах. Комплементарно префиксному кэшу: семантический ловит повторяющиеся ЗАПРОСЫ, префиксный — повторяющийся КОНТЕКСТ ([PointFive](https://www.pointfive.co/guides/top-prompt-compression-solutions-2026), [PremAI](https://www.premai.io/blog/llm-cost-optimization-8-strategies-that-cut-api-spend-by-80-2026-guide/)).

### 3.7. RAG и ленивая загрузка
Не класть всё в контекст — подтягивать по мере необходимости: поиск (grep/glob) вместо чтения файлов целиком, ссылки на файлы вместо их содержимого, загрузка скиллов/документов только при вызове. Anthropic формулирует это как «agentic search» вместо «context stuffing» ([Anthropic: context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)).

### 3.8. Дистилляция / fine-tuning малых моделей
Для высокочастотных узких задач: обучить малую модель на выходах флагмана (OpenAI model distillation, fine-tuning mini-моделей). Окупается на больших объёмах повторяющихся задач ([NeuralTrust](https://neuraltrust.ai/blog/llm-cost-reduction-guide)).

## 4. Anthropic-специфичные методы

Актуально и для вас: ваш endpoint Alibaba использует Anthropic-совместимый протокол с `cacheControl`.

1. **Prompt caching (явный, `cache_control: {"type": "ephemeral"}`)**: cache read = 0.1x цены input (−90%), cache write = 1.25x (5-мин TTL) или 2x (1-час TTL). Минимальный кэшируемый префикс 1024–4096 токенов (зависит от модели). До 4 breakpoints. Окупается со ~2 попаданий ([Amnic](https://amnic.com/blogs/how-to-reduce-anthropic-api-costs), [Morph](https://www.morphllm.com/llm-cost-optimization), [Anthropic docs](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)). Пример экономии: 200 запросов с 12K-префиксом — $7.20 → $0.76 (−89%).
2. **Context editing (`clear_tool_uses_20250919`)**: автоочистка старых tool-результатов из истории при превышении порога (trigger/keep/clear_at_least/exclude_tools). В 100-ходовом web-search бенчмарке: **−84% токенов** + задачи, которые раньше падали по переполнению контекста, завершаются; +39% к точности на длинных сериях (в связке с memory tool) ([Claude blog: context management](https://claude.com/blog/context-management), [platform docs](https://platform.claude.com/docs/en/build-with-claude/context-editing)).
3. **Memory tool (`memory_20250818`)**: файловая память вне контекстного окна — агент сохраняет важное до очистки истории и перечитывает по требованию. Паттерн для long-running агентов; комбинируется с context editing ([platform docs](https://platform.claude.com/docs/en/build-with-claude/context-editing), [Anthropic engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)).
4. **Programmatic Tool Calling (PTC)**: выполнение цепочек инструментов кодом внутри `code_execution`, промежуточные результаты не попадают в контекст. Официальный пример: 43 588 → 27 297 токенов (−37%) на research-задачах ([Anthropic: advanced tool use](https://www.anthropic.com/engineering/advanced-tool-use)).
5. **Token-efficient tool use**: флаг уменьшения verbosity tool-вызовов — до −70% output-токенов на tool-heavy нагрузках ([awesome-llm-token-optimization](https://github.com/pleasedodisturb/awesome-llm-token-optimization)).
6. **Message Batches API**: −50%, стекуется с кэшем до −95% ([Morph](https://www.morphllm.com/llm-cost-optimization)).
7. **Haiku для сабагентов**: дешёвая модель на read-only поиск/анализ, Sonnet/Opus только на реализацию (в Claude Code / Agent SDK — `small_fast_model`).

## 5. OpenAI-специфичные методы

1. **Prompt caching (автоматический)**: без изменения кода, для префиксов ≥1024 токенов (на старых моделях ≥2048). Скидка на кэшированный input: 50% на большинстве моделей, до 90% (0.1x) на новых флагманах. TTL 30 минут с продлением при reuse, на GPT-5-серии расширенное удержание до 24 часов. `prompt_cache_key` улучшает попадания при параллельных запросах ([OpenAI prompt caching docs](https://platform.openai.com/docs/guides/prompt-caching), [Morph](https://www.morphllm.com/llm-cost-optimization), [awesome-llm-token-optimization](https://github.com/pleasedodisturb/awesome-llm-token-optimization)).
2. **`reasoning.effort` (minimal/low/medium/high)**: управление бюджетом thinking-токенов reasoning-моделей; thinking-токены тарифицируются как output. `minimal` почти отключает размышления — для простых запросов. Резервировать ≥25K токенов на reasoning+output во избежание обрезки ([OpenAI reasoning guide](https://developers.openai.com/api/docs/guides/reasoning), [Simon Willison](https://simonw.substack.com/p/gpt-5-key-characteristics-pricing)).
3. **Batch API**: −50% на всё, SLA 24 часа, стекуется с кэшем ([Amnic](https://amnic.com/blogs/how-to-reduce-openai-api-costs)).
4. **Predicted Outputs (speculative decoding)**: при предсказуемых выходах (рефакторинг с известным шаблоном, диффы) — ускорение до 4–8x; токены оплачиваются, но меньше «черновиков» — выигрыш в латентности, при partial acceptance и в стоимости.
5. **Structured Outputs (JSON Schema)**: строгая схема = нет лишних слов и повторных попыток из-за невалидного JSON — экономия на ретраях и verbosity ([Amnic](https://amnic.com/blogs/how-to-reduce-openai-api-costs)).
6. **Model distillation + routing**: дистилляция флагманских ответов в gpt-mini/nano-класс; маршрутизация через роутер-модель. Разница mini vs flagship ~16x ([NeuralTrust](https://neuraltrust.ai/blog/llm-cost-reduction-guide)).
7. **Priority/Service tiers и `store: false`**: для полноты — гибкие тарифные слои под нагрузку; отключение хранения не экономит токены напрямую, но требуется некоторыми политиками.

## 6. Qwen / Alibaba Model Studio (ваш провайдер)

Официальные данные по Context Cache ([Alibaba docs](https://www.alibabacloud.com/help/en/model-studio/context-cache)):

| Параметр | Explicit cache | Implicit cache |
|---|---|---|
| Создание кэша | 125% цены input | 100% (без наценки) |
| Попадание в кэш | **10%** цены input (−90%) | **20%** цены input (−80%) |
| Минимум токенов | 1024 | 1024 |
| TTL | 5 минут (сбрасывается при попадании) | Неопределённый, чистится системой |
| Управление | Вручную (`cache_control`) | Автоматически, нельзя отключить |
| Взаимоисключение | Да — режимы несовместимы | Да |

Выводы для вашей связки:
- `cacheControl: true` в opencode → explicit cache: попадания по 10%. Это уже настроено — главное не ломать стабильность префикса (см. §2.8).
- TTL 5 минут с продлением при каждом попадании: длинные агентные сессии с частыми ходами держат кэш тёплым; паузы >5 мин → перезапись префикса по 125%.
- Инкрементальное кэширование: если новый запрос расширяет кэшированный префикс, доплата только за дельту (125% на новые токены, старые 10%).
- Цены Qwen3-Max на внешних хостингах с кэшем: например $1.20 → $0.24/M токенов ([DeepInfra](https://deepinfra.com/blog/qwen-api-pricing-2026-guide)) — масштаб экономии тот же, 80%.
- Для kinobot-ai (ваш проект): GigaChat/LLM-роутер с длинными системными промптами из `src/prompts/*.txt` — идеальный кандидат на explicit context cache (статический промпт >1024 токенов, повторяется на каждом запросе).

## Key Takeaways (что сделать прямо сейчас)

1. **`effort: xhigh` → `medium` глобально** (или per-agent) — самая дорогая текущая настройка; thinking-токены идут по output-тарифу на каждом тривиальном ходе.
2. **`limit.context` 983616 → ~200000** и **`compaction.prune: true`** — раньше и дешевле компакция, меньше пересылаемый транскрипт.
3. **Сессия на задачу + `/compact` на 70–80% заполнения** — не давать транскрипту компаундиться.
4. **`cacheControl` уже включён — не ломать префикс**: правки AGENTS.md/конфигов только между сессиями, статика в начало.
5. **Отключать firecrawl MCP**, когда сессия не про веб-исследование (схемы ~20 инструментов в каждом ходе).
6. **AGENTS.md: держать ≤1.5–2K токенов**, детали выносить в docs с ленивым чтением.
7. **Мониторинг**: `opencode stats --models --days 7 --tools` раз в неделю.
8. Для Anthropic-нагрузки в kinobot-ai/продакшене: explicit cache breakpoints на системные промпты (−90%), context editing + memory tool для длинных агентов (−84% в бенчмарке), Batch API (−50%) для фоновых задач.
9. Для OpenAI-нагрузки: кэш автоматический — просто держать стабильный префикс ≥1024 токенов и `prompt_cache_key`; `reasoning.effort` под задачу; structured outputs; Batch для не-реалтайма.
10. Совокупный реалистичный эффект всех мер: **50–85%** сокращения токенов/затрат ([Morph](https://www.morphllm.com/llm-cost-optimization), [PremAI](https://www.premai.io/blog/llm-cost-optimization-8-strategies-that-cut-api-spend-by-80-2026-guide/)).

## Sources

1. [Tokenminning: OpenCode hub](https://tokenminning.ai/ides/opencode) — как opencode биллит запрос; чек-лист экономии; антипаттерны.
2. [Tokenminning: OpenCode context](https://tokenminning.ai/ides/opencode/context) — compaction/prune/limit.context, сайдбар заполнения контекста.
3. [Alibaba Model Studio: Context Cache](https://www.alibabacloud.com/help/en/model-studio/context-cache) — официальные тарифы explicit/implicit кэша Qwen.
4. [Claude blog: Managing context](https://claude.com/blog/context-management) — context editing −84% токенов, memory tool +39% точности.
5. [Anthropic platform docs: Context editing](https://platform.claude.com/docs/en/build-with-claude/context-editing) — API `clear_tool_uses_20250919`, комбинация с memory tool.
6. [Anthropic: Advanced tool use](https://www.anthropic.com/engineering/advanced-tool-use) — Programmatic Tool Calling, −37% токенов.
7. [Anthropic: Effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) — agentic search, compaction, just-in-time контекст.
8. [Amnic: Reduce Anthropic API costs](https://amnic.com/blogs/how-to-reduce-anthropic-api-costs) — экономика кэша (0.1x/1.25x/2x), break-even, output 5x input.
9. [Amnic: Reduce OpenAI API costs](https://amnic.com/blogs/how-to-reduce-openai-api-costs) — Batch API, structured outputs, суммаризация истории.
10. [Morph: LLM Cost Optimization](https://www.morphllm.com/llm-cost-optimization) — 5 рычагов, −70–85%; детали кэша OpenAI (0.1x на GPT-5.6+, 30-min TTL, prompt_cache_key); batch+cache −95%.
11. [NeuralTrust: LLM Cost Reduction](https://neuraltrust.ai/blog/llm-cost-reduction-guide) — 12 стратегий; right-sizing; кэш 90%/50%.
12. [PremAI: 8 strategies](https://www.premai.io/blog/llm-cost-optimization-8-strategies-that-cut-api-spend-by-80-2026-guide/) — −60–80% в production; кейсы routing/compression.
13. [OpenAI: Reasoning guide](https://developers.openai.com/api/docs/guides/reasoning) — reasoning.effort, бюджеты токенов.
14. [OpenAI: Prompt caching](https://platform.openai.com/docs/guides/prompt-caching) — авто-кэш, минимальные префиксы, скидки.
15. [Simon Willison: GPT-5 pricing](https://simonw.substack.com/p/gpt-5-key-characteristics-pricing) — reasoning_effort=minimal, thinking как output.
16. [AWS Builder: Token Efficiency Playbook](https://builder.aws.com/content/3FRlppwY0rQsApCRxEksJP0s6hX/the-token-efficiency-playbook-10-methods-to-spend-less-on-llm-inference) — 10 методов incl. LLMLingua-2, KV-cache, CoT-компрессия.
17. [PointFive: Prompt compression solutions 2026](https://www.pointfive.co/guides/top-prompt-compression-solutions-2026) — сравнение компрессоров, семантическое кэширование.
18. [awesome-llm-token-optimization (GitHub)](https://github.com/pleasedodisturb/awesome-llm-token-optimization) — сводка: token-efficient tool use −70%, DeepSeek KV-cache −98%, инструменты сжатия tool-выводов.
19. [GitHub opencode issue #18037](https://github.com/anomalyco/opencode/issues/18037) — большой AGENTS.md съедает контекстное окно.
20. [DeepInfra: Qwen API Pricing 2026](https://deepinfra.com/blog/qwen-api-pricing-2026-guide) — context cache Qwen3-Max −80%.
21. [LLMLingua](https://llmlingua.com/llmlingua.html) — до 20x компрессия промптов.
22. [AI SDK: Dynamic prompt caching](https://ai-sdk.dev/cookbook/node/dynamic-prompt-caching) — cacheControl в Vercel AI SDK (механика, которую использует opencode).

## Methodology

Поиск: 7 запросов через firecrawl_search (общие методы, Anthropic caching/context editing, OpenAI caching/batch/reasoning, Qwen context cache, opencode-специфика, prompt compression). Полное чтение: 3 ключевых источника (Tokenminning OpenCode hub + context, Alibaba context cache docs). Локальный аудит: `opencode.json`, `~/.config/opencode/opencode.jsonc`, `.opencode/` (7 агентов, 10 скиллов), `.opencodeignore`, AGENTS.md. Под-вопросы: (1) как opencode расходует токены и какие есть настройки; (2) методы Anthropic; (3) методы OpenAI; (4) методы Qwen/Alibaba; (5) универсальные методы (компрессия, routing, batch, RAG). Ограничения: цифры скидок у сторонних обзорников (Morph, NeuralTrust) местами ссылаются на будущее ценообразование моделей — перепроверены по официальным докам провайдеров, где доступно; tokenminning — сторонний сайт, его опции конфига opencode (`compaction.prune`, `small_model`, `OPENCODE_DISABLE_AUTOCOMPACT`) сверить с вашей версией opencode перед применением (часть полей в новых версиях переехала в v2-схему `keep.tokens`/`buffer`).
