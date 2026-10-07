<#
.SYNOPSIS
    Ручной тест-харнесс скриптов autodev: Start-Worker / Heartbeat-Loop /
    Wait-Worker / Test-WorkerAlive.

.DESCRIPTION
    Прогоняет связку спавн -> heartbeat -> watchdog НА ФИКТИВНЫХ процессах
    (powershell Start-Sleep), БЕЗ запуска реальных `opencode run`-сессий и БЕЗ
    обращений к сети. Все артефакты пишутся во временный каталог
    ($env:TEMP\autodev-wtest-<guid>); настоящий backlog\state\autodev-state.json,
    heartbeat- и verdict-файлы НЕ трогаются.

    Тесты W4-W6 (T8-T12): поля heartbeat-строки (start_time/err_mtime/err_size),
    Test-WorkerAlive (живой dummy / чужой StartTime — имитация переиспользования
    PID), pre-spawn защита от дубля в Start-Worker (DUPLICATE_SKIPPED=True),
    UTF-8-кодировка кириллицы в .log.err после спавна через обёртку
    `cmd /c chcp 65001`, err-поля verdict при killed-hang.

    Тесты W10/W11 (T13-T16): hang-отчёт `backlog\state\reports\<Id>-hang-<ts>.md`
    при killed-hang/killed-walltime (все 6 секций, кириллица .err без каракулей,
    дерево процессов, snapshot state, state не перезаписан), поле `hang_report`
    в verdict (пустое для exited), параметр -HangReport в Start-Worker
    (HANG_REPORT= в выводе), автоформула бюджета группы 90+45*(N-1) при
    неявном -BudgetMin, приоритет явного -BudgetMin, дефолт 90 для одной задачи.

    Каждый тест обёрнут в try/finally: в finally гарантированно убиваются все
    созданные dummy-процессы ДЕРЕВОМ (taskkill /PID <pid> /T /F) и heartbeat-
    мониторы, чтобы не осталось сирот. Временный каталог удаляется в конце.

    Для детерминизма интервалы короткие (PollSec=1, PeriodSec=1). Устаревание
    heartbeat в тесте hang имитируется выставлением старого LastWriteTime
    (now-15мин) при StaleMin=10; wall-time — BudgetMin=0 (дедлайн в прошлом).

    Итог: PASSED <n>/<total>, FAILED <n>, OVERALL: PASS|FAIL; exit 0 при PASS,
    иначе exit 1. Каждый тест печатает [PASS] <имя> / [FAIL] <имя>: <причина>.

.NOTES
    PowerShell 5.1 (Windows). Запуск:
      powershell -NoProfile -ExecutionPolicy Bypass -File .opencode\scripts\tests\Invoke-WorkerScriptsTests.ps1
#>

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

# --- Каталоги и скрипты -------------------------------------------------------
# Исходные скрипты лежат на уровень выше tests\ (т.е. .opencode\scripts).
$SrcScripts = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

# Временная песочница: всё пишем сюда, настоящий backlog\state не трогаем.
$Temp = Join-Path $env:TEMP ('autodev-wtest-' + [guid]::NewGuid())
# Копия скриптов внутри песочницы: нужна, чтобы Start-Worker (Root=Temp) находил
# Heartbeat-Loop.ps1 по пути <Root>\.opencode\scripts\Heartbeat-Loop.ps1.
$TempScripts = Join-Path $Temp '.opencode\scripts'

$fmt = 'yyyy-MM-ddTHH:mm:ss'

# Счётчики результатов и списки PID для гарантированной зачистки в finally.
$script:Pass = 0
$script:Fail = 0
$script:Total = 0
$script:CleanupPids = @()      # все созданные тестом процессы
$script:CleanupWorkers = @()   # PID воркеров (чтобы добить их heartbeat-мониторы)

# --- Вспомогательные функции --------------------------------------------------

# Зарегистрировать PID для kill в finally.
function Register-Pid {
    param([int]$ProcId)
    if ($ProcId -gt 0) { $script:CleanupPids += $ProcId }
}

# Зарегистрировать PID воркера (для добоя его heartbeat-монитора).
function Register-Worker {
    param([int]$WorkerPid)
    if ($WorkerPid -gt 0) { $script:CleanupWorkers += $WorkerPid }
}

# Убить процесс ДЕРЕВОМ (taskkill /T /F), подавив ошибки.
function Stop-TreeById {
    param([int]$ProcId)
    if ($ProcId -le 0) { return }
    try { & taskkill /PID $ProcId /T /F 2>&1 | Out-Null } catch { }
}

# Добить heartbeat-мониторы, запущенные для конкретного воркера
# (powershell-процессы, чья CommandLine содержит 'Heartbeat-Loop.ps1' и '-WorkerPid <pid>').
function Stop-MonitorsForWorker {
    param([int]$WorkerPid)
    if ($WorkerPid -le 0) { return }
    try {
        $procs = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue
        foreach ($p in $procs) {
            $cl = [string]$p.CommandLine
            if ($cl -and $cl.Contains('Heartbeat-Loop.ps1') -and ($cl -match "-WorkerPid\s+$WorkerPid\b")) {
                try { & taskkill /PID $p.ProcessId /T /F 2>&1 | Out-Null } catch { }
            }
        }
    } catch { }
}

# Страховка: добить ЛЮБые powershell-процессы, чья CommandLine ссылается на
# песочницу (heartbeat-мониторы с -HeartbeatPath <Temp>\..., родительские
# скрипты T6 и т.п.). Сам харнесс не задевает (в его cmdline нет $Temp).
function Stop-TempOrphans {
    try {
        $procs = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue
        foreach ($p in $procs) {
            $cl = [string]$p.CommandLine
            if ($cl -and $cl.Contains($Temp)) {
                try { & taskkill /PID $p.ProcessId /T /F 2>&1 | Out-Null } catch { }
            }
        }
    } catch { }
}

# Засечь dummy-процесс `powershell Start-Sleep <Seconds>`, вернуть его PID.
function Start-DummySleep {
    param([int]$Seconds)
    $p = Start-Process -FilePath 'powershell' `
        -ArgumentList @('-NoProfile', '-Command', "Start-Sleep $Seconds") `
        -PassThru -WindowStyle Hidden
    return $p.Id
}

