from dataclasses import replace
from decimal import Decimal
import pickle
import pytest
from autosport.bookmaker_capability import BookmakerAccountSnapshot, BookmakerBalanceObservation, BookmakerCapability, BookmakerCapabilityFact, BookmakerCapabilityProfile, BookmakerCapabilityState, BookmakerPositionObservation, BookmakerPositionState
from autosport.provider_readback_health import ProviderReadbackFailureClass, ProviderReadbackHealth, ProviderReadbackHealthError, ProviderReadbackHealthState, ProviderReadScopeEvidence, _issue_complete_provider_read_scope_evidence, canonical_snapshot_sha256, classify_provider_readback_health
_OBSERVED = '2026-09-21T18:00:00+00:00'
_HASH = 'a' * 64
_SCOPE_HASH = 'b' * 64
_RESPONSE_HASH = 'c' * 64

def _profile(*capabilities: BookmakerCapability, account_id: str='acct-1') -> BookmakerCapabilityProfile:
    return BookmakerCapabilityProfile(venue_id='betfair', account_id=account_id, adapter_id='betfair-exchange-jsonrpc-readonly', adapter_version='1', profile_version=1, facts=tuple((BookmakerCapabilityFact(capability=c, state=BookmakerCapabilityState.SUPPORTED) for c in capabilities)), observed_at=_OBSERVED, source_ref='provider-capability-probe', source_payload_sha256=_HASH)

def _balance() -> BookmakerBalanceObservation:
    return BookmakerBalanceObservation(venue_id='betfair', account_id='acct-1', adapter_id='betfair-exchange-jsonrpc-readonly', observation_id='balance-1', currency='EUR', available_balance=Decimal('90'), observed_at=_OBSERVED, source_payload_sha256=_HASH, total_balance=Decimal('100'), total_balance_source_ref='getAccountFunds', exposure=Decimal('-10'))

def _open_position() -> BookmakerPositionObservation:
    return BookmakerPositionObservation(venue_id='betfair', account_id='acct-1', adapter_id='betfair-exchange-jsonrpc-readonly', observation_id='position-open-1', external_position_id='bet-123', state=BookmakerPositionState.OPEN, currency='EUR', observed_at=_OBSERVED, source_payload_sha256=_HASH, provider_amount=Decimal('10'), provider_amount_semantics='backer_stake', provider_side='LAY', decimal_odds=Decimal('3.0'))

def _snapshot(*capabilities: BookmakerCapability, open_positions=(), account_id: str='acct-1') -> BookmakerAccountSnapshot:
    if account_id != 'acct-1' and (BookmakerCapability.BALANCE_READ in capabilities or open_positions):
        raise ValueError('test helper requires matching nested account evidence')
    return BookmakerAccountSnapshot(profile=_profile(*capabilities, account_id=account_id), observed_capabilities=frozenset(capabilities), observed_at=_OBSERVED, balance=_balance() if BookmakerCapability.BALANCE_READ in capabilities else None, open_positions=open_positions)

def _scope(capability: BookmakerCapability, *, snapshot=None, complete: bool=True, observed_at: str=_OBSERVED) -> ProviderReadScopeEvidence:
    if complete and snapshot is not None:
        return _issue_complete_provider_read_scope_evidence(capability=capability, request_scope_sha256=_SCOPE_HASH, response_sha256=_RESPONSE_HASH, observed_at=observed_at, snapshot=snapshot)
    return ProviderReadScopeEvidence(capability=capability, request_scope_sha256=_SCOPE_HASH, response_sha256=_RESPONSE_HASH, observed_at=observed_at, snapshot_sha256=None if snapshot is None else canonical_snapshot_sha256(snapshot), complete=complete)

