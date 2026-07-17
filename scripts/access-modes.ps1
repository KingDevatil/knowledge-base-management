function ConvertTo-KnowbaseAccessModes {
    param(
        [string]$Value,
        [string]$LegacyMode = ""
    )

    $result = @()
    foreach ($rawToken in @($Value -split "[,;\s]+")) {
        $token = ([string]$rawToken).Trim().ToLowerInvariant()
        switch ($token) {
            { $_ -in @("local", "localhost") } { $normalized = "local"; break }
            { $_ -in @("lan", "internal") } { $normalized = "lan"; break }
            { $_ -in @("domain", "public", "external") } { $normalized = "domain"; break }
            { $_ -in @("cloudflare", "tunnel") } { $normalized = "cloudflare"; break }
            default { $normalized = "" }
        }
        if ($normalized -and $result -notcontains $normalized) { $result += $normalized }
    }

    if ($result.Count -eq 0) {
        switch ($LegacyMode.Trim().ToLowerInvariant()) {
            "local" { $result = @("local") }
            "lan" { $result = @("lan") }
            "domain" { $result = @("lan", "domain") }
            "cloudflare" { $result = @("local", "cloudflare") }
            default { $result = @("lan") }
        }
    }

    $ordered = @()
    foreach ($candidate in @("local", "lan", "domain", "cloudflare")) {
        if ($result -contains $candidate) { $ordered += $candidate }
    }
    return ($ordered -join ",")
}

function Test-KnowbaseAccessMode {
    param(
        [string]$Modes,
        [ValidateSet("local", "lan", "domain", "cloudflare")]
        [string]$Mode
    )

    return @((ConvertTo-KnowbaseAccessModes $Modes) -split ",") -contains $Mode
}

function Get-KnowbaseLegacyAccessMode {
    param([string]$Modes)

    $normalized = ConvertTo-KnowbaseAccessModes $Modes
    $items = @($normalized -split "," | Where-Object { $_ })
    if ($items.Count -eq 1) { return $items[0] }
    return "hybrid"
}

function Get-KnowbaseAccessModeChoices {
    param([string]$Modes)

    $choices = @()
    $map = @{ local = "1"; lan = "2"; domain = "3"; cloudflare = "4" }
    foreach ($mode in @((ConvertTo-KnowbaseAccessModes $Modes) -split ",")) {
        if ($map.ContainsKey($mode)) { $choices += $map[$mode] }
    }
    return ($choices -join ",")
}

function Get-KnowbaseAccessModeLabels {
    param([string]$Modes)

    $labels = @()
    $map = @{ local = "仅本机"; lan = "局域网"; domain = "公网"; cloudflare = "Cloudflare Tunnel" }
    foreach ($mode in @((ConvertTo-KnowbaseAccessModes $Modes) -split ",")) {
        if ($map.ContainsKey($mode)) { $labels += $map[$mode] }
    }
    return ($labels -join "、")
}

function Update-KnowbaseAccessModesForTunnel {
    param(
        [string]$Modes,
        [ValidateSet("off", "cloudflare")]
        [string]$TunnelMode
    )

    $items = @((ConvertTo-KnowbaseAccessModes $Modes) -split "," | Where-Object { $_ -ne "cloudflare" })
    if ($TunnelMode -eq "cloudflare") { $items += "cloudflare" }
    if ($items.Count -eq 0) { $items = @("local") }
    return ConvertTo-KnowbaseAccessModes ($items -join ",")
}

function Get-KnowbaseAccessModeMenuEntries {
    return @(
        [pscustomobject]@{ Number = "1"; Mode = "local"; Label = "仅本机" },
        [pscustomobject]@{ Number = "2"; Mode = "lan"; Label = "局域网" },
        [pscustomobject]@{ Number = "3"; Mode = "domain"; Label = "公网" },
        [pscustomobject]@{ Number = "4"; Mode = "cloudflare"; Label = "Cloudflare Tunnel" }
    )
}

