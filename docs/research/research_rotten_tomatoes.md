# Rotten Tomatoes для рекомендательного движка: исследование и план улучшений

*Дата: 2026-09-08 | Источников: 12+ | Уверенность: высокая (ключевые факты
подтверждены эмпирически на api.kinopoisk.dev с ключом проекта)*

## Executive Summary

1. **Tomatometer — это доля положительных рецензий критиков, а не средняя
   оценка.** Фильм «Fresh» при ≥60% положительных рецензий; с апреля 2025 RT
   больше не публикует среднюю оценку критиков 0–10 — только процент.
2. **Официальный API RT закрыт**: с 2022 доступ только по одобрению заявки,
   лицензия для коммерческих интеграций — от ~$60 000/год. Реальные варианты
   для MVP: **OMDb API** (возвращает Tomatometer % в поле `Ratings`,
   бесплатно 1000 запросов/день, от $1/мес, лицензия CC BY-NC 4.0) либо
   неофициальные RapidAPI-прокси (серая зона).
3. **Сюрприз: в api.kinopoisk.dev уже есть оценка кинокритиков**
   (`rating.filmCritics`, `votes.filmCritics`) — и ваш клиент её уже
   получает (selectFields `rating`/`votes` возвращают вложенные поля), но
   код её игнорирует. Это не RT, а собственная агрегация Кинопоиска в шкале
   0–10 (эмпирически ближе к Metacritic). **Фаза 0 улучшений не требует ни
   нового API, ни затрат.**
4. RT-данные хорошо покрывают только англоязычный широкий прокат; для
   российских фильмов Tomatometer почти всегда отсутствует — в ветке
   `is_russian_search` сигнал неприменим.

---

## 1. Как формируется рейтинг Rotten Tomatoes

### Tomatometer (критики)

