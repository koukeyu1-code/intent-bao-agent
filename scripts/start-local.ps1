param(
    [switch]$NoBrowser,
    [ValidateRange(1, 65535)]
    [int]$BackendPort = 8000,
    [ValidateRange(1, 65535)]
    [int]$FrontendPort = 3000,
    [string]$RuntimeDir,
    [string]$DataDir
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$backendRoot = Join-Path $projectRoot "server"
if ([string]::IsNullOrWhiteSpace($RuntimeDir)) {
    $RuntimeDir = Join-Path $projectRoot "runtime"
}
if ([string]::IsNullOrWhiteSpace($DataDir)) {
    $DataDir = Join-Path $backendRoot "data"
}
$venvPython = Join-Path $backendRoot ".venv\Scripts\python.exe"
$frontendCli = Join-Path $projectRoot "node_modules\vinext\dist\cli.js"

function Find-Executable {
    param([Parameter(Mandatory = $true)][string[]]$Candidates)
    foreach ($candidate in $Candidates) {
        if ([string]::IsNullOrWhiteSpace($candidate)) { continue }
        $resolved = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($resolved) { return $resolved.Source }
        if (Test-Path -LiteralPath $candidate) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    return $null
}

function Test-ServiceHealth {
    param([Parameter(Mandatory = $true)][string]$Url)
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 500
    }
    catch { return $false }
}

function Wait-ForUrl {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [int]$Attempts = 90
    )
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        if (Test-ServiceHealth -Url $Url) { return $true }
        Start-Sleep -Milliseconds 700
    }
    return $false
}

function Wait-ForPort {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [int]$Attempts = 45
    )
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        if (Get-PortOwnerPid -Port $Port) { return $true }
        Start-Sleep -Milliseconds 700
    }
    return $false
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

function Get-ManagedService {
    param(
        [Parameter(Mandatory = $true)][string]$PidFile,
        [Parameter(Mandatory = $true)][string]$MetaFile,
        [Parameter(Mandatory = $true)][string[]]$ExpectedNames,
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$HealthUrl
    )
    if (-not (Test-Path -LiteralPath $PidFile) -or -not (Test-Path -LiteralPath $MetaFile)) {
        return $null
    }
    try {
        $savedPid = [int](Get-Content -LiteralPath $PidFile -Raw)
        $meta = Get-Content -LiteralPath $MetaFile -Raw | ConvertFrom-Json
        $process = Get-Process -Id $savedPid -ErrorAction SilentlyContinue
        $portOwner = Get-PortOwnerPid -Port $Port
        $startedAt = $process.StartTime.ToUniversalTime().ToString("o")
        if (
            -not $process -or
            -not ($ExpectedNames -contains $process.ProcessName) -or
            $portOwner -ne $process.Id -or
            $meta.pid -ne $process.Id -or
            $meta.process_started_at -ne $startedAt -or
            -not (Test-ServiceHealth -Url $HealthUrl)
        ) {
            return $null
        }
        return $process
    }
    catch { return $null }
}

function Start-HiddenProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string]$Arguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [string]$StandardOutputPath,
        [string]$StandardErrorPath
    )
    $startParameters = @{
        FilePath = $FilePath
        ArgumentList = $Arguments
        WorkingDirectory = $WorkingDirectory
        PassThru = $true
        WindowStyle = "Hidden"
    }
    if ($StandardOutputPath) {
        $startParameters.RedirectStandardOutput = $StandardOutputPath
    }
    if ($StandardErrorPath) {
        $startParameters.RedirectStandardError = $StandardErrorPath
    }
    return Start-Process @startParameters
}

