# start-dev.ps1 — knowledge-base-management Windows dev launcher
# No Docker/WSL2 needed, all native components
# Usage: right-click "Run with PowerShell", or .\start-dev.ps1 in terminal
# Optional: .\start-dev.ps1 -Profile minimum|recommended|high-performance
# Automation: .\start-dev.ps1 -InitOnly | .\start-dev.ps1 -Background

param(
    [switch]$Install,
    [switch]$Quiet,
    [switch]$Stop,
    [switch]$NoAutoInstall,
    [switch]$InitOnly,
    [switch]$Background,
    [ValidateSet("auto", "minimum", "recommended", "high-performance")]
    [string]$Profile = "auto"
)

$ErrorActionPreference = "Continue"
$Global:ExitCode = 0
$RootDir = $PSScriptRoot

$logFile = "$RootDir\start-dev.log"
function Log { Add-Content -Path $logFile -Value "[$(Get-Date -Format 'HH:mm:ss')] $args" }

function Write-Info  { Write-Host "[INFO] $args" -Foreground Cyan }
function Write-Ok   { Write-Host "[OK]   $args" -Foreground Green }
function Write-Warn { Write-Host "[WARN] $args" -Foreground Yellow }
function Write-Err  { Write-Host "[ERR]  $args" -Foreground Red }

function Pause-IfInteractive([string]$Message = "Press Enter to exit") {
    if (-not $Quiet) { Read-Host $Message | Out-Null }
}

function Import-DotEnv([string]$Path) {
    foreach ($rawLine in [System.IO.File]::ReadAllLines($Path)) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith("#")) { continue }
        $parts = $line -split "=", 2
        if ($parts.Count -ne 2) { continue }
        $name = $parts[0].Trim()
        $value = $parts[1].Trim().Trim('"').Trim("'")
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}

function Set-DotEnvValue([string]$Path, [string]$Key, [string]$Value) {
    $lines = [System.Collections.Generic.List[string]]::new()
    if (Test-Path -LiteralPath $Path) {
        $lines.AddRange([string[]][System.IO.File]::ReadAllLines($Path))
    }
    $prefix = "$Key="
    $updated = $false
    for ($index = 0; $index -lt $lines.Count; $index++) {
        if ($lines[$index].StartsWith($prefix, [System.StringComparison]::Ordinal)) {
            $lines[$index] = "$prefix$Value"
            $updated = $true
            break
        }
    }
    if (-not $updated) { $lines.Add("$prefix$Value") }
    $utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($Path, $lines, $utf8WithoutBom)
}

function Set-HardwareProfile([string]$Name, [string]$TargetEnvFile) {
    if (-not $Name -or $Name -eq "auto") { return }
    $profilePath = Join-Path $RootDir "deploy\profiles\$Name.env"
    if (-not (Test-Path -LiteralPath $profilePath)) {
        throw "Hardware profile not found: $profilePath"
    }
    foreach ($rawLine in [System.IO.File]::ReadAllLines($profilePath)) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) { continue }
        $parts = $line -split "=", 2
        Set-DotEnvValue $TargetEnvFile $parts[0].Trim() $parts[1].Trim()
    }
    Write-Info "Applied hardware profile: $Name"
}

function Refresh-ProcessPath {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath"
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-DownloadWithFallback(
    [string[]]$Urls,
    [string]$OutFile,
    [string]$DisplayName
) {
    foreach ($url in $Urls) {
        try {
            Write-Info "Downloading $DisplayName from $url"
            Invoke-WebRequest -Uri $url -OutFile $OutFile -UseBasicParsing -TimeoutSec 300
            if ((Test-Path -LiteralPath $OutFile) -and (Get-Item -LiteralPath $OutFile).Length -gt 0) {
                return $true
            }
        } catch {
            Write-Warn "$DisplayName source unavailable, trying next source."
            Log "$DisplayName download failed from $url : $_"
            Remove-Item -LiteralPath $OutFile -Force -ErrorAction SilentlyContinue
        }
    }
    return $false
}

function Install-WingetPackage([string]$Id, [string]$DisplayName) {
    $winget = Get-Command "winget" -ErrorAction SilentlyContinue
    if (-not $winget) { return $false }
    Write-Info "Installing $DisplayName with winget..."
    & $winget.Source install --id $Id --exact --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
    $ok = $LASTEXITCODE -eq 0
    if (-not $ok) { Write-Warn "winget could not install $DisplayName; using fallback installer." }
    Refresh-ProcessPath
    return $ok
}

function Find-PythonCommand {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:ProgramFiles\Python313\python.exe",
        "python3", "python", "py"
    )
    foreach ($candidate in $candidates) {
        if ($candidate.EndsWith(".exe") -and -not (Test-Path -LiteralPath $candidate)) { continue }
        try {
            $version = (& $candidate -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null).Trim()
            if ($version -match "^(\d+)\.(\d+)$" -and
                ([int]$Matches[1] -gt 3 -or ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 11))) {
                return $candidate
            }
        } catch { Log "Python candidate unavailable: $candidate" }
    }
    return $null
}

