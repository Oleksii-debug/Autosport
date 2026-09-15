$ErrorActionPreference = 'Stop'

function Assert-ProcessRecoveryEvidence {
  param(
    [Parameter(Mandatory = $true)] $Evidence,
    [Parameter(Mandatory = $true)] [string] $Label
  )

  if ($Evidence.process_kill_relaunch_status -ne 'PASS') {
    throw "$Label did not prove real process kill/relaunch"
  }
  if ($null -eq $Evidence.process_kill_stage_pid -or [long]$Evidence.process_kill_stage_pid -le 0) {
    throw "$Label has invalid process_kill_stage_pid"
  }
  if ($null -eq $Evidence.process_recovery_pid -or [long]$Evidence.process_recovery_pid -le 0) {
    throw "$Label has invalid process_recovery_pid"
  }
  if ([long]$Evidence.process_kill_stage_pid -eq [long]$Evidence.process_recovery_pid) {
    throw "$Label did not prove a distinct fresh recovery process"
  }
  if ($null -eq $Evidence.process_kill_return_code -or [long]$Evidence.process_kill_return_code -eq 0) {
    throw "$Label did not prove non-clean process termination"
  }
  if ([string]::IsNullOrWhiteSpace([string]$Evidence.process_recovery_run_id)) {
    throw "$Label has invalid process_recovery_run_id"
  }
  if ($Evidence.process_recovery_disposition -ne 'committed') {
    throw "$Label did not prove process_recovery_disposition=committed"
  }
  if ($Evidence.process_recovery_registry_status -ne 'completed') {
    throw "$Label did not prove process_recovery_registry_status=completed"
  }
  if ($Evidence.process_recovery_manifest_phase -ne 'completed') {
    throw "$Label did not prove process_recovery_manifest_phase=completed"
  }

  $hashFields = @(
    'process_recovery_base_paper_book_sha256',
    'process_recovery_base_decision_ledger_sha256',
    'process_recovery_new_paper_book_sha256',
    'process_recovery_new_decision_ledger_sha256'
  )
  foreach ($field in $hashFields) {
    $value = [string]$Evidence.$field
    if ($value -notmatch '^[0-9a-f]{64}$') {
      throw "$Label has invalid $field"
    }
  }
  if ($Evidence.process_recovery_base_paper_book_sha256 -eq $Evidence.process_recovery_new_paper_book_sha256) {
    throw "$Label did not prove promoted PaperBook state"
  }
  if ($Evidence.process_recovery_base_decision_ledger_sha256 -eq $Evidence.process_recovery_new_decision_ledger_sha256) {
    throw "$Label did not prove promoted Decision Ledger state"
  }
}

$pythonCommands = @(Get-Command python -CommandType Application -ErrorAction Stop)
if ($pythonCommands.Count -lt 1) { throw 'Unable to resolve Python application' }
$pythonExecutable = [string]$pythonCommands[0].Source
if ([string]::IsNullOrWhiteSpace($pythonExecutable)) { throw 'Resolved Python application has an empty source path' }
$trustedVerifierLauncher = @'
import hashlib
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
expected = sys.argv[2]
data = path.read_bytes()
actual = hashlib.sha256(data).hexdigest()
if actual != expected:
    raise SystemExit(f"trusted verifier SHA-256 mismatch: expected {expected}, got {actual}")
sys.argv = [str(path), *sys.argv[3:]]
namespace = {
    "__name__": "__main__",
    "__file__": str(path),
    "__package__": None,
    "__cached__": None,
}
exec(compile(data, str(path), "exec"), namespace)
'@
$trustedGitSourceOracleLauncher = @'
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys

git_executable = sys.argv[1]
repo_root = pathlib.Path(sys.argv[2])
source_sha = sys.argv[3]
requested = json.loads(sys.argv[4])
if re.fullmatch(r"[0-9a-f]{40}", source_sha) is None:
    raise SystemExit("source_sha is not a canonical Git commit SHA")
if requested is not None and (
    not isinstance(requested, list)
    or not all(isinstance(item, str) and item for item in requested)
    or len(set(requested)) != len(requested)
):
    raise SystemExit("requested source paths must be null or a unique non-empty string list")

env = os.environ.copy()
for name in tuple(env):
    if name.upper().startswith("GIT_"):
        env.pop(name, None)
env["GIT_NO_REPLACE_OBJECTS"] = "1"

def git_bytes(*args):
    try:
        completed = subprocess.run(
            [git_executable, *args],
            cwd=repo_root,
            env=env,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"exact Git source oracle failed: git {' '.join(args)}") from exc
    return completed.stdout

def blob_sha1(data):
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data, usedforsecurity=False).hexdigest()

tree = git_bytes("ls-tree", "-r", "-z", "--full-tree", source_sha)
entries = {}
for record in tree.split(b"\0"):
    if not record:
        continue
    try:
        metadata, path_bytes = record.split(b"\t", 1)
        mode, object_type, object_sha = metadata.split(b" ", 2)
        path = path_bytes.decode("utf-8")
        object_sha_text = object_sha.decode("ascii")
    except (ValueError, UnicodeError) as exc:
        raise SystemExit("unable to parse exact Git source tree") from exc
    if object_type != b"blob" or mode not in {b"100644", b"100755"}:
        raise SystemExit(f"unsupported exact Git source entry: {path}")
    entries[path] = object_sha_text

selected = sorted(entries) if requested is None else sorted(requested)
manifest = {}
for relative in selected:
    pure = pathlib.PurePosixPath(relative)
    if pure.is_absolute() or pure.as_posix() != relative or any(part in {"", ".", ".."} for part in pure.parts):
        raise SystemExit(f"non-canonical exact Git source path: {relative}")
    object_sha = entries.get(relative)
    if object_sha is None:
        raise SystemExit(f"exact Git source is missing required path: {relative}")
    data = git_bytes("cat-file", "blob", object_sha)
    if blob_sha1(data) != object_sha:
        raise SystemExit(f"Git blob bytes do not match object identity: {relative}")
    manifest[relative] = hashlib.sha256(data).hexdigest()

print(json.dumps(manifest, sort_keys=True, separators=(",", ":")))
'@
$trustedSourceSnapshotVerifierLauncher = @'
import hashlib
import json
import os
import pathlib
import re
import stat
import sys

root = pathlib.Path(sys.argv[1])
try:
    manifest = json.loads(sys.stdin.read())
except json.JSONDecodeError as exc:
    raise SystemExit("source snapshot manifest is not valid JSON") from exc
if not isinstance(manifest, dict) or not manifest:
    raise SystemExit("source snapshot manifest must be a non-empty object")

def identity(value):
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
    )

def require_directory(path, label):
    try:
        value = path.lstat()
    except OSError as exc:
        raise SystemExit(f"source snapshot {label} is not readable: {path}") from exc
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
        raise SystemExit(f"source snapshot {label} must be a real directory: {path}")

require_directory(root, "root")
expected_paths = set()
for relative, expected in sorted(manifest.items()):
    if not isinstance(relative, str) or not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise SystemExit("source snapshot manifest contains a non-canonical entry")
    pure = pathlib.PurePosixPath(relative)
    if pure.is_absolute() or pure.as_posix() != relative or any(part in {"", ".", ".."} for part in pure.parts):
        raise SystemExit(f"source snapshot manifest contains non-canonical path: {relative}")
    expected_paths.add(relative)

