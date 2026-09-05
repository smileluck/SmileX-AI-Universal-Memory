# SmileX Memory 常驻服务注册(Windows 计划任务)
# 用法: powershell -File register-service-windows.ps1 [-Unregister]
#       可选环境变量 SMILEX_SERVE_ARGS="--config C:\smilex\config.yaml --port 9000"
# 注册后: 用户登录时自动启动 smilex-memory serve(后台无窗口)
[CmdletBinding()]
param([switch]$Unregister)

$TaskName = "SmileXMemoryServer"
$Exe = (Get-Command smilex-memory -ErrorAction SilentlyContinue).Source
if (-not $Exe) { $Exe = "smilex-memory" }  # 依赖 PATH

if ($Unregister) {
    schtasks /Delete /TN $TaskName /F
    Write-Host "已注销计划任务: $TaskName"
    exit 0
}

$existing = schtasks /Query /TN $TaskName 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "任务已存在,先删除重建…"
    schtasks /Delete /TN $TaskName /F | Out-Null
}

# 登录时启动;限最高权限不必要,用默认用户权限即可
$ExtraArgs = $env:SMILEX_SERVE_ARGS
schtasks /Create /TN $TaskName /SC ONLOGON /RL LIMITED `
    /TR "`"$Exe`" serve $ExtraArgs" /F | Out-Null
schtasks /Run /TN $TaskName | Out-Null
Write-Host "已注册并启动: $TaskName($Exe serve $ExtraArgs)"
Write-Host "管理: schtasks /End /TN $TaskName 停止; 本脚本 -Unregister 卸载"