function Write-ServiceMarker {
    param(
        [Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process,
        [Parameter(Mandatory = $true)][string]$PidFile,
        [Parameter(Mandatory = $true)][string]$MetaFile,
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$HealthUrl
    )
    $Process.Refresh()
    Set-Content -LiteralPath $PidFile -Value $Process.Id -NoNewline
    @{
        pid = $Process.Id
        process_started_at = $Process.StartTime.ToUniversalTime().ToString("o")
        port = $Port
        health_url = $HealthUrl
    } | ConvertTo-Json | Set-Content -LiteralPath $MetaFile -Encoding UTF8
}

function Show-InitialLoginCredentials {
    param(
        [Parameter(Mandatory = $true)][string]$CredentialsDirectory,
        [switch]$OpenFolder
    )
    if (-not (Test-Path -LiteralPath $CredentialsDirectory)) { return }
    $credentialFiles = @(
        Get-ChildItem -LiteralPath $CredentialsDirectory -Filter "*.txt" -File -ErrorAction SilentlyContinue
    )
    if ($credentialFiles.Count -eq 0) { return }

    # Best-effort NTFS hardening: remove inherited access and grant only the
    # desktop owner plus the effective process identity. Sandboxed launchers
    # can use a different process SID while retaining USERDOMAIN/USERNAME.
    # Python also creates each file with mode 0600.
    try {
        $effectiveIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
        $ownerIdentity = $effectiveIdentity
        $allowedIdentities = @($effectiveIdentity)
        if (-not [string]::IsNullOrWhiteSpace($env:USERNAME)) {
            $configuredAccount = if ([string]::IsNullOrWhiteSpace($env:USERDOMAIN)) {
                $env:USERNAME
            } else {
                "$($env:USERDOMAIN)\$($env:USERNAME)"
            }
            try {
                $configuredIdentity = ([System.Security.Principal.NTAccount]$configuredAccount).Translate(
                    [System.Security.Principal.SecurityIdentifier]
                )
                $ownerIdentity = $configuredIdentity
                if ($allowedIdentities.Value -notcontains $configuredIdentity.Value) {
                    $allowedIdentities += $configuredIdentity
                }
            }
            catch {
                Write-Warning "无法解析桌面用户身份，将仅保留当前进程访问初始凭据。"
            }
        }
        foreach ($file in $credentialFiles) {
            $acl = [System.Security.AccessControl.FileSecurity]::new()
            $acl.SetOwner($ownerIdentity)
            $acl.SetAccessRuleProtection($true, $false)
            foreach ($identity in $allowedIdentities) {
                $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
                    $identity,
                    [System.Security.AccessControl.FileSystemRights]::FullControl,
                    [System.Security.AccessControl.AccessControlType]::Allow
                )
                $acl.AddAccessRule($rule)
            }
            Set-Acl -LiteralPath $file.FullName -AclObject $acl
        }
    }
    catch {
        Write-Warning "无法自动收紧初始凭据文件权限，请确保该目录仅当前用户可访问。"
    }

    Write-Host ""
    Write-Host "首次登录凭据已在本机生成：" -ForegroundColor Yellow
    foreach ($file in $credentialFiles) {
        Write-Host "  $($file.FullName)"
    }
    Write-Host "请保存到个人密码管理器后删除以上文件。启动脚本不会在屏幕上打印密码。" -ForegroundColor Yellow
    if ($OpenFolder) {
        Start-Process -FilePath "explorer.exe" -ArgumentList $CredentialsDirectory
    }
}

if (-not (Test-Path -LiteralPath $venvPython) -or -not (Test-Path -LiteralPath $frontendCli)) {
    & (Join-Path $PSScriptRoot "setup-local.ps1")
}

$bundledNode = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
$node = Find-Executable -Candidates @("node.exe", "node", $bundledNode)
if (-not $node) { throw "未找到 Node.js 22。" }

New-Item -ItemType Directory -Path $RuntimeDir, $DataDir -Force | Out-Null
$logDir = Join-Path $RuntimeDir "logs"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$backendPidFile = Join-Path $RuntimeDir "backend.pid"
$frontendPidFile = Join-Path $RuntimeDir "frontend.pid"
$backendMetaFile = Join-Path $RuntimeDir "backend.meta.json"
$frontendMetaFile = Join-Path $RuntimeDir "frontend.meta.json"
$initialCredentialsDir = Join-Path $DataDir "config\initial-login-credentials"
$backendUrl = "http://127.0.0.1:$BackendPort/api/health"
$frontendUrl = "http://127.0.0.1:$FrontendPort"

$managedBackend = Get-ManagedService -PidFile $backendPidFile -MetaFile $backendMetaFile -ExpectedNames @("python", "pythonw") -Port $BackendPort -HealthUrl $backendUrl
$managedFrontend = Get-ManagedService -PidFile $frontendPidFile -MetaFile $frontendMetaFile -ExpectedNames @("node") -Port $FrontendPort -HealthUrl $frontendUrl
$backendHealthy = Test-ServiceHealth -Url $backendUrl
$frontendHealthy = Test-ServiceHealth -Url $frontendUrl

if ($backendHealthy -and $frontendHealthy) {
    if ($managedBackend -and $managedFrontend) {
        Write-Host "意向宝已经在运行：http://localhost:$FrontendPort" -ForegroundColor Green
    }
    else {
        Write-Host "检测到可用的本地服务（非当前运行目录托管），未改动已有进程。" -ForegroundColor Yellow
    }
    Show-InitialLoginCredentials -CredentialsDirectory $initialCredentialsDir -OpenFolder:(-not $NoBrowser)
    if (-not $NoBrowser) { Start-Process "http://localhost:$FrontendPort" }
    exit 0
}

