from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, DecimalException


@dataclass(frozen=True, slots=True)
class CandidateLeg:
    quote_key: str
    event_id: str
    decimal_odds: Decimal
    probability: Decimal
    market_id: str | None = None
    selection_id: str | None = None

    @property
    def paper_value_per_unit(self) -> Decimal:
        return self.probability * self.decimal_odds - Decimal("1")

    def ticket_identity(self) -> tuple[str, str, str]:
        """Return a lossless structured ticket identity or fail closed.

        ``quote_key`` is a serialization, not a reversible container when identity
        components themselves may contain ``|``.  New callers can therefore carry
        market/selection identity explicitly.  Legacy delimiter-free suffixes remain
        supported only when they are unambiguous after the already-structured event
        prefix.
        """

        if (
            not isinstance(self.event_id, str)
            or not self.event_id
            or self.event_id.strip() != self.event_id
        ):
            raise ValueError("candidate leg event_id must be a non-empty canonical string")
        if (self.market_id is None) != (self.selection_id is None):
            raise ValueError(
                "candidate leg market_id and selection_id must be provided together"
            )
        if self.market_id is not None and self.selection_id is not None:
            for field_name, value in (
                ("market_id", self.market_id),
                ("selection_id", self.selection_id),
            ):
                if not isinstance(value, str) or not value or value.strip() != value:
                    raise ValueError(
                        f"candidate leg {field_name} must be a non-empty canonical string"
                    )
            expected = f"{self.event_id}|{self.market_id}|{self.selection_id}"
            if self.quote_key != expected:
                raise ValueError(
                    "candidate structured event/market/selection identity does not match quote_key"
                )
            return self.event_id, self.market_id, self.selection_id

        prefix = f"{self.event_id}|"
        if not self.quote_key.startswith(prefix):
            raise ValueError(
                "candidate quote_key is not canonical event|market|selection for structured event_id"
            )
        remainder = self.quote_key[len(prefix) :]
        if remainder.count("|") != 1:
            raise ValueError(
                "candidate leg requires structured market_id and selection_id when quote_key suffix is ambiguous"
            )
        market_id, selection_id = remainder.split("|", 1)
        if not market_id or not selection_id:
            raise ValueError("candidate quote_key is not canonical event|market|selection")
        return self.event_id, market_id, selection_id


@dataclass(frozen=True, slots=True)
class ParlayCandidate:
    legs: tuple[CandidateLeg, ...]
    combined_odds: Decimal
    independent_probability: Decimal
    expected_profit_per_unit: Decimal


def _require_positive_integer(value: int, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field} must be a positive non-boolean integer")
    return value


def _quote_key_is_consistent_with_event(quote_key: str, event_id: str) -> bool:
    """Validate the known event prefix without assuming delimiter-free identity components.

    MarketEvent.quote_key serializes ``event_id|market_id|selection_id`` but the canonical
    identity contract does not forbid ``|`` inside any component.  CandidateLeg already
    carries the structured event_id, so treat that field as authoritative and only require
    a serialization that can contain at least one non-empty market/selection split after
    the exact event prefix.
    """

    prefix = f"{event_id}|"
    if not quote_key.startswith(prefix):
        return False
    remainder = quote_key[len(prefix) :]
    return any(
        remainder[:index] and remainder[index + 1 :]
        for index, char in enumerate(remainder)
        if char == "|"
    )


