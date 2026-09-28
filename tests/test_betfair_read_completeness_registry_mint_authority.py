from __future__ import annotations

import autosport.betfair_read_completeness as completeness
from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_read_completeness import (
    BetfairObservationCompleteness,
    BetfairPagedReadResult,
    BetfairReadCompletenessObserver,
    BetfairReadCompletenessWitness,
)


def _caller_constructed_complete_witness() -> BetfairReadCompletenessWitness:
    return BetfairReadCompletenessWitness(
        operation="listCurrentOrders",
        completeness=BetfairObservationCompleteness.COMPLETE_FOR_DECLARED_QUERY_WINDOW,
        venue_id="betfair",
        account_id="acct-1",
        adapter_id="betfair-exchange-jsonrpc-readonly",
        adapter_version="1",
        query_sha256="a" * 64,
        attempt_id="b" * 64,
        started_at="2026-09-28T20:00:00+00:00",
        finished_at="2026-09-28T20:00:01+00:00",
        pages=((0, 1000, False, "c" * 64),),
        rows_observed=0,
    )


def test_module_registry_mutation_cannot_mint_complete_witness_authority(
    monkeypatch,
) -> None:
    witness = _caller_constructed_complete_witness()
    assert witness.authoritative is False

    # This is ordinary Python module-state mutation, not private-slot/interpreter
    # reflection. Positive provider-origin authority must not be caller-mintable by
    # writing the same fingerprint format used by the observer issuance path.
    monkeypatch.setattr(
        completeness,
        "_ISSUED_WITNESSES",
        {id(witness): (witness._fingerprint(), True)},
        raising=False,
    )

    assert witness.authoritative is False


def test_module_registry_mutation_cannot_mint_authoritative_empty_result(
    monkeypatch,
) -> None:
    witness = _caller_constructed_complete_witness()
    result = BetfairPagedReadResult((), witness)
    assert result.authoritative_empty is False

    monkeypatch.setattr(
        completeness,
        "_ISSUED_WITNESSES",
        {id(witness): (witness._fingerprint(), True)},
        raising=False,
    )
    monkeypatch.setattr(
        completeness,
        "_ISSUED_RESULTS",
        {id(result): result._fingerprint()},
        raising=False,
    )

    assert result.authoritative_empty is False



def test_importable_issue_registrar_cannot_mint_complete_witness_authority(
    monkeypatch,
) -> None:
    witness = _caller_constructed_complete_witness()
    assert witness.authoritative is False
    assert not hasattr(completeness, "_issue")

    # Re-creating the old registrar name in ordinary module state must not regain
    # the closure-private issuance capability.
    monkeypatch.setattr(
        completeness,
        "_issue",
        lambda value, *, authoritative_origin: value,
        raising=False,
    )
    completeness._issue(witness, authoritative_origin=True)

    assert witness.authoritative is False


def test_direct_observer_witness_helper_cannot_mint_complete_without_provider_read() -> None:
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-key", "session-token"),
        account_id="acct-1",
    )
    observer = BetfairReadCompletenessObserver(client)

    witness = observer._witness(
        "listCurrentOrders",
        "a" * 64,
        "b" * 64,
        "2026-09-28T20:00:00+00:00",
        ((0, 1000, False, "c" * 64),),
        0,
        BetfairObservationCompleteness.COMPLETE_FOR_DECLARED_QUERY_WINDOW,
        None,
    )

    assert witness.authoritative is False


def test_importable_result_registrar_name_cannot_mint_authoritative_empty(
    monkeypatch,
) -> None:
    witness = _caller_constructed_complete_witness()
    result = BetfairPagedReadResult((), witness)
    assert result.authoritative_empty is False
    assert not hasattr(completeness, "_issue_result")

    monkeypatch.setattr(
        completeness,
        "_issue_result",
        lambda value: value,
        raising=False,
    )
    completeness._issue_result(result)

    assert result.authoritative_empty is False


def test_direct_paged_result_helper_cannot_mint_authoritative_empty_without_provider_read() -> None:
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-key", "session-token"),
        account_id="acct-1",
    )
    observer = BetfairReadCompletenessObserver(client)

    result = observer._paged_result(
        "listCurrentOrders",
        "a" * 64,
        "b" * 64,
        "2026-09-28T20:00:00+00:00",
        [(0, 1000, False, "c" * 64)],
        [],
        BetfairObservationCompleteness.COMPLETE_FOR_DECLARED_QUERY_WINDOW,
        None,
    )

    assert result.authoritative_empty is False
