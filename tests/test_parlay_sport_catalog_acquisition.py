from dataclasses import fields
from email.message import Message
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import pytest

import autosport.parlay_sport_catalog_acquisition as acquisition_module
from autosport.parlay_sport_catalog_acquisition import (
    PARLAY_SPORTS_URL,
    ParlaySportCatalogAcquisition,
    ParlaySportCatalogAcquisitionError,
    ParlaySportCatalogHttpResponse,
    ParlaySportCatalogTransportError,
    acquire_parlay_sport_catalog,
)


NOW = "2026-09-21T10:17:00+00:00"
BODY_A = b'[{"key":"table_tennis"}]'
BODY_B = b'[ { "key": "table_tennis" } ]'


def _response(
    body: bytes = BODY_A,
    *,
    status: int = 200,
    etag: str | None = '"catalog-v1"',
    final_url: str = PARLAY_SPORTS_URL,
) -> ParlaySportCatalogHttpResponse:
    headers = {} if etag is None else {"ETag": etag}
    return ParlaySportCatalogHttpResponse(status, headers, final_url, body)


def _acquire(
    response: ParlaySportCatalogHttpResponse,
    *,
    now: str = NOW,
    **kwargs,
):
    calls = []

    def transport(url, headers, timeout, maximum):
        calls.append((url, dict(headers), timeout, maximum))
        return response

    with patch(
        "autosport.parlay_sport_catalog_acquisition._utc_now_iso",
        return_value=now,
    ):
        evidence = acquire_parlay_sport_catalog(transport=transport, **kwargs)
    return evidence, calls


def _verified_200(
    body: bytes = BODY_A,
    *,
    etag: str | None = '"catalog-v1"',
) -> ParlaySportCatalogAcquisition:
    seen = {}

    def canonical(url, headers, timeout, maximum):
        seen["headers"] = dict(headers)
        return _response(body, etag=etag)

    with (
        patch(
            "autosport.parlay_sport_catalog_acquisition._canonical_transport",
            side_effect=canonical,
        ),
        patch(
            "autosport.parlay_sport_catalog_acquisition._utc_now_iso",
            return_value=NOW,
        ),
    ):
        evidence = acquire_parlay_sport_catalog()
    assert evidence.provider_origin_verified is True
    assert "X-API-Key" not in seen["headers"]
    assert "Authorization" not in seen["headers"]
    return evidence


def test_injected_transport_preserves_exact_bytes_but_never_verifies_origin() -> None:
    evidence, calls = _acquire(_response())

    assert calls == [
        (
            PARLAY_SPORTS_URL,
            {
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": "Autosport/0.1 read-only-sport-catalog",
            },
            10.0,
            1_048_576,
        )
    ]
    assert evidence.payload_bytes == BODY_A
    assert evidence.has_new_payload is True
    assert evidence.provider_origin_verified is False
    assert evidence.payload_root_acquisition_id == evidence.acquisition_id
    assert evidence.provider_write_authorized is False
    assert evidence.product_sport_admission_authorized is False
    assert evidence.real_money_execution_authorized is False


def test_internal_fixed_origin_path_can_verify_origin_without_secret_headers() -> None:
    evidence = _verified_200()

    assert evidence.requested_url == PARLAY_SPORTS_URL
    assert evidence.final_url == PARLAY_SPORTS_URL
    assert evidence.http_status == 200
    assert evidence.provider_origin_verified is True


@pytest.mark.parametrize(
    "wrong",
    [
        "http://parlay-api.com/v1/sports",
        "https://parlay-api.com/v1/sports/",
        "https://www.parlay-api.com/v1/sports",
        "https://parlay-api.com/v1/sports?x=1",
        "https://example.com/v1/sports",
    ],
)
def test_redirect_or_wrong_final_origin_is_rejected(wrong: str) -> None:
    with pytest.raises(ParlaySportCatalogAcquisitionError, match="final_url"):
        _acquire(_response(final_url=wrong))


def test_response_size_is_bounded_even_for_injected_transport() -> None:
    with pytest.raises(ParlaySportCatalogAcquisitionError, match="maximum response size"):
        _acquire(_response(body=b"x" * 9), max_response_bytes=8)


@pytest.mark.parametrize("status", [199, 201, 204, 301, 400, 429, 500])
def test_only_200_or_304_are_admissible(status: int) -> None:
    with pytest.raises(ParlaySportCatalogAcquisitionError, match="requires HTTP 200 or 304"):
        _acquire(_response(status=status))


def test_304_requires_exact_prior_etag_and_sends_only_that_conditional_value() -> None:
    prior, _ = _acquire(_response(etag='"catalog-v1"'))
    response = _response(body=b"", status=304, etag='"catalog-v1"')
    current, calls = _acquire(response, previous=prior)

    assert calls[0][1]["If-None-Match"] == '"catalog-v1"'
    assert "X-API-Key" not in calls[0][1]
    assert "Authorization" not in calls[0][1]
    assert current.http_status == 304
    assert current.payload_bytes is None
    assert current.payload_sha256 == prior.payload_sha256
    assert current.previous_acquisition_id == prior.acquisition_id
    assert current.payload_root_acquisition_id == prior.acquisition_id
    assert current.provider_origin_verified is False


