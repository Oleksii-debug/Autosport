from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from enum import Enum
from fractions import Fraction
from typing import Iterable


_FIXED_MINIMUM_REVIEW_DAYS = Decimal("14")


class AnchorFeasibilityError(ValueError):
    """Raised when feasibility evidence is malformed or causally ambiguous."""


class AcquisitionState(str, Enum):
    OBSERVED = "OBSERVED"
    ZERO_RESULT = "ZERO_RESULT"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    LOCAL_FAILURE = "LOCAL_FAILURE"
    STOPPED = "STOPPED"
    PENDING = "PENDING"


class FeasibilityDisposition(str, Enum):
    INCOMPLETE = "INCOMPLETE"
    COLLECT_MORE = "COLLECT_MORE"
    CONTINUE_EVIDENCE = "CONTINUE_EVIDENCE"


@dataclass(frozen=True, slots=True)
class AnchorScope:
    sport: str
    league: str
    market: str
    provider: str
    currency: str

    def __post_init__(self) -> None:
        for name in ("sport", "league", "market", "provider", "currency"):
            _canonical_text(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class AnchorObservation:
    sequence: int
    acquisition_id: str
    scope: AnchorScope
    state: AcquisitionState
    observed_at: datetime
    source_sha256: str
    applicable_cost: Decimal
    capital_amount: Decimal
    capital_held_hours: Decimal
    event_id: str | None = None
    reaction_slack_seconds: Decimal | None = None
    displayed_liquidity: Decimal | None = None

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise AnchorFeasibilityError("sequence must be a positive int")
        _canonical_text(self.acquisition_id, "acquisition_id")
        if not isinstance(self.scope, AnchorScope):
            raise AnchorFeasibilityError("scope must be AnchorScope")
        if not isinstance(self.state, AcquisitionState):
            raise AnchorFeasibilityError("state must be AcquisitionState")
        _utc_timestamp(self.observed_at, "observed_at")
        _sha256_hex(self.source_sha256, "source_sha256")
        _nonnegative_decimal(self.applicable_cost, "applicable_cost")
        _nonnegative_decimal(self.capital_amount, "capital_amount")
        _nonnegative_decimal(self.capital_held_hours, "capital_held_hours")

        if self.state is AcquisitionState.OBSERVED:
            _canonical_text(self.event_id, "event_id")
            _nonnegative_decimal(
                self.reaction_slack_seconds, "reaction_slack_seconds"
            )
            _nonnegative_decimal(self.displayed_liquidity, "displayed_liquidity")
        else:
            if self.event_id is not None:
                raise AnchorFeasibilityError(
                    "non-OBSERVED evidence cannot carry event_id"
                )
            if self.reaction_slack_seconds is not None:
                raise AnchorFeasibilityError(
                    "non-OBSERVED evidence cannot carry reaction_slack_seconds"
                )
            if self.displayed_liquidity is not None:
                raise AnchorFeasibilityError(
                    "non-OBSERVED evidence cannot carry displayed_liquidity"
                )


@dataclass(frozen=True, slots=True)
class AnchorWindowClosure:
    """Structural close evidence for one exact frozen acquisition window.

    This type does not prove provider/collector origin by itself.  It makes the
    completeness dependency explicit and hash-bound so a later product-owned
    source-universe authority can be mechanically re-resolved rather than
    inferring completeness from an omitted caller list.
    """

    scope: AnchorScope
    window_start: datetime
    window_end: datetime
    first_sequence: int
    last_sequence: int
    closed_at: datetime
    source_universe_sha256: str
    closure_evidence_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, AnchorScope):
            raise AnchorFeasibilityError("closure scope must be AnchorScope")
        start = _utc_timestamp(self.window_start, "closure.window_start")
        end = _utc_timestamp(self.window_end, "closure.window_end")
        closed_at = _utc_timestamp(self.closed_at, "closure.closed_at")
        if end <= start:
            raise AnchorFeasibilityError(
                "closure window_end must be after window_start"
            )
        if closed_at < end:
            raise AnchorFeasibilityError(
                "closure cannot predate the frozen window end"
            )
        if type(self.first_sequence) is not int or self.first_sequence <= 0:
            raise AnchorFeasibilityError(
                "closure first_sequence must be a positive int"
            )
        if type(self.last_sequence) is not int or self.last_sequence <= 0:
            raise AnchorFeasibilityError(
                "closure last_sequence must be a positive int"
            )
        if self.last_sequence < self.first_sequence:
            raise AnchorFeasibilityError(
                "closure last_sequence cannot precede first_sequence"
            )
        _sha256_hex(self.source_universe_sha256, "source_universe_sha256")
        _sha256_hex(self.closure_evidence_sha256, "closure_evidence_sha256")