# Поллинг условия до таймаута; возвращает $true/$false.
# (Scriptblock исполняется в контексте вызывающей функции — динамический scope.)
function Wait-Until {
    param([scriptblock]$Condition, [double]$TimeoutSec = 5, [int]$IntervalMs = 200)
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $TimeoutSec) {
        if (& $Condition) { return $true }
        Start-Sleep -Milliseconds $IntervalMs
    }
    return [bool](& $Condition)
}

# Обёртка теста: try/catch/finally. В finally — гарантированный kill деревом
# всех созданных процессом и heartbeat-мониторов + страховочная зачистка песочницы.
function Invoke-Test {
    param([string]$Name, [scriptblock]$Body)
    $script:Total++
    $script:CleanupPids = @()
    $script:CleanupWorkers = @()
    try {
        & $Body
        $script:Pass++
        Write-Output "[PASS] $Name"
    }
    catch {
        $script:Fail++
        Write-Output "[FAIL] ${Name}: $($_.Exception.Message)"
    }
    finally {
        foreach ($wp in $script:CleanupWorkers) { Stop-MonitorsForWorker -WorkerPid $wp }
        foreach ($cp in $script:CleanupPids) { Stop-TreeById -ProcId $cp }
        Stop-TempOrphans
    }
}

# --- Подготовка песочницы -----------------------------------------------------
New-Item -ItemType Directory -Force -Path $Temp | Out-Null
New-Item -ItemType Directory -Force -Path $TempScripts | Out-Null
Copy-Item (Join-Path $SrcScripts 'Start-Worker.ps1') -Destination $TempScripts -Force
Copy-Item (Join-Path $SrcScripts 'Heartbeat-Loop.ps1') -Destination $TempScripts -Force
Copy-Item (Join-Path $SrcScripts 'Wait-Worker.ps1') -Destination $TempScripts -Force
Copy-Item (Join-Path $SrcScripts 'Test-WorkerAlive.ps1') -Destination $TempScripts -Force

$startScript = Join-Path $TempScripts 'Start-Worker.ps1'
$hbScript = Join-Path $TempScripts 'Heartbeat-Loop.ps1'
$waitScript = Join-Path $TempScripts 'Wait-Worker.ps1'
$aliveScript = Join-Path $TempScripts 'Test-WorkerAlive.ps1'

# --- T1: жизненный цикл Heartbeat-Loop ---------------------------------------
$t1 = {
    $dummyPid = Start-DummySleep -Seconds 6
    Register-Pid -ProcId $dummyPid
    Register-Worker -WorkerPid $dummyPid

    $hb = Join-Path $Temp 'hb-T1.txt'
    $startedAt = (Get-Date).ToString($fmt)

    $mon = Start-Process -FilePath 'powershell' `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $hbScript,
            '-WorkerPid', "$dummyPid", '-HeartbeatPath', $hb, '-PeriodSec', '1', '-StartedAt', $startedAt) `
        -PassThru -WindowStyle Hidden
    Register-Pid -ProcId $mon.Id

    # heartbeat должен появиться в пределах ~4 с и содержать pid/start_time/ts.
    # Параметр передаётся СТАРЫМ именем -StartedAt — проверяем алиас (W6).
    if (-not (Wait-Until { Test-Path $hb } 4)) { throw 'heartbeat-файл не появился за 4 с' }
    $c = Get-Content $hb -Raw
    if (-not $c.Contains("pid=$dummyPid")) { throw "в heartbeat нет pid=$dummyPid" }
    if (-not $c.Contains("start_time=$startedAt")) { throw "в heartbeat нет start_time=$startedAt" }
    if (-not $c.Contains('ts=')) { throw 'в heartbeat нет ts=' }

    # убиваем воркер -> монитор должен записать финальный heartbeat и выйти (~4 с)
    Stop-TreeById -ProcId $dummyPid
    if (-not (Wait-Until { -not (Get-Process -Id $mon.Id -ErrorAction SilentlyContinue) } 4)) {
        throw 'монитор не завершился за 4 с после смерти воркера'
    }
    if (-not (Test-Path $hb)) { throw 'финальный heartbeat отсутствует' }
    $c2 = Get-Content $hb -Raw
    if (-not $c2.Contains("pid=$dummyPid")) { throw 'финальный heartbeat не содержит pid' }
}

