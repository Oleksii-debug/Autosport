from __future__ import annotations

import pytest

from autosport import parlay_sport_catalog_acquisition as catalog


class _HostilePrior(catalog.ParlaySportCatalogAcquisition):
    reads = 0

    def __getattribute__(self, name):
        if name not in {"reads", "__class__"}:
            type(self).reads += 1
        return super().__getattribute__(name)


def test_conditional_prior_subclass_is_rejected_before_virtual_field_dispatch() -> None:
    prior = _HostilePrior(
        acquired_at="2026-09-27T00:00:00+00:00",
        status_code=200,
        final_url=catalog.CANONICAL_PARLAY_SPORTS_URL,
        etag='"v1"',
        raw_body=b"[]",
        raw_body_sha256="4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e4e994e6817e53a90352d95",
        prior_acquisition_id=None,
        acquisition_id="caller-owned",
    )
    _HostilePrior.reads = 0

    with pytest.raises(
        catalog.ParlaySportCatalogEvidenceError,
        match="prior must be exact ParlaySportCatalogAcquisition",
    ):
        catalog.acquire_parlay_sport_catalog(prior=prior)

    assert _HostilePrior.reads == 0
