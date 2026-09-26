from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

import pytest

from scripts import package_windows


@pytest.mark.parametrize(
    "path",
    [
        "keys/operator.ppk",
        "keys/operator.pfx",
        "keys/operator.p12",
        "keys/id_rsa.pem",
        "keys/id_dsa.pem",
        "keys/id_ecdsa.pem",
        "keys/id_ed25519.pem",
        "KEYS/ID_RSA.PEM",
    ],
)
def test_private_key_containers_are_rejected(path: str) -> None:
    candidate = PurePosixPath(path)

    assert package_windows._secret_source_path_reason(candidate) == "private-key file"
    with pytest.raises(ValueError, match="private-key file"):
        package_windows._require_safe_static_release_paths((candidate,))


def test_public_certificate_pem_remains_allowed() -> None:
    candidate = PurePosixPath("docs/public-certificate.pem")

    assert package_windows._secret_source_path_reason(candidate) is None
    package_windows._require_safe_static_release_paths((candidate,))


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def test_tracked_private_key_container_fails_before_materialization(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    example_dir = repo / "examples" / "tt_demo"
    key_path = example_dir / "keys" / "id_rsa.pem"
    key_path.parent.mkdir(parents=True)
    (repo / "WINDOWS_START_HERE.txt").write_text(
        "canonical start instructions\n",
        encoding="utf-8",
    )
    (example_dir / "manifest.json").write_text(
        '{"kind":"manifest"}\n',
        encoding="utf-8",
    )
    key_path.write_text("placeholder-private-key-material\n", encoding="utf-8")

    _git(repo, "init")
    _git(repo, "config", "user.name", "Autosport Test")
    _git(repo, "config", "user.email", "autosport-test@example.invalid")
    _git(repo, "add", "WINDOWS_START_HERE.txt", "examples/tt_demo")
    _git(repo, "commit", "-m", "package fixture")
    source_sha = _git(repo, "rev-parse", "HEAD")
    snapshot_dir = tmp_path / "snapshot"

    with pytest.raises(ValueError, match="private-key file"):
        package_windows._materialize_exact_static_payload(
            repo_root=repo,
            source_sha=source_sha,
            start_file=repo / "WINDOWS_START_HERE.txt",
            example_dir=example_dir,
            snapshot_dir=snapshot_dir,
        )

    assert not (snapshot_dir / "exact-source-static").exists()
