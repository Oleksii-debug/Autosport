from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class CandidateLeg:
    quote_key: str
    event_id: str
    decimal_odds: Decimal
    probability: Decimal

    @property
    def paper_value_per_unit(self) -> Decimal:
        return self.probability * self.decimal_odds - Decimal("1")


@dataclass(frozen=True, slots=True)
class ParlayCandidate:
    legs: tuple[CandidateLeg, ...]
    combined_odds: Decimal
    independent_probability: Decimal
    expected_profit_per_unit: Decimal


class BeamParlayCandidateSearch:
    """Bounded research search that avoids brute-force enumeration of every possible parlay."""

    def __init__(self, beam_width: int = 250, max_legs: int = 12, result_limit: int = 100) -> None:
        if beam_width <= 0 or max_legs <= 0 or result_limit <= 0:
            raise ValueError("search limits must be positive")
        self.beam_width = beam_width
        self.max_legs = max_legs
        self.result_limit = result_limit

    def search(self, legs: list[CandidateLeg], minimum_legs: int = 2) -> list[ParlayCandidate]:
        if minimum_legs < 1 or minimum_legs > self.max_legs:
            raise ValueError("invalid minimum_legs")
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
                last_key = candidate[-1].quote_key if candidate else ""
                for leg in ordered:
                    if leg.event_id in used_events or (last_key and leg.quote_key <= last_key):
                        continue
                    new_candidate = candidate + (leg,)
                    identity = tuple(item.quote_key for item in new_candidate)
                    expanded[identity] = new_candidate
            ranked = sorted(expanded.values(), key=self._score, reverse=True)
            beam = ranked[: self.beam_width]
            if _depth >= minimum_legs:
                results.extend(self._to_candidate(candidate) for candidate in beam)
            if not beam:
                break
        results.sort(key=lambda candidate: candidate.expected_profit_per_unit, reverse=True)
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
            if not isinstance(leg.quote_key, str) or not leg.quote_key or leg.quote_key != leg.quote_key.strip():
                raise ValueError(f"candidate leg {index} quote_key must be a non-empty canonical string")
            parts = leg.quote_key.split("|")
            if len(parts) != 3 or any(not part or part != part.strip() for part in parts):
                raise ValueError(
                    f"candidate leg {index} quote_key must use canonical event|market|selection identity"
                )
            if not isinstance(leg.event_id, str) or not leg.event_id or leg.event_id != leg.event_id.strip():
                raise ValueError(f"candidate leg {index} event_id must be a non-empty canonical string")
            if parts[0] != leg.event_id:
                raise ValueError(f"candidate leg {index} event_id does not match quote_key")
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
        odds = Decimal("1")
        probability = Decimal("1")
        for leg in legs:
            odds *= leg.decimal_odds
            probability *= leg.probability
        return ParlayCandidate(legs, odds, probability, probability * odds - Decimal("1"))

    def _score(self, legs: tuple[CandidateLeg, ...]) -> tuple[Decimal, Decimal, int]:
        candidate = self._to_candidate(legs)
        return candidate.expected_profit_per_unit, candidate.independent_probability, -len(legs)
