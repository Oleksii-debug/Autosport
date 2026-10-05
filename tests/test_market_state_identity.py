from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.domain import MarketEvent
from autosport.market_state_identity import (
    MarketStateIdentityError,
    PROPHETX_REST_MARKET_STATE_CONTRACT,
    same_semantic_market_state,
    semantic_market_state_identity,
)


def _event(
    *,
    sequence: int,
    odds: str = "2.00",
    contract: object = PROPHETX_REST_MARKET_STATE_CONTRACT,
    source_id: str = "prophetx:sandbox",
) -> MarketEvent:
    metadata = {
        "provider": "prophetx",
        "environment": "sandbox",
        "transport_surface": "v3_affiliate_get_markets",
        "request_fingerprint_sha256": "a" * 64,
        "product_acquisition_sequence": sequence,
        "response_sha256": f"{sequence:x}".rjust(64, "0"),
        "snapshot_fingerprint_sha256": (
            f"{sequence + 100:x}".rjust(64, "0")
        ),
        "sequence_authority_id": "prophetx-rest-test-authority",
        "sequence_source_id": (
            "prophetx:sandbox:rest:v3-affiliate-get-markets"
        ),
    }
    if contract is not None:
        metadata["semantic_state_contract"] = contract
    timestamp = f"2026-09-16T19:00:{sequence:02d}+00:00"
    return MarketEvent(
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        decimal_odds=Decimal(odds),
        observed_ts=timestamp,
        source_id=source_id,
        sequence=sequence,
        status="open",
        ingest_ts=timestamp,
        metadata=metadata,
    )


def test_acquisition_only_fields_do_not_change_semantic_identity() -> None:
    first = _event(sequence=1)
    second = _event(sequence=2)

    assert first.dedupe_key != second.dedupe_key
    assert semantic_market_state_identity(first) == (
        semantic_market_state_identity(second)
    )
    assert same_semantic_market_state(first, second)


def test_request_fingerprint_rotation_is_acquisition_only() -> None:
    first = _event(sequence=1)
    second_payload = _event(sequence=2).to_dict()
    second_payload["metadata"]["request_fingerprint_sha256"] = "b" * 64
    second = MarketEvent.from_dict(second_payload)

    assert semantic_market_state_identity(first) == (
        semantic_market_state_identity(second)
    )
    assert same_semantic_market_state(first, second)


def test_sequence_authority_rotation_is_acquisition_only() -> None:
    first = _event(sequence=1)
    second_payload = _event(sequence=2).to_dict()
    second_payload["metadata"]["sequence_authority_id"] = (
        "prophetx-rest-restarted-authority"
    )
    second = MarketEvent.from_dict(second_payload)

    assert semantic_market_state_identity(first) == (
        semantic_market_state_identity(second)
    )
    assert same_semantic_market_state(first, second)


def test_economic_change_remains_semantic_transition() -> None:
    first = _event(sequence=1, odds="2.00")
    changed = _event(sequence=2, odds="2.10")

    assert semantic_market_state_identity(first) != (
        semantic_market_state_identity(changed)
    )
    assert not same_semantic_market_state(first, changed)


def test_absent_contract_preserves_legacy_event_semantics() -> None:
    first = _event(sequence=1, contract=None)
    second = _event(sequence=2, contract=None)

    assert semantic_market_state_identity(first) is None
    assert semantic_market_state_identity(second) is None
    assert not same_semantic_market_state(first, second)


@pytest.mark.parametrize(
    ("contract", "source_id", "message"),
    (
        (
            "autosport.prophetx-rest-market-state.v2",
            "prophetx:sandbox",
            "unsupported",
        ),
        (
            PROPHETX_REST_MARKET_STATE_CONTRACT,
            "provider-a",
            "canonical source_id",
        ),
    ),
)
def test_malformed_or_rebound_contract_fails_closed(
    contract: str,
    source_id: str,
    message: str,
) -> None:
    with pytest.raises(MarketStateIdentityError, match=message):
        semantic_market_state_identity(
            _event(
                sequence=1,
                contract=contract,
                source_id=source_id,
            )
        )


def test_acquisition_sequence_must_match_market_event_sequence() -> None:
    event = _event(sequence=1)
    payload = event.to_dict()
    payload["metadata"]["product_acquisition_sequence"] = 2
    forged = MarketEvent.from_dict(payload)

    with pytest.raises(
        MarketStateIdentityError,
        match="acquisition sequence is inconsistent",
    ):
        semantic_market_state_identity(forged)

