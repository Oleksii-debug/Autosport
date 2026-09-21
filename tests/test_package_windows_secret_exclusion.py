from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

import pytest

from scripts import package_windows


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        (".env", "credential/session file"),
        (".env.production", "runtime environment file"),
        ("oauth/client_secret_desktop.json", "OAuth client-secret file"),
        ("oauth/token.json", "credential/session file"),
        ("telegram/operator.session", "Telegram session file"),
        ("telegram/operator.session-journal", "Telegram session file"),
        (".aws/credentials", "credential/profile directory"),
        (".ssh/id_ed25519", "credential/profile directory"),
        ("browser/User Data/Default/Cookies", "credential/profile directory"),
        ("browser/Login Data", "credential/session file"),
        ("tls/server.key", "private-key file"),
        ("tls/private-client.pem", "private-key file"),
        ("python/.pypirc", "credential/session file"),
        ("app/secrets.toml", "credential/session file"),
    ],
)
def test_secret_bearing_static_paths_are_rejected(path: str, reason: str) -> None:
    candidate = PurePosixPath(path)

    assert package_windows._secret_source_path_reason(candidate) == reason
    with pytest.raises(ValueError, match="release static payload contains forbidden"):
        package_windows._require_safe_static_release_paths((candidate,))


@pytest.mark.parametrize(
    "path",
    [
        ".env.example",
        ".env.sample",
        ".env.template",
        ".env.dist",
        "manifest.json",
        "market.jsonl",
        "research_plan.json",
        "results.json",
        "docs/public-certificate.pem",
        "notes/session_notes.md",
    ],
)
def test_safe_static_templates_and_product_files_remain_allowed(path: str) -> None:
    candidate = PurePosixPath(path)

    assert package_windows._secret_source_path_reason(candidate) is None
    package_windows._require_safe_static_release_paths((candidate,))


def _make_release_fixture(
    tmp_path: Path,
    *,
    extra_relative: str | None = None,
) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    example_dir = repo / "examples" / "tt_demo"
    example_dir.mkdir(parents=True)
    (repo / "WINDOWS_START_HERE.txt").write_text(
        "canonical start instructions\n",
        encoding="utf-8",
    )
    (example_dir / "manifest.json").write_text(
        '{"kind":"manifest"}\n',
        encoding="utf-8",
    )
    if extra_relative is not None:
        extra = example_dir.joinpath(*PurePosixPath(extra_relative).parts)
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_text("placeholder-only\n", encoding="utf-8")

    _git(repo, "init")
    _git(repo, "config", "user.name", "Autosport Test")
    _git(repo, "config", "user.email", "autosport-test@example.invalid")
    _git(repo, "add", "WINDOWS_START_HERE.txt", "examples/tt_demo")
    _git(repo, "commit", "-m", "package fixture")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_tracked_secret_artifact_fails_before_static_materialization(
    tmp_path: Path,
) -> None:
    repo, source_sha = _make_release_fixture(
        tmp_path,
        extra_relative="oauth/token.json",
    )
    snapshot_dir = tmp_path / "snapshot"

    with pytest.raises(ValueError, match="credential/session file"):
        package_windows._materialize_exact_static_payload(
            repo_root=repo,
            source_sha=source_sha,
            start_file=repo / "WINDOWS_START_HERE.txt",
            example_dir=repo / "examples" / "tt_demo",
            snapshot_dir=snapshot_dir,
        )

    assert not (snapshot_dir / "exact-source-static").exists()


def test_arbitrary_tracked_start_file_cannot_be_renamed_into_release(
    tmp_path: Path,
) -> None:
    repo, source_sha = _make_release_fixture(tmp_path)
    alternate = repo / "alternate.txt"
    alternate.write_text("not the canonical operator instructions\n", encoding="utf-8")
    _git(repo, "add", "alternate.txt")
    _git(repo, "commit", "-m", "add alternate tracked input")
    source_sha = _git(repo, "rev-parse", "HEAD")

    with pytest.raises(ValueError, match="canonical tracked WINDOWS_START_HERE"):
        package_windows._materialize_exact_static_payload(
            repo_root=repo,
            source_sha=source_sha,
            start_file=alternate,
            example_dir=repo / "examples" / "tt_demo",
            snapshot_dir=tmp_path / "snapshot",
        )


def test_arbitrary_tracked_example_tree_cannot_be_selected(
    tmp_path: Path,
) -> None:
    repo, _source_sha = _make_release_fixture(tmp_path)
    alternate_dir = repo / "examples" / "alternate"
    alternate_dir.mkdir(parents=True)
    (alternate_dir / "manifest.json").write_text(
        '{"kind":"alternate"}\n',
        encoding="utf-8",
    )
    _git(repo, "add", "examples/alternate")
    _git(repo, "commit", "-m", "add alternate example tree")
    source_sha = _git(repo, "rev-parse", "HEAD")

    with pytest.raises(ValueError, match="canonical tracked examples/tt_demo"):
        package_windows._materialize_exact_static_payload(
            repo_root=repo,
            source_sha=source_sha,
            start_file=repo / "WINDOWS_START_HERE.txt",
            example_dir=alternate_dir,
            snapshot_dir=tmp_path / "snapshot",
        )
