from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import test_packaged_path_matrix as m


def _artifact(tmp_path: Path, *, exit_code: int = 0, sleep_seconds: float = 0.0) -> Path:
    root = tmp_path / "artifact"
    root.mkdir()
    (root / "nested").mkdir()
    (root / "nested" / "дані.txt").write_text("дані\n", encoding="utf-8")
    app = root / "app.py"
    app.write_text(
        "import sys,time\n"
        f"time.sleep({sleep_seconds!r})\n"
        f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    return root


def test_tree_identity_ignores_absolute_parent_path(tmp_path: Path) -> None:
    source = _artifact(tmp_path)
    destination = tmp_path / "Каталог з пробілами" / "package"
    copied_sha = m.copy_tree_verified(source, destination)
    assert copied_sha == m.tree_manifest_sha(m.tree_manifest(source))
    assert copied_sha == m.tree_manifest_sha(m.tree_manifest(destination))


def test_render_arguments_expands_case_and_package_tokens(tmp_path: Path) -> None:
    case = tmp_path / "Автоспорт Перевірка"
    package = case / "package"
    rendered = m.render_arguments(
        ("--workspace", "{CASE_ROOT}/workspace", "--package={PACKAGE_ROOT}"),
        case_root=case,
        package_root=package,
    )
    assert rendered[0] == "--workspace"
    assert str(case) in rendered[1]
    assert str(package) in rendered[2]


def test_rejects_executable_traversal() -> None:
    with pytest.raises(m.MatrixError):
        m._safe_relative_path(Path("..") / "outside.exe")


def test_rejects_symlinked_artifact_member(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    target = artifact / "nested" / "дані.txt"
    link = artifact / "link.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises(m.MatrixError, match="symlink"):
        m.tree_manifest(artifact)


def test_rejects_output_directory_inside_artifact(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    with pytest.raises(m.MatrixError, match="output directory"):
        m.run_matrix(
            artifact_root=artifact,
            executable_relative_path=Path("app.py"),
            output_dir=artifact / "evidence",
            arguments=(),
            launcher=(sys.executable,),
            mode="exit-zero",
            startup_seconds=0.01,
            timeout_seconds=2,
            keep_copies=False,
        )


def test_matrix_passes_identical_exit_zero_program_under_all_paths(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path, exit_code=0)
    report = m.run_matrix(
        artifact_root=artifact,
        executable_relative_path=Path("app.py"),
        output_dir=tmp_path / "out",
        arguments=("{CASE_ROOT}",),
        launcher=(sys.executable,),
        mode="exit-zero",
        startup_seconds=0.05,
        timeout_seconds=5,
        keep_copies=False,
    )
    assert report.matrix_status == "PASS"
    assert [r.name for r in report.scenarios] == [s[0] for s in m.SCENARIOS]
    assert all(r.status == "PASS" for r in report.scenarios)
    assert all(r.copied_bytes_equal for r in report.scenarios)
    assert report.human_tested is False
    assert report.nvda_verified is False
    assert report.real_money_execution is False
    assert report.whole_product_complete is False
    payload = json.loads((tmp_path / "out" / "path-matrix-report.json").read_text(encoding="utf-8"))
    assert payload["matrix_status"] == "PASS"


def test_persistent_mode_accepts_program_alive_for_probe_window(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path, sleep_seconds=2.0)
    report = m.run_matrix(
        artifact_root=artifact,
        executable_relative_path=Path("app.py"),
        output_dir=tmp_path / "out",
        arguments=(),
        launcher=(sys.executable,),
        mode="persistent",
        startup_seconds=0.1,
        timeout_seconds=2,
        keep_copies=False,
    )
    assert report.matrix_status == "PASS"
    assert all(r.startup_stable for r in report.scenarios)


def test_ascii_control_failure_is_inconclusive_not_path_failure(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path, exit_code=7)
    report = m.run_matrix(
        artifact_root=artifact,
        executable_relative_path=Path("app.py"),
        output_dir=tmp_path / "out",
        arguments=(),
        launcher=(sys.executable,),
        mode="exit-zero",
        startup_seconds=0.01,
        timeout_seconds=2,
        keep_copies=False,
    )
    assert report.matrix_status == "INCONCLUSIVE_CONTROL_FAILED"


def test_no_raw_arguments_are_serialized_in_report(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    secret_marker = "DO_NOT_PERSIST_ME"
    m.run_matrix(
        artifact_root=artifact,
        executable_relative_path=Path("app.py"),
        output_dir=tmp_path / "out",
        arguments=(secret_marker,),
        launcher=(sys.executable,),
        mode="exit-zero",
        startup_seconds=0.01,
        timeout_seconds=2,
        keep_copies=False,
    )
    report_text = (tmp_path / "out" / "path-matrix-report.json").read_text(encoding="utf-8")
    assert secret_marker not in report_text
