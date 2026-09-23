param(
    [Parameter(Mandatory=$true)][string]$Destination,
    [Parameter(Mandatory=$true)][string]$Browser,
    [string]$Python = 'python',
    [string]$Node = 'node',
    [string]$Npm = 'npm.cmd'
)
$ErrorActionPreference = 'Stop'
& $Python -c 'import tinycss2, cssselect2'
if ($LASTEXITCODE -ne 0) { throw 'Install deploy/requirements-matrix-text-controls.txt in the service Python environment first.' }
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$destinationPath = [IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $destinationPath) { throw 'Use a new release directory; active files are never overwritten.' }
New-Item -ItemType Directory -Path $destinationPath | Out-Null
$upstream = Join-Path $destinationPath 'upstream'
$commit = '981ecf0584d963c6e26a2f9d5cfa7fd6985758d2'
git init $upstream
if ($LASTEXITCODE -ne 0) { throw 'Cannot create staging checkout.' }
git -C $upstream remote add origin https://github.com/kong74007-ui/script-to-matrix-video.git
git -C $upstream fetch --depth 1 origin $commit
if ($LASTEXITCODE -ne 0) { throw 'Cannot download the pinned templates.' }
git -C $upstream checkout --detach FETCH_HEAD
if ($LASTEXITCODE -ne 0 -or ((git -C $upstream rev-parse HEAD).Trim() -ne $commit)) { throw 'Template revision mismatch.' }
$templates = Join-Path $destinationPath 'templates'
& $Python (Join-Path $PSScriptRoot 'prepare-motion-v3-template.py') --skill (Join-Path $upstream 'script-to-matrix-video') --output $templates
if ($LASTEXITCODE -ne 0) { throw 'Template staging failed.' }
$runtime = Join-Path $destinationPath 'hyperframes-runtime'
New-Item -ItemType Directory -Path $runtime | Out-Null
foreach ($name in 'package.json','package-lock.json') { Copy-Item -LiteralPath (Join-Path $PSScriptRoot "motion-v3-runtime/$name") -Destination (Join-Path $runtime $name) }
& $Npm ci --prefix $runtime --ignore-scripts --no-audit --no-fund
if ($LASTEXITCODE -ne 0) { throw 'HyperFrames dependencies failed.' }
$cli = Join-Path $runtime 'node_modules/.bin/hyperframes.cmd'
$version = & $cli --version
if ($LASTEXITCODE -ne 0 -or $version.Trim() -ne '0.8.38') { throw 'HyperFrames version mismatch.' }
$service = Join-Path $destinationPath 'service'
New-Item -ItemType Directory -Path $service | Out-Null
foreach ($name in 'matrix_template_api.py','matrix_motion_v3.py','matrix_text_controls.py','matrix_material_adaptation.py','matrix_gpu_runtime.py','matrix_gpu_supervisor.py') {
    Copy-Item -LiteralPath (Join-Path $repo "server/$name") -Destination (Join-Path $service $name)
}
& (Join-Path $repo 'deploy/matrix-gpu/stage-runtime.ps1') -Destination (Join-Path $destinationPath 'gpu') -Browser $Browser -Node $Node -Npm $Npm
Write-Output "MATRIX_TEMPLATE_MOTION_V3_ROOT=$templates"
Write-Output "MATRIX_TEMPLATE_MOTION_V3_HYPERFRAMES_CLI=$cli"
Write-Output "Prepared service entry: $(Join-Path $service 'matrix_template_api.py')"
Write-Output 'No service was changed or restarted. Deploy the main-site companion and upload owned videos before switching idle workers.'
