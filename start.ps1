<#
.SYNOPSIS
Docker deployment adapter for Windows.

.DESCRIPTION
Exposes the same interface as start.sh:
  .\start.ps1 [up|down|status|logs|init|configure|cli-install|cli-uninstall|cli-status] [-Gpu auto|cpu|gpu] [-Profile auto|minimum|recommended|high-performance] [-Tunnel auto|off|cloudflare] [-Source auto|mainland|official]
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("up", "down", "status", "logs", "init", "configure", "cli-install", "cli-uninstall", "cli-status", "help")]
    [string]$Command = "up",

    [ValidateSet("auto", "cpu", "gpu")]
    [string]$Gpu = "auto",

    [ValidateSet("auto", "minimum", "recommended", "high-performance")]
    [string]$Profile = "auto",

    [ValidateSet("auto", "off", "cloudflare")]
    [string]$Tunnel = "auto",

    [ValidateSet("auto", "mainland", "official")]
    [string]$Source = "auto",

    [switch]$NonInteractive,

    [switch]$InstallCli
)

$ErrorActionPreference = "Stop"
$RootDir = $PSScriptRoot
$EnvFile = Join-Path $RootDir ".env"
$NetworkDetectionScript = Join-Path $RootDir "scripts\network-detection.ps1"
if (-not (Test-Path -LiteralPath $NetworkDetectionScript)) {
    throw "网络地址检测脚本不存在：$NetworkDetectionScript"
}
. $NetworkDetectionScript
$script:ComposeFiles = @("-f", "docker-compose.yml")
$script:ComposeOptions = @()
$script:GpuWasSpecified = $PSBoundParameters.ContainsKey("Gpu")
$script:TunnelWasSpecified = $PSBoundParameters.ContainsKey("Tunnel")
$script:SourceWasSpecified = $PSBoundParameters.ContainsKey("Source")
$script:HasDeploymentOverrides = (
    $Profile -ne "auto" -or
    $script:GpuWasSpecified -or
    $script:TunnelWasSpecified -or
    $script:SourceWasSpecified
)
$script:EffectiveGpu = "auto"
$script:EffectiveTunnel = "off"
$script:EffectiveSource = "mainland"

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

function Ensure-DotEnvValue([string]$Key, [string]$DefaultValue) {
    if ([string]::IsNullOrWhiteSpace((Get-DotEnvValue $Key))) {
        Set-DotEnvValue $Key $DefaultValue
    }
}

function Get-DotEnvValueOrDefault([string]$Key, [string]$DefaultValue) {
    $value = Get-DotEnvValue $Key
    if ([string]::IsNullOrWhiteSpace($value)) { return $DefaultValue }
    return $value
}

function Test-InteractiveSession {
    if ($NonInteractive -or -not [Environment]::UserInteractive) { return $false }
    try { return -not [Console]::IsInputRedirected } catch { return $true }
}

function Read-MenuChoice(
    [string]$Prompt,
    [string]$DefaultValue,
    [hashtable]$Choices
) {
    while ($true) {
        $answer = (Read-Host "$Prompt [$DefaultValue]").Trim().ToLowerInvariant()
        if ([string]::IsNullOrWhiteSpace($answer)) { return $DefaultValue }
        if ($Choices.ContainsKey($answer)) { return [string]$Choices[$answer] }
        Write-Host "输入无效，请重新选择。" -ForegroundColor Yellow
    }
}

function Read-ConfigValue([string]$Prompt, [string]$CurrentValue) {
    $answer = Read-Host "$Prompt [$CurrentValue]"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $CurrentValue }
    return $answer.Trim()
}

function Read-SecretValue([string]$Prompt, [string]$CurrentValue) {
    $secureValue = Read-Host "$Prompt（留空保持当前值）" -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureValue)
    try {
        $plainValue = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
    if ([string]::IsNullOrEmpty($plainValue)) { return $CurrentValue }
    return $plainValue
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
        $parts = $line -split "=", 2
        Set-DotEnvValue $parts[0].Trim() $parts[1].Trim()
    }
    Write-Step "[配置] 已应用硬件档位：$Name"
}

