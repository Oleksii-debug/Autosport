from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator


_JOURNAL_SCHEMA_VERSION = 1
_ANCHOR_SCHEMA_VERSION = 1


class ExecutionStopAuthorityError(RuntimeError):
    """Base error for durable execution STOP authority."""


class ExecutionStopIntegrityError(ExecutionStopAuthorityError):
    """Raised when durable STOP authority evidence is incomplete or malformed."""


class ExecutionStopStateError(ExecutionStopAuthorityError):
    """Raised when an unsafe or stale STOP-authority transition is requested."""


class ExecutionStoppedError(ExecutionStopAuthorityError):
    """Raised when execution admission is denied by STOP authority."""


class ExecutionAuthorityMode(str, Enum):
    STOPPED = "STOPPED"
    ARMED = "ARMED"


@dataclass(frozen=True, slots=True)
class ExecutionAuthorityState:
    revision: int
    mode: ExecutionAuthorityMode
    command_id: str
    operator_id: str
    reason: str
    created_at: str
    confirmation_id: str | None
    record_sha256: str


@dataclass(frozen=True, slots=True)
class ExecutionAdmissionDecision:
    allowed: bool
    mode: ExecutionAuthorityMode
    revision: int | None
    reason: str


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ExecutionStopIntegrityError("value is not canonical JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _parse_json_object(raw: str, *, what: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ExecutionStopIntegrityError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ExecutionStopIntegrityError(
                    f"non-finite JSON constant {token!r}"
                )
            ),
        )
    except ExecutionStopIntegrityError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ExecutionStopIntegrityError(f"invalid {what} JSON") from exc
    if type(value) is not dict:
        raise ExecutionStopIntegrityError(f"{what} must be a JSON object")
    return value


def _text(value: object, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or "\x00" in value
    ):
        raise ValueError(f"{name} must be non-empty canonical text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be UTF-8 encodable") from exc
    return value


def _sha256(value: object, name: str) -> str:
    if type(value) is not str:
        raise ExecutionStopIntegrityError(f"{name} must be lowercase SHA-256 hex")
    raw = value
    if (
        len(raw) != 64
        or any(ch not in "0123456789abcdef" for ch in raw)
    ):
        raise ExecutionStopIntegrityError(f"{name} must be lowercase SHA-256 hex")
    return raw


def _timestamp_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _sync_directory(path: Path) -> None:
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
        raise ExecutionStopIntegrityError(
            "STOP authority directory sync failed"
        ) from exc
    finally:
        if fd is not None:
            os.close(fd)


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    """Cross-process lock for one authority journal, with no polling loop."""

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("a+b") as handle:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                    os.fsync(handle.fileno())
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except ExecutionStopAuthorityError:
        raise
    except OSError as exc:
        raise ExecutionStopIntegrityError(
            "cannot acquire STOP authority lock"
        ) from exc


