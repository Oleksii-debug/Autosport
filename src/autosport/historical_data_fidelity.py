from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum, IntEnum
from typing import FrozenSet


class HistoricalDataFidelity(IntEnum):
    """Native public market-data fidelity ordered by information resolution."""

    BETFAIR_BASIC_1M_LTP = 10
    BETFAIR_ADVANCED_1S_BEST_OFFERS = 20
    BETFAIR_PRO_TICK_FULL_LADDER = 30


class MarketDataCapability(str, Enum):
    MARKET_DEFINITION = "market_definition"
    LAST_TRADED_PRICE = "last_traded_price"
    TRADED_PRICES = "traded_prices"
    ONE_MINUTE_RESOLUTION = "one_minute_resolution"
    ONE_SECOND_RESOLUTION = "one_second_resolution"
    BEST_OFFERS = "best_offers"
    MARKET_TRADED_VOLUME = "market_traded_volume"
    RUNNER_TRADED_VOLUME = "runner_traded_volume"
    TICK_BY_TICK = "tick_by_tick"
    FULL_AVAILABLE_LADDER = "full_available_ladder"
    SP_FIELDS = "sp_fields"


class ExecutionEvidenceCapability(str, Enum):
    ORDER_RECEIPT = "order_receipt"
    ACCEPTED_PRICE = "accepted_price"
    PROVIDER_ACK = "provider_ack"
    TRANSPORT_TIMING = "transport_timing"
    REALIZED_FILL = "realized_fill"
    QUEUE_POSITION = "queue_position"


class FidelityTransformKind(str, Enum):
    DOWNSAMPLE = "downsample"
    INTERPOLATE = "interpolate"


class FidelityUseCase(str, Enum):
    MARKET_PERFORMANCE = "market_performance"
    SUB_MINUTE_MOVEMENT = "sub_minute_movement"
    SUB_SECOND_ORDERING = "sub_second_ordering"
    FULL_DEPTH_LIQUIDITY = "full_depth_liquidity"
    EXECUTION_FILL_QUALITY = "execution_fill_quality"
    PROVIDER_ACK_REJECTION = "provider_ack_rejection"
    TRANSPORT_LATENCY = "transport_latency"
    QUEUE_POSITION = "queue_position"


_CAPABILITIES_BY_FIDELITY: dict[HistoricalDataFidelity, FrozenSet[MarketDataCapability]] = {
    HistoricalDataFidelity.BETFAIR_BASIC_1M_LTP: frozenset(
        {
            MarketDataCapability.MARKET_DEFINITION,
            MarketDataCapability.LAST_TRADED_PRICE,
            MarketDataCapability.TRADED_PRICES,
            MarketDataCapability.ONE_MINUTE_RESOLUTION,
        }
    ),
    HistoricalDataFidelity.BETFAIR_ADVANCED_1S_BEST_OFFERS: frozenset(
        {
            MarketDataCapability.MARKET_DEFINITION,
            MarketDataCapability.LAST_TRADED_PRICE,
            MarketDataCapability.TRADED_PRICES,
            MarketDataCapability.ONE_MINUTE_RESOLUTION,
            MarketDataCapability.ONE_SECOND_RESOLUTION,
            MarketDataCapability.BEST_OFFERS,
            MarketDataCapability.MARKET_TRADED_VOLUME,
            MarketDataCapability.RUNNER_TRADED_VOLUME,
        }
    ),
    HistoricalDataFidelity.BETFAIR_PRO_TICK_FULL_LADDER: frozenset(
        {
            MarketDataCapability.MARKET_DEFINITION,
            MarketDataCapability.LAST_TRADED_PRICE,
            MarketDataCapability.TRADED_PRICES,
            MarketDataCapability.ONE_MINUTE_RESOLUTION,
            MarketDataCapability.ONE_SECOND_RESOLUTION,
            MarketDataCapability.BEST_OFFERS,
            MarketDataCapability.MARKET_TRADED_VOLUME,
            MarketDataCapability.RUNNER_TRADED_VOLUME,
            MarketDataCapability.TICK_BY_TICK,
            MarketDataCapability.FULL_AVAILABLE_LADDER,
            MarketDataCapability.SP_FIELDS,
        }
    ),
}


