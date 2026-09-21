from decimal import Decimal

from autosport.policy_evaluation import evaluate_policy_pair
from test_policy_evaluation_abstention_taxonomy import (
    T2,
    _authority,
    _case,
    _metrics,
    _policy,
)


def test_custom_abstain_action_cannot_reclassify_material_bet():
    """Caller configuration must not launder canonical BET activity as abstention."""

    actions = ("BET", "NO_BET", "WAIT")
    predecessor = _policy("BET", actions)
    challenger = _policy("NO_BET", actions)

    try:
        result = evaluate_policy_pair(
            predecessor,
            challenger,
            (_case(actions),),
            completed_at=T2,
            abstain_action="BET",
            counterfactual_authority=_authority(),
        )
    except ValueError as exc:
        message = str(exc).casefold()
        assert "abstain" in message or "bet" in message
        return

    predecessor_metrics = _metrics(result, challenger=False)
    challenger_metrics = _metrics(result, challenger=True)

    assert result.samples[0]["predecessor_action"] == "BET"
    assert predecessor_metrics["action_rate"] == Decimal("1")
    assert predecessor_metrics["abstention_rate"] == Decimal("0")

    assert result.samples[0]["challenger_action"] == "NO_BET"
    assert challenger_metrics["action_rate"] == Decimal("0")
    assert challenger_metrics["abstention_rate"] == Decimal("1")
