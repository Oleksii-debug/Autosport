from __future__ import annotations

from datetime import datetime, timezone
import hashlib

import pytest

from autosport.parlay_surface_capability import (
    ParlaySurfaceObservation,
    Surface,
    SurfaceCapabilityError,
)


UTC = timezone.utc
T0 = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
DIGEST = hashlib.sha256(b"provider-bytes").hexdigest()


@pytest.mark.parametrize("reserved_sport", ["unknown", "mixed"])
def test_surface_observation_rejects_reserved_dataset_scope_sports(
    reserved_sport: str,
) -> None:
    with pytest.raises(SurfaceCapabilityError, match="sport_key"):
        ParlaySurfaceObservation(
            sport_key=reserved_sport,
            surface=Surface.ODDS,
            observed_at=T0,
            available_at=T0,
            status_code=200,
            response_sha256=DIGEST,
            row_count=1,
            pagination_complete=True,
            requested_market="h2h",
            served_markets=("h2h",),
            oldest_row_age_seconds=1,
        )
