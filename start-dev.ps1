# start-dev.ps1 — knowledge-base-management Windows dev launcher
# No Docker/WSL2 needed, all native components
# Usage: right-click "Run with PowerShell", or .\start-dev.ps1 in terminal
# Optional: .\start-dev.ps1 -Profile minimum|recommended|high-performance

param(
    [switch]$Install,
    [switch]$Quiet,
    [switch]$Stop,
    [switch]$NoAutoInstall,
    [ValidateSet("auto", "minimum", "recommended", "high-performance")]
    [string]$Profile = "auto"
)

$ErrorActionPreference = "Continue"
$Global:ExitCode = 0

$logFile = "$PSScriptRoot\start-dev.log"
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
        $parts = $line.Split(@('='), 2)
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
    $profilePath = Join-Path $PSScriptRoot "deploy\profiles\$Name.env"
    if (-not (Test-Path -LiteralPath $profilePath)) {
        throw "Hardware profile not found: $profilePath"
    }
    foreach ($rawLine in [System.IO.File]::ReadAllLines($profilePath)) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) { continue }
        $parts = $line.Split(@('='), 2)
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

$localEnvFile = "$PSScriptRoot\.env.local"
$localEnvCreated = $false
if (-not (Test-Path -LiteralPath $localEnvFile)) {
    Copy-Item -LiteralPath "$PSScriptRoot\.env.example.local" -Destination $localEnvFile
    $localEnvCreated = $true
    Write-Info "Created .env.local from .env.example.local"
}
if ($Profile -ne "auto") {
    Set-HardwareProfile $Profile $localEnvFile
} elseif ($localEnvCreated) {
    Set-HardwareProfile "recommended" $localEnvFile
}
Import-DotEnv $localEnvFile
if (-not $env:KBDATA_DIR) {
    $env:KBDATA_DIR = "$PSScriptRoot\kbdata"
} elseif (-not [System.IO.Path]::IsPathRooted($env:KBDATA_DIR)) {
    $env:KBDATA_DIR = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot $env:KBDATA_DIR))
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
        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $candidatePid" -ErrorAction SilentlyContinue
        if ($processInfo.CommandLine -match "uvicorn.+src\.main:app|chroma.+run|minio.+server") {
            Stop-Process -Id $candidatePid -Force -ErrorAction SilentlyContinue
            $count++
            Write-Ok "  stopped PID $candidatePid"
            Log "Stopped project listener PID $candidatePid"
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
$reqFile = "$PSScriptRoot\mcp-gateway\requirements.txt"
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
        $r = Invoke-WebRequest "http://localhost:11434/api/tags" -Method GET -TimeoutSec 2 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $running = $true }
    } catch { Log "Ollama not running: $_" }
    if (-not $running) {
        Write-Info "Starting Ollama..."
        Log "Start Ollama"
        try { Start-Process -FilePath $ollamaExe -ArgumentList "serve" -WindowStyle Hidden } catch { Log "Ollama start fail: $_" }
        for ($attempt = 0; $attempt -lt 15 -and -not $running; $attempt++) {
            Start-Sleep -Seconds 1
            try {
                $r = Invoke-WebRequest "http://localhost:11434/api/tags" -Method GET -TimeoutSec 2 -UseBasicParsing
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
    $tags = Invoke-RestMethod "http://localhost:11434/api/tags" -Method GET -TimeoutSec 5
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
Log "Check port 6379"
$redisOk = $false
try { $check = netstat -an 2>$null | Select-String ":6379"; if ($check) { $redisOk = $true } } catch { Log "netstat fail: $_" }
if (-not $redisOk) {
    $memuraiExe = Find-MemuraiExecutable
    if (-not $memuraiExe) {
        Write-Info "Redis/Memurai not found; starting automatic Memurai installation."
        $memuraiExe = Install-MemuraiRuntime
    }
    if ($memuraiExe) {
        Write-Info "Starting Memurai..."
        Log "Start Memurai: $memuraiExe"
        try { Start-Process -FilePath $memuraiExe -WindowStyle Hidden; Start-Sleep -Seconds 2; Write-Ok "Memurai started" } catch { Log "Memurai start fail: $_" }
    } else {
        Write-Warn "Memurai automatic installation failed or was disabled."
        Log "Memurai not found"
    }
} else {
    Write-Ok "Redis port 6379 ready"
    Log "Redis 6379 ok"
}

$redisOk = $false
try { $check = netstat -an 2>$null | Select-String ":6379"; if ($check) { $redisOk = $true } } catch { }
if (-not $redisOk) {
    Write-Err "Redis/Memurai is required but not available. Install Memurai or use the Docker deployment."
    Pause-IfInteractive
    exit 1
}

# ---------- Step 5: Chroma ----------
Write-Info "Step 5/7 - Checking Chroma..."
Log "Check Chroma port 8001"
$chromaOk = $false
try {
    $r = Invoke-WebRequest "http://localhost:8001/api/v2/heartbeat" -Method GET -TimeoutSec 2 -UseBasicParsing
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
        $chromaArgs = "run --host localhost --port 8001 --path `"$env:KBDATA_DIR\chroma`""
        $p = Start-Process -FilePath $chromaExe -ArgumentList $chromaArgs -WindowStyle Hidden -PassThru
        Start-Sleep -Seconds 8
        try { $r = Invoke-WebRequest "http://localhost:8001/api/v2/heartbeat" -Method GET -TimeoutSec 3 -UseBasicParsing; if ($r.StatusCode -eq 200) { $chromaOk = $true; Write-Ok "Chroma started" } } catch { Log "Chroma still not reachable: $_" }
    } catch { Log "Chroma process start fail: $_" }
    if (-not $chromaOk) {
        Write-Err "Chroma auto-start failed. Manual command: chroma run --host localhost --port 8001"
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
$minioOk = $false
try {
    $r = Invoke-WebRequest "http://localhost:9000/minio/health/live" -Method GET -TimeoutSec 2 -UseBasicParsing
    if ($r.StatusCode -eq 200) { $minioOk = $true }
} catch { Log "MinIO not running: $_" }
if (-not $minioOk) {
    $minioExe = Get-Command "minio" -ErrorAction SilentlyContinue
    if (-not $minioExe) {
        $minioLocal = "$PSScriptRoot\minio.exe"
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
            $proc = Start-Process -FilePath $minioExe.Source -ArgumentList "server $dataDir --console-address :9001" -WindowStyle Hidden -PassThru -WorkingDirectory "$PSScriptRoot"
            for ($attempt = 0; $attempt -lt 10 -and -not $minioOk; $attempt++) {
                Start-Sleep -Seconds 1
                try {
                    $r = Invoke-WebRequest "http://localhost:9000/minio/health/live" -Method GET -TimeoutSec 2 -UseBasicParsing
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
$env:PYTHONPATH = "$PSScriptRoot\mcp-gateway\src"
if (-not $env:REDIS_URL) { $env:REDIS_URL = "redis://localhost:6379/0" }
if (-not $env:CHROMA_HOST) { $env:CHROMA_HOST = "localhost" }
if (-not $env:CHROMA_PORT) { $env:CHROMA_PORT = "8001" }
if (-not $env:OLLAMA_URL) { $env:OLLAMA_URL = "http://localhost:11434" }
if (-not $env:OLLAMA_NUM_PARALLEL) { $env:OLLAMA_NUM_PARALLEL = "4" }
if (-not $env:MINIO_ENDPOINT) { $env:MINIO_ENDPOINT = "localhost:9000" }
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
Write-Info ""
Write-Info "============================================"
Write-Info "  All services ready!"
Write-Info "  API:           http://localhost:8000"
Write-Info "  Admin:         http://localhost:8000/admin"
Write-Info "  MinIO Console: http://localhost:9001"
Write-Info "  Ctrl+C to stop mcp-gateway"
Write-Info "  Log: $logFile"
Write-Info "============================================"
Write-Info ""
Log "Starting uvicorn"
try {
    Set-Location "$PSScriptRoot\mcp-gateway"
    & $python -m uvicorn src.main:app --host $env:BIND_HOST --port 8000 --reload
} catch {
    Log "uvicorn start fail: $_"
    Write-Err "Start failed, check log: $logFile"
    Pause-IfInteractive
}
Pause-IfInteractive "mcp-gateway stopped, press Enter to exit"
