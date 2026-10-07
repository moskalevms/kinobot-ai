<#
.SYNOPSIS
    Watchdog воркера autodev: heartbeat + wall-time бюджет, вердикт, kill деревом.

.DESCRIPTION
    Запускается диспетчером DETACHED (-WindowStyle Hidden) сразу после спавна
    воркера. Периодически (PollSec) оценивает три сигнала:
      (а) процесс сам вышел            -> вердикт `exited`;
      (б) heartbeat устарел > StaleMin -> вердикт `killed-hang`  + kill деревом;
          (сразу после спавна действует grace-период: если процесс жив, heartbeat
           ещё не создан и с момента старта прошло < StaleMin — НЕ hang);
      (в) наступил deadline_at         -> вердикт `killed-walltime` + kill деревом
          (независимо от свежести heartbeat — ловушка «обёртка жива, воркер
           завис на LLM-стриме»).

    Kill выполняется ДЕРЕВОМ: `taskkill /PID <pid> /T /F` + добивание сирот
    (Win32_Process по ParentProcessId), т.к. opencode.cmd порождает node-потомков
    и одиночный Stop-Process оставляет сирот.

    Результат пишется в ОТДЕЛЬНЫЙ verdict-файл (verdict-<Id>.json). State-файл
    (autodev-state.json) в цикле ожидания НЕ перезаписывается (риск 9).

    Verdict дополнительно несёт диагностические поля `.log.err` (W5):
    `err_age_sec`/`err_size`/`err_mtime` — побочный сигнал прогресса (живой
    трафик воркера идёт в .err; stdout `.log` буферизуется и может не меняться
    часами). На решение о kill поля НЕ влияют (критерий — heartbeat/wall-time),
    но при `killed-hang` свежий .err (err_age_sec < StaleMin*60) — признак,
    что воркер был жив, а «завис» лишь heartbeat-мониторинг.

    HANG-ОТЧЁТ (W10): при вердиктах `killed-hang`/`killed-walltime` watchdog
    best-effort собирает диагностику в `backlog\state\reports\<Id>-hang-<ts>.md`
    (ts = yyyyMMdd-HHmmss): вердикт/времена, возраст heartbeat на момент
    убийства, mtime/size `.log` и `.log.err`, последние ~50 строк `.err` в
    устойчивой кодировке W4 (байты + строгий UTF-8-декодер, fallback ANSI),
    snapshot `worker`/`step`/`fix_iterations`/`current_task` из state (ТОЛЬКО
    чтение — риск 9) и дерево процессов (рекурсивно Win32_Process по
    ParentProcessId: PID/Name/первые ~200 символов CommandLine), снятое ДО
    kill. Относительный путь отчёта добавляется в verdict полем `hang_report`
    (пустая строка для `exited`/`running`). Ошибки сбора подавляются
    (try/catch в каждой секции) — отчёт не должен ронять watchdog.

.NOTES
    PowerShell 5.1 (Windows).
#>

[CmdletBinding()]
param(
    # ID задачи (используется для имени verdict-файла, если -Id не задан).
    [string]$TaskId,
    # Идентификатор единицы (для имени verdict-файла).
    [string]$Id,
    # PID процесса воркера.
    [Parameter(Mandatory = $true)][int]$WorkerPid,
    # Путь к heartbeat-файлу.
    [string]$Heartbeat,
    # Путь к файлу stderr воркера (.log.err) — для tail_err.
    [string]$ErrFile,
    # Путь к state-файлу (читаем deadline_at/started_at; НЕ пишем).
    [string]$State,
    # Порог устаревания heartbeat, минут.
    [int]$StaleMin = 10,
    # Wall-time бюджет, минут (если deadline_at не найден в state).
    [int]$BudgetMin = 90,
    # Путь к verdict-файлу. По умолчанию backlog\state\verdict-<Id>.json.
    [string]$Verdict,
    # Период поллинга, секунд.
    [int]$PollSec = 25,
    # Корень проекта (для резолва относительных путей).
    [string]$Root
)

$ErrorActionPreference = 'Continue'

