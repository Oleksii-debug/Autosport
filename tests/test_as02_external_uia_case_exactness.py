"""Plan 6 §2: external UIA accessible-name and role acceptance is case-exact.

Portable source-contract tests; physical Windows UIA/NVDA remains a separate gate.
"""
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "external_uia_audit.ps1"


def _validation_block() -> str:
    source = SCRIPT.read_text(encoding="utf-8")
    start = source.index("if ($currentName -cne [string]$spec.name)")
    end = source.index("if ([bool]$spec.require_external_focus", start)
    return source[start:end]


def test_external_uia_name_case_only_mismatch_is_rejected() -> None:
    block = _validation_block()
    assert "if ($currentName -cne [string]$spec.name) {" in block
    assert "external UIA Name mismatch" in block
    assert "if ($currentName -ne $spec.name)" not in block


def test_external_uia_control_type_case_only_mismatch_is_rejected() -> None:
    block = _validation_block()
    assert "$controlType -cne [string]$spec.expected_control_type" in block
    assert "external UIA ControlType mismatch" in block
    assert "$controlType -ne [string]$spec.expected_control_type" not in block


def test_exported_metadata_matches_same_case_exactness() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "name = $(if ($currentName -ceq [string]$spec.name)" in source
    assert "control_type = $(if ($controlType -ceq [string]$spec.expected_control_type)" in source
    assert "'[redacted mismatch]'" in source