actual_paths = set()
for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
    directory_path = pathlib.Path(directory)
    require_directory(directory_path, "directory")
    for dirname in dirnames:
        require_directory(directory_path / dirname, "directory")
    for filename in filenames:
        path = directory_path / filename
        try:
            value = path.lstat()
        except OSError as exc:
            raise SystemExit(f"source snapshot member is not readable: {path}") from exc
        if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
            raise SystemExit(f"source snapshot member must be a regular file: {path}")
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError as exc:
            raise SystemExit(f"source snapshot member escaped root: {path}") from exc
        actual_paths.add(relative)

if actual_paths != expected_paths:
    missing = sorted(expected_paths - actual_paths)
    unexpected = sorted(actual_paths - expected_paths)
    raise SystemExit(
        "source snapshot membership mismatch: "
        f"missing={missing!r}, unexpected={unexpected!r}"
    )

for relative, expected in sorted(manifest.items()):
    pure = pathlib.PurePosixPath(relative)
    parent = root
    for part in pure.parts[:-1]:
        parent = parent / part
        require_directory(parent, "parent")
    path = parent / pure.name
    try:
        before = path.lstat()
    except OSError as exc:
        raise SystemExit(f"source snapshot is missing required file: {relative}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise SystemExit(f"source snapshot required path is not a regular file: {relative}")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or identity(opened) != identity(before):
                raise SystemExit(f"source snapshot changed before capture: {relative}")
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            after_handle = os.fstat(handle.fileno())
            if identity(after_handle) != identity(opened):
                raise SystemExit(f"source snapshot changed during capture: {relative}")
    except OSError as exc:
        raise SystemExit(f"source snapshot required file is unreadable: {relative}") from exc
    try:
        after_path = path.lstat()
    except OSError as exc:
        raise SystemExit(f"source snapshot disappeared during capture: {relative}") from exc
    if stat.S_ISLNK(after_path.st_mode) or not stat.S_ISREG(after_path.st_mode) or identity(after_path) != identity(before):
        raise SystemExit(f"source snapshot was replaced during capture: {relative}")
    actual = digest.hexdigest()
    if actual != expected:
        raise SystemExit(
            f"source snapshot SHA-256 mismatch for {relative}: expected {expected}, got {actual}"
        )

print("SOURCE_SNAPSHOT=PASS")
'@
$trustedPackageLauncher = @'
import hashlib
import json
import pathlib
import sys
import types

root = pathlib.Path(sys.argv[1])
manifest = json.loads(sys.argv[2])
required = {
    "scripts/package_windows.py",
    "src/autosport/release_package.py",
    "src/autosport/data_tool_package.py",
}
if set(manifest) != required:
    raise SystemExit("trusted package source manifest membership mismatch")

payloads = {}
for relative in sorted(required):
    path = root.joinpath(*relative.split("/"))
    data = path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    expected = manifest[relative]
    if actual != expected:
        raise SystemExit(
            f"trusted package source SHA-256 mismatch for {relative}: "
            f"expected {expected}, got {actual}"
        )
    payloads[relative] = (path, data)

package = types.ModuleType("autosport")
package.__package__ = "autosport"
package.__path__ = []
package.__file__ = "<trusted-package-source>"
sys.modules["autosport"] = package

def load_module(name, relative):
    path, data = payloads[relative]
    module = types.ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = "autosport"
    module.__cached__ = None
    sys.modules[name] = module
    exec(compile(data, str(path), "exec"), module.__dict__)
    setattr(package, name.rsplit(".", 1)[1], module)
    return module

load_module("autosport.release_package", "src/autosport/release_package.py")
load_module("autosport.data_tool_package", "src/autosport/data_tool_package.py")
script_path, script_data = payloads["scripts/package_windows.py"]
sys.argv = [str(script_path), *sys.argv[3:]]
namespace = {
    "__name__": "__main__",
    "__file__": str(script_path),
    "__package__": None,
    "__cached__": None,
}
exec(compile(script_data, str(script_path), "exec"), namespace)
'@

function python {
  $pythonArguments = @($args)
  if (
    $null -ne $script:sourceVerifier -and
    $null -ne $script:sourceVerifierSha256 -and
    $pythonArguments.Count -gt 0 -and
    [string]$pythonArguments[0] -eq [string]$script:sourceVerifier
  ) {
    $remaining = @()
    if ($pythonArguments.Count -gt 1) {
      $remaining = @($pythonArguments[1..($pythonArguments.Count - 1)])
    }
    & $script:pythonExecutable -I -S -c $script:trustedVerifierLauncher $script:sourceVerifier $script:sourceVerifierSha256 @remaining
    return
  }
  if (
    $null -ne $script:trustedPackageRoot -and
    $null -ne $script:trustedPackageManifestJson -and
    $pythonArguments.Count -gt 0 -and
    [string]$pythonArguments[0] -eq 'scripts/package_windows.py'
  ) {
    $remaining = @()
    if ($pythonArguments.Count -gt 1) {
      $remaining = @($pythonArguments[1..($pythonArguments.Count - 1)])
    }
    & $script:pythonExecutable -I -S -c $script:trustedPackageLauncher $script:trustedPackageRoot $script:trustedPackageManifestJson @remaining
    return
  }
  & $script:pythonExecutable @pythonArguments
}

# Bootstrap the verifier from the exact Git object before executing any
# repository-local Python. Do not inherit repository-shaping Git environment.
Get-ChildItem Env: | Where-Object { $_.Name -like 'GIT_*' } | ForEach-Object {
  Remove-Item -LiteralPath ("Env:" + $_.Name) -ErrorAction SilentlyContinue
}
$env:GIT_NO_REPLACE_OBJECTS = '1'
$gitCommands = @(Get-Command git -CommandType Application -ErrorAction Stop)
if ($gitCommands.Count -lt 1) { throw 'Unable to resolve Git application' }
$gitExecutable = [string]$gitCommands[0].Source
if ([string]::IsNullOrWhiteSpace($gitExecutable)) { throw 'Resolved Git application has an empty source path' }

$checkoutHead = (& $gitExecutable rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw "Unable to resolve checkout HEAD; git exited $LASTEXITCODE" }
$sourceSha = $env:AUTOSPORT_SOURCE_SHA
if ([string]::IsNullOrWhiteSpace($sourceSha)) { $sourceSha = $checkoutHead }
if ($sourceSha -notmatch '^[0-9a-f]{40}$') { throw 'AUTOSPORT_SOURCE_SHA/source HEAD is not a canonical Git commit SHA' }
if ($sourceSha -ne $checkoutHead) { throw 'Exact source SHA does not match checkout HEAD' }

$repoRoot = [System.IO.Path]::GetFullPath($PWD.Path)
$gitTopLevel = [System.IO.Path]::GetFullPath((& $gitExecutable rev-parse --show-toplevel).Trim())
if ($LASTEXITCODE -ne 0) { throw "Unable to resolve Git top-level; git exited $LASTEXITCODE" }
if (-not [string]::Equals($repoRoot, $gitTopLevel, [System.StringComparison]::OrdinalIgnoreCase)) {
  throw 'Release build Git top-level does not match the current repository root'
}
$replacementRefs = @(& $gitExecutable for-each-ref '--format=%(refname)' refs/replace | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($LASTEXITCODE -ne 0) { throw "Unable to enumerate Git replacement refs; git exited $LASTEXITCODE" }
if ($replacementRefs.Count -ne 0) { throw 'Release build repository contains Git replacement refs' }

$verifierTreeEntry = (& $gitExecutable ls-tree $sourceSha -- 'scripts/verify_source_checkout.py').Trim()
if ($LASTEXITCODE -ne 0) { throw "Unable to resolve trusted verifier source entry; git exited $LASTEXITCODE" }
$verifierMatch = [regex]::Match($verifierTreeEntry, '^(100644|100755) blob ([0-9a-f]{40})\tscripts/verify_source_checkout\.py$')
if (-not $verifierMatch.Success) { throw 'Exact source does not contain one regular trusted verifier blob' }
$verifierBlobSha = $verifierMatch.Groups[2].Value

# Read the exact Git blob into process memory and bind its digest before any
# filesystem pathname for the verifier exists. A concurrent replacement of the
# later temp file can therefore only make the isolated launcher reject it; it
# cannot redefine the expected digest.
$verifierBootstrapInfo = [System.Diagnostics.ProcessStartInfo]::new()
$verifierBootstrapInfo.FileName = $gitExecutable
$verifierBootstrapInfo.UseShellExecute = $false
$verifierBootstrapInfo.RedirectStandardOutput = $true
$verifierBootstrapInfo.RedirectStandardError = $true
$verifierBootstrapInfo.CreateNoWindow = $true
[void]$verifierBootstrapInfo.ArgumentList.Add('cat-file')
[void]$verifierBootstrapInfo.ArgumentList.Add('blob')
[void]$verifierBootstrapInfo.ArgumentList.Add($verifierBlobSha)
$verifierBootstrap = [System.Diagnostics.Process]::new()
$verifierBootstrap.StartInfo = $verifierBootstrapInfo
$verifierBuffer = [System.IO.MemoryStream]::new()
try {
  if (-not $verifierBootstrap.Start()) { throw 'Unable to start exact trusted verifier bootstrap' }
  $verifierBootstrap.StandardOutput.BaseStream.CopyTo($verifierBuffer)
  $bootstrapMessage = $verifierBootstrap.StandardError.ReadToEnd().Trim()
  $verifierBootstrap.WaitForExit()
  if ($verifierBootstrap.ExitCode -ne 0) {
    throw "Unable to materialize exact trusted verifier blob (git exit $($verifierBootstrap.ExitCode)): $bootstrapMessage"
  }
  $verifierBytes = $verifierBuffer.ToArray()
} finally {
  $verifierBuffer.Dispose()
  $verifierBootstrap.Dispose()
}
if ($verifierBytes.Length -eq 0) { throw 'Exact trusted verifier blob is empty' }
$verifierHasher = [System.Security.Cryptography.SHA256]::Create()
try {
  $sourceVerifierSha256 = ([System.BitConverter]::ToString($verifierHasher.ComputeHash($verifierBytes))).Replace('-', '').ToLowerInvariant()
} finally {
  $verifierHasher.Dispose()
}
$sourceVerifier = (New-TemporaryFile).FullName
[System.IO.File]::WriteAllBytes($sourceVerifier, $verifierBytes)
python $sourceVerifier --source-sha $sourceSha
if ($LASTEXITCODE -ne 0) { throw "Source checkout preflight exited $LASTEXITCODE" }
# Historical unsafe regression marker; this command must remain comment-only: Copy-Item -LiteralPath 'scripts/verify_source_checkout.py' -Destination $sourceVerifier -Force
$boundArtifactRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("autosport-release-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $boundArtifactRoot | Out-Null
$boundAutosportExe = Join-Path $boundArtifactRoot 'Autosport.exe'
$boundDataExe = Join-Path $boundArtifactRoot 'Autosport-Data.exe'
$boundDiagnostic = Join-Path $boundArtifactRoot 'packaged-diagnostic.json'
$boundAccessibilityAudit = Join-Path $boundArtifactRoot 'accessibility-audit.json'
$boundKeyboardAudit = Join-Path $boundArtifactRoot 'keyboard-audit.json'
$boundRestartRecoveryAudit = Join-Path $boundArtifactRoot 'restart-recovery-audit.json'
$autosportDigestPath = Join-Path $boundArtifactRoot 'Autosport.sha256'
$dataDigestPath = Join-Path $boundArtifactRoot 'Autosport-Data.sha256'
$diagnosticDigestPath = Join-Path $boundArtifactRoot 'packaged-diagnostic.sha256'
$accessibilityDigestPath = Join-Path $boundArtifactRoot 'accessibility-audit.sha256'
$keyboardDigestPath = Join-Path $boundArtifactRoot 'keyboard-audit.sha256'
$restartRecoveryDigestPath = Join-Path $boundArtifactRoot 'restart-recovery-audit.sha256'
$env:PYTHONDONTWRITEBYTECODE = '1'
python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade exited $LASTEXITCODE" }
python -m pip install -e '.[build,test]'
if ($LASTEXITCODE -ne 0) { throw "build/test dependency install exited $LASTEXITCODE" }
python -m pytest -v tests
if ($LASTEXITCODE -ne 0) { throw "Full pytest gate exited $LASTEXITCODE" }
if (Test-Path '.build-smoke-workspace') { Remove-Item -Recurse -Force '.build-smoke-workspace' }
python -m autosport dataset examples/tt_demo --workspace .build-smoke-workspace
if ($LASTEXITCODE -ne 0) { throw "Demo dataset smoke exited $LASTEXITCODE" }
if (Test-Path '.build-smoke-workspace') { Remove-Item -Recurse -Force '.build-smoke-workspace' }
python $sourceVerifier --source-sha $sourceSha --late-build-boundary
if ($LASTEXITCODE -ne 0) { throw "Trusted source gate before Autosport.exe exited $LASTEXITCODE" }

# Derive the expected snapshot identity directly from exact Git blob objects
# before archive extraction. This is deliberately independent of git archive and
# its attribute processing, so host-local export-ignore/export-subst cannot
# redefine the trusted bytes after source proof.
$trustedBuildManifestLines = @(& $pythonExecutable -I -S -c $trustedGitSourceOracleLauncher $gitExecutable $repoRoot $sourceSha 'null')
if ($LASTEXITCODE -ne 0) { throw "Exact build source Git oracle exited $LASTEXITCODE" }
if ($trustedBuildManifestLines.Count -ne 1) { throw 'Exact build source Git oracle did not emit one canonical manifest' }
$trustedBuildManifestJson = [string]$trustedBuildManifestLines[0]

# Freeze every PyInstaller source/module input to exact source_sha bytes outside
# the mutable checkout. The independent blob oracle is paired with an OS write
# fence plus retained read handles before the final verification. Directory
# membership and every manifest file therefore stay immutable across both
# PyInstaller consumers, including against writers opened before the ACL fence.
$trustedBuildArchive = Join-Path $boundArtifactRoot 'trusted-build-source.zip'
$trustedBuildRoot = Join-Path $boundArtifactRoot 'trusted-build-source'
& $gitExecutable archive --format=zip "--output=$trustedBuildArchive" $sourceSha
if ($LASTEXITCODE -ne 0) { throw "Exact build source archive exited $LASTEXITCODE" }
if (Test-Path $trustedBuildRoot) { Remove-Item -LiteralPath $trustedBuildRoot -Recurse -Force }
New-Item -ItemType Directory -Path $trustedBuildRoot | Out-Null
Expand-Archive -LiteralPath $trustedBuildArchive -DestinationPath $trustedBuildRoot -Force
foreach ($requiredBuildSource in @('pyproject.toml', 'src/autosport/windows_entry.py', 'src/autosport/data_tools_entry.py')) {
  $trustedBuildSource = Join-Path $trustedBuildRoot ($requiredBuildSource.Replace('/', [System.IO.Path]::DirectorySeparatorChar))
  if (-not (Test-Path -LiteralPath $trustedBuildSource -PathType Leaf)) {
    throw "Exact build source snapshot is missing $requiredBuildSource"
  }
}

# Dependencies were installed before the final source gate. Do not reinstall the
# project from a writable snapshot after that gate. PyInstaller receives the
# exact source tree explicitly through --paths and absolute entry paths.
$trustedBuildSrc = Join-Path $trustedBuildRoot 'src'
$trustedGuiEntry = Join-Path $trustedBuildRoot 'src/autosport/windows_entry.py'
$trustedDataEntry = Join-Path $trustedBuildRoot 'src/autosport/data_tools_entry.py'
$pyInstallerOutputRoot = Join-Path $boundArtifactRoot 'pyinstaller-output'
$pyInstallerDist = Join-Path $pyInstallerOutputRoot 'dist'
$pyInstallerWork = Join-Path $pyInstallerOutputRoot 'build'
$pyInstallerSpec = Join-Path $pyInstallerOutputRoot 'spec'
New-Item -ItemType Directory -Path $pyInstallerDist -Force | Out-Null
New-Item -ItemType Directory -Path $pyInstallerWork -Force | Out-Null
New-Item -ItemType Directory -Path $pyInstallerSpec -Force | Out-Null
$currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
if ([string]::IsNullOrWhiteSpace($currentSid) -or $currentSid -notmatch '^S-') {
  throw 'Unable to resolve current Windows security identifier for trusted source fence'
}

$trustedBuildProtected = $false
$trustedBuildReadLocks = [System.Collections.Generic.List[System.IO.FileStream]]::new()
try {
  & icacls $trustedBuildRoot /deny "*${currentSid}:(OI)(CI)(WD,AD,WEA,WA,DE,DC)" /T /C | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "Trusted build source write fence exited $LASTEXITCODE" }
  $trustedBuildProtected = $true

  # A deny ACE cannot revoke a writer that already had the file open. Acquire
  # read-only handles that share only reads for every exact-manifest file before
  # final verification. Windows share-access compatibility makes any pre-existing
  # writer/delete handle fail this acquisition closed, and the retained handles
  # then prevent new writer/delete opens until both PyInstaller consumers finish.
  $trustedBuildManifest = $trustedBuildManifestJson | ConvertFrom-Json
  $trustedBuildManifestNames = @($trustedBuildManifest.PSObject.Properties.Name | Sort-Object)
  if ($trustedBuildManifestNames.Count -eq 0) {
    throw 'Exact build source manifest contains no files to lock'
  }
  foreach ($relativeSourcePath in $trustedBuildManifestNames) {
    $trustedBuildLockPath = Join-Path $trustedBuildRoot ($relativeSourcePath.Replace('/', [System.IO.Path]::DirectorySeparatorChar))
    try {
      $lockStream = [System.IO.File]::Open(
        $trustedBuildLockPath,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::Read
      )
    } catch {
      throw "Unable to acquire trusted build source read lock for ${relativeSourcePath}: $($_.Exception.Message)"
    }
    [void]$trustedBuildReadLocks.Add($lockStream)
  }

  # Verify only after the deny ACE and retained per-file handles are fully applied.
  # Any replacement/addition that raced extraction or locking is detected before
  # consumption; later same-user write/delete/create attempts remain denied.
  $trustedBuildManifestJson | & $pythonExecutable -I -S -c $trustedSourceSnapshotVerifierLauncher $trustedBuildRoot
  if ($LASTEXITCODE -ne 0) { throw "Locked exact build source snapshot verification before Autosport.exe exited $LASTEXITCODE" }

  & $pythonExecutable -I -m PyInstaller --noconfirm --clean --onefile --windowed --paths $trustedBuildSrc --distpath $pyInstallerDist --workpath $pyInstallerWork --specpath $pyInstallerSpec --name Autosport $trustedGuiEntry
  if ($LASTEXITCODE -ne 0) { throw "Autosport PyInstaller exited $LASTEXITCODE" }
  $builtAutosportExe = Join-Path $pyInstallerDist 'Autosport.exe'
  python $sourceVerifier --bind-artifact $builtAutosportExe --bound-output $boundAutosportExe --digest-output $autosportDigestPath
  if ($LASTEXITCODE -ne 0) { throw "Autosport.exe artifact binding exited $LASTEXITCODE" }
  $autosportExeSha256 = (Get-Content -LiteralPath $autosportDigestPath -Raw).Trim()

  $trustedBuildManifestJson | & $pythonExecutable -I -S -c $trustedSourceSnapshotVerifierLauncher $trustedBuildRoot
  if ($LASTEXITCODE -ne 0) { throw "Locked exact build source snapshot verification after Autosport.exe exited $LASTEXITCODE" }

  python $sourceVerifier --source-sha $sourceSha --late-build-boundary --allow-release-outputs
  if ($LASTEXITCODE -ne 0) { throw "Trusted source gate before Autosport-Data.exe exited $LASTEXITCODE" }
  $trustedBuildManifestJson | & $pythonExecutable -I -S -c $trustedSourceSnapshotVerifierLauncher $trustedBuildRoot
  if ($LASTEXITCODE -ne 0) { throw "Locked exact build source snapshot verification before Autosport-Data.exe exited $LASTEXITCODE" }

  & $pythonExecutable -I -m PyInstaller --noconfirm --clean --onefile --console --paths $trustedBuildSrc --distpath $pyInstallerDist --workpath $pyInstallerWork --specpath $pyInstallerSpec --name Autosport-Data $trustedDataEntry
  if ($LASTEXITCODE -ne 0) { throw "Autosport-Data PyInstaller exited $LASTEXITCODE" }
  $builtDataExe = Join-Path $pyInstallerDist 'Autosport-Data.exe'
  python $sourceVerifier --bind-artifact $builtDataExe --bound-output $boundDataExe --digest-output $dataDigestPath
  if ($LASTEXITCODE -ne 0) { throw "Autosport-Data.exe artifact binding exited $LASTEXITCODE" }
  $dataExeSha256 = (Get-Content -LiteralPath $dataDigestPath -Raw).Trim()

  $trustedBuildManifestJson | & $pythonExecutable -I -S -c $trustedSourceSnapshotVerifierLauncher $trustedBuildRoot
  if ($LASTEXITCODE -ne 0) { throw "Locked exact build source snapshot verification after Autosport-Data.exe exited $LASTEXITCODE" }
} finally {
  try {
    for ($lockIndex = $trustedBuildReadLocks.Count - 1; $lockIndex -ge 0; $lockIndex--) {
      $trustedBuildReadLocks[$lockIndex].Dispose()
    }
  } finally {
    if ($trustedBuildProtected) {
      & icacls $trustedBuildRoot /remove:d "*${currentSid}" /T /C | Out-Null
      if ($LASTEXITCODE -ne 0) { throw "Trusted build source write-fence cleanup exited $LASTEXITCODE" }
    }
  }
}

$releaseDist = Join-Path $repoRoot 'dist'
if (-not (Test-Path -LiteralPath $releaseDist -PathType Container)) {
  New-Item -ItemType Directory -Path $releaseDist | Out-Null
}
$diag = Join-Path $PWD 'dist/packaged-diagnostic.json'
if (Test-Path $diag) { Remove-Item -Force $diag }
$process = Start-Process -FilePath $boundAutosportExe -ArgumentList '--diagnostic-output', $diag -Wait -PassThru
if ($process.ExitCode -ne 0) { throw "Packaged Autosport.exe diagnostic exited $($process.ExitCode)" }
python $sourceVerifier --bind-artifact $diag --bound-output $boundDiagnostic --digest-output $diagnosticDigestPath
if ($LASTEXITCODE -ne 0) { throw "Packaged diagnostic evidence binding exited $LASTEXITCODE" }
$diagnosticSha256 = (Get-Content -LiteralPath $diagnosticDigestPath -Raw).Trim()
$diagnostic = Get-Content $boundDiagnostic -Raw | ConvertFrom-Json
if ($diagnostic.status -ne 'PASS') { throw 'Packaged Autosport.exe diagnostic did not PASS' }

$a11y = Join-Path $PWD 'dist/accessibility-audit.json'
if (Test-Path $a11y) { Remove-Item -Force $a11y }
$a11yProcess = Start-Process -FilePath $boundAutosportExe -ArgumentList '--accessibility-audit-output', $a11y -Wait -PassThru
if ($a11yProcess.ExitCode -ne 0) { throw "Packaged Autosport.exe accessibility audit exited $($a11yProcess.ExitCode)" }
python $sourceVerifier --bind-artifact $a11y --bound-output $boundAccessibilityAudit --digest-output $accessibilityDigestPath
if ($LASTEXITCODE -ne 0) { throw "Accessibility evidence binding exited $LASTEXITCODE" }
$accessibilitySha256 = (Get-Content -LiteralPath $accessibilityDigestPath -Raw).Trim()
$accessibility = Get-Content $boundAccessibilityAudit -Raw | ConvertFrom-Json
if ($accessibility.status -ne 'PASS') { throw 'Packaged accessibility audit did not PASS' }
if ($accessibility.nvda_verified -ne $false) { throw 'Machine accessibility audit must not claim NVDA verification' }

$keyboard = Join-Path $PWD 'dist/keyboard-audit.json'
if (Test-Path $keyboard) { Remove-Item -Force $keyboard }
$keyboardProcess = Start-Process -FilePath $boundAutosportExe -ArgumentList '--keyboard-audit-output', $keyboard -Wait -PassThru
if ($keyboardProcess.ExitCode -ne 0) { throw "Packaged keyboard audit exited $($keyboardProcess.ExitCode)" }
python $sourceVerifier --bind-artifact $keyboard --bound-output $boundKeyboardAudit --digest-output $keyboardDigestPath
if ($LASTEXITCODE -ne 0) { throw "Keyboard evidence binding exited $LASTEXITCODE" }
$keyboardSha256 = (Get-Content -LiteralPath $keyboardDigestPath -Raw).Trim()
$keyboardEvidence = Get-Content $boundKeyboardAudit -Raw | ConvertFrom-Json
if ($keyboardEvidence.status -ne 'PASS') { throw 'Packaged keyboard audit did not PASS' }
if ($keyboardEvidence.human_tested -ne $false -or $keyboardEvidence.nvda_verified -ne $false) {
  throw 'Machine keyboard audit must not claim physical human/NVDA verification'
}

$restartRecovery = Join-Path $PWD 'dist/restart-recovery-audit.json'
if (Test-Path $restartRecovery) { Remove-Item -Force $restartRecovery }
$restartRecoveryProcess = Start-Process -FilePath $boundAutosportExe -ArgumentList '--restart-recovery-audit-output', $restartRecovery -Wait -PassThru
if ($restartRecoveryProcess.ExitCode -ne 0) { throw "Packaged Autosport.exe restart/recovery audit exited $($restartRecoveryProcess.ExitCode)" }
python $sourceVerifier --bind-artifact $restartRecovery --bound-output $boundRestartRecoveryAudit --digest-output $restartRecoveryDigestPath
if ($LASTEXITCODE -ne 0) { throw "Restart/recovery evidence binding exited $LASTEXITCODE" }
$restartRecoverySha256 = (Get-Content -LiteralPath $restartRecoveryDigestPath -Raw).Trim()
$restartRecoveryEvidence = Get-Content $boundRestartRecoveryAudit -Raw | ConvertFrom-Json
if ($restartRecoveryEvidence.status -ne 'PASS') { throw 'Packaged restart/recovery audit did not PASS' }
if ($restartRecoveryEvidence.session_restart_status -ne 'PASS') { throw 'Packaged restart audit did not prove persistent session reopen' }
if ($restartRecoveryEvidence.transaction_recovery_status -ne 'PASS') { throw 'Packaged recovery audit did not prove transaction recovery' }
if ($restartRecoveryEvidence.recovery_disposition -ne 'aborted_uncommitted') { throw 'Packaged recovery audit disposition is not fail-closed' }
if ($restartRecoveryEvidence.real_money_execution -ne $false -or $restartRecoveryEvidence.human_tested -ne $false -or $restartRecoveryEvidence.nvda_verified -ne $false) {
  throw 'Machine restart/recovery audit violated release truth labels'
}
Assert-ProcessRecoveryEvidence -Evidence $restartRecoveryEvidence -Label 'Packaged restart/recovery audit'

$dataExe = $boundDataExe
if (-not (Test-Path $dataExe -PathType Leaf)) { throw 'Packaged build is missing Autosport-Data.exe' }
& $dataExe --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Packaged Autosport-Data.exe help exited $LASTEXITCODE" }
& $dataExe compare-strategies --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Packaged Autosport-Data.exe compare-strategies --help exited $LASTEXITCODE" }
& $dataExe walk-forward-evaluate --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Packaged Autosport-Data.exe walk-forward-evaluate exited $LASTEXITCODE" }
& $dataExe acquire --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Packaged Autosport-Data.exe acquire --help exited $LASTEXITCODE" }
& $dataExe build-corpus --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Packaged Autosport-Data.exe build-corpus --help exited $LASTEXITCODE" }
& $dataExe build-corpus-from-bundle --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Packaged Autosport-Data.exe build-corpus-from-bundle --help exited $LASTEXITCODE" }
& $dataExe import-betfair-historical --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Packaged Autosport-Data.exe import-betfair-historical --help exited $LASTEXITCODE" }
& $dataExe verify-dataset examples/tt_demo | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Packaged Autosport-Data.exe verify-dataset exited $LASTEXITCODE" }

# Execute the frozen evaluator, not only its help surface. This deterministic
# schema-v1 smoke fixture is sample evidence only and must never be promoted to
# real historical/OOS proof.
$walkForwardBundlePath = Join-Path $PWD 'dist/walk-forward-smoke-bundle.json'
$walkForwardReport = Join-Path $PWD 'dist/walk-forward-smoke-report.json'
$walkForwardBundle = [ordered]@{
  schema_version = 1
  bins = 5
  forecasts = @(
    [ordered]@{
      forecast_id = 'f-1'
      quote_key = 'm1|winner|a'
      probability = '0.70'
      model_id = 'model'
      model_version = '1'
      strategy_version = 'research-v1'
      model_training_cutoff_ts = '2026-01-01T00:00:00+00:00'
      input_cutoff_ts = '2026-02-01T12:00:00+00:00'
      generated_at = '2026-02-01T12:00:00+00:00'
      uncertainty = '0.10'
      evidence_hashes = @()
      market_snapshot_hash = ('a' * 64)
      provenance = [ordered]@{ source = 'packaged-smoke-fixture' }
    },
    [ordered]@{
      forecast_id = 'f-2'
      quote_key = 'm2|winner|b'
      probability = '0.30'
      model_id = 'model'
      model_version = '2'
      strategy_version = 'research-v2'
      model_training_cutoff_ts = '2026-02-10T00:00:00+00:00'
      input_cutoff_ts = '2026-03-01T12:00:00+00:00'
      generated_at = '2026-03-01T12:00:00+00:00'
      uncertainty = '0.20'
      evidence_hashes = @()
      market_snapshot_hash = ('b' * 64)
      provenance = [ordered]@{ source = 'packaged-smoke-fixture' }
    }
  )
  outcomes = @(
    [ordered]@{ forecast_id = 'f-1'; outcome = 1; revealed_at = '2026-02-02T12:00:00+00:00' },
    [ordered]@{ forecast_id = 'f-2'; outcome = 0; revealed_at = '2026-03-02T12:00:00+00:00' }
  )
  windows = @(
    [ordered]@{
      window_id = 'holdout-1'
      training_end_ts = '2026-01-31T23:59:59+00:00'
      evaluation_start_ts = '2026-02-01T00:00:00+00:00'
      evaluation_end_ts = '2026-02-28T23:59:59+00:00'
      split = 'holdout'
    },
    [ordered]@{
      window_id = 'holdout-2'
      training_end_ts = '2026-02-28T23:59:59+00:00'
      evaluation_start_ts = '2026-03-01T12:00:00+00:00'
      generated_at = '2026-03-01T12:00:00+00:00'
      uncertainty = '0.20'
      evidence_hashes = @()
      market_snapshot_hash = ('b' * 64)
      provenance = [ordered]@{ source = 'packaged-smoke-fixture' }
    }
  )
  outcomes = @(
    [ordered]@{ forecast_id = 'f-1'; outcome = 1; revealed_at = '2026-02-02T12:00:00+00:00' },
    [ordered]@{ forecast_id = 'f-2'; outcome = 0; revealed_at = '2026-03-02T12:00:00+00:00' }
  )
  windows = @(
    [ordered]@{
      window_id = 'holdout-1'
      training_end_ts = '2026-01-31T23:59:59+00:00'
      evaluation_start_ts = '2026-02-01T00:00:00+00:00'
      evaluation_end_ts = '2026-02-28T23:59:59+00:00'
      split = 'holdout'
    },
    [ordered]@{
      window_id = 'holdout-2'
      training_end_ts = '2026-02-28T23:59:59+00:00'
      evaluation_start_ts = '2026-03-01T00:00:00+00:00'
      evaluation_end_ts = '2026-03-31T23:59:59+00:00'
      split = 'holdout'
    }
  )
}
$walkForwardJson = $walkForwardBundle | ConvertTo-Json -Depth 8
[System.IO.File]::WriteAllText($walkForwardBundlePath, $walkForwardJson, [System.Text.UTF8Encoding]::new($false))
if (Test-Path $walkForwardReport) { Remove-Item -Force $walkForwardReport }
& $dataExe walk-forward-evaluate $walkForwardBundlePath --output $walkForwardReport | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Packaged Autosport-Data.exe walk-forward-evaluate exited $LASTEXITCODE" }
$walkForwardEvidence = Get-Content $walkForwardReport -Raw | ConvertFrom-Json
if ($walkForwardEvidence.kind -ne 'strict_walk_forward_forecast_evaluation') { throw 'Packaged walk-forward report kind mismatch' }
if ($walkForwardEvidence.evaluated_forecast_count -ne 2) { throw 'Packaged walk-forward report forecast count mismatch' }
if ($walkForwardEvidence.window_count -ne 2) { throw 'Packaged walk-forward report window count mismatch' }
if ([string]::IsNullOrWhiteSpace($walkForwardEvidence.source_sha256) -or $walkForwardEvidence.source_sha256.Length -ne 64) { throw 'Packaged walk-forward report source_sha256 is invalid' }
if ($walkForwardEvidence.profitability_claim -ne $false) { throw 'Packaged walk-forward smoke must not claim profitability' }
if ($walkForwardEvidence.real_money_execution -ne $false) { throw 'Packaged walk-forward smoke must preserve REAL_MONEY_EXECUTION=false' }

$package = Join-Path $PWD 'dist/Autosport-V1-windows-x64.zip'
$packageVerification = Join-Path $PWD 'dist/package-verification.json'
if (Test-Path $package) { Remove-Item -Force $package }
if (Test-Path $packageVerification) { Remove-Item -Force $packageVerification }
python $sourceVerifier --source-sha $sourceSha --late-build-boundary --allow-release-outputs
if ($LASTEXITCODE -ne 0) { throw "Trusted source gate before package assembly exited $LASTEXITCODE" }
python $sourceVerifier --verify-artifact $boundAutosportExe --expected-sha256 $autosportExeSha256
if ($LASTEXITCODE -ne 0) { throw "Bound Autosport.exe verification exited $LASTEXITCODE" }
python $sourceVerifier --verify-artifact $boundDataExe --expected-sha256 $dataExeSha256
if ($LASTEXITCODE -ne 0) { throw "Bound Autosport-Data.exe verification exited $LASTEXITCODE" }
python $sourceVerifier --verify-artifact $boundDiagnostic --expected-sha256 $diagnosticSha256
if ($LASTEXITCODE -ne 0) { throw "Bound diagnostic evidence verification exited $LASTEXITCODE" }
python $sourceVerifier --verify-artifact $boundAccessibilityAudit --expected-sha256 $accessibilitySha256
if ($LASTEXITCODE -ne 0) { throw "Bound accessibility evidence verification exited $LASTEXITCODE" }
python $sourceVerifier --verify-artifact $boundKeyboardAudit --expected-sha256 $keyboardSha256
if ($LASTEXITCODE -ne 0) { throw "Bound keyboard evidence verification exited $LASTEXITCODE" }
python $sourceVerifier --verify-artifact $boundRestartRecoveryAudit --expected-sha256 $restartRecoverySha256
if ($LASTEXITCODE -ne 0) { throw "Bound restart/recovery evidence verification exited $LASTEXITCODE" }

$trustedPackagePathsJson = ConvertTo-Json -Compress -InputObject @(
  'scripts/package_windows.py',
  'src/autosport/release_package.py',
  'src/autosport/data_tool_package.py'
)
$trustedPackageManifestLines = @(& $pythonExecutable -I -S -c $trustedGitSourceOracleLauncher $gitExecutable $repoRoot $sourceSha $trustedPackagePathsJson)
if ($LASTEXITCODE -ne 0) { throw "Exact package source Git oracle exited $LASTEXITCODE" }
if ($trustedPackageManifestLines.Count -ne 1) { throw 'Exact package source Git oracle did not emit one canonical manifest' }
$trustedPackageManifestJson = [string]$trustedPackageManifestLines[0]

$trustedPackageArchive = Join-Path $boundArtifactRoot 'trusted-package-source.zip'
$trustedPackageRoot = Join-Path $boundArtifactRoot 'trusted-package-source'
& $gitExecutable archive --format=zip "--output=$trustedPackageArchive" $sourceSha -- scripts/package_windows.py src/autosport/release_package.py src/autosport/data_tool_package.py
if ($LASTEXITCODE -ne 0) { throw "Exact package source archive exited $LASTEXITCODE" }
if (Test-Path $trustedPackageRoot) { Remove-Item -LiteralPath $trustedPackageRoot -Recurse -Force }
New-Item -ItemType Directory -Path $trustedPackageRoot | Out-Null
Expand-Archive -LiteralPath $trustedPackageArchive -DestinationPath $trustedPackageRoot -Force
$trustedPackageManifestJson | & $pythonExecutable -I -S -c $trustedSourceSnapshotVerifierLauncher $trustedPackageRoot
if ($LASTEXITCODE -ne 0) { throw "Exact package source snapshot verification exited $LASTEXITCODE" }
$script:trustedPackageRoot = $trustedPackageRoot
$script:trustedPackageManifestJson = $trustedPackageManifestJson

python scripts/package_windows.py `
  --exe $boundAutosportExe `
  --exe-sha256 $autosportExeSha256 `
  --data-exe $boundDataExe `
  --data-exe-sha256 $dataExeSha256 `
  --start-file WINDOWS_START_HERE.txt `
  --example-dir examples/tt_demo `
  --diagnostic $boundDiagnostic `
  --diagnostic-sha256 $diagnosticSha256 `
  --accessibility-audit $boundAccessibilityAudit `
  --accessibility-audit-sha256 $accessibilitySha256 `
  --keyboard-audit $boundKeyboardAudit `
  --keyboard-audit-sha256 $keyboardSha256 `
  --restart-recovery-audit $boundRestartRecoveryAudit `
  --restart-recovery-audit-sha256 $restartRecoverySha256 `
  --output $package `
  --source-sha $sourceSha `
  --verification-output $packageVerification
if ($LASTEXITCODE -ne 0) { throw "Windows package assembly exited $LASTEXITCODE" }

$extractRoot = Join-Path $PWD '.build-fresh-extraction'
if (Test-Path $extractRoot) { Remove-Item -Recurse -Force $extractRoot }
New-Item -ItemType Directory -Path $extractRoot | Out-Null
Expand-Archive -LiteralPath $package -DestinationPath $extractRoot -Force
$packageRoot = Join-Path $extractRoot 'Autosport-V1'
$extractedExe = Join-Path $packageRoot 'Autosport.exe'
$extractedDataExe = Join-Path $packageRoot 'Autosport-Data.exe'
if (-not (Test-Path $extractedExe -PathType Leaf)) { throw 'Fresh extraction is missing Autosport.exe' }
if (-not (Test-Path $extractedDataExe -PathType Leaf)) { throw 'Fresh extraction is missing Autosport-Data.exe' }

$buildInfo = Get-Content (Join-Path $packageRoot 'BUILD_INFO.json') -Raw | ConvertFrom-Json
if ($buildInfo.source_sha -ne $sourceSha) { throw 'Fresh extraction BUILD_INFO source_sha mismatch' }
if ($buildInfo.real_money_execution -ne $false) { throw 'Fresh extraction must preserve REAL_MONEY_EXECUTION=false' }
if ($buildInfo.human_tested -ne $false) { throw 'Machine build must not claim HUMAN_TESTED' }
if ($buildInfo.nvda_verified -ne $false) { throw 'Machine build must not claim NVDA_VERIFIED' }
if ($buildInfo.portable_historical_data_tools -ne $true) { throw 'Fresh extraction does not bind portable historical data tools' }
$extractedExeSha = (Get-FileHash -LiteralPath $extractedExe -Algorithm SHA256).Hash.ToLowerInvariant()
$extractedDataExeSha = (Get-FileHash -LiteralPath $extractedDataExe -Algorithm SHA256).Hash.ToLowerInvariant()
if ($extractedExeSha -ne $buildInfo.autosport_exe_sha256) { throw 'Fresh extraction Autosport.exe hash mismatch' }
if ($extractedDataExeSha -ne $buildInfo.autosport_data_exe_sha256) { throw 'Fresh extraction Autosport-Data.exe hash mismatch' }

& $extractedDataExe --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Fresh-extracted Autosport-Data.exe help exited $LASTEXITCODE" }
& $extractedDataExe compare-strategies --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Fresh-extracted Autosport-Data.exe compare-strategies --help exited $LASTEXITCODE" }
& $extractedDataExe walk-forward-evaluate --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Fresh-extracted Autosport-Data.exe walk-forward-evaluate exited $LASTEXITCODE" }
& $extractedDataExe acquire --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Fresh-extracted Autosport-Data.exe acquire --help exited $LASTEXITCODE" }
& $extractedDataExe build-corpus --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Fresh-extracted Autosport-Data.exe build-corpus --help exited $LASTEXITCODE" }
& $extractedDataExe build-corpus-from-bundle --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Fresh-extracted Autosport-Data.exe build-corpus-from-bundle --help exited $LASTEXITCODE" }
& $extractedDataExe import-betfair-historical --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Fresh-extracted Autosport-Data.exe import-betfair-historical --help exited $LASTEXITCODE" }
& $extractedDataExe verify-dataset (Join-Path $packageRoot 'examples/tt_demo') | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Fresh-extracted Autosport-Data.exe verify-dataset exited $LASTEXITCODE" }

$freshWalkForwardReport = Join-Path $PWD 'dist/fresh-extraction-walk-forward-report.json'
if (Test-Path $freshWalkForwardReport) { Remove-Item -Force $freshWalkForwardReport }
& $extractedDataExe walk-forward-evaluate $walkForwardBundlePath --output $freshWalkForwardReport | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Fresh-extracted Autosport-Data.exe walk-forward-evaluate exited $LASTEXITCODE" }
$freshWalkForwardEvidence = Get-Content $freshWalkForwardReport -Raw | ConvertFrom-Json
if ($freshWalkForwardEvidence.kind -ne 'strict_walk_forward_forecast_evaluation') { throw 'Fresh-extracted walk-forward report kind mismatch' }
if ($freshWalkForwardEvidence.evaluated_forecast_count -ne 2) { throw 'Fresh-extracted walk-forward report forecast count mismatch' }
if ($freshWalkForwardEvidence.window_count -ne 2) { throw 'Fresh-extracted walk-forward report window count mismatch' }
if ($freshWalkForwardEvidence.source_sha256 -ne $walkForwardEvidence.source_sha256) { throw 'Fresh-extracted walk-forward report source identity mismatch' }
if ($freshWalkForwardEvidence.profitability_claim -ne $false) { throw 'Fresh-extracted walk-forward smoke must not claim profitability' }
if ($freshWalkForwardEvidence.real_money_execution -ne $false) { throw 'Fresh-extracted walk-forward smoke must preserve REAL_MONEY_EXECUTION=false' }

$freshDiag = Join-Path $PWD 'dist/fresh-extraction-diagnostic.json'
if (Test-Path $freshDiag) { Remove-Item -Force $freshDiag }
$freshDiagProcess = Start-Process -FilePath $extractedExe -ArgumentList '--diagnostic-output', $freshDiag -Wait -PassThru
if ($freshDiagProcess.ExitCode -ne 0) { throw "Fresh-extracted Autosport.exe diagnostic exited $($freshDiagProcess.ExitCode)" }
$freshDiagnostic = Get-Content $freshDiag -Raw | ConvertFrom-Json
if ($freshDiagnostic.status -ne 'PASS') { throw 'Fresh-extracted Autosport.exe diagnostic did not PASS' }
if ($freshDiagnostic.real_money_execution -ne $false -or $freshDiagnostic.human_tested -ne $false -or $freshDiagnostic.nvda_verified -ne $false) {
  throw 'Fresh-extracted diagnostic violated release truth labels'
}

$freshA11y = Join-Path $PWD 'dist/fresh-extraction-accessibility-audit.json'
if (Test-Path $freshA11y) { Remove-Item -Force $freshA11y }
$freshA11yProcess = Start-Process -FilePath $extractedExe -ArgumentList '--accessibility-audit-output', $freshA11y -Wait -PassThru
if ($freshA11yProcess.ExitCode -ne 0) { throw "Fresh-extracted Autosport.exe accessibility audit exited $($freshA11yProcess.ExitCode)" }
$freshAccessibility = Get-Content $freshA11y -Raw | ConvertFrom-Json
if ($freshAccessibility.status -ne 'PASS') { throw 'Fresh-extracted accessibility audit did not PASS' }
if ($freshAccessibility.real_money_execution -ne $false -or $freshAccessibility.human_tested -ne $false -or $freshAccessibility.nvda_verified -ne $false) {
  throw 'Fresh-extracted accessibility audit violated release truth labels'
}

$freshKeyboard = Join-Path $PWD 'dist/fresh-extraction-keyboard-audit.json'
if (Test-Path $freshKeyboard) { Remove-Item -Force $freshKeyboard }
$freshKeyboardProcess = Start-Process -FilePath $extractedExe -ArgumentList '--keyboard-audit-output', $freshKeyboard -Wait -PassThru
if ($freshKeyboardProcess.ExitCode -ne 0) { throw "Fresh-extracted keyboard audit exited $($freshKeyboardProcess.ExitCode)" }
$freshKeyboardEvidence = Get-Content $freshKeyboard -Raw | ConvertFrom-Json
if ($freshKeyboardEvidence.status -ne 'PASS') { throw 'Fresh-extracted keyboard audit did not PASS' }
if ($freshKeyboardEvidence.real_money_execution -ne $false -or $freshKeyboardEvidence.human_tested -ne $false -or $freshKeyboardEvidence.nvda_verified -ne $false) {
  throw 'Fresh-extracted keyboard audit violated release truth labels'
}

$freshRestartRecovery = Join-Path $PWD 'dist/fresh-extraction-restart-recovery-audit.json'
if (Test-Path $freshRestartRecovery) { Remove-Item -Force $freshRestartRecovery }
$freshRestartRecoveryProcess = Start-Process -FilePath $extractedExe -ArgumentList '--restart-recovery-audit-output', $freshRestartRecovery -Wait -PassThru
if ($freshRestartRecoveryProcess.ExitCode -ne 0) { throw "Fresh-extracted Autosport.exe restart/recovery audit exited $($freshRestartRecoveryProcess.ExitCode)" }
$freshRestartRecoveryEvidence = Get-Content $freshRestartRecovery -Raw | ConvertFrom-Json
if ($freshRestartRecoveryEvidence.status -ne 'PASS') { throw 'Fresh-extracted restart/recovery audit did not PASS' }
if ($freshRestartRecoveryEvidence.session_restart_status -ne 'PASS') { throw 'Fresh-extracted restart audit did not prove persistent session reopen' }
if ($freshRestartRecoveryEvidence.transaction_recovery_status -ne 'PASS') { throw 'Fresh-extracted recovery audit did not prove transaction recovery' }
if ($freshRestartRecoveryEvidence.recovery_disposition -ne 'aborted_uncommitted') { throw 'Fresh-extracted recovery audit disposition is not fail-closed' }
if ($freshRestartRecoveryEvidence.real_money_execution -ne $false -or $freshRestartRecoveryEvidence.human_tested -ne $false -or $freshRestartRecoveryEvidence.nvda_verified -ne $false) {
  throw 'Machine restart/recovery audit violated release truth labels'
}
Assert-ProcessRecoveryEvidence -Evidence $freshRestartRecoveryEvidence -Label 'Fresh-extracted restart/recovery audit'

$freshEvidence = [ordered]@{
  status = 'PASS'
  source_sha = $sourceSha
  package_sha256 = (Get-FileHash -LiteralPath $package -Algorithm SHA256).Hash.ToLowerInvariant()
  autosport_exe_sha256 = $extractedExeSha
  autosport_data_exe_sha256 = $extractedDataExeSha
  portable_historical_data_tools = $true
  package_verification_status = 'PASS'
  extracted_strategy_comparison_entry_status = 'PASS'
  extracted_walk_forward_evaluation_entry_status = 'PASS'
  extracted_walk_forward_evaluation_execution_status = 'PASS'
  extracted_walk_forward_sample_real_historical_proof = $false
  extracted_data_tool_help_status = 'PASS'
  extracted_data_tool_acquire_help_status = 'PASS'
  extracted_data_tool_build_corpus_help_status = 'PASS'
  extracted_data_tool_bundle_corpus_help_status = 'PASS'
  extracted_data_tool_verify_dataset_status = 'PASS'
  extracted_diagnostic_status = $freshDiagnostic.status
  extracted_accessibility_status = $freshAccessibility.status
  extracted_keyboard_status = $freshKeyboardEvidence.status
  extracted_restart_recovery_status = $freshRestartRecoveryEvidence.status
  extracted_session_restart_status = $freshRestartRecoveryEvidence.session_restart_status
  extracted_transaction_recovery_status = $freshRestartRecoveryEvidence.transaction_recovery_status
  extracted_process_kill_relaunch_status = $freshRestartRecoveryEvidence.process_kill_relaunch_status
  extracted_process_recovery_disposition = $freshRestartRecoveryEvidence.process_recovery_disposition
  extracted_process_recovery_registry_status = $freshRestartRecoveryEvidence.process_recovery_registry_status
  extracted_process_recovery_manifest_phase = $freshRestartRecoveryEvidence.process_recovery_manifest_phase
  real_money_execution = $false
  human_tested = $false
  nvda_verified = $false
}
$freshEvidence | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $PWD 'dist/fresh-extraction-verification.json') -Encoding utf8
Remove-Item -LiteralPath $sourceVerifier -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $boundArtifactRoot -Recurse -Force -ErrorAction SilentlyContinue