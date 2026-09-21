from __future__ import annotations

from pathlib import Path

import autosport.accessibility_audit as base_accessibility_audit
from autosport import windows_entry
from autosport.product_windows_gui import (
    PRODUCT_RUNTIME_AUTOMATION_IDS,
    ProductWindowsAutosportApp,
)


def test_packaged_accessibility_entrypoint_gates_product_runtime_controls(
    monkeypatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, object] = {}

    def fake_canonical_run(output_path: str | Path) -> int:
        observed.update(
            {
                "path": Path(output_path),
                "app_class": base_accessibility_audit.WindowsAutosportApp,
                "patterns": dict(base_accessibility_audit._REQUIRED_PATTERNS),
                "roles": dict(base_accessibility_audit._EXPECTED_ROLES),
            }
        )
        return 0

    monkeypatch.setattr(
        base_accessibility_audit,
        "run_accessibility_audit",
        fake_canonical_run,
    )
    destination = tmp_path / "accessibility.json"
    assert windows_entry.main(["--accessibility-audit-output", str(destination)]) == 0

    assert observed["path"] == destination
    app_class = observed["app_class"]
    assert isinstance(app_class, type)
    assert issubclass(app_class, ProductWindowsAutosportApp)

    patterns = observed["patterns"]
    roles = observed["roles"]
    assert isinstance(patterns, dict)
    assert isinstance(roles, dict)

    expected = {
        "start": ({"INVOKE"}, "PUSH_BUTTON"),
        "stop": ({"INVOKE"}, "PUSH_BUTTON"),
        "status": ({"VALUE"}, "TEXT"),
    }
    for name, (required_patterns, expected_role) in expected.items():
        automation_id = PRODUCT_RUNTIME_AUTOMATION_IDS[name]
        assert patterns.get(automation_id) == required_patterns
        assert roles.get(automation_id) == expected_role