# --- T2: Start-Worker спавнит dummy и корректно пишет state ------------------
$t2 = {
    $stateFile = Join-Path $Temp 'state-T2.json'
    $backlog = Join-Path $Temp 'backlog-T2.md'
    $report = Join-Path $Temp 'report-T2.md'
    Set-Content -Path $backlog -Value '# backlog' -Encoding UTF8
    Set-Content -Path $report -Value '' -Encoding UTF8

    # предзаполненный state: attempt=2 + прочие поля (должны сохраниться)
    $pre = [ordered]@{
        phase     = 'test'
        group     = 'P0'
        extra_top = 42
        worker    = [ordered]@{ attempt = 2; note = 'keepme' }
    }
    $pre | ConvertTo-Json -Depth 20 | Set-Content -Path $stateFile -Encoding UTF8

    $out = & $startScript -DummyCommand 'powershell' `
        -DummyArgs @('-NoProfile', '-Command', 'Start-Sleep 8') `
        -State $stateFile -Backlog $backlog -Report $report `
        -Id 'TEST' -BudgetMin 90 -Root $Temp

    # (а) вывод содержит PID=<число>
    $pidLine = $out | Where-Object { $_ -match '^PID=\d+$' } | Select-Object -First 1
    if (-not $pidLine) { throw "в выводе Start-Worker нет строки PID=<число>: $($out -join ' | ')" }
    $workerPid = [int]($pidLine -replace '^PID=', '')
    Register-Pid -ProcId $workerPid
    Register-Worker -WorkerPid $workerPid

    # (б) state дополнен, attempt СОХРАНЁН (=2), прочие поля не потеряны
    $sj = Get-Content $stateFile -Raw | ConvertFrom-Json
    if ($sj.worker.pid -ne $workerPid) { throw "worker.pid=$($sj.worker.pid) != $workerPid" }
    if ([string]::IsNullOrWhiteSpace($sj.worker.heartbeat)) { throw 'worker.heartbeat пуст' }
    if ([string]::IsNullOrWhiteSpace($sj.worker.started_at_process)) { throw 'worker.started_at_process пуст' }
    if ([string]::IsNullOrWhiteSpace($sj.worker.deadline_at)) { throw 'worker.deadline_at пуст' }
    if ($sj.worker.attempt -ne 2) { throw "worker.attempt=$($sj.worker.attempt), ожидался 2 (не сохранён)" }
    if ($sj.worker.note -ne 'keepme') { throw 'worker.note потерян' }
    if ($sj.phase -ne 'test') { throw 'поле phase потеряно' }
    if ($sj.extra_top -ne 42) { throw 'поле extra_top потеряно' }

    # (в) процесс с этим PID жив
    if (-not (Get-Process -Id $workerPid -ErrorAction SilentlyContinue)) { throw "процесс $workerPid не жив" }

    # (г) heartbeat-файл появился в пределах ~6 с (монитор поднят)
    $hbFull = Join-Path $Temp $sj.worker.heartbeat
    if (-not (Wait-Until { Test-Path $hbFull } 6)) { throw "heartbeat-файл $hbFull не появился за 6 с" }
}

# --- T3: Wait-Worker -> exited (процесс сам вышел) ---------------------------
$t3 = {
    $dummyPid = Start-DummySleep -Seconds 3
    Register-Pid -ProcId $dummyPid

    $stateFile = Join-Path $Temp 'state-T3.json'
    $now = Get-Date
    $st = [ordered]@{
        worker = [ordered]@{
            deadline_at        = $now.AddMinutes(5).ToString($fmt)
            started_at_process = $now.ToString($fmt)
        }
    }
    $st | ConvertTo-Json -Depth 20 | Set-Content -Path $stateFile -Encoding UTF8

    $verdict = Join-Path $Temp 'verdict-T3.json'
    & $waitScript -WorkerPid $dummyPid -State $stateFile -PollSec 1 -BudgetMin 5 -Verdict $verdict | Out-Null

    if (-not (Test-Path $verdict)) { throw 'verdict-файл не создан' }
    $vj = Get-Content $verdict -Raw | ConvertFrom-Json
    if ($vj.status -ne 'exited') { throw "status=$($vj.status), ожидался exited" }
}

# --- T4: Wait-Worker -> killed-walltime (BudgetMin=0, дедлайн в прошлом) -----
$t4 = {
    $dummyPid = Start-DummySleep -Seconds 60
    Register-Pid -ProcId $dummyPid

    $verdict = Join-Path $Temp 'verdict-T4.json'
    & $waitScript -WorkerPid $dummyPid -BudgetMin 0 -PollSec 1 -Verdict $verdict | Out-Null

    if (-not (Test-Path $verdict)) { throw 'verdict-файл не создан' }
    $vj = Get-Content $verdict -Raw | ConvertFrom-Json
    if ($vj.status -ne 'killed-walltime') { throw "status=$($vj.status), ожидался killed-walltime" }
    if (-not (Wait-Until { -not (Get-Process -Id $dummyPid -ErrorAction SilentlyContinue) } 5)) {
        throw "dummy $dummyPid жив после walltime-kill"
    }
}

# --- T5: Wait-Worker -> killed-hang (heartbeat устарел, StaleMin=10) ---------
$t5 = {
    $dummyPid = Start-DummySleep -Seconds 60
    Register-Pid -ProcId $dummyPid

    $hb = Join-Path $Temp 'hb-T5.txt'
    Set-Content -Path $hb -Value "pid=$dummyPid;started_at=;ts=x" -Encoding UTF8
    # имитируем устаревание: LastWriteTime = now-15мин (не ждём реально)
    (Get-Item $hb).LastWriteTime = (Get-Date).AddMinutes(-15)

    $verdict = Join-Path $Temp 'verdict-T5.json'
    & $waitScript -WorkerPid $dummyPid -Heartbeat $hb -StaleMin 10 -BudgetMin 90 -PollSec 1 -Verdict $verdict | Out-Null

    if (-not (Test-Path $verdict)) { throw 'verdict-файл не создан' }
    $vj = Get-Content $verdict -Raw | ConvertFrom-Json
    if ($vj.status -ne 'killed-hang') { throw "status=$($vj.status), ожидался killed-hang" }
    # W5: без -ErrFile err-поля verdict заполнены заглушкой -1 (файла нет).
    if ($vj.err_age_sec -ne -1) { throw "err_age_sec=$($vj.err_age_sec), ожидался -1 (ErrFile не передавался)" }
    if ($vj.err_size -ne -1) { throw "err_size=$($vj.err_size), ожидался -1 (ErrFile не передавался)" }
    if (-not (Wait-Until { -not (Get-Process -Id $dummyPid -ErrorAction SilentlyContinue) } 5)) {
        throw "dummy $dummyPid жив после hang-kill"
    }
}

# --- T6: kill ДЕРЕВОМ родителя + потомка (сирота) ----------------------------
$t6 = {
    $childPidFile = Join-Path $Temp 'childpid-T6.txt'
    $parentScript = Join-Path $Temp 'spawn-parent-T6.ps1'
    # родитель сам порождает дочерний Start-Sleep 60 и пишет его PID в файл
    $parentBody = @'
param([string]$ChildPidFile)
$c = Start-Process powershell -ArgumentList '-NoProfile','-Command','Start-Sleep 60' -PassThru -WindowStyle Hidden
$c.Id | Out-File -FilePath $ChildPidFile -Encoding ascii
Start-Sleep 60
'@
    Set-Content -Path $parentScript -Value $parentBody -Encoding UTF8

    $parent = Start-Process -FilePath 'powershell' `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $parentScript, '-ChildPidFile', $childPidFile) `
        -PassThru -WindowStyle Hidden
    $parentPid = $parent.Id
    Register-Pid -ProcId $parentPid

    # даём ~3 с, читаем PID дочернего процесса
    if (-not (Wait-Until { Test-Path $childPidFile } 5)) { throw 'файл childpid не создан родителем' }
    Start-Sleep -Milliseconds 300
    $childPid = [int]((Get-Content $childPidFile -Raw).Trim())
    Register-Pid -ProcId $childPid

    $verdict = Join-Path $Temp 'verdict-T6.json'
    & $waitScript -WorkerPid $parentPid -BudgetMin 0 -PollSec 1 -Verdict $verdict | Out-Null

    # и родитель, и потомок должны быть мертвы (kill деревом)
    $dead = Wait-Until {
        (-not (Get-Process -Id $parentPid -ErrorAction SilentlyContinue)) -and
        (-not (Get-CimInstance Win32_Process -Filter "ProcessId=$childPid" -ErrorAction SilentlyContinue))
    } 6
    if (-not $dead) { throw "родитель $parentPid или потомок $childPid живы после tree-kill" }
}

# --- T7: tail_err попадает в verdict -----------------------------------------
$t7 = {
    $errFile = Join-Path $Temp 'worker-T7.log.err'
    Set-Content -Path $errFile -Value @('line one', 'MARKER-ERR-123', 'line three') -Encoding UTF8

    $dummyPid = Start-DummySleep -Seconds 2
    Register-Pid -ProcId $dummyPid

    $stateFile = Join-Path $Temp 'state-T7.json'
    $now = Get-Date
    $st = [ordered]@{
        worker = [ordered]@{
            deadline_at        = $now.AddMinutes(5).ToString($fmt)
            started_at_process = $now.ToString($fmt)
        }
    }
    $st | ConvertTo-Json -Depth 20 | Set-Content -Path $stateFile -Encoding UTF8

    $verdict = Join-Path $Temp 'verdict-T7.json'
    & $waitScript -WorkerPid $dummyPid -ErrFile $errFile -State $stateFile -PollSec 1 -BudgetMin 5 -Verdict $verdict | Out-Null

    if (-not (Test-Path $verdict)) { throw 'verdict-файл не создан' }
    $vj = Get-Content $verdict -Raw | ConvertFrom-Json
    if ($vj.status -ne 'exited') { throw "status=$($vj.status), ожидался exited" }
    if ([string]::IsNullOrEmpty($vj.tail_err) -or -not $vj.tail_err.Contains('MARKER-ERR-123')) {
        throw "tail_err не содержит MARKER-ERR-123: '$($vj.tail_err)'"
    }
}

# --- T8 (W5/W6): heartbeat содержит start_time/err_mtime/err_size ------------
$t8 = {
    $dummyPid = Start-DummySleep -Seconds 6
    Register-Pid -ProcId $dummyPid
    Register-Worker -WorkerPid $dummyPid

    # живой .err-файл известного размера
    $errFile = Join-Path $Temp 'worker-T8.log.err'
    Set-Content -Path $errFile -Value 'err-line-T8' -Encoding UTF8
    $errLen = (Get-Item $errFile).Length

    $hb = Join-Path $Temp 'hb-T8.txt'
    $st = (Get-Date).AddMinutes(-3).ToString($fmt)

    $mon = Start-Process -FilePath 'powershell' `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $hbScript,
            '-WorkerPid', "$dummyPid", '-HeartbeatPath', $hb, '-PeriodSec', '1',
            '-StartTime', $st, '-ErrFile', $errFile) `
        -PassThru -WindowStyle Hidden
    Register-Pid -ProcId $mon.Id

    if (-not (Wait-Until { (Test-Path $hb) -and ((Get-Content $hb -Raw) -match 'err_size=\d+') } 4)) {
        throw 'heartbeat с err_size не появился за 4 с'
    }
    $c = Get-Content $hb -Raw
    if (-not $c.Contains("start_time=$st")) { throw "в heartbeat нет start_time=$st" }
    if (-not $c.Contains("err_size=$errLen")) { throw "в heartbeat нет err_size=$errLen : '$c'" }
    if ($c -notmatch 'err_mtime=\d{4}-\d{2}-\d{2}T') { throw "в heartbeat нет err_mtime=<iso>: '$c'" }

    Stop-TreeById -ProcId $dummyPid
    if (-not (Wait-Until { -not (Get-Process -Id $mon.Id -ErrorAction SilentlyContinue) } 4)) {
        throw 'монитор не завершился за 4 с после смерти воркера'
    }
}

# --- T9 (W6): Test-WorkerAlive — живой dummy / чужой StartTime / мёртвый PID --
$t9 = {
    $dummy = Start-Process -FilePath 'powershell' `
        -ArgumentList @('-NoProfile', '-Command', 'Start-Sleep 30 # MARKER-T9') `
        -PassThru -WindowStyle Hidden
    Register-Pid -ProcId $dummy.Id

    # читаем фактический StartTime процесса (сразу может быть недоступен)
    $st = $null
    $swSt = [System.Diagnostics.Stopwatch]::StartNew()
    while ((-not $st) -and $swSt.Elapsed.TotalSeconds -lt 4) {
        try { $st = (Get-Process -Id $dummy.Id -ErrorAction Stop).StartTime }
        catch { Start-Sleep -Milliseconds 200 }
    }
    if (-not $st) { throw 'не удалось прочитать StartTime dummy-процесса' }
    $stIso = $st.ToString($fmt)

    # (а) живой «наш»: PID + фактический StartTime + маркер в CommandLine
    $out = & $aliveScript -WorkerPid $dummy.Id -StartedAt $stIso -Pattern 'MARKER-T9'
    $aliveLine = ($out | Where-Object { $_ -match '^ALIVE=' }) -join ''
    if ($LASTEXITCODE -ne 0 -or $aliveLine -ne 'ALIVE=True') {
        throw "живой dummy: exit=$LASTEXITCODE, out='$($out -join ' | ')', ожидалось ALIVE=True/exit 0"
    }

    # (б) имитация переиспользования PID: тот же PID, «чужой» StartTime (-5 мин)
    $out2 = & $aliveScript -WorkerPid $dummy.Id -StartedAt $st.AddMinutes(-5).ToString($fmt) -Pattern 'MARKER-T9'
    $alive2 = ($out2 | Where-Object { $_ -match '^ALIVE=' }) -join ''
    $reason2 = ($out2 | Where-Object { $_ -match '^REASON=' }) -join ''
    if ($LASTEXITCODE -ne 1 -or $alive2 -ne 'ALIVE=False') {
        throw "чужой StartTime: exit=$LASTEXITCODE, out='$($out2 -join ' | ')', ожидалось ALIVE=False/exit 1"
    }
    if ($reason2 -ne 'REASON=start-time-mismatch') { throw "reason='$reason2', ожидался start-time-mismatch" }

    # (в) мёртвый PID -> не жив
    Stop-TreeById -ProcId $dummy.Id
    if (-not (Wait-Until { -not (Get-Process -Id $dummy.Id -ErrorAction SilentlyContinue) } 4)) {
        throw "dummy $($dummy.Id) жив после kill"
    }
    $out3 = & $aliveScript -WorkerPid $dummy.Id -StartedAt $stIso -Pattern 'MARKER-T9'
    $alive3 = ($out3 | Where-Object { $_ -match '^ALIVE=' }) -join ''
    if ($LASTEXITCODE -ne 1 -or $alive3 -ne 'ALIVE=False') {
        throw "мёртвый PID: exit=$LASTEXITCODE, out='$($out3 -join ' | ')', ожидалось ALIVE=False/exit 1"
    }
}

# --- T10 (W6): Start-Worker НЕ спавнит дубль живого воркера ------------------
$t10 = {
    # «прежний» живой воркер: маркер autodev-worker в CommandLine
    $old = Start-Process -FilePath 'powershell' `
        -ArgumentList @('-NoProfile', '-Command', 'Start-Sleep 30 # autodev-worker-dummy-T10') `
        -PassThru -WindowStyle Hidden
    Register-Pid -ProcId $old.Id

    $oldStart = $null
    $swSt = [System.Diagnostics.Stopwatch]::StartNew()
    while ((-not $oldStart) -and $swSt.Elapsed.TotalSeconds -lt 4) {
        try { $oldStart = (Get-Process -Id $old.Id -ErrorAction Stop).StartTime }
        catch { Start-Sleep -Milliseconds 200 }
    }
    if (-not $oldStart) { throw 'не удалось прочитать StartTime прежнего воркера' }

    $stateFile = Join-Path $Temp 'state-T10.json'
    $backlog = Join-Path $Temp 'backlog-T10.md'
    $report = Join-Path $Temp 'report-T10.md'
    Set-Content -Path $backlog -Value '# backlog' -Encoding UTF8
    Set-Content -Path $report -Value '' -Encoding UTF8
    $pre = [ordered]@{
        phase  = 'test'
        worker = [ordered]@{
            pid                = $old.Id
            started_at_process = $oldStart.ToString($fmt)
            log                = 'backlog\state\logs\worker-OLD.log'
            heartbeat          = 'backlog\state\heartbeat-OLD.txt'
            deadline_at        = (Get-Date).AddMinutes(60).ToString($fmt)
            attempt            = 3
        }
    }
    $pre | ConvertTo-Json -Depth 20 | Set-Content -Path $stateFile -Encoding UTF8
    $stateBefore = Get-Content $stateFile -Raw

    $logsDir = Join-Path $Temp 'backlog\state\logs'
    New-Item -ItemType Directory -Force -Path $logsDir | Out-Null
    $logsBefore = @(Get-ChildItem $logsDir -File -ErrorAction SilentlyContinue).Count

    $out = & $startScript -DummyCommand 'powershell' `
        -DummyArgs @('-NoProfile', '-Command', 'Start-Sleep 5') `
        -State $stateFile -Backlog $backlog -Report $report `
        -Id 'T10' -BudgetMin 90 -Root $Temp

    if (-not ($out | Where-Object { $_ -eq 'DUPLICATE_SKIPPED=True' })) {
        throw "в выводе Start-Worker нет DUPLICATE_SKIPPED=True: '$($out -join ' | ')"
    }
    if (-not ($out | Where-Object { $_ -match '^WARNING=' })) { throw 'в выводе нет строки WARNING=' }
    if (-not ($out | Where-Object { $_ -eq "PID=$($old.Id)" })) { throw "в выводе нет PID=$($old.Id) прежнего воркера" }

    # state НЕ перезаписан, новый лог НЕ создан, прежний воркер жив
    $stateAfter = Get-Content $stateFile -Raw
    if ($stateAfter -ne $stateBefore) { throw 'state-файл изменён при duplicate-skip' }
    $logsAfter = @(Get-ChildItem $logsDir -File -ErrorAction SilentlyContinue).Count
    if ($logsAfter -ne $logsBefore) { throw "создан новый лог-файл при duplicate-skip ($logsBefore -> $logsAfter)" }
    if (-not (Get-Process -Id $old.Id -ErrorAction SilentlyContinue)) { throw 'прежний воркер погиб при duplicate-skip' }

    Stop-TreeById -ProcId $old.Id
}

