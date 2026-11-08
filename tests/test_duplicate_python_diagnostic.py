"""Portable privacy/command contracts for failure-only stack diagnostics."""
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "capture_duplicate_python_diagnostic.py"
spec = importlib.util.spec_from_file_location("duplicate_stack_diagnostic", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def frame(filename="C:/private/token=secret/windows_entry.py", line=93, **extra):
    return dict(filename=filename, line=line, locals=None, name="password=secret", **extra)


def test_export_only_known_module_and_numeric_line():
    payload = [{"frames": [frame(), frame("evil-password.py"), frame("<frozen importlib._bootstrap>", 488)],
                "thread_name": "token=secret", "process_info": {"command_line": "secret"}}]
    value = mod.sanitize(payload, {"windows_entry.py"})
    assert value == {"status": "CAPTURED", "thread_count": 1,
        "locations": [{"module": "windows_entry.py", "line": 93},
                      {"module": "IMPORT_BOOTSTRAP", "line": 488}], "omitted_frames": 1}
    assert "secret" not in json.dumps(value)


@pytest.mark.parametrize("bad", [None, {}, [], [{"frames": None}],
    [{"frames": [dict(filename="windows_entry.py", line=True, locals=None)]}],
    [{"frames": [dict(filename="windows_entry.py", line=-1, locals=None)]}],
    [{"frames": [dict(filename="windows_entry.py", line=2_000_000, locals=None)]}],
    [{"frames": [dict(filename="windows_entry.py", line=3, locals=[{"password": "secret"}])]}]])
def test_invalid_or_local_variable_payload_rejected(bad):
    with pytest.raises(ValueError):
        mod.sanitize(bad, {"windows_entry.py"})


def test_frame_output_is_bounded_and_deduplicated():
    payload = [{"frames": [frame(line=n) for n in range(40)] + [frame(line=0)]}]
    value = mod.sanitize(payload, {"windows_entry.py"})
    assert len(value["locations"]) == 32
    assert value["omitted_frames"] == 8


def test_sampler_never_requests_locals_pause_or_arbitrary_subprocesses(tmp_path, monkeypatch):
    (tmp_path / "windows_entry.py").write_text("# fixture", encoding="utf-8")
    observed = []
    def run(args, **kwargs):
        observed.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps([{"frames": [frame()]}]).encode())
    monkeypatch.setattr(mod.subprocess, "run", run)
    assert mod.capture("sampler.exe", 1234, tmp_path)["status"] == "CAPTURED"
    assert observed == [(["sampler.exe", "dump", "--json", "--nonblocking", "--pid", "1234"],
        {"capture_output": True, "timeout": 3, "check": False})]


@pytest.mark.parametrize("result,status", [
    (SimpleNamespace(returncode=1, stdout=b"password=secret"), "SAMPLER_ERROR"),
    (SimpleNamespace(returncode=0, stdout=b"x"*1_000_001), "PAYLOAD_LIMIT"),
    (SimpleNamespace(returncode=0, stdout=b"secret"), "INVALID_PAYLOAD")])
def test_sampler_failures_are_fixed_redacted_codes(tmp_path, monkeypatch, result, status):
    (tmp_path / "windows_entry.py").write_text("# fixture", encoding="utf-8")
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: result)
    assert mod.capture("sampler.exe", 1234, tmp_path) == {"status": status}


@pytest.mark.parametrize("failure,status", [
    (subprocess.TimeoutExpired(["secret"], 3), "SAMPLER_TIMEOUT"),
    (PermissionError("password=secret"), "SAMPLER_ERROR")])
def test_no_retry_or_escalation_after_sampler_error(tmp_path, monkeypatch, failure, status):
    (tmp_path / "windows_entry.py").write_text("# fixture", encoding="utf-8")
    calls = []
    def run(*args, **kwargs):
        calls.append(1)
        raise failure
    monkeypatch.setattr(mod.subprocess, "run", run)
    assert mod.capture("sampler.exe", 1234, tmp_path) == {"status": status}
    assert calls == [1]


def test_failure_sampler_cannot_run_before_audit_gate_failure():
    source = (ROOT / "scripts" / "external_uia_audit.ps1").read_text(encoding="utf-8")
    catch = source.index("} catch {\n    if ($script:lastDesktopWorkerStage")
    assert source.index("$report.duplicate_python_stacks = Get-DuplicatePythonStackDiagnostics", catch) > catch
    assert "Get-DuplicatePythonStackDiagnostics" not in source[source.index("$process = $null"):catch]
    workflow = (ROOT / ".github/workflows/windows-build.yml").read_text(encoding="utf-8")
    assert "py-spy==0.4.2" in workflow
    assert workflow.index("Prepare failure-only Python stack sampler") > workflow.index("Audit packaged research demo path")
