from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


_BUILD_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_windows.ps1"


def _trusted_package_launcher_source() -> str:
    script = _BUILD_SCRIPT.read_text(encoding="utf-8")
    start_marker = "$trustedPackageLauncher = @'\n"
    end_marker = "\n'@\n"
    assert start_marker in script
    tail = script.split(start_marker, 1)[1]
    assert end_marker in tail
    return tail.split(end_marker, 1)[0]


def _bound_git_environment() -> dict[str, str]:
    env = os.environ.copy()
    executable = Path(sys.executable).resolve()
    env["AUTOSPORT_BOUND_GIT_EXECUTABLE"] = str(executable)
    env["AUTOSPORT_BOUND_GIT_SHA256"] = hashlib.sha256(executable.read_bytes()).hexdigest()
    return env


def _write_snapshot(root: Path, *, package_source: str | None = None) -> dict[str, str]:
    if package_source is None:
        package_source = (
            "import sys\n"
            "from pathlib import Path\n"
            "from autosport.data_tool_package import SENTINEL\n"
            "Path(sys.argv[1]).write_text(SENTINEL, encoding='utf-8')\n"
        )
    files = {
        "src/autosport/release_package.py": (
            "def _require_git_commit_sha(value, *, field):\n"
            "    return value\n"
            "def build_windows_package(*args, **kwargs):\n"
            "    return None\n"
            "def verify_windows_package(*args, **kwargs):\n"
            "    return {}\n"
        ),
        "src/autosport/data_tool_package.py": (
            "from autosport.release_package import verify_windows_package\n"
            "SENTINEL = 'trusted-data-module'\n"
            "def bind_portable_data_tool(*args, **kwargs):\n"
            "    return {}\n"
            "def verify_portable_data_tool(*args, **kwargs):\n"
            "    return {}\n"
        ),
        "scripts/package_windows.py": package_source,
    }
    manifest: dict[str, str] = {}
    for relative, content in files.items():
        path = root.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = content.encode("utf-8")
        path.write_bytes(payload)
        manifest[relative] = hashlib.sha256(payload).hexdigest()
    return manifest


def test_trusted_package_launcher_executes_only_verified_snapshot_bytes(tmp_path: Path) -> None:
    launcher = _trusted_package_launcher_source()
    root = tmp_path / "trusted-package-source"
    manifest = _write_snapshot(root)
    marker = tmp_path / "marker.txt"

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            launcher,
            str(root),
            json.dumps(manifest, sort_keys=True, separators=(",", ":")),
            str(marker),
        ],
        env=_bound_git_environment(),
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert marker.read_text(encoding="utf-8") == "trusted-data-module"


def test_trusted_package_launcher_rejects_mutated_import_before_execution(tmp_path: Path) -> None:
    launcher = _trusted_package_launcher_source()
    root = tmp_path / "trusted-package-source"
    manifest = _write_snapshot(root)
    marker = tmp_path / "marker.txt"
    hostile_marker = tmp_path / "hostile.txt"
    imported_module = root / "src" / "autosport" / "data_tool_package.py"
    imported_module.write_text(
        "from pathlib import Path\n"
        f"Path({str(hostile_marker)!r}).write_text('executed', encoding='utf-8')\n"
        "SENTINEL = 'hostile'\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            launcher,
            str(root),
            json.dumps(manifest, sort_keys=True, separators=(",", ":")),
            str(marker),
        ],
        env=_bound_git_environment(),
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "trusted package source SHA-256 mismatch" in completed.stderr
    assert not hostile_marker.exists()
    assert not marker.exists()


def test_trusted_package_launcher_uses_bound_git_when_path_is_poisoned(tmp_path: Path) -> None:
    launcher = _trusted_package_launcher_source()
    root = tmp_path / "trusted-package-source"
    marker = tmp_path / "bound-git-marker.txt"
    payload = (
        "from pathlib import Path; "
        f"Path({str(marker)!r}).write_text('bound-git', encoding='utf-8')"
    )
    package_source = (
        "import subprocess\n"
        f"subprocess.run(['git', '-c', {payload!r}], check=True)\n"
    )
    manifest = _write_snapshot(root, package_source=package_source)
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    env = _bound_git_environment()
    env["PATH"] = str(empty_path)

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            launcher,
            str(root),
            json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        ],
        env=env,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert marker.read_text(encoding="utf-8") == "bound-git"


def test_trusted_package_launcher_rejects_bound_git_digest_tampering(tmp_path: Path) -> None:
    launcher = _trusted_package_launcher_source()
    root = tmp_path / "trusted-package-source"
    manifest = _write_snapshot(root)
    marker = tmp_path / "marker.txt"
    env = _bound_git_environment()
    env["AUTOSPORT_BOUND_GIT_SHA256"] = "0" * 64

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            launcher,
            str(root),
            json.dumps(manifest, sort_keys=True, separators=(",", ":")),
            str(marker),
        ],
        env=env,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "bound Git executable SHA-256 mismatch" in completed.stderr
    assert not marker.exists()


def test_windows_build_materializes_exact_package_consumer_after_final_source_gate() -> None:
    script = _BUILD_SCRIPT.read_text(encoding="utf-8")

    final_gate = "python $sourceVerifier --source-sha $sourceSha --late-build-boundary --allow-release-outputs"
    archive = (
        "& $gitExecutable archive --format=zip \"--output=$trustedPackageArchive\" $sourceSha -- "
        "scripts/package_windows.py src/autosport/release_package.py src/autosport/data_tool_package.py"
    )
    package_command = "python scripts/package_windows.py `"
    isolated_runner = (
        "& $script:pythonExecutable -I -S -c $script:trustedPackageLauncher "
        "$script:trustedPackageRoot $script:trustedPackageManifestJson @remaining"
    )

    package_index = script.index(package_command)
    final_gate_index = script.rindex(final_gate, 0, package_index)
    archive_index = script.index(archive, final_gate_index)
    launcher = _trusted_package_launcher_source()

    assert final_gate_index < archive_index < package_index
    assert isolated_runner in script
    assert "$trustedPackageLauncher = @'" in script
    assert "sys.path.insert" not in launcher
    assert "AUTOSPORT_BOUND_GIT_EXECUTABLE" in launcher
    assert "AUTOSPORT_BOUND_GIT_SHA256" in launcher
    assert "subprocess.Popen = _bound_git_popen" in launcher
    assert "[System.IO.FileShare]::Read" in script
    assert 'load_module("autosport.release_package", "src/autosport/release_package.py")' in launcher
    assert 'load_module("autosport.data_tool_package", "src/autosport/data_tool_package.py")' in launcher