function Install-PythonRuntime {
    if ($NoAutoInstall) { return $null }
    Install-WingetPackage "Python.Python.3.13" "Python 3.13" | Out-Null
    $pythonCommand = Find-PythonCommand
    if ($pythonCommand) { return $pythonCommand }

    $version = if ($env:PYTHON_BOOTSTRAP_VERSION) { $env:PYTHON_BOOTSTRAP_VERSION } else { "3.13.14" }
    $installer = Join-Path $env:TEMP "python-$version-amd64.exe"
    $downloaded = Invoke-DownloadWithFallback @(
        "https://mirrors.tuna.tsinghua.edu.cn/python/$version/python-$version-amd64.exe",
        "https://www.python.org/ftp/python/$version/python-$version-amd64.exe"
    ) $installer "Python $version"
    if (-not $downloaded) { return $null }
    Write-Info "Installing Python $version for the current user..."
    $process = Start-Process -FilePath $installer -ArgumentList @(
        "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_test=0",
        "Include_launcher=1", "SimpleInstall=1"
    ) -Wait -PassThru
    Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
    Refresh-ProcessPath
    if ($process.ExitCode -ne 0) { Log "Python installer exit code: $($process.ExitCode)" }
    return Find-PythonCommand
}

function Find-OllamaExecutable {
    $command = Get-Command "ollama" -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    foreach ($path in @(
        "$env:ProgramFiles\Ollama\ollama.exe",
        "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
    )) {
        if (Test-Path -LiteralPath $path) { return $path }
    }
    return $null
}

function Install-OllamaRuntime {
    if ($NoAutoInstall) { return $null }
    Install-WingetPackage "Ollama.Ollama" "Ollama" | Out-Null
    $ollama = Find-OllamaExecutable
    if ($ollama) { return $ollama }

    $installer = Join-Path $env:TEMP "OllamaSetup.exe"
    if (-not (Invoke-DownloadWithFallback @(
        "https://ollama.com/download/OllamaSetup.exe"
    ) $installer "Ollama")) { return $null }
    Write-Info "Installing Ollama..."
    $process = Start-Process -FilePath $installer -ArgumentList "/S" -Wait -PassThru
    Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
    Refresh-ProcessPath
    if ($process.ExitCode -ne 0) { Log "Ollama installer exit code: $($process.ExitCode)" }
    return Find-OllamaExecutable
}

function Find-MemuraiExecutable {
    $command = Get-Command "memurai" -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    foreach ($path in @(
        "$env:ProgramFiles\Memurai\memurai.exe",
        "${env:ProgramFiles(x86)}\Memurai\memurai.exe",
        "$env:LOCALAPPDATA\Memurai\memurai.exe"
    )) {
        if (Test-Path -LiteralPath $path) { return $path }
    }
    return $null
}

