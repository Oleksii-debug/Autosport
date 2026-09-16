from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_VERIFIER = _ROOT / "scripts" / "verify_source_checkout.py"
_BUILD_SCRIPT = _ROOT / "scripts" / "build_windows.ps1"


def _load_verifier():
    spec = importlib.util.spec_from_file_location(
        "autosport_trusted_verifier_snapshot_test",
        _VERIFIER,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _committed_verifier_repo(tmp_path: Path) -> tuple[Path, str, bytes]:
    repo = tmp_path / "repo"
    script_path = repo / "scripts" / "verify_source_checkout.py"
    script_path.parent.mkdir(parents=True)
    canonical = _VERIFIER.read_bytes()
    script_path.write_bytes(canonical)

    _git(repo, "init")
    _git(repo, "config", "user.email", "autosport-test@example.invalid")
    _git(repo, "config", "user.name", "Autosport Test")
    _git(repo, "add", "scripts/verify_source_checkout.py")
    _git(repo, "commit", "-m", "fixture")
    return repo, _git(repo, "rev-parse", "HEAD"), canonical


def _trusted_launcher_source() -> str:
    script = _BUILD_SCRIPT.read_text(encoding="utf-8")
    start_marker = "$trustedVerifierLauncher = @'\n"
    end_marker = "\n'@\n"
    assert start_marker in script
    tail = script.split(start_marker, 1)[1]
    assert end_marker in tail
    return tail.split(end_marker, 1)[0]


def test_trusted_verifier_snapshot_uses_exact_git_blob_after_live_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = _load_verifier()
    repo, source_sha, canonical = _committed_verifier_repo(tmp_path)
    live_verifier = repo / "scripts" / "verify_source_checkout.py"
    trusted_snapshot = tmp_path / "trusted-verifier.py"

    # This synthetic nested repository has its own source identity. Do not let
    # the outer GitHub Actions pull-request event bind it to the real PR head.
    for name in ("GITHUB_EVENT_PATH", "GITHUB_REPOSITORY", "GITHUB_SHA"):
        monkeypatch.delenv(name, raising=False)

    # Model the vulnerable old ordering: a clean proof succeeds, then the live
    # verifier path is replaced before snapshot materialization.
    verifier.verify_source_checkout(source_sha, repo_root=repo)
    live_verifier.write_bytes(b"raise SystemExit('hostile verifier replacement')\n")

    digest = verifier.materialize_trusted_verifier_snapshot(
        repo,
        source_sha,
        trusted_snapshot,
    )

    assert trusted_snapshot.read_bytes() == canonical
    assert digest == hashlib.sha256(canonical).hexdigest()
    assert trusted_snapshot.read_bytes() != live_verifier.read_bytes()


def test_trusted_verifier_snapshot_refuses_destination_inside_checkout(
    tmp_path: Path,
) -> None:
    verifier = _load_verifier()
    repo, source_sha, _canonical = _committed_verifier_repo(tmp_path)

    with pytest.raises(ValueError, match="outside the release source checkout"):
        verifier.materialize_trusted_verifier_snapshot(
            repo,
            source_sha,
            repo / "trusted-verifier.py",
        )


def test_trusted_launcher_rejects_snapshot_mutation_before_execution(tmp_path: Path) -> None:
    launcher = _trusted_launcher_source()
    verifier = tmp_path / "trusted-verifier.py"
    marker = tmp_path / "hostile-executed.txt"
    canonical = b"raise SystemExit(0)\n"
    expected = hashlib.sha256(canonical).hexdigest()
    verifier.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-c", launcher, str(verifier), expected],
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "trusted verifier SHA-256 mismatch" in completed.stderr
    assert not marker.exists()


def test_trusted_launcher_isolated_startup_does_not_import_sitecustomize(
    tmp_path: Path,
) -> None:
    launcher = _trusted_launcher_source()
    site_dir = tmp_path / "candidate-site"
    site_dir.mkdir()
    site_marker = tmp_path / "sitecustomize-executed.txt"
    verifier_marker = tmp_path / "verifier-executed.txt"
    (site_dir / "sitecustomize.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['AUTOSPORT_SITE_MARKER']).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    verifier = tmp_path / "trusted-verifier.py"
    verifier_bytes = (
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['AUTOSPORT_VERIFIER_MARKER']).write_text('executed', encoding='utf-8')\n"
    ).encode("utf-8")
    verifier.write_bytes(verifier_bytes)
    expected = hashlib.sha256(verifier_bytes).hexdigest()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(site_dir)
    env["AUTOSPORT_SITE_MARKER"] = str(site_marker)
    env["AUTOSPORT_VERIFIER_MARKER"] = str(verifier_marker)

    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-c", launcher, str(verifier), expected],
        env=env,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert verifier_marker.read_text(encoding="utf-8") == "executed"
    assert not site_marker.exists()


def test_windows_build_bootstraps_exact_verifier_before_repository_python() -> None:
    script = _BUILD_SCRIPT.read_text(encoding="utf-8")
    active_lines = [
        line.strip()
        for line in script.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    source_sha = "$sourceSha = $env:AUTOSPORT_SOURCE_SHA"
    exact_entry = "$verifierTreeEntry = (& $gitExecutable ls-tree $sourceSha -- 'scripts/verify_source_checkout.py').Trim()"
    bootstrap_setup = "$verifierBootstrapInfo = [System.Diagnostics.ProcessStartInfo]::new()"
    exact_blob_arg = "[void]$verifierBootstrapInfo.ArgumentList.Add($verifierBlobSha)"
    exact_bytes = "$verifierBytes = $verifierBuffer.ToArray()"
    digest = "$sourceVerifierSha256 = ([System.BitConverter]::ToString($verifierHasher.ComputeHash($verifierBytes))).Replace('-', '').ToLowerInvariant()"
    publish_path = "$sourceVerifier = (New-TemporaryFile).FullName"
    publish_bytes = "[System.IO.File]::WriteAllBytes($sourceVerifier, $verifierBytes)"
    preflight = "python $sourceVerifier --source-sha $sourceSha"
    first_mutation = "python -m pip install --upgrade pip"
    old_copy = "Copy-Item -LiteralPath 'scripts/verify_source_checkout.py' -Destination $sourceVerifier -Force"

    assert all(not line.startswith("python scripts/verify_source_checkout.py") for line in active_lines)
    assert "Get-ChildItem Env: | Where-Object { $_.Name -like 'GIT_*' }" in script
    assert "$env:GIT_NO_REPLACE_OBJECTS = '1'" in script
    assert script.index(source_sha) < script.index(exact_entry)
    assert (
        script.index(exact_entry)
        < script.index(bootstrap_setup)
        < script.index(exact_blob_arg)
        < script.index(exact_bytes)
        < script.index(digest)
        < script.index(publish_path)
        < script.index(publish_bytes)
        < script.index(preflight)
        < script.index(first_mutation)
    )
    assert "Get-FileHash -LiteralPath $sourceVerifier -Algorithm SHA256" not in script
    assert all(not line.startswith(old_copy) for line in active_lines)
    assert "& $script:pythonExecutable -I -S -c $script:trustedVerifierLauncher" in script
    assert script.count("python $sourceVerifier --source-sha $sourceSha --late-build-boundary") == 3
