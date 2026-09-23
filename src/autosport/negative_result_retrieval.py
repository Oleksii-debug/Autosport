"""Deterministic read-only retrieval for durable non-positive scientific results.

The canonical :class:`ScientificRegistry` remains the sole scientific-memory
authority. This module derives search results from causally available registry
records; it never authorizes a repeat, promotion, deployment, or execution.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping

from .scientific_registry import (
    RegistryEntry,
    ResearchOutcome,
    ScientificRegistry,
    ScientificRegistryError,
)


_NON_POSITIVE_OUTCOMES = frozenset(
    {
        ResearchOutcome.NEGATIVE.value,
        ResearchOutcome.NULL.value,
        ResearchOutcome.HARMFUL.value,
        ResearchOutcome.INCONCLUSIVE.value,
    }
)
_TOKEN_RE = re.compile(r"[^\W_]+", flags=re.UNICODE)
_SHA256_CHARS = frozenset("0123456789abcdef")


class NegativeResultRetrievalError(ScientificRegistryError):
    """Raised when canonical negative-result lineage cannot be resolved safely."""


@dataclass(frozen=True, slots=True)
class NegativeResultHit:
    """One causally available non-positive experiment and its resolved lineage."""

    experiment_id: str
    outcome: ResearchOutcome
    available_at: str
    experiment_record_sha256: str
    experiment_fingerprint: str
    research_protocol_id: str
    protocol_record_sha256: str
    research_question_id: str
    question_record_sha256: str
    hypothesis_id: str
    hypothesis_record_sha256: str
    dataset_snapshot_id: str
    feature_set_id: str
    strategy_version_id: str
    model_version_id: str | None
    evaluation_bundle_id: str
    postmortem_ids: tuple[str, ...]
    postmortem_record_sha256s: tuple[str, ...]
    matched_terms: tuple[str, ...]
    score: int


def _normalized_terms(value: str, *, field: str) -> tuple[str, ...]:
    if type(value) is not str or "\x00" in value:
        raise ValueError(f"{field} must be text without NUL")
    normalized = unicodedata.normalize("NFKC", value.strip()).casefold().replace("_", " ")
    terms = tuple(dict.fromkeys(_TOKEN_RE.findall(normalized)))
    if not terms:
        raise ValueError(f"{field} must contain searchable text")
    return terms


def _payload_text(values: Iterable[object]) -> str:
    parts: list[str] = []
    for value in values:
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, (tuple, list)) and all(
            isinstance(item, str) for item in value
        ):
            parts.extend(value)
    return " ".join(parts)


def _availability_instant(entry: RegistryEntry) -> datetime:
    """Return one canonical registry availability timestamp as a UTC instant."""

    return datetime.fromisoformat(
        entry.available_at.replace("Z", "+00:00")
    ).astimezone(timezone.utc)


def _payload_instant(
    payload: Mapping[str, object],
    key: str,
    *,
    context: str,
) -> datetime:
    """Parse one required payload timestamp and normalize it to a UTC instant."""

    value = _required_text(payload, key, context=context)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NegativeResultRetrievalError(
            f"{context}.{key} is not valid ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise NegativeResultRetrievalError(
            f"{context}.{key} must include a timezone"
        )
    return parsed.astimezone(timezone.utc)


def _entries_by_id(
    registry: ScientificRegistry,
    record_type: str,
    *,
    as_of: str,
) -> dict[str, RegistryEntry]:
    return {
        entry.record_id: entry
        for entry in registry.causal_records(record_type, as_of=as_of)
    }


def _require_entry(
    values: dict[str, RegistryEntry],
    record_id: object,
    *,
    context: str,
) -> RegistryEntry:
    if type(record_id) is not str or not record_id:
        raise NegativeResultRetrievalError(f"{context} has invalid record identity")
    entry = values.get(record_id)
    if entry is None:
        raise NegativeResultRetrievalError(
            f"{context} lacks causally available canonical lineage: {record_id}"
        )
    return entry


def _required_text(
    payload: Mapping[str, object],
    key: str,
    *,
    context: str,
) -> str:
    value = payload.get(key)
    if type(value) is not str or not value:
        raise NegativeResultRetrievalError(f"{context}.{key} is invalid")
    return value


def _optional_text(
    payload: Mapping[str, object],
    key: str,
    *,
    context: str,
) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if type(value) is not str or not value:
        raise NegativeResultRetrievalError(f"{context}.{key} is invalid")
    return value


def _required_sha256(
    payload: Mapping[str, object],
    key: str,
    *,
    context: str,
) -> str:
    value = _required_text(payload, key, context=context)
    lowered = value.lower()
    if len(lowered) != 64 or any(char not in _SHA256_CHARS for char in lowered):
        raise NegativeResultRetrievalError(f"{context}.{key} is not canonical SHA-256")
    return lowered


def _canonical_payload_sha256(payload: Mapping[str, object]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def search_negative_results(
    registry: ScientificRegistry,
    query: str,
    *,
    as_of: str,
    limit: int = 20,
) -> tuple[NegativeResultHit, ...]:
    """Search durable non-positive research memory without creating authority.

    Matching is intentionally deterministic: Unicode NFKC + casefold tokenization,
    then exact token overlap across canonical question, hypothesis, protocol,
    experiment-note, identity, and matching Postmortem text. The integer ``score``
    is only the count of distinct query terms found; it is not statistical evidence
    and it cannot authorize retesting or promotion.
    """

    if not isinstance(registry, ScientificRegistry):
        raise TypeError("registry must be a ScientificRegistry")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer from 1 to 100")
    query_terms = _normalized_terms(query, field="query")

    experiments = registry.causal_records("Experiment", as_of=as_of)
    protocols = _entries_by_id(registry, "ResearchProtocol", as_of=as_of)
    hypotheses = _entries_by_id(registry, "Hypothesis", as_of=as_of)
    questions = _entries_by_id(registry, "ResearchQuestion", as_of=as_of)
    postmortems = registry.causal_records("Postmortem", as_of=as_of)

    postmortems_by_experiment: dict[str, list[RegistryEntry]] = {}
    for postmortem in postmortems:
        experiment_id = _required_text(
            postmortem.payload,
            "experiment_id",
            context=f"Postmortem:{postmortem.record_id}",
        )
        postmortems_by_experiment.setdefault(experiment_id, []).append(postmortem)

    hits: list[NegativeResultHit] = []
    for experiment in experiments:
        outcome_value = experiment.payload.get("outcome")
        if type(outcome_value) is not str:
            raise NegativeResultRetrievalError(
                f"Experiment:{experiment.record_id} has invalid outcome"
            )
        if outcome_value == ResearchOutcome.POSITIVE.value:
            continue
        if outcome_value not in _NON_POSITIVE_OUTCOMES:
            raise NegativeResultRetrievalError(
                f"Experiment:{experiment.record_id} has unsupported outcome"
            )
        outcome = ResearchOutcome(outcome_value)

        context = f"Experiment:{experiment.record_id}"
        protocol = _require_entry(
            protocols,
            experiment.payload.get("research_protocol_id"),
            context=context,
        )
        binding = protocol.payload.get("binding")
        if type(binding) is not dict:
            raise NegativeResultRetrievalError(
                f"ResearchProtocol:{protocol.record_id} lacks canonical binding"
            )
        hypothesis = _require_entry(
            hypotheses,
            binding.get("hypothesis_id"),
            context=f"ResearchProtocol:{protocol.record_id}",
        )
        question = _require_entry(
            questions,
            binding.get("research_question_id"),
            context=f"ResearchProtocol:{protocol.record_id}",
        )
        if (
            hypothesis.payload.get("research_question_id") != question.record_id
            or binding.get("hypothesis_id") != hypothesis.record_id
        ):
            raise NegativeResultRetrievalError(
                f"{context} has inconsistent question/hypothesis lineage"
            )

        protocol_context = f"ResearchProtocol:{protocol.record_id}"
        if _required_sha256(
            binding,
            "research_question_sha256",
            context=protocol_context,
        ) != _canonical_payload_sha256(question.payload):
            raise NegativeResultRetrievalError(
                f"{protocol_context} research question hash does not match frozen binding"
            )
        if _required_sha256(
            binding,
            "hypothesis_sha256",
            context=protocol_context,
        ) != _canonical_payload_sha256(hypothesis.payload):
            raise NegativeResultRetrievalError(
                f"{protocol_context} hypothesis hash does not match frozen binding"
            )

        causal_lineage = (
            ("ResearchQuestion", question.record_id, "Hypothesis", hypothesis.record_id),
            ("Hypothesis", hypothesis.record_id, "ResearchProtocol", protocol.record_id),
            ("ResearchProtocol", protocol.record_id, "Experiment", experiment.record_id),
        )
        for earlier_type, earlier_id, later_type, later_id in causal_lineage:
            if not registry.causal_precedes(
                earlier_type,
                earlier_id,
                later_type,
                later_id,
            ):
                raise NegativeResultRetrievalError(
                    f"{context} lineage does not causally precede experiment: "
                    f"{earlier_type}:{earlier_id} -> {later_type}:{later_id}"
                )

        experiment_created = _payload_instant(
            experiment.payload,
            "created_at",
            context=context,
        )
        declared_chronology = (
            (
                f"ResearchQuestion:{question.record_id}",
                _availability_instant(question),
                f"Hypothesis:{hypothesis.record_id}",
                _availability_instant(hypothesis),
            ),
            (
                f"Hypothesis:{hypothesis.record_id}",
                _availability_instant(hypothesis),
                f"ResearchProtocol:{protocol.record_id}",
                _availability_instant(protocol),
            ),
            (
                f"ResearchProtocol:{protocol.record_id}",
                _availability_instant(protocol),
                f"{context}.created_at",
                experiment_created,
            ),
        )
        for earlier_label, earlier_at, later_label, later_at in declared_chronology:
            if earlier_at > later_at:
                raise NegativeResultRetrievalError(
                    f"{context} declared lineage violates availability chronology: "
                    f"{earlier_label} -> {later_label}"
                )

        matching_postmortems = tuple(
            sorted(
                postmortems_by_experiment.get(experiment.record_id, ()),
                key=lambda item: (item.available_at, item.record_id),
            )
        )
        postmortem_text: list[str] = []
        for postmortem in matching_postmortems:
            post_context = f"Postmortem:{postmortem.record_id}"
            if not registry.causal_precedes(
                "Experiment",
                experiment.record_id,
                "Postmortem",
                postmortem.record_id,
            ):
                raise NegativeResultRetrievalError(
                    f"{post_context} does not causally follow {context}"
                )
            if _availability_instant(postmortem) < _availability_instant(experiment):
                raise NegativeResultRetrievalError(
                    f"{post_context} availability precedes {context}"
                )
            if postmortem.payload.get("classification") != outcome.value:
                raise NegativeResultRetrievalError(
                    f"{post_context} classification conflicts with experiment outcome"
                )
            finding = _required_text(
                postmortem.payload,
                "finding",
                context=post_context,
            )
            retest_conditions = postmortem.payload.get("retest_conditions")
            if type(retest_conditions) is not list or any(
                type(value) is not str or not value
                for value in retest_conditions
            ):
                raise NegativeResultRetrievalError(
                    f"{post_context}.retest_conditions is invalid"
                )
            postmortem_text.append(finding)
            postmortem_text.extend(retest_conditions)

        experiment_fingerprint = _required_sha256(
            experiment.payload,
            "fingerprint",
            context=context,
        )
        dataset_snapshot_id = _required_text(
            experiment.payload,
            "dataset_snapshot_id",
            context=context,
        )
        feature_set_id = _required_text(
            experiment.payload,
            "feature_set_id",
            context=context,
        )
        strategy_version_id = _required_text(
            experiment.payload,
            "strategy_version_id",
            context=context,
        )
        evaluation_bundle_id = _required_text(
            experiment.payload,
            "evaluation_bundle_id",
            context=context,
        )
        model_version_id = _optional_text(
            experiment.payload,
            "model_version_id",
            context=context,
        )

        corpus = _payload_text(
            (
                outcome.value,
                question.payload.get("statement"),
                hypothesis.payload.get("statement"),
                hypothesis.payload.get("falsifiable_prediction"),
                hypothesis.payload.get("failure_criteria"),
                hypothesis.payload.get("primary_metric"),
                hypothesis.payload.get("protective_metrics"),
                binding.get("inclusion_criteria"),
                binding.get("exclusion_criteria"),
                binding.get("lawful_source_requirements"),
                binding.get("evaluation_design"),
                binding.get("feature_set_version"),
                binding.get("uncertainty_method"),
                binding.get("robustness_checks"),
                binding.get("promotion_rule"),
                experiment.payload.get("notes"),
                dataset_snapshot_id,
                feature_set_id,
                strategy_version_id,
                model_version_id,
                evaluation_bundle_id,
                tuple(postmortem_text),
            )
        )
        corpus_terms = set(
            _normalized_terms(corpus, field="canonical negative-result corpus")
        )
        matched_terms = tuple(term for term in query_terms if term in corpus_terms)
        if not matched_terms:
            continue

        evidence_entries = (
            experiment,
            protocol,
            hypothesis,
            question,
            *matching_postmortems,
        )
        evidence_available_at = max(
            evidence_entries,
            key=_availability_instant,
        ).available_at

        hits.append(
            NegativeResultHit(
                experiment_id=experiment.record_id,
                outcome=outcome,
                available_at=evidence_available_at,
                experiment_record_sha256=experiment.record_sha256,
                experiment_fingerprint=experiment_fingerprint,
                research_protocol_id=protocol.record_id,
                protocol_record_sha256=protocol.record_sha256,
                research_question_id=question.record_id,
                question_record_sha256=question.record_sha256,
                hypothesis_id=hypothesis.record_id,
                hypothesis_record_sha256=hypothesis.record_sha256,
                dataset_snapshot_id=dataset_snapshot_id,
                feature_set_id=feature_set_id,
                strategy_version_id=strategy_version_id,
                model_version_id=model_version_id,
                evaluation_bundle_id=evaluation_bundle_id,
                postmortem_ids=tuple(
                    item.record_id for item in matching_postmortems
                ),
                postmortem_record_sha256s=tuple(
                    item.record_sha256 for item in matching_postmortems
                ),
                matched_terms=matched_terms,
                score=len(matched_terms),
            )
        )

    hits.sort(key=lambda item: (-item.score, item.experiment_id))
    return tuple(hits[:limit])
