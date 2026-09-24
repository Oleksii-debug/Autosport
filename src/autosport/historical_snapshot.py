from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import http.client as http_client
import json
import os
import ssl
import tempfile
import weakref
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import (
    AbstractHTTPHandler,
    HTTPErrorProcessor,
    HTTPRedirectHandler,
    HTTPSHandler,
    OpenerDirector,
    ProxyHandler,
    Request,
    build_opener,
)

from .domain import MarketEvent
from .integrity import atomic_write_json, durable_path_lock
from .parlayapi_provider import (
    HttpJsonResponse,
    ParlayApiTableTennisProvider,
    ProviderPayloadError,
    ProviderTransportError,
)
from .providers import CanonicalNormalizer, ProviderQuote


TERMS_REFERENCE = "https://parlay-api.com/terms"
_TRACKED_RESPONSE_HEADERS = (
    "x-api-version",
    "x-api-release-date",
    "deprecation",
    "sunset",
    "link",
    "x-historical-window-hours",
    "x-historical-window-from",
    "x-markets-served",
    "x-markets-unservable",
    "x-markets-served-elsewhere",
    "cache-control",
)
_MAX_PROVENANCE_HEADER_CHARS = 4096


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _historical_request_scope(
    provider: ParlayApiTableTennisProvider,
    *,
    requested_at: str,
) -> tuple[str, dict[str, object]]:
    base_url = provider.base_url
    if type(base_url) is not str or not base_url or base_url != base_url.strip():
        raise ValueError("provider base_url must be a canonical non-empty URL")
    parsed_base = urlsplit(base_url)
    if (
        parsed_base.scheme != "https"
        or not parsed_base.netloc
        or parsed_base.username is not None
        or parsed_base.password is not None
        or parsed_base.query
        or parsed_base.fragment
    ):
        raise ValueError(
            "historical snapshot provider base_url must be a secret-free HTTPS origin/path"
        )

    request_query = {
        "date": requested_at,
        "regions": ",".join(provider.regions),
        "markets": ",".join(provider.markets),
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }
    query = urlencode(request_query)
    endpoint_path = f"/v1/historical/sports/{provider.sport_key}/odds"
    url = f"{base_url}{endpoint_path}?{query}"
    request_url_sha256 = hashlib.sha256(url.encode("utf-8")).hexdigest()
    origin = f"{parsed_base.scheme}://{parsed_base.netloc}"
    scope: dict[str, object] = {
        "method": "GET",
        "origin": origin,
        "base_url_sha256": hashlib.sha256(base_url.encode("utf-8")).hexdigest(),
        "endpoint_path": endpoint_path,
        "query": request_query,
        "query_string": query,
        "request_url_sha256": request_url_sha256,
        "request_url_persisted": False,
        "request_credentials_persisted": False,
    }
    return url, scope


def _tracked_response_headers(headers: Mapping[str, str]) -> dict[str, str | None]:
    observed: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        if type(raw_name) is not str:
            continue
        name = raw_name.lower()
        if name not in _TRACKED_RESPONSE_HEADERS:
            continue
        if name in observed:
            raise ProviderPayloadError(
                f"provider returned duplicate tracked response header {name}"
            )
        if (
            type(raw_value) is not str
            or not raw_value
            or raw_value != raw_value.strip()
            or len(raw_value) > _MAX_PROVENANCE_HEADER_CHARS
        ):
            raise ProviderPayloadError(
                f"provider tracked response header {name} is not canonical bounded text"
            )
        observed[name] = raw_value
    return {name: observed.get(name) for name in _TRACKED_RESPONSE_HEADERS}


@dataclass(frozen=True, slots=True, weakref_slot=True)
class HistoricalSnapshotCapture:
    requested_at: str
    snapshot_at: str
    captured_at: str
    previous_snapshot_at: str | None
    next_snapshot_at: str | None
    response_sha256: str
    market_sha256: str
    quote_count: int
    snapshot_timestamp_fallback_count: int
    market_types: tuple[str, ...]
    bookmaker_keys: tuple[str, ...]
    output_path: str
    evidence_path: str

    @property
    def has_data(self) -> bool:
        return self.quote_count > 0


def _ordered_publication_lock_paths(output: Path, evidence: Path) -> tuple[Path, Path]:
    keyed_paths: list[tuple[str, Path]] = []
    for path in (output, evidence):
        try:
            resolved = path.resolve(strict=False)
        except OSError as exc:
            raise ValueError("cannot resolve historical snapshot publication path") from exc
        key = os.path.normcase(str(resolved))
        keyed_paths.append((key, resolved))

    if keyed_paths[0][0] == keyed_paths[1][0]:
        raise ValueError("historical snapshot output and evidence paths must be distinct")
    keyed_paths.sort(key=lambda item: item[0])
    return keyed_paths[0][1], keyed_paths[1][1]


