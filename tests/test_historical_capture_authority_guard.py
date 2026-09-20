from __future__ import annotations

from pathlib import Path

import pytest

from autosport.historical_snapshot import (
    assert_historical_snapshot_capture_authoritative,
    capture_authoritative_historical_snapshot,
    capture_historical_snapshot,
)
from autosport.parlayapi_provider import (
    HttpJsonResponse,
    ParlayApiTableTennisProvider,
    ProviderPayloadError,
)


ORIGIN_ERROR = "independently re-resolved canonical production-origin evidence"


def _injected_provider() -> ParlayApiTableTennisProvider:
    payload = {
        "timestamp": "2026-01-04T10:00:00Z",
        "previous_timestamp": None,
        "next_timestamp": None,
        "data": [],
    }

    def transport(url: str, headers: object, timeout: float) -> HttpJsonResponse:
        return HttpJsonResponse(payload=payload, status_code=200, headers={})

    return ParlayApiTableTennisProvider(
        "test-key",
        transport=transport,
        clock=lambda: "2026-01-04T10:06:00Z",
        sleeper=lambda _: None,
    )


def test_caller_injected_transport_and_clock_cannot_mint_capture_authority(
    tmp_path: Path,
) -> None:
    capture = capture_historical_snapshot(
        _injected_provider(),
        requested_at="2026-01-04T10:00:00Z",
        output_path=tmp_path / "market.jsonl",
        evidence_path=tmp_path / "evidence.json",
    )

    with pytest.raises(ProviderPayloadError, match=ORIGIN_ERROR):
        assert_historical_snapshot_capture_authoritative(capture)


def test_api_key_only_constructor_also_fails_closed_without_re_resolvable_origin(
    tmp_path: Path,
) -> None:
    # Hiding transport/clock injection behind a module-private constructor is not
    # an origin-attestation boundary in Python.  Until provider origin is
    # independently re-resolvable, this path must fail before minting authority.
    with pytest.raises(ProviderPayloadError, match=ORIGIN_ERROR):
        capture_authoritative_historical_snapshot(
            api_key="test-key",
            requested_at="2026-01-04T10:00:00Z",
            output_path=tmp_path / "market.jsonl",
            evidence_path=tmp_path / "evidence.json",
        )
