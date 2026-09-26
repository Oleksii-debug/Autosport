from __future__ import annotations

import pytest

from autosport.parlay_sport_catalog_acquisition import (
    ParlaySportCatalogAcquisition,
    is_product_origin_acquisition,
)


def _caller_constructed_acquisition() -> ParlaySportCatalogAcquisition:
    return ParlaySportCatalogAcquisition(
        acquired_at="2026-09-26T00:00:00+00:00",
        status_code=200,
        final_url="https://parlay-api.com/v1/sports",
        etag='"caller-etag"',
        raw_body=b'{"sports":[]}',
        raw_body_sha256="0" * 64,
        prior_acquisition_id=None,
        acquisition_id="parlay-sports-acquisition:" + "1" * 64,
    )


def test_provider_origin_authority_is_not_caller_mintable_via_object_setattr() -> None:
    """Positive origin must come from product issuance, never caller-writable DTO state."""

    acquisition = _caller_constructed_acquisition()
    assert acquisition.provider_origin_verified is False
    assert is_product_origin_acquisition(acquisition) is False

    with pytest.raises((AttributeError, TypeError)):
        object.__setattr__(acquisition, "provider_origin_verified", True)

    assert acquisition.provider_origin_verified is False
    assert is_product_origin_acquisition(acquisition) is False
