<#
.SYNOPSIS
    Спавн воркера автономного пайплайна autodev + heartbeat-монитор + запись state.

.DESCRIPTION
    Инкапсулирует спавн `opencode run --agent autodev-worker --auto` (detached,
    с редиректом stdout/stderr в лог), поднимает ОТДЕЛЬНЫЙ detached
    heartbeat-монитор (Heartbeat-Loop.ps1) и атомарно дописывает в state-файл
    (worker) поля pid/log/report/started_at/heartbeat/started_at_process/deadline_at,
    сохраняя существующий attempt и не затирая прочие поля.

    КОДИРОВКА ЛОГОВ (W4): спавн выполняется через обёртку
    `cmd /c "chcp 65001 >nul && <команда>"` в ОТДЕЛЬНОЙ скрытой консоли
    (-WindowStyle Hidden), поэтому дочерний процесс пишет stdout/stderr в
    перенаправленные файлы `.log`/`.log.err` в UTF-8 (без `chcp` — каракули
    cp866/cp1251). `chcp` не влияет на консоль диспетчера (у воркера своя
    скрытая консоль). Диспетчеру возвращается PID обёртки cmd.exe, которая
    живёт ровно пока жив дочерний воркер (`cmd /c` ждёт его) — семантика
    heartbeat/watchdog и kill деревом (`taskkill /T`) сохраняется.
    ОГРАНИЧЕНИЕ обёртки: аргументы с пробелами кавычатся двойными кавычками,
    ВЛОЖЕННЫЕ двойные кавычки внутри аргумента недопустимы (cmd трактует их
    неоднозначно) — используйте одинарные кавычки внутри значений аргументов.

    ЗАЩИТА ОТ ДУБЛЯ (W6): перед спавном скрипт читает `worker` из state и
    проверяет его живость через `.opencode/scripts/Test-WorkerAlive.ps1`
    (PID + StartTime ± 2 с + CommandLine содержит `autodev-<Id>`/
    `autodev-worker`). Если прежний воркер жив — дубль НЕ спавнится:
    выводится `DUPLICATE_SKIPPED=True` + данные существующего воркера
    (PID/LOG/HEARTBEAT/VERDICT/DEADLINE_AT/STARTED_AT_PROCESS), state и файлы
    не перезаписываются, выход с кодом 0.

    ТЕСТ-ХУК (только ручное тестирование): необязательные параметры
    -DummyCommand/-DummyArgs позволяют вместо реального `opencode.cmd run`
    заспавнить фиктивный процесс (напр. `powershell -Command Start-Sleep N`),
    чтобы прогнать связку спавн→heartbeat→watchdog БЕЗ запуска реальных
    opencode-сессий. В проде эти параметры НЕ передаются: при пустом
    -DummyCommand спавнится обычный `opencode.cmd run` (прод-поведение).
    Обёртка `cmd /c chcp 65001`, редирект stdout/stderr, heartbeat-монитор и
    запись state работают одинаково в обеих ветках.

    HANG-ОТЧЁТ (W10): необязательный параметр -HangReport — путь к
    hang-отчёту предыдущего зависания (`verdict.hang_report` из
    Wait-Worker.ps1). При непустом значении к сообщению спавна дописывается
    ` HANG_REPORT=<путь>` (ASCII), а путь дополнительно выводится диспетчеру.
    Диспетчер передаёт его при перезапуске единицы (attempt+1) после
    `killed-hang`/`killed-walltime`, чтобы воркер учёл причину прошлого
    зависания; при первом спавне параметр НЕ передаётся. В ветке
    DUPLICATE_SKIPPED игнорируется (сообщение не перестраивается).

    БЮДЖЕТ ГРУППЫ (W11): если -BudgetMin НЕ передан явно
    ($PSBoundParameters.ContainsKey('BudgetMin') = ложно) И задан режим группы
    (-Group непуст И -Tasks содержит N>1 задач), бюджет вычисляется
    автоматически по формуле `90 + 45*(N-1)` мин (дефолт задачи — 90 мин,
    каждая дополнительная задача группы — +45 мин). Явно переданный
    -BudgetMin ВСЕГДА побеждает (и для задачи, и для группы).

.NOTES
    PowerShell 5.1 (Windows). Весь вывод — ASCII (пути/PID/ISO-время).
#>

