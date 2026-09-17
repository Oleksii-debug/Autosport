import json
from dataclasses import replace

import pytest

from autosport.bookmaker_capability import (
    AutomationPermission,
    BookmakerCapabilityConflictError,
    BookmakerCapabilityError,
    BookmakerCapabilityProfile,
    BookmakerCapabilityRegistry,
    BookmakerCapabilityRegistryCorruptionError,
    BookmakerCapabilityUnavailableError,
    BookmakerGovernanceEvidence,
    CapabilityName,
    CapabilityState,
    IntegrationMode,
    TechnicalCapabilities,
)


T0 = "2026-09-17T16:00:00+00:00"
T1 = "2026-09-17T16:10:00+00:00"


def _profile(*, version=1, technical=None, sports=("tennis", "table_tennis")):
    return BookmakerCapabilityProfile(
        provider_id="book-1",
        adapter_id="adapter-1",
        account_scope="account-scope-1",
        profile_version=version,
        observed_at_utc=T0 if version == 1 else T1,
        source_ref=f"capability-probe:{version}",
        integration_mode=IntegrationMode.OFFICIAL_API,
        supported_sports=sports,
        supported_markets=("match_winner", "totals"),
        technical=technical or TechnicalCapabilities(),
    )


def test_profile_identity_is_canonical_and_order_independent():
    left = _profile(sports=("tennis", "table_tennis"))
    right = _profile(sports=("table_tennis", "tennis"))
    assert left.supported_sports == ("table_tennis", "tennis")
    assert left.profile_id == right.profile_id
    assert BookmakerCapabilityProfile.from_payload(left.to_payload()) == left


def test_unknown_capability_fails_closed_and_supported_read_passes(tmp_path):
    path = tmp_path / "bookmaker_capabilities.json"
    registry = BookmakerCapabilityRegistry(path)
    registry.register_profile(_profile())

    with pytest.raises(BookmakerCapabilityUnavailableError, match="not proven supported"):
        registry.require_read_supported(
            "book-1", "account-scope-1", CapabilityName.BALANCE
        )

    supported = _profile(
        version=2,
        technical=TechnicalCapabilities(
            account_identity=CapabilityState.SUPPORTED,
            balance=CapabilityState.SUPPORTED,
            limits=CapabilityState.SUPPORTED,
        ),
    )
    registry.register_profile(supported)
    assert (
        registry.require_read_supported(
            "book-1", "account-scope-1", CapabilityName.BALANCE
        ).profile_id
        == supported.profile_id
    )


def test_action_capability_never_passes_read_guard(tmp_path):
    registry = BookmakerCapabilityRegistry(tmp_path / "bookmaker_capabilities.json")
    profile = _profile(
        technical=TechnicalCapabilities(place_bet=CapabilityState.SUPPORTED),
    )
    registry.register_profile(profile)

    with pytest.raises(BookmakerCapabilityError, match="not a read-only capability"):
        registry.require_read_supported(
            "book-1", "account-scope-1", CapabilityName.PLACE_BET
        )


def test_governance_evidence_is_separate_from_technical_support(tmp_path):
    registry = BookmakerCapabilityRegistry(tmp_path / "bookmaker_capabilities.json")
    technical = _profile(
        technical=TechnicalCapabilities(
            live_quotes=CapabilityState.SUPPORTED,
            place_bet=CapabilityState.SUPPORTED,
        )
    )
    registry.register_profile(technical)
    assert registry.latest_governance("book-1", "account-scope-1") is None

    governance = BookmakerGovernanceEvidence(
        provider_id="book-1",
        account_scope="account-scope-1",
        governance_version=1,
        jurisdiction="SK",
        terms_ref="terms:v2026-09-01",
        evidence_ref="lawful-review:case-1",
        observed_at_utc=T0,
        automation_permission=AutomationPermission.READ_ONLY_PERMITTED,
    )
    assert registry.register_governance(governance) is True
    assert (
        registry.latest_governance(
            "book-1", "account-scope-1"
        ).automation_permission
        is AutomationPermission.READ_ONLY_PERMITTED
    )
    assert technical.technical.place_bet is CapabilityState.SUPPORTED


def test_registry_reopen_is_durable_and_exact_replay_is_idempotent(tmp_path):
    path = tmp_path / "bookmaker_capabilities.json"
    first = BookmakerCapabilityRegistry(path)
    profile = _profile(
        technical=TechnicalCapabilities(live_quotes=CapabilityState.SUPPORTED)
    )
    assert first.register_profile(profile) is True
    assert first.register_profile(profile) is False

    reopened = BookmakerCapabilityRegistry(path)
    assert reopened.profiles == (profile,)
    assert reopened.latest_profile("book-1", "account-scope-1") == profile


def test_same_profile_version_cannot_be_rebound(tmp_path):
    registry = BookmakerCapabilityRegistry(tmp_path / "bookmaker_capabilities.json")
    registry.register_profile(_profile())
    conflict = replace(_profile(), source_ref="different-proof")

    with pytest.raises(BookmakerCapabilityConflictError, match="different evidence"):
        registry.register_profile(conflict)


def test_tampered_profile_identity_is_rejected_on_reopen(tmp_path):
    path = tmp_path / "bookmaker_capabilities.json"
    registry = BookmakerCapabilityRegistry(path)
    registry.register_profile(_profile())
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["profiles"][0]["source_ref"] = "tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BookmakerCapabilityRegistryCorruptionError):
        BookmakerCapabilityRegistry(path)


def test_duplicate_json_key_is_rejected(tmp_path):
    path = tmp_path / "bookmaker_capabilities.json"
    path.write_text(
        '{"schema_version":1,"profiles":[],"profiles":[],"governance":[]}',
        encoding="utf-8",
    )
    with pytest.raises(
        BookmakerCapabilityRegistryCorruptionError, match="duplicate JSON object key"
    ):
        BookmakerCapabilityRegistry(path)
