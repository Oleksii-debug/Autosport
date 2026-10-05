from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .causal_integrity import contains_forbidden_future_key
from .domain import utc_now_iso
from .economic_goal import EconomicGoalContract
from .economic_goal_provenance import (
    EconomicGoalProvenance,
    EconomicGoalProvenanceError,
    provenance_for,
    verify_provenance,
)
from .risk import PaperRiskPolicy


class DecisionLedgerIntegrityError(RuntimeError):
    """Raised when persisted decision-ledger evidence is not structurally self-consistent."""


class _FrozenDecisionPayloadList(tuple):
    """Tuple-backed marker preserving the source distinction between JSON lists and tuples."""


def _freeze_decision_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_decision_payload(child) for key, child in value.items()}
        )
    if isinstance(value, list):
        return _FrozenDecisionPayloadList(
            _freeze_decision_payload(child) for child in value
        )
    if isinstance(value, tuple):
        return tuple(_freeze_decision_payload(child) for child in value)
    return value


def _detached_decision_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _detached_decision_payload(child)
            for key, child in value.items()
        }
    if isinstance(value, _FrozenDecisionPayloadList):
        return [_detached_decision_payload(child) for child in value]
    if isinstance(value, tuple):
        return tuple(_detached_decision_payload(child) for child in value)
    return value


GENERAL_DECISION_KIND = "GENERAL"
ECONOMIC_DECISION_KIND = "ECONOMIC"
ECONOMIC_GOAL_PROVENANCE_PAYLOAD_KEY = "economic_goal_provenance"
RISK_POLICY_PROVENANCE_PAYLOAD_KEY = "risk_policy_provenance"
MATERIAL_ACTION_ID_PAYLOAD_KEY = "material_action_id"


@dataclass(frozen=True, slots=True)
class EconomicDecisionAuthority:
    """Exact goal + executable paper-risk authority for one economic append."""

    contract: EconomicGoalContract
    risk_policy: PaperRiskPolicy

    def __post_init__(self) -> None:
        if not isinstance(self.contract, EconomicGoalContract):
            raise TypeError("contract must be an EconomicGoalContract")
        if not isinstance(self.risk_policy, PaperRiskPolicy):
            raise TypeError("risk_policy must be a PaperRiskPolicy")
        if self.risk_policy.economic_goal != self.contract:
            raise DecisionLedgerIntegrityError(
                "risk policy is not bound to the supplied EconomicGoalContract"
            )


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    replay_run_id: str
    agent: str
    observed_ts: str
    action: str
    payload: dict[str, Any]
    context_hash: str
    decision_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    recorded_at: str = field(default_factory=utc_now_iso)
    decision_kind: str = GENERAL_DECISION_KIND

    def __post_init__(self) -> None:
        payload = _freeze_decision_payload(self.payload)
        if contains_forbidden_future_key(payload):
            raise ValueError("decision payload must not contain future-result fields")
        if self.decision_kind not in {GENERAL_DECISION_KIND, ECONOMIC_DECISION_KIND}:
            raise ValueError("decision_kind must be GENERAL or ECONOMIC")
        if (
            self.decision_kind == GENERAL_DECISION_KIND
            and ECONOMIC_GOAL_PROVENANCE_PAYLOAD_KEY in payload
        ):
            object.__setattr__(self, "decision_kind", ECONOMIC_DECISION_KIND)
        object.__setattr__(self, "payload", payload)

    def to_dict(self) -> dict[str, Any]:
        record = {
            "replay_run_id": self.replay_run_id,
            "agent": self.agent,
            "observed_ts": self.observed_ts,
            "action": self.action,
            "payload": _detached_decision_payload(self.payload),
            "context_hash": self.context_hash,
            "decision_id": self.decision_id,
            "recorded_at": self.recorded_at,
        }
        if self.decision_kind == ECONOMIC_DECISION_KIND:
            record["decision_kind"] = ECONOMIC_DECISION_KIND
        return record


