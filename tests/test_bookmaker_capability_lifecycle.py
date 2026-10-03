from datetime import datetime, timezone
import json

import pytest

import autosport.betdaq_account_readonly as betdaq_account_module
from autosport.betdaq_account_readonly import (
    BetdaqAccountReadOnlyClient,
    BetdaqCredentials,
)
from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.bookmaker_capability_lifecycle import (
    BETDAQ_AUTHENTICATED_MAX_OBSERVATION_AGE_SECONDS,
    BETDAQ_AUTHENTICATED_SOURCE_CONTRACT_REF,
    BETDAQ_AUTHENTICATED_VALIDATION_POLICY_VERSION,
    BetdaqAuthenticatedCapabilityIssuance,
    CapabilityAvailability,
    CapabilityAvailabilityState,
    CapabilityEvidence,
    CapabilityEvidenceError,
    CapabilityEvidenceJournal,
    CapabilityEvidenceSource,
    CapabilityEvidenceStrength,
    CapabilityLifecycleState,
    CapabilityRequirement,
    CapabilityScope,
    issue_betdaq_authenticated_capability_evidence,
)

_HASH = "a" * 64
_TS = "2026-09-21T10:00:00+00:00"


_BETDAQ_NS = "http://www.GlobalBettingExchange.com/ExternalAPI/"
_BETDAQ_SOAP = "http://schemas.xmlsoap.org/soap/envelope/"


class _BetdaqHttpResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.payload


def _canonical_betdaq_balance_client(
    monkeypatch, *, observed_minute=0, venue_id="betdaq"
):
    payload = (
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<soap:Envelope xmlns:soap="{_BETDAQ_SOAP}" xmlns="{_BETDAQ_NS}">'
        f"<soap:Body><GetAccountBalancesResponse>"
        f'<GetAccountBalancesResult Currency="EUR" Balance="120.02" '
        f'Exposure="-20.01" AvailableFunds="100.01" Credit="0">'
        f'<ReturnStatus Code="0" Description="fixture-status" CallId="fixture-call" />'
        f"</GetAccountBalancesResult></GetAccountBalancesResponse>"
        f"</soap:Body></soap:Envelope>"
    ).encode()
    calls = []

    def opener(request, *, timeout):
        calls.append((request, timeout))
        return _BetdaqHttpResponse(payload)

    monkeypatch.setattr(betdaq_account_module, "urlopen", opener)
    value = BetdaqAccountReadOnlyClient(
        BetdaqCredentials("alice", "secret-pass", "app-id"),
        clock=lambda: datetime(
            2026, 9, 21, 10, observed_minute, tzinfo=timezone.utc
        ),
        venue_id=venue_id,
        account_id="caller-label-must-not-be-authority",
    )
    return value, calls


def _betdaq_authenticated_issuance(
    monkeypatch, *, observed_minute=0, predecessor_id=None
):
    client, calls = _canonical_betdaq_balance_client(
        monkeypatch, observed_minute=observed_minute
    )
    issuance = issue_betdaq_authenticated_capability_evidence(
        client,
        BookmakerCapability.BALANCE_READ,
        committed_at=f"2026-09-21T10:{observed_minute + 1:02d}:00+00:00",
        review_due_at=f"2026-09-21T11:{observed_minute + 1:02d}:00+00:00",
        predecessor_id=predecessor_id,
    )
    return issuance, calls


def _betdaq_authenticated_requirement(issuance, *, require_available=False):
    return CapabilityRequirement(
        BookmakerCapability.BALANCE_READ,
        CapabilityEvidenceStrength.OBSERVED_AUTHENTICATED,
        issuance.evidence.scope,
        BETDAQ_AUTHENTICATED_VALIDATION_POLICY_VERSION,
        BETDAQ_AUTHENTICATED_SOURCE_CONTRACT_REF,
        3600,
        require_available=require_available,
    )


def test_product_issued_betdaq_authenticated_evidence_can_authorize_bounded_read_capability(
    monkeypatch,
):
    issuance, calls = _betdaq_authenticated_issuance(monkeypatch)
    assert type(issuance) is BetdaqAuthenticatedCapabilityIssuance
    assert len(calls) == 1
    assert issuance.evidence.strength is CapabilityEvidenceStrength.OBSERVED_AUTHENTICATED
    assert issuance.evidence.scope.account_id is None
    assert (
        issuance.evidence.scope.credential_identity
        == issuance.profile.account_id
    )
    assert "caller-label" not in issuance.profile.account_id

    journal = CapabilityEvidenceJournal()
    journal.publish(issuance.evidence)
    decision = journal.resolve(
        _betdaq_authenticated_requirement(issuance),
        {issuance.profile.profile_id: issuance.profile},
        as_of="2026-09-21T10:02:00+00:00",
    )

    assert decision.allowed
    assert decision.lifecycle is CapabilityLifecycleState.CURRENT
    assert decision.availability is CapabilityAvailabilityState.UNKNOWN
    assert "product-issued authenticated" in decision.reason


def test_betdaq_product_policy_rejects_caller_extended_freshness(monkeypatch):
    issuance, _ = _betdaq_authenticated_issuance(monkeypatch)
    journal = CapabilityEvidenceJournal()
    journal.publish(issuance.evidence)
    requirement = CapabilityRequirement(
        BookmakerCapability.BALANCE_READ,
        CapabilityEvidenceStrength.OBSERVED_AUTHENTICATED,
        issuance.evidence.scope,
        BETDAQ_AUTHENTICATED_VALIDATION_POLICY_VERSION,
        BETDAQ_AUTHENTICATED_SOURCE_CONTRACT_REF,
        BETDAQ_AUTHENTICATED_MAX_OBSERVATION_AGE_SECONDS + 1,
        require_available=False,
    )

    decision = journal.resolve(
        requirement,
        {issuance.profile.profile_id: issuance.profile},
        as_of="2026-09-21T10:02:00+00:00",
    )

    assert not decision.allowed
    assert decision.lifecycle is CapabilityLifecycleState.REVALIDATION_REQUIRED
    assert "exceeds BETDAQ product policy" in decision.reason


