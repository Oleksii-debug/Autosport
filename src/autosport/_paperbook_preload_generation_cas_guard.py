"""Restore PaperBook generation-CAS and cross-process publication serialization.

This is a composition extension of the existing independent PREPARE/COMMIT/ABORT
PaperBook witness authority.  It does not add another parser, store, journal, root, or
snapshot format.  It restores two invariants that must survive later hardening:

* a path-authorized PaperBook is bound to the exact committed generation and snapshot
  digest that it loaded or last published, so a stale object cannot mint a newer valid
  generation over intervening durable state; and
* compare/recovery/publication is serialized across processes with an advisory lock in
  the same independent authority root, while retaining the existing in-process guard.

The extension is installed before the module-member freeze and load/save graph freeze,
so those existing guards seal this augmented authority rather than a parallel runtime.
"""

from __future__ import annotations

import os
import threading
from types import FunctionType

from . import _paperbook_preload_authority_guard as _guard
from . import paper as _paper


_STALE_ERROR = "PaperBook snapshot authority is stale; reload current durable snapshot"
_LOCKED_ERROR = "PaperBook snapshot publication lock is held by another writer"


def _clone_into_guard(function: FunctionType) -> FunctionType:
    clone = FunctionType(
        function.__code__,
        _guard.__dict__,
        name=function.__name__,
        argdefs=function.__defaults__,
        closure=function.__closure__,
    )
    if function.__kwdefaults__ is not None:
        clone.__kwdefaults__ = dict(function.__kwdefaults__)
    clone.__annotations__ = dict(function.__annotations__)
    clone.__doc__ = function.__doc__
    clone.__qualname__ = function.__qualname__
    return clone


def _canonical_lock_path_key(path):
    return _LOCK_NORMCASE(_LOCK_ABSPATH(_LOCK_FSPATH(path)))


def _snapshot_publication_lock_path(witness_path):
    return witness_path.with_name(witness_path.name + ".writer.lock")


def _acquire_snapshot_publication_lock(witness_path):
    lock_path = _snapshot_publication_lock_path(witness_path)
    lock_key = _canonical_lock_path_key(lock_path)
    with _SNAPSHOT_PUBLICATION_PROCESS_GUARD:
        if lock_key in _SNAPSHOT_PUBLICATION_PROCESS_LOCKS:
            raise ValueError(_SNAPSHOT_PUBLICATION_LOCKED_ERROR)
        _SNAPSHOT_PUBLICATION_PROCESS_LOCKS.add(lock_key)

    fd = None
    try:
        try:
            fd = _LOCK_OS_OPEN(lock_path, _LOCK_OPEN_FLAGS, 0o600)
        except OSError as exc:
            raise ValueError("cannot open PaperBook snapshot publication lock") from exc

        if _LOCK_PLATFORM == "nt":
            if _LOCK_OS_FSTAT(fd).st_size == 0:
                _LOCK_OS_WRITE(fd, b"0")
                _LOCK_OS_FSYNC(fd)
            _LOCK_OS_LSEEK(fd, 0, _LOCK_SEEK_SET)
            try:
                _LOCK_PLATFORM_CALL(fd, _LOCK_PLATFORM_ACQUIRE, 1)
            except OSError as exc:
                raise ValueError(_SNAPSHOT_PUBLICATION_LOCKED_ERROR) from exc
        else:
            try:
                _LOCK_PLATFORM_CALL(fd, _LOCK_PLATFORM_ACQUIRE)
            except OSError as exc:
                raise ValueError(_SNAPSHOT_PUBLICATION_LOCKED_ERROR) from exc
        return fd, lock_key
    except BaseException:
        if fd is not None:
            _LOCK_OS_CLOSE(fd)
        with _SNAPSHOT_PUBLICATION_PROCESS_GUARD:
            _SNAPSHOT_PUBLICATION_PROCESS_LOCKS.discard(lock_key)
        raise


