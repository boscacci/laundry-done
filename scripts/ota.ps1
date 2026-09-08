$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$dockerConfig = Join-Path ([IO.Path]::GetTempPath()) ("laundry-docker-" + [guid]::NewGuid())
$previousDockerConfig = $env:DOCKER_CONFIG
$dockerExitCode = 1
New-Item -ItemType Directory -Path $dockerConfig | Out-Null
Set-Content -LiteralPath (Join-Path $dockerConfig 'config.json') -Value '{}' -NoNewline

try {
    $env:DOCKER_CONFIG = $dockerConfig
    & docker compose `
        --file (Join-Path $repoRoot 'compose.ota.yaml') `
        run --build --rm ota `
        --config /run/secrets/laundry_config `
        @args
    $dockerExitCode = $LASTEXITCODE
}
finally {
    $env:DOCKER_CONFIG = $previousDockerConfig
    Remove-Item -LiteralPath $dockerConfig -Recurse -Force
}
if ($dockerExitCode -ne 0) {
    throw "Firmware OTA operation failed with exit code $dockerExitCode."
}