function Install-MemuraiRuntime {
    if ($NoAutoInstall) { return $null }
    Install-WingetPackage "Memurai.MemuraiDeveloper" "Memurai Developer" | Out-Null
    $memurai = Find-MemuraiExecutable
    if ($memurai) { return $memurai }

    $version = if ($env:MEMURAI_BOOTSTRAP_VERSION) { $env:MEMURAI_BOOTSTRAP_VERSION } else { "4.1.2" }
    $installer = Join-Path $env:TEMP "Memurai-Developer-v$version.msi"
    if (-not (Invoke-DownloadWithFallback @(
        "https://dist.memurai.com/releases/Memurai-Developer/$version/Memurai-Developer-v$version.msi"
    ) $installer "Memurai Developer $version")) { return $null }
    Write-Info "Installing Memurai Developer (Windows Redis-compatible service)..."
    $msiArguments = @("/i", "`"$installer`"", "/qn", "/norestart")
    if (Test-IsAdministrator) {
        $process = Start-Process -FilePath "msiexec.exe" -ArgumentList $msiArguments -Wait -PassThru
    } else {
        Write-Info "Memurai requires administrator permission; Windows will show a UAC prompt."
        $process = Start-Process -FilePath "msiexec.exe" -ArgumentList $msiArguments -Verb RunAs -Wait -PassThru
    }
    Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
    Refresh-ProcessPath
    if ($process.ExitCode -ne 0) { Log "Memurai installer exit code: $($process.ExitCode)" }
    return Find-MemuraiExecutable
}

function Test-PythonDependencies([string]$PythonCommand) {
    & $PythonCommand -c "import fastapi, uvicorn, chromadb, redis, minio, mcp, markdown, bleach, graphify, networkx" *> $null
    return $LASTEXITCODE -eq 0
}

function Test-RedisReady([string]$PythonCommand) {
    & $PythonCommand -c "import os, redis; client = redis.from_url(os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379/0'), socket_connect_timeout=1, socket_timeout=1); assert client.ping() is True; client.close()" *> $null
    return $LASTEXITCODE -eq 0
}

function Wait-RedisReady([string]$PythonCommand, [int]$TimeoutSeconds = 20) {
    for ($attempt = 0; $attempt -le $TimeoutSeconds; $attempt++) {
        if (Test-RedisReady $PythonCommand) { return $true }
        if ($attempt -lt $TimeoutSeconds) { Start-Sleep -Seconds 1 }
    }
    return $false
}

function Test-GatewayReady([string]$HealthUrl) {
    try {
        $response = Invoke-WebRequest $HealthUrl -Method GET -TimeoutSec 3 -UseBasicParsing
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Wait-GatewayReady(
    [string]$HealthUrl,
    [System.Diagnostics.Process]$Process,
    [int]$TimeoutSeconds = 90
) {
    for ($attempt = 0; $attempt -le $TimeoutSeconds; $attempt++) {
        if (Test-GatewayReady $HealthUrl) { return $true }
        if ($Process) {
            try {
                $Process.Refresh()
                if ($Process.HasExited) { return $false }
            } catch {
                return $false
            }
        }
        if ($attempt -gt 0 -and $attempt % 10 -eq 0) {
            Write-Info "  -> Gateway is still starting (${attempt}s); waiting for /health..."
        }
        if ($attempt -lt $TimeoutSeconds) { Start-Sleep -Seconds 1 }
    }
    return $false
}

function Find-MatchingProcessAncestor([int]$ProcessId, [string]$CommandPattern) {
    $currentProcessId = $ProcessId
    for ($depth = 0; $depth -lt 8 -and $currentProcessId -gt 0; $depth++) {
        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $currentProcessId" -ErrorAction SilentlyContinue
        if (-not $processInfo) { return 0 }
        if ($processInfo.CommandLine -match $CommandPattern) { return [int]$processInfo.ProcessId }
        $currentProcessId = [int]$processInfo.ParentProcessId
    }
    return 0
}

function Stop-ProcessTree([int]$ProcessId) {
    $taskkill = Get-Command "taskkill.exe" -ErrorAction SilentlyContinue
    if ($taskkill) {
        & $taskkill.Source /PID $ProcessId /T /F *> $null
    } else {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }
}

$localEnvFile = "$RootDir\.env.local"
$localEnvCreated = $false
if (-not (Test-Path -LiteralPath $localEnvFile)) {
    Copy-Item -LiteralPath "$RootDir\.env.example.local" -Destination $localEnvFile
    $localEnvCreated = $true
    Write-Info "Created .env.local from .env.example.local"
}
if ($Profile -ne "auto") {
    Set-HardwareProfile $Profile $localEnvFile
} elseif ($localEnvCreated) {
    Set-HardwareProfile "recommended" $localEnvFile
}
Import-DotEnv $localEnvFile
# Normalize legacy localhost values for Windows IPv4 services. PowerShell 5.1 can
# spend the entire short health timeout on ::1 even when the service listens on IPv4.
if (-not $env:REDIS_URL) { $env:REDIS_URL = "redis://127.0.0.1:6379/0" }
if (-not $env:CHROMA_HOST) { $env:CHROMA_HOST = "127.0.0.1" }
if (-not $env:CHROMA_PORT) { $env:CHROMA_PORT = "8001" }
if (-not $env:OLLAMA_URL) { $env:OLLAMA_URL = "http://127.0.0.1:11434" }
if (-not $env:MINIO_ENDPOINT) { $env:MINIO_ENDPOINT = "127.0.0.1:9000" }
if ($env:REDIS_URL -match "^redis://localhost(?=[:/])") {
    $env:REDIS_URL = $env:REDIS_URL -replace "^redis://localhost", "redis://127.0.0.1"
}
if ($env:CHROMA_HOST -eq "localhost") { $env:CHROMA_HOST = "127.0.0.1" }
if ($env:OLLAMA_URL -match "^https?://localhost(?=[:/])") {
    $env:OLLAMA_URL = $env:OLLAMA_URL -replace "^(https?://)localhost", '${1}127.0.0.1'
}
if ($env:MINIO_ENDPOINT -match "^localhost(?=:)" ) {
    $env:MINIO_ENDPOINT = $env:MINIO_ENDPOINT -replace "^localhost", "127.0.0.1"
}
if ($InitOnly) {
    foreach ($requiredName in @("HARDWARE_PROFILE", "REDIS_URL", "CHROMA_HOST", "OLLAMA_URL")) {
        $requiredValue = [Environment]::GetEnvironmentVariable($requiredName, "Process")
        if ([string]::IsNullOrWhiteSpace($requiredValue)) {
            Write-Err "Required local setting is missing: $requiredName"
            exit 1
        }
    }
    Write-Ok "Local configuration initialized: $localEnvFile"
    exit 0
}
if (-not $env:KBDATA_DIR) {
    $env:KBDATA_DIR = "$RootDir\kbdata"
} elseif (-not [System.IO.Path]::IsPathRooted($env:KBDATA_DIR)) {
    $env:KBDATA_DIR = [System.IO.Path]::GetFullPath((Join-Path $RootDir $env:KBDATA_DIR))
}
New-Item -ItemType Directory -Path $env:KBDATA_DIR -Force | Out-Null

# ---------- Stop mode: stop only listeners owned by this dev stack ----------
if ($Stop) {
    Write-Info "Stopping project Gateway, Chroma and MinIO listeners..."
    $count = 0
    $candidatePids = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalPort -in @(8000, 8001, 9000, 9001) } |
        Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($candidatePid in $candidatePids) {
        $projectProcessId = Find-MatchingProcessAncestor $candidatePid "uvicorn.+src\.main:app|chroma.+run|minio.+server"
        if ($projectProcessId -gt 0) {
            Stop-ProcessTree $projectProcessId
            $count++
            Write-Ok "  stopped PID $projectProcessId (listener PID $candidatePid)"
            Log "Stopped project process tree PID $projectProcessId (listener PID $candidatePid)"
        }
    }
    if ($count -eq 0) {
        Write-Warn "No project listeners were running."
    } else {
        Write-Ok "Stopped $count process(es)."
    }
    Write-Info "Shared Ollama and Redis/Memurai processes were left running."
    exit 0
}


# ---------- Step 1: Python ----------
Write-Info "Step 1/7 - Checking Python..."
Log "Check Python"
$python = Find-PythonCommand
if (-not $python) {
    Write-Info "Python 3.11+ not found; starting automatic installation."
    $python = Install-PythonRuntime
}
if (-not $python) {
    Write-Err "Python 3.11+ is required. Automatic installation failed or was disabled."
    Log "Python not found"
    Pause-IfInteractive
    exit 1
}
Write-Ok "$(& $python --version)"
Log "Using: $(& $python --version)"

# ---------- Step 2: Python deps ----------
Write-Info "Step 2/7 - Checking Python deps..."
Log "Check requirements.txt"
$reqFile = "$RootDir\mcp-gateway\requirements.txt"
if (-not (Test-Path $reqFile)) {
    Write-Err "requirements.txt not found at: $reqFile"
    Log "requirements.txt missing"
    Pause-IfInteractive
    exit 1
}
Log "requirements.txt: $reqFile"
$depsReady = Test-PythonDependencies $python
if ($Install -or -not $depsReady) {
    Write-Info "Installing Python dependencies (TUNA mirror first, PyPI fallback)..."
    $sources = @(
        @{
            Url = if ($env:PIP_INDEX_URL) { $env:PIP_INDEX_URL } else { "https://pypi.tuna.tsinghua.edu.cn/simple" }
            Host = if ($env:PIP_TRUSTED_HOST) { $env:PIP_TRUSTED_HOST } else { "pypi.tuna.tsinghua.edu.cn" }
        },
        @{
            Url = if ($env:PIP_FALLBACK_INDEX_URL) { $env:PIP_FALLBACK_INDEX_URL } else { "https://pypi.org/simple" }
            Host = if ($env:PIP_FALLBACK_TRUSTED_HOST) { $env:PIP_FALLBACK_TRUSTED_HOST } else { "pypi.org" }
        }
    )
    $depsReady = $false
    foreach ($source in $sources) {
        $pipArgs = @(
            "-m", "pip", "install", "--disable-pip-version-check",
            "--index-url", $source.Url, "--trusted-host", $source.Host,
            "-r", $reqFile
        )
        if ($Quiet) { $pipArgs += "-q" }
        Write-Info "Trying Python package source: $($source.Url)"
        & $python @pipArgs
        if ($LASTEXITCODE -eq 0 -and (Test-PythonDependencies $python)) {
            $depsReady = $true
            break
        }
        Write-Warn "Python package source failed; trying the next source."
    }
    if (-not $depsReady) {
        Write-Err "Python dependency installation failed."
        Pause-IfInteractive
        exit 1
    }
    Log "pip install done"
} else {
    Write-Ok "Python deps already installed; skipped pip install"
}
Write-Ok "Python deps ready"

# ---------- Step 3: Ollama ----------
Write-Info "Step 3/7 - Checking Ollama..."
Log "Check Ollama"
$ollamaHealthUrl = "$($env:OLLAMA_URL.TrimEnd('/'))/api/tags"
$ollamaExe = Find-OllamaExecutable
if (-not $ollamaExe) {
    Write-Info "Ollama not found; starting automatic installation."
    $ollamaExe = Install-OllamaRuntime
}
if (-not $ollamaExe) {
    Write-Err "Ollama is required. Automatic installation failed or was disabled."
    Log "Ollama not found"
    Pause-IfInteractive
    exit 1
} else {
    $running = $false
    try {
        $r = Invoke-WebRequest $ollamaHealthUrl -Method GET -TimeoutSec 2 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $running = $true }
    } catch { Log "Ollama not running: $_" }
    if (-not $running) {
        Write-Info "Starting Ollama..."
        Log "Start Ollama"
        try { Start-Process -FilePath $ollamaExe -ArgumentList "serve" -WindowStyle Hidden } catch { Log "Ollama start fail: $_" }
        for ($attempt = 0; $attempt -lt 15 -and -not $running; $attempt++) {
            Start-Sleep -Seconds 1
            try {
                $r = Invoke-WebRequest $ollamaHealthUrl -Method GET -TimeoutSec 2 -UseBasicParsing
                $running = $r.StatusCode -eq 200
            } catch { }
        }
    }
    if (-not $running) {
        Write-Err "Ollama did not become ready."
        Pause-IfInteractive
        exit 1
    }

    $ollamaModel = if ($env:OLLAMA_MODEL) { $env:OLLAMA_MODEL } else { "bge-m3" }
    $tags = Invoke-RestMethod $ollamaHealthUrl -Method GET -TimeoutSec 5
    $modelInstalled = $tags.models | Where-Object {
        $_.name -eq $ollamaModel -or $_.name -like "${ollamaModel}:*"
    }
    if (-not $modelInstalled) {
        Write-Info "Pulling Ollama model $ollamaModel (first run only)..."
        $modelReady = $false
        for ($attempt = 1; $attempt -le 3 -and -not $modelReady; $attempt++) {
            & $ollamaExe pull $ollamaModel
            $modelReady = $LASTEXITCODE -eq 0
            if (-not $modelReady -and $attempt -lt 3) {
                Write-Warn "Model pull attempt $attempt failed; retrying..."
                Start-Sleep -Seconds 2
            }
        }
        if (-not $modelReady) {
            Write-Err "Failed to pull Ollama model $ollamaModel."
            Pause-IfInteractive
            exit 1
        }
    }
    Write-Ok "Ollama ready"
    Log "Ollama ready"
}

# ---------- Step 4: Redis / Memurai ----------
Write-Info "Step 4/7 - Checking Redis / Memurai..."
if (-not $env:REDIS_URL) { $env:REDIS_URL = "redis://127.0.0.1:6379/0" }
Log "Check Redis PING: $env:REDIS_URL"
$redisOk = Test-RedisReady $python
if (-not $redisOk) {
    $redisHost = ""
    try { $redisHost = ([System.Uri]$env:REDIS_URL).Host } catch { Log "Invalid REDIS_URL: $_" }
    $localRedisHosts = @("localhost", "127.0.0.1", "::1")
    if ($redisHost -and $redisHost -notin $localRedisHosts) {
        Write-Err "Configured Redis did not answer PING: $env:REDIS_URL"
        Write-Info "A remote REDIS_URL is configured, so the launcher will not start local Memurai."
        Pause-IfInteractive
        exit 1
    }

    $memuraiExe = Find-MemuraiExecutable
    if (-not $memuraiExe) {
        Write-Info "Redis/Memurai not found; starting automatic Memurai installation."
        $memuraiExe = Install-MemuraiRuntime
        $redisOk = Wait-RedisReady $python 5
    }

    if (-not $redisOk) {
        $memuraiService = Get-Service -Name "Memurai" -ErrorAction SilentlyContinue
        if ($memuraiService) {
            $memuraiServiceStarted = $memuraiService.Status -eq "Running"
            if ($memuraiService.Status -ne "Running") {
                Write-Info "Starting the Memurai Windows service..."
                Log "Start Memurai service"
                try {
                    Start-Service -Name "Memurai" -ErrorAction Stop
                    $memuraiServiceStarted = $true
                } catch {
                    Write-Warn "Could not start the Memurai service; trying the executable directly."
                    Log "Memurai service start failed: $_"
                }
            } else {
                Write-Info "Memurai service is running; waiting for Redis PING..."
            }
            if ($memuraiServiceStarted) {
                $redisOk = Wait-RedisReady $python 15
            }
        }
    }

    if (-not $redisOk -and $memuraiExe) {
        Write-Info "Starting Memurai directly..."
        Log "Start Memurai executable: $memuraiExe"
        try {
            Start-Process -FilePath $memuraiExe -WindowStyle Hidden | Out-Null
            $redisOk = Wait-RedisReady $python 20
        } catch {
            Log "Memurai executable start failed: $_"
        }
    }

    if (-not $memuraiExe -and -not $redisOk) {
        Write-Warn "Memurai automatic installation failed or was disabled."
        Log "Memurai not found"
    }
}

if ($redisOk) {
    Write-Ok "Redis PING succeeded"
    Log "Redis PING succeeded"
} else {
    Write-Err "Redis/Memurai is required but did not answer PING. Check REDIS_URL and the Memurai service."
    Pause-IfInteractive
    exit 1
}

# ---------- Step 5: Chroma ----------
Write-Info "Step 5/7 - Checking Chroma..."
Log "Check Chroma port 8001"
$chromaHealthUrl = "http://$($env:CHROMA_HOST):$($env:CHROMA_PORT)/api/v2/heartbeat"
$chromaOk = $false
try {
    $r = Invoke-WebRequest $chromaHealthUrl -Method GET -TimeoutSec 2 -UseBasicParsing
    if ($r.StatusCode -eq 200) { $chromaOk = $true }
} catch { Log "Chroma not running: $_" }
if (-not $chromaOk) {
    Write-Info "Starting Chroma..."
    $pythonScripts = (& $python -c "import sysconfig; print(sysconfig.get_path('scripts'))").Trim()
    $chromaExe = Join-Path $pythonScripts "chroma.exe"
    if (-not (Test-Path -LiteralPath $chromaExe)) {
        $chromaCommand = Get-Command "chroma" -ErrorAction SilentlyContinue
        if ($chromaCommand) { $chromaExe = $chromaCommand.Source }
    }
    if (-not (Test-Path -LiteralPath $chromaExe)) {
        Write-Err "Chroma executable not found after dependency installation."
        Pause-IfInteractive
        exit 1
    }
    Write-Info "  -> Starting Chroma process (wait 10s)..."
    Log "Start Chroma process"
    try {
        $chromaArgs = "run --host $env:CHROMA_HOST --port $env:CHROMA_PORT --path `"$env:KBDATA_DIR\chroma`""
        $p = Start-Process -FilePath $chromaExe -ArgumentList $chromaArgs -WindowStyle Hidden -PassThru
        Start-Sleep -Seconds 8
        try { $r = Invoke-WebRequest $chromaHealthUrl -Method GET -TimeoutSec 3 -UseBasicParsing; if ($r.StatusCode -eq 200) { $chromaOk = $true; Write-Ok "Chroma started" } } catch { Log "Chroma still not reachable: $_" }
    } catch { Log "Chroma process start fail: $_" }
    if (-not $chromaOk) {
        Write-Err "Chroma auto-start failed. Manual command: chroma run --host $env:CHROMA_HOST --port $env:CHROMA_PORT"
        Log "Chroma start failed"
        Pause-IfInteractive
        exit 1
    }
} else {
    Write-Ok "Chroma port 8001 ready"
    Log "Chroma 8001 ok"
}

