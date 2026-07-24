# Builds the apps payload: the HYPE app tree + generated desktop-manifest.json.
#
# Staging comes from GIT-TRACKED content only (git archive) - the checkout carries gitignored
# material (reference/ HEC-RAS install, .venv, workspace state) that must never enter a public
# release asset. The pathspec also excludes the linux-only solver bundles (bin/linux, bin/ras2025,
# ~310 MB dead weight on Windows) — the Windows solvers ship inside the env payload's tools\ tree.
[CmdletBinding()]
param(
    [string]$RepoRoot = (Join-Path $PSScriptRoot '..\..'),
    [string]$OutDir = (Join-Path $PSScriptRoot '..\build\release'),
    [string]$WorkDir = (Join-Path $PSScriptRoot '..\build\apps-work'),
    [Parameter(Mandatory)][string]$EnvVersion,
    [string]$AppsVersion,
    [string]$PythonExe = 'python'   # any python 3 — the manifest generator is stdlib-only
)
$ErrorActionPreference = 'Stop'
$RepoRoot = [IO.Path]::GetFullPath($RepoRoot)
$OutDir = [IO.Path]::GetFullPath($OutDir)
$WorkDir = [IO.Path]::GetFullPath($WorkDir)

# The tracked paths the app needs at runtime (repo-root app: app.py + packages + static assets).
$appPaths = @('app.py', 'hype_app', 'hypetool', 'www')

Push-Location $RepoRoot
try {
    $commit = (git rev-parse --short HEAD).Trim()
    if (-not $AppsVersion) {
        $AppsVersion = "apps-$(Get-Date -Format 'yyyy.MM.dd')-$commit"
    }
    Write-Host "[apps] APPS_VERSION = $AppsVersion"
    "APPS_VERSION=$AppsVersion" | Write-Output

    if (Test-Path $WorkDir) { Remove-Item $WorkDir -Recurse -Force }
    $stage = Join-Path $WorkDir 'stage'
    New-Item -ItemType Directory -Force $stage | Out-Null

    # -- 1. Stage tracked content only --
    Write-Host '[apps] staging tracked app content via git archive...'
    $tarPath = Join-Path $WorkDir 'apps.tar'
    git archive --format=tar -o $tarPath HEAD -- @appPaths
    if ($LASTEXITCODE -ne 0) { throw "git archive failed ($LASTEXITCODE)" }
    tar -xf $tarPath -C $stage
    if ($LASTEXITCODE -ne 0) { throw "tar extract failed ($LASTEXITCODE)" }

    # -- 2. Generate desktop-manifest.json (fixed single-app entry; stdlib-only) --
    $genScript = Join-Path $PSScriptRoot 'gen_desktop_manifest.py'
    $manifestOut = Join-Path $stage 'desktop-manifest.json'
    & $PythonExe $genScript --apps-version $AppsVersion --env-version $EnvVersion `
        --commit $commit --out $manifestOut
    if ($LASTEXITCODE -ne 0) { throw "manifest generation failed ($LASTEXITCODE)" }

    # -- 3. Zip (root = app.py hype_app/ hypetool/ www/ desktop-manifest.json) + sha256 --
    New-Item -ItemType Directory -Force $OutDir | Out-Null
    $zipPath = Join-Path $OutDir "hype-$AppsVersion.zip"
    Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
    tar -a -c -f $zipPath -C $stage '*'
    if ($LASTEXITCODE -ne 0) { throw "zip failed ($LASTEXITCODE)" }
    $zipHash = (Get-FileHash $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    Set-Content -Encoding ascii "$zipPath.sha256" $zipHash

    $sizeMB = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
    Write-Host "[apps] done: $zipPath ($sizeMB MB, sha256 $zipHash)"
} finally {
    Pop-Location
}
