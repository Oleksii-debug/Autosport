from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any, Mapping

from .integrity import durable_path_lock


SCHEMA_VERSION = 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class PolicyUtilityError(ValueError):
    """Raised when policy-utility evidence is malformed or unsafe to trust."""


class UtilityCompleteness(StrEnum):
    INCOMPLETE = "INCOMPLETE"
    UNSUPPORTED = "UNSUPPORTED"


class UtilityTruthClass(StrEnum):
    OBSERVED = "OBSERVED"
    ESTIMATED = "ESTIMATED"
    SIMULATED = "SIMULATED"


class DecisionKind(StrEnum):
    POSITIONED = "POSITIONED"
    WAIT_NO_BET = "WAIT_NO_BET"


@dataclass(frozen=True, order=True, slots=True)
class AuthorityRef:
    family: str
    evidence_id: str
    sha256: str

    def __post_init__(self) -> None:
        _text(self.family, "authority family")
        _text(self.evidence_id, "authority evidence_id")
        _sha256(self.sha256, "authority sha256")

    def to_dict(self) -> dict[str, str]:
        return {
            "family": self.family,
            "evidence_id": self.evidence_id,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AuthorityRef":
        _exact_keys(raw, {"family", "evidence_id", "sha256"}, "AuthorityRef")
        return cls(
            family=_string(raw["family"], "authority family"),
            evidence_id=_string(raw["evidence_id"], "authority evidence_id"),
            sha256=_string(raw["sha256"], "authority sha256"),
        )


@dataclass(frozen=True, slots=True)
class PolicyUtilityEvidence:
    """Fail-closed contract for owner-bound policy utility.

    Schema v1 is intentionally contract-only. It preserves incomplete or
    unsupported economic utility evidence but can never self-authorize a policy
    update. The semantic key remains the stable schema-v1 causal update key;
    owner/model/economic context remains evidence-bound and therefore conflicts
    as semantic drift for the same causal update instead of minting a second key.
    """

    environment_id: str
    episode_id: str
    action_id: str
    outcome_id: str
    reward_id: str
    transition_id: str
    policy_id: str
    model_id: str
    strategy_id: str
    config_sha256: str
    protocol_sha256: str
    economic_goal_fingerprint: str
    risk_fingerprint: str
    bankroll_id: str
    portfolio_identity: str
    utility_definition_family: str
    utility_definition_version: str
    utility_definition_sha256: str
    completeness: UtilityCompleteness
    truth_class: UtilityTruthClass
    decision_kind: DecisionKind
    available_at: datetime
    currency: str | None = None
    utility_value: Decimal | None = None
    authority_refs: tuple[AuthorityRef, ...] = ()
    denominator_ref: AuthorityRef | None = None
    counterfactual_ref: AuthorityRef | None = None
    support_count: int | None = None
    effective_sample_size: Decimal | None = None
    uncertainty: Decimal | None = None

    def __post_init__(self) -> None:
        if type(self.completeness) is not UtilityCompleteness:
            raise PolicyUtilityError("completeness must be UtilityCompleteness")
        if type(self.truth_class) is not UtilityTruthClass:
            raise PolicyUtilityError("truth_class must be UtilityTruthClass")
        if type(self.decision_kind) is not DecisionKind:
            raise PolicyUtilityError("decision_kind must be DecisionKind")
        if type(self.authority_refs) is not tuple or any(
            type(item) is not AuthorityRef for item in self.authority_refs
        ):
            raise PolicyUtilityError("authority_refs must contain exact AuthorityRef values")
        if self.denominator_ref is not None and type(self.denominator_ref) is not AuthorityRef:
            raise PolicyUtilityError("denominator_ref must be AuthorityRef or None")
        if self.counterfactual_ref is not None and type(self.counterfactual_ref) is not AuthorityRef:
            raise PolicyUtilityError("counterfactual_ref must be AuthorityRef or None")

        for value, label in (
            (self.environment_id, "environment_id"),
            (self.episode_id, "episode_id"),
            (self.action_id, "action_id"),
            (self.outcome_id, "outcome_id"),
            (self.reward_id, "reward_id"),
            (self.transition_id, "transition_id"),
            (self.policy_id, "policy_id"),
            (self.model_id, "model_id"),
            (self.strategy_id, "strategy_id"),
            (self.bankroll_id, "bankroll_id"),
            (self.portfolio_identity, "portfolio_identity"),
            (self.utility_definition_family, "utility_definition_family"),
            (self.utility_definition_version, "utility_definition_version"),
        ):
            _text(value, label)
        for value, label in (
            (self.config_sha256, "config_sha256"),
            (self.protocol_sha256, "protocol_sha256"),
            (self.economic_goal_fingerprint, "economic_goal_fingerprint"),
            (self.risk_fingerprint, "risk_fingerprint"),
            (self.utility_definition_sha256, "utility_definition_sha256"),
        ):
            _sha256(value, label)
        _utc(self.available_at, "available_at")

        if tuple(sorted(self.authority_refs)) != self.authority_refs:
            raise PolicyUtilityError("authority_refs must be sorted")
        if len(set(self.authority_refs)) != len(self.authority_refs):
            raise PolicyUtilityError("authority_refs must be unique")

        if self.currency is not None:
            if not isinstance(self.currency, str) or _CURRENCY_RE.fullmatch(self.currency) is None:
                raise PolicyUtilityError("currency must be an uppercase three-letter code")
        if self.utility_value is not None:
            _finite_decimal(self.utility_value, "utility_value")
            if self.currency is None:
                raise PolicyUtilityError("utility_value requires canonical currency")
        if self.completeness is UtilityCompleteness.UNSUPPORTED and self.utility_value is not None:
            raise PolicyUtilityError("UNSUPPORTED utility cannot carry a utility_value")

        if self.support_count is not None:
            if type(self.support_count) is not int or self.support_count <= 0:
                raise PolicyUtilityError("support_count must be a positive integer")
        if self.effective_sample_size is not None:
            _finite_decimal(self.effective_sample_size, "effective_sample_size")
            if self.effective_sample_size <= 0:
                raise PolicyUtilityError("effective_sample_size must be positive")
            if (
                self.support_count is not None
                and self.effective_sample_size > Decimal(self.support_count)
            ):
                raise PolicyUtilityError(
                    "effective_sample_size cannot exceed support_count"
                )
        if self.uncertainty is not None:
            _finite_decimal(self.uncertainty, "uncertainty")
            if self.uncertainty < 0:
                raise PolicyUtilityError("uncertainty cannot be negative")

        if self.truth_class in {UtilityTruthClass.ESTIMATED, UtilityTruthClass.SIMULATED}:
            if (
                self.support_count is None
                or self.effective_sample_size is None
                or self.uncertainty is None
            ):
                raise PolicyUtilityError(
                    "estimated/simulated utility requires support, ESS and uncertainty"
                )
        if self.truth_class is UtilityTruthClass.SIMULATED and self.counterfactual_ref is None:
            raise PolicyUtilityError("SIMULATED utility requires counterfactual authority")

        if (
            self.decision_kind is DecisionKind.WAIT_NO_BET
            and self.utility_value not in (None, Decimal("0"))
            and (self.denominator_ref is None or self.counterfactual_ref is None)
        ):
            raise PolicyUtilityError(
                "non-zero WAIT/NO_BET utility requires denominator and counterfactual authority"
            )
        if (
            self.decision_kind is DecisionKind.WAIT_NO_BET
            and self.truth_class is UtilityTruthClass.OBSERVED
            and self.utility_value not in (None, Decimal("0"))
        ):
            raise PolicyUtilityError("OBSERVED WAIT/NO_BET utility must be zero or absent")

    @property
    def source_resolved(self) -> bool:
        return False

    @property
    def policy_update_eligible(self) -> bool:
        return False

    @property
    def semantic_key(self) -> str:
        return _digest(
            {
                "environment_id": self.environment_id,
                "episode_id": self.episode_id,
                "action_id": self.action_id,
                "outcome_id": self.outcome_id,
                "reward_id": self.reward_id,
                "transition_id": self.transition_id,
                "policy_id": self.policy_id,
            }
        )

    @property
    def evidence_id(self) -> str:
        return _digest(self.payload())

    def require_policy_update_eligible(self) -> None:
        raise PolicyUtilityError(
            "schema v1 is contract-only: product-owned authority re-resolution is required "
            "before economic policy update"
        )

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "environment_id": self.environment_id,
            "episode_id": self.episode_id,
            "action_id": self.action_id,
            "outcome_id": self.outcome_id,
            "reward_id": self.reward_id,
            "transition_id": self.transition_id,
            "policy_id": self.policy_id,
            "model_id": self.model_id,
            "strategy_id": self.strategy_id,
            "config_sha256": self.config_sha256,
            "protocol_sha256": self.protocol_sha256,
            "economic_goal_fingerprint": self.economic_goal_fingerprint,
            "risk_fingerprint": self.risk_fingerprint,
            "bankroll_id": self.bankroll_id,
            "portfolio_identity": self.portfolio_identity,
            "utility_definition_family": self.utility_definition_family,
            "utility_definition_version": self.utility_definition_version,
            "utility_definition_sha256": self.utility_definition_sha256,
            "completeness": self.completeness.value,
            "truth_class": self.truth_class.value,
            "decision_kind": self.decision_kind.value,
            "available_at": _datetime_text(self.available_at),
            "currency": self.currency,
            "utility_value": None if self.utility_value is None else _decimal_text(self.utility_value),
            "authority_refs": [item.to_dict() for item in self.authority_refs],
            "denominator_ref": (
                None if self.denominator_ref is None else self.denominator_ref.to_dict()
            ),
            "counterfactual_ref": (
                None if self.counterfactual_ref is None else self.counterfactual_ref.to_dict()
            ),
            "support_count": self.support_count,
            "effective_sample_size": (
                None
                if self.effective_sample_size is None
                else _decimal_text(self.effective_sample_size)
            ),
            "uncertainty": None if self.uncertainty is None else _decimal_text(self.uncertainty),
            "source_resolved": False,
            "policy_update_eligible": False,
        }

    def to_dict(self) -> dict[str, Any]:
        raw = self.payload()
        raw["semantic_key"] = self.semantic_key
        raw["evidence_id"] = self.evidence_id
        return raw

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PolicyUtilityEvidence":
        expected = {
            "schema_version", "environment_id", "episode_id", "action_id", "outcome_id",
            "reward_id", "transition_id", "policy_id", "model_id", "strategy_id",
            "config_sha256", "protocol_sha256", "economic_goal_fingerprint",
            "risk_fingerprint", "bankroll_id", "portfolio_identity",
            "utility_definition_family", "utility_definition_version",
            "utility_definition_sha256", "completeness", "truth_class",
            "decision_kind", "available_at", "currency", "utility_value",
            "authority_refs", "denominator_ref", "counterfactual_ref",
            "support_count", "effective_sample_size", "uncertainty",
            "source_resolved", "policy_update_eligible", "semantic_key", "evidence_id",
        }
        _exact_keys(raw, expected, "PolicyUtilityEvidence")
        if type(raw["schema_version"]) is not int or raw["schema_version"] != SCHEMA_VERSION:
            raise PolicyUtilityError("unsupported policy utility schema_version")
        if raw["source_resolved"] is not False or raw["policy_update_eligible"] is not False:
            raise PolicyUtilityError("schema v1 cannot carry positive authority/eligibility")

        utility_raw = raw["utility_value"]
        ess_raw = raw["effective_sample_size"]
        uncertainty_raw = raw["uncertainty"]
        denominator_raw = raw["denominator_ref"]
        counterfactual_raw = raw["counterfactual_ref"]
        try:
            completeness = UtilityCompleteness(_string(raw["completeness"], "completeness"))
            truth_class = UtilityTruthClass(_string(raw["truth_class"], "truth_class"))
            decision_kind = DecisionKind(_string(raw["decision_kind"], "decision_kind"))
        except ValueError as exc:
            raise PolicyUtilityError("unsupported policy utility enum value") from exc

        evidence = cls(
            environment_id=_string(raw["environment_id"], "environment_id"),
            episode_id=_string(raw["episode_id"], "episode_id"),
            action_id=_string(raw["action_id"], "action_id"),
            outcome_id=_string(raw["outcome_id"], "outcome_id"),
            reward_id=_string(raw["reward_id"], "reward_id"),
            transition_id=_string(raw["transition_id"], "transition_id"),
            policy_id=_string(raw["policy_id"], "policy_id"),
            model_id=_string(raw["model_id"], "model_id"),
            strategy_id=_string(raw["strategy_id"], "strategy_id"),
            config_sha256=_string(raw["config_sha256"], "config_sha256"),
            protocol_sha256=_string(raw["protocol_sha256"], "protocol_sha256"),
            economic_goal_fingerprint=_string(
                raw["economic_goal_fingerprint"], "economic_goal_fingerprint"
            ),
            risk_fingerprint=_string(raw["risk_fingerprint"], "risk_fingerprint"),
            bankroll_id=_string(raw["bankroll_id"], "bankroll_id"),
            portfolio_identity=_string(raw["portfolio_identity"], "portfolio_identity"),
            utility_definition_family=_string(
                raw["utility_definition_family"], "utility_definition_family"
            ),
            utility_definition_version=_string(
                raw["utility_definition_version"], "utility_definition_version"
            ),
            utility_definition_sha256=_string(
                raw["utility_definition_sha256"], "utility_definition_sha256"
            ),
            completeness=completeness,
            truth_class=truth_class,
            decision_kind=decision_kind,
            available_at=_parse_datetime(raw["available_at"], "available_at"),
            currency=_optional_string(raw["currency"], "currency"),
            utility_value=(
                None if utility_raw is None else _parse_decimal(utility_raw, "utility_value")
            ),
            authority_refs=tuple(
                AuthorityRef.from_dict(_mapping(value, "authority_ref"))
                for value in _list(raw["authority_refs"], "authority_refs")
            ),
            denominator_ref=(
                None
                if denominator_raw is None
                else AuthorityRef.from_dict(_mapping(denominator_raw, "denominator_ref"))
            ),
            counterfactual_ref=(
                None
                if counterfactual_raw is None
                else AuthorityRef.from_dict(_mapping(counterfactual_raw, "counterfactual_ref"))
            ),
            support_count=_optional_positive_int(raw["support_count"], "support_count"),
            effective_sample_size=(
                None if ess_raw is None else _parse_decimal(ess_raw, "effective_sample_size")
            ),
            uncertainty=(
                None
                if uncertainty_raw is None
                else _parse_decimal(uncertainty_raw, "uncertainty")
            ),
        )
        if _string(raw["semantic_key"], "semantic_key") != evidence.semantic_key:
            raise PolicyUtilityError("policy utility semantic key mismatch")
        if _string(raw["evidence_id"], "evidence_id") != evidence.evidence_id:
            raise PolicyUtilityError("policy utility evidence digest mismatch")
        return evidence


