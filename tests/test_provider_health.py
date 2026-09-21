from dataclasses import replace

import pytest

from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.provider_health import (
    ProviderHealthEvidence,
    ProviderHealthEvidenceError,
    ProviderHealthState,
    detect_capability_change,
)

_T0 = "2026-09-21T10:00:00+00:00"
_T1 = "2026-09-21T10:05:00+00:00"
_T2 = "2026-09-21T10:06:00+00:00"
_HASH_A = "a" * 64
_HASH_B = "b" * 64


def _profile(
    version: int,
    *,
    observed_at: str = _T0,
    account_id: str = "acct-1",
    adapter_id: str = "adapter-1",
    live: BookmakerCapabilityState = BookmakerCapabilityState.SUPPORTED,
    balance: BookmakerCapabilityState = BookmakerCapabilityState.SUPPORTED,
) -> BookmakerCapabilityProfile:
    return BookmakerCapabilityProfile(
        venue_id="betfair",
        account_id=account_id,
        adapter_id=adapter_id,
        adapter_version="1.0",
        profile_version=version,
        facts=(
            BookmakerCapabilityFact(BookmakerCapability.LIVE_QUOTES_READ, live),
            BookmakerCapabilityFact(BookmakerCapability.BALANCE_READ, balance),
        ),
        observed_at=observed_at,
        source_ref=f"profile-{version}",
        source_payload_sha256=_HASH_A,
    )


def _health(state: ProviderHealthState = ProviderHealthState.HEALTHY) -> ProviderHealthEvidence:
    return ProviderHealthEvidence(
        profile=_profile(1),
        state=state,
        since=_T0,
        observed_at=_T1,
        reason="bounded provider health observation",
        source_ref="health-probe",
        source_payload_sha256=_HASH_B,
    )


def test_healthy_observation_is_deterministic_but_never_positive_authority() -> None:
    first = _health()
    second = _health()
    assert first.evidence_id == second.evidence_id
    assert first.is_degraded is False
    assert first.certification_requalification_required is False
    assert first.provider_origin_proven is False
    assert first.healthy_authority is False
    assert first.provider_write_authorized is False
    assert first.execution_authorized is False
    assert first.real_money_execution is False


@pytest.mark.parametrize(
    "state",
    [state for state in ProviderHealthState if state is not ProviderHealthState.HEALTHY],
)
def test_every_nonhealthy_state_remains_explicitly_degraded(state: ProviderHealthState) -> None:
    profile = _profile(1)
    since = _T0
    observed_at = _T1
    kwargs = {}
    if state is ProviderHealthState.CAPABILITY_CHANGED:
        previous = _profile(1)
        profile = _profile(
            2,
            observed_at=_T1,
            live=BookmakerCapabilityState.UNSUPPORTED,
        )
        since = _T1
        observed_at = _T2
        kwargs = {
            "previous_profile_id": previous.profile_id,
            "changed_capabilities": (BookmakerCapability.LIVE_QUOTES_READ,),
            "previous_profile": previous,
        }
    evidence = ProviderHealthEvidence(
        profile=profile,
        state=state,
        since=since,
        observed_at=observed_at,
        reason="explicit degradation",
        source_ref="health-probe",
        source_payload_sha256=_HASH_B,
        **kwargs,
    )
    assert evidence.is_degraded is True
    assert evidence.certification_requalification_required is (
        state is ProviderHealthState.CAPABILITY_CHANGED
    )


def test_health_time_order_is_fail_closed() -> None:
    with pytest.raises(ProviderHealthEvidenceError, match="since"):
        replace(_health(), since=_T2, observed_at=_T1)
    with pytest.raises(ProviderHealthEvidenceError, match="predate"):
        replace(
            _health(),
            since="2026-09-21T09:59:00+00:00",
            observed_at="2026-09-21T09:59:59+00:00",
        )