_ECONOMIC_GOAL_PROVENANCE_FIELDS = frozenset(
    {
        "schema",
        "schema_version",
        "goal_id",
        "revision",
        "bankroll_id",
        "contract_sha256",
    }
)


def _economic_goal_provenance_payload(
    provenance: EconomicGoalProvenance,
) -> dict[str, object]:
    return {
        "schema": provenance.schema,
        "schema_version": provenance.schema_version,
        "goal_id": provenance.goal_id,
        "revision": provenance.revision,
        "bankroll_id": provenance.bankroll_id,
        "contract_sha256": provenance.contract_sha256,
    }


def _economic_goal_provenance_from_payload(
    payload: object,
) -> EconomicGoalProvenance:
    if not isinstance(payload, Mapping) or set(payload) != _ECONOMIC_GOAL_PROVENANCE_FIELDS:
        raise DecisionLedgerIntegrityError(
            "Decision Ledger economic-goal provenance schema is invalid"
        )
    try:
        return EconomicGoalProvenance(
            schema=payload["schema"],  # type: ignore[arg-type]
            schema_version=payload["schema_version"],  # type: ignore[arg-type]
            goal_id=payload["goal_id"],  # type: ignore[arg-type]
            revision=payload["revision"],  # type: ignore[arg-type]
            bankroll_id=payload["bankroll_id"],  # type: ignore[arg-type]
            contract_sha256=payload["contract_sha256"],  # type: ignore[arg-type]
        )
    except (EconomicGoalProvenanceError, KeyError, TypeError) as exc:
        raise DecisionLedgerIntegrityError(
            "Decision Ledger economic-goal provenance is invalid"
        ) from exc


def _risk_policy_provenance_payload(
    policy: PaperRiskPolicy,
) -> dict[str, object]:
    payload = policy.provenance_payload()
    return {**payload, "sha256": policy.provenance_sha256}


def bind_economic_goal(
    record: DecisionRecord,
    contract: EconomicGoalContract,
    risk_policy: PaperRiskPolicy | None = None,
) -> DecisionRecord:
    """Return the same economic decision identity with canonical goal evidence bound."""

    if not isinstance(record, DecisionRecord):
        raise TypeError("economic decision binding requires a DecisionRecord")
    if not isinstance(contract, EconomicGoalContract):
        raise TypeError("economic decision binding requires an EconomicGoalContract")
    if record.decision_kind != ECONOMIC_DECISION_KIND:
        raise DecisionLedgerIntegrityError(
            "Decision Ledger material economic decision must declare ECONOMIC decision_kind"
        )

    payload = _detached_decision_payload(record.payload)
    if not isinstance(payload, dict):
        raise DecisionLedgerIntegrityError("Decision Ledger record payload is invalid")
    if (
        ECONOMIC_GOAL_PROVENANCE_PAYLOAD_KEY in payload
        or RISK_POLICY_PROVENANCE_PAYLOAD_KEY in payload
    ):
        raise DecisionLedgerIntegrityError(
            "Decision Ledger economic-goal provenance must be derived, not caller supplied"
        )

    provenance = provenance_for(contract)
    payload[ECONOMIC_GOAL_PROVENANCE_PAYLOAD_KEY] = _economic_goal_provenance_payload(
        provenance
    )
    if risk_policy is not None:
        if not isinstance(risk_policy, PaperRiskPolicy):
            raise TypeError("risk_policy must be a PaperRiskPolicy or None")
        if risk_policy.economic_goal != contract:
            raise DecisionLedgerIntegrityError(
                "Decision Ledger risk policy is not bound to the supplied EconomicGoalContract"
            )
        payload[RISK_POLICY_PROVENANCE_PAYLOAD_KEY] = _risk_policy_provenance_payload(
            risk_policy
        )
    return DecisionRecord(
        replay_run_id=record.replay_run_id,
        agent=record.agent,
        observed_ts=record.observed_ts,
        action=record.action,
        payload=payload,
        context_hash=record.context_hash,
        decision_id=record.decision_id,
        recorded_at=record.recorded_at,
        decision_kind=ECONOMIC_DECISION_KIND,
    )


