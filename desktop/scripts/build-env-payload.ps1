# Builds the env payload: a relocatable python-build-standalone 3.12 interpreter with the full
# HYPE dependency set installed straight into it (no venv -> no pyvenv.cfg path problems), PLUS
# the Windows solver runtimes (tools\ras2025 = HEC-RAS 2025 CLI, tools\modflow = mf6/mp7) pinned
# by desktop/payload/tools.lock.
#
# Pipeline: pbs fetch+verify -> uv pip install (env.lock) -> fixups (drop Scripts\*.exe trampolines,
# fail on absolute-path .pth, apply prune.txt, long-path check) -> compileall (unchecked-hash pycs,
# CI prefix stripped) -> tools fetch+verify+extract -> RELOCATION SMOKE GATE (move the tree, import
# the heavy stack, boot the real app against the relocated tools) -> zip + sha256. Nothing should
# ever publish an env zip that skipped the gate.
#
# ENV_VERSION is content-derived: env-cp312-<first 8 hex of sha256(LF(env.lock)+LF(pbs.lock)+
# LF(prune.txt)+LF(tools.lock))>. The same computation runs in CI to decide whether a rebuild is
# needed at all — so a tools.lock bump re-ships the env automatically.
[CmdletBinding()]
param(
    [string]$RepoRoot = (Join-Path $PSScriptRoot '..\..'),
    [string]$OutDir = (Join-Path $PSScriptRoot '..\build\release'),
    [string]$WorkDir = (Join-Path $PSScriptRoot '..\build\env-work'),
    [string]$UvExe = 'uv',
    [switch]$NoUvCache,
    [switch]$SkipSmoke,  # local debugging only - CI must never pass this
    [switch]$VersionOnly # print ENV_VERSION and exit (CI uses this to decide whether to rebuild)
)
$ErrorActionPreference = 'Stop'
$RepoRoot = [IO.Path]::GetFullPath($RepoRoot)
$OutDir = [IO.Path]::GetFullPath($OutDir)
$WorkDir = [IO.Path]::GetFullPath($WorkDir)
$payloadDir = Join-Path $RepoRoot 'desktop\payload'
$envLock = Join-Path $payloadDir 'env.lock'
$pbsLock = Join-Path $payloadDir 'pbs.lock'
$pruneFile = Join-Path $payloadDir 'prune.txt'
$toolsLock = Join-Path $payloadDir 'tools.lock'

function Get-EnvVersion {
    # Content hash over ALL env-build inputs (locks + prune recipe + tools pins) so any change
    # triggers a rebuild in CI. Operates on RAW BYTES with CR (0x0D) stripped: text decoding here
    # would tie the version to the PowerShell edition (5.1 reads BOM-less UTF-8 as CP-1252,
    # pwsh 7 as UTF-8), and eol normalization keeps working trees and CI checkouts in agreement.
    $stream = New-Object IO.MemoryStream
    foreach ($file in @($envLock, $pbsLock, $pruneFile, $toolsLock)) {
        foreach ($byte in [IO.File]::ReadAllBytes($file)) {
            if ($byte -ne 13) { $stream.WriteByte($byte) }
        }
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $hex = -join ($sha.ComputeHash($stream.ToArray()) | ForEach-Object { $_.ToString('x2') })
    } finally { $sha.Dispose(); $stream.Dispose() }
    "env-cp312-$($hex.Substring(0, 8))"
}

$envVersion = Get-EnvVersion
Write-Host "[env] ENV_VERSION = $envVersion"
"ENV_VERSION=$envVersion" | Write-Output
if ($VersionOnly) { return }

New-Item -ItemType Directory -Force $OutDir | Out-Null
$zipPath = Join-Path $OutDir "hype-$envVersion.zip"
if (Test-Path $zipPath) {
    Write-Host "[env] $zipPath already exists - nothing to do"
    return
}

if (Test-Path $WorkDir) { Remove-Item $WorkDir -Recurse -Force }
New-Item -ItemType Directory -Force $WorkDir | Out-Null

# -- 1. python-build-standalone: fetch + verify against pbs.lock --
$pbs = Get-Content $pbsLock -Raw | ConvertFrom-Json
$tarPath = Join-Path $WorkDir $pbs.file
Write-Host "[env] fetching $($pbs.file)..."
Invoke-WebRequest $pbs.url -OutFile $tarPath
$actual = (Get-FileHash $tarPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $pbs.sha256) {
    throw "pbs archive hash mismatch: expected $($pbs.sha256), got $actual"
}
tar -xzf $tarPath -C $WorkDir
if ($LASTEXITCODE -ne 0) { throw "tar extraction failed ($LASTEXITCODE)" }
$pyRoot = Join-Path $WorkDir 'python'
$py = Join-Path $pyRoot 'python.exe'
if (-not (Test-Path $py)) { throw 'python.exe missing after extraction' }

# -- 2. Install the locked dependency set straight into the interpreter --
Write-Host '[env] uv pip install (env.lock)...'
$uvArgs = @('pip', 'install', '--python', $py, '--link-mode=copy', '-r', $envLock)
if ($NoUvCache) { $uvArgs += '--no-cache' }
& $UvExe @uvArgs
if ($LASTEXITCODE -ne 0) { throw "uv pip install failed ($LASTEXITCODE)" }

# -- 3. Fixups --
# 3a. pip's Scripts\*.exe trampolines embed this machine's absolute python path; the shell only
#     ever invokes python.exe -m ..., so they are dead weight that would break after relocation.
Get-ChildItem (Join-Path $pyRoot 'Scripts') -Filter '*.exe' -ErrorAction SilentlyContinue | Remove-Item -Force

