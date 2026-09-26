from __future__ import annotations

import autosport.parlay_sport_catalog_acquisition as catalog


def test_product_origin_issuer_is_not_caller_invokable_module_authority() -> None:
    """The private issuance callable must not remain reachable from module globals.

    Positive provider-origin authority is meaningful only if callers cannot invoke the
    issuer directly. The product acquirer already captures issuance authority during
    module initialization, so the mutable/reachable module binding is unnecessary.
    """

    acquisition = catalog.ParlaySportCatalogAcquisition(
        acquired_at="2026-09-26T00:00:00+00:00",
        status_code=200,
        final_url=catalog.CANONICAL_PARLAY_SPORTS_URL,
        etag=None,
        raw_body=b"{}",
        raw_body_sha256="0" * 64,
        prior_acquisition_id=None,
        acquisition_id="parlay-sports-acquisition:" + "1" * 64,
    )

    assert catalog.is_product_origin_acquisition(acquisition) is False
    assert not hasattr(catalog, "_issue_product_origin")
    assert catalog.is_product_origin_acquisition(acquisition) is False
