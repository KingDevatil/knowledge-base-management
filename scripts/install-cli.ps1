[CmdletBinding()]
param(
    [ValidateSet("install", "uninstall", "status")]
    [string]$Action = "install",

    [ValidateSet("User", "Machine", "Process")]
    [string]$Scope = "User",

    [string]$BinDir = "",

    [string]$ConfigDir = "",

    [switch]$Quiet
)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $PSScriptRoot
$UsingDefaultBin = [string]::IsNullOrWhiteSpace($BinDir)
if (-not $env:LOCALAPPDATA) { throw "LOCALAPPDATA 未定义，无法确定用户级 CLI 目录。" }
$ProductDir = Join-Path $env:LOCALAPPDATA "KnowledgeBaseManagement"
$PreferredBinDir = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps"
$LegacyBinDir = Join-Path $ProductDir "bin"
$LegacyShimPath = Join-Path $LegacyBinDir "knowbase.cmd"
$LegacyHomeFile = Join-Path $LegacyBinDir "knowbase-home.txt"

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

if ($UsingDefaultBin) {
    $BinDir = if (Test-Path -LiteralPath $PreferredBinDir -PathType Container) { $PreferredBinDir } else { $LegacyBinDir }
}
$BinDir = [System.IO.Path]::GetFullPath($BinDir)
if ([string]::IsNullOrWhiteSpace($ConfigDir)) {
    $ConfigDir = if ($UsingDefaultBin) { $ProductDir } else { $BinDir }
}
$ConfigDir = [System.IO.Path]::GetFullPath($ConfigDir)
$ShimPath = Join-Path $BinDir "knowbase.cmd"
$HomeFile = Join-Path $ConfigDir "knowbase-home.txt"
$PathMarkerFile = Join-Path $ConfigDir "path-added.txt"
$ShimTemplate = Join-Path $PSScriptRoot "knowbase.cmd"
$BinWasAlreadyOnCurrentPath = [bool](Get-PathEntries $env:Path | Where-Object { Test-SamePath $_ $BinDir })

function Add-PathEntry([string]$TargetDir) {
    $current = [Environment]::GetEnvironmentVariable("Path", $Scope)
    $entries = @(Get-PathEntries $current)
    if ($entries | Where-Object { Test-SamePath $_ $TargetDir }) { return $false }
    $newPath = (@($entries) + $TargetDir) -join [System.IO.Path]::PathSeparator
    [Environment]::SetEnvironmentVariable("Path", $newPath, $Scope)
    return $true
}

function Remove-PathEntry([string]$TargetDir) {
    $current = [Environment]::GetEnvironmentVariable("Path", $Scope)
    $entries = @(Get-PathEntries $current | Where-Object { -not (Test-SamePath $_ $TargetDir) })
    [Environment]::SetEnvironmentVariable("Path", ($entries -join [System.IO.Path]::PathSeparator), $Scope)
}

function Add-CurrentProcessPath([string]$TargetDir) {
    $entries = @(Get-PathEntries $env:Path)
    if ($entries | Where-Object { Test-SamePath $_ $TargetDir }) { return }
    $env:Path = (@($entries) + $TargetDir) -join [System.IO.Path]::PathSeparator
}

function Send-EnvironmentChanged {
    if ($Scope -eq "Process") { return }
    if (-not ("Knowbase.EnvironmentBroadcast" -as [type])) {
        Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
namespace Knowbase {
    public static class EnvironmentBroadcast {
        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern IntPtr SendMessageTimeout(
            IntPtr hWnd, uint message, UIntPtr wParam, string lParam,
            uint flags, uint timeout, out UIntPtr result);
    }
}
"@
    }
    $result = [UIntPtr]::Zero
    [void][Knowbase.EnvironmentBroadcast]::SendMessageTimeout(
        [IntPtr]0xffff, 0x001a, [UIntPtr]::Zero, "Environment", 0x0002, 5000, [ref]$result
    )
}

function Test-CliInstalled {
    if (-not (Test-Path -LiteralPath $ShimPath) -or -not (Test-Path -LiteralPath $HomeFile)) { return $false }
    $registeredRoot = [System.IO.File]::ReadAllText($HomeFile).Trim()
    if (-not (Test-SamePath $registeredRoot $RootDir)) { return $false }
    $pathValue = [Environment]::GetEnvironmentVariable("Path", $Scope)
    return [bool](Get-PathEntries $pathValue | Where-Object { Test-SamePath $_ $BinDir })
}

