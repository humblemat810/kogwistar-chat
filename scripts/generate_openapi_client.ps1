param(
    [Parameter(Mandatory=$true)]
    [string]$CoreOpenApi
)

$ErrorActionPreference = "Stop"
$chatRoot = Split-Path -Parent $PSScriptRoot
$output = Join-Path $chatRoot "generated\kogwistar_api"

if (-not (Test-Path -LiteralPath $CoreOpenApi)) {
    throw "OpenAPI input missing: $CoreOpenApi"
}

Push-Location $chatRoot
try {
    & ".venv\Scripts\openapi-python-client.exe" generate `
        --path $CoreOpenApi `
        --output-path $output `
        --overwrite
    if ($LASTEXITCODE -ne 0) {
        throw "OpenAPI client generation failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
