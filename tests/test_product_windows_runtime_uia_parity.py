from __future__ import annotations

import inspect
import re
from pathlib import Path

from autosport import accessibility_audit
from autosport.localization_product_runtime import product_text
from autosport.product_windows_gui import (
    PRODUCT_RUNTIME_AUTOMATION_IDS,
    ProductWindowsAutosportApp,
)


_ROOT = Path(__file__).resolve().parents[1]
_PRODUCT_WINDOWS_GUI = _ROOT / "src" / "autosport" / "product_windows_gui.py"
_EXTERNAL_UIA_AUDIT = _ROOT / "scripts" / "external_uia_audit.ps1"
_UKRAINIAN_CONTEXT = re.compile(r"[А-Яа-яІіЇїЄєҐґ]")


def _assert_ukrainian_product_context(value: str) -> None:
    assert value.strip()
    assert _UKRAINIAN_CONTEXT.search(value), value


def test_packaged_runtime_visible_and_uia_copy_share_ukrainian_product_authority() -> None:
    source = _PRODUCT_WINDOWS_GUI.read_text(encoding="utf-8")

    visible_keys = (
        "ui.product_runtime.button.start",
        "ui.product_runtime.button.stop",
        "ui.product_runtime.status.idle",
        "ui.product_runtime.status.starting",
        "ui.product_runtime.status.stopping",
        "ui.product_runtime.status.stop_not_running",
    )
    uia_keys = (
        "ui.product_runtime.accessibility.start.name",
        "ui.product_runtime.accessibility.start.description",
        "ui.product_runtime.accessibility.stop.name",
        "ui.product_runtime.accessibility.stop.description",
        "ui.product_runtime.accessibility.status.name",
        "ui.product_runtime.accessibility.status.description",
    )
    for key in (*visible_keys, *uia_keys):
        assert f'product_text("{key}")' in source
        _assert_ukrainian_product_context(product_text(key))

    assert "textvariable=self.product_status" in source
    assert 'state="readonly"' in source
    assert PRODUCT_RUNTIME_AUTOMATION_IDS == {
        "start": 206,
        "stop": 207,
        "status": 208,
    }


def test_packaged_runtime_error_projection_is_ukrainian_and_secret_safe_by_contract() -> None:
    rendered = product_text(
        "ui.product_runtime.status.error",
        error_type="RuntimeError",
    )
    _assert_ukrainian_product_context(rendered)
    assert "RuntimeError" in rendered
    assert "provider-token-should-never-reach-ui" not in rendered

    source = inspect.getsource(ProductWindowsAutosportApp._apply_product_message)
    assert "message.error_type" in source
    assert "status.error" in source
    assert "message.error_detail" not in source
    assert "message.exception" not in source


def test_in_process_packaged_accessibility_audit_covers_product_runtime_controls() -> None:
    expected_patterns = {
        PRODUCT_RUNTIME_AUTOMATION_IDS["start"]: {"INVOKE"},
        PRODUCT_RUNTIME_AUTOMATION_IDS["stop"]: {"INVOKE"},
        PRODUCT_RUNTIME_AUTOMATION_IDS["status"]: {"VALUE"},
    }
    expected_roles = {
        PRODUCT_RUNTIME_AUTOMATION_IDS["start"]: "PUSH_BUTTON",
        PRODUCT_RUNTIME_AUTOMATION_IDS["stop"]: "PUSH_BUTTON",
        PRODUCT_RUNTIME_AUTOMATION_IDS["status"]: "TEXT",
    }

    for automation_id, patterns in expected_patterns.items():
        assert accessibility_audit._REQUIRED_PATTERNS.get(automation_id) == patterns
        assert accessibility_audit._EXPECTED_ROLES.get(automation_id) == expected_roles[
            automation_id
        ]

    source = inspect.getsource(accessibility_audit.run_accessibility_audit)
    assert "ProductWindowsAutosportApp()" in source
    assert "app = WindowsAutosportApp()" not in source


def test_external_packaged_uia_audit_requires_product_runtime_controls() -> None:
    audit = _EXTERNAL_UIA_AUDIT.read_text(encoding="utf-8")

    expected = (
        (
            "206",
            "Запустити канонічну тривалу PAPER-роботу",
            "Invoke",
            "ControlType.Button",
            False,
        ),
        (
            "207",
            "Зупинити канонічну тривалу PAPER-роботу",
            "Invoke",
            "ControlType.Button",
            False,
        ),
        (
            "208",
            "Стан тривалої PAPER-роботи",
            "Value",
            "ControlType.Edit",
            True,
        ),
    )
    for automation_id, name, pattern, control_type, readonly in expected:
        matching = [
            line
            for line in audit.splitlines()
            if f"automation_id = '{automation_id}'" in line
        ]
        assert len(matching) == 1
        line = matching[0]
        assert f"name = '{name}'" in line
        assert f"required_pattern = '{pattern}'" in line
        assert "require_external_focus = $true" in line
        assert f"expected_control_type = '{control_type}'" in line
        if readonly:
            assert "require_value_read_only = $true" in line

    # Machine accessibility evidence must remain distinct from physical NVDA proof.
    assert "nvda_verified = $false" in audit
    assert "human_tested = $false" in audit