# --- T11 (W4): кириллица в .log.err читается строгим UTF-8 без каракулей -----
$t11 = {
    # маркер с кириллицей (файл харнесса — UTF-8 с BOM, литерал читается верно)
    $marker = 'ОШИБКА-К8-ошибка'

    $stateFile = Join-Path $Temp 'state-T11.json'
    $backlog = Join-Path $Temp 'backlog-T11.md'
    $report = Join-Path $Temp 'report-T11.md'
    Set-Content -Path $backlog -Value '# backlog' -Encoding UTF8
    Set-Content -Path $report -Value '' -Encoding UTF8

    # dummy пишет маркер в stderr И stdout, затем «работает» (обёртка cmd /c
    # chcp 65001 должна обеспечить UTF-8 в перенаправленных файлах)
    $cmd = "[Console]::Error.WriteLine('$marker'); Write-Output '$marker-out'; Start-Sleep 6"
    $out = & $startScript -DummyCommand 'powershell' `
        -DummyArgs @('-NoProfile', '-Command', $cmd) `
        -State $stateFile -Backlog $backlog -Report $report `
        -Id 'T11' -BudgetMin 90 -Root $Temp

    $pidLine = $out | Where-Object { $_ -match '^PID=\d+$' } | Select-Object -First 1
    if (-not $pidLine) { throw "в выводе Start-Worker нет PID=<число>: '$($out -join ' | ')" }
    $workerPid = [int]($pidLine -replace '^PID=', '')
    Register-Pid -ProcId $workerPid
    Register-Worker -WorkerPid $workerPid

    $errLine = $out | Where-Object { $_ -match '^LOG_ERR=' } | Select-Object -First 1
    if (-not $errLine) { throw "в выводе нет LOG_ERR=: '$($out -join ' | ')" }
    $errFull = Join-Path $Temp ($errLine -replace '^LOG_ERR=', '')

    # ждём записи stderr (старт обёртки cmd + powershell ≈ 1-3 с)
    if (-not (Wait-Until { (Test-Path $errFull) -and ((Get-Item $errFull).Length -gt 0) } 10)) {
        throw ".log.err с кириллицей не появился за 10 с: $errFull"
    }

    # Контрольная команда SKILL.md (W4): Get-Content -Encoding Byte (открывает
    # файл с FileShare.ReadWrite — работает и у ЖИВОГО воркера, чей stderr
    # держит файл под записью; [IO.File]::ReadAllBytes в этом случае падает)
    # + строгий UTF-8-декодер с fallback в системную ANSI-кодировку.
    # Для НОВОГО лога fallback срабатывать НЕ должен — файл обязан быть UTF-8.
    $bytes = Get-Content $errFull -Encoding Byte -ReadCount 0
    $strict = New-Object System.Text.UTF8Encoding($false, $true)
    try { $text = $strict.GetString($bytes) }
    catch { throw "байты .log.err НЕ декодируются строгим UTF-8 (записаны не в UTF-8): $($_.Exception.Message)" }

    if (-not $text.Contains($marker)) { throw "маркер '$marker' не читается в .err: '$text'" }
    $fffd = ([regex]::Matches($text, [string][char]0xFFFD)).Count
    if ($fffd -gt 0) { throw "в тексте .err $fffd шт. U+FFFD (каракули)" }
}

# --- T12 (W5): verdict killed-hang содержит err_age_sec/err_size/err_mtime ---
$t12 = {
    $dummyPid = Start-DummySleep -Seconds 60
    Register-Pid -ProcId $dummyPid

    # состаренный heartbeat (имитация зависания, как T5)
    $hb = Join-Path $Temp 'hb-T12.txt'
    Set-Content -Path $hb -Value "pid=$dummyPid;start_time=;err_mtime=;err_size=0;ts=x" -Encoding UTF8
    (Get-Item $hb).LastWriteTime = (Get-Date).AddMinutes(-15)

    # «живой» .err: обновлён 30 с назад, непустой
    $errFile = Join-Path $Temp 'worker-T12.log.err'
    Set-Content -Path $errFile -Value @('MARKER-T12-first', 'err-line-2') -Encoding UTF8
    (Get-Item $errFile).LastWriteTime = (Get-Date).AddSeconds(-30)
    $errLen = (Get-Item $errFile).Length

    $verdict = Join-Path $Temp 'verdict-T12.json'
    & $waitScript -WorkerPid $dummyPid -Heartbeat $hb -ErrFile $errFile -StaleMin 10 -BudgetMin 90 -PollSec 1 -Verdict $verdict | Out-Null

    if (-not (Test-Path $verdict)) { throw 'verdict-файл не создан' }
    $vj = Get-Content $verdict -Raw | ConvertFrom-Json
    if ($vj.status -ne 'killed-hang') { throw "status=$($vj.status), ожидался killed-hang" }
    if ($vj.err_age_sec -lt 0 -or $vj.err_age_sec -gt 600) { throw "err_age_sec=$($vj.err_age_sec), ожидался 0..600" }
    if ($vj.err_size -ne $errLen) { throw "err_size=$($vj.err_size), ожидался $errLen" }
    if ([string]::IsNullOrWhiteSpace($vj.err_mtime)) { throw 'err_mtime в verdict пуст' }
    if (-not (Wait-Until { -not (Get-Process -Id $dummyPid -ErrorAction SilentlyContinue) } 5)) {
        throw "dummy $dummyPid жив после hang-kill"
    }
}

# --- T13 (W10): hang-отчёт при killed-hang (секции + кириллица + hang_report) -
$t13 = {
    $dummyPid = Start-DummySleep -Seconds 60
    Register-Pid -ProcId $dummyPid

    # состаренный heartbeat (имитация зависания, как T5)
    $hb = Join-Path $Temp 'hb-T13.txt'
    Set-Content -Path $hb -Value "pid=$dummyPid;start_time=;err_mtime=;err_size=0;ts=x" -Encoding UTF8
    (Get-Item $hb).LastWriteTime = (Get-Date).AddMinutes(-15)

    # .err с кириллическим маркером (UTF-8) — хвост должен читаться без каракулей
    $marker = 'ОШИБКА-К13-hang-диагностика'
    $errFile = Join-Path $Temp 'worker-T13.log.err'
    Set-Content -Path $errFile -Value @('line-1', $marker, 'line-3') -Encoding UTF8

    # state со snapshot-полями (только чтение — watchdog не должен его менять)
    $stateFile = Join-Path $Temp 'state-T13.json'
    $st = [ordered]@{
        step           = 'verifier'
        fix_iterations = 2
        current_task   = 'T13'
        worker         = [ordered]@{ pid = $dummyPid; log = 'backlog\state\logs\worker-T13.log' }
    }
    $st | ConvertTo-Json -Depth 20 | Set-Content -Path $stateFile -Encoding UTF8
    $stateBefore = Get-Content $stateFile -Raw

    $verdict = Join-Path $Temp 'verdict-T13.json'
    & $waitScript -WorkerPid $dummyPid -Heartbeat $hb -ErrFile $errFile -State $stateFile `
        -StaleMin 10 -BudgetMin 90 -PollSec 1 -Id 'T13' -Verdict $verdict -Root $Temp | Out-Null

    # verdict: killed-hang + непустой hang_report с относительным путём
    if (-not (Test-Path $verdict)) { throw 'verdict-файл не создан' }
    $vj = Get-Content $verdict -Raw | ConvertFrom-Json
    if ($vj.status -ne 'killed-hang') { throw "status=$($vj.status), ожидался killed-hang" }
    if ([string]::IsNullOrWhiteSpace($vj.hang_report)) { throw 'verdict.hang_report пуст при killed-hang' }
    if ($vj.hang_report -notmatch '^backlog\\state\\reports\\T13-hang-\d{8}-\d{6}\.md$') {
        throw "hang_report='$($vj.hang_report)', ожидался относительный путь backlog\state\reports\T13-hang-<ts>.md"
    }

    # отчёт создан (относительно -Root $Temp) и содержит все 6 обязательных секций
    $reportFull = Join-Path $Temp $vj.hang_report
    if (-not (Test-Path $reportFull)) { throw "hang-отчёт не создан: $reportFull" }
    $rt = Get-Content $reportFull -Raw
    foreach ($sec in @('## 1. Вердикт и времена', '## 2. Heartbeat', '## 3. Логи',
            '## 4. Хвост .log.err', '## 5. Snapshot state', '## 6. Дерево процессов')) {
        if (-not $rt.Contains($sec)) { throw "в hang-отчёте нет секции '$sec'" }
    }
    # кириллица в хвосте .err читаема: маркер присутствует, каракулей U+FFFD нет
    if (-not $rt.Contains($marker)) { throw "в хвосте .err hang-отчёта нет маркера '$marker'" }
    $fffd = ([regex]::Matches($rt, [string][char]0xFFFD)).Count
    if ($fffd -gt 0) { throw "в hang-отчёте $fffd шт. U+FFFD (каракули)" }
    # дерево процессов (снимок ДО kill) содержит PID убитого dummy
    if (-not $rt.Contains("pid=$dummyPid")) { throw "в дереве процессов нет pid=$dummyPid" }
    # snapshot state: step/fix_iterations/current_task
    if (-not $rt.Contains('step: verifier')) { throw 'в Snapshot state нет step: verifier' }
    if (-not $rt.Contains('fix_iterations: 2')) { throw 'в Snapshot state нет fix_iterations: 2' }
    if (-not $rt.Contains('current_task: T13')) { throw 'в Snapshot state нет current_task: T13' }
    # state НЕ перезаписан watchdog'ом (риск 9)
    $stateAfter = Get-Content $stateFile -Raw
    if ($stateAfter -ne $stateBefore) { throw 'watchdog перезаписал state при сборе hang-отчёта' }
    # dummy убит деревом
    if (-not (Wait-Until { -not (Get-Process -Id $dummyPid -ErrorAction SilentlyContinue) } 5)) {
        throw "dummy $dummyPid жив после hang-kill"
    }
}

