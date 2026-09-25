<#
  停止由 start-local.ps1 -Background 启动的服务。
  前台运行的服务请在对应窗口按 Ctrl+C。
#>
[CmdletBinding()]
param(
    [int]$Port = 8000
)

$root = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $root "data\api.pid"

function Get-ListeningPids {
    param([int]$ListenPort)
    $found = @()
    foreach ($line in (netstat -ano | Select-String -Pattern ":$ListenPort\s+.*LISTENING")) {
        $parts = ($line.Line.Trim() -split '\s+')
        if ($parts[-1] -match '^\d+$') { $found += [int]$parts[-1] }
    }
    return $found | Select-Object -Unique
}

$targets = New-Object System.Collections.Generic.List[int]

if (Test-Path $pidFile) {
    foreach ($raw in (Get-Content $pidFile)) {
        $value = 0
        if ([int]::TryParse($raw.Trim(), [ref]$value) -and $value -gt 0) { $targets.Add($value) }
    }
}

foreach ($listenerPid in (Get-ListeningPids -ListenPort $Port)) {
    $targets.Add($listenerPid)
}

$stopped = 0
foreach ($targetPid in ($targets | Select-Object -Unique)) {
    $process = Get-Process -Id $targetPid -ErrorAction SilentlyContinue
    if ($process) {
        Stop-Process -Id $targetPid -Force
        Write-Host "已停止进程 PID=$targetPid" -ForegroundColor Green
        $stopped++
    }
}

if ($stopped -eq 0) {
    Write-Host "没有找到正在运行的服务（端口 $Port 无监听）。" -ForegroundColor Yellow
    Write-Host "如果服务是前台运行的，请在对应 PowerShell 窗口按 Ctrl+C 停止。"
}

if (Test-Path $pidFile) { Remove-Item -LiteralPath $pidFile -Force }
