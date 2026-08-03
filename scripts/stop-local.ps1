param(
    [string]$RuntimeDir,
    [ValidateRange(1, 65535)]
    [int]$BackendPort = 8000,
    [ValidateRange(1, 65535)]
    [int]$FrontendPort = 3000,
    [ValidateRange(1, 65535)]
    [int]$AsrPort = 8099
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($RuntimeDir)) {
    $RuntimeDir = Join-Path $projectRoot "runtime"
}

function Get-PortOwnerPid {
    param([Parameter(Mandatory = $true)][int]$Port)
    foreach ($line in (& netstat.exe -ano -p tcp 2>$null)) {
        if ($line -match "^\s*TCP\s+\S+:$Port\s+\S+\s+LISTENING\s+(\d+)\s*$") {
            return [int]$Matches[1]
        }
    }
    return $null
}

function Get-SafelyOwnedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$PidFile,
        [Parameter(Mandatory = $true)][string]$MetaFile,
        [Parameter(Mandatory = $true)][string[]]$ExpectedNames,
        [Parameter(Mandatory = $true)][int]$Port
    )
    if (-not (Test-Path -LiteralPath $PidFile) -or -not (Test-Path -LiteralPath $MetaFile)) {
        return $null
    }
    try {
        $savedPid = [int](Get-Content -LiteralPath $PidFile -Raw)
        $meta = Get-Content -LiteralPath $MetaFile -Raw | ConvertFrom-Json
        $process = Get-Process -Id $savedPid -ErrorAction SilentlyContinue
        if (
            -not $process -or
            -not ($ExpectedNames -contains $process.ProcessName) -or
            $meta.pid -ne $process.Id -or
            $meta.process_started_at -ne $process.StartTime.ToUniversalTime().ToString("o")
        ) {
            return $null
        }
        return $process
    }
    catch { return $null }
}

$services = @(
    @{ Name = "后端"; File = "backend.pid"; Meta = "backend.meta.json"; ExpectedNames = @("python", "pythonw"); Port = $BackendPort },
    @{ Name = "前端"; File = "frontend.pid"; Meta = "frontend.meta.json"; ExpectedNames = @("node"); Port = $FrontendPort },
    @{ Name = "ASR语音"; File = "asr.pid"; Meta = "asr.meta.json"; ExpectedNames = @("llama-server"); Port = $AsrPort }
)

foreach ($entry in $services) {
    $pidFile = Join-Path $RuntimeDir $entry.File
    $metaFile = Join-Path $RuntimeDir $entry.Meta
    $process = Get-SafelyOwnedProcess -PidFile $pidFile -MetaFile $metaFile -ExpectedNames $entry.ExpectedNames -Port $entry.Port
    if ($process) {
        Stop-Process -Id $process.Id -Force
        Remove-Item -LiteralPath $pidFile, $metaFile -Force -ErrorAction SilentlyContinue
        Write-Host "$($entry.Name)服务已停止。" -ForegroundColor Green
    }
    elseif ((Test-Path -LiteralPath $pidFile) -or (Test-Path -LiteralPath $metaFile)) {
        Write-Warning "$($entry.Name)服务缺少可信归属校验，未停止任何进程。请人工核对后清理运行状态文件。"
    }
}

Write-Host "意向宝本地停止操作完成。" -ForegroundColor Green
