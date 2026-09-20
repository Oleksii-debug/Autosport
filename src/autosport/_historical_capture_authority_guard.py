from __future__ import annotations

"""Narrow authority boundary for provider historical captures.

Artifact capture remains injectable for tests/tools, but only the production-owned
constructor path may mint an in-process capability that downstream point-in-time
authority accepts.  In particular, caller supplied transports, clocks, base URLs,
or provider instances can never decide their own ``available_at`` authority.
"""

from pathlib import Path
from weakref import ref

from . import historical_snapshot as _historical
from .parlayapi_provider import ParlayApiTableTennisProvider, ProviderPayloadError


_artifact_capture = _historical.capture_historical_snapshot
_issued: dict[int, tuple[object, str]] = {}


def _remember(capture: _historical.HistoricalSnapshotCapture) -> None:
    key = id(capture)

    def forget(_weakref: object, *, capture_key: int = key) -> None:
        _issued.pop(capture_key, None)

    _issued[key] = (
        ref(capture, forget),
        _historical._historical_snapshot_capture_fingerprint(capture),
    )


def capture_historical_snapshot(
    provider: ParlayApiTableTennisProvider,
    *,
    requested_at: str,
    output_path: str | Path,
    evidence_path: str | Path | None = None,
) -> _historical.HistoricalSnapshotCapture:
    """Create artifacts from a caller-owned provider without minting authority."""

    return _artifact_capture(
        provider,
        requested_at=requested_at,
        output_path=output_path,
        evidence_path=evidence_path,
    )


def capture_authoritative_historical_snapshot(
    *,
    api_key: str,
    requested_at: str,
    output_path: str | Path,
    evidence_path: str | Path | None = None,
    regions: tuple[str, ...] = ("us",),
    markets: tuple[str, ...] = ("h2h", "spreads", "totals"),
) -> _historical.HistoricalSnapshotCapture:
    """Capture through the fixed production transport/clock boundary and mint authority."""

    provider = ParlayApiTableTennisProvider(
        api_key,
        regions=regions,
        markets=markets,
    )
    capture = _artifact_capture(
        provider,
        requested_at=requested_at,
        output_path=output_path,
        evidence_path=evidence_path,
    )
    _remember(capture)
    return capture


def assert_historical_snapshot_capture_authoritative(
    capture: _historical.HistoricalSnapshotCapture,
) -> None:
    if not isinstance(capture, _historical.HistoricalSnapshotCapture):
        raise ProviderPayloadError("historical snapshot evidence type is not canonical")
    record = _issued.get(id(capture))
    if record is None or record[0]() is not capture:
        raise ProviderPayloadError(
            "historical snapshot capture was not issued by canonical production capture path"
        )
    if record[1] != _historical._historical_snapshot_capture_fingerprint(capture):
        raise ProviderPayloadError(
            "historical snapshot capture changed after canonical production capture"
        )


# Replace the permissive historical module capability after its legacy installer
# has run.  Imports performed later (notably point_in_time_authority) receive only
# this stricter boundary.
_historical.capture_historical_snapshot = capture_historical_snapshot
_historical.capture_authoritative_historical_snapshot = (
    capture_authoritative_historical_snapshot
)
_historical.assert_historical_snapshot_capture_authoritative = (
    assert_historical_snapshot_capture_authoritative
)