# --- Корень проекта -----------------------------------------------------------
if ([string]::IsNullOrWhiteSpace($Root)) {
    $Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
}
$Root = (Resolve-Path $Root).Path

function Resolve-UnderRoot {
    param([string]$Path, [string]$Base)
    if ([string]::IsNullOrWhiteSpace($Path)) { return $Path }
    if ([System.IO.Path]::IsPathRooted($Path)) { return $Path }
    return (Join-Path $Base $Path)
}

# --- Идентификатор единицы и путь verdict-файла ------------------------------
if ([string]::IsNullOrWhiteSpace($Id)) {
    if (-not [string]::IsNullOrWhiteSpace($TaskId)) { $Id = $TaskId }
    else { $Id = 'GROUP' }
}
if ([string]::IsNullOrWhiteSpace($Verdict)) {
    $Verdict = "backlog\state\verdict-$Id.json"
}

$verdictFull = Resolve-UnderRoot $Verdict $Root
$heartbeatFull = Resolve-UnderRoot $Heartbeat $Root
$errFileFull = Resolve-UnderRoot $ErrFile $Root
$stateFull = Resolve-UnderRoot $State $Root

$fmt = 'yyyy-MM-ddTHH:mm:ss'
$now = Get-Date

# --- Читаем deadline_at / started_at из state (если есть) ---------------------
$deadline = $null
$startedAt = ''
if (-not [string]::IsNullOrWhiteSpace($stateFull) -and (Test-Path $stateFull)) {
    try {
        $sj = Get-Content $stateFull -Raw | ConvertFrom-Json
        if ($sj.worker) {
            if ($sj.worker.deadline_at) { $deadline = [datetime]::Parse($sj.worker.deadline_at) }
            if ($sj.worker.started_at_process) { $startedAt = [string]$sj.worker.started_at_process }
            elseif ($sj.worker.started_at) { $startedAt = [string]$sj.worker.started_at }
        }
    }
    catch {
        # state повреждён/недоступен — работаем от now + BudgetMin.
    }
}
if (-not $deadline) { $deadline = $now.AddMinutes($BudgetMin) }

# started_at: из state, иначе из StartTime процесса, иначе now.
if ([string]::IsNullOrWhiteSpace($startedAt)) {
    $pobj = Get-Process -Id $WorkerPid -ErrorAction SilentlyContinue
    if ($pobj) {
        try { $startedAt = $pobj.StartTime.ToString($fmt) }
        catch { $startedAt = $now.ToString($fmt) }
    }
    else { $startedAt = $now.ToString($fmt) }
}
$startedDt = $now
try { $startedDt = [datetime]::Parse($startedAt) } catch { $startedDt = $now }
$deadlineIso = $deadline.ToString($fmt)

# --- Вспомогательные функции --------------------------------------------------
# Возраст heartbeat-файла в секундах; -1 если файла нет.
function Get-HeartbeatAge {
    # Пара Test-Path -> Get-Item неатомарна: файл может исчезнуть/быть
    # заблокирован между вызовами. Глотаем исключение (-1 = «файла нет»),
    # чтобы случайный сбой чтения не убил watchdog без финального verdict.
    try {
        if (-not [string]::IsNullOrWhiteSpace($heartbeatFull) -and (Test-Path $heartbeatFull)) {
            $lw = (Get-Item $heartbeatFull).LastWriteTime
            return [int]((Get-Date) - $lw).TotalSeconds
        }
    }
    catch { return -1 }
    return -1
}

# Диагностика .err-файла (W5): возраст в секундах, размер, mtime (iso).
# Файла нет / ошибка чтения -> age_sec=-1, size=-1, mtime=''. Свежий .err —
# сильный признак жизни воркера (живой трафик идёт в .err, а не в stdout),
# но решение о kill принимает ТОЛЬКО heartbeat/wall-time — это диагностика.
function Get-ErrInfo {
    $info = [ordered]@{ age_sec = -1; size = -1; mtime = '' }
    if (-not [string]::IsNullOrWhiteSpace($errFileFull)) {
        try {
            if (Test-Path $errFileFull) {
                $it = Get-Item $errFileFull -ErrorAction Stop
                $info.age_sec = [int]((Get-Date) - $it.LastWriteTime).TotalSeconds
                $info.size = [int]$it.Length
                $info.mtime = $it.LastWriteTime.ToString($fmt)
            }
        }
        catch {
            # Файл заблокирован/исчез — оставляем значения по умолчанию.
        }
    }
    return $info
}