# --- T14 (W10): hang-отчёт при killed-walltime; для exited hang_report пуст ---
$t14 = {
    # (а) killed-walltime: дедлайн в прошлом (BudgetMin=0, как T4)
    $dummyPid = Start-DummySleep -Seconds 60
    Register-Pid -ProcId $dummyPid
    $verdict = Join-Path $Temp 'verdict-T14.json'
    & $waitScript -WorkerPid $dummyPid -BudgetMin 0 -PollSec 1 -Id 'T14' -Verdict $verdict -Root $Temp | Out-Null
    if (-not (Test-Path $verdict)) { throw 'verdict-файл не создан (walltime)' }
    $vj = Get-Content $verdict -Raw | ConvertFrom-Json
    if ($vj.status -ne 'killed-walltime') { throw "status=$($vj.status), ожидался killed-walltime" }
    if ([string]::IsNullOrWhiteSpace($vj.hang_report)) { throw 'verdict.hang_report пуст при killed-walltime' }
    $reportFull = Join-Path $Temp $vj.hang_report
    if (-not (Test-Path $reportFull)) { throw "hang-отчёт не создан: $reportFull" }
    $rt = Get-Content $reportFull -Raw
    if (-not $rt.Contains('verdict: killed-walltime')) { throw 'в отчёте нет причины killed-walltime' }
    if (-not $rt.Contains('## 6. Дерево процессов')) { throw 'в отчёте нет секции дерева процессов' }
    if (-not (Wait-Until { -not (Get-Process -Id $dummyPid -ErrorAction SilentlyContinue) } 5)) {
        throw "dummy $dummyPid жив после walltime-kill"
    }

    # (б) exited: поле hang_report присутствует, но пустая строка, отчёт НЕ создаётся
    $dummy2 = Start-DummySleep -Seconds 2
    Register-Pid -ProcId $dummy2
    $verdict2 = Join-Path $Temp 'verdict-T14b.json'
    & $waitScript -WorkerPid $dummy2 -BudgetMin 5 -PollSec 1 -Id 'T14B' -Verdict $verdict2 -Root $Temp | Out-Null
    if (-not (Test-Path $verdict2)) { throw 'verdict-файл не создан (exited)' }
    $vj2 = Get-Content $verdict2 -Raw | ConvertFrom-Json
    if ($vj2.status -ne 'exited') { throw "status=$($vj2.status), ожидался exited" }
    if ($null -eq $vj2.PSObject.Properties['hang_report']) { throw 'в verdict нет поля hang_report' }
    if ($vj2.hang_report -ne '') { throw "hang_report='$($vj2.hang_report)', ожидалась пустая строка для exited" }
}

