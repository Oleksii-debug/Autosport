from __future__ import annotations

import hashlib
import importlib.util
import subprocess
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


def test_trusted_verifier_snapshot_uses_exact_git_blob_after_live_mutation(
    tmp_path: Path,
) -> None:
    verifier = _load_verifier()
    repo, source_sha, canonical = _committed_verifier_repo(tmp_path)
    live_verifier = repo / "scripts" / "verify_source_checkout.py"
    trusted_snapshot = tmp_path / "trusted-verifier.py"

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


def test_windows_build_materializes_trusted_verifier_during_initial_preflight() -> None:
    script = _BUILD_SCRIPT.read_text(encoding="utf-8")

    source_sha = "$sourceSha = $env:AUTOSPORT_SOURCE_SHA"
    snapshot_path = "$sourceVerifier = (New-TemporaryFile).FullName"
    preflight = (
        "python scripts/verify_source_checkout.py --source-sha $sourceSha "
        "--trusted-verifier-output $sourceVerifier"
    )
    old_copy = "Copy-Item -LiteralPath 'scripts/verify_source_checkout.py' -Destination $sourceVerifier -Force"
    first_mutation = "python -m pip install --upgrade pip"

    assert script.index(source_sha) < script.index(snapshot_path) < script.index(preflight)
    assert script.index(preflight) < script.index(first_mutation)
    assert all(not line.strip().startswith(old_copy) for line in script.splitlines())
    assert script.count("python $sourceVerifier --source-sha $sourceSha --late-build-boundary") == 3