def _release_snapshot_publication_lock(lock):
    fd, lock_key = lock
    try:
        try:
            if _LOCK_PLATFORM == "nt":
                _LOCK_OS_LSEEK(fd, 0, _LOCK_SEEK_SET)
                _LOCK_PLATFORM_CALL(fd, _LOCK_PLATFORM_RELEASE, 1)
            else:
                _LOCK_PLATFORM_CALL(fd, _LOCK_PLATFORM_RELEASE)
        except OSError as exc:
            raise ValueError("cannot release PaperBook snapshot publication lock") from exc
        finally:
            _LOCK_OS_CLOSE(fd)
    finally:
        with _SNAPSHOT_PUBLICATION_PROCESS_GUARD:
            _SNAPSHOT_PUBLICATION_PROCESS_LOCKS.discard(lock_key)


def _binding(snapshot_path):
    witness = _witness_path(snapshot_path)
    try:
        resolved_witness = witness.resolve(strict=False)
        resolved_snapshot = snapshot_path.resolve(strict=False)
    except OSError as exc:
        raise ValueError("cannot resolve PaperBook independent witness path") from exc
    return (
        _snapshot_identity(snapshot_path),
        _LOCK_NORMCASE(_LOCK_ABSPATH(_LOCK_FSPATH(resolved_snapshot))),
        _LOCK_NORMCASE(_LOCK_ABSPATH(_LOCK_FSPATH(resolved_witness))),
    )


def _durable_binding_state(snapshot_path):
    _records, committed, pending = _read_witnesses(snapshot_path)
    current_sha = _file_sha256(snapshot_path)
    if pending is not None:
        raise ValueError(_SNAPSHOT_AUTHORITY_STALE_ERROR)
    if committed is None:
        if current_sha is not None:
            raise ValueError("existing PaperBook snapshot lacks independent durable authority")
        return 0, ""
    generation, snapshot_sha = committed
    if current_sha != snapshot_sha:
        raise ValueError("PaperBook current snapshot differs from independent durable witness")
    return generation, snapshot_sha


def _bind_book(book, snapshot_path):
    target = _binding(snapshot_path)
    generation, snapshot_sha = _durable_binding_state(snapshot_path)
    next_binding = (*target, generation, snapshot_sha)
    with _BINDING_LOCK:
        existing = _BOOK_BINDINGS.get(book)
        if existing is None:
            _BOOK_BINDINGS[book] = next_binding
            return
        if existing[:3] != target:
            raise ValueError(
                "PaperBook durable authority is already bound to another path or authority root"
            )
        if existing != next_binding:
            raise ValueError(_SNAPSHOT_AUTHORITY_STALE_ERROR)


def _require_bound_book(book, snapshot_path):
    target = _binding(snapshot_path)
    with _BINDING_LOCK:
        existing = _BOOK_BINDINGS.get(book)
    if existing is None or existing[:3] != target:
        raise ValueError(
            "existing PaperBook snapshot lineage requires verified path-bound authority"
        )
    generation, snapshot_sha = _durable_binding_state(snapshot_path)
    if existing[3:] != (generation, snapshot_sha):
        raise ValueError(_SNAPSHOT_AUTHORITY_STALE_ERROR)


def _advance_book_binding(book, snapshot_path):
    target = _binding(snapshot_path)
    generation, snapshot_sha = _durable_binding_state(snapshot_path)
    with _BINDING_LOCK:
        existing = _BOOK_BINDINGS.get(book)
        if existing is None or existing[:3] != target:
            raise ValueError(
                "PaperBook snapshot authority changed before durable generation advance"
            )
        old_generation = existing[3]
        if generation <= old_generation:
            raise ValueError("PaperBook snapshot authority generation did not advance")
        _BOOK_BINDINGS[book] = (*target, generation, snapshot_sha)


def _bound_snapshot_path(book):
    with _BINDING_LOCK:
        binding = _BOOK_BINDINGS.get(book)
    if binding is None:
        return None
    return _PATH(binding[1])


