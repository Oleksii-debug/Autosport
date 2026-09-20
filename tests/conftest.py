from __future__ import annotations

"""Narrow deterministic fixture bridge for production-owned capture authority tests.

The product API deliberately refuses to mint provider availability authority from
caller-supplied transports/clocks.  The existing point-in-time test module predates
that boundary and builds deterministic provider responses through those public
injection seams.  During collection we replace only its private positive helper so
positive tests exercise the production-owned constructor path while still avoiding
network access.  Adversarial tests continue to call the public injected path and
must remain non-authoritative.
"""

from pathlib import Path
from types import ModuleType

import autosport._historical_capture_authority_guard as _guard
from autosport.parlayapi_provider import ParlayApiTableTennisProvider


def _install_point_in_time_capture_fixture(module: ModuleType) -> None:
    def authoritative_provider_capture(
        tmp_path: Path,
        *,
        source_as_of,
        available_at,
    ):
        token = module.hashlib.sha256(
            (module._iso(source_as_of) + "|" + module._iso(available_at)).encode(
                "utf-8"
            )
        ).hexdigest()[:12]
        market_path = tmp_path / f"provider-capture-{token}.jsonl"
        evidence_path = tmp_path / f"provider-capture-{token}.evidence.json"
        payload = {
            "timestamp": module._iso(source_as_of),
            "previous_timestamp": None,
            "next_timestamp": None,
            "data": [],
        }
        provider = ParlayApiTableTennisProvider(
            "test-provider-key",
            transport=module._HistoricalTransport(payload),
            clock=lambda: module._iso(available_at),
            sleeper=lambda _: None,
        )

        previous_factory = _guard.ParlayApiTableTennisProvider
        try:
            # Replace only the guard's private constructor symbol. The public
            # historical capture function still treats this provider as caller
            # supplied and therefore cannot mint authority.
            _guard.ParlayApiTableTennisProvider = lambda *args, **kwargs: provider
            return _guard.capture_authoritative_historical_snapshot(
                api_key="test-provider-key",
                requested_at=module._iso(source_as_of),
                output_path=market_path,
                evidence_path=evidence_path,
            )
        finally:
            _guard.ParlayApiTableTennisProvider = previous_factory

    module._provider_capture = authoritative_provider_capture


def pytest_collection_modifyitems(items) -> None:
    patched: set[int] = set()
    for item in items:
        path = getattr(item, "path", None)
        if path is None or path.name != "test_point_in_time_authority.py":
            continue
        module = item.module
        key = id(module)
        if key not in patched:
            _install_point_in_time_capture_fixture(module)
            patched.add(key)