function Initialize-DeploymentMetadata {
    $accessMode = Get-DotEnvValue "DEPLOY_ACCESS_MODE"
    if ([string]::IsNullOrWhiteSpace($accessMode)) {
        $externalDomain = Get-DotEnvValue "EXTERNAL_DOMAIN"
        $internalIp = Get-DotEnvValue "INTERNAL_IP"
        if (-not [string]::IsNullOrWhiteSpace($externalDomain) -and $externalDomain -ne "kb.company.com") {
            $accessMode = "domain"
        } elseif ($internalIp -eq "127.0.0.1") {
            $accessMode = "local"
        } else {
            $accessMode = "lan"
        }
        Set-DotEnvValue "DEPLOY_ACCESS_MODE" $accessMode
    }

    $imageSource = Get-DotEnvValue "DEPLOY_IMAGE_SOURCE"
    if ([string]::IsNullOrWhiteSpace($imageSource)) {
        $imageSource = if ([string]::IsNullOrWhiteSpace((Get-DotEnvValue "MIRROR_PREFIX"))) { "official" } else { "mainland" }
        Set-DotEnvValue "DEPLOY_IMAGE_SOURCE" $imageSource
    }

    Ensure-DotEnvValue "DEPLOY_GPU_MODE" "auto"
    Ensure-DotEnvValue "DEPLOY_TUNNEL_MODE" "off"
    Ensure-DotEnvValue "DEPLOY_CONFIGURED" "true"
}

function Test-DeploymentConfigured {
    return (Get-DotEnvValueOrDefault "DEPLOY_CONFIGURED" "true").ToLowerInvariant() -eq "true"
}

function Apply-CommandConfigurationOverrides {
    if ($script:GpuWasSpecified) {
        Set-DotEnvValue "DEPLOY_GPU_MODE" $Gpu
    }
    if ($script:TunnelWasSpecified -and $Tunnel -ne "auto") {
        Set-DotEnvValue "DEPLOY_TUNNEL_MODE" $Tunnel
        if ($Tunnel -eq "cloudflare") {
            Set-DotEnvValue "DEPLOY_ACCESS_MODE" "cloudflare"
        }
    }
    if ($script:SourceWasSpecified -and $Source -ne "auto") {
        Set-DotEnvValue "DEPLOY_IMAGE_SOURCE" $Source
    }
}

function Set-HardwareConfiguration {
    Write-Host ""
    Write-Host "硬件与并发档位" -ForegroundColor Cyan
    Write-Host "  1) minimum          4 核 / 8 GB / 1–5 人"
    Write-Host "  2) recommended      8 核 / 16 GB / 10–20 人间歇使用"
    Write-Host "  3) high-performance 12 核+ / 32 GB+ / 持续并发"
    $currentProfile = Get-DotEnvValueOrDefault "HARDWARE_PROFILE" "recommended"
    $profileChoice = Read-MenuChoice "请选择档位" $currentProfile @{
        "1" = "minimum"; "minimum" = "minimum"
        "2" = "recommended"; "recommended" = "recommended"
        "3" = "high-performance"; "high-performance" = "high-performance"
    }
    Set-HardwareProfile $profileChoice

    Write-Host "  GPU: 1) 自动检测  2) 强制 CPU  3) NVIDIA GPU"
    $currentGpu = Get-DotEnvValueOrDefault "DEPLOY_GPU_MODE" "auto"
    $gpuChoice = Read-MenuChoice "请选择 GPU 模式" $currentGpu @{
        "1" = "auto"; "auto" = "auto"
        "2" = "cpu"; "cpu" = "cpu"
        "3" = "gpu"; "gpu" = "gpu"
    }
    Set-DotEnvValue "DEPLOY_GPU_MODE" $gpuChoice
}

