from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import runpy

import autosport.betfair_timeout_reconciliation as timeout_resolution


_HELPERS = runpy.run_path(
    str(Path(__file__).with_name("test_betfair_timeout_reconciliation.py"))
)
_ledger_with_timeout = _HELPERS["_ledger_with_timeout"]
_profile = _HELPERS["_profile"]
_empty_provider_capture = _HELPERS["_empty_provider_capture"]


def test_authoritative_capture_ignores_rebound_module_clocks(
    tmp_path,
    monkeypatch,
) -> None:
    ledger, action, provider_ref, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    profile = _profile()

    forged_wall = datetime(2026, 9, 21, 18, 0, 16, tzinfo=timezone.utc)

    class _ForgedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return forged_wall.replace(tzinfo=None)
            return forged_wall.astimezone(tz)

    forged_ticks = iter([1_000_000_000, 16_000_000_000])
    monkeypatch.setattr(timeout_resolution, "datetime", _ForgedDateTime)
    monkeypatch.setattr(
        timeout_resolution,
        "monotonic_ns",
        lambda: next(forged_ticks),
    )

    first_capture = _empty_provider_capture(action, provider_ref)
    first_started_at = timeout_resolution._betfair_readback_capture_started_at(
        first_capture
    )
    first_started_ns = (
        timeout_resolution._betfair_readback_capture_started_monotonic_ns(
            first_capture
        )
    )

    assert first_started_at is not None
    assert first_started_at != forged_wall.isoformat()
    assert first_started_ns is not None
    assert first_started_ns not in {1_000_000_000, 16_000_000_000}

    first = timeout_resolution.resolve_betfair_timeout_provider_state(
        ledger,
        action,
        profile,
        attempt_id="attempt-1",
        expected_profile_sha256=profile.profile_id,
        readback=first_capture,
    )
    assert (
        first.kind
        is timeout_resolution.BetfairTimeoutResolutionKind.INDETERMINATE_BEFORE_VISIBILITY_HORIZON
    )
    assert first.evidence is None

    second_capture = _empty_provider_capture(action, provider_ref)
    second_started_ns = (
        timeout_resolution._betfair_readback_capture_started_monotonic_ns(
            second_capture
        )
    )
    assert second_started_ns is not None
    assert second_started_ns not in {1_000_000_000, 16_000_000_000}

    second = timeout_resolution.resolve_betfair_timeout_provider_state(
        ledger,
        action,
        profile,
        attempt_id="attempt-1",
        expected_profile_sha256=profile.profile_id,
        readback=second_capture,
    )
    assert (
        second.kind
        is timeout_resolution.BetfairTimeoutResolutionKind.INDETERMINATE_BEFORE_VISIBILITY_HORIZON
    )
    assert second.evidence is None

    # The forged production-global monotonic source was never consulted. If it had
    # been, two immediate captures would have appeared exactly 15 seconds apart.
    assert next(forged_ticks) == 1_000_000_000
