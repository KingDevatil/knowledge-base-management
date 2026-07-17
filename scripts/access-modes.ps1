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
