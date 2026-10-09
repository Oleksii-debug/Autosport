from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from statistics import NormalDist
from typing import Iterable

from .domain import _quote_identity
from .forecasting import (
    ForecastOutcomeFact,
    ForecastRecord,
    TemporalEvaluationWindow,
    evaluate_forecast_window,
    parse_iso_timestamp,
)

_LOG_LOSS_PROBABILITY_SUPPORT_FLOOR = Decimal("1e-15")
_BRIER_INTERVAL_METHOD = "hoeffding-bounded-brier-v1"
_LOG_LOSS_INTERVAL_METHOD = "hoeffding-predeclared-probability-support-v2"
_CALIBRATION_INTERVAL_METHOD = "bonferroni-wilson-binomial-v1"
_ECE_INTERVAL_METHOD = "simultaneous-bin-envelope-v1"
_DEPENDENCE_SCREEN_METHOD = "canonical-event-snapshot-evidence-uniqueness-v2"


class CalibrationDependenceAssumption(str, Enum):
    """Predeclared sampling assumption for nominal uncertainty intervals."""

    SINGLE_OBSERVATION = "single-observation-v1"
    INDEPENDENT_BERNOULLI = "independent-bernoulli-v1"


def _canonical_quote_event_cluster(quote_key: str) -> tuple[str, str]:
    """Return the canonical sport/event cluster encoded by one quote identity.

    IID calibration diagnostics must not treat multiple selections or markets from
    one sporting event as independent Bernoulli trials. ForecastRecord deliberately
    stores the canonical quote identity rather than a second event-id field, so this
    decoder validates that identity against the product's domain codec before using
    it as a dependence witness.
    """

    if type(quote_key) is not str or not quote_key or quote_key != quote_key.strip():
        raise ValueError(
            "INDEPENDENT_BERNOULLI diagnostics require canonical quote_key identity"
        )

    if "|" in quote_key:
        parts = quote_key.split("|")
        if len(parts) != 3 or any(not part or part != part.strip() for part in parts):
            raise ValueError(
                "INDEPENDENT_BERNOULLI diagnostics require canonical quote_key identity"
            )
        event_id, market_id, selection_id = parts
        if _quote_identity(event_id, market_id, selection_id, None) != quote_key:
            raise ValueError(
                "INDEPENDENT_BERNOULLI diagnostics require canonical quote_key identity"
            )
        return ("", event_id)

    prefix = "sport-v2-"
    if not quote_key.startswith(prefix):
        raise ValueError(
            "INDEPENDENT_BERNOULLI diagnostics require canonical quote_key identity"
        )
    token = quote_key[len(prefix) :]
    if not token:
        raise ValueError(
            "INDEPENDENT_BERNOULLI diagnostics require canonical quote_key identity"
        )
    try:
        padding = "=" * ((-len(token)) % 4)
        decoded = base64.urlsafe_b64decode((token + padding).encode("ascii"))
        payload = json.loads(decoded.decode("utf-8"))
    except (UnicodeError, ValueError, binascii.Error) as exc:
        raise ValueError(
            "INDEPENDENT_BERNOULLI diagnostics require canonical quote_key identity"
        ) from exc
    if type(payload) is not list or len(payload) != 5 or payload[0] != "quote":
        raise ValueError(
            "INDEPENDENT_BERNOULLI diagnostics require canonical quote_key identity"
        )
    _, sport, event_id, market_id, selection_id = payload
    components = (sport, event_id, market_id, selection_id)
    if any(
        type(value) is not str or not value or value != value.strip()
        for value in components
    ):
        raise ValueError(
            "INDEPENDENT_BERNOULLI diagnostics require canonical quote_key identity"
        )
    try:
        canonical = _quote_identity(event_id, market_id, selection_id, sport)
    except ValueError as exc:
        raise ValueError(
            "INDEPENDENT_BERNOULLI diagnostics require canonical quote_key identity"
        ) from exc
    if canonical != quote_key:
        raise ValueError(
            "INDEPENDENT_BERNOULLI diagnostics require canonical quote_key identity"
        )
    return (sport, event_id)