def test_fresh_complete_requires_exact_successful_scope_and_can_authorize_empty() -> None:
    snapshot = _snapshot(BookmakerCapability.OPEN_POSITIONS_READ)
    health = classify_provider_readback_health(required_capabilities=(BookmakerCapability.OPEN_POSITIONS_READ,), evaluated_at='2026-09-21T18:00:05+00:00', freshness_limit_seconds=30, snapshot=snapshot, successful_scopes=(_scope(BookmakerCapability.OPEN_POSITIONS_READ, snapshot=snapshot),))
    assert health.state is ProviderReadbackHealthState.FRESH_COMPLETE
    assert health.can_authorize_current_state
    assert health.scope_is_authoritative(BookmakerCapability.OPEN_POSITIONS_READ)
    assert snapshot.open_positions == ()
    assert health.operator_status_uk == 'дані букмекера актуальні'

def test_direct_caller_cannot_mint_fresh_complete_health() -> None:
    snapshot = _snapshot(BookmakerCapability.BALANCE_READ)
    with pytest.raises(ProviderReadbackHealthError, match='product-issued'):
        ProviderReadbackHealth(venue_id='betfair', account_id='acct-1', adapter_id='betfair-exchange-jsonrpc-readonly', state=ProviderReadbackHealthState.FRESH_COMPLETE, evaluated_at='2026-09-21T18:00:05+00:00', freshness_limit_seconds=30, required_capabilities=(BookmakerCapability.BALANCE_READ,), successful_scopes=(_scope(BookmakerCapability.BALANCE_READ, snapshot=snapshot),), current_snapshot_sha256=canonical_snapshot_sha256(snapshot), last_known_snapshot_sha256=None)

def test_timeout_preserves_last_known_nonempty_exposure_and_never_becomes_empty() -> None:
    last_known = _snapshot(BookmakerCapability.OPEN_POSITIONS_READ, open_positions=(_open_position(),))
    health = classify_provider_readback_health(required_capabilities=(BookmakerCapability.OPEN_POSITIONS_READ,), evaluated_at='2026-09-21T18:01:00+00:00', freshness_limit_seconds=30, failure_class=ProviderReadbackFailureClass.TIMEOUT, last_known_snapshot=last_known)
    assert health.state is ProviderReadbackHealthState.UNAVAILABLE_TRANSIENT
    assert not health.can_authorize_current_state
    assert health.current_snapshot_sha256 is None
    assert health.last_known_snapshot_sha256 == canonical_snapshot_sha256(last_known)
    assert last_known.open_positions[0].external_position_id == 'bet-123'

@pytest.mark.parametrize('failure', [ProviderReadbackFailureClass.UNAUTHORIZED, ProviderReadbackFailureClass.SESSION_EXPIRED])
def test_auth_failure_is_explicit_and_never_zeroes_account_truth(failure) -> None:
    last_known = _snapshot(BookmakerCapability.BALANCE_READ)
    health = classify_provider_readback_health(required_capabilities=(BookmakerCapability.BALANCE_READ,), evaluated_at='2026-09-21T18:01:00+00:00', freshness_limit_seconds=30, failure_class=failure, last_known_snapshot=last_known)
    assert health.state is ProviderReadbackHealthState.UNAUTHORIZED_OR_SESSION_EXPIRED
    assert not health.can_authorize_current_state
    assert health.last_known_snapshot_sha256 == canonical_snapshot_sha256(last_known)
    assert last_known.balance is not None and last_known.balance.available_balance == Decimal('90')

def test_mid_pagination_failure_is_partial_not_complete_empty() -> None:
    snapshot = _snapshot(BookmakerCapability.OPEN_POSITIONS_READ)
    health = classify_provider_readback_health(required_capabilities=(BookmakerCapability.OPEN_POSITIONS_READ,), evaluated_at='2026-09-21T18:00:05+00:00', freshness_limit_seconds=30, snapshot=snapshot, successful_scopes=(_scope(BookmakerCapability.OPEN_POSITIONS_READ, snapshot=snapshot, complete=False),), failure_class=ProviderReadbackFailureClass.TIMEOUT)
    assert health.state is ProviderReadbackHealthState.FRESH_PARTIAL
    assert not health.can_authorize_current_state

