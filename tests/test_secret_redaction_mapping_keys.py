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

def test_redacted_mapping_keys_do_not_collapse_or_overwrite_ordinary_keys() -> None:
    first = "AS-MAPPING-COLLISION-FIRST-61d2"
    second = "AS-MAPPING-COLLISION-SECOND-93a4"
    payload = {
        first: "first-record",
        second: "second-record",
        REDACTED: "ordinary-record",
    }

    redacted = redact_operator_value(
        payload,
        extra_secret_values=(first, second),
    )

    assert redacted[REDACTED] == "ordinary-record"
    assert redacted[f"{REDACTED}#2"] == "first-record"
    assert redacted[f"{REDACTED}#3"] == "second-record"
    assert len(redacted) == 3
    assert first not in repr(redacted)
    assert second not in repr(redacted)


def test_same_redacted_embedded_key_spelling_preserves_both_records() -> None:
    first = "AS-MAPPING-INNER-FIRST-d481"
    second = "AS-MAPPING-INNER-SECOND-a172"
    payload = {
        f"provider-{first}-error": "first-record",
        f"provider-{second}-error": "second-record",
    }

    redacted = redact_operator_value(
        payload,
        extra_secret_values=(first, second),
    )

    base = f"provider-{REDACTED}-error"
    assert redacted[base] == "first-record"
    assert redacted[f"{base}#2"] == "second-record"
    assert len(redacted) == 2
    assert first not in repr(redacted)
    assert second not in repr(redacted)