def _assert_iid_dependence_witnesses(
    records: tuple[ForecastRecord, ...],
) -> None:
    """Reject known shared causal/source clusters before nominal IID intervals.

    This is deliberately a fail-closed screen, not an effective-sample-size
    estimator. Raw row count remains the report count. A future cluster-aware
    estimator needs its own preregistered sufficient statistics and method identity.
    """

    quote_keys = tuple(record.quote_key for record in records)
    if len(set(quote_keys)) != len(quote_keys):
        raise ValueError(
            "INDEPENDENT_BERNOULLI diagnostics reject repeated quote_key "
            "dependence; use a separately justified cluster/ESS method"
        )

    event_clusters = tuple(
        _canonical_quote_event_cluster(record.quote_key) for record in records
    )
    if len(set(event_clusters)) != len(event_clusters):
        raise ValueError(
            "INDEPENDENT_BERNOULLI diagnostics reject repeated canonical event "
            "cluster dependence; use a separately justified cluster/ESS method"
        )

    snapshots = tuple(record.market_snapshot_hash for record in records)
    if any(snapshot is None for snapshot in snapshots):
        raise ValueError(
            "INDEPENDENT_BERNOULLI diagnostics require market_snapshot_hash "
            "dependence evidence for every row"
        )
    if len(set(snapshots)) != len(snapshots):
        raise ValueError(
            "INDEPENDENT_BERNOULLI diagnostics reject repeated market_snapshot_hash "
            "dependence; use a separately justified cluster/ESS method"
        )

    evidence_owner: dict[str, str] = {}
    for record in records:
        for evidence_hash in record.evidence_hashes:
            prior = evidence_owner.get(evidence_hash)
            if prior is not None:
                raise ValueError(
                    "INDEPENDENT_BERNOULLI diagnostics reject shared source evidence "
                    "dependence; use a separately justified cluster/ESS method"
                )
            evidence_owner[evidence_hash] = record.forecast_id


@dataclass(frozen=True, slots=True)
class CalibrationPopulationEntry:
    """One exact forecast in the declared calibration eligibility population."""

    forecast_id: str
    forecast_sha256: str
    selected: bool

    def __post_init__(self) -> None:
        if type(self.forecast_id) is not str or not self.forecast_id.strip():
            raise ValueError("population forecast_id must be a non-empty string")
        if (
            type(self.forecast_sha256) is not str
            or len(self.forecast_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.forecast_sha256)
        ):
            raise ValueError("population forecast_sha256 must be a canonical SHA-256 digest")
        if type(self.selected) is not bool:
            raise ValueError("population selected flag must be a bool")

    def to_payload(self) -> dict[str, object]:
        return {
            "forecast_id": self.forecast_id,
            "forecast_sha256": self.forecast_sha256,
            "selected": self.selected,
        }


