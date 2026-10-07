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
from .integrity import durable_path_lock
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


_SAFE_DECISION_PAYLOAD_KEY_TYPES = (str, int, float, bool, type(None))


def _freeze_decision_payload(value: Any) -> Any:
    if type(value) is dict:
        # Snapshot an exact built-in dict without rehashing its existing keys.
        # This lets us reject hostile key subclasses before any caller-defined
        # __hash__/__eq__ can run while still preserving the later canonical
        # JSON validator's historical rejection point for benign non-string keys.
        snapshot = dict.copy(value)
        keys = tuple(dict.keys(snapshot))
        if any(type(key) not in _SAFE_DECISION_PAYLOAD_KEY_TYPES for key in keys):
            raise ValueError(
                "decision payload mapping keys must use exact built-in scalar types"
            )
        return MappingProxyType(
            {
                key: _freeze_decision_payload(dict.__getitem__(snapshot, key))
                for key in keys
            }
        )
    if type(value) is MappingProxyType:
        # dataclasses.replace/canonical copy paths may legitimately feed an
        # already-frozen payload back through the constructor. Re-prove its
        # key types before indexing the proxy.
        keys = tuple(value.keys())
        if any(type(key) not in _SAFE_DECISION_PAYLOAD_KEY_TYPES for key in keys):
            raise ValueError(
                "decision payload mapping keys must use exact built-in scalar types"
            )
        return MappingProxyType(
            {key: _freeze_decision_payload(value[key]) for key in keys}
        )
    if isinstance(value, Mapping):
        raise ValueError("decision payload mappings must be exact dictionaries")
    if isinstance(value, list):
        return _FrozenDecisionPayloadList(
            _freeze_decision_payload(child) for child in value
        )
    if isinstance(value, _FrozenDecisionPayloadList):
        return _FrozenDecisionPayloadList(
            _freeze_decision_payload(child) for child in value
        )
    if isinstance(value, tuple):
        return tuple(_freeze_decision_payload(child) for child in value)
    return value


def _detached_decision_payload(value: Any) -> Any:
    if type(value) is MappingProxyType:
        keys = tuple(value.keys())
        if any(type(key) not in _SAFE_DECISION_PAYLOAD_KEY_TYPES for key in keys):
            raise ValueError(
                "frozen decision payload mapping keys must use exact built-in scalar types"
            )
        return {
            key: _detached_decision_payload(value[key])
            for key in keys
        }
    if isinstance(value, Mapping):
        raise ValueError(
            "frozen decision payload mappings must be exact mapping proxies"
        )
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

_MAX_DECISION_LEDGER_BYTES = 64 * 1024 * 1024


def _canonical_decision_text(value: object, field_name: str) -> str:
    """Require one exact lossless top-level decision identity spelling."""

    if (
        type(value) is not str
        or not value
        or str.strip(value) != value
        or "\x00" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field_name} must be a non-empty canonical string")
    try:
        str.encode(value, "utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must be valid UTF-8 text") from exc
    return value