_MARKET_REQUIREMENTS: dict[FidelityUseCase, FrozenSet[MarketDataCapability]] = {
    FidelityUseCase.MARKET_PERFORMANCE: frozenset({MarketDataCapability.LAST_TRADED_PRICE}),
    FidelityUseCase.SUB_MINUTE_MOVEMENT: frozenset({MarketDataCapability.ONE_SECOND_RESOLUTION}),
    FidelityUseCase.SUB_SECOND_ORDERING: frozenset({MarketDataCapability.TICK_BY_TICK}),
    FidelityUseCase.FULL_DEPTH_LIQUIDITY: frozenset({MarketDataCapability.FULL_AVAILABLE_LADDER}),
    FidelityUseCase.EXECUTION_FILL_QUALITY: frozenset(),
    FidelityUseCase.PROVIDER_ACK_REJECTION: frozenset(),
    FidelityUseCase.TRANSPORT_LATENCY: frozenset(),
    FidelityUseCase.QUEUE_POSITION: frozenset(),
}


_EXECUTION_REQUIREMENTS: dict[FidelityUseCase, FrozenSet[ExecutionEvidenceCapability]] = {
    FidelityUseCase.MARKET_PERFORMANCE: frozenset(),
    FidelityUseCase.SUB_MINUTE_MOVEMENT: frozenset(),
    FidelityUseCase.SUB_SECOND_ORDERING: frozenset(),
    FidelityUseCase.FULL_DEPTH_LIQUIDITY: frozenset(),
    FidelityUseCase.EXECUTION_FILL_QUALITY: frozenset(
        {
            ExecutionEvidenceCapability.ORDER_RECEIPT,
            ExecutionEvidenceCapability.ACCEPTED_PRICE,
            ExecutionEvidenceCapability.REALIZED_FILL,
        }
    ),
    FidelityUseCase.PROVIDER_ACK_REJECTION: frozenset(
        {
            ExecutionEvidenceCapability.ORDER_RECEIPT,
            ExecutionEvidenceCapability.PROVIDER_ACK,
        }
    ),
    FidelityUseCase.TRANSPORT_LATENCY: frozenset(
        {
            ExecutionEvidenceCapability.ORDER_RECEIPT,
            ExecutionEvidenceCapability.TRANSPORT_TIMING,
        }
    ),
    FidelityUseCase.QUEUE_POSITION: frozenset({ExecutionEvidenceCapability.QUEUE_POSITION}),
}


