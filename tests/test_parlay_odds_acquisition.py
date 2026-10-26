from __future__ import annotations

import hashlib
import unittest

from autosport.parlay_odds_acquisition import (
    DEFAULT_MAX_RESPONSE_BYTES,
    ParlayOddsAcquisition,
    ParlayOddsEvidenceError,
    ParlayOddsRequestScope,
    RawOddsHttpResponse,
    acquire_parlay_odds,
)


NOW = "2026-09-22T14:15:00Z"


def _clock() -> str:
    return NOW


def _scope() -> ParlayOddsRequestScope:
    return ParlayOddsRequestScope(
        sport_key="baseball_mlb",
        regions=("us",),
        markets=("h2h",),
    )


def _response(
    scope: ParlayOddsRequestScope,
    *,
    status_code: int = 200,
    headers: tuple[tuple[str, str], ...] = (
        ("X-Markets-Served", "h2h"),
        ("X-API-Version", "3.2.0"),
    ),
    body: bytes = b"[]",
    final_url: str | None = None,
) -> RawOddsHttpResponse:
    return RawOddsHttpResponse(
        status_code=status_code,
        headers=headers,
        body=body,
        final_url=scope.request_url if final_url is None else final_url,
    )


class ParlayOddsAcquisitionTests(unittest.TestCase):
    def test_scope_builds_exact_header_auth_url_without_secret(self) -> None:
        scope = ParlayOddsRequestScope(
            sport_key="baseball_mlb",
            regions=("eu", "us"),
            markets=("h2h", "spreads"),
        )
        secret = "test-secret-key"
        observed: dict[str, object] = {}

        def transport(url, headers, timeout, max_bytes):
            observed.update(
                url=url,
                headers=dict(headers),
                timeout=timeout,
                max_bytes=max_bytes,
            )
            return _response(scope)

        acquisition = acquire_parlay_odds(
            scope,
            api_key=secret,
            transport=transport,
            clock=_clock,
        )

        self.assertEqual(
            observed["url"],
            "https://parlay-api.com/v1/sports/baseball_mlb/odds?regions=eu,us&markets=h2h,spreads&oddsFormat=decimal",
        )
        self.assertEqual(observed["headers"]["X-API-Key"], secret)
        self.assertNotIn(secret, str(observed["url"]))
        self.assertNotIn(secret, acquisition.final_url)
        self.assertNotIn(secret, acquisition.acquisition_id)
        self.assertNotIn(secret, acquisition.request_sha256)
        self.assertNotIn(secret, repr(acquisition))
        self.assertFalse(acquisition.provider_origin_verified)

    def test_exact_body_digest_and_capability_headers_are_preserved(self) -> None:
        scope = _scope()
        body = b'[{"id":"event-1"}]'
        headers = (
            ("X-Ignored", "not durable"),
            ("X-Markets-Unservable", "team_totals"),
            ("X-Markets-Served-Elsewhere", "/v1/sports/{sport_key}/props?markets=team_totals"),
            ("X-Markets-Served", "h2h"),
        )

        result = acquire_parlay_odds(
            scope,
            api_key="secret",
            transport=lambda *_: _response(scope, headers=headers, body=body),
            clock=_clock,
        )

        self.assertEqual(result.raw_body, body)
        self.assertEqual(result.response_sha256, hashlib.sha256(body).hexdigest())
        self.assertEqual(
            result.capability_headers,
            (
                ("x-markets-served", "h2h"),
                (
                    "x-markets-served-elsewhere",
                    "/v1/sports/{sport_key}/props?markets=team_totals",
                ),
                ("x-markets-unservable", "team_totals"),
            ),
        )

    def test_custom_transport_and_clock_cannot_mint_origin_authority(self) -> None:
        scope = _scope()
        result = acquire_parlay_odds(
            scope,
            api_key="secret",
            transport=lambda *_: _response(scope),
            clock=_clock,
        )
        self.assertFalse(result.provider_origin_verified)
        with self.assertRaises(TypeError):
            ParlayOddsAcquisition(
                scope=scope,
                acquired_at=NOW,
                status_code=200,
                final_url=scope.request_url,
                response_sha256="0" * 64,
                capability_headers=(),
                acquisition_id="1" * 64,
                raw_body=b"[]",
                provider_origin_verified=True,
            )

    def test_request_and_acquisition_identity_do_not_depend_on_api_key(self) -> None:
        scope = _scope()

        def transport(*_):
            return _response(scope, body=b"[]")

        first = acquire_parlay_odds(
            scope,
            api_key="secret-one",
            transport=transport,
            clock=_clock,
        )
        second = acquire_parlay_odds(
            scope,
            api_key="secret-two",
            transport=transport,
            clock=_clock,
        )
        self.assertEqual(first.request_sha256, second.request_sha256)
        self.assertEqual(first.acquisition_id, second.acquisition_id)

    def test_final_url_drift_fails_closed(self) -> None:
        scope = _scope()
        with self.assertRaisesRegex(ParlayOddsEvidenceError, "final URL"):
            acquire_parlay_odds(
                scope,
                api_key="secret",
                transport=lambda *_: _response(
                    scope,
                    final_url="https://example.invalid/v1/sports/baseball_mlb/odds",
                ),
                clock=_clock,
            )

    def test_duplicate_capability_header_fails_closed(self) -> None:
        scope = _scope()
        with self.assertRaisesRegex(ParlayOddsEvidenceError, "duplicate capability header"):
            acquire_parlay_odds(
                scope,
                api_key="secret",
                transport=lambda *_: _response(
                    scope,
                    headers=(
                        ("X-Markets-Served", "h2h"),
                        ("x-markets-served", "spreads"),
                    ),
                ),
                clock=_clock,
            )

    def test_response_body_is_bounded_even_for_injected_transport(self) -> None:
        scope = _scope()
        with self.assertRaisesRegex(ParlayOddsEvidenceError, "bounded size"):
            acquire_parlay_odds(
                scope,
                api_key="secret",
                max_response_bytes=3,
                transport=lambda *_: _response(scope, body=b"1234"),
                clock=_clock,
            )

    def test_api_credential_echo_in_body_or_durable_header_fails_closed(self) -> None:
        scope = _scope()
        secret = "never-persist-this"
        for response in (
            _response(scope, body=f'{{"echo":"{secret}"}}'.encode()),
            _response(scope, headers=(("X-API-Version", secret),)),
        ):
            with self.subTest(response=response), self.assertRaisesRegex(
                ParlayOddsEvidenceError,
                "echoed API credential",
            ):
                acquire_parlay_odds(
                    scope,
                    api_key=secret,
                    transport=lambda *_, response=response: response,
                    clock=_clock,
                )

    def test_http_403_is_retained_as_observation_not_reclassified(self) -> None:
        scope = _scope()
        result = acquire_parlay_odds(
            scope,
            api_key="secret",
            transport=lambda *_: _response(
                scope,
                status_code=403,
                headers=(("X-Ignored", "value"),),
                body=b'{"error":"forbidden"}',
            ),
            clock=_clock,
        )
        self.assertEqual(result.status_code, 403)
        self.assertEqual(result.capability_headers, ())
        self.assertEqual(result.raw_body, b'{"error":"forbidden"}')

    def test_clock_is_sampled_only_after_transport_returns(self) -> None:
        scope = _scope()
        order: list[str] = []

        def transport(*_):
            order.append("transport")
            return _response(scope)

        def clock() -> str:
            order.append("clock")
            return NOW

        acquire_parlay_odds(
            scope,
            api_key="secret",
            transport=transport,
            clock=clock,
        )
        self.assertEqual(order, ["transport", "clock"])

    def test_invalid_or_secret_shaped_request_inputs_fail_before_transport(self) -> None:
        with self.assertRaises(ParlayOddsEvidenceError):
            ParlayOddsRequestScope("Baseball_MLB", ("us",), ("h2h",))
        with self.assertRaises(ParlayOddsEvidenceError):
            ParlayOddsRequestScope("baseball_mlb", ("us", "eu"), ("h2h",))
        with self.assertRaises(ParlayOddsEvidenceError):
            ParlayOddsRequestScope("baseball_mlb", ("us",), ("h2h", "h2h"))
        with self.assertRaisesRegex(ParlayOddsEvidenceError, "control"):
            acquire_parlay_odds(_scope(), api_key="secret\nvalue", transport=lambda *_: None)

    def test_odds_format_is_fixed_to_decimal(self) -> None:
        with self.assertRaisesRegex(ParlayOddsEvidenceError, "fixed to decimal"):
            ParlayOddsRequestScope(
                sport_key="baseball_mlb",
                regions=("us",),
                markets=("h2h",),
                odds_format="american",
            )

    def test_capability_header_mutation_changes_acquisition_identity(self) -> None:
        scope = _scope()
        first = acquire_parlay_odds(
            scope,
            api_key="secret",
            transport=lambda *_: _response(
                scope,
                headers=(("X-Markets-Served", "h2h"),),
            ),
            clock=_clock,
        )
        second = acquire_parlay_odds(
            scope,
            api_key="secret",
            transport=lambda *_: _response(
                scope,
                headers=(("X-Markets-Served", "spreads"),),
            ),
            clock=_clock,
        )
        self.assertNotEqual(first.acquisition_id, second.acquisition_id)

    def test_product_bounds_cannot_be_widened_by_caller(self) -> None:
        with self.assertRaises(ValueError):
            acquire_parlay_odds(
                _scope(),
                api_key="secret",
                timeout_seconds=10.1,
                transport=lambda *_: None,
            )
        with self.assertRaises(ValueError):
            acquire_parlay_odds(
                _scope(),
                api_key="secret",
                max_response_bytes=DEFAULT_MAX_RESPONSE_BYTES + 1,
                transport=lambda *_: None,
            )


if __name__ == "__main__":
    unittest.main()
