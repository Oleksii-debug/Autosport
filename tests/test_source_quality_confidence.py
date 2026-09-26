from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import random

import pytest

import autosport.source_quality_confidence as source_quality_module
from autosport.source_quality_confidence import (
    ConfidenceAction,
    SourceClass,
    SourceQualityAssessment,
    SourceQualityObservation,
    SourceQualityPolicy,
    assess_source_quality,
    validate_independent_corroborators,
)

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
DIGEST = "a" * 64


def policy(**overrides):
    values = dict(
        max_age=timedelta(seconds=30),
        accept_confidence=Decimal("0.80"),
        downweight_confidence=Decimal("0.50"),
        schema_version=1,
    )
    values.update(overrides)
    return SourceQualityPolicy(**values)


def obs(**overrides):
    values = dict(
        provider_id="provider-a",
        source_id="source-a",
        evidence_id="evidence-a",
        source_class=SourceClass.OFFICIAL_API,
        observed_at=NOW - timedelta(seconds=1),
        base_confidence=Decimal("0.90"),
        schema_version=1,
        transport_verified=True,
        provenance_bound=True,
        provenance_sha256=DIGEST,
        corroborator_ids=(),
    )
    values.update(overrides)
    return SourceQualityObservation(**values)


def test_official_api_high_confidence_fails_closed_without_authority_resolver():
    result = assess_source_quality(obs(base_confidence=Decimal("1")), now=NOW, policy=policy())
    assert result.action is ConfidenceAction.ABSTAIN
    assert result.reasons == ("OFFICIAL_API_AUTHORITY_UNRESOLVED",)


def test_official_api_mid_confidence_also_fails_closed_without_authority_resolver():
    result = assess_source_quality(obs(base_confidence=Decimal("0.79")), now=NOW, policy=policy())
    assert result.action is ConfidenceAction.ABSTAIN
    assert result.reasons == ("OFFICIAL_API_AUTHORITY_UNRESOLVED",)


def test_no_caller_callable_official_issuance_boundary_exists():
    assert not hasattr(source_quality_module, "_issue_from_authenticated_provider_evidence")
    assert not hasattr(source_quality_module, "_SOURCE_QUALITY_AUTHORITY")


def test_observation_has_no_authority_marker_field():
    assert "_authority_marker" not in SourceQualityObservation.__dataclass_fields__
    with pytest.raises(TypeError):
        SourceQualityObservation(
            provider_id="provider-a",
            source_id="source-a",
            evidence_id="evidence-a",
            source_class=SourceClass.OFFICIAL_API,
            observed_at=NOW,
            base_confidence=Decimal("1"),
            schema_version=1,
            transport_verified=True,
            provenance_bound=True,
            provenance_sha256=DIGEST,
            _authority_marker=object(),
        )


def test_arbitrary_module_marker_cannot_promote_official_api(monkeypatch):
    monkeypatch.setattr(source_quality_module, "_SOURCE_QUALITY_AUTHORITY", object(), raising=False)
    monkeypatch.setattr(source_quality_module, "is_product_issued", True, raising=False)
    result = assess_source_quality(obs(base_confidence=Decimal("1")), now=NOW, policy=policy())
    assert result.action is ConfidenceAction.ABSTAIN


def test_accept_enum_remains_forward_compatible_but_is_not_currently_mintable():
    assert ConfidenceAction.ACCEPT.value == "ACCEPT"
    assert all(
        assess_source_quality(
            obs(source_class=source_class, base_confidence=confidence),
            now=NOW,
            policy=policy(),
        ).action is not ConfidenceAction.ACCEPT
        for source_class in SourceClass
        for confidence in (Decimal("0"), Decimal("0.49"), Decimal("0.50"), Decimal("0.80"), Decimal("1"))
    )


def test_direct_assessment_construction_cannot_mint_accept():
    with pytest.raises(ValueError, match="canonical provider authority"):
        SourceQualityAssessment(
            action=ConfidenceAction.ACCEPT,
            input_confidence=Decimal("1"),
            reasons=("FORGED_ACCEPT",),
            corroborated=False,
        )


def test_direct_assessment_construction_cannot_mint_corroboration():
    with pytest.raises(ValueError, match="canonical corroborator authority"):
        SourceQualityAssessment(
            action=ConfidenceAction.DOWNWEIGHT,
            input_confidence=Decimal("0.75"),
            reasons=("FORGED_CORROBORATION",),
            corroborated=True,
        )


