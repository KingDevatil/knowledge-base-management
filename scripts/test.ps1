$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot

if ($env:PYTHON) {
    $Python = $env:PYTHON
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $Python = "python"
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $Python = "py"
} else {
    $Python = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python" -Filter python.exe -Recurse -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending |
        Select-Object -First 1 -ExpandProperty FullName
}

if (-not $Python) {
    throw "Python executable not found. Set the PYTHON environment variable and retry."
}

& $Python -m pytest "$Root\mcp-gateway\tests" @args
exit $LASTEXITCODE
