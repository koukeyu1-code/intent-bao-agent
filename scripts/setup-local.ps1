$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$backendRoot = Join-Path $projectRoot "server"
$venvPython = Join-Path $backendRoot ".venv\Scripts\python.exe"
$requirements = Join-Path $projectRoot "server\requirements.txt"

function Find-Executable {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Candidates
    )

    foreach ($candidate in $Candidates) {
        if ([string]::IsNullOrWhiteSpace($candidate)) {
            continue
        }

        $resolved = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($resolved) {
            return $resolved.Source
        }

        if (Test-Path -LiteralPath $candidate) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    return $null
}

function Ensure-ExternalTool {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Probe,
        [string]$WingetId = "",
        [string]$Hint = ""
    )

    if (Get-Command $Probe -ErrorAction SilentlyContinue) {
        return $true
    }
    if ($WingetId -and (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host "正在通过 winget 安装 $Name ..."
        & winget install --id $WingetId --accept-source-agreements --accept-package-agreements --disable-interactivity
        if (Get-Command $Probe -ErrorAction SilentlyContinue) {
            return $true
        }
    }
    Write-Host "[setup] 警告：未找到 $Name。$Hint" -ForegroundColor Yellow
    return $false
}

# 1) Python 3.11+
$pythonCandidates = @($env:PYTHON, "python.exe", "python3.exe") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
$python = Find-Executable -Candidates $pythonCandidates
if (-not $python) {
    throw "未找到 Python 3.11+。请先安装 Python（https://www.python.org/downloads/）并勾选 Add to PATH，或设置 PYTHON 环境变量指向 python.exe。"
}

# 2) 后端虚拟环境
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "正在创建本地 Python 虚拟环境（server\.venv）..."
    & $python -m venv (Join-Path $backendRoot ".venv")
}

if (-not (Test-Path -LiteralPath $requirements)) {
    throw "缺少后端依赖清单：$requirements"
}

# 3) PyTorch：优先 CUDA 版（有 NVIDIA 显卡时），失败自动回退 CPU 版
Write-Host "正在安装 PyTorch（优先 CUDA 版，需要几分钟；无显卡会自动回退 CPU 版）..."
& $venvPython -m pip install --disable-pip-version-check torch torchaudio --index-url https://download.pytorch.org/whl/cu126
if ($LASTEXITCODE -ne 0) {
    Write-Host "CUDA 版 PyTorch 安装失败，回退到默认 CPU 版（功能相同，速度较慢）。" -ForegroundColor Yellow
}

Write-Host "正在安装后端依赖..."
& $venvPython -m pip install --disable-pip-version-check -r $requirements
if ($LASTEXITCODE -ne 0) {
    throw "后端依赖安装失败，请查看上方日志。"
}

# 4) 前端依赖
$nodeModules = Join-Path $projectRoot "node_modules"
if (-not (Test-Path -LiteralPath $nodeModules)) {
    $pnpm = Find-Executable -Candidates @("pnpm.cmd", "pnpm")
    if (-not $pnpm) {
        $pnpm = Find-Executable -Candidates @((Join-Path $env:LOCALAPPDATA "pnpm\pnpm.cmd"))
    }
    if (-not $pnpm) {
        throw "未找到 pnpm。请先安装 Node.js 22+（https://nodejs.org/），然后运行 npm install -g pnpm。"
    }

    Write-Host "正在安装前端依赖（node_modules）..."
    Push-Location $projectRoot
    try {
        & $pnpm install
        if ($LASTEXITCODE -ne 0) {
            throw "前端依赖安装失败。"
        }
    }
    finally {
        Pop-Location
    }
}

# 5) 外部工具与模型检查
Ensure-ExternalTool -Name "FFmpeg" -Probe "ffmpeg" -WingetId "Gyan.FFmpeg" -Hint "请安装 FFmpeg 并加入 PATH（winget install Gyan.FFmpeg），否则录音/录屏处理不可用。"
Ensure-ExternalTool -Name "FFprobe" -Probe "ffprobe" -Hint "FFprobe 随 FFmpeg 一起安装，用于探测媒体时长。"

$tesseract = Find-Executable -Candidates @("tesseract.exe")
if (-not $tesseract) {
    Write-Host "[setup] 警告：未找到 Tesseract OCR（录屏关键帧 OCR 不可用）。请从 https://github.com/UB-Mannheim/tesseract/wiki 安装 64 位版本，并勾选 Chinese (Simplified) 语言包。" -ForegroundColor Yellow
}
else {
    $langs = & $tesseract --list-langs 2>$null
    if ($langs -notmatch "chi_sim") {
        Write-Host "[setup] 警告：Tesseract 缺少中文语言包 chi_sim，中文 OCR 不可用。请重新运行 Tesseract 安装程序并勾选 Chinese (Simplified)。" -ForegroundColor Yellow
    }
}

if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    Write-Host "[setup] 警告：未找到 Ollama（意向抽取大模型）。请从 https://ollama.com/download 安装，然后执行 ollama pull qwen3.5:9b。" -ForegroundColor Yellow
}

$asrModel = Join-Path $projectRoot "models\qwen3-asr\Qwen3-ASR-1.7B.Q8_0.gguf"
$asrServer = Join-Path $projectRoot "models\llama.cpp\llama-server.exe"
$funasrDir = Join-Path $projectRoot "models\funasr\models"
$missing = @()
if (-not (Test-Path -LiteralPath $asrModel)) { $missing += "models/qwen3-asr（Qwen3-ASR 模型）" }
if (-not (Test-Path -LiteralPath $asrServer)) { $missing += "models/llama.cpp（llama-server 运行时）" }
if (-not (Test-Path -LiteralPath $funasrDir)) { $missing += "models/funasr（说话人分离声纹模型）" }
if ($missing.Count -gt 0) {
    Write-Host "[setup] 警告：models/ 目录缺少：$($missing -join '、')。请参照 README「模型准备」补齐后，语音转写与说话人分离才能使用。" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "意向宝本地环境准备完成。" -ForegroundColor Green
Write-Host "下一步：双击 启动意向宝.bat 启动；首次登录凭据见启动后的提示目录。"
