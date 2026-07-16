<#
.SYNOPSIS
Docker deployment adapter for Windows.

.DESCRIPTION
Exposes the same interface as start.sh:
  .\start.ps1 [up|down|status|logs|init] [-Gpu auto|cpu|gpu] [-Profile auto|minimum|recommended|high-performance] [-Tunnel off|cloudflare]
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("up", "down", "status", "logs", "init", "help")]
    [string]$Command = "up",

    [ValidateSet("auto", "cpu", "gpu")]
    [string]$Gpu = "auto",

    [ValidateSet("auto", "minimum", "recommended", "high-performance")]
    [string]$Profile = "auto",

    [ValidateSet("off", "cloudflare")]
    [string]$Tunnel = "off"
)

$ErrorActionPreference = "Stop"
$RootDir = $PSScriptRoot
$EnvFile = Join-Path $RootDir ".env"
$script:ComposeFiles = @("-f", "docker-compose.yml")
$script:ComposeOptions = @()

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

function Set-HardwareProfile([string]$Name) {
    if ([string]::IsNullOrWhiteSpace($Name) -or $Name -eq "auto") {
        return
    }
    $profilePath = Join-Path $RootDir "deploy\profiles\$Name.env"
    if (-not (Test-Path -LiteralPath $profilePath)) {
        throw "硬件配置档位不存在：$profilePath"
    }
    foreach ($rawLine in [System.IO.File]::ReadAllLines($profilePath)) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) {
            continue
        }
        $parts = $line.Split(@('='), 2)
        Set-DotEnvValue $parts[0].Trim() $parts[1].Trim()
    }
    Write-Step "[配置] 已应用硬件档位：$Name"
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

    if ($Profile -ne "auto") {
        Set-HardwareProfile $Profile
    } elseif ($created) {
        Set-HardwareProfile "recommended"
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

function Select-Tunnel {
    if ($Tunnel -eq "off") { return }
    $token = Get-DotEnvValue "CLOUDFLARE_TUNNEL_TOKEN"
    if ([string]::IsNullOrWhiteSpace($token)) {
        throw "启用 Cloudflare Tunnel 前请在 .env 设置 CLOUDFLARE_TUNNEL_TOKEN。"
    }
    $script:ComposeOptions += @("--profile", "tunnel")
    Write-Step "[穿透] Cloudflare Tunnel 已启用；Public Hostname 上游应配置为 http://nginx:80"
}

function Invoke-Compose([string[]]$Arguments) {
    & docker compose @script:ComposeFiles @script:ComposeOptions @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose 命令失败：$($Arguments -join ' ')"
    }
}

function Invoke-ComposeUpWithFallback {
    & docker compose @script:ComposeFiles @script:ComposeOptions up -d --build
    if ($LASTEXITCODE -eq 0) {
        return
    }

    Write-Host "[回退] 中国大陆镜像拉取或构建失败，改用 Docker Hub、PyPI、Debian 官方源重试。" -ForegroundColor Yellow
    $officialOverride = "docker-compose.official.yml"
    if ($script:ComposeFiles -notcontains $officialOverride) {
        $script:ComposeFiles += @("-f", $officialOverride)
    }
    Invoke-Compose @("up", "-d", "--build")
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
    & docker compose @script:ComposeFiles @script:ComposeOptions ps
    & docker compose @script:ComposeFiles @script:ComposeOptions logs --tail=80 ollama-model-init mcp-gateway
    throw "Gateway 启动超时"
}

function Show-Usage {
    @"
用法: .\start.ps1 [up|down|status|logs|init] [-Gpu auto|cpu|gpu] [-Profile auto|minimum|recommended|high-performance] [-Tunnel off|cloudflare]

  up      初始化配置、构建并启动，等待模型和 Gateway 就绪（默认）
  down    停止 Docker 服务
  status  查看容器状态并检查 Gateway
  logs    跟踪所有容器日志
  init    仅创建/修复 .env，不启动服务

  -Profile 显式覆盖硬件档位；auto 只在首次创建 .env 时应用 recommended
  -Tunnel cloudflare 读取 .env 中的 Token 并启动可选内网穿透容器
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
        Select-Tunnel
        Write-Step "[等待] Compose 将依次检查依赖、拉取模型并等待 Gateway 健康。"
        Invoke-ComposeUpWithFallback
        Wait-Gateway
        Write-Host ""
        Write-Host "部署完成：" -ForegroundColor Green
        Write-Host "  管理后台: http://localhost/admin"
        Write-Host "  MCP:      http://localhost/mcp"
        Write-Host "  局域网:   将 localhost 替换为本机 IP"
        if ($Tunnel -eq "cloudflare") { Write-Host "  穿透:     Cloudflare Tunnel 已启动" }
    }
    "down" {
        Assert-DockerReady
        Select-ComposeFiles
        Select-Tunnel
        Invoke-Compose @("down", "--remove-orphans")
    }
    "status" {
        Assert-DockerReady
        Select-ComposeFiles
        Select-Tunnel
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
        Select-Tunnel
        Invoke-Compose @("logs", "-f")
    }
    "help" {
        Show-Usage
    }
}