def _require_canonical_decision_id(
    value: object,
    *,
    location: str = "",
) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise DecisionLedgerIntegrityError(
            f"Decision Ledger decision_id is invalid{location}"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise DecisionLedgerIntegrityError(
            f"Decision Ledger decision_id is invalid{location}"
        ) from exc
    return value


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
        for field_name in (
            "replay_run_id",
            "agent",
            "observed_ts",
            "action",
            "context_hash",
            "recorded_at",
            "decision_kind",
        ):
            _canonical_decision_text(getattr(self, field_name), field_name)
        _require_canonical_decision_id(self.decision_id)
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

    if type(record) is not DecisionRecord:
        raise TypeError("economic decision binding requires an exact DecisionRecord")
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

    if type(record) is not DecisionRecord:
        raise TypeError("economic decision verification requires an exact DecisionRecord")
    if not isinstance(contract, EconomicGoalContract):
        raise TypeError("economic decision verification requires an EconomicGoalContract")
    if record.decision_kind != ECONOMIC_DECISION_KIND:
        raise DecisionLedgerIntegrityError(
            "Decision Ledger record is not classified as a material economic decision"
        )

    payload = _detached_decision_payload(record.payload)
    evidence = payload.get(ECONOMIC_GOAL_PROVENANCE_PAYLOAD_KEY)
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
        actual_policy = payload.get(RISK_POLICY_PROVENANCE_PAYLOAD_KEY)
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
        # Canonicalize symlink aliases before deriving the durable lock domain.
        # Hard-link aliases cannot be collapsed by pathname resolution, so every
        # authoritative read/write additionally requires a single-link file identity.
        self.path = Path(path).expanduser().resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _require_regular_single_link(path_stat: os.stat_result) -> None:
        if not stat.S_ISREG(path_stat.st_mode):
            raise DecisionLedgerIntegrityError(
                "Decision Ledger path must be a regular non-symlink file"
            )
        if path_stat.st_nlink != 1:
            raise DecisionLedgerIntegrityError(
                "Decision Ledger path must not have hard-link aliases"
            )

    @classmethod
    def _require_same_file_identity(
        cls,
        opened: os.stat_result,
        path_stat: os.stat_result,
    ) -> None:
        cls._require_regular_single_link(opened)
        cls._require_regular_single_link(path_stat)
        if opened.st_dev != path_stat.st_dev or opened.st_ino != path_stat.st_ino:
            raise DecisionLedgerIntegrityError(
                "Decision Ledger path changed during durable access"
            )

    def _verified_read_under_lock(self) -> bytes:
        try:
            path_before = os.stat(self.path, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise DecisionLedgerIntegrityError(
                "Decision Ledger file is missing or unreadable"
            ) from exc
        except OSError as exc:
            raise DecisionLedgerIntegrityError(
                "Decision Ledger file is missing or unreadable"
            ) from exc
        self._require_regular_single_link(path_before)
        if path_before.st_size > _MAX_DECISION_LEDGER_BYTES:
            raise DecisionLedgerIntegrityError(
                "Decision Ledger exceeds durable resource limit"
            )
        try:
            with self.path.open("rb") as handle:
                opened = os.fstat(handle.fileno())
                path_opened = os.stat(self.path, follow_symlinks=False)
                self._require_same_file_identity(opened, path_opened)
                if opened.st_size > _MAX_DECISION_LEDGER_BYTES:
                    raise DecisionLedgerIntegrityError(
                        "Decision Ledger exceeds durable resource limit"
                    )
                raw = handle.read()
                opened_after = os.fstat(handle.fileno())
                path_after = os.stat(self.path, follow_symlinks=False)
                self._require_same_file_identity(opened_after, path_after)
                if (
                    opened.st_dev != opened_after.st_dev
                    or opened.st_ino != opened_after.st_ino
                    or opened.st_size != opened_after.st_size
                    or opened.st_mtime_ns != opened_after.st_mtime_ns
                    or opened.st_ctime_ns != opened_after.st_ctime_ns
                ):
                    raise DecisionLedgerIntegrityError(
                        "Decision Ledger changed during durable read"
                    )
                return raw
        except DecisionLedgerIntegrityError:
            raise
        except OSError as exc:
            raise DecisionLedgerIntegrityError(
                "Decision Ledger file is missing or unreadable"
            ) from exc

    @staticmethod
    def _require_utf8_text(value: str, *, path: str) -> None:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise DecisionLedgerIntegrityError(
                f"Decision Ledger JSON text at {path} is not valid UTF-8"
            ) from exc

    @classmethod
    def _require_material_action_id(
        cls,
        value: object,
        *,
        location: str = "",
    ) -> str:
        if (
            type(value) is not str
            or not value
            or value != value.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise DecisionLedgerIntegrityError(
                f"Decision Ledger material_action_id is invalid{location}"
            )
        cls._require_utf8_text(value, path="payload.material_action_id")
        return value

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
        # Exact-fence top-level identity text before the generic JSON walk so a
        # hostile str subclass cannot execute virtual encode/strip/hash behavior first.
        for field_name in cls._STRING_FIELDS:
            value = record.get(field_name)
            if field_name == "decision_id":
                _require_canonical_decision_id(value, location=location)
                continue
            try:
                _canonical_decision_text(value, field_name)
            except ValueError as exc:
                raise DecisionLedgerIntegrityError(
                    f"Decision Ledger record field {field_name!r} is invalid{location}: {exc}"
                ) from exc
        if "decision_kind" in record:
            try:
                _canonical_decision_text(record["decision_kind"], "decision_kind")
            except ValueError as exc:
                raise DecisionLedgerIntegrityError(
                    f"Decision Ledger decision_kind is invalid{location}: {exc}"
                ) from exc
        try:
            cls._validate_json_value(record, path="record")
        except RecursionError as exc:
            raise DecisionLedgerIntegrityError(
                f"Decision Ledger record nesting is too deep{location}"
            ) from exc
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
        material_action_id = payload.get(MATERIAL_ACTION_ID_PAYLOAD_KEY)
        if material_action_id is not None:
            # material_action_id is a cross-restart idempotence key, not proof of
            # EconomicGoal authority. Legacy/no-goal paper actions legitimately
            # carry it as GENERAL records. Economic lookups separately require
            # ECONOMIC_DECISION_KIND plus verified goal/risk provenance.
            cls._require_material_action_id(
                material_action_id,
                location=location,
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
        if type(record) is not DecisionRecord:
            raise TypeError("Decision Ledger append requires an exact DecisionRecord")
        payload = self._validate_record(DecisionRecord.to_dict(record))
        canonical = self._canonical_record(payload)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        envelope = json.dumps(
            {"sha256": digest, "record": payload},
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
        encoded = (envelope + "\n").encode("utf-8")

        # The complete read -> collision proof -> append transaction is one
        # cooperating-writer critical section.  durable_path_lock alone is
        # pathname-scoped, so the ledger file itself is also required to be a
        # single-link regular file: two hard-link aliases must never acquire
        # different sidecar locks and race the same material_action_id.
        with durable_path_lock(self.path):
            handle = None
            try:
                try:
                    handle = self.path.open("x+b")
                    existing = b""
                except FileExistsError:
                    path_before = os.stat(self.path, follow_symlinks=False)
                    self._require_regular_single_link(path_before)
                    handle = self.path.open("r+b")
                    opened = os.fstat(handle.fileno())
                    path_opened = os.stat(self.path, follow_symlinks=False)
                    self._require_same_file_identity(opened, path_opened)
                    handle.seek(0)
                    existing = handle.read()
                    opened_after_read = os.fstat(handle.fileno())
                    path_after_read = os.stat(self.path, follow_symlinks=False)
                    self._require_same_file_identity(
                        opened_after_read,
                        path_after_read,
                    )
                    if (
                        opened_after_read.st_size > _MAX_DECISION_LEDGER_BYTES
                        or opened.st_dev != opened_after_read.st_dev
                        or opened.st_ino != opened_after_read.st_ino
                        or opened.st_size != opened_after_read.st_size
                        or opened.st_mtime_ns != opened_after_read.st_mtime_ns
                        or opened.st_ctime_ns != opened_after_read.st_ctime_ns
                    ):
                        raise DecisionLedgerIntegrityError(
                            "Decision Ledger changed during pre-append verification"
                        )

                assert handle is not None
                opened_for_write = os.fstat(handle.fileno())
                path_for_write = os.stat(self.path, follow_symlinks=False)
                self._require_same_file_identity(opened_for_write, path_for_write)
                if len(existing) + len(encoded) > _MAX_DECISION_LEDGER_BYTES:
                    raise DecisionLedgerIntegrityError(
                        "Decision Ledger append exceeds durable resource limit"
                    )
                self._verify_bytes(
                    existing,
                    reserved_decision_id=payload["decision_id"],
                    reserved_material_action_id=payload["payload"].get(
                        MATERIAL_ACTION_ID_PAYLOAD_KEY
                    ),
                )
                handle.seek(0, os.SEEK_END)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())

                written = os.fstat(handle.fileno())
                path_written = os.stat(self.path, follow_symlinks=False)
                self._require_same_file_identity(written, path_written)
            except DecisionLedgerIntegrityError:
                raise
            except OSError as exc:
                raise DecisionLedgerIntegrityError(
                    "Decision Ledger durable append failed"
                ) from exc
            finally:
                if handle is not None:
                    handle.close()
        return digest

    def append(self, record: DecisionRecord) -> str:
        """Persist a non-economic decision only."""

        if type(record) is not DecisionRecord:
            raise TypeError("Decision Ledger append requires an exact DecisionRecord")
        payload = _detached_decision_payload(record.payload)
        if (
            record.decision_kind == ECONOMIC_DECISION_KIND
            or ECONOMIC_GOAL_PROVENANCE_PAYLOAD_KEY in payload
            or RISK_POLICY_PROVENANCE_PAYLOAD_KEY in payload
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
    def _verify_bytes(
        cls,
        raw: bytes,
        *,
        reserved_decision_id: str | None = None,
        reserved_material_action_id: str | None = None,
    ) -> int:
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
        seen_material_action_ids: set[str] = set()
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

            decision_id = _require_canonical_decision_id(
                record["decision_id"],
                location=f" at line {line_number}",
            )
            if decision_id in seen_decision_ids:
                raise DecisionLedgerIntegrityError(
                    f"Decision Ledger contains duplicate decision_id at line {line_number}"
                )
            seen_decision_ids.add(decision_id)
            material_action_id = record["payload"].get(
                MATERIAL_ACTION_ID_PAYLOAD_KEY
            )
            if material_action_id is not None:
                material_action_id = cls._require_material_action_id(
                    material_action_id,
                    location=f" at line {line_number}",
                )
                if material_action_id in seen_material_action_ids:
                    raise DecisionLedgerIntegrityError(
                        "Decision Ledger contains duplicate material_action_id "
                        f"at line {line_number}"
                    )
                seen_material_action_ids.add(material_action_id)
            line_count += 1

        if (
            reserved_decision_id is not None
            and reserved_decision_id in seen_decision_ids
        ):
            raise DecisionLedgerIntegrityError(
                "Decision Ledger already contains decision_id"
            )
        if reserved_material_action_id is not None:
            reserved_material_action_id = cls._require_material_action_id(
                reserved_material_action_id
            )
            if reserved_material_action_id in seen_material_action_ids:
                raise DecisionLedgerIntegrityError(
                    "Decision Ledger already contains material_action_id"
                )
        return line_count

    def verified_snapshot(self) -> VerifiedDecisionLedgerSnapshot:
        with durable_path_lock(self.path):
            raw = self._verified_read_under_lock()
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
        try:
            decision_id = _require_canonical_decision_id(decision_id)
        except DecisionLedgerIntegrityError as exc:
            raise ValueError("decision_id must be exact canonical text") from exc
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

        try:
            material_action_id = self._require_material_action_id(
                material_action_id
            )
        except DecisionLedgerIntegrityError as exc:
            raise ValueError(
                "material_action_id must be exact canonical text"
            ) from exc
        matched: list[DecisionRecord] = []
        for record in self.verified_records():
            value = record.payload.get(MATERIAL_ACTION_ID_PAYLOAD_KEY)
            if value is None:
                continue
            self._require_material_action_id(value)
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