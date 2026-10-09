"""Fence the #1272 settlement journal to one regular file identity.

The monotonic settlement authority proves journal content progression.  It does not
make a pathname immutable.  This composition keeps the existing revision schema,
writer lock and monotonic protocol, but refuses symlink/non-regular journals and
requires append to target the exact file identity that was just reloaded.
"""
from __future__ import annotations

import json
import os
import stat

from . import betfair_settlement_revisions as _settlement


_STORE = _settlement.BetfairSettlementRevisionStore
_ORIGINAL_RELOAD = _STORE._reload
_ORIGINAL_APPEND = _STORE._append
_IDENTITY_ATTR = "_settlement_loaded_file_identity"


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _lstat_regular(path) -> os.stat_result | None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        raise _settlement.BetfairSettlementRevisionError(
            "settlement journal path must not be a symlink"
        )
    if not stat.S_ISREG(info.st_mode):
        raise _settlement.BetfairSettlementRevisionError(
            "settlement journal path must be a regular file"
        )
    if info.st_nlink != 1:
        raise _settlement.BetfairSettlementRevisionError(
            "settlement journal path must not have hard-link aliases"
        )
    return info


def _open_existing_regular(
    path,
    flags: int,
    *,
    expected_identity: tuple[int, int, int, int] | None = None,
) -> int:
    before = _lstat_regular(path)
    if before is None:
        raise FileNotFoundError(path)
    before_identity = _identity(before)
    if expected_identity is not None and before_identity != expected_identity:
        raise _settlement.BetfairSettlementRevisionError(
            "settlement journal changed after reload"
        )
    fd = os.open(
        path,
        flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise _settlement.BetfairSettlementRevisionError(
                "settlement journal path must be a regular file"
            )
        if opened.st_nlink != 1:
            raise _settlement.BetfairSettlementRevisionError(
                "settlement journal path must not have hard-link aliases"
            )
        opened_identity = _identity(opened)
        if opened_identity != before_identity:
            raise _settlement.BetfairSettlementRevisionError(
                "settlement journal path changed during open"
            )
        if expected_identity is not None and opened_identity != expected_identity:
            raise _settlement.BetfairSettlementRevisionError(
                "settlement journal changed after reload"
            )
    except BaseException:
        os.close(fd)
        raise
    return fd


def _require_path_references_open_file(path, fd: int) -> None:
    current = _lstat_regular(path)
    if current is None:
        raise _settlement.BetfairSettlementRevisionError(
            "settlement journal path disappeared during I/O"
        )
    opened = os.fstat(fd)
    if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
        raise _settlement.BetfairSettlementRevisionError(
            "settlement journal path changed during I/O"
        )


def _secure_reload(self) -> None:
    previous_revisions = self._revisions
    previous_by_bet = self._by_bet
    previous_last_record_sha256 = self._last_record_sha256
    previous_identity = getattr(self, _IDENTITY_ATTR, None)
    self._revisions = []
    self._by_bet = {}
    self._last_record_sha256 = None
    fd: int | None = None
    try:
        previous_hash: str | None = None
        loaded_identity: tuple[int, int, int, int] | None = None
        try:
            fd = _open_existing_regular(
                self.path,
                os.O_RDONLY | getattr(os, "O_BINARY", 0),
            )
        except FileNotFoundError:
            fd = None

        if fd is not None:
            opened_identity = _identity(os.fstat(fd))
            try:
                with os.fdopen(fd, "r", encoding="utf-8", newline="") as handle:
                    fd = None  # fdopen owns the descriptor from here.
                    for line_no, line in enumerate(handle, 1):
                        if not line.endswith("\n"):
                            raise _settlement.BetfairSettlementRevisionError(
                                "settlement log has partial final record"
                            )
                        record = json.loads(
                            line,
                            object_pairs_hook=_settlement._pairs,
                            parse_constant=_settlement._nonfinite,
                        )
                        if type(record) is not dict or set(record) != {
                            "schema",
                            "schema_version",
                            "previous_record_sha256",
                            "revision",
                            "record_sha256",
                        }:
                            raise _settlement.BetfairSettlementRevisionError(
                                f"settlement record {line_no} schema invalid"
                            )
                        if (
                            record["schema"] != _settlement._SCHEMA
                            or record["schema_version"] != _settlement._SCHEMA_VERSION
                        ):
                            raise _settlement.BetfairSettlementRevisionError(
                                f"settlement record {line_no} schema unsupported"
                            )
                        if record["previous_record_sha256"] != previous_hash:
                            raise _settlement.BetfairSettlementRevisionError(
                                f"settlement record {line_no} hash chain broken"
                            )
                        supplied = _settlement._sha(
                            record["record_sha256"], "record_sha256"
                        )
                        unsigned = dict(record)
                        unsigned.pop("record_sha256")
                        if supplied != _settlement._digest(unsigned):
                            raise _settlement.BetfairSettlementRevisionError(
                                f"settlement record {line_no} digest mismatch"
                            )
                        self._accept(
                            _settlement.BetfairSettlementRevision.from_dict(
                                record["revision"]
                            )
                        )
                        previous_hash = supplied
                    after_identity = _identity(os.fstat(handle.fileno()))
                    if after_identity != opened_identity:
                        raise _settlement.BetfairSettlementRevisionError(
                            "settlement journal changed during reload"
                        )
                    _require_path_references_open_file(
                        self.path,
                        handle.fileno(),
                    )
                    loaded_identity = after_identity
            except json.JSONDecodeError as exc:
                raise _settlement.BetfairSettlementRevisionError(
                    "settlement log is invalid JSON"
                ) from exc

        self._last_record_sha256 = previous_hash
        setattr(self, _IDENTITY_ATTR, loaded_identity)
        self._ensure_monotonic_current(adopt_if_missing=True)
    except Exception:
        self._revisions = previous_revisions
        self._by_bet = previous_by_bet
        self._last_record_sha256 = previous_last_record_sha256
        setattr(self, _IDENTITY_ATTR, previous_identity)
        raise
    finally:
        if fd is not None:
            os.close(fd)


def _secure_append(self, revision) -> None:
    unsigned = self._record_unsigned(revision)
    record_hash = _settlement._digest(unsigned)
    line = (
        _settlement._canonical({**unsigned, "record_sha256": record_hash}) + "\n"
    ).encode("utf-8")
    expected_identity = getattr(self, _IDENTITY_ATTR, None)
    self.path.parent.mkdir(parents=True, exist_ok=True)
    base_flags = (
        os.O_WRONLY
        | os.O_APPEND
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    fd: int | None = None
    created = False
    try:
        if expected_identity is None:
            try:
                fd = os.open(
                    self.path,
                    base_flags
                    | getattr(os, "O_NOFOLLOW", 0)
                    | os.O_CREAT
                    | os.O_EXCL,
                    0o600,
                )
            except FileExistsError as exc:
                raise _settlement.BetfairSettlementRevisionError(
                    "settlement journal path appeared after reload"
                ) from exc
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise _settlement.BetfairSettlementRevisionError(
                    "settlement journal path must be a regular file"
                )
            created = True
        else:
            try:
                fd = _open_existing_regular(
                    self.path,
                    base_flags,
                    expected_identity=expected_identity,
                )
            except FileNotFoundError as exc:
                raise _settlement.BetfairSettlementRevisionError(
                    "settlement journal path disappeared after reload"
                ) from exc

        offset = 0
        while offset < len(line):
            written = os.write(fd, line[offset:])
            if written <= 0:
                raise OSError("settlement journal append made no progress")
            offset += written
        os.fsync(fd)
        _require_path_references_open_file(self.path, fd)

        if created and os.name != "nt":
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_fd = os.open(self.path.parent, flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if fd is not None:
            os.close(fd)


if _STORE._reload is not _ORIGINAL_RELOAD or _STORE._append is not _ORIGINAL_APPEND:
    raise RuntimeError("Betfair settlement journal I/O changed before path guard install")

_STORE._reload = _secure_reload
_STORE._append = _secure_append

__all__: list[str] = []
