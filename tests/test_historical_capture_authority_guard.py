from __future__ import annotations

from pathlib import Path

import pytest

import autosport._historical_capture_authority_guard as guard
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

    with pytest.raises(ProviderPayloadError, match="canonical production capture path"):
        assert_historical_snapshot_capture_authoritative(capture)


def test_production_owned_constructor_path_mints_capture_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Tests replace only the private constructor symbol. The public capture API has
    # no provider/transport/clock injection seam on its authoritative path.
    provider = _injected_provider()
    monkeypatch.setattr(guard, "ParlayApiTableTennisProvider", lambda *args, **kwargs: provider)

    capture = capture_authoritative_historical_snapshot(
        api_key="test-key",
        requested_at="2026-01-04T10:00:00Z",
        output_path=tmp_path / "market.jsonl",
        evidence_path=tmp_path / "evidence.json",
    )

    assert_historical_snapshot_capture_authoritative(capture)
