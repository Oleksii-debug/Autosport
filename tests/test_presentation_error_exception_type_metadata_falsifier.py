from __future__ import annotations

from autosport.presentation_error import safe_exception_text


_SECRET = "WIN-SECRET-SENTINEL-TYPE-NAME-A71C"


class _DynamicallyNamedError(RuntimeError):
    pass


def test_exception_type_name_is_not_untrusted_presentation_authority() -> None:
    original_name = _DynamicallyNamedError.__name__
    try:
        _DynamicallyNamedError.__name__ = f"RuntimeError_{_SECRET}"
        rendered = safe_exception_text(_DynamicallyNamedError("benign detail"))
    finally:
        _DynamicallyNamedError.__name__ = original_name

    assert _SECRET not in rendered
    assert "RuntimeError_" + _SECRET not in rendered
