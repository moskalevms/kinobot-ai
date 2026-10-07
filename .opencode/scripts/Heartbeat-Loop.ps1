<#
.SYNOPSIS
    Heartbeat-монитор воркера autodev (detached, живёт отдельно от воркера).

.DESCRIPTION
    Пока процесс воркера (WorkerPid) жив — каждые PeriodSec секунд перезаписывает
    файл HeartbeatPath строкой формата:
      pid=<pid>;start_time=<StartTime процесса>;err_mtime=<iso|пусто>;
      err_size=<байты|0>;ts=<текущее iso>
    Как только процесс умер — записывает ФИНАЛЬНЫЙ heartbeat (та же строка,
    ts=сейчас) и выходит.

    Поля err_mtime/err_size (W5) — побочный сигнал прогресса: mtime/размер
    stderr-лога воркера (.log.err, путь из -ErrFile). Живой трафик воркера
    идёт именно в .err; stdout (.log) буферизуется и может не меняться часами.
    Отказ чтения .err не роняет монитор — поля остаются пустыми/нулевыми.

    Поле start_time (W6) — StartTime процесса воркера: вместе с pid это данные
    для сверки идентичности «наш ли воркер» (Test-WorkerAlive.ps1), защищающей
    от переиспользования PID операционной системой.

    ВАЖНО (риск 9 бэклога): монитор НЕ пишет autodev-state.json — только свой
    отдельный heartbeat-файл, чтобы не конкурировать с воркером за state.

.NOTES
    PowerShell 5.1 (Windows). Запускается из Start-Worker.ps1 с -WindowStyle Hidden.
#>

[CmdletBinding()]
param(
    # PID процесса воркера, за которым наблюдаем.
    [Parameter(Mandatory = $true)][int]$WorkerPid,
    # Путь к heartbeat-файлу (перезаписывается).
    [Parameter(Mandatory = $true)][string]$HeartbeatPath,
    # Период записи heartbeat, секунд.
    [int]$PeriodSec = 180,
    # ISO-время старта процесса воркера (StartTime); алиас StartedAt сохранён
    # для совместимости с прежними вызовами. Включается в строку heartbeat.
    [Alias('StartedAt')]
    [string]$StartTime = '',
    # Путь к stderr-логу воркера (.log.err) — для полей err_mtime/err_size (W5).
    [string]$ErrFile = ''
)

# Ошибки записи не должны ронять монитор — продолжаем наблюдение.
$ErrorActionPreference = 'Continue'

$fmt = 'yyyy-MM-ddTHH:mm:ss'

# Снимает mtime/размер .err-файла; при отсутствии/ошибке — пусто/0 (W5).
function Get-ErrStat {
    $stat = [ordered]@{ mtime = ''; size = 0 }
    if (-not [string]::IsNullOrWhiteSpace($ErrFile)) {
        try {
            if (Test-Path $ErrFile) {
                $it = Get-Item $ErrFile -ErrorAction Stop
                $stat.mtime = $it.LastWriteTime.ToString($fmt)
                $stat.size = [int]$it.Length
            }
        }
        catch {
            # Файл может быть кратковременно заблокирован воркером — не роняем
            # монитор, оставляем значения по умолчанию.
        }
    }
    return $stat
}

# Перезаписывает heartbeat-файл строкой с текущим ts и err-сигналом.
function Write-Heartbeat {
    param([string]$Ts)
    $err = Get-ErrStat
    $line = "pid=$WorkerPid;start_time=$StartTime;err_mtime=$($err.mtime);err_size=$($err.size);ts=$Ts"
    try {
        Set-Content -Path $HeartbeatPath -Value $line -Encoding UTF8
    }
    catch {
        # Файл может быть кратковременно заблокирован — пропускаем итерацию.
    }
}

# Основной цикл: пока процесс жив — пишем heartbeat и спим.
while ($true) {
    $proc = Get-Process -Id $WorkerPid -ErrorAction SilentlyContinue
    if (-not $proc) { break }
    Write-Heartbeat -Ts ((Get-Date).ToString($fmt))
    Start-Sleep -Seconds $PeriodSec
}

# Процесс воркера умер — пишем финальный heartbeat и выходим.
Write-Heartbeat -Ts ((Get-Date).ToString($fmt))