[CmdletBinding()]
param(
    # Идентификатор единицы для имён файлов/title. По умолчанию — из TaskId,
    # иначе из Group, иначе литерал "GROUP".
    [string]$Id,
    # ID одной задачи (режим задачи).
    [string]$TaskId,
    # Имя группы приоритета, напр. P0 (режим группы).
    [string]$Group,
    # Список задач группы через запятую, напр. "W1,W2,W3" (режим группы).
    [string]$Tasks,
    # Путь к файлу бэклога (BACKLOG=).
    [Parameter(Mandatory = $true)][string]$Backlog,
    # Путь к state-файлу autodev-state.json (STATE=).
    [Parameter(Mandatory = $true)][string]$State,
    # Путь к файлу отчёта воркера (REPORT=).
    [Parameter(Mandatory = $true)][string]$Report,
    # Wall-time бюджет на единицу работы, минут (deadline = now + BudgetMin).
    # W11: если параметр НЕ передан явно, для группы (-Group + -Tasks, N>1)
    # бюджет вычисляется автоматически: 90 + 45*(N-1); явный -BudgetMin
    # всегда побеждает.
    [int]$BudgetMin = 90,
    # Корень проекта. По умолчанию — на два уровня выше каталога скрипта.
    [string]$Root,
    # W10: необязательный путь к hang-отчёту предыдущего зависания
    # (verdict.hang_report). Непустой -> суффикс ` HANG_REPORT=<путь>` в
    # сообщении спавна + строка HANG_REPORT= в выводе. При первом спавне
    # (и в ветке DUPLICATE_SKIPPED) не используется/игнорируется.
    [string]$HangReport,
    # ТОЛЬКО РУЧНОЕ ТЕСТИРОВАНИЕ: если задан — используется как -FilePath в
    # Start-Process ВМЕСТО `opencode.cmd` (напр. 'powershell'). В проде пуст.
    [string]$DummyCommand,
    # ТОЛЬКО РУЧНОЕ ТЕСТИРОВАНИЕ: -ArgumentList для -DummyCommand ВМЕСТО
    # `run --agent autodev-worker ...` (напр. @('-NoProfile','-Command','Start-Sleep 8')).
    # Если -DummyCommand задан, а -DummyArgs пуст — используется @(). В проде пуст.
    [string[]]$DummyArgs
)

$ErrorActionPreference = 'Stop'

# --- Корень проекта -----------------------------------------------------------
if ([string]::IsNullOrWhiteSpace($Root)) {
    # .opencode\scripts -> .opencode -> корень
    $Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
}
$Root = (Resolve-Path $Root).Path

# --- Идентификатор единицы ----------------------------------------------------
if ([string]::IsNullOrWhiteSpace($Id)) {
    if (-not [string]::IsNullOrWhiteSpace($TaskId)) { $Id = $TaskId }
    elseif (-not [string]::IsNullOrWhiteSpace($Group)) { $Id = $Group }
    else { $Id = 'GROUP' }
}

