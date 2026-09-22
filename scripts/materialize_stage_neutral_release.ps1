param(
    [string]$SourceSha = $env:AUTOSPORT_SOURCE_SHA,
    [string]$RepoRoot = ''
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($SourceSha)) {
    throw 'Stage-neutral release materialization requires an exact source SHA via -SourceSha or AUTOSPORT_SOURCE_SHA.'
}
if ($SourceSha -notmatch '^[0-9a-f]{40}$') {
    throw 'Stage-neutral release materialization requires a canonical 40-character lowercase Git SHA.'
}
if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    if ([string]::IsNullOrWhiteSpace($PSScriptRoot)) {
        throw 'Stage-neutral release materialization requires an explicit repository root.'
    }
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}
$repoRoot = [System.IO.Path]::GetFullPath($RepoRoot)
$distRoot = Join-Path $repoRoot 'dist'
$legacyPackage = Join-Path $distRoot 'Autosport-V1-windows-x64.zip'
$stageNeutralPackage = Join-Path $distRoot 'Autosport-windows-x64.zip'
$evidencePath = Join-Path $distRoot 'stage-neutral-release-verification.json'

if (-not (Test-Path -LiteralPath $legacyPackage -PathType Leaf)) {
    throw "Canonical legacy release package is missing: $legacyPackage"
}

$pythonCommands = @(Get-Command python -CommandType Application -ErrorAction Stop)
if ($pythonCommands.Count -lt 1) {
    throw 'Unable to resolve Python application for stage-neutral release materialization.'
}
$pythonExecutable = [string]$pythonCommands[0].Source
if ([string]::IsNullOrWhiteSpace($pythonExecutable)) {
    throw 'Resolved Python application has an empty source path.'
}

$gitCommands = @(Get-Command git -CommandType Application -ErrorAction Stop)
if ($gitCommands.Count -lt 1) {
    throw 'Unable to resolve Git application for stage-neutral release materialization.'
}
$gitExecutable = [string]$gitCommands[0].Source
if ([string]::IsNullOrWhiteSpace($gitExecutable)) {
    throw 'Resolved Git application has an empty source path.'
}

Get-ChildItem Env: | Where-Object { $_.Name -like 'GIT_*' } | ForEach-Object {
    Remove-Item -LiteralPath ("Env:" + $_.Name) -ErrorAction SilentlyContinue
}
$env:GIT_NO_REPLACE_OBJECTS = '1'

$checkoutHead = (& $gitExecutable -C $repoRoot rev-parse --verify HEAD 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to resolve exact checkout HEAD for stage-neutral release materialization.'
}
if ($checkoutHead -ne $SourceSha) {
    throw 'Stage-neutral release source SHA does not match checkout HEAD.'
}

$gitTopLevel = (& $gitExecutable -C $repoRoot rev-parse --show-toplevel 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to resolve Git top-level for stage-neutral release materialization.'
}
if (
    -not [string]::Equals(
        [System.IO.Path]::GetFullPath($gitTopLevel),
        $repoRoot,
        [System.StringComparison]::OrdinalIgnoreCase
    )
) {
    throw 'Stage-neutral release Git top-level does not match repository root.'
}
$replacementRefs = @(
    & $gitExecutable -C $repoRoot for-each-ref '--format=%(refname)' refs/replace |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
)
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to enumerate Git replacement refs for stage-neutral release materialization.'
}
if ($replacementRefs.Count -ne 0) {
    throw 'Stage-neutral release repository contains Git replacement refs.'
}

function Get-ExactGitBlobBytes {
    param(
        [string]$RepoPath
    )

    $treeEntry = (
        & $gitExecutable -C $repoRoot ls-tree $SourceSha -- $RepoPath 2>&1 |
            Out-String
    ).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to resolve exact Git object for $RepoPath"
    }
    $escapedPath = [regex]::Escape($RepoPath)
    $match = [regex]::Match(
        $treeEntry,
        "^(100644|100755) blob ([0-9a-f]{40})`t$escapedPath$"
    )
    if (-not $match.Success) {
        throw "Exact source does not contain one regular blob for $RepoPath"
    }
    $blobSha = $match.Groups[2].Value

    $info = [System.Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $gitExecutable
    $info.UseShellExecute = $false
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.CreateNoWindow = $true
    [void]$info.ArgumentList.Add('-C')
    [void]$info.ArgumentList.Add($repoRoot)
    [void]$info.ArgumentList.Add('cat-file')
    [void]$info.ArgumentList.Add('blob')
    [void]$info.ArgumentList.Add($blobSha)

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $info
    $buffer = [System.IO.MemoryStream]::new()
    try {
        if (-not $process.Start()) {
            throw "Unable to start exact Git blob read for $RepoPath"
        }
        $process.StandardOutput.BaseStream.CopyTo($buffer)
        $message = $process.StandardError.ReadToEnd().Trim()
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) {
            throw "Unable to read exact Git blob for $RepoPath (git exit $($process.ExitCode)): $message"
        }
        $bytes = $buffer.ToArray()
    } finally {
        $buffer.Dispose()
        $process.Dispose()
    }
    if ($bytes.Length -eq 0) {
        throw "Exact Git blob is empty for $RepoPath"
    }
    return ,$bytes
}