$startedProcesses = @()
try {
    if (-not $backendHealthy) {
        Write-Host "正在启动后端服务（端口 $BackendPort）..." -ForegroundColor Cyan
        # Keep the backend as the direct child process so its PID can be
        # checked against the listening port.  Passing the isolated data
        # directory in Python avoids relying on parent-shell environment state.
        $escapedDataDir = (Resolve-Path -LiteralPath $DataDir).Path.Replace('\', '\\').Replace("'", "\\'")
        $escapedBackendRoot = $backendRoot.Replace('\', '\\').Replace("'", "\\'")
        $backendEntry = Join-Path $RuntimeDir "intent_bao_backend_entry.py"
        @"
import sys
sys.path.insert(0, '$escapedBackendRoot')
from app.main import create_app
app = create_app(data_dir='$escapedDataDir')
"@ | Set-Content -LiteralPath $backendEntry -Encoding UTF8
        $backendProcess = Start-HiddenProcess `
            -FilePath $venvPython `
            -Arguments "-m uvicorn intent_bao_backend_entry:app --host 127.0.0.1 --port $BackendPort" `
            -WorkingDirectory $RuntimeDir `
            -StandardOutputPath (Join-Path $logDir "backend.out.log") `
            -StandardErrorPath (Join-Path $logDir "backend.err.log")
        $startedProcesses += $backendProcess
    }
    if (-not $frontendHealthy) {
        Write-Host "正在启动前端服务（端口 $FrontendPort）..." -ForegroundColor Cyan
        $previousApiBase = $env:NEXT_PUBLIC_INTENT_BAO_API_BASE
        try {
            # Use one explicit loopback API origin. Authentication and
            # business requests both carry the browser-session Bearer token.
            $env:NEXT_PUBLIC_INTENT_BAO_API_BASE = "http://127.0.0.1:$BackendPort/api"
            $frontendProcess = Start-HiddenProcess `
                -FilePath $node `
                -Arguments "`"$frontendCli`" dev --hostname 127.0.0.1 --port $FrontendPort" `
                -WorkingDirectory $projectRoot `
                -StandardOutputPath (Join-Path $logDir "frontend.out.log") `
                -StandardErrorPath (Join-Path $logDir "frontend.err.log")
        }
        finally {
            if ($null -eq $previousApiBase) {
                Remove-Item Env:NEXT_PUBLIC_INTENT_BAO_API_BASE -ErrorAction SilentlyContinue
            }
            else {
                $env:NEXT_PUBLIC_INTENT_BAO_API_BASE = $previousApiBase
            }
        }
        $startedProcesses += $frontendProcess
        if (Wait-ForPort -Port $FrontendPort) {
            Write-Host "前端端口已启动，正在完成首次页面编译；通常还需 30-90 秒..." -ForegroundColor Yellow
        }
    }

    $backendReady = Wait-ForUrl -Url $backendUrl -Attempts 90
    $frontendReady = Wait-ForUrl -Url $frontendUrl -Attempts 180
    if (-not $backendReady -or -not $frontendReady) {
        throw "意向宝启动失败。请确认后端端口${BackendPort}和前端端口${FrontendPort}未被占用，并查看终端输出。"
    }

    if ($backendProcess) {
        $backendOwnerPid = Get-PortOwnerPid -Port $BackendPort
        $backendProcess = Get-Process -Id $backendOwnerPid -ErrorAction Stop
        Write-ServiceMarker -Process $backendProcess -PidFile $backendPidFile -MetaFile $backendMetaFile -Port $BackendPort -HealthUrl $backendUrl
    }
    if ($frontendProcess) {
        $frontendOwnerPid = Get-PortOwnerPid -Port $FrontendPort
        $frontendProcess = Get-Process -Id $frontendOwnerPid -ErrorAction Stop
        Write-ServiceMarker -Process $frontendProcess -PidFile $frontendPidFile -MetaFile $frontendMetaFile -Port $FrontendPort -HealthUrl $frontendUrl
    }
}
catch {
    foreach ($process in $startedProcesses) {
        if ($process -and -not $process.HasExited) { Stop-Process -Id $process.Id -Force }
    }
    throw
}

if (-not $NoBrowser) { Start-Process "http://localhost:$FrontendPort" }
Write-Host ""
Write-Host "意向宝已启动：" -ForegroundColor Green
Write-Host "  应用：http://localhost:$FrontendPort"
Write-Host "  后端：http://127.0.0.1:$BackendPort/docs"
Show-InitialLoginCredentials -CredentialsDirectory $initialCredentialsDir -OpenFolder:(-not $NoBrowser)
Write-Host ""
Write-Host "默认使用模拟分析；如需调用本地Ollama，请在页面“模型设置”中测试并保存。"
