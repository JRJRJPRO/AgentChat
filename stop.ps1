# 关闭 AgentChat 服务器（界面里的 ⏻ 按钮效果相同）
$conns = Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue
if ($conns) {
    $conns | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
    Write-Host "AgentChat 已停止" -ForegroundColor Green
} else {
    Write-Host "AgentChat 没有在运行"
}