def capture_historical_snapshot(
    provider: ParlayApiTableTennisProvider,
    *,
    requested_at: str,
    output_path: str | Path,
    evidence_path: str | Path | None = None,
) -> HistoricalSnapshotCapture:
    """Capture one authenticated point-in-time table-tennis odds snapshot.

    The provider's historical response timestamp is the causal snapshot boundary.
    Per-quote ``last_update`` is preferred when present. When the provider omits it,
    the provider snapshot timestamp is used explicitly as a *snapshot-boundary*
    source time and the row metadata records that weaker semantic. No wall-clock
    timestamp is substituted for historical market time.

    This creates canonical market rows plus machine evidence only. It deliberately
    does not fabricate results, settlement outcomes, licensing proof, historical
    window coverage, or a replay-ready schema-v2 dataset manifest.
    """

    if provider.public_preview or not provider.api_key:
        raise ValueError("historical snapshot capture requires an authenticated API key")

    output = Path(output_path)
    evidence = Path(evidence_path) if evidence_path is not None else output.with_suffix(output.suffix + ".evidence.json")
    publication_lock_paths = _ordered_publication_lock_paths(output, evidence)

    requested_dt = _parse_timestamp(requested_at, field="requested_at")
    url, request_scope = _historical_request_scope(
        provider,
        requested_at=requested_at,
    )
    response = provider._request(url)  # package-internal transport preserves secret/header policy and retries
    if type(response.status_code) is not int or response.status_code != 200:
        raise ProviderPayloadError("historical odds acquisition requires HTTP 200")
    response_headers = _tracked_response_headers(response.headers)
    captured_at = provider.clock()
    captured_dt = _parse_timestamp(captured_at, field="captured_at")
    if captured_dt < requested_dt:
        raise ProviderPayloadError("capture clock is before requested_at")

    payload = response.payload
    if not isinstance(payload, dict):
        raise ProviderPayloadError("historical odds response must be an object")
    snapshot_at = _required_text(payload, "timestamp")
    snapshot_dt = _parse_timestamp(snapshot_at, field="historical response timestamp")
    if snapshot_dt > requested_dt:
        raise ProviderPayloadError("historical snapshot timestamp is after requested_at")
    if captured_dt < snapshot_dt:
        raise ProviderPayloadError("capture clock is before historical snapshot timestamp")

    data = payload.get("data")
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise ProviderPayloadError("historical odds response requires data[] event objects")

    previous_snapshot_at = _optional_timestamp(payload.get("previous_timestamp"), "previous_timestamp")
    next_snapshot_at = _optional_timestamp(payload.get("next_timestamp"), "next_timestamp")
    if previous_snapshot_at is not None and _parse_timestamp(previous_snapshot_at, field="previous_timestamp") > snapshot_dt:
        raise ProviderPayloadError("previous_timestamp is after snapshot timestamp")
    if next_snapshot_at is not None and _parse_timestamp(next_snapshot_at, field="next_timestamp") < snapshot_dt:
        raise ProviderPayloadError("next_timestamp is before snapshot timestamp")

    normalizer = CanonicalNormalizer()
    events: list[MarketEvent] = []
    fallback_count = 0
    bookmaker_keys: set[str] = set()
    dedupe_keys: set[str] = set()

    for raw_event in data:
        quotes = provider._event_quotes(raw_event, snapshot_at, response.status_code)
        for quote in quotes:
            causal_quote, used_fallback = _bind_quote_to_snapshot(quote, snapshot_at, snapshot_dt)
            if used_fallback:
                fallback_count += 1
            bookmaker = str(causal_quote.metadata.get("bookmaker_key") or "").strip()
            if bookmaker:
                bookmaker_keys.add(bookmaker)
            event = normalizer.normalize(provider.source_id, causal_quote)
            event = replace(event, ingest_ts=captured_at)
            if event.dedupe_key in dedupe_keys:
                raise ProviderPayloadError("historical snapshot contains duplicate canonical quote identity")
            dedupe_keys.add(event.dedupe_key)
            events.append(event)

    events.sort(key=lambda item: (item.observed_ts, item.sequence, item.event_id, item.market_id, item.selection_id))
    canonical_response = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    response_sha256 = hashlib.sha256(canonical_response.encode("utf-8")).hexdigest()
    acquisition_provenance = {
        "schema_version": 1,
        "kind": "parlayapi_point_in_time_historical_acquisition",
        "product_kind": "POINT_IN_TIME_ODDS",
        "request": request_scope,
        "http_status": response.status_code,
        "response_payload_sha256": response_sha256,
        "response_headers": response_headers,
        "canonical_response_payload_bound": True,
        "raw_response_bytes_bound": False,
        "provider_origin_authority_persisted": False,
        "provider_origin_requires_live_product_capture": True,
    }
    acquisition_sha256 = _canonical_sha256(acquisition_provenance)
    market_types = tuple(sorted({event.market_type.value for event in events}))
    observed_fixture_ids = {event.event_id for event in events}
    observed_fixture_markets = {(event.event_id, event.market_id) for event in events}
    observed_quote_count_by_market_type: dict[str, int] = {}
    for event in events:
        market_type = event.market_type.value
        observed_quote_count_by_market_type[market_type] = (
            observed_quote_count_by_market_type.get(market_type, 0) + 1
        )

    evidence_payload = {
        "schema_version": 1,
        "kind": "parlayapi_point_in_time_historical_snapshot",
        "provider": "parlayapi",
        "sport_key": provider.sport_key,
        "requested_at": requested_at,
        "snapshot_at": snapshot_at,
        "captured_at": captured_at,
        "previous_snapshot_at": previous_snapshot_at,
        "next_snapshot_at": next_snapshot_at,
        "response_sha256": response_sha256,
        "acquisition_provenance": acquisition_provenance,
        "acquisition_sha256": acquisition_sha256,
        "quote_count": len(events),
        "has_data": bool(events),
        "market_types": list(market_types),
        "bookmaker_keys": sorted(bookmaker_keys),
        "observed_coverage": {
            "scope": "captured_snapshot_rows_only",
            "fixture_identity": "canonical_event_id",
            "fixture_market_identity": "canonical_event_id_plus_market_id",
            "fixture_count": len(observed_fixture_ids),
            "fixture_market_count": len(observed_fixture_markets),
            "quote_count": len(events),
            "quote_count_by_market_type": dict(sorted(observed_quote_count_by_market_type.items())),
            "completeness_semantics": "observed_rows_only_not_provider_universe",
        },
        "snapshot_timestamp_fallback_count": fallback_count,
        "source_time_semantics": {
            "preferred": "provider_quote_last_update",
            "fallback": "provider_historical_snapshot_timestamp",
            "wall_clock_used_as_historical_market_time": False,
        },
        "point_in_time_snapshot_contains_odds": bool(events),
        "point_in_time_odds_market_coverage_verified": False,
        "historical_window_market_coverage_verified": False,
        "sealed_outcomes_present": False,
        "replay_corpus_ready": False,
        "terms_reference": TERMS_REFERENCE,
        "licensing_or_retention_verified": False,
        "redistribution_verified": False,
        "real_money_execution": False,
        "human_tested": False,
        "nvda_verified": False,
    }
    with ExitStack() as publication_locks:
        for publication_lock_path in publication_lock_paths:
            publication_locks.enter_context(durable_path_lock(publication_lock_path))
        _atomic_write_jsonl(output, (event.to_dict() for event in events))
        market_sha256 = _sha256(output)
        evidence_payload["market_sha256"] = market_sha256
        atomic_write_json(evidence, evidence_payload)

    return HistoricalSnapshotCapture(
        requested_at=requested_at,
        snapshot_at=snapshot_at,
        captured_at=captured_at,
        previous_snapshot_at=previous_snapshot_at,
        next_snapshot_at=next_snapshot_at,
        response_sha256=response_sha256,
        market_sha256=market_sha256,
        quote_count=len(events),
        snapshot_timestamp_fallback_count=fallback_count,
        market_types=market_types,
        bookmaker_keys=tuple(sorted(bookmaker_keys)),
        output_path=str(output),
        evidence_path=str(evidence),
    )


