---
description: "Полный автономный цикл по бэклогу: итеративно по группам приоритета (P0→P1→P2), каждая задача/группа — в НОВОЙ сессии opencode (воркер autodev-worker): OpenSpec-спека → реализация → сборка+тесты → ревью → повторные тесты, с отметкой чекбоксов. Вход: ID задачи, группа (P0/P1/P2), all, или свободное описание."
agent: autodev
---

Режим: **MAKE-TASK**.

Область/задача: $ARGUMENTS

Выполни режим MAKE-TASK пайплайна `autodev-pipeline` СКВОЗНЯКОМ и автономно
(без вопросов человеку, БЕЗ остановки на «planning boundary» OpenSpec). Ты —
ДИСПЕТЧЕР: сам код не пишешь, на каждую единицу работы спавнишь новую сессию
opencode (раздел скилла «Сессионная модель»).

**Резолв области** по `$ARGUMENTS`:
- `resume` — режим RESUME: прочитай чекпоинт `backlog/state/autodev-state.json`
  (нет/неполный → восстанови контекст по артефактам: ближайший бэклог с `- [ ]`,
  активные `openspec/changes/`); воркер жив (свежий heartbeat-файл И нет
  финального `verdict-<ID>.json`, RESUME п.2 скилла) → присоединись к коротким
  проверкам verdict, НЕ спавнь дубль; воркер мёртв и единица не завершена →
  перезапуск воркера (`attempt+1`); сверь с реальностью (артефакты главнее state)
  и продолжи с шага остановки, не дублируя артефакты и не обнуляя
  `fix_iterations`;
- один ID (`A1`, `B2`) — выполнить эту задачу;
- группа (`P0`/`P1`/`P2`) — все невыполненные `- [ ]` задачи группы;
- `all` или пусто — группы по возрастанию: **P0 → P1 → P2**;
- свободное описание — сначала делегируй `backlog-planner` (превратить описание в
  бэклог с чекбоксами), затем область = `all` (или P0 по умолчанию).

**Строгий гейт**: к группе P(N+1) переходи ТОЛЬКО после того, как группа P(N)
полностью реализована и верифицирована (все задачи `- [x]`, verifier OVERALL
PASS). Если группа не зелёная (цикл исправления ≤3 исчерпан) — СТОП, не переходи
дальше, верни отчёт с блокерами.

**Цикл каждой единицы работы** (задача; группа — если задачи плотно связаны одним
OpenSpec-изменением, `unit: group` в state):
1. Обнови state: `current_task`/`queue`/`unit`, `step: spawn`, очисти
   `worker_result`; проверь, что предыдущий воркер не жив (heartbeat устарел
   ИЛИ есть финальный `verdict-<ID>.json`).
2. **Спавн НОВОЙ сессии** через `.opencode\scripts\Start-Worker.ps1`
   (`powershell -NoProfile -ExecutionPolicy Bypass -File
   .opencode\scripts\Start-Worker.ps1 -Id <ID> -TaskId <ID> -Backlog ... -State
   ... -Report backlog\state\reports\<ID>.md -BudgetMin 90`; для группы —
   `-Group P0 -Tasks A1,A2 -Id P0`). Скрипт сам спавнит `opencode run --agent
   autodev-worker --auto` detached с логом
   `backlog\state\logs\worker-<ID>-<ts>.log`, поднимает heartbeat-монитор и
   пишет в state `worker.pid/log/heartbeat/started_at_process/deadline_at`.
3. **Ожидание**: сразу после спавна запусти detached watchdog
   `.opencode\scripts\Wait-Worker.ps1` (`-WorkerPid/-Heartbeat/-ErrFile/-State
   -StaleMin 10 -BudgetMin 90 -Verdict backlog\state\verdict-<ID>.json`; пути —
   из вывода Start-Worker.ps1), затем КОРОТКИЕ проверки `verdict-<ID>.json`
   раз в ~2-5 мин (каждая <30 с, без блокирующих циклов дольше 5 мин):
   `exited` → вердикт по `worker_result`; `killed-hang`/`killed-walltime` →
   перезапуск единицы `attempt+1` ≤3 с паузами 30/60/120 с, лимит исчерпан →
   `status: interrupted`. Kill выполняет только watchdog и только деревом
   процессов — диспетчер не убивает воркера одним PID вручную.
4. **Вердикт** после `exited` по `worker_result.status` + отчёту воркера +
   чекбоксам: `completed` и `- [x]` → следующая единица; `failed` И `last_error`
   НАЧИНАЕТСЯ с `requires operator: ` → **blocked** (не провал качества):
   зафиксировать в финальном отчёте (ID задачи + что требуется от оператора) и
   перейти к СЛЕДУЮЩЕЙ единице, группу НЕ стопить; гейт-СТОП только если
   blocked-задача блокирует остальные незакрытые (у них `зависит: <ID>`
   заблокированной); blocked НЕ ретраится перезапуском воркера (подробно —
   SKILL.md «Ожидание и вердикт» Шаг 3); прочие `failed`/блокер → СТОП группы
   (гейт); `interrupted`/нет `worker_result` → инфраструктурный сбой, перезапуск
   единицы ≤3 с паузами 30/60/120 с (бюджет исправлений не тратится).

Воркер в своей сессии выполняет: `implementer` (OpenSpec propose+apply, код,
тесты) → `verifier` (ruff+mypy+pytest) → [исправления ≤3] → `reviewer` → [при
REQUEST_CHANGES/blocker/major — `implementer` + повторный `verifier`] →
чекбоксы `- [x]`/`✅` в бэклоге и `tasks.md` → state (`step: task-done`,
`worker_result`) → отчёт в REPORT.

**Завершение**: заархивировать OpenSpec-изменения (`openspec-archive-change`);
**НЕ делать git commit/push**. Финальный отчёт (собирай из
`backlog/state/reports/*.md` и state, не из полных логов): закрытые
задачи/группы (чекбоксы), путь к бэклогу, результаты обоих прогонов verifier по
задачам, вердикты reviewer, minor/nit, где остановились (если гейт не пройден),
готовность к коммиту.
