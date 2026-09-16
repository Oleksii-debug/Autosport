from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Iterable

from .strategy_comparison import StrategyRunEvidence


class ExperimentDecision(StrEnum):
    CHALLENGER_ELIGIBLE = "CHALLENGER_ELIGIBLE"
    RETAIN_CHAMPION = "RETAIN_CHAMPION"


_SUPPORTED_METRICS = {"net_profit", "roi", "final_balance"}
_MAX_PROTOCOL_JSON_DEPTH = 32


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _require_sha256(value: Any, field: str) -> str:
    text = _require_text(value, field).lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{field} must be a SHA-256 hex digest")
    return text


def _require_decimal(
    value: Any, field: str, *, minimum: Decimal | None = None
) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite Decimal")
    try:
        decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field} must be a finite Decimal") from exc
    if not decimal.is_finite():
        raise ValueError(f"{field} must be a finite Decimal")
    if minimum is not None and decimal < minimum:
        raise ValueError(f"{field} must be >= {minimum}")
    return decimal


def _require_decimal_instance(
    value: Any, field: str, *, minimum: Decimal | None = None
) -> Decimal:
    if not isinstance(value, Decimal):
        raise ValueError(f"{field} must be an exact Decimal")
    return _require_decimal(value, field, minimum=minimum)


def _decimal_coefficient(value: Decimal) -> tuple[int, int]:
    value = _require_decimal_instance(value, "decimal")
    sign, digits, exponent = value.as_tuple()
    coefficient = int("".join(str(digit) for digit in digits) or "0")
    if sign:
        coefficient = -coefficient
    return coefficient, exponent


def _exact_decimal_sum(values: Iterable[Decimal]) -> Decimal:
    decimals = tuple(_require_decimal_instance(value, "decimal") for value in values)
    if not decimals:
        return Decimal("0")
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


def _exact_decimal_negate(value: Decimal) -> Decimal:
    value = _require_decimal_instance(value, "decimal")
    sign, digits, exponent = value.as_tuple()
    if all(digit == 0 for digit in digits):
        return Decimal("0")
    return Decimal((0 if sign else 1, digits, exponent))


def _exact_decimal_difference(left: Decimal, right: Decimal) -> Decimal:
    return _exact_decimal_sum((left, _exact_decimal_negate(right)))


def _require_bool(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field} must be a boolean")
    return value


def _require_text_tuple(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ValueError(f"{field} must be a tuple of strings")
    normalized = tuple(
        _require_text(item, f"{field}[{index}]") for index, item in enumerate(value)
    )
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field} must not contain duplicates")
    return normalized


def _require_price_source_ids(
    value: Any, field: str = "price_source_ids"
) -> tuple[str, ...]:
    return _require_text_tuple(value, field)