def _generation_guarded_load(cls, path):
    source = _PATH(path)
    witness = _witness_path(source)
    publication_lock = _acquire_snapshot_publication_lock(witness)
    try:
        book = _GENERATION_ORIGINAL_TRUSTED_LOAD(cls, path)
        _require_bound_book(book, source)
        return book
    finally:
        _release_snapshot_publication_lock(publication_lock)


def _generation_guarded_save(self, path):
    destination = _PATH(path)
    witness = _witness_path(destination)
    publication_lock = _acquire_snapshot_publication_lock(witness)
    try:
        with _BINDING_LOCK:
            has_binding = _BOOK_BINDINGS.get(self) is not None
        if has_binding:
            _require_bound_book(self, destination)
        _GENERATION_ORIGINAL_TRUSTED_SAVE(self, path)
        _advance_book_binding(self, destination)
    finally:
        _release_snapshot_publication_lock(publication_lock)


def _generation_guarded_committed_stake(self):
    snapshot_path = _bound_snapshot_path(self)
    if snapshot_path is None:
        return _GENERATION_ORIGINAL_COMMITTED_STAKE(self)
    publication_lock = _acquire_snapshot_publication_lock(_witness_path(snapshot_path))
    try:
        _require_bound_book(self, snapshot_path)
        return _GENERATION_ORIGINAL_COMMITTED_STAKE(self)
    finally:
        _release_snapshot_publication_lock(publication_lock)


def _generation_guarded_open_ticket(self, *args, **kwargs):
    snapshot_path = _bound_snapshot_path(self)
    if snapshot_path is None:
        return _GENERATION_ORIGINAL_OPEN_TICKET(self, *args, **kwargs)
    publication_lock = _acquire_snapshot_publication_lock(_witness_path(snapshot_path))
    try:
        _require_bound_book(self, snapshot_path)
        return _GENERATION_ORIGINAL_OPEN_TICKET(self, *args, **kwargs)
    finally:
        _release_snapshot_publication_lock(publication_lock)


def _generation_guarded_settle(self, *args, **kwargs):
    snapshot_path = _bound_snapshot_path(self)
    if snapshot_path is None:
        return _GENERATION_ORIGINAL_SETTLE(self, *args, **kwargs)
    publication_lock = _acquire_snapshot_publication_lock(_witness_path(snapshot_path))
    try:
        _require_bound_book(self, snapshot_path)
        return _GENERATION_ORIGINAL_SETTLE(self, *args, **kwargs)
    finally:
        _release_snapshot_publication_lock(publication_lock)