# Последние ~30 строк stderr-лога; пустая строка если файла нет.
function Get-TailErr {
    if (-not [string]::IsNullOrWhiteSpace($errFileFull) -and (Test-Path $errFileFull)) {
        try {
            $lines = Get-Content $errFileFull -Tail 30 -Encoding UTF8
            return ($lines -join "`n")
        }
        catch { return '' }
    }
    return ''
}

# Устойчивое чтение `.err` для hang-отчёта (W4/W10): байты + строгий
# UTF-8-декодер, fallback — системная ANSI-кодировка (старые OEM-логи);
# кириллица без каракулей U+FFFD. `Get-Content -Encoding Byte` открывает файл
# с FileShare.ReadWrite — чтение работает даже у живого воркера. Возвращает
# последние ~50 строк текста ('' если файла нет/ошибка).
function Get-ErrTail50 {
    if ([string]::IsNullOrWhiteSpace($errFileFull) -or -not (Test-Path $errFileFull)) { return '' }
    try {
        $bytes = Get-Content $errFileFull -Encoding Byte -ReadCount 0
        if (-not $bytes) { return '' }
        $strict = New-Object System.Text.UTF8Encoding($false, $true)
        try { $text = $strict.GetString($bytes) }
        catch { $text = [System.Text.Encoding]::Default.GetString($bytes) }
        $lines = $text -split "`r?`n"
        if ($lines.Count -gt 50) { $lines = $lines[($lines.Count - 50)..($lines.Count - 1)] }
        return ($lines -join "`n")
    }
    catch { return '' }
}

# Снимок дерева процессов (W10): обход от $RootPid по ParentProcessId
# (Win32_Process, BFS с защитой от циклов через множество посещённых PID).
# Для каждого процесса: PID, Name, первые ~200 символов CommandLine.
# Вызывается ДО kill — после kill дерево теряется безвозвратно.
function Get-ProcessTreeSnapshot {
    param([int]$RootPid)
    $result = New-Object System.Collections.ArrayList
    try {
        $all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    }
    catch { $all = @() }
    $visited = @{}
    $queue = New-Object System.Collections.Queue
    $queue.Enqueue($RootPid)
    while ($queue.Count -gt 0) {
        $curPid = [int]$queue.Dequeue()
        if ($visited.ContainsKey($curPid)) { continue }
        $visited[$curPid] = $true
        $name = ''
        $cl = ''
        foreach ($p in $all) {
            if ([int]$p.ProcessId -eq $curPid) {
                $name = [string]$p.Name
                $cl = [string]$p.CommandLine
                break
            }
        }
        if ($cl.Length -gt 200) { $cl = $cl.Substring(0, 200) }
        [void]$result.Add([pscustomobject]@{ proc_id = $curPid; name = $name; cmdline = $cl })
        foreach ($p in $all) {
            if (([int]$p.ParentProcessId -eq $curPid) -and -not $visited.ContainsKey([int]$p.ProcessId)) {
                $queue.Enqueue([int]$p.ProcessId)
            }
        }
    }
    return $result
}