class ExecutionStopAuthority:
    """Durable, explicit, fail-closed execution STOP/ARM authority.

    The journal is authoritative. Every command is hash chained and revisioned.
    A separately replaced anchor must agree with the journal tip; interruption,
    truncation, stale replay, or malformed state therefore denies execution.
    ARM is never inferred or automatic: it requires an explicit confirmation.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._anchor_path = self.path.with_name(self.path.name + ".anchor.json")
        self._lock_path = self.path.with_name(self.path.name + ".lock")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._thread_lock = threading.RLock()

    @property
    def anchor_path(self) -> Path:
        return self._anchor_path

    def _read_journal_unlocked(self) -> list[dict[str, Any]]:
        journal_exists = self.path.exists()
        anchor_exists = self._anchor_path.exists()
        if not journal_exists and not anchor_exists:
            return []
        if journal_exists != anchor_exists:
            raise ExecutionStopIntegrityError(
                "STOP authority journal/anchor pair is incomplete"
            )

        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise ExecutionStopIntegrityError(
                "cannot read STOP authority journal"
            ) from exc
        if not lines:
            raise ExecutionStopIntegrityError(
                "STOP authority journal must not be empty"
            )

        expected_keys = {
            "schema_version",
            "revision",
            "command_id",
            "mode",
            "operator_id",
            "reason",
            "created_at",
            "confirmation_id",
            "previous_sha256",
            "record_sha256",
        }
        records: list[dict[str, Any]] = []
        seen_command_ids: set[str] = set()
        seen_arm_confirmations: set[str] = set()
        previous_sha256: str | None = None

        for index, raw in enumerate(lines, start=1):
            if not raw:
                raise ExecutionStopIntegrityError(
                    "STOP authority journal contains a blank record"
                )
            record = _parse_json_object(raw, what="STOP authority record")
            if set(record) != expected_keys:
                raise ExecutionStopIntegrityError(
                    "STOP authority record schema is invalid"
                )
            if record["schema_version"] != _JOURNAL_SCHEMA_VERSION:
                raise ExecutionStopIntegrityError(
                    "unsupported STOP authority journal schema"
                )
            if type(record["revision"]) is not int or record["revision"] != index:
                raise ExecutionStopIntegrityError(
                    "STOP authority revision is not contiguous"
                )
            try:
                mode = ExecutionAuthorityMode(record["mode"])
                command_id = _text(record["command_id"], "command_id")
                _text(record["operator_id"], "operator_id")
                _text(record["reason"], "reason")
                created_at = _text(record["created_at"], "created_at")
                parsed_created_at = datetime.fromisoformat(
                    created_at.replace("Z", "+00:00")
                )
                if (
                    parsed_created_at.tzinfo is None
                    or parsed_created_at.utcoffset() is None
                ):
                    raise ValueError("created_at must be timezone-aware")
            except (ValueError, TypeError) as exc:
                raise ExecutionStopIntegrityError(
                    "invalid STOP authority record"
                ) from exc

            if command_id in seen_command_ids:
                raise ExecutionStopIntegrityError(
                    "STOP authority command replay detected"
                )
            seen_command_ids.add(command_id)

            confirmation_id = record["confirmation_id"]
            if mode is ExecutionAuthorityMode.ARMED:
                try:
                    confirmation = _text(confirmation_id, "confirmation_id")
                except (ValueError, TypeError) as exc:
                    raise ExecutionStopIntegrityError(
                        "ARM record requires explicit confirmation"
                    ) from exc
                if confirmation in seen_arm_confirmations:
                    raise ExecutionStopIntegrityError(
                        "ARM confirmation replay detected"
                    )
                seen_arm_confirmations.add(confirmation)
            elif confirmation_id is not None:
                raise ExecutionStopIntegrityError(
                    "STOP record cannot carry ARM confirmation"
                )

            if record["previous_sha256"] != previous_sha256:
                raise ExecutionStopIntegrityError(
                    "STOP authority hash-chain predecessor mismatch"
                )
            record_sha256 = _sha256(
                record["record_sha256"], "record_sha256"
            )
            body = {
                key: record[key]
                for key in expected_keys
                if key != "record_sha256"
            }
            if record_sha256 != _digest(body):
                raise ExecutionStopIntegrityError(
                    "STOP authority record digest mismatch"
                )
            previous_sha256 = record_sha256
            records.append(record)

        anchor = self._read_anchor_unlocked()
        latest = records[-1]
        if (
            anchor["revision"] != latest["revision"]
            or anchor["record_sha256"] != latest["record_sha256"]
        ):
            raise ExecutionStopIntegrityError(
                "STOP authority journal is older or newer than its durable anchor"
            )
        return records

    def _read_anchor_unlocked(self) -> dict[str, Any]:
        try:
            raw = self._anchor_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ExecutionStopIntegrityError(
                "cannot read STOP authority anchor"
            ) from exc
        anchor = _parse_json_object(raw, what="STOP authority anchor")
        expected = {
            "schema_version",
            "revision",
            "record_sha256",
            "anchor_sha256",
        }
        if set(anchor) != expected:
            raise ExecutionStopIntegrityError(
                "STOP authority anchor schema is invalid"
            )
        if anchor["schema_version"] != _ANCHOR_SCHEMA_VERSION:
            raise ExecutionStopIntegrityError(
                "unsupported STOP authority anchor schema"
            )
        if type(anchor["revision"]) is not int or anchor["revision"] <= 0:
            raise ExecutionStopIntegrityError(
                "STOP authority anchor revision is invalid"
            )
        _sha256(anchor["record_sha256"], "record_sha256")
        _sha256(anchor["anchor_sha256"], "anchor_sha256")
        body = {
            key: anchor[key]
            for key in expected
            if key != "anchor_sha256"
        }
        if anchor["anchor_sha256"] != _digest(body):
            raise ExecutionStopIntegrityError(
                "STOP authority anchor digest mismatch"
            )
        return anchor

    def _write_anchor_unlocked(
        self, *, revision: int, record_sha256: str
    ) -> None:
        body = {
            "schema_version": _ANCHOR_SCHEMA_VERSION,
            "revision": revision,
            "record_sha256": record_sha256,
        }
        anchor = {**body, "anchor_sha256": _digest(body)}
        encoded = _canonical(anchor) + "\n"
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=self.path.parent,
                prefix=self._anchor_path.name + ".",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
                temp_path = Path(handle.name)
            os.replace(temp_path, self._anchor_path)
            temp_path = None
            _sync_directory(self.path.parent)
        except OSError as exc:
            raise ExecutionStopIntegrityError(
                "STOP authority anchor durability barrier failed"
            ) from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _append_unlocked(
        self,
        *,
        mode: ExecutionAuthorityMode,
        operator_id: str,
        reason: str,
        expected_revision: int,
        confirmation_id: str | None,
        command_id: str | None,
    ) -> ExecutionAuthorityState:
        operator = _text(operator_id, "operator_id")
        why = _text(reason, "reason")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative int")

        records = self._read_journal_unlocked()
        actual_revision = 0 if not records else records[-1]["revision"]
        if expected_revision != actual_revision:
            raise ExecutionStopStateError(
                f"stale STOP authority revision: expected {expected_revision}, "
                f"actual {actual_revision}"
            )

        if mode is ExecutionAuthorityMode.ARMED:
            if actual_revision == 0:
                raise ExecutionStopStateError(
                    "cannot ARM missing STOP authority; initialize STOPPED first"
                )
            confirmation = _text(confirmation_id, "confirmation_id")
            for record in records:
                if (
                    record["mode"] == ExecutionAuthorityMode.ARMED.value
                    and record["confirmation_id"] == confirmation
                ):
                    raise ExecutionStopStateError(
                        "ARM confirmation replay rejected"
                    )
        else:
            if confirmation_id is not None:
                raise ValueError("STOP command cannot carry confirmation_id")
            confirmation = None

        command = command_id or str(uuid.uuid4())
        command = _text(command, "command_id")
        if any(record["command_id"] == command for record in records):
            raise ExecutionStopStateError(
                "STOP authority command replay rejected"
            )

        revision = actual_revision + 1
        previous_sha256 = None if not records else records[-1]["record_sha256"]
        body = {
            "schema_version": _JOURNAL_SCHEMA_VERSION,
            "revision": revision,
            "command_id": command,
            "mode": mode.value,
            "operator_id": operator,
            "reason": why,
            "created_at": _timestamp_text(self._clock()),
            "confirmation_id": confirmation,
            "previous_sha256": previous_sha256,
        }
        record = {**body, "record_sha256": _digest(body)}
        encoded = _canonical(record) + "\n"
        path_existed = self.path.exists()
        try:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            if not path_existed:
                _sync_directory(self.path.parent)
            self._write_anchor_unlocked(
                revision=revision,
                record_sha256=record["record_sha256"],
            )
        except OSError as exc:
            raise ExecutionStopIntegrityError(
                "STOP authority journal durability barrier failed"
            ) from exc
        return self._state_from_record(record)

    @staticmethod
    def _state_from_record(record: dict[str, Any]) -> ExecutionAuthorityState:
        return ExecutionAuthorityState(
            revision=record["revision"],
            mode=ExecutionAuthorityMode(record["mode"]),
            command_id=record["command_id"],
            operator_id=record["operator_id"],
            reason=record["reason"],
            created_at=record["created_at"],
            confirmation_id=record["confirmation_id"],
            record_sha256=record["record_sha256"],
        )

    def current(self) -> ExecutionAuthorityState:
        with self._thread_lock, _exclusive_file_lock(self._lock_path):
            records = self._read_journal_unlocked()
            if not records:
                raise ExecutionStopStateError(
                    "STOP authority is missing; execution remains stopped"
                )
            return self._state_from_record(records[-1])

    def initialize_stopped(
        self,
        *,
        operator_id: str,
        reason: str,
        command_id: str | None = None,
    ) -> ExecutionAuthorityState:
        with self._thread_lock, _exclusive_file_lock(self._lock_path):
            if self.path.exists() or self._anchor_path.exists():
                records = self._read_journal_unlocked()
                if records:
                    raise ExecutionStopStateError(
                        "STOP authority is already initialized"
                    )
            return self._append_unlocked(
                mode=ExecutionAuthorityMode.STOPPED,
                operator_id=operator_id,
                reason=reason,
                expected_revision=0,
                confirmation_id=None,
                command_id=command_id,
            )

    def stop(
        self,
        *,
        operator_id: str,
        reason: str,
        expected_revision: int | None = None,
        command_id: str | None = None,
    ) -> ExecutionAuthorityState:
        with self._thread_lock, _exclusive_file_lock(self._lock_path):
            records = self._read_journal_unlocked()
            actual_revision = 0 if not records else records[-1]["revision"]
            expected = actual_revision if expected_revision is None else expected_revision
            return self._append_unlocked(
                mode=ExecutionAuthorityMode.STOPPED,
                operator_id=operator_id,
                reason=reason,
                expected_revision=expected,
                confirmation_id=None,
                command_id=command_id,
            )

    def arm(
        self,
        *,
        operator_id: str,
        reason: str,
        confirmation_id: str,
        expected_revision: int,
        command_id: str | None = None,
    ) -> ExecutionAuthorityState:
        with self._thread_lock, _exclusive_file_lock(self._lock_path):
            return self._append_unlocked(
                mode=ExecutionAuthorityMode.ARMED,
                operator_id=operator_id,
                reason=reason,
                expected_revision=expected_revision,
                confirmation_id=confirmation_id,
                command_id=command_id,
            )

    def decision(self) -> ExecutionAdmissionDecision:
        try:
            state = self.current()
        except ExecutionStopAuthorityError as exc:
            return ExecutionAdmissionDecision(
                allowed=False,
                mode=ExecutionAuthorityMode.STOPPED,
                revision=None,
                reason=f"fail-closed: {exc}",
            )
        if state.mode is not ExecutionAuthorityMode.ARMED:
            return ExecutionAdmissionDecision(
                allowed=False,
                mode=state.mode,
                revision=state.revision,
                reason=state.reason,
            )
        return ExecutionAdmissionDecision(
            allowed=True,
            mode=state.mode,
            revision=state.revision,
            reason=state.reason,
        )

    def assert_execution_allowed(self) -> ExecutionAuthorityState:
        state = self.current()
        if state.mode is not ExecutionAuthorityMode.ARMED:
            raise ExecutionStoppedError(
                f"execution STOP is active at revision {state.revision}: "
                f"{state.reason}"
            )
        return state
