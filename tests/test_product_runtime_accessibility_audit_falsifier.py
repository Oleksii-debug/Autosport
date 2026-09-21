from __future__ import annotations

import autosport.accessibility_audit as accessibility_audit
from autosport.product_windows_gui import (
    PRODUCT_RUNTIME_AUTOMATION_IDS,
    ProductWindowsAutosportApp,
)


def test_accessibility_audit_targets_packaged_product_runtime_controls() -> None:
    assert issubclass(
        accessibility_audit.WindowsAutosportApp,
        ProductWindowsAutosportApp,
    ), "canonical accessibility audit still instantiates the predecessor Windows GUI"

    expected = {
        "start": ({"INVOKE"}, "PUSH_BUTTON"),
        "stop": ({"INVOKE"}, "PUSH_BUTTON"),
        "status": ({"VALUE"}, "TEXT"),
    }
    for name, (patterns, role) in expected.items():
        automation_id = PRODUCT_RUNTIME_AUTOMATION_IDS[name]
        assert accessibility_audit._REQUIRED_PATTERNS.get(automation_id) == patterns
        assert accessibility_audit._EXPECTED_ROLES.get(automation_id) == role

    product_ids = set(PRODUCT_RUNTIME_AUTOMATION_IDS.values())
    assert len(product_ids) == 3
    assert product_ids <= set(accessibility_audit._REQUIRED_PATTERNS)
    assert product_ids <= set(accessibility_audit._EXPECTED_ROLES)
