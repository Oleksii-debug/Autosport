from __future__ import annotations

from autosport.product_gui_worker import _safe_error_type


def test_product_gui_error_type_uses_builtin_exception_authority() -> None:
    assert _safe_error_type(ValueError("ordinary failure")) == "ValueError"
    assert _safe_error_type(RuntimeError("ordinary failure")) == "RuntimeError"


def test_product_gui_error_type_rejects_valid_ascii_secret_bearing_class_name() -> None:
    hostile_type = type("ApiKeySECRET123456", (RuntimeError,), {})
    rendered = _safe_error_type(hostile_type("provider detail must not surface"))

    assert rendered == "RuntimeError"
    assert "SECRET" not in rendered
    assert "123456" not in rendered


def test_product_gui_error_type_rejects_builtin_name_spoofing() -> None:
    spoofed_value_error = type("ValueError", (RuntimeError,), {})
    assert _safe_error_type(spoofed_value_error("spoofed")) == "RuntimeError"
