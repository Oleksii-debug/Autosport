from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import threading
import uuid
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator

from . import monotonic_workspace_authority as _monotonic_authority_module
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

    @staticmethod
    def _product_monotonic_authority_root() -> Path:
        """Resolve the supported STOP machine-state root without env retargeting."""

        if os.name == "nt":
            try:
                import ctypes

                buffer = ctypes.create_unicode_buffer(32768)
                result = ctypes.windll.shell32.SHGetFolderPathW(  # type: ignore[attr-defined]
                    None,
                    0x001C,  # CSIDL_LOCAL_APPDATA
                    None,
                    0,
                    buffer,
                )
            except (AttributeError, OSError, ValueError) as exc:
                raise ExecutionStopIntegrityError(
                    "cannot resolve product-owned Windows STOP authority root"
                ) from exc
            if result != 0 or not buffer.value:
                raise ExecutionStopIntegrityError(
                    "cannot resolve product-owned Windows STOP authority root"
                )
            base = Path(buffer.value)
            relative = (
                Path("Autosport")
                / "application-state"
                / "monotonic-authority-v1"
            )
        else:
            try:
                import pwd

                home = pwd.getpwuid(os.getuid()).pw_dir
            except (AttributeError, ImportError, KeyError, OSError) as exc:
                raise ExecutionStopIntegrityError(
                    "cannot resolve product-owned POSIX STOP authority root"
                ) from exc
            base = Path(home) / ".local" / "state"
            relative = Path("autosport") / "monotonic-authority-v1"

        if not base.is_absolute():
            raise ExecutionStopIntegrityError(
                "product-owned STOP authority root must be absolute"
            )
        return base / relative

    def _monotonic_authority(self) -> MonotonicWorkspaceAuthority:
        absolute = Path(os.path.abspath(os.fspath(self.path)))
        expected_workspace = absolute.parent
        expected_root = _CANONICAL_ADMISSION_PRODUCT_MONOTONIC_ROOT
        expected_key = _CANONICAL_ADMISSION_MONOTONIC_KEY(absolute)
        _CANONICAL_ADMISSION_REQUIRE_MONOTONIC_MODULE_GRAPH()
        authority = _CANONICAL_ADMISSION_MONOTONIC_AUTHORITY_CLASS(
            workspace=expected_workspace,
            domain=_MONOTONIC_DOMAIN,
            key=expected_key,
            authority_root=expected_root,
        )
        _CANONICAL_ADMISSION_REQUIRE_MONOTONIC_MODULE_GRAPH()
        return _CANONICAL_ADMISSION_REQUIRE_MONOTONIC_COORDINATES(
            authority,
            expected_workspace=expected_workspace,
            expected_root=expected_root,
            expected_key=expected_key,
        )

    def _stable_serialization_lock_path(self) -> Path:
        """Return the machine-root lock shared by every STOP authority instance."""

        authority = _CANONICAL_ADMISSION_MONOTONIC_AUTHORITY(self)
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
            stable_lock_path = _CANONICAL_ADMISSION_STABLE_SERIALIZATION_LOCK_PATH(self)
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
                "state_key": _CANONICAL_ADMISSION_MONOTONIC_KEY(self.path),
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
        path = _CANONICAL_ADMISSION_MONOTONIC_RECEIPT_PATH(authority)
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
        expected = _CANONICAL_ADMISSION_MONOTONIC_RECEIPT_PAYLOAD(
            self,
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
        if _CANONICAL_ADMISSION_READ_MONOTONIC_RECEIPT_UNLOCKED(
            self,
            authority,
            semantic_binding_sha256=semantic_binding_sha256,
        ):
            return
        path = _CANONICAL_ADMISSION_MONOTONIC_RECEIPT_PATH(authority)
        payload = _CANONICAL_ADMISSION_MONOTONIC_RECEIPT_PAYLOAD(
            self,
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
            if not _CANONICAL_ADMISSION_READ_MONOTONIC_RECEIPT_UNLOCKED(
                self,
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
                "state_key": _CANONICAL_ADMISSION_MONOTONIC_KEY(self.path),
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
            None if not records else _CANONICAL_ADMISSION_MONOTONIC_STATE_DIGEST(self, records[-1])
        )
        binding = _CANONICAL_ADMISSION_MONOTONIC_BINDING(self)
        try:
            authority = _CANONICAL_ADMISSION_MONOTONIC_AUTHORITY(self)
            history = _CANONICAL_ADMISSION_MONOTONIC_READ_HISTORY(authority)
            receipt_exists = _CANONICAL_ADMISSION_READ_MONOTONIC_RECEIPT_UNLOCKED(
                self,
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
                tx_id = _CANONICAL_ADMISSION_MONOTONIC_TX_ID(
                    operation="ADOPT_VALIDATED_BASELINE",
                    observed_state_sha256=None,
                    intended_state_sha256=observed,
                    semantic_binding_sha256=binding,
                    authority_tip_sha256=None,
                )
                _CANONICAL_ADMISSION_MONOTONIC_PREPARE(
                    authority,
                    tx_id=tx_id,
                    observed_state_sha256=None,
                    intended_state_sha256=observed,
                    semantic_binding_sha256=binding,
                )
                _CANONICAL_ADMISSION_ENSURE_MONOTONIC_RECEIPT_UNLOCKED(
                    self,
                    authority,
                    semantic_binding_sha256=binding,
                )
                _CANONICAL_ADMISSION_MONOTONIC_COMMIT(
                    authority,
                    tx_id=tx_id,
                    observed_state_sha256=observed,
                    semantic_binding_sha256=binding,
                )
                return

            if not receipt_exists:
                _CANONICAL_ADMISSION_ENSURE_MONOTONIC_RECEIPT_UNLOCKED(
                    self,
                    authority,
                    semantic_binding_sha256=binding,
                )
            latest = history[-1]
            if (
                latest.phase is AuthorityPhase.PREPARE
                and observed == latest.intended_state_sha256
            ):
                _CANONICAL_ADMISSION_MONOTONIC_RECOVER(
                    authority,
                    observed_state_sha256=observed,
                    tx_id=latest.tx_id,
                    semantic_binding_sha256=binding,
                )
            else:
                _CANONICAL_ADMISSION_MONOTONIC_RECOVER(
                    authority,
                    observed_state_sha256=observed,
                )
        except MonotonicWorkspaceAuthorityError as exc:
            _CANONICAL_ADMISSION_RAISE_MONOTONIC_ERROR(exc)

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
            history = _CANONICAL_ADMISSION_MONOTONIC_READ_HISTORY(authority)
            tip = None if not history else history[-1].record_sha256
            tx_id = self._monotonic_tx_id(
                operation="APPEND_STOP_AUTHORITY_RECORD",
                observed_state_sha256=observed,
                intended_state_sha256=intended,
                semantic_binding_sha256=binding,
                authority_tip_sha256=tip,
            )
            _CANONICAL_ADMISSION_MONOTONIC_PREPARE(
                authority,
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
            _CANONICAL_ADMISSION_MONOTONIC_COMMIT(
                authority,
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

        anchor = _CANONICAL_ADMISSION_READ_ANCHOR_UNLOCKED(self)
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

        records = _CANONICAL_ADMISSION_READ_JOURNAL_UNLOCKED(self)
        if not records:
            _CANONICAL_ADMISSION_ENSURE_MONOTONIC_CURRENT_UNLOCKED(
                self,
                records,
                adopt_if_missing=False,
            )
            raise ExecutionStopStateError(
                "STOP authority is missing; execution remains stopped"
            )
        _CANONICAL_ADMISSION_ENSURE_MONOTONIC_CURRENT_UNLOCKED(
            self,
            records,
            adopt_if_missing=True,
        )
        return _CANONICAL_ADMISSION_STATE_FROM_RECORD(records[-1])

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

        _require_canonical_admission_graph()
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


def _monotonic_dependency_callable_state(
    value: object,
) -> tuple[object | None, ...]:
    wrapped = getattr(value, "__wrapped__", None)
    descriptor_function = getattr(value, "__func__", None)
    property_getter = value.fget if type(value) is property else None
    property_setter = value.fset if type(value) is property else None
    property_deleter = value.fdel if type(value) is property else None
    return (
        getattr(value, "__code__", None),
        wrapped,
        getattr(wrapped, "__code__", None),
        descriptor_function,
        getattr(descriptor_function, "__code__", None),
        property_getter,
        getattr(property_getter, "__code__", None),
        property_setter,
        getattr(property_setter, "__code__", None),
        property_deleter,
        getattr(property_deleter, "__code__", None),
    )


_CANONICAL_ADMISSION_MONOTONIC_MODULE = _monotonic_authority_module
_CANONICAL_ADMISSION_MONOTONIC_MODULE_GRAPH = tuple(
    (
        name,
        value,
        _monotonic_dependency_callable_state(value),
    )
    for name, value in sorted(
        vars(_CANONICAL_ADMISSION_MONOTONIC_MODULE).items()
    )
)
_CANONICAL_ADMISSION_MONOTONIC_DEPENDENCY_CLASSES = tuple(
    (
        name,
        value,
        tuple(
            (
                member_name,
                member,
                _monotonic_dependency_callable_state(member),
            )
            for member_name, member in value.__dict__.items()
        ),
    )
    for name, value in (
        (
            "MonotonicWorkspaceAuthority",
            _monotonic_authority_module.MonotonicWorkspaceAuthority,
        ),
        (
            "WorkspaceIdentityBinding",
            _monotonic_authority_module.WorkspaceIdentityBinding,
        ),
        (
            "WorkspaceEconomicLock",
            _monotonic_authority_module.WorkspaceEconomicLock,
        ),
    )
)
# pathlib dispatch is part of the transitive monotonic read/durability graph.
# Freezing only the module-level Path object is insufficient: its inherited class
# members can be rebound while Path identity remains unchanged, allowing a forged
# directory view to hide a newer STOP authority suffix.
_CANONICAL_ADMISSION_MONOTONIC_PATH_CLASS = _monotonic_authority_module.Path
_CANONICAL_ADMISSION_MONOTONIC_CONCRETE_PATH_CLASS = type(
    _CANONICAL_ADMISSION_MONOTONIC_PATH_CLASS(".")
)
_CANONICAL_ADMISSION_MONOTONIC_PATH_MRO_GRAPH = tuple(
    (
        path_class,
        tuple(
            (
                member_name,
                member,
                _monotonic_dependency_callable_state(member),
            )
            for member_name, member in path_class.__dict__.items()
        ),
    )
    for path_class in _CANONICAL_ADMISSION_MONOTONIC_CONCRETE_PATH_CLASS.__mro__
    if path_class is not object
)


_CANONICAL_ADMISSION_MONOTONIC_AUTHORITY_ID = (
    _monotonic_authority_module.AUTHORITY_ID
)
_CANONICAL_ADMISSION_MONOTONIC_BINDING_CLASS = (
    _monotonic_authority_module.WorkspaceIdentityBinding
)


def _require_canonical_monotonic_module_graph() -> None:
    live_graph = vars(_CANONICAL_ADMISSION_MONOTONIC_MODULE)
    if len(live_graph) != len(_CANONICAL_ADMISSION_MONOTONIC_MODULE_GRAPH):
        raise ExecutionStopIntegrityError(
            "canonical monotonic authority module dependency graph changed"
        )
    for name, expected, expected_state in (
        _CANONICAL_ADMISSION_MONOTONIC_MODULE_GRAPH
    ):
        live_value = live_graph.get(name)
        if (
            live_value is not expected
            or _monotonic_dependency_callable_state(live_value)
            != expected_state
        ):
            raise ExecutionStopIntegrityError(
                "canonical monotonic authority module dependency graph changed"
            )

    for class_name, expected_class, expected_members in (
        _CANONICAL_ADMISSION_MONOTONIC_DEPENDENCY_CLASSES
    ):
        live_class = live_graph.get(class_name)
        if live_class is not expected_class:
            raise ExecutionStopIntegrityError(
                "canonical monotonic authority dependency class changed"
            )
        live_members = live_class.__dict__
        if len(live_members) != len(expected_members):
            raise ExecutionStopIntegrityError(
                "canonical monotonic authority dependency class graph changed"
            )
        for member_name, expected_member, expected_state in expected_members:
            live_member = live_members.get(member_name)
            if (
                live_member is not expected_member
                or _monotonic_dependency_callable_state(live_member)
                != expected_state
            ):
                raise ExecutionStopIntegrityError(
                    "canonical monotonic authority dependency class graph changed"
                )

    expected_path_mro = tuple(
        path_class
        for path_class, _members in _CANONICAL_ADMISSION_MONOTONIC_PATH_MRO_GRAPH
    )
    live_path_mro = tuple(
        path_class
        for path_class in _CANONICAL_ADMISSION_MONOTONIC_CONCRETE_PATH_CLASS.__mro__
        if path_class is not object
    )
    if (
        live_graph.get("Path") is not _CANONICAL_ADMISSION_MONOTONIC_PATH_CLASS
        or live_path_mro != expected_path_mro
    ):
        raise ExecutionStopIntegrityError(
            "canonical monotonic authority filesystem dispatch graph changed"
        )
    for path_class, expected_members in (
        _CANONICAL_ADMISSION_MONOTONIC_PATH_MRO_GRAPH
    ):
        live_members = path_class.__dict__
        if len(live_members) != len(expected_members):
            raise ExecutionStopIntegrityError(
                "canonical monotonic authority filesystem dispatch graph changed"
            )
        for member_name, expected_member, expected_state in expected_members:
            live_member = live_members.get(member_name)
            if (
                live_member is not expected_member
                or _monotonic_dependency_callable_state(live_member)
                != expected_state
            ):
                raise ExecutionStopIntegrityError(
                    "canonical monotonic authority filesystem dispatch graph changed"
                )


def _require_monotonic_authority_coordinates(
    authority: MonotonicWorkspaceAuthority,
    *,
    expected_workspace: Path,
    expected_root: Path,
    expected_key: str,
) -> MonotonicWorkspaceAuthority:
    if type(authority) is not MonotonicWorkspaceAuthority:
        raise ExecutionStopIntegrityError(
            "canonical monotonic authority type changed"
        )
    binding = authority.workspace_binding
    if type(binding) is not _CANONICAL_ADMISSION_MONOTONIC_BINDING_CLASS:
        raise ExecutionStopIntegrityError(
            "canonical monotonic workspace binding type changed"
        )

    expected_locator = os.path.normcase(
        os.path.normpath(str(expected_workspace))
    )
    expected_locator_sha256 = hashlib.sha256(
        expected_locator.encode("utf-8")
    ).hexdigest()
    expected_workspace_marker = (
        expected_workspace
        / ".autosport"
        / "monotonic-workspace-binding.json"
    )
    expected_path_binding = (
        expected_root
        / "workspace-bindings"
        / expected_locator_sha256[:2]
        / f"{expected_locator_sha256}.json"
    )

    workspace_instance_id = authority.workspace_instance_id
    if (
        type(workspace_instance_id) is not str
        or not workspace_instance_id
        or workspace_instance_id != binding.workspace_instance_id
    ):
        raise ExecutionStopIntegrityError(
            "canonical monotonic workspace identity changed"
        )

    namespace_material = "\0".join(
        (
            _CANONICAL_ADMISSION_MONOTONIC_AUTHORITY_ID,
            workspace_instance_id,
            _MONOTONIC_DOMAIN,
            expected_key,
        )
    ).encode("utf-8")
    expected_namespace = hashlib.sha256(namespace_material).hexdigest()
    expected_journal_dir = (
        expected_root
        / "journals"
        / expected_namespace[:2]
        / expected_namespace
    )
    expected_records_dir = expected_journal_dir / "records"
    expected_namespace_marker = (
        expected_root
        / "namespace-bindings"
        / expected_namespace[:2]
        / f"{expected_namespace}.json"
    )

    if (
        authority.workspace != expected_workspace
        or authority.authority_root != expected_root
        or authority.domain != _MONOTONIC_DOMAIN
        or authority.key != expected_key
        or authority.namespace_sha256 != expected_namespace
        or authority.journal_dir != expected_journal_dir
        or authority.records_dir != expected_records_dir
        or authority.namespace_marker_path != expected_namespace_marker
        or authority.workspace_binding_path != expected_workspace_marker
        or binding.workspace != expected_workspace
        or binding.authority_root != expected_root
        or binding.workspace_marker_path != expected_workspace_marker
        or binding.path_binding_path != expected_path_binding
        or binding.workspace_locator != expected_locator
        or binding.workspace_locator_sha256 != expected_locator_sha256
    ):
        raise ExecutionStopIntegrityError(
            "canonical monotonic authority coordinates changed"
        )
    return authority


_CANONICAL_ADMISSION_REQUIRE_MONOTONIC_MODULE_GRAPH = (
    _require_canonical_monotonic_module_graph
)
_CANONICAL_ADMISSION_REQUIRE_MONOTONIC_COORDINATES = (
    _require_monotonic_authority_coordinates
)


# Freeze the public MonotonicWorkspaceAuthority entry points consumed by STOP
# state verification/transitions. Calling these exact unbound functions prevents
# a class-attribute rebind from silently changing authority semantics.
_CANONICAL_ADMISSION_MONOTONIC_AUTHORITY_CLASS = MonotonicWorkspaceAuthority
_CANONICAL_ADMISSION_MONOTONIC_READ_HISTORY = (
    MonotonicWorkspaceAuthority.read_history
)
_CANONICAL_ADMISSION_MONOTONIC_PREPARE = MonotonicWorkspaceAuthority.prepare
_CANONICAL_ADMISSION_MONOTONIC_COMMIT = MonotonicWorkspaceAuthority.commit
_CANONICAL_ADMISSION_MONOTONIC_RECOVER = MonotonicWorkspaceAuthority.recover
_CANONICAL_ADMISSION_MONOTONIC_CLASS_GRAPH = tuple(
    (name, value)
    for name, value in MonotonicWorkspaceAuthority.__dict__.items()
)
_CANONICAL_ADMISSION_MONOTONIC_CLASS_CODES = tuple(
    (name, getattr(value, "__code__", None))
    for name, value in _CANONICAL_ADMISSION_MONOTONIC_CLASS_GRAPH
)


# admission_lease is consumed across irreversible provider effects. Capture the
# complete class-level helper graph reachable from its linearization lock and durable
# current-state verification. Security-critical edges below the lease invoke these
# exact unbound callables rather than re-resolving self.<helper> dynamically.
_CANONICAL_ADMISSION_AUTHORITY_CLASS = ExecutionStopAuthority
_CANONICAL_ADMISSION_MONOTONIC_KEY = ExecutionStopAuthority._monotonic_key


def _build_product_root_bound_monotonic_authority():
    """Bind every STOP authority read/write to one import-composed product root."""

    product_root = ExecutionStopAuthority._product_monotonic_authority_root()
    path_class = Path
    path_abspath = os.path.abspath
    fspath = os.fspath
    monotonic_key = ExecutionStopAuthority._monotonic_key
    require_module_graph = _CANONICAL_ADMISSION_REQUIRE_MONOTONIC_MODULE_GRAPH
    authority_class = MonotonicWorkspaceAuthority
    require_coordinates = _CANONICAL_ADMISSION_REQUIRE_MONOTONIC_COORDINATES
    monotonic_domain = _MONOTONIC_DOMAIN

    def monotonic_authority(
        self: ExecutionStopAuthority,
    ) -> MonotonicWorkspaceAuthority:
        absolute = path_class(path_abspath(fspath(self.path)))
        expected_workspace = absolute.parent
        expected_key = monotonic_key(absolute)
        require_module_graph()
        authority = authority_class(
            workspace=expected_workspace,
            domain=monotonic_domain,
            key=expected_key,
            authority_root=product_root,
        )
        require_module_graph()
        return require_coordinates(
            authority,
            expected_workspace=expected_workspace,
            expected_root=product_root,
            expected_key=expected_key,
        )

    return product_root, monotonic_authority


# Resolve the supported product root once at import composition time, then bind it
# into the canonical authority callable itself. The exported Path below is evidence,
# not a live authority input: rebinding it cannot retarget current()/decision()/
# assert_execution_allowed() or the sealed provider admission lease.
(
    _CANONICAL_ADMISSION_PRODUCT_MONOTONIC_ROOT,
    _product_root_bound_monotonic_authority,
) = _build_product_root_bound_monotonic_authority()
ExecutionStopAuthority._monotonic_authority = _product_root_bound_monotonic_authority
del _product_root_bound_monotonic_authority
del _build_product_root_bound_monotonic_authority

def _build_sealed_authority_coordinate(name: str):
    """Keep one construction-bound STOP pathname outside mutable instance state."""

    values: weakref.WeakKeyDictionary[ExecutionStopAuthority, Path] = (
        weakref.WeakKeyDictionary()
    )
    path_type = Path
    integrity_error = ExecutionStopIntegrityError

    class SealedAuthorityCoordinate:
        __slots__ = ()

        def __get__(self, instance, owner=None):
            if instance is None:
                return self
            try:
                return values[instance]
            except KeyError as exc:
                raise integrity_error(
                    f"execution STOP authority {name} is unavailable"
                ) from exc

        def __set__(self, instance, value) -> None:
            if not isinstance(value, path_type):
                raise integrity_error(
                    f"execution STOP authority {name} must be a Path"
                )
            if instance in values:
                raise integrity_error(
                    "execution STOP authority construction coordinates are immutable"
                )
            values[instance] = value

        def __delete__(self, _instance) -> None:
            raise integrity_error(
                "execution STOP authority construction coordinates are immutable"
            )

    return SealedAuthorityCoordinate()


_CANONICAL_ADMISSION_PATH_DESCRIPTOR = _build_sealed_authority_coordinate("path")
_CANONICAL_ADMISSION_ANCHOR_PATH_DESCRIPTOR = _build_sealed_authority_coordinate(
    "_anchor_path"
)
_CANONICAL_ADMISSION_LOCK_PATH_DESCRIPTOR = _build_sealed_authority_coordinate(
    "_lock_path"
)
ExecutionStopAuthority.path = _CANONICAL_ADMISSION_PATH_DESCRIPTOR
ExecutionStopAuthority._anchor_path = _CANONICAL_ADMISSION_ANCHOR_PATH_DESCRIPTOR
ExecutionStopAuthority._lock_path = _CANONICAL_ADMISSION_LOCK_PATH_DESCRIPTOR
del _build_sealed_authority_coordinate


_CANONICAL_ADMISSION_PRODUCT_MONOTONIC_AUTHORITY_ROOT = (
    ExecutionStopAuthority._product_monotonic_authority_root
)
_CANONICAL_ADMISSION_MONOTONIC_AUTHORITY = ExecutionStopAuthority._monotonic_authority
_CANONICAL_ADMISSION_STABLE_SERIALIZATION_LOCK_PATH = (
    ExecutionStopAuthority._stable_serialization_lock_path
)
_CANONICAL_ADMISSION_OPERATION_LOCK = ExecutionStopAuthority._authority_operation_lock
_CANONICAL_ADMISSION_MONOTONIC_BINDING = ExecutionStopAuthority._monotonic_binding
_CANONICAL_ADMISSION_MONOTONIC_RECEIPT_PATH = (
    ExecutionStopAuthority._monotonic_receipt_path
)
_CANONICAL_ADMISSION_MONOTONIC_RECEIPT_PAYLOAD = (
    ExecutionStopAuthority._monotonic_receipt_payload
)
_CANONICAL_ADMISSION_READ_MONOTONIC_RECEIPT_UNLOCKED = (
    ExecutionStopAuthority._read_monotonic_receipt_unlocked
)
_CANONICAL_ADMISSION_ENSURE_MONOTONIC_RECEIPT_UNLOCKED = (
    ExecutionStopAuthority._ensure_monotonic_receipt_unlocked
)
_CANONICAL_ADMISSION_MONOTONIC_STATE_DIGEST = (
    ExecutionStopAuthority._monotonic_state_digest
)
_CANONICAL_ADMISSION_MONOTONIC_TX_ID = ExecutionStopAuthority._monotonic_tx_id
_CANONICAL_ADMISSION_RAISE_MONOTONIC_ERROR = (
    ExecutionStopAuthority._raise_monotonic_error
)
_CANONICAL_ADMISSION_ENSURE_MONOTONIC_CURRENT_UNLOCKED = (
    ExecutionStopAuthority._ensure_monotonic_current_unlocked
)
_CANONICAL_ADMISSION_READ_JOURNAL_UNLOCKED = (
    ExecutionStopAuthority._read_journal_unlocked
)
_CANONICAL_ADMISSION_READ_ANCHOR_UNLOCKED = (
    ExecutionStopAuthority._read_anchor_unlocked
)
_CANONICAL_ADMISSION_STATE_FROM_RECORD = ExecutionStopAuthority._state_from_record
_CANONICAL_ADMISSION_CURRENT_UNLOCKED = ExecutionStopAuthority._current_unlocked

_CANONICAL_ADMISSION_GRAPH = (
    ("path", _CANONICAL_ADMISSION_PATH_DESCRIPTOR),
    ("_anchor_path", _CANONICAL_ADMISSION_ANCHOR_PATH_DESCRIPTOR),
    ("_lock_path", _CANONICAL_ADMISSION_LOCK_PATH_DESCRIPTOR),
    ("_monotonic_key", _CANONICAL_ADMISSION_MONOTONIC_KEY),
    (
        "_product_monotonic_authority_root",
        _CANONICAL_ADMISSION_PRODUCT_MONOTONIC_AUTHORITY_ROOT,
    ),
    ("_monotonic_authority", _CANONICAL_ADMISSION_MONOTONIC_AUTHORITY),
    (
        "_stable_serialization_lock_path",
        _CANONICAL_ADMISSION_STABLE_SERIALIZATION_LOCK_PATH,
    ),
    ("_authority_operation_lock", _CANONICAL_ADMISSION_OPERATION_LOCK),
    ("_monotonic_binding", _CANONICAL_ADMISSION_MONOTONIC_BINDING),
    ("_monotonic_receipt_path", _CANONICAL_ADMISSION_MONOTONIC_RECEIPT_PATH),
    (
        "_monotonic_receipt_payload",
        _CANONICAL_ADMISSION_MONOTONIC_RECEIPT_PAYLOAD,
    ),
    (
        "_read_monotonic_receipt_unlocked",
        _CANONICAL_ADMISSION_READ_MONOTONIC_RECEIPT_UNLOCKED,
    ),
    (
        "_ensure_monotonic_receipt_unlocked",
        _CANONICAL_ADMISSION_ENSURE_MONOTONIC_RECEIPT_UNLOCKED,
    ),
    (
        "_monotonic_state_digest",
        _CANONICAL_ADMISSION_MONOTONIC_STATE_DIGEST,
    ),
    ("_monotonic_tx_id", _CANONICAL_ADMISSION_MONOTONIC_TX_ID),
    ("_raise_monotonic_error", _CANONICAL_ADMISSION_RAISE_MONOTONIC_ERROR),
    (
        "_ensure_monotonic_current_unlocked",
        _CANONICAL_ADMISSION_ENSURE_MONOTONIC_CURRENT_UNLOCKED,
    ),
    (
        "_read_journal_unlocked",
        _CANONICAL_ADMISSION_READ_JOURNAL_UNLOCKED,
    ),
    (
        "_read_anchor_unlocked",
        _CANONICAL_ADMISSION_READ_ANCHOR_UNLOCKED,
    ),
    ("_state_from_record", _CANONICAL_ADMISSION_STATE_FROM_RECORD),
    ("_current_unlocked", _CANONICAL_ADMISSION_CURRENT_UNLOCKED),
)
_CANONICAL_ADMISSION_GRAPH_CODES = tuple(
    (name, getattr(method, "__code__", None))
    for name, method in _CANONICAL_ADMISSION_GRAPH
)
_CANONICAL_ADMISSION_OPERATION_LOCK_GENERATOR = getattr(
    _CANONICAL_ADMISSION_OPERATION_LOCK,
    "__wrapped__",
    None,
)
_CANONICAL_ADMISSION_OPERATION_LOCK_GENERATOR_CODE = getattr(
    _CANONICAL_ADMISSION_OPERATION_LOCK_GENERATOR,
    "__code__",
    None,
)


def _require_canonical_admission_graph() -> None:
    if ExecutionStopAuthority is not _CANONICAL_ADMISSION_AUTHORITY_CLASS:
        raise ExecutionStopIntegrityError(
            "canonical execution admission authority class changed"
        )
    expected_codes = dict(_CANONICAL_ADMISSION_GRAPH_CODES)
    for method_name, expected_method in _CANONICAL_ADMISSION_GRAPH:
        live_method = getattr(
            _CANONICAL_ADMISSION_AUTHORITY_CLASS,
            method_name,
            None,
        )
        if (
            live_method is not expected_method
            or getattr(live_method, "__code__", None)
            is not expected_codes[method_name]
        ):
            raise ExecutionStopIntegrityError(
                "canonical execution admission helper graph changed"
            )

    _CANONICAL_ADMISSION_REQUIRE_MONOTONIC_MODULE_GRAPH()

    if (
        MonotonicWorkspaceAuthority
        is not _CANONICAL_ADMISSION_MONOTONIC_AUTHORITY_CLASS
    ):
        raise ExecutionStopIntegrityError(
            "canonical monotonic authority class changed"
        )
    live_monotonic_graph = MonotonicWorkspaceAuthority.__dict__
    expected_monotonic_codes = dict(
        _CANONICAL_ADMISSION_MONOTONIC_CLASS_CODES
    )
    if len(live_monotonic_graph) != len(
        _CANONICAL_ADMISSION_MONOTONIC_CLASS_GRAPH
    ):
        raise ExecutionStopIntegrityError(
            "canonical monotonic authority helper graph changed"
        )
    for method_name, expected_method in (
        _CANONICAL_ADMISSION_MONOTONIC_CLASS_GRAPH
    ):
        live_method = live_monotonic_graph.get(method_name)
        if (
            live_method is not expected_method
            or getattr(live_method, "__code__", None)
            is not expected_monotonic_codes[method_name]
        ):
            raise ExecutionStopIntegrityError(
                "canonical monotonic authority helper graph changed"
            )

    live_operation_generator = getattr(
        _CANONICAL_ADMISSION_OPERATION_LOCK,
        "__wrapped__",
        None,
    )
    if (
        _CANONICAL_ADMISSION_OPERATION_LOCK_GENERATOR is None
        or _CANONICAL_ADMISSION_OPERATION_LOCK_GENERATOR_CODE is None
        or live_operation_generator
        is not _CANONICAL_ADMISSION_OPERATION_LOCK_GENERATOR
        or getattr(live_operation_generator, "__code__", None)
        is not _CANONICAL_ADMISSION_OPERATION_LOCK_GENERATOR_CODE
    ):
        raise ExecutionStopIntegrityError(
            "canonical execution admission lock generator changed"
        )


def _build_public_stop_read_authority():
    """Remove instance virtual dispatch from public positive STOP reads."""

    require_admission_graph = _require_canonical_admission_graph
    operation_lock = _CANONICAL_ADMISSION_OPERATION_LOCK
    current_unlocked = _CANONICAL_ADMISSION_CURRENT_UNLOCKED
    authority_error = ExecutionStopAuthorityError
    stopped_error = ExecutionStoppedError
    armed_mode = ExecutionAuthorityMode.ARMED
    stopped_mode = ExecutionAuthorityMode.STOPPED
    decision_type = ExecutionAdmissionDecision
    integrity_error = ExecutionStopIntegrityError

    def current(
        self: ExecutionStopAuthority,
    ) -> ExecutionAuthorityState:
        require_admission_graph()
        with operation_lock(self):
            state = current_unlocked(self)
        require_admission_graph()
        return state

    def decision(
        self: ExecutionStopAuthority,
    ) -> ExecutionAdmissionDecision:
        try:
            state = current(self)
        except authority_error as exc:
            return decision_type(
                allowed=False,
                mode=stopped_mode,
                revision=None,
                reason=f"fail-closed: {exc}",
            )
        if state.mode is not armed_mode:
            return decision_type(
                allowed=False,
                mode=state.mode,
                revision=state.revision,
                reason=state.reason,
            )
        return decision_type(
            allowed=True,
            mode=state.mode,
            revision=state.revision,
            reason=state.reason,
        )

    def assert_execution_allowed(
        self: ExecutionStopAuthority,
    ) -> ExecutionAuthorityState:
        state = current(self)
        if state.mode is not armed_mode:
            raise stopped_error(
                f"execution STOP is active at revision {state.revision}: "
                f"{state.reason}"
            )
        return state

    def sealed_public_method(method):
        class SealedPublicMethod:
            __slots__ = ()

            def __get__(self, instance, owner=None):
                if instance is None:
                    return method
                return method.__get__(instance, owner)

            def __set__(self, _instance, _value) -> None:
                raise integrity_error(
                    "canonical public STOP authority method is immutable"
                )

            def __delete__(self, _instance) -> None:
                raise integrity_error(
                    "canonical public STOP authority method is immutable"
                )

        return SealedPublicMethod()

    return (
        sealed_public_method(current),
        sealed_public_method(decision),
        sealed_public_method(assert_execution_allowed),
    )


(
    _canonical_public_current,
    _canonical_public_decision,
    _canonical_public_assert_execution_allowed,
) = _build_public_stop_read_authority()
ExecutionStopAuthority.current = _canonical_public_current
ExecutionStopAuthority.decision = _canonical_public_decision
ExecutionStopAuthority.assert_execution_allowed = (
    _canonical_public_assert_execution_allowed
)
del _canonical_public_current
del _canonical_public_decision
del _canonical_public_assert_execution_allowed
del _build_public_stop_read_authority


def _build_sealed_admission_lease():
    """Bind positive STOP admission to the import-time canonical module graph.

    The provider write boundary captures admission_lease by function identity,
    but a function object still resolves ordinary module globals at call time.
    Keep the lease's security-critical dispatch and its verifier in closure state,
    and reject any pre-call rebinding of the canonical admission graph or its
    low-level durable/locking helpers.
    """

    module_globals = globals()
    # Positive admission is rare and security-critical. Freeze every module
    # binding that existed when the canonical lease was built, rather than
    # maintaining a hand-written allowlist that can miss a deeper helper
    # constant or callable. For callable bindings, identity alone is
    # insufficient: Python permits in-place __code__ / contextmanager
    # __wrapped__ mutation without rebinding the module name.
    empty_cell = object()

    def callable_state(value: object) -> tuple[
        object | None,
        object | None,
        object | None,
        tuple[object, ...] | None,
    ]:
        wrapped = getattr(value, "__wrapped__", None)
        closure = getattr(value, "__closure__", None)
        closure_state: tuple[object, ...] | None
        if closure is None:
            closure_state = None
        else:
            captured: list[object] = []
            for cell in closure:
                try:
                    captured.append(cell.cell_contents)
                except ValueError:
                    captured.append(empty_cell)
            closure_state = tuple(captured)
        return (
            getattr(value, "__code__", None),
            wrapped,
            getattr(wrapped, "__code__", None),
            closure_state,
        )

    sealed_bindings = tuple(
        (
            name,
            module_globals[name],
            callable_state(module_globals[name]),
        )
        for name in sorted(module_globals)
    )
    authority_mode = ExecutionAuthorityMode
    stopped_error = ExecutionStoppedError
    integrity_error = ExecutionStopIntegrityError
    require_graph = _require_canonical_admission_graph
    operation_lock = _CANONICAL_ADMISSION_OPERATION_LOCK
    current_unlocked = _CANONICAL_ADMISSION_CURRENT_UNLOCKED

    def require_sealed_module_graph() -> None:
        for name, expected, expected_state in sealed_bindings:
            if module_globals.get(name) is not expected:
                raise integrity_error(
                    "canonical execution admission module dependency graph changed"
                )
            current_state = callable_state(expected)
            current_code, current_wrapped, current_wrapped_code, current_closure = (
                current_state
            )
            expected_code, expected_wrapped, expected_wrapped_code, expected_closure = (
                expected_state
            )
            if (
                current_code is not expected_code
                or current_wrapped is not expected_wrapped
                or current_wrapped_code is not expected_wrapped_code
                or (
                    (current_closure is None) != (expected_closure is None)
                )
                or (
                    current_closure is not None
                    and expected_closure is not None
                    and (
                        len(current_closure) != len(expected_closure)
                        or any(
                            current is not frozen
                            for current, frozen in zip(
                                current_closure,
                                expected_closure,
                            )
                        )
                    )
                )
            ):
                raise integrity_error(
                    "canonical execution admission callable state changed"
                )
        require_graph()

    @contextmanager
    def sealed_admission_lease(
        self,
    ) -> Iterator[ExecutionAuthorityState]:
        require_sealed_module_graph()
        with operation_lock(self):
            require_sealed_module_graph()
            state = current_unlocked(self)
            require_sealed_module_graph()
            if state.mode is not authority_mode.ARMED:
                raise stopped_error(
                    f"execution STOP is active at revision {state.revision}: "
                    f"{state.reason}"
                )
            try:
                yield state
            finally:
                # If any canonical dependency changed while an irreversible
                # caller held the lease, never return a successful authority
                # result. The provider layer will retain UNKNOWN/reconciliation
                # semantics for an already-attempted external effect.
                require_sealed_module_graph()

    return sealed_admission_lease, require_sealed_module_graph


# Install the sealed lease only after the complete canonical helper graph above
# has been frozen. Reuse the exact same closure-owned module verifier below for
# the public positive read APIs so they cannot have a weaker transitive trust graph.
(
    _canonical_sealed_admission_lease,
    _CANONICAL_ADMISSION_REQUIRE_SEALED_MODULE_GRAPH,
) = _build_sealed_admission_lease()
ExecutionStopAuthority.admission_lease = _canonical_sealed_admission_lease
del _canonical_sealed_admission_lease


def _build_module_guarded_public_stop_read_authority():
    """Bind public positive STOP reads to the sealed module dependency graph."""

    require_sealed_module_graph = (
        _CANONICAL_ADMISSION_REQUIRE_SEALED_MODULE_GRAPH
    )
    operation_lock = _CANONICAL_ADMISSION_OPERATION_LOCK
    current_unlocked = _CANONICAL_ADMISSION_CURRENT_UNLOCKED
    authority_error = ExecutionStopAuthorityError
    stopped_error = ExecutionStoppedError
    armed_mode = ExecutionAuthorityMode.ARMED
    stopped_mode = ExecutionAuthorityMode.STOPPED
    decision_type = ExecutionAdmissionDecision
    integrity_error = ExecutionStopIntegrityError

    def current(
        self: ExecutionStopAuthority,
    ) -> ExecutionAuthorityState:
        require_sealed_module_graph()
        with operation_lock(self):
            require_sealed_module_graph()
            state = current_unlocked(self)
            require_sealed_module_graph()
        require_sealed_module_graph()
        return state

    def decision(
        self: ExecutionStopAuthority,
    ) -> ExecutionAdmissionDecision:
        try:
            state = current(self)
        except authority_error as exc:
            return decision_type(
                allowed=False,
                mode=stopped_mode,
                revision=None,
                reason=f"fail-closed: {exc}",
            )
        if state.mode is not armed_mode:
            return decision_type(
                allowed=False,
                mode=state.mode,
                revision=state.revision,
                reason=state.reason,
            )
        return decision_type(
            allowed=True,
            mode=state.mode,
            revision=state.revision,
            reason=state.reason,
        )

    def assert_execution_allowed(
        self: ExecutionStopAuthority,
    ) -> ExecutionAuthorityState:
        state = current(self)
        if state.mode is not armed_mode:
            raise stopped_error(
                f"execution STOP is active at revision {state.revision}: "
                f"{state.reason}"
            )
        return state

    def sealed_public_method(method):
        class SealedPublicMethod:
            __slots__ = ()

            def __get__(self, instance, owner=None):
                if instance is None:
                    return method
                return method.__get__(instance, owner)

            def __set__(self, _instance, _value) -> None:
                raise integrity_error(
                    "canonical public STOP authority method is immutable"
                )

            def __delete__(self, _instance) -> None:
                raise integrity_error(
                    "canonical public STOP authority method is immutable"
                )

        return SealedPublicMethod()

    return (
        sealed_public_method(current),
        sealed_public_method(decision),
        sealed_public_method(assert_execution_allowed),
    )


(
    _canonical_guarded_public_current,
    _canonical_guarded_public_decision,
    _canonical_guarded_public_assert_execution_allowed,
) = _build_module_guarded_public_stop_read_authority()
ExecutionStopAuthority.current = _canonical_guarded_public_current
ExecutionStopAuthority.decision = _canonical_guarded_public_decision
ExecutionStopAuthority.assert_execution_allowed = (
    _canonical_guarded_public_assert_execution_allowed
)
del _canonical_guarded_public_current
del _canonical_guarded_public_decision
del _canonical_guarded_public_assert_execution_allowed
del _build_module_guarded_public_stop_read_authority