def test_product_issued_betdaq_evidence_does_not_mint_available_health(monkeypatch):
    issuance, _ = _betdaq_authenticated_issuance(monkeypatch)
    journal = CapabilityEvidenceJournal()
    journal.publish(issuance.evidence)

    decision = journal.resolve(
        _betdaq_authenticated_requirement(issuance, require_available=True),
        {issuance.profile.profile_id: issuance.profile},
        as_of="2026-09-21T10:02:00+00:00",
    )

    assert not decision.allowed
    assert decision.availability is CapabilityAvailabilityState.UNKNOWN
    assert "not AVAILABLE" in decision.reason


def test_available_audit_record_cannot_clear_prior_negative_runtime_state(
    monkeypatch,
):
    issuance, _ = _betdaq_authenticated_issuance(monkeypatch)
    journal = CapabilityEvidenceJournal()
    journal.publish(issuance.evidence)
    journal.publish_availability(
        CapabilityAvailability(
            evidence_id=issuance.evidence.evidence_id,
            state=CapabilityAvailabilityState.TEMPORARILY_UNAVAILABLE,
            observed_at="2026-09-21T10:02:00+00:00",
            source_ref="provider-outage",
            source_payload_sha256="1" * 64,
        )
    )
    journal.publish_availability(
        CapabilityAvailability(
            evidence_id=issuance.evidence.evidence_id,
            state=CapabilityAvailabilityState.AVAILABLE,
            observed_at="2026-09-21T10:03:00+00:00",
            source_ref="caller-claims-recovered",
            source_payload_sha256="2" * 64,
        )
    )

    decision = journal.resolve(
        _betdaq_authenticated_requirement(issuance),
        {issuance.profile.profile_id: issuance.profile},
        as_of="2026-09-21T10:04:00+00:00",
    )

    assert decision.allowed
    assert decision.availability is CapabilityAvailabilityState.TEMPORARILY_UNAVAILABLE


def test_unknown_audit_record_cannot_clear_prior_degraded_runtime_state(
    monkeypatch,
):
    issuance, _ = _betdaq_authenticated_issuance(monkeypatch)
    journal = CapabilityEvidenceJournal()
    journal.publish(issuance.evidence)
    journal.publish_availability(
        CapabilityAvailability(
            evidence_id=issuance.evidence.evidence_id,
            state=CapabilityAvailabilityState.DEGRADED,
            observed_at="2026-09-21T10:02:00+00:00",
            source_ref="provider-degraded",
            source_payload_sha256="3" * 64,
        )
    )
    journal.publish_availability(
        CapabilityAvailability(
            evidence_id=issuance.evidence.evidence_id,
            state=CapabilityAvailabilityState.UNKNOWN,
            observed_at="2026-09-21T10:03:00+00:00",
            source_ref="caller-unknown",
            source_payload_sha256="4" * 64,
        )
    )

    decision = journal.resolve(
        _betdaq_authenticated_requirement(issuance, require_available=True),
        {issuance.profile.profile_id: issuance.profile},
        as_of="2026-09-21T10:04:00+00:00",
    )

    assert not decision.allowed
    assert decision.availability is CapabilityAvailabilityState.DEGRADED
    assert "not AVAILABLE" in decision.reason


def test_fresh_lifecycle_successor_does_not_inherit_old_evidence_runtime_outage(
    monkeypatch,
):
    first, _ = _betdaq_authenticated_issuance(monkeypatch)
    journal = CapabilityEvidenceJournal()
    journal.publish(first.evidence)
    journal.publish_availability(
        CapabilityAvailability(
            evidence_id=first.evidence.evidence_id,
            state=CapabilityAvailabilityState.TEMPORARILY_UNAVAILABLE,
            observed_at="2026-09-21T10:02:00+00:00",
            source_ref="old-provider-outage",
            source_payload_sha256="5" * 64,
        )
    )

    fresh, _ = _betdaq_authenticated_issuance(
        monkeypatch,
        observed_minute=5,
        predecessor_id=first.evidence.evidence_id,
    )
    journal.publish(fresh.evidence)
    decision = journal.resolve(
        _betdaq_authenticated_requirement(fresh),
        {fresh.profile.profile_id: fresh.profile},
        as_of="2026-09-21T10:07:00+00:00",
    )

    assert decision.allowed
    assert decision.evidence_id == fresh.evidence.evidence_id
    assert decision.availability is CapabilityAvailabilityState.UNKNOWN


def test_copied_betdaq_issuance_payload_cannot_reuse_process_local_provenance(
    monkeypatch,
):
    issuance, _ = _betdaq_authenticated_issuance(monkeypatch)
    copied = CapabilityEvidence(
        **{
            field: getattr(issuance.evidence, field)
            for field in issuance.evidence.__dataclass_fields__
            if field != "__weakref__"
        }
    )
    assert copied.evidence_id == issuance.evidence.evidence_id
    journal = CapabilityEvidenceJournal()
    journal.publish(copied)

    decision = journal.resolve(
        _betdaq_authenticated_requirement(issuance),
        {issuance.profile.profile_id: issuance.profile},
        as_of="2026-09-21T10:02:00+00:00",
    )

    assert not decision.allowed
    assert decision.lifecycle is CapabilityLifecycleState.REVALIDATION_REQUIRED
    assert "product-owned upstream authority" in decision.reason


