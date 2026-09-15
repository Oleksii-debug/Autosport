from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_SCRIPT = _ROOT / "scripts" / "package_windows.py"


def _load_package_windows():
    spec = importlib.util.spec_from_file_location(
        "autosport_package_snapshot_fence_test",
        _PACKAGE_SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def test_capture_destination_mutation_is_rejected_before_final_consumer(
    tmp_path: Path,
) -> None:
    package_windows = _load_package_windows()
    handoff = tmp_path / "handoff.exe"
    handoff.write_bytes(b"MZ-canonical")
    expected = _sha256(handoff)
    snapshot_dir = tmp_path / "snapshot"

    trusted = package_windows._capture_verified_executable(
        handoff,
        expected,
        snapshot_dir=snapshot_dir,
        snapshot_name="Autosport.exe",
    )
    trusted.write_bytes(b"MZ-hostile-post-capture")

    root, manifest = package_windows._normalize_snapshot_manifest(
        snapshot_dir,
        {trusted: expected},
    )
    with pytest.raises(ValueError, match="private package snapshot SHA-256 mismatch"):
        package_windows._verify_snapshot_manifest(root, manifest)


def test_exact_static_snapshot_manifest_rejects_post_materialization_mutation(
    tmp_path: Path,
) -> None:
    package_windows = _load_package_windows()
    repo = tmp_path / "repo"
    example_dir = repo / "examples" / "tt_demo"
    example_dir.mkdir(parents=True)
    start_file = repo / "WINDOWS_START_HERE.txt"
    start_file.write_text("canonical start\n", encoding="utf-8")
    (example_dir / "manifest.json").write_text('{"kind":"canonical"}\n', encoding="utf-8")

    _git(repo, "init")
    _git(repo, "config", "user.email", "autosport-test@example.invalid")
    _git(repo, "config", "user.name", "Autosport Test")
    _git(repo, "add", "WINDOWS_START_HERE.txt", "examples/tt_demo")
    _git(repo, "commit", "-m", "canonical package static inputs")
    source_sha = _git(repo, "rev-parse", "HEAD")

    expected_snapshot_sha256: dict[Path, str] = {}
    snapshot_dir = tmp_path / "snapshot"
    trusted_start, _trusted_examples = package_windows._materialize_exact_static_payload(
        repo_root=repo,
        source_sha=source_sha,
        start_file=start_file,
        example_dir=example_dir,
        snapshot_dir=snapshot_dir,
        expected_sha256=expected_snapshot_sha256,
    )
    assert expected_snapshot_sha256[trusted_start] == hashlib.sha256(
        b"canonical start\n"
    ).hexdigest()

    trusted_start.write_text("hostile after materialization\n", encoding="utf-8")
    root, manifest = package_windows._normalize_snapshot_manifest(
        snapshot_dir,
        expected_snapshot_sha256,
    )
    with pytest.raises(ValueError, match="private package snapshot SHA-256 mismatch"):
        package_windows._verify_snapshot_manifest(root, manifest)


def test_final_package_consumers_are_inside_private_snapshot_fence() -> None:
    script = _PACKAGE_SCRIPT.read_text(encoding="utf-8")
    fence = "with _package_input_write_fence(snapshot_dir, expected_snapshot_sha256):"
    build = "            output, _base_digest = build_windows_package("
    bind = "            binding = bind_portable_data_tool("
    expected_base_digest = "                expected_base_package_sha256=_base_digest,"
    expected_exe_digest = (
        "                expected_autosport_exe_sha256=expected_snapshot_sha256[trusted_exe],"
    )
    static_manifest = "expected_sha256=expected_snapshot_sha256,"

    fence_index = script.index(fence)
    build_index = script.index(build, fence_index)
    bind_index = script.index(bind, build_index)
    base_digest_index = script.index(expected_base_digest, bind_index)
    exe_digest_index = script.index(expected_exe_digest, base_digest_index)

    assert static_manifest in script
    assert fence_index < build_index < bind_index < base_digest_index < exe_digest_index


def test_bound_final_package_verification_ignores_post_capture_live_path_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_windows = _load_package_windows()
    package = tmp_path / "candidate.zip"
    producer_bytes = b"producer-bound-final-zip"
    replacement_bytes = b"post-publication-replacement-zip"
    package.write_bytes(producer_bytes)
    producer_digest = hashlib.sha256(producer_bytes).hexdigest()
    source_sha = "a" * 40
    observed: dict[str, object] = {}

    def adversarial_windows_verifier(
        path: str | Path,
        *,
        expected_source_sha: str,
    ) -> dict[str, object]:
        assert expected_source_sha == source_sha
        package.write_bytes(replacement_bytes)
        observed["windows_path"] = Path(path)
        observed["windows_bytes"] = Path(path).read_bytes()
        return {"status": "PASS", "source_sha": expected_source_sha}

    def data_verifier(path: str | Path) -> dict[str, object]:
        observed["data_path"] = Path(path)
        observed["data_bytes"] = Path(path).read_bytes()
        return {"status": "PASS", "portable_historical_data_tools": True}

    monkeypatch.setattr(
        package_windows,
        "verify_windows_package",
        adversarial_windows_verifier,
    )
    monkeypatch.setattr(package_windows, "verify_portable_data_tool", data_verifier)

    report = package_windows._verify_bound_final_package(
        package,
        producer_digest,
        expected_source_sha=source_sha,
    )

    assert package.read_bytes() == replacement_bytes
    assert observed["windows_bytes"] == producer_bytes
    assert observed["data_bytes"] == producer_bytes
    assert observed["windows_path"] == observed["data_path"]
    assert observed["windows_path"] != package.absolute()
    assert report["package_sha256"] == producer_digest


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL/share-mode fence")
def test_windows_snapshot_fence_blocks_post_capture_mutation_and_namespace_replace(
    tmp_path: Path,
) -> None:
    package_windows = _load_package_windows()
    snapshot_dir = tmp_path / "snapshot"
    nested = snapshot_dir / "exact-source-static" / "examples" / "tt_demo"
    nested.mkdir(parents=True)
    executable = snapshot_dir / "Autosport.exe"
    static_file = nested / "manifest.json"
    executable.write_bytes(b"MZ-canonical")
    static_file.write_bytes(b'{"kind":"canonical"}\n')
    manifest = {
        executable: _sha256(executable),
        static_file: _sha256(static_file),
    }
    hostile = tmp_path / "hostile.exe"
    hostile.write_bytes(b"MZ-hostile")

    with package_windows._package_input_write_fence(snapshot_dir, manifest):
        assert executable.read_bytes() == b"MZ-canonical"
        assert static_file.read_bytes() == b'{"kind":"canonical"}\n'
        with pytest.raises(OSError):
            executable.write_bytes(b"MZ-mutated")
        with pytest.raises(OSError):
            os.replace(hostile, executable)
        with pytest.raises(OSError):
            (nested / "extra.json").write_text("hostile\n", encoding="utf-8")

    executable.write_bytes(b"MZ-cleanup-proved-writable")
    assert executable.read_bytes() == b"MZ-cleanup-proved-writable"


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL/share-mode fence")
def test_windows_snapshot_fence_fails_closed_on_preopened_writer(tmp_path: Path) -> None:
    package_windows = _load_package_windows()
    snapshot_dir = tmp_path / "snapshot"
    snapshot_dir.mkdir()
    executable = snapshot_dir / "Autosport.exe"
    executable.write_bytes(b"MZ-canonical")
    manifest = {executable: _sha256(executable)}

    with executable.open("r+b") as writer:
        writer.seek(0)
        with pytest.raises(OSError, match="file read fence"):
            with package_windows._package_input_write_fence(snapshot_dir, manifest):
                raise AssertionError("preopened writer must prevent fence acquisition")
