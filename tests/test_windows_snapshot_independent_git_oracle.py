from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_BUILD_SCRIPT = _ROOT / "scripts" / "build_windows.ps1"


def _launcher_source(variable: str) -> str:
    script = _BUILD_SCRIPT.read_text(encoding="utf-8")
    start_marker = f"${variable} = @'\n"
    end_marker = "\n'@\n"
    assert start_marker in script
    tail = script.split(start_marker, 1)[1]
    assert end_marker in tail
    return tail.split(end_marker, 1)[0]


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _canonical_repo(tmp_path: Path) -> tuple[Path, str, bytes]:
    repo = tmp_path / "repo"
    source = repo / "scripts" / "package_windows.py"
    source.parent.mkdir(parents=True)
    payload = b"VALUE = 'canonical exact source'\n"
    source.write_bytes(payload)
    _git(repo, "init")
    _git(repo, "config", "user.email", "autosport-test@example.invalid")
    _git(repo, "config", "user.name", "Autosport Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "canonical source")
    return repo, _git(repo, "rev-parse", "HEAD"), payload


def test_exact_git_oracle_rejects_post_materialization_replacement(tmp_path: Path) -> None:
    repo, source_sha, canonical = _canonical_repo(tmp_path)
    oracle_launcher = _launcher_source("trustedGitSourceOracleLauncher")
    snapshot_launcher = _launcher_source("trustedSourceSnapshotVerifierLauncher")
    git_executable = shutil.which("git")
    assert git_executable is not None

    oracle = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            oracle_launcher,
            git_executable,
            str(repo),
            source_sha,
            json.dumps(["scripts/package_windows.py"]),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    manifest = json.loads(oracle.stdout)
    assert manifest == {
        "scripts/package_windows.py": hashlib.sha256(canonical).hexdigest()
    }

    snapshot = tmp_path / "snapshot"
    snapshot_source = snapshot / "scripts" / "package_windows.py"
    snapshot_source.parent.mkdir(parents=True)
    snapshot_source.write_bytes(canonical)
    accepted = subprocess.run(
        [sys.executable, "-I", "-S", "-c", snapshot_launcher, str(snapshot)],
        input=json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        capture_output=True,
        text=True,
    )
    assert accepted.returncode == 0, accepted.stderr
    assert "SOURCE_SNAPSHOT=PASS" in accepted.stdout

    snapshot_source.write_bytes(b"VALUE = 'post-extraction replacement'\n")
    rejected = subprocess.run(
        [sys.executable, "-I", "-S", "-c", snapshot_launcher, str(snapshot)],
        input=json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert "source snapshot SHA-256 mismatch" in rejected.stderr


def test_windows_build_uses_independent_oracle_for_build_and_package_snapshots() -> None:
    script = _BUILD_SCRIPT.read_text(encoding="utf-8")

    build_oracle = (
        "$trustedBuildManifestLines = @(& $pythonExecutable -I -S -c "
        "$trustedGitSourceOracleLauncher $gitExecutable $repoRoot $sourceSha 'null')"
    )
    build_archive = '& $gitExecutable archive --format=zip "--output=$trustedBuildArchive" $sourceSha'
    package_oracle = (
        "$trustedPackageManifestLines = @(& $pythonExecutable -I -S -c "
        "$trustedGitSourceOracleLauncher $gitExecutable $repoRoot $sourceSha $trustedPackagePathsJson)"
    )
    package_archive = (
        "& $gitExecutable archive --format=zip \"--output=$trustedPackageArchive\" $sourceSha -- "
        "scripts/package_windows.py src/autosport/release_package.py src/autosport/data_tool_package.py"
    )
    snapshot_gate = (
        "$trustedBuildManifestJson | & $pythonExecutable -I -S -c "
        "$trustedSourceSnapshotVerifierLauncher $trustedBuildRoot"
    )

    assert script.index(build_oracle) < script.index(build_archive)
    assert script.count(snapshot_gate) >= 3
    assert script.index(package_oracle) < script.index(package_archive)
    assert "$script:trustedPackageManifestJson = $trustedPackageManifestJson" in script
    assert "$trustedPackageManifest[$relativePath] = (Get-FileHash" not in script
