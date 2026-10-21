from __future__ import annotations

import hashlib
import json
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


def _write_snapshot(root: Path) -> dict[str, str]:
    files = {
        "src/autosport/release_package.py": (
            "def _require_git_commit_sha(value, *, field):\n"
            "    return value\n"
            "def build_windows_package(*args, **kwargs):\n"
            "    return None\n"
            "def verify_windows_package(*args, **kwargs):\n"
            "    return {}\n"
        ),
        "src/autosport/release_sbom.py": "SENTINEL = 'trusted-sbom-module'\n",
        "src/autosport/release_dependency_graph.py": "SENTINEL = 'trusted-graph-module'\n",
        "src/autosport/windows_pe_integrity.py": "SENTINEL = 'trusted-pe-module'\n",
        "src/autosport/windows_update_admission.py": "SENTINEL = 'trusted-update-admission'\n",
        "src/autosport/windows_update_release_metadata.py": (
            "from autosport.windows_update_admission import SENTINEL\n"
        ),
        "src/autosport/data_tool_package.py": (
            "from autosport.release_package import verify_windows_package\n"
            "SENTINEL = 'trusted-data-module'\n"
            "def bind_portable_data_tool(*args, **kwargs):\n"
            "    return {}\n"
            "def verify_portable_data_tool(*args, **kwargs):\n"
            "    return {}\n"
        ),
        "scripts/package_windows.py": (
            "import sys\n"
            "from pathlib import Path\n"
            "from autosport.data_tool_package import SENTINEL\n"
            "Path(sys.argv[1]).write_text(SENTINEL, encoding='utf-8')\n"
        ),
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
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "trusted package source SHA-256 mismatch" in completed.stderr
    assert not hostile_marker.exists()
    assert not marker.exists()


def test_windows_build_materializes_exact_package_consumer_after_final_source_gate() -> None:
    script = _BUILD_SCRIPT.read_text(encoding="utf-8")

    final_gate = "python $sourceVerifier --source-sha $sourceSha --late-build-boundary --allow-release-outputs"
    archive = (
        "& $gitExecutable archive --format=zip \"--output=$trustedPackageArchive\" $sourceSha -- "
        "scripts/package_windows.py src/autosport/release_package.py "
        "src/autosport/release_sbom.py src/autosport/release_dependency_graph.py "
        "src/autosport/data_tool_package.py "
        "src/autosport/windows_pe_integrity.py "
        "src/autosport/windows_update_admission.py "
        "src/autosport/windows_update_release_metadata.py"
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
    assert 'load_module("autosport.release_sbom", "src/autosport/release_sbom.py")' in launcher
    assert 'load_module("autosport.release_dependency_graph", "src/autosport/release_dependency_graph.py")' in launcher
    assert 'load_module("autosport.windows_pe_integrity", "src/autosport/windows_pe_integrity.py")' in launcher
    assert 'load_module("autosport.windows_update_admission", "src/autosport/windows_update_admission.py")' in launcher
    assert 'load_module("autosport.windows_update_release_metadata", "src/autosport/windows_update_release_metadata.py")' in launcher
    assert 'load_module("autosport.release_package", "src/autosport/release_package.py")' in launcher
    assert 'load_module("autosport.data_tool_package", "src/autosport/data_tool_package.py")' in launcher


def test_trusted_launcher_refuses_mutated_graph_module_before_execution(tmp_path: Path) -> None:
    launcher = _trusted_package_launcher_source()
    root = tmp_path / "snapshot"
    manifest = _write_snapshot(root)
    graph_module = root / "src/autosport/release_dependency_graph.py"
    graph_module.write_text("raise AssertionError('untrusted graph executed')\n", encoding="utf-8")
    marker = tmp_path / "marker.txt"
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", launcher, str(root),
         json.dumps(manifest, sort_keys=True, separators=(",", ":")), str(marker)],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "trusted package source SHA-256 mismatch" in result.stderr
    assert "untrusted graph executed" not in result.stderr
    assert not marker.exists()


def test_trusted_launcher_refuses_mutated_pe_oracle_before_script_runs(tmp_path: Path) -> None:
    launcher = _trusted_package_launcher_source()
    root = tmp_path / "snapshot"
    manifest = _write_snapshot(root)
    pe_module = root / "src" / "autosport" / "windows_pe_integrity.py"
    pe_module.write_text("raise AssertionError('untrusted PE oracle executed')\n", encoding="utf-8")
    marker = tmp_path / "marker.txt"
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", launcher, str(root),
         json.dumps(manifest, sort_keys=True, separators=(",", ":")), str(marker)],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "trusted package source SHA-256 mismatch" in result.stderr
    assert "untrusted PE oracle executed" not in result.stderr
    assert not marker.exists()


def test_trusted_launcher_imports_real_release_modules_in_isolation(tmp_path: Path) -> None:
    """Catch a missing preload: production package has no ambient sys.path fallback."""
    launcher = _trusted_package_launcher_source()
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "snapshot"
    names = (
        "release_sbom",
        "release_dependency_graph",
        "windows_pe_integrity",
        "windows_update_admission",
        "windows_update_release_metadata",
        "release_package",
        "data_tool_package",
    )
    payloads = {
        f"src/autosport/{name}.py": (
            repository / "src" / "autosport" / f"{name}.py"
        ).read_bytes()
        for name in names
    }
    payloads["scripts/package_windows.py"] = (
        "from autosport.release_package import verify_windows_package\n"
        "from autosport.windows_pe_integrity import validate_pe32plus_amd64\n"
        "from autosport.windows_update_release_metadata import validate_embedded_update_identity\n"
        "from pathlib import Path\n"
        "import sys\n"
        "Path(sys.argv[1]).write_text('trusted-imports-ready', encoding='utf-8')\n"
    ).encode("utf-8")
    manifest = {}
    for relative, payload in payloads.items():
        destination = root.joinpath(*relative.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        manifest[relative] = hashlib.sha256(payload).hexdigest()
    marker = tmp_path / "trusted-imports.txt"
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", launcher, str(root),
         json.dumps(manifest, sort_keys=True, separators=(",", ":")), str(marker)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert marker.read_text(encoding="utf-8") == "trusted-imports-ready"