function Set-ImageSourceConfiguration {
    Write-Host ""
    Write-Host "镜像与软件源" -ForegroundColor Cyan
    Write-Host "  1) 中国大陆镜像优先，失败后自动回退官方源"
    Write-Host "  2) 直接使用 Docker Hub / PyPI / Debian 官方源"
    $currentSource = Get-DotEnvValueOrDefault "DEPLOY_IMAGE_SOURCE" "mainland"
    $sourceChoice = Read-MenuChoice "请选择镜像源" $currentSource @{
        "1" = "mainland"; "mainland" = "mainland"
        "2" = "official"; "official" = "official"
    }
    Set-DotEnvValue "DEPLOY_IMAGE_SOURCE" $sourceChoice
}

function Read-RequiredDomain([string]$CurrentValue) {
    while ($true) {
        $domain = Read-ConfigValue "请输入访问域名" $CurrentValue
        if (-not [string]::IsNullOrWhiteSpace($domain) -and $domain -notmatch "\s") {
            return $domain
        }
        Write-Host "域名不能为空且不能包含空格。" -ForegroundColor Yellow
    }
}

function Read-InternalHostConfiguration([string]$CurrentValue) {
    $detectedHostName = Get-KnowbaseComputerName
    $detectedIPv4s = @(Get-KnowbaseLanIPv4Addresses)
    $detectedValue = ConvertTo-KnowbaseInternalHostValue ((@($detectedHostName) + $detectedIPv4s) -join ",")
    $suggestedValue = Get-KnowbaseSuggestedInternalHostValue $CurrentValue $detectedValue

    Write-Host "[检测] 计算机名：$(if ($detectedHostName) { $detectedHostName } else { '未检测到' })" -ForegroundColor Green
    Write-Host "[检测] 局域网 IPv4：$(if ($detectedIPv4s.Count -gt 0) { $detectedIPv4s -join ', ' } else { '未检测到，请确认网卡已连接' })" -ForegroundColor Green
    $configuredValue = Read-ConfigValue "内网访问名称（计算机名/IP；多个值用逗号分隔，回车使用检测值）" $suggestedValue
    $normalizedValue = ConvertTo-KnowbaseInternalHostValue $configuredValue
    if ([string]::IsNullOrWhiteSpace($normalizedValue)) { return "localhost" }
    return $normalizedValue
}

