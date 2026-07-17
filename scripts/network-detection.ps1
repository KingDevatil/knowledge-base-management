function Test-KnowbaseUsableIPv4Address {
    param([string]$Address)

    $parsed = $null
    if ([string]::IsNullOrWhiteSpace($Address) -or
        -not [System.Net.IPAddress]::TryParse($Address.Trim(), [ref]$parsed) -or
        $parsed.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) {
        return $false
    }

    $bytes = $parsed.GetAddressBytes()
    if ($bytes[0] -eq 0 -or $bytes[0] -eq 127 -or $bytes[0] -ge 224) { return $false }
    if ($bytes[0] -eq 169 -and $bytes[1] -eq 254) { return $false }
    return $true
}

function Get-KnowbaseComputerName {
    param([string]$Override = "")

    if (-not [string]::IsNullOrWhiteSpace($Override)) { return $Override.Trim() }
    if (-not [string]::IsNullOrWhiteSpace($env:COMPUTERNAME)) { return $env:COMPUTERNAME.Trim() }
    try {
        $name = [System.Net.Dns]::GetHostName()
        if (-not [string]::IsNullOrWhiteSpace($name)) { return $name.Trim() }
    } catch {
        # Fall through to the hostname executable when DNS lookup is unavailable.
    }
    try {
        $name = (& hostname 2>$null | Select-Object -First 1)
        if (-not [string]::IsNullOrWhiteSpace($name)) { return ([string]$name).Trim() }
    } catch {
        # The caller will keep localhost when neither source is available.
    }
    return ""
}

function Get-KnowbaseLanIPv4Addresses {
    param([string[]]$Override)

    $candidates = @()
    if ($PSBoundParameters.ContainsKey("Override")) {
        $candidates = @($Override)
    } else {
        try {
            if (Get-Command Get-NetIPConfiguration -ErrorAction SilentlyContinue) {
                $configurations = @(
                    Get-NetIPConfiguration -ErrorAction Stop |
                        Where-Object { -not $_.NetAdapter -or $_.NetAdapter.Status -eq "Up" }
                )
                $preferred = @($configurations | Where-Object { $_.IPv4DefaultGateway })
                if ($preferred.Count -eq 0) { $preferred = $configurations }
                $preferred = @(
                    $preferred | Sort-Object @{ Expression = {
                        if ($_.NetIPv4Interface) { [int]$_.NetIPv4Interface.InterfaceMetric } else { [int]::MaxValue }
                    } }
                )
                foreach ($configuration in $preferred) {
                    foreach ($entry in @($configuration.IPv4Address)) {
                        if ($entry -and $entry.IPAddress) { $candidates += [string]$entry.IPAddress }
                    }
                }
            }
        } catch {
            $candidates = @()
        }

        if ($candidates.Count -eq 0) {
            try {
                $candidates = @(
                    [System.Net.Dns]::GetHostAddresses((Get-KnowbaseComputerName)) |
                        ForEach-Object { $_.IPAddressToString }
                )
            } catch {
                $candidates = @()
            }
        }
    }

    $result = @()
    foreach ($candidate in $candidates) {
        $address = ([string]$candidate).Trim().Split("%")[0]
        if ((Test-KnowbaseUsableIPv4Address $address) -and $result -notcontains $address) {
            $result += $address
        }
    }
    return $result
}

function ConvertTo-KnowbaseInternalHostValue {
    param([string]$Value)

    $result = @()
    foreach ($rawToken in @($Value -split "[,;\s]+")) {
        $token = ([string]$rawToken).Trim()
        if ($token -and $result -notcontains $token) { $result += $token }
    }
    return ($result -join ",")
}

function Get-KnowbaseDetectedInternalHostValue {
    param(
        [string]$HostNameOverride = "",
        [string[]]$IPv4Override
    )

    $hostName = Get-KnowbaseComputerName -Override $HostNameOverride
    $addresses = if ($PSBoundParameters.ContainsKey("IPv4Override")) {
        @(Get-KnowbaseLanIPv4Addresses -Override $IPv4Override)
    } else {
        @(Get-KnowbaseLanIPv4Addresses)
    }
    return ConvertTo-KnowbaseInternalHostValue ((@($hostName) + $addresses) -join ",")
}

function Get-KnowbaseSuggestedInternalHostValue {
    param(
        [string]$CurrentValue,
        [string]$DetectedValue
    )

    $retainedNames = @()
    foreach ($token in @((ConvertTo-KnowbaseInternalHostValue $CurrentValue) -split ",")) {
        if (-not $token -or $token -in @("localhost", "127.0.0.1")) { continue }
        if (-not (Test-KnowbaseUsableIPv4Address $token)) { $retainedNames += $token }
    }
    $suggested = ConvertTo-KnowbaseInternalHostValue ((@($retainedNames) + @($DetectedValue)) -join ",")
    if ($suggested) { return $suggested }
    $current = ConvertTo-KnowbaseInternalHostValue $CurrentValue
    if ($current) { return $current }
    return "localhost"
}
