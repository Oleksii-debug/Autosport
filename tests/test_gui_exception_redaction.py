"""Plan 6 §3А: canonical GUI exception presentation must not leak secrets."""

from autosport.gui import _safe_exception_text


def test_gui_exception_keeps_actionable_nonsecret_context_without_credentials() -> None:
    secret = "SYNTHETIC_PLAN6_GUI_SECRET_20261021"
    rendered = _safe_exception_text(
        RuntimeError("Replay configuration failed: api_key=" + secret)
    )
    assert "RuntimeError" in rendered
    assert "Replay configuration failed" in rendered
    assert "api_key=[REDACTED]" in rendered
    assert secret not in rendered


def test_gui_exception_rejects_untrusted_exception_class_name() -> None:
    marker = "SYNTHETIC_PRIVATE_EXCEPTION_CLASS_20261021"
    error_class = type(marker, (RuntimeError,), {})
    rendered = _safe_exception_text(error_class("replay cannot start"))
    assert rendered == "RuntimeError: replay cannot start"
    assert marker not in rendered


def test_gui_exception_handles_malicious_stringification_without_ui_crash() -> None:
    class BrokenError(ValueError):
        def __str__(self) -> str:
            raise RuntimeError("do not expose exception internals")

    rendered = _safe_exception_text(BrokenError())
    assert rendered == "ValueError: exception details unavailable"
    assert "do not expose" not in rendered
