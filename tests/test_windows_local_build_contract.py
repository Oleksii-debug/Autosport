from pathlib import Path


_BUILD_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_windows.ps1"


def _build_script_text() -> str:
    return _BUILD_SCRIPT.read_text(encoding="utf-8")


def test_local_windows_build_proves_pristine_source_before_mutation() -> None:
    script = _build_script_text()

    source_sha = "$sourceSha = $env:AUTOSPORT_SOURCE_SHA"
    preflight = "python scripts/verify_source_checkout.py --source-sha $sourceSha"
    first_mutation = "python -m pip install --upgrade pip"

    assert script.index(source_sha) < script.index(preflight) < script.index(first_mutation)
    assert script.count(source_sha) == 1
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
