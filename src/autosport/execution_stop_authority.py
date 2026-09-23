from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator

from .monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
)


_JOURNAL_SCHEMA_VERSION = 1
_ANCHOR_SCHEMA_VERSION = 1
_MONOTONIC_DOMAIN = "execution-stop-authority"
_MONOTONIC_BINDING_SCHEMA = "autosport.execution_stop_authority.monotonic_binding"
_MONOTONIC_BINDING_VERSION = 1
_MONOTONIC_STATE_SCHEMA = "autosport.execution_stop_authority.monotonic_state"
_MONOTONIC_STATE_VERSION = 1
_MONOTONIC_RECEIPT_SCHEMA = "autosport.execution_stop_authority.monotonic_binding_receipt"
_MONOTONIC_RECEIPT_VERSION = 1
_MONOTONIC_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "workspace_instance_id",
        "domain",
        "key",
        "namespace_sha256",
        "semantic_binding_sha256",
        "receipt_sha256",
    }
)


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
def _exclusive_file_lock(
    path: Path,
    *,
    guard_path: Path | None = None,
) -> Iterator[None]:
    """Cross-process lock for one authority journal, with no polling loop.

    On POSIX, an existing canonical journal is locked as a second inode-stable
    guard.  Unlinking/replacing the pathname-based sidecar can therefore not
    create a second authority lock domain while the original operation is live.
    """

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
                guard_handle = None
                try:
                    locked_sidecar = os.fstat(handle.fileno())
                    try:
                        current_sidecar = os.stat(path, follow_symlinks=False)
                    except FileNotFoundError as exc:
                        raise ExecutionStopIntegrityError(
                            "STOP authority lock sidecar disappeared during acquisition"
                        ) from exc
                    if (
                        not stat.S_ISREG(locked_sidecar.st_mode)
                        or locked_sidecar.st_nlink != 1
                        or (locked_sidecar.st_dev, locked_sidecar.st_ino)
                        != (current_sidecar.st_dev, current_sidecar.st_ino)
                    ):
                        raise ExecutionStopIntegrityError(
                            "STOP authority lock sidecar identity changed during acquisition"
                        )

                    if guard_path is not None:
                        try:
                            guard_handle = guard_path.open("rb")
                        except FileNotFoundError:
                            guard_handle = None
                        if guard_handle is not None:
                            fcntl.flock(guard_handle.fileno(), fcntl.LOCK_EX)
                            locked_guard = os.fstat(guard_handle.fileno())
                            try:
                                current_guard = os.stat(
                                    guard_path, follow_symlinks=False
                                )
                            except FileNotFoundError as exc:
                                raise ExecutionStopIntegrityError(
                                    "STOP authority journal disappeared during lock acquisition"
                                ) from exc
                            if (
                                not stat.S_ISREG(locked_guard.st_mode)
                                or locked_guard.st_nlink != 1
                                or (locked_guard.st_dev, locked_guard.st_ino)
                                != (current_guard.st_dev, current_guard.st_ino)
                            ):
                                raise ExecutionStopIntegrityError(
                                    "STOP authority journal identity changed during lock acquisition"
                                )
                    yield
                finally:
                    if guard_handle is not None:
                        try:
                            fcntl.flock(guard_handle.fileno(), fcntl.LOCK_UN)
                        finally:
                            guard_handle.close()
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

    @staticmethod
    def _monotonic_key(path: Path) -> str:
        name = os.path.normcase(Path(path).name)
        return "execution-stop-" + hashlib.sha256(name.encode("utf-8")).hexdigest()

    def _monotonic_authority(self) -> MonotonicWorkspaceAuthority:
        absolute = Path(os.path.abspath(os.fspath(self.path)))
        return MonotonicWorkspaceAuthority(
            workspace=absolute.parent,
            domain=_MONOTONIC_DOMAIN,
            key=self._monotonic_key(absolute),
        )

    def _stable_serialization_lock_path(self) -> Path:
        """Return the machine-root lock shared by every STOP authority instance."""

        authority = self._monotonic_authority()
        return (
            authority.authority_root
            / "consumer-locks"
            / authority.namespace_sha256[:2]
            / f"{authority.namespace_sha256}.execution-stop.lock"
        )

    @contextmanager
    def _authority_operation_lock(self) -> Iterator[None]:
        """Serialize STOP operations outside replaceable workspace pathnames."""

        with self._thread_lock:
            stable_lock_path = self._stable_serialization_lock_path()
            with _exclusive_file_lock(stable_lock_path):
                with _exclusive_file_lock(
                    self._lock_path,
                    guard_path=self.path,
                ):
                    yield

    def _monotonic_binding(self) -> str:
        return _digest(
            {
                "schema": _MONOTONIC_BINDING_SCHEMA,
                "schema_version": _MONOTONIC_BINDING_VERSION,
                "state_key": self._monotonic_key(self.path),
                "journal_schema_version": _JOURNAL_SCHEMA_VERSION,
                "anchor_schema_version": _ANCHOR_SCHEMA_VERSION,
            }
        )

    @staticmethod
    def _monotonic_receipt_path(
        authority: MonotonicWorkspaceAuthority,
    ) -> Path:
        return (
            authority.authority_root
            / "consumer-bindings"
            / authority.namespace_sha256[:2]
            / f"{authority.namespace_sha256}.execution-stop.json"
        )

    def _monotonic_receipt_payload(
        self,
        authority: MonotonicWorkspaceAuthority,
        *,
        semantic_binding_sha256: str,
    ) -> dict[str, object]:
        body: dict[str, object] = {
            "schema": _MONOTONIC_RECEIPT_SCHEMA,
            "schema_version": _MONOTONIC_RECEIPT_VERSION,
            "workspace_instance_id": authority.workspace_instance_id,
            "domain": authority.domain,
            "key": authority.key,
            "namespace_sha256": authority.namespace_sha256,
            "semantic_binding_sha256": semantic_binding_sha256,
        }
        return {**body, "receipt_sha256": _digest(body)}

    def _read_monotonic_receipt_unlocked(
        self,
        authority: MonotonicWorkspaceAuthority,
        *,
        semantic_binding_sha256: str,
    ) -> bool:
        path = self._monotonic_receipt_path(authority)
        if not path.exists():
            return False
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ExecutionStopIntegrityError(
                "cannot inspect STOP monotonic binding receipt"
            ) from exc
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ExecutionStopIntegrityError(
                "STOP monotonic binding receipt must be a single-link regular file"
            )
        try:
            raw = _parse_json_object(
                path.read_text(encoding="utf-8"),
                what="STOP monotonic binding receipt",
            )
        except (OSError, UnicodeError) as exc:
            raise ExecutionStopIntegrityError(
                "cannot read STOP monotonic binding receipt"
            ) from exc
        if set(raw) != _MONOTONIC_RECEIPT_KEYS:
            raise ExecutionStopIntegrityError(
                "STOP monotonic binding receipt schema is invalid"
            )
        expected = self._monotonic_receipt_payload(
            authority,
            semantic_binding_sha256=semantic_binding_sha256,
        )
        if raw != expected:
            raise ExecutionStopIntegrityError(
                "STOP monotonic binding receipt identity or digest mismatch"
            )
        return True

    def _ensure_monotonic_receipt_unlocked(
        self,
        authority: MonotonicWorkspaceAuthority,
        *,
        semantic_binding_sha256: str,
    ) -> None:
        if self._read_monotonic_receipt_unlocked(
            authority,
            semantic_binding_sha256=semantic_binding_sha256,
        ):
            return
        path = self._monotonic_receipt_path(authority)
        payload = self._monotonic_receipt_payload(
            authority,
            semantic_binding_sha256=semantic_binding_sha256,
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_BINARY", 0)
            )
            if os.name != "nt":
                flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, 0o600)
            try:
                with os.fdopen(
                    descriptor,
                    "w",
                    encoding="utf-8",
                    newline="\n",
                ) as handle:
                    descriptor = -1
                    handle.write(_canonical(payload))
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            _sync_directory(path.parent)
            if path.parent.parent != authority.authority_root:
                _sync_directory(path.parent.parent)
            _sync_directory(authority.authority_root)
        except FileExistsError:
            if not self._read_monotonic_receipt_unlocked(
                authority,
                semantic_binding_sha256=semantic_binding_sha256,
            ):
                raise ExecutionStopIntegrityError(
                    "STOP monotonic binding receipt creation raced without evidence"
                )
        except ExecutionStopAuthorityError:
            raise
        except OSError as exc:
            raise ExecutionStopIntegrityError(
                "cannot durably persist STOP monotonic binding receipt"
            ) from exc

    def _monotonic_state_digest(self, record: dict[str, Any]) -> str:
        return _digest(
            {
                "schema": _MONOTONIC_STATE_SCHEMA,
                "schema_version": _MONOTONIC_STATE_VERSION,
                "state_key": self._monotonic_key(self.path),
                "revision": record["revision"],
                "mode": record["mode"],
                "record_sha256": record["record_sha256"],
            }
        )

    @staticmethod
    def _monotonic_tx_id(
        *,
        operation: str,
        observed_state_sha256: str | None,
        intended_state_sha256: str,
        semantic_binding_sha256: str,
        authority_tip_sha256: str | None,
    ) -> str:
        return _digest(
            {
                "operation": operation,
                "observed_state_sha256": observed_state_sha256,
                "intended_state_sha256": intended_state_sha256,
                "semantic_binding_sha256": semantic_binding_sha256,
                "authority_tip_sha256": authority_tip_sha256,
            }
        )

    @staticmethod
    def _raise_monotonic_error(exc: MonotonicWorkspaceAuthorityError) -> None:
        raise ExecutionStopIntegrityError(
            f"STOP monotonic authority rejected local state: {exc}"
        ) from exc

    def _ensure_monotonic_current_unlocked(
        self,
        records: list[dict[str, Any]],
        *,
        adopt_if_missing: bool,
    ) -> None:
        observed = (
            None if not records else self._monotonic_state_digest(records[-1])
        )
        binding = self._monotonic_binding()
        try:
            authority = self._monotonic_authority()
            history = authority.read_history()
            receipt_exists = self._read_monotonic_receipt_unlocked(
                authority,
                semantic_binding_sha256=binding,
            )
            if not history:
                if receipt_exists:
                    raise ExecutionStopIntegrityError(
                        "STOP monotonic authority history is missing after prior binding"
                    )
                if observed is None:
                    return
                if not adopt_if_missing:
                    raise ExecutionStopIntegrityError(
                        "STOP state exists without independent monotonic authority"
                    )
                tx_id = self._monotonic_tx_id(
                    operation="ADOPT_VALIDATED_BASELINE",
                    observed_state_sha256=None,
                    intended_state_sha256=observed,
                    semantic_binding_sha256=binding,
                    authority_tip_sha256=None,
                )
                authority.prepare(
                    tx_id=tx_id,
                    observed_state_sha256=None,
                    intended_state_sha256=observed,
                    semantic_binding_sha256=binding,
                )
                self._ensure_monotonic_receipt_unlocked(
                    authority,
                    semantic_binding_sha256=binding,
                )
                authority.commit(
                    tx_id=tx_id,
                    observed_state_sha256=observed,
                    semantic_binding_sha256=binding,
                )
                return

            if not receipt_exists:
                self._ensure_monotonic_receipt_unlocked(
                    authority,
                    semantic_binding_sha256=binding,
                )
            latest = history[-1]
            if (
                latest.phase is AuthorityPhase.PREPARE
                and observed == latest.intended_state_sha256
            ):
                authority.recover(
                    observed_state_sha256=observed,
                    tx_id=latest.tx_id,
                    semantic_binding_sha256=binding,
                )
            else:
                authority.recover(observed_state_sha256=observed)
        except MonotonicWorkspaceAuthorityError as exc:
            self._raise_monotonic_error(exc)

    def _prepare_monotonic_transition_unlocked(
        self,
        *,
        records: list[dict[str, Any]],
        record: dict[str, Any],
    ) -> tuple[MonotonicWorkspaceAuthority, str, str, str]:
        self._ensure_monotonic_current_unlocked(
            records,
            adopt_if_missing=True,
        )
        observed = (
            None if not records else self._monotonic_state_digest(records[-1])
        )
        intended = self._monotonic_state_digest(record)
        binding = self._monotonic_binding()
        try:
            authority = self._monotonic_authority()
            history = authority.read_history()
            tip = None if not history else history[-1].record_sha256
            tx_id = self._monotonic_tx_id(
                operation="APPEND_STOP_AUTHORITY_RECORD",
                observed_state_sha256=observed,
                intended_state_sha256=intended,
                semantic_binding_sha256=binding,
                authority_tip_sha256=tip,
            )
            authority.prepare(
                tx_id=tx_id,
                observed_state_sha256=observed,
                intended_state_sha256=intended,
                semantic_binding_sha256=binding,
            )
            self._ensure_monotonic_receipt_unlocked(
                authority,
                semantic_binding_sha256=binding,
            )
            return authority, tx_id, binding, intended
        except MonotonicWorkspaceAuthorityError as exc:
            self._raise_monotonic_error(exc)

    def _commit_monotonic_transition_unlocked(
        self,
        *,
        authority: MonotonicWorkspaceAuthority,
        tx_id: str,
        binding: str,
        intended_state_sha256: str,
    ) -> None:
        records = self._read_journal_unlocked()
        if not records:
            raise ExecutionStopIntegrityError(
                "STOP transition published no durable local state"
            )
        observed = self._monotonic_state_digest(records[-1])
        if observed != intended_state_sha256:
            raise ExecutionStopIntegrityError(
                "STOP transition local state differs from monotonic PREPARE"
            )
        try:
            authority.commit(
                tx_id=tx_id,
                observed_state_sha256=observed,
                semantic_binding_sha256=binding,
            )
        except MonotonicWorkspaceAuthorityError as exc:
            self._raise_monotonic_error(exc)

    def _read_journal_unlocked(
        self,
        *,
        require_anchor_match: bool = True,
        allow_missing_anchor: bool = False,
    ) -> list[dict[str, Any]]:
        journal_exists = self.path.exists()
        anchor_exists = self._anchor_path.exists()
        if not journal_exists and not anchor_exists:
            return []
        if journal_exists != anchor_exists:
            if not (
                allow_missing_anchor
                and journal_exists
                and not anchor_exists
            ):
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

        if not anchor_exists:
            return records

        anchor = self._read_anchor_unlocked()
        latest = records[-1]
        if require_anchor_match and (
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

    def _rewrite_journal_unlocked(
        self, records: list[dict[str, Any]]
    ) -> None:
        if not records:
            raise ExecutionStopIntegrityError(
                "STOP authority recovery cannot erase the complete journal"
            )
        encoded = "".join(_canonical(record) + "\n" for record in records)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=self.path.parent,
                prefix=self.path.name + ".",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
                temp_path = Path(handle.name)
            os.replace(temp_path, self.path)
            temp_path = None
            _sync_directory(self.path.parent)
        except OSError as exc:
            raise ExecutionStopIntegrityError(
                "STOP authority journal recovery durability barrier failed"
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
        authority, tx_id, binding, intended_state_sha256 = (
            self._prepare_monotonic_transition_unlocked(
                records=records,
                record=record,
            )
        )
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
        self._commit_monotonic_transition_unlocked(
            authority=authority,
            tx_id=tx_id,
            binding=binding,
            intended_state_sha256=intended_state_sha256,
        )
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

    def recover_torn_transition(self) -> ExecutionAuthorityState:
        """Recover one complete journal record that was not durably anchored.

        Recovery is intentionally asymmetric. A complete unanchored STOPPED
        record may be committed because it can only narrow execution authority.
        A complete unanchored ARMED record is discarded back to the already
        anchored prefix and is never promoted automatically.
        """

        with self._authority_operation_lock():
            records = self._read_journal_unlocked(
                require_anchor_match=False,
                allow_missing_anchor=True,
            )
            if not records:
                self._ensure_monotonic_current_unlocked(
                    records,
                    adopt_if_missing=False,
                )
                raise ExecutionStopStateError(
                    "STOP authority is missing; there is no torn transition to recover"
                )

            if not self._anchor_path.exists():
                if (
                    len(records) != 1
                    or records[0]["revision"] != 1
                    or records[0]["previous_sha256"] is not None
                    or records[0]["mode"] != ExecutionAuthorityMode.STOPPED.value
                ):
                    raise ExecutionStopIntegrityError(
                        "STOP authority recovery cannot establish an ambiguous first anchor"
                    )
                initial = records[0]
                self._write_anchor_unlocked(
                    revision=1,
                    record_sha256=initial["record_sha256"],
                )
                recovered = self._read_journal_unlocked()
                self._ensure_monotonic_current_unlocked(
                    recovered,
                    adopt_if_missing=True,
                )
                return self._state_from_record(recovered[-1])

            anchor = self._read_anchor_unlocked()
            anchor_revision = anchor["revision"]
            if anchor_revision > len(records):
                raise ExecutionStopIntegrityError(
                    "STOP authority anchor points beyond the journal"
                )

            anchored = records[anchor_revision - 1]
            if anchored["record_sha256"] != anchor["record_sha256"]:
                raise ExecutionStopIntegrityError(
                    "STOP authority anchor does not match its journal prefix"
                )

            suffix_count = len(records) - anchor_revision
            if suffix_count == 0:
                self._ensure_monotonic_current_unlocked(
                    records,
                    adopt_if_missing=True,
                )
                return self._state_from_record(anchored)
            if suffix_count != 1:
                raise ExecutionStopIntegrityError(
                    "STOP authority recovery requires exactly one unanchored record"
                )

            candidate = records[-1]
            if (
                candidate["revision"] != anchor_revision + 1
                or candidate["previous_sha256"] != anchored["record_sha256"]
            ):
                raise ExecutionStopIntegrityError(
                    "STOP authority recovery suffix does not extend the anchor"
                )

            mode = ExecutionAuthorityMode(candidate["mode"])
            if mode is ExecutionAuthorityMode.STOPPED:
                self._write_anchor_unlocked(
                    revision=candidate["revision"],
                    record_sha256=candidate["record_sha256"],
                )
            else:
                self._rewrite_journal_unlocked(records[:anchor_revision])

            recovered = self._read_journal_unlocked()
            self._ensure_monotonic_current_unlocked(
                recovered,
                adopt_if_missing=True,
            )
            return self._state_from_record(recovered[-1])

    def _current_unlocked(self) -> ExecutionAuthorityState:
        """Return current durable STOP state while the canonical locks are held."""

        records = self._read_journal_unlocked()
        if not records:
            self._ensure_monotonic_current_unlocked(
                records,
                adopt_if_missing=False,
            )
            raise ExecutionStopStateError(
                "STOP authority is missing; execution remains stopped"
            )
        self._ensure_monotonic_current_unlocked(
            records,
            adopt_if_missing=True,
        )
        return self._state_from_record(records[-1])

    def current(self) -> ExecutionAuthorityState:
        with self._authority_operation_lock():
            return self._current_unlocked()

    @contextmanager
    def admission_lease(self) -> Iterator[ExecutionAuthorityState]:
        """Hold exact ARMED authority across one irreversible execution effect.

        The existing thread and cross-process STOP locks remain held for the whole
        context.  A concurrent STOP/ARM command therefore linearizes either before
        admission or after the caller leaves this lease; it cannot commit between
        the positive ARMED check and the protected provider-write boundary.
        """

        if ExecutionStopAuthority is not _CANONICAL_ADMISSION_AUTHORITY_CLASS:
            raise ExecutionStopIntegrityError(
                "canonical execution admission authority class changed"
            )
        live_operation_lock = getattr(
            _CANONICAL_ADMISSION_AUTHORITY_CLASS,
            "_authority_operation_lock",
            None,
        )
        live_current_unlocked = getattr(
            _CANONICAL_ADMISSION_AUTHORITY_CLASS,
            "_current_unlocked",
            None,
        )
        if (
            live_operation_lock is not _CANONICAL_ADMISSION_OPERATION_LOCK
            or getattr(live_operation_lock, "__code__", None)
            is not _CANONICAL_ADMISSION_OPERATION_LOCK_CODE
            or live_current_unlocked is not _CANONICAL_ADMISSION_CURRENT_UNLOCKED
            or getattr(live_current_unlocked, "__code__", None)
            is not _CANONICAL_ADMISSION_CURRENT_UNLOCKED_CODE
        ):
            raise ExecutionStopIntegrityError(
                "canonical execution admission lower dispatch changed"
            )

        with _CANONICAL_ADMISSION_OPERATION_LOCK(self):
            state = _CANONICAL_ADMISSION_CURRENT_UNLOCKED(self)
            if state.mode is not ExecutionAuthorityMode.ARMED:
                raise ExecutionStoppedError(
                    f"execution STOP is active at revision {state.revision}: "
                    f"{state.reason}"
                )
            yield state

    def initialize_stopped(
        self,
        *,
        operator_id: str,
        reason: str,
        command_id: str | None = None,
    ) -> ExecutionAuthorityState:
        with self._authority_operation_lock():
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
        with self._authority_operation_lock():
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
        with self._authority_operation_lock():
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


# admission_lease is consumed across irreversible provider effects. Freeze the lower
# class dispatch that establishes its linearization lock and re-resolves current STOP
# state, so replacing those methods cannot preserve the public lease surface while
# bypassing durable authority or the STOP-vs-write ordering.
_CANONICAL_ADMISSION_AUTHORITY_CLASS = ExecutionStopAuthority
_CANONICAL_ADMISSION_OPERATION_LOCK = ExecutionStopAuthority._authority_operation_lock
_CANONICAL_ADMISSION_OPERATION_LOCK_CODE = getattr(
    _CANONICAL_ADMISSION_OPERATION_LOCK,
    "__code__",
    None,
)
_CANONICAL_ADMISSION_CURRENT_UNLOCKED = ExecutionStopAuthority._current_unlocked
_CANONICAL_ADMISSION_CURRENT_UNLOCKED_CODE = getattr(
    _CANONICAL_ADMISSION_CURRENT_UNLOCKED,
    "__code__",
    None,
)