def test_betdaq_positive_provenance_is_not_restored_from_serialized_journal(
    monkeypatch,
):
    issuance, _ = _betdaq_authenticated_issuance(monkeypatch)
    journal = CapabilityEvidenceJournal()
    journal.publish(issuance.evidence)
    restored = CapabilityEvidenceJournal.from_json(journal.to_json())

    decision = restored.resolve(
        _betdaq_authenticated_requirement(issuance),
        {issuance.profile.profile_id: issuance.profile},
        as_of="2026-09-21T10:02:00+00:00",
    )

    assert not decision.allowed
    assert decision.lifecycle is CapabilityLifecycleState.REVALIDATION_REQUIRED
    assert "product-owned upstream authority" in decision.reason


def test_durable_journal_can_stage_exact_product_issued_evidence_without_mutation(
    monkeypatch,
):
    issuance, _ = _betdaq_authenticated_issuance(monkeypatch)
    durable = CapabilityEvidenceJournal()
    durable.publish(issuance.evidence)
    restored = CapabilityEvidenceJournal.from_json(durable.to_json())
    before = restored.to_json()

    staged = restored.staged_with_exact_evidence(issuance.evidence)
    decision = staged.resolve(
        _betdaq_authenticated_requirement(issuance),
        {issuance.profile.profile_id: issuance.profile},
        as_of="2026-09-21T10:02:00+00:00",
    )

    assert decision.allowed
    assert decision.evidence_id == issuance.evidence.evidence_id
    assert restored.to_json() == before


def test_staging_copied_payload_does_not_restore_product_provenance(monkeypatch):
    issuance, _ = _betdaq_authenticated_issuance(monkeypatch)
    durable = CapabilityEvidenceJournal()
    durable.publish(issuance.evidence)
    restored = CapabilityEvidenceJournal.from_json(durable.to_json())
    before = restored.to_json()
    copied = CapabilityEvidence(
        **{
            field: getattr(issuance.evidence, field)
            for field in issuance.evidence.__dataclass_fields__
            if field != "__weakref__"
        }
    )

    staged = restored.staged_with_exact_evidence(copied)
    decision = staged.resolve(
        _betdaq_authenticated_requirement(issuance),
        {issuance.profile.profile_id: issuance.profile},
        as_of="2026-09-21T10:02:00+00:00",
    )

    assert not decision.allowed
    assert decision.lifecycle is CapabilityLifecycleState.REVALIDATION_REQUIRED
    assert "product-owned upstream authority" in decision.reason
    assert restored.to_json() == before


def test_latest_evidence_query_keeps_newer_successor_authoritative(monkeypatch):
    first, _ = _betdaq_authenticated_issuance(monkeypatch)
    journal = CapabilityEvidenceJournal()
    journal.publish(first.evidence)
    fresh, _ = _betdaq_authenticated_issuance(
        monkeypatch,
        observed_minute=5,
        predecessor_id=first.evidence.evidence_id,
    )
    journal.publish(fresh.evidence)

    assert journal.latest_evidence_id_for(first.evidence) == fresh.evidence.evidence_id
    assert journal.latest_evidence_id_for(fresh.evidence) == fresh.evidence.evidence_id
    assert journal.latest_evidence_id_for(
        first.evidence,
        as_of="2026-09-21T10:05:59+00:00",
    ) == first.evidence.evidence_id
    assert journal.latest_evidence_id_for(
        first.evidence,
        as_of="2026-09-21T10:06:00+00:00",
    ) == fresh.evidence.evidence_id


def test_staged_historical_exact_object_does_not_displace_newer_successor(monkeypatch):
    first, _ = _betdaq_authenticated_issuance(monkeypatch)
    journal = CapabilityEvidenceJournal()
    journal.publish(first.evidence)
    fresh, _ = _betdaq_authenticated_issuance(
        monkeypatch,
        observed_minute=5,
        predecessor_id=first.evidence.evidence_id,
    )
    journal.publish(fresh.evidence)

    staged = journal.staged_with_exact_evidence(first.evidence)

    assert staged.latest_evidence_id_for(first.evidence) == fresh.evidence.evidence_id
    assert staged.latest_evidence_id_for(
        first.evidence,
        as_of="2026-09-21T10:05:59+00:00",
    ) == first.evidence.evidence_id
    assert journal.latest_evidence_id_for(first.evidence) == fresh.evidence.evidence_id


def test_betdaq_issuer_rejects_noncanonical_client_before_provider_io(monkeypatch):
    canonical, calls = _canonical_betdaq_balance_client(monkeypatch)

    class DerivedClient(BetdaqAccountReadOnlyClient):
        pass

    derived = DerivedClient(canonical._credentials)

    with pytest.raises(
        CapabilityEvidenceError,
        match="exact canonical read-only client",
    ):
        issue_betdaq_authenticated_capability_evidence(
            derived,
            BookmakerCapability.BALANCE_READ,
            committed_at="2026-09-21T10:01:00+00:00",
            review_due_at="2026-09-21T11:01:00+00:00",
        )

    assert calls == []





def test_betdaq_issuer_rejects_noncanonical_venue_before_provider_io(monkeypatch):
    client, calls = _canonical_betdaq_balance_client(
        monkeypatch, venue_id="caller-betdaq-alias"
    )

    with pytest.raises(
        CapabilityEvidenceError,
        match="canonical betdaq venue",
    ):
        issue_betdaq_authenticated_capability_evidence(
            client,
            BookmakerCapability.BALANCE_READ,
            committed_at="2026-09-21T10:01:00+00:00",
            review_due_at="2026-09-21T11:01:00+00:00",
        )

    assert calls == []