function Start-DeferredShimCleanup([string]$TargetShim, [string]$TargetBin, [bool]$RemoveBinDir) {
    $cleanupPath = Join-Path ([System.IO.Path]::GetTempPath()) "knowbase-cli-cleanup-$PID.ps1"
    $escapedShim = $TargetShim.Replace("'", "''")
    $escapedBin = $TargetBin.Replace("'", "''")
    $escapedCleanup = $cleanupPath.Replace("'", "''")
    $removeBinBlock = if ($RemoveBinDir) {
@"
if ((Test-Path -LiteralPath '$escapedBin') -and -not (Get-ChildItem -LiteralPath '$escapedBin' -Force | Select-Object -First 1)) {
    Remove-Item -LiteralPath '$escapedBin' -Force -ErrorAction SilentlyContinue
}
"@
    } else { "" }
    $cleanupContent = @"
Start-Sleep -Milliseconds 1200
Remove-Item -LiteralPath '$escapedShim' -Force -ErrorAction SilentlyContinue
$removeBinBlock
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

function Remove-ShimFiles(
    [string]$TargetShim,
    [string]$TargetHome,
    [string]$TargetBin,
    [bool]$RemovePath,
    [bool]$RemoveBinDir
) {
    $runningFromShim = $env:KNOWBASE_CLI_SHIM -and (Test-SamePath $env:KNOWBASE_CLI_SHIM $TargetShim)
    Remove-Item -LiteralPath $TargetHome -Force -ErrorAction SilentlyContinue
    if ($runningFromShim) {
        Start-DeferredShimCleanup $TargetShim $TargetBin $RemoveBinDir
    } else {
        Remove-Item -LiteralPath $TargetShim -Force -ErrorAction SilentlyContinue
    }
    if ($RemovePath) { Remove-PathEntry $TargetBin }
    if ($RemoveBinDir -and -not $runningFromShim -and (Test-Path -LiteralPath $TargetBin) -and -not (Get-ChildItem -LiteralPath $TargetBin -Force | Select-Object -First 1)) {
        Remove-Item -LiteralPath $TargetBin -Force
    }
}

function Remove-LegacyInstallation {
    if (-not $UsingDefaultBin -or (Test-SamePath $LegacyBinDir $BinDir)) { return }
    Remove-ShimFiles $LegacyShimPath $LegacyHomeFile $LegacyBinDir $true $true
}

switch ($Action) {
    "install" {
        if (-not (Test-Path -LiteralPath $ShimTemplate)) { throw "CLI 模板不存在：$ShimTemplate" }
        New-Item -ItemType Directory -Path $BinDir, $ConfigDir -Force | Out-Null
        Copy-Item -LiteralPath $ShimTemplate -Destination $ShimPath -Force
        $utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($HomeFile, $RootDir, $utf8WithoutBom)
        $pathWasPreviouslyOwned = (Test-Path -LiteralPath $PathMarkerFile) -and (Test-SamePath ([System.IO.File]::ReadAllText($PathMarkerFile).Trim()) $BinDir)
        $pathWasAdded = Add-PathEntry $BinDir
        if ($pathWasAdded -or $pathWasPreviouslyOwned) {
            [System.IO.File]::WriteAllText($PathMarkerFile, $BinDir, $utf8WithoutBom)
        } else {
            Remove-Item -LiteralPath $PathMarkerFile -Force -ErrorAction SilentlyContinue
        }
        Add-CurrentProcessPath $BinDir
        Remove-LegacyInstallation
        Send-EnvironmentChanged
        Write-CliMessage "[完成] knowbase 已安装到 $BinDir" Green
        if ($BinWasAlreadyOnCurrentPath) {
            Write-CliMessage "[可用] 当前终端已能执行：knowbase health" Green
        } else {
            Write-CliMessage "[提示] 当前脚本进程已刷新 PATH；若原终端仍未识别，请关闭所有 Windows Terminal 窗口后重新打开。" Cyan
        }
    }
    "uninstall" {
        $removeCurrentPath = (Test-Path -LiteralPath $PathMarkerFile) -and (Test-SamePath ([System.IO.File]::ReadAllText($PathMarkerFile).Trim()) $BinDir)
        $removeBinDir = -not (Test-SamePath $BinDir $PreferredBinDir)
        Remove-ShimFiles $ShimPath $HomeFile $BinDir $removeCurrentPath $removeBinDir
        Remove-Item -LiteralPath $PathMarkerFile -Force -ErrorAction SilentlyContinue
        Remove-LegacyInstallation
        if ((Test-Path -LiteralPath $ConfigDir) -and -not (Get-ChildItem -LiteralPath $ConfigDir -Force | Select-Object -First 1)) {
            Remove-Item -LiteralPath $ConfigDir -Force
        }
        Send-EnvironmentChanged
        Write-CliMessage "[完成] knowbase 全局命令已卸载。" Green
    }
    "status" {
        if (Test-CliInstalled) {
            Write-CliMessage "[已安装] knowbase -> $RootDir ($ShimPath)" Green
        } else {
            Write-CliMessage "[未安装] 运行 .\start.ps1 cli-install 注册全局命令。" Yellow
            throw "knowbase CLI 未正确安装"
        }
    }
}
