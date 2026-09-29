from decimal import Decimal

from autosport.candidate_search import BeamParlayCandidateSearch, CandidateLeg


def _leg(quote_key: str, event_id: str, probability: str) -> CandidateLeg:
    return CandidateLeg(
        quote_key=quote_key,
        event_id=event_id,
        decimal_odds=Decimal("2"),
        probability=Decimal(probability),
    )


def test_narrow_beam_keeps_pair_reachable_when_best_single_has_late_key() -> None:
    search = BeamParlayCandidateSearch(beam_width=1, max_legs=2, result_limit=5)
    stronger_late = _leg("z|winner|x", "z", "0.90")
    weaker_early = _leg("a|winner|x", "a", "0.50")

    forward = search.search([stronger_late, weaker_early], minimum_legs=2)
    reverse = search.search([weaker_early, stronger_late], minimum_legs=2)

    assert forward == reverse
    assert len(forward) == 1
    assert tuple(leg.quote_key for leg in forward[0].legs) == (
        "a|winner|x",
        "z|winner|x",
    )
    assert forward[0].expected_profit_per_unit == Decimal("0.80")
