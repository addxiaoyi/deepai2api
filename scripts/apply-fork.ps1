# 把 DeepAI provider 套用到 Open WebUI
#
# 这个脚本做三件事：
#   1. 克隆固定版本的 open-webui 到 vendor/open-webui
#   2. 打上 patches/ 里的改动 + 复制新增文件
#   3. 构建前端产物（build/），供 Dockerfile.deepai 做 overlay
#
#   .\scripts\apply-fork.ps1                 # 首次：克隆 + 套用 + 构建
#   .\scripts\apply-fork.ps1 -SkipBuild      # 只套用，不构建
#   .\scripts\apply-fork.ps1 -Force          # 丢弃 vendor 里已有改动重新来
param(
  [string]$Ref = "v0.13.4",
  [switch]$SkipBuild,
  [switch]$Force
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$target = Join-Path $root "vendor\open-webui"
$patches = Join-Path $root "patches"

function Need([string]$cmd, [string]$hint) {
  if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
    Write-Host "缺少 $cmd。$hint" -ForegroundColor Red
    exit 1
  }
}

Need git "安装 Git 后重试。"
Need node "安装 Node.js（<=22 更省事）后重试。"

if ((Test-Path $target) -and $Force) {
  Write-Host "删除已有 vendor/open-webui …" -ForegroundColor Yellow
  Remove-Item $target -Recurse -Force
}

if (-not (Test-Path $target)) {
  Write-Host "克隆 open-webui $Ref …" -ForegroundColor Cyan
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
  git clone --depth 1 --branch $Ref https://github.com/open-webui/open-webui.git $target
  if ($LASTEXITCODE -ne 0) { throw "克隆失败" }
} else {
  Write-Host "vendor/open-webui 已存在，跳过克隆" -ForegroundColor DarkGray
}

Write-Host "套用补丁 …" -ForegroundColor Cyan
Push-Location $target
try {
  $applied = git apply --check (Join-Path $patches "0001-register-deepai-provider.patch") 2>&1
  if ($LASTEXITCODE -eq 0) {
    git apply (Join-Path $patches "0001-register-deepai-provider.patch")
    Write-Host "  patch 已应用" -ForegroundColor Green
  } elseif ($applied -match "already applied|patch does not apply") {
    Write-Host "  patch 已应用过或冲突，尝试反向检测 …" -ForegroundColor Yellow
    $reverse = git apply --reverse --check (Join-Path $patches "0001-register-deepai-provider.patch") 2>&1
    if ($LASTEXITCODE -eq 0) { Write-Host "  已应用，跳过" -ForegroundColor Green }
    else { Write-Host "  补丁冲突：请 -Force 重新克隆" -ForegroundColor Red; exit 1 }
  } else {
    Write-Host "  $applied" -ForegroundColor Red
    exit 1
  }
} finally { Pop-Location }

Write-Host "复制新增文件 …" -ForegroundColor Cyan
Copy-Item (Join-Path $patches "files\*") $target -Recurse -Force
Write-Host "  完成" -ForegroundColor Green

if ($SkipBuild) {
  Write-Host "跳过前端构建（未生成 build/，Docker overlay 需要它）" -ForegroundColor Yellow
  exit 0
}

Write-Host "安装依赖并构建前端（首次约 3-5 分钟）…" -ForegroundColor Cyan
Push-Location $target
try {
  if (-not (Test-Path "node_modules")) {
    # Open WebUI 的 engines 上限是 22.x；Node 更高版本需要这个开关
    npm ci --engine-strict=false
    if ($LASTEXITCODE -ne 0) { throw "npm ci 失败" }
  }
  npm run build
  if ($LASTEXITCODE -ne 0) { throw "npm run build 失败" }
} finally { Pop-Location }

Write-Host ""
Write-Host "完成。接下来：" -ForegroundColor Green
Write-Host "  docker compose up -d --build      # 构建 overlay 镜像并启动"
Write-Host "  http://localhost:3000             # 注册管理员 → 设置 → Admin → DeepAI Code"