function Set-NetworkConfiguration {
    Write-Host ""
    Write-Host "访问方式" -ForegroundColor Cyan
    Write-Host "  1) 仅本机访问"
    Write-Host "  2) 局域网访问（推荐）"
    Write-Host "  3) 公网域名 / 路由器端口转发"
    Write-Host "  4) Cloudflare Tunnel（无需公网 IP）"
    $currentAccess = Get-DotEnvValueOrDefault "DEPLOY_ACCESS_MODE" "lan"
    $accessChoice = Read-MenuChoice "请选择访问方式" $currentAccess @{
        "1" = "local"; "local" = "local"
        "2" = "lan"; "lan" = "lan"
        "3" = "domain"; "domain" = "domain"
        "4" = "cloudflare"; "cloudflare" = "cloudflare"
    }

    switch ($accessChoice) {
        "local" {
            Set-DotEnvValue "EXTERNAL_DOMAIN" ""
            Set-DotEnvValue "INTERNAL_DOMAIN" "localhost"
            Set-DotEnvValue "EXTERNAL_IP" "127.0.0.1"
            Set-DotEnvValue "INTERNAL_IP" "127.0.0.1"
            Set-DotEnvValue "CORS_ORIGINS" "*"
            Set-DotEnvValue "DEPLOY_TUNNEL_MODE" "off"
        }
        "lan" {
            $internalDomain = Read-InternalHostConfiguration (Get-DotEnvValueOrDefault "INTERNAL_DOMAIN" "localhost")
            Set-DotEnvValue "EXTERNAL_DOMAIN" ""
            Set-DotEnvValue "INTERNAL_DOMAIN" $internalDomain
            Set-DotEnvValue "EXTERNAL_IP" "0.0.0.0"
            Set-DotEnvValue "INTERNAL_IP" "0.0.0.0"
            Set-DotEnvValue "CORS_ORIGINS" "*"
            Set-DotEnvValue "DEPLOY_TUNNEL_MODE" "off"
        }
        "domain" {
            $externalDomain = Read-RequiredDomain (Get-DotEnvValueOrDefault "EXTERNAL_DOMAIN" "kb.example.com")
            $internalDomain = Read-InternalHostConfiguration (Get-DotEnvValueOrDefault "INTERNAL_DOMAIN" "localhost")
            Set-DotEnvValue "EXTERNAL_DOMAIN" $externalDomain
            Set-DotEnvValue "INTERNAL_DOMAIN" $internalDomain
            Set-DotEnvValue "EXTERNAL_IP" "0.0.0.0"
            Set-DotEnvValue "INTERNAL_IP" "0.0.0.0"
            Set-DotEnvValue "CORS_ORIGINS" "https://$externalDomain"
            Set-DotEnvValue "DEPLOY_TUNNEL_MODE" "off"
            Write-Host "[提示] 请把证书放入 nginx/ssl/$externalDomain/。" -ForegroundColor Yellow
        }
        "cloudflare" {
            $externalDomain = Read-RequiredDomain (Get-DotEnvValueOrDefault "EXTERNAL_DOMAIN" "kb.example.com")
            $token = Read-SecretValue "Cloudflare Tunnel Token" (Get-DotEnvValue "CLOUDFLARE_TUNNEL_TOKEN")
            if ([string]::IsNullOrWhiteSpace($token)) {
                throw "Cloudflare Tunnel 模式必须提供 Token。"
            }
            Set-DotEnvValue "EXTERNAL_DOMAIN" $externalDomain
            Set-DotEnvValue "INTERNAL_DOMAIN" "localhost"
            Set-DotEnvValue "EXTERNAL_IP" "127.0.0.1"
            Set-DotEnvValue "INTERNAL_IP" "127.0.0.1"
            Set-DotEnvValue "CORS_ORIGINS" "https://$externalDomain"
            Set-DotEnvValue "CLOUDFLARE_TUNNEL_TOKEN" $token
            Set-DotEnvValue "DEPLOY_TUNNEL_MODE" "cloudflare"
        }
    }
    Set-DotEnvValue "DEPLOY_ACCESS_MODE" $accessChoice
}

function Set-StorageConfiguration {
    Write-Host ""
    Write-Host "数据与模型" -ForegroundColor Cyan
    $dataPath = Read-ConfigValue "宿主机数据目录（Windows 绝对路径建议使用正斜杠）" (Get-DotEnvValueOrDefault "HOST_KBDATA_DIR" "./kbdata")
    $model = Read-ConfigValue "Ollama Embedding 模型" (Get-DotEnvValueOrDefault "OLLAMA_MODEL" "bge-m3")
    Set-DotEnvValue "HOST_KBDATA_DIR" $dataPath
    Set-DotEnvValue "OLLAMA_MODEL" $model
}

function Set-AdminConfiguration {
    Write-Host ""
    Write-Host "初始管理员（仅账号库为空时生效）" -ForegroundColor Cyan
    $username = Read-ConfigValue "管理员用户名" (Get-DotEnvValueOrDefault "ADMIN_INITIAL_USERNAME" "admin")
    $password = Read-SecretValue "管理员初始密码" (Get-DotEnvValueOrDefault "ADMIN_INITIAL_PASSWORD" "123456")
    Set-DotEnvValue "ADMIN_INITIAL_USERNAME" $username
    Set-DotEnvValue "ADMIN_INITIAL_PASSWORD" $password
    if ($password -eq "123456") {
        Write-Host "[提示] 当前仍使用 Demo 默认密码 123456。局域网多人使用前建议修改。" -ForegroundColor Yellow
    }
}

