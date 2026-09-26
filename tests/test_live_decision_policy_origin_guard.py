from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from autosport.decision_ledger import EconomicDecisionAuthority, JsonlDecisionLedger
from autosport.economic_goal import EconomicGoalContract
from autosport.live_decision_disposition import bind_product_policy_authority
from autosport.risk import PaperRiskPolicy


def _authority() -> EconomicDecisionAuthority:
    goal = EconomicGoalContract(
        goal_id="goal-live-policy-origin-guard",
        revision=1,
        bankroll_id="bankroll-live-policy-origin-guard",
        currency="EUR",
    )
    return EconomicDecisionAuthority(
        contract=goal,
        risk_policy=PaperRiskPolicy(economic_goal=goal),
    )


class LiveDecisionPolicyOriginGuardTests(unittest.TestCase):
    def test_ledger_subclass_cannot_dispatch_forged_economic_resolution(self) -> None:
        called = False

        class ForgedLedger(JsonlDecisionLedger):
            def verified_economic_decision(self, *args, **kwargs):
                nonlocal called
                called = True
                raise AssertionError("forged resolver must never execute")

        with tempfile.TemporaryDirectory() as tmp:
            ledger = ForgedLedger(Path(tmp) / "decisions.jsonl")
            with self.assertRaisesRegex(TypeError, "canonical JsonlDecisionLedger"):
                bind_product_policy_authority(
                    ledger=ledger,
                    authority=_authority(),
                    decision_id="forged-decision",
                )
        self.assertFalse(called)

    def test_exact_ledger_instance_cannot_shadow_read_authority(self) -> None:
        called = False
        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonlDecisionLedger(Path(tmp) / "decisions.jsonl")

            def forged(*args, **kwargs):
                nonlocal called
                called = True
                raise AssertionError("shadowed resolver must never execute")

            ledger.verified_economic_decision = forged  # type: ignore[method-assign]
            with self.assertRaisesRegex(TypeError, "canonical JsonlDecisionLedger"):
                bind_product_policy_authority(
                    ledger=ledger,
                    authority=_authority(),
                    decision_id="forged-decision",
                )
        self.assertFalse(called)

    def test_authority_subclass_is_rejected_before_policy_dispatch(self) -> None:
        class ForgedAuthority(EconomicDecisionAuthority):
            pass

        canonical = _authority()
        forged = ForgedAuthority(
            contract=canonical.contract,
            risk_policy=canonical.risk_policy,
        )
        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonlDecisionLedger(Path(tmp) / "decisions.jsonl")
            with self.assertRaisesRegex(TypeError, "exact EconomicDecisionAuthority"):
                bind_product_policy_authority(
                    ledger=ledger,
                    authority=forged,
                    decision_id="forged-decision",
                )


if __name__ == "__main__":
    unittest.main()