# Сбор hang-отчёта (W10): `backlog\state\reports\<Id>-hang-<ts>.md`, все 6
# секций обязательны, каждая в своём try/catch (best-effort: сбой секции —
# заглушка, сбой всего сбора — пустой путь, verdict пишется в любом случае).
# State-файл только ЧИТАЕТСЯ (риск 9). Возвращает относительный путь отчёта
# или '' при полной неудаче.
function Write-HangReport {
    param(
        [string]$Status,
        [string]$KilledAt,
        [int]$HeartbeatAgeSec,
        [object[]]$TreeSnapshot
    )
    try {
        $reportsDir = Join-Path $Root 'backlog\state\reports'
        if (-not (Test-Path $reportsDir)) {
            New-Item -ItemType Directory -Force -Path $reportsDir | Out-Null
        }
        $tsNow = Get-Date -Format 'yyyyMMdd-HHmmss'
        $rel = "backlog\state\reports\$Id-hang-$tsNow.md"
        $full = Join-Path $Root $rel
        $parts = New-Object System.Collections.ArrayList

        [void]$parts.Add("# Hang-отчёт: $Id")
        [void]$parts.Add('')

        # Секция 1 — вердикт/причина и времена.
        try {
            [void]$parts.Add('## 1. Вердикт и времена')
            [void]$parts.Add('')
            [void]$parts.Add("- verdict: $Status")
            [void]$parts.Add("- pid: $WorkerPid")
            [void]$parts.Add("- started_at: $startedAt")
            [void]$parts.Add("- killed_at: $KilledAt")
            [void]$parts.Add("- deadline_at: $deadlineIso")
            [void]$parts.Add('')
        }
        catch { }

        # Секция 2 — возраст heartbeat на момент убийства.
        try {
            [void]$parts.Add('## 2. Heartbeat')
            [void]$parts.Add('')
            [void]$parts.Add("- heartbeat_age_sec: $HeartbeatAgeSec (-1 = файл отсутствует)")
            if (-not [string]::IsNullOrWhiteSpace($heartbeatFull) -and (Test-Path $heartbeatFull)) {
                [void]$parts.Add("- heartbeat_file: $heartbeatFull")
                [void]$parts.Add('- heartbeat_content: ' + (Get-Content $heartbeatFull -Raw))
            }
            [void]$parts.Add('')
        }
        catch { }

        # Секция 3 — mtime/size `.log` и `.log.err` (.log = путь .err минус суффикс).
        try {
            [void]$parts.Add('## 3. Логи (.log и .log.err)')
            [void]$parts.Add('')
            if (-not [string]::IsNullOrWhiteSpace($errFileFull)) {
                $logFileFull = ''
                if ($errFileFull.EndsWith('.err')) {
                    $logFileFull = $errFileFull.Substring(0, $errFileFull.Length - 4)
                }
                foreach ($f in @($logFileFull, $errFileFull)) {
                    if ([string]::IsNullOrWhiteSpace($f)) { continue }
                    try {
                        if (Test-Path $f) {
                            $it = Get-Item $f
                            [void]$parts.Add("- $f : mtime=$($it.LastWriteTime.ToString($fmt)) size=$($it.Length)")
                        }
                        else {
                            [void]$parts.Add("- $f : файл отсутствует")
                        }
                    }
                    catch {
                        [void]$parts.Add("- $f : ошибка чтения ($($_.Exception.Message))")
                    }
                }
            }
            else {
                [void]$parts.Add('- пути логов watchdog не передавались (-ErrFile пуст)')
            }
            [void]$parts.Add('')
        }
        catch { }

        # Секция 4 — хвост ~50 строк `.err` (устойчивая кодировка W4).
        try {
            [void]$parts.Add('## 4. Хвост .log.err (последние ~50 строк)')
            [void]$parts.Add('')
            [void]$parts.Add('```text')
            $tail50 = Get-ErrTail50
            if ([string]::IsNullOrWhiteSpace($tail50)) { $tail50 = '(пусто или файл недоступен)' }
            [void]$parts.Add($tail50)
            [void]$parts.Add('```')
            [void]$parts.Add('')
        }
        catch { }

        # Секция 5 — snapshot state (ТОЛЬКО чтение, не пишем — риск 9).
        try {
            [void]$parts.Add('## 5. Snapshot state')
            [void]$parts.Add('')
            if (-not [string]::IsNullOrWhiteSpace($stateFull) -and (Test-Path $stateFull)) {
                $sj = Get-Content $stateFull -Raw | ConvertFrom-Json
                [void]$parts.Add("- step: $($sj.step)")
                [void]$parts.Add("- fix_iterations: $($sj.fix_iterations)")
                [void]$parts.Add("- current_task: $($sj.current_task)")
                if ($sj.worker) {
                    [void]$parts.Add('- worker:')
                    [void]$parts.Add('')
                    [void]$parts.Add('```json')
                    [void]$parts.Add(($sj.worker | ConvertTo-Json -Depth 10))
                    [void]$parts.Add('```')
                }
            }
            else {
                [void]$parts.Add('- state-файл недоступен')
            }
            [void]$parts.Add('')
        }
        catch { }

        # Секция 6 — дерево процессов (снимок ДО kill).
        try {
            [void]$parts.Add('## 6. Дерево процессов (снимок ДО kill)')
            [void]$parts.Add('')
            if ($TreeSnapshot -and $TreeSnapshot.Count -gt 0) {
                foreach ($n in $TreeSnapshot) {
                    [void]$parts.Add("- pid=$($n.proc_id) name=$($n.name) cmdline=$($n.cmdline)")
                }
            }
            else {
                [void]$parts.Add('- (пусто: процессы не найдены)')
            }
            [void]$parts.Add('')
        }
        catch { }

        Set-Content -Path $full -Value ($parts -join "`r`n") -Encoding UTF8
        return $rel
    }
    catch { return '' }
}