function Show-ConfigurationSummary {
    $tokenConfigured = if ([string]::IsNullOrWhiteSpace((Get-DotEnvValue "CLOUDFLARE_TUNNEL_TOKEN"))) { "否" } else { "是（已隐藏）" }
    Write-Host ""
    Write-Host "当前部署配置" -ForegroundColor Green
    Write-Host "  硬件档位: $(Get-DotEnvValueOrDefault 'HARDWARE_PROFILE' 'recommended')"
    Write-Host "  GPU 模式:  $(Get-DotEnvValueOrDefault 'DEPLOY_GPU_MODE' 'auto')"
    Write-Host "  镜像源:    $(Get-DotEnvValueOrDefault 'DEPLOY_IMAGE_SOURCE' 'mainland')"
    Write-Host "  访问方式:  $(Get-DotEnvValueOrDefault 'DEPLOY_ACCESS_MODE' 'lan')"
    Write-Host "  内网名称/IP: $(Get-DotEnvValueOrDefault 'INTERNAL_DOMAIN' 'localhost')"
    Write-Host "  外部域名:  $(Get-DotEnvValueOrDefault 'EXTERNAL_DOMAIN' '未配置')"
    Write-Host "  Tunnel:    $(Get-DotEnvValueOrDefault 'DEPLOY_TUNNEL_MODE' 'off') / Token $tokenConfigured"
    Write-Host "  数据目录:  $(Get-DotEnvValueOrDefault 'HOST_KBDATA_DIR' './kbdata')"
    Write-Host "  模型:      $(Get-DotEnvValueOrDefault 'OLLAMA_MODEL' 'bge-m3')"
    Write-Host "  初始管理员: $(Get-DotEnvValueOrDefault 'ADMIN_INITIAL_USERNAME' 'admin')"
    Write-Host "  配置文件:  $EnvFile"
}

function Invoke-ConfigurationWizard([bool]$InitialSetup) {
    if (-not (Test-InteractiveSession)) {
        throw "配置向导需要交互式终端；自动化环境请使用 init/configure -NonInteractive 及 -Profile/-Gpu/-Source/-Tunnel 参数。"
    }

    Write-Host ""
    Write-Host "============================================" -ForegroundColor Cyan
    Write-Host "  Knowledge Base Management 部署配置向导"
    Write-Host "============================================" -ForegroundColor Cyan

    if ($InitialSetup) {
        Set-HardwareConfiguration
        Set-ImageSourceConfiguration
        Set-NetworkConfiguration
        Set-StorageConfiguration
        Set-AdminConfiguration
    } else {
        while ($true) {
            Show-ConfigurationSummary
            Write-Host ""
            Write-Host "重新配置：1) 硬件/GPU  2) 镜像源  3) 访问方式  4) 数据/模型  5) 初始管理员  6) 全部  0) 完成"
            $section = Read-MenuChoice "请选择要修改的部分" "0" @{
                "0" = "done"; "done" = "done"
                "1" = "hardware"; "hardware" = "hardware"
                "2" = "source"; "source" = "source"
                "3" = "network"; "network" = "network"
                "4" = "storage"; "storage" = "storage"
                "5" = "admin"; "admin" = "admin"
                "6" = "all"; "all" = "all"
            }
            switch ($section) {
                "done" { break }
                "hardware" { Set-HardwareConfiguration }
                "source" { Set-ImageSourceConfiguration }
                "network" { Set-NetworkConfiguration }
                "storage" { Set-StorageConfiguration }
                "admin" { Set-AdminConfiguration }
                "all" {
                    Set-HardwareConfiguration
                    Set-ImageSourceConfiguration
                    Set-NetworkConfiguration
                    Set-StorageConfiguration
                    Set-AdminConfiguration
                }
            }
            if ($section -eq "done") { break }
        }
    }

    Set-DotEnvValue "DEPLOY_CONFIGURED" "true"
    Show-ConfigurationSummary
    Write-Host "[完成] 配置已保存。服务已运行时，请重新执行 up 使修改生效。" -ForegroundColor Green
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

    Initialize-DeploymentMetadata

    if ($Profile -ne "auto") {
        Set-HardwareProfile $Profile
    } elseif ($created) {
        Set-HardwareProfile "recommended"
    }
    Apply-CommandConfigurationOverrides
    return $created
}

