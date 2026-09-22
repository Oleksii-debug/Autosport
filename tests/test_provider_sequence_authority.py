from __future__ import annotations

import multiprocessing
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import pytest

from autosport.provider_sequence_authority import (
    ProviderSequenceAuthorityError,
    SQLiteProviderSequenceAuthority,
)


AUTHORITY_ID = "provider-sequence:test-v1"
SOURCE_ID = "matchbook:soccer:EUR:expanded:abc123"


def _authority(
    path: Path,
    *,
    create: bool,
    authority_id: str = AUTHORITY_ID,
    timeout: float = 10.0,
) -> SQLiteProviderSequenceAuthority:
    return SQLiteProviderSequenceAuthority(
        path,
        authority_id=authority_id,
        create=create,
        busy_timeout_seconds=timeout,
    )


def _spawn_allocate(
    path_text: str,
    authority_id: str,
    source_id: str,
    count: int,
    output,
) -> None:
    try:
        authority = SQLiteProviderSequenceAuthority(
            path_text,
            authority_id=authority_id,
            create=False,
            busy_timeout_seconds=15.0,
        )
        values = [authority(source_id) for _ in range(count)]
    except Exception as exc:  # child process must return failure evidence to parent
        output.put(("error", type(exc).__name__, str(exc)))
    else:
        output.put(("ok", values))


def test_first_create_and_reopen_preserve_strict_sequence(tmp_path: Path) -> None:
    path = tmp_path / "provider-sequence.db"
    first = _authority(path, create=True)

    assert first(SOURCE_ID) == 1
    assert first.allocate(SOURCE_ID) == 2

    reopened = _authority(path, create=False)
    assert reopened(SOURCE_ID) == 3
    assert first(SOURCE_ID) == 4


def test_create_true_on_existing_authority_never_resets_state(tmp_path: Path) -> None:
    path = tmp_path / "provider-sequence.db"
    first = _authority(path, create=True)
    assert [first(SOURCE_ID) for _ in range(3)] == [1, 2, 3]

    reopened_with_create = _authority(path, create=True)
    assert reopened_with_create(SOURCE_ID) == 4


def test_source_sequences_are_independent_but_same_authority_bound(tmp_path: Path) -> None:
    path = tmp_path / "provider-sequence.db"
    authority = _authority(path, create=True)

    assert authority("provider-a") == 1
    assert authority("provider-b") == 1
    assert authority("provider-a") == 2
    assert authority("provider-b") == 2


def test_missing_reopen_fails_without_creating_database(tmp_path: Path) -> None:
    path = tmp_path / "missing.db"
    assert not path.exists()

    with pytest.raises(ProviderSequenceAuthorityError, match="missing"):
        _authority(path, create=False)

    assert not path.exists()


