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

$bundledPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$basePython = Find-Executable -Candidates @("python.exe", "python3.exe", $bundledPython)
if (-not $basePython) {
    throw "未找到 Python 3。请先安装 Python 3.11+，或在 Codex 桌面环境中运行。"
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "正在创建本地 Python 环境..."
    & $basePython -m venv (Join-Path $backendRoot ".venv")
}

if (-not (Test-Path -LiteralPath $requirements)) {
    throw "缺少后端依赖清单：$requirements"
}

Write-Host "正在安装后端依赖..."
& $venvPython -m pip install --disable-pip-version-check -r $requirements

$nodeModules = Join-Path $projectRoot "node_modules"
if (-not (Test-Path -LiteralPath $nodeModules)) {
    $bundledPnpm = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\bin\fallback\pnpm.cmd"
    $pnpm = Find-Executable -Candidates @("pnpm.cmd", "pnpm", $bundledPnpm)
    if (-not $pnpm) {
        throw "未找到 pnpm，且前端依赖尚未安装。请安装 Node.js 22 和 pnpm。"
    }

    Write-Host "正在安装前端依赖..."
    Push-Location $projectRoot
    try {
        & $pnpm install
    }
    finally {
        Pop-Location
    }
}

Write-Host ""
Write-Host "意向宝初版本地环境准备完成。" -ForegroundColor Green