def test_capability_changed_requires_exact_drift_metadata() -> None:
    previous = _profile(1)
    current = _profile(
        2,
        observed_at=_T1,
        live=BookmakerCapabilityState.UNSUPPORTED,
    )
    base = ProviderHealthEvidence(
        profile=current,
        state=ProviderHealthState.CAPABILITY_CHANGED,
        since=_T1,
        observed_at=_T2,
        reason="drift",
        source_ref="probe",
        source_payload_sha256=_HASH_B,
        previous_profile_id=previous.profile_id,
        changed_capabilities=(BookmakerCapability.LIVE_QUOTES_READ,),
        previous_profile=previous,
    )
    with pytest.raises(ProviderHealthEvidenceError, match="previous_profile_id"):
        replace(base, previous_profile_id=None)
    with pytest.raises(ProviderHealthEvidenceError, match="requires previous_profile"):
        replace(base, previous_profile=None)
    with pytest.raises(ProviderHealthEvidenceError, match="exact semantic profile delta"):
        replace(base, changed_capabilities=())
    with pytest.raises(ProviderHealthEvidenceError, match="only valid"):
        replace(_health(), previous_profile_id=previous.profile_id)
    with pytest.raises(ProviderHealthEvidenceError, match="only valid"):
        replace(_health(), previous_profile=previous)


def test_capability_change_metadata_is_canonical() -> None:
    previous = _profile(1)
    profile = _profile(
        2,
        observed_at=_T1,
        live=BookmakerCapabilityState.UNSUPPORTED,
        balance=BookmakerCapabilityState.UNSUPPORTED,
    )
    with pytest.raises(ProviderHealthEvidenceError, match="duplicates"):
        ProviderHealthEvidence(
            profile=profile,
            state=ProviderHealthState.CAPABILITY_CHANGED,
            since=_T1,
            observed_at=_T2,
            reason="drift",
            source_ref="probe",
            source_payload_sha256=_HASH_B,
            previous_profile_id=previous.profile_id,
            changed_capabilities=(
                BookmakerCapability.LIVE_QUOTES_READ,
                BookmakerCapability.LIVE_QUOTES_READ,
            ),
            previous_profile=previous,
        )
    with pytest.raises(ProviderHealthEvidenceError, match="sorted"):
        ProviderHealthEvidence(
            profile=profile,
            state=ProviderHealthState.CAPABILITY_CHANGED,
            since=_T1,
            observed_at=_T2,
            reason="drift",
            source_ref="probe",
            source_payload_sha256=_HASH_B,
            previous_profile_id=previous.profile_id,
            changed_capabilities=(
                BookmakerCapability.LIVE_QUOTES_READ,
                BookmakerCapability.BALANCE_READ,
            ),
            previous_profile=previous,
        )


def test_direct_capability_change_rejects_forged_previous_identity_and_delta() -> None:
    previous = _profile(1)
    current = _profile(
        2,
        observed_at=_T1,
        live=BookmakerCapabilityState.UNSUPPORTED,
    )
    with pytest.raises(ProviderHealthEvidenceError, match="must match"):
        ProviderHealthEvidence(
            profile=current,
            state=ProviderHealthState.CAPABILITY_CHANGED,
            since=_T1,
            observed_at=_T2,
            reason="drift",
            source_ref="probe",
            source_payload_sha256=_HASH_B,
            previous_profile_id="c" * 64,
            changed_capabilities=(BookmakerCapability.LIVE_QUOTES_READ,),
            previous_profile=previous,
        )
    with pytest.raises(ProviderHealthEvidenceError, match="exact semantic profile delta"):
        ProviderHealthEvidence(
            profile=current,
            state=ProviderHealthState.CAPABILITY_CHANGED,
            since=_T1,
            observed_at=_T2,
            reason="drift",
            source_ref="probe",
            source_payload_sha256=_HASH_B,
            previous_profile_id=previous.profile_id,
            changed_capabilities=(BookmakerCapability.BALANCE_READ,),
            previous_profile=previous,
        )


def test_direct_capability_change_requires_real_semantic_delta() -> None:
    previous = _profile(1)
    current = _profile(2, observed_at=_T1)
    with pytest.raises(ProviderHealthEvidenceError, match="no semantic"):
        ProviderHealthEvidence(
            profile=current,
            state=ProviderHealthState.CAPABILITY_CHANGED,
            since=_T1,
            observed_at=_T2,
            reason="fabricated drift",
            source_ref="probe",
            source_payload_sha256=_HASH_B,
            previous_profile_id=previous.profile_id,
            changed_capabilities=(BookmakerCapability.LIVE_QUOTES_READ,),
            previous_profile=previous,
        )


