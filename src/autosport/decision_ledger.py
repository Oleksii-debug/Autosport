from __future__ import annotations

import hashlib
import json
import math
import os
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
MATERIAL_ACTION_ID_PAYLOAD_KEY = "material_action_id"


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


def bind_economic_goal(
    record: DecisionRecord,
    contract: EconomicGoalContract,
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
    if ECONOMIC_GOAL_PROVENANCE_PAYLOAD_KEY in payload:
        raise DecisionLedgerIntegrityError(
            "Decision Ledger economic-goal provenance must be derived, not caller supplied"
        )

    provenance = provenance_for(contract)
    payload[ECONOMIC_GOAL_PROVENANCE_PAYLOAD_KEY] = _economic_goal_provenance_payload(
        provenance
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
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

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
        payload = self._validate_record(record.to_dict())
        canonical = self._canonical_record(payload)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        envelope = json.dumps(
            {"sha256": digest, "record": payload},
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(envelope + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return digest

    def append(self, record: DecisionRecord) -> str:
        """Persist a non-economic decision only."""

        if not isinstance(record, DecisionRecord):
            raise TypeError("Decision Ledger append requires a DecisionRecord")
        if (
            record.decision_kind == ECONOMIC_DECISION_KIND
            or ECONOMIC_GOAL_PROVENANCE_PAYLOAD_KEY in record.payload
        ):
            raise DecisionLedgerIntegrityError(
                "Decision Ledger material economic decision must use append_economic"
            )
        return self._append_validated(record)

    def append_economic(
        self,
        record: DecisionRecord,
        contract: EconomicGoalContract,
    ) -> str:
        """Persist a material economic decision with derived goal provenance bound."""

        return self._append_validated(bind_economic_goal(record, contract))

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

    def verified_snapshot(self) -> VerifiedDecisionLedgerSnapshot:
        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            raise DecisionLedgerIntegrityError(
                "Decision Ledger file is missing or unreadable"
            ) from exc
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
    ) -> DecisionRecord:
        if not isinstance(decision_id, str) or not decision_id.strip():
            raise ValueError("decision_id must be a non-empty string")
        for record in self.verified_records():
            if record.decision_id == decision_id:
                verify_economic_goal_binding(record, contract)
                return record
        raise DecisionLedgerIntegrityError(
            "Decision Ledger economic decision_id was not found"
        )

    def verified_economic_decision_for_material_action(
        self,
        material_action_id: str,
        contract: EconomicGoalContract,
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
            verify_economic_goal_binding(record, contract)
            matched.append(record)
        if len(matched) > 1:
            raise DecisionLedgerIntegrityError(
                "Decision Ledger contains duplicate material_action_id"
            )
        return matched[0] if matched else None

    def verify_integrity(self) -> int:
        return self.verified_snapshot().record_count