def verify_economic_goal_binding(
    record: DecisionRecord,
    contract: EconomicGoalContract,
    risk_policy: PaperRiskPolicy | None = None,
) -> EconomicGoalProvenance:
    """Fail closed unless one durable economic decision is bound to ``contract`` exactly."""

    if not isinstance(record, DecisionRecord):
        raise TypeError("economic decision verification requires a DecisionRecord")
    if not isinstance(contract, EconomicGoalContract):
        raise TypeError("economic decision verification requires an EconomicGoalContract")
    if record.decision_kind != ECONOMIC_DECISION_KIND:
        raise DecisionLedgerIntegrityError(
            "Decision Ledger record is not classified as a material economic decision"
        )

    evidence = record.payload.get(ECONOMIC_GOAL_PROVENANCE_PAYLOAD_KEY)
    if evidence is None:
        raise DecisionLedgerIntegrityError(
            "Decision Ledger economic decision is missing EconomicGoal provenance"
        )
    provenance = _economic_goal_provenance_from_payload(evidence)
    try:
        verify_provenance(contract, provenance)
    except EconomicGoalProvenanceError as exc:
        raise DecisionLedgerIntegrityError(
            f"Decision Ledger economic-goal provenance mismatch: {exc}"
        ) from exc
    if risk_policy is not None:
        if not isinstance(risk_policy, PaperRiskPolicy):
            raise TypeError("risk_policy must be a PaperRiskPolicy or None")
        if risk_policy.economic_goal != contract:
            raise DecisionLedgerIntegrityError(
                "Decision Ledger risk policy is not bound to the supplied EconomicGoalContract"
            )
        actual_policy = record.payload.get(RISK_POLICY_PROVENANCE_PAYLOAD_KEY)
        if actual_policy != _risk_policy_provenance_payload(risk_policy):
            raise DecisionLedgerIntegrityError(
                "Decision Ledger risk-policy provenance mismatch"
            )
    return provenance


@dataclass(frozen=True, slots=True)
class VerifiedDecisionLedgerSnapshot:
    """One immutable ledger byte snapshot bound to its semantic proof and SHA-256."""

    payload: bytes
    sha256: str
    record_count: int


class _DecisionLedgerPathLock:
    """Persistent OS-backed lock whose ownership is released automatically on crash."""

    def __init__(self, path: Path, *, blocking: bool) -> None:
        self.path = path
        self.blocking = blocking
        self._handle = None

    def __enter__(self) -> "_DecisionLedgerPathLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing = os.lstat(self.path)
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            raise DecisionLedgerIntegrityError(
                "Decision Ledger writer-lock path is unavailable"
            ) from exc
        if existing is not None and (
            stat.S_ISLNK(existing.st_mode)
            or not stat.S_ISREG(existing.st_mode)
            or getattr(existing, "st_nlink", 1) != 1
        ):
            raise DecisionLedgerIntegrityError(
                "Decision Ledger writer-lock path must be one regular non-symlink file"
            )

        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise DecisionLedgerIntegrityError(
                "Decision Ledger writer-lock path is unavailable"
            ) from exc
        handle = os.fdopen(fd, "a+b", closefd=True)
        try:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or getattr(info, "st_nlink", 1) != 1
            ):
                raise DecisionLedgerIntegrityError(
                    "Decision Ledger writer-lock path must be one regular file"
                )
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                mode = msvcrt.LK_LOCK if self.blocking else msvcrt.LK_NBLCK
                try:
                    msvcrt.locking(handle.fileno(), mode, 1)
                except OSError as exc:
                    if not self.blocking:
                        raise DecisionLedgerIntegrityError(
                            "Decision Ledger writer is active"
                        ) from exc
                    raise
            else:
                import fcntl

                flags = fcntl.LOCK_EX
                if not self.blocking:
                    flags |= fcntl.LOCK_NB
                try:
                    fcntl.flock(handle.fileno(), flags)
                except BlockingIOError as exc:
                    raise DecisionLedgerIntegrityError(
                        "Decision Ledger writer is active"
                    ) from exc
        except BaseException:
            handle.close()
            raise
        self._handle = handle
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None