def test_below_downweight_floor_abstains_for_non_official_source():
    result = assess_source_quality(
        obs(source_class=SourceClass.BROWSER_ADAPTER, base_confidence=Decimal("0.49")),
        now=NOW,
        policy=policy(),
    )
    assert result.action is ConfidenceAction.ABSTAIN
    assert result.reasons == ("CONFIDENCE_BELOW_DOWNWEIGHT_FLOOR",)


@pytest.mark.parametrize("source_class", [SourceClass.BROWSER_ADAPTER, SourceClass.MANUAL])
def test_lower_authority_sources_are_bounded_to_downweight(source_class):
    result = assess_source_quality(obs(source_class=source_class, base_confidence=Decimal("1")), now=NOW, policy=policy())
    assert result.action is ConfidenceAction.DOWNWEIGHT
    assert result.reasons == (f"{source_class.value}_CANNOT_MINT_ACCEPT",)


def test_downweight_preserves_only_explicit_input_confidence_not_effective_score():
    result = assess_source_quality(
        obs(source_class=SourceClass.BROWSER_ADAPTER, base_confidence=Decimal("1")),
        now=NOW,
        policy=policy(),
    )
    assert result.action is ConfidenceAction.DOWNWEIGHT
    assert result.input_confidence == Decimal("1")
    assert not hasattr(result, "effective_confidence")


def test_stale_observation_abstains():
    result = assess_source_quality(obs(observed_at=NOW - timedelta(seconds=31)), now=NOW, policy=policy())
    assert result.action is ConfidenceAction.ABSTAIN
    assert "STALE_OBSERVATION" in result.reasons


def test_exact_max_age_is_not_stale_but_official_still_unresolved():
    result = assess_source_quality(obs(observed_at=NOW - timedelta(seconds=30)), now=NOW, policy=policy())
    assert result.action is ConfidenceAction.ABSTAIN
    assert result.reasons == ("OFFICIAL_API_AUTHORITY_UNRESOLVED",)


def test_exact_max_age_browser_can_downweight():
    result = assess_source_quality(
        obs(source_class=SourceClass.BROWSER_ADAPTER, observed_at=NOW - timedelta(seconds=30)),
        now=NOW,
        policy=policy(),
    )
    assert result.action is ConfidenceAction.DOWNWEIGHT


def test_future_observation_abstains():
    result = assess_source_quality(obs(observed_at=NOW + timedelta(microseconds=1)), now=NOW, policy=policy())
    assert result.action is ConfidenceAction.ABSTAIN
    assert "FUTURE_OBSERVATION" in result.reasons


def test_schema_mismatch_abstains():
    result = assess_source_quality(obs(schema_version=2), now=NOW, policy=policy())
    assert result.action is ConfidenceAction.ABSTAIN
    assert "SCHEMA_VERSION_MISMATCH" in result.reasons


@pytest.mark.parametrize("value", [True, 1.0, 0, -1])
def test_invalid_observation_schema_version_rejected(value):
    with pytest.raises(ValueError):
        obs(schema_version=value)


def test_unverified_transport_abstains():
    result = assess_source_quality(obs(transport_verified=False), now=NOW, policy=policy())
    assert result.action is ConfidenceAction.ABSTAIN
    assert "TRANSPORT_UNVERIFIED" in result.reasons


def test_unbound_provenance_abstains():
    result = assess_source_quality(obs(provenance_bound=False), now=NOW, policy=policy())
    assert result.action is ConfidenceAction.ABSTAIN
    assert "PROVENANCE_UNBOUND" in result.reasons


def test_multiple_hard_failures_are_all_reported_in_stable_order():
    result = assess_source_quality(
        obs(schema_version=2, transport_verified=False, provenance_bound=False),
        now=NOW,
        policy=policy(),
    )
    assert result.action is ConfidenceAction.ABSTAIN
    assert result.reasons == (
        "SCHEMA_VERSION_MISMATCH",
        "TRANSPORT_UNVERIFIED",
        "PROVENANCE_UNBOUND",
    )


def test_hard_failure_precedes_unresolved_official_authority():
    result = assess_source_quality(obs(transport_verified=False), now=NOW, policy=policy())
    assert result.reasons == ("TRANSPORT_UNVERIFIED",)


def test_corroboration_never_rewrites_input_confidence():
    observation = obs(
        source_class=SourceClass.BROWSER_ADAPTER,
        base_confidence=Decimal("0.51"),
        corroborator_ids=("peer-1", "peer-2"),
    )
    result = assess_source_quality(observation, now=NOW, policy=policy())
    assert result.input_confidence == Decimal("0.51")
    assert result.action is ConfidenceAction.DOWNWEIGHT


