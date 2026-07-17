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
$AccessModesScript = Join-Path $RootDir "scripts\access-modes.ps1"
foreach ($requiredScript in @($NetworkDetectionScript, $AccessModesScript)) {
    if (-not (Test-Path -LiteralPath $requiredScript)) {
        throw "部署辅助脚本不存在：$requiredScript"
    }
}
. $NetworkDetectionScript
. $AccessModesScript
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
    $accessModes = Get-DotEnvValue "DEPLOY_ACCESS_MODES"
    $legacyAccessMode = Get-DotEnvValue "DEPLOY_ACCESS_MODE"
    if ([string]::IsNullOrWhiteSpace($legacyAccessMode)) {
        $externalDomain = Get-DotEnvValue "EXTERNAL_DOMAIN"
        $internalIp = Get-DotEnvValue "INTERNAL_IP"
        if (-not [string]::IsNullOrWhiteSpace($externalDomain) -and $externalDomain -ne "kb.company.com") {
            $legacyAccessMode = "domain"
        } elseif ($internalIp -eq "127.0.0.1") {
            $legacyAccessMode = "local"
        } else {
            $legacyAccessMode = "lan"
        }
    }

    $accessModes = ConvertTo-KnowbaseAccessModes $accessModes $legacyAccessMode
    if ((Get-DotEnvValueOrDefault "DEPLOY_TUNNEL_MODE" "off") -eq "cloudflare" -and
        -not (Test-KnowbaseAccessMode $accessModes "cloudflare")) {
        $accessModes = Update-KnowbaseAccessModesForTunnel $accessModes "cloudflare"
    }
    Set-AccessModesMetadata $accessModes

    $externalDomain = Get-DotEnvValue "EXTERNAL_DOMAIN"
    if ((Test-KnowbaseAccessMode $accessModes "domain") -and
        [string]::IsNullOrWhiteSpace((Get-DotEnvValue "PUBLIC_DOMAIN")) -and
        -not [string]::IsNullOrWhiteSpace($externalDomain)) {
        Set-DotEnvValue "PUBLIC_DOMAIN" $externalDomain
    }
    if ((Test-KnowbaseAccessMode $accessModes "cloudflare") -and
        [string]::IsNullOrWhiteSpace((Get-DotEnvValue "CLOUDFLARE_PUBLIC_HOSTNAME")) -and
        -not [string]::IsNullOrWhiteSpace($externalDomain)) {
        Set-DotEnvValue "CLOUDFLARE_PUBLIC_HOSTNAME" $externalDomain
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

function Set-AccessModesMetadata([string]$Modes) {
    $normalizedModes = ConvertTo-KnowbaseAccessModes $Modes
    Set-DotEnvValue "DEPLOY_ACCESS_MODES" $normalizedModes
    Set-DotEnvValue "DEPLOY_ACCESS_MODE" (Get-KnowbaseLegacyAccessMode $normalizedModes)
    $tunnelMode = if (Test-KnowbaseAccessMode $normalizedModes "cloudflare") { "cloudflare" } else { "off" }
    Set-DotEnvValue "DEPLOY_TUNNEL_MODE" $tunnelMode
}

function Apply-CommandConfigurationOverrides {
    if ($script:GpuWasSpecified) {
        Set-DotEnvValue "DEPLOY_GPU_MODE" $Gpu
    }
    if ($script:TunnelWasSpecified -and $Tunnel -ne "auto") {
        $accessModes = Get-DotEnvValueOrDefault "DEPLOY_ACCESS_MODES" "lan"
        Set-AccessModesMetadata (Update-KnowbaseAccessModesForTunnel $accessModes $Tunnel)
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

function Read-RequiredDomain([string]$CurrentValue, [string]$Prompt = "请输入访问域名") {
    while ($true) {
        $domain = Read-ConfigValue $Prompt $CurrentValue
        if (-not [string]::IsNullOrWhiteSpace($domain) -and $domain -notmatch "\s") {
            return $domain
        }
        Write-Host "域名不能为空且不能包含空格。" -ForegroundColor Yellow
    }
}

function Read-AccessModeSelection([string]$CurrentModes) {
    $currentModes = ConvertTo-KnowbaseAccessModes $CurrentModes
    while ($true) {
        Write-Host ""
        Write-Host "访问方式（可多选）" -ForegroundColor Cyan
        foreach ($entry in @(
            @{ Number = "1"; Mode = "local"; Label = "仅本机" },
            @{ Number = "2"; Mode = "lan"; Label = "局域网" },
            @{ Number = "3"; Mode = "domain"; Label = "公网" },
            @{ Number = "4"; Mode = "cloudflare"; Label = "Cloudflare Tunnel" }
        )) {
            $mark = if (Test-KnowbaseAccessMode $currentModes $entry.Mode) { "x" } else { " " }
            Write-Host "  [$mark] $($entry.Number)) $($entry.Label)"
        }

        $defaultChoices = Get-KnowbaseAccessModeChoices $currentModes
        $answer = Read-Host "请输入要启用的编号，多个用逗号分隔 [$defaultChoices]"
        if ([string]::IsNullOrWhiteSpace($answer)) { return $currentModes }

        $selectedModes = @()
        $invalid = @()
        foreach ($rawChoice in @($answer -split "[,;\s]+")) {
            $choice = $rawChoice.Trim().ToLowerInvariant()
            switch ($choice) {
                { $_ -in @("1", "local", "localhost") } { $mode = "local"; break }
                { $_ -in @("2", "lan", "internal") } { $mode = "lan"; break }
                { $_ -in @("3", "domain", "public", "external") } { $mode = "domain"; break }
                { $_ -in @("4", "cloudflare", "tunnel") } { $mode = "cloudflare"; break }
                default { $mode = ""; if ($choice) { $invalid += $choice } }
            }
            if ($mode -and $selectedModes -notcontains $mode) { $selectedModes += $mode }
        }
        if ($invalid.Count -gt 0) {
            Write-Host "输入包含无效选项：$($invalid -join ', ')。" -ForegroundColor Yellow
            continue
        }
        if ($selectedModes.Count -eq 0) {
            Write-Host "至少选择一种访问方式。" -ForegroundColor Yellow
            continue
        }
        return ConvertTo-KnowbaseAccessModes ($selectedModes -join ",")
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
    $currentModes = Get-DotEnvValueOrDefault "DEPLOY_ACCESS_MODES" "lan"
    $accessModes = Read-AccessModeSelection $currentModes
    Write-Host ""
    Write-Host "具体配置" -ForegroundColor Cyan

    if (Test-KnowbaseAccessMode $accessModes "local") {
        Write-Host "[本机] 无需额外设置，将保留 localhost 访问。" -ForegroundColor Green
    }

    if (Test-KnowbaseAccessMode $accessModes "lan") {
        Write-Host ""
        Write-Host "局域网访问设置" -ForegroundColor Cyan
        $internalDomain = Read-InternalHostConfiguration (Get-DotEnvValueOrDefault "INTERNAL_DOMAIN" "localhost")
    } else {
        $internalDomain = "localhost"
    }

    $publicDomain = Get-DotEnvValue "PUBLIC_DOMAIN"
    $activePublicDomain = ""
    if (Test-KnowbaseAccessMode $accessModes "domain") {
        Write-Host ""
        Write-Host "公网访问设置" -ForegroundColor Cyan
        if ([string]::IsNullOrWhiteSpace($publicDomain)) {
            $publicDomain = if (Test-KnowbaseAccessMode $currentModes "domain") {
                Get-DotEnvValueOrDefault "EXTERNAL_DOMAIN" "kb.example.com"
            } else { "kb.example.com" }
        }
        $publicDomain = Read-RequiredDomain $publicDomain "公网访问域名"
        $activePublicDomain = $publicDomain
        Set-DotEnvValue "PUBLIC_DOMAIN" $publicDomain
        Write-Host "[提示] 请把证书放入 nginx/ssl/$publicDomain/。" -ForegroundColor Yellow
    }

    $cloudflareHostname = Get-DotEnvValue "CLOUDFLARE_PUBLIC_HOSTNAME"
    if (Test-KnowbaseAccessMode $accessModes "cloudflare") {
        Write-Host ""
        Write-Host "Cloudflare Tunnel 设置" -ForegroundColor Cyan
        if ([string]::IsNullOrWhiteSpace($cloudflareHostname)) {
            $cloudflareHostname = if ((Test-KnowbaseAccessMode $currentModes "cloudflare") -and
                -not (Test-KnowbaseAccessMode $currentModes "domain")) {
                Get-DotEnvValueOrDefault "EXTERNAL_DOMAIN" "kb-tunnel.example.com"
            } elseif ($activePublicDomain) { "tunnel.$activePublicDomain" } else { "kb-tunnel.example.com" }
        }
        if ($activePublicDomain -and $cloudflareHostname.Equals($activePublicDomain, [System.StringComparison]::OrdinalIgnoreCase)) {
            $cloudflareHostname = "tunnel.$activePublicDomain"
        }
        while ($true) {
            $cloudflareHostname = Read-RequiredDomain $cloudflareHostname "Cloudflare Public Hostname"
            if (-not $activePublicDomain -or -not $cloudflareHostname.Equals($activePublicDomain, [System.StringComparison]::OrdinalIgnoreCase)) { break }
            Write-Host "公网直连域名和 Tunnel Hostname 应使用不同名称，例如 tunnel.$activePublicDomain。" -ForegroundColor Yellow
            $cloudflareHostname = "tunnel.$activePublicDomain"
        }
        $token = Read-SecretValue "Cloudflare Tunnel Token" (Get-DotEnvValue "CLOUDFLARE_TUNNEL_TOKEN")
        if ([string]::IsNullOrWhiteSpace($token)) { throw "Cloudflare Tunnel 必须提供 Token。" }
        Set-DotEnvValue "CLOUDFLARE_PUBLIC_HOSTNAME" $cloudflareHostname
        Set-DotEnvValue "CLOUDFLARE_TUNNEL_TOKEN" $token
    }

    $runtimeExternalDomain = if (Test-KnowbaseAccessMode $accessModes "domain") {
        $activePublicDomain
    } elseif (Test-KnowbaseAccessMode $accessModes "cloudflare") {
        $cloudflareHostname
    } else { "" }
    $internalBind = if ((Test-KnowbaseAccessMode $accessModes "lan") -or
        (Test-KnowbaseAccessMode $accessModes "domain")) { "0.0.0.0" } else { "127.0.0.1" }
    $externalBind = if (Test-KnowbaseAccessMode $accessModes "domain") { "0.0.0.0" } else { "127.0.0.1" }
    $corsOrigins = @()
    if ((Test-KnowbaseAccessMode $accessModes "domain") -and $activePublicDomain) { $corsOrigins += "https://$activePublicDomain" }
    if ((Test-KnowbaseAccessMode $accessModes "cloudflare") -and $cloudflareHostname) { $corsOrigins += "https://$cloudflareHostname" }
    $corsValue = if ((Test-KnowbaseAccessMode $accessModes "lan") -or $corsOrigins.Count -eq 0) { "*" } else { $corsOrigins -join "," }

    Set-DotEnvValue "INTERNAL_DOMAIN" $internalDomain
    Set-DotEnvValue "EXTERNAL_DOMAIN" $runtimeExternalDomain
    Set-DotEnvValue "INTERNAL_IP" $internalBind
    Set-DotEnvValue "EXTERNAL_IP" $externalBind
    Set-DotEnvValue "CORS_ORIGINS" $corsValue
    Set-AccessModesMetadata $accessModes
    if (Test-KnowbaseAccessMode $accessModes "cloudflare") {
        Write-Host "[提示] Cloudflare Public Hostname 上游应配置为 http://nginx:80。" -ForegroundColor Yellow
    }
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
    $accessModes = Get-DotEnvValueOrDefault "DEPLOY_ACCESS_MODES" "lan"
    Write-Host ""
    Write-Host "当前部署配置" -ForegroundColor Green
    Write-Host "  硬件档位: $(Get-DotEnvValueOrDefault 'HARDWARE_PROFILE' 'recommended')"
    Write-Host "  GPU 模式:  $(Get-DotEnvValueOrDefault 'DEPLOY_GPU_MODE' 'auto')"
    Write-Host "  镜像源:    $(Get-DotEnvValueOrDefault 'DEPLOY_IMAGE_SOURCE' 'mainland')"
    Write-Host "  访问方式:  $(Get-KnowbaseAccessModeLabels $accessModes)"
    if (Test-KnowbaseAccessMode $accessModes "lan") { Write-Host "  局域网名称/IP: $(Get-DotEnvValueOrDefault 'INTERNAL_DOMAIN' 'localhost')" }
    if (Test-KnowbaseAccessMode $accessModes "domain") { Write-Host "  公网域名:  $(Get-DotEnvValueOrDefault 'PUBLIC_DOMAIN' '未配置')" }
    if (Test-KnowbaseAccessMode $accessModes "cloudflare") {
        Write-Host "  Tunnel:    $(Get-DotEnvValueOrDefault 'CLOUDFLARE_PUBLIC_HOSTNAME' '未配置') / Token $tokenConfigured"
    }
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
    $hostname = Get-DotEnvValueOrDefault "CLOUDFLARE_PUBLIC_HOSTNAME" "已在 Cloudflare 控制台配置的 Hostname"
    Write-Step "[穿透] Cloudflare Tunnel 已启用：$hostname；上游应配置为 http://nginx:80"
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
    $accessModes = Get-DotEnvValueOrDefault "DEPLOY_ACCESS_MODES" "lan"
    $internalHosts = @()
    if (Test-KnowbaseAccessMode $accessModes "local") { $internalHosts += "localhost" }
    if (Test-KnowbaseAccessMode $accessModes "lan") {
        $internalHosts += @((Get-DotEnvValueOrDefault "INTERNAL_DOMAIN" "localhost") -split "[,;\s]+" | Where-Object { $_ })
    }
    $internalHosts = @($internalHosts | Select-Object -Unique)
    Write-Host ""
    Write-Host "部署完成：" -ForegroundColor Green
    foreach ($internalHost in $internalHosts) {
        Write-Host "  管理后台: http://$internalHost/admin"
        Write-Host "  MCP:      http://$internalHost/mcp"
    }
    if (Test-KnowbaseAccessMode $accessModes "domain") {
        $publicDomain = Get-DotEnvValueOrDefault "PUBLIC_DOMAIN" (Get-DotEnvValue "EXTERNAL_DOMAIN")
        Write-Host "  公网访问: https://$publicDomain/"
        Write-Host "  公网 MCP: https://$publicDomain/mcp"
    }
    if (Test-KnowbaseAccessMode $accessModes "cloudflare") {
        $tunnelHostname = Get-DotEnvValueOrDefault "CLOUDFLARE_PUBLIC_HOSTNAME" (Get-DotEnvValue "EXTERNAL_DOMAIN")
        Write-Host "  Tunnel:   https://$tunnelHostname/"
        Write-Host "  Tunnel MCP: https://$tunnelHostname/mcp"
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
