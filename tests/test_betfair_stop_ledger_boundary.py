from __future__ import annotations

import tempfile

import pytest

import test_betfair_supervised_execution as provider_tests
from autosport.betfair_supervised_execution import (
    BetfairSupervisedExecutionError,
    execute_betfair_supervised_action,
)
from autosport.real_execution_ledger import AttemptState


def test_stop_denial_never_crosses_submitted_uncertainty_boundary(monkeypatch) -> None:
    """Known-zero STOP denial must not be persisted as provider uncertainty."""

    monkeypatch.setattr(
        "autosport.supervised_execution._trusted_now",
        lambda: provider_tests.RESERVED_AT,
    )

    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = provider_tests._prepared(
            tmp
        )
        transport = provider_tests._Transport(
            lambda request: provider_tests._response(request)
        )
        client = provider_tests._enabled_client(
            profile,
            transport,
            store=goal_store,
        )
        attempt_id = "attempt-stop-known-zero"

        # Deliberately do not create execution-stop.jsonl. The canonical STOP
        # authority fails closed before any provider transport can be reached.
        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="STOP authority denied provider write",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id=attempt_id,
                profile=profile,
                client=client,
                clock=lambda: provider_tests.SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.attempt_state(attempt_id) is AttemptState.RESERVED
