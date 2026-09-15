from pathlib import Path


_BUILD_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_windows.ps1"


def _build_script_text() -> str:
    return _BUILD_SCRIPT.read_text(encoding="utf-8")


def _all_indices(text: str, needle: str) -> list[int]:
    indices: list[int] = []
    start = 0
    while True:
        index = text.find(needle, start)
        if index < 0:
            return indices
        indices.append(index)
        start = index + len(needle)


def test_local_windows_build_proves_pristine_source_before_mutation() -> None:
    script = _build_script_text()

    source_sha = "$sourceSha = $env:AUTOSPORT_SOURCE_SHA"
    preflight = "python scripts/verify_source_checkout.py --source-sha $sourceSha"
    disable_repo_bytecode = "$env:PYTHONDONTWRITEBYTECODE = '1'"
    first_mutation = "python -m pip install --upgrade pip"

    assert (
        script.index(source_sha)
        < script.index(preflight)
        < script.index(disable_repo_bytecode)
        < script.index(first_mutation)
    )
    assert script.count(source_sha) == 1
    assert script.count(disable_repo_bytecode) == 1
    assert 'if ($LASTEXITCODE -ne 0) { throw "Source checkout preflight exited $LASTEXITCODE" }' in script


def test_local_windows_build_runs_canonical_full_pytest_gate() -> None:
    script = _build_script_text()

    dependency_install = "python -m pip install -e '.[build,test]'"
    pytest_gate = "python -m pytest -v tests"
    first_build = "python -m PyInstaller"

    assert script.index(dependency_install) < script.index(pytest_gate) < script.index(first_build)
    assert "python -m unittest discover" not in script
    assert 'if ($LASTEXITCODE -ne 0) { throw "build/test dependency install exited $LASTEXITCODE" }' in script
    assert 'if ($LASTEXITCODE -ne 0) { throw "Full pytest gate exited $LASTEXITCODE" }' in script


def test_local_windows_build_reproves_source_before_every_source_consuming_release_step() -> None:
    script = _build_script_text()

    dataset_smoke = "python -m autosport dataset examples/tt_demo --workspace .build-smoke-workspace"
    smoke_cleanup = "if (Test-Path '.build-smoke-workspace') { Remove-Item -Recurse -Force '.build-smoke-workspace' }"
    late_gate = "python scripts/verify_source_checkout.py --source-sha $sourceSha --late-build-boundary"
    first_build = "python -m PyInstaller --noconfirm --clean --onefile --windowed --name Autosport src/autosport/windows_entry.py"
    second_build = "python -m PyInstaller --noconfirm --clean --onefile --console --name Autosport-Data src/autosport/data_tools_entry.py"
    package_command = "python scripts/package_windows.py `"

    dataset_index = script.index(dataset_smoke)
    cleanup_index = script.index(smoke_cleanup, dataset_index)
    late_gate_indices = _all_indices(script, late_gate)

    assert len(late_gate_indices) == 3
    assert dataset_index < cleanup_index < late_gate_indices[0] < script.index(first_build)
    assert script.index(first_build) < late_gate_indices[1] < script.index(second_build)
    assert script.index(second_build) < late_gate_indices[2] < script.index(package_command)
    assert script.count(smoke_cleanup) == 2

    required_gate_failures = (
        'throw "Late source checkout integrity gate before Autosport.exe exited $LASTEXITCODE"',
        'throw "Late source checkout integrity gate before Autosport-Data.exe exited $LASTEXITCODE"',
        'throw "Late source checkout integrity gate before package assembly exited $LASTEXITCODE"',
    )
    for gate_index, failure in zip(late_gate_indices, required_gate_failures, strict=True):
        failure_index = script.index(failure)
        assert gate_index < failure_index


def test_local_windows_build_fails_closed_on_release_native_steps() -> None:
    script = _build_script_text()

    required_checks = (
        (
            "python -m autosport dataset examples/tt_demo --workspace .build-smoke-workspace",
            'if ($LASTEXITCODE -ne 0) { throw "Demo dataset smoke exited $LASTEXITCODE" }',
        ),
        (
            "python -m PyInstaller --noconfirm --clean --onefile --windowed --name Autosport src/autosport/windows_entry.py",
            'if ($LASTEXITCODE -ne 0) { throw "Autosport PyInstaller exited $LASTEXITCODE" }',
        ),
        (
            "python -m PyInstaller --noconfirm --clean --onefile --console --name Autosport-Data src/autosport/data_tools_entry.py",
            'if ($LASTEXITCODE -ne 0) { throw "Autosport-Data PyInstaller exited $LASTEXITCODE" }',
        ),
        (
            "python scripts/package_windows.py `",
            'if ($LASTEXITCODE -ne 0) { throw "Windows package assembly exited $LASTEXITCODE" }',
        ),
    )

    for command, check in required_checks:
        command_index = script.index(command)
        check_index = script.index(check)
        assert command_index < check_index

    package_assignment = "$package = Join-Path $PWD 'dist/Autosport-V1-windows-x64.zip'"
    package_cleanup = "if (Test-Path $package) { Remove-Item -Force $package }"
    verification_cleanup = "if (Test-Path $packageVerification) { Remove-Item -Force $packageVerification }"
    package_command = "python scripts/package_windows.py `"
    assert script.index(package_assignment) < script.index(package_cleanup) < script.index(package_command)
    assert script.index(verification_cleanup) < script.index(package_command)


def test_local_windows_build_binds_real_process_recovery_before_and_after_packaging() -> None:
    script = _build_script_text()

    packaged_call = (
        "Assert-ProcessRecoveryEvidence -Evidence $restartRecoveryEvidence "
        "-Label 'Packaged restart/recovery audit'"
    )
    package_command = "python scripts/package_windows.py `"
    fresh_call = (
        "Assert-ProcessRecoveryEvidence -Evidence $freshRestartRecoveryEvidence "
        "-Label 'Fresh-extracted restart/recovery audit'"
    )
    fresh_evidence_write = "dist/fresh-extraction-verification.json"

    assert script.count("Assert-ProcessRecoveryEvidence -Evidence") == 2
    assert script.index(packaged_call) < script.index(package_command)
    assert script.index(package_command) < script.index(fresh_call) < script.index(fresh_evidence_write)

    for required_binding in (
        "process_kill_relaunch_status",
        "process_kill_stage_pid",
        "process_kill_return_code",
        "process_recovery_pid",
        "process_recovery_run_id",
        "process_recovery_disposition",
        "process_recovery_registry_status",
        "process_recovery_manifest_phase",
        "process_recovery_base_paper_book_sha256",
        "process_recovery_base_decision_ledger_sha256",
        "process_recovery_new_paper_book_sha256",
        "process_recovery_new_decision_ledger_sha256",
    ):
        assert required_binding in script