def test_deleted_live_authority_fails_closed_instead_of_restarting_at_one(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-sequence.db"
    authority = _authority(path, create=True)
    assert authority(SOURCE_ID) == 1

    path.unlink()
    with pytest.raises(ProviderSequenceAuthorityError, match="missing"):
        authority(SOURCE_ID)


def test_wrong_authority_identity_cannot_rebind_existing_database(tmp_path: Path) -> None:
    path = tmp_path / "provider-sequence.db"
    authority = _authority(path, create=True)
    assert authority(SOURCE_ID) == 1

    with pytest.raises(ProviderSequenceAuthorityError, match="identity/schema"):
        _authority(path, create=False, authority_id="different-authority")

    assert authority(SOURCE_ID) == 2


def test_two_independent_instances_serialize_one_source(tmp_path: Path) -> None:
    path = tmp_path / "provider-sequence.db"
    first = _authority(path, create=True)
    second = _authority(path, create=False)

    values = []
    for index in range(20):
        current = first if index % 2 == 0 else second
        values.append(current(SOURCE_ID))

    assert values == list(range(1, 21))


def test_threaded_allocations_have_no_duplicates_or_gaps(tmp_path: Path) -> None:
    path = tmp_path / "provider-sequence.db"
    _authority(path, create=True)
    authorities = [_authority(path, create=False) for _ in range(4)]

    def allocate(index: int) -> int:
        return authorities[index % len(authorities)](SOURCE_ID)

    with ThreadPoolExecutor(max_workers=8) as executor:
        values = list(executor.map(allocate, range(80)))

    assert sorted(values) == list(range(1, 81))
    assert len(set(values)) == 80


def test_spawned_processes_share_one_sqlite_sequence(tmp_path: Path) -> None:
    path = tmp_path / "provider-sequence.db"
    _authority(path, create=True, timeout=15.0)

    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    processes = [
        context.Process(
            target=_spawn_allocate,
            args=(str(path), AUTHORITY_ID, SOURCE_ID, 12, output),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()

    for process in processes:
        process.join(timeout=45)
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
            pytest.fail("spawned sequence allocator process did not terminate")
        assert process.exitcode == 0

    results = [output.get(timeout=5) for _ in processes]
    assert all(result[0] == "ok" for result in results), results
    values = [value for result in results for value in result[1]]
    assert sorted(values) == list(range(1, 25))


@pytest.mark.parametrize(
    "source_id",
    [
        "",
        " source",
        "source ",
        "provider|source",
        "bad\x00source",
        "bad\nsource",
    ],
)
def test_noncanonical_source_identity_is_rejected(
    tmp_path: Path,
    source_id: str,
) -> None:
    authority = _authority(tmp_path / "provider-sequence.db", create=True)
    with pytest.raises(ValueError, match="source_id"):
        authority(source_id)


@pytest.mark.parametrize(
    "authority_id",
    ["", " id", "id ", "bad\x00id", "bad\nid"],
)
def test_noncanonical_authority_identity_is_rejected(
    tmp_path: Path,
    authority_id: str,
) -> None:
    with pytest.raises(ValueError, match="authority_id"):
        _authority(
            tmp_path / "provider-sequence.db",
            create=True,
            authority_id=authority_id,
        )


@pytest.mark.parametrize("value", [None, 0, 1, "true"])
def test_create_mode_must_be_explicit_bool(tmp_path: Path, value: object) -> None:
    with pytest.raises(TypeError, match="create"):
        SQLiteProviderSequenceAuthority(
            tmp_path / "provider-sequence.db",
            authority_id=AUTHORITY_ID,
            create=value,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "timeout",
    [True, False, 0, -1, float("inf"), float("-inf"), float("nan"), Decimal("1")],
)
def test_busy_timeout_must_be_positive_finite_runtime_number(
    tmp_path: Path,
    timeout: object,
) -> None:
    with pytest.raises(ValueError, match="busy_timeout"):
        SQLiteProviderSequenceAuthority(
            tmp_path / "provider-sequence.db",
            authority_id=AUTHORITY_ID,
            create=True,
            busy_timeout_seconds=timeout,  # type: ignore[arg-type]
        )


def test_memory_database_is_rejected_as_non_durable(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="durable filesystem"):
        SQLiteProviderSequenceAuthority(
            ":memory:",
            authority_id=AUTHORITY_ID,
            create=True,
        )


def test_missing_parent_directory_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "missing-parent" / "provider-sequence.db"
    with pytest.raises(ValueError, match="parent directory"):
        _authority(path, create=True)


def test_signed_64_exhaustion_fails_closed_without_wraparound(tmp_path: Path) -> None:
    path = tmp_path / "provider-sequence.db"
    authority = _authority(path, create=True)
    assert authority(SOURCE_ID) == 1

    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE provider_sequences_v1 SET last_sequence=? WHERE source_id=?",
        ((1 << 63) - 1, SOURCE_ID),
    )
    connection.commit()
    connection.close()

    with pytest.raises(ProviderSequenceAuthorityError, match="exhausted"):
        authority(SOURCE_ID)

    connection = sqlite3.connect(path)
    row = connection.execute(
        "SELECT last_sequence FROM provider_sequences_v1 WHERE source_id=?",
        (SOURCE_ID,),
    ).fetchone()
    connection.close()
    assert row == ((1 << 63) - 1,)


def test_noninteger_persisted_sequence_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "provider-sequence.db"
    authority = _authority(path, create=True)
    assert authority(SOURCE_ID) == 1

    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE provider_sequences_v1 SET last_sequence=? WHERE source_id=?",
        ("not-an-integer", SOURCE_ID),
    )
    connection.commit()
    connection.close()

    with pytest.raises(ProviderSequenceAuthorityError, match="non-canonical integer"):
        authority(SOURCE_ID)


def test_schema_tamper_is_rejected_before_allocation(tmp_path: Path) -> None:
    path = tmp_path / "provider-sequence.db"
    authority = _authority(path, create=True)
    assert authority(SOURCE_ID) == 1

    connection = sqlite3.connect(path)
    connection.execute("ALTER TABLE provider_sequences_v1 ADD COLUMN attacker TEXT")
    connection.commit()
    connection.close()

    with pytest.raises(ProviderSequenceAuthorityError, match="schema is not canonical"):
        authority(SOURCE_ID)


def test_trigger_tamper_is_rejected_before_allocation(tmp_path: Path) -> None:
    path = tmp_path / "provider-sequence.db"
    authority = _authority(path, create=True)
    assert authority(SOURCE_ID) == 1

    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TRIGGER mutate_sequence_after_update
           AFTER UPDATE ON provider_sequences_v1
           BEGIN
             UPDATE provider_sequences_v1
             SET last_sequence = last_sequence + 1
             WHERE source_id = NEW.source_id;
           END"""
    )
    connection.commit()
    connection.close()

    with pytest.raises(ProviderSequenceAuthorityError, match="must not have triggers"):
        authority(SOURCE_ID)


def test_meta_row_multiplicity_or_version_drift_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "provider-sequence.db"
    authority = _authority(path, create=True)
    assert authority(SOURCE_ID) == 1

    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE provider_sequence_meta_v1 SET schema_version=99 WHERE singleton=1"
    )
    connection.commit()
    connection.close()

    with pytest.raises(ProviderSequenceAuthorityError, match="identity/schema"):
        authority(SOURCE_ID)


def test_create_true_does_not_claim_preexisting_uninitialized_file(tmp_path: Path) -> None:
    path = tmp_path / "provider-sequence.db"
    path.write_bytes(b"")

    with pytest.raises(ProviderSequenceAuthorityError, match="schema is not canonical"):
        _authority(path, create=True)

    assert path.read_bytes() == b""


def test_create_true_cannot_rebind_existing_different_authority(tmp_path: Path) -> None:
    path = tmp_path / "provider-sequence.db"
    original = _authority(path, create=True, authority_id="authority-a")
    assert original(SOURCE_ID) == 1

    with pytest.raises(ProviderSequenceAuthorityError, match="identity/schema"):
        _authority(path, create=True, authority_id="authority-b")

    assert original(SOURCE_ID) == 2

def test_journal_mode_drift_fails_closed_on_reopen_and_allocation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-sequence.db"
    authority = _authority(path, create=True)
    assert authority(SOURCE_ID) == 1

    connection = sqlite3.connect(path, isolation_level=None)
    changed = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
    connection.close()
    assert changed is not None
    assert str(changed[0]).lower() == "delete"

    with pytest.raises(ProviderSequenceAuthorityError, match="WAL"):
        _authority(path, create=False)

    with pytest.raises(ProviderSequenceAuthorityError, match="WAL"):
        authority(SOURCE_ID)

    connection = sqlite3.connect(path)
    row = connection.execute(
        "SELECT last_sequence FROM provider_sequences_v1 WHERE source_id=?",
        (SOURCE_ID,),
    ).fetchone()
    connection.close()
    assert row == (1,)

