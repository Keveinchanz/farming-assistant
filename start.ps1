# ============================================================================
# 田头农技台 —— 一键启动（任务 6.3）
#
# 用法（在项目根目录执行）：
#     .\start.ps1                 # 启动后端服务并打开浏览器
#     .\start.ps1 -Port 8080      # 换端口
#     .\start.ps1 -Setup          # 先跑一遍数据准备（已生成过会自动跳过/增量）
#     .\start.ps1 -NoBrowser      # 不自动打开浏览器
#
# 首次使用请先准备环境：
#     conda activate az
#     python -m pip install -r requirements.txt
# ============================================================================

[CmdletBinding()]
param(
    [int]$Port = 8000,
    [string]$BindHost = "127.0.0.1",
    [switch]$Setup,
    [switch]$NoBrowser,
    [string]$EnvName = "az"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
Set-Location $ProjectRoot

function Write-Step($text) { Write-Host "`n=== $text ===" -ForegroundColor Cyan }
function Write-Ok($text)   { Write-Host "  [OK] $text" -ForegroundColor Green }
function Write-Warn2($text) { Write-Host "  [!!] $text" -ForegroundColor Yellow }

# ---------------------------------------------------------------- 找 Python

Write-Step "定位 Python 解释器"

$python = $null
$condaEnvPython = Join-Path $env:USERPROFILE ".conda\envs\$EnvName\python.exe"
if (Test-Path $condaEnvPython) {
    $python = $condaEnvPython
    Write-Ok "使用 conda 环境 $EnvName：$python"
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $python = (Get-Command python).Source
    Write-Warn2 "未找到 conda 环境 $EnvName，改用 PATH 中的 python：$python"
} else {
    Write-Host "找不到 Python。请先安装 Anaconda 并创建环境：conda create -n az python=3.11" -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------- 依赖检查

Write-Step "检查依赖"

$required = @("fastapi", "uvicorn", "onnxruntime", "torch", "yaml", "PIL")
$missing = @()
foreach ($pkg in $required) {
    $import = if ($pkg -eq "yaml") { "yaml" } else { $pkg }
    # PowerShell 5.1 下，原生命令写到 stderr 的内容在 $ErrorActionPreference="Stop" 时
    # 会变成终止性错误（NativeCommandError），于是「缺少依赖」的友好提示会被一段 traceback
    # 打断、脚本直接退出。这里用 try/catch 把它归一成「这个包不可用」。
    $ok = $true
    try {
        & $python -c "import $import" 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { $ok = $false }
    } catch {
        $ok = $false
    }
    if (-not $ok) { $missing += $pkg }
}

if ($missing.Count -gt 0) {
    Write-Warn2 ("缺少依赖：" + ($missing -join ", "))
    Write-Host "  请执行： & `"$python`" -m pip install -r requirements.txt" -ForegroundColor Yellow
    exit 1
}
Write-Ok "核心依赖齐备"

# ---------------------------------------------------------------- 数据准备

function Test-Artifact($path, $desc) {
    if (Test-Path $path) { Write-Ok "$desc 已就绪"; return $true }
    Write-Warn2 "$desc 缺失：$path"; return $false
}

if ($Setup) {
    Write-Step "生成数据（任务 1.1 ~ 1.6 / 4.5）"
    $steps = @(
        @{ Script = "scripts\01_organize_data.py";    Desc = "数据归档" },
        @{ Script = "scripts\02_split_dataset.py";    Desc = "数据集划分" },
        @{ Script = "scripts\03_build_label_map.py";  Desc = "标签映射" },
        @{ Script = "scripts\04_gen_province_pest.py"; Desc = "各省病虫害数据" },
        @{ Script = "scripts\05_fetch_policies.py";   Desc = "惠农政策抓取（需联网）" },
        @{ Script = "scripts\07_build_library.py";    Desc = "知识图书库" },
        @{ Script = "scripts\23_init_farm_db.py";     Desc = "农田数据库（阶段 7）" }
    )
    foreach ($s in $steps) {
        Write-Host "  -> $($s.Desc)" -ForegroundColor Gray
        & $python $s.Script
        if ($LASTEXITCODE -ne 0) { Write-Warn2 "$($s.Desc) 失败，但继续启动服务" }
    }
} else {
    Write-Step "检查数据与模型产物"
    $ok = $true
    $ok = (Test-Artifact "data\label_map.json" "标签映射") -and $ok
    $ok = (Test-Artifact "data\splits\train.csv" "数据集划分") -and $ok
    $ok = (Test-Artifact "data\province_pest_top3.json" "各省病虫害数据") -and $ok
    $ok = (Test-Artifact "data\policy_list.json" "惠农政策数据") -and $ok
    $ok = (Test-Artifact "knowledge\library\library_items.json" "知识图书库") -and $ok
    $modelOk = (Test-Artifact "models\onnx\inference_model.onnx" "ONNX 模型")

    if (-not $ok) {
        Write-Host "`n  部分数据缺失。可以：" -ForegroundColor Yellow
        Write-Host "    .\start.ps1 -Setup        自动生成（政策抓取需联网）" -ForegroundColor Yellow
    }
    if (-not $modelOk) {
        Write-Host "  图像识别模型未就绪。训练模型：" -ForegroundColor Yellow
        Write-Host "    & `"$python`" scripts\10_train_resnet50.py      约 10~60 分钟（RTX 5060）" -ForegroundColor Yellow
        Write-Host "    & `"$python`" scripts\13_export_onnx.py" -ForegroundColor Yellow
    }

    # 阶段 7 的农田库：不在这里判失败 —— 服务启动时会自己建（lifespan 里调 init_db）。
    # 这里只说清现状，避免用户以为少了什么。
    if (Test-Path "data\farm\farm.db") {
        Write-Ok "农田数据库已就绪"
    } else {
        Write-Warn2 "农田数据库尚未建立 —— 服务启动时会自动创建（或手动跑 scripts\23_init_farm_db.py）"
    }
}

# ---------------------------------------------------------------- 可选能力提示

Write-Step "可选能力状态"
$envFile = Join-Path $ProjectRoot ".env"
if (Test-Path $envFile) {
    $envText = Get-Content $envFile -Raw -Encoding UTF8
    if ($envText -match "BAIDU_API_KEY=\s*\S") { Write-Ok "百度语音 Key 已配置" }
    else { Write-Warn2 "未配置百度语音 Key —— 语音识别/方言播报将走 Mock 降级" }
    if ($envText -match "DEEPSEEK_API_KEY=\s*\S") { Write-Ok "DeepSeek Key 已配置" }
    else { Write-Warn2 "未配置 DeepSeek Key —— 诊断报告将由本地规则给出" }
} else {
    Write-Warn2 "未找到 .env，正在从 .env.example 复制一份模板"
    Copy-Item ".env.example" ".env"
    Write-Host "  请编辑 .env 填入 API Key（不填也能以演示模式运行）" -ForegroundColor Yellow
}

# ---------------------------------------------------------------- 启动

Write-Step "启动服务"
Write-Host "  首页      http://${BindHost}:${Port}/" -ForegroundColor White
Write-Host "  我的农田  http://${BindHost}:${Port}/farm.html   （首次访问需注册，会自动建三条示例田）" -ForegroundColor White
Write-Host "  接口文档  http://${BindHost}:${Port}/docs" -ForegroundColor White
Write-Host "  服务状态  http://${BindHost}:${Port}/api/health" -ForegroundColor White
Write-Host "`n  按 Ctrl+C 停止服务`n" -ForegroundColor Gray

# 录音（农田页语音记账、诊断页、图书库语音检索）只在安全上下文里可用：
# http://127.0.0.1 与 localhost 算，局域网 IP 不算。用 IP 打开时页面会明确提示，
# 但在这里先说一句，免得演示时才发现。
if ($BindHost -ne "127.0.0.1" -and $BindHost -ne "localhost") {
    Write-Warn2 "当前用 $BindHost 打开：浏览器的录音 API 只在 127.0.0.1 / localhost 下可用，语音功能会提示需要安全上下文"
}

$args = @("scripts\20_run_backend.py", "--host", $BindHost, "--port", $Port)
if ($NoBrowser) { $args += "--no-browser" }

& $python @args
