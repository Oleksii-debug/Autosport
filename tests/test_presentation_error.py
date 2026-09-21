from __future__ import annotations

from autosport.presentation_error import safe_exception_text, safe_worker_error_text


_SECRET = "WIN-SECRET-SENTINEL-9d8a4b"


class _ExplodingStrError(RuntimeError):
    def __str__(self) -> str:
        raise AssertionError("presentation boundary must not stringify exceptions")


def test_safe_exception_text_omits_untrusted_exception_detail() -> None:
    rendered = safe_exception_text(
        RuntimeError(f"Authorization: Bearer {_SECRET}; token={_SECRET}")
    )

    assert _SECRET not in rendered
    assert "Authorization" not in rendered
    assert "token=" not in rendered
    assert "RuntimeError" in rendered


def test_safe_exception_text_does_not_call_hostile_str() -> None:
    rendered = safe_exception_text(_ExplodingStrError())

    assert _SECRET not in rendered
    assert "_ExplodingStrError" in rendered


def test_safe_worker_error_text_treats_worker_message_as_opaque() -> None:
    rendered = safe_worker_error_text(
        f"https://user:{_SECRET}@provider.example/?api_key={_SECRET}"
    )

    assert _SECRET not in rendered
    assert "provider.example" not in rendered
    assert "api_key" not in rendered
    assert "WorkerError" in rendered


def test_presented_error_text_is_deterministic_for_same_category() -> None:
    first = safe_worker_error_text(f"password={_SECRET}")
    second = safe_worker_error_text("completely different provider failure")

    assert first == second
