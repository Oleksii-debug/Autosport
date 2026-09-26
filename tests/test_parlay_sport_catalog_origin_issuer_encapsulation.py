from __future__ import annotations

import autosport.parlay_sport_catalog_acquisition as catalog


def test_product_origin_issuer_is_not_module_reachable() -> None:
    """Positive provider origin must not expose a caller-invokable mint capability."""

    forged = catalog.ParlaySportCatalogAcquisition(
        acquired_at="2026-09-26T00:00:00+00:00",
        status_code=200,
        final_url=catalog.CANONICAL_PARLAY_SPORTS_URL,
        etag=None,
        raw_body=b"[]",
        raw_body_sha256="4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e6f35c7d3f0f5a4f6f8f3f0",
        prior_acquisition_id=None,
        acquisition_id="caller-forged",
    )

    assert catalog.is_product_origin_acquisition(forged) is False
    assert not hasattr(catalog, "_issue_product_origin")
    assert catalog.is_product_origin_acquisition(forged) is False