def _nonempty_text(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty canonical string")
    return value


def _aware_timestamp(value: str, *, field: str) -> str:
    canonical = _nonempty_text(value, field=field)
    try:
        parsed = datetime.fromisoformat(canonical.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include an explicit timezone")
    return canonical


def capabilities_for_fidelity(
    fidelity: HistoricalDataFidelity,
) -> FrozenSet[MarketDataCapability]:
    if type(fidelity) is not HistoricalDataFidelity:
        raise TypeError("fidelity must be HistoricalDataFidelity")
    return _CAPABILITIES_BY_FIDELITY[fidelity]


@dataclass(frozen=True, slots=True)
class FidelityTransform:
    kind: FidelityTransformKind
    source_fidelity: HistoricalDataFidelity
    output_fidelity: HistoricalDataFidelity
    transform_id: str
    feature_set_id: str
    input_cutoff_ts: str

    def __post_init__(self) -> None:
        if type(self.kind) is not FidelityTransformKind:
            raise TypeError("kind must be FidelityTransformKind")
        if type(self.source_fidelity) is not HistoricalDataFidelity:
            raise TypeError("source_fidelity must be HistoricalDataFidelity")
        if type(self.output_fidelity) is not HistoricalDataFidelity:
            raise TypeError("output_fidelity must be HistoricalDataFidelity")
        _nonempty_text(self.transform_id, field="transform_id")
        _nonempty_text(self.feature_set_id, field="feature_set_id")
        _aware_timestamp(self.input_cutoff_ts, field="input_cutoff_ts")
        if self.source_fidelity == self.output_fidelity:
            raise ValueError("fidelity transform must change fidelity")
        if self.kind is FidelityTransformKind.DOWNSAMPLE:
            if self.output_fidelity >= self.source_fidelity:
                raise ValueError("downsample must lower declared fidelity")
        elif self.kind is FidelityTransformKind.INTERPOLATE:
            if self.output_fidelity <= self.source_fidelity:
                raise ValueError("interpolate must raise declared sampling fidelity")


@dataclass(frozen=True, slots=True)
class DatasetFidelityEvidence:
    provider: str
    dataset_id: str
    native_fidelity: HistoricalDataFidelity
    analysis_fidelity: HistoricalDataFidelity
    transform: FidelityTransform | None = None
    execution_capabilities: FrozenSet[ExecutionEvidenceCapability] = frozenset()

    def __post_init__(self) -> None:
        _nonempty_text(self.provider, field="provider")
        _nonempty_text(self.dataset_id, field="dataset_id")
        if type(self.native_fidelity) is not HistoricalDataFidelity:
            raise TypeError("native_fidelity must be HistoricalDataFidelity")
        if type(self.analysis_fidelity) is not HistoricalDataFidelity:
            raise TypeError("analysis_fidelity must be HistoricalDataFidelity")
        if not isinstance(self.execution_capabilities, frozenset):
            raise TypeError("execution_capabilities must be frozenset")
        if any(type(item) is not ExecutionEvidenceCapability for item in self.execution_capabilities):
            raise TypeError("execution_capabilities must contain ExecutionEvidenceCapability values")

        if self.analysis_fidelity == self.native_fidelity:
            if self.transform is not None:
                raise ValueError("transform must be absent when analysis fidelity equals native fidelity")
            return

        if self.transform is None:
            raise ValueError("changed analysis fidelity requires explicit transform provenance")
        if self.transform.source_fidelity is not self.native_fidelity:
            raise ValueError("transform source_fidelity must match native_fidelity")
        if self.transform.output_fidelity is not self.analysis_fidelity:
            raise ValueError("transform output_fidelity must match analysis_fidelity")

    @property
    def truth_fidelity(self) -> HistoricalDataFidelity:
        """Maximum fidelity the evidence may truthfully claim after transformation.

        Downsampling deliberately lowers the analysis information set. Interpolation may
        make a denser representation, but it cannot create provider observations that were
        not present in the native package, so truth remains capped at native fidelity.
        """

        if self.transform is None:
            return self.native_fidelity
        if self.transform.kind is FidelityTransformKind.DOWNSAMPLE:
            return self.analysis_fidelity
        return self.native_fidelity

    @property
    def market_capabilities(self) -> FrozenSet[MarketDataCapability]:
        return capabilities_for_fidelity(self.truth_fidelity)

    @property
    def interpolated(self) -> bool:
        return self.transform is not None and self.transform.kind is FidelityTransformKind.INTERPOLATE


@dataclass(frozen=True, slots=True)
class FidelityQualification:
    qualified: bool
    use_case: FidelityUseCase
    truth_fidelity: HistoricalDataFidelity
    missing_market_capabilities: tuple[MarketDataCapability, ...]
    missing_execution_capabilities: tuple[ExecutionEvidenceCapability, ...]
    interpolated: bool
    reasons: tuple[str, ...]


def qualify_use_case(
    evidence: DatasetFidelityEvidence,
    use_case: FidelityUseCase,
) -> FidelityQualification:
    if not isinstance(evidence, DatasetFidelityEvidence):
        raise TypeError("evidence must be DatasetFidelityEvidence")
    if type(use_case) is not FidelityUseCase:
        raise TypeError("use_case must be FidelityUseCase")

    missing_market = tuple(
        sorted(
            _MARKET_REQUIREMENTS[use_case] - evidence.market_capabilities,
            key=lambda value: value.value,
        )
    )
    missing_execution = tuple(
        sorted(
            _EXECUTION_REQUIREMENTS[use_case] - evidence.execution_capabilities,
            key=lambda value: value.value,
        )
    )
    reasons: list[str] = []
    if missing_market:
        reasons.append(
            "missing market-data capabilities: "
            + ",".join(value.value for value in missing_market)
        )
    if missing_execution:
        reasons.append(
            "missing execution evidence: "
            + ",".join(value.value for value in missing_execution)
        )
    if evidence.interpolated and missing_market:
        reasons.append("interpolation cannot upgrade provider-observed data fidelity")

    return FidelityQualification(
        qualified=not missing_market and not missing_execution,
        use_case=use_case,
        truth_fidelity=evidence.truth_fidelity,
        missing_market_capabilities=missing_market,
        missing_execution_capabilities=missing_execution,
        interpolated=evidence.interpolated,
        reasons=tuple(reasons),
    )


@dataclass(frozen=True, slots=True)
class CommonInformationProjection:
    projection_id: str
    feature_set_id: str
    target_fidelity: HistoricalDataFidelity
    source_dataset_ids: tuple[str, ...]
    input_cutoff_ts: str

    def __post_init__(self) -> None:
        _nonempty_text(self.projection_id, field="projection_id")
        _nonempty_text(self.feature_set_id, field="feature_set_id")
        if type(self.target_fidelity) is not HistoricalDataFidelity:
            raise TypeError("target_fidelity must be HistoricalDataFidelity")
        if not isinstance(self.source_dataset_ids, tuple) or len(self.source_dataset_ids) < 2:
            raise ValueError("source_dataset_ids must contain at least two dataset identities")
        normalized = tuple(sorted(self.source_dataset_ids))
        if normalized != self.source_dataset_ids:
            raise ValueError("source_dataset_ids must be sorted canonically")
        if len(set(normalized)) != len(normalized):
            raise ValueError("source_dataset_ids must be unique")
        for item in normalized:
            _nonempty_text(item, field="source_dataset_ids")
        _aware_timestamp(self.input_cutoff_ts, field="input_cutoff_ts")


@dataclass(frozen=True, slots=True)
class FidelityComparisonQualification:
    comparable: bool
    comparison_fidelity: HistoricalDataFidelity | None
    projection_id: str | None
    reasons: tuple[str, ...]


def qualify_comparison(
    left: DatasetFidelityEvidence,
    right: DatasetFidelityEvidence,
    *,
    projection: CommonInformationProjection | None = None,
) -> FidelityComparisonQualification:
    if not isinstance(left, DatasetFidelityEvidence) or not isinstance(right, DatasetFidelityEvidence):
        raise TypeError("left and right must be DatasetFidelityEvidence")

    if left.truth_fidelity is right.truth_fidelity:
        if projection is not None:
            _validate_projection(left, right, projection)
            if projection.target_fidelity > left.truth_fidelity:
                return FidelityComparisonQualification(
                    comparable=False,
                    comparison_fidelity=None,
                    projection_id=projection.projection_id,
                    reasons=("common-information projection exceeds available truth fidelity",),
                )
            return FidelityComparisonQualification(
                comparable=True,
                comparison_fidelity=projection.target_fidelity,
                projection_id=projection.projection_id,
                reasons=(),
            )
        return FidelityComparisonQualification(
            comparable=True,
            comparison_fidelity=left.truth_fidelity,
            projection_id=None,
            reasons=(),
        )

    if projection is None:
        return FidelityComparisonQualification(
            comparable=False,
            comparison_fidelity=None,
            projection_id=None,
            reasons=(
                "datasets have different truth fidelity; explicit common-information projection is required",
            ),
        )

    _validate_projection(left, right, projection)
    if projection.target_fidelity > min(left.truth_fidelity, right.truth_fidelity):
        return FidelityComparisonQualification(
            comparable=False,
            comparison_fidelity=None,
            projection_id=projection.projection_id,
            reasons=("common-information projection exceeds lower-fidelity dataset truth",),
        )
    return FidelityComparisonQualification(
        comparable=True,
        comparison_fidelity=projection.target_fidelity,
        projection_id=projection.projection_id,
        reasons=(),
    )


def _validate_projection(
    left: DatasetFidelityEvidence,
    right: DatasetFidelityEvidence,
    projection: CommonInformationProjection,
) -> None:
    if not isinstance(projection, CommonInformationProjection):
        raise TypeError("projection must be CommonInformationProjection")
    expected = tuple(sorted((left.dataset_id, right.dataset_id)))
    if projection.source_dataset_ids != expected:
        raise ValueError("projection source_dataset_ids must bind the exact compared datasets")
