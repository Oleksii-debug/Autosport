"""Canonical post-outcome scoring authority for realized value of computation.

The authority in this module deliberately recomputes VOC arithmetic from causal
sources.  It never accepts utility, cost, support, ESS, or uncertainty values from
``PairedVOCEvaluation`` as scoring inputs.  Those values remain a persisted claim
that ``CanonicalVOCAuthorityResolver`` compares against this independently-derived
score.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .decision_ledger import DecisionLedgerIntegrityError, JsonlDecisionLedger
from .market_outcomes import MarketSettlementOutcomeAuthority
from .outcome_lineage import validate_outcome_source_lineage
from .scientific_registry import ScientificRegistry
from .voc_evaluation import (
    CanonicalVOCAuthorityResolver,
    OutcomeDerivedVOCScore,
    PairedVOCEvaluation,
    VOCEvaluationError,
)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_MICROSECOND = Decimal("0.000001")
_SCORING_EVIDENCE_KEY = "voc_scoring_evidence"
_SCORING_KIND = "paired-realized-utility-v1"
_UNCERTAINTY_METHOD = "paired-range-v1"
_ALLOWED_OUTCOMES = frozenset({"win", "loss", "void"})
_SCORING_EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "evaluation_id",
        "decision_input_sha256",
        "baseline_output_sha256",
        "challenger_output_sha256",
        "baseline_action",
        "challenger_action",
        "baseline_abstained",
        "challenger_abstained",
        "samples",
    }
)
_SAMPLE_FIELDS = frozenset(
    {
        "sample_id",
        "quote_key",
        "baseline_compute_cost",
        "challenger_compute_cost",
        "baseline_completed_at",
        "challenger_completed_at",
    }
)
_SCORING_RULE_FIELDS = frozenset(
    {
        "kind",
        "metric",
        "utility_by_outcome",
        "abstain_utility",
        "compute_cost_multiplier",
        "latency_cost_per_second",
        "uncertainty_method",
    }
)


def _text(value: object, *, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise VOCEvaluationError(f"{field} must be a non-empty canonical string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise VOCEvaluationError(f"{field} must be valid UTF-8") from exc
    return value


def _sha256(value: object, *, field: str) -> str:
    digest = _text(value, field=field)
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise VOCEvaluationError(f"{field} must be lowercase SHA-256 hex")
    return digest


def _instant(value: object, *, field: str) -> datetime:
    text = _text(value, field=field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise VOCEvaluationError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise VOCEvaluationError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _decimal(value: object, *, field: str, nonnegative: bool = False) -> Decimal:
    if type(value) is not str or not value or value != value.strip():
        raise VOCEvaluationError(f"{field} must be canonical decimal text")
    if any(ch in value for ch in "eE"):
        raise VOCEvaluationError(f"{field} must use fixed-point decimal text")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise VOCEvaluationError(f"{field} must be decimal text") from exc
    if not parsed.is_finite() or (nonnegative and parsed < _ZERO):
        qualifier = " finite non-negative" if nonnegative else " finite"
        raise VOCEvaluationError(f"{field} must be{qualifier}")
    return parsed


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise VOCEvaluationError("canonical VOC scoring input is not JSON-safe") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _strict_json_object(payload: bytes, *, context: str) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise VOCEvaluationError(f"{context} contains duplicate JSON key: {key}")
            value[key] = item
        return value

    def reject_constant(value: str) -> None:
        raise VOCEvaluationError(f"{context} contains non-standard JSON constant: {value}")

    try:
        raw = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=reject_constant,
        )
    except UnicodeDecodeError as exc:
        raise VOCEvaluationError(f"{context} is not UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise VOCEvaluationError(f"{context} is not valid JSON") from exc
    if type(raw) is not dict:
        raise VOCEvaluationError(f"{context} must be a JSON object")
    return raw


def _duration_seconds(later: datetime, earlier: datetime) -> Decimal:
    if later <= earlier:
        return _ZERO
    delta = later - earlier
    micros = (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )
    return Decimal(micros) * _MICROSECOND


def _mean(values: list[Decimal], *, field: str) -> Decimal:
    if not values:
        raise VOCEvaluationError(f"{field} has no canonical paired samples")
    return sum(values, _ZERO) / Decimal(len(values))


class CanonicalOutcomeDerivedVOCScoreAuthority:
    """Derive one VOC score from pre-outcome ledger evidence and revealed settlement.

    ``voc_scoring_evidence`` lives in the exact DecisionLedger record already bound
    by ``decision_evidence_sha256``.  It may carry actions, timestamps and measured
    compute cost, but never an outcome or a utility.  After reveal this authority:

    * re-verifies that decision-ledger record and its causal timing;
    * re-verifies the frozen scoring rule in the pre-decision ResearchProtocol;
    * hash-verifies and lineage-verifies the authoritative outcome source record;
    * proves that the revealed quote outcomes are one state from the supplied
      ``MarketSettlementOutcomeAuthority``;
    * computes utility/cost/support/ESS/uncertainty itself.

    Consequently changing numeric fields on ``PairedVOCEvaluation`` cannot change
    the score returned here.
    """

    def __init__(
        self,
        *,
        decision_ledger: JsonlDecisionLedger,
        scientific_registry: ScientificRegistry,
        outcome_authority: MarketSettlementOutcomeAuthority,
        outcome_source_root: str | Path,
        source_record_file: str,
        source_record_sha256: str,
    ) -> None:
        if not isinstance(decision_ledger, JsonlDecisionLedger):
            raise TypeError("decision_ledger must be JsonlDecisionLedger")
        if not isinstance(scientific_registry, ScientificRegistry):
            raise TypeError("scientific_registry must be ScientificRegistry")
        if not isinstance(outcome_authority, MarketSettlementOutcomeAuthority):
            raise TypeError("outcome_authority must be MarketSettlementOutcomeAuthority")
        self.decision_ledger = decision_ledger
        self.scientific_registry = scientific_registry
        self.outcome_authority = outcome_authority
        self.outcome_source_root = Path(outcome_source_root)
        self.source_record_file = _text(
            source_record_file,
            field="VOC outcome source_record_file",
        )
        self.source_record_sha256 = _sha256(
            source_record_sha256,
            field="VOC outcome source_record_sha256",
        )

    def _evaluation(self, evaluation_id: str, *, as_of: datetime) -> PairedVOCEvaluation | None:
        entry = self.scientific_registry.get("PairedVOCEvaluation", evaluation_id)
        if entry is None:
            return None
        if _instant(entry.available_at, field="PairedVOCEvaluation.available_at") > as_of:
            return None
        try:
            value = PairedVOCEvaluation.from_payload(entry.payload)
        except (TypeError, ValueError) as exc:
            if isinstance(exc, VOCEvaluationError):
                raise
            raise VOCEvaluationError("canonical PairedVOCEvaluation is invalid") from exc
        if value.evaluation_id != evaluation_id:
            raise VOCEvaluationError("canonical PairedVOCEvaluation identity mismatch")
        return value

    def _decision_scoring_evidence(
        self,
        evaluation: PairedVOCEvaluation,
    ) -> tuple[Mapping[str, Any], str]:
        try:
            # Full-ledger verification remains an integrity gate, but the mutable
            # future ledger tip is not part of historical VOC score identity.
            self.decision_ledger.verified_snapshot()
            records = self.decision_ledger.verified_records()
        except DecisionLedgerIntegrityError as exc:
            raise VOCEvaluationError("canonical DecisionLedger verification failed") from exc

        matched = []
        for record in records:
            if _digest(record.to_dict()) == evaluation.decision_evidence_sha256:
                matched.append(record)
        if len(matched) != 1:
            raise VOCEvaluationError(
                "canonical VOC scoring DecisionLedger evidence is missing or ambiguous"
            )
        record = matched[0]
        recorded_at = _instant(record.recorded_at, field="VOC scoring evidence recorded_at")
        completed_at = max(
            _instant(evaluation.baseline_completed_at, field="baseline_completed_at"),
            _instant(evaluation.challenger_completed_at, field="challenger_completed_at"),
        )
        reveal_at = _instant(evaluation.outcome_revealed_at, field="outcome_revealed_at")
        if recorded_at < completed_at:
            raise VOCEvaluationError(
                "canonical VOC scoring evidence predates paired candidate outputs"
            )
        if recorded_at >= reveal_at:
            raise VOCEvaluationError(
                "canonical VOC scoring evidence was not frozen before outcome reveal"
            )
        payload = record.payload
        if not isinstance(payload, Mapping):
            raise VOCEvaluationError("canonical VOC scoring decision payload is invalid")
        evidence = payload.get(_SCORING_EVIDENCE_KEY)
        if not isinstance(evidence, Mapping) or set(evidence) != _SCORING_EVIDENCE_FIELDS:
            raise VOCEvaluationError("canonical voc_scoring_evidence schema is invalid")
        if evidence.get("schema_version") != 1:
            raise VOCEvaluationError("unsupported voc_scoring_evidence schema version")
        expected = {
            "evaluation_id": evaluation.evaluation_id,
            "decision_input_sha256": evaluation.decision_input_sha256,
            "baseline_output_sha256": evaluation.baseline_output_sha256,
            "challenger_output_sha256": evaluation.challenger_output_sha256,
            "baseline_action": evaluation.baseline_action,
            "challenger_action": evaluation.challenger_action,
            "baseline_abstained": evaluation.baseline_abstained,
            "challenger_abstained": evaluation.challenger_abstained,
        }
        for field, wanted in expected.items():
            if evidence.get(field) != wanted:
                raise VOCEvaluationError(
                    f"canonical VOC scoring evidence {field} does not match paired outputs"
                )

        raw_samples = evidence.get("samples")
        if not isinstance(raw_samples, (list, tuple)) or not raw_samples:
            raise VOCEvaluationError("canonical VOC scoring evidence samples are missing")
        for index, raw in enumerate(raw_samples, start=1):
            if not isinstance(raw, Mapping) or set(raw) != _SAMPLE_FIELDS:
                raise VOCEvaluationError(
                    f"canonical VOC scoring sample {index} schema is invalid"
                )
            baseline_completed = _instant(
                raw.get("baseline_completed_at"),
                field=f"VOC sample {index} baseline_completed_at",
            )
            challenger_completed = _instant(
                raw.get("challenger_completed_at"),
                field=f"VOC sample {index} challenger_completed_at",
            )
            if baseline_completed > recorded_at or challenger_completed > recorded_at:
                raise VOCEvaluationError(
                    "VOC scoring sample completion was not frozen by DecisionRecord"
                )

        return evidence, evaluation.decision_evidence_sha256

    def _scoring_rule(
        self,
        evaluation: PairedVOCEvaluation,
    ) -> dict[str, Any]:
        entry = self.scientific_registry.get(
            "ResearchProtocol", evaluation.research_protocol_id
        )
        if entry is None:
            raise VOCEvaluationError("canonical ResearchProtocol is missing for VOC scoring")
        if _instant(entry.available_at, field="ResearchProtocol.available_at") > _instant(
            evaluation.decision_at,
            field="decision_at",
        ):
            raise VOCEvaluationError("VOC scoring rule was not frozen before decision time")
        payload = entry.payload
        if payload.get("research_protocol_id") != evaluation.research_protocol_id:
            raise VOCEvaluationError("canonical ResearchProtocol identity mismatch")
        if payload.get("protocol_sha256") != evaluation.research_protocol_sha256:
            raise VOCEvaluationError("canonical ResearchProtocol digest mismatch")
        binding = payload.get("binding")
        if not isinstance(binding, Mapping):
            raise VOCEvaluationError("canonical ResearchProtocol binding is missing")
        design_text = binding.get("evaluation_design")
        if type(design_text) is not str or not design_text.strip():
            raise VOCEvaluationError("canonical VOC evaluation design is missing")
        try:
            design = json.loads(design_text)
        except json.JSONDecodeError as exc:
            raise VOCEvaluationError("canonical VOC evaluation design is invalid JSON") from exc
        if type(design) is not dict:
            raise VOCEvaluationError("canonical VOC evaluation design must be an object")
        scoring_rule = design.get("scoring_rule")
        if type(scoring_rule) is not dict:
            raise VOCEvaluationError("canonical VOC scoring rule is missing")
        if scoring_rule.get("id") != evaluation.scoring_rule_id:
            raise VOCEvaluationError("canonical VOC scoring rule identity mismatch")
        rule = scoring_rule.get("payload")
        if type(rule) is not dict or set(rule) != _SCORING_RULE_FIELDS:
            raise VOCEvaluationError("canonical VOC scoring rule schema is invalid")
        if _digest(rule) != evaluation.scoring_rule_sha256:
            raise VOCEvaluationError("canonical VOC scoring rule SHA-256 mismatch")
        if rule.get("kind") != _SCORING_KIND or rule.get("metric") != "incremental_value":
            raise VOCEvaluationError("canonical VOC scoring rule kind/metric is unsupported")
        if rule.get("uncertainty_method") != _UNCERTAINTY_METHOD:
            raise VOCEvaluationError("canonical VOC uncertainty method is unsupported")
        utility_table = rule.get("utility_by_outcome")
        if type(utility_table) is not dict or set(utility_table) != _ALLOWED_OUTCOMES:
            raise VOCEvaluationError("canonical VOC utility table must cover win/loss/void")
        for outcome, action_values in utility_table.items():
            if type(action_values) is not dict or not action_values:
                raise VOCEvaluationError(
                    f"canonical VOC utility table for {outcome} must be a non-empty action map"
                )
            for action, value in action_values.items():
                _text(action, field=f"VOC utility action for {outcome}")
                _decimal(value, field=f"VOC utility for {outcome}/{action}")
        _decimal(rule.get("abstain_utility"), field="VOC abstain_utility")
        _decimal(
            rule.get("compute_cost_multiplier"),
            field="VOC compute_cost_multiplier",
            nonnegative=True,
        )
        _decimal(
            rule.get("latency_cost_per_second"),
            field="VOC latency_cost_per_second",
            nonnegative=True,
        )
        return rule

    def _revealed_outcomes(
        self,
        evaluation: PairedVOCEvaluation,
        *,
        as_of: datetime,
    ) -> tuple[dict[str, str], str, str]:
        path = self.outcome_source_root / self.source_record_file
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise VOCEvaluationError("canonical VOC outcome source record is unreadable") from exc
        actual_sha = hashlib.sha256(payload).hexdigest()
        if actual_sha != self.source_record_sha256:
            raise VOCEvaluationError("canonical VOC outcome source SHA-256 mismatch")
        raw = _strict_json_object(payload, context="canonical VOC outcome source record")
        try:
            lineage = validate_outcome_source_lineage(
                source_root=self.outcome_source_root,
                source_record_file=self.source_record_file,
                source_record_sha256=self.source_record_sha256,
                source_record=raw,
                expected_source_identity=self.outcome_authority.identity.source_id,
            )
        except ValueError as exc:
            raise VOCEvaluationError("canonical VOC outcome lineage verification failed") from exc
        revealed_at = _instant(lineage.recorded_at, field="canonical VOC outcome recorded_at")
        expected_reveal = _instant(evaluation.outcome_revealed_at, field="outcome_revealed_at")
        if revealed_at != expected_reveal:
            raise VOCEvaluationError(
                "canonical VOC outcome reveal time does not match paired evaluation"
            )
        if revealed_at > as_of:
            raise VOCEvaluationError("canonical VOC outcome is not causally available")
        try:
            self.outcome_authority.assert_available_as_of(revealed_at)
        except (TypeError, ValueError) as exc:
            raise VOCEvaluationError(
                "market outcome authority was not available by outcome reveal"
            ) from exc
        if set(lineage.quote_outcomes) != set(self.outcome_authority.quote_keys):
            raise VOCEvaluationError(
                "canonical VOC revealed outcome does not cover the authoritative quote roster"
            )
        matching_states = [
            state
            for state in self.outcome_authority.terminal_states
            if self.outcome_authority.settlement_by_quote(state) == lineage.quote_outcomes
        ]
        if len(matching_states) != 1:
            raise VOCEvaluationError(
                "canonical VOC revealed outcome is not one authoritative terminal state"
            )
        if evaluation.outcome_evidence_sha256 != self.outcome_authority.authority_sha256:
            raise VOCEvaluationError(
                "paired VOC outcome evidence does not match market outcome authority"
            )
        return dict(lineage.quote_outcomes), lineage.recorded_at, actual_sha

    @staticmethod
    def _action_utility(
        *,
        rule: Mapping[str, Any],
        outcome: str,
        action: str,
        abstained: bool,
    ) -> Decimal:
        if type(abstained) is not bool:
            raise VOCEvaluationError("VOC abstention evidence must be bool")
        if abstained:
            return _decimal(rule.get("abstain_utility"), field="VOC abstain_utility")
        table = rule.get("utility_by_outcome")
        assert isinstance(table, Mapping)
        values = table.get(outcome)
        if not isinstance(values, Mapping) or action not in values:
            raise VOCEvaluationError(
                f"frozen VOC scoring rule does not define action {action!r} for {outcome}"
            )
        return _decimal(values[action], field=f"VOC utility for {outcome}/{action}")

    def _derive_score(
        self,
        *,
        evaluation: PairedVOCEvaluation,
        evidence: Mapping[str, Any],
        decision_record_sha256: str,
        rule: Mapping[str, Any],
        outcomes: Mapping[str, str],
        outcome_source_sha256: str,
    ) -> OutcomeDerivedVOCScore:
        raw_samples = evidence.get("samples")
        if not isinstance(raw_samples, (list, tuple)) or not raw_samples:
            raise VOCEvaluationError("canonical VOC scoring evidence samples are missing")

        baseline_utilities: list[Decimal] = []
        challenger_utilities: list[Decimal] = []
        extra_compute_costs: list[Decimal] = []
        extra_latency_seconds: list[Decimal] = []
        sample_net_values: list[Decimal] = []
        sample_ids: list[str] = []
        quote_keys: list[str] = []
        compute_multiplier = _decimal(
            rule.get("compute_cost_multiplier"),
            field="VOC compute_cost_multiplier",
            nonnegative=True,
        )
        latency_rate = _decimal(
            rule.get("latency_cost_per_second"),
            field="VOC latency_cost_per_second",
            nonnegative=True,
        )

        for index, raw in enumerate(raw_samples, start=1):
            if not isinstance(raw, Mapping) or set(raw) != _SAMPLE_FIELDS:
                raise VOCEvaluationError(
                    f"canonical VOC scoring sample {index} schema is invalid"
                )
            sample_id = _text(raw.get("sample_id"), field=f"VOC sample {index} id")
            quote_key = _text(raw.get("quote_key"), field=f"VOC sample {index} quote_key")
            if sample_id in sample_ids:
                raise VOCEvaluationError("canonical VOC scoring samples reuse sample_id")
            if quote_key in quote_keys:
                raise VOCEvaluationError("canonical VOC scoring samples reuse quote_key")
            if quote_key not in outcomes:
                raise VOCEvaluationError(
                    "canonical VOC scoring sample is outside authoritative outcome roster"
                )
            sample_ids.append(sample_id)
            quote_keys.append(quote_key)
            outcome = outcomes[quote_key]
            if outcome not in _ALLOWED_OUTCOMES:
                raise VOCEvaluationError("canonical VOC outcome value is unsupported")

            baseline_utility = self._action_utility(
                rule=rule,
                outcome=outcome,
                action=evaluation.baseline_action,
                abstained=evaluation.baseline_abstained,
            )
            challenger_utility = self._action_utility(
                rule=rule,
                outcome=outcome,
                action=evaluation.challenger_action,
                abstained=evaluation.challenger_abstained,
            )
            baseline_cost = _decimal(
                raw.get("baseline_compute_cost"),
                field=f"VOC sample {index} baseline_compute_cost",
                nonnegative=True,
            )
            challenger_cost = _decimal(
                raw.get("challenger_compute_cost"),
                field=f"VOC sample {index} challenger_compute_cost",
                nonnegative=True,
            )
            extra_compute = max(_ZERO, challenger_cost - baseline_cost)
            baseline_completed = _instant(
                raw.get("baseline_completed_at"),
                field=f"VOC sample {index} baseline_completed_at",
            )
            challenger_completed = _instant(
                raw.get("challenger_completed_at"),
                field=f"VOC sample {index} challenger_completed_at",
            )
            decision_at = _instant(evaluation.decision_at, field="decision_at")
            reveal_at = _instant(evaluation.outcome_revealed_at, field="outcome_revealed_at")
            if baseline_completed < decision_at or challenger_completed < decision_at:
                raise VOCEvaluationError("VOC scoring sample completion predates decision")
            if baseline_completed >= reveal_at or challenger_completed >= reveal_at:
                raise VOCEvaluationError("VOC scoring sample completion is not pre-outcome")
            extra_latency = _duration_seconds(challenger_completed, baseline_completed)
            compute_penalty = extra_compute * compute_multiplier
            latency_penalty = extra_latency * latency_rate
            sample_net = (
                challenger_utility
                - baseline_utility
                - compute_penalty
                - latency_penalty
            )
            baseline_utilities.append(baseline_utility)
            challenger_utilities.append(challenger_utility)
            extra_compute_costs.append(extra_compute)
            extra_latency_seconds.append(extra_latency)
            sample_net_values.append(sample_net)

        # Quote legs are correlated components of this one baseline/challenger
        # compute decision. They determine its realized economics but are not
        # independent paired observations and therefore cannot inflate ESS.
        paired_sample_count = 1
        effective_sample_size = 1
        baseline_utility = _mean(baseline_utilities, field="baseline utility")
        challenger_utility = _mean(challenger_utilities, field="challenger utility")
        measured_compute_cost = _mean(extra_compute_costs, field="measured compute cost")
        compute_cost_penalty = measured_compute_cost * compute_multiplier
        latency_opportunity_cost_penalty = (
            _mean(extra_latency_seconds, field="measured latency") * latency_rate
        )
        support_fraction = _ONE
        interval_low = min(sample_net_values)
        interval_high = max(sample_net_values)

        source_artifact_sha256 = _digest(
            {
                "schema": "autosport.canonical_voc_score_sources",
                "schema_version": 2,
                "evaluation_id": evaluation.evaluation_id,
                "decision_evidence_sha256": evaluation.decision_evidence_sha256,
                "decision_record_sha256": decision_record_sha256,
                "outcome_source_record_sha256": outcome_source_sha256,
                "outcome_authority_sha256": self.outcome_authority.authority_sha256,
                "scoring_rule_sha256": evaluation.scoring_rule_sha256,
                "research_protocol_sha256": evaluation.research_protocol_sha256,
                "sample_ids": sample_ids,
                "quote_keys": quote_keys,
            }
        )
        return OutcomeDerivedVOCScore(
            evaluation_id=evaluation.evaluation_id,
            available_at=evaluation.evaluated_at,
            outcome_evidence_sha256=self.outcome_authority.authority_sha256,
            scoring_rule_sha256=evaluation.scoring_rule_sha256,
            research_protocol_sha256=evaluation.research_protocol_sha256,
            holdout_access_id=evaluation.holdout_access_id,
            multiple_comparison_control_sha256=(
                evaluation.multiple_comparison_control_sha256
            ),
            baseline_utility=baseline_utility,
            challenger_utility=challenger_utility,
            compute_cost_penalty=compute_cost_penalty,
            latency_opportunity_cost_penalty=latency_opportunity_cost_penalty,
            measured_compute_cost=measured_compute_cost,
            paired_sample_count=paired_sample_count,
            effective_sample_size=effective_sample_size,
            support_fraction=support_fraction,
            incremental_value_interval_low=interval_low,
            incremental_value_interval_high=interval_high,
            source_artifact_sha256=source_artifact_sha256,
        )

    def resolve(
        self,
        evaluation_id: str,
        *,
        as_of: str,
    ) -> OutcomeDerivedVOCScore | None:
        identity = _text(evaluation_id, field="evaluation_id")
        cutoff = _instant(as_of, field="as_of")
        evaluation = self._evaluation(identity, as_of=cutoff)
        if evaluation is None:
            return None
        if _instant(evaluation.evaluated_at, field="evaluated_at") > cutoff:
            return None
        evidence, decision_record_sha256 = self._decision_scoring_evidence(evaluation)
        rule = self._scoring_rule(evaluation)
        outcomes, _, outcome_source_sha256 = self._revealed_outcomes(
            evaluation,
            as_of=cutoff,
        )
        return self._derive_score(
            evaluation=evaluation,
            evidence=evidence,
            decision_record_sha256=decision_record_sha256,
            rule=rule,
            outcomes=outcomes,
            outcome_source_sha256=outcome_source_sha256,
        )


def build_canonical_voc_authority_resolver(
    *,
    decision_ledger: JsonlDecisionLedger,
    scientific_registry: ScientificRegistry,
    outcome_authority: MarketSettlementOutcomeAuthority,
    outcome_source_root: str | Path,
    source_record_file: str,
    source_record_sha256: str,
) -> CanonicalVOCAuthorityResolver:
    """Build the production VOC resolver with the non-self-attested scorer wired in."""

    score_authority = CanonicalOutcomeDerivedVOCScoreAuthority(
        decision_ledger=decision_ledger,
        scientific_registry=scientific_registry,
        outcome_authority=outcome_authority,
        outcome_source_root=outcome_source_root,
        source_record_file=source_record_file,
        source_record_sha256=source_record_sha256,
    )
    return CanonicalVOCAuthorityResolver(
        decision_ledger=decision_ledger,
        scientific_registry=scientific_registry,
        outcome_authority=outcome_authority,
        outcome_score_authority=score_authority,
    )