def test_corroboration_never_resolves_official_authority():
    result = assess_source_quality(
        obs(base_confidence=Decimal("1"), corroborator_ids=("peer-1", "peer-2")),
        now=NOW,
        policy=policy(),
    )
    assert result.action is ConfidenceAction.ABSTAIN
    assert result.reasons == ("OFFICIAL_API_AUTHORITY_UNRESOLVED",)


def test_caller_declared_corroborators_do_not_mint_positive_truth():
    result = assess_source_quality(obs(corroborator_ids=("peer-1",)), now=NOW, policy=policy())
    assert result.corroborated is False


def test_no_corroboration_is_false():
    assert assess_source_quality(obs(), now=NOW, policy=policy()).corroborated is False


def test_duplicate_corroborators_rejected():
    with pytest.raises(ValueError):
        obs(corroborator_ids=("peer-1", "peer-1"))


@pytest.mark.parametrize("identity", ["provider-a", "source-a", "evidence-a"])
def test_self_corroboration_rejected(identity):
    with pytest.raises(ValueError):
        obs(corroborator_ids=(identity,))


def test_corroborator_count_cannot_be_minted_as_an_input():
    assert "corroboration_count" not in SourceQualityObservation.__dataclass_fields__


def test_allowed_corroborators_only_narrow_explicit_set():
    observation = obs(corroborator_ids=("peer-1", "peer-2", "peer-3"))
    assert validate_independent_corroborators(observation, ["peer-3", "peer-1"]) == ("peer-1", "peer-3")


@pytest.mark.parametrize("container", ["xyz", ["peer-1"], {"peer-1"}])
def test_corroborator_ids_require_exact_tuple(container):
    with pytest.raises(ValueError, match="exact tuple"):
        obs(corroborator_ids=container)


def test_allowed_corroborators_reject_text_iterable():
    observation = obs(corroborator_ids=("x",))
    with pytest.raises(ValueError, match="not text"):
        validate_independent_corroborators(observation, "x")


def test_allowed_corroborators_reject_non_iterable():
    observation = obs(corroborator_ids=("x",))
    with pytest.raises(ValueError, match="iterable"):
        validate_independent_corroborators(observation, 7)


def test_allowed_corroborators_reject_non_text_member():
    observation = obs(corroborator_ids=("x",))
    with pytest.raises(ValueError, match="trimmed text"):
        validate_independent_corroborators(observation, [7])


def test_invalid_digest_rejected():
    with pytest.raises(ValueError):
        obs(provenance_sha256="not-a-digest")


def test_uppercase_digest_is_normalized():
    assert obs(provenance_sha256="A" * 64).provenance_sha256 == "a" * 64


def test_naive_observation_time_rejected():
    with pytest.raises(ValueError):
        obs(observed_at=datetime(2026, 9, 21, 12, 0))


def test_naive_now_rejected():
    with pytest.raises(ValueError):
        assess_source_quality(obs(), now=datetime(2026, 9, 21, 12, 0), policy=policy())


def test_non_observation_rejected():
    with pytest.raises(TypeError, match="observation"):
        assess_source_quality(object(), now=NOW, policy=policy())


def test_non_policy_rejected():
    with pytest.raises(TypeError, match="policy"):
        assess_source_quality(obs(), now=NOW, policy=object())


def test_non_decimal_confidence_rejected():
    with pytest.raises(ValueError):
        obs(base_confidence=0.9)


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), Decimal("-0.01"), Decimal("1.01")])
def test_invalid_probability_rejected(value):
    with pytest.raises(ValueError):
        obs(base_confidence=value)


def test_policy_rejects_nonpositive_max_age():
    with pytest.raises(ValueError):
        policy(max_age=timedelta(0))


def test_policy_rejects_inverted_thresholds():
    with pytest.raises(ValueError):
        policy(accept_confidence=Decimal("0.60"), downweight_confidence=Decimal("0.70"))


@pytest.mark.parametrize("value", [True, 0, -1, 1.0])
def test_policy_rejects_invalid_schema_version(value):
    with pytest.raises(ValueError):
        policy(schema_version=value)


def test_text_identity_must_be_trimmed_nonempty():
    with pytest.raises(ValueError):
        obs(provider_id=" provider-a ")


