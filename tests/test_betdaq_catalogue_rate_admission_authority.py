from __future__ import annotations

import pytest

from autosport.betdaq_catalogue_binding import (
    BetdaqCatalogueEvidence,
    BetdaqLiveCatalogueResolver,
)
from autosport.betdaq_rate_governor import (
    BetdaqBlacklistStatus,
    BetdaqRateAdmission,
    BetdaqRatePriority,
    BetdaqRateTier,
)
from autosport.betdaq_readonly_provider import BetdaqMarketBinding
from autosport.domain import MarketType


def _admission() -> BetdaqRateAdmission:
    return BetdaqRateAdmission(
        governor_id="fixture-governor",
        policy_revision="fixture-policy",
        policy_fingerprint="1" * 64,
        documented_policy_sha256="2" * 64,
        tier=BetdaqRateTier.DEFAULT,
        method="GetEventSubTreeNoSelections",
        rate_policy_key="GetEventSubTreeNoSelections",
        priority=BetdaqRatePriority.BACKGROUND_READ,
        sequence=1,
        admitted_monotonic=1.0,
        admitted_at="2026-10-05T06:00:00Z",
        method_active=1,
        combined_active=1,
        method_remaining_total=24,
        combined_remaining_total=299,
        method_remaining_background=24,
        combined_remaining_background=299,
        blacklist_status=BetdaqBlacklistStatus.UNKNOWN,
    )


def _resolver(transport: object, *, attempts: int = 2) -> BetdaqLiveCatalogueResolver:
    return BetdaqLiveCatalogueResolver(
        transport=transport,
        timeout_seconds=1.0,
        clock=lambda: "2026-10-05T06:00:01Z",
        max_attempts=attempts,
    )


def _binding() -> BetdaqMarketBinding:
    return BetdaqMarketBinding(9001, "100", "football", MarketType.WINNER)


def _catalogue_evidence(**overrides) -> BetdaqCatalogueEvidence:
    values = {
        "requested_event_classifier_ids": (100, 101),
        "received_at": "2026-10-05T06:00:01Z",
        "request_fingerprint": "3" * 64,
        "response_sha256": "4" * 64,
        "provider_call_id": "call-1",
        "provider_created_at": "2026-10-05T06:00:00Z",
        "rate_admission_receipts": ("5" * 64,),
    }
    values.update(overrides)
    return BetdaqCatalogueEvidence(**values)


class _ReusedAdmissionTransport:
    def __init__(self) -> None:
        self.calls = 0
        self.last_rate_admission = _admission()

    def get_event_subtree_no_selections(self, request, *, timeout_seconds):
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("explicit transient with first admission")
        # The resolver must reject the reused admission before trusting/parsing this
        # successor payload, so intentionally-invalid bytes are sufficient here.
        return b"must-not-be-parsed"


class _MissingAdmissionTransport:
    last_rate_admission = None

    def get_event_subtree_no_selections(self, request, *, timeout_seconds):
        raise TimeoutError("transient dispatch without governor evidence")


def test_catalogue_retry_rejects_reused_rate_admission() -> None:
    transport = _ReusedAdmissionTransport()

    with pytest.raises(ValueError, match="reused rate admission"):
        _resolver(transport).resolve([_binding()])

    assert transport.calls == 2


def test_catalogue_retry_rejects_missing_rate_admission() -> None:
    transport = _MissingAdmissionTransport()

    with pytest.raises(
        TypeError,
        match="requires canonical BetdaqRateAdmission",
    ):
        _resolver(transport).resolve([_binding()])


def test_catalogue_evidence_requires_canonical_root_order() -> None:
    with pytest.raises(ValueError, match="sorted unique canonical provider ids"):
        _catalogue_evidence(requested_event_classifier_ids=(101, 100))


def test_catalogue_evidence_requires_trimmed_provider_call_id() -> None:
    with pytest.raises(ValueError, match="trimmed non-empty str"):
        _catalogue_evidence(provider_call_id=" call-1 ")


def test_catalogue_evidence_rejects_provider_time_after_receipt() -> None:
    with pytest.raises(ValueError, match="cannot follow catalogue receipt"):
        _catalogue_evidence(provider_created_at="2026-10-05T06:00:02Z")
