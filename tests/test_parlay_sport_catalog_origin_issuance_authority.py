from __future__ import annotations

import pytest

from autosport.parlay_sport_catalog_acquisition import ParlaySportCatalogAcquisition


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
    """A frozen dataclass flag must not be the positive provider-origin authority.

    ``frozen=True`` only routes ordinary assignment through a guard; callers can invoke
    ``object.__setattr__`` directly.  If the public ``provider_origin_verified`` value
    is intended to authorize downstream provider-origin composition, that value must
    therefore be backed by product issuance/verification rather than a mutable slot.
    """

    acquisition = _caller_constructed_acquisition()
    assert acquisition.provider_origin_verified is False

    with pytest.raises((AttributeError, TypeError)):
        object.__setattr__(acquisition, "provider_origin_verified", True)

    assert acquisition.provider_origin_verified is False