@pytest.mark.parametrize(
    "method_name",
    [
        "read_account_balance",
        "read_complete_current_orders",
        "read_bootstrap_page",
        "read_orders_changed_since",
        "_call",
        "_request_xml",
        "_observed_at",
    ],
)
def test_betdaq_issuer_rejects_instance_shadowed_client_dispatch_before_io(
    monkeypatch, method_name
):
    client, calls = _canonical_betdaq_balance_client(monkeypatch)
    monkeypatch.setattr(client, method_name, lambda *args, **kwargs: None)

    with pytest.raises(
        CapabilityEvidenceError,
        match="client dispatch was replaced or shadowed",
    ):
        issue_betdaq_authenticated_capability_evidence(
            client,
            BookmakerCapability.BALANCE_READ,
            committed_at="2026-09-21T10:01:00+00:00",
            review_due_at="2026-09-21T11:01:00+00:00",
        )

    assert calls == []


def test_betdaq_issuer_rejects_class_dispatch_mutation_before_io(monkeypatch):
    client, calls = _canonical_betdaq_balance_client(monkeypatch)
    monkeypatch.setattr(
        BetdaqAccountReadOnlyClient,
        "read_account_balance",
        lambda self: None,
    )

    with pytest.raises(
        CapabilityEvidenceError,
        match="issuance surface changed",
    ):
        issue_betdaq_authenticated_capability_evidence(
            client,
            BookmakerCapability.BALANCE_READ,
            committed_at="2026-09-21T10:01:00+00:00",
            review_due_at="2026-09-21T11:01:00+00:00",
        )

    assert calls == []


def test_betdaq_issuer_rejects_mutated_secure_endpoint_before_io(monkeypatch):
    client, calls = _canonical_betdaq_balance_client(monkeypatch)
    monkeypatch.setattr(
        betdaq_account_module,
        "_SECURE_ENDPOINT",
        "https://attacker.invalid/SecureService.asmx",
    )

    with pytest.raises(
        CapabilityEvidenceError,
        match="issuance surface changed",
    ):
        issue_betdaq_authenticated_capability_evidence(
            client,
            BookmakerCapability.BALANCE_READ,
            committed_at="2026-09-21T10:01:00+00:00",
            review_due_at="2026-09-21T11:01:00+00:00",
        )

    assert calls == []


def test_betdaq_issuer_rejects_mutated_canonical_transport_root_before_io(monkeypatch):
    client, calls = _canonical_betdaq_balance_client(monkeypatch)
    monkeypatch.setattr(
        betdaq_account_module,
        "_CANONICAL_HTTPS_POST",
        lambda *args, **kwargs: b"",
    )

    with pytest.raises(
        CapabilityEvidenceError,
        match="issuance surface changed",
    ):
        issue_betdaq_authenticated_capability_evidence(
            client,
            BookmakerCapability.BALANCE_READ,
            committed_at="2026-09-21T10:01:00+00:00",
            review_due_at="2026-09-21T11:01:00+00:00",
        )

    assert calls == []


def test_restart_can_recover_only_after_fresh_canonical_betdaq_reacquisition(
    monkeypatch,
):
    first, _ = _betdaq_authenticated_issuance(monkeypatch)
    journal = CapabilityEvidenceJournal()
    journal.publish(first.evidence)
    restored = CapabilityEvidenceJournal.from_json(journal.to_json())

    stale_decision = restored.resolve(
        _betdaq_authenticated_requirement(first),
        {first.profile.profile_id: first.profile},
        as_of="2026-09-21T10:02:00+00:00",
    )
    assert not stale_decision.allowed

    fresh, calls = _betdaq_authenticated_issuance(
        monkeypatch,
        observed_minute=5,
        predecessor_id=first.evidence.evidence_id,
    )
    assert len(calls) == 1
    assert (
        fresh.evidence.scope.credential_identity
        == first.evidence.scope.credential_identity
    )
    restored.publish(fresh.evidence)

    recovered = restored.resolve(
        _betdaq_authenticated_requirement(fresh),
        {fresh.profile.profile_id: fresh.profile},
        as_of="2026-09-21T10:07:00+00:00",
    )
    assert recovered.allowed
    assert recovered.evidence_id == fresh.evidence.evidence_id
    assert "product-issued authenticated" in recovered.reason


def _profile(
    capability=BookmakerCapability.BALANCE_READ,
    state=BookmakerCapabilityState.SUPPORTED,
    observed_at=_TS,
):
    return BookmakerCapabilityProfile(
        venue_id="betfair",
        account_id="acct-a",
        adapter_id="adapter-a",
        adapter_version="1",
        profile_version=1,
        facts=(BookmakerCapabilityFact(capability, state),),
        observed_at=observed_at,
        source_ref="probe",
        source_payload_sha256=_HASH,
    )


def _scope(*, account="acct-a", credential="app-v1"):
    return CapabilityScope(
        venue_id="betfair",
        account_id=account,
        environment="production",
        jurisdiction="SK",
        sport="soccer",
        market_family="match_odds",
        live_mode="live",
        credential_identity=credential,
    )


def _evidence(profile=None, **overrides):
    profile = profile or _profile()
    values = {
        "profile_id": profile.profile_id,
        "capability": BookmakerCapability.BALANCE_READ,
        "support_state": BookmakerCapabilityState.SUPPORTED,
        "strength": CapabilityEvidenceStrength.OBSERVED_ACCOUNT_SCOPED,
        "source": CapabilityEvidenceSource.ACCOUNT_OBSERVATION,
        "scope": _scope(),
        "observed_at": profile.observed_at,
        "committed_at": "2026-09-21T10:01:00+00:00",
        "review_due_at": "2026-09-22T10:01:00+00:00",
        "validation_policy_version": "v1",
        "source_contract_ref": "api-v1",
        "source_payload_sha256": _HASH,
    }
    values.update(overrides)
    return CapabilityEvidence(**values)


def _requirement(**overrides):
    values = {
        "capability": BookmakerCapability.BALANCE_READ,
        "minimum_strength": CapabilityEvidenceStrength.OBSERVED_AUTHENTICATED,
        "scope": _scope(),
        "validation_policy_version": "v1",
        "source_contract_ref": "api-v1",
        "max_observation_age_seconds": 86400,
    }
    values.update(overrides)
    return CapabilityRequirement(**values)


