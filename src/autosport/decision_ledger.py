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

    def __post_init__(self) -> None:
        payload = _freeze_decision_payload(self.payload)
        if contains_forbidden_future_key(payload):
            raise ValueError("decision payload must not contain future-result fields")
        object.__setattr__(self, "payload", payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "replay_run_id": self.replay_run_id,
            "agent": self.agent,
            "observed_ts": self.observed_ts,
            "action": self.action,
            "payload": _detached_decision_payload(self.payload),
            "context_hash": self.context_hash,
            "decision_id": self.decision_id,
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True, slots=True)
class VerifiedDecisionLedgerSnapshot:
    """One immutable ledger byte snapshot bound to its semantic proof and SHA-256."""

    payload: bytes
    sha256: str
    record_count: int


class JsonlDecisionLedger:
    """Append-only causal decision ledger. Result/outcome fields do not belong here."""

    _ENVELOPE_FIELDS = frozenset({"sha256", "record"})
    _RECORD_FIELDS = frozenset(
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
        """Reject values whose JSON encoding changes identity or is non-standard."""

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
        if not isinstance(record, dict) or set(record) != cls._RECORD_FIELDS:
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
        payload = record.get("payload")
        if not isinstance(payload, dict):
            raise DecisionLedgerIntegrityError(
                f"Decision Ledger record field 'payload' is invalid{location}"
            )
        if contains_forbidden_future_key(payload):
            raise DecisionLedgerIntegrityError(
                f"Decision Ledger payload contains future-result fields{location}"
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

    def append(self, record: DecisionRecord) -> str:
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

    @classmethod
    def _verify_bytes(cls, raw: bytes) -> int:
        """Validate one already-captured immutable JSONL byte snapshot."""

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
        """Read once, then hash and semantically validate the exact same bytes."""

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

    def verify_integrity(self) -> int:
        """Validate every durable JSONL envelope and return the number of decisions."""

        return self.verified_snapshot().record_count
