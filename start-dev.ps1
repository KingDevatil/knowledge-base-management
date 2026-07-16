# start-dev.ps1 — knowledge-base-management Windows dev launcher
# No Docker/WSL2 needed, all native components
# Usage: right-click "Run with PowerShell", or .\start-dev.ps1 in terminal

param(
    [switch]$Install,
    [switch]$Quiet,
    [switch]$Stop
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

function Test-PythonDependencies([string]$PythonCommand) {
    & $PythonCommand -c "import fastapi, uvicorn, chromadb, redis, minio, mcp, markdown, bleach" *> $null
    return $LASTEXITCODE -eq 0
}

$localEnvFile = "$PSScriptRoot\.env.local"
if (-not (Test-Path -LiteralPath $localEnvFile)) {
    Copy-Item -LiteralPath "$PSScriptRoot\.env.example.local" -Destination $localEnvFile
    Write-Info "Created .env.local from .env.example.local"
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
$python = $null
foreach ($c in @("python3", "python", "py")) {
    try {
        $v = & $c --version 2>&1
        Log "Try $c : $v"
        if ($v -match "3\.\d+") { $python = $c; break }
    } catch { Log "$c not available" }
}
if (-not $python) {
    Write-Err "Python not found, install: https://www.python.org/downloads/"
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
    Write-Info "Installing Python dependencies (first run or -Install requested)..."
    $pipArgs = @("-m", "pip", "install", "-r", $reqFile)
    if ($Quiet) { $pipArgs += "-q" }
    & $python @pipArgs
    if ($LASTEXITCODE -ne 0 -or -not (Test-PythonDependencies $python)) {
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
$ollamaPaths = @(
    "$env:ProgramFiles\Ollama\ollama.exe",
    "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
)
$ollamaCmd = Get-Command "ollama" -ErrorAction SilentlyContinue
if (-not $ollamaCmd) {
    foreach ($p in $ollamaPaths) { if (Test-Path $p) { $ollamaCmd = $p; break } }
}
if (-not $ollamaCmd) {
    Write-Err "Ollama not installed -> https://ollama.com/download/windows"
    Log "Ollama not found"
    Pause-IfInteractive
    exit 1
} else {
    $ollamaExe = if ($ollamaCmd -is [string]) { $ollamaCmd } else { $ollamaCmd.Source }
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
        & $ollamaExe pull $ollamaModel
        if ($LASTEXITCODE -ne 0) {
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
    $memuraiPaths = @(
        "$env:ProgramFiles\Memurai\memurai.exe",
        "${env:ProgramFiles(x86)}\Memurai\memurai.exe",
        "$env:LOCALAPPDATA\Memurai\memurai.exe"
    )
    $memuraiExe = $null
    foreach ($p in $memuraiPaths) { if (Test-Path $p) { $memuraiExe = $p; break } }
    if (-not $memuraiExe) { $memuraiExe = Get-Command "memurai" -ErrorAction SilentlyContinue }
    if ($memuraiExe) {
        Write-Info "Starting Memurai..."
        Log "Start Memurai: $memuraiExe"
        try { Start-Process -FilePath $memuraiExe -WindowStyle Hidden; Start-Sleep -Seconds 2; Write-Ok "Memurai started" } catch { Log "Memurai start fail: $_" }
    } else {
        Write-Warn "Memurai not installed -> https://www.memurai.com/download"
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
            try { Invoke-WebRequest "https://dl.minio.org.cn/server/minio/release/windows-amd64/minio.exe" -OutFile "$minioLocal" -UseBasicParsing; Write-Ok "Downloaded" } catch { Log "MinIO download fail: $_" }
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
            Start-Sleep -Seconds 3; Write-Ok "MinIO started"
        } catch { Log "MinIO start fail: $_" }
    }
} else {
    Write-Ok "MinIO port 9000 ready"
    Log "MinIO 9000 ok"
}

# ---------- Step 7: mcp-gateway ----------
Write-Info "Step 7/7 - Starting mcp-gateway..."
Log "Start mcp-gateway"
$env:PYTHONPATH = "$PSScriptRoot\mcp-gateway\src"
if (-not $env:REDIS_URL) { $env:REDIS_URL = "redis://localhost:6379/0" }
if (-not $env:CHROMA_HOST) { $env:CHROMA_HOST = "localhost" }
if (-not $env:CHROMA_PORT) { $env:CHROMA_PORT = "8001" }
if (-not $env:OLLAMA_URL) { $env:OLLAMA_URL = "http://localhost:11434" }
if (-not $env:OLLAMA_NUM_PARALLEL) { $env:OLLAMA_NUM_PARALLEL = "8" }
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
