from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from .integrity import atomic_write_json
from .scientific_registry import ResearchOutcome
from .workspace_lock import WorkspaceEconomicLock


_HEX = frozenset("0123456789abcdef")


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty canonical string")
    value.encode("utf-8")
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(char not in _HEX for char in text):
        raise ValueError(f"{name} must be a canonical SHA-256 hex string")
    return text


def _iso(value: object, name: str) -> str:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return text


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite Decimal")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite Decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    return parsed


def _decimal_text(value: Decimal) -> str:
    value = _decimal(value, "decimal")
    if value.is_zero():
        return "0"
    sign, digits, exponent = value.as_tuple()
    body = "".join(str(digit) for digit in digits)
    if exponent >= 0:
        body = body + ("0" * exponent)
    else:
        point = len(body) + exponent
        if point <= 0:
            body = "0." + ("0" * (-point)) + body
        else:
            body = body[:point] + "." + body[point:]
        body = body.rstrip("0").rstrip(".")
    return ("-" if sign else "") + body


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _decimal_coefficient(value: Decimal) -> tuple[int, int]:
    sign, digits, exponent = _decimal(value, "decimal").as_tuple()
    coefficient = int("".join(str(digit) for digit in digits) or "0")
    if sign:
        coefficient = -coefficient
    return coefficient, exponent


def _exact_sum(values: tuple[Decimal, ...]) -> Decimal:
    if not values:
        return Decimal("0")
    decimals = tuple(_decimal(value, "decimal") for value in values)
    minimum_exponent = min(value.as_tuple().exponent for value in decimals)
    scaled_total = 0
    for value in decimals:
        coefficient, exponent = _decimal_coefficient(value)
        scaled_total += coefficient * (10 ** (exponent - minimum_exponent))
    if scaled_total == 0:
        return Decimal("0")
    sign = 1 if scaled_total < 0 else 0
    digits = tuple(int(char) for char in str(abs(scaled_total)))
    return Decimal((sign, digits, minimum_exponent))


def _exact_multiply_int(value: Decimal, multiplier: int) -> Decimal:
    if type(multiplier) is not int or multiplier < 0:
        raise ValueError("multiplier must be a non-negative integer")
    coefficient, exponent = _decimal_coefficient(value)
    product = coefficient * multiplier
    if product == 0:
        return Decimal("0")
    sign = 1 if product < 0 else 0
    digits = tuple(int(char) for char in str(abs(product)))
    return Decimal((sign, digits, exponent))


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


class MetricDirection(StrEnum):
    HIGHER_IS_BETTER = "HIGHER_IS_BETTER"
    LOWER_IS_BETTER = "LOWER_IS_BETTER"


class MultiplicityControlKind(StrEnum):
    FWER = "FWER"
    FDR = "FDR"
    E_VALUE = "E_VALUE"
    OTHER = "OTHER"


class SequentialDecision(StrEnum):
    CONTINUE = "CONTINUE"
    REJECT_NULL = "REJECT_NULL"
    RETAIN_NULL = "RETAIN_NULL"
    TERMINAL_NEGATIVE = "TERMINAL_NEGATIVE"
    TERMINAL_HARMFUL = "TERMINAL_HARMFUL"
    TERMINAL_INCONCLUSIVE = "TERMINAL_INCONCLUSIVE"


@dataclass(frozen=True, slots=True)
class ExperimentFamilyMember:
    """Stable family member bound to existing scientific identities.

    candidate_label is display metadata only. The authority identity deliberately
    excludes it so renaming a candidate cannot mint fresh multiplicity capacity.
    """

    hypothesis_id: str
    hypothesis_sha256: str
    semantic_variant_sha256: str
    candidate_label: str

    def __post_init__(self) -> None:
        _text(self.hypothesis_id, "hypothesis_id")
        _sha256(self.hypothesis_sha256, "hypothesis_sha256")
        _sha256(self.semantic_variant_sha256, "semantic_variant_sha256")
        _text(self.candidate_label, "candidate_label")

    def authority_payload(self) -> dict[str, Any]:
        return {
            "hypothesis_sha256": self.hypothesis_sha256.lower(),
            "semantic_variant_sha256": self.semantic_variant_sha256.lower(),
        }

    @property
    def member_authority_id(self) -> str:
        return _digest(self.authority_payload())

    def to_payload(self) -> dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_sha256": self.hypothesis_sha256.lower(),
            "semantic_variant_sha256": self.semantic_variant_sha256.lower(),
            "candidate_label": self.candidate_label,
            "member_authority_id": self.member_authority_id,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "ExperimentFamilyMember":
        if type(payload) is not dict:
            raise ValueError("family member payload must be an object")
        required = {
            "hypothesis_id",
            "hypothesis_sha256",
            "semantic_variant_sha256",
            "candidate_label",
            "member_authority_id",
        }
        if set(payload) != required:
            raise ValueError("family member payload fields mismatch")
        member = cls(
            hypothesis_id=payload["hypothesis_id"],
            hypothesis_sha256=payload["hypothesis_sha256"],
            semantic_variant_sha256=payload["semantic_variant_sha256"],
            candidate_label=payload["candidate_label"],
        )
        if payload["member_authority_id"] != member.member_authority_id:
            raise ValueError("family member authority identity mismatch")
        return member