class JsonlDecisionLedger:
    """Append-only causal decision ledger. Result/outcome fields do not belong here."""

    _ENVELOPE_FIELDS = frozenset({"sha256", "record"})
    _LEGACY_RECORD_FIELDS = frozenset(
        {
            "replay_run_id",
            "agent",
            "observed_ts",
            "action",
            "payload",
            "context_hash",
            "decision_id",
            "recorded_at",
        }
    )
    _ECONOMIC_RECORD_FIELDS = _LEGACY_RECORD_FIELDS | {"decision_kind"}
    _STRING_FIELDS = (
        "replay_run_id",
        "agent",
        "observed_ts",
        "action",
        "context_hash",
        "decision_id",
        "recorded_at",
    )

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._path_authority = self.path
        self._absolute_path_authority = self.path.resolve(strict=False)
        self._writer_lock_path_authority = self._absolute_path_authority.with_name(
            self._absolute_path_authority.name + ".writer.lock"
        )
        self._file_identity_authority: tuple[int, int] | None = None
        if self._absolute_path_authority.exists():
            self._file_identity_authority = self._read_file_identity()

    def _assert_persistence_authority(self) -> None:
        if (
            self.path != self._path_authority
            or self.path.resolve(strict=False) != self._absolute_path_authority
            or self._writer_lock_path_authority
            != self._absolute_path_authority.with_name(
                self._absolute_path_authority.name + ".writer.lock"
            )
        ):
            raise DecisionLedgerIntegrityError(
                "Decision Ledger persistence authority changed after construction"
            )

    def _read_file_identity(self) -> tuple[int, int]:
        try:
            info = os.lstat(self._absolute_path_authority)
        except OSError as exc:
            raise DecisionLedgerIntegrityError(
                "Decision Ledger path identity is unavailable"
            ) from exc
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or getattr(info, "st_nlink", 1) != 1
        ):
            raise DecisionLedgerIntegrityError(
                "Decision Ledger path must be one regular non-linked file"
            )
        return (info.st_dev, info.st_ino)

    def _assert_file_identity(self, fd: int | None = None) -> None:
        identity = self._file_identity_authority
        if identity is None:
            raise DecisionLedgerIntegrityError(
                "Decision Ledger file identity is not bound"
            )
        if self._read_file_identity() != identity:
            raise DecisionLedgerIntegrityError(
                "Decision Ledger file identity changed after construction"
            )
        if fd is not None:
            try:
                opened = os.fstat(fd)
            except OSError as exc:
                raise DecisionLedgerIntegrityError(
                    "Decision Ledger opened file identity is unavailable"
                ) from exc
            if (
                not stat.S_ISREG(opened.st_mode)
                or getattr(opened, "st_nlink", 1) != 1
                or (opened.st_dev, opened.st_ino) != identity
            ):
                raise DecisionLedgerIntegrityError(
                    "Decision Ledger opened file identity changed"
                )

    def _sync_parent_directory(self) -> None:
        if os.name == "nt":
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            directory_fd = os.open(self._absolute_path_authority.parent, flags)
        except OSError as exc:
            raise DecisionLedgerIntegrityError(
                "Decision Ledger parent-directory durability barrier failed"
            ) from exc
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            raise DecisionLedgerIntegrityError(
                "Decision Ledger parent-directory durability barrier failed"
            ) from exc
        finally:
            os.close(directory_fd)

    def _ensure_path_durable(self) -> None:
        if self._absolute_path_authority.exists():
            if self._file_identity_authority is None:
                self._file_identity_authority = self._read_file_identity()
            self._assert_file_identity()
            return
        try:
            with self._absolute_path_authority.open(
                "a",
                encoding="utf-8",
                newline="\n",
            ) as handle:
                handle.flush()
                os.fsync(handle.fileno())
            self._sync_parent_directory()
            self._file_identity_authority = self._read_file_identity()
        except DecisionLedgerIntegrityError:
            raise
        except OSError as exc:
            raise DecisionLedgerIntegrityError(
                "Decision Ledger path durability barrier failed"
            ) from exc

    def _writer_guard(self, *, blocking: bool = True) -> _DecisionLedgerPathLock:
        self._assert_persistence_authority()
        return _DecisionLedgerPathLock(
            self._writer_lock_path_authority,
            blocking=blocking,
        )

    def assert_transaction_authority(self) -> None:
        """Fail closed before external I/O if durable decision publication is unavailable."""

        self._assert_persistence_authority()
        try:
            with self._writer_guard(blocking=False):
                if self._file_identity_authority is not None:
                    self._assert_file_identity()
                elif self._absolute_path_authority.exists():
                    # A peer-created ledger is not silently adopted as this
                    # instance's durable authority.
                    self._read_file_identity()
        except DecisionLedgerIntegrityError as exc:
            if "writer is active" in str(exc):
                raise DecisionLedgerIntegrityError(
                    "Decision Ledger transaction authority is unavailable"
                ) from exc
            raise

    @staticmethod
    def _require_utf8_text(value: str, *, path: str) -> None:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise DecisionLedgerIntegrityError(
                f"Decision Ledger JSON text at {path} is not valid UTF-8"
            ) from exc

    @classmethod
    def _validate_json_value(cls, value: object, *, path: str) -> None:
        if value is None or isinstance(value, (bool, int)):
            return
        if isinstance(value, str):
            cls._require_utf8_text(value, path=path)
            return
        if isinstance(value, float):
            if not math.isfinite(value):
                raise DecisionLedgerIntegrityError(
                    f"Decision Ledger JSON value at {path} is non-finite"
                )
            return
        if isinstance(value, list):
            for index, item in enumerate(value):
                cls._validate_json_value(item, path=f"{path}[{index}]")
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise DecisionLedgerIntegrityError(
                        f"Decision Ledger JSON object keys at {path} must be strings"
                    )
                cls._require_utf8_text(key, path=f"{path} object key")
                child_path = "payload" if path == "record" and key == "payload" else f"{path}.{key}"
                cls._validate_json_value(item, path=child_path)
            return
        raise DecisionLedgerIntegrityError(
            f"Decision Ledger JSON value at {path} has unsupported type {type(value).__name__}"
        )

    @staticmethod
    def _canonical_record(record: dict[str, Any]) -> str:
        try:
            return json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise DecisionLedgerIntegrityError(
                "Decision Ledger record is not canonical JSON"
            ) from exc

    @classmethod
    def _validate_record(
        cls,
        record: object,
        *,
        line_number: int | None = None,
    ) -> dict[str, Any]:
        location = f" at line {line_number}" if line_number is not None else ""
        if not isinstance(record, dict):
            raise DecisionLedgerIntegrityError(
                f"Decision Ledger record schema is invalid{location}"
            )
        fields = frozenset(record)
        if fields not in {cls._LEGACY_RECORD_FIELDS, cls._ECONOMIC_RECORD_FIELDS}:
            raise DecisionLedgerIntegrityError(
                f"Decision Ledger record schema is invalid{location}"
            )
        try:
            cls._validate_json_value(record, path="record")
        except RecursionError as exc:
            raise DecisionLedgerIntegrityError(
                f"Decision Ledger record nesting is too deep{location}"
            ) from exc
        for field_name in cls._STRING_FIELDS:
            value = record.get(field_name)
            if not isinstance(value, str) or not value.strip():
                raise DecisionLedgerIntegrityError(
                    f"Decision Ledger record field {field_name!r} is invalid{location}"
                )
        if "decision_kind" in record and record["decision_kind"] != ECONOMIC_DECISION_KIND:
            raise DecisionLedgerIntegrityError(
                f"Decision Ledger decision_kind is invalid{location}"
            )
        payload = record.get("payload")
        if not isinstance(payload, dict):
            raise DecisionLedgerIntegrityError(
                f"Decision Ledger record field 'payload' is invalid{location}"
            )
        if contains_forbidden_future_key(payload):
            raise DecisionLedgerIntegrityError(
                f"Decision Ledger payload contains future-result fields{location}"
            )
        if MATERIAL_ACTION_ID_PAYLOAD_KEY in payload:
            material_action_id = payload[MATERIAL_ACTION_ID_PAYLOAD_KEY]
            if (
                not isinstance(material_action_id, str)
                or not material_action_id.strip()
                or material_action_id != material_action_id.strip()
            ):
                raise DecisionLedgerIntegrityError(
                    f"Decision Ledger material_action_id is invalid{location}"
                )
        if (
            "decision_kind" in record
            and ECONOMIC_GOAL_PROVENANCE_PAYLOAD_KEY not in payload
        ):
            raise DecisionLedgerIntegrityError(
                f"Decision Ledger ECONOMIC record is missing EconomicGoal provenance{location}"
            )
        return record

    @staticmethod
    def _json_object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key, value in pairs:
            if key in payload:
                raise DecisionLedgerIntegrityError(
                    f"Decision Ledger JSON object contains duplicate key {key!r}"
                )
            payload[key] = value
        return payload

    @staticmethod
    def _reject_non_finite_json(value: str) -> None:
        raise DecisionLedgerIntegrityError(
            f"Decision Ledger JSON contains non-finite numeric value {value!r}"
        )

    def _append_validated(self, record: DecisionRecord) -> str:
        self._assert_persistence_authority()
        payload = self._validate_record(record.to_dict())
        canonical = self._canonical_record(payload)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        envelope = json.dumps(
            {"sha256": digest, "record": payload},
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )

        with self._writer_guard():
            self._assert_persistence_authority()
            try:
                existing = self._absolute_path_authority.read_bytes()
            except FileNotFoundError:
                existing = b""
            except OSError as exc:
                raise DecisionLedgerIntegrityError(
                    "Decision Ledger file is unreadable before append"
                ) from exc

            self._verify_bytes(existing)
            self._ensure_path_durable()
            material_action_id = payload["payload"].get(
                MATERIAL_ACTION_ID_PAYLOAD_KEY
            )
            if material_action_id is not None and (
                not isinstance(material_action_id, str)
                or not material_action_id.strip()
            ):
                raise DecisionLedgerIntegrityError(
                    "Decision Ledger material_action_id is invalid"
                )
            for line in existing.decode("utf-8").splitlines():
                prior = json.loads(line)
                prior_record = prior["record"]
                if prior_record["decision_id"] == payload["decision_id"]:
                    raise DecisionLedgerIntegrityError(
                        "Decision Ledger decision_id already exists"
                    )
                if (
                    material_action_id is not None
                    and prior_record["payload"].get(MATERIAL_ACTION_ID_PAYLOAD_KEY)
                    == material_action_id
                ):
                    raise DecisionLedgerIntegrityError(
                        "Decision Ledger material_action_id already exists"
                    )

            with self._absolute_path_authority.open(
                "a",
                encoding="utf-8",
                newline="\n",
            ) as handle:
                self._assert_file_identity(handle.fileno())
                handle.write(envelope + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                self._assert_file_identity(handle.fileno())
            self._assert_persistence_authority()
            self._assert_file_identity()
            return digest

    def append(self, record: DecisionRecord) -> str:
        """Persist a non-economic decision only."""

        if not isinstance(record, DecisionRecord):
            raise TypeError("Decision Ledger append requires a DecisionRecord")
        if (
            record.decision_kind == ECONOMIC_DECISION_KIND
            or ECONOMIC_GOAL_PROVENANCE_PAYLOAD_KEY in record.payload
            or RISK_POLICY_PROVENANCE_PAYLOAD_KEY in record.payload
        ):
            raise DecisionLedgerIntegrityError(
                "Decision Ledger material economic decision must use append_economic"
            )
        return self._append_validated(record)

    def append_economic(
        self,
        record: DecisionRecord,
        contract: EconomicGoalContract | EconomicDecisionAuthority,
        *,
        risk_policy: PaperRiskPolicy | None = None,
    ) -> str:
        """Persist a material economic decision with derived goal and policy provenance."""

        if isinstance(contract, EconomicDecisionAuthority):
            if risk_policy is not None and risk_policy != contract.risk_policy:
                raise DecisionLedgerIntegrityError(
                    "conflicting risk-policy authority supplied for economic append"
                )
            economic_goal = contract.contract
            effective_policy = contract.risk_policy
        else:
            economic_goal = contract
            effective_policy = risk_policy
        return self._append_validated(
            bind_economic_goal(record, economic_goal, effective_policy)
        )

    @classmethod
    def _verify_bytes(cls, raw: bytes) -> int:
        if not raw:
            return 0
        if not raw.endswith(b"\n"):
            raise DecisionLedgerIntegrityError(
                "Decision Ledger has an unterminated final record"
            )

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DecisionLedgerIntegrityError(
                "Decision Ledger is not valid UTF-8"
            ) from exc

        lines = text.split("\n")
        if lines[-1] != "":
            raise DecisionLedgerIntegrityError(
                "Decision Ledger has an unterminated final record"
            )

        seen_decision_ids: set[str] = set()
        line_count = 0
        for line_number, line in enumerate(lines[:-1], start=1):
            if not line:
                raise DecisionLedgerIntegrityError(
                    f"Decision Ledger contains a blank record at line {line_number}"
                )
            try:
                envelope = json.loads(
                    line,
                    object_pairs_hook=cls._json_object_without_duplicate_keys,
                    parse_constant=cls._reject_non_finite_json,
                )
            except json.JSONDecodeError as exc:
                raise DecisionLedgerIntegrityError(
                    f"Decision Ledger contains invalid JSON at line {line_number}"
                ) from exc
            except RecursionError as exc:
                raise DecisionLedgerIntegrityError(
                    f"Decision Ledger JSON nesting is too deep at line {line_number}"
                ) from exc
            except DecisionLedgerIntegrityError as exc:
                raise DecisionLedgerIntegrityError(
                    f"{exc} at line {line_number}"
                ) from exc

            if not isinstance(envelope, dict) or set(envelope) != cls._ENVELOPE_FIELDS:
                raise DecisionLedgerIntegrityError(
                    f"Decision Ledger envelope schema is invalid at line {line_number}"
                )

            digest = envelope.get("sha256")
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise DecisionLedgerIntegrityError(
                    f"Decision Ledger SHA-256 field is invalid at line {line_number}"
                )

            record = cls._validate_record(envelope.get("record"), line_number=line_number)
            canonical = cls._canonical_record(record)
            actual_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            if actual_digest != digest:
                raise DecisionLedgerIntegrityError(
                    f"Decision Ledger SHA-256 mismatch at line {line_number}"
                )

            decision_id = str(record["decision_id"])
            if decision_id in seen_decision_ids:
                raise DecisionLedgerIntegrityError(
                    f"Decision Ledger contains duplicate decision_id at line {line_number}"
                )
            seen_decision_ids.add(decision_id)
            line_count += 1

        return line_count

    def verified_snapshot_if_exists(self) -> VerifiedDecisionLedgerSnapshot:
        """Return an empty verified snapshot only before this authority has a ledger file."""

        self._assert_persistence_authority()
        if self._writer_lock_path_authority.exists():
            raise DecisionLedgerIntegrityError(
                "Decision Ledger writer lock exists; verified snapshot is unavailable"
            )
        if not self._absolute_path_authority.exists():
            if self._file_identity_authority is not None:
                raise DecisionLedgerIntegrityError(
                    "Decision Ledger bound file is missing"
                )
            return VerifiedDecisionLedgerSnapshot(
                payload=b"",
                sha256=hashlib.sha256(b"").hexdigest(),
                record_count=0,
            )
        if self._file_identity_authority is None:
            raise DecisionLedgerIntegrityError(
                "Decision Ledger file appeared outside this persistence authority"
            )
        return self.verified_snapshot()

    def verified_snapshot(self) -> VerifiedDecisionLedgerSnapshot:
        self._assert_persistence_authority()
        self._assert_file_identity()
        if self._writer_lock_path_authority.exists():
            raise DecisionLedgerIntegrityError(
                "Decision Ledger writer lock exists; verified snapshot is unavailable"
            )
        self._assert_file_identity()
        try:
            raw = self._absolute_path_authority.read_bytes()
        except OSError as exc:
            raise DecisionLedgerIntegrityError(
                "Decision Ledger file is missing or unreadable"
            ) from exc
        self._assert_persistence_authority()
        if self._writer_lock_path_authority.exists():
            raise DecisionLedgerIntegrityError(
                "Decision Ledger writer lock exists; verified snapshot is unavailable"
            )
        record_count = self._verify_bytes(raw)
        return VerifiedDecisionLedgerSnapshot(
            payload=raw,
            sha256=hashlib.sha256(raw).hexdigest(),
            record_count=record_count,
        )

    def verified_records(self) -> tuple[DecisionRecord, ...]:
        snapshot = self.verified_snapshot()
        if not snapshot.payload:
            return ()
        records: list[DecisionRecord] = []
        for line in snapshot.payload.decode("utf-8").splitlines():
            envelope = json.loads(line)
            record = self._validate_record(envelope["record"])
            records.append(DecisionRecord(**record))
        return tuple(records)

    def verified_economic_decision(
        self,
        decision_id: str,
        contract: EconomicGoalContract,
        *,
        risk_policy: PaperRiskPolicy | None = None,
    ) -> DecisionRecord:
        if not isinstance(decision_id, str) or not decision_id.strip():
            raise ValueError("decision_id must be a non-empty string")
        for record in self.verified_records():
            if record.decision_id == decision_id:
                verify_economic_goal_binding(record, contract, risk_policy)
                return record
        raise DecisionLedgerIntegrityError(
            "Decision Ledger economic decision_id was not found"
        )

    def verified_economic_decision_for_material_action(
        self,
        material_action_id: str,
        contract: EconomicGoalContract,
        *,
        risk_policy: PaperRiskPolicy | None = None,
    ) -> DecisionRecord | None:
        """Return one exact durable economic action identity, or ``None`` if absent.

        The material action id is a caller-owned idempotence key.  It does not
        replace ``decision_id``; instead it lets a restarted caller prove that a
        logical money-affecting paper action already crossed the ledger durability
        boundary before creating another material position.
        """

        if not isinstance(material_action_id, str) or not material_action_id.strip():
            raise ValueError("material_action_id must be a non-empty string")
        matched: list[DecisionRecord] = []
        for record in self.verified_records():
            value = record.payload.get(MATERIAL_ACTION_ID_PAYLOAD_KEY)
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                raise DecisionLedgerIntegrityError(
                    "Decision Ledger material_action_id is invalid"
                )
            if value != material_action_id:
                continue
            if record.decision_kind != ECONOMIC_DECISION_KIND:
                raise DecisionLedgerIntegrityError(
                    "Decision Ledger material_action_id is attached to a non-economic decision"
                )
            verify_economic_goal_binding(record, contract, risk_policy)
            matched.append(record)
        if len(matched) > 1:
            raise DecisionLedgerIntegrityError(
                "Decision Ledger contains duplicate material_action_id"
            )
        return matched[0] if matched else None

    def verify_integrity(self) -> int:
        return self.verified_snapshot().record_count