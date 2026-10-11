from pathlib import Path


_BUILD_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_windows.ps1"


def _build_script_text() -> str:
    return _BUILD_SCRIPT.read_text(encoding="utf-8")


def test_local_windows_build_proves_pristine_source_before_mutation() -> None:
    script = _build_script_text()

    git_sanitizer = "Get-ChildItem Env: | Where-Object { $_.Name -like 'GIT_*' }"
    source_sha = "$sourceSha = $env:AUTOSPORT_SOURCE_SHA"
    exact_tree_lookup = (
        "$verifierTreeEntry = (& $gitExecutable ls-tree $sourceSha -- "
        "'scripts/verify_source_checkout.py').Trim()"
    )
    exact_blob_capture = "$verifierBootstrapInfo = [System.Diagnostics.ProcessStartInfo]::new()"
    exact_blob_arg = "[void]$verifierBootstrapInfo.ArgumentList.Add($verifierBlobSha)"
    bound_digest = (
        "$sourceVerifierSha256 = ([System.BitConverter]::ToString("
        "$verifierHasher.ComputeHash($verifierBytes))).Replace('-', '').ToLowerInvariant()"
    )
    publish_path = "$sourceVerifier = (New-TemporaryFile).FullName"
    preflight = "python $sourceVerifier --source-sha $sourceSha"
    first_mutation = "python -m pip install --upgrade pip"

    assert (
        script.index(git_sanitizer)
        < script.index(source_sha)
        < script.index(exact_tree_lookup)
        < script.index(exact_blob_capture)
        < script.index(exact_blob_arg)
        < script.index(bound_digest)
        < script.index(publish_path)
        < script.index(preflight)
        < script.index(first_mutation)
    )
    assert script.count(source_sha) == 1
    assert "python scripts/verify_source_checkout.py --source-sha $sourceSha" not in script
    assert "$env:GIT_NO_REPLACE_OBJECTS = '1'" in script
    assert "hashlib.sha256(data).hexdigest()" in script
    assert "Get-FileHash -LiteralPath $sourceVerifier -Algorithm SHA256" not in script
    assert (
        "& $script:pythonExecutable -I -S -c $script:trustedVerifierLauncher "
        "$script:sourceVerifier $script:sourceVerifierSha256 @remaining"
    ) in script
    assert 'if ($LASTEXITCODE -ne 0) { throw "Source checkout preflight exited $LASTEXITCODE" }' in script


def test_local_windows_build_scalarizes_application_resolution() -> None:
    script = _build_script_text()

    assert "(Get-Command git -CommandType Application -ErrorAction Stop).Source" not in script
    assert "(Get-Command python -CommandType Application -ErrorAction Stop).Source" not in script
    assert "$gitCommands = @(Get-Command git -CommandType Application -ErrorAction Stop)" in script
    assert "$gitExecutable = [string]$gitCommands[0].Source" in script
    assert "$pythonCommands = @(Get-Command python -CommandType Application -ErrorAction Stop)" in script
    assert "$pythonExecutable = [string]$pythonCommands[0].Source" in script
    assert "Resolved Git application has an empty source path" in script
    assert "Resolved Python application has an empty source path" in script


def test_local_windows_build_runs_canonical_full_pytest_gate() -> None:
    script = _build_script_text()

    dependency_install = "python -m pip install -e '.[build,test]'"
    pytest_gate = "python -m pytest -v tests"
    first_build = "& $packagingPython -I -m PyInstaller"

    assert script.index(dependency_install) < script.index(pytest_gate) < script.index(first_build)
    assert "python -m unittest discover" not in script
    assert 'if ($LASTEXITCODE -ne 0) { throw "build/test dependency install exited $LASTEXITCODE" }' in script
    assert 'if ($LASTEXITCODE -ne 0) { throw "Full pytest gate exited $LASTEXITCODE" }' in script


