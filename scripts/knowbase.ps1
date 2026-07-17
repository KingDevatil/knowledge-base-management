$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $PSScriptRoot
$DeployScript = Join-Path $RootDir "start.ps1"
$NativeScript = Join-Path $RootDir "start-dev.ps1"
$InstallerScript = Join-Path $PSScriptRoot "install-cli.ps1"
$DefaultHealthUrl = if ($env:KNOWBASE_HEALTH_URL) { $env:KNOWBASE_HEALTH_URL } else { "http://127.0.0.1:8000/health" }
$PowerShellExecutable = (Get-Process -Id $PID).Path
if ([string]::IsNullOrWhiteSpace($PowerShellExecutable)) { $PowerShellExecutable = "powershell.exe" }

function Show-KnowbaseUsage {
    @"
Knowbase CLI

用法:
  knowbase up|start [部署参数]          启动完整 Docker 服务
  knowbase down|stop                   停止完整 Docker 服务
  knowbase restart [部署参数]           重启完整 Docker 服务
  knowbase status                      查看容器和 Gateway 健康状态
  knowbase logs                        跟踪完整服务日志
  knowbase configure|config [参数]      打开或更新部署配置
  knowbase init [参数]                  非交互初始化部署配置
  knowbase health [--url URL] [--json] 查询 Gateway 健康状态
  knowbase gateway start|stop|restart  单独管理已部署的 Gateway 容器
  knowbase gateway status|logs|health  查看 Gateway 状态、日志或健康
  knowbase native start|stop|restart   管理 Windows 原生开发服务
  knowbase native status|logs          查看原生服务状态或日志
  knowbase cli install|uninstall|status 管理全局命令
  knowbase doctor                      检查目录、Docker、配置和健康状态
  knowbase home                        输出当前绑定的项目目录
  knowbase version                     输出 CLI 版本

示例:
  knowbase gateway restart
  knowbase health
  knowbase configure
  knowbase up -Profile recommended -Gpu auto
"@ | Write-Host
}

function Invoke-Deployment([string]$Action, [string[]]$ForwardArgs = @()) {
    & $PowerShellExecutable -NoProfile -ExecutionPolicy Bypass -File $DeployScript $Action @ForwardArgs | Out-Host
    return $LASTEXITCODE
}

function Invoke-NativeScript([string[]]$NativeArgs) {
    & $PowerShellExecutable -NoProfile -ExecutionPolicy Bypass -File $NativeScript @NativeArgs | Out-Host
    return $LASTEXITCODE
}

function Test-HealthEndpoint([string]$Url = $DefaultHealthUrl) {
    try {
        $response = Invoke-RestMethod -Uri $Url -Method Get -TimeoutSec 5
        return $response.status -eq "ok"
    } catch {
        return $false
    }
}

function Wait-GatewayHealth([string]$Url = $DefaultHealthUrl, [int]$TimeoutSeconds = 90) {
    Write-Host "[等待] Gateway 正在启动，将持续检查 $Url（最多 $TimeoutSeconds 秒）..." -ForegroundColor Cyan
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $attempt = 0
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-HealthEndpoint $Url) {
            Write-Host "[就绪] Gateway 健康检查通过。" -ForegroundColor Green
            return $true
        }
        $attempt++
        if (($attempt % 5) -eq 0) {
            Write-Host "[等待] Gateway 尚未就绪，已等待 $($attempt * 2) 秒..." -ForegroundColor Cyan
        }
        Start-Sleep -Seconds 2
    }
    Write-Host "[错误] Gateway 在 $TimeoutSeconds 秒内未通过健康检查。" -ForegroundColor Red
    return $false
}