- Штат кураторов RT еженедельно собирает рецензии «Tomatometer-approved»
  критиков и изданий; каждая рецензия вручную классифицируется как
  Fresh/Rotten (плюс самоподача рецензий одобренными критиками)
  ([rottentomatoes.com/about](https://www.rottentomatoes.com/about)).
- **Tomatometer = % положительных рецензий.** ≥60% → Fresh (красный томат),
  <60% → Rotten (зелёная клякса), мало рецензий → «No score yet».
- Средняя оценка критиков по шкале 0–10 **удалена в апреле 2025** — остался
  только процент ([Wikipedia](https://en.wikipedia.org/wiki/Rotten_Tomatoes)).
- Порог показа оценки (обновление 2024): привязан к кассовому прогнозу —
  40 рецензий для фильмов с прогнозом $120M+, 20 — для $60M, 10 — для
  остальных/фестивальных
  ([RT Editorial](https://editorial.rottentomatoes.com/article/rotten-tomatoes-score-update/)).

### Certified Fresh (знак качества критиков)

- Tomatometer ≥75%, минимум 5 рецензий Top Critics;
- широкий прокат — ≥80 рецензий, ограниченный/стриминг — ≥40;
- сериалы — по сезонам, ≥20 критиков (5 из них Top Critics);
- статус теряется при падении ниже 70%
  ([rottentomatoes.com/about](https://www.rottentomatoes.com/about)).

### Popcornmeter (зрители) и Verified Hot

- Popcornmeter = % пользователей, поставивших ≥3.5 звезды из 5.
- Для фильмов с продажей билетов через партнёров (Fandango) показывается
  **Verified Audience Score** — только от зрителей с подтверждённой покупкой
  билета (защита от review-bombing); отдельный «All»-score тоже доступен.
- **Verified Hot**: VAS ≥90% + ≥500 верифицированных оценок (широкий прокат)
  или ≥250 (ограниченный).

### Критика методологии (важно для использования в ранжировании)

- **Бинарность**: рецензия 6/10 и 10/10 дают одинаковый вклад в процент;
  «свежие» 80% могут означать массу средних 6/10
  ([Limelight](https://www.thelimelight.app/blog/rotten-tomatoes-vs-imdb-vs-metacritic),
  [The Record](https://record.goshen.edu/opinion/the-rotten-tomatoes-problem)).
- Review-bombing зрительских оценок — историческая проблема; Verified
  Ratings её частично снимают, но выборка смещена к покупателям билетов
  Fandango ([Reddit r/boxoffice](https://www.reddit.com/r/boxoffice/comments/1ct8h8n/)).
- Систематические смещения: критики строже к жанровому кино (хоррор,
  комиксы), процентные оценки RT и средние IMDb/Metacritic коррелируют, но
  не взаимозаменяемы.

**Вывод для движка:** Tomatometer полезен как *фильтр/бейдж* («одобрено
критиками»), а не как замена числовому рейтингу в сортировке. Смешивать
процент со шкалой 0–10 напрямую нельзя — нужна нормализация.

---

## 2. Источники RT-данных: сравнение

| Источник | Что даёт | Доступ | Цена | Легальность                                                                                                              |
|---|---|---|---|--------------------------------------------------------------------------------------------------------------------------|
| **Официальный RT API** | Tomatometer, audience score, рецензии | только approved developers с 2022, free-тариф US-only | лицензия от ~$60 000/год ([jaebradley/rotten_tomatoes_client](https://github.com/jaebradley/rotten_tomatoes_client)) | ✅, но нереально для MVP                                                                                                  |
| **OMDb API** | `Ratings: [{Source: "Rotten Tomatoes", Value: "91%"}, {Metacritic}]`, IMDb ID | ключ по email/Patreon | free 1000 зап/день; от $1/мес → 100k/день | ✅ данные, но **CC BY-NC 4.0 — только некоммерческое использование** ([omdbapi.com](https://www.omdbapi.com/apikey.aspx)) |
| RapidAPI «RT API» (неофициальные) | скрейп RT | ключ RapidAPI | от $0–10/мес | ⚠️ скрейпинг против ToS RT, нестабильно                                                                                  |
| Скрейпинг rottentomatoes.com | всё | — | — | ❌ нарушение ToS, Cloudflare-защита                                                                                       |
| **api.kinopoisk.dev (уже подключён!)** | `rating.filmCritics` 0–10 + `votes.filmCritics` — агрегация критиков Кинопоиска | текущий ключ | 0 | ✅                                                                                                                        |

OMDb-ответ реально содержит RT-процент (подтверждено примерами разных лет,
включая 2024:
[Cornell INFO2951](https://info2951.infosci.cornell.edu/ae/ae-11-omdb-api-A.html),
[Reddit/Obsidian 2024](https://www.reddit.com/r/ObsidianMD/comments/1ff7vdr/)).
Join-ключ — IMDb ID, который kinopoisk.dev отдаёт в `externalId.imdb`.

---

## 3. Эмпирические факты об api.kinopoisk.dev v1.4 (проверено ключом проекта, 2026-09-08)

OpenAPI-спек: `https://api.kinopoisk.dev/documentation` (X-API-KEY).

Модель `Rating`: `kp, imdb, tmdb, filmCritics, russianFilmCritics, await`;
модель `Votes` — те же поля. `filmCritics` — «рейтинг кинокритиков» (0–10),
`russianFilmCritics` — отдельно российские критики.

Измерения по базе (~957 705 фильмов `type=movie`):

| Запрос | Результат | Вывод |
|---|---|---|
| `notNullFields=rating.filmCritics` | 91 159 (9.5%) | поле заполнено у ~9.5% фильмов (включая нули) |
| `votes.filmCritics=5-1000` | 21 425 (2.2%) | реально полезный критический сигнал — лишь у ~2% |
| `notNullFields=externalId.imdb` | 663 534 (69%) | покрытие join-ключа для OMDb |
| `rating.filmCritics=7.5-10` (и `7-10`) | total не меняется | **диапазонный фильтр по filmCritics молча игнорируется (баг API)** |
| `sortField=rating.filmCritics&sortType=-1` | работает | сортировка доступна |

Примеры значений: «Побег из Шоушенка» — filmCritics 8.4 при 147 голосах
критиков (RT Tomatometer у него 91%) → **filmCritics — это средняя оценка
0–10, не процент RT**; «Матрица» — 7.4, «Бэтмен: Начало» — 6.9 (близко к
Metacritic 73/70). «Апокалипсис сегодня» — 4.8 при 5 голосах: при малом
числе рецензий значения ненадёжны.

Ловушки, подтверждённые тестами:
- Сортировка по `rating.filmCritics` без порога голосов выводит мусор
  (10.0 при 1–2 рецензиях) — обязателен `votes.filmCritics ≥ 5–10`.
- `filmCritics = 0` у фильмов без данных — ноль нужно трактовать как
  «нет данных», а не как провальную оценку (иначе weighted_rating сломается).
- Текущий код **уже получает** эти поля: `selectFields=rating,votes`
  возвращает вложенные объекты целиком (`src/kinopoisk_client.py:79-80`).

---

## 4. План улучшений рекомендательного движка

### Фаза 0 — «бесплатные критики» через уже имеющиеся данные (0.5–1 день)

Без новых API и зависимостей. Цель — критический сигнал в ранжировании и выдаче.

1. **`src/utils/movie_filter.py`, `get_weighted_rating` (строка 81)**:
   - читать `rating.filmCritics` и `votes.filmCritics`; `0/None` → данных нет;
   - при `votes.filmCritics ≥ 10`: мягкая коррекция
     `score = base + 0.2 * (filmCritics - base)` (шринк-к консенсусу
     критиков; коэффициент 0.2 консервативен — критики систематически строже);
   - при `filmCritics ≥ 8 и votes ≥ 20` — небольшой бонус к порядку
     (аналог «Certified Fresh»); при `filmCritics ≤ 3 и votes ≥ 20` —
     демотирование (критический провал при высоком зрительском рейтинге —
     типичный маркер культового трэша, его лучше не ставить в топ).
2. **`src/recommendation_engine.py`**: в `_format_movies_list` прокинуть
   `critics_rating` + `critics_votes` в итоговый dict (рядом с
   `rating_source`).
3. **Триггер «одобено критиками»**: слова «критик» в запросе →
   `sortField=rating.filmCritics`, `notNullFields=rating.filmCritics`,
   `votes.filmCritics=10-100000` на стороне API (фильтр голосов работает!) +
   локальный порог `filmCritics ≥ 7` (диапазонный фильтр рейтинга сломан).
   Правки: `kinopoisk_client.search_movies` (параметры sort/notNull/votes),
   `recommendation_engine`, fallback-классификатор/промпт извлечения параметров.

Ограничение фазы 0: покрытие ~2% базы (21k фильмов) — сигнал сработает для
популярного западного кино, что совпадает с профилем выдач (приоритет
англоязычных стран).

### Фаза 1 — настоящий Tomatometer через OMDb (2–3 дня)

1. **Юридическая проверка**: лицензия OMDb CC BY-NC 4.0 — некоммерческое
   использование. Для MVP-бота без монетизации — приемлемо; при
   коммерциализации потребуется лицензия RT (от $60k/год — фактически
   означает отказ от RT). Зафиксировать решение в `.env.example`
   (`OMDB_API_KEY`, `ENABLE_RT_SCORES=true/false`).
2. **Новый модуль `src/omdb_client.py`** (по образцу `kinopoisk_client.py`:
   aiohttp + tenacity, таймаут 5–10 с):
   - `GET https://www.omdbapi.com/?i={imdb_id}&apikey=...`;
   - парсинг `Ratings[]` → `rt_score: int|None` («91%» → 91), `metascore`;
   - при `Response != "True"` или отсутствии RT → None, без исключений.
3. **Join**: добавить `externalId` в `selectFields`
   (`src/kinopoisk_client.py:79-81,129-131,173-175`); покрытие IMDb ID 69%.
4. **Обогащать только финал**: вызывать OMDb для ≤13 фильмов после
   `filter_movies_by_quality` (а не для 250 кандидатов) — `asyncio.gather`
   с семафором (3–5 одновременных). Латентность: +100–300 мс на выдачу.
5. **Кэш в PostgreSQL** (уже развёрнут; in-memory не переживает рестарт —
   известный недостаток из §9 docs/recommendation_algorithm.md):
   ```sql
   CREATE TABLE rt_scores (
     imdb_id TEXT PRIMARY KEY,
     rt_score SMALLINT,          -- NULL = данных нет
     metascore SMALLINT,
     fetched_at TIMESTAMPTZ DEFAULT now()
   );
   ```
   TTL 30–90 дней (Tomatometer стабилен после проката). Отрицательное
   кэширование обязательно (фильмы без RT не дёргаем повторно). Лимит free-
   тарифа 1000/день: кэш + 13 запросов/выдача ≈ 75 выдач/день без ключа —
   для MVP с малым трафиком может хватить, иначе $1/мес → 100k/день.
   Синхронизировать схему в `src/models/database.py` **и** `init_db.py`
   (ловушка из AGENTS.md).
6. **Использование в ранжировании** (`movie_filter.py` /
   `recommendation_engine.py`):
   - бейдж в выдаче: «🍅 91%» рядом с рейтингом (в `_generate_list_response`
     и в ответе intent=info);
   - мягкий буст: при `rt_score ≥ 75` (аналог Certified Fresh) — небольшой
     подъём в сортировке; при `rt_score ≤ 40` — демотирование в
     англоязычной ветке;
   - **не применять** при `is_russian_search` и для фильмов без IMDb ID —
   деградация должна быть незаметной (просто нет бейджа).
7. **Отказоустойчивость**: любой сбой OMDb → выдача без RT-бейджей
   (принцип «исключение → пустой результат» из movie_agent.py распространить
   как «исключение → None»).

### Фаза 2 — опциональные развития (после валидации фаз 0–1)

- **Audience score (Popcornmeter)**: легально и дёшево недоступен (OMDb не
  отдаёт `tomatoUserMeter` — поля удалены; официальный API закрыт).
  Практическая замена — `rating.imdb`/`rating.kp` уже в движке. Не делать.
- **Персонализация порога**: статистика кликов «Подробнее» по фильмам с
  бейджем 🍅 против без (таблицы статистики в PG уже есть) — решение,
  повышать ли вес критиков.
- **Отдельный интент/пресет «выбор критиков»** в админке — подборки с
  `sortField=rating.filmCritics`.
- При росте нагрузки: предвычисление RT-скоров фоновой задачей (ночные
  батчи по популярным фильмам из топ-250) вместо on-demand запросов.

### Итоговая формула ранжирования (если выполнены все фазы)

Обозначения: `base` — текущий `get_weighted_rating` (КП/IMDB с фолбэками,
шкала 0–10); `fc` — `rating.filmCritics` (считается данным только при
`votes.filmCritics ≥ 10`, ноль → нет данных); `rt` — Tomatometer % из OMDb
(есть только при наличии IMDb ID и не в российской ветке).

```
1. Гейт (без изменений): фильм проходит фильтр, если base ≥ min_rating.

2. Шринк к консенсусу критиков (последовательно, оба шага опциональны):
   s1 = base + 0.2 · (fc      − base)    если fc есть, иначе s1 = base
   s2 = s1   + 0.2 · (rt/10   − s1  )    если rt есть, иначе s2 = s1

3. Ступени «Certified Fresh / критический провал» (аддитивно, клампятся):
   tier = clamp( (+0.3 если fc ≥ 8 и votes.fc ≥ 20)
               + (+0.3 если rt ≥ 75)
               + (−0.3 если fc ≤ 3 и votes.fc ≥ 20)
               + (−0.3 если rt ≤ 40),  −0.3, +0.3 )

4. score = clamp(s2 + tier, 0, 10)

5. Сортировка (как сейчас): (country_priority, score ↓), где
   country_priority = 0/1/2 по англоязычным странам при
   prioritize_english_speaking=True; в российской ветке — только score ↓.
```

Свойства: без `fc`/`rt` формула вырождается ровно в текущее поведение
(обратно совместимо); каждый новый источник сдвигает оценку не более чем
на 20% разницы; жёсткие бусты/пени ограничены ±0.3 (≈ пол-позиции рейтинга).
Коэффициенты 0.2/±0.3 — стартовые, тюнинг через статистику кликов (фаза 2).

---

## Key Takeaways

- Начните с **фазы 0**: `rating.filmCritics` уже приходит в ответах API —
  это «бесплатный» критический сигнал без единого нового запроса.
- Настоящий RT добавляйте через **OMDb + кэш в PostgreSQL + только для
  финальных 13 фильмов**; проверьте некоммерческую лицензию CC BY-NC.
- Tomatometer — процент, а не оценка: используйте как **бейдж и мягкий
  буст/гейт**, не смешивайте со шкалой 0–10 без нормализации.
- Помните о багах kinopoisk.dev: фильтр `rating.filmCritics` игнорируется,
  `votes.filmCritics` работает; `0` = нет данных; сортировка по критикам
  без порога голосов даёт мусор.
- Для российского контента RT-сигнал не работает в принципе — ветка
  `is_russian_search` должна оставаться на КП.

## Источники

1. [Rotten Tomatoes — About](https://www.rottentomatoes.com/about) — официальные определения Tomatometer, Certified Fresh, Popcornmeter, Verified Hot.
2. [RT Editorial — Score Update (2024)](https://editorial.rottentomatoes.com/article/rotten-tomatoes-score-update/) — новые пороги показа оценок (10/20/40 рецензий по кассовому прогнозу).
3. [Wikipedia — Rotten Tomatoes](https://en.wikipedia.org/wiki/Rotten_Tomatoes) — методология, удаление средней оценки 0–10 (апрель 2025), статус API (restricted с 2022).
4. [jaebradley/rotten_tomatoes_client (GitHub)](https://github.com/jaebradley/rotten_tomatoes_client) — лицензия официального API от $60k/год.
5. [OMDb API — ключи/тарифы](https://www.omdbapi.com/apikey.aspx) — free 1000/день, Patreon от $1/мес, CC BY-NC 4.0.
6. [Cornell INFO2951 — пример OMDb-ответа](https://info2951.infosci.cornell.edu/ae/ae-11-omdb-api-A.html) — `Ratings` с Rotten Tomatoes %.
7. [Reddit r/ObsidianMD (2024)](https://www.reddit.com/r/ObsidianMD/comments/1ff7vdr/) — актуальный OMDb-ответ с RT.
8. [minimaxir — Movie Review Aggregator Ratings](https://minimaxir.com/2016/01/movie-revenue-ratings/) — анализ корреляций RT/IMDb/Metacritic.
9. [Limelight — RT vs IMDb vs Metacritic](https://www.thelimelight.app/blog/rotten-tomatoes-vs-imdb-vs-metacritic) — критика бинарности Tomatometer.
10. [The Record — The Rotten Tomatoes problem](https://record.goshen.edu/opinion/the-rotten-tomatoes-problem) — критика методологии.
11. [Hacker News — «magic formula» RT](https://news.ycombinator.com/item?id=37424663) — расхождение критики/зрители как сигнал.
12. OpenAPI-спек api.kinopoisk.dev (`/documentation`, получен с ключом проекта) + живые запросы к `/v1.4/movie` — все цифры раздела 3.

## Методология

Выполнено 8 поисковых запросов (web + GitHub-индекс), глубоко прочитаны
5 источников (RT About, RT Editorial, Wikipedia, OMDb, OpenAPI kinopoisk.dev).
Ключевые утверждения о kinopoisk.dev проверены живыми запросами к API с
ключом из `.env` проекта (7 измерений, результаты воспроизводимы).
Пробелы: точный источник данных `rating.filmCritics` Кинопоиска официально
не документирован (эмпирически значения близки к Metacritic); наличие RT в
ответе OMDb на free-ключе следует проверить при получении своего ключа.