@dataclass(frozen=True, slots=True)
class ExperimentFamilyPlan:
    """Frozen multiplicity/sequential contract for one comparison family.

    The implemented path is a conservative predeclared-batch alpha-spending FWER
    contract. control_kind and method_id stay explicit so downstream code cannot
    silently relabel this method as FDR, e-value or universal statistical evidence.
    """

    family_id: str
    research_protocol_id: str
    protocol_sha256: str
    research_question_id: str
    primary_metric: str
    direction: MetricDirection
    control_kind: MultiplicityControlKind
    method_id: str
    stopping_rule: str
    familywise_alpha: Decimal
    look_alpha_spend: tuple[Decimal, ...]
    members: tuple[ExperimentFamilyMember, ...]
    frozen_at: str
    schema_version: int = 1

    IMPLEMENTED_METHOD = "autosport.predeclared-batch-alpha-spending-fwer.v1"

    def __post_init__(self) -> None:
        for name in (
            "family_id",
            "research_protocol_id",
            "research_question_id",
            "primary_metric",
            "method_id",
            "stopping_rule",
            "frozen_at",
        ):
            _text(getattr(self, name), name)
        _sha256(self.protocol_sha256, "protocol_sha256")
        _iso(self.frozen_at, "frozen_at")
        if not isinstance(self.direction, MetricDirection):
            raise ValueError("direction must be a MetricDirection")
        if not isinstance(self.control_kind, MultiplicityControlKind):
            raise ValueError("control_kind must be a MultiplicityControlKind")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("schema_version must be 1")
        alpha = _decimal(self.familywise_alpha, "familywise_alpha")
        if alpha <= 0 or alpha >= 1:
            raise ValueError("familywise_alpha must be > 0 and < 1")
        if not isinstance(self.look_alpha_spend, tuple) or not self.look_alpha_spend:
            raise ValueError("look_alpha_spend must be a non-empty tuple")
        spends = tuple(_decimal(value, "look_alpha_spend") for value in self.look_alpha_spend)
        if any(value <= 0 or value >= 1 for value in spends):
            raise ValueError("each look alpha spend must be > 0 and < 1")
        if not isinstance(self.members, tuple) or not self.members:
            raise ValueError("members must be a non-empty tuple")
        if any(not isinstance(member, ExperimentFamilyMember) for member in self.members):
            raise ValueError("members must contain ExperimentFamilyMember values")
        authority_ids = tuple(member.member_authority_id for member in self.members)
        if len(authority_ids) != len(set(authority_ids)):
            raise ValueError("experiment family contains duplicate semantic members")
        if self.method_id != self.IMPLEMENTED_METHOD:
            raise ValueError("unsupported sequential multiplicity method")
        if self.control_kind is not MultiplicityControlKind.FWER:
            raise ValueError("implemented alpha-spending method provides FWER control only")
        total_per_member = _exact_sum(spends)
        total_family_spend = _exact_multiply_int(total_per_member, len(self.members))
        if total_family_spend > alpha:
            raise ValueError("predeclared family alpha spending exceeds familywise_alpha")

    def authority_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "family_id": self.family_id,
            "research_protocol_id": self.research_protocol_id,
            "protocol_sha256": self.protocol_sha256.lower(),
            "research_question_id": self.research_question_id,
            "primary_metric": self.primary_metric,
            "direction": self.direction.value,
            "control_kind": self.control_kind.value,
            "method_id": self.method_id,
            "stopping_rule": self.stopping_rule,
            "familywise_alpha": _decimal_text(self.familywise_alpha),
            "look_alpha_spend": [_decimal_text(value) for value in self.look_alpha_spend],
            "members": [
                {
                    "hypothesis_id": member.hypothesis_id,
                    **member.authority_payload(),
                }
                for member in sorted(self.members, key=lambda value: value.member_authority_id)
            ],
            "frozen_at": self.frozen_at,
        }

    @property
    def plan_sha256(self) -> str:
        return _digest(self.authority_payload())

    def to_payload(self) -> dict[str, Any]:
        return {
            **self.authority_payload(),
            "members": [
                member.to_payload()
                for member in sorted(self.members, key=lambda value: value.member_authority_id)
            ],
            "plan_sha256": self.plan_sha256,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "ExperimentFamilyPlan":
        if type(payload) is not dict:
            raise ValueError("experiment family plan payload must be an object")
        required = {
            "schema_version",
            "family_id",
            "research_protocol_id",
            "protocol_sha256",
            "research_question_id",
            "primary_metric",
            "direction",
            "control_kind",
            "method_id",
            "stopping_rule",
            "familywise_alpha",
            "look_alpha_spend",
            "members",
            "frozen_at",
            "plan_sha256",
        }
        if set(payload) != required:
            raise ValueError("experiment family plan payload fields mismatch")
        if type(payload["look_alpha_spend"]) is not list:
            raise ValueError("look_alpha_spend payload must be a list")
        if type(payload["members"]) is not list:
            raise ValueError("members payload must be a list")
        plan = cls(
            schema_version=payload["schema_version"],
            family_id=payload["family_id"],
            research_protocol_id=payload["research_protocol_id"],
            protocol_sha256=payload["protocol_sha256"],
            research_question_id=payload["research_question_id"],
            primary_metric=payload["primary_metric"],
            direction=MetricDirection(payload["direction"]),
            control_kind=MultiplicityControlKind(payload["control_kind"]),
            method_id=payload["method_id"],
            stopping_rule=payload["stopping_rule"],
            familywise_alpha=_decimal(payload["familywise_alpha"], "familywise_alpha"),
            look_alpha_spend=tuple(
                _decimal(value, "look_alpha_spend")
                for value in payload["look_alpha_spend"]
            ),
            members=tuple(
                ExperimentFamilyMember.from_payload(value)
                for value in payload["members"]
            ),
            frozen_at=payload["frozen_at"],
        )
        if payload["plan_sha256"] != plan.plan_sha256:
            raise ValueError("experiment family plan identity mismatch")
        return plan

    def member(self, authority_id: str) -> ExperimentFamilyMember:
        wanted = _sha256(authority_id, "member_authority_id")
        matches = tuple(
            member for member in self.members if member.member_authority_id == wanted
        )
        if len(matches) != 1:
            raise ValueError("member_authority_id is not in the frozen experiment family")
        return matches[0]

    def alpha_for_look(self, look_index: int) -> Decimal:
        if type(look_index) is not int or look_index < 1:
            raise ValueError("look_index must be an integer >= 1")
        if look_index > len(self.look_alpha_spend):
            raise ValueError("look_index exceeds the frozen stopping rule")
        return self.look_alpha_spend[look_index - 1]


@dataclass(frozen=True, slots=True)
class SequentialLookEvidence:
    family_plan_sha256: str
    member_authority_id: str
    hypothesis_id: str
    experiment_id: str
    evaluation_bundle_id: str
    evaluation_bundle_sha256: str
    look_index: int
    observed_p_value: Decimal
    classification: ResearchOutcome
    observed_at: str
    candidate_label: str

    def __post_init__(self) -> None:
        _sha256(self.family_plan_sha256, "family_plan_sha256")
        _sha256(self.member_authority_id, "member_authority_id")
        for name in (
            "hypothesis_id",
            "experiment_id",
            "evaluation_bundle_id",
            "observed_at",
            "candidate_label",
        ):
            _text(getattr(self, name), name)
        _sha256(self.evaluation_bundle_sha256, "evaluation_bundle_sha256")
        _iso(self.observed_at, "observed_at")
        if type(self.look_index) is not int or self.look_index < 1:
            raise ValueError("look_index must be an integer >= 1")
        p_value = _decimal(self.observed_p_value, "observed_p_value")
        if p_value < 0 or p_value > 1:
            raise ValueError("observed_p_value must be between 0 and 1")
        if not isinstance(self.classification, ResearchOutcome):
            raise ValueError("classification must be a ResearchOutcome")

    def authority_payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "family_plan_sha256": self.family_plan_sha256.lower(),
            "member_authority_id": self.member_authority_id.lower(),
            "hypothesis_id": self.hypothesis_id,
            "experiment_id": self.experiment_id,
            "evaluation_bundle_id": self.evaluation_bundle_id,
            "evaluation_bundle_sha256": self.evaluation_bundle_sha256.lower(),
            "look_index": self.look_index,
            "observed_p_value": _decimal_text(self.observed_p_value),
            "classification": self.classification.value,
            "observed_at": self.observed_at,
        }

    @property
    def evidence_sha256(self) -> str:
        return _digest(self.authority_payload())

    def to_payload(self) -> dict[str, Any]:
        return {
            **self.authority_payload(),
            "candidate_label": self.candidate_label,
            "evidence_sha256": self.evidence_sha256,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "SequentialLookEvidence":
        if type(payload) is not dict:
            raise ValueError("sequential look payload must be an object")
        required = {
            "schema_version",
            "family_plan_sha256",
            "member_authority_id",
            "hypothesis_id",
            "experiment_id",
            "evaluation_bundle_id",
            "evaluation_bundle_sha256",
            "look_index",
            "observed_p_value",
            "classification",
            "observed_at",
            "candidate_label",
            "evidence_sha256",
        }
        if set(payload) != required or payload["schema_version"] != 1:
            raise ValueError("sequential look payload fields/schema mismatch")
        evidence = cls(
            family_plan_sha256=payload["family_plan_sha256"],
            member_authority_id=payload["member_authority_id"],
            hypothesis_id=payload["hypothesis_id"],
            experiment_id=payload["experiment_id"],
            evaluation_bundle_id=payload["evaluation_bundle_id"],
            evaluation_bundle_sha256=payload["evaluation_bundle_sha256"],
            look_index=payload["look_index"],
            observed_p_value=_decimal(payload["observed_p_value"], "observed_p_value"),
            classification=ResearchOutcome(payload["classification"]),
            observed_at=payload["observed_at"],
            candidate_label=payload["candidate_label"],
        )
        if payload["evidence_sha256"] != evidence.evidence_sha256:
            raise ValueError("sequential look evidence identity mismatch")
        return evidence


@dataclass(frozen=True, slots=True)
class SequentialAssessment:
    evidence: SequentialLookEvidence
    decision: SequentialDecision
    alpha_boundary: Decimal

    @property
    def terminal(self) -> bool:
        return self.decision is not SequentialDecision.CONTINUE


def assess_sequential_look(
    plan: ExperimentFamilyPlan,
    evidence: SequentialLookEvidence,
) -> SequentialAssessment:
    if evidence.family_plan_sha256 != plan.plan_sha256:
        raise ValueError("look evidence is bound to a different experiment family plan")
    if datetime.fromisoformat(evidence.observed_at.replace("Z", "+00:00")) < datetime.fromisoformat(
        plan.frozen_at.replace("Z", "+00:00")
    ):
        raise ValueError("look evidence predates the frozen experiment family plan")
    member = plan.member(evidence.member_authority_id)
    if evidence.hypothesis_id != member.hypothesis_id:
        raise ValueError("look evidence hypothesis_id does not match frozen family member")
    boundary = plan.alpha_for_look(evidence.look_index)
    if evidence.classification is ResearchOutcome.HARMFUL:
        decision = SequentialDecision.TERMINAL_HARMFUL
    elif evidence.classification is ResearchOutcome.NEGATIVE:
        decision = SequentialDecision.TERMINAL_NEGATIVE
    elif evidence.classification is ResearchOutcome.INCONCLUSIVE:
        decision = SequentialDecision.TERMINAL_INCONCLUSIVE
    elif evidence.observed_p_value <= boundary:
        decision = SequentialDecision.REJECT_NULL
    elif evidence.look_index == len(plan.look_alpha_spend):
        decision = SequentialDecision.RETAIN_NULL
    else:
        decision = SequentialDecision.CONTINUE
    return SequentialAssessment(evidence=evidence, decision=decision, alpha_boundary=boundary)


class SequentialMultiplicityEvidenceStore:
    """Restart-safe append-only logical journal for one frozen experiment family.

    Every accepted look is persisted, including null/negative/harmful outcomes.
    This store does not mint promotion authority; it provides reproducible
    multiplicity/sequential evidence for later canonical registry binding.

    A separately bootstrapped canonical workspace authority owns one immutable
    semantic-member enrollment registry across every nested store path. Store
    initialization never creates that authority, so deleting it cannot silently
    rebootstrap consumed alpha/evidence. Workspace-wide locking serializes both
    enrollment and evidence mutations across sibling directories.
    """

    SCHEMA_VERSION = 1
    WORKSPACE_SCHEMA_VERSION = 1
    ENROLLMENT_SCHEMA_VERSION = 2
    BOOTSTRAP_MARKER_SCHEMA_VERSION = 1
    WORKSPACE_AUTHORITY_FILE = ".research-multiplicity-workspace.json"
    ENROLLMENT_FILE = ".research-multiplicity-enrollment.json"
    BOOTSTRAP_MARKER_FILE = ".research-multiplicity-bootstrap.json"

    def __init__(
        self,
        path: str | Path,
        *,
        workspace_root: str | Path | None = None,
    ) -> None:
        self.path = Path(path).resolve(strict=False)
        self.workspace_root = self._resolve_workspace_root(self.path, workspace_root)
        self._read_state()

    @classmethod
    def _workspace_authority_path(cls, workspace: Path) -> Path:
        return workspace / cls.WORKSPACE_AUTHORITY_FILE

    @classmethod
    def _enrollment_path(cls, workspace: Path) -> Path:
        return workspace / cls.ENROLLMENT_FILE

    @classmethod
    def _bootstrap_marker_path(cls, workspace: Path) -> Path:
        return workspace / cls.BOOTSTRAP_MARKER_FILE

    @classmethod
    def _read_bootstrap_marker(cls, workspace: Path) -> None:
        marker_path = cls._bootstrap_marker_path(workspace)
        try:
            raw = marker_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ValueError("multiplicity workspace bootstrap marker is missing") from exc
        try:
            state = json.loads(
                raw,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite,
            )
        except json.JSONDecodeError as exc:
            raise ValueError(
                "multiplicity workspace bootstrap marker must be valid UTF-8 JSON"
            ) from exc
        if type(state) is not dict or set(state) != {"schema_version", "authority_kind"}:
            raise ValueError("multiplicity workspace bootstrap marker fields mismatch")
        if state["schema_version"] != cls.BOOTSTRAP_MARKER_SCHEMA_VERSION:
            raise ValueError("multiplicity workspace bootstrap marker schema_version mismatch")
        if state["authority_kind"] != "autosport.research-multiplicity-bootstrap.v1":
            raise ValueError("multiplicity workspace bootstrap marker kind mismatch")

    @classmethod
    def _write_bootstrap_marker(cls, workspace: Path) -> None:
        atomic_write_json(
            cls._bootstrap_marker_path(workspace),
            {
                "schema_version": cls.BOOTSTRAP_MARKER_SCHEMA_VERSION,
                "authority_kind": "autosport.research-multiplicity-bootstrap.v1",
            },
        )

    @classmethod
    def _read_workspace_authority(cls, workspace: Path) -> None:
        authority_path = cls._workspace_authority_path(workspace)
        try:
            raw = authority_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ValueError("canonical multiplicity workspace authority is missing") from exc
        try:
            state = json.loads(
                raw,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite,
            )
        except json.JSONDecodeError as exc:
            raise ValueError(
                "canonical multiplicity workspace authority must be valid UTF-8 JSON"
            ) from exc
        if type(state) is not dict or set(state) != {"schema_version", "authority_kind"}:
            raise ValueError("canonical multiplicity workspace authority fields mismatch")
        if state["schema_version"] != cls.WORKSPACE_SCHEMA_VERSION:
            raise ValueError("canonical multiplicity workspace authority schema_version mismatch")
        if state["authority_kind"] != "autosport.research-multiplicity-workspace.v1":
            raise ValueError("canonical multiplicity workspace authority kind mismatch")

    @classmethod
    def _find_workspace_roots(cls, target: Path) -> tuple[Path, ...]:
        parent = target.parent.resolve(strict=False)
        candidates = (parent, *parent.parents)
        return tuple(
            candidate
            for candidate in candidates
            if cls._workspace_authority_path(candidate).exists()
        )

    @classmethod
    def _resolve_workspace_root(
        cls,
        target: Path,
        workspace_root: str | Path | None,
    ) -> Path:
        target = target.resolve(strict=False)
        discovered = cls._find_workspace_roots(target)
        if len(discovered) > 1:
            raise ValueError("nested multiplicity workspace authorities are forbidden")
        if workspace_root is None:
            if not discovered:
                raise ValueError(
                    "canonical multiplicity workspace authority is missing; "
                    "bootstrap it explicitly before creating stores"
                )
            root = discovered[0]
        else:
            root = Path(workspace_root).resolve(strict=False)
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise ValueError("multiplicity store path must be inside workspace_root") from exc
            if not discovered:
                raise ValueError("canonical multiplicity workspace authority is missing")
            if discovered[0] != root:
                raise ValueError("workspace_root does not match canonical multiplicity authority")
        cls._read_workspace_authority(root)
        cls._read_workspace_enrollments(root)
        marker_path = cls._bootstrap_marker_path(root)
        if marker_path.exists():
            cls._read_bootstrap_marker(root)
        return root

    @classmethod
    def _contains_existing_store(cls, workspace: Path) -> bool:
        for candidate in workspace.rglob("*"):
            if not candidate.is_file():
                continue
            if candidate.name in {
                cls.WORKSPACE_AUTHORITY_FILE,
                cls.ENROLLMENT_FILE,
                cls.BOOTSTRAP_MARKER_FILE,
            }:
                continue
            try:
                raw = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise ValueError(
                    "unreadable workspace file prevents multiplicity authority rebootstrap"
                ) from exc

            recognizable_text = (
                '"plan"' in raw
                and '"records"' in raw
                and (
                    '"plan_sha256"' in raw
                    or '"family_plan_sha256"' in raw
                    or '"familywise_alpha"' in raw
                )
            )
            try:
                state = json.loads(
                    raw,
                    object_pairs_hook=_reject_duplicate_keys,
                    parse_constant=_reject_nonfinite,
                )
            except (json.JSONDecodeError, ValueError) as exc:
                if recognizable_text:
                    raise ValueError(
                        "recognizable multiplicity evidence is corrupt; "
                        "refusing workspace authority rebootstrap"
                    ) from exc
                continue

            if type(state) is not dict:
                if recognizable_text:
                    raise ValueError(
                        "recognizable multiplicity evidence is invalid; "
                        "refusing workspace authority rebootstrap"
                    )
                continue
            plan = state.get("plan")
            recognizable_state = (
                "records" in state
                and type(plan) is dict
                and (
                    {"family_id", "method_id", "plan_sha256"}.issubset(plan)
                    or {
                        "research_protocol_id",
                        "familywise_alpha",
                        "look_alpha_spend",
                    }.issubset(plan)
                )
            )
            if not recognizable_state:
                if recognizable_text:
                    raise ValueError(
                        "recognizable multiplicity evidence is invalid; "
                        "refusing workspace authority rebootstrap"
                    )
                continue

            if (
                set(state) != {"schema_version", "plan", "records"}
                or state["schema_version"] != cls.SCHEMA_VERSION
                or type(state["records"]) is not list
            ):
                raise ValueError(
                    "recognizable multiplicity evidence is invalid; "
                    "refusing workspace authority rebootstrap"
                )
            try:
                ExperimentFamilyPlan.from_payload(plan)
            except ValueError as exc:
                raise ValueError(
                    "recognizable multiplicity evidence is invalid; "
                    "refusing workspace authority rebootstrap"
                ) from exc
            return True
        return False

    @classmethod
    def initialize_workspace(cls, workspace_root: str | Path) -> Path:
        """Explicitly bootstrap one canonical multiplicity workspace authority.

        Store creation is intentionally separate from this operation. A missing or
        partial authority after initialization is treated as corruption, never as
        permission to mint a new pristine multiplicity budget.
        """

        workspace = Path(workspace_root).resolve(strict=False)
        workspace.mkdir(parents=True, exist_ok=True)
        ancestor_roots = tuple(
            ancestor
            for ancestor in workspace.parents
            if cls._workspace_authority_path(ancestor).exists()
            or cls._bootstrap_marker_path(ancestor).exists()
        )
        if ancestor_roots:
            raise ValueError("nested multiplicity workspace authorities are forbidden")
        descendant_roots = tuple(
            path.parent
            for path in (
                *workspace.rglob(cls.WORKSPACE_AUTHORITY_FILE),
                *workspace.rglob(cls.BOOTSTRAP_MARKER_FILE),
            )
            if path.parent != workspace
        )
        if descendant_roots:
            raise ValueError("workspace cannot enclose another multiplicity authority")

        authority_path = cls._workspace_authority_path(workspace)
        enrollment_path = cls._enrollment_path(workspace)
        marker_path = cls._bootstrap_marker_path(workspace)
        with WorkspaceEconomicLock(workspace):
            authority_exists = authority_path.exists()
            enrollment_exists = enrollment_path.exists()
            marker_exists = marker_path.exists()
            if authority_exists != enrollment_exists:
                raise ValueError("multiplicity workspace authority is incomplete")
            if authority_exists:
                cls._read_workspace_authority(workspace)
                cls._read_workspace_enrollments(workspace)
                if marker_exists:
                    cls._read_bootstrap_marker(workspace)
                else:
                    cls._write_bootstrap_marker(workspace)
                return workspace
            if marker_exists:
                cls._read_bootstrap_marker(workspace)
                raise ValueError(
                    "multiplicity workspace bootstrap marker prevents authority rebootstrap"
                )
            if cls._contains_existing_store(workspace):
                raise ValueError(
                    "existing multiplicity evidence prevents workspace authority rebootstrap"
                )
            cls._write_bootstrap_marker(workspace)
            atomic_write_json(
                authority_path,
                {
                    "schema_version": cls.WORKSPACE_SCHEMA_VERSION,
                    "authority_kind": "autosport.research-multiplicity-workspace.v1",
                },
            )
            atomic_write_json(
                enrollment_path,
                {
                    "schema_version": cls.ENROLLMENT_SCHEMA_VERSION,
                    "members": {},
                },
            )
        return workspace

    @classmethod
    def _read_workspace_enrollments(
        cls,
        workspace: Path,
    ) -> dict[str, dict[str, str]]:
        enrollment_path = cls._enrollment_path(workspace)
        try:
            raw = enrollment_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ValueError("multiplicity workspace enrollment authority is missing") from exc
        try:
            state = json.loads(
                raw,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite,
            )
        except json.JSONDecodeError as exc:
            raise ValueError(
                "multiplicity workspace enrollment authority must be valid UTF-8 JSON"
            ) from exc
        if type(state) is not dict or set(state) != {"schema_version", "members"}:
            raise ValueError("multiplicity workspace enrollment authority fields mismatch")
        if state["schema_version"] != cls.ENROLLMENT_SCHEMA_VERSION:
            raise ValueError(
                "multiplicity workspace enrollment authority schema_version mismatch"
            )
        members = state["members"]
        if type(members) is not dict:
            raise ValueError("multiplicity workspace enrollment members must be an object")

        required = {
            "family_id",
            "family_plan_sha256",
            "research_protocol_id",
            "protocol_sha256",
            "research_question_id",
            "store_path",
        }
        normalized: dict[str, dict[str, str]] = {}
        for raw_authority_id, raw_record in members.items():
            authority_id = _sha256(raw_authority_id, "member_authority_id")
            if type(raw_record) is not dict or set(raw_record) != required:
                raise ValueError(
                    "multiplicity workspace enrollment member fields mismatch"
                )
            normalized[authority_id] = {
                "family_id": _text(raw_record["family_id"], "family_id"),
                "family_plan_sha256": _sha256(
                    raw_record["family_plan_sha256"], "family_plan_sha256"
                ),
                "research_protocol_id": _text(
                    raw_record["research_protocol_id"], "research_protocol_id"
                ),
                "protocol_sha256": _sha256(
                    raw_record["protocol_sha256"], "protocol_sha256"
                ),
                "research_question_id": _text(
                    raw_record["research_question_id"], "research_question_id"
                ),
                "store_path": _text(raw_record["store_path"], "store_path"),
            }
        return normalized

    @classmethod
    def _canonical_store_path(cls, target: Path, workspace: Path) -> str:
        target = target.resolve(strict=False)
        workspace = workspace.resolve(strict=False)
        try:
            relative = target.relative_to(workspace)
        except ValueError as exc:
            raise ValueError("multiplicity store path must be inside canonical workspace") from exc
        if relative == Path("."):
            raise ValueError("multiplicity store path must name a file inside canonical workspace")
        return relative.as_posix()

    @classmethod
    def _expected_enrollment(
        cls,
        target: Path,
        workspace: Path,
        plan: ExperimentFamilyPlan,
    ) -> dict[str, str]:
        return {
            "family_id": plan.family_id,
            "family_plan_sha256": plan.plan_sha256,
            "research_protocol_id": plan.research_protocol_id,
            "protocol_sha256": plan.protocol_sha256.lower(),
            "research_question_id": plan.research_question_id,
            "store_path": cls._canonical_store_path(target, workspace),
        }

    @classmethod
    def _next_workspace_enrollment_state(
        cls,
        target: Path,
        workspace: Path,
        plan: ExperimentFamilyPlan,
    ) -> dict[str, Any]:
        enrollments = dict(cls._read_workspace_enrollments(workspace))
        expected = cls._expected_enrollment(target, workspace, plan)
        for member in plan.members:
            prior = enrollments.get(member.member_authority_id)
            if prior is not None:
                if prior == expected:
                    raise ValueError(
                        "enrolled multiplicity store is missing; refusing pristine reset"
                    )
                raise ValueError(
                    "semantic member is already enrolled in another family plan or store"
                )
            enrollments[member.member_authority_id] = dict(expected)
        return {
            "schema_version": cls.ENROLLMENT_SCHEMA_VERSION,
            "members": enrollments,
        }

    def _validate_workspace_enrollment(self, plan: ExperimentFamilyPlan) -> None:
        enrollments = self._read_workspace_enrollments(self.workspace_root)
        expected = self._expected_enrollment(self.path, self.workspace_root, plan)
        for member in plan.members:
            if enrollments.get(member.member_authority_id) != expected:
                raise ValueError(
                    "multiplicity workspace enrollment does not match frozen family/store"
                )

    @classmethod
    def initialize_pristine(
        cls,
        path: str | Path,
        plan: ExperimentFamilyPlan,
        *,
        workspace_root: str | Path | None = None,
    ) -> "SequentialMultiplicityEvidenceStore":
        target = Path(path).resolve(strict=False)
        if target.name in {
            cls.ENROLLMENT_FILE,
            cls.WORKSPACE_AUTHORITY_FILE,
            cls.BOOTSTRAP_MARKER_FILE,
        }:
            raise ValueError("multiplicity store path conflicts with workspace authority")
        workspace = cls._resolve_workspace_root(target, workspace_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        with WorkspaceEconomicLock(workspace):
            workspace = cls._resolve_workspace_root(target, workspace)
            if target.exists():
                store = cls(target, workspace_root=workspace)
                if store.plan.plan_sha256 != plan.plan_sha256:
                    raise ValueError("existing multiplicity store is bound to another family plan")
                return store

            next_enrollments = cls._next_workspace_enrollment_state(
                target,
                workspace,
                plan,
            )
            # Publish the journal before its enrollment. If the second write fails,
            # restart sees an unusable orphan and fails closed instead of silently
            # treating a pre-existing family as pristine.
            atomic_write_json(
                target,
                {
                    "schema_version": cls.SCHEMA_VERSION,
                    "plan": plan.to_payload(),
                    "records": [],
                },
            )
            atomic_write_json(
                cls._enrollment_path(workspace),
                next_enrollments,
            )
        return cls(target, workspace_root=workspace)

    def _read_state(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ValueError("multiplicity evidence store is missing") from exc
        try:
            state = json.loads(
                raw,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite,
            )
        except json.JSONDecodeError as exc:
            raise ValueError("multiplicity evidence store must be valid UTF-8 JSON") from exc
        if type(state) is not dict or set(state) != {"schema_version", "plan", "records"}:
            raise ValueError("multiplicity evidence store fields mismatch")
        if state["schema_version"] != self.SCHEMA_VERSION:
            raise ValueError("multiplicity evidence store schema_version mismatch")
        plan = ExperimentFamilyPlan.from_payload(state["plan"])
        self._validate_workspace_enrollment(plan)
        records = state["records"]
        if type(records) is not list:
            raise ValueError("multiplicity evidence records must be a list")
        by_member: dict[str, list[SequentialAssessment]] = {}
        experiment_ids: set[str] = set()
        bundle_hashes: set[str] = set()
        bundle_ids: dict[str, str] = {}
        for raw_record in records:
            evidence = SequentialLookEvidence.from_payload(raw_record)
            assessment = assess_sequential_look(plan, evidence)
            history = by_member.setdefault(evidence.member_authority_id, [])
            expected_index = len(history) + 1
            if evidence.look_index != expected_index:
                raise ValueError("persisted sequential look order is invalid")
            if history and history[-1].terminal:
                raise ValueError("persisted evidence continues after a terminal sequential decision")
            if history and datetime.fromisoformat(
                evidence.observed_at.replace("Z", "+00:00")
            ) < datetime.fromisoformat(
                history[-1].evidence.observed_at.replace("Z", "+00:00")
            ):
                raise ValueError("persisted sequential look timestamps are not monotonic")
            if evidence.experiment_id in experiment_ids:
                raise ValueError("experiment_id cannot be reused for another sequential look")
            prior_bundle_hash = bundle_ids.get(evidence.evaluation_bundle_id)
            if prior_bundle_hash is not None:
                if prior_bundle_hash != evidence.evaluation_bundle_sha256:
                    raise ValueError(
                        "evaluation_bundle_id cannot resolve to a different immutable digest"
                    )
                raise ValueError("evaluation_bundle_id has already been consumed")
            if evidence.evaluation_bundle_sha256 in bundle_hashes:
                raise ValueError("evaluation bundle evidence cannot be reused across looks")
            experiment_ids.add(evidence.experiment_id)
            bundle_hashes.add(evidence.evaluation_bundle_sha256)
            bundle_ids[evidence.evaluation_bundle_id] = evidence.evaluation_bundle_sha256
            history.append(assessment)
        return {"state": state, "plan": plan, "by_member": by_member}

    @property
    def plan(self) -> ExperimentFamilyPlan:
        return self._read_state()["plan"]

    def assessments(
        self,
        member_authority_id: str | None = None,
    ) -> tuple[SequentialAssessment, ...]:
        loaded = self._read_state()
        if member_authority_id is None:
            return tuple(
                assessment
                for histories in loaded["by_member"].values()
                for assessment in histories
            )
        wanted = _sha256(member_authority_id, "member_authority_id")
        return tuple(loaded["by_member"].get(wanted, ()))

    def append(self, evidence: SequentialLookEvidence) -> SequentialAssessment:
        with WorkspaceEconomicLock(self.workspace_root):
            loaded = self._read_state()
            plan: ExperimentFamilyPlan = loaded["plan"]
            if evidence.family_plan_sha256 != plan.plan_sha256:
                raise ValueError("look evidence is bound to another family plan")
            member = plan.member(evidence.member_authority_id)
            if evidence.hypothesis_id != member.hypothesis_id:
                raise ValueError("look evidence hypothesis_id does not match family member")
            history: list[SequentialAssessment] = loaded["by_member"].get(
                evidence.member_authority_id, []
            )
            if history and history[-1].terminal:
                raise ValueError("cannot append after a terminal sequential decision")
            if history and datetime.fromisoformat(
                evidence.observed_at.replace("Z", "+00:00")
            ) < datetime.fromisoformat(
                history[-1].evidence.observed_at.replace("Z", "+00:00")
            ):
                raise ValueError("sequential look timestamps must be monotonic")
            if evidence.look_index != len(history) + 1:
                raise ValueError("look_index must be the next predeclared sequential look")
            records: list[dict[str, Any]] = loaded["state"]["records"]
            if any(
                raw_record["experiment_id"] == evidence.experiment_id
                for raw_record in records
            ):
                raise ValueError("experiment_id has already been consumed")
            for raw_record in records:
                if raw_record["evaluation_bundle_id"] == evidence.evaluation_bundle_id:
                    if (
                        raw_record["evaluation_bundle_sha256"]
                        != evidence.evaluation_bundle_sha256.lower()
                    ):
                        raise ValueError(
                            "evaluation_bundle_id cannot resolve to a different immutable digest"
                        )
                    raise ValueError("evaluation_bundle_id has already been consumed")
            if any(
                raw_record["evaluation_bundle_sha256"]
                == evidence.evaluation_bundle_sha256.lower()
                for raw_record in records
            ):
                raise ValueError("evaluation bundle evidence has already been consumed")
            assessment = assess_sequential_look(plan, evidence)
            next_state = {
                "schema_version": self.SCHEMA_VERSION,
                "plan": loaded["state"]["plan"],
                "records": [*records, evidence.to_payload()],
            }
            atomic_write_json(self.path, next_state)
        self._read_state()
        return assessment