def _availability(evidence, state=CapabilityAvailabilityState.AVAILABLE, **overrides):
    values = {
        "evidence_id": evidence.evidence_id,
        "state": state,
        "observed_at": "2026-09-21T10:02:00+00:00",
        "source_ref": "health",
        "source_payload_sha256": _HASH,
    }
    values.update(overrides)
    return CapabilityAvailability(**values)


def _journal(profile=None, evidence=None, availability=True):
    profile = profile or _profile()
    evidence = evidence or _evidence(profile)
    journal = CapabilityEvidenceJournal()
    journal.publish(evidence)
    if availability:
        journal.publish_availability(_availability(evidence))
    return profile, evidence, journal


def _resolve(journal, profile, requirement=None, as_of="2026-09-21T10:03:00+00:00"):
    return journal.resolve(
        requirement or _requirement(),
        {profile.profile_id: profile},
        as_of=as_of,
    )


def test_caller_authenticated_account_scope_requires_upstream_authority():
    profile, _, journal = _journal()
    decision = _resolve(journal, profile)
    assert not decision.allowed
    assert decision.lifecycle is CapabilityLifecycleState.REVALIDATION_REQUIRED
    assert decision.availability is CapabilityAvailabilityState.UNKNOWN
    assert "product-owned upstream authority" in decision.reason


def test_availability_cannot_precede_capability_observation():
    profile = _profile()
    evidence = _evidence(profile)
    journal = CapabilityEvidenceJournal()
    journal.publish(evidence)
    earlier = _availability(
        evidence,
        CapabilityAvailabilityState.DEGRADED,
        observed_at="2026-09-21T09:59:59+00:00",
    )

    with pytest.raises(
        CapabilityEvidenceError,
        match="cannot precede its capability observation",
    ):
        journal.publish_availability(earlier)


def test_availability_same_time_conflict_fails_but_identical_is_idempotent():
    profile = _profile()
    evidence = _evidence(profile)
    journal = CapabilityEvidenceJournal()
    journal.publish(evidence)
    degraded = _availability(
        evidence,
        CapabilityAvailabilityState.DEGRADED,
        observed_at="2026-09-21T10:02:00+00:00",
    )
    assert journal.publish_availability(degraded) == degraded.availability_id
    assert journal.publish_availability(degraded) == degraded.availability_id

    conflicting = _availability(
        evidence,
        CapabilityAvailabilityState.TEMPORARILY_UNAVAILABLE,
        observed_at="2026-09-21T12:02:00+02:00",
    )
    with pytest.raises(
        CapabilityEvidenceError,
        match="conflicting availability at the same observation timestamp",
    ):
        journal.publish_availability(conflicting)


def test_caller_available_state_cannot_mint_runtime_health_authority():
    profile = _profile()
    scope = CapabilityScope("betfair", None, "production")
    evidence = _evidence(
        profile,
        strength=CapabilityEvidenceStrength.DOCUMENTED_ONLY,
        source=CapabilityEvidenceSource.OFFICIAL_DOCUMENT,
        scope=scope,
    )
    journal = CapabilityEvidenceJournal()
    journal.publish(evidence)
    journal.publish_availability(_availability(evidence))
    requirement = CapabilityRequirement(
        BookmakerCapability.BALANCE_READ,
        CapabilityEvidenceStrength.DOCUMENTED_ONLY,
        scope,
        "v1",
        "api-v1",
        86400,
        require_available=True,
    )
    decision = journal.resolve(
        requirement,
        {profile.profile_id: profile},
        as_of="2026-09-21T10:03:00+00:00",
    )
    assert not decision.allowed
    assert decision.lifecycle is CapabilityLifecycleState.UNKNOWN
    assert decision.availability is CapabilityAvailabilityState.UNKNOWN
    assert "product-owned provenance authority" in decision.reason


@pytest.mark.parametrize(
    ("strength", "source"),
    [
        (
            CapabilityEvidenceStrength.DOCUMENTED_ONLY,
            CapabilityEvidenceSource.OFFICIAL_DOCUMENT,
        ),
        (
            CapabilityEvidenceStrength.OBSERVED_PUBLIC,
            CapabilityEvidenceSource.PUBLIC_OBSERVATION,
        ),
    ],
)
def test_caller_low_grade_evidence_cannot_mint_positive_authority_without_provenance(
    strength, source
):
    profile = _profile()
    scope = CapabilityScope("betfair", None, "production")
    evidence = _evidence(
        profile,
        strength=strength,
        source=source,
        scope=scope,
    )
    journal = CapabilityEvidenceJournal()
    journal.publish(evidence)
    requirement = CapabilityRequirement(
        BookmakerCapability.BALANCE_READ,
        strength,
        scope,
        "v1",
        "api-v1",
        86400,
        require_available=False,
    )

    decision = journal.resolve(
        requirement,
        {profile.profile_id: profile},
        as_of="2026-09-21T10:03:00+00:00",
    )

    assert not decision.allowed
    assert decision.lifecycle is CapabilityLifecycleState.UNKNOWN
    assert decision.availability is CapabilityAvailabilityState.UNKNOWN
    assert "product-owned provenance authority" in decision.reason


