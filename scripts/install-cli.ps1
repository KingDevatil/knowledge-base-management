[CmdletBinding()]
param(
    [ValidateSet("install", "uninstall", "status")]
    [string]$Action = "install",

    [ValidateSet("User", "Machine", "Process")]
    [string]$Scope = "User",

    [string]$BinDir = "",

    [switch]$Quiet
)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($BinDir)) {
    if (-not $env:LOCALAPPDATA) { throw "LOCALAPPDATA 未定义，无法确定用户级 CLI 目录。" }
    $BinDir = Join-Path $env:LOCALAPPDATA "KnowledgeBaseManagement\bin"
}
$BinDir = [System.IO.Path]::GetFullPath($BinDir)
$ShimPath = Join-Path $BinDir "knowbase.cmd"
$HomeFile = Join-Path $BinDir "knowbase-home.txt"
$ShimTemplate = Join-Path $PSScriptRoot "knowbase.cmd"

function Write-CliMessage([string]$Message, [ConsoleColor]$Color = [ConsoleColor]::Gray) {
    if (-not $Quiet) { Write-Host $Message -ForegroundColor $Color }
}

function Get-PathEntries([string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) { return @() }
    return @($Value.Split([System.IO.Path]::PathSeparator) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
}

function Test-SamePath([string]$Left, [string]$Right) {
    try {
        $leftPath = [System.IO.Path]::GetFullPath($Left).TrimEnd('\', '/')
        $rightPath = [System.IO.Path]::GetFullPath($Right).TrimEnd('\', '/')
        return $leftPath.Equals($rightPath, [System.StringComparison]::OrdinalIgnoreCase)
    } catch {
        return $Left.TrimEnd('\', '/').Equals($Right.TrimEnd('\', '/'), [System.StringComparison]::OrdinalIgnoreCase)
    }
}

function Add-ToPath {
    $current = [Environment]::GetEnvironmentVariable("Path", $Scope)
    $entries = @(Get-PathEntries $current)
    if ($entries | Where-Object { Test-SamePath $_ $BinDir }) { return }
    $newPath = (@($entries) + $BinDir) -join [System.IO.Path]::PathSeparator
    [Environment]::SetEnvironmentVariable("Path", $newPath, $Scope)
}

function Remove-FromPath {
    $current = [Environment]::GetEnvironmentVariable("Path", $Scope)
    $entries = @(Get-PathEntries $current | Where-Object { -not (Test-SamePath $_ $BinDir) })
    [Environment]::SetEnvironmentVariable("Path", ($entries -join [System.IO.Path]::PathSeparator), $Scope)
}

function Test-CliInstalled {
    if (-not (Test-Path -LiteralPath $ShimPath) -or -not (Test-Path -LiteralPath $HomeFile)) { return $false }
    $registeredRoot = [System.IO.File]::ReadAllText($HomeFile).Trim()
    if (-not (Test-SamePath $registeredRoot $RootDir)) { return $false }
    $pathValue = [Environment]::GetEnvironmentVariable("Path", $Scope)
    return [bool](Get-PathEntries $pathValue | Where-Object { Test-SamePath $_ $BinDir })
}

function Start-DeferredShimCleanup {
    $cleanupPath = Join-Path ([System.IO.Path]::GetTempPath()) "knowbase-cli-cleanup-$PID.ps1"
    $escapedShim = $ShimPath.Replace("'", "''")
    $escapedBin = $BinDir.Replace("'", "''")
    $escapedCleanup = $cleanupPath.Replace("'", "''")
    $cleanupContent = @"
Start-Sleep -Milliseconds 1200
Remove-Item -LiteralPath '$escapedShim' -Force -ErrorAction SilentlyContinue
if ((Test-Path -LiteralPath '$escapedBin') -and -not (Get-ChildItem -LiteralPath '$escapedBin' -Force | Select-Object -First 1)) {
    Remove-Item -LiteralPath '$escapedBin' -Force -ErrorAction SilentlyContinue
}
Remove-Item -LiteralPath '$escapedCleanup' -Force -ErrorAction SilentlyContinue
"@
    $utf8WithBom = New-Object System.Text.UTF8Encoding($true)
    [System.IO.File]::WriteAllText($cleanupPath, $cleanupContent, $utf8WithBom)
    $powerShellExecutable = (Get-Process -Id $PID).Path
    if ([string]::IsNullOrWhiteSpace($powerShellExecutable)) { $powerShellExecutable = "powershell.exe" }
    Start-Process -FilePath $powerShellExecutable `
        -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$cleanupPath`"" `
        -WindowStyle Hidden | Out-Null
}

switch ($Action) {
    "install" {
        if (-not (Test-Path -LiteralPath $ShimTemplate)) { throw "CLI 模板不存在：$ShimTemplate" }
        New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
        Copy-Item -LiteralPath $ShimTemplate -Destination $ShimPath -Force
        $utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($HomeFile, $RootDir, $utf8WithoutBom)
        Add-ToPath
        Write-CliMessage "[完成] knowbase 已安装到 $BinDir" Green
        Write-CliMessage "[提示] 请重新打开终端，然后运行：knowbase health" Cyan
    }
    "uninstall" {
        $runningFromShim = $env:KNOWBASE_CLI_SHIM -and (Test-SamePath $env:KNOWBASE_CLI_SHIM $ShimPath)
        Remove-Item -LiteralPath $HomeFile -Force -ErrorAction SilentlyContinue
        if ($runningFromShim) {
            Start-DeferredShimCleanup
        } else {
            Remove-Item -LiteralPath $ShimPath -Force -ErrorAction SilentlyContinue
        }
        Remove-FromPath
        if (-not $runningFromShim -and (Test-Path -LiteralPath $BinDir) -and -not (Get-ChildItem -LiteralPath $BinDir -Force | Select-Object -First 1)) {
            Remove-Item -LiteralPath $BinDir -Force
        }
        Write-CliMessage "[完成] knowbase 全局命令已卸载。" Green
    }
    "status" {
        if (Test-CliInstalled) {
            Write-CliMessage "[已安装] knowbase -> $RootDir" Green
        } else {
            Write-CliMessage "[未安装] 运行 .\start.ps1 cli-install 注册全局命令。" Yellow
            throw "knowbase CLI 未正确安装"
        }
    }
}
