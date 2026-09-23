from __future__ import annotations

from autosport.bookmaker_capability import (
    BookmakerAccountSnapshot,
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.provider_readback_health import (
    ProviderReadbackHealth,
    ProviderReadbackHealthError,
    ProviderReadbackHealthState,
    _issue_complete_provider_read_scope_evidence,
    classify_provider_readback_health,
)

_OBSERVED_AT = "2026-09-23T10:00:00+00:00"
_EVALUATED_AT = "2026-09-23T10:00:05+00:00"
_REQUEST_SHA = "b" * 64
_RESPONSE_SHA = "c" * 64
_PROFILE_SHA = "a" * 64


def _open_positions_snapshot(*, account_id: str = "acct-1") -> BookmakerAccountSnapshot:
    capability = BookmakerCapability.OPEN_POSITIONS_READ
    profile = BookmakerCapabilityProfile(
        venue_id="betfair",
        account_id=account_id,
        adapter_id="betfair-exchange-jsonrpc-readonly",
        adapter_version="1",
        profile_version=1,
        facts=(
            BookmakerCapabilityFact(
                capability=capability,
                state=BookmakerCapabilityState.SUPPORTED,
            ),
        ),
        observed_at=_OBSERVED_AT,
        source_ref="provider-capability-probe",
        source_payload_sha256=_PROFILE_SHA,
    )
    return BookmakerAccountSnapshot(
        profile=profile,
        observed_capabilities=frozenset({capability}),
        observed_at=_OBSERVED_AT,
        balance=None,
        open_positions=(),
    )


def _assert_not_authoritative(health: ProviderReadbackHealth) -> None:
    try:
        authorized = health.can_authorize_current_state
    except ProviderReadbackHealthError:
        return
    assert authorized is False


def test_same_object_state_mutation_cannot_promote_public_partial_health() -> None:
    """A public non-issued health object must not gain authority by enum mutation."""

    health = ProviderReadbackHealth(
        venue_id="betfair",
        account_id="acct-1",
        adapter_id="betfair-exchange-jsonrpc-readonly",
        state=ProviderReadbackHealthState.FRESH_PARTIAL,
        evaluated_at=_EVALUATED_AT,
        freshness_limit_seconds=30,
        required_capabilities=(BookmakerCapability.OPEN_POSITIONS_READ,),
        successful_scopes=(),
        current_snapshot_sha256=None,
        last_known_snapshot_sha256=None,
    )
    assert health.can_authorize_current_state is False

    object.__setattr__(
        health,
        "state",
        ProviderReadbackHealthState.FRESH_COMPLETE,
    )

    _assert_not_authoritative(health)
    assert health.operator_status_uk == "стан даних букмекера невідомий"


def test_same_object_identity_mutation_revokes_product_issued_complete_health() -> None:
    """Positive issuance must remain bound to the exact account payload after issue."""

    snapshot = _open_positions_snapshot()
    scope = _issue_complete_provider_read_scope_evidence(
        capability=BookmakerCapability.OPEN_POSITIONS_READ,
        request_scope_sha256=_REQUEST_SHA,
        response_sha256=_RESPONSE_SHA,
        observed_at=_OBSERVED_AT,
        snapshot=snapshot,
    )
    health = classify_provider_readback_health(
        required_capabilities=(BookmakerCapability.OPEN_POSITIONS_READ,),
        evaluated_at=_EVALUATED_AT,
        freshness_limit_seconds=30,
        snapshot=snapshot,
        successful_scopes=(scope,),
    )
    assert health.state is ProviderReadbackHealthState.FRESH_COMPLETE
    assert health.can_authorize_current_state is True
    original_result_id = health.result_id

    object.__setattr__(health, "account_id", "acct-substituted")

    assert health.result_id != original_result_id
    _assert_not_authoritative(health)
    assert health.operator_status_uk == "стан даних букмекера невідомий"


def test_same_object_scope_mutation_revokes_complete_health_and_reclassification() -> None:
    """Issued scope authority must remain bound to its exact issuance payload."""

    snapshot = _open_positions_snapshot()
    scope = _issue_complete_provider_read_scope_evidence(
        capability=BookmakerCapability.OPEN_POSITIONS_READ,
        request_scope_sha256=_REQUEST_SHA,
        response_sha256=_RESPONSE_SHA,
        observed_at=_OBSERVED_AT,
        snapshot=snapshot,
    )
    health = classify_provider_readback_health(
        required_capabilities=(BookmakerCapability.OPEN_POSITIONS_READ,),
        evaluated_at=_EVALUATED_AT,
        freshness_limit_seconds=30,
        snapshot=snapshot,
        successful_scopes=(scope,),
    )
    assert health.can_authorize_current_state is True
    assert health.scope_is_authoritative(BookmakerCapability.OPEN_POSITIONS_READ)
    original_evidence_id = scope.evidence_id

    object.__setattr__(scope, "response_sha256", "d" * 64)

    assert scope.evidence_id != original_evidence_id
    _assert_not_authoritative(health)
    assert not health.scope_is_authoritative(BookmakerCapability.OPEN_POSITIONS_READ)
    assert health.operator_status_uk == "стан даних букмекера невідомий"

    reclassified = classify_provider_readback_health(
        required_capabilities=(BookmakerCapability.OPEN_POSITIONS_READ,),
        evaluated_at=_EVALUATED_AT,
        freshness_limit_seconds=30,
        snapshot=snapshot,
        successful_scopes=(scope,),
    )
    assert reclassified.state is ProviderReadbackHealthState.FRESH_PARTIAL
    assert reclassified.can_authorize_current_state is False