def test_low_grade_restart_does_not_strengthen_unproven_provenance():
    profile = _profile()
    scope = CapabilityScope("betfair", None, "production")
    evidence = _evidence(
        profile,
        strength=CapabilityEvidenceStrength.DOCUMENTED_ONLY,
        source=CapabilityEvidenceSource.OFFICIAL_DOCUMENT,
        scope=scope,
    )
    journal = CapabilityEvidenceJournal()
    journal.publish(evidence)
    restored = CapabilityEvidenceJournal.from_json(journal.to_json())
    requirement = CapabilityRequirement(
        BookmakerCapability.BALANCE_READ,
        CapabilityEvidenceStrength.DOCUMENTED_ONLY,
        scope,
        "v1",
        "api-v1",
        86400,
        require_available=False,
    )

    decision = restored.resolve(
        requirement,
        {profile.profile_id: profile},
        as_of="2026-09-21T10:03:00+00:00",
    )

    assert not decision.allowed
    assert decision.lifecycle is CapabilityLifecycleState.UNKNOWN
    assert decision.evidence_id == evidence.evidence_id
    assert "product-owned provenance authority" in decision.reason


def test_documented_place_bet_cannot_satisfy_authenticated_requirement():
    profile = _profile(BookmakerCapability.PLACE_BET)
    evidence = _evidence(
        profile,
        capability=BookmakerCapability.PLACE_BET,
        strength=CapabilityEvidenceStrength.DOCUMENTED_ONLY,
        source=CapabilityEvidenceSource.OFFICIAL_DOCUMENT,
        scope=CapabilityScope("betfair", None, "production"),
    )
    journal = CapabilityEvidenceJournal()
    journal.publish(evidence)
    journal.publish_availability(_availability(evidence))
    requirement = CapabilityRequirement(
        BookmakerCapability.PLACE_BET,
        CapabilityEvidenceStrength.OBSERVED_AUTHENTICATED,
        CapabilityScope("betfair", None, "production"),
        "v1",
        "api-v1",
        86400,
    )
    decision = journal.resolve(
        requirement, {profile.profile_id: profile}, as_of="2026-09-21T10:03:00+00:00"
    )
    assert not decision.allowed
    assert "strength" in decision.reason


def test_unproven_requirement_cannot_authorize_any_capability():
    with pytest.raises(CapabilityEvidenceError, match="UNPROVEN"):
        _requirement(minimum_strength=CapabilityEvidenceStrength.UNPROVEN)


def test_republishing_old_profile_cannot_extend_observation_freshness():
    profile = _profile()
    republished = _evidence(
        profile,
        committed_at="2026-09-21T10:59:00+00:00",
        review_due_at="2026-09-22T10:59:00+00:00",
        source_payload_sha256="b" * 64,
    )
    journal = CapabilityEvidenceJournal()
    journal.publish(republished)
    journal.publish_availability(
        _availability(republished, observed_at="2026-09-21T10:59:30+00:00")
    )
    decision = _resolve(
        journal,
        profile,
        _requirement(max_observation_age_seconds=3600),
        as_of="2026-09-21T11:00:00+00:00",
    )
    assert not decision.allowed
    assert decision.lifecycle is CapabilityLifecycleState.REVALIDATION_REQUIRED
    assert "observation age" in decision.reason


def test_account_scope_cannot_generalize():
    profile, _, journal = _journal()
    decision = _resolve(
        journal,
        profile,
        _requirement(scope=_scope(account="acct-b")),
    )
    assert not decision.allowed
    assert decision.lifecycle is CapabilityLifecycleState.UNKNOWN


def test_credential_policy_and_contract_changes_require_revalidation():
    profile, _, journal = _journal()
    credential = _resolve(
        journal,
        profile,
        _requirement(scope=_scope(credential="app-v2")),
    )
    policy = _resolve(
        journal,
        profile,
        _requirement(validation_policy_version="v2"),
    )
    contract = _resolve(
        journal,
        profile,
        _requirement(source_contract_ref="api-v2"),
    )
    assert credential.lifecycle is CapabilityLifecycleState.REVALIDATION_REQUIRED
    assert policy.lifecycle is CapabilityLifecycleState.REVALIDATION_REQUIRED
    assert contract.lifecycle is CapabilityLifecycleState.REVALIDATION_REQUIRED


def test_expiry_and_review_boundaries_are_closed():
    profile = _profile()
    expiring = _evidence(
        profile,
        provider_expires_at="2026-09-21T11:00:00+00:00",
        review_due_at="2026-09-21T12:00:00+00:00",
    )
    _, _, journal = _journal(profile, expiring)
    before_expiry = _resolve(
        journal, profile, as_of="2026-09-21T10:59:59+00:00"
    )
    assert not before_expiry.allowed
    assert before_expiry.lifecycle is CapabilityLifecycleState.REVALIDATION_REQUIRED
    assert "product-owned upstream authority" in before_expiry.reason
    expired = _resolve(journal, profile, as_of="2026-09-21T11:00:00+00:00")
    assert expired.lifecycle is CapabilityLifecycleState.STALE

    review = _evidence(profile, review_due_at="2026-09-21T11:00:00+00:00")
    _, _, journal = _journal(profile, review)
    due = _resolve(journal, profile, as_of="2026-09-21T11:00:00+00:00")
    assert due.lifecycle is CapabilityLifecycleState.REVALIDATION_REQUIRED


def test_transient_outage_does_not_revoke_durable_support():
    profile = _profile()
    evidence = _evidence(profile)
    journal = CapabilityEvidenceJournal()
    journal.publish(evidence)
    journal.publish_availability(
        _availability(
            evidence,
            CapabilityAvailabilityState.TEMPORARILY_UNAVAILABLE,
        )
    )
    decision = _resolve(journal, profile)
    assert not decision.allowed
    assert decision.lifecycle is CapabilityLifecycleState.REVALIDATION_REQUIRED
    assert decision.availability is CapabilityAvailabilityState.TEMPORARILY_UNAVAILABLE
    assert "product-owned upstream authority" in decision.reason
    assert decision.lifecycle is not CapabilityLifecycleState.REVOKED_OR_UNSUPPORTED