# ---------- Step 6: MinIO ----------
Write-Info "Step 6/7 - Checking MinIO..."
Log "Check MinIO port 9000"
$minioScheme = if ($env:MINIO_SECURE -eq "true") { "https" } else { "http" }
$minioHealthUrl = "${minioScheme}://$($env:MINIO_ENDPOINT)/minio/health/live"
$minioOk = $false
try {
    $r = Invoke-WebRequest $minioHealthUrl -Method GET -TimeoutSec 2 -UseBasicParsing
    if ($r.StatusCode -eq 200) { $minioOk = $true }
} catch { Log "MinIO not running: $_" }
if (-not $minioOk) {
    $minioExe = Get-Command "minio" -ErrorAction SilentlyContinue
    if (-not $minioExe) {
        $minioLocal = "$RootDir\minio.exe"
        if (-not (Test-Path $minioLocal)) {
            Write-Info "Downloading MinIO..."
            Log "Download MinIO"
            $downloaded = Invoke-DownloadWithFallback @(
                "https://dl.minio.org.cn/server/minio/release/windows-amd64/minio.exe",
                "https://dl.min.io/community/server/minio/release/windows-amd64/minio.exe"
            ) $minioLocal "MinIO"
            if ($downloaded) { Write-Ok "Downloaded MinIO" }
        }
        if (Test-Path $minioLocal) { $minioExe = Get-Command "$minioLocal" -ErrorAction SilentlyContinue }
    }
    if ($minioExe) {
        $dataDir = "$env:KBDATA_DIR\minio"
        try { New-Item -ItemType Directory -Path "$dataDir" -Force | Out-Null } catch {}
        Write-Info "Starting MinIO..."
        Log "Start MinIO: $($minioExe.Source)"
        try {
            if (-not $env:MINIO_ROOT_USER) { $env:MINIO_ROOT_USER = if ($env:MINIO_ACCESS_KEY) { $env:MINIO_ACCESS_KEY } else { "minioadmin" } }
            if (-not $env:MINIO_ROOT_PASSWORD) { $env:MINIO_ROOT_PASSWORD = if ($env:MINIO_SECRET_KEY) { $env:MINIO_SECRET_KEY } else { "minioadmin" } }
            $proc = Start-Process -FilePath $minioExe.Source -ArgumentList "server $dataDir --console-address :9001" -WindowStyle Hidden -PassThru -WorkingDirectory "$RootDir"
            for ($attempt = 0; $attempt -lt 10 -and -not $minioOk; $attempt++) {
                Start-Sleep -Seconds 1
                try {
                    $r = Invoke-WebRequest $minioHealthUrl -Method GET -TimeoutSec 2 -UseBasicParsing
                    $minioOk = $r.StatusCode -eq 200
                } catch { }
            }
            if ($minioOk) { Write-Ok "MinIO started" }
        } catch { Log "MinIO start fail: $_" }
    }
} else {
    Write-Ok "MinIO port 9000 ready"
    Log "MinIO 9000 ok"
}
if (-not $minioOk) {
    Write-Err "MinIO is required but did not become ready."
    Pause-IfInteractive
    exit 1
}

