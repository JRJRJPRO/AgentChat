# AgentChat 启动脚本：双击 AgentChat.vbs（推荐）或在 PowerShell 里运行 .\start.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# 已在运行就不再起第二个（疯狂双击也无副作用）
try {
    $null = Invoke-WebRequest -Uri "http://127.0.0.1:8787/api/state" -UseBasicParsing -TimeoutSec 2
    Write-Host "AgentChat 已经在运行: http://127.0.0.1:8787" -ForegroundColor Yellow
    exit 0
} catch { }

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (!(Test-Path $py)) {
    Write-Host "首次运行：创建虚拟环境并安装依赖..." -ForegroundColor Cyan
    python -m venv .venv
    & $py -m pip install --quiet -r requirements.txt
}

if (!(Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Warning "PATH 里找不到 claude CLI，agent 将无法被唤醒（界面仍可打开）"
}

New-Item -ItemType Directory -Force -Path "$PSScriptRoot\data" | Out-Null
$log = Join-Path $PSScriptRoot "data\server.log"
Add-Content -Path $log -Value "===== AgentChat start $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ====="

$env:PYTHONIOENCODING = "utf-8"
Write-Host "AgentChat 运行在 http://127.0.0.1:8787   (Ctrl+C 停止; 日志: data\server.log)" -ForegroundColor Green
& $py -m uvicorn server.main:app --host 127.0.0.1 --port 8787 --log-level warning *>> $log