def test_local_windows_build_uses_trusted_snapshot_immediately_before_pyinstaller() -> None:
    script = _build_script_text()

    dataset_smoke = "python -m autosport dataset examples/tt_demo --workspace .build-smoke-workspace"
    smoke_cleanup = "if (Test-Path '.build-smoke-workspace') { Remove-Item -Recurse -Force '.build-smoke-workspace' }"
    trusted_gate = "python $sourceVerifier --source-sha $sourceSha --late-build-boundary"
    live_late_gate = "python scripts/verify_source_checkout.py --source-sha $sourceSha --late-build-boundary"
    gate_check = 'if ($LASTEXITCODE -ne 0) { throw "Trusted source gate before Autosport.exe exited $LASTEXITCODE" }'
    write_fence = (
        '& icacls $trustedBuildRoot /deny '
        '"*${currentSid}:(OI)(CI)(WD,AD,WEA,WA,DE,DC)" /T /C'
    )
    locked_snapshot_gate = (
        "$trustedBuildManifestJson | & $pythonExecutable -I -S -c "
        "$trustedSourceSnapshotVerifierLauncher $trustedBuildRoot"
    )
    first_build = (
        "& $packagingPython -I -m PyInstaller --noconfirm --clean --onefile --windowed "
        "--paths $trustedBuildSrc --add-data $trustedWebAssetsSpec --distpath $pyInstallerDist "
        "--workpath $pyInstallerWork --specpath $pyInstallerSpec --name Autosport $trustedGuiEntry"
    )

    dataset_index = script.index(dataset_smoke)
    cleanup_index = script.index(smoke_cleanup, dataset_index)
    gate_index = script.index(trusted_gate)
    fence_index = script.index(write_fence)
    locked_gate_index = script.index(locked_snapshot_gate, fence_index)
    first_build_index = script.index(first_build)

    assert live_late_gate not in script
    assert dataset_index < cleanup_index < gate_index < fence_index < locked_gate_index < first_build_index
    assert gate_index < script.index(gate_check) < fence_index
    assert script.count(trusted_gate) == 3
    assert script.count(locked_snapshot_gate) == 4
    assert script.count(smoke_cleanup) == 2


def test_local_windows_build_phase_separates_later_release_outputs() -> None:
    script = _build_script_text()

    strict_gate = "python $sourceVerifier --source-sha $sourceSha --late-build-boundary"
    release_gate = strict_gate + " --allow-release-outputs"
    first_build = (
        "& $packagingPython -I -m PyInstaller --noconfirm --clean --onefile --windowed "
        "--paths $trustedBuildSrc --add-data $trustedWebAssetsSpec --distpath $pyInstallerDist "
        "--workpath $pyInstallerWork --specpath $pyInstallerSpec --name Autosport $trustedGuiEntry"
    )
    second_build = (
        "& $packagingPython -I -m PyInstaller --noconfirm --clean --onefile --console "
        "--paths $trustedBuildSrc --distpath $pyInstallerDist --workpath $pyInstallerWork "
        "--specpath $pyInstallerSpec --name Autosport-Data $trustedDataEntry"
    )
    package_command = "python scripts/package_windows.py `"

    first_gate_index = script.index(strict_gate)
    first_build_index = script.index(first_build)
    second_gate_index = script.index(release_gate, first_build_index)
    second_build_index = script.index(second_build)
    package_gate_index = script.index(release_gate, second_build_index)
    package_index = script.index(package_command)

    assert script.count(release_gate) == 2
    assert first_gate_index < first_build_index < second_gate_index < second_build_index
    assert second_build_index < package_gate_index < package_index


def test_local_windows_build_binds_pyinstaller_outputs_before_consumption() -> None:
    script = _build_script_text()

    first_build = (
        "& $packagingPython -I -m PyInstaller --noconfirm --clean --onefile --windowed "
        "--paths $trustedBuildSrc --add-data $trustedWebAssetsSpec --distpath $pyInstallerDist "
        "--workpath $pyInstallerWork --specpath $pyInstallerSpec --name Autosport $trustedGuiEntry"
    )
    first_bind = (
        "python $sourceVerifier --bind-artifact $builtAutosportExe "
        "--bound-output $boundAutosportExe --digest-output $autosportDigestPath"
    )
    second_build = (
        "& $packagingPython -I -m PyInstaller --noconfirm --clean --onefile --console "
        "--paths $trustedBuildSrc --distpath $pyInstallerDist --workpath $pyInstallerWork "
        "--specpath $pyInstallerSpec --name Autosport-Data $trustedDataEntry"
    )
    second_bind = (
        "python $sourceVerifier --bind-artifact $builtDataExe "
        "--bound-output $boundDataExe --digest-output $dataDigestPath"
    )
    verify_gui = "python $sourceVerifier --verify-artifact $boundAutosportExe --expected-sha256 $autosportExeSha256"
    verify_data = "python $sourceVerifier --verify-artifact $boundDataExe --expected-sha256 $dataExeSha256"
    package_command = "python scripts/package_windows.py `"

    assert script.index(first_build) < script.index(first_bind)
    assert script.index(second_build) < script.index(second_bind)
    assert "Start-Process -FilePath $boundAutosportExe" in script
    assert "$dataExe = $boundDataExe" in script
    assert script.index(verify_gui) < script.index(package_command)
    assert script.index(verify_data) < script.index(package_command)
    assert "--exe $boundAutosportExe `" in script
    assert "--data-exe $boundDataExe `" in script