# ---------- Step 7: mcp-gateway ----------
Write-Info "Step 7/7 - Starting mcp-gateway..."
Log "Start mcp-gateway"
$env:PYTHONPATH = "$RootDir\mcp-gateway\src"
if (-not $env:REDIS_URL) { $env:REDIS_URL = "redis://127.0.0.1:6379/0" }
if (-not $env:CHROMA_HOST) { $env:CHROMA_HOST = "127.0.0.1" }
if (-not $env:CHROMA_PORT) { $env:CHROMA_PORT = "8001" }
if (-not $env:OLLAMA_URL) { $env:OLLAMA_URL = "http://127.0.0.1:11434" }
if (-not $env:OLLAMA_NUM_PARALLEL) { $env:OLLAMA_NUM_PARALLEL = "4" }
if (-not $env:MINIO_ENDPOINT) { $env:MINIO_ENDPOINT = "127.0.0.1:9000" }
if (-not $env:MINIO_ACCESS_KEY) { $env:MINIO_ACCESS_KEY = "minioadmin" }
if (-not $env:MINIO_SECRET_KEY) { $env:MINIO_SECRET_KEY = "minioadmin" }
if (-not $env:MINIO_BUCKET) { $env:MINIO_BUCKET = "kb-sources" }
if (-not $env:MINIO_SECURE) { $env:MINIO_SECURE = "false" }
if (-not $env:DEBUG) { $env:DEBUG = "true" }
if (-not $env:CORS_ORIGINS) { $env:CORS_ORIGINS = "*" }
if (-not $env:BIND_HOST) { $env:BIND_HOST = "0.0.0.0" }
# 配置文件路径（Docker 默认路径不适用于 Windows）
if (-not $env:ADMIN_ACCOUNTS_FILE) { $env:ADMIN_ACCOUNTS_FILE = "$env:KBDATA_DIR\config\admin_accounts.json" }
if (-not $env:API_KEY_FILE) { $env:API_KEY_FILE = "$env:KBDATA_DIR\config\api_keys.json" }
Log "PYTHONPATH=$env:PYTHONPATH"
Log "REDIS_URL=$env:REDIS_URL"
$gatewayProbeHost = $env:BIND_HOST
if ($gatewayProbeHost -in @("0.0.0.0", "*", "::", "[::]")) { $gatewayProbeHost = "127.0.0.1" }
$gatewayHealthUrl = "http://${gatewayProbeHost}:8000/health"
$gatewayStdOut = Join-Path $RootDir "mcp-gateway-dev.stdout.log"
$gatewayStdErr = Join-Path $RootDir "mcp-gateway-dev.stderr.log"