def test_later_unsupported_evidence_supersedes_historical_positive():
    first = _profile()
    positive = _evidence(first)
    journal = CapabilityEvidenceJournal()
    journal.publish(positive)

    second = _profile(
        state=BookmakerCapabilityState.UNSUPPORTED,
        observed_at="2026-09-21T10:05:00+00:00",
    )
    revoked = _evidence(
        second,
        support_state=BookmakerCapabilityState.UNSUPPORTED,
        observed_at=second.observed_at,
        committed_at="2026-09-21T10:06:00+00:00",
        review_due_at="2026-09-22T10:06:00+00:00",
        predecessor_id=positive.evidence_id,
    )
    journal.publish(revoked)
    journal.publish_availability(
        _availability(revoked, observed_at="2026-09-21T10:07:00+00:00")
    )
    decision = journal.resolve(
        _requirement(),
        {first.profile_id: first, second.profile_id: second},
        as_of="2026-09-21T10:08:00+00:00",
    )
    assert not decision.allowed
    assert decision.lifecycle is CapabilityLifecycleState.REVOKED_OR_UNSUPPORTED


def test_restart_roundtrip_preserves_age_and_identity():
    profile, evidence, journal = _journal()
    restored = CapabilityEvidenceJournal.from_json(journal.to_json())
    assert restored.to_json() == journal.to_json()
    decision = restored.resolve(
        _requirement(),
        {profile.profile_id: profile},
        as_of="2026-09-22T10:01:00+00:00",
    )
    assert decision.evidence_id == evidence.evidence_id
    assert decision.lifecycle is CapabilityLifecycleState.REVALIDATION_REQUIRED


def test_journal_json_rejects_boolean_schema_version_and_unknown_top_level_field():
    _, _, journal = _journal()

    boolean_version = json.loads(journal.to_json())
    boolean_version["schema_version"] = True
    with pytest.raises(CapabilityEvidenceError, match="unsupported journal schema"):
        CapabilityEvidenceJournal.from_json(json.dumps(boolean_version))

    widened = json.loads(journal.to_json())
    widened["future_authority"] = "caller-controlled"
    with pytest.raises(
        CapabilityEvidenceError,
        match="journal contains unexpected or missing fields",
    ):
        CapabilityEvidenceJournal.from_json(json.dumps(widened))


def test_journal_json_rejects_unknown_nested_authority_fields():
    _, _, journal = _journal()

    evidence_widened = json.loads(journal.to_json())
    evidence_widened["evidence"][0]["future_authority"] = True
    with pytest.raises(
        CapabilityEvidenceError,
        match="evidence contains unexpected or missing fields",
    ):
        CapabilityEvidenceJournal.from_json(json.dumps(evidence_widened))

    scope_widened = json.loads(journal.to_json())
    scope_widened["evidence"][0]["scope"]["future_scope"] = "widened"
    with pytest.raises(
        CapabilityEvidenceError,
        match="scope contains unexpected or missing fields",
    ):
        CapabilityEvidenceJournal.from_json(json.dumps(scope_widened))

    availability_widened = json.loads(journal.to_json())
    availability_widened["availability"][0]["future_health_authority"] = True
    with pytest.raises(
        CapabilityEvidenceError,
        match="availability contains unexpected or missing fields",
    ):
        CapabilityEvidenceJournal.from_json(json.dumps(availability_widened))


def test_journal_json_rejects_duplicate_evidence_and_availability_entries():
    _, _, journal = _journal()

    duplicate_evidence = json.loads(journal.to_json())
    duplicate_evidence["evidence"].append(dict(duplicate_evidence["evidence"][0]))
    with pytest.raises(CapabilityEvidenceError, match="duplicate evidence entries"):
        CapabilityEvidenceJournal.from_json(json.dumps(duplicate_evidence))

    duplicate_availability = json.loads(journal.to_json())
    duplicate_availability["availability"].append(
        dict(duplicate_availability["availability"][0])
    )
    with pytest.raises(CapabilityEvidenceError, match="duplicate availability entries"):
        CapabilityEvidenceJournal.from_json(json.dumps(duplicate_availability))


def test_journal_json_rejects_duplicate_object_keys_and_nonstandard_constants():
    _, _, journal = _journal()
    canonical = journal.to_json()

    duplicate_top_level = canonical.replace(
        '"schema_version":1',
        '"schema_version":1,"schema_version":1',
        1,
    )
    with pytest.raises(CapabilityEvidenceError, match="duplicate JSON object key"):
        CapabilityEvidenceJournal.from_json(duplicate_top_level)

    duplicate_nested = canonical.replace(
        '"venue_id":"betfair"',
        '"venue_id":"betfair","venue_id":"betfair"',
        1,
    )
    with pytest.raises(CapabilityEvidenceError, match="duplicate JSON object key"):
        CapabilityEvidenceJournal.from_json(duplicate_nested)

    nonstandard_constant = canonical.replace(
        '"schema_version":1',
        '"schema_version":NaN',
        1,
    )
    with pytest.raises(
        CapabilityEvidenceError,
        match="non-standard JSON constant: NaN",
    ):
        CapabilityEvidenceJournal.from_json(nonstandard_constant)


def test_missing_profile_after_restart_fails_closed():
    _, _, journal = _journal()
    decision = journal.resolve(
        _requirement(), {}, as_of="2026-09-21T10:03:00+00:00"
    )
    assert not decision.allowed
    assert "re-resolved" in decision.reason


def test_subclass_and_execution_authority_cannot_be_minted():
    class ForgedEvidence(CapabilityEvidence):
        pass

    profile = _profile()
    valid = _evidence(profile)
    forged = ForgedEvidence(**{
        field: getattr(valid, field)
        for field in valid.__dataclass_fields__
    })
    with pytest.raises(CapabilityEvidenceError, match="exact CapabilityEvidence"):
        CapabilityEvidenceJournal().publish(forged)
    with pytest.raises(CapabilityEvidenceError, match="real execution authority"):
        _evidence(profile, strength=CapabilityEvidenceStrength.EXECUTION_PROVEN)