@dataclass(frozen=True, slots=True)
class CalibrationPopulationManifest:
    """Exact declared denominator/selection identity for calibration diagnostics.

    This binds what population was declared and which forecasts were selected.
    It does not by itself prove durable pre-outcome issuance; callers that need
    promotion authority must compose it with the canonical scientific
    precommit/holdout authority. Calibration diagnostics remain non-promotional.
    """

    window_id: str
    source_snapshot_sha256: str
    selection_policy_sha256: str
    entries: tuple[CalibrationPopulationEntry, ...]

    def __post_init__(self) -> None:
        if type(self.window_id) is not str or not self.window_id.strip():
            raise ValueError("population window_id required")
        for field_name, digest in (
            ("source_snapshot_sha256", self.source_snapshot_sha256),
            ("selection_policy_sha256", self.selection_policy_sha256),
        ):
            if (
                type(digest) is not str
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError(f"{field_name} must be a canonical SHA-256 digest")
        if type(self.entries) is not tuple:
            raise ValueError("population entries must be a tuple")
        if any(type(entry) is not CalibrationPopulationEntry for entry in self.entries):
            raise ValueError("population entries must be exact CalibrationPopulationEntry values")
        ids = tuple(entry.forecast_id for entry in self.entries)
        if len(set(ids)) != len(ids):
            raise ValueError("population manifest contains duplicate forecast_id values")

    @property
    def manifest_sha256(self) -> str:
        return _digest(self.to_payload(include_identity=False))

    def to_payload(self, *, include_identity: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": 1,
            "kind": "autosport-calibration-population-manifest",
            "window_id": self.window_id,
            "source_snapshot_sha256": self.source_snapshot_sha256,
            "selection_policy_sha256": self.selection_policy_sha256,
            "entries": [
                entry.to_payload()
                for entry in sorted(self.entries, key=lambda value: value.forecast_id)
            ],
        }
        if include_identity:
            payload["manifest_sha256"] = self.manifest_sha256
        return payload


@dataclass(frozen=True, slots=True)
class CalibrationCoverage:
    """Denominator/selection/resolution accounting independent of score availability."""

    population_manifest_sha256: str
    eligible_count: int
    selected_count: int
    forecasted_count: int
    resolved_count: int
    pending_outcome_count: int
    missing_forecast_count: int
    missing_selected_count: int
    selection_coverage: float
    forecast_coverage: float
    resolution_coverage: float

    def __post_init__(self) -> None:
        if (
            type(self.population_manifest_sha256) is not str
            or len(self.population_manifest_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.population_manifest_sha256
            )
        ):
            raise ValueError("population_manifest_sha256 must be a canonical SHA-256 digest")
        integer_fields = (
            self.eligible_count,
            self.selected_count,
            self.forecasted_count,
            self.resolved_count,
            self.pending_outcome_count,
            self.missing_forecast_count,
            self.missing_selected_count,
        )
        if any(type(value) is not int or value < 0 for value in integer_fields):
            raise ValueError("coverage counts must be non-negative integers")
        if self.selected_count > self.eligible_count:
            raise ValueError("selected_count cannot exceed eligible_count")
        if self.forecasted_count + self.missing_forecast_count != self.eligible_count:
            raise ValueError("forecasted + missing must equal eligible population")
        if self.missing_selected_count > self.missing_forecast_count:
            raise ValueError("missing_selected_count cannot exceed missing_forecast_count")
        if (
            self.resolved_count
            + self.pending_outcome_count
            + self.missing_selected_count
            != self.selected_count
        ):
            raise ValueError("selected population must resolve to resolved/pending/missing states")
        ratios = (
            self.selection_coverage,
            self.forecast_coverage,
            self.resolution_coverage,
        )
        if any(type(value) is not float or not math.isfinite(value) for value in ratios):
            raise ValueError("coverage ratios must be finite floats")
        if any(not 0.0 <= value <= 1.0 for value in ratios):
            raise ValueError("coverage ratios must be inside 0..1")
        expected_selection = (
            self.selected_count / self.eligible_count if self.eligible_count else 0.0
        )
        expected_forecast = (
            self.forecasted_count / self.eligible_count if self.eligible_count else 0.0
        )
        expected_resolution = (
            self.resolved_count / self.selected_count if self.selected_count else 0.0
        )
        if self.selection_coverage != expected_selection:
            raise ValueError("selection_coverage must equal selected / eligible")
        if self.forecast_coverage != expected_forecast:
            raise ValueError("forecast_coverage must equal forecasted / eligible")
        if self.resolution_coverage != expected_resolution:
            raise ValueError("resolution_coverage must equal resolved / selected")

    def to_payload(self) -> dict[str, object]:
        return {
            "population_manifest_sha256": self.population_manifest_sha256,
            "eligible_count": self.eligible_count,
            "selected_count": self.selected_count,
            "forecasted_count": self.forecasted_count,
            "resolved_count": self.resolved_count,
            "pending_outcome_count": self.pending_outcome_count,
            "missing_forecast_count": self.missing_forecast_count,
            "missing_selected_count": self.missing_selected_count,
            "selection_coverage": _canonical_float(self.selection_coverage),
            "forecast_coverage": _canonical_float(self.forecast_coverage),
            "resolution_coverage": _canonical_float(self.resolution_coverage),
        }


def _canonical_float(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("diagnostic values must be finite")
    if value == 0:
        value = 0.0
    return format(value, ".17g")


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class MetricUncertainty:
    point: float
    lower: float
    upper: float
    method: str

    def __post_init__(self) -> None:
        for value in (self.point, self.lower, self.upper):
            if type(value) is not float or not math.isfinite(value):
                raise ValueError("metric uncertainty values must be finite floats")
        if not self.method:
            raise ValueError("metric uncertainty method required")
        if self.lower > self.point or self.point > self.upper:
            raise ValueError("metric point must lie inside uncertainty interval")

    def to_payload(self) -> dict[str, str]:
        return {
            "point": _canonical_float(self.point),
            "lower": _canonical_float(self.lower),
            "upper": _canonical_float(self.upper),
            "method": self.method,
        }


@dataclass(frozen=True, slots=True)
class CalibrationBinUncertainty:
    bin_lower: float
    bin_upper: float
    count: int
    mean_probability: float
    observed_rate: float
    observed_rate_lower: float
    observed_rate_upper: float
    absolute_gap: float
    absolute_gap_lower: float
    absolute_gap_upper: float

    def __post_init__(self) -> None:
        values = (
            self.bin_lower,
            self.bin_upper,
            self.mean_probability,
            self.observed_rate,
            self.observed_rate_lower,
            self.observed_rate_upper,
            self.absolute_gap,
            self.absolute_gap_lower,
            self.absolute_gap_upper,
        )
        if any(type(value) is not float or not math.isfinite(value) for value in values):
            raise ValueError("calibration-bin values must be finite floats")
        if type(self.count) is not int or self.count <= 0:
            raise ValueError("calibration-bin count must be a positive integer")
        if not 0.0 <= self.bin_lower < self.bin_upper <= 1.0:
            raise ValueError("calibration-bin bounds must be inside 0..1")
        for value in (
            self.mean_probability,
            self.observed_rate,
            self.observed_rate_lower,
            self.observed_rate_upper,
            self.absolute_gap,
            self.absolute_gap_lower,
            self.absolute_gap_upper,
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError("calibration-bin probabilities/gaps must be inside 0..1")
        if not self.observed_rate_lower <= self.observed_rate <= self.observed_rate_upper:
            raise ValueError("observed rate must lie inside Wilson interval")
        if not self.absolute_gap_lower <= self.absolute_gap <= self.absolute_gap_upper:
            raise ValueError("absolute calibration gap must lie inside uncertainty envelope")

    def to_payload(self) -> dict[str, object]:
        return {
            "bin_lower": _canonical_float(self.bin_lower),
            "bin_upper": _canonical_float(self.bin_upper),
            "count": self.count,
            "mean_probability": _canonical_float(self.mean_probability),
            "observed_rate": _canonical_float(self.observed_rate),
            "observed_rate_lower": _canonical_float(self.observed_rate_lower),
            "observed_rate_upper": _canonical_float(self.observed_rate_upper),
            "absolute_gap": _canonical_float(self.absolute_gap),
            "absolute_gap_lower": _canonical_float(self.absolute_gap_lower),
            "absolute_gap_upper": _canonical_float(self.absolute_gap_upper),
        }


@dataclass(frozen=True, slots=True)
class CalibrationDiagnostics:
    window_id: str
    split: str
    count: int
    bins: int
    confidence_level: float
    cohort_sha256: str
    config_sha256: str
    dependence_assumption: CalibrationDependenceAssumption
    raw_sample_count: int
    effective_sample_count: int
    population_manifest_sha256: str
    coverage: CalibrationCoverage
    brier_score: MetricUncertainty
    log_loss: MetricUncertainty
    expected_calibration_error: MetricUncertainty
    calibration: tuple[CalibrationBinUncertainty, ...]
    model_versions: tuple[str, ...]
    strategy_versions: tuple[str, ...]
    promotion_authorized: bool = False
    real_money_execution: bool = False

    def __post_init__(self) -> None:
        if not self.window_id or not self.split:
            raise ValueError("window identity required")
        if type(self.count) is not int or self.count <= 0:
            raise ValueError("diagnostic count must be a positive integer")
        if type(self.bins) is not int or self.bins <= 0:
            raise ValueError("bins must be a positive integer")
        if type(self.confidence_level) is not float or not 0.0 < self.confidence_level < 1.0:
            raise ValueError("confidence_level must be a float strictly between 0 and 1")
        if type(self.dependence_assumption) is not CalibrationDependenceAssumption:
            raise ValueError("dependence_assumption must be a typed calibration assumption")
        if type(self.raw_sample_count) is not int or self.raw_sample_count != self.count:
            raise ValueError("raw_sample_count must equal the evaluated cohort count")
        if (
            type(self.effective_sample_count) is not int
            or self.effective_sample_count <= 0
            or self.effective_sample_count > self.raw_sample_count
        ):
            raise ValueError(
                "effective_sample_count must be positive and cannot exceed raw count"
            )
        if (
            self.dependence_assumption
            is CalibrationDependenceAssumption.SINGLE_OBSERVATION
            and (self.raw_sample_count != 1 or self.effective_sample_count != 1)
        ):
            raise ValueError("single-observation assumption requires exactly one row")
        for field_name, digest in (
            ("cohort_sha256", self.cohort_sha256),
            ("config_sha256", self.config_sha256),
            ("population_manifest_sha256", self.population_manifest_sha256),
        ):
            if (
                type(digest) is not str
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError(f"{field_name} must be a canonical SHA-256 digest")
        if type(self.coverage) is not CalibrationCoverage:
            raise ValueError("coverage must be exact CalibrationCoverage")
        if self.coverage.population_manifest_sha256 != self.population_manifest_sha256:
            raise ValueError("coverage population manifest identity mismatch")
        if (
            self.coverage.resolved_count != self.count
            or self.coverage.selected_count != self.count
            or self.coverage.pending_outcome_count != 0
            or self.coverage.missing_forecast_count != 0
        ):
            raise ValueError(
                "scored diagnostics require a complete selected population with no missing forecasts"
            )
        if type(self.calibration) is not tuple or not self.calibration:
            raise ValueError("at least one non-empty calibration bin required")
        if sum(item.count for item in self.calibration) != self.count:
            raise ValueError("calibration bins must cover the complete evaluated cohort")
        if type(self.model_versions) is not tuple or type(self.strategy_versions) is not tuple:
            raise ValueError("version identities must be tuples")
        if self.promotion_authorized is not False or self.real_money_execution is not False:
            raise ValueError("calibration diagnostics cannot authorize promotion or real-money execution")

    @property
    def report_sha256(self) -> str:
        return _digest(self.to_payload(include_identity=False))

    def to_payload(self, *, include_identity: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": 2,
            "kind": "autosport-calibration-diagnostics",
            "window_id": self.window_id,
            "split": self.split,
            "count": self.count,
            "bins": self.bins,
            "confidence_level": _canonical_float(self.confidence_level),
            "cohort_sha256": self.cohort_sha256,
            "config_sha256": self.config_sha256,
            "dependence_assumption": self.dependence_assumption.value,
            "dependence_screen_method": _DEPENDENCE_SCREEN_METHOD,
            "raw_sample_count": self.raw_sample_count,
            "effective_sample_count": self.effective_sample_count,
            "population_manifest_sha256": self.population_manifest_sha256,
            "coverage": self.coverage.to_payload(),
            "brier_score": self.brier_score.to_payload(),
            "log_loss": self.log_loss.to_payload(),
            "expected_calibration_error": self.expected_calibration_error.to_payload(),
            "calibration_interval_method": _CALIBRATION_INTERVAL_METHOD,
            "calibration": [item.to_payload() for item in self.calibration],
            "model_versions": list(self.model_versions),
            "strategy_versions": list(self.strategy_versions),
            "promotion_authorized": False,
            "real_money_execution": False,
        }
        if include_identity:
            payload["report_sha256"] = self.report_sha256
        return payload


def _bounded_mean_interval(
    point: float,
    *,
    count: int,
    lower_bound: float,
    upper_bound: float,
    confidence_level: float,
    method: str,
) -> MetricUncertainty:
    alpha = 1.0 - confidence_level
    half_width = (upper_bound - lower_bound) * math.sqrt(
        math.log(2.0 / alpha) / (2.0 * count)
    )
    return MetricUncertainty(
        point=float(point),
        lower=float(max(lower_bound, point - half_width)),
        upper=float(min(upper_bound, point + half_width)),
        method=method,
    )


def _wilson_interval(
    successes: int,
    count: int,
    *,
    z_value: float,
) -> tuple[float, float]:
    proportion = successes / count
    z_squared = z_value * z_value
    denominator = 1.0 + z_squared / count
    center = (proportion + z_squared / (2.0 * count)) / denominator
    half = (
        z_value
        * math.sqrt(
            proportion * (1.0 - proportion) / count
            + z_squared / (4.0 * count * count)
        )
        / denominator
    )
    lower = max(0.0, center - half)
    upper = min(1.0, center + half)
    if successes == 0:
        lower = 0.0
    if successes == count:
        upper = 1.0
    return lower, upper


def _distance_to_interval(point: float, lower: float, upper: float) -> float:
    if lower <= point <= upper:
        return 0.0
    return min(abs(point - lower), abs(point - upper))


def _resolve_population_coverage(
    record_values: tuple[ForecastRecord, ...],
    outcome_values: tuple[ForecastOutcomeFact, ...],
    window: TemporalEvaluationWindow,
    population_manifest: CalibrationPopulationManifest,
) -> tuple[
    CalibrationCoverage,
    tuple[ForecastRecord, ...],
    dict[str, ForecastOutcomeFact],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    if type(population_manifest) is not CalibrationPopulationManifest:
        raise ValueError(
            "population_manifest must be an exact CalibrationPopulationManifest"
        )
    if population_manifest.window_id != window.window_id:
        raise ValueError("population manifest window_id mismatch")

    in_window = tuple(
        sorted(
            (record for record in record_values if window.contains(record.generated_at)),
            key=lambda record: record.forecast_id,
        )
    )
    records_by_id: dict[str, ForecastRecord] = {}
    for record in in_window:
        if record.forecast_id in records_by_id:
            raise ValueError(f"duplicate forecast_id: {record.forecast_id}")
        records_by_id[record.forecast_id] = record

    entries_by_id = {
        entry.forecast_id: entry
        for entry in population_manifest.entries
    }
    unexpected_records = tuple(
        sorted(set(records_by_id).difference(entries_by_id))
    )
    if unexpected_records:
        raise ValueError(
            "forecast records outside declared population manifest: "
            + ", ".join(unexpected_records)
        )

    missing_forecasts = tuple(
        sorted(set(entries_by_id).difference(records_by_id))
    )
    for forecast_id in sorted(set(entries_by_id).intersection(records_by_id)):
        if (
            records_by_id[forecast_id].canonical_hash
            != entries_by_id[forecast_id].forecast_sha256
        ):
            raise ValueError(
                f"population manifest forecast hash mismatch: {forecast_id}"
            )

    outcome_by_id: dict[str, ForecastOutcomeFact] = {}
    for fact in outcome_values:
        if fact.forecast_id in outcome_by_id:
            raise ValueError(f"duplicate outcome fact for forecast: {fact.forecast_id}")
        if fact.forecast_id not in entries_by_id:
            raise ValueError(
                f"outcome fact outside declared population manifest: {fact.forecast_id}"
            )
        outcome_by_id[fact.forecast_id] = fact

    selected_ids = tuple(
        sorted(
            entry.forecast_id
            for entry in population_manifest.entries
            if entry.selected
        )
    )
    selected = tuple(
        records_by_id[forecast_id]
        for forecast_id in selected_ids
        if forecast_id in records_by_id
    )
    missing_selected = tuple(
        forecast_id for forecast_id in selected_ids if forecast_id not in records_by_id
    )

    evaluation_end = parse_iso_timestamp(window.evaluation_end_ts)
    pending_outcomes: list[str] = []
    late_outcomes: list[str] = []
    resolved_count = 0
    for record in selected:
        fact = outcome_by_id.get(record.forecast_id)
        if fact is None:
            pending_outcomes.append(record.forecast_id)
            continue
        revealed_at = parse_iso_timestamp(fact.revealed_at)
        if revealed_at > evaluation_end:
            late_outcomes.append(record.forecast_id)
            continue
        if revealed_at <= parse_iso_timestamp(record.generated_at):
            raise ValueError(
                f"outcome reveal must be after forecast generation: {record.forecast_id}"
            )
        resolved_count += 1

    eligible_count = len(population_manifest.entries)
    selected_count = len(selected_ids)
    forecasted_count = eligible_count - len(missing_forecasts)
    pending_count = len(pending_outcomes) + len(late_outcomes)
    selection_coverage = (
        selected_count / eligible_count if eligible_count else 0.0
    )
    forecast_coverage = (
        forecasted_count / eligible_count if eligible_count else 0.0
    )
    resolution_coverage = (
        resolved_count / selected_count if selected_count else 0.0
    )
    coverage = CalibrationCoverage(
        population_manifest_sha256=population_manifest.manifest_sha256,
        eligible_count=eligible_count,
        selected_count=selected_count,
        forecasted_count=forecasted_count,
        resolved_count=resolved_count,
        pending_outcome_count=pending_count,
        missing_forecast_count=len(missing_forecasts),
        missing_selected_count=len(missing_selected),
        selection_coverage=float(selection_coverage),
        forecast_coverage=float(forecast_coverage),
        resolution_coverage=float(resolution_coverage),
    )
    return (
        coverage,
        selected,
        outcome_by_id,
        missing_forecasts,
        tuple(sorted(pending_outcomes)),
        tuple(sorted(late_outcomes)),
    )


def evaluate_calibration_coverage(
    records: Iterable[ForecastRecord],
    outcomes: Iterable[ForecastOutcomeFact],
    window: TemporalEvaluationWindow,
    *,
    population_manifest: CalibrationPopulationManifest,
) -> CalibrationCoverage:
    """Resolve denominator/selection/outcome coverage without fabricating scores."""

    if type(window) is not TemporalEvaluationWindow:
        raise ValueError("window must be an exact TemporalEvaluationWindow")
    record_values = tuple(records)
    outcome_values = tuple(outcomes)
    if any(type(record) is not ForecastRecord for record in record_values):
        raise ValueError("records must contain exact ForecastRecord values")
    if any(type(fact) is not ForecastOutcomeFact for fact in outcome_values):
        raise ValueError("outcomes must contain exact ForecastOutcomeFact values")
    coverage, *_ = _resolve_population_coverage(
        record_values,
        outcome_values,
        window,
        population_manifest,
    )
    return coverage


def evaluate_calibration_diagnostics(
    records: Iterable[ForecastRecord],
    outcomes: Iterable[ForecastOutcomeFact],
    window: TemporalEvaluationWindow,
    *,
    bins: int = 10,
    confidence_level: float = 0.95,
    dependence_assumption: CalibrationDependenceAssumption | None = None,
    population_manifest: CalibrationPopulationManifest,
) -> CalibrationDiagnostics:
    """Build bounded calibration uncertainty evidence for one complete temporal cohort.

    Point Brier/log-loss values come from the canonical forecast evaluator.
    Their bands use bounded-loss Hoeffding diagnostics. Log-loss bands are published
    only for the product-predeclared symmetric forecast-probability support; rows
    outside that support are rejected rather than clipped or outcome-conditioned.
    Per-bin observed-rate intervals use a Bonferroni-adjusted Wilson construction so
    all non-empty bin intervals are treated as one simultaneous diagnostic family.
    The ECE envelope is then derived from those bin intervals. These are diagnostics,
    not promotion or execution authority.
    """

    if type(window) is not TemporalEvaluationWindow:
        raise ValueError("window must be an exact TemporalEvaluationWindow")
    if type(bins) is not int or bins <= 0:
        raise ValueError("bins must be a positive integer")
    if (
        type(confidence_level) is not float
        or not math.isfinite(confidence_level)
        or not 0.0 < confidence_level < 1.0
    ):
        raise ValueError("confidence_level must be a finite float strictly between 0 and 1")

    record_values = tuple(records)
    outcome_values = tuple(outcomes)
    if any(type(record) is not ForecastRecord for record in record_values):
        raise ValueError("records must contain exact ForecastRecord values")
    if any(type(fact) is not ForecastOutcomeFact for fact in outcome_values):
        raise ValueError("outcomes must contain exact ForecastOutcomeFact values")

    (
        coverage,
        selected,
        outcome_by_id,
        missing_forecasts,
        pending_outcomes,
        late_outcomes,
    ) = _resolve_population_coverage(
        record_values,
        outcome_values,
        window,
        population_manifest,
    )
    if missing_forecasts:
        raise ValueError(
            "population manifest requires forecast records missing from evaluation input: "
            + ", ".join(missing_forecasts)
        )
    if not selected:
        raise ValueError(
            "population manifest selected no forecasts; coverage is valid but scores are unavailable"
        )
    if pending_outcomes:
        raise ValueError(
            "complete calibration cohort required; missing outcome facts for: "
            + ", ".join(pending_outcomes)
        )
    if late_outcomes:
        raise ValueError(
            "calibration outcome facts revealed after evaluation cutoff: "
            + ", ".join(late_outcomes)
        )

    support_floor = _LOG_LOSS_PROBABILITY_SUPPORT_FLOOR
    support_ceiling = Decimal(1) - support_floor
    unsupported = tuple(
        record.forecast_id
        for record in selected
        if record.probability < support_floor or record.probability > support_ceiling
    )
    if unsupported:
        raise ValueError(
            "predeclared log-loss probability support requires every selected "
            f"forecast probability inside [{support_floor}, {support_ceiling}]; "
            "out-of-support forecasts: " + ", ".join(sorted(unsupported))
        )

    if dependence_assumption is not None and (
        type(dependence_assumption) is not CalibrationDependenceAssumption
    ):
        raise ValueError(
            "dependence_assumption must be a typed CalibrationDependenceAssumption"
        )
    if len(selected) == 1:
        resolved_dependence = (
            CalibrationDependenceAssumption.SINGLE_OBSERVATION
            if dependence_assumption is None
            else dependence_assumption
        )
    else:
        if (
            dependence_assumption
            is not CalibrationDependenceAssumption.INDEPENDENT_BERNOULLI
        ):
            raise ValueError(
                "multi-row calibration uncertainty requires an explicit "
                "INDEPENDENT_BERNOULLI dependence assumption"
            )
        resolved_dependence = dependence_assumption

    if (
        resolved_dependence
        is CalibrationDependenceAssumption.INDEPENDENT_BERNOULLI
        and len(selected) > 1
    ):
        _assert_iid_dependence_witnesses(selected)

    selected_outcomes = tuple(
        outcome_by_id[record.forecast_id] for record in selected
    )
    summary = evaluate_forecast_window(
        selected,
        selected_outcomes,
        window,
        bins=bins,
    )

    paired = tuple(
        sorted(
            (
                (record, outcome_by_id[record.forecast_id])
                for record in selected
            ),
            key=lambda pair: pair[0].forecast_id,
        )
    )
    cohort_payload = [
        {
            "forecast_id": record.forecast_id,
            "forecast_sha256": record.canonical_hash,
            "outcome": fact.outcome,
            "revealed_at": fact.revealed_at,
        }
        for record, fact in paired
    ]
    cohort_sha256 = _digest(cohort_payload)
    config_payload = {
        "schema_version": 2,
        "window": {
            "window_id": window.window_id,
            "training_end_ts": window.training_end_ts,
            "evaluation_start_ts": window.evaluation_start_ts,
            "evaluation_end_ts": window.evaluation_end_ts,
            "split": window.split,
        },
        "bins": bins,
        "confidence_level": _canonical_float(confidence_level),
        "population_manifest_sha256": population_manifest.manifest_sha256,
        "selection_policy_sha256": population_manifest.selection_policy_sha256,
        "source_snapshot_sha256": population_manifest.source_snapshot_sha256,
        "dependence_assumption": resolved_dependence.value,
        "dependence_screen_method": _DEPENDENCE_SCREEN_METHOD,
        "brier_interval_method": _BRIER_INTERVAL_METHOD,
        "log_loss_interval_method": _LOG_LOSS_INTERVAL_METHOD,
        "log_loss_probability_support_floor": str(support_floor),
        "log_loss_probability_support_ceiling": str(support_ceiling),
        "calibration_interval_method": _CALIBRATION_INTERVAL_METHOD,
        "ece_interval_method": _ECE_INTERVAL_METHOD,
    }
    config_sha256 = _digest(config_payload)

    alpha = 1.0 - confidence_level
    non_empty_bin_count = len(summary.calibration)
    tail_probability = alpha / (2.0 * non_empty_bin_count)
    quantile_probability = 1.0 - tail_probability
    if not 0.5 < quantile_probability < 1.0:
        raise ValueError("confidence_level is too extreme for stable Wilson quantile")
    z_value = NormalDist().inv_cdf(quantile_probability)

    bucket_outcomes: dict[int, list[int]] = {}
    for record, fact in paired:
        bucket_index = min(bins - 1, int(float(record.probability) * bins))
        bucket_outcomes.setdefault(bucket_index, []).append(fact.outcome)
    non_empty_indexes = tuple(sorted(bucket_outcomes))
    if len(non_empty_indexes) != len(summary.calibration):
        raise ValueError("canonical calibration bin membership mismatch")

    calibration_output: list[CalibrationBinUncertainty] = []
    ece_point = 0.0
    ece_lower = 0.0
    ece_upper = 0.0
    for bucket_index, item in zip(non_empty_indexes, summary.calibration, strict=True):
        expected_lower = bucket_index / bins
        expected_upper = (bucket_index + 1) / bins
        outcomes_in_bin = bucket_outcomes[bucket_index]
        if (
            item.lower != expected_lower
            or item.upper != expected_upper
            or item.count != len(outcomes_in_bin)
        ):
            raise ValueError("canonical calibration bin semantics changed")
        successes = sum(outcomes_in_bin)
        lower_rate, upper_rate = _wilson_interval(successes, item.count, z_value=z_value)
        absolute_gap = abs(item.mean_probability - item.observed_rate)
        absolute_gap_lower = _distance_to_interval(
            item.mean_probability, lower_rate, upper_rate
        )
        absolute_gap_upper = max(
            abs(item.mean_probability - lower_rate),
            abs(item.mean_probability - upper_rate),
        )
        weight = item.count / summary.count
        ece_point += weight * absolute_gap
        ece_lower += weight * absolute_gap_lower
        ece_upper += weight * absolute_gap_upper
        calibration_output.append(
            CalibrationBinUncertainty(
                bin_lower=float(item.lower),
                bin_upper=float(item.upper),
                count=item.count,
                mean_probability=float(item.mean_probability),
                observed_rate=float(item.observed_rate),
                observed_rate_lower=float(lower_rate),
                observed_rate_upper=float(upper_rate),
                absolute_gap=float(absolute_gap),
                absolute_gap_lower=float(absolute_gap_lower),
                absolute_gap_upper=float(absolute_gap_upper),
            )
        )

    brier = _bounded_mean_interval(
        summary.brier_score,
        count=summary.count,
        lower_bound=0.0,
        upper_bound=1.0,
        confidence_level=confidence_level,
        method=_BRIER_INTERVAL_METHOD,
    )
    max_log_loss = max(
        -math.log(float(support_floor)),
        -math.log1p(-float(support_ceiling)),
    )
    log_loss = _bounded_mean_interval(
        summary.log_loss,
        count=summary.count,
        lower_bound=0.0,
        upper_bound=max_log_loss,
        confidence_level=confidence_level,
        method=_LOG_LOSS_INTERVAL_METHOD,
    )
    ece = MetricUncertainty(
        point=float(ece_point),
        lower=float(max(0.0, ece_lower)),
        upper=float(min(1.0, ece_upper)),
        method=_ECE_INTERVAL_METHOD,
    )

    return CalibrationDiagnostics(
        window_id=summary.window_id,
        split=summary.split,
        count=summary.count,
        bins=bins,
        confidence_level=confidence_level,
        cohort_sha256=cohort_sha256,
        config_sha256=config_sha256,
        dependence_assumption=resolved_dependence,
        raw_sample_count=summary.count,
        effective_sample_count=summary.count,
        population_manifest_sha256=population_manifest.manifest_sha256,
        coverage=coverage,
        brier_score=brier,
        log_loss=log_loss,
        expected_calibration_error=ece,
        calibration=tuple(calibration_output),
        model_versions=summary.model_versions,
        strategy_versions=summary.strategy_versions,
    )