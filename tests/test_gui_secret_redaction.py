from __future__ import annotations

import pytest

from autosport.gui import _safe_exception_text
from autosport.secret_redaction import REDACTED
from autosport.windows_gui import _safe_exception_detail


_RENDERERS = (_safe_exception_text, _safe_exception_detail)


@pytest.mark.parametrize("renderer", _RENDERERS)
def test_operator_gui_exception_text_redacts_credentials(
    renderer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment_secret = "env-secret-token-93715"
    bearer_secret = "bearer-secret-token-48126"
    query_secret = "query-secret-token-72504"
    monkeypatch.setenv("AUTOSPORT_PARLAYAPI_KEY", environment_secret)

    rendered = renderer(
        RuntimeError(
            "Authorization: Bearer "
            + bearer_secret
            + "; api_key="
            + query_secret
            + "; password=abc; echo="
            + environment_secret
        )
    )

    assert rendered.startswith("RuntimeError:")
    assert REDACTED in rendered
    for secret in (
        environment_secret,
        bearer_secret,
        query_secret,
        "password=abc",
    ):
        assert secret not in rendered


@pytest.mark.parametrize("renderer", _RENDERERS)
def test_operator_gui_exception_type_name_cannot_carry_secret(renderer) -> None:
    type_secret = "TYPE-SECRET-58319"
    secret_type = type(
        "api_key=" + type_secret,
        (RuntimeError,),
        {},
    )

    rendered = renderer(secret_type("safe detail"))

    assert type_secret not in rendered
    assert REDACTED in rendered
    assert "safe detail" in rendered

def test_gui_exception_renderers_preserve_empty_message_semantics() -> None:
    assert _safe_exception_text(RuntimeError()) == "RuntimeError"
    assert _safe_exception_detail(RuntimeError()) == "RuntimeError: "

