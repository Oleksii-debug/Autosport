from __future__ import annotations

from autosport.secret_redaction import (
    REDACTED,
    is_sensitive_key,
    redact_operator_text,
    redact_operator_value,
)


def test_wave_f_redacts_app_key_aliases_and_bounded_wrappers() -> None:
    secrets = {
        "appKey": "app-secret-wave-f",
        "APPKEY": "upper-secret-wave-f",
        "betfairAppKey": "provider-secret-wave-f",
        "betfairAppKeyValue": "value-secret-wave-f",
        "BETFAIR_APP_KEY_HEADER": "header-secret-wave-f",
    }

    structured = redact_operator_value(secrets)

    for key, secret in secrets.items():
        assert is_sensitive_key(key)
        assert structured[key] == REDACTED
        rendered = redact_operator_text(f"{key}={secret}")
        assert rendered == f"{key}={REDACTED}"
        assert secret not in rendered


def test_wave_f_app_key_wrapper_rule_does_not_hide_ordinary_metadata() -> None:
    assert not is_sensitive_key("marketValue")
    assert not is_sensitive_key("responseHeader")
    assert redact_operator_value(
        {"marketValue": "ordinary", "responseHeader": "diagnostic"}
    ) == {"marketValue": "ordinary", "responseHeader": "diagnostic"}
