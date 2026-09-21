from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

import autosport.forensic_journal as forensic_journal
from autosport.forensic_journal import (
    ForensicEventKind,
    ForensicJournalIntegrityError,
    ForensicSessionJournal,
    HeartbeatState,
)


class FakeClock:
    def __init__(self) -> None:
        self._value = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        value = self._value
        self._value += timedelta(seconds=1)
        return value


def test_forensic_journal_records_typed_lifecycle_and_survives_restart(tmp_path):
    path = tmp_path / "forensic.jsonl"
    clock = FakeClock()
    journal = ForensicSessionJournal(path, clock=clock)

    startup = journal.record_startup("product", details={"pid": 123})
    heartbeat = journal.record_heartbeat(
        "research-supervisor",
        HeartbeatState.WAITING,
        message="waiting for lawful evidence",
    )
    material = journal.record_material_event(
        "collector",
        "source-gap",
        details={"provider": "example", "count": 2},
    )
    shutdown = journal.record_shutdown("product", message="operator stop")

    assert [startup.sequence, heartbeat.sequence, material.sequence, shutdown.sequence] == [
        1,
        2,
        3,
        4,
    ]
    assert heartbeat.kind is ForensicEventKind.HEARTBEAT
    assert heartbeat.heartbeat_state is HeartbeatState.WAITING
    assert material.event_name == "source-gap"

    reopened = ForensicSessionJournal(path, clock=clock)
    integrity = reopened.verify()
    assert integrity.record_count == 4
    assert integrity.last_record_sha256 == shutdown.record_sha256
    assert [record.kind for record in reopened.read_records()] == [
        ForensicEventKind.STARTUP,
        ForensicEventKind.HEARTBEAT,
        ForensicEventKind.MATERIAL_EVENT,
        ForensicEventKind.SHUTDOWN,
    ]


def test_forensic_journal_redacts_secrets_before_persistence_and_export(tmp_path):
    path = tmp_path / "forensic.jsonl"
    journal = ForensicSessionJournal(path, clock=FakeClock())

    record = journal.record_material_event(
        "provider-health",
        "adapter-health",
        message=(
            "Authorization: Bearer abc.def token=raw-token "
            "client_secret=raw-client-secret"
        ),
        details={
            "api_token": "raw-token",
            "password": "raw-password",
            "nested": [{"client_secret": "raw-secret"}],
            "safe": "visible",
            "url": "https://name:pass@example.invalid/path",
        },
    )

    persisted = path.read_text(encoding="utf-8")
    assert "raw-token" not in persisted
    assert "raw-client-secret" not in persisted
    assert "raw-password" not in persisted
    assert "raw-secret" not in persisted
    assert "name:pass@" not in persisted
    assert record.details["api_token"] == "[REDACTED]"
    assert record.details["safe"] == "visible"
    assert "[REDACTED]" in (record.message or "")

    export = journal.export(tmp_path / "owner-export.json")
    exported = export.read_text(encoding="utf-8")
    assert "raw-token" not in exported
    assert "raw-client-secret" not in exported
    assert "raw-password" not in exported
    assert "raw-secret" not in exported
    payload = json.loads(exported)
    assert payload["record_count"] == 1
    assert payload["records"][0]["schema_version"] == 1
    assert payload["records"][0]["details"]["api_token"] == "[REDACTED]"


@pytest.mark.parametrize(
    ("message", "secret_fragments"),
    [
        ('password="alpha beta gamma"', ("alpha", "beta", "gamma")),
        ("client_secret='first second'", ("first", "second")),
        ('before token="one two three" after', ("one", "two", "three")),
        (
            'authorization="unterminated secret tail',
            ("unterminated", "secret", "tail"),
        ),
    ],
)
def test_forensic_journal_redacts_quoted_secret_assignments_without_partial_leak(
    tmp_path, message, secret_fragments
):
    path = tmp_path / "forensic.jsonl"
    journal = ForensicSessionJournal(path, clock=FakeClock())

    record = journal.record_material_event(
        "provider-health",
        "quoted-secret-redaction",
        message=message,
        details={"note": message},
    )

    persisted = path.read_text(encoding="utf-8")
    exported = journal.export(tmp_path / "owner-export.json").read_text(
        encoding="utf-8"
    )
    in_memory = (record.message or "") + " " + str(record.details["note"])

    for fragment in secret_fragments:
        assert fragment not in persisted
        assert fragment not in exported
        assert fragment not in in_memory
    assert "[REDACTED]" in persisted
    assert "[REDACTED]" in exported
    assert "[REDACTED]" in in_memory