def _build_historical_snapshot_provider_origin_authority():
    """Build one lexical live-only issuer for canonical Parlay historical capture."""

    provider_type = ParlayApiTableTennisProvider
    canonical_provider_init = provider_type.__init__
    canonical_provider_request = provider_type._request
    canonical_event_quotes = provider_type._event_quotes
    canonical_capture = capture_historical_snapshot
    canonical_defaults = canonical_provider_init.__kwdefaults__
    if type(canonical_defaults) is not dict:
        raise RuntimeError("canonical Parlay provider defaults are unavailable")
    canonical_default_transport = canonical_defaults.get("transport")
    canonical_clock = canonical_defaults.get("clock")
    canonical_sleeper = canonical_defaults.get("sleeper")
    if (
        not callable(canonical_default_transport)
        or not callable(canonical_clock)
        or not callable(canonical_sleeper)
    ):
        raise RuntimeError("canonical Parlay provider runtime defaults are invalid")

    # Positive provider-origin authority must not flow through the provider module's
    # live `urlopen` global or urllib.request's process-global opener. Build one
    # private opener per product-owned acquisition and keep its complete dispatch/
    # TLS verifier graph lexical to this issuer. This mirrors the bounded network
    # authority used by other provider-facing product surfaces while leaving the
    # generic provider transport seam available for non-authoritative parsing/tests.
    request_type = Request
    build_opener_fn = build_opener
    opener_type = OpenerDirector
    proxy_handler_type = ProxyHandler
    http_redirect_handler_type = HTTPRedirectHandler
    https_handler_type = HTTPSHandler
    abstract_http_handler_type = AbstractHTTPHandler
    http_error_processor_type = HTTPErrorProcessor
    http_error_type = HTTPError
    url_error_type = URLError
    response_type = HttpJsonResponse
    transport_error_type = ProviderTransportError
    decode_provider_json = canonical_default_transport.__globals__.get(
        "_decode_provider_json"
    )
    parse_retry_after = canonical_default_transport.__globals__.get(
        "_parse_retry_after"
    )
    urlsplit_fn = urlsplit
    sha256_fn = hashlib.sha256
    ssl_context_type = ssl.SSLContext
    ssl_cert_required = ssl.CERT_REQUIRED
    ssl_error_type = ssl.SSLError
    http_client_module = http_client
    http_connection_type = http_client_module.HTTPConnection
    https_connection_type = http_client_module.HTTPSConnection
    connection_dispatch_names = (
        "__init__",
        "connect",
        "set_tunnel",
        "set_debuglevel",
        "request",
        "_send_request",
        "putrequest",
        "putheader",
        "endheaders",
        "_send_output",
        "send",
        "getresponse",
        "close",
    )
    canonical_http_connection_dispatch = {
        name: getattr(http_connection_type, name)
        for name in connection_dispatch_names
    }
    canonical_http_connection_dispatch_codes = {
        name: getattr(method, "__code__", None)
        for name, method in canonical_http_connection_dispatch.items()
    }
    canonical_https_connection_dispatch = {
        name: getattr(https_connection_type, name)
        for name in connection_dispatch_names
    }
    canonical_https_connection_dispatch_codes = {
        name: getattr(method, "__code__", None)
        for name, method in canonical_https_connection_dispatch.items()
    }

    # Freeze the private network dispatch before any product-owned capture can be
    # called. Capturing these descriptors inside build_product_owned_transport()
    # would let a caller install a replacement first and have that replacement
    # promoted to positive provider-origin authority for the current acquisition.
    canonical_build_opener_code = getattr(build_opener_fn, "__code__", None)
    canonical_opener_open = opener_type.open
    canonical_opener_open_code = getattr(canonical_opener_open, "__code__", None)
    canonical_opener_internal_open = opener_type._open
    canonical_opener_internal_open_code = getattr(
        canonical_opener_internal_open, "__code__", None
    )
    canonical_opener_call_chain = opener_type._call_chain
    canonical_opener_call_chain_code = getattr(
        canonical_opener_call_chain, "__code__", None
    )
    canonical_opener_error = opener_type.error
    canonical_opener_error_code = getattr(canonical_opener_error, "__code__", None)
    canonical_https_open = https_handler_type.https_open
    canonical_https_open_code = getattr(canonical_https_open, "__code__", None)
    canonical_https_request = https_handler_type.https_request
    canonical_https_request_code = getattr(canonical_https_request, "__code__", None)
    canonical_do_open = abstract_http_handler_type.do_open
    canonical_do_open_code = getattr(canonical_do_open, "__code__", None)
    canonical_https_response = http_error_processor_type.https_response
    canonical_https_response_code = getattr(
        canonical_https_response, "__code__", None
    )
    canonical_urllib_http_package = canonical_https_open.__globals__.get("http")
    if (
        canonical_build_opener_code is None
        or canonical_opener_open_code is None
        or canonical_opener_internal_open_code is None
        or canonical_opener_call_chain_code is None
        or canonical_opener_error_code is None
        or canonical_https_open_code is None
        or canonical_https_request_code is None
        or canonical_do_open_code is None
        or canonical_https_response_code is None
        or canonical_urllib_http_package is None
        or getattr(canonical_urllib_http_package, "client", None)
        is not http_client_module
        or any(
            code is None
            for code in canonical_http_connection_dispatch_codes.values()
        )
        or any(
            code is None
            for code in canonical_https_connection_dispatch_codes.values()
        )
    ):
        raise RuntimeError("canonical Parlay historical network dispatch is unavailable")

    def connection_dispatch_is_canonical() -> bool:
        if (
            getattr(canonical_urllib_http_package, "client", None)
            is not http_client_module
            or getattr(http_client_module, "HTTPConnection", None)
            is not http_connection_type
            or getattr(http_client_module, "HTTPSConnection", None)
            is not https_connection_type
        ):
            return False
        for name in connection_dispatch_names:
            current_http = getattr(http_connection_type, name, None)
            expected_http = canonical_http_connection_dispatch[name]
            if (
                current_http is not expected_http
                or getattr(current_http, "__code__", None)
                is not canonical_http_connection_dispatch_codes[name]
            ):
                return False
            current_https = getattr(https_connection_type, name, None)
            expected_https = canonical_https_connection_dispatch[name]
            if (
                current_https is not expected_https
                or getattr(current_https, "__code__", None)
                is not canonical_https_connection_dispatch_codes[name]
            ):
                return False
        return True

    def stdlib_dispatch_is_canonical() -> bool:
        return (
            connection_dispatch_is_canonical()
            getattr(build_opener_fn, "__code__", None) is canonical_build_opener_code
            and opener_type.open is canonical_opener_open
            and getattr(canonical_opener_open, "__code__", None)
            is canonical_opener_open_code
            and opener_type._open is canonical_opener_internal_open
            and getattr(canonical_opener_internal_open, "__code__", None)
            is canonical_opener_internal_open_code
            and opener_type._call_chain is canonical_opener_call_chain
            and getattr(canonical_opener_call_chain, "__code__", None)
            is canonical_opener_call_chain_code
            and opener_type.error is canonical_opener_error
            and getattr(canonical_opener_error, "__code__", None)
            is canonical_opener_error_code
            and https_handler_type.https_open is canonical_https_open
            and getattr(canonical_https_open, "__code__", None)
            is canonical_https_open_code
            and https_handler_type.https_request is canonical_https_request
            and getattr(canonical_https_request, "__code__", None)
            is canonical_https_request_code
            and getattr(https_handler_type, "do_open", None) is canonical_do_open
            and abstract_http_handler_type.do_open is canonical_do_open
            and getattr(canonical_do_open, "__code__", None)
            is canonical_do_open_code
            and http_error_processor_type.https_response
            is canonical_https_response
            and getattr(canonical_https_response, "__code__", None)
            is canonical_https_response_code
        )

    if not callable(decode_provider_json) or not callable(parse_retry_after):
        raise RuntimeError("canonical Parlay provider transport parser is unavailable")

    def tls_context_snapshot(context: object):
        if type(context) is not ssl_context_type:
            return None
        try:
            ciphers = tuple(
                (
                    cipher.get("id"),
                    cipher.get("name"),
                    cipher.get("protocol"),
                    cipher.get("strength_bits"),
                    cipher.get("alg_bits"),
                    cipher.get("aead"),
                    cipher.get("symmetric"),
                    cipher.get("digest"),
                    cipher.get("kea"),
                    cipher.get("auth"),
                )
                for cipher in context.get_ciphers()
            )
            trust_anchors = tuple(
                sorted(
                    sha256_fn(certificate).hexdigest()
                    for certificate in context.get_ca_certs(binary_form=True)
                )
            )
            store_stats = tuple(sorted(context.cert_store_stats().items()))
            return (
                int(context.verify_mode),
                context.check_hostname,
                int(context.verify_flags),
                int(context.minimum_version),
                int(context.maximum_version),
                int(context.options),
                getattr(context, "hostname_checks_common_name", None),
                getattr(context, "security_level", None),
                store_stats,
                trust_anchors,
                ciphers,
            )
        except (AttributeError, TypeError, ValueError, ssl_error_type):
            return None

    def dispatch_snapshot(current_opener: object):
        records: list[tuple[str, object, tuple[object, ...]]] = []
        for map_name in ("handle_open", "process_request", "process_response"):
            mapping = getattr(current_opener, map_name, None)
            if type(mapping) is not dict:
                return None
            for key, handlers in mapping.items():
                if type(key) not in (str, int) or type(handlers) is not list:
                    return None
                records.append((map_name, key, tuple(handlers)))
        error_mapping = getattr(current_opener, "handle_error", None)
        if type(error_mapping) is not dict:
            return None
        for protocol, by_code in error_mapping.items():
            if type(protocol) not in (str, int) or type(by_code) is not dict:
                return None
            for code, handlers in by_code.items():
                if type(code) not in (str, int) or type(handlers) is not list:
                    return None
                records.append(
                    (f"handle_error:{protocol}", code, tuple(handlers))
                )
        records.sort(
            key=lambda item: (
                item[0],
                type(item[1]).__name__,
                str(item[1]),
            )
        )
        return tuple(records)

    def dispatch_matches(current, expected) -> bool:
        if current is None or len(current) != len(expected):
            return False
        for actual, wanted in zip(current, expected):
            if actual[0] != wanted[0] or actual[1] != wanted[1]:
                return False
            if len(actual[2]) != len(wanted[2]):
                return False
            if any(
                actual_handler is not expected_handler
                for actual_handler, expected_handler in zip(
                    actual[2], wanted[2]
                )
            ):
                return False
        return True

    def build_product_owned_transport():
        if not stdlib_dispatch_is_canonical():
            raise ProviderPayloadError(
                "canonical Parlay historical network dispatch changed before construction"
            )

        class RejectRedirectHandler(http_redirect_handler_type):
            def redirect_request(
                self,
                req,
                fp,
                code,
                msg,
                headers,
                newurl,
            ):  # type: ignore[no-untyped-def]
                raise transport_error_type(
                    "Parlay historical HTTP redirect refused",
                    int(code),
                )

        opener = build_opener_fn(
            proxy_handler_type({}),
            RejectRedirectHandler(),
        )
        if type(opener) is not opener_type:
            raise ProviderPayloadError(
                "canonical Parlay historical opener authority is invalid"
            )

        open_response = opener.open
        canonical_redirect_request = RejectRedirectHandler.redirect_request

        expected_handlers_raw = getattr(opener, "handlers", None)
        expected_dispatch = dispatch_snapshot(opener)
        if type(expected_handlers_raw) is not list or expected_dispatch is None:
            raise ProviderPayloadError(
                "canonical Parlay historical opener graph is not inspectable"
            )
        expected_handlers = tuple(expected_handlers_raw)
        redirect_handlers = tuple(
            handler
            for handler in expected_handlers
            if isinstance(handler, http_redirect_handler_type)
        )
        https_handlers = tuple(
            handler
            for handler in expected_handlers
            if isinstance(handler, https_handler_type)
        )
        response_handlers = tuple(
            handlers
            for map_name, key, handlers in expected_dispatch
            if map_name == "process_response" and key == "https"
        )
        if (
            len(redirect_handlers) != 1
            or type(redirect_handlers[0]) is not RejectRedirectHandler
            or len(https_handlers) != 1
            or type(https_handlers[0]) is not https_handler_type
            or len(response_handlers) != 1
            or len(response_handlers[0]) != 1
            or type(response_handlers[0][0]) is not http_error_processor_type
            or any(
                isinstance(handler, proxy_handler_type)
                for handler in expected_handlers
            )
        ):
            raise ProviderPayloadError(
                "canonical Parlay historical opener graph is invalid"
            )
        expected_redirect_handler = redirect_handlers[0]
        expected_https_handler = https_handlers[0]
        expected_error_processor = response_handlers[0][0]
        expected_tls_context = getattr(expected_https_handler, "_context", None)
        expected_tls_state = tls_context_snapshot(expected_tls_context)
        if (
            expected_tls_state is None
            or expected_tls_context.verify_mode != ssl_cert_required
            or expected_tls_context.check_hostname is not True
        ):
            raise ProviderPayloadError(
                "canonical Parlay historical TLS verifier is invalid"
            )

        if (
            getattr(open_response, "__self__", None) is not opener
            or getattr(open_response, "__func__", None) is not canonical_opener_open
        ):
            raise ProviderPayloadError(
                "canonical Parlay historical opener dispatch is invalid"
            )

        def authority_is_current() -> bool:
            if (
                not stdlib_dispatch_is_canonical()
                or type(opener) is not opener_type
                or RejectRedirectHandler.redirect_request
                is not canonical_redirect_request
            ):
                return False

            current_tls_context = getattr(expected_https_handler, "_context", None)
            if (
                current_tls_context is not expected_tls_context
                or tls_context_snapshot(current_tls_context) != expected_tls_state
            ):
                return False

            opener_dict = getattr(opener, "__dict__", None)
            if type(opener_dict) is not dict or any(
                name in opener_dict
                for name in ("open", "_open", "_call_chain", "error")
            ):
                return False

            handlers = getattr(opener, "handlers", None)
            if type(handlers) is not list or len(handlers) != len(expected_handlers):
                return False
            if any(
                current is not expected
                for current, expected in zip(handlers, expected_handlers)
            ):
                return False
            if any(
                isinstance(handler, proxy_handler_type)
                for handler in handlers
            ):
                return False

            redirect_dict = getattr(expected_redirect_handler, "__dict__", None)
            https_dict = getattr(expected_https_handler, "__dict__", None)
            error_processor_dict = getattr(expected_error_processor, "__dict__", None)
            if (
                type(redirect_dict) is not dict
                or "redirect_request" in redirect_dict
                or type(https_dict) is not dict
                or any(
                    name in https_dict
                    for name in ("https_open", "https_request", "do_open")
                )
                or type(error_processor_dict) is not dict
                or "https_response" in error_processor_dict
            ):
                return False

            current_dispatch = dispatch_snapshot(opener)
            if not dispatch_matches(current_dispatch, expected_dispatch):
                return False

            dispatch_handlers = tuple(
                handlers
                for map_name, key, handlers in current_dispatch
                if map_name == "handle_open" and key == "https"
            )
            request_handlers = tuple(
                handlers
                for map_name, key, handlers in current_dispatch
                if map_name == "process_request" and key == "https"
            )
            response_handlers_now = tuple(
                handlers
                for map_name, key, handlers in current_dispatch
                if map_name == "process_response" and key == "https"
            )
            return (
                len(dispatch_handlers) == 1
                and len(dispatch_handlers[0]) == 1
                and dispatch_handlers[0][0] is expected_https_handler
                and len(request_handlers) == 1
                and len(request_handlers[0]) == 1
                and request_handlers[0][0] is expected_https_handler
                and len(response_handlers_now) == 1
                and len(response_handlers_now[0]) == 1
                and response_handlers_now[0][0] is expected_error_processor
                and all(
                    any(handler is registered for registered in expected_handlers)
                    for _map_name, _key, mapped_handlers in current_dispatch
                    for handler in mapped_handlers
                )
            )

        def require_authority() -> None:
            if not authority_is_current():
                raise transport_error_type(
                    "canonical Parlay historical network authority changed"
                )

        def product_owned_transport(
            url: str,
            headers: Mapping[str, str],
            timeout: float,
        ) -> HttpJsonResponse:
            parsed = urlsplit_fn(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname != "parlay-api.com"
                or parsed.port is not None
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
            ):
                raise transport_error_type(
                    "Parlay historical transport target is outside fixed production origin"
                )
            require_authority()
            request = request_type(url, headers=dict(headers), method="GET")
            try:
                with canonical_opener_open(
                    opener, request, timeout=timeout
                ) as response:
                    final_url = str(response.geturl())
                    if final_url != url:
                        raise transport_error_type(
                            "Parlay historical response origin changed unexpectedly"
                        )
                    raw = response.read()
                    payload = decode_provider_json(raw)
                    result = response_type(
                        payload,
                        int(response.status),
                        dict(response.headers.items()),
                    )
                require_authority()
                return result
            except transport_error_type:
                raise
            except http_error_type as exc:
                retry_after = parse_retry_after(
                    exc.headers.get("Retry-After") if exc.headers else None
                )
                raise transport_error_type(
                    f"provider HTTP {exc.code}",
                    int(exc.code),
                    retry_after,
                ) from exc
            except url_error_type as exc:
                raise transport_error_type(
                    f"provider transport error: {exc.reason}"
                ) from exc

        return product_owned_transport, require_authority

    issued: dict[
        int,
        tuple[
            weakref.ReferenceType,
            tuple[object, ...],
        ],
    ] = {}

    def capture_fingerprint(capture: HistoricalSnapshotCapture) -> tuple[object, ...]:
        return (
            capture.requested_at,
            capture.snapshot_at,
            capture.captured_at,
            capture.previous_snapshot_at,
            capture.next_snapshot_at,
            capture.response_sha256,
            capture.market_sha256,
            capture.quote_count,
            capture.snapshot_timestamp_fallback_count,
            capture.market_types,
            capture.bookmaker_keys,
            capture.output_path,
            capture.evidence_path,
        )

    def forget_issued(
        capture_id: int,
        reference: weakref.ReferenceType,
    ) -> None:
        current = issued.get(capture_id)
        if current is not None and current[0] is reference:
            issued.pop(capture_id, None)

    def provider_state_is_current(
        provider: ParlayApiTableTennisProvider,
        *,
        regions: tuple[str, ...],
        markets: tuple[str, ...],
        expected_transport: object,
    ) -> bool:
        if (
            type(provider) is not provider_type
            or provider_type.__init__ is not canonical_provider_init
            or provider_type._request is not canonical_provider_request
            or provider_type._event_quotes is not canonical_event_quotes
            or provider_type.source_id != "parlayapi:table_tennis"
            or provider_type.sport_key != "table_tennis"
        ):
            return False
        instance_dict = getattr(provider, "__dict__", None)
        if type(instance_dict) is not dict or any(
            name in instance_dict
            for name in (
                "source_id",
                "sport_key",
                "_request",
                "_event_quotes",
            )
        ):
            return False
        return (
            provider.public_preview is False
            and provider.base_url == "https://parlay-api.com"
            and provider.transport is expected_transport
            and provider.clock is canonical_clock
            and provider.sleeper is canonical_sleeper
            and provider.regions == regions
            and provider.markets == markets
        )

    def issue_capture(
        capture: HistoricalSnapshotCapture,
    ) -> HistoricalSnapshotCapture:
        capture_id = id(capture)
        reference = weakref.ref(
            capture,
            lambda current, capture_id=capture_id: forget_issued(
                capture_id,
                current,
            ),
        )
        issued[capture_id] = (reference, capture_fingerprint(capture))
        return capture

    def capture_product_owned_historical_snapshot(
        *,
        api_key: str,
        requested_at: str,
        output_path: str | Path,
        evidence_path: str | Path | None = None,
        regions: tuple[str, ...] = ("us",),
        markets: tuple[str, ...] = ("h2h", "spreads", "totals"),
    ) -> HistoricalSnapshotCapture:
        """Acquire through the fixed production Parlay boundary and issue live origin authority."""

        if type(api_key) is not str or not api_key or api_key != api_key.strip():
            raise ValueError("api_key must be non-empty trimmed text")
        if any(character.isspace() for character in api_key):
            raise ValueError("api_key must not contain whitespace")
        if type(regions) is not tuple or not regions:
            raise ValueError("regions must be a non-empty tuple")
        if type(markets) is not tuple or not markets:
            raise ValueError("markets must be a non-empty tuple")
        if any(type(value) is not str or not value or value != value.strip() for value in regions):
            raise ValueError("regions must contain canonical non-empty text")
        if any(type(value) is not str or not value or value != value.strip() for value in markets):
            raise ValueError("markets must contain canonical non-empty text")

        product_transport, require_network_authority = (
            build_product_owned_transport()
        )
        provider = object.__new__(provider_type)
        canonical_provider_init(
            provider,
            api_key,
            public_preview=False,
            regions=regions,
            markets=markets,
            base_url="https://parlay-api.com",
            transport=product_transport,
            clock=canonical_clock,
            sleeper=canonical_sleeper,
        )
        if not provider_state_is_current(
            provider,
            regions=regions,
            markets=markets,
            expected_transport=product_transport,
        ):
            raise ProviderPayloadError(
                "canonical Parlay historical provider authority changed before acquisition"
            )
        require_network_authority()
        capture = canonical_capture(
            provider,
            requested_at=requested_at,
            output_path=output_path,
            evidence_path=evidence_path,
        )
        require_network_authority()
        if not provider_state_is_current(
            provider,
            regions=regions,
            markets=markets,
            expected_transport=product_transport,
        ):
            raise ProviderPayloadError(
                "canonical Parlay historical provider authority changed during acquisition"
            )
        return issue_capture(capture)

    def assert_historical_snapshot_provider_origin(
        capture: HistoricalSnapshotCapture,
    ) -> None:
        """Require the exact live object issued by canonical production acquisition."""

        if type(capture) is not HistoricalSnapshotCapture:
            raise ProviderPayloadError(
                "historical snapshot provider origin requires exact capture type"
            )
        current = issued.get(id(capture))
        if (
            current is None
            or current[0]() is not capture
            or current[1] != capture_fingerprint(capture)
        ):
            raise ProviderPayloadError(
                "historical snapshot was not issued by canonical product-owned Parlay acquisition"
            )

    return (
        capture_product_owned_historical_snapshot,
        assert_historical_snapshot_provider_origin,
    )


