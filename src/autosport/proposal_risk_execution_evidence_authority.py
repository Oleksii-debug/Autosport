from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, localcontext

from .proposal_risk_evaluation_precommit_authority import (
    ProductProposalRiskEvaluationPrecommit,
)


_SCHEMA = "autosport.proposal-risk-execution-evidence.v1"
_BOUND_METHOD = "HOEFFDING_ONE_SIDED_BERNOULLI_V1"
_EXECUTION_SCOPE = "EXACT_PROPOSAL_TARGET_FIXED_STAKE_VECTOR_COUNTERFACTUAL_V1"
_HEX = frozenset("0123456789abcdef")
_MAX_DECIMAL_TEXT = 256


class ProductProposalRiskExecutionEvidenceError(RuntimeError):
    """Counterfactual proposal execution evidence is incomplete or inconsistent."""


def _text(value: object, name: str, *, max_length: int = 512) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProductProposalRiskExecutionEvidenceError(
            f"{name} must be non-empty canonical text"
        )
    if (
        len(value) > max_length
        or "\x00" in value
        or "\r" in value
        or "\n" in value
        or any(ord(ch) < 32 for ch in value)
    ):
        raise ProductProposalRiskExecutionEvidenceError(
            f"{name} contains unsupported characters"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ProductProposalRiskExecutionEvidenceError(
            f"{name} must be valid UTF-8"
        ) from exc
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if (
        len(text) != 64
        or text != text.lower()
        or any(ch not in _HEX for ch in text)
    ):
        raise ProductProposalRiskExecutionEvidenceError(
            f"{name} must be lowercase SHA-256 hex"
        )
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProductProposalRiskExecutionEvidenceError(
            f"{name} must be valid ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProductProposalRiskExecutionEvidenceError(
            f"{name} must include a timezone offset"
        )
    return parsed.astimezone(timezone.utc)


def _decimal(value: object, name: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise ProductProposalRiskExecutionEvidenceError(
            f"{name} must be a finite exact Decimal"
        )
    _decimal_text(value, name)
    return value


def _decimal_text(value: Decimal, name: str) -> str:
    if type(value) is not Decimal or not value.is_finite():
        raise ProductProposalRiskExecutionEvidenceError(
            f"{name} must be a finite exact Decimal"
        )
    if value.is_zero():
        return "0"
    parts = value.as_tuple()
    exponent = int(parts.exponent)
    digits = len(parts.digits)
    sign = 1 if parts.sign else 0
    if exponent >= 0:
        length = sign + digits + exponent
    elif digits + exponent > 0:
        length = sign + digits + 1
    else:
        length = sign + 2 - exponent
    if length > _MAX_DECIMAL_TEXT:
        raise ProductProposalRiskExecutionEvidenceError(
            f"{name} exceeds supported canonical decimal size"
        )
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProductProposalRiskExecutionEvidenceError(
            "execution evidence is outside canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class CounterfactualMemberExecutionEvidence:
    """One source-backed counterfactual execution of the exact proposal target."""

    member_id: str
    binding_sha256: str
    target_sha256: str
    candidate_vector_sha256: str
    executed_stakes: tuple[Decimal, ...]
    execution_engine_sha256: str
    source_sha256: str
    observed_at: str
    starting_equity: Decimal
    minimum_equity: Decimal
    terminal_equity: Decimal
    gross_pnl: Decimal
    costs: Decimal
    net_pnl: Decimal

    def __post_init__(self) -> None:
        _text(self.member_id, "member_id")
        _sha(self.binding_sha256, "binding_sha256")
        _sha(self.target_sha256, "target_sha256")
        _sha(self.candidate_vector_sha256, "candidate_vector_sha256")
        _sha(self.execution_engine_sha256, "execution_engine_sha256")
        _sha(self.source_sha256, "source_sha256")
        _instant(self.observed_at, "observed_at")
        if type(self.executed_stakes) is not tuple or not self.executed_stakes:
            raise ProductProposalRiskExecutionEvidenceError(
                "executed_stakes must be a non-empty exact tuple"
            )
        for index, stake in enumerate(self.executed_stakes):
            parsed = _decimal(stake, f"executed_stakes[{index}]")
            if parsed < 0:
                raise ProductProposalRiskExecutionEvidenceError(
                    f"executed_stakes[{index}] must be non-negative"
                )
        starting = _decimal(self.starting_equity, "starting_equity")
        minimum = _decimal(self.minimum_equity, "minimum_equity")
        terminal = _decimal(self.terminal_equity, "terminal_equity")
        gross = _decimal(self.gross_pnl, "gross_pnl")
        costs = _decimal(self.costs, "costs")
        net = _decimal(self.net_pnl, "net_pnl")
        if starting <= 0:
            raise ProductProposalRiskExecutionEvidenceError(
                "starting_equity must be positive"
            )
        if costs < 0:
            raise ProductProposalRiskExecutionEvidenceError("costs must be non-negative")
        if net != gross - costs:
            raise ProductProposalRiskExecutionEvidenceError(
                "net_pnl must equal gross_pnl minus costs"
            )
        if terminal != starting + net:
            raise ProductProposalRiskExecutionEvidenceError(
                "terminal_equity must equal starting_equity plus net_pnl"
            )
        if minimum > starting or minimum > terminal:
            raise ProductProposalRiskExecutionEvidenceError(
                "minimum_equity cannot exceed starting or terminal equity"
            )


def _make_identity_capability():
    token = object()

    def proven(instance: object) -> bool:
        return getattr(instance, "_execution_evidence_capability", None) is token

    def bind(instance: object) -> None:
        object.__setattr__(instance, "_execution_evidence_capability", token)

    return proven, bind


_IDENTITY_PROVEN, _BIND_IDENTITY = _make_identity_capability()
del _make_identity_capability


@dataclass(frozen=True, slots=True, init=False)
class ProductProposalRiskExecutionEvidence:
    """Product-qualified fixed-N counterfactual evidence for one exact proposal.

    This object proves only target-specific counterfactual execution evidence and a
    statistical upper bound for that exact target. It deliberately cannot authorize
    a ticket, broker/exchange side effect, real-money action, risk approval, or state
    mutation.
    """

    workspace_instance_id: str
    precommit_binding_sha256: str
    target_sha256: str
    candidate_vector_sha256: str
    evaluated_stakes: tuple[Decimal, ...]
    planned_member_ids: tuple[str, ...]
    execution_engine_sha256: str
    evaluated_at: str
    member_source_sha256s: tuple[str, ...]
    member_observed_ats: tuple[str, ...]
    member_starting_equities: tuple[Decimal, ...]
    member_minimum_equities: tuple[Decimal, ...]
    member_terminal_equities: tuple[Decimal, ...]
    member_gross_pnls: tuple[Decimal, ...]
    member_costs: tuple[Decimal, ...]
    member_net_pnls: tuple[Decimal, ...]
    ruin_observations: tuple[bool, ...]
    ruin_count: int
    sample_size: int
    confidence_level: Decimal
    ruin_threshold: Decimal
    bound_method: str
    ruin_probability_upper_bound: Decimal
    evidence_sha256: str
    _execution_evidence_capability: object = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __new__(cls, *args: object, **kwargs: object) -> "ProductProposalRiskExecutionEvidence":
        raise TypeError(
            "ProductProposalRiskExecutionEvidence is product-derived; use "
            "derive_product_proposal_risk_execution_evidence"
        )

    @property
    def execution_evidence_identity_proven(self, _proven=_IDENTITY_PROVEN) -> bool:
        return _proven(self)

    @property
    def fixed_n_cohort_complete(self, _proven=_IDENTITY_PROVEN) -> bool:
        return _proven(self)

    @property
    def proposal_target_counterfactual_execution_proven(
        self, _proven=_IDENTITY_PROVEN
    ) -> bool:
        return _proven(self)

    @property
    def risk_upper_bound_for_target(self, _proven=_IDENTITY_PROVEN) -> bool:
        return _proven(self)

    @property
    def proposal_target_risk_qualified(self, _proven=_IDENTITY_PROVEN) -> bool:
        return _proven(self) and self.ruin_probability_upper_bound <= self.ruin_threshold

    @property
    def grants_risk_approval_authority(self) -> bool:
        return False

    @property
    def grants_ticket_authority(self) -> bool:
        return False

    @property
    def grants_broker_execution_authority(self) -> bool:
        return False

    @property
    def grants_real_money_authority(self) -> bool:
        return False

    @property
    def grants_state_mutation_authority(self) -> bool:
        return False


_RESULT_FIELDS = (
    "workspace_instance_id",
    "precommit_binding_sha256",
    "target_sha256",
    "candidate_vector_sha256",
    "evaluated_stakes",
    "planned_member_ids",
    "execution_engine_sha256",
    "evaluated_at",
    "member_source_sha256s",
    "member_observed_ats",
    "member_starting_equities",
    "member_minimum_equities",
    "member_terminal_equities",
    "member_gross_pnls",
    "member_costs",
    "member_net_pnls",
    "ruin_observations",
    "ruin_count",
    "sample_size",
    "confidence_level",
    "ruin_threshold",
    "bound_method",
    "ruin_probability_upper_bound",
    "evidence_sha256",
)


def _hoeffding_upper_bound(*, ruin_count: int, sample_size: int, confidence: Decimal) -> Decimal:
    if type(ruin_count) is not int or type(sample_size) is not int:
        raise ProductProposalRiskExecutionEvidenceError(
            "ruin count and sample size must be exact integers"
        )
    if sample_size <= 0 or ruin_count < 0 or ruin_count > sample_size:
        raise ProductProposalRiskExecutionEvidenceError("invalid fixed-N ruin counts")
    confidence = _decimal(confidence, "confidence_level")
    if confidence <= 0 or confidence >= 1:
        raise ProductProposalRiskExecutionEvidenceError(
            "confidence_level must be strictly between zero and one"
        )
    with localcontext() as context:
        context.prec = 50
        n = Decimal(sample_size)
        empirical = Decimal(ruin_count) / n
        alpha = Decimal(1) - confidence
        radius = ((-alpha.ln()) / (Decimal(2) * n)).sqrt()
        upper = empirical + radius
        if upper > 1:
            upper = Decimal(1)
        return +upper


def _evidence_payload(values: dict[str, object]) -> dict[str, object]:
    return {
        "schema": _SCHEMA,
        "workspace_instance_id": values["workspace_instance_id"],
        "precommit_binding_sha256": values["precommit_binding_sha256"],
        "target_sha256": values["target_sha256"],
        "candidate_vector_sha256": values["candidate_vector_sha256"],
        "evaluated_stakes": [
            _decimal_text(value, "evaluated_stake")
            for value in values["evaluated_stakes"]
        ],
        "planned_member_ids": list(values["planned_member_ids"]),
        "execution_engine_sha256": values["execution_engine_sha256"],
        "evaluated_at": values["evaluated_at"],
        "member_source_sha256s": list(values["member_source_sha256s"]),
        "member_observed_ats": list(values["member_observed_ats"]),
        "member_starting_equities": [
            _decimal_text(value, "starting_equity")
            for value in values["member_starting_equities"]
        ],
        "member_minimum_equities": [
            _decimal_text(value, "minimum_equity")
            for value in values["member_minimum_equities"]
        ],
        "member_terminal_equities": [
            _decimal_text(value, "terminal_equity")
            for value in values["member_terminal_equities"]
        ],
        "member_gross_pnls": [
            _decimal_text(value, "gross_pnl") for value in values["member_gross_pnls"]
        ],
        "member_costs": [
            _decimal_text(value, "costs") for value in values["member_costs"]
        ],
        "member_net_pnls": [
            _decimal_text(value, "net_pnl") for value in values["member_net_pnls"]
        ],
        "ruin_observations": list(values["ruin_observations"]),
        "ruin_count": values["ruin_count"],
        "sample_size": values["sample_size"],
        "confidence_level": _decimal_text(values["confidence_level"], "confidence_level"),
        "ruin_threshold": _decimal_text(values["ruin_threshold"], "ruin_threshold"),
        "bound_method": values["bound_method"],
        "ruin_probability_upper_bound": _decimal_text(
            values["ruin_probability_upper_bound"],
            "ruin_probability_upper_bound",
        ),
    }


def _mint(values: dict[str, object]) -> ProductProposalRiskExecutionEvidence:
    instance = object.__new__(ProductProposalRiskExecutionEvidence)
    for name in _RESULT_FIELDS:
        object.__setattr__(instance, name, values[name])
    _BIND_IDENTITY(instance)
    return instance


def derive_product_proposal_risk_execution_evidence(
    precommit: ProductProposalRiskEvaluationPrecommit,
    rows: tuple[CounterfactualMemberExecutionEvidence, ...],
    *,
    evaluated_at: str,
) -> ProductProposalRiskExecutionEvidence:
    """Validate complete target-specific fixed-N evidence and derive its risk bound."""

    if type(precommit) is not ProductProposalRiskEvaluationPrecommit:
        raise ProductProposalRiskExecutionEvidenceError(
            "precommit must be an exact ProductProposalRiskEvaluationPrecommit"
        )
    if not precommit.binding_identity_proven:
        raise ProductProposalRiskExecutionEvidenceError(
            "precommit binding identity is not product-proven"
        )
    if precommit.proposal_evaluation_scope != _EXECUTION_SCOPE:
        raise ProductProposalRiskExecutionEvidenceError(
            "precommit proposal evaluation scope is unsupported"
        )
    if (
        precommit.proposal_target_counterfactual_execution_proven
        or precommit.risk_upper_bound_for_target
        or precommit.grants_ticket_authority
        or precommit.grants_real_money_authority
    ):
        raise ProductProposalRiskExecutionEvidenceError(
            "precommit authority boundary is inconsistent"
        )
    if type(rows) is not tuple or not rows:
        raise ProductProposalRiskExecutionEvidenceError(
            "rows must be a non-empty exact tuple"
        )
    if len(rows) != len(precommit.planned_member_ids):
        raise ProductProposalRiskExecutionEvidenceError(
            "execution evidence must contain the exact fixed-N cohort"
        )

    evaluated_dt = _instant(evaluated_at, "evaluated_at")
    reveal_after = _instant(
        precommit.membership_outcome_reveal_after,
        "membership_outcome_reveal_after",
    )
    if evaluated_dt < reveal_after:
        raise ProductProposalRiskExecutionEvidenceError(
            "evaluation cannot precede the precommitted outcome reveal boundary"
        )

    expected_ids = precommit.planned_member_ids
    actual_ids = tuple(row.member_id for row in rows)
    if actual_ids != expected_ids or len(set(actual_ids)) != len(actual_ids):
        raise ProductProposalRiskExecutionEvidenceError(
            "execution evidence member order must exactly match the precommitted cohort"
        )

    source_shas: list[str] = []
    observed_ats: list[str] = []
    starting_equities: list[Decimal] = []
    minimum_equities: list[Decimal] = []
    terminal_equities: list[Decimal] = []
    gross_pnls: list[Decimal] = []
    costs: list[Decimal] = []
    net_pnls: list[Decimal] = []
    ruin_observations: list[bool] = []
    execution_engine_sha256: str | None = None

    for index, row in enumerate(rows):
        if type(row) is not CounterfactualMemberExecutionEvidence:
            raise ProductProposalRiskExecutionEvidenceError(
                f"rows[{index}] must be exact CounterfactualMemberExecutionEvidence"
            )
        if row.binding_sha256 != precommit.binding_sha256:
            raise ProductProposalRiskExecutionEvidenceError(
                f"rows[{index}] is not bound to this evaluation precommit"
            )
        if row.target_sha256 != precommit.target_sha256:
            raise ProductProposalRiskExecutionEvidenceError(
                f"rows[{index}] is not bound to this proposal target"
            )
        if row.candidate_vector_sha256 != precommit.candidate_vector_sha256:
            raise ProductProposalRiskExecutionEvidenceError(
                f"rows[{index}] candidate vector changed after precommit"
            )
        if row.executed_stakes != precommit.evaluated_stakes:
            raise ProductProposalRiskExecutionEvidenceError(
                f"rows[{index}] stake vector changed after precommit"
            )
        observed_dt = _instant(row.observed_at, f"rows[{index}].observed_at")
        if observed_dt < reveal_after:
            raise ProductProposalRiskExecutionEvidenceError(
                f"rows[{index}] predates the outcome reveal boundary"
            )
        if observed_dt > evaluated_dt:
            raise ProductProposalRiskExecutionEvidenceError(
                f"rows[{index}] is later than evaluated_at"
            )
        if execution_engine_sha256 is None:
            execution_engine_sha256 = row.execution_engine_sha256
        elif row.execution_engine_sha256 != execution_engine_sha256:
            raise ProductProposalRiskExecutionEvidenceError(
                "fixed-N cohort must use one exact execution engine"
            )

        source_shas.append(row.source_sha256)
        observed_ats.append(row.observed_at)
        starting_equities.append(row.starting_equity)
        minimum_equities.append(row.minimum_equity)
        terminal_equities.append(row.terminal_equity)
        gross_pnls.append(row.gross_pnl)
        costs.append(row.costs)
        net_pnls.append(row.net_pnl)
        ruin_observations.append(row.minimum_equity <= 0)

    if len(set(source_shas)) != len(source_shas):
        raise ProductProposalRiskExecutionEvidenceError(
            "each fixed-N member requires distinct source evidence"
        )
    if any(value != starting_equities[0] for value in starting_equities[1:]):
        raise ProductProposalRiskExecutionEvidenceError(
            "fixed-N members must share one exact starting equity"
        )

    confidence = _decimal(precommit.confidence_level, "confidence_level")
    threshold = _decimal(precommit.ruin_threshold, "ruin_threshold")
    if threshold < 0 or threshold > 1:
        raise ProductProposalRiskExecutionEvidenceError(
            "ruin_threshold must be between zero and one"
        )
    ruin_count = sum(1 for value in ruin_observations if value)
    sample_size = len(rows)
    upper = _hoeffding_upper_bound(
        ruin_count=ruin_count,
        sample_size=sample_size,
        confidence=confidence,
    )

    values: dict[str, object] = {
        "workspace_instance_id": precommit.workspace_instance_id,
        "precommit_binding_sha256": precommit.binding_sha256,
        "target_sha256": precommit.target_sha256,
        "candidate_vector_sha256": precommit.candidate_vector_sha256,
        "evaluated_stakes": precommit.evaluated_stakes,
        "planned_member_ids": expected_ids,
        "execution_engine_sha256": execution_engine_sha256,
        "evaluated_at": evaluated_at,
        "member_source_sha256s": tuple(source_shas),
        "member_observed_ats": tuple(observed_ats),
        "member_starting_equities": tuple(starting_equities),
        "member_minimum_equities": tuple(minimum_equities),
        "member_terminal_equities": tuple(terminal_equities),
        "member_gross_pnls": tuple(gross_pnls),
        "member_costs": tuple(costs),
        "member_net_pnls": tuple(net_pnls),
        "ruin_observations": tuple(ruin_observations),
        "ruin_count": ruin_count,
        "sample_size": sample_size,
        "confidence_level": confidence,
        "ruin_threshold": threshold,
        "bound_method": _BOUND_METHOD,
        "ruin_probability_upper_bound": upper,
        "evidence_sha256": "",
    }
    values["evidence_sha256"] = _digest(_evidence_payload(values))
    return _mint(values)
