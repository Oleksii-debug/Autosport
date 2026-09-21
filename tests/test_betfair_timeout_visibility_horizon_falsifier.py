"""Falsifiers for Betfair TIMEOUT_ERROR visibility-horizon retry safety.

These tests are intentionally expected to fail on the current canonical implementation.
Betfair documents that a timed-out placeOrders/replaceOrders request can take up to
15 seconds to appear. An empty exact customerOrderRef current+cleared capture inside
that window must therefore not become positive NOT_FOUND/retry authority.

The tests reuse the canonical supervised Betfair fixtures without modifying their
owned test file. A safe repair may reject the early reconciliation or retain UNKNOWN;
it must not authorize a second attempt from this early empty observation, including
after process restart.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
import sys
import tempfile


ATTEMPT_ID = "attempt-timeout-visibility-horizon"


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


def _prepare_unknown_attempt(base, tmp: str, monkeypatch):
    monkeypatch.setattr(
        "autosport.supervised_execution._trusted_now",
        lambda: base.RESERVED_AT,
    )
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
        attempt_id=ATTEMPT_ID,
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
        attempt_id=ATTEMPT_ID,
        provider_id="betfair",
    )
    assert provider_ref is not None
    return profile, bound, ledger, action, provider_ref


def _verified_early_absence(base, *, profile, bound, ledger, action, provider_ref):
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
        attempt_id=ATTEMPT_ID,
    )
    return base.verify_betfair_provider_state(
        action,
        profile,
        expected_profile_sha256=profile.profile_id,
        readback=envelope,
        expected_provider_order_ref=provider_ref,
    )


def _attempt_reconcile_not_found(base, *, ledger, bound, verified_absence) -> None:
    # The verifier may describe what the early capture contained, but an
    # observation still inside the provider visibility horizon must not
    # release UNKNOWN to retry authority.
    try:
        base.reconcile_provider_not_found(
            ledger,
            bound,
            attempt_id=ATTEMPT_ID,
            readback=verified_absence,
        )
    except base.SupervisedExecutionError:
        pass


def _assert_retry_stays_blocked(base, *, ledger, bound, action) -> None:
    assert ledger.attempt_state(ATTEMPT_ID) is base.AttemptState.UNKNOWN
    assert not ledger.can_retry_action(
        plan_id=bound.execution_plan.plan_id,
        action_id=action.action_id,
    )


def test_early_empty_readback_after_timeout_cannot_release_retry(monkeypatch) -> None:
    base = _load_canonical_betfair_test_module()
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, ledger, action, provider_ref = _prepare_unknown_attempt(
            base,
            tmp,
            monkeypatch,
        )
        verified_absence = _verified_early_absence(
            base,
            profile=profile,
            bound=bound,
            ledger=ledger,
            action=action,
            provider_ref=provider_ref,
        )
        _attempt_reconcile_not_found(
            base,
            ledger=ledger,
            bound=bound,
            verified_absence=verified_absence,
        )
        _assert_retry_stays_blocked(
            base,
            ledger=ledger,
            bound=bound,
            action=action,
        )


def test_restart_cannot_launder_early_empty_readback_into_retry(monkeypatch) -> None:
    base = _load_canonical_betfair_test_module()
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, ledger, action, provider_ref = _prepare_unknown_attempt(
            base,
            tmp,
            monkeypatch,
        )
        restarted = type(ledger)(Path(tmp) / "real.jsonl")
        _assert_retry_stays_blocked(
            base,
            ledger=restarted,
            bound=bound,
            action=action,
        )
        verified_absence = _verified_early_absence(
            base,
            profile=profile,
            bound=bound,
            ledger=restarted,
            action=action,
            provider_ref=provider_ref,
        )
        _attempt_reconcile_not_found(
            base,
            ledger=restarted,
            bound=bound,
            verified_absence=verified_absence,
        )
        _assert_retry_stays_blocked(
            base,
            ledger=restarted,
            bound=bound,
            action=action,
        )
