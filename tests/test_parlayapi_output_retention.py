from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pytest

from autosport.parlayapi_output_retention import (
    LINE_LEVEL_MAX_RETENTION,
    PARLAYAPI_TERMS_POLICY_IDENTITY,
    ParlayApiOutputClass,
    ParlayApiOutputRetentionEvidence,
    ParlayApiRetentionError,
    ParlayApiRetentionState,
    deletion_tombstone,
    new_capture_retention_evidence,
)


T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
SHA_A = "a" * 64
SHA_B = "b" * 64


def capture(
    output_class: ParlayApiOutputClass = ParlayApiOutputClass.LINE_LEVEL_PRICING,
    **overrides: object,
) -> ParlayApiOutputRetentionEvidence:
    values: dict[str, object] = {
        "output_class": output_class,
        "source_payload_sha256": SHA_A,
        "acquisition_identity_sha256": SHA_B,
        "captured_at": T0,
        "available_at": T0,
    }
    values.update(overrides)
    return new_capture_retention_evidence(**values)  # type: ignore[arg-type]


def test_line_level_pricing_has_hard_90_day_cap_without_consent_authority() -> None:
    evidence = capture(
        internal_retention_until=T0 + timedelta(days=365),
        subscription_tier="Scale",
    )

    assert evidence.effective_retention_deadline() == T0 + LINE_LEVEL_MAX_RETENTION
    before = evidence.evaluate(as_of=T0 + LINE_LEVEL_MAX_RETENTION - timedelta(seconds=1))
    expired = evidence.evaluate(as_of=T0 + LINE_LEVEL_MAX_RETENTION)

    assert before.state is ParlayApiRetentionState.ACQUISITION_AUTHORITY_UNRESOLVED
    assert before.raw_use_allowed is False
    assert before.training_corpus_allowed is False
    assert before.raw_redistribution_allowed is False
    assert expired.state is ParlayApiRetentionState.DELETE_REQUIRED
    assert expired.delete_raw_output is True
    assert expired.raw_use_allowed is False
    with pytest.raises(ParlayApiRetentionError, match="DELETE_REQUIRED"):
        evidence.require_training_corpus_use(as_of=T0 + LINE_LEVEL_MAX_RETENTION)


def test_cache_control_shorter_than_90_days_wins() -> None:
    evidence = capture(observed_cache_control="private, max-age=86400")

    assert evidence.effective_retention_deadline() == T0 + timedelta(days=1)
    assert evidence.evaluate(
        as_of=T0 + timedelta(days=1) - timedelta(seconds=1)
    ).state is ParlayApiRetentionState.ACQUISITION_AUTHORITY_UNRESOLVED
    assert evidence.evaluate(
        as_of=T0 + timedelta(days=1)
    ).state is ParlayApiRetentionState.DELETE_REQUIRED


def test_no_store_requires_immediate_deletion_and_no_cache_requires_review() -> None:
    no_store = capture(observed_cache_control="no-store")
    assert no_store.evaluate(as_of=T0).state is ParlayApiRetentionState.DELETE_REQUIRED

    no_cache = capture(observed_cache_control="no-cache")
    decision = no_cache.evaluate(as_of=T0)
    assert decision.state is ParlayApiRetentionState.LEGAL_REVIEW_REQUIRED
    assert decision.raw_use_allowed is False


def test_subscription_termination_shortens_raw_output_window() -> None:
    evidence = capture(subscription_terminated_at=T0 + timedelta(days=5))

    assert evidence.effective_retention_deadline() == T0 + timedelta(days=5)
    decision = evidence.evaluate(as_of=T0 + timedelta(days=5))
    assert decision.state is ParlayApiRetentionState.DELETE_REQUIRED
    assert decision.delete_raw_output is True


def test_enterprise_or_historical_access_tier_never_mints_redistribution_or_extension() -> None:
    evidence = capture(
        subscription_tier="Enterprise full archive",
        internal_retention_until=T0 + timedelta(days=1000),
    )
    decision = evidence.evaluate(as_of=T0 + timedelta(days=30))

    assert decision.state is ParlayApiRetentionState.ACQUISITION_AUTHORITY_UNRESOLVED
    assert decision.raw_use_allowed is False
    assert decision.raw_redistribution_allowed is False
    assert evidence.effective_retention_deadline() == T0 + timedelta(days=90)


def test_result_or_other_raw_output_needs_explicit_finite_internal_horizon() -> None:
    missing_policy = capture(ParlayApiOutputClass.MATCH_RESULT_OR_OTHER_OUTPUT)
    decision = missing_policy.evaluate(as_of=T0)
    assert decision.state is ParlayApiRetentionState.LEGAL_REVIEW_REQUIRED
    assert decision.training_corpus_allowed is False

    bounded = capture(
        ParlayApiOutputClass.MATCH_RESULT_OR_OTHER_OUTPUT,
        internal_retention_until=T0 + timedelta(days=30),
    )
    bounded_decision = bounded.evaluate(as_of=T0 + timedelta(days=29))
    assert bounded_decision.state is ParlayApiRetentionState.ACQUISITION_AUTHORITY_UNRESOLVED
    assert bounded_decision.training_corpus_allowed is False
    assert bounded.evaluate(
        as_of=T0 + timedelta(days=30)
    ).state is ParlayApiRetentionState.DELETE_REQUIRED


