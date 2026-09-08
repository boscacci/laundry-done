$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$configPath = Join-Path $repoRoot 'firmware/include/laundry_config.h'
$outputPath = Join-Path $repoRoot 'outputs/firmware'

if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "Missing $configPath; copy and edit laundry_config.h.example first."
}

$configDigest = (Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash.ToLowerInvariant()
New-Item -ItemType Directory -Force -Path $outputPath | Out-Null

& docker build `
    --file (Join-Path $repoRoot 'Dockerfile.firmware') `
    --secret "id=laundry_config,src=$configPath" `
    --build-arg "CONFIG_DIGEST=$configDigest" `
    --output "type=local,dest=$outputPath" `
    $repoRoot
if ($LASTEXITCODE -ne 0) {
    throw "Firmware container build failed with exit code $LASTEXITCODE."
}

Get-FileHash -LiteralPath (Join-Path $outputPath 'firmware.bin') -Algorithm SHA256
