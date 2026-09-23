from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import uuid
from functools import wraps
from decimal import (
    Context,
    Decimal,
    DecimalException,
    Inexact,
    InvalidOperation,
    Overflow,
    ROUND_HALF_EVEN,
    Underflow,
    localcontext,
)
from pathlib import Path
from weakref import WeakKeyDictionary

from .domain import PaperTicket, TicketLeg, TicketStatus, utc_now_iso
from .forecasting import parse_iso_timestamp


_PAPER_DECIMAL_PRECISION = 28
_PAPER_DECIMAL_EMIN = -999999
_PAPER_DECIMAL_EMAX = 999999
_PAPER_SNAPSHOT_SCHEMA_VERSION = 8
_SUPPORTED_PAPER_SNAPSHOT_SCHEMA_VERSIONS = frozenset({2, 3, 4, 5, 6, 7, 8})
_SCHEMA_MISSING = object()
_PAPER_SNAPSHOT_WITNESS_SCHEMA_VERSION = 1
_PAPER_SNAPSHOT_WITNESS_SUFFIX = ".paper-book-snapshot-witness.jsonl"
_PAPER_SNAPSHOT_WITNESS_PREPARE = "PREPARE"
_PAPER_SNAPSHOT_WITNESS_COMMIT = "COMMIT"
_PAPER_SNAPSHOT_WITNESS_ABORT = "ABORT"

_LifecycleEntry = tuple[str, str, tuple[str, ...], tuple[str, ...]]


def _paper_decimal_context() -> Context:
    context = Context(
        prec=_PAPER_DECIMAL_PRECISION,
        rounding=ROUND_HALF_EVEN,
        Emin=_PAPER_DECIMAL_EMIN,
        Emax=_PAPER_DECIMAL_EMAX,
    )
    context.traps[InvalidOperation] = True
    context.traps[Overflow] = True
    context.traps[Underflow] = True
    context.clear_flags()
    return context


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"PaperBook snapshot contains duplicate JSON key: {key}")
        payload[key] = value
    return payload


def _reject_nonfinite_json_constant(value: str) -> None:
    raise ValueError(f"PaperBook snapshot contains non-finite JSON constant: {value}")


def _snapshot_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _snapshot_identity(path: Path) -> str:
    normalized = os.path.normcase(os.path.abspath(os.fspath(path)))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _snapshot_witness_path(snapshot_path: Path) -> Path:
    # Reuse the already-established independent PAPER execution authority root
    # rather than storing a caller-editable trust digest beside the snapshot.
    from ._paper_execution_anti_rollback import _authority_root

    identity = _snapshot_identity(snapshot_path)
    return _authority_root(snapshot_path) / f"{identity}{_PAPER_SNAPSHOT_WITNESS_SUFFIX}"


def _snapshot_publication_lock_path(witness_path: Path) -> Path:
    return witness_path.with_name(witness_path.name + ".writer.lock")


_snapshot_publication_process_guard = threading.Lock()
_snapshot_publication_process_locks: set[str] = set()


