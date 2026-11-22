# 停止 tts-api：先停守护脚本（否则它会把 gateway 拉起来），再停 gateway；推理 worker 会跟着 gateway 退出。
# 正在跑的训练进程不受影响，下次启动后 gateway 自动接管；要停训练先 POST /jobs/{id}/cancel。
$root = $PSScriptRoot
$scriptPattern = [regex]::Escape((Join-Path $root 'start.ps1'))
foreach ($name in 'supervisor', 'gateway') {
    $pidFile = "$root\runtime\$name.pid"
    if (-not (Test-Path $pidFile)) { continue }
    # pid 文件可能是上次留下的，pid 已被别的进程复用，所以要核对命令行确实是 tts-api 的
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$(Get-Content $pidFile)"
    $expected = if ($name -eq 'supervisor') { $scriptPattern } else { 'ttsapi\.gateway' }
    if ($p -and $p.CommandLine -match $expected) {
        Stop-Process -Id $p.ProcessId -Force
        "stopped $name (pid $($p.ProcessId))"
    }
    Remove-Item $pidFile
}
