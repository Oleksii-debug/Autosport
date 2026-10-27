"""Pinned mixed-head diagnostics must never replace exact release acceptance."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replay():
    workflow = (ROOT / '.github/workflows/windows-uia-environment-control.yml').read_text(encoding='utf-8')
    return workflow.split('  packaged-failure-replay:', 1)[1]


def test_replay_is_bounded_read_only_and_restricted_to_the_finisher():
    source = replay()
    assert "github.event.pull_request.number == 2573" in source
    assert 'timeout-minutes: 8' in source
    assert 'contents: read' in source and 'actions: read' in source
    assert 'actions: write' not in source and 'contents: write' not in source
    assert 'persist-credentials: false' in source


def test_failed_bytes_are_pinned_and_completely_verified_before_extraction():
    source = replay()
    assert 'REPLAY_SOURCE_SHA: c5c2890c1ec40afa005705bc82d4d149a3413241' in source
    assert 'REPLAY_PACKAGE_SHA256: 1e523a9cedfafb1cf53e212edcfbd71e3bd5545e79805169984dfa43b21e506c' in source
    assert "artifact-ids: '12697343003'" in source and "run-id: '40414794336'" in source
    # ID-based downloads otherwise nest even one artifact beneath its name.
    assert 'merge-multiple: true' in source
    assert 'actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093' in source
    assert source.index('Get-FileHash -LiteralPath $env:REPLAY_ARCHIVE') < source.index('verify_windows_package(') < source.index('Expand-Archive')
    for field in ('source_manifest', 'dependency_metadata', 'dependency_graph',
                  'dependency_license_evidence', 'runtime_license_evidence',
                  'embedded_executable_inventory', 'binary_module_attribution',
                  'complete_binary_sbom'):
        assert f'require_{field}=True' in source
    assert "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }" in source


def test_replay_uses_the_existing_real_audit_and_preserves_failure():
    source = replay()
    assert "scripts/external_uia_audit.ps1" in source
    assert "-Workspace $env:AUTOSPORT_WORKSPACE -WorkingDirectory (Join-Path $env:SystemRoot 'System32')" in source
    assert '$audit.WaitForExit(180000)' in source
    assert '$audit.Kill($true)' in source
    assert 'REPLAY_WATCHDOG_TIMEOUT fixed_stage=$observedStage' in source
    assert 'Receive-Job -Job $audit' not in source
    assert "if ($result.status -cne 'PASS') { throw" in source
    assert 'Stop-Job -Job' not in source
    assert 'icacls $packageRoot /deny' in source


def test_mixed_head_cannot_be_misrepresented_as_release_qualification():
    source = replay()
    assert 'MIXED_HEAD_FAILURE_REPLAY_NOT_RELEASE_QUALIFICATION' in source
    assert 'archive_source_sha = $env:REPLAY_SOURCE_SHA' in source
    assert 'audit_source_sha = $auditSha' in source
    for field in ('human_tested', 'nvda_verified', 'whole_product_complete', 'real_money_execution'):
        assert f'{field} = $false' in source
    assert 'Windows-packaged-failure-replay-NOT-QUALIFIED' in source
    normal = (ROOT / '.github/workflows/windows-build.yml').read_text(encoding='utf-8')
    assert 'REPLAY_SOURCE_SHA' not in normal and '12697343003' not in normal
    assert "if ($null -eq $external -or $external.status -ne 'PASS')" in normal
    assert 'External UIA fresh-extraction gate' in normal


def test_source_and_frozen_lock_controls_precede_the_primary_gui_audit():
    source = replay()
    assert source.index('Verify old package identity') < source.index('Compare held-lock and two-frozen startup controls') < source.index('Replay existing external audit')
    assert '$env:AUTOSPORT_NATIVE_PRODUCT_EXE = Join-Path' in source
    assert '--junitxml=uia-replay-startup-control.xml' in source
    native = (ROOT / 'tests/test_external_uia_duplicate_native_dialog.py').read_text(encoding='utf-8')
    assert 'env.pop("PYTHONPATH", None)' in native
    assert 'command = [packaged_exe]' in native
    assert 'child, timeout=90 if packaged else 15, process_family=packaged' in native
    assert 'source.index("function Get-ProcessFamilyIds {")' in native
    assert 'allowed = _family_ids(process.pid)' in native
    assert 'owner.value in allowed' in native
    assert '_run_probe(window_owner, handle)' in native


def test_paired_frozen_control_never_queries_the_primary_provider():
    native = (ROOT / 'tests/test_external_uia_duplicate_native_dialog.py').read_text(encoding='utf-8')
    paired = native.split('def test_packaged_primary_then_duplicate_without_primary_uia(', 1)[1]
    assert 'subprocess.Popen([executable], cwd=hostile, env=env)' in paired
    assert '_visible_handle(primary, timeout=20, process_family=True, single=False)' in paired
    assert 'native_core_webview2_environment' in paired
    assert '_run_probe(owner, handle)' in paired and 'duplicate.wait(timeout=10) == 2' in paired
    assert '_run_probe(primary' not in paired
    assert 'capture_duplicate_python_diagnostic.py' in native
    assert 'FAILURE_ONLY_OWNED_FAMILY_STACK_LOCATIONS_NOT_ACCEPTANCE' in native


def test_independent_controls_use_the_same_read_only_package_precondition():
    source = replay()
    control = source.split('- name: Compare held-lock and two-frozen startup controls', 1)[1].split('- name: Replay existing external audit', 1)[0]
    assert '$controlPackageRoot = Split-Path -Parent $env:AUTOSPORT_NATIVE_PRODUCT_EXE' in control
    assert 'icacls $controlPackageRoot /deny ("*" + $controlSid + ":(OI)(CI)(WD,AD,WEA,WA,DE,DC)")' in control
    assert control.index('icacls $controlPackageRoot /deny') < control.index('python -m pytest -v')
    assert '} finally {' in control and 'icacls $controlPackageRoot /remove:d' in control
    assert '$testExit = $LASTEXITCODE' in control and 'exit $testExit' in control


def test_canonical_launcher_control_reuses_the_actual_shipped_block():
    native = (ROOT / 'tests/test_external_uia_duplicate_native_dialog.py').read_text(encoding='utf-8')
    launcher = native.split('def _canonical_threadjob_launch(', 1)[1].split('@pytest.mark.parametrize("launcher"', 1)[0]
    assert 'SCRIPT.read_text(encoding="utf-8")' in launcher
    assert 'DUPLICATE_START_PROCESS_PROBE' in launcher and 'DUPLICATE_START_PROCESS_READY' in launcher
    assert 'Start-Job -ScriptBlock' in launcher and '+ launch' in launcher
    assert '$duplicateProcess.WaitForExit(110000)' in launcher
    assert 'exit ([int]$value[0])' in launcher
    assert 'creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)' in launcher