def test_local_windows_build_fails_closed_on_release_native_steps() -> None:
    script = _build_script_text()

    required_checks = (
        (
            "python -m autosport dataset examples/tt_demo --workspace .build-smoke-workspace",
            'if ($LASTEXITCODE -ne 0) { throw "Demo dataset smoke exited $LASTEXITCODE" }',
        ),
        (
            "& $packagingPython -I -m PyInstaller --noconfirm --clean --onefile --windowed --paths $trustedBuildSrc --add-data $trustedWebAssetsSpec --distpath $pyInstallerDist --workpath $pyInstallerWork --specpath $pyInstallerSpec --name Autosport $trustedGuiEntry",
            'if ($LASTEXITCODE -ne 0) { throw "Autosport PyInstaller exited $LASTEXITCODE" }',
        ),
        (
            "& $packagingPython -I -m PyInstaller --noconfirm --clean --onefile --console --paths $trustedBuildSrc --distpath $pyInstallerDist --workpath $pyInstallerWork --specpath $pyInstallerSpec --name Autosport-Data $trustedDataEntry",
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

def test_local_windows_build_known_folder_first_run_scrubs_all_storage_overrides() -> None:
    script = _build_script_text()

    save_local = "$originalLocalAppData = $env:LOCALAPPDATA"
    save_workspace = "$originalAutosportWorkspace = $env:AUTOSPORT_WORKSPACE"
    detect_local = "$hadLocalAppData = Test-Path Env:LOCALAPPDATA"
    detect_workspace = "$hadAutosportWorkspace = Test-Path Env:AUTOSPORT_WORKSPACE"
    remove_local = "Remove-Item Env:LOCALAPPDATA -ErrorAction SilentlyContinue"
    remove_workspace = "Remove-Item Env:AUTOSPORT_WORKSPACE -ErrorAction SilentlyContinue"
    process_a = "$firstRunProcessA = Start-Process -FilePath $extractedExe"
    process_b = "$firstRunProcessB = Start-Process -FilePath $extractedExe"
    restore_local = "$env:LOCALAPPDATA = $originalLocalAppData"
    restore_workspace = "$env:AUTOSPORT_WORKSPACE = $originalAutosportWorkspace"
    known_folder_claim = "extracted_first_run_storage_known_folder_status = 'PASS'"

    save_local_index = script.index(save_local)
    save_workspace_index = script.index(save_workspace, save_local_index)
    detect_local_index = script.index(detect_local, save_local_index)
    detect_workspace_index = script.index(detect_workspace, save_workspace_index)
    remove_local_index = script.index(remove_local, detect_workspace_index)
    remove_workspace_index = script.index(remove_workspace, remove_local_index)
    process_a_index = script.index(process_a, remove_workspace_index)
    process_b_index = script.index(process_b, process_a_index)
    restore_local_index = script.index(restore_local, process_b_index)
    restore_workspace_index = script.index(restore_workspace, restore_local_index)
    known_folder_claim_index = script.index(known_folder_claim, restore_workspace_index)

    assert save_local_index < detect_local_index < save_workspace_index < detect_workspace_index
    assert detect_workspace_index < remove_local_index < remove_workspace_index < process_a_index
    assert process_a_index < process_b_index < restore_local_index < restore_workspace_index
    assert restore_workspace_index < known_folder_claim_index
    assert script.count(remove_workspace) >= 2


def test_local_windows_build_requires_clean_webview_environment_storage_witness() -> None:
    script = _build_script_text()

    witness = (
        "$freshFirstRunEvidenceA.webview_environment_overrides_clear -ne $true "
        "-or $freshFirstRunEvidenceB.webview_environment_overrides_clear -ne $true"
    )
    failure = (
        "Fresh-extracted first-run storage audit did not prove a clean "
        "WebView2 release environment"
    )
    truth_gate = "$freshFirstRunEvidenceA.real_money_execution -ne $false"

    assert witness in script
    assert failure in script
    assert script.index(witness) < script.index(truth_gate)

def test_local_windows_build_requires_exact_first_run_storage_proof_boundaries() -> None:
    script = _build_script_text()

    workspace_witness = (
        "$freshFirstRunEvidenceA.workspace_canonical_atomic_publication_proven "
        "-ne $true"
    )
    webview_host_witness = (
        "$freshFirstRunEvidenceA.webview_host_writability_proven -ne $true"
    )
    webview_child_boundary = (
        "$freshFirstRunEvidenceA.webview_child_profile_access_proven -ne $false"
    )
    truth_gate = "$freshFirstRunEvidenceA.real_money_execution -ne $false"

    assert workspace_witness in script
    assert webview_host_witness in script
    assert webview_child_boundary in script
    assert script.index(workspace_witness) < script.index(truth_gate)
    assert script.index(webview_host_witness) < script.index(truth_gate)
    assert script.index(webview_child_boundary) < script.index(truth_gate)
    assert "extracted_first_run_workspace_atomic_publication_status = 'PASS'" in script
    assert "extracted_first_run_webview_host_writability_status = 'PASS'" in script
    assert (
        "extracted_first_run_webview_child_profile_access_proven = $false"
        in script
    )