def _install() -> None:
    guard_namespace = _guard.__dict__
    paper_book = _paper.PaperBook
    paper_namespace = vars(paper_book)

    trusted_load = guard_namespace.get("_trusted_path_load")
    trusted_save = guard_namespace.get("_trusted_save")
    committed_stake_descriptor = paper_namespace.get("committed_stake")
    open_ticket = paper_namespace.get("open_ticket")
    settle = paper_namespace.get("settle")
    if any(type(value) is not FunctionType for value in (trusted_load, trusted_save, open_ticket, settle)):
        raise RuntimeError("canonical PaperBook generation-CAS composition surface changed")
    if (
        type(committed_stake_descriptor) is not property
        or type(committed_stake_descriptor.fget) is not FunctionType
    ):
        raise RuntimeError("canonical PaperBook committed_stake authority surface changed")
    committed_stake_getter = committed_stake_descriptor.fget

    guard_namespace["_LOCK_NORMCASE"] = os.path.normcase
    guard_namespace["_LOCK_ABSPATH"] = os.path.abspath
    guard_namespace["_LOCK_FSPATH"] = os.fspath
    guard_namespace["_LOCK_OS_OPEN"] = os.open
    guard_namespace["_LOCK_OS_CLOSE"] = os.close
    guard_namespace["_LOCK_OS_FSTAT"] = os.fstat
    guard_namespace["_LOCK_OS_WRITE"] = os.write
    guard_namespace["_LOCK_OS_FSYNC"] = os.fsync
    guard_namespace["_LOCK_OS_LSEEK"] = os.lseek
    guard_namespace["_LOCK_SEEK_SET"] = os.SEEK_SET
    guard_namespace["_LOCK_OPEN_FLAGS"] = os.O_CREAT | os.O_RDWR
    guard_namespace["_LOCK_PLATFORM"] = os.name
    if os.name == "nt":
        import msvcrt

        guard_namespace["_LOCK_PLATFORM_CALL"] = msvcrt.locking
        guard_namespace["_LOCK_PLATFORM_ACQUIRE"] = msvcrt.LK_NBLCK
        guard_namespace["_LOCK_PLATFORM_RELEASE"] = msvcrt.LK_UNLCK
    else:
        import fcntl

        guard_namespace["_LOCK_PLATFORM_CALL"] = fcntl.flock
        guard_namespace["_LOCK_PLATFORM_ACQUIRE"] = fcntl.LOCK_EX | fcntl.LOCK_NB
        guard_namespace["_LOCK_PLATFORM_RELEASE"] = fcntl.LOCK_UN

    guard_namespace["_SNAPSHOT_PUBLICATION_PROCESS_GUARD"] = threading.Lock()
    guard_namespace["_SNAPSHOT_PUBLICATION_PROCESS_LOCKS"] = set()
    guard_namespace["_SNAPSHOT_PUBLICATION_LOCKED_ERROR"] = _LOCKED_ERROR
    guard_namespace["_SNAPSHOT_AUTHORITY_STALE_ERROR"] = _STALE_ERROR
    guard_namespace["_GENERATION_ORIGINAL_TRUSTED_LOAD"] = trusted_load
    guard_namespace["_GENERATION_ORIGINAL_TRUSTED_SAVE"] = trusted_save
    guard_namespace["_GENERATION_ORIGINAL_COMMITTED_STAKE"] = committed_stake_getter
    guard_namespace["_GENERATION_ORIGINAL_OPEN_TICKET"] = open_ticket
    guard_namespace["_GENERATION_ORIGINAL_SETTLE"] = settle

    for function in (
        _canonical_lock_path_key,
        _snapshot_publication_lock_path,
        _acquire_snapshot_publication_lock,
        _release_snapshot_publication_lock,
        _binding,
        _durable_binding_state,
        _bind_book,
        _require_bound_book,
        _advance_book_binding,
        _bound_snapshot_path,
        _generation_guarded_load,
        _generation_guarded_save,
        _generation_guarded_committed_stake,
        _generation_guarded_open_ticket,
        _generation_guarded_settle,
    ):
        guard_namespace[function.__name__] = _clone_into_guard(function)

    guarded_load = guard_namespace["_generation_guarded_load"]
    guarded_save = guard_namespace["_generation_guarded_save"]
    guarded_committed_stake = guard_namespace["_generation_guarded_committed_stake"]
    guarded_open = guard_namespace["_generation_guarded_open_ticket"]
    guarded_settle = guard_namespace["_generation_guarded_settle"]

    guarded_load.__name__ = "load"
    guarded_load.__qualname__ = "PaperBook.load"
    guarded_save.__name__ = "save"
    guarded_save.__qualname__ = "PaperBook.save"
    guarded_committed_stake.__name__ = "committed_stake"
    guarded_committed_stake.__qualname__ = "PaperBook.committed_stake"
    guarded_open.__name__ = "open_ticket"
    guarded_open.__qualname__ = "PaperBook.open_ticket"
    guarded_settle.__name__ = "settle"
    guarded_settle.__qualname__ = "PaperBook.settle"

    guard_namespace["_trusted_path_load"] = guarded_load
    guard_namespace["_trusted_save"] = guarded_save
    paper_book.load = classmethod(guarded_load)
    paper_book.save = guarded_save
    paper_book.committed_stake = property(guarded_committed_stake)
    paper_book.open_ticket = guarded_open
    paper_book.settle = guarded_settle


_install()
del _install
