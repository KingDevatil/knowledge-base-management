<#
.SYNOPSIS
Docker deployment adapter for Windows.

.DESCRIPTION
Exposes the same interface as start.sh:
  .\start.ps1 [up|down|status|logs|init] [-Gpu auto|cpu|gpu]
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("up", "down", "status", "logs", "init", "help")]
    [string]$Command = "up",

    [ValidateSet("auto", "cpu", "gpu")]
    [string]$Gpu = "auto"
)

$ErrorActionPreference = "Stop"
$RootDir = $PSScriptRoot
$EnvFile = Join-Path $RootDir ".env"
$script:ComposeFiles = @("-f", "docker-compose.yml")

Set-Location $RootDir

function Write-Step([string]$Message) {
    Write-Host $Message -ForegroundColor Cyan
}

function New-RandomSecret {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    } finally {
        $rng.Dispose()
    }
    return -join ($bytes | ForEach-Object { $_.ToString("x2") })
}

function Get-DotEnvValue([string]$Key) {
    if (-not (Test-Path -LiteralPath $EnvFile)) {
        return ""
    }
    foreach ($line in [System.IO.File]::ReadAllLines($EnvFile)) {
        if ($line -match "^$([regex]::Escape($Key))=(.*)$") {
            return $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
    return ""
}

function Set-DotEnvValue([string]$Key, [string]$Value) {
    $lines = [System.Collections.Generic.List[string]]::new()
    if (Test-Path -LiteralPath $EnvFile) {
        $lines.AddRange([string[]][System.IO.File]::ReadAllLines($EnvFile))
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
    if (-not $updated) {
        $lines.Add("$prefix$Value")
    }
    $utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($EnvFile, $lines, $utf8WithoutBom)
}

function Initialize-Environment {
    $created = $false
    if (-not (Test-Path -LiteralPath $EnvFile)) {
        Copy-Item -LiteralPath (Join-Path $RootDir ".env.example") -Destination $EnvFile
        $created = $true
        Write-Step "[配置] 已从 .env.example 创建 .env"
    }

    $sessionSecret = Get-DotEnvValue "SESSION_SECRET"
    if ($sessionSecret.Length -lt 32 -or $sessionSecret -eq "change-me-to-a-random-long-string-at-least-32-chars") {
        Set-DotEnvValue "SESSION_SECRET" (New-RandomSecret)
        Write-Step "[配置] 已生成 SESSION_SECRET"
    }

    $minioPassword = Get-DotEnvValue "MINIO_ROOT_PASSWORD"
    if ([string]::IsNullOrWhiteSpace($minioPassword) -or $minioPassword -eq "change-me-strong-password") {
        $minioPassword = New-RandomSecret
        Set-DotEnvValue "MINIO_ROOT_PASSWORD" $minioPassword
        Set-DotEnvValue "MINIO_SECRET_KEY" $minioPassword
        Write-Step "[配置] 已生成 MinIO 密码"
    }

    if ($created) {
        Set-DotEnvValue "EXTERNAL_DOMAIN" ""
        Set-DotEnvValue "INTERNAL_DOMAIN" "localhost"
    }
}

function Assert-DockerReady {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "未找到 Docker。请先安装并启动 Docker Desktop。"
    }
    & docker compose version *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "未找到 Docker Compose v2（docker compose）。"
    }
    & docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Desktop 尚未启动，或当前用户无法连接 Docker daemon。"
    }
}

function Select-ComposeFiles {
    $gpuEnabled = $false
    switch ($Gpu) {
        "gpu" { $gpuEnabled = $true }
        "cpu" { $gpuEnabled = $false }
        "auto" {
            if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
                & nvidia-smi *> $null
                $gpuEnabled = ($LASTEXITCODE -eq 0)
            }
        }
    }
    if ($gpuEnabled) {
        $script:ComposeFiles += @("-f", "docker-compose.gpu.yml")
        Write-Step "[启动] NVIDIA GPU 模式"
    } else {
        Write-Step "[启动] CPU 模式"
    }
}

function Invoke-Compose([string[]]$Arguments) {
    & docker compose @script:ComposeFiles @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose 命令失败：$($Arguments -join ' ')"
    }
}

function Test-GatewayReady {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8000/health" -TimeoutSec 3
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Wait-Gateway {
    Write-Step "[等待] 首次启动会自动拉取 Embedding 模型，可能需要几分钟。"
    for ($attempt = 1; $attempt -le 300; $attempt++) {
        if (Test-GatewayReady) {
            Write-Host "[就绪] Gateway 健康检查通过" -ForegroundColor Green
            return
        }
        if (($attempt % 15) -eq 0) {
            Write-Step "[等待] 已等待 $($attempt * 2) 秒..."
        }
        Start-Sleep -Seconds 2
    }

    Write-Host "[错误] 10 分钟内未就绪。以下是关键容器状态和日志：" -ForegroundColor Red
    & docker compose @script:ComposeFiles ps
    & docker compose @script:ComposeFiles logs --tail=80 ollama-model-init mcp-gateway
    throw "Gateway 启动超时"
}

function Show-Usage {
    @"
用法: .\start.ps1 [up|down|status|logs|init] [-Gpu auto|cpu|gpu]

  up      初始化配置、构建并启动，等待模型和 Gateway 就绪（默认）
  down    停止 Docker 服务
  status  查看容器状态并检查 Gateway
  logs    跟踪所有容器日志
  init    仅创建/修复 .env，不启动服务
"@ | Write-Host
}

switch ($Command) {
    "init" {
        Initialize-Environment
        Write-Host "[完成] 配置位于 $EnvFile" -ForegroundColor Green
    }
    "up" {
        Initialize-Environment
        Assert-DockerReady
        Select-ComposeFiles
        Write-Step "[等待] Compose 将依次检查依赖、拉取模型并等待 Gateway 健康。"
        Invoke-Compose @("up", "-d", "--build")
        Wait-Gateway
        Write-Host ""
        Write-Host "部署完成：" -ForegroundColor Green
        Write-Host "  管理后台: http://localhost/admin"
        Write-Host "  MCP:      http://localhost/mcp"
        Write-Host "  局域网:   将 localhost 替换为本机 IP"
    }
    "down" {
        Assert-DockerReady
        Select-ComposeFiles
        Invoke-Compose @("down")
    }
    "status" {
        Assert-DockerReady
        Select-ComposeFiles
        Invoke-Compose @("ps")
        if (Test-GatewayReady) {
            Write-Host "[健康] Gateway 正常" -ForegroundColor Green
        } else {
            throw "Gateway 尚未就绪"
        }
    }
    "logs" {
        Assert-DockerReady
        Select-ComposeFiles
        Invoke-Compose @("logs", "-f")
    }
    "help" {
        Show-Usage
    }
}
