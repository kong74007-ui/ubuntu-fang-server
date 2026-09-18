param(
    [Parameter(Mandatory=$true)][string]$Destination,
    [Parameter(Mandatory=$true)][string]$Browser,
    [string]$Node = 'node',
    [string]$Npm = 'npm.cmd'
)
$ErrorActionPreference = 'Stop'
$destinationPath = [IO.Path]::GetFullPath($Destination)
$browserPath = (Resolve-Path -LiteralPath $Browser).Path
if (Test-Path -LiteralPath $destinationPath) { throw 'Use a new staging directory; active releases are never overwritten.' }
$nodePath = (Get-Command $Node -ErrorAction Stop).Source
$nodeVersion = & $nodePath --version
if ($LASTEXITCODE -ne 0 -or [int]($nodeVersion.TrimStart('v').Split('.')[0]) -lt 22) { throw 'Node.js 22 or newer is required.' }
New-Item -ItemType Directory -Path $destinationPath | Out-Null
$files = 'package.json','package-lock.json','contract.json','render.mjs','browser.mjs','compile.mjs','geometry.mjs','compositor.mjs','probe.mjs'
foreach ($name in $files) { Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination (Join-Path $destinationPath $name) }
& $Npm ci --prefix $destinationPath --ignore-scripts --no-audit --no-fund
if ($LASTEXITCODE -ne 0) { throw 'GPU dependency installation failed; no service was changed.' }
& $nodePath (Join-Path $destinationPath 'probe.mjs')
if ($LASTEXITCODE -ne 0) { throw 'Hardware GPU preflight failed; no service was changed.' }
Write-Output 'Staging passed. No service was stopped, restarted or switched.'
Write-Output 'MATRIX_TEMPLATE_GPU_MODE=required'
Write-Output "MATRIX_TEMPLATE_GPU_RUNTIME=$destinationPath"
Write-Output "MATRIX_TEMPLATE_GPU_NODE=$nodePath"
Write-Output "MATRIX_TEMPLATE_HYPERFRAMES_BROWSER=$browserPath"
Write-Output 'Install the matching matrix_template_api.py and matrix_gpu_runtime.py before switching an idle worker to this runtime.'