# --- T15 (W10): Start-Worker -HangReport -> HANG_REPORT= в выводе --------------
$t15 = {
    $stateFile = Join-Path $Temp 'state-T15.json'
    $backlog = Join-Path $Temp 'backlog-T15.md'
    $report = Join-Path $Temp 'report-T15.md'
    Set-Content -Path $backlog -Value '# backlog' -Encoding UTF8
    Set-Content -Path $report -Value '' -Encoding UTF8

    $hangRel = 'backlog\state\reports\OLD-hang-20260101-000000.md'
    $out = & $startScript -DummyCommand 'powershell' `
        -DummyArgs @('-NoProfile', '-Command', 'Start-Sleep 5') `
        -State $stateFile -Backlog $backlog -Report $report `
        -Id 'T15' -BudgetMin 90 -Root $Temp -HangReport $hangRel

    $pidLine = $out | Where-Object { $_ -match '^PID=\d+$' } | Select-Object -First 1
    if (-not $pidLine) { throw "в выводе Start-Worker нет PID=<число>: '$($out -join ' | ')'" }
    $workerPid = [int]($pidLine -replace '^PID=', '')
    Register-Pid -ProcId $workerPid
    Register-Worker -WorkerPid $workerPid

    # путь hang-отчёта выводится диспетчеру
    if (-not ($out | Where-Object { $_ -eq "HANG_REPORT=$hangRel" })) {
        throw "в выводе нет HANG_REPORT=$hangRel : '$($out -join ' | ')'"
    }
}

# --- T16 (W11): автоформула бюджета группы и переопределение явным -BudgetMin --
$t16 = {
    $backlog = Join-Path $Temp 'backlog-T16.md'
    $report = Join-Path $Temp 'report-T16.md'
    Set-Content -Path $backlog -Value '# backlog' -Encoding UTF8
    Set-Content -Path $report -Value '' -Encoding UTF8

    # (а) группа из 3 задач БЕЗ явного -BudgetMin -> deadline ~ now + 180 мин
    $stateA = Join-Path $Temp 'state-T16A.json'
    $beforeA = Get-Date
    $outA = & $startScript -DummyCommand 'powershell' `
        -DummyArgs @('-NoProfile', '-Command', 'Start-Sleep 5') `
        -State $stateA -Backlog $backlog -Report $report `
        -Id 'T16A' -Group 'P0' -Tasks 'A1,A2,A3' -Root $Temp
    $pidLineA = $outA | Where-Object { $_ -match '^PID=\d+$' } | Select-Object -First 1
    if (-not $pidLineA) { throw "нет PID= в выводе (а): '$($outA -join ' | ')'" }
    $pidA = [int]($pidLineA -replace '^PID=', '')
    Register-Pid -ProcId $pidA
    Register-Worker -WorkerPid $pidA
    $sjA = Get-Content $stateA -Raw | ConvertFrom-Json
    $minA = ([datetime]::Parse($sjA.worker.deadline_at) - $beforeA).TotalMinutes
    if ($minA -lt 178 -or $minA -gt 183) { throw "группа 3 задачи: бюджет $([int]$minA) мин, ожидалось ~180 (90+45*2)" }

    # (б) группа из 2 задач с явным -BudgetMin 30 -> deadline ~ now + 30 мин
    $stateB = Join-Path $Temp 'state-T16B.json'
    $beforeB = Get-Date
    $outB = & $startScript -DummyCommand 'powershell' `
        -DummyArgs @('-NoProfile', '-Command', 'Start-Sleep 5') `
        -State $stateB -Backlog $backlog -Report $report `
        -Id 'T16B' -Group 'P0' -Tasks 'B1,B2' -BudgetMin 30 -Root $Temp
    $pidLineB = $outB | Where-Object { $_ -match '^PID=\d+$' } | Select-Object -First 1
    if (-not $pidLineB) { throw "нет PID= в выводе (б): '$($outB -join ' | ')'" }
    $pidB = [int]($pidLineB -replace '^PID=', '')
    Register-Pid -ProcId $pidB
    Register-Worker -WorkerPid $pidB
    $sjB = Get-Content $stateB -Raw | ConvertFrom-Json
    $minB = ([datetime]::Parse($sjB.worker.deadline_at) - $beforeB).TotalMinutes
    if ($minB -lt 28 -or $minB -gt 33) { throw "явный -BudgetMin 30: бюджет $([int]$minB) мин, ожидалось ~30 (формула переопределена)" }

    # (в) одна задача БЕЗ явного -BudgetMin -> дефолт ~90 мин
    $stateC = Join-Path $Temp 'state-T16C.json'
    $beforeC = Get-Date
    $outC = & $startScript -DummyCommand 'powershell' `
        -DummyArgs @('-NoProfile', '-Command', 'Start-Sleep 5') `
        -State $stateC -Backlog $backlog -Report $report `
        -Id 'T16C' -TaskId 'C1' -Root $Temp
    $pidLineC = $outC | Where-Object { $_ -match '^PID=\d+$' } | Select-Object -First 1
    if (-not $pidLineC) { throw "нет PID= в выводе (в): '$($outC -join ' | ')'" }
    $pidC = [int]($pidLineC -replace '^PID=', '')
    Register-Pid -ProcId $pidC
    Register-Worker -WorkerPid $pidC
    $sjC = Get-Content $stateC -Raw | ConvertFrom-Json
    $minC = ([datetime]::Parse($sjC.worker.deadline_at) - $beforeC).TotalMinutes
    if ($minC -lt 88 -or $minC -gt 93) { throw "одна задача: бюджет $([int]$minC) мин, ожидалось ~90 (дефолт)" }
}

