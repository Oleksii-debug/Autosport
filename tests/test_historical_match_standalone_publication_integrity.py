from __future__ import annotations

from pathlib import Path

import pytest

from autosport import historical_matches
from autosport.historical_matches import capture_historical_matches
from autosport.parlayapi_provider import HttpJsonResponse, ParlayApiTableTennisProvider


class _Transport:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def __call__(
        self,
        url: str,
        headers: dict[str, str],
        timeout: float,
    ) -> HttpJsonResponse:
        self.urls.append(url)
        return HttpJsonResponse(
            [{"provider_defined_id": "match-1", "opaque": {"value": 1}}],
            200,
            {
                "x-historical-window-hours": "168",
                "x-historical-window-from": "2026-09-06T00:00:00Z",
                "x-api-version": "test",
            },
        )


def _provider(transport: _Transport) -> ParlayApiTableTennisProvider:
    return ParlayApiTableTennisProvider(
        "unit-test-key",
        transport=transport,
        clock=lambda: "2026-09-13T03:00:00+00:00",
        sleeper=lambda _: None,
    )


def test_standalone_capture_fails_closed_if_published_capture_changes_before_evidence_commit(
    tmp_path,
    monkeypatch,
):
    output = tmp_path / "matches.json"
    evidence = tmp_path / "matches.evidence.json"
    transport = _Transport()
    real_atomic_write_json = historical_matches.atomic_write_json
    replacement = b'{"foreign":"replacement"}\n'
    replacement_observed = False

    def replace_capture_before_evidence(path, payload):
        nonlocal replacement_observed
        if Path(path) == evidence:
            assert output.exists(), "falsifier requires capture to have been published first"
            output.write_bytes(replacement)
            replacement_observed = True
        return real_atomic_write_json(path, payload)

    monkeypatch.setattr(
        historical_matches,
        "atomic_write_json",
        replace_capture_before_evidence,
    )

    with pytest.raises(Exception):
        capture_historical_matches(
            _provider(transport),
            requested_date="2026-09-10",
            output_path=output,
            evidence_path=evidence,
        )

    assert replacement_observed is True
    assert output.read_bytes() == replacement
    assert len(transport.urls) == 1