# --- Автоформула бюджета группы (W11) -----------------------------------------
# Если -BudgetMin НЕ передан явно (в PS 5.1 значение по умолчанию неотличимо от
# явного — проверяем $PSBoundParameters) И задан режим группы с N>1 задачами —
# бюджет = 90 + 45*(N-1) мин (дефолт задачи 90 мин, каждая дополнительная
# задача группы +45 мин). Явно переданный -BudgetMin всегда побеждает.
if (-not $PSBoundParameters.ContainsKey('BudgetMin')) {
    if (-not [string]::IsNullOrWhiteSpace($Group) -and -not [string]::IsNullOrWhiteSpace($Tasks)) {
        $taskList = @($Tasks -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' })
        if ($taskList.Count -gt 1) {
            $BudgetMin = 90 + 45 * ($taskList.Count - 1)
        }
    }
}

# --- Вспомогательные функции --------------------------------------------------
# Резолвит относительный путь относительно корня; абсолютный оставляет как есть.
function Resolve-UnderRoot {
    param([string]$Path, [string]$Base)
    if ([string]::IsNullOrWhiteSpace($Path)) { return $Path }
    if ([System.IO.Path]::IsPathRooted($Path)) { return $Path }
    return (Join-Path $Base $Path)
}

# Устанавливает свойство PSCustomObject, создавая его при отсутствии.
function Set-Prop {
    param($Object, [string]$Name, $Value)
    if ($Object.PSObject.Properties[$Name]) {
        $Object.$Name = $Value
    }
    else {
        $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value -Force
    }
}

# Кавычит аргумент для внутренней командной строки cmd /c (W4): аргументы с
# пробелами/табами оборачиваются в двойные кавычки; уже кавыченные и аргументы
# без пробелов передаются как есть. Вложенные двойные кавычки НЕ поддерживаются
# (см. ограничение в шапке скрипта).
function Format-CmdArg {
    param([string]$Arg)
    if ([string]::IsNullOrEmpty($Arg)) { return '""' }
    if ($Arg -match '[\s\t]' -and -not ($Arg.StartsWith('"') -and $Arg.EndsWith('"'))) {
        return '"' + $Arg + '"'
    }
    return $Arg
}

# --- Pre-spawn проверка (W6): живой прежний воркер — дубль НЕ спавним ---------
# Читаем state (ТОЛЬКО чтение, без записи); если worker содержит pid и
# started_at_process — сверяем идентичность через Test-WorkerAlive.ps1
# (PID + StartTime + CommandLine с маркером autodev-<Id>/autodev-worker).
# Жив -> предупреждение DUPLICATE_SKIPPED=True, эхо данных существующего
# воркера и выход 0; диспетчер присоединяется к коротким проверкам verdict.
$stateFull = Resolve-UnderRoot $State $Root
$aliveScript = Join-Path $Root '.opencode\scripts\Test-WorkerAlive.ps1'
if ((Test-Path $stateFull) -and (Test-Path $aliveScript)) {
    $dupOk = $false
    try {
        $preJson = Get-Content $stateFull -Raw | ConvertFrom-Json
        $preWorker = $preJson.worker
        if ($preWorker -and $preWorker.pid -and $preWorker.started_at_process) {
            & powershell -NoProfile -ExecutionPolicy Bypass -File $aliveScript `
                -WorkerPid ([int]$preWorker.pid) `
                -StartedAt ([string]$preWorker.started_at_process) `
                -Pattern "autodev-$Id" | Out-Null
            if ($LASTEXITCODE -eq 0) { $dupOk = $true }
        }
    }
    catch {
        # state повреждён / проверка не удалась — считаем прежнего воркера
        # мёртвым и разрешаем спавн (ложный спавн безопаснее ложного «жив»).
        $dupOk = $false
    }
    if ($dupOk) {
        Write-Output 'WARNING=worker-alive-duplicate-not-spawned'
        Write-Output 'DUPLICATE_SKIPPED=True'
        Write-Output "PID=$($preWorker.pid)"
        Write-Output "ID=$Id"
        if ($preWorker.log) { Write-Output "LOG=$($preWorker.log)"; Write-Output "LOG_ERR=$($preWorker.log).err" }
        if ($preWorker.heartbeat) { Write-Output "HEARTBEAT=$($preWorker.heartbeat)" }
        Write-Output "VERDICT=backlog\state\verdict-$Id.json"
        if ($preWorker.deadline_at) { Write-Output "DEADLINE_AT=$($preWorker.deadline_at)" }
        Write-Output "STARTED_AT_PROCESS=$($preWorker.started_at_process)"
        exit 0
    }
}

# --- Каталоги рантайм-артефактов ---------------------------------------------
$logsDir = Join-Path $Root 'backlog\state\logs'
$reportsDir = Join-Path $Root 'backlog\state\reports'
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null
New-Item -ItemType Directory -Force -Path $reportsDir | Out-Null

# --- ASCII-сообщение спавна ---------------------------------------------------
if (-not [string]::IsNullOrWhiteSpace($Group)) {
    # Режим группы: GROUP=<P> TASKS=<a,b> BACKLOG=... STATE=... REPORT=...
    $msg = "GROUP=$Group TASKS=$Tasks BACKLOG=$Backlog STATE=$State REPORT=$Report"
}
else {
    # Режим одной задачи: TASK=<id> BACKLOG=... STATE=... REPORT=...
    $msg = "TASK=$TaskId BACKLOG=$Backlog STATE=$State REPORT=$Report"
}
# W10: ссылка на hang-отчёт предыдущего зависания — необязательный суффикс
# сообщения спавна (ASCII-путь; воркер читает отчёт при идемпотентном
# продолжении после attempt+1).
if (-not [string]::IsNullOrWhiteSpace($HangReport)) {
    $msg = $msg + " HANG_REPORT=$HangReport"
}

# --- Пути логов ---------------------------------------------------------------
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'
$logRel = "backlog\state\logs\worker-$Id-$ts.log"
$logFull = Join-Path $Root $logRel
$logErrFull = $logFull + '.err'

# --- Спавн воркера (detached, с редиректом потоков) --------------------------
# Прод-путь: `opencode.cmd run ...`. Тест-хук: если задан -DummyCommand — спавним
# его (с -DummyArgs как аргументы) ВМЕСТО opencode.cmd; ТОЛЬКО для ручного
# тестирования на фиктивных процессах, в проде -DummyCommand пуст. Обе ветки
# оборачиваются ЕДИНООБРАЗНО в `cmd /c "chcp 65001 >nul && <команда>"` (W4):
# дочерний процесс получает свою скрытую консоль с UTF-8-кодировкой и пишет
# .log/.log.err в UTF-8. -WindowStyle Hidden (вместо прежнего -NoNewWindow) —
# консоль воркера отделена от консоли диспетчера. Редирект stdout/stderr и
# -PassThru идентичны в обеих ветках.
if (-not [string]::IsNullOrWhiteSpace($DummyCommand)) {
    $exe = $DummyCommand
    if ($DummyArgs) { $procArgs = $DummyArgs } else { $procArgs = @() }
}
else {
    $exe = (Get-Command opencode.cmd).Source
    $procArgs = @('run', '--agent', 'autodev-worker', '--auto', '--title', "autodev-$Id", $msg)
}
$innerParts = @(Format-CmdArg $exe)
foreach ($a in $procArgs) { $innerParts += (Format-CmdArg ([string]$a)) }
$innerCmd = 'chcp 65001 >nul && ' + ($innerParts -join ' ')
$p = Start-Process -FilePath $env:ComSpec `
    -ArgumentList @('/c', $innerCmd) `
    -WorkingDirectory $Root `
    -RedirectStandardOutput $logFull `
    -RedirectStandardError $logErrFull `
    -PassThru -WindowStyle Hidden

# --- Время старта процесса и дедлайн -----------------------------------------
$now = Get-Date
$deadline = $now.AddMinutes($BudgetMin)
$startedProcess = $now
try {
    if ($p.StartTime) { $startedProcess = $p.StartTime }
}
catch {
    # StartTime может быть недоступен — используем текущее время.
    $startedProcess = $now
}

$fmt = 'yyyy-MM-ddTHH:mm:ss'
$nowIso = $now.ToString($fmt)
$deadlineIso = $deadline.ToString($fmt)
$startedProcIso = $startedProcess.ToString($fmt)

# --- Heartbeat-файл и запуск detached-монитора -------------------------------
$hbRel = "backlog\state\heartbeat-$Id.txt"
$hbFull = Join-Path $Root $hbRel
$hbScript = Join-Path $Root '.opencode\scripts\Heartbeat-Loop.ps1'

# Монитор живёт ОТДЕЛЬНО от воркера и от сессии диспетчера: -WindowStyle Hidden
# (не -NoNewWindow), чтобы переживать таймауты инструментов диспетчера.
# -StartTime (W6) — StartTime процесса воркера для сверки идентичности
# (PID+StartTime); -ErrFile (W5) — путь к .log.err для побочного сигнала
# прогресса err_mtime/err_size в heartbeat-строке.
$monitorArgs = @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass',
    '-File', $hbScript,
    '-WorkerPid', "$($p.Id)",
    '-HeartbeatPath', $hbFull,
    '-PeriodSec', '180',
    '-StartTime', $startedProcIso,
    '-ErrFile', $logErrFull
)
Start-Process -FilePath 'powershell' -ArgumentList $monitorArgs -WindowStyle Hidden | Out-Null

# --- Атомарное обновление state (worker) -------------------------------------
# $stateFull уже вычислен в pre-spawn проверке; перечитываем state СВЕЖИМ
# (между проверкой и спавном его мог обновить другой процесс).
if (Test-Path $stateFull) {
    $json = Get-Content $stateFull -Raw | ConvertFrom-Json
}
else {
    $json = [PSCustomObject]@{}
}

$worker = $json.worker
if (-not $worker) { $worker = [PSCustomObject]@{} }

# Сохраняем существующий attempt (не обнуляем); по умолчанию — 1.
$attempt = 1
if ($worker.PSObject.Properties['attempt'] -and ($null -ne $worker.attempt)) {
    $attempt = $worker.attempt
}

Set-Prop $worker 'pid' $p.Id
Set-Prop $worker 'log' $logRel
Set-Prop $worker 'report' $Report
Set-Prop $worker 'attempt' $attempt
Set-Prop $worker 'started_at' $nowIso
Set-Prop $worker 'heartbeat' $hbRel
Set-Prop $worker 'started_at_process' $startedProcIso
Set-Prop $worker 'deadline_at' $deadlineIso

Set-Prop $json 'worker' $worker
Set-Prop $json 'updated_at' $nowIso

# Атомарно: пишем во временный файл (Set-Content -Encoding UTF8), затем rename.
$stateTmp = $stateFull + '.tmp'
$json | ConvertTo-Json -Depth 20 | Set-Content -Path $stateTmp -Encoding UTF8
Move-Item -Path $stateTmp -Destination $stateFull -Force

# --- Путь verdict-заготовки (пишется watchdog'ом Wait-Worker.ps1) ------------
$verdictRel = "backlog\state\verdict-$Id.json"

# --- Вывод для диспетчера (ASCII) --------------------------------------------
Write-Output "PID=$($p.Id)"
Write-Output "ID=$Id"
Write-Output "LOG=$logRel"
Write-Output "LOG_ERR=$logRel.err"
Write-Output "HEARTBEAT=$hbRel"
Write-Output "VERDICT=$verdictRel"
Write-Output "DEADLINE_AT=$deadlineIso"
Write-Output "STARTED_AT_PROCESS=$startedProcIso"
if (-not [string]::IsNullOrWhiteSpace($HangReport)) {
    Write-Output "HANG_REPORT=$HangReport"
}
