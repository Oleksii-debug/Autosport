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
        ("repair-workspace", "--workspace", "{WORKSPACE_ROOT}", "--package={PACKAGE_ROOT}"),
        case_root=case,
        package_root=package,
    )
    assert rendered[:2] == ("repair-workspace", "--workspace")
    assert rendered[2] == str(case / "workspace")
    assert str(package) in rendered[3]


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



def test_stale_report_and_logs_are_removed_before_validation(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    stale_report = out / "path-matrix-report.json"
    stale_report.write_text('{"matrix_status":"PASS","stale":true}\n', encoding="utf-8")
    stale_logs = out / "logs"
    stale_logs.mkdir()
    (stale_logs / "old.log").write_text("SECRET_OLD_OUTPUT", encoding="utf-8")

    report = m.run_matrix(
        artifact_root=artifact,
        executable_relative_path=Path("app.py"),
        output_dir=out,
        arguments=(),
        launcher=(sys.executable,),
        mode="exit-zero",
        startup_seconds=0.01,
        timeout_seconds=2,
        keep_copies=False,
    )
    assert report.matrix_status == "PASS"
    payload = json.loads(stale_report.read_text(encoding="utf-8"))
    assert payload["matrix_status"] == "PASS"
    assert "stale" not in payload
    assert not stale_logs.exists()


def test_stale_report_is_removed_even_when_validation_fails_early(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    stale_report = out / "path-matrix-report.json"
    stale_report.write_text('{"matrix_status":"PASS","stale":true}\n', encoding="utf-8")
    stale_logs = out / "logs"
    stale_logs.mkdir()
    (stale_logs / "old.log").write_text("SECRET_OLD_OUTPUT", encoding="utf-8")

    with pytest.raises(m.MatrixError, match="executable does not exist"):
        m.run_matrix(
            artifact_root=artifact,
            executable_relative_path=Path("missing.exe"),
            output_dir=out,
            arguments=(),
            launcher=(),
            mode="exit-zero",
            startup_seconds=0.01,
            timeout_seconds=2,
            keep_copies=False,
        )
    assert not stale_report.exists()
    assert not stale_logs.exists()


def test_stale_case_workspace_is_removed_before_probe(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    app = artifact / "app.py"
    app.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "workspace = Path(sys.argv[1])\n"
        "raise SystemExit(17 if (workspace / 'stale.txt').exists() else 0)\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    stale = out / "cases" / m.SCENARIOS[0][1] / "workspace"
    stale.mkdir(parents=True)
    (stale / "stale.txt").write_text("old", encoding="utf-8")

    report = m.run_matrix(
        artifact_root=artifact,
        executable_relative_path=Path("app.py"),
        output_dir=out,
        arguments=("{WORKSPACE_ROOT}",),
        launcher=(sys.executable,),
        mode="exit-zero",
        startup_seconds=0.01,
        timeout_seconds=2,
        keep_copies=False,
    )
    assert report.matrix_status == "PASS"


def test_output_capture_is_opt_in(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    quiet_out = tmp_path / "quiet"
    quiet = m.run_matrix(
        artifact_root=artifact,
        executable_relative_path=Path("app.py"),
        output_dir=quiet_out,
        arguments=(),
        launcher=(sys.executable,),
        mode="exit-zero",
        startup_seconds=0.01,
        timeout_seconds=2,
        keep_copies=False,
    )
    assert quiet.matrix_status == "PASS"
    assert all(not row.output_captured for row in quiet.scenarios)
    assert not (quiet_out / "logs").exists()

    captured_out = tmp_path / "captured"
    captured = m.run_matrix(
        artifact_root=artifact,
        executable_relative_path=Path("app.py"),
        output_dir=captured_out,
        arguments=(),
        launcher=(sys.executable,),
        mode="exit-zero",
        startup_seconds=0.01,
        timeout_seconds=2,
        keep_copies=False,
        capture_output=True,
    )
    assert captured.matrix_status == "PASS"
    assert all(row.output_captured for row in captured.scenarios)
    assert (captured_out / "logs" / "ascii_control.log").is_file()



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

def test_matrix_uses_unrelated_cwd_and_isolated_absolute_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _artifact(tmp_path)
    observed: list[tuple[Path, str]] = []

    def fake_probe(
        command: object,
        *,
        cwd: Path,
        environment: dict[str, str] | None = None,
        **_kwargs: object,
    ) -> tuple[bool, bool, int | None, float, str, str | None]:
        assert command
        assert environment is not None
        observed.append((Path(cwd), environment["AUTOSPORT_WORKSPACE"]))
        return True, True, 0, 0.001, "PASS", None

    monkeypatch.setattr(m, "probe_process", fake_probe)
    monkeypatch.setattr(m, "detect_privilege_context", lambda: "ADMINISTRATOR")

    output = tmp_path / "out"
    report = m.run_matrix(
        artifact_root=artifact,
        executable_relative_path=Path("app.py"),
        output_dir=output,
        arguments=("--workspace={WORKSPACE_ROOT}",),
        launcher=(sys.executable,),
        mode="exit-zero",
        startup_seconds=0.01,
        timeout_seconds=2,
        keep_copies=True,
    )

    assert report.schema_version == 2
    assert report.launch_cwd_policy == m.LAUNCH_CWD_POLICY
    assert report.workspace_policy == m.WORKSPACE_POLICY
    assert report.privilege_context == "ADMINISTRATOR"
    assert report.non_admin_verified is False
    assert len(observed) == len(m.SCENARIOS)

    for (cwd, workspace), (_name, component) in zip(observed, m.SCENARIOS, strict=True):
        case_root = output / "cases" / component
        package_root = case_root / "package"
        expected_workspace = (case_root / "workspace").resolve()
        assert cwd == case_root / "launch-cwd"
        assert cwd != package_root
        assert package_root not in cwd.parents
        assert Path(workspace).is_absolute()
        assert Path(workspace) == expected_workspace
        assert cwd != expected_workspace
        assert expected_workspace not in cwd.parents

    payload = json.loads((output / "path-matrix-report.json").read_text(encoding="utf-8"))
    assert payload["launch_cwd_policy"] == "UNRELATED_CASE_DIRECTORY"
    assert payload["workspace_policy"] == "AUTOSPORT_WORKSPACE_PER_CASE_ABSOLUTE"
    assert payload["privilege_context"] == "ADMINISTRATOR"
    assert payload["non_admin_verified"] is False


def test_non_admin_verification_requires_observed_standard_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _artifact(tmp_path)
    monkeypatch.setattr(m, "detect_privilege_context", lambda: "STANDARD_USER")

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

    assert report.privilege_context == "STANDARD_USER"
    assert report.non_admin_verified is True