def test_forensic_journal_rejects_record_tamper(tmp_path):
    path = tmp_path / "forensic.jsonl"
    journal = ForensicSessionJournal(path, clock=FakeClock())
    journal.record_startup("product", message="clean")

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["message"] = "tampered"
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ForensicJournalIntegrityError, match="digest mismatch"):
        journal.verify()


def test_forensic_journal_rejects_partial_trailing_record(tmp_path):
    path = tmp_path / "forensic.jsonl"
    journal = ForensicSessionJournal(path, clock=FakeClock())
    journal.record_startup("product")

    with path.open("ab") as handle:
        handle.write(b'{"schema_version":1')

    with pytest.raises(ForensicJournalIntegrityError, match="truncated trailing record"):
        journal.verify()


def test_forensic_journal_checkpoint_detects_complete_tail_truncation(tmp_path):
    path = tmp_path / "forensic.jsonl"
    journal = ForensicSessionJournal(path, clock=FakeClock())
    journal.record_startup("product")
    journal.record_material_event("collector", "quote-gap")

    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text(lines[0] + "\n", encoding="utf-8")

    with pytest.raises(ForensicJournalIntegrityError, match="tail was truncated"):
        journal.verify()


def test_forensic_journal_recovers_durable_tail_after_checkpoint_write_failure(
    tmp_path, monkeypatch
):
    path = tmp_path / "forensic.jsonl"
    journal = ForensicSessionJournal(path, clock=FakeClock())
    first = journal.record_startup("product")

    real_atomic_write_json = forensic_journal.atomic_write_json
    failures = 0

    def fail_checkpoint_once(destination, payload):
        nonlocal failures
        if destination == journal.checkpoint_path and failures == 0:
            failures += 1
            raise OSError("simulated checkpoint publication failure")
        return real_atomic_write_json(destination, payload)

    monkeypatch.setattr(
        forensic_journal,
        "atomic_write_json",
        fail_checkpoint_once,
    )
    with pytest.raises(OSError, match="simulated checkpoint"):
        journal.record_material_event("collector", "durable-before-checkpoint")

    monkeypatch.setattr(
        forensic_journal,
        "atomic_write_json",
        real_atomic_write_json,
    )
    reopened = ForensicSessionJournal(path, clock=FakeClock())
    integrity = reopened.verify()
    assert integrity.record_count == 2
    assert integrity.last_record_sha256 != first.record_sha256
    checkpoint = json.loads(
        reopened.checkpoint_path.read_text(encoding="utf-8")
    )
    assert checkpoint["record_count"] == 2
    assert checkpoint["last_record_sha256"] == integrity.last_record_sha256


@pytest.mark.parametrize("state", ["RUNNING", "WAITING", "IDLE"])
def test_forensic_journal_requires_typed_heartbeat_state(tmp_path, state):
    journal = ForensicSessionJournal(tmp_path / "forensic.jsonl", clock=FakeClock())
    with pytest.raises(TypeError, match="HeartbeatState"):
        journal.record_heartbeat("agent", state)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_forensic_journal_rejects_nonfinite_details_without_appending(tmp_path, value):
    path = tmp_path / "forensic.jsonl"
    journal = ForensicSessionJournal(path, clock=FakeClock())

    with pytest.raises(ValueError, match="non-finite"):
        journal.record_material_event(
            "research",
            "metric",
            details={"value": value},
        )

    assert journal.verify().record_count == 0
    assert path.read_bytes() == b""


def test_forensic_journal_rejects_oversized_json_integer_before_schema(tmp_path):
    path = tmp_path / "forensic.jsonl"
    journal = ForensicSessionJournal(path, clock=FakeClock())
    path.write_text('{"value":' + ("9" * 641) + "}\n", encoding="utf-8")

    with pytest.raises(ForensicJournalIntegrityError, match="strict JSON"):
        journal.verify()


def test_forensic_journal_rejects_oversized_integer_before_durable_append(tmp_path):
    path = tmp_path / "forensic.jsonl"
    journal = ForensicSessionJournal(path, clock=FakeClock())

    with pytest.raises(ValueError, match="canonical strict JSON"):
        journal.record_material_event(
            "research",
            "metric",
            details={"value": int("9" * 641)},
        )

    assert path.read_bytes() == b""
    assert journal.verify().record_count == 0


def test_forensic_journal_rejects_nonempty_journal_without_checkpoint(tmp_path):
    path = tmp_path / "forensic.jsonl"
    path.write_text('{"untrusted":"legacy"}\n', encoding="utf-8")

    with pytest.raises(
        ForensicJournalIntegrityError,
        match="missing its checkpoint",
    ):
        ForensicSessionJournal(path, clock=FakeClock())