function Invoke-Health([string[]]$HealthArgs = @()) {
    $url = $DefaultHealthUrl
    $asJson = $false
    for ($index = 0; $index -lt $HealthArgs.Count; $index++) {
        switch ($HealthArgs[$index]) {
            "--json" { $asJson = $true }
            "--url" {
                if ($index + 1 -ge $HealthArgs.Count) {
                    Write-Host "[错误] --url 缺少地址。" -ForegroundColor Red
                    return 2
                }
                $index++
                $url = $HealthArgs[$index]
            }
            default {
                if ($HealthArgs[$index] -like "--url=*") {
                    $url = $HealthArgs[$index].Substring(6)
                } else {
                    Write-Host "[错误] health 不支持参数：$($HealthArgs[$index])" -ForegroundColor Red
                    return 2
                }
            }
        }
    }

    try {
        $response = Invoke-RestMethod -Uri $url -Method Get -TimeoutSec 10
    } catch {
        Write-Host "[异常] Gateway 健康检查失败：$($_.Exception.Message)" -ForegroundColor Red
        return 1
    }

    if ($asJson) {
        Write-Host ($response | ConvertTo-Json -Depth 8 -Compress)
    } else {
        Write-Host "[健康] Gateway: $($response.status) ($url)" -ForegroundColor Green
        if ($response.services) {
            foreach ($property in $response.services.PSObject.Properties) {
                Write-Host "  $($property.Name): $($property.Value)"
            }
        }
        if ($response.embedding_providers) {
            foreach ($provider in $response.embedding_providers) {
                $circuit = if ($provider.circuit_open) { "open" } else { "closed" }
                Write-Host "  embedding: $($provider.name) / circuit=$circuit / failures=$($provider.failures)"
            }
        }
    }
    if ($response.status -eq "ok") { return 0 }
    return 1
}

function Assert-DockerCommand {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Write-Host "[错误] 未找到 Docker。" -ForegroundColor Red
        return $false
    }
    & docker compose version *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[错误] 未找到 Docker Compose v2。" -ForegroundColor Red
        return $false
    }
    return $true
}

