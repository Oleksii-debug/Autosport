from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import verify_source_checkout


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _clean_repo(root: Path) -> str:
    _git(root, "init")
    _git(root, "config", "user.email", "autosport-tests@example.invalid")
    _git(root, "config", "user.name", "Autosport Tests")
    # Match the release checkout's byte-canonical text policy and avoid
    # platform newline translation inside this temporary Git fixture.
    (root / ".gitattributes").write_bytes(b"* text=auto eol=lf\n")
    (root / ".gitignore").write_bytes(b"*.ignored\n")
    (root / "tracked.py").write_bytes(b"VALUE = 1\n")
    _git(root, "add", ".gitattributes", ".gitignore", "tracked.py")
    _git(root, "commit", "-m", "fixture")
    return _git(root, "rev-parse", "HEAD")


def _without_github_event_environment():
    return patch.dict(
        os.environ,
        {"GITHUB_EVENT_PATH": "", "GITHUB_REPOSITORY": "", "GITHUB_SHA": ""},
        clear=False,
    )


def test_source_verifier_does_not_execute_mutable_clean_filter() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        source_sha = _clean_repo(root)
        marker = root / "filter-ran.txt"
        filter_script = root / ".git" / "hostile_filter.py"
        filter_script.write_text(
            "from pathlib import Path\n"
            "import sys\n"
            "payload = sys.stdin.buffer.read()\n"
            f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n"
            "Path('tracked.py').write_text('VALUE = 999\\n', encoding='utf-8')\n"
            "sys.stdout.buffer.write(payload)\n",
            encoding="utf-8",
        )
        attributes = root / ".git" / "info" / "attributes"
        attributes.write_text("tracked.py filter=hostile\n", encoding="utf-8")
        filter_command = f'"{sys.executable}" "{filter_script}"'
        _git(root, "config", "filter.hostile.clean", filter_command)
        _git(root, "config", "filter.hostile.smudge", "cat")

        with _without_github_event_environment():
            verify_source_checkout.verify_source_checkout(
                source_sha,
                repo_root=root,
                late_build_boundary=True,
            )

        assert not marker.exists()
        assert (root / "tracked.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_source_verifier_rejects_index_only_drift_with_canonical_worktree_bytes() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        source_sha = _clean_repo(root)
        tracked = root / "tracked.py"
        tracked.write_bytes(b"VALUE = 2\n")
        _git(root, "add", "tracked.py")
        tracked.write_bytes(b"VALUE = 1\n")

        with _without_github_event_environment(), pytest.raises(
            ValueError,
            match="index does not match exact source_sha",
        ):
            verify_source_checkout.verify_source_checkout(
                source_sha,
                repo_root=root,
                late_build_boundary=True,
            )