# Атомарная запись verdict-файла.
function Write-VerdictFile {
    param(
        [string]$Status,
        [string]$KilledAt,
        [int]$HeartbeatAgeSec,
        [string]$TailErr,
        [int]$ErrAgeSec = -1,
        [int]$ErrSize = -1,
        [string]$ErrMtime = '',
        # W10: относительный путь hang-отчёта (пустая строка для exited/running).
        [string]$HangReportRel = ''
    )
    $obj = [ordered]@{
        status            = $Status
        pid               = $WorkerPid
        started_at        = $startedAt
        killed_at         = $KilledAt
        deadline_at       = $deadlineIso
        heartbeat_age_sec = $HeartbeatAgeSec
        # Диагностика .err (W5): возраст/размер/mtime stderr-лога на момент
        # решения; обязательны при killed-hang, -1/пусто если файла нет.
        err_age_sec       = $ErrAgeSec
        err_size          = $ErrSize
        err_mtime         = $ErrMtime
        # W10: путь hang-отчёта (только для killed-hang/killed-walltime).
        hang_report       = $HangReportRel
        tail_err          = $TailErr
    }
    $verdictDir = Split-Path -Parent $verdictFull
    if ($verdictDir -and -not (Test-Path $verdictDir)) {
        New-Item -ItemType Directory -Force -Path $verdictDir | Out-Null
    }
    $jsonText = ($obj | ConvertTo-Json -Depth 20)
    $tmp = $verdictFull + '.tmp'
    Set-Content -Path $tmp -Value $jsonText -Encoding UTF8
    Move-Item -Path $tmp -Destination $verdictFull -Force
}

# Гарантированный kill деревом процессов + добивание сирот.
function Stop-WorkerTree {
    try { & taskkill /PID $WorkerPid /T /F 2>&1 | Out-Null } catch { }
    # Если taskkill /T снял не всех — добиваем потомков по ParentProcessId.
    for ($i = 0; $i -lt 5; $i++) {
        $orphans = Get-CimInstance Win32_Process -Filter "ParentProcessId=$WorkerPid" -ErrorAction SilentlyContinue
        if (-not $orphans) { break }
        foreach ($o in $orphans) {
            try { & taskkill /PID $o.ProcessId /T /F 2>&1 | Out-Null } catch { }
        }
        Start-Sleep -Milliseconds 300
    }
}

# --- Verdict-заготовка: статус running ---------------------------------------
Write-VerdictFile -Status 'running' -KilledAt '' -HeartbeatAgeSec -1 -TailErr ''

# --- Основной цикл ожидания ---------------------------------------------------
$staleSec = $StaleMin * 60
$status = 'exited'
$hbAge = -1
$killedAt = ''
# Диагностика .err на МОМЕНТ решения (снимается до kill в ветках hang/walltime).
$errAtDecision = $null
# W10: относительный путь hang-отчёта (заполняется только в kill-ветках).
$hangReportRel = ''

