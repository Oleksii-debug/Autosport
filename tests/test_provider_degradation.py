from __future__ import annotations

import pytest

from autosport.bookmaker_capability import BookmakerCapability
from autosport.provider_degradation import (
    ProviderDegradationError,
    ProviderDegradationEvent,
    ProviderDegradationReason,
    ProviderDegradationReport,
    ProviderRecoveryClass,
)


def _event(
    *,
    venue_id: str = "venue-a",
    account_id: str = "account-a",
    adapter_id: str = "adapter-a",
    capability: BookmakerCapability = BookmakerCapability.LIVE_QUOTES_READ,
    reason: ProviderDegradationReason = ProviderDegradationReason.TRANSIENT_PROVIDER_FAILURE,
    observed_at: str = "2026-09-21T08:00:00+00:00",
    evidence_ref: str = "evidence://provider/failure/1",
    source_seed: str = "a",
    detail_code: str = "HTTP_503",
) -> ProviderDegradationEvent:
    return ProviderDegradationEvent(
        venue_id=venue_id,
        account_id=account_id,
        adapter_id=adapter_id,
        capability=capability,
        reason=reason,
        observed_at=observed_at,
        evidence_ref=evidence_ref,
        source_payload_sha256=source_seed * 64,
        detail_code=detail_code,
    )


def test_transient_failure_is_diagnostic_but_never_fallback_or_execution_authority() -> None:
    event = _event()

    assert event.recovery_class is ProviderRecoveryClass.RETRY_WITH_BACKOFF
    assert event.automatic_fallback_authorized is False
    assert event.provider_write_authorized is False
    assert event.real_money_execution is False
    assert event.to_canonical_dict()["automatic_fallback_authorized"] is False


def test_recovery_class_is_deterministic_for_every_reason() -> None:
    expected = {
        ProviderDegradationReason.TRANSIENT_PROVIDER_FAILURE: ProviderRecoveryClass.RETRY_WITH_BACKOFF,
        ProviderDegradationReason.STALE_DATA: ProviderRecoveryClass.REFRESH_EVIDENCE,
        ProviderDegradationReason.MALFORMED_EVIDENCE: ProviderRecoveryClass.OPERATOR_REVIEW,
        ProviderDegradationReason.RATE_LIMITED: ProviderRecoveryClass.RETRY_WITH_BACKOFF,
        ProviderDegradationReason.AUTHENTICATION_OR_PERMISSION_FAILURE: ProviderRecoveryClass.REAUTHORIZE,
        ProviderDegradationReason.TRANSPORT_UNAVAILABLE: ProviderRecoveryClass.RETRY_WITH_BACKOFF,
        ProviderDegradationReason.UNKNOWN: ProviderRecoveryClass.OPERATOR_REVIEW,
    }

    assert set(expected) == set(ProviderDegradationReason)
    for index, (reason, recovery) in enumerate(expected.items()):
        event = _event(
            reason=reason,
            source_seed=f"{index:x}",
            detail_code=f"DETAIL_{index}",
        )
        assert event.recovery_class is recovery
        assert event.automatic_fallback_authorized is False


def test_unsupported_capability_truth_is_not_mintable_as_degradation_reason() -> None:
    with pytest.raises(ValueError):
        ProviderDegradationReason("capability_unsupported")


def test_event_identity_binds_scope_capability_reason_and_source() -> None:
    base = _event()
    changed_capability = _event(capability=BookmakerCapability.PREMATCH_QUOTES_READ)
    changed_source = _event(source_seed="b")
    changed_reason = _event(reason=ProviderDegradationReason.RATE_LIMITED)

    assert len(base.event_id) == 64
    assert base.event_id != changed_capability.event_id
    assert base.event_id != changed_source.event_id
    assert base.event_id != changed_reason.event_id