def test_assessment_is_deterministic_for_identical_input():
    observation = obs(
        source_class=SourceClass.BROWSER_ADAPTER,
        base_confidence=Decimal("0.73"),
        corroborator_ids=("peer-1",),
    )
    first = assess_source_quality(observation, now=NOW, policy=policy())
    second = assess_source_quality(observation, now=NOW, policy=policy())
    assert first == second


def test_randomized_fail_closed_invariants_50000_cases():
    rng = random.Random(0xB05)
    p = policy()

    for i in range(50_000):
        confidence = Decimal(rng.randrange(0, 101)) / Decimal(100)
        source_class = rng.choice(list(SourceClass))
        transport_verified = bool(rng.getrandbits(1))
        provenance_bound = bool(rng.getrandbits(1))
        schema_version = rng.choice([1, 1, 1, 2])
        age_seconds = rng.randrange(-5, 61)
        corroborators = tuple(f"peer-{j}" for j in range(rng.randrange(0, 4)))

        observation = obs(
            evidence_id=f"evidence-{i}",
            source_class=source_class,
            observed_at=NOW - timedelta(seconds=age_seconds),
            base_confidence=confidence,
            schema_version=schema_version,
            transport_verified=transport_verified,
            provenance_bound=provenance_bound,
            corroborator_ids=corroborators,
        )
        result = assess_source_quality(observation, now=NOW, policy=p)

        assert result.action is not ConfidenceAction.ACCEPT
        assert result.input_confidence == confidence
        assert result.corroborated is False
        assert result == assess_source_quality(observation, now=NOW, policy=p)

        hard_invalid = (
            schema_version != 1
            or not transport_verified
            or not provenance_bound
            or age_seconds < 0
            or age_seconds > 30
        )
        if hard_invalid:
            assert result.action is ConfidenceAction.ABSTAIN
        elif source_class is SourceClass.OFFICIAL_API:
            assert result.action is ConfidenceAction.ABSTAIN
        elif confidence < p.downweight_confidence:
            assert result.action is ConfidenceAction.ABSTAIN
        else:
            assert result.action is ConfidenceAction.DOWNWEIGHT


@pytest.mark.parametrize("source_class", list(SourceClass))
@pytest.mark.parametrize("confidence", [Decimal("0.49"), Decimal("0.50"), Decimal("0.79"), Decimal("0.80"), Decimal("1")])
def test_corroboration_identity_never_escalates_action(source_class, confidence):
    plain = obs(source_class=source_class, base_confidence=confidence, corroborator_ids=())
    corroborated = obs(source_class=source_class, base_confidence=confidence, corroborator_ids=("peer-1", "peer-2"))
    plain_result = assess_source_quality(plain, now=NOW, policy=policy())
    corroborated_result = assess_source_quality(corroborated, now=NOW, policy=policy())
    assert corroborated_result.action is plain_result.action
    assert corroborated_result.input_confidence == plain_result.input_confidence
    assert corroborated_result.corroborated is False


def test_exhaustive_fail_closed_truth_matrix():
    p = policy()
    confidences = [Decimal("0.49"), Decimal("0.50"), Decimal("0.79"), Decimal("0.80"), Decimal("1")]
    ages = [-1, 0, 30, 31]
    schemas = [1, 2]

    for source_class in SourceClass:
        for confidence in confidences:
            for age_seconds in ages:
                for transport_verified in (False, True):
                    for provenance_bound in (False, True):
                        for schema_version in schemas:
                            observation = obs(
                                source_class=source_class,
                                base_confidence=confidence,
                                observed_at=NOW - timedelta(seconds=age_seconds),
                                transport_verified=transport_verified,
                                provenance_bound=provenance_bound,
                                schema_version=schema_version,
                            )
                            result = assess_source_quality(observation, now=NOW, policy=p)
                            assert result.action is not ConfidenceAction.ACCEPT
                            assert result.input_confidence == confidence
                            assert result.corroborated is False

                            hard_invalid = (
                                age_seconds < 0
                                or age_seconds > 30
                                or not transport_verified
                                or not provenance_bound
                                or schema_version != 1
                            )
                            if hard_invalid:
                                assert result.action is ConfidenceAction.ABSTAIN
                            elif source_class is SourceClass.OFFICIAL_API:
                                assert result.action is ConfidenceAction.ABSTAIN
                            elif confidence < p.downweight_confidence:
                                assert result.action is ConfidenceAction.ABSTAIN
                            else:
                                assert result.action is ConfidenceAction.DOWNWEIGHT