@dataclass(frozen=True, slots=True)
class AnchorFeasibilityReport:
    scope: AnchorScope
    window_start: datetime
    window_end: datetime
    review_as_of: datetime
    disposition: FeasibilityDisposition
    evidence_sha256: str
    acquisition_count: int
    distinct_event_count: int
    state_counts: tuple[tuple[str, int], ...]
    terminal_coverage_complete: bool
    review_window_days: Decimal
    reaction_slack_p10_seconds: Decimal | None
    reaction_slack_median_seconds: Decimal | None
    displayed_liquidity_min: Decimal | None
    displayed_liquidity_median: Decimal | None
    total_applicable_cost: Decimal
    cost_per_observed_opportunity: Decimal | None
    total_capital_time_currency_hours: Decimal
    ranking_authority: bool = False
    winner_authority: bool = False
    promotion_authority: bool = False
    execution_authority: bool = False
    external_validity_authority: bool = False
    real_money_authority: bool = False

    def __post_init__(self) -> None:
        authority_values = (
            self.ranking_authority,
            self.winner_authority,
            self.promotion_authority,
            self.execution_authority,
            self.external_validity_authority,
            self.real_money_authority,
        )
        if any(value is not False for value in authority_values):
            raise AnchorFeasibilityError(
                "anchor feasibility reports cannot grant downstream authority"
            )


