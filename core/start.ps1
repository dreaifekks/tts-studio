# 启动 tts-api。本脚本自己就是守护进程：gateway 退出后 5 秒自动重启（一键更新后的主动重启不等）。
# 前台运行：pwsh -File start.ps1。
$root = $PSScriptRoot
$gsv = if ($env:TTSAPI_GSV_ROOT) { $env:TTSAPI_GSV_ROOT } else { Join-Path $HOME 'GPT-SoVITS' }
$py = if ($env:TTSAPI_PYTHON) { $env:TTSAPI_PYTHON } else { Join-Path $gsv '.venv\Scripts\python.exe' }
$scriptPattern = [regex]::Escape($PSCommandPath)
$pidFile = "$root\runtime\supervisor.pid"
New-Item -ItemType Directory -Force "$root\runtime", "$root\logs" | Out-Null

if (Test-Path $pidFile) {
    $old = Get-CimInstance Win32_Process -Filter "ProcessId=$(Get-Content $pidFile)"
    if ($old -and $old.CommandLine -match $scriptPattern) { "tts-api 已在运行（supervisor pid $($old.ProcessId)）"; exit }
}
$PID | Set-Content $pidFile

$env:PYTHONPATH = $root
$env:PYTHONPYCACHEPREFIX = "$root\runtime\pycache"
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
$log = "$root\logs\gateway.log"

while ($true) {
    if ((Test-Path $log) -and (Get-Item $log).Length -gt 20MB) { Move-Item $log "$log.old" -Force }
    "$(Get-Date -Format s) supervisor: start gateway" | Add-Content "$root\logs\supervisor.log"
    cmd /c "`"$py`" -m ttsapi.gateway >> `"$log`" 2>&1"
    "$(Get-Date -Format s) supervisor: gateway exited with $LASTEXITCODE" | Add-Content "$root\logs\supervisor.log"
    # 75 是 POST /system/update 之后的主动重启，立刻拉起；其他退出（崩溃）等 5 秒，免得反复崩溃时空转
    if ($LASTEXITCODE -ne 75) { Start-Sleep 5 }
}
