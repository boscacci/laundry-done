$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$configPath = Join-Path $repoRoot 'firmware/include/laundry_config.h'
$outputPath = Join-Path $repoRoot 'outputs'

if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "Missing $configPath; copy and edit laundry_config.h.example first."
}

$configDigest = (Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash.ToLowerInvariant()
New-Item -ItemType Directory -Force -Path $outputPath | Out-Null
$stagingPath = Join-Path $outputPath ('.firmware-build-' + [guid]::NewGuid())
$dockerExitCode = 1
New-Item -ItemType Directory -Path $stagingPath | Out-Null

try {
    & docker build `
        --file (Join-Path $repoRoot 'Dockerfile.firmware') `
        --secret "id=laundry_config,src=$configPath" `
        --build-arg "CONFIG_DIGEST=$configDigest" `
        --output "type=local,dest=$stagingPath" `
        $repoRoot
    $dockerExitCode = $LASTEXITCODE
    if ($dockerExitCode -eq 0) {
        Move-Item `
            -LiteralPath (Join-Path $stagingPath 'firmware.bin') `
            -Destination (Join-Path $outputPath 'firmware.bin') `
            -Force
    }
}
finally {
    Remove-Item -LiteralPath $stagingPath -Recurse -Force
}
if ($dockerExitCode -ne 0) {
    throw "Firmware container build failed with exit code $dockerExitCode."
}

Get-FileHash -LiteralPath (Join-Path $outputPath 'firmware.bin') -Algorithm SHA256
