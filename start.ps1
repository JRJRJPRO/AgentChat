# AgentChat 启动脚本：双击或在 PowerShell 里运行 .\start.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (!(Test-Path $py)) {
    Write-Host "首次运行：创建虚拟环境并安装依赖..." -ForegroundColor Cyan
    python -m venv .venv
    & $py -m pip install --quiet -r requirements.txt
}

if (!(Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Warning "PATH 里找不到 claude CLI，agent 将无法被唤醒（界面仍可打开）"
}

$env:PYTHONIOENCODING = "utf-8"
Write-Host "AgentChat 运行在 http://127.0.0.1:8787   (Ctrl+C 停止)" -ForegroundColor Green
& $py -m uvicorn server.main:app --host 127.0.0.1 --port 8787 --log-level warning
