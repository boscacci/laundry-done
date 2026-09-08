$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
& docker compose `
    --file (Join-Path $repoRoot 'compose.ota.yaml') `
    run --build --rm ota `
    --config /run/secrets/laundry_config `
    @args
if ($LASTEXITCODE -ne 0) {
    throw "Firmware OTA operation failed with exit code $LASTEXITCODE."
}
