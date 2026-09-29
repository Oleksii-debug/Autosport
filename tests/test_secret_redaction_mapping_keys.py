from __future__ import annotations

from autosport.secret_redaction import REDACTED, redact_operator_value


def test_explicit_secret_embedded_in_mapping_key_is_redacted() -> None:
    secret = "AS-MAPPING-KEY-SECRET-7f31"
    payload = {
        f"provider-{secret}-error": {
            "status": "rejected",
        }
    }

    redacted = redact_operator_value(
        payload,
        extra_secret_values=(secret,),
    )

    assert redacted == {
        f"provider-{REDACTED}-error": {
            "status": "rejected",
        }
    }
    assert secret not in repr(redacted)


def test_environment_secret_used_as_mapping_key_is_redacted(
    monkeypatch,
) -> None:
    secret = "AS-MAPPING-ENV-SECRET-291c"
    monkeypatch.setenv("AUTOSPORT_TEST_API_KEY", secret)

    redacted = redact_operator_value({secret: "provider-error"})

    assert redacted == {REDACTED: "provider-error"}
    assert secret not in repr(redacted)


def test_sensitive_label_contract_is_preserved_when_key_is_not_secret() -> None:
    redacted = redact_operator_value(
        {
            "api_key": "provider-secret",
            "ordinary_key": "ordinary-value",
        }
    )

    assert redacted == {
        "api_key": REDACTED,
        "ordinary_key": "ordinary-value",
    }
