from __future__ import annotations

import pytest

import autosport.parlay_sport_catalog_acquisition as catalog


def _forged_performer(
    url,
    headers,
    timeout_seconds,
    max_response_bytes,
    **kwargs,
):
    del headers, timeout_seconds, max_response_bytes, kwargs
    return catalog.RawCatalogHttpResponse(
        status_code=200,
        headers=(),
        body=b'[{"sport":"forged"}]',
        final_url=url,
    )


def _local_response(url, _headers, _timeout, _limit):
    return catalog.RawCatalogHttpResponse(
        status_code=200,
        headers=(),
        body=b"[]",
        final_url=url,
    )


def test_in_place_product_transport_executable_mutation_fails_closed() -> None:
    performer = catalog._perform_catalog_http_response
    original_code = performer.__code__
    try:
        performer.__code__ = _forged_performer.__code__
        with pytest.raises(catalog.ParlaySportCatalogEvidenceError):
            catalog.acquire_parlay_sport_catalog()
    finally:
        performer.__code__ = original_code


def test_product_clock_executable_mutation_fails_closed_before_origin_issuance() -> None:
    clock = catalog._utc_now_iso
    original_code = clock.__code__

    def forged_clock():
        return "2026-09-26T00:00:00+00:00"

    try:
        clock.__code__ = forged_clock.__code__
        with pytest.raises(catalog.ParlaySportCatalogEvidenceError):
            catalog.acquire_parlay_sport_catalog(transport=_local_response)
    finally:
        clock.__code__ = original_code


def test_product_redirect_refusal_dispatch_mutation_fails_closed() -> None:
    handler_type = catalog._PRODUCT_REDIRECT_HANDLER
    original = handler_type.redirect_request

    def allow_redirect(self, req, fp, code, msg, headers, newurl):
        del self, req, fp, code, msg, headers, newurl
        return None

    try:
        handler_type.redirect_request = allow_redirect
        with pytest.raises(
            catalog.ParlaySportCatalogEvidenceError,
            match="product acquirer dispatch changed",
        ):
            # Injected transport avoids network I/O; the guard must reject the mutated
            # product redirect authority before any acquisition path is entered.
            catalog.acquire_parlay_sport_catalog(transport=_local_response)
    finally:
        handler_type.redirect_request = original