def test_equivalent_commit_instant_spelling_cannot_bypass_conflict():
    profile = _profile()
    first = _evidence(profile)
    journal = CapabilityEvidenceJournal()
    journal.publish(first)
    equivalent_instant = _evidence(
        profile,
        committed_at="2026-09-21T12:01:00+02:00",
        review_due_at="2026-09-22T12:01:00+02:00",
        source_payload_sha256="b" * 64,
    )

    with pytest.raises(CapabilityEvidenceError, match="conflicting"):
        journal.publish(equivalent_instant)


def test_identical_revalidation_is_idempotent_but_same_time_conflict_fails():
    profile = _profile()
    evidence = _evidence(profile)
    journal = CapabilityEvidenceJournal()
    assert journal.publish(evidence) == evidence.evidence_id
    assert journal.publish(evidence) == evidence.evidence_id
    conflicting = _evidence(profile, source_payload_sha256="b" * 64)
    with pytest.raises(CapabilityEvidenceError, match="conflicting"):
        journal.publish(conflicting)


def test_credential_rotation_revalidation_can_link_predecessor_and_recover():
    first = _profile()
    old = _evidence(first)
    journal = CapabilityEvidenceJournal()
    journal.publish(old)

    second = _profile(observed_at="2026-09-21T10:05:00+00:00")
    fresh = _evidence(
        second,
        scope=_scope(credential="app-v2"),
        observed_at=second.observed_at,
        committed_at="2026-09-21T10:06:00+00:00",
        review_due_at="2026-09-22T10:06:00+00:00",
        predecessor_id=old.evidence_id,
    )
    journal.publish(fresh)
    journal.publish_availability(
        _availability(fresh, observed_at="2026-09-21T10:07:00+00:00")
    )
    decision = journal.resolve(
        _requirement(scope=_scope(credential="app-v2")),
        {first.profile_id: first, second.profile_id: second},
        as_of="2026-09-21T10:08:00+00:00",
    )
    assert not decision.allowed
    assert decision.lifecycle is CapabilityLifecycleState.REVALIDATION_REQUIRED
    assert "product-owned upstream authority" in decision.reason
    assert decision.evidence_id == fresh.evidence_id

def test_successor_cannot_recover_from_observation_not_newer_than_predecessor():
    first = _profile()
    positive = _evidence(first)
    journal = CapabilityEvidenceJournal()
    journal.publish(positive)

    revoked_profile = _profile(
        state=BookmakerCapabilityState.UNSUPPORTED,
        observed_at="2026-09-21T10:05:00+00:00",
    )
    revoked = _evidence(
        revoked_profile,
        support_state=BookmakerCapabilityState.UNSUPPORTED,
        observed_at=revoked_profile.observed_at,
        committed_at="2026-09-21T10:06:00+00:00",
        review_due_at="2026-09-22T10:06:00+00:00",
        predecessor_id=positive.evidence_id,
    )
    journal.publish(revoked)

    stale_recovery_profile = _profile(observed_at="2026-09-21T10:04:00+00:00")
    stale_recovery = _evidence(
        stale_recovery_profile,
        observed_at=stale_recovery_profile.observed_at,
        committed_at="2026-09-21T10:07:00+00:00",
        review_due_at="2026-09-22T10:07:00+00:00",
        predecessor_id=revoked.evidence_id,
    )
    with pytest.raises(
        CapabilityEvidenceError,
        match="successor evidence must be observed after predecessor",
    ):
        journal.publish(stale_recovery)

    same_time_profile = _profile(observed_at=revoked_profile.observed_at)
    same_time_recovery = _evidence(
        same_time_profile,
        observed_at=same_time_profile.observed_at,
        committed_at="2026-09-21T10:08:00+00:00",
        review_due_at="2026-09-22T10:08:00+00:00",
        predecessor_id=revoked.evidence_id,
    )
    with pytest.raises(
        CapabilityEvidenceError,
        match="successor evidence must be observed after predecessor",
    ):
        journal.publish(same_time_recovery)


def test_successor_must_link_exact_latest_same_scope_predecessor():
    first = _profile()
    positive = _evidence(first)
    journal = CapabilityEvidenceJournal()
    journal.publish(positive)

    revoked_profile = _profile(
        state=BookmakerCapabilityState.UNSUPPORTED,
        observed_at="2026-09-21T10:05:00+00:00",
    )
    revoked = _evidence(
        revoked_profile,
        support_state=BookmakerCapabilityState.UNSUPPORTED,
        observed_at=revoked_profile.observed_at,
        committed_at="2026-09-21T10:06:00+00:00",
        review_due_at="2026-09-22T10:06:00+00:00",
        predecessor_id=positive.evidence_id,
    )
    journal.publish(revoked)

    recovered_profile = _profile(observed_at="2026-09-21T10:10:00+00:00")
    common = {
        "observed_at": recovered_profile.observed_at,
        "committed_at": "2026-09-21T10:11:00+00:00",
        "review_due_at": "2026-09-22T10:11:00+00:00",
    }

    unlinked_recovery = _evidence(
        recovered_profile,
        predecessor_id=None,
        **common,
    )
    with pytest.raises(CapabilityEvidenceError, match="exact latest predecessor"):
        journal.publish(unlinked_recovery)

    stale_link_recovery = _evidence(
        recovered_profile,
        predecessor_id=positive.evidence_id,
        **common,
    )
    with pytest.raises(CapabilityEvidenceError, match="exact latest predecessor"):
        journal.publish(stale_link_recovery)

    linked_recovery = _evidence(
        recovered_profile,
        predecessor_id=revoked.evidence_id,
        **common,
    )
    assert journal.publish(linked_recovery) == linked_recovery.evidence_id

