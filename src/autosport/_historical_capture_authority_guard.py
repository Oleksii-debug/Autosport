from __future__ import annotations

"""Fail-closed authority boundary for provider historical captures.

Artifact capture remains injectable for tests/tools.  Positive point-in-time source
authority, however, must not be minted from caller-owned Python state.  Python
function closures, private module attributes, object identity and locally persisted
hashes are all inspectable/reconstructible by ordinary in-process callers, so none
of them is an origin-attestation boundary.

Until the historical provider path supplies independently re-resolvable canonical
production-origin evidence, historical captures remain useful artifacts/audit
inputs but cannot become positive availability authority.
"""

from pathlib import Path

from . import historical_snapshot as _historical
from .parlayapi_provider import ParlayApiTableTennisProvider, ProviderPayloadError


_artifact_capture = _historical.capture_historical_snapshot


def capture_historical_snapshot(
    provider: ParlayApiTableTennisProvider,
    *,
    requested_at: str,
    output_path: str | Path,
    evidence_path: str | Path | None = None,
) -> _historical.HistoricalSnapshotCapture:
    """Create historical artifacts without minting positive source authority."""

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
    """Fail closed until production origin can be independently re-resolved.

    Keeping this API explicit prevents downstream callers from silently falling
    back to the injectable artifact path.  Do not replace this with a local token,
    closure registry, private helper or caller-recomputable digest.
    """

    del api_key, requested_at, output_path, evidence_path, regions, markets
    raise ProviderPayloadError(
        "historical snapshot positive authority requires independently re-resolved "
        "canonical production-origin evidence"
    )


def assert_historical_snapshot_capture_authoritative(
    capture: _historical.HistoricalSnapshotCapture,
) -> None:
    """Reject local capture objects as positive origin authority."""

    if not isinstance(capture, _historical.HistoricalSnapshotCapture):
        raise ProviderPayloadError("historical snapshot evidence type is not canonical")
    raise ProviderPayloadError(
        "historical snapshot positive authority requires independently re-resolved "
        "canonical production-origin evidence"
    )


# Replace the legacy in-process identity issuer after its installer has run.
_historical.capture_historical_snapshot = capture_historical_snapshot
_historical.capture_authoritative_historical_snapshot = (
    capture_authoritative_historical_snapshot
)
_historical.assert_historical_snapshot_capture_authoritative = (
    assert_historical_snapshot_capture_authoritative
)


__all__ = [
    "capture_historical_snapshot",
    "capture_authoritative_historical_snapshot",
    "assert_historical_snapshot_capture_authoritative",
]
