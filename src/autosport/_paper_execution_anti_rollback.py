from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from . import _paper_execution_reality_legacy as _impl


_WITNESS_SCHEMA_VERSION = 2
_WITNESS_SUFFIX = ".monotonic-witness.jsonl"
_WITNESS_DIR_ENV = "AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR"


_ORIGINAL_INIT = _impl.PaperExecutionLedger.__init__
_ORIGINAL_LOAD_UNLOCKED = _impl.PaperExecutionLedger._load_unlocked
_ORIGINAL_WRITE_ANCHOR_UNLOCKED = _impl.PaperExecutionLedger._write_anchor_unlocked


def _ledger_identity(path: Path) -> str:
    normalized = os.path.normcase(os.path.abspath(os.fspath(path)))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _authority_root(ledger_path: Path) -> Path:
    configured = os.environ.get(_WITNESS_DIR_ENV)
    if configured:
        root = Path(configured).expanduser()
    elif os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        root = (
            Path(base)
            if base
            else Path.home() / "AppData" / "Local"
        ) / "Autosport" / "state" / "paper-execution-reality"
    else:
        base = os.environ.get("XDG_STATE_HOME")
        root = (
            Path(base).expanduser()
            if base
            else Path.home() / ".local" / "state"
        ) / "autosport" / "paper-execution-reality"
    try:
        root = root.resolve(strict=False)
        workspace = Path(ledger_path).expanduser().resolve(strict=False).parent
    except OSError as exc:
        raise _impl.PaperExecutionIntegrityError(
            "cannot resolve independent PAPER execution monotonic authority"
        ) from exc
    if root == workspace or _is_within(root, workspace):
        raise _impl.PaperExecutionIntegrityError(
            "PAPER execution monotonic authority must resolve outside ledger workspace"
        )
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise _impl.PaperExecutionIntegrityError(
            "cannot establish independent PAPER execution monotonic authority"
        ) from exc
    return root


def _sync_authority_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd: int | None = None
    try:
        fd = os.open(path, flags)
        os.fsync(fd)
    except OSError as exc:
        raise _impl.PaperExecutionIntegrityError(
            "PAPER execution monotonic authority directory sync failed"
        ) from exc
    finally:
        if fd is not None:
            os.close(fd)


def _witness_path(self) -> Path:
    path = getattr(self, "_monotonic_witness_path", None)
    if path is None:
        identity = _ledger_identity(self.path)
        path = _authority_root(self.path) / f"{identity}{_WITNESS_SUFFIX}"
        self._monotonic_witness_path = path
        self._monotonic_witness_ledger_identity = identity
    return Path(path)


def _validate_root(root: object, *, allow_none: bool) -> str | None:
    if root is None and allow_none:
        return None
    if (
        type(root) is not str
        or len(root) != 64
        or any(ch not in "0123456789abcdef" for ch in root)
    ):
        raise _impl.PaperExecutionIntegrityError(
            "PAPER execution monotonic witness root is invalid"
        )
    return root


def _read_witnesses_unlocked(self) -> list[dict[str, Any]]:
    path = _witness_path(self)
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise _impl.PaperExecutionIntegrityError(
            "cannot read PAPER execution monotonic witness journal"
        ) from exc

    records: list[dict[str, Any]] = []
    previous_witness_sha256: str | None = None
    previous_event_count = 0
    expected_identity = _ledger_identity(self.path)
    expected_keys = {
        "witness_schema_version",
        "generation",
        "ledger_identity",
        "ledger_name",
        "event_count",
        "ledger_root_sha256",
        "previous_witness_sha256",
        "witness_sha256",
    }
    for index, raw in enumerate(lines, start=1):
        if not raw:
            raise _impl.PaperExecutionIntegrityError(
                "PAPER execution monotonic witness journal contains a blank line"
            )
        record = _impl._parse_json_object(
            raw,
            what="PAPER execution monotonic witness",
        )
        if set(record) != expected_keys:
            raise _impl.PaperExecutionIntegrityError(
                "PAPER execution monotonic witness schema is invalid"
            )
        if record["witness_schema_version"] != _WITNESS_SCHEMA_VERSION:
            raise _impl.PaperExecutionIntegrityError(
                "unsupported PAPER execution monotonic witness schema"
            )
        if record["generation"] != index:
            raise _impl.PaperExecutionIntegrityError(
                "PAPER execution monotonic witness generation is not contiguous"
            )
        if record["ledger_identity"] != expected_identity:
            raise _impl.PaperExecutionIntegrityError(
                "PAPER execution monotonic witness belongs to another ledger identity"
            )
        if record["ledger_name"] != self.path.name:
            raise _impl.PaperExecutionIntegrityError(
                "PAPER execution monotonic witness belongs to another ledger"
            )
        event_count = record["event_count"]
        if type(event_count) is not int or event_count <= previous_event_count:
            raise _impl.PaperExecutionIntegrityError(
                "PAPER execution monotonic witness event count is not increasing"
            )
        root = _validate_root(record["ledger_root_sha256"], allow_none=False)
        if record["previous_witness_sha256"] != previous_witness_sha256:
            raise _impl.PaperExecutionIntegrityError(
                "PAPER execution monotonic witness chain predecessor mismatch"
            )
        body = {
            key: record[key]
            for key in expected_keys
            if key != "witness_sha256"
        }
        if record["witness_sha256"] != _impl._digest(body):
            raise _impl.PaperExecutionIntegrityError(
                "PAPER execution monotonic witness digest mismatch"
            )
        records.append(record)
        previous_witness_sha256 = record["witness_sha256"]
        previous_event_count = event_count
        assert root is not None
    return records


