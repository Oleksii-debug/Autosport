"""Falsifier for Betfair TIMEOUT_ERROR visibility-horizon retry safety.

This test is intentionally expected to fail on the current canonical implementation.
Betfair documents that a timed-out placeOrders/replaceOrders request can take up to
15 seconds to appear. An empty exact customerOrderRef current+cleared capture inside
that window must therefore not become positive NOT_FOUND/retry authority.

The test reuses the canonical supervised Betfair fixtures without modifying their
owned test file. A safe repair may reject the early reconciliation or retain UNKNOWN;
it must not authorize a second attempt from this early empty observation.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
import sys
import tempfile

import pytest


def _load_canonical_betfair_test_module():
    path = Path(__file__).with_name("test_betfair_supervised_execution.py")
    spec = importlib.util.spec_from_file_location(
        "_autosport_canonical_betfair_supervised_test",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load canonical Betfair supervised test helpers")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_early_empty_readback_after_timeout_cannot_release_retry(monkeypatch) -> None:
    base = _load_canonical_betfair_test_module()
    monkeypatch.setattr(
        "autosport.supervised_execution._trusted_now",
        lambda: base.RESERVED_AT,
    )

    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = base._prepared(tmp)
        timeout_client = base._enabled_client(
            profile,
            base._TimeoutTransport(),
            store=goal_store,
        )

        result = base.execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-timeout-visibility-horizon",
            profile=profile,
            client=timeout_client,
            clock=lambda: base.SUBMITTED_AT,
        )

        assert result.attempt_state is base.AttemptState.UNKNOWN
        assert not ledger.can_retry_action(
            plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        )

        provider_ref = ledger.provider_order_reference(
            attempt_id="attempt-timeout-visibility-horizon",
            provider_id="betfair",
        )
        assert provider_ref is not None

        unknown_at = datetime.fromisoformat(base.SUBMITTED_AT)
        early_readback_at = unknown_at + timedelta(seconds=10)
        assert early_readback_at - unknown_at < timedelta(seconds=15)

        read_client = base.BetfairReadOnlyClient(
            base.BetfairSessionCredentials("app-key", "session-token"),
            transport=base._ReadbackTransport(
                provider_order_ref=provider_ref,
                action=action,
            ),
            clock=lambda: early_readback_at,
            venue_id="betfair",
            account_id="acct-1",
        )
        envelope = base.read_betfair_supervised_action_readback(
            read_client,
            ledger,
            bound,
            attempt_id="attempt-timeout-visibility-horizon",
        )
        verified_absence = base.verify_betfair_provider_state(
            action,
            profile,
            expected_profile_sha256=profile.profile_id,
            readback=envelope,
            expected_provider_order_ref=provider_ref,
        )

        # The verifier may describe what the early capture contained, but an
        # observation still inside the provider visibility horizon must not
        # release UNKNOWN to retry authority.
        try:
            base.reconcile_provider_not_found(
                ledger,
                bound,
                attempt_id="attempt-timeout-visibility-horizon",
                readback=verified_absence,
            )
        except base.SupervisedExecutionError:
            pass

        assert (
            ledger.attempt_state("attempt-timeout-visibility-horizon")
            is base.AttemptState.UNKNOWN
        )
        assert not ledger.can_retry_action(
            plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        )
