from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, DecimalTuple, localcontext
from enum import StrEnum
from typing import Any
from weakref import ReferenceType, ref

from .evaluation_universe import (
    CanonicalPaperExecutionResolver,
    EvaluationRow,
    EvaluationUniverseError,
    EvaluationUniverseLedger,
    FunnelStage,
)
from .paper_execution_reality import PaperAttemptOutcome, PaperLegAttempt


SCHEMA_VERSION = 1
_OUTCOME_STAGES = frozenset(
    {
        FunnelStage.ACCEPTED,
        FunnelStage.PARTIAL,
        FunnelStage.REJECTED,
        FunnelStage.UNKNOWN,
    }
)


class ExecutionQualityEvidenceClass(StrEnum):
    PAPER_EXECUTION_MODEL = "PAPER_EXECUTION_MODEL"
    PROVIDER_OBSERVED = "PROVIDER_OBSERVED"
    LOCAL_TRANSPORT_OBSERVED = "LOCAL_TRANSPORT_OBSERVED"
    INCOMPLETE_OR_UNKNOWN = "INCOMPLETE_OR_UNKNOWN"


class ExecutionQualityEvidenceError(ValueError):
    """Execution-quality projection cannot be proven from canonical evidence."""


def _decimal_text(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ExecutionQualityEvidenceError("quality Decimal must be finite")
    # Decimal.normalize() applies the ambient Decimal context and can therefore
    # round authority-bearing odds/stakes before hashing. Formatting without a
    # precision is exact; trimming only insignificant fractional zeros is
    # context-independent and preserves the existing canonical text shape.
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _exact_decimal_subtract(left: Decimal, right: Decimal) -> Decimal:
    """Subtract finite Decimals without consulting the ambient Decimal context."""

    if (
        not isinstance(left, Decimal)
        or not isinstance(right, Decimal)
        or not left.is_finite()
        or not right.is_finite()
    ):
        raise ExecutionQualityEvidenceError(
            "quality Decimal arithmetic requires finite Decimal operands"
        )

    left_tuple = left.as_tuple()
    right_tuple = right.as_tuple()
    common_exponent = min(left_tuple.exponent, right_tuple.exponent)

    def scaled_coefficient(value_tuple: DecimalTuple) -> int:
        coefficient = 0
        for digit in value_tuple.digits:
            coefficient = coefficient * 10 + digit
        if value_tuple.sign:
            coefficient = -coefficient
        return coefficient * (10 ** (value_tuple.exponent - common_exponent))

    difference = scaled_coefficient(left_tuple) - scaled_coefficient(right_tuple)
    sign = 1 if difference < 0 else 0
    digits = (
        tuple(int(character) for character in str(abs(difference)))
        if difference
        else (0,)
    )
    return Decimal((sign, digits, common_exponent))


def _canonical(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ExecutionQualityEvidenceError(
            "execution-quality evidence is not canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True, init=False, weakref_slot=True)
class PaperExecutionQualitySample:
    """Derived PAPER-only execution-quality sample.

    This object never upgrades PAPER/model timing into real provider or local-transport
    latency. Positive provider/local latency fields intentionally do not exist here.
    """

    universe_sha256: str
    membership_sha256: str
    research_protocol_id: str
    protocol_sha256: str
    row_id: str
    canonical_reality_sha256: str
    attempt_id: str
    run_id: str
    plan_id: str
    action_id: str
    bookmaker_id: str
    account_id: str
    event_id: str
    market_id: str
    selection_id: str
    model_fingerprint: str
    decision_quote_id: str
    side: str
    decision_odds: Decimal
    requested_stake: Decimal
    outcome: PaperAttemptOutcome
    execution_odds: Decimal | None
    execution_stake: Decimal | None
    paper_model_delay_ms: int
    paper_model_quote_age_ms: int
    signed_price_delta: Decimal | None
    adverse_slippage: Decimal | None
    completion_numerator: Decimal | None
    evidence_grade: str
    evidence_source: str
    evidence_id: str | None
    evidence_sha256: str | None
    evidence_class: ExecutionQualityEvidenceClass = (
        ExecutionQualityEvidenceClass.PAPER_EXECUTION_MODEL
    )
    schema_version: int = SCHEMA_VERSION

    def __new__(cls, *args, **kwargs):
        del args, kwargs
        raise TypeError(
            "PaperExecutionQualitySample is issued only by the canonical quality projector"
        )

    @classmethod
    def _construct(cls, **values: object) -> "PaperExecutionQualitySample":
        values = {
            "evidence_class": ExecutionQualityEvidenceClass.PAPER_EXECUTION_MODEL,
            "schema_version": SCHEMA_VERSION,
            **values,
        }
        expected = set(cls.__dataclass_fields__)
        if set(values) != expected:
            raise ExecutionQualityEvidenceError(
                "execution-quality sample construction fields are incomplete"
            )
        obj = object.__new__(cls)
        for name in cls.__dataclass_fields__:
            object.__setattr__(obj, name, values[name])
        obj._validate()
        return obj

    def _validate(self) -> None:
        if self.evidence_class is not ExecutionQualityEvidenceClass.PAPER_EXECUTION_MODEL:
            raise ExecutionQualityEvidenceError(
                "PaperExecutionQualitySample cannot claim non-PAPER evidence"
            )
        if self.schema_version != SCHEMA_VERSION:
            raise ExecutionQualityEvidenceError(
                "unsupported execution-quality sample schema"
            )
        if self.paper_model_delay_ms < 0 or self.paper_model_quote_age_ms < 0:
            raise ExecutionQualityEvidenceError(
                "PAPER timing evidence cannot be negative"
            )
        if self.outcome in {
            PaperAttemptOutcome.ACCEPTED,
            PaperAttemptOutcome.PARTIAL,
        }:
            if self.execution_odds is None or self.execution_stake is None:
                raise ExecutionQualityEvidenceError(
                    "accepted/partial sample requires execution odds/stake"
                )
            if self.completion_numerator != self.execution_stake:
                raise ExecutionQualityEvidenceError(
                    "completion numerator must equal canonical execution stake"
                )
        elif self.outcome is PaperAttemptOutcome.REJECTED:
            if (
                self.execution_odds is not None
                or self.execution_stake is not None
                or self.completion_numerator != Decimal("0")
            ):
                raise ExecutionQualityEvidenceError(
                    "rejected sample must carry zero known completion and no fill price"
                )
        elif self.outcome is PaperAttemptOutcome.UNKNOWN:
            if (
                self.execution_odds is not None
                or self.execution_stake is not None
                or self.completion_numerator is not None
            ):
                raise ExecutionQualityEvidenceError(
                    "UNKNOWN sample cannot fabricate fill completion"
                )
        else:
            raise ExecutionQualityEvidenceError("unsupported PAPER attempt outcome")

        price_applicable = (
            self.side == "BACK"
            and self.outcome
            in {PaperAttemptOutcome.ACCEPTED, PaperAttemptOutcome.PARTIAL}
        )
        if price_applicable:
            if self.signed_price_delta is None or self.adverse_slippage is None:
                raise ExecutionQualityEvidenceError(
                    "accepted/partial BACK sample requires derived price-quality evidence"
                )
            if self.adverse_slippage < 0:
                raise ExecutionQualityEvidenceError(
                    "adverse slippage cannot be negative"
                )
        elif self.signed_price_delta is not None or self.adverse_slippage is not None:
            raise ExecutionQualityEvidenceError(
                "unsupported side/outcome cannot claim BACK price-quality evidence"
            )

    @property
    def completion_ratio(self) -> Decimal | None:
        if self.completion_numerator is None:
            return None
        with localcontext() as context:
            context.prec = 50
            return self.completion_numerator / self.requested_stake

    @property
    def sample_sha256(self) -> str:
        return _digest(self.to_payload(include_digest=False))

    def to_payload(self, *, include_digest: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "evidence_class": self.evidence_class.value,
            "universe_sha256": self.universe_sha256,
            "membership_sha256": self.membership_sha256,
            "research_protocol_id": self.research_protocol_id,
            "protocol_sha256": self.protocol_sha256,
            "row_id": self.row_id,
            "canonical_reality_sha256": self.canonical_reality_sha256,
            "attempt_id": self.attempt_id,
            "run_id": self.run_id,
            "plan_id": self.plan_id,
            "action_id": self.action_id,
            "bookmaker_id": self.bookmaker_id,
            "account_id": self.account_id,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "model_fingerprint": self.model_fingerprint,
            "decision_quote_id": self.decision_quote_id,
            "side": self.side,
            "decision_odds": _decimal_text(self.decision_odds),
            "requested_stake": _decimal_text(self.requested_stake),
            "outcome": self.outcome.value,
            "execution_odds": (
                None
                if self.execution_odds is None
                else _decimal_text(self.execution_odds)
            ),
            "execution_stake": (
                None
                if self.execution_stake is None
                else _decimal_text(self.execution_stake)
            ),
            "paper_model_delay_ms": self.paper_model_delay_ms,
            "paper_model_quote_age_ms": self.paper_model_quote_age_ms,
            "signed_price_delta": (
                None
                if self.signed_price_delta is None
                else _decimal_text(self.signed_price_delta)
            ),
            "adverse_slippage": (
                None
                if self.adverse_slippage is None
                else _decimal_text(self.adverse_slippage)
            ),
            "completion_numerator": (
                None
                if self.completion_numerator is None
                else _decimal_text(self.completion_numerator)
            ),
            "completion_denominator": _decimal_text(self.requested_stake),
            "evidence_grade": self.evidence_grade,
            "evidence_source": self.evidence_source,
            "evidence_id": self.evidence_id,
            "evidence_sha256": self.evidence_sha256,
            # Real local/provider latency remains deliberately absent from this class.
            "local_decision_to_submit_ms": None,
            "local_submit_to_response_ms": None,
            "provider_processing_latency_ms": None,
        }
        if include_digest:
            payload["sample_sha256"] = self.sample_sha256
        return payload


@dataclass(frozen=True, slots=True, weakref_slot=True)
class PaperExecutionQualityReport:
    """Complete frozen-denominator report over canonical PAPER outcomes."""

    universe_sha256: str
    membership_sha256: str
    research_protocol_id: str
    protocol_sha256: str
    denominator_row_ids: tuple[str, ...]
    attempted_row_ids: tuple[str, ...]
    stage_counts: tuple[tuple[str, int], ...]
    samples: tuple[PaperExecutionQualitySample, ...]
    schema_version: int = SCHEMA_VERSION

    def __new__(cls, *args, **kwargs):
        del args, kwargs
        raise TypeError(
            "PaperExecutionQualityReport is issued only from a canonical frozen ledger"
        )

    @classmethod
    def _construct(cls, **values: object) -> "PaperExecutionQualityReport":
        values = {"schema_version": SCHEMA_VERSION, **values}
        expected = set(cls.__dataclass_fields__)
        if set(values) != expected:
            raise ExecutionQualityEvidenceError(
                "execution-quality report construction fields are incomplete"
            )
        obj = object.__new__(cls)
        for name in cls.__dataclass_fields__:
            object.__setattr__(obj, name, values[name])
        obj._validate()
        return obj

    def _validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ExecutionQualityEvidenceError(
                "unsupported execution-quality report schema"
            )
        if not self.denominator_row_ids:
            raise ExecutionQualityEvidenceError(
                "execution-quality report requires a frozen denominator"
            )
        if self.denominator_row_ids != tuple(sorted(self.denominator_row_ids)):
            raise ExecutionQualityEvidenceError(
                "denominator row ids must use canonical order"
            )
        if len(set(self.denominator_row_ids)) != len(self.denominator_row_ids):
            raise ExecutionQualityEvidenceError(
                "denominator row ids must be unique"
            )
        if (
            self.attempted_row_ids != tuple(sorted(self.attempted_row_ids))
            or len(set(self.attempted_row_ids)) != len(self.attempted_row_ids)
            or not set(self.attempted_row_ids).issubset(set(self.denominator_row_ids))
        ):
            raise ExecutionQualityEvidenceError(
                "attempted rows must be a canonical subset of the frozen denominator"
            )
        sample_rows = tuple(sample.row_id for sample in self.samples)
        if sample_rows != tuple(sorted(sample_rows)) or len(set(sample_rows)) != len(
            sample_rows
        ):
            raise ExecutionQualityEvidenceError(
                "quality samples must be one per row in canonical order"
            )
        denominator = set(self.denominator_row_ids)
        attempted = set(self.attempted_row_ids)
        if not set(sample_rows).issubset(attempted):
            raise ExecutionQualityEvidenceError(
                "execution outcomes require a prior ATTEMPTED funnel identity"
            )
        for sample in self.samples:
            if type(sample) is not PaperExecutionQualitySample:
                raise ExecutionQualityEvidenceError(
                    "report samples must use exact PaperExecutionQualitySample"
                )
            if sample.row_id not in denominator:
                raise ExecutionQualityEvidenceError(
                    "quality sample lies outside frozen denominator"
                )
            if (
                sample.universe_sha256,
                sample.membership_sha256,
                sample.research_protocol_id,
                sample.protocol_sha256,
            ) != (
                self.universe_sha256,
                self.membership_sha256,
                self.research_protocol_id,
                self.protocol_sha256,
            ):
                raise ExecutionQualityEvidenceError(
                    "quality sample does not bind report universe/protocol"
                )

    @property
    def denominator_count(self) -> int:
        return len(self.denominator_row_ids)

    @property
    def attempted_count(self) -> int:
        return len(self.attempted_row_ids)

    @property
    def attempted_without_execution_outcome_count(self) -> int:
        return self.attempted_count - len(self.samples)

    @property
    def outcome_counts(self) -> tuple[tuple[str, int], ...]:
        counts = {outcome.value: 0 for outcome in PaperAttemptOutcome}
        for sample in self.samples:
            counts[sample.outcome.value] += 1
        return tuple(sorted(counts.items()))

    @property
    def rows_without_execution_outcome_count(self) -> int:
        return self.denominator_count - len(self.samples)

    @property
    def paper_model_delay_ms_distribution(self) -> tuple[int, ...]:
        return tuple(sorted(sample.paper_model_delay_ms for sample in self.samples))

    @property
    def paper_model_quote_age_ms_distribution(self) -> tuple[int, ...]:
        return tuple(
            sorted(sample.paper_model_quote_age_ms for sample in self.samples)
        )

    @property
    def back_signed_price_delta_distribution(self) -> tuple[Decimal, ...]:
        return tuple(
            sorted(
                sample.signed_price_delta
                for sample in self.samples
                if sample.signed_price_delta is not None
            )
        )

    @property
    def back_adverse_slippage_distribution(self) -> tuple[Decimal, ...]:
        return tuple(
            sorted(
                sample.adverse_slippage
                for sample in self.samples
                if sample.adverse_slippage is not None
            )
        )

    @property
    def known_completion_fraction_distribution(
        self,
    ) -> tuple[tuple[Decimal, Decimal], ...]:
        return tuple(
            sorted(
                (
                    sample.completion_numerator,
                    sample.requested_stake,
                )
                for sample in self.samples
                if sample.completion_numerator is not None
            )
        )

    @property
    def report_sha256(self) -> str:
        return _digest(self.to_payload(include_digest=False))

    def to_payload(self, *, include_digest: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "evidence_class": ExecutionQualityEvidenceClass.PAPER_EXECUTION_MODEL.value,
            "universe_sha256": self.universe_sha256,
            "membership_sha256": self.membership_sha256,
            "research_protocol_id": self.research_protocol_id,
            "protocol_sha256": self.protocol_sha256,
            "denominator_row_ids": list(self.denominator_row_ids),
            "denominator_count": self.denominator_count,
            "attempted_row_ids": list(self.attempted_row_ids),
            "attempted_count": self.attempted_count,
            "attempted_without_execution_outcome_count": (
                self.attempted_without_execution_outcome_count
            ),
            "stage_counts": [list(item) for item in self.stage_counts],
            "outcome_counts": [list(item) for item in self.outcome_counts],
            "rows_without_execution_outcome_count": (
                self.rows_without_execution_outcome_count
            ),
            "sample_sha256s": [sample.sample_sha256 for sample in self.samples],
            "paper_model_delay_ms_distribution": list(
                self.paper_model_delay_ms_distribution
            ),
            "paper_model_quote_age_ms_distribution": list(
                self.paper_model_quote_age_ms_distribution
            ),
            "back_signed_price_delta_distribution": [
                _decimal_text(value)
                for value in self.back_signed_price_delta_distribution
            ],
            "back_adverse_slippage_distribution": [
                _decimal_text(value)
                for value in self.back_adverse_slippage_distribution
            ],
            "known_completion_fraction_distribution": [
                {
                    "filled": _decimal_text(filled),
                    "requested": _decimal_text(requested),
                }
                for filled, requested in self.known_completion_fraction_distribution
            ],
        }
        if include_digest:
            payload["report_sha256"] = self.report_sha256
        return payload


def _project_paper_attempt(
    *,
    ledger: EvaluationUniverseLedger,
    row: EvaluationRow,
    attempt: PaperLegAttempt,
    canonical_reality_sha256: str,
) -> PaperExecutionQualitySample:
    signed_price_delta: Decimal | None = None
    adverse_slippage: Decimal | None = None
    if (
        attempt.side == "BACK"
        and attempt.outcome
        in {PaperAttemptOutcome.ACCEPTED, PaperAttemptOutcome.PARTIAL}
    ):
        assert attempt.execution_odds is not None
        signed_price_delta = _exact_decimal_subtract(
            attempt.execution_odds,
            attempt.decision_odds,
        )
        adverse_slippage = (
            signed_price_delta.copy_negate()
            if signed_price_delta < 0
            else Decimal("0")
        )

    completion_numerator: Decimal | None
    if attempt.outcome in {
        PaperAttemptOutcome.ACCEPTED,
        PaperAttemptOutcome.PARTIAL,
    }:
        assert attempt.execution_stake is not None
        completion_numerator = attempt.execution_stake
    elif attempt.outcome is PaperAttemptOutcome.REJECTED:
        completion_numerator = Decimal("0")
    else:
        completion_numerator = None

    universe = ledger.universe
    return PaperExecutionQualitySample._construct(
        universe_sha256=universe.universe_sha256,
        membership_sha256=universe.membership_sha256,
        research_protocol_id=universe.research_protocol_id,
        protocol_sha256=universe.protocol_sha256,
        row_id=row.row_id,
        canonical_reality_sha256=canonical_reality_sha256,
        attempt_id=attempt.attempt_id,
        run_id=attempt.run_id,
        plan_id=attempt.plan_id,
        action_id=attempt.action_id,
        bookmaker_id=attempt.bookmaker_id,
        account_id=attempt.account_id,
        event_id=attempt.event_id,
        market_id=attempt.market_id,
        selection_id=attempt.selection_id,
        model_fingerprint=attempt.model_fingerprint,
        decision_quote_id=attempt.decision_quote_id,
        side=attempt.side,
        decision_odds=attempt.decision_odds,
        requested_stake=attempt.requested_stake,
        outcome=attempt.outcome,
        execution_odds=attempt.execution_odds,
        execution_stake=attempt.execution_stake,
        paper_model_delay_ms=attempt.delay_ms,
        paper_model_quote_age_ms=attempt.quote_age_ms,
        signed_price_delta=signed_price_delta,
        adverse_slippage=adverse_slippage,
        completion_numerator=completion_numerator,
        evidence_grade=attempt.evidence_grade.value,
        evidence_source=attempt.evidence_source,
        evidence_id=attempt.evidence_id,
        evidence_sha256=attempt.evidence_sha256,
    )


def _build_paper_execution_quality_report_unissued(
    ledger: EvaluationUniverseLedger,
) -> PaperExecutionQualityReport:
    """Project canonical PAPER outcomes over the entire frozen denominator.

    The caller cannot pass attempts, prices, latencies, or a hand-picked row subset.
    Every positive sample is re-resolved through the exact canonical #623 resolver
    already bound into the validated EvaluationUniverseLedger.
    """

    if type(ledger) is not EvaluationUniverseLedger:
        raise TypeError("ledger must be exact EvaluationUniverseLedger")

    resolver = ledger.paper_resolver
    outcome_events_by_row: dict[str, list[Any]] = {}
    for event in ledger.events:
        if event.stage in _OUTCOME_STAGES:
            outcome_events_by_row.setdefault(event.row_id, []).append(event)

    if outcome_events_by_row and type(resolver) is not CanonicalPaperExecutionResolver:
        raise ExecutionQualityEvidenceError(
            "PAPER quality evidence requires exact canonical execution resolver"
        )

    samples: list[PaperExecutionQualitySample] = []
    for row in ledger.universe.rows:
        events = outcome_events_by_row.get(row.row_id, [])
        if not events:
            continue
        if len(events) != 1:
            raise EvaluationUniverseError(
                "one frozen row cannot contribute multiple independent PAPER outcomes"
            )
        event = events[0]
        if event.execution_attempt_id is None or event.execution_reality_sha256 is None:
            raise ExecutionQualityEvidenceError(
                "PAPER outcome lacks canonical attempt/reality identity"
            )
        assert resolver is not None
        attempt = resolver.resolve(
            row=row,
            attempt_id=event.execution_attempt_id,
            reality_sha256=event.execution_reality_sha256,
        )
        samples.append(
            _project_paper_attempt(
                ledger=ledger,
                row=row,
                attempt=attempt,
                canonical_reality_sha256=event.execution_reality_sha256,
            )
        )

    samples.sort(key=lambda sample: sample.row_id)
    attempted_row_ids = tuple(
        sorted(
            {
                event.row_id
                for event in ledger.events
                if event.stage is FunnelStage.ATTEMPTED
            }
        )
    )
    cohort = ledger.cohort()
    return PaperExecutionQualityReport._construct(
        universe_sha256=ledger.universe.universe_sha256,
        membership_sha256=ledger.universe.membership_sha256,
        research_protocol_id=ledger.universe.research_protocol_id,
        protocol_sha256=ledger.universe.protocol_sha256,
        denominator_row_ids=ledger.universe.row_ids,
        attempted_row_ids=attempted_row_ids,
        stage_counts=cohort.stage_counts,
        samples=tuple(samples),
    )


@dataclass(frozen=True, slots=True)
class _IssuedQualityRecord:
    value_ref: ReferenceType[object]
    digest: str


def _build_quality_issuance_boundary():
    """Bind positive quality evidence to canonical projector issuance.

    The registry is deliberately process-local. After restart callers rebuild the
    deterministic report from the durable canonical EvaluationUniverse/PAPER
    evidence instead of deserializing DTOs into authority.
    """

    sample_cls = PaperExecutionQualitySample
    report_cls = PaperExecutionQualityReport
    unissued_builder = _build_paper_execution_quality_report_unissued
    issued_samples: dict[int, _IssuedQualityRecord] = {}
    issued_reports: dict[int, _IssuedQualityRecord] = {}

    def register(
        registry: dict[int, _IssuedQualityRecord],
        value: object,
        digest: str,
    ) -> None:
        identity = id(value)

        def discard(dead_ref: ReferenceType[object]) -> None:
            current = registry.get(identity)
            if current is not None and current.value_ref is dead_ref:
                registry.pop(identity, None)

        value_ref = ref(value, discard)
        registry[identity] = _IssuedQualityRecord(value_ref, digest)

    def build(
        ledger: EvaluationUniverseLedger,
    ) -> PaperExecutionQualityReport:
        report = unissued_builder(ledger)
        for sample in report.samples:
            register(issued_samples, sample, sample.sample_sha256)
        register(issued_reports, report, report.report_sha256)
        return report

    def validate_sample(
        value: object,
    ) -> PaperExecutionQualitySample:
        if type(value) is not sample_cls:
            raise ExecutionQualityEvidenceError(
                "quality sample must use exact canonical type"
            )
        value._validate()
        record = issued_samples.get(id(value))
        if (
            record is None
            or record.value_ref() is not value
            or record.digest != value.sample_sha256
        ):
            raise ExecutionQualityEvidenceError(
                "quality sample must be product-issued from canonical evidence"
            )
        return value

    def validate_report(
        value: object,
    ) -> PaperExecutionQualityReport:
        if type(value) is not report_cls:
            raise ExecutionQualityEvidenceError(
                "quality report must use exact canonical type"
            )
        value._validate()
        for sample in value.samples:
            validate_sample(sample)
        record = issued_reports.get(id(value))
        if (
            record is None
            or record.value_ref() is not value
            or record.digest != value.report_sha256
        ):
            raise ExecutionQualityEvidenceError(
                "quality report must be product-issued from canonical evidence"
            )
        return value

    return build, validate_sample, validate_report


(
    build_paper_execution_quality_report,
    validate_paper_execution_quality_sample,
    validate_paper_execution_quality_report,
) = _build_quality_issuance_boundary()


__all__ = [
    "ExecutionQualityEvidenceClass",
    "ExecutionQualityEvidenceError",
    "PaperExecutionQualityReport",
    "PaperExecutionQualitySample",
    "build_paper_execution_quality_report",
    "validate_paper_execution_quality_report",
    "validate_paper_execution_quality_sample",
]