function Invoke-Gateway([string[]]$GatewayArgs) {
    if ($GatewayArgs.Count -eq 0) {
        Write-Host "用法: knowbase gateway start|stop|restart|status|logs|health"
        return 2
    }
    $action = $GatewayArgs[0].ToLowerInvariant()
    $extra = if ($GatewayArgs.Count -gt 1) { @($GatewayArgs[1..($GatewayArgs.Count - 1)]) } else { @() }
    if ($action -eq "health") { return Invoke-Health $extra }
    if ($action -notin @("start", "stop", "restart", "status", "logs")) {
        Write-Host "[错误] 未知 Gateway 操作：$action" -ForegroundColor Red
        return 2
    }
    if (-not (Assert-DockerCommand)) { return 1 }

    Push-Location $RootDir
    try {
        switch ($action) {
            "start" { & docker compose -f docker-compose.yml start mcp-gateway | Out-Host }
            "stop" { & docker compose -f docker-compose.yml stop mcp-gateway | Out-Host }
            "restart" { & docker compose -f docker-compose.yml restart mcp-gateway | Out-Host }
            "status" { & docker compose -f docker-compose.yml ps mcp-gateway | Out-Host }
            "logs" {
                $logArgs = if ($extra.Count -gt 0) { $extra } else { @("-f") }
                & docker compose -f docker-compose.yml logs @logArgs mcp-gateway | Out-Host
            }
        }
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($code -ne 0) {
        if ($action -eq "start") {
            Write-Host "[提示] Gateway 容器尚未创建时，请先运行 knowbase up。" -ForegroundColor Yellow
        }
        return $code
    }
    if ($action -in @("start", "restart")) {
        if (-not (Wait-GatewayHealth)) { return 1 }
    } elseif ($action -eq "status") {
        return Invoke-Health
    }
    return 0
}

function Invoke-Native([string[]]$NativeArgs) {
    if ($NativeArgs.Count -eq 0) {
        Write-Host "用法: knowbase native start|stop|restart|status|logs"
        return 2
    }
    $action = $NativeArgs[0].ToLowerInvariant()
    $extra = if ($NativeArgs.Count -gt 1) { @($NativeArgs[1..($NativeArgs.Count - 1)]) } else { @() }
    switch ($action) {
        "start" { return Invoke-NativeScript (@("-Background") + $extra) }
        "stop" { return Invoke-NativeScript @("-Stop", "-Quiet") }
        "restart" {
            $stopCode = Invoke-NativeScript @("-Stop", "-Quiet")
            if ($stopCode -ne 0) { return $stopCode }
            return Invoke-NativeScript (@("-Background") + $extra)
        }
        "status" { return Invoke-Health $extra }
        "logs" {
            $logs = @(
                (Join-Path $RootDir "mcp-gateway-dev.stderr.log"),
                (Join-Path $RootDir "mcp-gateway-dev.stdout.log"),
                (Join-Path $RootDir "start-dev.log")
            ) | Where-Object { Test-Path -LiteralPath $_ }
            if ($logs.Count -eq 0) {
                Write-Host "[提示] 尚未生成原生服务日志。" -ForegroundColor Yellow
                return 1
            }
            Get-Content -LiteralPath $logs -Tail 80 -Wait | Out-Host
            return 0
        }
        default {
            Write-Host "[错误] 未知 native 操作：$action" -ForegroundColor Red
            return 2
        }
    }
}

function Invoke-CliManagement([string[]]$CliArgs) {
    $action = if ($CliArgs.Count -gt 0) { $CliArgs[0].ToLowerInvariant() } else { "status" }
    if ($action -notin @("install", "uninstall", "status")) {
        Write-Host "用法: knowbase cli install|uninstall|status"
        return 2
    }
    $extra = if ($CliArgs.Count -gt 1) { @($CliArgs[1..($CliArgs.Count - 1)]) } else { @() }
    & $PowerShellExecutable -NoProfile -ExecutionPolicy Bypass -File $InstallerScript -Action $action @extra | Out-Host
    return $LASTEXITCODE
}

function Invoke-Doctor {
    $failed = $false
    Write-Host "[目录] $RootDir"
    if (Test-Path -LiteralPath (Join-Path $RootDir ".env")) {
        Write-Host "[配置] .env 已存在" -ForegroundColor Green
    } else {
        Write-Host "[配置] .env 尚未生成；运行 knowbase configure" -ForegroundColor Yellow
        $failed = $true
    }
    if (Assert-DockerCommand) {
        Write-Host "[Docker] Compose 可用" -ForegroundColor Green
    } else {
        $failed = $true
    }
    if ((Invoke-Health) -ne 0) { $failed = $true }
    if ($failed) { return 1 }
    return 0
}

$tokens = @($args | ForEach-Object { [string]$_ })
$command = if ($tokens.Count -gt 0) { $tokens[0].ToLowerInvariant() } else { "help" }
$remaining = if ($tokens.Count -gt 1) { @($tokens[1..($tokens.Count - 1)]) } else { @() }

try {
    $exitCode = switch ($command) {
        { $_ -in @("up", "start") } { Invoke-Deployment "up" $remaining; break }
        { $_ -in @("down", "stop") } { Invoke-Deployment "down" $remaining; break }
        "restart" {
            $downCode = Invoke-Deployment "down"
            if ($downCode -ne 0) { $downCode } else { Invoke-Deployment "up" $remaining }
            break
        }
        "status" { Invoke-Deployment "status" $remaining; break }
        "logs" { Invoke-Deployment "logs" $remaining; break }
        { $_ -in @("configure", "config") } { Invoke-Deployment "configure" $remaining; break }
        "init" { Invoke-Deployment "init" $remaining; break }
        "health" { Invoke-Health $remaining; break }
        "gateway" { Invoke-Gateway $remaining; break }
        "native" { Invoke-Native $remaining; break }
        "cli" { Invoke-CliManagement $remaining; break }
        "doctor" { Invoke-Doctor; break }
        "home" { Write-Host $RootDir; 0; break }
        "version" { Write-Host "knowbase CLI 1.0"; 0; break }
        { $_ -in @("help", "-h", "--help") } { Show-KnowbaseUsage; 0; break }
        default {
            Write-Host "[错误] 未知命令：$command" -ForegroundColor Red
            Show-KnowbaseUsage
            2
        }
    }
} catch {
    Write-Host "[异常] $($_.Exception.Message)" -ForegroundColor Red
    $exitCode = 1
}

exit ([int]$exitCode)