def _acquire_snapshot_publication_lock(witness_path: Path) -> tuple[int, str]:
    lock_path = _snapshot_publication_lock_path(witness_path)
    lock_key = _canonical_path_key(lock_path)
    with _snapshot_publication_process_guard:
        if lock_key in _snapshot_publication_process_locks:
            raise ValueError(
                "PaperBook snapshot publication lock is held by another writer"
            )
        _snapshot_publication_process_locks.add(lock_key)

    fd: int | None = None
    try:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            raise ValueError("cannot open PaperBook snapshot publication lock") from exc

        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
                os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ValueError(
                    "PaperBook snapshot publication lock is held by another writer"
                ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ValueError(
                    "PaperBook snapshot publication lock is held by another writer"
                ) from exc
        return fd, lock_key
    except BaseException:
        if fd is not None:
            os.close(fd)
        with _snapshot_publication_process_guard:
            _snapshot_publication_process_locks.discard(lock_key)
        raise


def _release_snapshot_publication_lock(lock: tuple[int, str]) -> None:
    fd, lock_key = lock
    try:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError as exc:
            raise ValueError("cannot release PaperBook snapshot publication lock") from exc
        finally:
            os.close(fd)
    finally:
        with _snapshot_publication_process_guard:
            _snapshot_publication_process_locks.discard(lock_key)


def _canonical_path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _make_snapshot_authority_registry():
    # Authority is intentionally kept in an installation-private weak registry.
    # A caller-visible attribute on PaperBook is not authority: byte-loaded books
    # must not become mutable merely by flipping a boolean field. Bound authority
    # also carries an optimistic concurrency token: the exact durable witness
    # generation and snapshot SHA from which the object was loaded/published.
    bindings = WeakKeyDictionary()

    def validated_generation_state(
        generation: int,
        snapshot_sha256: str,
    ) -> tuple[int, str]:
        if type(generation) is not int or generation < 0:
            raise ValueError("PaperBook snapshot authority generation is invalid")
        if generation == 0:
            if snapshot_sha256 != "":
                raise ValueError(
                    "PaperBook virgin snapshot authority cannot carry a snapshot SHA"
                )
            return generation, snapshot_sha256
        if (
            type(snapshot_sha256) is not str
            or len(snapshot_sha256) != 64
            or any(character not in "0123456789abcdef" for character in snapshot_sha256)
        ):
            raise ValueError("PaperBook snapshot authority SHA-256 is invalid")
        return generation, snapshot_sha256

    def candidate(
        snapshot_path: Path,
        witness_path: Path,
        *,
        generation: int,
        snapshot_sha256: str,
    ) -> tuple[str, str, str, str, int, str]:
        generation, snapshot_sha256 = validated_generation_state(
            generation,
            snapshot_sha256,
        )
        return (
            "BOUND",
            _snapshot_identity(snapshot_path),
            _canonical_path_key(snapshot_path),
            _canonical_path_key(witness_path),
            generation,
            snapshot_sha256,
        )

    def register_fresh(book: object) -> None:
        bindings[book] = ("FRESH", "", "", "", 0, "")

    def revoke(book: object) -> None:
        bindings.pop(book, None)

    def current(book: object):
        return bindings.get(book)

    def bind(
        book: object,
        snapshot_path: Path,
        witness_path: Path,
        *,
        generation: int = 0,
        snapshot_sha256: str = "",
    ) -> None:
        next_binding = candidate(
            snapshot_path,
            witness_path,
            generation=generation,
            snapshot_sha256=snapshot_sha256,
        )
        existing = bindings.get(book)
        if (
            existing is not None
            and existing[0] == "BOUND"
            and existing[:4] != next_binding[:4]
        ):
            raise ValueError("PaperBook snapshot authority cannot be rebound to another path")
        if (
            existing is not None
            and existing[0] == "BOUND"
            and existing != next_binding
        ):
            raise ValueError(
                "PaperBook snapshot authority generation can advance only after commit"
            )
        if existing is not None and existing[0] not in {"FRESH", "BOUND"}:
            raise ValueError("PaperBook snapshot authority state is invalid")
        bindings[book] = next_binding

    def advance(
        book: object,
        snapshot_path: Path,
        witness_path: Path,
        *,
        expected_generation: int,
        expected_snapshot_sha256: str,
        new_generation: int,
        new_snapshot_sha256: str,
    ) -> None:
        expected = candidate(
            snapshot_path,
            witness_path,
            generation=expected_generation,
            snapshot_sha256=expected_snapshot_sha256,
        )
        if bindings.get(book) != expected:
            raise ValueError(
                "PaperBook snapshot authority changed before durable generation advance"
            )
        if new_generation <= expected_generation:
            raise ValueError("PaperBook snapshot authority generation did not advance")
        bindings[book] = candidate(
            snapshot_path,
            witness_path,
            generation=new_generation,
            snapshot_sha256=new_snapshot_sha256,
        )

    return register_fresh, revoke, current, bind, advance


(
    _register_fresh_snapshot_authority,
    _revoke_snapshot_authority,
    _snapshot_authority_binding,
    _bind_snapshot_authority,
    _advance_snapshot_authority,
) = _make_snapshot_authority_registry()


def _make_paperbook_state_lock_registry():
    locks = WeakKeyDictionary()
    guard = threading.Lock()

    def register(book: object) -> None:
        with guard:
            locks[book] = threading.RLock()

    def current(book: object):
        with guard:
            lock = locks.get(book)
        if lock is None:
            raise RuntimeError("PaperBook state lock is unavailable")
        return lock

    return register, current


_register_paperbook_state_lock, _paperbook_state_lock = (
    _make_paperbook_state_lock_registry()
)


def _serialize_paperbook_state(method):
    @wraps(method)
    def serialized(self, *args, **kwargs):
        with _paperbook_state_lock(self):
            return method(self, *args, **kwargs)

    return serialized


def _ticket_opening_commitment(ticket: PaperTicket) -> tuple[object, ...]:
    return (
        ticket.stake,
        ticket.legs,
        ticket.placed_at,
        ticket.provider_source_ids,
        ticket.provider_accounts,
        ticket.bankroll_id,
        ticket.currency,
    )


def _make_ticket_opening_authority_registry():
    authorities = WeakKeyDictionary()
    guard = threading.Lock()

    def register_book(book: object) -> None:
        with guard:
            authorities[book] = {}

    def record(book: object, ticket: PaperTicket) -> None:
        commitment = _ticket_opening_commitment(ticket)
        with guard:
            current = authorities.get(book)
            if current is None:
                raise RuntimeError("PaperBook opening authority registry is unavailable")
            existing = current.get(ticket.ticket_id)
            if existing is not None and existing != commitment:
                raise ValueError(
                    "PaperBook ticket opening authority cannot be rebound"
                )
            current[ticket.ticket_id] = commitment

    def install_verified_snapshot(book: object) -> None:
        commitments = {
            ticket_id: _ticket_opening_commitment(ticket)
            for ticket_id, ticket in book.tickets.items()
        }
        with guard:
            if book not in authorities:
                raise RuntimeError("PaperBook opening authority registry is unavailable")
            authorities[book] = commitments

    def require(book: object, ticket: PaperTicket) -> None:
        with guard:
            current = authorities.get(book)
            expected = None if current is None else current.get(ticket.ticket_id)
        if expected is None:
            raise ValueError(
                "PaperBook ticket lacks product-issued opening economic authority"
            )
        if _ticket_opening_commitment(ticket) != expected:
            raise ValueError(
                "PaperBook ticket opening economic identity changed after admission"
            )

    def require_candidate(source_book: object, candidate_book: object) -> None:
        with guard:
            current = authorities.get(source_book)
            if current is None:
                raise RuntimeError("PaperBook opening authority registry is unavailable")
            expected = dict(current)
        candidate_tickets = getattr(candidate_book, "tickets", None)
        if type(candidate_tickets) is not dict:
            raise ValueError("PaperBook serialized candidate ticket mapping is invalid")
        if set(candidate_tickets) != set(expected):
            raise ValueError(
                "PaperBook serialized candidate ticket set differs from "
                "product-issued opening authority"
            )
        for ticket_id, ticket in candidate_tickets.items():
            if (
                type(ticket) is not PaperTicket
                or _ticket_opening_commitment(ticket) != expected[ticket_id]
            ):
                raise ValueError(
                    "PaperBook serialized candidate opening economic identity "
                    "differs from product-issued authority"
                )

    return (
        register_book,
        record,
        install_verified_snapshot,
        require,
        require_candidate,
    )


(
    _register_ticket_opening_authority_book,
    _record_ticket_opening_authority,
    _install_verified_ticket_opening_authority,
    _require_ticket_opening_authority,
    _require_snapshot_candidate_opening_authority,
) = _make_ticket_opening_authority_registry()


def _paperbook_causal_history_snapshot(book: object) -> tuple[object, ...]:
    lifecycle = getattr(book, "_lifecycle", None)
    settlement_times = getattr(book, "_settlement_times", None)
    if type(lifecycle) is not list or type(settlement_times) is not dict:
        raise ValueError("PaperBook causal history state is not canonical")
    return (
        tuple(lifecycle),
        tuple(sorted(settlement_times.items())),
    )


def _make_paperbook_causal_history_authority_registry():
    authorities = WeakKeyDictionary()
    guard = threading.Lock()

    def register_book(book: object) -> None:
        with guard:
            authorities[book] = ((), ())

    def install_verified_snapshot(book: object) -> None:
        snapshot = _paperbook_causal_history_snapshot(book)
        with guard:
            if book not in authorities:
                raise RuntimeError(
                    "PaperBook causal history authority registry is unavailable"
                )
            authorities[book] = snapshot

    def require(book: object) -> None:
        actual = _paperbook_causal_history_snapshot(book)
        with guard:
            expected = authorities.get(book)
        if expected is None:
            raise ValueError(
                "PaperBook lacks product-issued causal history authority"
            )
        if actual != expected:
            raise ValueError(
                "PaperBook causal history changed outside product-issued transitions"
            )

    def require_candidate(source_book: object, candidate_book: object) -> None:
        candidate = _paperbook_causal_history_snapshot(candidate_book)
        with guard:
            expected = authorities.get(source_book)
        if expected is None:
            raise ValueError(
                "PaperBook lacks product-issued causal history authority"
            )
        if candidate != expected:
            raise ValueError(
                "PaperBook serialized candidate causal history differs from "
                "product-issued authority"
            )

    def advance_open(book: object, ticket_id: str) -> None:
        with guard:
            expected = authorities.get(book)
            if expected is None:
                raise RuntimeError(
                    "PaperBook causal history authority registry is unavailable"
                )
            lifecycle, settlement_times = expected
            authorities[book] = (
                lifecycle + (("open", ticket_id, (), ()),),
                settlement_times,
            )

    def advance_settle(
        book: object,
        ticket_id: str,
        winners: tuple[str, ...],
        voids: tuple[str, ...],
        settled_at: str | None,
    ) -> None:
        with guard:
            expected = authorities.get(book)
            if expected is None:
                raise RuntimeError(
                    "PaperBook causal history authority registry is unavailable"
                )
            lifecycle, settlement_times = expected
            settlement_mapping = dict(settlement_times)
            if ticket_id in settlement_mapping:
                raise ValueError(
                    "PaperBook causal history settlement authority cannot be rebound"
                )
            settlement_mapping[ticket_id] = settled_at
            authorities[book] = (
                lifecycle + (("settle", ticket_id, winners, voids),),
                tuple(sorted(settlement_mapping.items())),
            )

    return (
        register_book,
        install_verified_snapshot,
        require,
        require_candidate,
        advance_open,
        advance_settle,
    )


(
    _register_paperbook_causal_history_authority_book,
    _install_verified_paperbook_causal_history_authority,
    _require_paperbook_causal_history_authority,
    _require_snapshot_candidate_causal_history_authority,
    _advance_paperbook_causal_history_open,
    _advance_paperbook_causal_history_settle,
) = _make_paperbook_causal_history_authority_registry()


def _snapshot_witness_digest(payload: dict[str, object]) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("PaperBook snapshot witness is not canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _require_snapshot_sha256(value: object, label: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"PaperBook {label} must be lowercase SHA-256 hex")
    return value


def _read_snapshot_witnesses(
    snapshot_path: Path,
    *,
    witness_path: Path | None = None,
) -> tuple[list[dict[str, object]], tuple[int, str] | None, tuple[int, str] | None]:
    witness_path = (
        _snapshot_witness_path(snapshot_path)
        if witness_path is None
        else Path(witness_path)
    )
    if not witness_path.exists():
        return [], None, None
    try:
        lines = witness_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError("cannot read PaperBook independent snapshot witness") from exc

    expected_keys = {
        "witness_schema_version",
        "sequence",
        "generation",
        "event",
        "snapshot_identity",
        "snapshot_name",
        "snapshot_sha256",
        "previous_witness_sha256",
        "witness_sha256",
    }
    expected_identity = _snapshot_identity(snapshot_path)
    records: list[dict[str, object]] = []
    previous_witness_sha256: str | None = None
    committed: tuple[int, str] | None = None
    pending: tuple[int, str] | None = None
    last_generation = 0

    for sequence, line in enumerate(lines, start=1):
        if not line:
            raise ValueError("PaperBook snapshot witness contains a blank line")
        try:
            record = json.loads(
                line,
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=_reject_nonfinite_json_constant,
            )
        except (json.JSONDecodeError, RecursionError) as exc:
            raise ValueError("PaperBook snapshot witness is unreadable") from exc
        if type(record) is not dict or set(record) != expected_keys:
            raise ValueError("PaperBook snapshot witness schema is invalid")
        if record["witness_schema_version"] != _PAPER_SNAPSHOT_WITNESS_SCHEMA_VERSION:
            raise ValueError("unsupported PaperBook snapshot witness schema")
        if record["sequence"] != sequence:
            raise ValueError("PaperBook snapshot witness sequence is not contiguous")
        if record["snapshot_identity"] != expected_identity:
            raise ValueError("PaperBook snapshot witness belongs to another path")
        if record["snapshot_name"] != snapshot_path.name:
            raise ValueError("PaperBook snapshot witness belongs to another file")
        snapshot_sha = _require_snapshot_sha256(
            record["snapshot_sha256"],
            "snapshot witness snapshot_sha256",
        )
        previous = record["previous_witness_sha256"]
        if previous != previous_witness_sha256:
            raise ValueError("PaperBook snapshot witness predecessor mismatch")
        body = {key: record[key] for key in expected_keys if key != "witness_sha256"}
        witness_sha = _require_snapshot_sha256(
            record["witness_sha256"],
            "snapshot witness witness_sha256",
        )
        if witness_sha != _snapshot_witness_digest(body):
            raise ValueError("PaperBook snapshot witness digest mismatch")
        generation = record["generation"]
        if type(generation) is not int or generation <= 0:
            raise ValueError("PaperBook snapshot witness generation is invalid")
        event = record["event"]
        if event == _PAPER_SNAPSHOT_WITNESS_PREPARE:
            if pending is not None or generation != last_generation + 1:
                raise ValueError("PaperBook snapshot witness PREPARE order is invalid")
            pending = (generation, snapshot_sha)
            last_generation = generation
        elif event in {
            _PAPER_SNAPSHOT_WITNESS_COMMIT,
            _PAPER_SNAPSHOT_WITNESS_ABORT,
        }:
            if pending != (generation, snapshot_sha):
                raise ValueError(
                    f"PaperBook snapshot witness {event} has no matching PREPARE"
                )
            if event == _PAPER_SNAPSHOT_WITNESS_COMMIT:
                committed = pending
            pending = None
        else:
            raise ValueError("PaperBook snapshot witness event is invalid")
        records.append(record)
        previous_witness_sha256 = witness_sha

    return records, committed, pending


def _append_snapshot_witness(
    snapshot_path: Path,
    *,
    event: str,
    generation: int,
    snapshot_sha256: str,
    witness_path: Path | None = None,
) -> None:
    witness_path = (
        _snapshot_witness_path(snapshot_path)
        if witness_path is None
        else Path(witness_path)
    )
    records, _, _ = _read_snapshot_witnesses(
        snapshot_path,
        witness_path=witness_path,
    )
    previous_witness_sha256 = (
        None if not records else str(records[-1]["witness_sha256"])
    )
    body: dict[str, object] = {
        "witness_schema_version": _PAPER_SNAPSHOT_WITNESS_SCHEMA_VERSION,
        "sequence": len(records) + 1,
        "generation": generation,
        "event": event,
        "snapshot_identity": _snapshot_identity(snapshot_path),
        "snapshot_name": snapshot_path.name,
        "snapshot_sha256": _require_snapshot_sha256(
            snapshot_sha256,
            "snapshot witness snapshot_sha256",
        ),
        "previous_witness_sha256": previous_witness_sha256,
    }
    record = {**body, "witness_sha256": _snapshot_witness_digest(body)}
    existed = witness_path.exists()
    try:
        with witness_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        if not existed:
            from ._paper_execution_anti_rollback import _sync_authority_directory

            _sync_authority_directory(witness_path.parent)
    except OSError as exc:
        raise ValueError("PaperBook snapshot witness durability barrier failed") from exc


def _file_sha256(path: Path) -> str | None:
    try:
        return _snapshot_sha256(path.read_bytes())
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError("cannot read PaperBook snapshot for witness verification") from exc


def _recover_snapshot_witness_if_possible(
    snapshot_path: Path,
    snapshot_sha256: str,
    *,
    witness_path: Path | None = None,
) -> tuple[int, str] | None:
    witness_path = (
        _snapshot_witness_path(snapshot_path)
        if witness_path is None
        else Path(witness_path)
    )
    _, committed, pending = _read_snapshot_witnesses(
        snapshot_path,
        witness_path=witness_path,
    )
    if pending is None:
        return committed

    generation, pending_sha256 = pending
    if pending_sha256 == snapshot_sha256:
        # The replacement reached durable snapshot bytes but COMMIT publication
        # was interrupted. Complete that exact generation.
        _append_snapshot_witness(
            snapshot_path,
            event=_PAPER_SNAPSHOT_WITNESS_COMMIT,
            generation=generation,
            snapshot_sha256=pending_sha256,
            witness_path=witness_path,
        )
    elif committed is not None and committed[1] == snapshot_sha256:
        # PREPARE was durable but os.replace did not publish the candidate.
        # Preserve the last committed snapshot and close the failed attempt.
        _append_snapshot_witness(
            snapshot_path,
            event=_PAPER_SNAPSHOT_WITNESS_ABORT,
            generation=generation,
            snapshot_sha256=pending_sha256,
            witness_path=witness_path,
        )
    else:
        raise ValueError(
            "PaperBook snapshot witness has an incomplete generation for different bytes"
        )

    _, committed_after, pending_after = _read_snapshot_witnesses(
        snapshot_path,
        witness_path=witness_path,
    )
    if pending_after is not None:
        raise ValueError("PaperBook snapshot witness recovery did not close pending state")
    return committed_after


def _verify_snapshot_witness(
    snapshot_path: Path,
    payload: bytes,
    *,
    witness_path: Path | None = None,
) -> tuple[int, str]:
    witness_path = (
        _snapshot_witness_path(snapshot_path)
        if witness_path is None
        else Path(witness_path)
    )
    snapshot_sha = _snapshot_sha256(payload)
    committed = _recover_snapshot_witness_if_possible(
        snapshot_path,
        snapshot_sha,
        witness_path=witness_path,
    )
    if committed is None:
        raise ValueError(
            "PaperBook ticket snapshot is missing independent durable opening witness"
        )
    if committed[1] != snapshot_sha:
        raise ValueError(
            "PaperBook snapshot bytes do not match independent durable opening witness"
        )
    return committed


class PaperBook:
    """Virtual bankroll and auditable paper tickets. No real-money execution path exists."""

    def __init__(self, initial_bankroll: Decimal | str = Decimal("10000")) -> None:
        _register_paperbook_state_lock(self)
        _register_ticket_opening_authority_book(self)
        _register_paperbook_causal_history_authority_book(self)
        initial = Decimal(str(initial_bankroll))
        self._require_finite(initial, "initial_bankroll")
        if initial <= 0:
            raise ValueError("initial virtual bankroll must be positive")
        self.initial_bankroll = initial
        self.balance = self.initial_bankroll
        self.tickets: dict[str, PaperTicket] = {}
        # PaperBook owns the minimum lifecycle witness required to replay bankroll
        # chronology and settlement economics without changing the domain model.
        self._lifecycle: list[_LifecycleEntry] = []
        # Settlement timestamps are causal lifecycle evidence, not mutable ticket
        # decoration. Keeping them in a sidecar preserves the canonical four-field
        # lifecycle tuple used by rollback/state hashes while making timestamp
        # mutation mechanically detectable.
        self._settlement_times: dict[str, str | None] = {}
        # Fresh in-memory books may build one virgin PAPER snapshot. After the
        # first durable save, authority is bound to that exact snapshot identity
        # and external witness path. Byte-decoded books are explicitly revoked
        # until load() verifies and binds an existing durable witness.
        _register_fresh_snapshot_authority(self)
        self._snapshot_schema_version: int | None = _PAPER_SNAPSHOT_SCHEMA_VERSION

    def _require_snapshot_authority_for_economic_mutation(
        self,
        *,
        verify_bound_head: bool = True,
    ) -> None:
        binding = _snapshot_authority_binding(self)
        if binding is None:
            raise ValueError(
                "PaperBook byte-loaded snapshot lacks independent durable witness authority"
            )
        if binding[0] == "BOUND":
            snapshot_path = Path(binding[2])
            witness_path = Path(binding[3])
            current_witness_path = _snapshot_witness_path(snapshot_path)
            if _canonical_path_key(current_witness_path) != binding[3]:
                raise ValueError(
                    "PaperBook independent snapshot authority root changed after binding"
                )
            if verify_bound_head:
                publication_lock = _acquire_snapshot_publication_lock(witness_path)
                try:
                    _, committed, pending = _read_snapshot_witnesses(
                        snapshot_path,
                        witness_path=witness_path,
                    )
                    current_sha = _file_sha256(snapshot_path)
                    expected_generation = int(binding[4])
                    expected_snapshot_sha = str(binding[5])
                    if expected_generation == 0:
                        if (
                            pending is not None
                            or committed is not None
                            or current_sha is not None
                        ):
                            raise ValueError(
                                "PaperBook snapshot authority is stale; reload current durable snapshot"
                            )
                    elif (
                        pending is not None
                        or committed
                        != (expected_generation, expected_snapshot_sha)
                        or current_sha != expected_snapshot_sha
                    ):
                        raise ValueError(
                            "PaperBook snapshot authority is stale; reload current durable snapshot"
                        )
                finally:
                    _release_snapshot_publication_lock(publication_lock)

    @property
    @_serialize_paperbook_state
    def committed_stake(self) -> Decimal:
        return sum((t.stake for t in self.tickets.values() if t.status is TicketStatus.OPEN), Decimal("0"))

    @classmethod
    def _debit_balance(cls, balance: Decimal, amount: Decimal) -> Decimal:
        cls._require_finite(balance, "balance")
        cls._require_finite(amount, "stake")
        if amount <= 0:
            raise ValueError("stake must be positive")
        if amount > balance:
            raise ValueError("insufficient virtual bankroll")
        try:
            with localcontext(_paper_decimal_context()) as context:
                new_balance = balance - amount
                if context.flags[Inexact]:
                    raise ValueError("PaperBook stake debit loses Decimal precision")
        except DecimalException as exc:
            raise ValueError("PaperBook stake debit arithmetic is not representable") from exc
        return new_balance

    @_serialize_paperbook_state
    def open_ticket(
        self,
        legs,
        stake,
        reason: str = "",
        placed_at: str | None = None,
        *,
        provider_source_ids: tuple[str, ...] = (),
        provider_accounts: tuple[tuple[str, str], ...] = (),
        bankroll_id: str | None = None,
        currency: str | None = None,
    ) -> PaperTicket:
        self._require_snapshot_authority_for_economic_mutation()
        # A product transition must start from one fully coherent live epoch.
        # Private causal history alone does not cover caller-mutated balance,
        # ticket status/payout, or other replay-derived fields.
        self._validate_loaded_state(self)
        amount = Decimal(str(stake))
        new_balance = self._debit_balance(self.balance, amount)

        ticket_placed_at = self._validate_placed_at(
            placed_at if placed_at is not None else utc_now_iso()
        )
        self._require_utf8_string(reason, "strategy_reason")
        (
            provider_source_ids,
            provider_accounts,
            bankroll_id,
            currency,
        ) = self._validate_ticket_provenance(
            provider_source_ids,
            provider_accounts,
            bankroll_id,
            currency,
        )
        ticket_legs = tuple(legs)
        if not ticket_legs:
            raise ValueError("ticket requires at least one leg")
        for leg in ticket_legs:
            self._validate_ticket_leg(leg)
        quote_keys = [leg.quote_key for leg in ticket_legs]
        if len(quote_keys) != len(set(quote_keys)):
            raise ValueError("ticket contains duplicate quote_key leg")
        ticket = PaperTicket(
            ticket_id=str(uuid.uuid4()),
            stake=amount,
            legs=ticket_legs,
            placed_at=ticket_placed_at,
            strategy_reason=reason,
            provider_source_ids=provider_source_ids,
            provider_accounts=provider_accounts,
            bankroll_id=bankroll_id,
            currency=currency,
        )
        _record_ticket_opening_authority(self, ticket)
        self.balance = new_balance
        self.tickets[ticket.ticket_id] = ticket
        self._lifecycle.append(("open", ticket.ticket_id, (), ()))
        _advance_paperbook_causal_history_open(self, ticket.ticket_id)
        return ticket

    @staticmethod
    def _normalize_resolution_keys(values: object, label: str) -> set[str]:
        if isinstance(values, str) or values is None:
            raise ValueError(f"PaperBook {label} must be a collection of quote keys")
        try:
            normalized = set(values)
        except TypeError as exc:
            raise ValueError(f"PaperBook {label} must be a collection of quote keys") from exc
        if any(not isinstance(value, str) or not value for value in normalized):
            raise ValueError(f"PaperBook {label} must contain non-empty string quote keys")
        return normalized

    @classmethod
    def _settlement_result(
        cls,
        ticket: PaperTicket,
        balance: Decimal,
        winning_quote_keys: set[str],
        void_quote_keys: set[str],
    ) -> tuple[TicketStatus, Decimal, Decimal]:
        cls._require_finite(balance, "balance")
        leg_quote_keys = {leg.quote_key for leg in ticket.legs}
        unknown_winners = winning_quote_keys - leg_quote_keys
        unknown_voids = void_quote_keys - leg_quote_keys
        if unknown_winners:
            raise ValueError("PaperBook settlement contains unknown winning quote_key")
        if unknown_voids:
            raise ValueError("PaperBook settlement contains unknown void quote_key")
        if winning_quote_keys & void_quote_keys:
            raise ValueError("PaperBook settlement quote_key cannot be both winning and void")

        effective_legs = tuple(
            leg for leg in ticket.legs if leg.quote_key not in void_quote_keys
        )
        if any(leg.quote_key not in winning_quote_keys for leg in effective_legs):
            return TicketStatus.LOST, Decimal("0"), balance

        status = TicketStatus.VOID if not effective_legs else TicketStatus.WON
        try:
            with localcontext(_paper_decimal_context()):
                effective_odds = Decimal("1")
                for leg in effective_legs:
                    effective_odds *= leg.locked_odds
                payout = ticket.stake if status is TicketStatus.VOID else ticket.stake * effective_odds
                cls._require_finite(payout, f"settlement payout for ticket {ticket.ticket_id}")
                if status is TicketStatus.WON and payout <= ticket.stake:
                    raise ValueError(
                        "PaperBook winning settlement payout must exceed stake after canonical Decimal rounding"
                    )
                new_balance = balance + payout
                cls._require_finite(new_balance, f"balance after settling ticket {ticket.ticket_id}")
                if payout != 0 and new_balance == balance:
                    raise ValueError("PaperBook settlement payout loses all Decimal balance effect")
        except DecimalException as exc:
            raise ValueError("PaperBook settlement arithmetic is not representable") from exc
        return status, payout, new_balance

    @_serialize_paperbook_state
    def settle(
        self,
        ticket_id: str,
        winning_quote_keys: set[str],
        void_quote_keys: set[str] | None = None,
        *,
        settled_at: str | None = None,
    ) -> PaperTicket:
        self._require_snapshot_authority_for_economic_mutation()
        # Reject any caller-created live-state divergence before calculating or
        # publishing another official settlement transition.
        self._validate_loaded_state(self)
        ticket = self.tickets[ticket_id]
        if ticket.status is not TicketStatus.OPEN:
            raise ValueError("ticket already settled")

        # PaperTicket is intentionally mutable during a paper run. Revalidate
        # the current leg identities immediately before settlement so caller
        # mutation cannot route an unsupported LAY identity through BACK-only
        # payout arithmetic.
        for leg in ticket.legs:
            self._validate_ticket_leg(leg, ticket_id=ticket.ticket_id)
        self._validate_ticket_opening_economics(ticket)
        _require_ticket_opening_authority(self, ticket)

        winners = self._normalize_resolution_keys(winning_quote_keys, "winning_quote_keys")
        voids = (
            set()
            if void_quote_keys is None
            else self._normalize_resolution_keys(void_quote_keys, "void_quote_keys")
        )
        settlement_time = (
            None
            if settled_at is None
            else self._validate_settled_at(settled_at, ticket.placed_at)
        )
        status, payout, new_balance = self._settlement_result(
            ticket,
            self.balance,
            winners,
            voids,
        )

        ticket.payout = payout
        ticket.status = status
        ticket.settled_at = settlement_time
        self.balance = new_balance
        self._lifecycle.append(
            (
                "settle",
                ticket.ticket_id,
                tuple(sorted(winners)),
                tuple(sorted(voids)),
            )
        )
        self._settlement_times[ticket.ticket_id] = settlement_time
        _advance_paperbook_causal_history_settle(
            self,
            ticket.ticket_id,
            tuple(sorted(winners)),
            tuple(sorted(voids)),
            settlement_time,
        )
        return ticket

    def _lifecycle_to_json(self) -> list[dict[str, object]]:
        payload: list[dict[str, object]] = []
        for action, ticket_id, winners, voids in self._lifecycle:
            if action == "open":
                payload.append({"action": "open", "ticket_id": ticket_id})
            else:
                payload.append(
                    {
                        "action": "settle",
                        "ticket_id": ticket_id,
                        "winning_quote_keys": list(winners),
                        "void_quote_keys": list(voids),
                        "settled_at": self._settlement_times[ticket_id],
                    }
                )
        return payload

    @_serialize_paperbook_state
    def save(self, path: str | Path) -> None:
        self._require_snapshot_authority_for_economic_mutation(
            verify_bound_head=False,
        )
        # PaperBook and PaperTicket are intentionally mutable during a paper run.
        # Revalidate the complete economic/identity state immediately before any
        # durable replacement so caller/agent mutation cannot persist a snapshot
        # that a trusted fresh load would reject.
        self._validate_loaded_state(self)
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        witness_path = _snapshot_witness_path(destination)
        binding = _snapshot_authority_binding(self)
        if binding is None:
            raise ValueError(
                "PaperBook byte-loaded snapshot lacks independent durable witness authority"
            )
        was_fresh = binding[0] == "FRESH"
        if was_fresh:
            if destination.exists() or witness_path.exists():
                raise ValueError(
                    "fresh PaperBook cannot overwrite an existing snapshot authority; "
                    "load the existing PaperBook first"
                )
            # Bind before PREPARE so even an interrupted first save cannot later
            # retry against a different path or authority root. Generation zero is
            # a virgin pre-publication state, not a durable snapshot generation.
            _bind_snapshot_authority(
                self,
                destination,
                witness_path,
                generation=0,
                snapshot_sha256="",
            )
            expected_generation = 0
            expected_snapshot_sha = ""
        else:
            expected_prefix = (
                "BOUND",
                _snapshot_identity(destination),
                _canonical_path_key(destination),
                _canonical_path_key(witness_path),
            )
            if binding[:4] != expected_prefix:
                raise ValueError(
                    "PaperBook snapshot authority is bound to another path or witness root"
                )
            expected_generation = int(binding[4])
            expected_snapshot_sha = str(binding[5])
        raw = {
            "schema_version": _PAPER_SNAPSHOT_SCHEMA_VERSION,
            "initial_bankroll": str(self.initial_bankroll),
            "balance": str(self.balance),
            "tickets": [
                {
                    "ticket_id": t.ticket_id,
                    "stake": str(t.stake),
                    "placed_at": t.placed_at,
                    "settled_at": t.settled_at,
                    "status": t.status.value,
                    "payout": str(t.payout),
                    "strategy_reason": t.strategy_reason,
                    "provider_source_ids": list(t.provider_source_ids),
                    "provider_accounts": [
                        {"source_id": source_id, "account_id": account_id}
                        for source_id, account_id in t.provider_accounts
                    ],
                    "bankroll_id": t.bankroll_id,
                    "currency": t.currency,
                    "legs": [
                        {
                            "event_id": leg.event_id,
                            "market_id": leg.market_id,
                            "selection_id": leg.selection_id,
                            "locked_odds": str(leg.locked_odds),
                            "sport": leg.sport,
                            "exchange_side": leg.exchange_side,
                            "market_semantics_id": leg.market_semantics_id,
                        }
                        for leg in t.legs
                    ],
                }
                for t in self.tickets.values()
            ],
            "lifecycle": self._lifecycle_to_json(),
        }
        try:
            snapshot_bytes = json.dumps(
                raw,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise ValueError("PaperBook snapshot cannot be serialized canonically") from exc

        # Validate the exact bytes that are about to become durable, not only
        # the live object state observed before serialization. Direct caller
        # mutation does not participate in the per-book state lock, so it can
        # otherwise race the interval between the first validation and raw
        # field collection. Structural replay catches mixed epochs, while the
        # private opening-authority comparison prevents a coherently rewritten
        # opening history from self-baselining when the candidate is decoded.
        candidate = self._decode_snapshot_bytes(snapshot_bytes)
        _require_snapshot_candidate_opening_authority(self, candidate)
        _require_snapshot_candidate_causal_history_authority(self, candidate)
        snapshot_sha = _snapshot_sha256(snapshot_bytes)

        temporary: Path | None = None
        publication_lock: tuple[int, str] | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "wb",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(snapshot_bytes)
                handle.flush()
                os.fsync(handle.fileno())

            # Serialize the full compare -> PREPARE -> replace -> COMMIT protocol.
            # The kernel advisory lock is released automatically on process death,
            # so a later load can still perform the existing crash recovery.
            publication_lock = _acquire_snapshot_publication_lock(witness_path)

            records, committed, pending = _read_snapshot_witnesses(
                destination,
                witness_path=witness_path,
            )
            current_sha = _file_sha256(destination)
            if was_fresh and (records or current_sha is not None):
                raise ValueError(
                    "fresh PaperBook lost virgin first-save authority before publication"
                )

            # Close an interrupted attempt before starting another generation.
            # A failed replace may leave PREPARE ahead of the still-good committed
            # file; ABORT preserves that last good snapshot without pretending
            # the candidate ever became durable.
            if pending is not None:
                generation, pending_sha = pending
                if current_sha == pending_sha:
                    _append_snapshot_witness(
                        destination,
                        event=_PAPER_SNAPSHOT_WITNESS_COMMIT,
                        generation=generation,
                        snapshot_sha256=pending_sha,
                        witness_path=witness_path,
                    )
                elif committed is not None and current_sha == committed[1]:
                    _append_snapshot_witness(
                        destination,
                        event=_PAPER_SNAPSHOT_WITNESS_ABORT,
                        generation=generation,
                        snapshot_sha256=pending_sha,
                        witness_path=witness_path,
                    )
                elif committed is None and current_sha is None:
                    _append_snapshot_witness(
                        destination,
                        event=_PAPER_SNAPSHOT_WITNESS_ABORT,
                        generation=generation,
                        snapshot_sha256=pending_sha,
                        witness_path=witness_path,
                    )
                else:
                    raise ValueError(
                        "PaperBook snapshot witness has an unresolved different PREPARE"
                    )
                records, committed, pending = _read_snapshot_witnesses(
                    destination,
                    witness_path=witness_path,
                )
                if pending is not None:
                    raise ValueError(
                        "PaperBook snapshot witness pending state did not close"
                    )

            # Existing witnessed state must still match both its independent
            # authority and the exact durable generation from which THIS object
            # was loaded/last published. Without this object-level CAS, a stale
            # PaperBook can overwrite a newer valid generation and turn rollback
            # into a new apparently legitimate witness generation.
            current_sha = _file_sha256(destination)
            if expected_generation == 0:
                if committed is not None or current_sha is not None:
                    raise ValueError(
                        "PaperBook snapshot authority is stale; reload current durable snapshot"
                    )
            elif (
                committed
                != (expected_generation, expected_snapshot_sha)
                or current_sha != expected_snapshot_sha
            ):
                raise ValueError(
                    "PaperBook snapshot authority is stale; reload current durable snapshot"
                )
            if committed is None and current_sha is not None:
                raise ValueError(
                    "PaperBook existing snapshot lacks independent durable witness"
                )
            if committed is not None and current_sha != committed[1]:
                raise ValueError(
                    "PaperBook current snapshot differs from independent durable witness"
                )

            # Preserve save() publication semantics even when bytes are unchanged:
            # each call performs a new PREPARE -> replace -> COMMIT attempt.
            generation = 1 if not records else int(records[-1]["generation"]) + 1
            _append_snapshot_witness(
                destination,
                event=_PAPER_SNAPSHOT_WITNESS_PREPARE,
                generation=generation,
                snapshot_sha256=snapshot_sha,
                witness_path=witness_path,
            )
            os.replace(temporary, destination)
            temporary = None
            if os.name != "nt":
                from ._paper_execution_anti_rollback import _sync_authority_directory

                _sync_authority_directory(destination.parent)
            _append_snapshot_witness(
                destination,
                event=_PAPER_SNAPSHOT_WITNESS_COMMIT,
                generation=generation,
                snapshot_sha256=snapshot_sha,
                witness_path=witness_path,
            )
            _advance_snapshot_authority(
                self,
                destination,
                witness_path,
                expected_generation=expected_generation,
                expected_snapshot_sha256=expected_snapshot_sha,
                new_generation=generation,
                new_snapshot_sha256=snapshot_sha,
            )
        finally:
            try:
                if publication_lock is not None:
                    _release_snapshot_publication_lock(publication_lock)
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass

    @staticmethod
    def _require_finite(value: object, label: str) -> None:
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError(f"PaperBook snapshot contains non-finite {label}")

    @staticmethod
    def _require_utf8_string(value: object, label: str) -> str:
        if not isinstance(value, str):
            raise ValueError(f"PaperBook {label} must be a string")
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError(f"PaperBook {label} must be valid UTF-8 text") from exc
        return value

    @classmethod
    def _require_canonical_text(
        cls,
        value: object,
        label: str,
        *,
        forbid_quote_key_delimiter: bool = False,
    ) -> str:
        text = cls._require_utf8_string(value, label)
        if not text or text.strip() != text:
            raise ValueError(f"PaperBook {label} must be a non-empty trimmed string")
        if forbid_quote_key_delimiter and "|" in text:
            raise ValueError(f"PaperBook {label} must not contain quote-key delimiter '|'")
        return text

    @classmethod
    def _validate_ticket_provenance(
        cls,
        provider_source_ids: object,
        provider_accounts: object,
        bankroll_id: object,
        currency: object,
    ) -> tuple[
        tuple[str, ...],
        tuple[tuple[str, str], ...],
        str | None,
        str | None,
    ]:
        if type(provider_source_ids) is not tuple:
            raise ValueError("PaperBook provider_source_ids must be a canonical tuple")
        canonical_sources = tuple(
            cls._require_canonical_text(source_id, "provider_source_id")
            for source_id in provider_source_ids
        )
        if (
            canonical_sources != tuple(sorted(canonical_sources))
            or len(canonical_sources) != len(set(canonical_sources))
        ):
            raise ValueError("PaperBook provider_source_ids must be sorted and unique")

        if type(provider_accounts) is not tuple:
            raise ValueError("PaperBook provider_accounts must be a canonical tuple")
        account_bindings: list[tuple[str, str]] = []
        for binding in provider_accounts:
            if type(binding) is not tuple or len(binding) != 2:
                raise ValueError(
                    "PaperBook provider_accounts must contain (source_id, account_id) tuples"
                )
            source_id, account_id = binding
            account_bindings.append(
                (
                    cls._require_canonical_text(source_id, "provider account source_id"),
                    cls._require_canonical_text(account_id, "provider account_id"),
                )
            )
        canonical_accounts = tuple(account_bindings)
        if (
            canonical_accounts != tuple(sorted(canonical_accounts))
            or len(canonical_accounts) != len(set(canonical_accounts))
        ):
            raise ValueError("PaperBook provider_accounts must be sorted and unique")
        account_sources = tuple(source_id for source_id, _ in canonical_accounts)
        if len(account_sources) != len(set(account_sources)):
            raise ValueError(
                "PaperBook provider_accounts may bind at most one account_id per provider source"
            )
        if canonical_accounts and frozenset(account_sources) != frozenset(canonical_sources):
            raise ValueError(
                "PaperBook provider_accounts must cover provider_source_ids exactly"
            )

        if (bankroll_id is None) != (currency is None):
            raise ValueError(
                "PaperBook bankroll_id and currency provenance must be supplied together"
            )
        if bankroll_id is None:
            return canonical_sources, canonical_accounts, None, None

        canonical_bankroll = cls._require_canonical_text(bankroll_id, "bankroll_id")
        canonical_currency = cls._require_canonical_text(currency, "currency")
        if (
            len(canonical_currency) != 3
            or not canonical_currency.isascii()
            or not canonical_currency.isalpha()
            or canonical_currency != canonical_currency.upper()
        ):
            raise ValueError(
                "PaperBook currency provenance must be a three-letter uppercase ASCII code"
            )
        return canonical_sources, canonical_accounts, canonical_bankroll, canonical_currency

    @classmethod
    def _validate_timestamp(cls, value: object, label: str) -> str:
        message = f"PaperBook {label} must be a non-empty trimmed timezone-aware ISO timestamp"
        try:
            text = cls._require_utf8_string(value, label)
        except ValueError as exc:
            raise ValueError(message) from exc
        if not text or text.strip() != text:
            raise ValueError(message)
        try:
            parse_iso_timestamp(text)
        except ValueError as exc:
            raise ValueError(message) from exc
        return text

    @classmethod
    def _validate_placed_at(cls, value: object, *, snapshot: bool = False) -> str:
        label = "snapshot placed_at" if snapshot else "placed_at"
        return cls._validate_timestamp(value, label)

    @classmethod
    def _validate_settled_at(
        cls,
        value: object,
        placed_at: object,
        *,
        snapshot: bool = False,
    ) -> str:
        label = "snapshot settled_at" if snapshot else "settled_at"
        settled_text = cls._validate_timestamp(value, label)
        placed_text = cls._validate_placed_at(placed_at, snapshot=snapshot)
        if parse_iso_timestamp(settled_text) < parse_iso_timestamp(placed_text):
            raise ValueError("PaperBook settled_at must not precede placed_at")
        return settled_text

    @classmethod
    def _validate_ticket_leg(cls, leg: object, *, ticket_id: str | None = None) -> TicketLeg:
        if type(leg) is not TicketLeg:
            raise ValueError("PaperBook ticket legs must be canonical TicketLeg values")
        suffix = f" for ticket {ticket_id}" if ticket_id is not None else ""
        cls._require_canonical_text(
            leg.event_id,
            f"event_id{suffix}",
            forbid_quote_key_delimiter=True,
        )
        cls._require_canonical_text(
            leg.market_id,
            f"market_id{suffix}",
            forbid_quote_key_delimiter=True,
        )
        cls._require_canonical_text(
            leg.selection_id,
            f"selection_id{suffix}",
            forbid_quote_key_delimiter=True,
        )
        if leg.sport is not None:
            sport = cls._require_canonical_text(
                leg.sport,
                f"sport{suffix}",
                forbid_quote_key_delimiter=True,
            )
            if sport != sport.lower() or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
                for character in sport
            ):
                raise ValueError(
                    "PaperBook ticket sport must be a lowercase canonical sport identity"
                )
        if leg.exchange_side is not None:
            exchange_side = cls._require_canonical_text(
                leg.exchange_side,
                f"exchange_side{suffix}",
                forbid_quote_key_delimiter=True,
            )
            if exchange_side not in {"back", "lay"}:
                raise ValueError(
                    "PaperBook ticket exchange_side must be canonical 'back' or 'lay'"
                )
            if exchange_side == "lay":
                raise ValueError(
                    "PaperBook LAY materialization is unsupported until canonical "
                    "side-aware liability and settlement authority is integrated"
                )
        if leg.market_semantics_id is not None:
            market_semantics_id = cls._require_canonical_text(
                leg.market_semantics_id,
                f"market_semantics_id{suffix}",
                forbid_quote_key_delimiter=True,
            )
            if (
                market_semantics_id != market_semantics_id.lower()
                or any(
                    character
                    not in "abcdefghijklmnopqrstuvwxyz0123456789._:/-"
                    for character in market_semantics_id
                )
                or market_semantics_id in {"unknown", "mixed", "unspecified"}
            ):
                raise ValueError(
                    "PaperBook ticket market_semantics_id must be a non-reserved "
                    "lowercase canonical semantic identity"
                )
        cls._require_finite(leg.locked_odds, f"locked_odds{suffix}")
        if leg.locked_odds <= 1:
            raise ValueError("PaperBook snapshot decimal odds must be greater than 1")
        return leg

    @staticmethod
    def _validate_ticket_opening_economics(ticket: PaperTicket) -> None:
        if (
            ticket.stake != ticket._opening_stake
            or ticket.legs != ticket._opening_legs
        ):
            raise ValueError(
                "PaperBook ticket opening economic identity changed after admission"
            )

    @classmethod
    def _validate_lifecycle_entry(cls, entry: object) -> _LifecycleEntry:
        if type(entry) is not tuple or len(entry) != 4:
            raise ValueError("PaperBook lifecycle entries must be canonical tuples")
        action, ticket_id, winners, voids = entry
        if action not in {"open", "settle"}:
            raise ValueError("PaperBook lifecycle action must be open or settle")
        cls._require_canonical_text(ticket_id, "lifecycle ticket_id")
        if type(winners) is not tuple or type(voids) is not tuple:
            raise ValueError("PaperBook lifecycle settlement keys must be canonical tuples")
        for values, label in ((winners, "winning_quote_keys"), (voids, "void_quote_keys")):
            for value in values:
                cls._require_canonical_text(value, f"lifecycle {label}")
            if values != tuple(sorted(values)) or len(values) != len(set(values)):
                raise ValueError(f"PaperBook lifecycle {label} must be sorted and unique")
        if action == "open" and (winners or voids):
            raise ValueError("PaperBook lifecycle open action cannot contain settlement keys")
        return action, ticket_id, winners, voids

    @classmethod
    def _validate_lifecycle_reachability(cls, book: "PaperBook") -> None:
        if type(book._lifecycle) is not list:
            raise ValueError("PaperBook lifecycle must be a canonical list")
        if type(book._settlement_times) is not dict:
            raise ValueError("PaperBook settlement-time witness must be a canonical mapping")

        replay_balance = book.initial_bankroll
        opened: set[str] = set()
        settled: set[str] = set()
        open_order: list[str] = []

        for raw_entry in book._lifecycle:
            action, ticket_id, winners_raw, voids_raw = cls._validate_lifecycle_entry(
                raw_entry
            )
            ticket = book.tickets.get(ticket_id)
            if ticket is None:
                raise ValueError("PaperBook lifecycle references unknown ticket_id")

            if action == "open":
                if ticket_id in opened:
                    raise ValueError("PaperBook lifecycle opens a ticket more than once")
                try:
                    replay_balance = cls._debit_balance(replay_balance, ticket.stake)
                except ValueError as exc:
                    raise ValueError(
                        f"PaperBook lifecycle stake for ticket {ticket_id} was not affordable"
                    ) from exc
                opened.add(ticket_id)
                open_order.append(ticket_id)
                continue

            if ticket_id not in opened:
                raise ValueError("PaperBook lifecycle settles a ticket before opening it")
            if ticket_id in settled:
                raise ValueError("PaperBook lifecycle settles a ticket more than once")
            if ticket_id not in book._settlement_times:
                raise ValueError(
                    f"PaperBook ticket {ticket_id} settlement is missing timestamp provenance witness"
                )
            settlement_time = book._settlement_times[ticket_id]
            if ticket.settled_at != settlement_time:
                raise ValueError(
                    f"PaperBook ticket {ticket_id} settled_at is inconsistent with lifecycle provenance"
                )
            if settlement_time is not None:
                cls._validate_settled_at(
                    settlement_time,
                    ticket.placed_at,
                    snapshot=True,
                )
            winners = set(winners_raw)
            voids = set(voids_raw)
            status, payout, replay_balance = cls._settlement_result(
                ticket,
                replay_balance,
                winners,
                voids,
            )
            if ticket.status is not status or ticket.payout != payout:
                raise ValueError(
                    f"PaperBook ticket {ticket_id} state is inconsistent with lifecycle settlement witness"
                )
            settled.add(ticket_id)

        if tuple(open_order) != tuple(book.tickets):
            raise ValueError("PaperBook lifecycle open order must match canonical ticket order")
        for ticket_id, ticket in book.tickets.items():
            if ticket_id not in opened:
                raise ValueError("PaperBook lifecycle is missing ticket open action")
            if ticket_id not in settled and ticket.status is not TicketStatus.OPEN:
                raise ValueError(
                    f"PaperBook ticket {ticket_id} settled state is missing lifecycle provenance"
                )
            if ticket_id in settled and ticket.status is TicketStatus.OPEN:
                raise ValueError(
                    f"PaperBook ticket {ticket_id} open state conflicts with lifecycle settlement witness"
                )
        if set(book._settlement_times) != settled:
            raise ValueError(
                "PaperBook settlement-time witness must match settled lifecycle tickets exactly"
            )
        if replay_balance != book.balance:
            raise ValueError(
                "PaperBook snapshot balance is inconsistent with lifecycle-replayed ticket economics"
            )

    @classmethod
    def _validate_loaded_state(
        cls,
        book: "PaperBook",
        *,
        require_private_opening_authority: bool = True,
        require_private_causal_history_authority: bool = True,
    ) -> None:
        cls._require_finite(book.initial_bankroll, "initial_bankroll")
        cls._require_finite(book.balance, "balance")
        if book.initial_bankroll <= 0:
            raise ValueError("PaperBook snapshot initial_bankroll must be positive")
        if book.balance < 0:
            raise ValueError("PaperBook snapshot balance cannot be negative")
        if type(book.tickets) is not dict:
            raise ValueError("PaperBook tickets must be a canonical ticket mapping")

        for ticket_key, ticket in book.tickets.items():
            cls._require_canonical_text(ticket_key, "ticket mapping key")
            if type(ticket) is not PaperTicket:
                raise ValueError("PaperBook tickets must contain canonical PaperTicket values")
            cls._require_canonical_text(ticket.ticket_id, "ticket_id")
            if ticket_key != ticket.ticket_id:
                raise ValueError("PaperBook ticket mapping key must match ticket_id")
            cls._validate_placed_at(ticket.placed_at, snapshot=True)
            if ticket.settled_at is not None:
                cls._validate_settled_at(
                    ticket.settled_at,
                    ticket.placed_at,
                    snapshot=True,
                )
            if ticket.status is TicketStatus.OPEN and ticket.settled_at is not None:
                raise ValueError("PaperBook snapshot open ticket cannot have settled_at")
            cls._require_utf8_string(ticket.strategy_reason, "snapshot strategy_reason")
            cls._validate_ticket_provenance(
                ticket.provider_source_ids,
                ticket.provider_accounts,
                ticket.bankroll_id,
                ticket.currency,
            )
            if type(ticket.status) is not TicketStatus:
                raise ValueError("PaperBook snapshot ticket status must be canonical TicketStatus")
            cls._require_finite(ticket.stake, f"stake for ticket {ticket.ticket_id}")
            cls._require_finite(ticket.payout, f"payout for ticket {ticket.ticket_id}")
            if ticket.stake <= 0:
                raise ValueError("PaperBook snapshot ticket stake must be positive")
            if ticket.payout < 0:
                raise ValueError("PaperBook snapshot ticket payout cannot be negative")
            if type(ticket.legs) is not tuple or not ticket.legs:
                raise ValueError("PaperBook snapshot ticket requires a canonical non-empty leg tuple")
            for leg in ticket.legs:
                cls._validate_ticket_leg(leg, ticket_id=ticket.ticket_id)
            quote_keys = [leg.quote_key for leg in ticket.legs]
            if len(quote_keys) != len(set(quote_keys)):
                raise ValueError("PaperBook snapshot ticket contains duplicate quote_key leg")
            cls._validate_ticket_opening_economics(ticket)
            if require_private_opening_authority:
                _require_ticket_opening_authority(book, ticket)

            if ticket.status in {TicketStatus.OPEN, TicketStatus.LOST} and ticket.payout != 0:
                raise ValueError("PaperBook snapshot open/lost ticket payout must be zero")
            if ticket.status is TicketStatus.VOID and ticket.payout != ticket.stake:
                raise ValueError("PaperBook snapshot void ticket payout must equal stake")
            if ticket.status is TicketStatus.WON and ticket.payout <= ticket.stake:
                raise ValueError("PaperBook snapshot won ticket payout must exceed stake")

        cls._validate_lifecycle_reachability(book)
        if require_private_causal_history_authority:
            _require_paperbook_causal_history_authority(book)

    @classmethod
    def _parse_lifecycle_key_list(cls, value: object, label: str) -> tuple[str, ...]:
        if type(value) is not list:
            raise ValueError(f"PaperBook snapshot lifecycle {label} must be a list")
        for item in value:
            cls._require_canonical_text(item, f"snapshot lifecycle {label}")
        normalized = tuple(value)
        if normalized != tuple(sorted(normalized)) or len(normalized) != len(set(normalized)):
            raise ValueError(
                f"PaperBook snapshot lifecycle {label} must be sorted and unique"
            )
        return normalized

    @classmethod
    def _parse_lifecycle(
        cls,
        value: object,
        schema_version: int,
    ) -> tuple[list[_LifecycleEntry], dict[str, str | None]]:
        if type(value) is not list:
            raise ValueError("PaperBook snapshot lifecycle must be a list")
        entries: list[_LifecycleEntry] = []
        settlement_times: dict[str, str | None] = {}
        for item in value:
            if type(item) is not dict:
                raise ValueError("PaperBook snapshot lifecycle entry must be an object")
            action = item.get("action")
            ticket_id = item.get("ticket_id")
            if action == "open":
                if set(item) != {"action", "ticket_id"}:
                    raise ValueError("PaperBook snapshot open lifecycle entry has unexpected fields")
                entry: _LifecycleEntry = ("open", ticket_id, (), ())
            elif action == "settle":
                expected_fields = {
                    "action",
                    "ticket_id",
                    "winning_quote_keys",
                    "void_quote_keys",
                }
                if schema_version >= 5:
                    expected_fields.add("settled_at")
                if set(item) != expected_fields:
                    raise ValueError("PaperBook snapshot settle lifecycle entry has unexpected fields")
                settled_at = item.get("settled_at") if schema_version >= 5 else None
                if settled_at is not None:
                    cls._validate_timestamp(
                        settled_at,
                        "snapshot lifecycle settled_at",
                    )
                entry = (
                    "settle",
                    ticket_id,
                    cls._parse_lifecycle_key_list(
                        item["winning_quote_keys"], "winning_quote_keys"
                    ),
                    cls._parse_lifecycle_key_list(
                        item["void_quote_keys"], "void_quote_keys"
                    ),
                )
            else:
                raise ValueError("PaperBook snapshot lifecycle action must be open or settle")
            canonical_entry = cls._validate_lifecycle_entry(entry)
            entries.append(canonical_entry)
            if action == "settle":
                settlement_times[canonical_entry[1]] = settled_at
        return entries, settlement_times

    @classmethod
    def _parse_snapshot_decimal(cls, value: object, label: str) -> Decimal:
        if not isinstance(value, str) or not value or value.strip() != value:
            raise ValueError(
                f"PaperBook snapshot {label} must be a non-empty trimmed decimal string"
            )
        try:
            parsed = Decimal(value)
        except DecimalException as exc:
            raise ValueError(f"PaperBook snapshot {label} is not a valid Decimal string") from exc
        cls._require_finite(parsed, label)
        return parsed

    @staticmethod
    def _required_snapshot_field(
        mapping: dict[str, object], field: str, context: str
    ) -> object:
        if field not in mapping:
            raise ValueError(
                f"PaperBook snapshot {context} is missing required field: {field}"
            )
        return mapping[field]

    @classmethod
    def _parse_snapshot_legs(
        cls,
        value: object,
        ticket_id: str,
        *,
        schema_version: int | None,
    ) -> tuple[TicketLeg, ...]:
        if type(value) is not list:
            raise ValueError(f"PaperBook snapshot legs for ticket {ticket_id} must be a list")
        legs: list[TicketLeg] = []
        for index, raw_leg in enumerate(value):
            if type(raw_leg) is not dict:
                raise ValueError(
                    f"PaperBook snapshot leg {index} for ticket {ticket_id} must be an object"
                )
            sport = (
                cls._required_snapshot_field(raw_leg, "sport", "ticket leg")
                if schema_version is not None and schema_version >= 6
                else None
            )
            exchange_side = (
                cls._required_snapshot_field(raw_leg, "exchange_side", "ticket leg")
                if schema_version is not None and schema_version >= 7
                else None
            )
            market_semantics_id = (
                cls._required_snapshot_field(
                    raw_leg,
                    "market_semantics_id",
                    "ticket leg",
                )
                if schema_version is not None and schema_version >= 8
                else None
            )
            if sport is not None:
                cls._require_canonical_text(
                    sport,
                    f"sport for ticket {ticket_id}",
                    forbid_quote_key_delimiter=True,
                )
            legs.append(
                TicketLeg(
                    cls._required_snapshot_field(raw_leg, "event_id", "ticket leg"),
                    cls._required_snapshot_field(raw_leg, "market_id", "ticket leg"),
                    cls._required_snapshot_field(raw_leg, "selection_id", "ticket leg"),
                    cls._parse_snapshot_decimal(
                        cls._required_snapshot_field(raw_leg, "locked_odds", "ticket leg"),
                        f"locked_odds for ticket {ticket_id}",
                    ),
                    sport=sport,
                    exchange_side=exchange_side,
                    market_semantics_id=market_semantics_id,
                )
            )
        return tuple(legs)

    @classmethod
    def _parse_snapshot_provider_accounts(
        cls, value: object, ticket_id: str
    ) -> tuple[tuple[str, str], ...]:
        if type(value) is not list:
            raise ValueError(
                f"PaperBook snapshot provider_accounts for ticket {ticket_id} must be a list"
            )
        bindings: list[tuple[str, str]] = []
        for index, raw_binding in enumerate(value):
            if type(raw_binding) is not dict or set(raw_binding) != {"source_id", "account_id"}:
                raise ValueError(
                    "PaperBook snapshot provider account binding "
                    f"{index} for ticket {ticket_id} must contain exactly source_id and account_id"
                )
            bindings.append(
                (
                    cls._require_canonical_text(
                        raw_binding["source_id"], "snapshot provider account source_id"
                    ),
                    cls._require_canonical_text(
                        raw_binding["account_id"], "snapshot provider account_id"
                    ),
                )
            )
        return tuple(bindings)

    @staticmethod
    def _parse_snapshot_status(value: object, ticket_id: str) -> TicketStatus:
        if not isinstance(value, str):
            raise ValueError(
                f"PaperBook snapshot status for ticket {ticket_id} must be a string"
            )
        try:
            return TicketStatus(value)
        except ValueError as exc:
            raise ValueError(
                f"PaperBook snapshot status for ticket {ticket_id} is invalid"
            ) from exc

    @classmethod
    def _from_raw_snapshot(cls, raw: object) -> "PaperBook":
        if type(raw) is not dict:
            raise ValueError("PaperBook snapshot root must be an object")
        schema_version = raw.get("schema_version", _SCHEMA_MISSING)
        is_legacy = schema_version is _SCHEMA_MISSING
        if not is_legacy and (
            type(schema_version) is not int
            or schema_version not in _SUPPORTED_PAPER_SNAPSHOT_SCHEMA_VERSIONS
        ):
            raise ValueError("unsupported PaperBook snapshot schema_version")

        initial_bankroll = cls._parse_snapshot_decimal(
            cls._required_snapshot_field(raw, "initial_bankroll", "root"),
            "initial_bankroll",
        )
        book = cls(initial_bankroll)
        book._snapshot_schema_version = None if is_legacy else schema_version
        book.balance = cls._parse_snapshot_decimal(
            cls._required_snapshot_field(raw, "balance", "root"),
            "balance",
        )
        tickets_raw = cls._required_snapshot_field(raw, "tickets", "root")
        if type(tickets_raw) is not list:
            raise ValueError("PaperBook snapshot tickets must be a list")
        seen_ticket_ids: set[str] = set()
        for item in tickets_raw:
            if type(item) is not dict:
                raise ValueError("PaperBook snapshot ticket must be an object")
            ticket_id = cls._require_canonical_text(
                cls._required_snapshot_field(item, "ticket_id", "ticket"),
                "snapshot ticket_id",
            )
            if ticket_id in seen_ticket_ids:
                raise ValueError("PaperBook snapshot contains duplicate ticket_id")
            seen_ticket_ids.add(ticket_id)
            if schema_version in {3, 4, 5, 6, 7, 8}:
                provider_source_ids_raw = cls._required_snapshot_field(
                    item, "provider_source_ids", f"ticket {ticket_id}"
                )
                if type(provider_source_ids_raw) is not list:
                    raise ValueError(
                        f"PaperBook snapshot provider_source_ids for ticket {ticket_id} must be a list"
                    )
                provider_source_ids = tuple(provider_source_ids_raw)
                provider_accounts = (
                    cls._parse_snapshot_provider_accounts(
                        cls._required_snapshot_field(
                            item, "provider_accounts", f"ticket {ticket_id}"
                        ),
                        ticket_id,
                    )
                    if schema_version in {4, 5, 6, 7, 8}
                    else ()
                )
                bankroll_id = cls._required_snapshot_field(
                    item, "bankroll_id", f"ticket {ticket_id}"
                )
                currency = cls._required_snapshot_field(
                    item, "currency", f"ticket {ticket_id}"
                )
            else:
                provider_source_ids = ()
                provider_accounts = ()
                bankroll_id = None
                currency = None

            ticket = PaperTicket(
                ticket_id=ticket_id,
                stake=cls._parse_snapshot_decimal(
                    cls._required_snapshot_field(item, "stake", f"ticket {ticket_id}"),
                    f"stake for ticket {ticket_id}",
                ),
                legs=cls._parse_snapshot_legs(
                    cls._required_snapshot_field(item, "legs", f"ticket {ticket_id}"),
                    ticket_id,
                    schema_version=None if is_legacy else schema_version,
                ),
                placed_at=cls._required_snapshot_field(
                    item, "placed_at", f"ticket {ticket_id}"
                ),
                settled_at=(
                    cls._required_snapshot_field(
                        item, "settled_at", f"ticket {ticket_id}"
                    )
                    if schema_version in {5, 6, 7, 8}
                    else None
                ),
                status=cls._parse_snapshot_status(
                    cls._required_snapshot_field(item, "status", f"ticket {ticket_id}"),
                    ticket_id,
                ),
                payout=cls._parse_snapshot_decimal(
                    cls._required_snapshot_field(item, "payout", f"ticket {ticket_id}"),
                    f"payout for ticket {ticket_id}",
                ),
                strategy_reason=item.get("strategy_reason", ""),
                provider_source_ids=provider_source_ids,
                provider_accounts=provider_accounts,
                bankroll_id=bankroll_id,
                currency=currency,
            )
            book.tickets[ticket.ticket_id] = ticket

        if is_legacy:
            # Legacy snapshots did not persist settlement chronology or resolution
            # witnesses. Open-only books are still exactly replayable from ticket
            # insertion order. Settled legacy books fail closed later in lifecycle
            # validation after all older structural/status invariants have run.
            book._lifecycle = [
                ("open", ticket_id, (), ())
                for ticket_id in book.tickets
            ]
            book._settlement_times = {}
        else:
            if "lifecycle" not in raw:
                raise ValueError(
                    f"PaperBook snapshot schema {schema_version} requires lifecycle provenance"
                )
            book._lifecycle, book._settlement_times = cls._parse_lifecycle(
                raw["lifecycle"],
                schema_version,
            )

        cls._validate_loaded_state(
            book,
            require_private_opening_authority=False,
            require_private_causal_history_authority=False,
        )
        _revoke_snapshot_authority(book)
        return book

    @classmethod
    def _decode_snapshot_bytes(cls, payload: bytes) -> "PaperBook":
        if not isinstance(payload, bytes):
            raise TypeError("PaperBook snapshot payload must be bytes")
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("PaperBook snapshot must be valid UTF-8") from exc
        try:
            raw = json.loads(
                text,
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=_reject_nonfinite_json_constant,
            )
        except RecursionError as exc:
            raise ValueError("PaperBook snapshot JSON nesting is too deep") from exc
        return cls._from_raw_snapshot(raw)

    @classmethod
    def load_bytes(cls, payload: bytes) -> "PaperBook":
        # Byte ingestion remains available for structural/forensic parsing, but
        # without a path-bound external witness it cannot authorize economic
        # mutation, persistence, or settlement.
        return cls._decode_snapshot_bytes(payload)

    @classmethod
    def load(cls, path: str | Path) -> "PaperBook":
        source = Path(path)
        witness_path = _snapshot_witness_path(source)
        publication_lock = _acquire_snapshot_publication_lock(witness_path)
        try:
            payload = source.read_bytes()
            book = cls._decode_snapshot_bytes(payload)
            if not (
                book.tickets
                or book._snapshot_schema_version == _PAPER_SNAPSHOT_SCHEMA_VERSION
            ):
                # Empty pre-witness legacy snapshots are structural/forensic input only.
                # Skipping verification must never promote them into economic authority.
                return book
            try:
                verified_head = _verify_snapshot_witness(
                    source,
                    payload,
                    witness_path=witness_path,
                )
            except ValueError as exc:
                # Pre-witness legacy schemas remain available for forensic/read-only
                # inspection, but cannot be promoted to trusted economics by save(),
                # open_ticket(), or settle(). Current schema-8 snapshots must have
                # the independent authority because otherwise caller-edited current
                # bytes could be silently re-baselined.
                if (
                    book._snapshot_schema_version is None
                    or book._snapshot_schema_version < _PAPER_SNAPSHOT_SCHEMA_VERSION
                ) and "missing independent durable opening witness" in str(exc):
                    return book
                raise
            _install_verified_ticket_opening_authority(book)
            _install_verified_paperbook_causal_history_authority(book)
            cls._validate_loaded_state(book)
            _bind_snapshot_authority(
                book,
                source,
                witness_path,
                generation=verified_head[0],
                snapshot_sha256=verified_head[1],
            )
            return book
        finally:
            _release_snapshot_publication_lock(publication_lock)
