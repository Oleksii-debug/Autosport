"""Dependent falsifier for PR #972 forensic-journal credential redaction."""

from datetime import datetime, timezone

from autosport.forensic_session_journal import (
    REDACTED,
    ForensicSessionJournal,
    verify_journal,
)


def test_camelcase_credentials_are_redacted_before_durable_jsonl_write(tmp_path) -> None:
    path = tmp_path / "forensic-session.jsonl"
    fixed = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
    secrets = {
        "accessToken": "access-live-secret",
        "refreshToken": "refresh-live-secret",
        "clientSecret": "client-live-secret",
        "sessionToken": "session-live-secret",
    }

    journal = ForensicSessionJournal(path, clock=lambda: fixed)
    record = journal.append_material(
        "provider.diagnostic",
        {
            **secrets,
            "nested": {
                "accessToken": "nested-access-secret",
                "ordinary": "safe-value",
            },
        },
    )
    journal.close()

    assert record.payload["accessToken"] == REDACTED
    assert record.payload["refreshToken"] == REDACTED
    assert record.payload["clientSecret"] == REDACTED
    assert record.payload["sessionToken"] == REDACTED
    assert record.payload["nested"]["accessToken"] == REDACTED
    assert record.payload["nested"]["ordinary"] == "safe-value"

    raw = path.read_text(encoding="utf-8")
    for secret in (*secrets.values(), "nested-access-secret"):
        assert secret not in raw

    verified = verify_journal(path)
    material = next(item for item in verified if item.event_type == "provider.diagnostic")
    assert material.payload == record.payload
