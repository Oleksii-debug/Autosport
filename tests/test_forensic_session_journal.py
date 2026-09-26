from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import autosport.forensic_session_journal as forensic_session_journal
from autosport.forensic_session_journal import (
    REDACTED,
    ForensicSessionJournal,
    JournalClosedError,
    JournalIntegrityError,
    JournalLockedError,
    JournalUncertainError,
    read_verified_records,
    redact_payload,
    verify_journal,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 21, 11, 30, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        current = self.value
        self.value += timedelta(microseconds=1)
        return current


def new_journal(tmp_path: Path, *, clock: FakeClock | None = None) -> ForensicSessionJournal:
    return ForensicSessionJournal(
        tmp_path / "forensic-session.jsonl",
        clock=clock or FakeClock(),
        session_id=str(uuid.UUID(int=1)),
    )


def test_startup_material_heartbeat_shutdown_forms_verified_chain(tmp_path: Path) -> None:
    journal = new_journal(tmp_path)
    journal.append_material("provider.connected", {"provider": "paper"})
    journal.heartbeat({"queue_depth": 3})
    journal.close({"reason": "operator_exit"})

    records = verify_journal(journal.path)
    assert [r.event_type for r in records] == [
        "lifecycle.startup",
        "provider.connected",
        "lifecycle.heartbeat",
        "lifecycle.shutdown",
    ]
    assert [r.seq for r in records] == [1, 2, 3, 4]
    assert records[0].prev_sha256 == "0" * 64
    assert all(records[i].prev_sha256 == records[i - 1].sha256 for i in range(1, len(records)))


def test_payload_redacts_nested_credentials_without_mutating_input(tmp_path: Path) -> None:
    payload = {
        "token": "secret-1",
        "nested": {
            "api_key": "secret-2",
            "normal": "ok",
            "children": [{"password": "secret-3", "x": 1}],
        },
    }
    journal = new_journal(tmp_path)
    record = journal.append_material("provider.request", payload)
    journal.close()

    assert record.payload["token"] == REDACTED
    assert record.payload["nested"]["api_key"] == REDACTED
    assert record.payload["nested"]["children"][0]["password"] == REDACTED
    assert record.payload["nested"]["normal"] == "ok"
    assert payload["token"] == "secret-1"


def test_redaction_matches_authorization_cookie_and_session_key() -> None:
    redacted = redact_payload(
        {
            "Authorization": "Bearer x",
            "http_cookie": "a=b",
            "session-key": "xyz",
            "market_token_count": 7,
        }
    )
    assert redacted["Authorization"] == REDACTED
    assert redacted["http_cookie"] == REDACTED
    assert redacted["session-key"] == REDACTED
    assert redacted["market_token_count"] == 7


def test_redaction_catches_camelcase_and_mixed_separator_credentials() -> None:
    redacted = redact_payload(
        {
            "accessToken": "a",
            "refreshToken": "b",
            "clientSecret": "c",
            "authToken": "d",
            "bearerToken": "e",
            "sessionKey": "f",
            "APIKey": "g",
            "access.token": "h",
            "market_token_count": 7,
        }
    )
    for key in (
        "accessToken",
        "refreshToken",
        "clientSecret",
        "authToken",
        "bearerToken",
        "sessionKey",
        "APIKey",
        "access.token",
    ):
        assert redacted[key] == REDACTED
    assert redacted["market_token_count"] == 7


def test_payload_rejects_non_json_safe_values(tmp_path: Path) -> None:
    journal = new_journal(tmp_path)
    with pytest.raises(TypeError, match="not JSON-safe"):
        journal.append_material("bad.payload", {"opaque": object()})


def test_payload_rejects_nan_instead_of_serializing_nonstandard_json(tmp_path: Path) -> None:
    journal = new_journal(tmp_path)
    with pytest.raises(ValueError):
        journal.append_material("bad.number", {"value": float("nan")})


def test_payload_rejects_non_string_mapping_keys(tmp_path: Path) -> None:
    journal = new_journal(tmp_path)
    with pytest.raises(TypeError, match="mapping keys must be strings"):
        journal.append_material("bad.payload", {1: "value"})


def test_fsync_failure_fail_stops_writer_until_reopen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    journal = new_journal(tmp_path)
    import autosport.forensic_session_journal as module

    real_fsync = module.os.fsync
    calls = {"count": 0}

    def fail_once(fd: int) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("simulated fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(module.os, "fsync", fail_once)
    with pytest.raises(JournalUncertainError, match="reopen and reverify"):
        journal.append_material("worker.result", {"ok": True})
    with pytest.raises(JournalUncertainError, match="reopen and reverify"):
        journal.append_material("worker.next", {})

    monkeypatch.setattr(module.os, "fsync", real_fsync)
    reopened = ForensicSessionJournal(
        journal.path,
        clock=FakeClock(),
        session_id=str(uuid.UUID(int=9)),
    )
    reopened.close()
    records = verify_journal(journal.path)
    assert records[-3].event_type == "lifecycle.unclean_restart"
    assert records[-2].event_type == "lifecycle.startup"
    assert records[-1].event_type == "lifecycle.shutdown"


def test_lifecycle_namespace_is_journal_owned(tmp_path: Path) -> None:
    journal = new_journal(tmp_path)
    with pytest.raises(ValueError, match="journal-owned"):
        journal.append_material("lifecycle.shutdown", {})


def test_invalid_material_event_type_rejected(tmp_path: Path) -> None:
    journal = new_journal(tmp_path)
    with pytest.raises(ValueError, match="invalid journal event_type"):
        journal.append_material("Execution Accepted", {})


def test_close_is_idempotent_and_append_after_close_fails(tmp_path: Path) -> None:
    journal = new_journal(tmp_path)
    first = journal.close()
    second = journal.close()
    assert first is not None
    assert second is None
    with pytest.raises(JournalClosedError):
        journal.heartbeat()
    assert [r.event_type for r in verify_journal(journal.path)] == [
        "lifecycle.startup",
        "lifecycle.shutdown",
    ]


def test_concurrent_close_writes_exactly_one_shutdown(tmp_path: Path) -> None:
    journal = new_journal(tmp_path)
    barrier = threading.Barrier(8)
    results: list[object] = []

    def closer() -> None:
        barrier.wait()
        results.append(journal.close())

    threads = [threading.Thread(target=closer) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    records = verify_journal(journal.path)
    assert sum(r.event_type == "lifecycle.shutdown" for r in records) == 1
    assert sum(result is not None for result in results) == 1


def test_concurrent_material_appends_keep_contiguous_hash_chain(tmp_path: Path) -> None:
    journal = new_journal(tmp_path)
    threads = [
        threading.Thread(target=journal.append_material, args=("worker.result", {"i": i}))
        for i in range(20)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    journal.close()

    records = verify_journal(journal.path)
    assert len(records) == 22
    assert [r.seq for r in records] == list(range(1, 23))


def test_unclean_restart_is_recorded_before_new_startup(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    path = tmp_path / "forensic-session.jsonl"
    code = r'''
import os
import sys
import uuid
from datetime import datetime, timezone
from autosport.forensic_session_journal import ForensicSessionJournal
journal = ForensicSessionJournal(
    sys.argv[1],
    clock=lambda: datetime(2026, 9, 21, 11, 35, tzinfo=timezone.utc),
    session_id=str(uuid.UUID(int=1)),
)
journal.append_material("worker.started", {"work": "x"})
os._exit(17)
'''
    env = dict(os.environ)
    src = str(Path(__file__).parents[1] / "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run([sys.executable, "-c", code, str(path)], env=env, check=False)
    assert completed.returncode == 17

    second = ForensicSessionJournal(path, clock=FakeClock(), session_id=str(uuid.UUID(int=2)))
    second.close()
    records = verify_journal(path)
    assert [r.event_type for r in records] == [
        "lifecycle.startup",
        "worker.started",
        "lifecycle.unclean_restart",
        "lifecycle.startup",
        "lifecycle.shutdown",
    ]
    unclean = next(r for r in records if r.event_type == "lifecycle.unclean_restart")
    assert unclean.payload["prior_event_type"] == "worker.started"
    assert unclean.payload["prior_session_id"] == str(uuid.UUID(int=1))


def test_clean_restart_does_not_emit_unclean_restart(tmp_path: Path) -> None:
    path = tmp_path / "forensic-session.jsonl"
    first = ForensicSessionJournal(path, clock=FakeClock(), session_id=str(uuid.UUID(int=1)))
    first.close()
    second = ForensicSessionJournal(path, clock=FakeClock(), session_id=str(uuid.UUID(int=2)))
    second.close()
    assert [r.event_type for r in verify_journal(path)] == [
        "lifecycle.startup",
        "lifecycle.shutdown",
        "lifecycle.startup",
        "lifecycle.shutdown",
    ]


def test_torn_tail_fails_closed_before_new_startup(tmp_path: Path) -> None:
    journal = new_journal(tmp_path)
    journal.close()
    raw = journal.path.read_bytes()
    journal.path.write_bytes(raw[:-1])
    before = journal.path.read_bytes()
    with pytest.raises(JournalIntegrityError, match="torn"):
        new_journal(tmp_path)
    assert journal.path.read_bytes() == before



def test_durable_checkpoint_detects_complete_valid_tail_truncation(
    tmp_path: Path,
) -> None:
    journal = new_journal(tmp_path)
    journal.append_material("provider.connected", {"provider": "paper"})
    journal.close({"reason": "clean"})

    original = journal.path.read_text(encoding="utf-8").splitlines()
    assert len(original) == 3
    journal.path.write_text("\n".join(original[:-1]) + "\n", encoding="utf-8")

    with pytest.raises(
        JournalIntegrityError,
        match="tail was truncated behind its durable checkpoint",
    ):
        verify_journal(journal.path)


def test_nonempty_journal_without_durable_checkpoint_fails_closed(
    tmp_path: Path,
) -> None:
    journal = new_journal(tmp_path)
    journal.append_material("provider.connected", {"provider": "paper"})
    journal.close()
    journal.checkpoint_path.unlink()

    with pytest.raises(
        JournalIntegrityError,
        match="missing its durable checkpoint",
    ):
        verify_journal(journal.path)


def test_reopen_recovers_fsynced_tail_after_checkpoint_publication_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = new_journal(tmp_path)
    real_write_checkpoint = forensic_session_journal._write_checkpoint
    failed = False

    def fail_second_checkpoint(path: Path, record_count: int, digest: str) -> None:
        nonlocal failed
        if record_count == 2 and not failed:
            failed = True
            raise OSError("simulated checkpoint publication failure")
        real_write_checkpoint(path, record_count, digest)

    monkeypatch.setattr(
        forensic_session_journal,
        "_write_checkpoint",
        fail_second_checkpoint,
    )
    with pytest.raises(JournalUncertainError, match="durability is uncertain"):
        journal.append_material("provider.durable_before_checkpoint", {"attempt": 1})
    assert failed

    with pytest.raises(
        JournalIntegrityError,
        match="durable records beyond its checkpoint",
    ):
        verify_journal(journal.path)

    monkeypatch.setattr(
        forensic_session_journal,
        "_write_checkpoint",
        real_write_checkpoint,
    )
    reopened = ForensicSessionJournal(
        journal.path,
        clock=FakeClock(),
        session_id=str(uuid.UUID(int=2)),
    )
    reopened.close({"reason": "recovered"})

    records = verify_journal(journal.path)
    assert any(
        record.event_type == "provider.durable_before_checkpoint"
        for record in records
    )
    checkpoint = json.loads(reopened.checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["record_count"] == len(records)
    assert checkpoint["last_record_sha256"] == records[-1].sha256

def test_malformed_utf8_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "forensic-session.jsonl"
    path.write_bytes(b"\xff\n")
    before = path.read_bytes()
    with pytest.raises(JournalIntegrityError, match="UTF-8"):
        ForensicSessionJournal(path, clock=FakeClock())
    assert path.read_bytes() == before


def test_malformed_json_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "forensic-session.jsonl"
    path.write_text("not-json\n", encoding="utf-8")
    with pytest.raises(JournalIntegrityError, match="valid JSON"):
        ForensicSessionJournal(path, clock=FakeClock())


def test_schema_drift_fails_closed(tmp_path: Path) -> None:
    journal = new_journal(tmp_path)
    journal.close()
    lines = journal.path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["unexpected"] = True
    lines[0] = json.dumps(first, sort_keys=True, separators=(",", ":"))
    journal.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(JournalIntegrityError, match="schema drift"):
        verify_journal(journal.path)


def test_payload_tamper_is_detected_by_record_hash(tmp_path: Path) -> None:
    journal = new_journal(tmp_path)
    journal.append_material("worker.result", {"accepted": False})
    journal.close()
    lines = journal.path.read_text(encoding="utf-8").splitlines()
    target = json.loads(lines[1])
    target["payload"]["accepted"] = True
    lines[1] = json.dumps(target, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    journal.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(JournalIntegrityError, match="record hash mismatch"):
        verify_journal(journal.path)


def test_record_deletion_is_detected_by_sequence_or_predecessor(tmp_path: Path) -> None:
    journal = new_journal(tmp_path)
    journal.append_material("one.event", {})
    journal.append_material("two.event", {})
    journal.close()
    lines = journal.path.read_text(encoding="utf-8").splitlines()
    del lines[1]
    journal.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(JournalIntegrityError, match="sequence|predecessor"):
        verify_journal(journal.path)


def test_record_reordering_is_detected(tmp_path: Path) -> None:
    journal = new_journal(tmp_path)
    journal.append_material("one.event", {})
    journal.append_material("two.event", {})
    journal.close()
    lines = journal.path.read_text(encoding="utf-8").splitlines()
    lines[1], lines[2] = lines[2], lines[1]
    journal.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(JournalIntegrityError, match="sequence|predecessor"):
        verify_journal(journal.path)


def test_noncanonical_or_naive_clock_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ForensicSessionJournal(
            tmp_path / "forensic-session.jsonl",
            clock=lambda: datetime(2026, 9, 21, 11, 30, 0),
        )


def test_context_manager_records_error_exit_without_suppressing_exception(tmp_path: Path) -> None:
    path = tmp_path / "forensic-session.jsonl"
    with pytest.raises(RuntimeError, match="boom"):
        with ForensicSessionJournal(path, clock=FakeClock(), session_id=str(uuid.UUID(int=3))):
            raise RuntimeError("boom")
    records = verify_journal(path)
    assert records[-1].event_type == "lifecycle.shutdown"
    assert records[-1].payload == {"exit": "error"}


def test_empty_and_missing_files_verify_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    assert read_verified_records(path) == ()
    path.write_bytes(b"")
    assert read_verified_records(path) == ()


def test_second_process_fails_closed_while_writer_lock_is_held(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    path = tmp_path / "forensic-session.jsonl"
    owner = ForensicSessionJournal(path, clock=FakeClock(), session_id=str(uuid.UUID(int=21)))
    code = r'''
import sys
import uuid
from datetime import datetime, timezone
from autosport.forensic_session_journal import ForensicSessionJournal, JournalLockedError
try:
    journal = ForensicSessionJournal(
        sys.argv[1],
        clock=lambda: datetime(2026, 9, 21, 11, 45, tzinfo=timezone.utc),
        session_id=str(uuid.UUID(int=22)),
    )
except JournalLockedError:
    raise SystemExit(23)
else:
    journal.close()
    raise SystemExit(0)
'''
    env = dict(os.environ)
    src = str(Path(__file__).parents[1] / "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    blocked = subprocess.run([sys.executable, "-c", code, str(path)], env=env, check=False)
    assert blocked.returncode == 23
    assert [record.event_type for record in verify_journal(path)] == ["lifecycle.startup"]

    owner.close()
    released = subprocess.run([sys.executable, "-c", code, str(path)], env=env, check=False)
    assert released.returncode == 0
    assert [record.event_type for record in verify_journal(path)] == [
        "lifecycle.startup",
        "lifecycle.shutdown",
        "lifecycle.startup",
        "lifecycle.shutdown",
    ]


def test_writer_lock_releases_after_constructor_validation_failure(tmp_path: Path) -> None:
    path = tmp_path / "forensic-session.jsonl"
    with pytest.raises(ValueError, match="canonical UUID"):
        ForensicSessionJournal(path, clock=FakeClock(), session_id="NOT-CANONICAL")

    reopened = ForensicSessionJournal(path, clock=FakeClock(), session_id=str(uuid.UUID(int=23)))
    reopened.close()
    assert [record.event_type for record in verify_journal(path)] == [
        "lifecycle.startup",
        "lifecycle.shutdown",
    ]


def test_real_subprocess_death_is_marked_unclean_on_restart(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    path = tmp_path / "forensic-session.jsonl"
    code = r'''
import os
import sys
import uuid
from datetime import datetime, timezone
from autosport.forensic_session_journal import ForensicSessionJournal

path = sys.argv[1]
journal = ForensicSessionJournal(
    path,
    clock=lambda: datetime(2026, 9, 21, 11, 40, tzinfo=timezone.utc),
    session_id=str(uuid.UUID(int=10)),
)
journal.append_material("worker.material_result", {"phase": "before_crash"})
os._exit(17)
'''
    env = dict(os.environ)
    src = str(Path(__file__).parents[1] / "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, "-c", code, str(path)],
        env=env,
        check=False,
    )
    assert completed.returncode == 17

    recovered = ForensicSessionJournal(
        path,
        clock=FakeClock(),
        session_id=str(uuid.UUID(int=11)),
    )
    recovered.close()
    records = verify_journal(path)
    assert [record.event_type for record in records] == [
        "lifecycle.startup",
        "worker.material_result",
        "lifecycle.unclean_restart",
        "lifecycle.startup",
        "lifecycle.shutdown",
    ]
    assert records[2].payload["prior_event_type"] == "worker.material_result"
    assert records[2].payload["prior_session_id"] == str(uuid.UUID(int=10))


def test_randomized_single_record_tamper_never_verifies(tmp_path: Path) -> None:
    import random

    rng = random.Random(20260921)
    for case in range(100):
        path = tmp_path / f"tamper-{case}.jsonl"
        journal = ForensicSessionJournal(
            path,
            clock=FakeClock(),
            session_id=str(uuid.UUID(int=1000 + case)),
        )
        for i in range(rng.randint(1, 8)):
            journal.append_material("worker.result", {"i": i, "value": rng.randint(-10_000, 10_000)})
        journal.close()

        lines = path.read_text(encoding="utf-8").splitlines()
        target_index = rng.randrange(len(lines))
        target = json.loads(lines[target_index])
        target["payload"]["tampered"] = case
        lines[target_index] = json.dumps(target, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        with pytest.raises(JournalIntegrityError):
            verify_journal(path)


def test_noncanonical_json_serialization_is_rejected_even_if_semantics_and_hash_match(tmp_path: Path) -> None:
    journal = new_journal(tmp_path)
    journal.append_material("worker.result", {"x": 1})
    journal.close()
    lines = journal.path.read_text(encoding="utf-8").splitlines()
    target = json.loads(lines[1])
    lines[1] = json.dumps(target, ensure_ascii=False, sort_keys=False, separators=(", ", ": "))
    journal.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(JournalIntegrityError, match="canonical JSON"):
        verify_journal(journal.path)


def test_duplicate_json_keys_are_rejected_instead_of_last_wins(tmp_path: Path) -> None:
    import hashlib

    path = tmp_path / "forensic-session.jsonl"
    unsigned = {
        "schema_version": 1,
        "seq": 1,
        "timestamp_utc": "2026-09-21T11:30:00.000000Z",
        "session_id": str(uuid.UUID(int=77)),
        "event_type": "worker.result",
        "payload": {"x": 2},
        "prev_sha256": "0" * 64,
    }
    encoded = json.dumps(
        unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    raw = (
        '{"schema_version":1,"seq":1,"timestamp_utc":"2026-09-21T11:30:00.000000Z",'
        f'"session_id":"{unsigned["session_id"]}","event_type":"worker.result",'
        '"payload":{"x":1},"payload":{"x":2},'
        f'"prev_sha256":"{"0" * 64}","sha256":"{digest}"}}\n'
    )
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(JournalIntegrityError, match="duplicate JSON object key"):
        verify_journal(path)


def test_nonstandard_nan_constant_is_rejected_as_integrity_error(tmp_path: Path) -> None:
    path = tmp_path / "forensic-session.jsonl"
    path.write_text(
        '{"event_type":"worker.result","payload":{"x":NaN},"prev_sha256":"'
        + "0" * 64
        + '","schema_version":1,"seq":1,"session_id":"00000000-0000-0000-0000-00000000004d",'
        + '"sha256":"' + "0" * 64 + '","timestamp_utc":"2026-09-21T11:30:00.000000Z"}\n',
        encoding="utf-8",
    )
    with pytest.raises(JournalIntegrityError, match="non-standard JSON constant"):
        verify_journal(path)


def test_verifier_rejects_hash_valid_record_with_unredacted_secret(tmp_path: Path) -> None:
    import hashlib

    path = tmp_path / "forensic-session.jsonl"
    session_id = str(uuid.UUID(int=77))
    unsigned = {
        "schema_version": 1,
        "seq": 1,
        "timestamp_utc": "2026-09-21T11:30:00.000000Z",
        "session_id": session_id,
        "event_type": "provider.request",
        "payload": {"api_key": "should-never-be-stored"},
        "prev_sha256": "0" * 64,
    }
    encoded = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    path.write_text(
        json.dumps(
            {**unsigned, "sha256": digest},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(JournalIntegrityError, match="unredacted sensitive"):
        verify_journal(path)


def test_symlink_alias_cannot_bypass_single_writer_lock(tmp_path: Path) -> None:
    path = tmp_path / "forensic-session.jsonl"
    alias = tmp_path / "alias.jsonl"
    owner = ForensicSessionJournal(path, clock=FakeClock(), session_id=str(uuid.UUID(int=31)))
    try:
        try:
            alias.symlink_to(path)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not available in this environment")
        with pytest.raises(JournalLockedError):
            ForensicSessionJournal(alias, clock=FakeClock(), session_id=str(uuid.UUID(int=32)))
        assert [record.event_type for record in verify_journal(path)] == ["lifecycle.startup"]
    finally:
        owner.close()


def test_hardlink_alias_is_rejected_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "forensic-session.jsonl"
    owner = ForensicSessionJournal(path, clock=FakeClock(), session_id=str(uuid.UUID(int=33)))
    owner.close()
    alias = tmp_path / "hardlink.jsonl"
    try:
        alias.hardlink_to(path)
    except (OSError, NotImplementedError):
        pytest.skip("hard links are not available in this environment")
    with pytest.raises(JournalIntegrityError, match="hard-linked"):
        ForensicSessionJournal(alias, clock=FakeClock(), session_id=str(uuid.UUID(int=34)))
    assert [record.event_type for record in verify_journal(path)] == [
        "lifecycle.startup",
        "lifecycle.shutdown",
    ]


def test_constructor_never_adopts_path_replacement_after_verified_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal_path = tmp_path / "forensic-session.jsonl"
    journal = ForensicSessionJournal(
        journal_path,
        clock=FakeClock(),
        session_id=str(uuid.UUID(int=1)),
    )
    journal.append_material("provider.connected", {"provider": "paper"})
    journal.close()

    original = journal_path.read_bytes()
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(original)

    real_parse = forensic_session_journal._parse_verified_record_bytes
    swapped = False

    def parse_then_replace(raw: bytes):
        nonlocal swapped
        records = real_parse(raw)
        if not swapped:
            swapped = True
            replacement.replace(journal_path)
        return records

    monkeypatch.setattr(
        forensic_session_journal,
        "_parse_verified_record_bytes",
        parse_then_replace,
    )

    with pytest.raises(
        JournalIntegrityError,
        match="journal path identity changed during verification",
    ):
        ForensicSessionJournal(
            journal_path,
            clock=FakeClock(),
            session_id=str(uuid.UUID(int=2)),
        )

    assert swapped is True
    assert journal_path.read_bytes() == original
    assert len(journal_path.read_text(encoding="utf-8").splitlines()) == len(
        original.decode("utf-8").splitlines()
    )


def test_deleted_active_journal_is_never_silently_recreated(tmp_path: Path) -> None:
    path = tmp_path / "forensic-session.jsonl"
    journal = ForensicSessionJournal(path, clock=FakeClock(), session_id=str(uuid.UUID(int=41)))
    path.unlink()
    with pytest.raises(JournalUncertainError, match="reopen and reverify"):
        journal.append_material("after.delete", {"x": 1})
    assert not path.exists()


def test_truncated_active_journal_fails_before_next_append(tmp_path: Path) -> None:
    path = tmp_path / "forensic-session.jsonl"
    journal = ForensicSessionJournal(path, clock=FakeClock(), session_id=str(uuid.UUID(int=42)))
    path.write_bytes(b"")
    with pytest.raises(JournalIntegrityError, match="size changed"):
        journal.append_material("after.truncate", {"x": 1})
    assert path.read_bytes() == b""


def test_replaced_active_journal_same_size_fails_on_identity_change(tmp_path: Path) -> None:
    path = tmp_path / "forensic-session.jsonl"
    journal = ForensicSessionJournal(path, clock=FakeClock(), session_id=str(uuid.UUID(int=43)))
    original = path.read_bytes()
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(original)
    replacement.replace(path)
    with pytest.raises(JournalIntegrityError, match="identity changed"):
        journal.append_material("after.replace", {"x": 1})
    assert path.read_bytes() == original


def test_hardlink_created_while_writer_active_blocks_next_append(tmp_path: Path) -> None:
    path = tmp_path / "forensic-session.jsonl"
    journal = ForensicSessionJournal(path, clock=FakeClock(), session_id=str(uuid.UUID(int=44)))
    alias = tmp_path / "late-hardlink.jsonl"
    try:
        alias.hardlink_to(path)
    except (OSError, NotImplementedError):
        journal.close()
        pytest.skip("hard links are not available in this environment")
    with pytest.raises(JournalIntegrityError, match="link count changed"):
        journal.append_material("after.hardlink", {"x": 1})
    assert verify_journal(path)[-1].event_type == "lifecycle.startup"


def test_deleted_lock_sidecar_cannot_admit_second_writer(tmp_path: Path) -> None:
    path = tmp_path / "forensic-session.jsonl"
    owner = ForensicSessionJournal(path, clock=FakeClock(), session_id=str(uuid.UUID(int=51)))
    before = path.read_bytes()
    lock_path = path.with_name(path.name + ".lock")
    try:
        lock_path.unlink()
    except OSError:
        owner.close()
        pytest.skip("platform prevents unlinking an active writer lock sidecar")

    with pytest.raises(JournalIntegrityError, match="lock sidecar was missing|ownership continuity"):
        ForensicSessionJournal(path, clock=FakeClock(), session_id=str(uuid.UUID(int=52)))

    assert path.read_bytes() == before
    assert not lock_path.exists()
    with pytest.raises(JournalIntegrityError, match="lock sidecar disappeared"):
        owner.append_material("must.not.write", {"x": 1})
    assert path.read_bytes() == before


def test_replaced_lock_sidecar_invalidates_active_writer(tmp_path: Path) -> None:
    path = tmp_path / "forensic-session.jsonl"
    owner = ForensicSessionJournal(path, clock=FakeClock(), session_id=str(uuid.UUID(int=53)))
    before = path.read_bytes()
    lock_path = path.with_name(path.name + ".lock")
    try:
        lock_path.unlink()
    except OSError:
        owner.close()
        pytest.skip("platform prevents replacing an active writer lock sidecar")
    lock_path.write_bytes(b"AUTOSPORT_FORENSIC_SESSION_LOCK_V1\n")

    with pytest.raises(JournalIntegrityError, match="lock sidecar identity changed"):
        owner.append_material("must.not.write", {"x": 1})
    assert path.read_bytes() == before


def test_symlink_lock_sidecar_is_rejected_without_touching_target(tmp_path: Path) -> None:
    path = tmp_path / "forensic-session.jsonl"
    target = tmp_path / "unrelated.lock-target"
    target.write_bytes(b"AUTOSPORT_FORENSIC_SESSION_LOCK_V1\n")
    lock_path = path.with_name(path.name + ".lock")
    try:
        lock_path.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available in this environment")

    with pytest.raises(JournalIntegrityError, match="lock sidecar"):
        ForensicSessionJournal(path, clock=FakeClock(), session_id=str(uuid.UUID(int=54)))
    assert target.read_bytes() == b"AUTOSPORT_FORENSIC_SESSION_LOCK_V1\n"
    assert not path.exists()