def test_stale_snapshot_cannot_authorize_current_balance_or_positions() -> None:
    snapshot = _snapshot(BookmakerCapability.BALANCE_READ, BookmakerCapability.OPEN_POSITIONS_READ, open_positions=(_open_position(),))
    health = classify_provider_readback_health(required_capabilities=(BookmakerCapability.BALANCE_READ, BookmakerCapability.OPEN_POSITIONS_READ), evaluated_at='2026-09-21T18:00:31+00:00', freshness_limit_seconds=30, snapshot=snapshot, successful_scopes=(_scope(BookmakerCapability.BALANCE_READ, snapshot=snapshot), _scope(BookmakerCapability.OPEN_POSITIONS_READ, snapshot=snapshot)))
    assert health.state is ProviderReadbackHealthState.STALE_LAST_KNOWN
    assert not health.can_authorize_current_state
    assert health.last_known_snapshot_sha256 == canonical_snapshot_sha256(snapshot)
    assert health.operator_status_uk == 'дані букмекера застарілі'

def test_recovery_is_new_causal_health_result_without_rewriting_outage() -> None:
    snapshot = _snapshot(BookmakerCapability.BALANCE_READ)
    outage = classify_provider_readback_health(required_capabilities=(BookmakerCapability.BALANCE_READ,), evaluated_at='2026-09-21T18:00:10+00:00', freshness_limit_seconds=30, failure_class=ProviderReadbackFailureClass.PROVIDER_5XX, last_known_snapshot=snapshot)
    recovered = classify_provider_readback_health(required_capabilities=(BookmakerCapability.BALANCE_READ,), evaluated_at='2026-09-21T18:00:20+00:00', freshness_limit_seconds=30, snapshot=snapshot, successful_scopes=(_scope(BookmakerCapability.BALANCE_READ, snapshot=snapshot),), last_known_snapshot=snapshot)
    assert outage.state is ProviderReadbackHealthState.UNAVAILABLE_TRANSIENT
    assert recovered.state is ProviderReadbackHealthState.FRESH_COMPLETE
    assert outage.result_id != recovered.result_id

def test_result_identity_is_deterministic_across_required_capability_order() -> None:
    snapshot = _snapshot(BookmakerCapability.BALANCE_READ, BookmakerCapability.OPEN_POSITIONS_READ)
    scopes = (_scope(BookmakerCapability.OPEN_POSITIONS_READ, snapshot=snapshot), _scope(BookmakerCapability.BALANCE_READ, snapshot=snapshot))
    kwargs = dict(evaluated_at='2026-09-21T18:00:05+00:00', freshness_limit_seconds=30, snapshot=snapshot, successful_scopes=scopes)
    one = classify_provider_readback_health(required_capabilities=(BookmakerCapability.BALANCE_READ, BookmakerCapability.OPEN_POSITIONS_READ), **kwargs)
    two = classify_provider_readback_health(required_capabilities=(BookmakerCapability.OPEN_POSITIONS_READ, BookmakerCapability.BALANCE_READ), **kwargs)
    assert one.result_id == two.result_id

def test_unbound_complete_scope_cannot_mint_fresh_complete_authority() -> None:
    with pytest.raises(ProviderReadbackHealthError, match='product-issued'):
        _scope(BookmakerCapability.BALANCE_READ)

def test_caller_cannot_self_mint_complete_scope_even_with_exact_current_snapshot_hash() -> None:
    snapshot = _snapshot(BookmakerCapability.BALANCE_READ)
    with pytest.raises(ProviderReadbackHealthError, match='product-issued'):
        ProviderReadScopeEvidence(capability=BookmakerCapability.BALANCE_READ, request_scope_sha256=_SCOPE_HASH, response_sha256=_RESPONSE_HASH, observed_at=_OBSERVED, snapshot_sha256=canonical_snapshot_sha256(snapshot), complete=True)