def test_excluded_request_fingerprint_still_requires_canonical_shape() -> None:
    payload = _event(sequence=1).to_dict()
    payload["metadata"]["request_fingerprint_sha256"] = "not-a-digest"
    malformed = MarketEvent.from_dict(payload)

    with pytest.raises(MarketStateIdentityError, match="request fingerprint"):
        semantic_market_state_identity(malformed)


def test_excluded_sequence_authority_still_requires_canonical_text() -> None:
    payload = _event(sequence=1).to_dict()
    payload["metadata"]["sequence_authority_id"] = " authority-with-whitespace "
    malformed = MarketEvent.from_dict(payload)

    with pytest.raises(MarketStateIdentityError, match="sequence authority identity"):
        semantic_market_state_identity(malformed)

@pytest.mark.parametrize(
    "authority_id",
    (
        "UPPERCASE",
        "authority\nwith-control",
        "authority with-space",
        "аuthority-unicode",
    ),
)
def test_sequence_authority_requires_canonical_ascii_identity(
    authority_id: str,
) -> None:
    payload = _event(sequence=1).to_dict()
    payload["metadata"]["sequence_authority_id"] = authority_id
    malformed = MarketEvent.from_dict(payload)

    with pytest.raises(
        MarketStateIdentityError,
        match="canonical sequence authority identity",
    ):
        semantic_market_state_identity(malformed)


def test_provider_source_timestamp_remains_semantic_state() -> None:
    first_payload = _event(sequence=1).to_dict()
    first_payload["source_ts"] = "2026-09-16T18:59:59+00:00"
    first = MarketEvent.from_dict(first_payload)

    second_payload = _event(sequence=2).to_dict()
    second_payload["source_ts"] = "2026-09-16T19:00:00+00:00"
    second = MarketEvent.from_dict(second_payload)

    assert semantic_market_state_identity(first) != (
        semantic_market_state_identity(second)
    )
    assert not same_semantic_market_state(first, second)


def test_unknown_metadata_remains_conservatively_semantic() -> None:
    first = _event(sequence=1)
    second_payload = _event(sequence=2).to_dict()
    second_payload["metadata"]["provider_market_phase"] = "in_play"
    second = MarketEvent.from_dict(second_payload)

    assert semantic_market_state_identity(first) != (
        semantic_market_state_identity(second)
    )
    assert not same_semantic_market_state(first, second)


def test_local_receipt_clocks_are_not_semantic_market_state() -> None:
    first = _event(sequence=1)
    second_payload = _event(sequence=2).to_dict()
    second_payload["observed_ts"] = "2026-09-16T20:00:00+00:00"
    second_payload["ingest_ts"] = "2026-09-16T20:00:01+00:00"
    second = MarketEvent.from_dict(second_payload)

    assert semantic_market_state_identity(first) == (
        semantic_market_state_identity(second)
    )
    assert same_semantic_market_state(first, second)

@pytest.mark.parametrize(
    ("field_name", "replacement", "message"),
    (
        ("provider", "other", "provider=prophetx"),
        ("environment", "production", "sandbox environment"),
        ("transport_surface", "other_surface", "canonical transport surface"),
        ("sequence_source_id", "other:sequence:source", "canonical sequence source identity"),
    ),
)
def test_contract_binding_fields_fail_closed(
    field_name: str,
    replacement: str,
    message: str,
) -> None:
    payload = _event(sequence=1).to_dict()
    payload["metadata"][field_name] = replacement
    malformed = MarketEvent.from_dict(payload)

    with pytest.raises(MarketStateIdentityError, match=message):
        semantic_market_state_identity(malformed)


@pytest.mark.parametrize(
    "field_name",
    ("request_fingerprint_sha256", "response_sha256", "snapshot_fingerprint_sha256"),
)
def test_digest_provenance_fields_require_lowercase_sha256(field_name: str) -> None:
    payload = _event(sequence=1).to_dict()
    payload["metadata"][field_name] = "A" * 64
    malformed = MarketEvent.from_dict(payload)

    with pytest.raises(MarketStateIdentityError, match=field_name):
        semantic_market_state_identity(malformed)


@pytest.mark.parametrize(
    "bad_sequence",
    (True, 0, -1, (1 << 63)),
)
def test_acquisition_sequence_requires_positive_signed_int64(
    bad_sequence: object,
) -> None:
    payload = _event(sequence=1).to_dict()
    payload["metadata"]["product_acquisition_sequence"] = bad_sequence
    malformed = MarketEvent.from_dict(payload)

    with pytest.raises(
        MarketStateIdentityError,
        match="acquisition sequence is inconsistent",
    ):
        semantic_market_state_identity(malformed)