while ($true) {
    $nowLoop = Get-Date
    $proc = Get-Process -Id $WorkerPid -ErrorAction SilentlyContinue

    # (а) процесс мёртв -> exited
    if (-not $proc) {
        $status = 'exited'
        $hbAge = Get-HeartbeatAge
        break
    }

    # Текущий возраст heartbeat (-1 если файла ещё нет).
    $hbAge = Get-HeartbeatAge

    # (б) зависание по heartbeat: устарел > StaleMin, либо файла нет и grace истёк.
    # Приоритет вердиктов: если в одной итерации heartbeat устарел И наступил
    # дедлайн — побеждает `killed-hang` (ветка проверяется раньше wall-time):
    # это диагностически более точная причина сбоя.
    $hang = $false
    if ($hbAge -ge 0) {
        if ($hbAge -gt $staleSec) { $hang = $true }
    }
    else {
        # Heartbeat ещё не создан: grace-период от момента старта процесса.
        $sinceStart = ($nowLoop - $startedDt).TotalSeconds
        if ($sinceStart -ge $staleSec) { $hang = $true }
    }
    if ($hang) {
        $status = 'killed-hang'
        $killedAt = $nowLoop.ToString($fmt)
        # W5: фиксируем свежесть .err ДО kill — если err_age_sec < StaleMin*60,
        # воркер, вероятно, был жив (трафик в .err), и hang — следствие смерти
        # heartbeat-монитора; решение о kill это НЕ отменяет (wall-time/heartbeat
        # — единственный критерий), поле нужно для диагностики.
        $errAtDecision = Get-ErrInfo
        # W10: снимок дерева процессов ОБЯЗАТЕЛЬНО до kill (после — не собрать).
        $treeSnap = @(Get-ProcessTreeSnapshot -RootPid $WorkerPid)
        Stop-WorkerTree
        # W10: hang-отчёт best-effort (ошибки сбора не роняют watchdog).
        $hangReportRel = Write-HangReport -Status $status -KilledAt $killedAt `
            -HeartbeatAgeSec $hbAge -TreeSnapshot $treeSnap
        break
    }

    # (в) wall-time: наступил дедлайн — kill независимо от свежести heartbeat.
    if ($nowLoop -ge $deadline) {
        $status = 'killed-walltime'
        $killedAt = $nowLoop.ToString($fmt)
        $errAtDecision = Get-ErrInfo
        # W10: снимок дерева процессов ДО kill + hang-отчёт best-effort.
        $treeSnap = @(Get-ProcessTreeSnapshot -RootPid $WorkerPid)
        Stop-WorkerTree
        $hangReportRel = Write-HangReport -Status $status -KilledAt $killedAt `
            -HeartbeatAgeSec $hbAge -TreeSnapshot $treeSnap
        break
    }

    Start-Sleep -Seconds $PollSec
}

# --- Финальный verdict --------------------------------------------------------
$tailErr = Get-TailErr
# Пересчитываем возраст heartbeat на момент решения (для exited/hang/walltime).
$finalHbAge = Get-HeartbeatAge
if ($finalHbAge -ge 0) { $hbAge = $finalHbAge }
# Диагностика .err (W5): для kill-веток — снимок ДО kill, для exited — сейчас.
if (-not $errAtDecision) { $errAtDecision = Get-ErrInfo }
Write-VerdictFile -Status $status -KilledAt $killedAt -HeartbeatAgeSec $hbAge -TailErr $tailErr `
    -ErrAgeSec $errAtDecision.age_sec -ErrSize $errAtDecision.size -ErrMtime $errAtDecision.mtime `
    -HangReportRel $hangReportRel

# Краткий ASCII-вывод (для логов/отладки).
Write-Output "STATUS=$status"
Write-Output "PID=$WorkerPid"
Write-Output "HEARTBEAT_AGE_SEC=$hbAge"
Write-Output "ERR_AGE_SEC=$($errAtDecision.age_sec)"
Write-Output "ERR_SIZE=$($errAtDecision.size)"
Write-Output "VERDICT=$Verdict"
if (-not [string]::IsNullOrWhiteSpace($hangReportRel)) {
    Write-Output "HANG_REPORT=$hangReportRel"
}
