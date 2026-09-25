from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


SOURCE_SHA = "a" * 40


def _command(
    *,
    root: Path,
    report_path: Path,
    budget_path: Path,
    output_path: Path,
) -> tuple[list[str], dict[str, str]]:
    script = root / "scripts" / "qualify_endurance_performance.py"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return (
        [
            sys.executable,
            str(script),
            str(report_path),
            str(budget_path),
            "--source-sha",
            SOURCE_SHA,
            "--machine-profile",
            "stale-output-fixture",
            "--output",
            str(output_path),
        ],
        env,
    )


def _run(
    *,
    root: Path,
    report_path: Path,
    budget_path: Path,
    output_path: Path,
) -> subprocess.CompletedProcess[str]:
    command, env = _command(
        root=root,
        report_path=report_path,
        budget_path=budget_path,
        output_path=output_path,
    )
    return subprocess.run(
        command,
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _load_qualification_script(root: Path):
    script = root / "scripts" / "qualify_endurance_performance.py"
    spec = importlib.util.spec_from_file_location(
        "_autosport_qualify_endurance_performance_test", script
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_stale_pass(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": "autosport.endurance-performance-qualification",
                "status": "PASS",
                "qualification_id": "stale-pass",
            }
        ),
        encoding="utf-8",
    )


def test_invalid_run_removes_preexisting_pass_evidence(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    report_path = tmp_path / "report.json"
    budget_path = tmp_path / "budget.json"
    output_path = tmp_path / "qualification.json"

    report_path.write_text("{", encoding="utf-8")
    budget_path.write_text("{}", encoding="utf-8")
    _write_stale_pass(output_path)

    result = _run(
        root=root,
        report_path=report_path,
        budget_path=budget_path,
        output_path=output_path,
    )

    assert result.returncode == 3
    assert "performance_qualification=INVALID" in result.stdout
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr
    assert not output_path.exists()


def test_output_cannot_alias_report_input(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    report_path = tmp_path / "report.json"
    budget_path = tmp_path / "budget.json"
    report_text = "{}"
    report_path.write_text(report_text, encoding="utf-8")
    budget_path.write_text("{}", encoding="utf-8")

    result = _run(
        root=root,
        report_path=report_path,
        budget_path=budget_path,
        output_path=report_path,
    )

    assert result.returncode == 3
    assert "must not overwrite report or budget input" in result.stdout
    assert report_path.read_text(encoding="utf-8") == report_text


def test_unknown_argument_cannot_leave_stale_pass_evidence(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    report_path = tmp_path / "report.json"
    budget_path = tmp_path / "budget.json"
    output_path = tmp_path / "qualification.json"
    report_path.write_text("{}", encoding="utf-8")
    budget_path.write_text("{}", encoding="utf-8")
    _write_stale_pass(output_path)

    command, env = _command(
        root=root,
        report_path=report_path,
        budget_path=budget_path,
        output_path=output_path,
    )
    command.append("--unexpected-qualification-option")
    result = subprocess.run(
        command,
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr
    assert not output_path.exists()


def test_missing_required_option_cannot_leave_stale_pass_evidence(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    report_path = tmp_path / "report.json"
    budget_path = tmp_path / "budget.json"
    output_path = tmp_path / "qualification.json"
    report_path.write_text("{}", encoding="utf-8")
    budget_path.write_text("{}", encoding="utf-8")
    _write_stale_pass(output_path)

    command, env = _command(
        root=root,
        report_path=report_path,
        budget_path=budget_path,
        output_path=output_path,
    )
    source_option = command.index("--source-sha")
    del command[source_option : source_option + 2]
    result = subprocess.run(
        command,
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "--source-sha" in result.stderr
    assert not output_path.exists()


def test_atomic_writer_closes_raw_fd_when_fdopen_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).resolve().parents[1]
    module = _load_qualification_script(root)
    output_path = tmp_path / "qualification.json"
    captured: dict[str, int] = {}

    def fail_fdopen(fd: int, *args: object, **kwargs: object):
        captured["fd"] = fd
        raise OSError("fdopen sentinel failure")

    monkeypatch.setattr(module.os, "fdopen", fail_fdopen)

    with pytest.raises(OSError, match="fdopen sentinel failure"):
        module._write_json_atomic(output_path, {"status": "PASS"})

    with pytest.raises(OSError):
        os.fstat(captured["fd"])
    assert not output_path.exists()
    assert list(tmp_path.iterdir()) == []