def test_product_issued_scope_cannot_be_rebound_with_dataclass_replace() -> None:
    snapshot = _snapshot(BookmakerCapability.OPEN_POSITIONS_READ)
    other = _snapshot(BookmakerCapability.OPEN_POSITIONS_READ, account_id='acct-2')
    issued = _scope(BookmakerCapability.OPEN_POSITIONS_READ, snapshot=snapshot)
    with pytest.raises(ProviderReadbackHealthError, match='product-issued'):
        replace(issued, snapshot_sha256=canonical_snapshot_sha256(other))

def test_serialized_complete_scope_loses_process_local_issuer_authority() -> None:
    snapshot = _snapshot(BookmakerCapability.BALANCE_READ)
    issued = _scope(BookmakerCapability.BALANCE_READ, snapshot=snapshot)
    replayed = pickle.loads(pickle.dumps(issued))
    health = classify_provider_readback_health(required_capabilities=(BookmakerCapability.BALANCE_READ,), evaluated_at='2026-09-21T18:00:05+00:00', freshness_limit_seconds=30, snapshot=snapshot, successful_scopes=(replayed,))
    assert health.state is ProviderReadbackHealthState.FRESH_PARTIAL
    assert not health.can_authorize_current_state

def test_public_partial_scope_stays_non_authoritative() -> None:
    snapshot = _snapshot(BookmakerCapability.BALANCE_READ)
    partial = ProviderReadScopeEvidence(capability=BookmakerCapability.BALANCE_READ, request_scope_sha256=_SCOPE_HASH, response_sha256=_RESPONSE_HASH, observed_at=_OBSERVED, snapshot_sha256=canonical_snapshot_sha256(snapshot), complete=False)
    health = classify_provider_readback_health(required_capabilities=(BookmakerCapability.BALANCE_READ,), evaluated_at='2026-09-21T18:00:05+00:00', freshness_limit_seconds=30, snapshot=snapshot, successful_scopes=(partial,))
    assert health.state is ProviderReadbackHealthState.FRESH_PARTIAL
    assert not health.can_authorize_current_state

def test_scope_bound_to_different_account_snapshot_is_rejected() -> None:
    snapshot = _snapshot(BookmakerCapability.OPEN_POSITIONS_READ)
    other = _snapshot(BookmakerCapability.OPEN_POSITIONS_READ, account_id='acct-2')
    with pytest.raises(ProviderReadbackHealthError, match='bound to a different canonical snapshot'):
        classify_provider_readback_health(required_capabilities=(BookmakerCapability.OPEN_POSITIONS_READ,), evaluated_at='2026-09-21T18:00:05+00:00', freshness_limit_seconds=30, snapshot=snapshot, successful_scopes=(_scope(BookmakerCapability.OPEN_POSITIONS_READ, snapshot=other),))

def test_stale_scope_evidence_cannot_be_laundered_by_fresh_snapshot_timestamp() -> None:
    snapshot = _snapshot(BookmakerCapability.BALANCE_READ)
    health = classify_provider_readback_health(required_capabilities=(BookmakerCapability.BALANCE_READ,), evaluated_at='2026-09-21T18:00:05+00:00', freshness_limit_seconds=30, snapshot=snapshot, successful_scopes=(_scope(BookmakerCapability.BALANCE_READ, snapshot=snapshot, observed_at='2026-09-21T17:59:00+00:00'),))
    assert health.state is ProviderReadbackHealthState.STALE_LAST_KNOWN
    assert not health.can_authorize_current_state
    assert health.last_known_snapshot_sha256 == canonical_snapshot_sha256(snapshot)

def test_future_scope_timestamp_is_rejected_fail_closed() -> None:
    snapshot = _snapshot(BookmakerCapability.BALANCE_READ)
    with pytest.raises(ProviderReadbackHealthError, match='scope observation cannot be after health evaluation'):
        classify_provider_readback_health(required_capabilities=(BookmakerCapability.BALANCE_READ,), evaluated_at='2026-09-21T18:00:05+00:00', freshness_limit_seconds=30, snapshot=snapshot, successful_scopes=(_scope(BookmakerCapability.BALANCE_READ, snapshot=snapshot, observed_at='2026-09-21T18:00:06+00:00'),))