class BeamParlayCandidateSearch:
    """Bounded research search that avoids brute-force enumeration of every possible parlay."""

    def __init__(self, beam_width: int = 250, max_legs: int = 12, result_limit: int = 100) -> None:
        self.beam_width = _require_positive_integer(beam_width, field="beam_width")
        self.max_legs = _require_positive_integer(max_legs, field="max_legs")
        self.result_limit = _require_positive_integer(result_limit, field="result_limit")

    def search(self, legs: list[CandidateLeg], minimum_legs: int = 2) -> list[ParlayCandidate]:
        minimum_legs = _require_positive_integer(minimum_legs, field="minimum_legs")
        if minimum_legs > self.max_legs:
            raise ValueError("minimum_legs must not exceed max_legs")
        self._validate_input_legs(legs)
        ordered = sorted(
            legs,
            key=lambda leg: (
                -leg.paper_value_per_unit,
                -leg.probability,
                leg.quote_key,
            ),
        )
        beam: list[tuple[CandidateLeg, ...]] = [tuple()]
        results: list[ParlayCandidate] = []
        for _depth in range(1, self.max_legs + 1):
            expanded: dict[tuple[str, ...], tuple[CandidateLeg, ...]] = {}
            for candidate in beam:
                used_events = {leg.event_id for leg in candidate}
                for leg in ordered:
                    if leg.event_id in used_events:
                        continue
                    new_candidate = tuple(
                        sorted(candidate + (leg,), key=lambda item: item.quote_key)
                    )
                    identity = tuple(item.quote_key for item in new_candidate)
                    expanded.setdefault(identity, new_candidate)
            ranked = sorted(expanded.values(), key=self._rank_key)
            beam = ranked[: self.beam_width]
            if _depth >= minimum_legs:
                results.extend(self._to_candidate(candidate) for candidate in beam)
            if not beam:
                break
        results.sort(key=self._result_rank_key)
        unique: dict[tuple[str, ...], ParlayCandidate] = {}
        for candidate in results:
            key = tuple(leg.quote_key for leg in candidate.legs)
            unique.setdefault(key, candidate)
            if len(unique) >= self.result_limit:
                break
        return list(unique.values())

    @staticmethod
    def _validate_input_legs(legs: list[CandidateLeg]) -> None:
        seen_quote_keys: set[str] = set()
        for index, leg in enumerate(legs):
            if not isinstance(leg, CandidateLeg):
                raise ValueError(f"candidate leg {index} must be a CandidateLeg")
            if (
                not isinstance(leg.quote_key, str)
                or not leg.quote_key
                or leg.quote_key != leg.quote_key.strip()
            ):
                raise ValueError(
                    f"candidate leg {index} quote_key must be a non-empty canonical string"
                )
            if (
                not isinstance(leg.event_id, str)
                or not leg.event_id
                or leg.event_id != leg.event_id.strip()
            ):
                raise ValueError(
                    f"candidate leg {index} event_id must be a non-empty canonical string"
                )
            if not _quote_key_is_consistent_with_event(leg.quote_key, leg.event_id):
                raise ValueError(
                    f"candidate leg {index} quote_key is not consistent with structured event_id"
                )
            if leg.market_id is not None or leg.selection_id is not None:
                try:
                    leg.ticket_identity()
                except ValueError as exc:
                    raise ValueError(f"candidate leg {index}: {exc}") from exc
            if not isinstance(leg.decimal_odds, Decimal):
                raise ValueError(f"candidate leg {index} decimal_odds must be Decimal")
            if not leg.decimal_odds.is_finite():
                raise ValueError(f"candidate leg {index} decimal_odds must be finite")
            if leg.decimal_odds <= 1:
                raise ValueError(f"candidate leg {index} decimal_odds must be greater than 1")
            if not isinstance(leg.probability, Decimal):
                raise ValueError(f"candidate leg {index} probability must be Decimal")
            if not leg.probability.is_finite():
                raise ValueError(f"candidate leg {index} probability must be finite")
            if leg.probability < 0 or leg.probability > 1:
                raise ValueError(f"candidate leg {index} probability must be between 0 and 1")
            if leg.quote_key in seen_quote_keys:
                raise ValueError(f"duplicate candidate quote_key: {leg.quote_key}")
            seen_quote_keys.add(leg.quote_key)

    @staticmethod
    def _to_candidate(legs: tuple[CandidateLeg, ...]) -> ParlayCandidate:
        try:
            odds = Decimal("1")
            probability = Decimal("1")
            for leg in legs:
                odds *= leg.decimal_odds
                probability *= leg.probability
            expected_profit = probability * odds - Decimal("1")
        except DecimalException as exc:
            raise ValueError("candidate combined economics exceed Decimal range") from exc
        if not odds.is_finite() or not probability.is_finite() or not expected_profit.is_finite():
            raise ValueError("candidate combined economics exceed Decimal range")
        return ParlayCandidate(legs, odds, probability, expected_profit)

    def _rank_key(
        self,
        legs: tuple[CandidateLeg, ...],
    ) -> tuple[Decimal, Decimal, int, tuple[str, ...]]:
        candidate = self._to_candidate(legs)
        return (
            -candidate.expected_profit_per_unit,
            -candidate.independent_probability,
            len(legs),
            tuple(leg.quote_key for leg in legs),
        )

    @staticmethod
    def _result_rank_key(
        candidate: ParlayCandidate,
    ) -> tuple[Decimal, Decimal, int, tuple[str, ...]]:
        return (
            -candidate.expected_profit_per_unit,
            -candidate.independent_probability,
            len(candidate.legs),
            tuple(leg.quote_key for leg in candidate.legs),
        )