# --- Прогон -------------------------------------------------------------------
try {
    Write-Output '=== autodev worker scripts: manual tests (dummy processes) ==='
    Write-Output "TEMP=$Temp"
    Write-Output "SCRIPTS=$SrcScripts"

    Invoke-Test -Name 'T1_HeartbeatLoop_Lifecycle' -Body $t1
    Invoke-Test -Name 'T2_StartWorker_DummySpawn' -Body $t2
    Invoke-Test -Name 'T3_WaitWorker_Exited' -Body $t3
    Invoke-Test -Name 'T4_WaitWorker_KilledWalltime' -Body $t4
    Invoke-Test -Name 'T5_WaitWorker_KilledHang' -Body $t5
    Invoke-Test -Name 'T6_OrphanTreeKill' -Body $t6
    Invoke-Test -Name 'T7_TailErr' -Body $t7
    Invoke-Test -Name 'T8_HeartbeatErrFields' -Body $t8
    Invoke-Test -Name 'T9_TestWorkerAlive' -Body $t9
    Invoke-Test -Name 'T10_StartWorker_DuplicateSkip' -Body $t10
    Invoke-Test -Name 'T11_ErrLogUtf8' -Body $t11
    Invoke-Test -Name 'T12_VerdictErrFields' -Body $t12
    Invoke-Test -Name 'T13_HangReport_KilledHang' -Body $t13
    Invoke-Test -Name 'T14_HangReport_Walltime_ExitedEmpty' -Body $t14
    Invoke-Test -Name 'T15_StartWorker_HangReportParam' -Body $t15
    Invoke-Test -Name 'T16_BudgetAutoFormula' -Body $t16
}
finally {
    Stop-TempOrphans
    Remove-Item -LiteralPath $Temp -Recurse -Force -ErrorAction SilentlyContinue
}

# --- Сводка -------------------------------------------------------------------
Write-Output ''
Write-Output "PASSED $($script:Pass)/$($script:Total)"
Write-Output "FAILED $($script:Fail)"
if ($script:Fail -eq 0 -and $script:Pass -eq $script:Total) {
    Write-Output 'OVERALL: PASS'
    exit 0
}
else {
    Write-Output 'OVERALL: FAIL'
    exit 1
}
