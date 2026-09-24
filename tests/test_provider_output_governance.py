import json
from dataclasses import replace
from itertools import permutations

import pytest

from autosport.provider_output_governance import (
    ProviderOutputGovernanceAuthority,
    ProviderOutputGrant,
    ProviderOutputUseDecision,
    ProviderOutputUseRequest,
    RetentionPolicy,
    decide_provider_output_use,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
T0 = "2026-09-01T00:00:00Z"
T1 = "2026-09-10T12:00:00Z"
T2 = "2026-10-01T00:00:00Z"


def grants():
    return (
        ProviderOutputGrant("research", "odds_snapshot", RetentionPolicy.BOUNDED, 86400),
        ProviderOutputGrant("paper_decision", "odds_snapshot", RetentionPolicy.UNBOUNDED),
        ProviderOutputGrant("transient_display", "account_balance", RetentionPolicy.FORBIDDEN),
    )


def authority(**changes):
    values = dict(
        provider_id="provider-a",
        service_id="market-data",
        authority_version="owner-v1",
        terms_reference="https://provider.example/legal/terms",
        terms_sha256=SHA_A,
        owner_approval_reference="urn:autosport:owner-approval:2026-09-01",
        owner_approval_sha256=SHA_B,
        valid_from=T0,
        valid_until=T2,
        grants=grants(),
    )
    values.update(changes)
    return ProviderOutputGovernanceAuthority(**values)


def request(auth=None, **changes):
    auth = auth or authority()
    values = dict(
        authority_id=auth.authority_id,
        provider_id=auth.provider_id,
        service_id=auth.service_id,
        artifact_sha256=SHA_C,
        purpose="research",
        artifact_class="odds_snapshot",
        acquired_at=T1,
        requested_retain_until="2026-09-11T12:00:00Z",
    )
    values.update(changes)
    return ProviderOutputUseRequest(**values)


def test_grant_permutations_have_one_identity_and_wire_form():
    ids = set()
    wires = set()
    for ordered in permutations(grants()):
        item = authority(grants=tuple(ordered))
        ids.add(item.authority_id)
        wires.add(item.to_json())
    assert len(ids) == 1
    assert len(wires) == 1


def test_timezone_aliases_normalize_to_one_identity():
    a = authority(valid_from="2026-09-01T02:00:00+02:00", valid_until="2026-10-01T02:00:00+02:00")
    b = authority(valid_from="2026-09-01T00:00:00Z", valid_until="2026-10-01T00:00:00+00:00")
    assert a.authority_id == b.authority_id
    assert a.valid_from == "2026-09-01T00:00:00Z"
    assert a.valid_until == "2026-10-01T00:00:00Z"


def test_terms_reference_must_be_public_https_and_secret_free():
    with pytest.raises(ValueError, match="HTTPS"):
        authority(terms_reference="http://provider.example/terms")
    with pytest.raises(ValueError, match="credentials"):
        authority(terms_reference="https://user:pass@provider.example/terms")
    with pytest.raises(ValueError, match="query or fragment"):
        authority(terms_reference="https://provider.example/terms?token=secret")


def test_owner_approval_reference_rejects_query_fragment_and_unsafe_reference_forms():
    with pytest.raises(ValueError, match="query or fragment"):
        authority(owner_approval_reference="urn:approval:1#secret")
    with pytest.raises(ValueError, match="absolute reference"):
        authority(owner_approval_reference="bare-secret-token")
    with pytest.raises(ValueError, match="HTTPS or URN"):
        authority(owner_approval_reference="file:///C:/private/approval.txt")
    with pytest.raises(ValueError, match="credentials"):
        authority(owner_approval_reference="https://user:pass@example.com/approval")


def test_authority_requires_half_open_nonempty_validity_interval():
    with pytest.raises(ValueError, match="later"):
        authority(valid_until=T0)


def test_bounded_grant_requires_positive_integer_horizon():
    with pytest.raises(ValueError, match="positive"):
        ProviderOutputGrant("research", "odds_snapshot", RetentionPolicy.BOUNDED, 0)
    with pytest.raises(ValueError, match="positive"):
        ProviderOutputGrant("research", "odds_snapshot", RetentionPolicy.BOUNDED, True)


def test_bounded_grant_rejects_unrepresentable_horizon():
    with pytest.raises(ValueError, match="supported datetime range"):
        ProviderOutputGrant(
            "research",
            "odds_snapshot",
            RetentionPolicy.BOUNDED,
            10**30,
        )


def test_nonbounded_grants_reject_max_retention_seconds():
    with pytest.raises(ValueError, match="only"):
        ProviderOutputGrant("research", "odds_snapshot", RetentionPolicy.UNBOUNDED, 1)
    with pytest.raises(ValueError, match="only"):
        ProviderOutputGrant("research", "odds_snapshot", RetentionPolicy.FORBIDDEN, 1)


def test_duplicate_grant_key_is_rejected_even_when_values_match():
    duplicate = ProviderOutputGrant("research", "odds_snapshot", RetentionPolicy.BOUNDED, 86400)
    with pytest.raises(ValueError, match="ambiguous duplicate"):
        authority(grants=(duplicate, duplicate))


def test_round_trip_requires_exact_canonical_json():
    original = authority()
    restored = ProviderOutputGovernanceAuthority.from_json(original.to_json())
    assert restored == original
    assert restored.authority_id == original.authority_id


def test_reconstruction_rejects_duplicate_json_keys():
    raw = authority().to_json()
    tampered = raw.replace('"provider_id":"provider-a"', '"provider_id":"provider-a","provider_id":"provider-a"')
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        ProviderOutputGovernanceAuthority.from_json(tampered)


def test_reconstruction_rejects_noncanonical_whitespace():
    payload = json.loads(authority().to_json())
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    with pytest.raises(ValueError, match="not canonical"):
        ProviderOutputGovernanceAuthority.from_json(raw)


def test_reconstruction_rejects_schema_drift():
    payload = json.loads(authority().to_json())
    payload["unexpected"] = True
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with pytest.raises(ValueError, match="schema fields"):
        ProviderOutputGovernanceAuthority.from_json(raw)


def test_reconstruction_rejects_noncanonical_grant_order():
    payload = json.loads(authority().to_json())
    payload["grants"] = list(reversed(payload["grants"]))
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with pytest.raises(ValueError, match="canonical purpose/artifact order"):
        ProviderOutputGovernanceAuthority.from_json(raw)


def test_reconstruction_rejects_tampered_authority_id():
    payload = json.loads(authority().to_json())
    payload["authority_id"] = "f" * 64
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with pytest.raises(ValueError, match="authority_id"):
        ProviderOutputGovernanceAuthority.from_json(raw)


def test_exact_grant_remains_blocked_without_durable_owner_approval_resolution():
    auth = authority()
    outcome = decide_provider_output_use(auth, request(auth), decided_at=T1)
    assert outcome.allowed is False
    assert outcome.reason == "OWNER_APPROVAL_UNRESOLVED"
    assert outcome.retention_policy is RetentionPolicy.BOUNDED
    assert outcome.max_retention_seconds == 86400


def test_positive_decision_cannot_be_minted_with_public_constructor():
    auth = authority()
    with pytest.raises(TypeError, match="product-issued"):
        ProviderOutputUseDecision(
            allowed=True,
            reason="ALLOWED",
            authority_id=auth.authority_id,
            artifact_sha256=SHA_C,
            purpose="training",
            artifact_class="raw_private_output",
            decided_at=T1,
            retention_policy=RetentionPolicy.UNBOUNDED,
            max_retention_seconds=None,
        )


def test_caller_authored_owner_approval_reference_cannot_mint_allowed():
    forged = authority(
        owner_approval_reference="urn:autosport:owner-approval:forged",
        owner_approval_sha256="f" * 64,
        grants=(
            ProviderOutputGrant(
                "training",
                "raw_private_output",
                RetentionPolicy.UNBOUNDED,
            ),
        ),
    )
    req = request(
        forged,
        purpose="training",
        artifact_class="raw_private_output",
        requested_retain_until=None,
    )
    outcome = decide_provider_output_use(forged, req, decided_at=T1)
    assert outcome.allowed is False
    assert outcome.reason == "OWNER_APPROVAL_UNRESOLVED"
    assert outcome.authority_id == forged.authority_id
    assert outcome.retention_policy is RetentionPolicy.UNBOUNDED


def test_authority_id_mismatch_fails_closed():
    auth = authority()
    outcome = decide_provider_output_use(auth, request(auth, authority_id="d" * 64), decided_at=T1)
    assert outcome.allowed is False
    assert outcome.reason == "AUTHORITY_ID_MISMATCH"


def test_provider_or_service_alias_fails_closed():
    auth = authority()
    assert decide_provider_output_use(auth, request(auth, provider_id="provider-b"), decided_at=T1).reason == "PROVIDER_SERVICE_MISMATCH"
    assert decide_provider_output_use(auth, request(auth, service_id="other"), decided_at=T1).reason == "PROVIDER_SERVICE_MISMATCH"


def test_acquisition_before_or_at_end_of_authority_is_rejected():
    auth = authority()
    before = request(auth, acquired_at="2026-08-31T23:59:59Z", requested_retain_until=None)
    at_end = request(auth, acquired_at=T2, requested_retain_until=None)
    assert decide_provider_output_use(auth, before, decided_at=T1).reason == "ACQUISITION_OUTSIDE_AUTHORITY_VALIDITY"
    assert decide_provider_output_use(auth, at_end, decided_at="2026-10-01T00:00:01Z").reason == "ACQUISITION_OUTSIDE_AUTHORITY_VALIDITY"


def test_decision_before_acquisition_fails_closed():
    auth = authority()
    outcome = decide_provider_output_use(auth, request(auth, requested_retain_until=None), decided_at="2026-09-10T11:59:59Z")
    assert outcome.reason == "DECISION_PRECEDES_ACQUISITION"


def test_expired_authority_at_decision_fails_closed():
    auth = authority()
    outcome = decide_provider_output_use(auth, request(auth, requested_retain_until=None), decided_at=T2)
    assert outcome.reason == "AUTHORITY_NOT_ACTIVE_AT_DECISION"


def test_missing_exact_purpose_artifact_grant_is_not_inferred():
    auth = authority()
    outcome = decide_provider_output_use(auth, request(auth, purpose="training"), decided_at=T1)
    assert outcome.allowed is False
    assert outcome.reason == "NO_EXACT_GRANT"


def test_forbidden_retention_transient_match_still_needs_owner_approval_resolution():
    auth = authority()
    transient = request(
        auth,
        purpose="transient_display",
        artifact_class="account_balance",
        requested_retain_until=None,
    )
    persisted = replace(transient, requested_retain_until="2026-09-10T12:00:01Z")
    transient_outcome = decide_provider_output_use(auth, transient, decided_at=T1)
    assert transient_outcome.allowed is False
    assert transient_outcome.reason == "OWNER_APPROVAL_UNRESOLVED"
    assert transient_outcome.retention_policy is RetentionPolicy.FORBIDDEN
    outcome = decide_provider_output_use(auth, persisted, decided_at=T1)
    assert outcome.allowed is False
    assert outcome.reason == "PERSISTENCE_FORBIDDEN"


def test_bounded_retention_rejects_one_microsecond_over_horizon():
    auth = authority()
    too_long = request(auth, requested_retain_until="2026-09-11T12:00:00.000001Z")
    outcome = decide_provider_output_use(auth, too_long, decided_at=T1)
    assert outcome.allowed is False
    assert outcome.reason == "RETENTION_HORIZON_EXCEEDS_GRANT"


def test_bounded_retention_near_datetime_max_fails_closed_without_overflow():
    long_grant = ProviderOutputGrant(
        "research",
        "odds_snapshot",
        RetentionPolicy.BOUNDED,
        172800,
    )
    auth = authority(
        valid_from="9999-12-29T00:00:00Z",
        valid_until="9999-12-31T23:59:59Z",
        grants=(long_grant,),
    )
    req = request(
        auth,
        acquired_at="9999-12-30T12:00:00Z",
        requested_retain_until="9999-12-31T12:00:00Z",
    )
    outcome = decide_provider_output_use(
        auth,
        req,
        decided_at="9999-12-30T12:00:00Z",
    )
    assert outcome.allowed is False
    assert outcome.reason == "OWNER_APPROVAL_UNRESOLVED"


def test_unbounded_retention_match_still_needs_owner_approval_resolution():
    auth = authority()
    req = request(
        auth,
        purpose="paper_decision",
        requested_retain_until="2026-09-30T00:00:00Z",
    )
    outcome = decide_provider_output_use(auth, req, decided_at=T1)
    assert outcome.allowed is False
    assert outcome.reason == "OWNER_APPROVAL_UNRESOLVED"
    assert outcome.retention_policy is RetentionPolicy.UNBOUNDED


def test_retain_until_before_decision_is_rejected():
    auth = authority()
    req = request(auth, requested_retain_until="2026-09-10T11:59:59Z")
    outcome = decide_provider_output_use(auth, req, decided_at=T1)
    assert outcome.reason == "RETENTION_HORIZON_PRECEDES_DECISION"


def test_uppercase_sha_inputs_normalize_without_identity_change():
    lower = authority()
    upper = authority(terms_sha256=SHA_A.upper(), owner_approval_sha256=SHA_B.upper())
    assert lower.authority_id == upper.authority_id


def test_request_identity_fields_reject_noncanonical_types_and_bad_hashes():
    auth = authority()
    with pytest.raises(ValueError, match="SHA-256"):
        request(auth, artifact_sha256="bad")
    with pytest.raises(ValueError, match="canonical string"):
        request(auth, purpose=" research")


def test_governance_decision_never_exposes_execution_or_settlement_authority():
    auth = authority()
    outcome = decide_provider_output_use(auth, request(auth), decided_at=T1)
    assert outcome.allowed is False
    assert outcome.reason == "OWNER_APPROVAL_UNRESOLVED"
    for forbidden in (
        "execution_authorized",
        "provider_write_authorized",
        "settlement_authorized",
        "learning_outcome_authorized",
        "real_money_execution",
    ):
        assert not hasattr(outcome, forbidden)
