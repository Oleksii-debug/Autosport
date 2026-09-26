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


def _closure_cells(function):
    return dict(zip(function.__code__.co_freevars, function.__closure__ or ()))


def _captured_public_acquirer():
    return _closure_cells(catalog.acquire_parlay_sport_catalog)["public_acquirer"].cell_contents


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
            catalog.acquire_parlay_sport_catalog(transport=_local_response)
    finally:
        handler_type.redirect_request = original


def test_hidden_product_implementation_code_mutation_fails_closed() -> None:
    public_acquirer = _captured_public_acquirer()
    implementation = _closure_cells(public_acquirer)["implementation"].cell_contents
    original_code = implementation.__code__

    def forged_implementation(**_kwargs):
        raise AssertionError("forged hidden implementation executed")

    try:
        implementation.__code__ = forged_implementation.__code__
        with pytest.raises(
            catalog.ParlaySportCatalogEvidenceError,
            match="closure executable changed",
        ):
            catalog.acquire_parlay_sport_catalog(transport=_local_response)
    finally:
        implementation.__code__ = original_code


def test_hidden_product_implementation_origin_issuer_default_mutation_fails_closed() -> None:
    public_acquirer = _captured_public_acquirer()
    implementation = _closure_cells(public_acquirer)["implementation"].cell_contents
    defaults = implementation.__kwdefaults__
    assert defaults is not None
    original_issuer = defaults["origin_issuer"]

    try:
        defaults["origin_issuer"] = lambda value: value
        with pytest.raises(
            catalog.ParlaySportCatalogEvidenceError,
            match="keyword defaults changed",
        ):
            catalog.acquire_parlay_sport_catalog(transport=_local_response)
    finally:
        defaults["origin_issuer"] = original_issuer


def test_product_transport_closure_cell_replacement_fails_closed() -> None:
    transport = catalog._default_transport
    performer_cell = _closure_cells(transport)["performer"]
    original = performer_cell.cell_contents

    try:
        performer_cell.cell_contents = _forged_performer
        with pytest.raises(
            catalog.ParlaySportCatalogEvidenceError,
            match="product transport closure changed",
        ):
            catalog.acquire_parlay_sport_catalog(transport=_local_response)
    finally:
        performer_cell.cell_contents = original