def test_direct_and_detected_capability_change_have_same_canonical_evidence() -> None:
    previous = _profile(1)
    current = _profile(
        2,
        observed_at=_T1,
        live=BookmakerCapabilityState.UNSUPPORTED,
    )
    direct = ProviderHealthEvidence(
        profile=current,
        state=ProviderHealthState.CAPABILITY_CHANGED,
        since=_T1,
        observed_at=_T2,
        reason="live quote capability was withdrawn",
        source_ref="capability-reprobe",
        source_payload_sha256=_HASH_B,
        previous_profile_id=previous.profile_id,
        changed_capabilities=(BookmakerCapability.LIVE_QUOTES_READ,),
        previous_profile=previous,
    )
    detected = detect_capability_change(
        previous,
        current,
        since=_T1,
        observed_at=_T2,
        reason="live quote capability was withdrawn",
        source_ref="capability-reprobe",
        source_payload_sha256=_HASH_B,
    )
    assert direct.to_canonical_dict() == detected.to_canonical_dict()
    assert direct.evidence_id == detected.evidence_id


def test_detect_capability_change_binds_exact_semantic_delta() -> None:
    previous = _profile(1)
    current = _profile(
        2,
        observed_at=_T1,
        live=BookmakerCapabilityState.UNSUPPORTED,
    )
    evidence = detect_capability_change(
        previous,
        current,
        since=_T1,
        observed_at=_T2,
        reason="live quote capability was withdrawn",
        source_ref="capability-reprobe",
        source_payload_sha256=_HASH_B,
    )
    assert evidence.state is ProviderHealthState.CAPABILITY_CHANGED
    assert evidence.previous_profile_id == previous.profile_id
    assert evidence.previous_profile == previous
    assert evidence.profile.profile_id == current.profile_id
    assert evidence.changed_capabilities == (BookmakerCapability.LIVE_QUOTES_READ,)
    assert evidence.certification_requalification_required is True


def test_detect_capability_change_ignores_nonsemantic_profile_identity_change() -> None:
    previous = _profile(1)
    current = _profile(2, observed_at=_T1)
    assert previous.profile_id != current.profile_id
    with pytest.raises(ProviderHealthEvidenceError, match="no semantic"):
        detect_capability_change(
            previous,
            current,
            since=_T1,
            observed_at=_T2,
            reason="reprobe only",
            source_ref="capability-reprobe",
            source_payload_sha256=_HASH_B,
        )


def test_detect_capability_change_rejects_cross_scope_and_nonmonotonic_version() -> None:
    previous = _profile(1)
    cross_account = _profile(
        2,
        observed_at=_T1,
        account_id="acct-2",
        live=BookmakerCapabilityState.UNSUPPORTED,
    )
    with pytest.raises(ProviderHealthEvidenceError, match="one exact"):
        detect_capability_change(
            previous,
            cross_account,
            since=_T1,
            observed_at=_T2,
            reason="drift",
            source_ref="probe",
            source_payload_sha256=_HASH_B,
        )

    same_version = _profile(
        1,
        observed_at=_T1,
        live=BookmakerCapabilityState.UNSUPPORTED,
    )
    with pytest.raises(ProviderHealthEvidenceError, match="profile_version"):
        detect_capability_change(
            previous,
            same_version,
            since=_T1,
            observed_at=_T2,
            reason="drift",
            source_ref="probe",
            source_payload_sha256=_HASH_B,
        )


def test_capability_change_cannot_be_backdated_before_current_profile() -> None:
    previous = _profile(1)
    current = _profile(
        2,
        observed_at=_T1,
        live=BookmakerCapabilityState.UNSUPPORTED,
    )
    with pytest.raises(ProviderHealthEvidenceError, match="backdated"):
        detect_capability_change(
            previous,
            current,
            since=_T0,
            observed_at=_T2,
            reason="drift",
            source_ref="probe",
            source_payload_sha256=_HASH_B,
        )


def test_profile_subclasses_do_not_gain_health_evidence_authority() -> None:
    class EvilProfile(BookmakerCapabilityProfile):
        pass

    base = _profile(1)
    evil = EvilProfile(
        venue_id=base.venue_id,
        account_id=base.account_id,
        adapter_id=base.adapter_id,
        adapter_version=base.adapter_version,
        profile_version=base.profile_version,
        facts=base.facts,
        observed_at=base.observed_at,
        source_ref=base.source_ref,
        source_payload_sha256=base.source_payload_sha256,
    )
    with pytest.raises(ProviderHealthEvidenceError, match="exact BookmakerCapabilityProfile"):
        replace(_health(), profile=evil)


def test_malformed_source_digest_fails_closed() -> None:
    with pytest.raises(ProviderHealthEvidenceError, match="SHA-256"):
        replace(_health(), source_payload_sha256="not-a-digest")