$gatewayProcess = $null
$gatewayWasRunning = Test-GatewayReady $gatewayHealthUrl
if (-not $gatewayWasRunning) {
    $gatewayPortOwner = Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty OwningProcess
    if ($gatewayPortOwner) {
        Write-Err "Port 8000 is occupied, but Gateway /health is not ready (listener PID $gatewayPortOwner)."
        Write-Info "Run .\start-dev.ps1 -Stop, inspect the existing process, then start again."
        Pause-IfInteractive
        exit 1
    }

    Remove-Item -LiteralPath $gatewayStdOut, $gatewayStdErr -Force -ErrorAction SilentlyContinue
    $gatewayArgs = @("-m", "uvicorn", "src.main:app", "--host", $env:BIND_HOST, "--port", "8000")
    if (-not $Background) { $gatewayArgs += "--reload" }
    Write-Info "  -> Gateway process started; waiting for /health (up to 90s)..."
    Log "Starting uvicorn; stdout=$gatewayStdOut; stderr=$gatewayStdErr"
    try {
        $gatewayProcess = Start-Process -FilePath $python -ArgumentList $gatewayArgs `
            -WorkingDirectory "$RootDir\mcp-gateway" -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $gatewayStdOut -RedirectStandardError $gatewayStdErr
    } catch {
        Log "uvicorn start failed: $_"
        Write-Err "Could not start Gateway: $_"
        Pause-IfInteractive
        exit 1
    }

    if (-not (Wait-GatewayReady $gatewayHealthUrl $gatewayProcess 90)) {
        Write-Err "Gateway did not become healthy. It is not being reported as ready."
        Log "Gateway health wait failed"
        if ($gatewayProcess) {
            try {
                $gatewayProcess.Refresh()
                if (-not $gatewayProcess.HasExited) { Stop-ProcessTree $gatewayProcess.Id }
            } catch { Log "Gateway cleanup failed: $_" }
        }
        foreach ($diagnosticLog in @($gatewayStdErr, $gatewayStdOut)) {
            if ((Test-Path -LiteralPath $diagnosticLog) -and (Get-Item -LiteralPath $diagnosticLog).Length -gt 0) {
                Write-Warn "Last lines from $diagnosticLog"
                Get-Content -LiteralPath $diagnosticLog -Tail 30 | ForEach-Object { Write-Host "  $_" }
            }
        }
        Pause-IfInteractive
        exit 1
    }
} else {
    Write-Ok "Gateway was already healthy; reusing the existing process"
}

Write-Info ""
Write-Info "============================================"
Write-Info "  All services are healthy!"
Write-Info "  API:           http://localhost:8000"
Write-Info "  Admin:         http://localhost:8000/admin"
Write-Info "  MinIO Console: http://localhost:9001"
Write-Info "  Health:        $gatewayHealthUrl"
if ($Background -or $gatewayWasRunning) {
    Write-Info "  Stop:          .\start-dev.ps1 -Stop"
} else {
    Write-Info "  Ctrl+C to stop mcp-gateway"
}
Write-Info "  Launcher log:  $logFile"
Write-Info "  Gateway logs:  $gatewayStdOut / $gatewayStdErr"
Write-Info "============================================"
Write-Info ""
Log "All services healthy"

if ($Background -or $gatewayWasRunning) { exit 0 }

try {
    $gatewayProcess.WaitForExit()
} finally {
    try {
        $gatewayProcess.Refresh()
        if (-not $gatewayProcess.HasExited) { Stop-ProcessTree $gatewayProcess.Id }
    } catch { Log "Gateway foreground cleanup failed: $_" }
}

if ($gatewayProcess.ExitCode -ne 0) {
    Write-Err "mcp-gateway stopped with exit code $($gatewayProcess.ExitCode). Check $gatewayStdErr"
    Pause-IfInteractive
    exit $gatewayProcess.ExitCode
}
