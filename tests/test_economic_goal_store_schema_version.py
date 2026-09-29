from __future__ import annotations

import pytest

from autosport.economic_goal import EconomicGoalContract, EconomicGoalContractError
from autosport.economic_goal_store import economic_goal_from_payload, economic_goal_to_payload


def test_schema_version_rejects_float_equal_to_current_integer_version() -> None:
    payload = economic_goal_to_payload(
        EconomicGoalContract(
            goal_id="owner-goal-v1",
            revision=1,
            bankroll_id="paper-main",
            currency="EUR",
        )
    )
    payload["schema_version"] = 1.0

    with pytest.raises(EconomicGoalContractError, match="schema_version"):
        economic_goal_from_payload(payload)