(
    capture_product_owned_historical_snapshot,
    assert_historical_snapshot_provider_origin,
) = _build_historical_snapshot_provider_origin_authority()
del _build_historical_snapshot_provider_origin_authority


def _bind_quote_to_snapshot(
    quote: ProviderQuote,
    snapshot_at: str,
    snapshot_dt: datetime,
) -> tuple[ProviderQuote, bool]:
    metadata = dict(quote.metadata)
    if quote.source_ts is None:
        metadata["source_time_semantics"] = "provider_historical_snapshot_timestamp"
        metadata["provider_quote_last_update_present"] = False
        return replace(quote, source_ts=snapshot_at, metadata=metadata), True

    quote_dt = _parse_timestamp(quote.source_ts, field="provider quote last_update")
    if quote_dt > snapshot_dt:
        raise ProviderPayloadError("provider quote last_update is after historical snapshot timestamp")
    metadata["source_time_semantics"] = "provider_quote_last_update"
    metadata["provider_quote_last_update_present"] = True
    return replace(quote, metadata=metadata), False


def _required_text(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ProviderPayloadError(f"historical odds response requires non-empty {field}")
    return value.strip()


def _optional_timestamp(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ProviderPayloadError(f"{field} must be null or a non-empty timestamp")
    parsed = value.strip()
    _parse_timestamp(parsed, field=field)
    return parsed


def _parse_timestamp(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ProviderPayloadError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderPayloadError(f"{field} must include an explicit timezone")
    return parsed


def _atomic_write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        temporary = Path(temporary_name)
        try:
            handle = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            descriptor = None
            raise
        descriptor = None

        with handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        with durable_path_lock(path):
            os.replace(temporary, path)
        temporary = None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m autosport.historical_snapshot",
        description="Capture one authenticated point-in-time table-tennis historical odds snapshot.",
    )
    parser.add_argument("--at", required=True, help="requested historical snapshot timestamp (ISO-8601 with timezone)")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".autosport-workspace/historical-snapshot.jsonl"),
        help="canonical market-event JSONL output",
    )
    parser.add_argument("--evidence", type=Path, default=None, help="optional machine evidence JSON path")
    parser.add_argument("--regions", default="us", help="comma-separated provider regions")
    parser.add_argument("--markets", default="h2h,spreads,totals", help="comma-separated historical game-line markets")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    api_key = os.environ.get("AUTOSPORT_PARLAYAPI_KEY")
    if not api_key:
        print("historical_snapshot=BLOCKED reason=AUTOSPORT_PARLAYAPI_KEY_not_set")
        return 2
    regions = tuple(value.strip() for value in args.regions.split(",") if value.strip())
    markets = tuple(value.strip() for value in args.markets.split(",") if value.strip())
    try:
        report = capture_product_owned_historical_snapshot(
            api_key=api_key,
            regions=regions,
            markets=markets,
            requested_at=args.at,
            output_path=args.output,
            evidence_path=args.evidence,
        )
    except (ProviderTransportError, ProviderPayloadError, ValueError, OSError) as exc:
        print(f"historical_snapshot=FAIL_CLOSED error={exc}")
        return 3

    status = "DATA_AVAILABLE" if report.has_data else "NO_DATA"
    print(
        f"historical_snapshot={status} quotes={report.quote_count} "
        f"snapshot_at={report.snapshot_at} fallback_source_times={report.snapshot_timestamp_fallback_count}"
    )
    print(
        "point_in_time_snapshot_contains_odds=" + str(report.has_data).lower()
        + " point_in_time_odds_market_coverage_verified=false"
        + " historical_window_market_coverage_verified=false"
        + " sealed_outcomes_present=false replay_corpus_ready=false"
    )
    print("licensing_or_retention_verified=false real_money_execution=false")
    print(f"market={report.output_path}")
    print(f"evidence={report.evidence_path}")
    return 0 if report.has_data else 6


if __name__ == "__main__":
    raise SystemExit(main())
