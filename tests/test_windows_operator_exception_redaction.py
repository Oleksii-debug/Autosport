"""Plan 6 §3А: source-level secret boundaries in real Windows recovery dialogs.

These tests reuse the canonical operator exception presenter. They do not
simulate NVDA or imply an installed Windows package has been tested.
"""

from __future__ import annotations

import pytest

from autosport.windows_gui import _safe_exception_detail


@pytest.mark.parametrize(
    "detail, secret",
    [
        ("restart rejected api_key=OperatorSecret987", "OperatorSecret987"),
        ("configuration failed Authorization: Bearer superPrivateJwt123", "superPrivateJwt123"),
        ("provider URL https://account:privatePassword99@broker.example/path", "privatePassword99"),
        ("source failed ?access_token=hiddenToken457&mode=paper", "hiddenToken457"),
    ],
)
def test_operator_recovery_exception_never_displays_embedded_credentials(
    detail: str, secret: str
) -> None:
    presentation = _safe_exception_detail(RuntimeError(detail))
    assert secret not in presentation
    assert "[REDACTED]" in presentation
    assert presentation.startswith("RuntimeError: ")


def test_operator_recovery_exception_redacts_known_environment_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOSPORT_API_KEY", "privateEnv987654")
    presentation = _safe_exception_detail(
        ValueError("Cannot read provider account with privateEnv987654")
    )
    assert "privateEnv987654" not in presentation
    assert "[REDACTED]" in presentation


def test_operator_recovery_exception_keeps_benign_diagnostic() -> None:
    assert _safe_exception_detail(RuntimeError("teardown exploded")) == (
        "RuntimeError: teardown exploded"
    )


def test_operator_recovery_exception_never_uses_untrusted_subclass_name() -> None:
    # Exception classes created by provider plugins can encode secret material
    # in __name__. The UI should reveal only an actual built-in MRO category.
    hostile_type = type("AuthorizationBearerHiddenSecret987", (RuntimeError,), {})
    presentation = _safe_exception_detail(hostile_type("restart unavailable"))
    assert presentation == "RuntimeError: restart unavailable"
    assert "HiddenSecret987" not in presentation


def test_operator_recovery_exception_str_failure_keeps_type_private() -> None:
    def broken_str(self: BaseException) -> str:
        raise RuntimeError("provider token must stay private")

    hostile_type = type(
        "api_key_BadCredential987",
        (RuntimeError,),
        {"__str__": broken_str},
    )
    presentation = _safe_exception_detail(hostile_type())
    assert "BadCredential987" not in presentation
    assert "provider token must stay private" not in presentation
    assert "RuntimeError" in presentation