function Resolve-DeploymentOptions {
    $script:EffectiveGpu = if ($script:GpuWasSpecified) { $Gpu } else { Get-DotEnvValueOrDefault "DEPLOY_GPU_MODE" "auto" }
    $script:EffectiveTunnel = if ($script:TunnelWasSpecified -and $Tunnel -ne "auto") { $Tunnel } else { Get-DotEnvValueOrDefault "DEPLOY_TUNNEL_MODE" "off" }
    $script:EffectiveSource = if ($script:SourceWasSpecified -and $Source -ne "auto") { $Source } else { Get-DotEnvValueOrDefault "DEPLOY_IMAGE_SOURCE" "mainland" }

    if ($script:EffectiveGpu -notin @("auto", "cpu", "gpu")) {
        throw "DEPLOY_GPU_MODE 只能是 auto、cpu 或 gpu。"
    }
    if ($script:EffectiveTunnel -notin @("off", "cloudflare")) {
        throw "DEPLOY_TUNNEL_MODE 只能是 off 或 cloudflare。"
    }
    if ($script:EffectiveSource -notin @("mainland", "official")) {
        throw "DEPLOY_IMAGE_SOURCE 只能是 mainland 或 official。"
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
    switch ($script:EffectiveGpu) {
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

function Select-ImageSource {
    if ($script:EffectiveSource -ne "official") { return }
    $officialOverride = "docker-compose.official.yml"
    if ($script:ComposeFiles -notcontains $officialOverride) {
        $script:ComposeFiles += @("-f", $officialOverride)
    }
    Write-Step "[镜像] 直接使用官方 Docker/PyPI/Debian 源"
}

function Select-Tunnel {
    if ($script:EffectiveTunnel -eq "off") { return }
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
    & docker compose @script:ComposeFiles @script:ComposeOptions up -d --build --remove-orphans
    if ($LASTEXITCODE -eq 0) {
        return
    }

    if ($script:EffectiveSource -eq "official") {
        throw "官方镜像源启动失败，请检查上方 Docker 输出。"
    }

    Write-Host "[回退] 中国大陆镜像拉取或构建失败，改用 Docker Hub、PyPI、Debian 官方源重试。" -ForegroundColor Yellow
    $officialOverride = "docker-compose.official.yml"
    if ($script:ComposeFiles -notcontains $officialOverride) {
        $script:ComposeFiles += @("-f", $officialOverride)
    }
    Invoke-Compose @("up", "-d", "--build", "--remove-orphans")
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

function Show-DeploymentAccessUrls {
    $internalHosts = @((Get-DotEnvValueOrDefault "INTERNAL_DOMAIN" "localhost") -split "[,;\s]+" | Where-Object { $_ })
    Write-Host ""
    Write-Host "部署完成：" -ForegroundColor Green
    foreach ($internalHost in $internalHosts) {
        Write-Host "  管理后台: http://$internalHost/admin"
        Write-Host "  MCP:      http://$internalHost/mcp"
    }
    $externalDomain = Get-DotEnvValue "EXTERNAL_DOMAIN"
    if ($externalDomain) {
        Write-Host "  外部访问: https://$externalDomain/"
        Write-Host "  外部 MCP: https://$externalDomain/mcp"
    }
}

function Invoke-CliInstaller([ValidateSet("install", "uninstall", "status")][string]$Action, [switch]$Quiet) {
    $installer = Join-Path $RootDir "scripts\install-cli.ps1"
    if (-not (Test-Path -LiteralPath $installer)) {
        throw "CLI 安装脚本不存在：$installer"
    }
    & $installer -Action $Action -Quiet:$Quiet
}

function Test-CliRegistered {
    try {
        Invoke-CliInstaller "status" -Quiet
        return $true
    } catch {
        return $false
    }
}

function Install-CliIfRequested {
    if ($InstallCli) {
        Invoke-CliInstaller "install"
        return
    }
    if ($NonInteractive -or -not (Test-InteractiveSession) -or (Test-CliRegistered)) {
        return
    }
    $choice = Read-Host "是否注册全局 knowbase 命令，可在任意新终端使用？[Y/n]"
    if ([string]::IsNullOrWhiteSpace($choice) -or $choice.Trim().ToLowerInvariant() -in @("y", "yes")) {
        Invoke-CliInstaller "install"
    } else {
        Write-Step "[跳过] 稍后可运行 .\start.ps1 cli-install。"
    }
}

function Show-Usage {
    @"
用法: .\start.ps1 [up|down|status|logs|init|configure|cli-install|cli-uninstall|cli-status] [-Gpu auto|cpu|gpu] [-Profile auto|minimum|recommended|high-performance] [-Tunnel auto|off|cloudflare] [-Source auto|mainland|official] [-NonInteractive] [-InstallCli]

  up         首次部署时运行交互向导，然后构建并等待 Gateway 就绪（默认）
  configure  交互式查看并重新配置硬件、镜像、网络、存储或初始管理员
  init       非交互创建/修复 .env，不启动服务
  down       停止 Docker 服务
  status     查看容器状态并检查 Gateway
  logs       跟踪所有容器日志
  cli-install   注册用户级全局 knowbase 命令并加入 PATH
  cli-uninstall 删除全局 knowbase 命令及 PATH 项
  cli-status    检查全局 knowbase 命令状态

  首次 up 无参数时显示向导；显式参数或 -NonInteractive 保持自动化兼容。
  选择结果会写入 .env，后续 up/down/status/logs 自动复用。
  示例: .\start.ps1 up -Tunnel cloudflare -InstallCli / .\start.ps1 cli-install / knowbase gateway restart
"@ | Write-Host
}

switch ($Command) {
    "cli-install" {
        Invoke-CliInstaller "install"
    }
    "cli-uninstall" {
        Invoke-CliInstaller "uninstall"
    }
    "cli-status" {
        Invoke-CliInstaller "status"
    }
    "init" {
        $null = Initialize-Environment
        Set-DotEnvValue "DEPLOY_CONFIGURED" "true"
        Resolve-DeploymentOptions
        Show-ConfigurationSummary
        Write-Host "[完成] 配置位于 $EnvFile" -ForegroundColor Green
    }
    "configure" {
        $created = Initialize-Environment
        $needsFullSetup = $created -or (-not (Test-DeploymentConfigured))
        if ($NonInteractive) {
            Set-DotEnvValue "DEPLOY_CONFIGURED" "true"
            Show-ConfigurationSummary
            Write-Host "[完成] 已按命令参数更新配置。" -ForegroundColor Green
        } else {
            Invoke-ConfigurationWizard $needsFullSetup
        }
    }
    "up" {
        $created = Initialize-Environment
        $needsFullSetup = $created -or (-not (Test-DeploymentConfigured))
        if ($needsFullSetup -and -not $script:HasDeploymentOverrides -and (Test-InteractiveSession)) {
            Invoke-ConfigurationWizard $true
        } elseif ($needsFullSetup) {
            Set-DotEnvValue "DEPLOY_CONFIGURED" "true"
        }
        Resolve-DeploymentOptions
        Assert-DockerReady
        Select-ComposeFiles
        Select-ImageSource
        Select-Tunnel
        Write-Step "[等待] Compose 将依次检查依赖、拉取模型并等待 Gateway 健康。"
        Invoke-ComposeUpWithFallback
        Wait-Gateway
        Show-DeploymentAccessUrls
        if ($script:EffectiveTunnel -eq "cloudflare") { Write-Host "  穿透:     Cloudflare Tunnel 已启动" }
        Install-CliIfRequested
    }
    "down" {
        Assert-DockerReady
        Resolve-DeploymentOptions
        Select-ComposeFiles
        Select-ImageSource
        Select-Tunnel
        Invoke-Compose @("down", "--remove-orphans")
    }
    "status" {
        Assert-DockerReady
        Resolve-DeploymentOptions
        Select-ComposeFiles
        Select-ImageSource
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
        Resolve-DeploymentOptions
        Select-ComposeFiles
        Select-ImageSource
        Select-Tunnel
        Invoke-Compose @("logs", "-f")
    }
    "help" {
        Show-Usage
    }
}
