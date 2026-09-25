<#
  本地一键启动：
    1. 校验虚拟环境与前端构建产物（缺失时自动构建）
    2. 校验配置、迁移数据库、创建内置管理员（幂等）
    3. 启动后台服务

  用法：
    .\scripts\start-local.ps1                  # 前台运行，Ctrl+C 停止
    .\scripts\start-local.ps1 -Background       # 后台运行，PID 写入 data\api.pid
    .\scripts\start-local.ps1 -Port 8080        # 换端口
#>
[CmdletBinding()]
param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8000,
    [switch]$Background,
    [switch]$SkipFrontendBuild
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "未找到虚拟环境：$python" -ForegroundColor Red
    Write-Host "请先执行：" -ForegroundColor Yellow
    Write-Host "  python -m venv .venv"
    Write-Host "  .\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt"
    exit 1
}

$distEntry = Join-Path $root "frontend\dist\index.html"
if (-not $SkipFrontendBuild -and -not (Test-Path $distEntry)) {
    Write-Host "== 构建前端 ==" -ForegroundColor Cyan
    Push-Location (Join-Path $root "frontend")
    try {
        if (-not (Test-Path "node_modules")) {
            npm install --no-audit --no-fund
        }
        npm run build
    }
    finally {
        Pop-Location
    }
}

Write-Host "== 校验配置 ==" -ForegroundColor Cyan
& $python main.py check-config

Write-Host "== 初始化数据库 ==" -ForegroundColor Cyan
& $python main.py init-db

Write-Host "== 创建内置管理员（已存在则跳过）==" -ForegroundColor Cyan
& $python main.py create-admin

$url = "http://${HostAddress}:${Port}"

function Get-ListeningPids {
    param([int]$ListenPort)
    $found = @()
    foreach ($line in (netstat -ano | Select-String -Pattern ":$ListenPort\s+.*LISTENING")) {
        $parts = ($line.Line.Trim() -split '\s+')
        if ($parts[-1] -match '^\d+$') { $found += [int]$parts[-1] }
    }
    return $found | Select-Object -Unique
}

if ($Background) {
    $dataDir = Join-Path $root "data"
    New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
    $proc = Start-Process -FilePath $python `
        -ArgumentList "main.py", "api", "--host", $HostAddress, "--port", "$Port" `
        -WorkingDirectory $root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $dataDir "api.stdout.log") `
        -RedirectStandardError (Join-Path $dataDir "api.stderr.log")

    # venv 的 python.exe 可能会再拉起子进程，因此以真实监听端口的 PID 为准
    $listenerPids = @()
    for ($i = 0; $i -lt 15; $i++) {
        Start-Sleep -Milliseconds 800
        $listenerPids = Get-ListeningPids -ListenPort $Port
        if ($listenerPids.Count -gt 0) { break }
    }

    $pidsToRecord = if ($listenerPids.Count -gt 0) { $listenerPids } else { @($proc.Id) }
    Set-Content -Path (Join-Path $dataDir "api.pid") -Value ($pidsToRecord -join "`n")

    if ($listenerPids.Count -gt 0) {
        Write-Host "服务已在后台启动，监听 PID=$($pidsToRecord -join ',')" -ForegroundColor Green
    }
    else {
        Write-Host "已启动进程 PID=$($proc.Id)，但端口 $Port 尚未进入监听，请查看 data\api.stderr.log" -ForegroundColor Yellow
    }
    Write-Host "访问地址：$url" -ForegroundColor Green
    Write-Host "停止服务：.\scripts\stop-local.ps1" -ForegroundColor Yellow
}
else {
    Write-Host "访问地址：$url（按 Ctrl+C 停止）" -ForegroundColor Green
    & $python main.py api --host $HostAddress --port $Port
}