New-Item -ItemType Directory -Path $distRoot -Force | Out-Null
$trustedSourceRoot = Join-Path (
    [System.IO.Path]::GetTempPath()
) ("autosport-stage-neutral-source-" + [guid]::NewGuid().ToString('N'))
$trustedPackageRoot = Join-Path $trustedSourceRoot 'autosport'
New-Item -ItemType Directory -Path $trustedPackageRoot -Force | Out-Null

try {
    $trustedSources = [ordered]@{
        'src/autosport/stage_neutral_release.py' = 'stage_neutral_release.py'
        'src/autosport/release_package.py' = 'release_package.py'
    }
    foreach ($entry in $trustedSources.GetEnumerator()) {
        [byte[]]$payload = Get-ExactGitBlobBytes -RepoPath ([string]$entry.Key)
        [System.IO.File]::WriteAllBytes(
            (Join-Path $trustedPackageRoot ([string]$entry.Value)),
            $payload
        )
    }

    $stageNeutralLauncher = @'
import importlib.util
import pathlib
import sys
import types

package_root = pathlib.Path(sys.argv[1]).resolve(strict=True)
package = types.ModuleType("autosport")
package.__package__ = "autosport"
package.__path__ = [str(package_root)]
sys.modules["autosport"] = package

module_path = package_root / "stage_neutral_release.py"
spec = importlib.util.spec_from_file_location(
    "autosport.stage_neutral_release",
    module_path,
)
if spec is None or spec.loader is None:
    raise SystemExit("unable to load exact stage-neutral release module")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

raise SystemExit(
    module.main(
        [
            "--input",
            sys.argv[2],
            "--output",
            sys.argv[3],
            "--source-sha",
            sys.argv[4],
            "--evidence",
            sys.argv[5],
        ]
    )
)
'@

    & $pythonExecutable -I -S -B -c $stageNeutralLauncher `
        $trustedPackageRoot `
        $legacyPackage `
        $stageNeutralPackage `
        $SourceSha `
        $evidencePath
    if ($LASTEXITCODE -ne 0) {
        throw "Stage-neutral release repack exited with code $LASTEXITCODE"
    }
} finally {
    if (Test-Path -LiteralPath $trustedSourceRoot) {
        Remove-Item -LiteralPath $trustedSourceRoot -Recurse -Force
    }
}

foreach ($required in @($stageNeutralPackage, $evidencePath)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Stage-neutral release materialization is missing required output: $required"
    }
}

$evidence = Get-Content -LiteralPath $evidencePath -Raw | ConvertFrom-Json
if ($evidence.status -ne 'PASS') {
    throw 'Stage-neutral release evidence did not record PASS.'
}
if ([string]$evidence.source_sha -ne [string]$SourceSha) {
    throw 'Stage-neutral release evidence source_sha does not match requested source SHA.'
}
if ([string]$evidence.archive_name -ne 'Autosport-windows-x64.zip') {
    throw 'Stage-neutral release evidence archive identity drifted.'
}
if ([string]$evidence.package_prefix -ne 'Autosport/') {
    throw 'Stage-neutral release evidence package prefix drifted.'
}
if ([string]$evidence.build_version -ne '0.1.0-prehuman') {
    throw 'Stage-neutral release evidence build version drifted.'
}
if (
    $evidence.real_money_execution -ne $false -or
    $evidence.human_tested -ne $false -or
    $evidence.nvda_verified -ne $false -or
    $evidence.whole_product_complete -ne $false
) {
    throw 'Stage-neutral release evidence violated required false product-truth labels.'
}

$packageSha = (Get-FileHash -LiteralPath $stageNeutralPackage -Algorithm SHA256).Hash.ToLowerInvariant()
if ([string]$evidence.package_sha256 -ne $packageSha) {
    throw 'Stage-neutral release evidence package digest does not match authored ZIP.'
}

Write-Output "stage-neutral-release=PASS source_sha=$SourceSha package_sha256=$packageSha"