def _append_witness_unlocked(self, events: list[dict[str, Any]]) -> None:
    if not events:
        raise _impl.PaperExecutionIntegrityError(
            "cannot witness an empty PAPER execution history"
        )
    records = _read_witnesses_unlocked(self)
    event_count = len(events)
    root = events[-1]["event_sha256"]
    _validate_root(root, allow_none=False)

    if records:
        previous = records[-1]
        previous_count = previous["event_count"]
        previous_root = previous["ledger_root_sha256"]
        if event_count <= previous_count:
            raise _impl.PaperExecutionIntegrityError(
                "PAPER execution history did not advance beyond monotonic witness"
            )
        if previous_count > len(events):
            raise _impl.PaperExecutionIntegrityError(
                "PAPER execution history is older than monotonic witness"
            )
        if events[previous_count - 1]["event_sha256"] != previous_root:
            raise _impl.PaperExecutionIntegrityError(
                "PAPER execution history does not extend monotonic witness root"
            )
        previous_witness_sha256 = previous["witness_sha256"]
    else:
        previous_witness_sha256 = None

    identity = _ledger_identity(self.path)
    body = {
        "witness_schema_version": _WITNESS_SCHEMA_VERSION,
        "generation": len(records) + 1,
        "ledger_identity": identity,
        "ledger_name": self.path.name,
        "event_count": event_count,
        "ledger_root_sha256": root,
        "previous_witness_sha256": previous_witness_sha256,
    }
    record = {**body, "witness_sha256": _impl._digest(body)}
    path = _witness_path(self)
    path_existed_before = path.exists()
    try:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(_impl._canonical(record) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if not path_existed_before:
            _sync_authority_directory(path.parent)
    except OSError as exc:
        raise _impl.PaperExecutionIntegrityError(
            "PAPER execution monotonic witness durability barrier failed"
        ) from exc


def _patched_init(self, path) -> None:
    _ORIGINAL_INIT(self, path)
    identity = _ledger_identity(self.path)
    self._monotonic_witness_ledger_identity = identity
    self._monotonic_witness_path = (
        _authority_root(self.path) / f"{identity}{_WITNESS_SUFFIX}"
    )


def _patched_write_anchor_unlocked(self, events: list[dict[str, Any]]) -> None:
    # Publish the authority outside the workspace rollback domain before
    # replacing the mutable latest-root anchor. If anchor publication crashes,
    # restart fails closed because the external authority is ahead of local state.
    _append_witness_unlocked(self, events)
    _ORIGINAL_WRITE_ANCHOR_UNLOCKED(self, events)


def _patched_load_unlocked(self) -> list[dict[str, Any]]:
    events = _ORIGINAL_LOAD_UNLOCKED(self)
    records = _read_witnesses_unlocked(self)

    pair_exists = self.path.exists() or self._anchor_path.exists()
    if not records:
        if events or pair_exists:
            raise _impl.PaperExecutionIntegrityError(
                "PAPER execution history is missing independent monotonic authority"
            )
        return events

    latest = records[-1]
    expected_count = latest["event_count"]
    expected_root = latest["ledger_root_sha256"]
    actual_count = len(events)
    actual_root = None if not events else events[-1]["event_sha256"]
    if actual_count != expected_count or actual_root != expected_root:
        raise _impl.PaperExecutionIntegrityError(
            "PAPER execution ledger/anchor pair is older than monotonic witness authority"
        )
    return events


def _install() -> None:
    cls = _impl.PaperExecutionLedger
    if getattr(cls, "_autosport_monotonic_witness_installed", False):
        return
    cls.__init__ = _patched_init
    cls._load_unlocked = _patched_load_unlocked
    cls._write_anchor_unlocked = _patched_write_anchor_unlocked
    cls._read_monotonic_witnesses_unlocked = _read_witnesses_unlocked
    cls._autosport_monotonic_witness_installed = True


_install()
