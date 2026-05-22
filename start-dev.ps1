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
function Write-Err  { Write-Host "[ERR]  $args" -Foreground Red }

# ---------- Stop mode: kill all services ----------
if ($Stop) {
    Write-Info "Stopping all services..."
    $targets = @(
        @{Name="mcp-gateway";   Process="python"}
        @{Name="Chroma";        Process="chroma"}
        @{Name="MinIO";         Process="minio"}
        @{Name="Memurai/Redis"; Process="memurai"}
        @{Name="Ollama";        Process="ollama"}
    )
    $count = 0
    foreach ($t in $targets) {
        # Memurai/Redis might be a Windows service — handle specially
        if ($t.Process -eq "memurai") {
            $svc = Get-Service -Name "Memurai" -ErrorAction SilentlyContinue
            if ($svc -and $svc.Status -eq "Running") {
                Stop-Service -Name "Memurai" -Force
                $count++
                Write-Ok "  $($t.Name) (Windows service) x 1"
                Log "Stopped $($t.Name) (Windows service)"
                continue
            }
        }
        $procs = Get-Process -Name $t.Process -ErrorAction SilentlyContinue
        if ($procs) {
            $procs | Stop-Process -Force -ErrorAction SilentlyContinue
            $count += $procs.Count
            Write-Ok "  $($t.Name) ($($t.Process).exe) x $($procs.Count)"
            Log "Stopped $($t.Name) ($($t.Process))"
        } else {
            Write-Info "  $($t.Name) - not running"
        }
    }
    if ($count -eq 0) {
        Write-Warn "No services were running."
    } else {
        Write-Ok "Stopped $count process(es)."
    }
    exit 0
}


# ---------- Step 1: Python ----------
Write-Info "Step 1/7 - Checking Python..."
Log "Check Python"
$python = $null
foreach ($c in @("python3", "python")) {
    try {
        $v = & $c --version 2>&1
        Log "Try $c : $v"
        if ($v -match "3\.\d+") { $python = $c; break }
    } catch { Log "$c not available" }
}
if (-not $python) {
    Write-Err "Python not found, install: https://www.python.org/downloads/"
    Log "Python not found"
    Read-Host "Press Enter to exit"
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
    Read-Host "Press Enter to exit"
    exit 1
}
Log "requirements.txt: $reqFile"
try {
    & $python -m pip install -r "$reqFile" -q 2>&1 | Out-Null
    Write-Ok "Python deps ready"
    Log "pip install done"
} catch {
    Write-Warn "pip install failed, retrying..."
    Log "pip first fail: $_"
    try { & $python -m pip install -r "$reqFile" } catch { Log "pip second fail: $_" }
}

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
    Write-Warn "Ollama not installed -> https://ollama.com/download/windows"
    Log "Ollama not found"
} else {
    $running = $false
    try {
        $r = Invoke-WebRequest "http://localhost:11434/api/tags" -Method GET -TimeoutSec 2 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $running = $true }
    } catch { Log "Ollama not running: $_" }
    if (-not $running) {
        Write-Info "Starting Ollama..."
        Log "Start Ollama"
        try { Start-Process -FilePath $ollamaCmd -ArgumentList "serve" -WindowStyle Hidden; Start-Sleep -Seconds 3 } catch { Log "Ollama start fail: $_" }
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

# ---------- Step 5: Chroma ----------
Write-Info "Step 5/7 - Checking Chroma..."
Log "Check Chroma port 8001"
$chromaOk = $false
try {
    $r = Invoke-WebRequest "http://localhost:8001/api/v2/heartbeat" -Method GET -TimeoutSec 2 -UseBasicParsing
    if ($r.StatusCode -eq 200) { $chromaOk = $true }
} catch { Log "Chroma not running: $_" }
if (-not $chromaOk) {
    Write-Info "Installing/starting Chroma..."
    Log "pip install chromadb"
    try { & $python -m pip install chromadb -q } catch { Log "chromadb install fail: $_" }
    Write-Info "  -> Starting Chroma process (wait 10s)..."
    Log "Start Chroma process"
    try {
        $p = Start-Process -FilePath "chroma" -ArgumentList "run --host localhost --port 8001 --path $PSScriptRoot\kbdata\chroma" -WindowStyle Hidden -PassThru
        Start-Sleep -Seconds 8
        try { $r = Invoke-WebRequest "http://localhost:8001/api/v2/heartbeat" -Method GET -TimeoutSec 3 -UseBasicParsing; if ($r.StatusCode -eq 200) { $chromaOk = $true; Write-Ok "Chroma started" } } catch { Log "Chroma still not reachable: $_" }
    } catch { Log "Chroma process start fail: $_" }
    if (-not $chromaOk) { Write-Warn "Chroma auto-start failed, manual: chroma run --host localhost --port 8001"; Log "Chroma start failed" }
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
            try { Invoke-WebRequest "https://dl.min.io/server/minio/release/windows-amd64/minio.exe" -OutFile "$minioLocal" -UseBasicParsing; Write-Ok "Downloaded" } catch { Log "MinIO download fail: $_" }
        }
        if (Test-Path $minioLocal) { $minioExe = Get-Command "$minioLocal" -ErrorAction SilentlyContinue }
    }
    if ($minioExe) {
        $dataDir = "$PSScriptRoot\kbdata\minio"
        try { New-Item -ItemType Directory -Path "$dataDir" -Force | Out-Null } catch {}
        Write-Info "Starting MinIO..."
        Log "Start MinIO: $($minioExe.Source)"
        try {
            $env:MINIO_ROOT_USER = "minioadmin"; $env:MINIO_ROOT_PASSWORD = "minioadmin"
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
$env:REDIS_URL = "redis://localhost:6379/0"
$env:CHROMA_HOST = "localhost"
$env:CHROMA_PORT = "8001"
$env:OLLAMA_URL = "http://localhost:11434"
$env:MINIO_ENDPOINT = "localhost:9000"
$env:MINIO_ACCESS_KEY = "minioadmin"
$env:MINIO_SECRET_KEY = "minioadmin"
$env:MINIO_BUCKET = "kb-sources"
$env:MINIO_SECURE = "false"
$env:DEBUG = "true"
$env:CORS_ORIGINS = "*"
# 配置文件路径（Docker 默认路径不适用于 Windows）
$env:ADMIN_ACCOUNTS_FILE = "$PSScriptRoot\kbdata\config\admin_accounts.json"
$env:API_KEY_FILE = "$PSScriptRoot\kbdata\config\api_keys.json"
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
    & $python -m uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload
} catch {
    Log "uvicorn start fail: $_"
    Write-Err "Start failed, check log: $logFile"
    Read-Host "Press Enter to exit"
}
Read-Host "mcp-gateway stopped, press Enter to exit"