function Read-KnowbaseAccessModeSelectionByKeys([string]$CurrentModes) {
    $entries = @(Get-KnowbaseAccessModeMenuEntries)
    $selected = @{}
    foreach ($entry in $entries) {
        $selected[$entry.Mode] = Test-KnowbaseAccessMode $CurrentModes $entry.Mode
    }

    $bufferWidth = [Console]::BufferWidth
    if ($bufferWidth -lt 20) { throw "当前终端宽度不足，无法显示按键选择菜单。" }
    $lineWidth = $bufferWidth - 1
    $currentIndex = 0
    $status = ""
    $cursorVisibilityChanged = $false
    $originalCursorVisible = $true

    Write-Host ""
    Write-Host "访问方式（可多选）" -ForegroundColor Cyan
    Write-Host "  ↑/↓ 移动  Space 勾选/取消  Enter 提交  Esc 保留当前配置"
    foreach ($unused in 1..($entries.Count + 1)) { Write-Host "" }
    $menuBottom = [Console]::CursorTop
    $menuTop = $menuBottom - ($entries.Count + 1)
    if ($menuTop -lt 0) { throw "当前终端无法定位选择菜单。" }

    $renderMenu = {
        for ($index = 0; $index -lt $entries.Count; $index++) {
            $entry = $entries[$index]
            $pointer = if ($index -eq $currentIndex) { ">" } else { " " }
            $mark = if ($selected[$entry.Mode]) { "x" } else { " " }
            $line = "  $pointer [$mark] $($entry.Number)) $($entry.Label)"
            if ($line.Length -gt $lineWidth) { $line = $line.Substring(0, $lineWidth) }
            [Console]::SetCursorPosition(0, $menuTop + $index)
            [Console]::Write(" " * $lineWidth)
            [Console]::SetCursorPosition(0, $menuTop + $index)
            [Console]::Write($line)
        }
        [Console]::SetCursorPosition(0, $menuTop + $entries.Count)
        $statusLine = if ($status) { "  $status" } else { "" }
        if ($statusLine.Length -gt $lineWidth) { $statusLine = $statusLine.Substring(0, $lineWidth) }
        [Console]::Write(" " * $lineWidth)
        [Console]::SetCursorPosition(0, $menuTop + $entries.Count)
        [Console]::Write($statusLine)
        [Console]::SetCursorPosition(0, $menuBottom)
    }

    try {
        try {
            $originalCursorVisible = [Console]::CursorVisible
            [Console]::CursorVisible = $false
            $cursorVisibilityChanged = $true
        } catch {
            $cursorVisibilityChanged = $false
        }

        & $renderMenu
        while ($true) {
            $key = [Console]::ReadKey($true)
            switch ($key.Key) {
                "UpArrow" {
                    $currentIndex = if ($currentIndex -le 0) { $entries.Count - 1 } else { $currentIndex - 1 }
                    $status = ""
                }
                "DownArrow" {
                    $currentIndex = if ($currentIndex -ge $entries.Count - 1) { 0 } else { $currentIndex + 1 }
                    $status = ""
                }
                "Spacebar" {
                    $mode = $entries[$currentIndex].Mode
                    $selected[$mode] = -not $selected[$mode]
                    $status = ""
                }
                "Enter" {
                    $selectedModes = @($entries | Where-Object { $selected[$_.Mode] } | ForEach-Object { $_.Mode })
                    if ($selectedModes.Count -eq 0) {
                        $status = "至少勾选一种访问方式。"
                    } else {
                        $status = "已提交。"
                        & $renderMenu
                        return ConvertTo-KnowbaseAccessModes ($selectedModes -join ",")
                    }
                }
                "Escape" {
                    $status = "已取消修改，保留当前配置。"
                    & $renderMenu
                    return ConvertTo-KnowbaseAccessModes $CurrentModes
                }
            }
            & $renderMenu
        }
    } finally {
        try { [Console]::SetCursorPosition(0, $menuBottom) } catch {}
        if ($cursorVisibilityChanged) {
            try { [Console]::CursorVisible = $originalCursorVisible } catch {}
        }
    }
}

function Read-KnowbaseAccessModeSelectionByNumbers([string]$CurrentModes) {
    $currentModes = ConvertTo-KnowbaseAccessModes $CurrentModes
    while ($true) {
        Write-Host ""
        Write-Host "访问方式（兼容输入模式，可多选）" -ForegroundColor Cyan
        foreach ($entry in @(Get-KnowbaseAccessModeMenuEntries)) {
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

function Read-KnowbaseAccessModeSelection([string]$CurrentModes) {
    $currentModes = ConvertTo-KnowbaseAccessModes $CurrentModes
    try {
        return Read-KnowbaseAccessModeSelectionByKeys $currentModes
    } catch [System.Management.Automation.PipelineStoppedException] {
        throw
    } catch [System.OperationCanceledException] {
        throw
    } catch {
        Write-Host "[提示] 当前终端不支持逐键菜单，已切换为编号输入。" -ForegroundColor Yellow
        return Read-KnowbaseAccessModeSelectionByNumbers $currentModes
    }
}
