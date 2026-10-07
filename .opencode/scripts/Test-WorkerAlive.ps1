<#
.SYNOPSIS
    Проверка «жив ли НАШ воркер autodev»: PID + StartTime + CommandLine (W6).

.DESCRIPTION
    `Get-Process -Id <pid>` может дать ложное «жив» из-за переиспользования PID
    операционной системой: к моменту проверки под этим PID может жить любой
    чужой процесс. Наш воркер считается живым только при ОДНОВРЕМЕННОМ
    выполнении трёх условий:

      (1) процесс с PID -WorkerPid существует;
      (2) его StartTime совпадает с ожидаемым -StartedAt
          (формат 'yyyy-MM-ddTHH:mm:ss') в пределах допуска -ToleranceSec
          (по умолчанию 2 с); если -StartedAt пуст — сверка времени
          пропускается (ручное использование «жив ли вообще этот PID»);
      (3) CommandLine процесса (Get-CimInstance Win32_Process) содержит
          маркер -Pattern (напр. 'autodev-<ID>' — title сессии) ИЛИ литерал
          'autodev-worker'.

    Недоступные StartTime/CommandLine трактуются КОНСЕРВАТИВНО — «не наш
    воркер»: ложное «не жив» лишь разрешает дополнительный спавн (диспетчер
    всё равно сверяет единицу по свежему heartbeat и отсутствию финального
    verdict), а ложное «жив» пропустило бы необходимый перезапуск.

    Вывод (ASCII): 'ALIVE=True|False', 'REASON=<slug>', 'PID=<pid>'.
    Exit code: 0 — жив (наш воркер), 1 — мёртв/не наш. Вызывается дочерним
    процессом из Start-Worker.ps1 (pre-spawn защита от дубля), диспетчером
    (RESUME) и ручными тестами.

.NOTES
    PowerShell 5.1 (Windows). Пример вызова:
      powershell -NoProfile -ExecutionPolicy Bypass -File `
        .opencode\scripts\Test-WorkerAlive.ps1 -WorkerPid 12345 `
        -StartedAt 2026-10-05T12:00:00 -Pattern autodev-W4
#>

[CmdletBinding()]
param(
    # PID проверяемого процесса.
    [int]$WorkerPid = 0,
    # Ожидаемое время старта процесса (iso 'yyyy-MM-ddTHH:mm:ss');
    # пустая строка — сверка времени пропускается.
    [string]$StartedAt = '',
    # Маркер CommandLine нашей сессии (напр. 'autodev-<ID>'); дополнительно
    # всегда принимается литерал 'autodev-worker'.
    [string]$Pattern = '',
    # Допуск совпадения StartTime, секунд.
    [int]$ToleranceSec = 2
)

$ErrorActionPreference = 'Continue'

$alive = $false
$reason = 'unknown'

# (1) Процесс с таким PID существует?
$proc = $null
if ($WorkerPid -gt 0) {
    $proc = Get-Process -Id $WorkerPid -ErrorAction SilentlyContinue
}
if (-not $proc) {
    $reason = 'no-process'
}
else {
    # (2) StartTime совпадает с ожидаемым в пределах допуска?
    $timeOk = $true
    if (-not [string]::IsNullOrWhiteSpace($StartedAt)) {
        $expected = $null
        try { $expected = [datetime]::Parse($StartedAt) } catch { $expected = $null }
        if (-not $expected) {
            $timeOk = $false
            $reason = 'bad-started-at'
        }
        else {
            $actual = $null
            try { $actual = $proc.StartTime } catch { $actual = $null }
            if (-not $actual) {
                $timeOk = $false
                $reason = 'start-time-unavailable'
            }
            elseif ([Math]::Abs((($actual - $expected).TotalSeconds)) -gt $ToleranceSec) {
                $timeOk = $false
                $reason = 'start-time-mismatch'
            }
        }
    }

    # (3) CommandLine содержит наш маркер?
    if ($timeOk) {
        $cmdLine = $null
        try {
            $w32 = Get-CimInstance Win32_Process -Filter "ProcessId=$WorkerPid" -ErrorAction Stop
            if ($w32) { $cmdLine = [string]$w32.CommandLine }
        }
        catch { $cmdLine = $null }

        if ([string]::IsNullOrWhiteSpace($cmdLine)) {
            $reason = 'cmdline-unavailable'
        }
        else {
            $hit = $cmdLine.Contains('autodev-worker')
            if (-not $hit -and -not [string]::IsNullOrWhiteSpace($Pattern)) {
                $hit = $cmdLine.Contains($Pattern)
            }
            if ($hit) {
                $alive = $true
                $reason = 'alive'
            }
            else {
                $reason = 'cmdline-marker-missing'
            }
        }
    }
}

# Вывод — только ASCII (консоль cp1251/cp866 диспетчера).
Write-Output "ALIVE=$alive"
Write-Output "REASON=$reason"
Write-Output "PID=$WorkerPid"

if ($alive) { exit 0 } else { exit 1 }