# 3b. Absolute-path .pth files would silently re-point imports at the build machine.
$badPth = Get-ChildItem (Join-Path $pyRoot 'Lib\site-packages') -Filter '*.pth' |
    Where-Object { (Get-Content $_.FullName) -match '^[A-Za-z]:[\\/]' }
if ($badPth) {
    throw "Absolute paths found in .pth files: $($badPth.Name -join ', ') - not relocatable"
}

# 3c. prune.txt globs + long-path guard (python's ** globbing is authoritative here).
Write-Host '[env] pruning + path-length check...'
& $py -c @"
import pathlib, shutil, sys

root = pathlib.Path(r'$pyRoot')
prune_file = pathlib.Path(r'$pruneFile')
removed = 0
for line in prune_file.read_text(encoding='utf-8').splitlines():
    pattern = line.strip()
    if not pattern or pattern.startswith('#'):
        continue
    pattern = pattern.rstrip('/*').rstrip('/')
    for match in sorted(root.glob(pattern), reverse=True):
        if match.is_dir():
            shutil.rmtree(match, ignore_errors=True)
        else:
            match.unlink(missing_ok=True)
        removed += 1
print(f'[env] pruned {removed} path(s)')

too_long = [p for p in root.rglob('*') if len(str(p)) - len(str(root)) > 180]
if too_long:
    print('[env] FAIL: payload-relative paths too long (would break at 260-char limits):')
    for p in too_long[:10]:
        print('   ', p)
    sys.exit(1)
"@
if ($LASTEXITCODE -ne 0) { throw 'prune / path-length step failed' }

# -- 4. Precompile: pycs valid regardless of extraction mtime/path; CI prefix stripped --
Write-Host '[env] compileall...'
& $py -m compileall -f -q -j 0 --invalidation-mode unchecked-hash -s $WorkDir (Join-Path $pyRoot 'Lib')
if ($LASTEXITCODE -ne 0) { throw "compileall failed ($LASTEXITCODE)" }

# -- 5. Windows solver runtimes: fetch + verify + extract per tools.lock --
$toolsRoot = Join-Path $WorkDir 'tools'
$tools = (Get-Content $toolsLock -Raw | ConvertFrom-Json).tools
foreach ($tool in $tools) {
    $zipName = [IO.Path]::GetFileName(([Uri]$tool.url).AbsolutePath)
    $toolZip = Join-Path $WorkDir $zipName
    Write-Host "[env] fetching tool $($tool.name) ($zipName)..."
    Invoke-WebRequest $tool.url -OutFile $toolZip
    $actual = (Get-FileHash $toolZip -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $tool.sha256) {
        throw "tool $($tool.name) hash mismatch: expected $($tool.sha256), got $actual"
    }
    $dest = Join-Path $WorkDir ($tool.extractTo -replace '/', '\')
    New-Item -ItemType Directory -Force $dest | Out-Null
    tar -xf $toolZip -C $dest
    if ($LASTEXITCODE -ne 0) { throw "tool $($tool.name) extraction failed ($LASTEXITCODE)" }
    Remove-Item $toolZip -Force
}
if (-not (Test-Path (Join-Path $toolsRoot 'ras2025\ras.exe'))) { throw 'tools\ras2025\ras.exe missing after extraction' }
if (-not (Test-Path (Join-Path $toolsRoot 'modflow\mf6.exe'))) { throw 'tools\modflow\mf6.exe missing after extraction' }

# Same long-path guard over the tools tree (RAS's GDAL data dirs are the deepest part).
& $py -c @"
import pathlib, sys
root = pathlib.Path(r'$toolsRoot')
too_long = [p for p in root.rglob('*') if len(str(p)) - len(str(root)) > 180]
if too_long:
    print('[env] FAIL: tools-relative paths too long (would break at 260-char limits):')
    for p in too_long[:10]:
        print('   ', p)
    sys.exit(1)
"@
if ($LASTEXITCODE -ne 0) { throw 'tools path-length check failed' }

# -- 6. RELOCATION SMOKE GATE: move python AND tools, then import + boot the real app --
$relocated = Join-Path $WorkDir 'relocated'
New-Item -ItemType Directory -Force $relocated | Out-Null
Move-Item $pyRoot (Join-Path $relocated 'python')
Move-Item $toolsRoot (Join-Path $relocated 'tools')
$relocatedPy = Join-Path $relocated 'python\python.exe'

if ($SkipSmoke) {
    Write-Warning '[env] SMOKE GATE SKIPPED - do not publish this zip'
} else {
    & $relocatedPy (Join-Path $PSScriptRoot 'smoke_boot_app.py') `
        --python $relocatedPy --app-root $RepoRoot --tools-dir (Join-Path $relocated 'tools')
    if ($LASTEXITCODE -ne 0) { throw "relocation smoke gate FAILED ($LASTEXITCODE)" }
}

# -- 7. Zip (root = python\ + tools\) + sha256 --
Write-Host '[env] zipping...'
tar -a -c -f $zipPath -C $relocated python tools
if ($LASTEXITCODE -ne 0) { throw "zip failed ($LASTEXITCODE)" }
$zipHash = (Get-FileHash $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -Encoding ascii "$zipPath.sha256" $zipHash

$sizeMB = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
Write-Host "[env] done: $zipPath ($sizeMB MB, sha256 $zipHash)"