def _runtime_identity_sha256(
    canonical_strategy_id: str,
    agent_composition_sha256: str,
    research_plan_sha256: str | None,
) -> str:
    payload = {
        "canonical_strategy_id": _require_text(
            canonical_strategy_id, "canonical_strategy_id"
        ),
        "agent_composition_sha256": _require_sha256(
            agent_composition_sha256, "agent_composition_sha256"
        ),
        "research_plan_sha256": (
            _require_sha256(research_plan_sha256, "research_plan_sha256")
            if research_plan_sha256 is not None
            else None
        ),
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ScientificProtocolBinding:
    """Narrow immutable binding to durable #367 research preregistration.

    This deliberately is not a second research registry. The durable question and
    hypothesis remain external objects identified by hashes. The binding captures
    the promotion-critical preregistration fields and its SHA must already be
    present in each candidate run as ``research_plan_sha256``.
    """

    research_protocol_id: str
    research_question_id: str
    research_question_sha256: str
    hypothesis_id: str
    hypothesis_sha256: str
    inclusion_criteria: str
    exclusion_criteria: str
    lawful_source_requirements: str
    causal_cutoff: str
    evaluation_design: str
    feature_set_version: str
    uncertainty_method: str
    multiple_comparison_control: str
    robustness_checks: tuple[str, ...]
    random_seed_policy: str
    stopping_rule: str
    promotion_rule: str
    expected_artifacts: tuple[str, ...]
    code_config_sha256: str
    frozen_at_utc: str
    protocol_version: int = 1

    def __post_init__(self) -> None:
        for field in (
            "research_protocol_id",
            "research_question_id",
            "hypothesis_id",
            "inclusion_criteria",
            "exclusion_criteria",
            "lawful_source_requirements",
            "causal_cutoff",
            "evaluation_design",
            "feature_set_version",
            "uncertainty_method",
            "multiple_comparison_control",
            "random_seed_policy",
            "stopping_rule",
            "promotion_rule",
            "frozen_at_utc",
        ):
            _require_text(getattr(self, field), field)
        _require_sha256(self.research_question_sha256, "research_question_sha256")
        _require_sha256(self.hypothesis_sha256, "hypothesis_sha256")
        _require_sha256(self.code_config_sha256, "code_config_sha256")
        _require_text_tuple(self.robustness_checks, "robustness_checks")
        _require_text_tuple(self.expected_artifacts, "expected_artifacts")
        if type(self.protocol_version) is not int or self.protocol_version < 1:
            raise ValueError("protocol_version must be an integer >= 1")

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "research_protocol_id": self.research_protocol_id,
            "research_question_id": self.research_question_id,
            "research_question_sha256": self.research_question_sha256.lower(),
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_sha256": self.hypothesis_sha256.lower(),
            "inclusion_criteria": self.inclusion_criteria,
            "exclusion_criteria": self.exclusion_criteria,
            "lawful_source_requirements": self.lawful_source_requirements,
            "causal_cutoff": self.causal_cutoff,
            "evaluation_design": self.evaluation_design,
            "feature_set_version": self.feature_set_version,
            "uncertainty_method": self.uncertainty_method,
            "multiple_comparison_control": self.multiple_comparison_control,
            "robustness_checks": list(self.robustness_checks),
            "random_seed_policy": self.random_seed_policy,
            "stopping_rule": self.stopping_rule,
            "promotion_rule": self.promotion_rule,
            "expected_artifacts": list(self.expected_artifacts),
            "code_config_sha256": self.code_config_sha256.lower(),
            "frozen_at_utc": self.frozen_at_utc,
        }

    @property
    def binding_sha256(self) -> str:
        canonical = json.dumps(
            self.canonical_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    case_id: str
    dataset_name: str
    sport: str
    dataset_schema_version: int
    market_sha256: str
    sealed_results_sha256: str
    historical_import_identity: str | None
    replay_dataset_hash: str
    event_count: int
    price_semantics: str
    executable_quote_verified: bool
    paper_fill_fidelity_verified: bool
    price_source_ids: tuple[str, ...]
    initial_bankroll: Decimal

    def __post_init__(self) -> None:
        _require_text(self.case_id, "case_id")
        _require_text(self.dataset_name, "dataset_name")
        _require_text(self.sport, "sport")
        if type(self.dataset_schema_version) is not int or self.dataset_schema_version < 1:
            raise ValueError("dataset_schema_version must be an integer >= 1")
        _require_sha256(self.market_sha256, "market_sha256")
        _require_sha256(self.sealed_results_sha256, "sealed_results_sha256")
        if self.historical_import_identity is not None:
            _require_sha256(self.historical_import_identity, "historical_import_identity")
        _require_sha256(self.replay_dataset_hash, "replay_dataset_hash")
        if type(self.event_count) is not int or self.event_count < 1:
            raise ValueError("event_count must be an integer >= 1")
        _require_text(self.price_semantics, "price_semantics")
        _require_bool(self.executable_quote_verified, "executable_quote_verified")
        _require_bool(self.paper_fill_fidelity_verified, "paper_fill_fidelity_verified")
        _require_price_source_ids(self.price_source_ids)
        _require_decimal_instance(self.initial_bankroll, "initial_bankroll")

    @property
    def identity(self) -> tuple[Any, ...]:
        return (
            self.dataset_name,
            self.sport,
            self.dataset_schema_version,
            self.market_sha256.lower(),
            self.sealed_results_sha256.lower(),
            self.historical_import_identity.lower()
            if self.historical_import_identity
            else None,
            self.replay_dataset_hash.lower(),
            self.event_count,
            self.price_semantics,
            self.executable_quote_verified,
            self.paper_fill_fidelity_verified,
            self.price_source_ids,
            self.initial_bankroll,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "dataset_name": self.dataset_name,
            "sport": self.sport,
            "dataset_schema_version": self.dataset_schema_version,
            "market_sha256": self.market_sha256.lower(),
            "sealed_results_sha256": self.sealed_results_sha256.lower(),
            "historical_import_identity": self.historical_import_identity.lower()
            if self.historical_import_identity
            else None,
            "replay_dataset_hash": self.replay_dataset_hash.lower(),
            "event_count": self.event_count,
            "price_semantics": self.price_semantics,
            "executable_quote_verified": self.executable_quote_verified,
            "paper_fill_fidelity_verified": self.paper_fill_fidelity_verified,
            "price_source_ids": list(self.price_source_ids),
            "initial_bankroll": str(self.initial_bankroll),
        }


@dataclass(frozen=True, slots=True)
class CandidateRef:
    candidate_id: str
    canonical_strategy_id: str
    authority_fingerprint: str
    agent_composition_sha256: str
    research_plan_sha256: str | None = None

    def __post_init__(self) -> None:
        for field in ("candidate_id", "canonical_strategy_id", "authority_fingerprint"):
            _require_text(getattr(self, field), field)
        _require_sha256(self.agent_composition_sha256, "agent_composition_sha256")
        if self.research_plan_sha256 is not None:
            _require_sha256(self.research_plan_sha256, "research_plan_sha256")

    @property
    def runtime_identity_sha256(self) -> str:
        return _runtime_identity_sha256(
            self.canonical_strategy_id,
            self.agent_composition_sha256,
            self.research_plan_sha256,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "canonical_strategy_id": self.canonical_strategy_id,
            "runtime_identity_sha256": self.runtime_identity_sha256,
            "authority_fingerprint": self.authority_fingerprint,
            "agent_composition_sha256": self.agent_composition_sha256.lower(),
            "research_plan_sha256": self.research_plan_sha256.lower()
            if self.research_plan_sha256
            else None,
        }


@dataclass(frozen=True, slots=True)
class GuardrailRule:
    metric: str
    higher_is_better: bool = True
    max_regression: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if self.metric not in _SUPPORTED_METRICS:
            raise ValueError(f"unsupported guardrail metric: {self.metric}")
        _require_bool(self.higher_is_better, "higher_is_better")
        _require_decimal_instance(
            self.max_regression, "max_regression", minimum=Decimal("0")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "higher_is_better": self.higher_is_better,
            "max_regression": str(self.max_regression),
        }


@dataclass(frozen=True, slots=True)
class ChampionChallengerProtocol:
    experiment_id: str
    research_question_id: str
    hypothesis_id: str
    scientific_protocol: ScientificProtocolBinding
    champion: CandidateRef
    challengers: tuple[CandidateRef, ...]
    cases: tuple[EvaluationCase, ...]
    primary_metric: str
    minimum_total_improvement: Decimal = Decimal("0")
    primary_higher_is_better: bool = True
    guardrails: tuple[GuardrailRule, ...] = ()
    protocol_schema_version: int = 1

    def __post_init__(self) -> None:
        _require_text(self.experiment_id, "experiment_id")
        _require_text(self.research_question_id, "research_question_id")
        _require_text(self.hypothesis_id, "hypothesis_id")
        if not isinstance(self.scientific_protocol, ScientificProtocolBinding):
            raise ValueError("scientific_protocol must be a ScientificProtocolBinding")
        if self.scientific_protocol.research_question_id != self.research_question_id:
            raise ValueError("scientific protocol research question identity mismatch")
        if self.scientific_protocol.hypothesis_id != self.hypothesis_id:
            raise ValueError("scientific protocol hypothesis identity mismatch")
        if type(self.protocol_schema_version) is not int or self.protocol_schema_version != 1:
            raise ValueError("protocol_schema_version must be 1")
        if not isinstance(self.champion, CandidateRef):
            raise ValueError("champion must be a CandidateRef")
        if not isinstance(self.challengers, tuple) or not all(
            isinstance(candidate, CandidateRef) for candidate in self.challengers
        ):
            raise ValueError("challengers must be a tuple of CandidateRef values")
        if not isinstance(self.cases, tuple) or not all(
            isinstance(case, EvaluationCase) for case in self.cases
        ):
            raise ValueError("cases must be a tuple of EvaluationCase values")
        if not isinstance(self.guardrails, tuple) or not all(
            isinstance(rule, GuardrailRule) for rule in self.guardrails
        ):
            raise ValueError("guardrails must be a tuple of GuardrailRule values")
        if not self.challengers:
            raise ValueError("at least one challenger is required")
        if not self.cases:
            raise ValueError("at least one evaluation case is required")
        if self.primary_metric not in _SUPPORTED_METRICS:
            raise ValueError(f"unsupported primary metric: {self.primary_metric}")
        _require_decimal_instance(
            self.minimum_total_improvement,
            "minimum_total_improvement",
            minimum=Decimal("0"),
        )
        _require_bool(self.primary_higher_is_better, "primary_higher_is_better")

        candidates = (self.champion, *self.challengers)
        candidate_ids = [candidate.candidate_id for candidate in candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidate IDs must be unique")
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("evaluation case IDs must be unique")
        fingerprints = {candidate.authority_fingerprint for candidate in candidates}
        if len(fingerprints) != 1:
            raise PermissionError("experiment candidates may not widen or alter authority")

        preregistration_sha = self.scientific_protocol.binding_sha256
        for candidate in candidates:
            if candidate.research_plan_sha256 != preregistration_sha:
                raise ValueError(
                    "candidate run identity is not bound to the frozen scientific protocol"
                )

        guardrail_metrics = [rule.metric for rule in self.guardrails]
        if len(guardrail_metrics) != len(set(guardrail_metrics)):
            raise ValueError("guardrail metrics must be unique")
        if self.primary_metric in guardrail_metrics:
            raise ValueError("primary metric must not also be a guardrail")

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "protocol_schema_version": self.protocol_schema_version,
            "experiment_id": self.experiment_id,
            "research_question_id": self.research_question_id,
            "hypothesis_id": self.hypothesis_id,
            "scientific_protocol": {
                **self.scientific_protocol.canonical_dict(),
                "binding_sha256": self.scientific_protocol.binding_sha256,
            },
            "champion": self.champion.to_dict(),
            "challengers": [candidate.to_dict() for candidate in self.challengers],
            "cases": [case.to_dict() for case in self.cases],
            "primary_metric": self.primary_metric,
            "minimum_total_improvement": str(self.minimum_total_improvement),
            "primary_higher_is_better": self.primary_higher_is_better,
            "guardrails": [rule.to_dict() for rule in self.guardrails],
        }

    @property
    def protocol_sha256(self) -> str:
        canonical = json.dumps(
            self.canonical_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_json_depth(value: Any, *, depth: int = 0) -> None:
    if depth > _MAX_PROTOCOL_JSON_DEPTH:
        raise ValueError(
            f"protocol JSON nesting exceeds maximum depth {_MAX_PROTOCOL_JSON_DEPTH}"
        )
    if isinstance(value, dict):
        for item in value.values():
            _validate_json_depth(item, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            _validate_json_depth(item, depth=depth + 1)


def _decode_protocol_json(raw: str | bytes) -> dict[str, Any]:
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("protocol must be valid UTF-8 JSON") from exc
    elif isinstance(raw, str):
        text = raw
    else:
        raise ValueError("protocol JSON input must be str or bytes")

    def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"protocol contains duplicate JSON object key: {key}")
            value[key] = item
        return value

    def _reject_nonstandard_constant(value: str) -> None:
        raise ValueError(f"protocol contains non-standard JSON constant: {value}")

    try:
        payload = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonstandard_constant,
        )
    except RecursionError as exc:
        raise ValueError("protocol JSON nesting exceeds parser recursion limit") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("protocol must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("protocol JSON root must be an object")
    _validate_json_depth(payload)
    return payload


def _require_exact_keys(payload: dict[str, Any], expected: set[str], field: str) -> None:
    actual = set(payload)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(
            f"{field} fields mismatch: missing={missing} unexpected={extra}"
        )


def _require_json_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _require_json_array(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    return value


def _require_json_decimal(value: Any, field: str) -> Decimal:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a canonical decimal string")
    return _require_decimal(value, field)


def _json_text_tuple(value: Any, field: str) -> tuple[str, ...]:
    raw = _require_json_array(value, field)
    result = tuple(
        _require_text(item, f"{field}[{index}]") for index, item in enumerate(raw)
    )
    if not result:
        raise ValueError(f"{field} must not be empty")
    if len(result) != len(set(result)):
        raise ValueError(f"{field} must not contain duplicates")
    return result


def _scientific_protocol_from_dict(payload: dict[str, Any]) -> ScientificProtocolBinding:
    expected = {
        "protocol_version",
        "research_protocol_id",
        "research_question_id",
        "research_question_sha256",
        "hypothesis_id",
        "hypothesis_sha256",
        "inclusion_criteria",
        "exclusion_criteria",
        "lawful_source_requirements",
        "causal_cutoff",
        "evaluation_design",
        "feature_set_version",
        "uncertainty_method",
        "multiple_comparison_control",
        "robustness_checks",
        "random_seed_policy",
        "stopping_rule",
        "promotion_rule",
        "expected_artifacts",
        "code_config_sha256",
        "frozen_at_utc",
        "binding_sha256",
    }
    _require_exact_keys(payload, expected, "scientific_protocol")
    version = payload["protocol_version"]
    if type(version) is not int or version < 1:
        raise ValueError("scientific_protocol.protocol_version must be an integer >= 1")
    binding = ScientificProtocolBinding(
        research_protocol_id=_require_text(payload["research_protocol_id"], "scientific_protocol.research_protocol_id"),
        research_question_id=_require_text(payload["research_question_id"], "scientific_protocol.research_question_id"),
        research_question_sha256=_require_sha256(payload["research_question_sha256"], "scientific_protocol.research_question_sha256"),
        hypothesis_id=_require_text(payload["hypothesis_id"], "scientific_protocol.hypothesis_id"),
        hypothesis_sha256=_require_sha256(payload["hypothesis_sha256"], "scientific_protocol.hypothesis_sha256"),
        inclusion_criteria=_require_text(payload["inclusion_criteria"], "scientific_protocol.inclusion_criteria"),
        exclusion_criteria=_require_text(payload["exclusion_criteria"], "scientific_protocol.exclusion_criteria"),
        lawful_source_requirements=_require_text(payload["lawful_source_requirements"], "scientific_protocol.lawful_source_requirements"),
        causal_cutoff=_require_text(payload["causal_cutoff"], "scientific_protocol.causal_cutoff"),
        evaluation_design=_require_text(payload["evaluation_design"], "scientific_protocol.evaluation_design"),
        feature_set_version=_require_text(payload["feature_set_version"], "scientific_protocol.feature_set_version"),
        uncertainty_method=_require_text(payload["uncertainty_method"], "scientific_protocol.uncertainty_method"),
        multiple_comparison_control=_require_text(payload["multiple_comparison_control"], "scientific_protocol.multiple_comparison_control"),
        robustness_checks=_json_text_tuple(payload["robustness_checks"], "scientific_protocol.robustness_checks"),
        random_seed_policy=_require_text(payload["random_seed_policy"], "scientific_protocol.random_seed_policy"),
        stopping_rule=_require_text(payload["stopping_rule"], "scientific_protocol.stopping_rule"),
        promotion_rule=_require_text(payload["promotion_rule"], "scientific_protocol.promotion_rule"),
        expected_artifacts=_json_text_tuple(payload["expected_artifacts"], "scientific_protocol.expected_artifacts"),
        code_config_sha256=_require_sha256(payload["code_config_sha256"], "scientific_protocol.code_config_sha256"),
        frozen_at_utc=_require_text(payload["frozen_at_utc"], "scientific_protocol.frozen_at_utc"),
        protocol_version=version,
    )
    declared = _require_sha256(
        payload["binding_sha256"], "scientific_protocol.binding_sha256"
    )
    if declared != binding.binding_sha256:
        raise ValueError(
            "scientific_protocol.binding_sha256 does not match frozen preregistration"
        )
    return binding


def _candidate_from_dict(payload: dict[str, Any], field: str) -> CandidateRef:
    expected = {
        "candidate_id",
        "canonical_strategy_id",
        "runtime_identity_sha256",
        "authority_fingerprint",
        "agent_composition_sha256",
        "research_plan_sha256",
    }
    _require_exact_keys(payload, expected, field)
    research_plan_sha256 = payload["research_plan_sha256"]
    if research_plan_sha256 is not None:
        research_plan_sha256 = _require_sha256(
            research_plan_sha256, f"{field}.research_plan_sha256"
        )
    candidate = CandidateRef(
        candidate_id=_require_text(payload["candidate_id"], f"{field}.candidate_id"),
        canonical_strategy_id=_require_text(payload["canonical_strategy_id"], f"{field}.canonical_strategy_id"),
        authority_fingerprint=_require_text(payload["authority_fingerprint"], f"{field}.authority_fingerprint"),
        agent_composition_sha256=_require_sha256(payload["agent_composition_sha256"], f"{field}.agent_composition_sha256"),
        research_plan_sha256=research_plan_sha256,
    )
    declared = _require_sha256(
        payload["runtime_identity_sha256"], f"{field}.runtime_identity_sha256"
    )
    if declared != candidate.runtime_identity_sha256:
        raise ValueError(
            f"{field}.runtime_identity_sha256 does not match canonical runtime fields"
        )
    return candidate


def _case_from_dict(payload: dict[str, Any], field: str) -> EvaluationCase:
    expected = {
        "case_id", "dataset_name", "sport", "dataset_schema_version",
        "market_sha256", "sealed_results_sha256", "historical_import_identity",
        "replay_dataset_hash", "event_count", "price_semantics",
        "executable_quote_verified", "paper_fill_fidelity_verified",
        "price_source_ids", "initial_bankroll",
    }
    _require_exact_keys(payload, expected, field)
    schema_version = payload["dataset_schema_version"]
    event_count = payload["event_count"]
    if type(schema_version) is not int or schema_version < 1:
        raise ValueError(f"{field}.dataset_schema_version must be an integer >= 1")
    if type(event_count) is not int or event_count < 1:
        raise ValueError(f"{field}.event_count must be an integer >= 1")
    historical = payload["historical_import_identity"]
    if historical is not None:
        historical = _require_sha256(historical, f"{field}.historical_import_identity")
    return EvaluationCase(
        case_id=_require_text(payload["case_id"], f"{field}.case_id"),
        dataset_name=_require_text(payload["dataset_name"], f"{field}.dataset_name"),
        sport=_require_text(payload["sport"], f"{field}.sport"),
        dataset_schema_version=schema_version,
        market_sha256=_require_sha256(payload["market_sha256"], f"{field}.market_sha256"),
        sealed_results_sha256=_require_sha256(payload["sealed_results_sha256"], f"{field}.sealed_results_sha256"),
        historical_import_identity=historical,
        replay_dataset_hash=_require_sha256(payload["replay_dataset_hash"], f"{field}.replay_dataset_hash"),
        event_count=event_count,
        price_semantics=_require_text(payload["price_semantics"], f"{field}.price_semantics"),
        executable_quote_verified=_require_bool(payload["executable_quote_verified"], f"{field}.executable_quote_verified"),
        paper_fill_fidelity_verified=_require_bool(payload["paper_fill_fidelity_verified"], f"{field}.paper_fill_fidelity_verified"),
        price_source_ids=_json_text_tuple(payload["price_source_ids"], f"{field}.price_source_ids"),
        initial_bankroll=_require_json_decimal(payload["initial_bankroll"], f"{field}.initial_bankroll"),
    )


def _guardrail_from_dict(payload: dict[str, Any], field: str) -> GuardrailRule:
    _require_exact_keys(
        payload, {"metric", "higher_is_better", "max_regression"}, field
    )
    return GuardrailRule(
        metric=_require_text(payload["metric"], f"{field}.metric"),
        higher_is_better=_require_bool(payload["higher_is_better"], f"{field}.higher_is_better"),
        max_regression=_require_json_decimal(payload["max_regression"], f"{field}.max_regression"),
    )


def load_champion_challenger_protocol_json(
    raw: str | bytes,
) -> ChampionChallengerProtocol:
    payload = _decode_protocol_json(raw)
    expected = {
        "protocol_schema_version", "experiment_id", "research_question_id",
        "hypothesis_id", "scientific_protocol", "champion", "challengers",
        "cases", "primary_metric", "minimum_total_improvement",
        "primary_higher_is_better", "guardrails",
    }
    _require_exact_keys(payload, expected, "protocol")
    schema_version = payload["protocol_schema_version"]
    if type(schema_version) is not int or schema_version != 1:
        raise ValueError("protocol_schema_version must be 1")
    scientific_protocol = _scientific_protocol_from_dict(
        _require_json_object(payload["scientific_protocol"], "scientific_protocol")
    )
    champion = _candidate_from_dict(
        _require_json_object(payload["champion"], "champion"), "champion"
    )
    challengers = tuple(
        _candidate_from_dict(
            _require_json_object(item, f"challengers[{index}]"),
            f"challengers[{index}]",
        )
        for index, item in enumerate(
            _require_json_array(payload["challengers"], "challengers")
        )
    )
    cases = tuple(
        _case_from_dict(
            _require_json_object(item, f"cases[{index}]"), f"cases[{index}]"
        )
        for index, item in enumerate(_require_json_array(payload["cases"], "cases"))
    )
    guardrails = tuple(
        _guardrail_from_dict(
            _require_json_object(item, f"guardrails[{index}]"), f"guardrails[{index}]"
        )
        for index, item in enumerate(
            _require_json_array(payload["guardrails"], "guardrails")
        )
    )
    return ChampionChallengerProtocol(
        experiment_id=_require_text(payload["experiment_id"], "experiment_id"),
        research_question_id=_require_text(payload["research_question_id"], "research_question_id"),
        hypothesis_id=_require_text(payload["hypothesis_id"], "hypothesis_id"),
        scientific_protocol=scientific_protocol,
        champion=champion,
        challengers=challengers,
        cases=cases,
        primary_metric=_require_text(payload["primary_metric"], "primary_metric"),
        minimum_total_improvement=_require_json_decimal(payload["minimum_total_improvement"], "minimum_total_improvement"),
        primary_higher_is_better=_require_bool(payload["primary_higher_is_better"], "primary_higher_is_better"),
        guardrails=guardrails,
        protocol_schema_version=schema_version,
    )


@dataclass(frozen=True, slots=True)
class ExperimentRunCell:
    case_id: str
    candidate_id: str
    evidence: StrategyRunEvidence

    def __post_init__(self) -> None:
        _require_text(self.case_id, "case_id")
        _require_text(self.candidate_id, "candidate_id")
        if not isinstance(self.evidence, StrategyRunEvidence):
            raise ValueError("evidence must be StrategyRunEvidence")
        if self.evidence.strategy_id != self.candidate_id:
            raise ValueError(
                "strategy run evidence candidate mismatch: "
                f"expected={self.candidate_id} actual={self.evidence.strategy_id}"
            )


@dataclass(frozen=True, slots=True)
class ExperimentDecisionReport:
    experiment_id: str
    research_question_id: str
    hypothesis_id: str
    scientific_protocol_sha256: str
    protocol_sha256: str
    authority_fingerprint: str
    decision: ExperimentDecision
    selected_candidate_id: str
    previous_champion_id: str
    eligible_challenger_ids: tuple[str, ...]
    aggregate_primary_improvements: dict[str, Decimal]
    case_metrics: tuple[dict[str, Any], ...]
    evidence_sha256s: tuple[str, ...]
    donor_provenance: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "autosport_champion_challenger_decision",
            "experiment_id": self.experiment_id,
            "research_question_id": self.research_question_id,
            "hypothesis_id": self.hypothesis_id,
            "scientific_protocol_sha256": self.scientific_protocol_sha256,
            "protocol_sha256": self.protocol_sha256,
            "authority_fingerprint": self.authority_fingerprint,
            "decision": self.decision.value,
            "selected_candidate_id": self.selected_candidate_id,
            "previous_champion_id": self.previous_champion_id,
            "eligible_challenger_ids": list(self.eligible_challenger_ids),
            "aggregate_primary_improvements": {
                candidate: str(value)
                for candidate, value in sorted(
                    self.aggregate_primary_improvements.items()
                )
            },
            "case_metrics": list(self.case_metrics),
            "evidence_sha256s": list(self.evidence_sha256s),
            "donor_provenance": dict(self.donor_provenance),
            "truth": {
                "recommendation_only": True,
                "active_strategy_mutation": False,
                "real_money_execution": False,
                "human_tested": False,
                "nvda_verified": False,
                "profitability_claim": False,
                "predictive_superiority_claim": False,
            },
        }


def evaluate_champion_challenger(
    protocol: ChampionChallengerProtocol,
    cells: Iterable[ExperimentRunCell],
    *,
    donor_repository: str = "Oleksii-debug/Nika-Core",
    donor_sha: str = "2f7be3389109d7dd6fb3bae40540fe0cf2eba695",
) -> ExperimentDecisionReport:
    cell_values = tuple(cells)
    expected = {
        (candidate.candidate_id, case.case_id)
        for candidate in (protocol.champion, *protocol.challengers)
        for case in protocol.cases
    }
    actual = {(cell.candidate_id, cell.case_id) for cell in cell_values}
    if len(cell_values) != len(actual):
        raise ValueError("experiment matrix contains duplicate candidate/case cells")
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(
            f"experiment matrix is incomplete or unexpected: missing={missing} extra={extra}"
        )

    candidates = {
        candidate.candidate_id: candidate
        for candidate in (protocol.champion, *protocol.challengers)
    }
    cases = {case.case_id: case for case in protocol.cases}
    by_key = {
        (cell.candidate_id, cell.case_id): cell.evidence for cell in cell_values
    }
    frozen_scientific_sha = protocol.scientific_protocol.binding_sha256
    seen_run_ids: set[str] = set()
    seen_summary_hashes: set[str] = set()
    for (candidate_id, case_id), evidence in by_key.items():
        candidate = candidates[candidate_id]
        case = cases[case_id]
        if evidence.comparison_identity != case.identity:
            raise ValueError(
                f"evidence dataset/price identity mismatch for {candidate_id}/{case_id}"
            )
        if evidence.canonical_strategy_id != candidate.canonical_strategy_id:
            raise ValueError(
                f"canonical strategy identity mismatch for {candidate_id}/{case_id}"
            )
        if evidence.agent_composition_sha256 != candidate.agent_composition_sha256.lower():
            raise ValueError(
                f"agent composition identity mismatch for {candidate_id}/{case_id}"
            )
        if evidence.research_plan_sha256 != frozen_scientific_sha:
            raise ValueError(
                f"scientific preregistration identity mismatch for {candidate_id}/{case_id}"
            )
        if evidence.research_plan_sha256 != candidate.research_plan_sha256:
            raise ValueError(
                f"research-plan identity mismatch for {candidate_id}/{case_id}"
            )
        evidence_runtime_identity = _runtime_identity_sha256(
            evidence.canonical_strategy_id,
            evidence.agent_composition_sha256,
            evidence.research_plan_sha256,
        )
        if evidence_runtime_identity != candidate.runtime_identity_sha256:
            raise ValueError(f"runtime identity mismatch for {candidate_id}/{case_id}")
        if evidence.run_id in seen_run_ids:
            raise ValueError(f"run_id reused across experiment matrix: {evidence.run_id}")
        if evidence.source_sha256 in seen_summary_hashes:
            raise ValueError(
                f"summary evidence reused across experiment matrix: {evidence.source_sha256}"
            )
        seen_run_ids.add(evidence.run_id)
        seen_summary_hashes.add(evidence.source_sha256)

    def metric(evidence: StrategyRunEvidence, name: str) -> Decimal:
        return _require_decimal_instance(getattr(evidence, name), f"metric {name}")

    champion_id = protocol.champion.candidate_id
    case_metrics: list[dict[str, Any]] = []
    for case in protocol.cases:
        champion = by_key[(champion_id, case.case_id)]
        for challenger in protocol.challengers:
            candidate = by_key[(challenger.candidate_id, case.case_id)]
            challenger_value = metric(candidate, protocol.primary_metric)
            champion_value = metric(champion, protocol.primary_metric)
            delta = _exact_decimal_difference(challenger_value, champion_value)
            case_metrics.append(
                {
                    "case_id": case.case_id,
                    "candidate_id": challenger.candidate_id,
                    "primary_metric": protocol.primary_metric,
                    "champion_value": str(champion_value),
                    "challenger_value": str(challenger_value),
                    "delta": str(delta),
                    "guardrails": {
                        rule.metric: {
                            "champion": str(metric(champion, rule.metric)),
                            "challenger": str(metric(candidate, rule.metric)),
                            "allowed_regression": str(rule.max_regression),
                            "passed": _guardrail_pass(rule, champion, candidate),
                        }
                        for rule in protocol.guardrails
                    },
                }
            )

    eligible: list[tuple[str, Decimal]] = []
    aggregate_improvements: dict[str, Decimal] = {}
    for challenger in protocol.challengers:
        deltas: list[Decimal] = []
        guardrails_ok = True
        for case in protocol.cases:
            champion = by_key[(champion_id, case.case_id)]
            candidate = by_key[(challenger.candidate_id, case.case_id)]
            challenger_value = metric(candidate, protocol.primary_metric)
            champion_value = metric(champion, protocol.primary_metric)
            delta = _exact_decimal_difference(challenger_value, champion_value)
            deltas.append(
                delta
                if protocol.primary_higher_is_better
                else _exact_decimal_negate(delta)
            )
            if not all(
                _guardrail_pass(rule, champion, candidate)
                for rule in protocol.guardrails
            ):
                guardrails_ok = False
        total = _exact_decimal_sum(deltas)
        aggregate_improvements[challenger.candidate_id] = total
        if total >= protocol.minimum_total_improvement and guardrails_ok:
            eligible.append((challenger.candidate_id, total))

    eligible.sort(key=lambda item: (-item[1], item[0]))
    selected = eligible[0][0] if eligible else champion_id
    decision = (
        ExperimentDecision.CHALLENGER_ELIGIBLE
        if eligible
        else ExperimentDecision.RETAIN_CHAMPION
    )
    return ExperimentDecisionReport(
        experiment_id=protocol.experiment_id,
        research_question_id=protocol.research_question_id,
        hypothesis_id=protocol.hypothesis_id,
        scientific_protocol_sha256=frozen_scientific_sha,
        protocol_sha256=protocol.protocol_sha256,
        authority_fingerprint=protocol.champion.authority_fingerprint,
        decision=decision,
        selected_candidate_id=selected,
        previous_champion_id=champion_id,
        eligible_challenger_ids=tuple(
            candidate_id for candidate_id, _ in eligible
        ),
        aggregate_primary_improvements=aggregate_improvements,
        case_metrics=tuple(case_metrics),
        evidence_sha256s=tuple(sorted(seen_summary_hashes)),
        donor_provenance={"repository": donor_repository, "reviewed_sha": donor_sha},
    )


def _guardrail_pass(
    rule: GuardrailRule,
    champion: StrategyRunEvidence,
    challenger: StrategyRunEvidence,
) -> bool:
    champion_value = _require_decimal_instance(
        getattr(champion, rule.metric), f"champion {rule.metric}"
    )
    challenger_value = _require_decimal_instance(
        getattr(challenger, rule.metric), f"challenger {rule.metric}"
    )
    regression = (
        _exact_decimal_difference(champion_value, challenger_value)
        if rule.higher_is_better
        else _exact_decimal_difference(challenger_value, champion_value)
    )
    return regression <= rule.max_regression


__all__ = [
    "CandidateRef",
    "ChampionChallengerProtocol",
    "EvaluationCase",
    "ExperimentDecision",
    "ExperimentDecisionReport",
    "ExperimentRunCell",
    "GuardrailRule",
    "ScientificProtocolBinding",
    "evaluate_champion_challenger",
    "load_champion_challenger_protocol_json",
]