def test_report_identity_is_independent_of_input_order() -> None:
    first = _event(source_seed="a")
    second = _event(
        venue_id="venue-b",
        account_id="account-b",
        adapter_id="adapter-b",
        capability=BookmakerCapability.BALANCE_READ,
        reason=ProviderDegradationReason.STALE_DATA,
        source_seed="b",
        detail_code="STALE_42S",
    )

    left = ProviderDegradationReport(
        events=(first, second), as_of="2026-09-21T08:01:00+00:00"
    )
    right = ProviderDegradationReport(
        events=(second, first), as_of="2026-09-21T08:01:00+00:00"
    )

    assert left.to_canonical_dict() == right.to_canonical_dict()
    assert left.report_id == right.report_id
    assert left.automatic_fallback_authorized is False
    assert left.provider_write_authorized is False
    assert left.real_money_execution is False


def test_report_rejects_ambiguous_same_scope_outcomes() -> None:
    first = _event(reason=ProviderDegradationReason.TRANSIENT_PROVIDER_FAILURE)
    second = _event(
        reason=ProviderDegradationReason.RATE_LIMITED,
        source_seed="b",
        detail_code="HTTP_429",
    )

    with pytest.raises(ProviderDegradationError, match="multiple degradation outcomes"):
        ProviderDegradationReport(
            events=(first, second), as_of="2026-09-21T08:01:00+00:00"
        )


def test_report_rejects_future_observation() -> None:
    event = _event(observed_at="2026-09-21T08:02:00+00:00")

    with pytest.raises(ProviderDegradationError, match="after report as_of"):
        ProviderDegradationReport(
            events=(event,), as_of="2026-09-21T08:01:00+00:00"
        )


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"capability": "live_quotes_read"}, "exact BookmakerCapability"),
        ({"reason": "rate_limited"}, "exact ProviderDegradationReason"),
        ({"observed_at": "2026-09-21T08:00:00"}, "timezone offset"),
        ({"source_seed": "A"}, "lowercase 64-character SHA-256"),
        ({"detail_code": " HTTP_503"}, "detail_code"),
    ],
)
def test_event_rejects_noncanonical_evidence(kwargs: dict[str, object], match: str) -> None:
    with pytest.raises(ProviderDegradationError, match=match):
        _event(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("schema_version", [True, 1.0, 2])
def test_event_rejects_schema_aliases_or_unknown_revision(schema_version: object) -> None:
    with pytest.raises(ProviderDegradationError, match="schema_version"):
        ProviderDegradationEvent(
            venue_id="venue-a",
            account_id="account-a",
            adapter_id="adapter-a",
            capability=BookmakerCapability.LIVE_QUOTES_READ,
            reason=ProviderDegradationReason.TRANSIENT_PROVIDER_FAILURE,
            observed_at="2026-09-21T08:00:00+00:00",
            evidence_ref="evidence://provider/failure/1",
            source_payload_sha256="a" * 64,
            detail_code="HTTP_503",
            schema_version=schema_version,  # type: ignore[arg-type]
        )


def test_report_rejects_empty_non_tuple_and_derived_event_values() -> None:
    with pytest.raises(ProviderDegradationError, match="must not be empty"):
        ProviderDegradationReport(events=(), as_of="2026-09-21T08:01:00+00:00")

    with pytest.raises(ProviderDegradationError, match="must be a tuple"):
        ProviderDegradationReport(  # type: ignore[arg-type]
            events=[], as_of="2026-09-21T08:01:00+00:00"
        )

    class DerivedEvent(ProviderDegradationEvent):
        pass

    derived = DerivedEvent(
        venue_id="venue-a",
        account_id="account-a",
        adapter_id="adapter-a",
        capability=BookmakerCapability.LIVE_QUOTES_READ,
        reason=ProviderDegradationReason.UNKNOWN,
        observed_at="2026-09-21T08:00:00+00:00",
        evidence_ref="evidence://provider/failure/1",
        source_payload_sha256="a" * 64,
        detail_code="UNKNOWN",
    )
    with pytest.raises(ProviderDegradationError, match="exact ProviderDegradationEvent"):
        ProviderDegradationReport(
            events=(derived,), as_of="2026-09-21T08:01:00+00:00"
        )
