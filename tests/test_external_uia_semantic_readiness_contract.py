from pathlib import Path


_AUDIT_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "external_uia_audit.ps1"


def _audit_script_text() -> str:
    return _AUDIT_SCRIPT.read_text(encoding="utf-8")


def test_external_uia_waits_for_webview_semantics_before_tree_snapshot() -> None:
    script = _audit_script_text()

    startup_deadline = "$deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)"
    semantic_state = "$semanticReady = $null"
    semantic_probe = (
        "$semanticReady = Find-UiaElementForProcessFamily "
        "-ProcessIds $lastFamilyIds -AutomationId '330'"
    )
    semantic_timeout = (
        'throw "Timed out waiting for WebView2 semantic UIA readiness '
        '(automation_id=330) across packaged process family"'
    )
    tree_snapshot = "$descendants = $uiaRoot.FindAll("
    manual_open = "$manualOpen = $semanticReady"

    assert script.count(startup_deadline) == 1
    assert (
        script.index(startup_deadline)
        < script.index(semantic_state)
        < script.index(semantic_probe)
        < script.index(semantic_timeout)
        < script.index(tree_snapshot)
        < script.index(manual_open)
    )


def test_external_uia_semantic_readiness_reuses_bounded_startup_deadline() -> None:
    script = _audit_script_text()

    semantic_state_index = script.index("$semanticReady = $null")
    semantic_timeout_index = script.index(
        'throw "Timed out waiting for WebView2 semantic UIA readiness '
    )
    semantic_block = script[semantic_state_index:semantic_timeout_index]

    assert "while ([DateTime]::UtcNow -lt $deadline)" in semantic_block
    assert "Get-ProcessFamilyIds -RootProcessId $process.Id" in semantic_block
    assert "Find-UiaRootForProcessFamily -ProcessIds $lastFamilyIds" in semantic_block
    assert "Find-UiaElementForProcessFamily -ProcessIds $lastFamilyIds -AutomationId '330'" in semantic_block
    assert "Start-Sleep -Milliseconds 100" in semantic_block
    assert "AddSeconds(" not in semantic_block