def evaluate_anchor_feasibility(
    *,
    scope: AnchorScope,
    window_start: datetime,
    window_end: datetime,
    review_as_of: datetime,
    observations: Iterable[AnchorObservation],
    minimum_review_days: Decimal = _FIXED_MINIMUM_REVIEW_DAYS,
    window_closure: AnchorWindowClosure | None = None,
) -> AnchorFeasibilityReport:
    """Evaluate one frozen sport/league/market/provider/currency feasibility window.

    This is measurement evidence only. It deliberately cannot rank anchors, select a
    winner, promote a strategy, authorize execution, generalize beyond the frozen
    scope, or establish real-money readiness.
    """

    if not isinstance(scope, AnchorScope):
        raise AnchorFeasibilityError("scope must be AnchorScope")
    start = _utc_timestamp(window_start, "window_start")
    end = _utc_timestamp(window_end, "window_end")
    as_of = _utc_timestamp(review_as_of, "review_as_of")
    if end <= start:
        raise AnchorFeasibilityError("window_end must be after window_start")
    if as_of < start:
        raise AnchorFeasibilityError("review_as_of cannot precede window_start")
    min_days = _nonnegative_decimal(minimum_review_days, "minimum_review_days")
    if min_days != _FIXED_MINIMUM_REVIEW_DAYS:
        raise AnchorFeasibilityError(
            "minimum_review_days is fixed at 14 and cannot be relaxed by callers"
        )

    rows = tuple(observations)
    if not rows:
        raise AnchorFeasibilityError("at least one acquisition observation is required")

    seen_acquisition_ids: set[str] = set()
    previous_time: datetime | None = None
    state_counts = {state: 0 for state in AcquisitionState}
    event_reaction_slack: dict[str, list[Decimal]] = {}
    event_liquidity: dict[str, list[Decimal]] = {}
    total_cost = Fraction(0, 1)
    total_capital_time = Fraction(0, 1)
    canonical_rows: list[dict[str, object]] = []

    for expected_sequence, row in enumerate(rows, start=1):
        if not isinstance(row, AnchorObservation):
            raise AnchorFeasibilityError("all observations must be AnchorObservation")
        if row.sequence != expected_sequence:
            raise AnchorFeasibilityError(
                f"acquisition sequence must be contiguous from 1; expected {expected_sequence}"
            )
        if row.acquisition_id in seen_acquisition_ids:
            raise AnchorFeasibilityError("acquisition_id replay detected")
        seen_acquisition_ids.add(row.acquisition_id)
        if row.scope != scope:
            raise AnchorFeasibilityError("observation scope drift detected")

        observed_at = _utc_timestamp(row.observed_at, "observed_at")
        if not (start <= observed_at <= end):
            raise AnchorFeasibilityError("observation falls outside frozen review window")
        if observed_at > as_of:
            raise AnchorFeasibilityError(
                "observation is not causally available at review_as_of"
            )
        if previous_time is not None and observed_at < previous_time:
            raise AnchorFeasibilityError("observation timestamp rollback detected")
        previous_time = observed_at

        state_counts[row.state] += 1
        total_cost += _fraction(row.applicable_cost)
        total_capital_time += _fraction(row.capital_amount) * _fraction(
            row.capital_held_hours
        )

        if row.state is AcquisitionState.OBSERVED:
            assert row.event_id is not None
            assert row.reaction_slack_seconds is not None
            assert row.displayed_liquidity is not None
            event_reaction_slack.setdefault(row.event_id, []).append(
                row.reaction_slack_seconds
            )
            event_liquidity.setdefault(row.event_id, []).append(
                row.displayed_liquidity
            )

        canonical_rows.append(_canonical_observation(row))

    closure_complete = False
    if window_closure is not None:
        if not isinstance(window_closure, AnchorWindowClosure):
            raise AnchorFeasibilityError(
                "window_closure must be AnchorWindowClosure"
            )
        if window_closure.scope != scope:
            raise AnchorFeasibilityError("window closure scope drift detected")
        closure_start = _utc_timestamp(
            window_closure.window_start, "closure.window_start"
        )
        closure_end = _utc_timestamp(
            window_closure.window_end, "closure.window_end"
        )
        if closure_start != start or closure_end != end:
            raise AnchorFeasibilityError(
                "window closure does not bind the exact frozen review window"
            )
        closed_at = _utc_timestamp(window_closure.closed_at, "closure.closed_at")
        if closed_at > as_of:
            raise AnchorFeasibilityError(
                "window closure is not causally available at review_as_of"
            )
        if (
            window_closure.first_sequence != 1
            or window_closure.last_sequence != rows[-1].sequence
        ):
            raise AnchorFeasibilityError(
                "window closure sequence range does not match supplied observations"
            )
        closure_complete = True

    distinct_event_count = len(event_reaction_slack)
    # Multiple quote/update rows for one event never increase recurrence. For
    # per-event feasibility summaries use the conservative minimum observation.
    event_slack_values = sorted(
        min(values) for values in event_reaction_slack.values()
    )
    event_liquidity_values = sorted(
        min(values) for values in event_liquidity.values()
    )

    p10 = _nearest_rank_percentile(event_slack_values, 10)
    slack_median = _median(event_slack_values)
    liquidity_min = None if not event_liquidity_values else event_liquidity_values[0]
    liquidity_median = _median(event_liquidity_values)

    total_cost_decimal = _decimal_from_terminating_fraction(total_cost)
    capital_time_decimal = _decimal_from_terminating_fraction(total_capital_time)
    if distinct_event_count:
        with localcontext() as ctx:
            ctx.prec = 80
            cost_per_observed = total_cost_decimal / Decimal(distinct_event_count)
    else:
        cost_per_observed = None

    terminal_complete = (
        closure_complete
        and state_counts[AcquisitionState.PENDING] == 0
        and as_of >= end
    )
    window_delta = end - start
    window_microseconds = (
        (window_delta.days * 86_400 + window_delta.seconds) * 1_000_000
        + window_delta.microseconds
    )
    review_days_fraction = Fraction(
        window_microseconds,
        86_400 * 1_000_000,
    )
    review_days = _fraction_to_decimal(review_days_fraction, precision=50)

    if not terminal_complete:
        disposition = FeasibilityDisposition.INCOMPLETE
    elif _fraction(review_days) < _fraction(min_days):
        disposition = FeasibilityDisposition.COLLECT_MORE
    else:
        # Deliberately no PASS/FAIL/WINNER/PROMOTE state exists here. Fourteen
        # complete days are a measurement checkpoint, not evidence of edge.
        disposition = FeasibilityDisposition.CONTINUE_EVIDENCE

    identity_payload = {
        "schema": "autosport.anchor-feasibility.v1",
        "scope": {
            "sport": scope.sport,
            "league": scope.league,
            "market": scope.market,
            "provider": scope.provider,
            "currency": scope.currency,
        },
        "window_start": _iso_utc(start),
        "window_end": _iso_utc(end),
        "review_as_of": _iso_utc(as_of),
        "minimum_review_days": _canonical_decimal(min_days),
        "observations": canonical_rows,
        "window_closure": (
            None
            if window_closure is None
            else _canonical_window_closure(window_closure)
        ),
    }
    evidence_sha256 = hashlib.sha256(
        json.dumps(
            identity_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()

    return AnchorFeasibilityReport(
        scope=scope,
        window_start=start,
        window_end=end,
        review_as_of=as_of,
        disposition=disposition,
        evidence_sha256=evidence_sha256,
        acquisition_count=len(rows),
        distinct_event_count=distinct_event_count,
        state_counts=tuple((state.value, state_counts[state]) for state in AcquisitionState),
        terminal_coverage_complete=terminal_complete,
        review_window_days=review_days,
        reaction_slack_p10_seconds=p10,
        reaction_slack_median_seconds=slack_median,
        displayed_liquidity_min=liquidity_min,
        displayed_liquidity_median=liquidity_median,
        total_applicable_cost=total_cost_decimal,
        cost_per_observed_opportunity=cost_per_observed,
        total_capital_time_currency_hours=capital_time_decimal,
    )


def _canonical_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value.strip() != value or " " in value:
        raise AnchorFeasibilityError(f"{name} must be non-empty canonical text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise AnchorFeasibilityError(f"{name} must be UTF-8 encodable") from exc
    return value


def _sha256_hex(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise AnchorFeasibilityError(f"{name} must be lowercase SHA-256 hex")
    return value


def _utc_timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AnchorFeasibilityError(f"{name} must be timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _nonnegative_decimal(value: object, name: str) -> Decimal:
    # Exact product evidence must never accept bool/int aliases or binary floats.
    if type(value) is not Decimal or not value.is_finite() or value < 0:
        raise AnchorFeasibilityError(f"{name} must be a finite non-negative Decimal")
    return value


def _fraction(value: Decimal) -> Fraction:
    numerator, denominator = value.as_integer_ratio()
    return Fraction(numerator, denominator)


def _decimal_from_terminating_fraction(value: Fraction) -> Decimal:
    numerator = value.numerator
    denominator = value.denominator
    twos = 0
    fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        raise AnchorFeasibilityError("internal exact decimal result is non-terminating")
    scale = max(twos, fives)
    coefficient = numerator * (2 ** (scale - twos)) * (5 ** (scale - fives))
    sign = 1 if coefficient < 0 else 0
    digits_text = str(abs(coefficient))
    digits = tuple(int(ch) for ch in digits_text)
    return Decimal((sign, digits, -scale))


def _fraction_to_decimal(value: Fraction, *, precision: int) -> Decimal:
    try:
        return _decimal_from_terminating_fraction(value)
    except AnchorFeasibilityError:
        with localcontext() as ctx:
            ctx.prec = precision
            return Decimal(value.numerator) / Decimal(value.denominator)


def _median(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    result = (_fraction(values[middle - 1]) + _fraction(values[middle])) / 2
    return _decimal_from_terminating_fraction(result)


def _nearest_rank_percentile(values: list[Decimal], percentile: int) -> Decimal | None:
    if not values:
        return None
    if type(percentile) is not int or not 1 <= percentile <= 100:
        raise AnchorFeasibilityError("percentile must be integer in 1..100")
    # ceil(percentile * n / 100) without float arithmetic.
    rank = (percentile * len(values) + 99) // 100
    return values[rank - 1]


def _canonical_decimal(value: Decimal) -> tuple[int, str, int]:
    value = _nonnegative_decimal(value, "decimal")
    sign, digits, exponent = value.as_tuple()
    digits_list = list(digits)
    while len(digits_list) > 1 and digits_list[-1] == 0:
        digits_list.pop()
        exponent += 1
    if not any(digits_list):
        return (0, "0", 0)
    return (sign, "".join(str(digit) for digit in digits_list), exponent)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _canonical_window_closure(
    closure: AnchorWindowClosure,
) -> dict[str, object]:
    return {
        "scope": {
            "sport": closure.scope.sport,
            "league": closure.scope.league,
            "market": closure.scope.market,
            "provider": closure.scope.provider,
            "currency": closure.scope.currency,
        },
        "window_start": _iso_utc(closure.window_start),
        "window_end": _iso_utc(closure.window_end),
        "first_sequence": closure.first_sequence,
        "last_sequence": closure.last_sequence,
        "closed_at": _iso_utc(closure.closed_at),
        "source_universe_sha256": closure.source_universe_sha256,
        "closure_evidence_sha256": closure.closure_evidence_sha256,
    }


def _canonical_observation(row: AnchorObservation) -> dict[str, object]:
    return {
        "sequence": row.sequence,
        "acquisition_id": row.acquisition_id,
        "state": row.state.value,
        "observed_at": _iso_utc(row.observed_at),
        "source_sha256": row.source_sha256,
        "applicable_cost": _canonical_decimal(row.applicable_cost),
        "capital_amount": _canonical_decimal(row.capital_amount),
        "capital_held_hours": _canonical_decimal(row.capital_held_hours),
        "event_id": row.event_id,
        "reaction_slack_seconds": (
            None
            if row.reaction_slack_seconds is None
            else _canonical_decimal(row.reaction_slack_seconds)
        ),
        "displayed_liquidity": (
            None
            if row.displayed_liquidity is None
            else _canonical_decimal(row.displayed_liquidity)
        ),
    }