def test_304_without_prior_or_without_prior_etag_fails_closed() -> None:
    response = _response(body=b"", status=304, etag=None)
    with pytest.raises(ParlaySportCatalogAcquisitionError, match="prior acquisition with ETag"):
        _acquire(response)

    prior, _ = _acquire(_response(etag=None))
    with pytest.raises(ParlaySportCatalogAcquisitionError, match="prior acquisition with ETag"):
        _acquire(response, previous=prior)


def test_304_never_fabricates_new_payload_and_rejects_etag_contradiction() -> None:
    prior, _ = _acquire(_response(etag='"catalog-v1"'))

    with pytest.raises(ParlaySportCatalogAcquisitionError, match="must not contain payload bytes"):
        _acquire(_response(body=b"[]", status=304, etag='"catalog-v1"'), previous=prior)

    with pytest.raises(ParlaySportCatalogAcquisitionError, match="contradicts"):
        _acquire(_response(body=b"", status=304, etag='"catalog-v2"'), previous=prior)


def test_verified_304_requires_both_canonical_response_and_verified_payload_ancestry() -> None:
    prior = _verified_200()

    def canonical(url, headers, timeout, maximum):
        assert headers["If-None-Match"] == '"catalog-v1"'
        return _response(body=b"", status=304, etag=None)

    with (
        patch(
            "autosport.parlay_sport_catalog_acquisition._canonical_transport",
            side_effect=canonical,
        ),
        patch(
            "autosport.parlay_sport_catalog_acquisition._utc_now_iso",
            return_value="2026-09-21T10:18:00+00:00",
        ),
    ):
        current = acquire_parlay_sport_catalog(previous=prior)

    assert current.provider_origin_verified is True
    assert current.payload_bytes is None
    assert current.payload_root_acquisition_id == prior.acquisition_id


def test_canonical_304_does_not_launder_unverified_prior_payload() -> None:
    prior, _ = _acquire(_response(etag='"catalog-v1"'))
    assert prior.provider_origin_verified is False

    with (
        patch(
            "autosport.parlay_sport_catalog_acquisition._canonical_transport",
            return_value=_response(body=b"", status=304, etag='"catalog-v1"'),
        ),
        patch(
            "autosport.parlay_sport_catalog_acquisition._utc_now_iso",
            return_value="2026-09-21T10:18:00+00:00",
        ),
    ):
        current = acquire_parlay_sport_catalog(previous=prior)

    assert current.provider_origin_verified is False


def test_304_chain_retains_the_original_200_payload_root() -> None:
    root, _ = _acquire(_response(etag='"catalog-v1"'))
    mid, _ = _acquire(_response(body=b"", status=304, etag=None), previous=root)
    tail, _ = _acquire(_response(body=b"", status=304, etag=None), previous=mid)

    assert mid.payload_root_acquisition_id == root.acquisition_id
    assert tail.payload_root_acquisition_id == root.acquisition_id
    assert tail.previous_acquisition_id == mid.acquisition_id


def test_changed_raw_bytes_are_distinct_acquisitions_even_with_same_timestamp_and_etag() -> None:
    first, _ = _acquire(_response(body=BODY_A))
    second, _ = _acquire(_response(body=BODY_B))

    assert first.payload_sha256 != second.payload_sha256
    assert first.acquisition_id != second.acquisition_id


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"timeout_seconds": float("nan")}, "timeout_seconds"),
        ({"timeout_seconds": True}, "timeout_seconds"),
        ({"max_response_bytes": 0}, "max_response_bytes"),
        ({"max_response_bytes": True}, "max_response_bytes"),
    ],
)
def test_runtime_bounds_are_strict(kwargs, message: str) -> None:
    with pytest.raises(ParlaySportCatalogAcquisitionError, match=message):
        acquire_parlay_sport_catalog(
            transport=lambda *_: _response(),
            **kwargs,
        )


def test_etag_rejects_control_characters_before_reuse_as_request_header() -> None:
    with pytest.raises(ParlaySportCatalogAcquisitionError, match="control characters"):
        _acquire(_response(etag='"bad\r\nvalue"'))


def test_contract_authority_flags_cannot_be_widened_by_constructor() -> None:
    prior, _ = _acquire(_response())
    values = {
        field.name: getattr(prior, field.name)
        for field in fields(ParlaySportCatalogAcquisition)
    }
    for name in (
        "provider_write_authorized",
        "product_sport_admission_authorized",
        "real_money_execution_authorized",
    ):
        altered = dict(values)
        altered[name] = True
        with pytest.raises(ParlaySportCatalogAcquisitionError):
            ParlaySportCatalogAcquisition(**altered)


