from __future__ import annotations

import importlib.util
import subprocess
import zlib
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGER = _ROOT / "scripts" / "package_windows.py"


def _load_packager():
    spec = importlib.util.spec_from_file_location(
        "autosport_package_static_source_handoff_test",
        _PACKAGER,
    )
    assert spec is not None and spec.loader is not None
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


def test_static_package_payload_comes_from_exact_source_tree_after_live_mutation(
    tmp_path: Path,
) -> None:
    packager = _load_packager()
    repo = tmp_path / "repo"
    example_dir = repo / "examples" / "tt_demo"
    example_dir.mkdir(parents=True)
    start_file = repo / "WINDOWS_START_HERE.txt"

    canonical_start = "canonical start instructions\n"
    canonical_files = {
        "manifest.json": '{"kind":"manifest"}\n',
        "market.jsonl": '{"market":"canonical"}\n',
        "research_plan.json": '{"plan":"canonical"}\n',
        "results.json": '{"result":"canonical"}\n',
    }
    start_file.write_text(canonical_start, encoding="utf-8")
    for name, text in canonical_files.items():
        (example_dir / name).write_text(text, encoding="utf-8")

    _git(repo, "init")
    _git(repo, "config", "user.email", "autosport-test@example.invalid")
    _git(repo, "config", "user.name", "Autosport Test")
    _git(repo, "add", "WINDOWS_START_HERE.txt", "examples/tt_demo")
    _git(repo, "commit", "-m", "canonical static package payload")
    source_sha = _git(repo, "rev-parse", "HEAD")

    # Model the recorded post-source-proof race: replace tracked bytes, delete a
    # tracked member, and add an untracked member before package consumption.
    start_file.write_text("hostile replacement\n", encoding="utf-8")
    (example_dir / "manifest.json").write_text('{"kind":"hostile"}\n', encoding="utf-8")
    (example_dir / "results.json").unlink()
    (example_dir / "extra.json").write_text('{"extra":true}\n', encoding="utf-8")

    snapshot_dir = tmp_path / "private-snapshot"
    trusted_start, trusted_examples = packager._materialize_exact_static_payload(
        repo_root=repo,
        source_sha=source_sha,
        start_file=start_file,
        example_dir=example_dir,
        snapshot_dir=snapshot_dir,
    )

    assert trusted_start.read_text(encoding="utf-8") == canonical_start
    assert {
        path.relative_to(trusted_examples).as_posix(): path.read_text(encoding="utf-8")
        for path in trusted_examples.rglob("*")
        if path.is_file()
    } == canonical_files
    assert not (trusted_examples / "extra.json").exists()


def test_static_package_payload_rejects_corrupted_loose_git_blob(
    tmp_path: Path,
) -> None:
    packager = _load_packager()
    repo = tmp_path / "repo"
    example_dir = repo / "examples" / "tt_demo"
    example_dir.mkdir(parents=True)
    start_file = repo / "WINDOWS_START_HERE.txt"
    start_file.write_text("canonical start instructions\n", encoding="utf-8")
    (example_dir / "manifest.json").write_text('{"kind":"manifest"}\n', encoding="utf-8")

    _git(repo, "init")
    _git(repo, "config", "user.email", "autosport-test@example.invalid")
    _git(repo, "config", "user.name", "Autosport Test")
    _git(repo, "add", "WINDOWS_START_HERE.txt", "examples/tt_demo")
    _git(repo, "commit", "-m", "canonical static package payload")
    source_sha = _git(repo, "rev-parse", "HEAD")
    object_sha = _git(repo, "rev-parse", f"{source_sha}:WINDOWS_START_HERE.txt")

    hostile = b"hostile replacement\n"
    raw_object = b"blob " + str(len(hostile)).encode("ascii") + b"\0" + hostile
    object_path = repo / ".git" / "objects" / object_sha[:2] / object_sha[2:]
    assert object_path.is_file()
    object_path.chmod(0o600)
    object_path.write_bytes(zlib.compress(raw_object))

    cat_file = subprocess.run(
        ["git", "cat-file", "blob", object_sha],
        cwd=repo,
        check=True,
        capture_output=True,
    ).stdout
    assert cat_file == hostile

    snapshot_dir = tmp_path / "private-snapshot"
    try:
        packager._materialize_exact_static_payload(
            repo_root=repo,
            source_sha=source_sha,
            start_file=start_file,
            example_dir=example_dir,
            snapshot_dir=snapshot_dir,
        )
    except ValueError as exc:
        assert "Git blob identity mismatch" in str(exc)
    else:
        raise AssertionError("corrupted Git blob object must fail closed")

    trusted_start = snapshot_dir / "exact-source-static" / "WINDOWS_START_HERE.txt"
    assert not trusted_start.exists()


def test_static_package_payload_rejects_path_outside_checkout(tmp_path: Path) -> None:
    packager = _load_packager()
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")

    try:
        packager._repo_relative_path(repo, outside, field="start_file")
    except ValueError as exc:
        assert "inside the release source checkout" in str(exc)
    else:
        raise AssertionError("outside package input path must fail closed")
