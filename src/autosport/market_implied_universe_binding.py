"""Product-owned composition gate for market-implied baseline evidence.

The lower-level market-implied module derives a causal market probability vector but
explicitly does not prove provider origin for the typed outcome roster on current main.
This module closes the separate cohort-identity seam by resolving the complete
evaluation membership from the canonical durable ``EvaluationUniverseStore`` and
binding each caller label to the exact frozen row it names.  Row authority must not be
mistaken for outcome-roster provenance.

No scoring, promotion, provider write, execution, or real-money authority is added.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .evaluation_universe import (
    EvaluationRow,
    EvaluationUniverseStore,
    FunnelStage,
    SlotState,
)
from .external_validity_baseline import BaselineDefinition, FrozenBaselineProtocol
from .market_implied_baseline import (
    MarketImpliedBaselineEvidence,
    bind_market_implied_baseline_cohort,
)


class MarketImpliedUniverseBindingError(ValueError):
    """Market-implied evidence does not bind the canonical evaluation universe."""


_ROW_TOKEN = object()
_COHORT_TOKEN = object()
_HEX = frozenset("0123456789abcdef")


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise MarketImpliedUniverseBindingError(
            f"{name} must be a non-empty canonical string"
        )
    value.encode("utf-8")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or text != text.lower() or any(ch not in _HEX for ch in text):
        raise MarketImpliedUniverseBindingError(f"{name} must be canonical SHA-256 hex")
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MarketImpliedUniverseBindingError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MarketImpliedUniverseBindingError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _digest(payload: Mapping[str, Any] | Sequence[Any]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class MarketImpliedUniverseRowBinding:
    """Exact baseline probability attached to one canonical evaluation row."""

    row_key: str
    row_id: str
    target_selection_id: str
    market_evidence_sha256: str
    probability_numerator: int
    probability_denominator: int
    _token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _ROW_TOKEN:
            raise TypeError(
                "MarketImpliedUniverseRowBinding must be issued by the universe gate"
            )
        _text(self.row_key, "row_key")
        _sha(self.row_id, "row_id")
        _text(self.target_selection_id, "target_selection_id")
        _sha(self.market_evidence_sha256, "market_evidence_sha256")
        if (
            type(self.probability_numerator) is not int
            or type(self.probability_denominator) is not int
            or self.probability_numerator <= 0
            or self.probability_denominator <= 0
        ):
            raise MarketImpliedUniverseBindingError(
                "bound probability must use positive exact integer fraction"
            )

    def to_payload(self) -> dict[str, object]:
        return {
            "row_key": self.row_key,
            "row_id": self.row_id,
            "target_selection_id": self.target_selection_id,
            "market_evidence_sha256": self.market_evidence_sha256,
            "probability": {
                "numerator": self.probability_numerator,
                "denominator": self.probability_denominator,
            },
        }

    @property
    def binding_sha256(self) -> str:
        return _digest(self.to_payload())


@dataclass(frozen=True, slots=True)
class MarketImpliedUniverseCohortEvidence:
    """Durable-universe binding around the lower-level market-implied cohort."""

    protocol_sha256: str
    base_cohort_evidence_sha256: str
    universe_sha256: str
    membership_sha256: str
    intake_snapshot_sha256: str
    intake_authority_id: str
    row_bindings: tuple[MarketImpliedUniverseRowBinding, ...]
    _token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _COHORT_TOKEN:
            raise TypeError(
                "MarketImpliedUniverseCohortEvidence must be issued by the universe gate"
            )
        for name in (
            "protocol_sha256",
            "base_cohort_evidence_sha256",
            "universe_sha256",
            "membership_sha256",
            "intake_snapshot_sha256",
        ):
            _sha(getattr(self, name), name)
        _text(self.intake_authority_id, "intake_authority_id")
        if type(self.row_bindings) is not tuple or not self.row_bindings:
            raise MarketImpliedUniverseBindingError("row_bindings must be a non-empty tuple")
        if any(type(item) is not MarketImpliedUniverseRowBinding for item in self.row_bindings):
            raise MarketImpliedUniverseBindingError(
                "row_bindings must contain exact issued row bindings"
            )
        keys = tuple(item.row_key for item in self.row_bindings)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise MarketImpliedUniverseBindingError(
                "row_bindings must use unique canonical row-key order"
            )

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": "autosport.market-implied-evaluation-universe-binding.v1",
            "protocol_sha256": self.protocol_sha256,
            "base_cohort_evidence_sha256": self.base_cohort_evidence_sha256,
            "universe_sha256": self.universe_sha256,
            "membership_sha256": self.membership_sha256,
            "intake_snapshot_sha256": self.intake_snapshot_sha256,
            "intake_authority_id": self.intake_authority_id,
            "row_bindings": [item.to_payload() for item in self.row_bindings],
            "truth": {
                "canonical_evaluation_universe_bound": True,
                "complete_universe_membership_required": True,
                "zero_or_missing_membership_fails_closed": True,
                "outcome_roster_origin_verified": False,
                "scientific_completeness_proven": False,
                "source_stream_continuity_proven": False,
                "metric_computed": False,
                "execution_authority": False,
                "promotion_authority": False,
                "real_money_execution": False,
            },
        }

    @property
    def evidence_sha256(self) -> str:
        return _digest(self._payload())

    def to_dict(self) -> dict[str, object]:
        return {**self._payload(), "evidence_sha256": self.evidence_sha256}


def _assert_row_matches_market_evidence(
    *,
    row: EvaluationRow,
    evidence: MarketImpliedBaselineEvidence,
) -> MarketImpliedUniverseRowBinding:
    if type(row) is not EvaluationRow:
        raise MarketImpliedUniverseBindingError("universe must contain exact EvaluationRow values")
    if type(evidence) is not MarketImpliedBaselineEvidence:
        raise MarketImpliedUniverseBindingError(
            "market evidence must use exact issued MarketImpliedBaselineEvidence"
        )
    if row.slot_state is not SlotState.CANDIDATE:
        raise MarketImpliedUniverseBindingError(
            "market-implied cohort cannot erase zero/outage/missing denominator rows"
        )
    if row.decision_stage not in {
        FunnelStage.ELIGIBLE,
        FunnelStage.EXECUTION_MODEL_ELIGIBLE,
    }:
        raise MarketImpliedUniverseBindingError(
            "market-implied cohort requires a canonical decided evaluation row"
        )
    if row.decision_at is None or row.event_id is None or row.market_id is None or row.selection_id is None:
        raise MarketImpliedUniverseBindingError(
            "canonical evaluation row lacks decision market/selection identity"
        )
    if evidence.cohort_key != row.row_key:
        raise MarketImpliedUniverseBindingError(
            "market evidence cohort label does not equal canonical row_key"
        )
    if (
        evidence.sport,
        evidence.event_id,
        evidence.market_id,
        evidence.source_id,
    ) != (
        row.sport,
        row.event_id,
        row.market_id,
        row.source_id,
    ):
        raise MarketImpliedUniverseBindingError(
            "market evidence identity does not match canonical evaluation row"
        )
    if _instant(evidence.decision_cutoff, "decision_cutoff") != _instant(
        row.decision_at,
        "row decision_at",
    ):
        raise MarketImpliedUniverseBindingError(
            "market evidence cutoff does not equal canonical row decision time"
        )
    probability = next(
        (item for item in evidence.probabilities if item.selection_id == row.selection_id),
        None,
    )
    if probability is None:
        raise MarketImpliedUniverseBindingError(
            "canonical row selection is absent from market-implied probability vector"
        )
    return MarketImpliedUniverseRowBinding(
        row_key=row.row_key,
        row_id=row.row_id,
        target_selection_id=row.selection_id,
        market_evidence_sha256=evidence.evidence_sha256,
        probability_numerator=probability.numerator,
        probability_denominator=probability.denominator,
        _token=_ROW_TOKEN,
    )


def bind_market_implied_baseline_to_evaluation_universe(
    *,
    protocol: FrozenBaselineProtocol,
    baseline_definition: BaselineDefinition,
    evidence: Sequence[MarketImpliedBaselineEvidence],
    evaluation_store: EvaluationUniverseStore,
) -> MarketImpliedUniverseCohortEvidence:
    """Bind market-implied probabilities to the complete durable evaluation universe.

    The store is loaded through its monotonic/intake verification boundary.  The
    external-validity cohort must equal the complete frozen universe row-key set;
    rows without a causally decided candidate fail closed rather than disappearing
    from the denominator.  This operation intentionally does not strengthen the
    lower-level outcome-roster origin truth.
    """

    if type(evaluation_store) is not EvaluationUniverseStore:
        raise TypeError("evaluation_store must be exact EvaluationUniverseStore")
    ledger = evaluation_store.load()
    if ledger is None:
        raise MarketImpliedUniverseBindingError(
            "canonical evaluation-universe store has no durable ledger"
        )
    universe = ledger.universe

    base = bind_market_implied_baseline_cohort(
        protocol=protocol,
        baseline_definition=baseline_definition,
        evidence=evidence,
    )
    rows = tuple(universe.rows)
    row_keys = tuple(row.row_key for row in rows)
    if row_keys != protocol.evidence_scope.cohort_keys:
        raise MarketImpliedUniverseBindingError(
            "external-validity cohort must equal complete canonical evaluation-universe membership"
        )
    evidence_rows = tuple(evidence)
    if len(evidence_rows) != len(rows):
        raise MarketImpliedUniverseBindingError(
            "market evidence count must equal complete canonical universe membership"
        )

    bindings = tuple(
        _assert_row_matches_market_evidence(row=row, evidence=item)
        for row, item in zip(rows, evidence_rows, strict=True)
    )
    return MarketImpliedUniverseCohortEvidence(
        protocol_sha256=protocol.identity_sha256,
        base_cohort_evidence_sha256=base.evidence_sha256,
        universe_sha256=universe.universe_sha256,
        membership_sha256=universe.membership_sha256,
        intake_snapshot_sha256=universe.intake_snapshot.snapshot_sha256,
        intake_authority_id=evaluation_store.intake_ledger.authority_id,
        row_bindings=bindings,
        _token=_COHORT_TOKEN,
    )