def test_direct_constructor_cannot_mint_verified_provider_origin() -> None:
    prior, _ = _acquire(_response())
    values = {
        field.name: getattr(prior, field.name)
        for field in fields(ParlaySportCatalogAcquisition)
    }
    values["provider_origin_verified"] = True
    with pytest.raises(
        ParlaySportCatalogAcquisitionError,
        match="canonical acquisition path",
    ):
        ParlaySportCatalogAcquisition(**values)


def test_conditional_sequence_fails_closed_on_clock_regression() -> None:
    prior, _ = _acquire(
        _response(etag='"catalog-v1"'),
        now="2026-09-21T10:18:00+00:00",
    )
    with pytest.raises(ParlaySportCatalogAcquisitionError, match="clock regressed"):
        _acquire(
            _response(body=b"", status=304, etag='"catalog-v1"'),
            previous=prior,
            now="2026-09-21T10:17:59+00:00",
        )


class _FakeUrlResponse:
    def __init__(
        self,
        body: object,
        *,
        status: int = 200,
        final_url: str = PARLAY_SPORTS_URL,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._body = body
        self.status = status
        self._final_url = final_url
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def geturl(self) -> str:
        return self._final_url

    def read(self, amount: int):
        if isinstance(self._body, bytes):
            return self._body[:amount]
        return self._body


def test_canonical_transport_uses_get_and_preserves_raw_http_evidence() -> None:
    fake = _FakeUrlResponse(
        BODY_A,
        headers={"Content-Length": str(len(BODY_A)), "ETag": '"catalog-v1"'},
    )
    captured = {}

    def open_fake(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return fake

    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "User-Agent": "Autosport/0.1 read-only-sport-catalog",
    }
    with patch.object(acquisition_module, "urlopen", side_effect=open_fake):
        response = acquisition_module._canonical_transport(
            PARLAY_SPORTS_URL,
            headers,
            3.5,
            1024,
        )

    assert captured["request"].full_url == PARLAY_SPORTS_URL
    assert captured["request"].get_method() == "GET"
    assert captured["timeout"] == 3.5
    assert response.status_code == 200
    assert response.final_url == PARLAY_SPORTS_URL
    assert response.body == BODY_A
    assert response.headers["ETag"] == '"catalog-v1"'


def test_canonical_transport_rejects_declared_or_actual_oversize_body() -> None:
    declared = _FakeUrlResponse(
        b"[]",
        headers={"Content-Length": "999"},
    )
    with patch.object(acquisition_module, "urlopen", return_value=declared):
        with pytest.raises(ParlaySportCatalogAcquisitionError, match="maximum response size"):
            acquisition_module._canonical_transport(
                PARLAY_SPORTS_URL,
                {},
                1.0,
                8,
            )

    actual = _FakeUrlResponse(b"123456789")
    with patch.object(acquisition_module, "urlopen", return_value=actual):
        with pytest.raises(ParlaySportCatalogAcquisitionError, match="maximum response size"):
            acquisition_module._canonical_transport(
                PARLAY_SPORTS_URL,
                {},
                1.0,
                8,
            )


def test_canonical_transport_rejects_nonbyte_body() -> None:
    fake = _FakeUrlResponse("not-bytes")
    with patch.object(acquisition_module, "urlopen", return_value=fake):
        with pytest.raises(ParlaySportCatalogAcquisitionError, match="exact bytes"):
            acquisition_module._canonical_transport(
                PARLAY_SPORTS_URL,
                {},
                1.0,
                1024,
            )


def test_canonical_transport_preserves_304_for_conditional_validation() -> None:
    headers = Message()
    headers["ETag"] = '"catalog-v1"'
    error = HTTPError(
        PARLAY_SPORTS_URL,
        304,
        "Not Modified",
        headers,
        BytesIO(b""),
    )
    with patch.object(acquisition_module, "urlopen", side_effect=error):
        response = acquisition_module._canonical_transport(
            PARLAY_SPORTS_URL,
            {"If-None-Match": '"catalog-v1"'},
            1.0,
            1024,
        )

    assert response.status_code == 304
    assert response.body == b""
    assert response.final_url == PARLAY_SPORTS_URL
    assert response.headers["ETag"] == '"catalog-v1"'


def test_canonical_transport_maps_http_and_network_failure_to_typed_error() -> None:
    server_error = HTTPError(
        PARLAY_SPORTS_URL,
        503,
        "Unavailable",
        Message(),
        BytesIO(b""),
    )
    with patch.object(acquisition_module, "urlopen", side_effect=server_error):
        with pytest.raises(ParlaySportCatalogTransportError, match="HTTP 503"):
            acquisition_module._canonical_transport(
                PARLAY_SPORTS_URL,
                {},
                1.0,
                1024,
            )

    with patch.object(
        acquisition_module,
        "urlopen",
        side_effect=URLError("network down"),
    ):
        with pytest.raises(ParlaySportCatalogTransportError, match="network down"):
            acquisition_module._canonical_transport(
                PARLAY_SPORTS_URL,
                {},
                1.0,
                1024,
            )
