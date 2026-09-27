"""Bind PaperBook persistence to the existing independent PAPER authority root.

PaperBook JSON is a mutable workspace snapshot. Structural validation proves only
internal coherence; positive restart authority therefore comes from a separate,
append-only PREPARE/COMMIT/ABORT witness stored under the already-canonical PAPER
execution anti-rollback root. This module adapts the earlier same-lineage witness
protocol to the current PaperBook implementation without replacing its parser,
serializer, opening-economics registry, causal-history registry, or settlement logic.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from types import FunctionType
from weakref import WeakKeyDictionary

from . import paper as _paper
from ._paper_execution_anti_rollback import (
    _authority_root as _paper_authority_root,
    _sync_authority_directory as _sync_authority_directory,
)


_PAPER_BOOK = _paper.PaperBook
_LOAD_BYTES = vars(_PAPER_BOOK)["load_bytes"].__func__
_ORIGINAL_SAVE = vars(_PAPER_BOOK)["save"]
_REGISTER_OPENING = _paper._register_ticket_opening_authority_book
_REGISTER_CAUSAL = _paper._register_paperbook_causal_history_authority_book
_INSTALL_OPENING = _paper._install_validated_ticket_opening_authority
_INSTALL_CAUSAL = _paper._install_validated_paperbook_causal_history_authority
_PATH = Path
_OS_REPLACE = os.replace

_WITNESS_SCHEMA_VERSION = 1
_WITNESS_SUFFIX = ".paper-book-snapshot-witness.jsonl"
_PREPARE = "PREPARE"
_COMMIT = "COMMIT"
_ABORT = "ABORT"
_WITNESS_LOCK = threading.RLock()
_BOOK_BINDINGS: WeakKeyDictionary[object, tuple[str, str]] = WeakKeyDictionary()
_BINDING_LOCK = threading.RLock()
_EMPTY_CLOSURE_CELL = object()


def _closure_contents(closure: tuple[object, ...] | None) -> tuple[object, ...] | None:
    if closure is None:
        return None
    contents: list[object] = []
    for cell in closure:
        try:
            contents.append(cell.cell_contents)
        except ValueError:
            contents.append(_EMPTY_CLOSURE_CELL)
    return tuple(contents)


def _global_bindings(delegate: FunctionType) -> tuple[tuple[str, object], ...]:
    mapping = delegate.__globals__
    return tuple(
        (name, mapping[name])
        for name in delegate.__code__.co_names
        if name in mapping
    )


def _capture_delegate_witness(delegate: object, label: str) -> tuple[object, ...]:
    if type(delegate) is not FunctionType:
        raise RuntimeError(f"PaperBook {label} delegate is not a canonical Python function")
    kwdefaults = delegate.__kwdefaults__
    closure = delegate.__closure__
    return (
        delegate.__code__,
        delegate.__globals__,
        delegate.__name__,
        delegate.__defaults__,
        None if kwdefaults is None else dict(kwdefaults),
        closure,
        _closure_contents(closure),
        _global_bindings(delegate),
    )


def _require_delegate_witness(
    delegate: object,
    witness: tuple[object, ...],
    label: str,
) -> None:
    if type(delegate) is not FunctionType:
        raise ValueError(f"PaperBook {label} executable authority changed")
    (
        code,
        globals_mapping,
        _name,
        defaults,
        kwdefaults,
        closure,
        closure_contents,
        global_bindings,
    ) = witness
    if (
        delegate.__code__ is not code
        or delegate.__globals__ is not globals_mapping
        or delegate.__defaults__ != defaults
        or delegate.__kwdefaults__ != kwdefaults
        or delegate.__closure__ != closure
    ):
        raise ValueError(f"PaperBook {label} executable authority changed")
    current_closure_contents = _closure_contents(delegate.__closure__)
    if closure_contents is None:
        if current_closure_contents is not None:
            raise ValueError(f"PaperBook {label} closure authority changed")
    elif (
        current_closure_contents is None
        or len(current_closure_contents) != len(closure_contents)
        or any(
            current is not expected
            for current, expected in zip(current_closure_contents, closure_contents)
        )
    ):
        raise ValueError(f"PaperBook {label} closure authority changed")
    for name, expected in global_bindings:
        if globals_mapping.get(name, _EMPTY_CLOSURE_CELL) is not expected:
            raise ValueError(f"PaperBook {label} global authority changed: {name}")


def _fresh_cell(value: object):
    def capture():
        return value

    closure = capture.__closure__
    if closure is None:
        raise RuntimeError("PaperBook delegate closure capture failed")
    return closure[0]


def _call_witnessed_delegate(
    delegate: object,
    witness: tuple[object, ...],
    label: str,
    *args: object,
):
    """Execute the exact captured implementation, not mutable delegate state.

    The call uses the captured code, captured global bindings and fresh closure cells
    containing the captured authority objects. Persistent mutation of the original
    delegate is checked both before and after execution; in-flight mutation therefore
    cannot retarget this authority-bearing invocation through the live function object,
    its private registry cells, or a rebound module-global helper.
    """

    _require_delegate_witness(delegate, witness, label)
    (
        code,
        globals_mapping,
        name,
        defaults,
        kwdefaults,
        _closure,
        closure_contents,
        global_bindings,
    ) = witness
    trusted_globals = dict(globals_mapping)
    for global_name, expected in global_bindings:
        trusted_globals[global_name] = expected
    trusted_closure = (
        None
        if closure_contents is None
        else tuple(_fresh_cell(value) for value in closure_contents)
    )
    trusted = FunctionType(
        code,
        trusted_globals,
        name=name,
        argdefs=defaults,
        closure=trusted_closure,
    )
    if kwdefaults is not None:
        trusted.__kwdefaults__ = dict(kwdefaults)
    result = trusted(*args)
    _require_delegate_witness(delegate, witness, label)
    return result


_LOAD_BYTES_WITNESS = _capture_delegate_witness(_LOAD_BYTES, "canonical load_bytes")
_ORIGINAL_SAVE_WITNESS = _capture_delegate_witness(_ORIGINAL_SAVE, "canonical save")
_REGISTER_OPENING_WITNESS = _capture_delegate_witness(
    _REGISTER_OPENING, "opening-authority registration"
)
_REGISTER_CAUSAL_WITNESS = _capture_delegate_witness(
    _REGISTER_CAUSAL, "causal-authority registration"
)
_INSTALL_OPENING_WITNESS = _capture_delegate_witness(
    _INSTALL_OPENING, "opening-authority installation"
)
_INSTALL_CAUSAL_WITNESS = _capture_delegate_witness(
    _INSTALL_CAUSAL, "causal-authority installation"
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _snapshot_identity(path: Path) -> str:
    normalized = os.path.normcase(os.path.abspath(os.fspath(path)))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _witness_path(snapshot_path: Path) -> Path:
    identity = _snapshot_identity(snapshot_path)
    return _paper_authority_root(snapshot_path) / f"{identity}{_WITNESS_SUFFIX}"


def _binding(snapshot_path: Path) -> tuple[str, str]:
    witness = _witness_path(snapshot_path)
    try:
        resolved = witness.resolve(strict=False)
    except OSError as exc:
        raise ValueError("cannot resolve PaperBook independent witness path") from exc
    return _snapshot_identity(snapshot_path), os.fspath(resolved)


def _bind_book(book: object, snapshot_path: Path) -> None:
    target = _binding(snapshot_path)
    with _BINDING_LOCK:
        existing = _BOOK_BINDINGS.get(book)
        if existing is not None and existing != target:
            raise ValueError(
                "PaperBook durable authority is already bound to another path or authority root"
            )
        _BOOK_BINDINGS[book] = target


def _require_bound_book(book: object, snapshot_path: Path) -> None:
    target = _binding(snapshot_path)
    with _BINDING_LOCK:
        if _BOOK_BINDINGS.get(book) != target:
            raise ValueError(
                "existing PaperBook snapshot lineage requires verified path-bound authority"
            )


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"PaperBook snapshot witness contains duplicate JSON key: {key}")
        result[key] = value
    return result


def _digest_record(payload: dict[str, object]) -> str:
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


def _require_sha256(value: object, label: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"PaperBook {label} must be lowercase SHA-256 hex")
    return value


def _read_witnesses(
    snapshot_path: Path,
) -> tuple[list[dict[str, object]], tuple[int, str] | None, tuple[int, str] | None]:
    witness_path = _witness_path(snapshot_path)
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
            record = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise ValueError("PaperBook snapshot witness is unreadable") from exc
        if type(record) is not dict or set(record) != expected_keys:
            raise ValueError("PaperBook snapshot witness schema is invalid")
        if record["witness_schema_version"] != _WITNESS_SCHEMA_VERSION:
            raise ValueError("unsupported PaperBook snapshot witness schema")
        if record["sequence"] != sequence:
            raise ValueError("PaperBook snapshot witness sequence is not contiguous")
        if record["snapshot_identity"] != expected_identity:
            raise ValueError("PaperBook snapshot witness belongs to another path")
        if record["snapshot_name"] != snapshot_path.name:
            raise ValueError("PaperBook snapshot witness belongs to another file")
        snapshot_sha = _require_sha256(
            record["snapshot_sha256"], "snapshot witness snapshot_sha256"
        )
        if record["previous_witness_sha256"] != previous_witness_sha256:
            raise ValueError("PaperBook snapshot witness predecessor mismatch")
        body = {key: record[key] for key in expected_keys if key != "witness_sha256"}
        witness_sha = _require_sha256(
            record["witness_sha256"], "snapshot witness witness_sha256"
        )
        if witness_sha != _digest_record(body):
            raise ValueError("PaperBook snapshot witness digest mismatch")

        generation = record["generation"]
        if type(generation) is not int or generation <= 0:
            raise ValueError("PaperBook snapshot witness generation is invalid")
        event = record["event"]
        if event == _PREPARE:
            if pending is not None or generation != last_generation + 1:
                raise ValueError("PaperBook snapshot witness PREPARE order is invalid")
            pending = (generation, snapshot_sha)
            last_generation = generation
        elif event in {_COMMIT, _ABORT}:
            if pending != (generation, snapshot_sha):
                raise ValueError(
                    f"PaperBook snapshot witness {event} has no matching PREPARE"
                )
            if event == _COMMIT:
                committed = pending
            pending = None
        else:
            raise ValueError("PaperBook snapshot witness event is invalid")

        records.append(record)
        previous_witness_sha256 = witness_sha

    return records, committed, pending


def _append_witness(
    snapshot_path: Path,
    *,
    event: str,
    generation: int,
    snapshot_sha256: str,
) -> None:
    records, _, _ = _read_witnesses(snapshot_path)
    previous = None if not records else str(records[-1]["witness_sha256"])
    body: dict[str, object] = {
        "witness_schema_version": _WITNESS_SCHEMA_VERSION,
        "sequence": len(records) + 1,
        "generation": generation,
        "event": event,
        "snapshot_identity": _snapshot_identity(snapshot_path),
        "snapshot_name": snapshot_path.name,
        "snapshot_sha256": _require_sha256(
            snapshot_sha256, "snapshot witness snapshot_sha256"
        ),
        "previous_witness_sha256": previous,
    }
    record = {**body, "witness_sha256": _digest_record(body)}
    witness_path = _witness_path(snapshot_path)
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
            _sync_authority_directory(witness_path.parent)
    except OSError as exc:
        raise ValueError("PaperBook snapshot witness durability barrier failed") from exc


def _file_sha256(path: Path) -> str | None:
    try:
        return _sha256(path.read_bytes())
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError("cannot read PaperBook snapshot for witness verification") from exc


def _recover_pending(
    snapshot_path: Path,
    *,
    current_sha256: str | None,
) -> tuple[list[dict[str, object]], tuple[int, str] | None]:
    records, committed, pending = _read_witnesses(snapshot_path)
    if pending is None:
        return records, committed

    generation, pending_sha = pending
    if current_sha256 == pending_sha:
        _append_witness(
            snapshot_path,
            event=_COMMIT,
            generation=generation,
            snapshot_sha256=pending_sha,
        )
    elif committed is not None and current_sha256 == committed[1]:
        _append_witness(
            snapshot_path,
            event=_ABORT,
            generation=generation,
            snapshot_sha256=pending_sha,
        )
    elif committed is None and current_sha256 is None:
        _append_witness(
            snapshot_path,
            event=_ABORT,
            generation=generation,
            snapshot_sha256=pending_sha,
        )
    else:
        raise ValueError(
            "PaperBook snapshot witness has an incomplete generation for different bytes"
        )

    records_after, committed_after, pending_after = _read_witnesses(snapshot_path)
    if pending_after is not None:
        raise ValueError("PaperBook snapshot witness recovery did not close pending state")
    return records_after, committed_after


def _verify_snapshot_witness(snapshot_path: Path, payload: bytes) -> None:
    snapshot_sha = _sha256(payload)
    _, committed = _recover_pending(
        snapshot_path,
        current_sha256=snapshot_sha,
    )
    if committed is None:
        raise ValueError(
            "PaperBook snapshot is missing independent durable opening witness"
        )
    if committed[1] != snapshot_sha:
        raise ValueError(
            "PaperBook snapshot bytes do not match independent durable opening witness"
        )


def _trusted_path_load(cls, path: str | Path):
    """Load only bytes admitted by the independent durable snapshot witness."""

    if cls is not _PAPER_BOOK:
        raise TypeError("PaperBook.load requires the canonical PaperBook class")
    source = _PATH(path)
    with _WITNESS_LOCK:
        payload = source.read_bytes()
        _verify_snapshot_witness(source, payload)
        book = _call_witnessed_delegate(
            _LOAD_BYTES,
            _LOAD_BYTES_WITNESS,
            "canonical load_bytes",
            cls,
            payload,
        )
        # load_bytes intentionally revokes both registries. Re-register only after
        # the independent witness has authenticated the exact immutable byte image.
        _call_witnessed_delegate(
            _REGISTER_OPENING,
            _REGISTER_OPENING_WITNESS,
            "opening-authority registration",
            book,
        )
        _call_witnessed_delegate(
            _REGISTER_CAUSAL,
            _REGISTER_CAUSAL_WITNESS,
            "causal-authority registration",
            book,
        )
        _call_witnessed_delegate(
            _INSTALL_OPENING,
            _INSTALL_OPENING_WITNESS,
            "opening-authority installation",
            book,
        )
        _call_witnessed_delegate(
            _INSTALL_CAUSAL,
            _INSTALL_CAUSAL_WITNESS,
            "causal-authority installation",
            book,
        )
        _bind_book(book, source)
        return book


def _trusted_save(self, path: str | Path) -> None:
    """Publish a PaperBook snapshot and its independent crash-recoverable witness."""

    if type(self) is not _PAPER_BOOK:
        raise TypeError("PaperBook.save requires the canonical PaperBook class")
    destination = _PATH(path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    with _WITNESS_LOCK:
        current_sha = _file_sha256(destination)
        records, committed = _recover_pending(
            destination,
            current_sha256=current_sha,
        )
        current_sha = _file_sha256(destination)

        if committed is not None and current_sha != committed[1]:
            raise ValueError(
                "PaperBook current snapshot differs from independent durable witness"
            )
        if committed is None and current_sha is not None:
            raise ValueError(
                "existing PaperBook snapshot lacks independent durable authority"
            )

        temporary: Path | None = None
        try:
            fd, temporary_name = tempfile.mkstemp(
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".authority-stage",
            )
            os.close(fd)
            temporary = _PATH(temporary_name)
            temporary.unlink()

            # Reuse the current canonical serializer and all of its private opening
            # / causal candidate validation; only the publication boundary is new.
            _call_witnessed_delegate(
                _ORIGINAL_SAVE,
                _ORIGINAL_SAVE_WITNESS,
                "canonical save",
                self,
                temporary,
            )
            candidate_sha = _file_sha256(temporary)
            if candidate_sha is None:
                raise ValueError("PaperBook canonical serializer produced no snapshot")

            if committed is not None:
                try:
                    _require_bound_book(self, destination)
                except ValueError:
                    # Canonical product restart may reconstruct an independently
                    # authoritative in-memory book, compare it to the durable copy,
                    # then continue using that object. Exact canonical-byte equality
                    # is the only safe generic rebind: structural load_bytes objects
                    # cannot reach this point because _ORIGINAL_SAVE rejects them.
                    if candidate_sha != current_sha:
                        raise ValueError(
                            "existing PaperBook snapshot lineage requires verified path-bound authority"
                        )
                    _bind_book(self, destination)

            # Re-read the authority immediately before PREPARE so in-process
            # concurrent publication cannot silently fork the witness generation.
            records_now, committed_now, pending_now = _read_witnesses(destination)
            if pending_now is not None:
                raise ValueError("PaperBook snapshot witness changed during save")
            if committed_now != committed or len(records_now) != len(records):
                raise ValueError("PaperBook snapshot witness changed during save")
            if _file_sha256(destination) != current_sha:
                raise ValueError("PaperBook snapshot changed during save")

            generation = 1 if not records_now else int(records_now[-1]["generation"]) + 1
            # Bind only once the canonical candidate exists and the target lineage is
            # stable. A root/path change on later saves is then mechanically rejected.
            _bind_book(self, destination)
            _append_witness(
                destination,
                event=_PREPARE,
                generation=generation,
                snapshot_sha256=candidate_sha,
            )
            _OS_REPLACE(temporary, destination)
            temporary = None
            if os.name != "nt":
                _sync_authority_directory(destination.parent)
            _append_witness(
                destination,
                event=_COMMIT,
                generation=generation,
                snapshot_sha256=candidate_sha,
            )
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass


_trusted_path_load.__name__ = "load"
_trusted_path_load.__qualname__ = "PaperBook.load"
_trusted_save.__name__ = "save"
_trusted_save.__qualname__ = "PaperBook.save"

# Preserve one canonical parser/serializer. The guard replaces only path publication
# and positive restart admission, both composed with the existing authority root.
_PAPER_BOOK.load = classmethod(_trusted_path_load)
_PAPER_BOOK.save = _trusted_save
