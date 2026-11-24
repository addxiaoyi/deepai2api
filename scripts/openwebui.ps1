# 一键启动：构建 OpenWebUI fork 并接到网关
#
#   .\scripts\openwebui.ps1 -Token <网关管理口令>
#   .\scripts\openwebui.ps1 -Token <口令> -BindGateway      # 顺便把网关切到 0.0.0.0
#
# 说明：这一版**不再拉官方镜像**，而是构建 vendor/open-webui（Open WebUI 0.13.4
# + DeepAI provider）。前端就是 Open WebUI 本体，管理入口在「设置 → DeepAI Code」。
param(
  [string]$Token,
  [string]$Gateway = "http://127.0.0.1:8787",
  [int]$Port = 3000,
  [switch]$BindGateway,
  [switch]$NoDocker
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root

function Get-EnvValue([string]$name) {
  if (-not (Test-Path ".env")) { return $null }
  $match = Select-String -Path ".env" -Pattern "^$name=(.*)$"
  if ($match) { return $match.Matches[0].Groups[1].Value.Trim() }
  return $null
}

if (-not $Token) { $Token = Get-EnvValue "DEEPAI_GATEWAY_TOKEN" }
if (-not $Token) { $Token = $env:DEEPAI_GATEWAY_TOKEN }

# 网关在线检查
$health = $null
try { $health = Invoke-RestMethod -Uri "$Gateway/health" -TimeoutSec 5 -NoProxy } catch {
  try { $health = Invoke-RestMethod -Uri "$Gateway/health" -TimeoutSec 5 } catch {
    Write-Host "网关没有响应 $Gateway/health —— 先运行 node src\index.mjs --host 0.0.0.0" -ForegroundColor Red
    exit 1
  }
}
Write-Host ("网关在线 · 账号: " + ($(if ($health.account) { $health.account.username } else { "未连接" })) + " · 模型通道: " + $health.lane) -ForegroundColor Green

if (-not $Token) {
  Write-Host "缺少网关管理口令（网关启动时打印，也可用 node src\index.mjs --show-admin-token 取）。" -ForegroundColor Yellow
  Write-Host "先跑：.\scripts\openwebui.ps1 -Token <口令>"
  Write-Host "（也可以先启动 OpenWebUI，再在「设置 → Admin → DeepAI Code」里填。）"
}

$host_ = $Gateway -replace '^https?://','' -replace ':\d+$',''
if ($host_ -in @("127.0.0.1", "localhost")) {
  Write-Host ""
  Write-Host "注意：容器里的 host.docker.internal 到不了 127.0.0.1。" -ForegroundColor Yellow
  if ($BindGateway) {
    Write-Host "把网关切到 0.0.0.0 …" -ForegroundColor Yellow
    Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
      Where-Object { $_.CommandLine -like "*src\index.mjs*" } |
      ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Start-Sleep -Milliseconds 500
    Start-Process -FilePath "node" -ArgumentList "src\index.mjs","--host","0.0.0.0","--port","8787" `
      -WorkingDirectory $root -WindowStyle Hidden
    Start-Sleep -Seconds 3
    Write-Host "网关已重启在 0.0.0.0:8787" -ForegroundColor Green
  } else {
    Write-Host "  请改用： node src\index.mjs --host 0.0.0.0   或加 -BindGateway" -ForegroundColor Yellow
  }
}

$lines = @(
  "# 由 scripts/openwebui.ps1 生成",
  "DEEPAI_GATEWAY_URL=http://host.docker.internal:8787",
  "DEEPAI_GATEWAY_TOKEN=$Token",
  "OPENWEBUI_PORT=$Port"
)
Set-Content -Path ".env" -Value ($lines -join "`n") -Encoding utf8
Write-Host ".env 已写入"

# fork 前端是否已构建
$buildDir = "vendor\open-webui\build"
$frontendDir = "vendor\open-webui\frontend"
if (-not (Test-Path $frontendDir) -and -not (Test-Path $buildDir)) {
  Write-Host ""
  Write-Host "fork 还没有构建产物。首次需要：" -ForegroundColor Yellow
  Write-Host "  cd vendor\open-webui"
  Write-Host "  npm ci --engine-strict=false     # 需要 Node <=22 才不用这个开关"
  Write-Host "  npm run build"
  Write-Host "  cd ..\.."
  if (-not $NoDocker) { Write-Host "或者直接用 Docker 构建（compose 里已是 build 模式）：docker compose up -d --build" }
}

if ($NoDocker) { exit 0 }

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  Write-Host ""
  Write-Host "没找到 docker CLI。pip 方式：" -ForegroundColor Yellow
  Write-Host "  cd vendor\open-webui; python -m venv .venv; .venv\Scripts\pip install -e ."
  Write-Host "  `$env:DEEPAI_GATEWAY_URL='$Gateway'; `$env:DEEPAI_GATEWAY_TOKEN='$Token'"
  Write-Host "  .venv\Scripts\open-webui serve --port $Port"
  exit 0
}

docker version --format '{{.Server.Version}}' 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
  Write-Host ""
  Write-Host "Docker 守护进程没启动（CLI 在，Docker Desktop 没跑）。" -ForegroundColor Yellow
  Write-Host "启动 Docker Desktop 后重跑，或：docker compose up -d --build"
  exit 0
}

Write-Host "构建 fork 镜像（首次较慢：装依赖 + 前端构建）…" -ForegroundColor Yellow
docker compose up -d --build

Write-Host ""
Write-Host "OpenWebUI: http://localhost:$Port" -ForegroundColor Green
Write-Host "接下来：注册管理员 → 设置 → Admin → DeepAI Code → 登录 DeepAI → 创建 Key → 接入并启用模型"
