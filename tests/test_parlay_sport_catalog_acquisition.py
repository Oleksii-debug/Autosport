from __future__ import annotations

import hashlib
import unittest
from unittest import mock

import autosport.parlay_sport_catalog_acquisition as acquisition_module
from autosport.parlay_sport_catalog_acquisition import (
    CANONICAL_PARLAY_SPORTS_URL,
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    ParlaySportCatalogAcquisition,
    ParlaySportCatalogEvidenceError,
    ParlaySportCatalogTransportError,
    RawCatalogHttpResponse,
    acquire_parlay_sport_catalog,
)


NOW = "2026-09-21T10:30:00+00:00"


class RecordingTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, timeout_seconds, max_response_bytes):
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "timeout_seconds": timeout_seconds,
                "max_response_bytes": max_response_bytes,
            }
        )
        return self.responses.pop(0)


def response(*, status=200, body=b"[]", etag='"catalog-v1"', final_url=CANONICAL_PARLAY_SPORTS_URL):
    headers = () if etag is None else (("ETag", etag),)
    return RawCatalogHttpResponse(
        status_code=status,
        headers=headers,
        body=body,
        final_url=final_url,
    )


class ParlaySportCatalogAcquisitionTests(unittest.TestCase):
    def acquire(self, transport, *, prior=None, max_response_bytes=1024):
        return acquire_parlay_sport_catalog(
            prior=prior,
            transport=transport,
            clock=lambda: NOW,
            timeout_seconds=3.5,
            max_response_bytes=max_response_bytes,
        )

    def test_fixed_read_only_request_sends_no_api_key_or_authorization(self):
        transport = RecordingTransport([response()])
        result = self.acquire(transport)

        self.assertEqual(result.raw_body, b"[]")
        self.assertFalse(result.provider_origin_verified)
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertEqual(call["url"], CANONICAL_PARLAY_SPORTS_URL)
        self.assertEqual(call["timeout_seconds"], 3.5)
        self.assertEqual(call["max_response_bytes"], 1024)
        self.assertEqual(
            call["headers"],
            {
                "Accept": "application/json",
                "User-Agent": "Autosport/0.1 read-only-sport-catalog",
            },
        )
        self.assertNotIn("Authorization", call["headers"])
        self.assertNotIn("X-API-Key", call["headers"])

    def test_exact_response_bytes_and_digest_are_preserved(self):
        raw = b'[ { "key": "soccer_epl" } ]\n'
        transport = RecordingTransport([response(body=raw)])
        result = self.acquire(transport)

        self.assertIsInstance(result.raw_body, bytes)
        self.assertEqual(result.raw_body, raw)
        self.assertEqual(result.raw_body_sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.etag, '"catalog-v1"')
        self.assertIsNone(result.prior_acquisition_id)

    def test_changed_raw_bytes_produce_distinct_acquisition_identity(self):
        first = self.acquire(RecordingTransport([response(body=b'[{"key":"a"}]')]))
        second = self.acquire(RecordingTransport([response(body=b'[ { "key": "a" } ]')]))

        self.assertNotEqual(first.raw_body_sha256, second.raw_body_sha256)
        self.assertNotEqual(first.acquisition_id, second.acquisition_id)

    def test_injected_transport_cannot_mint_positive_provider_origin(self):
        result = self.acquire(RecordingTransport([response()]))

        self.assertFalse(result.provider_origin_verified)

    def test_wrong_final_origin_or_path_is_rejected(self):
        for final_url in (
            "http://parlay-api.com/v1/sports",
            "https://example.com/v1/sports",
            "https://parlay-api.com/v1/sports/",
            "https://parlay-api.com/v1/sports?x=1",
        ):
            with self.subTest(final_url=final_url):
                with self.assertRaises(ParlaySportCatalogEvidenceError):
                    self.acquire(
                        RecordingTransport([response(final_url=final_url)])
                    )

    def test_oversized_transport_response_is_rejected_even_for_injected_transport(self):
        with self.assertRaisesRegex(
            ParlaySportCatalogEvidenceError, "exceeds bounded size"
        ):
            self.acquire(
                RecordingTransport([response(body=b"x" * 17)]),
                max_response_bytes=16,
            )

    def test_304_without_exact_prior_and_conditional_etag_fails_closed(self):
        with self.assertRaisesRegex(
            ParlaySportCatalogEvidenceError, "exact prior acquisition"
        ):
            self.acquire(RecordingTransport([response(status=304, body=b"")]))

        prior_without_etag = self.acquire(
            RecordingTransport([response(etag=None)])
        )
        with self.assertRaisesRegex(
            ParlaySportCatalogEvidenceError, "If-None-Match"
        ):
            self.acquire(
                RecordingTransport([response(status=304, body=b"", etag=None)]),
                prior=prior_without_etag,
            )

    def test_304_references_prior_exact_acquisition_and_never_fabricates_body(self):
        prior = self.acquire(
            RecordingTransport([response(body=b'[{"key":"a"}]', etag='"v1"')])
        )
        transport = RecordingTransport(
            [response(status=304, body=b"", etag='"v1"')]
        )
        result = self.acquire(transport, prior=prior)

        self.assertTrue(result.is_not_modified)
        self.assertIsNone(result.raw_body)
        self.assertIsNone(result.raw_body_sha256)
        self.assertEqual(result.prior_acquisition_id, prior.acquisition_id)
        self.assertFalse(result.provider_origin_verified)
        self.assertEqual(transport.calls[0]["headers"]["If-None-Match"], '"v1"')

    def test_304_with_body_is_rejected(self):
        prior = self.acquire(
            RecordingTransport([response(body=b"[]", etag='"v1"')])
        )
        with self.assertRaisesRegex(
            ParlaySportCatalogEvidenceError, "must not carry a catalog body"
        ):
            self.acquire(
                RecordingTransport(
                    [response(status=304, body=b"unexpected", etag='"v1"')]
                ),
                prior=prior,
            )

    def test_tampered_prior_body_is_rejected_before_conditional_request(self):
        prior = self.acquire(
            RecordingTransport([response(body=b"[]", etag='"v1"')])
        )
        object.__setattr__(prior, "raw_body", b"[1]")
        transport = RecordingTransport([response(status=304, body=b"", etag='"v1"')])

        with self.assertRaisesRegex(
            ParlaySportCatalogEvidenceError, "raw-body digest mismatch"
        ):
            self.acquire(transport, prior=prior)
        self.assertEqual(transport.calls, [])

    def test_tampered_prior_acquisition_identity_is_rejected_before_request(self):
        prior = self.acquire(
            RecordingTransport([response(body=b"[]", etag='"v1"')])
        )
        object.__setattr__(prior, "acquisition_id", "forged")
        transport = RecordingTransport([response(status=304, body=b"", etag='"v1"')])

        with self.assertRaisesRegex(
            ParlaySportCatalogEvidenceError, "identity mismatch"
        ):
            self.acquire(transport, prior=prior)
        self.assertEqual(transport.calls, [])

    def test_duplicate_etag_headers_are_rejected(self):
        duplicate = RawCatalogHttpResponse(
            status_code=200,
            headers=(("ETag", '"a"'), ("etag", '"b"')),
            body=b"[]",
            final_url=CANONICAL_PARLAY_SPORTS_URL,
        )
        with self.assertRaisesRegex(
            ParlaySportCatalogEvidenceError, "duplicate ETag"
        ):
            self.acquire(RecordingTransport([duplicate]))

    def test_naive_acquisition_timestamp_is_rejected(self):
        with self.assertRaisesRegex(
            ParlaySportCatalogEvidenceError, "timezone-aware"
        ):
            acquire_parlay_sport_catalog(
                transport=RecordingTransport([response()]),
                clock=lambda: "2026-09-21T10:30:00",
            )

    def test_runtime_bounds_reject_bool_nan_and_nonpositive_values(self):
        for bad_timeout in (True, 0, -1, float("nan"), float("inf")):
            with self.subTest(timeout=bad_timeout):
                with self.assertRaises(ValueError):
                    acquire_parlay_sport_catalog(
                        transport=RecordingTransport([response()]),
                        timeout_seconds=bad_timeout,
                        clock=lambda: NOW,
                    )
        for bad_size in (True, 0, -1, 1.5):
            with self.subTest(size=bad_size):
                with self.assertRaises(ValueError):
                    acquire_parlay_sport_catalog(
                        transport=RecordingTransport([response()]),
                        max_response_bytes=bad_size,
                        clock=lambda: NOW,
                    )

    def test_runtime_bounds_are_tighten_only_product_limits(self):
        for bad_timeout in (
            DEFAULT_TIMEOUT_SECONDS + 0.0001,
            DEFAULT_TIMEOUT_SECONDS * 2,
        ):
            with self.subTest(timeout=bad_timeout):
                transport = RecordingTransport([response()])
                with self.assertRaisesRegex(ValueError, "product maximum"):
                    acquire_parlay_sport_catalog(
                        transport=transport,
                        timeout_seconds=bad_timeout,
                        clock=lambda: NOW,
                    )
                self.assertEqual(transport.calls, [])

        for bad_size in (
            DEFAULT_MAX_RESPONSE_BYTES + 1,
            DEFAULT_MAX_RESPONSE_BYTES * 2,
        ):
            with self.subTest(max_response_bytes=bad_size):
                transport = RecordingTransport([response()])
                with self.assertRaisesRegex(ValueError, "product maximum"):
                    acquire_parlay_sport_catalog(
                        transport=transport,
                        max_response_bytes=bad_size,
                        clock=lambda: NOW,
                    )
                self.assertEqual(transport.calls, [])

        result = acquire_parlay_sport_catalog(
            transport=RecordingTransport([response()]),
            timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
            max_response_bytes=DEFAULT_MAX_RESPONSE_BYTES,
            clock=lambda: NOW,
        )
        self.assertEqual(result.status_code, 200)

    def test_304_cannot_regress_causal_acquisition_time(self):
        prior = acquire_parlay_sport_catalog(
            transport=RecordingTransport([response(body=b"[]", etag='"v1"')]),
            timeout_seconds=3.5,
            max_response_bytes=1024,
            clock=lambda: "2026-09-21T10:30:00+00:00",
        )
        transport = RecordingTransport(
            [response(status=304, body=b"", etag='"v1"')]
        )

        with self.assertRaisesRegex(
            ParlaySportCatalogEvidenceError, "cannot precede"
        ):
            acquire_parlay_sport_catalog(
                prior=prior,
                transport=transport,
                timeout_seconds=3.5,
                max_response_bytes=1024,
                clock=lambda: "2026-09-21T10:29:59+00:00",
            )
        self.assertEqual(len(transport.calls), 1)

    def test_equivalent_timezone_instant_is_not_clock_regression(self):
        prior = acquire_parlay_sport_catalog(
            transport=RecordingTransport([response(body=b"[]", etag='"v1"')]),
            timeout_seconds=3.5,
            max_response_bytes=1024,
            clock=lambda: "2026-09-21T10:30:00+00:00",
        )
        result = acquire_parlay_sport_catalog(
            prior=prior,
            transport=RecordingTransport(
                [response(status=304, body=b"", etag='"v1"')]
            ),
            timeout_seconds=3.5,
            max_response_bytes=1024,
            clock=lambda: "2026-09-21T12:30:00+02:00",
        )
        self.assertTrue(result.is_not_modified)

    def test_default_transport_blocks_redirect_before_second_hop(self):
        captured = {}

        class SimulatedRedirectOpener:
            def __init__(self, handler):
                self.handler = handler
                self.second_hop_attempted = False

            def open(self, request, timeout):
                self.handler.redirect_request(
                    request,
                    None,
                    302,
                    "Found",
                    {"Location": "https://example.invalid/x"},
                    "https://example.invalid/x",
                )
                self.second_hop_attempted = True
                raise AssertionError("redirect handler allowed a second hop")

        def fake_build_opener(handler):
            opener = SimulatedRedirectOpener(handler)
            captured["opener"] = opener
            return opener

        with self.assertRaisesRegex(
            ParlaySportCatalogTransportError,
            "redirect blocked before second hop",
        ):
            acquisition_module._perform_catalog_http_response(
                CANONICAL_PARLAY_SPORTS_URL,
                {"Accept": "application/json"},
                1.0,
                1024,
                request_factory=acquisition_module.urllib.request.Request,
                opener_factory=fake_build_opener,
                redirect_handler_factory=acquisition_module._PRODUCT_REDIRECT_HANDLER,
                bounded_reader=acquisition_module._bounded_read,
                response_factory=RawCatalogHttpResponse,
                evidence_error_type=ParlaySportCatalogEvidenceError,
                transport_error_type=ParlaySportCatalogTransportError,
                http_error_type=acquisition_module.HTTPError,
                url_error_type=acquisition_module.URLError,
                canonical_url=CANONICAL_PARLAY_SPORTS_URL,
            )

        self.assertFalse(captured["opener"].second_hop_attempted)

    def test_public_acquirer_captures_product_transport_against_module_rebind(self):
        original_transport = acquisition_module._default_transport
        fake_transport = RecordingTransport([response(body=b'[{"key":"forged"}]')])
        before = tuple(
            cell.cell_contents
            for cell in (acquire_parlay_sport_catalog.__closure__ or ())
        )
        self.assertIn(original_transport, before)

        with mock.patch.object(
            acquisition_module,
            "_default_transport",
            fake_transport,
        ):
            during = tuple(
                cell.cell_contents
                for cell in (acquire_parlay_sport_catalog.__closure__ or ())
            )

        self.assertIn(original_transport, during)
        self.assertNotIn(fake_transport, during)
        self.assertEqual(fake_transport.calls, [])

    def test_public_acquirer_captures_authority_validators_against_module_rebind(self):
        implementation = next(
            cell.cell_contents
            for cell in (acquire_parlay_sport_catalog.__closure__ or ())
            if getattr(cell.cell_contents, "__name__", "")
            == "_acquire_parlay_sport_catalog_impl"
        )
        defaults = implementation.__kwdefaults__
        self.assertIsNotNone(defaults)
        original_validator = acquisition_module._validate_raw_response
        original_digest_factory = acquisition_module.hashlib.sha256
        self.assertIs(defaults["raw_response_validator"], original_validator)
        self.assertIs(defaults["digest_factory"], original_digest_factory)

        fake_validator = lambda *_args, **_kwargs: response(
            body=b'[{"key":"forged"}]'
        )
        fake_digest_factory = lambda _payload: None
        with (
            mock.patch.object(
                acquisition_module,
                "_validate_raw_response",
                fake_validator,
            ),
            mock.patch.object(
                acquisition_module.hashlib,
                "sha256",
                fake_digest_factory,
            ),
        ):
            self.assertIs(
                implementation.__kwdefaults__["raw_response_validator"],
                original_validator,
            )
            self.assertIs(
                implementation.__kwdefaults__["digest_factory"],
                original_digest_factory,
            )

    def test_product_transport_captures_lower_network_dependencies(self):
        product_transport = acquisition_module._default_transport
        captured = tuple(
            cell.cell_contents
            for cell in (product_transport.__closure__ or ())
        )
        original_request = acquisition_module.urllib.request.Request
        original_build_opener = acquisition_module.urllib.request.build_opener
        original_redirect_handler = acquisition_module._PRODUCT_REDIRECT_HANDLER

        self.assertIn(original_request, captured)
        self.assertIn(original_build_opener, captured)
        self.assertIn(original_redirect_handler, captured)

        fake_request = lambda *_args, **_kwargs: None
        fake_build_opener = lambda *_args, **_kwargs: None
        with (
            mock.patch.object(
                acquisition_module.urllib.request,
                "Request",
                fake_request,
            ),
            mock.patch.object(
                acquisition_module.urllib.request,
                "build_opener",
                fake_build_opener,
            ),
        ):
            captured_after_patch = tuple(
                cell.cell_contents
                for cell in (product_transport.__closure__ or ())
            )

        self.assertIn(original_request, captured_after_patch)
        self.assertIn(original_build_opener, captured_after_patch)
        self.assertNotIn(fake_request, captured_after_patch)
        self.assertNotIn(fake_build_opener, captured_after_patch)

    def test_304_cannot_transfer_positive_origin_from_caller_prior(self):
        prior = acquire_parlay_sport_catalog(
            transport=RecordingTransport(
                [response(body=b'[{"key":"forged"}]', etag='"v1"')]
            ),
            timeout_seconds=3.5,
            max_response_bytes=1024,
            clock=lambda: "2020-01-01T00:00:00+00:00",
        )
        object.__setattr__(prior, "provider_origin_verified", True)
        object.__setattr__(
            prior,
            "acquisition_id",
            acquisition_module._acquisition_id(
                acquired_at=prior.acquired_at,
                status_code=200,
                final_url=prior.final_url,
                etag=prior.etag,
                raw_body_sha256=prior.raw_body_sha256,
                prior_acquisition_id=None,
                provider_origin_verified=True,
            ),
        )

        transport = RecordingTransport(
            [response(status=304, body=b"", etag='"v1"')]
        )
        result = acquire_parlay_sport_catalog(
            prior=prior,
            transport=transport,
            timeout_seconds=3.5,
            max_response_bytes=1024,
            clock=lambda: NOW,
        )

        self.assertTrue(result.is_not_modified)
        self.assertFalse(result.provider_origin_verified)
        self.assertEqual(result.prior_acquisition_id, prior.acquisition_id)
        self.assertEqual(
            transport.calls[0]["headers"]["If-None-Match"],
            '"v1"',
        )

    def test_prior_must_be_exact_200_body_acquisition(self):
        fake_304 = ParlaySportCatalogAcquisition(
            acquired_at=NOW,
            status_code=304,
            final_url=CANONICAL_PARLAY_SPORTS_URL,
            etag='"v1"',
            raw_body=None,
            raw_body_sha256=None,
            prior_acquisition_id="prior",
            acquisition_id="fake",
        )
        with self.assertRaisesRegex(
            ParlaySportCatalogEvidenceError, "exact prior HTTP 200"
        ):
            self.acquire(RecordingTransport([response()]), prior=fake_304)


if __name__ == "__main__":
    unittest.main()
