"""Rollback-resistant UTC calendar-day boundary evidence for risk accounting.

This module owns only the UTC day boundary that later risk composition can consume.
It does not calculate turnover, grant turnover headroom, define a risk session, or
authorize execution. Positive consumers must re-resolve evidence with
``ProductDayRiskWindowStore.require_current`` immediately before use.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Final

from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .monotonic_workspace_authority import (
    MonotonicAuthorityRollbackError,
    MonotonicWorkspaceAuthority,
    RecoveryDisposition,
)
from .workspace_lock import WorkspaceEconomicLock, _open_read_only_descriptor


_STATE_SCHEMA: Final = "autosport.risk.utc-day-window"
_STATE_SCHEMA_VERSION: Final = 1
_AUTHORITY_DOMAIN: Final = "portfolio.risk.utc-day-window"
_STATE_FILE_NAME: Final = "risk_day_window.json"
_STATE_KEYS: Final = frozenset(
    {
        "schema",
        "schema_version",
        "workspace_instance_id",
        "transition_id",
        "timezone",
        "day_key",
        "window_start",
        "window_end_exclusive",
    }
)
_UTC: Final = timezone.utc
_HEX_DIGITS: Final = frozenset("0123456789abcdef")

# Capture the actual built-in wall-clock and timestamp converter at import time.
# Replacing module globals such as time.time_ns or datetime.fromtimestamp later
# cannot turn a test/synthetic clock into the default product clock.
_PRODUCT_TIME_NS: Final = time.time_ns
_DATETIME_FROMTIMESTAMP: Final = datetime.fromtimestamp


class RiskDayWindowError(RuntimeError):
    """Base error for UTC day-window issuance or verification."""


class RiskDayWindowIntegrityError(RiskDayWindowError):
    """Persisted day-window bytes are malformed, aliased, or inconsistent."""


class RiskDayWindowClockRollbackError(RiskDayWindowError):
    """The product UTC day moved behind the already-issued durable day."""


class RiskDayWindowMismatchError(RiskDayWindowError):
    """Supplied day-window evidence is not the currently re-resolved authority."""


@dataclass(frozen=True, slots=True)
class ProductDayRiskWindow:
    """Re-resolvable UTC calendar-day boundary evidence."""

    workspace_instance_id: str
    day_key: str
    window_start: str
    window_end_exclusive: str
    state_sha256: str
    authority_generation: int
    product_clock_authoritative: bool
    timezone: str = "UTC"

    def __post_init__(self) -> None:
        for field_name in (
            "workspace_instance_id",
            "day_key",
            "window_start",
            "window_end_exclusive",
            "state_sha256",
            "timezone",
        ):
            if type(getattr(self, field_name)) is not str:
                raise RiskDayWindowIntegrityError(
                    f"{field_name} must be an exact built-in string"
                )
        if type(self.authority_generation) is not int:
            raise RiskDayWindowIntegrityError(
                "authority_generation must be an exact built-in integer"
            )
        if type(self.product_clock_authoritative) is not bool:
            raise RiskDayWindowIntegrityError(
                "product_clock_authoritative must be an exact built-in boolean"
            )

        day = _parse_day_key(self.day_key)
        expected_start, expected_end = _day_bounds(day)
        if self.timezone != "UTC":
            raise RiskDayWindowIntegrityError("risk day timezone must be exactly UTC")
        if self.window_start != expected_start:
            raise RiskDayWindowIntegrityError(
                "risk day start is not canonical UTC midnight"
            )
        if self.window_end_exclusive != expected_end:
            raise RiskDayWindowIntegrityError(
                "risk day end is not the next UTC midnight"
            )
        if (
            not isinstance(self.workspace_instance_id, str)
            or not self.workspace_instance_id
            or self.workspace_instance_id != self.workspace_instance_id.strip()
        ):
            raise RiskDayWindowIntegrityError(
                "workspace_instance_id must be a non-empty canonical string"
            )
        if not _is_sha256(self.state_sha256):
            raise RiskDayWindowIntegrityError(
                "state_sha256 must be canonical SHA-256"
            )
        if (
            isinstance(self.authority_generation, bool)
            or not isinstance(self.authority_generation, int)
            or self.authority_generation <= 0
        ):
            raise RiskDayWindowIntegrityError(
                "authority_generation must be positive"
            )
        if not isinstance(self.product_clock_authoritative, bool):
            raise RiskDayWindowIntegrityError(
                "product_clock_authoritative must be boolean"
            )

    @property
    def turnover_headroom_authoritative(self) -> bool:
        return False

    @property
    def session_boundary_authoritative(self) -> bool:
        return False

    @property
    def real_money_execution_authorized(self) -> bool:
        return False


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(ch in _HEX_DIGITS for ch in value)
    )


def _is_transition_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(ch in _HEX_DIGITS for ch in value)
    )


def _is_product_clock(
    clock: Callable[[], int],
    _product_clock: Callable[[], int] = _PRODUCT_TIME_NS,
) -> bool:
    return clock is _product_clock


def _clock_utc_instant(
    clock: Callable[[], int],
    _fromtimestamp=_DATETIME_FROMTIMESTAMP,
) -> datetime:
    epoch_ns = clock()
    if isinstance(epoch_ns, bool) or not isinstance(epoch_ns, int) or epoch_ns < 0:
        raise RiskDayWindowIntegrityError("UTC clock returned an invalid epoch")
    seconds, nanoseconds = divmod(epoch_ns, 1_000_000_000)
    instant = _fromtimestamp(seconds, _UTC).replace(microsecond=nanoseconds // 1000)
    if instant.tzinfo is None or instant.utcoffset() != timedelta(0):
        raise RiskDayWindowIntegrityError("UTC clock did not resolve to UTC")
    return instant


def _parse_day_key(value: object) -> date:
    if not isinstance(value, str) or len(value) != 10:
        raise RiskDayWindowIntegrityError("day_key must be canonical YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise RiskDayWindowIntegrityError(
            "day_key is not a valid calendar day"
        ) from exc
    if parsed.isoformat() != value:
        raise RiskDayWindowIntegrityError("day_key must be canonical YYYY-MM-DD")
    return parsed


def _format_utc_midnight(value: date) -> str:
    instant = datetime.combine(value, datetime_time.min, tzinfo=_UTC)
    return instant.isoformat().replace("+00:00", "Z")


def _day_bounds(value: date) -> tuple[str, str]:
    return (
        _format_utc_midnight(value),
        _format_utc_midnight(value + timedelta(days=1)),
    )


def _state_payload(
    workspace_instance_id: str,
    value: date,
    *,
    transition_id: str,
) -> dict[str, object]:
    if not _is_transition_id(transition_id):
        raise RiskDayWindowIntegrityError("transition_id must be canonical UUID hex")
    window_start, window_end = _day_bounds(value)
    return {
        "schema": _STATE_SCHEMA,
        "schema_version": _STATE_SCHEMA_VERSION,
        "workspace_instance_id": workspace_instance_id,
        "transition_id": transition_id,
        "timezone": "UTC",
        "day_key": value.isoformat(),
        "window_start": window_start,
        "window_end_exclusive": window_end,
    }


def _serialized_state(payload: dict[str, object]) -> bytes:
    try:
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RiskDayWindowIntegrityError(
            "risk day state is outside canonical JSON domain"
        ) from exc


def _state_digest(payload: dict[str, object]) -> str:
    return hashlib.sha256(_serialized_state(payload)).hexdigest()


def _semantic_binding(payload: dict[str, object]) -> str:
    try:
        material = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RiskDayWindowIntegrityError(
            "risk day binding is outside canonical JSON domain"
        ) from exc
    return hashlib.sha256(
        _AUTHORITY_DOMAIN.encode("utf-8") + b"\0" + material
    ).hexdigest()


def _transaction_id(payload: dict[str, object]) -> str:
    transition_id = payload["transition_id"]
    if not _is_transition_id(transition_id):
        raise RiskDayWindowIntegrityError("invalid persisted transition_id")
    return f"risk-day-{transition_id}"


def _read_regular_bytes(path: Path) -> bytes:
    """Read one exact single-link regular-file snapshot without following aliases."""

    try:
        path_before = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise RiskDayWindowIntegrityError("cannot inspect risk day state") from exc
    if not stat.S_ISREG(path_before.st_mode) or path_before.st_nlink != 1:
        raise RiskDayWindowIntegrityError(
            "risk day state must be a single-link regular file"
        )

    try:
        descriptor = _open_read_only_descriptor(path)
    except OSError as exc:
        raise RiskDayWindowIntegrityError(
            "cannot open risk day state safely"
        ) from exc

    primary_error: BaseException | None = None
    try:
        opened_before = os.fstat(descriptor)
        verification = _open_read_only_descriptor(path)
        try:
            same_file = os.path.sameopenfile(descriptor, verification)
            verified_stat = os.fstat(verification)
        finally:
            os.close(verification)
        if (
            not same_file
            or not stat.S_ISREG(opened_before.st_mode)
            or opened_before.st_nlink != 1
            or not stat.S_ISREG(verified_stat.st_mode)
            or verified_stat.st_nlink != 1
        ):
            raise RiskDayWindowIntegrityError(
                "risk day state path changed during verification"
            )

        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)

        opened_after = os.fstat(descriptor)
        path_after = os.stat(path, follow_symlinks=False)
        final_verification = _open_read_only_descriptor(path)
        try:
            same_final_file = os.path.sameopenfile(descriptor, final_verification)
        finally:
            os.close(final_verification)

        if (
            not same_final_file
            or not stat.S_ISREG(opened_after.st_mode)
            or opened_after.st_nlink != 1
            or not stat.S_ISREG(path_after.st_mode)
            or path_after.st_nlink != 1
            or opened_before.st_mode != opened_after.st_mode
            or opened_before.st_size != opened_after.st_size
            or opened_before.st_mtime_ns != opened_after.st_mtime_ns
            or opened_before.st_ctime_ns != opened_after.st_ctime_ns
        ):
            raise RiskDayWindowIntegrityError(
                "risk day state changed while it was being read"
            )
        return b"".join(chunks)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            os.close(descriptor)
        except OSError as close_error:
            if primary_error is None:
                raise RiskDayWindowIntegrityError(
                    "cannot close risk day state descriptor"
                ) from close_error
            try:
                primary_error.add_note(
                    "risk day state descriptor close also failed"
                )
            except BaseException:
                pass


def _decode_state(
    raw_bytes: bytes,
    *,
    expected_workspace_instance_id: str,
) -> tuple[dict[str, object], date]:
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeError as exc:
        raise RiskDayWindowIntegrityError("risk day state is not UTF-8") from exc
    try:
        raw = strict_json_loads(text)
    except (TypeError, ValueError) as exc:
        raise RiskDayWindowIntegrityError(
            "risk day state is not strict JSON"
        ) from exc
    if not isinstance(raw, dict) or frozenset(raw) != _STATE_KEYS:
        raise RiskDayWindowIntegrityError(
            "risk day state keys do not match schema"
        )
    if (
        raw["schema"] != _STATE_SCHEMA
        or raw["schema_version"] != _STATE_SCHEMA_VERSION
        or isinstance(raw["schema_version"], bool)
        or raw["workspace_instance_id"] != expected_workspace_instance_id
        or raw["timezone"] != "UTC"
        or not _is_transition_id(raw["transition_id"])
    ):
        raise RiskDayWindowIntegrityError(
            "risk day state schema/identity mismatch"
        )

    day = _parse_day_key(raw["day_key"])
    canonical = _state_payload(
        expected_workspace_instance_id,
        day,
        transition_id=str(raw["transition_id"]),
    )
    if raw != canonical:
        raise RiskDayWindowIntegrityError(
            "risk day state does not contain the complete canonical UTC day"
        )
    if raw_bytes != _serialized_state(canonical):
        raise RiskDayWindowIntegrityError(
            "risk day state bytes are not canonical"
        )
    return canonical, day


class ProductDayRiskWindowStore:
    """Issue/re-resolve one rollback-resistant UTC calendar-day boundary."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        authority_root: str | Path | None = None,
        _test_clock: Callable[[], int] | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve(strict=False)
        if not self.workspace.is_absolute():
            raise RiskDayWindowIntegrityError(
                "workspace must resolve to an absolute path"
            )
        self.state_path = self.workspace / ".autosport" / _STATE_FILE_NAME
        self._clock = _PRODUCT_TIME_NS if _test_clock is None else _test_clock
        if not callable(self._clock):
            raise RiskDayWindowIntegrityError("_test_clock must be callable")
        self._authority = MonotonicWorkspaceAuthority(
            workspace=self.workspace,
            domain=_AUTHORITY_DOMAIN,
            key=str(Path(".autosport") / _STATE_FILE_NAME),
            authority_root=authority_root,
        )

    def current(self) -> ProductDayRiskWindow:
        """Return current UTC day evidence after durable rollback validation."""

        with WorkspaceEconomicLock(self.workspace):
            target_day = _clock_utc_instant(self._clock).date()
            if os.path.lexists(self.state_path):
                state_bytes = _read_regular_bytes(self.state_path)
                payload, persisted_day = _decode_state(
                    state_bytes,
                    expected_workspace_instance_id=(
                        self._authority.workspace_instance_id
                    ),
                )
                observed_sha256 = hashlib.sha256(state_bytes).hexdigest()
                recovery = self._authority.recover(
                    observed_state_sha256=observed_sha256,
                    tx_id=_transaction_id(payload),
                    semantic_binding_sha256=_semantic_binding(payload),
                )
                if recovery.disposition not in {
                    RecoveryDisposition.CURRENT,
                    RecoveryDisposition.ABORTED_PREPARE,
                    RecoveryDisposition.COMMITTED_PREPARE,
                }:
                    raise RiskDayWindowIntegrityError(
                        "risk day state is not a recoverable authority tip"
                    )
                generation = recovery.committed_generation
            else:
                recovery = self._authority.recover(
                    observed_state_sha256=None
                )
                if recovery.disposition not in {
                    RecoveryDisposition.PRISTINE,
                    RecoveryDisposition.ABORTED_PREPARE,
                }:
                    raise MonotonicAuthorityRollbackError(
                        "risk day state is missing after authority establishment"
                    )
                return self._publish_day(
                    target_day,
                    observed_sha256=None,
                )

            if target_day < persisted_day:
                raise RiskDayWindowClockRollbackError(
                    "UTC day moved behind the committed risk day"
                )
            if target_day == persisted_day:
                return self._evidence(
                    payload,
                    observed_sha256,
                    generation,
                )
            return self._publish_day(
                target_day,
                observed_sha256=observed_sha256,
            )

    def require_current(
        self,
        candidate: ProductDayRiskWindow,
    ) -> ProductDayRiskWindow:
        """Re-resolve positive product-clock day authority and reject substitutes."""

        if type(candidate) is not ProductDayRiskWindow:
            raise RiskDayWindowMismatchError(
                "candidate must be ProductDayRiskWindow evidence"
            )
        current = self.current()
        if not current.product_clock_authoritative:
            raise RiskDayWindowIntegrityError(
                "test/synthetic clock cannot mint product day authority"
            )
        if candidate != current:
            raise RiskDayWindowMismatchError(
                "risk day evidence does not match current durable authority"
            )
        return current

    def _publish_day(
        self,
        target_day: date,
        *,
        observed_sha256: str | None,
    ) -> ProductDayRiskWindow:
        payload = _state_payload(
            self._authority.workspace_instance_id,
            target_day,
            transition_id=uuid.uuid4().hex,
        )
        intended_sha256 = _state_digest(payload)
        binding = _semantic_binding(payload)
        tx_id = _transaction_id(payload)

        self._authority.prepare(
            tx_id=tx_id,
            observed_state_sha256=observed_sha256,
            intended_state_sha256=intended_sha256,
            semantic_binding_sha256=binding,
        )
        atomic_write_json(self.state_path, payload)
        state_bytes = _read_regular_bytes(self.state_path)
        published_sha256 = hashlib.sha256(state_bytes).hexdigest()
        if published_sha256 != intended_sha256:
            raise RiskDayWindowIntegrityError(
                "published risk day state does not match prepared authority digest"
            )
        committed = self._authority.commit(
            tx_id=tx_id,
            observed_state_sha256=published_sha256,
            semantic_binding_sha256=binding,
        )
        return self._evidence(
            payload,
            published_sha256,
            committed.generation,
        )

    def _evidence(
        self,
        payload: dict[str, object],
        state_sha256: str,
        generation: int,
    ) -> ProductDayRiskWindow:
        return ProductDayRiskWindow(
            workspace_instance_id=str(payload["workspace_instance_id"]),
            day_key=str(payload["day_key"]),
            window_start=str(payload["window_start"]),
            window_end_exclusive=str(payload["window_end_exclusive"]),
            state_sha256=state_sha256,
            authority_generation=generation,
            product_clock_authoritative=_is_product_clock(self._clock),
        )