def test_capture_availability_is_causal_and_replay_waits_for_actual_capture() -> None:
    provider_available = T0 + timedelta(hours=1)
    captured = T0 + timedelta(hours=2)
    evidence = capture(
        available_at=provider_available,
        captured_at=captured,
    )

    before_provider = evidence.evaluate(as_of=T0 + timedelta(minutes=30))
    after_provider_before_capture = evidence.evaluate(
        as_of=T0 + timedelta(hours=1, minutes=30)
    )
    after_capture = evidence.evaluate(as_of=captured)

    assert before_provider.state is ParlayApiRetentionState.NOT_YET_AVAILABLE
    assert after_provider_before_capture.state is ParlayApiRetentionState.NOT_YET_AVAILABLE
    assert after_provider_before_capture.training_corpus_allowed is False
    assert after_capture.state is ParlayApiRetentionState.ACQUISITION_AUTHORITY_UNRESOLVED
    assert after_capture.raw_use_allowed is False


def test_caller_rewrap_cannot_reset_old_raw_output_retention_authority() -> None:
    old_capture = capture()
    assert old_capture.evaluate(
        as_of=T0 + LINE_LEVEL_MAX_RETENTION
    ).state is ParlayApiRetentionState.DELETE_REQUIRED

    # The exact same provider payload digest can be wrapped by an ordinary caller
    # with a fresh arbitrary acquisition digest and refreshed timestamps. Deadline
    # calculation may describe that supplied evidence, but it must not mint use
    # authority until canonical acquisition identity/time are re-resolved.
    refreshed_at = T0 + timedelta(days=120)
    rewrapped = new_capture_retention_evidence(
        output_class=ParlayApiOutputClass.LINE_LEVEL_PRICING,
        source_payload_sha256=SHA_A,
        acquisition_identity_sha256="c" * 64,
        captured_at=refreshed_at,
        available_at=refreshed_at,
    )

    assert rewrapped.effective_retention_deadline() == refreshed_at + LINE_LEVEL_MAX_RETENTION
    decision = rewrapped.evaluate(as_of=refreshed_at)
    assert decision.state is ParlayApiRetentionState.ACQUISITION_AUTHORITY_UNRESOLVED
    assert decision.raw_use_allowed is False
    assert decision.training_corpus_allowed is False
    with pytest.raises(ParlayApiRetentionError, match="ACQUISITION_AUTHORITY_UNRESOLVED"):
        rewrapped.require_raw_use(as_of=refreshed_at)


def test_policy_change_preserves_capture_identity_but_blocks_use_for_review() -> None:
    historical = ParlayApiOutputRetentionEvidence(
        output_class=ParlayApiOutputClass.LINE_LEVEL_PRICING,
        source_payload_sha256=SHA_A,
        acquisition_identity_sha256=SHA_B,
        captured_at=T0,
        available_at=T0,
        policy_identity="parlayapi-terms+aup@2025-01-01",
    )
    decision = historical.evaluate(
        as_of=T0 + timedelta(days=1),
        current_policy_identity=PARLAYAPI_TERMS_POLICY_IDENTITY,
    )

    assert historical.policy_identity == "parlayapi-terms+aup@2025-01-01"
    assert decision.state is ParlayApiRetentionState.LEGAL_REVIEW_REQUIRED
    assert decision.delete_raw_output is False
    current = capture()
    assert current.policy_identity == PARLAYAPI_TERMS_POLICY_IDENTITY


def test_expiry_wins_over_policy_review_so_old_raw_bytes_are_not_retained() -> None:
    historical = ParlayApiOutputRetentionEvidence(
        output_class=ParlayApiOutputClass.LINE_LEVEL_PRICING,
        source_payload_sha256=SHA_A,
        acquisition_identity_sha256=SHA_B,
        captured_at=T0,
        available_at=T0,
        policy_identity="parlayapi-terms+aup@2025-01-01",
    )

    decision = historical.evaluate(
        as_of=T0 + timedelta(days=90),
        current_policy_identity=PARLAYAPI_TERMS_POLICY_IDENTITY,
    )
    assert decision.state is ParlayApiRetentionState.DELETE_REQUIRED
    assert decision.delete_raw_output is True


def test_derived_non_raw_does_not_inherit_raw_rights_or_automatic_corpus_authority() -> None:
    evidence = capture(ParlayApiOutputClass.DERIVED_NON_RAW)
    decision = evidence.evaluate(as_of=T0)

    assert decision.state is ParlayApiRetentionState.DERIVED_NON_RAW
    assert decision.raw_use_allowed is False
    assert decision.raw_redistribution_allowed is False
    assert decision.training_corpus_allowed is False
    with pytest.raises(ParlayApiRetentionError, match="separate derived-corpus qualification"):
        evidence.require_training_corpus_use(as_of=T0)


def test_deletion_tombstone_retains_only_non_output_audit_metadata() -> None:
    evidence = capture()
    tombstone = deletion_tombstone(
        evidence,
        deleted_at=T0 + timedelta(days=90),
        reason="retention deadline reached",
    )

    payload = asdict(tombstone)
    assert payload["source_payload_sha256"] == SHA_A
    assert payload["acquisition_identity_sha256"] == SHA_B
    assert "raw" not in " ".join(payload).lower()
    assert "payload_bytes" not in payload
    assert "content" not in payload


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"source_payload_sha256": "A" * 64}, "source_payload_sha256"),
        (
            {"available_at": T0 + timedelta(seconds=1)},
            "captured_at cannot precede available_at",
        ),
        ({"observed_cache_control": "max-age=abc"}, "non-negative integer"),
        (
            {"observed_cache_control": "max-age=" + "9" * 50},
            "exceeds supported datetime range",
        ),
        ({"internal_retention_until": T0 - timedelta(seconds=1)}, "cannot precede"),
    ],
)
def test_invalid_retention_evidence_fails_closed(
    kwargs: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ParlayApiRetentionError, match=message):
        capture(**kwargs)