class PolicyUtilityStore:
    """Canonical exactly-once durable store for schema-v1 utility evidence.

    Every writer uses the same cross-process path fence and compare/publish
    protocol. Publication writes a complete successor image, fsyncs the file,
    then performs platform-aware metadata-durable replacement. Windows uses
    ``MoveFileExW`` with ``MOVEFILE_WRITE_THROUGH``; POSIX uses ``os.replace``
    followed by a containing-directory fsync. No receipt is returned until that
    durability boundary succeeds.
    """

    _locks_guard = threading.Lock()
    _locks: dict[str, threading.RLock] = {}

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        key = str(self.path.resolve())
        with self._locks_guard:
            self._lock = self._locks.setdefault(key, threading.RLock())
        self._by_id: dict[str, PolicyUtilityEvidence] = {}
        self._by_semantic_key: dict[str, PolicyUtilityEvidence] = {}
        with self._lock:
            self._reload()

    def append(self, evidence: PolicyUtilityEvidence) -> bool:
        if type(evidence) is not PolicyUtilityEvidence:
            raise PolicyUtilityError("append requires exact PolicyUtilityEvidence")
        with self._lock:
            with durable_path_lock(self.path):
                self._reload()
                existing = self._by_semantic_key.get(evidence.semantic_key)
                if existing is not None:
                    if existing.evidence_id == evidence.evidence_id:
                        return False
                    raise PolicyUtilityError(
                        "policy utility semantic drift for existing causal update key"
                    )
                by_id = self._by_id.get(evidence.evidence_id)
                if by_id is not None:
                    if by_id.semantic_key == evidence.semantic_key:
                        return False
                    raise PolicyUtilityError("policy utility evidence_id collision")

                self._publish_successor(evidence)
                self._reload()
                persisted = self._by_id.get(evidence.evidence_id)
                if persisted != evidence:
                    raise PolicyUtilityError(
                        "published policy utility evidence failed exact reload verification"
                    )
                return True

    def get(self, evidence_id: str) -> PolicyUtilityEvidence:
        _sha256(evidence_id, "evidence_id")
        with self._lock:
            self._reload()
            try:
                return self._by_id[evidence_id]
            except KeyError as exc:
                raise PolicyUtilityError("unknown policy utility evidence_id") from exc

    def list(self) -> tuple[PolicyUtilityEvidence, ...]:
        with self._lock:
            self._reload()
            return tuple(self._by_id.values())

    def _publish_successor(self, evidence: PolicyUtilityEvidence) -> None:
        try:
            previous = self.path.read_bytes() if self.path.exists() else b""
        except OSError as exc:
            raise PolicyUtilityError("unable to read policy utility store") from exc
        if previous and not previous.endswith(b"\n"):
            raise PolicyUtilityError(
                "policy utility store lacks canonical trailing record boundary"
            )

        encoded = (
            json.dumps(
                evidence.to_dict(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")

        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "wb",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(previous)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())

            _durable_replace(temporary, self.path)
            temporary = None
        except PolicyUtilityError:
            raise
        except OSError as exc:
            raise PolicyUtilityError("unable to durably publish policy utility evidence") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    def _reload(self) -> None:
        by_id: dict[str, PolicyUtilityEvidence] = {}
        by_semantic: dict[str, PolicyUtilityEvidence] = {}
        if self.path.exists():
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                raise PolicyUtilityError("unable to read policy utility store") from exc
            for line_number, line in enumerate(lines, start=1):
                if not line:
                    raise PolicyUtilityError(
                        f"empty policy utility store record at line {line_number}"
                    )
                try:
                    raw = json.loads(line)
                except (json.JSONDecodeError, UnicodeError) as exc:
                    raise PolicyUtilityError(
                        f"invalid policy utility store JSON at line {line_number}"
                    ) from exc
                evidence = PolicyUtilityEvidence.from_dict(
                    _mapping(raw, f"store record line {line_number}")
                )
                existing_id = by_id.get(evidence.evidence_id)
                if existing_id is not None:
                    if existing_id != evidence:
                        raise PolicyUtilityError("policy utility evidence_id collision")
                    continue
                existing_semantic = by_semantic.get(evidence.semantic_key)
                if existing_semantic is not None:
                    if existing_semantic.evidence_id != evidence.evidence_id:
                        raise PolicyUtilityError(
                            "policy utility semantic drift in durable store"
                        )
                    continue
                by_id[evidence.evidence_id] = evidence
                by_semantic[evidence.semantic_key] = evidence
        self._by_id = by_id
        self._by_semantic_key = by_semantic


def _durable_replace(source: Path, destination: Path) -> None:
    """Publish one complete image with platform-appropriate metadata durability."""

    if os.name == "nt":
        _replace_windows_write_through(source, destination)
        return

    os.replace(source, destination)
    _fsync_directory(destination.parent)


def _replace_windows_write_through(source: Path, destination: Path) -> None:
    """Atomically replace ``destination`` and synchronously flush Windows metadata."""

    import ctypes

    movefile_replace_existing = 0x1
    movefile_write_through = 0x8
    move_file_ex = ctypes.windll.kernel32.MoveFileExW
    move_file_ex.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
    move_file_ex.restype = ctypes.c_int
    if not move_file_ex(
        str(source),
        str(destination),
        movefile_replace_existing | movefile_write_through,
    ):
        raise ctypes.WinError()


def _fsync_directory(path: Path) -> None:
    """Persist a POSIX rename by synchronizing its containing directory."""

    if not hasattr(os, "O_DIRECTORY"):
        raise PolicyUtilityError(
            "platform lacks a directory durability primitive for policy utility store"
        )
    try:
        directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise PolicyUtilityError("unable to open policy utility store directory") from exc
    try:
        os.fsync(directory_fd)
    except OSError as exc:
        raise PolicyUtilityError("unable to fsync policy utility store directory") from exc
    finally:
        os.close(directory_fd)


def _digest(raw: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        raw,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise PolicyUtilityError(f"{label} must be non-empty text")


def _sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise PolicyUtilityError(f"{label} must be lowercase 64-hex sha256")


def _utc(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise PolicyUtilityError(f"{label} must be timezone-aware")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise PolicyUtilityError(f"{label} must be UTC")


def _datetime_text(value: datetime) -> str:
    _utc(value, "datetime")
    return value.isoformat().replace("+00:00", "Z")


def _parse_datetime(value: Any, label: str) -> datetime:
    text = _string(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PolicyUtilityError(f"{label} must be ISO-8601 UTC") from exc
    _utc(parsed, label)
    if _datetime_text(parsed) != text:
        raise PolicyUtilityError(f"{label} must use canonical ISO-8601 UTC text")
    return parsed


def _finite_decimal(value: Decimal, label: str) -> None:
    if type(value) is not Decimal or not value.is_finite():
        raise PolicyUtilityError(f"{label} must be an exact finite Decimal")


def _decimal_text(value: Decimal) -> str:
    _finite_decimal(value, "decimal")
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _parse_decimal(value: Any, label: str) -> Decimal:
    if not isinstance(value, str) or not value:
        raise PolicyUtilityError(f"{label} must be canonical decimal text")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise PolicyUtilityError(f"{label} must be canonical decimal text") from exc
    _finite_decimal(parsed, label)
    if _decimal_text(parsed) != value:
        raise PolicyUtilityError(f"{label} must use canonical decimal text")
    return parsed


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PolicyUtilityError(f"{label} must be non-empty text")
    return value


def _optional_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _string(value, label)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PolicyUtilityError(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise PolicyUtilityError(f"{label} must be a list")
    return value


def _optional_positive_int(value: Any, label: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value <= 0:
        raise PolicyUtilityError(f"{label} must be a positive integer")
    return value


def _exact_keys(raw: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(raw) != expected:
        missing = sorted(expected - set(raw))
        extra = sorted(set(raw) - expected)
        raise PolicyUtilityError(
            f"{label} keys mismatch; missing={missing}; extra={extra}"
        )
