from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen


_PARLAY_ORIGIN = "https://parlay-api.com"
_SPORT_KEY = "table_tennis"
_MATCHES_PATH = f"/v1/historical/sports/{_SPORT_KEY}/matches"
_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_REQUEST_LIMIT = 5000
_USER_AGENT = "Autosport/0.1 historical-match-acquisition"


class HistoricalMatchAcquisitionError(ValueError):
    """Raised when historical-match evidence is malformed or origin-ambiguous."""


class HistoricalMatchTransportError(RuntimeError):
    """Raised when the fixed-origin historical endpoint cannot be read safely."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class RawHistoricalMatchResponse:
    body: bytes
    status_code: int
    headers: Mapping[str, str]
    final_url: str


@dataclass(frozen=True, slots=True)
class ParlayHistoricalMatchAcquisition:
    """Immutable evidence for one authenticated ParlayAPI match-archive read.

    This proves only the HTTP acquisition boundary.  It deliberately does not
    translate scores into Autosport outcome labels or grant settlement/training
    authority.
    """

    sport_key: str
    date_from: str
    date_to: str
    requested_sources: tuple[str, ...]
    requested_limit: int
    acquired_at: str
    request_url: str
    final_url: str
    response_sha256: str
    response_bytes: bytes
    row_count: int
    observed_sources: tuple[str, ...]
    historical_window_hours: int
    historical_window_from: str
    api_version: str | None
    coverage_hint: str | None
    possibly_truncated: bool
    provider_origin_verified: bool
    acquisition_time_verified: bool


HistoricalMatchTransport = Callable[
    [str, Mapping[str, str], float, int],
    RawHistoricalMatchResponse,
]


def _canonical_date(value: object, *, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise HistoricalMatchAcquisitionError(f"{field} must be canonical YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise HistoricalMatchAcquisitionError(
            f"{field} must be canonical YYYY-MM-DD"
        ) from exc
    if parsed.isoformat() != value:
        raise HistoricalMatchAcquisitionError(f"{field} must be canonical YYYY-MM-DD")
    return value


def _canonical_source(value: object, *, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise HistoricalMatchAcquisitionError(
            f"{field} must be a non-empty canonical source key"
        )
    if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in value):
        raise HistoricalMatchAcquisitionError(
            f"{field} must use lowercase ASCII letters, digits, '_' or '-' only"
        )
    return value


def _canonical_sources(values: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    if values is None:
        return ()
    if type(values) not in {tuple, list}:
        raise HistoricalMatchAcquisitionError(
            "sources must be a tuple/list of canonical source keys"
        )
    normalized = tuple(
        _canonical_source(value, field=f"sources[{index}]")
        for index, value in enumerate(values)
    )
    if len(set(normalized)) != len(normalized):
        raise HistoricalMatchAcquisitionError("sources must not contain duplicates")
    return tuple(sorted(normalized))


def _finite_positive_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HistoricalMatchAcquisitionError("timeout_seconds must be a positive finite number")
    numeric = float(value)
    if numeric <= 0 or numeric == float("inf") or numeric == float("-inf") or numeric != numeric:
        raise HistoricalMatchAcquisitionError("timeout_seconds must be a positive finite number")
    return numeric


def _header(headers: Mapping[str, str], name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target and str(value).strip():
            return str(value).strip()
    return None


def _parse_historical_window_from(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HistoricalMatchAcquisitionError(
                "x-historical-window-from must be an ISO date or timezone-aware timestamp"
            ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise HistoricalMatchAcquisitionError(
                "x-historical-window-from timestamp must include timezone"
            )
        return parsed.date()


def _strict_json_rows(payload: bytes) -> list[dict[str, object]]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HistoricalMatchAcquisitionError(
            "historical matches response is not valid UTF-8"
        ) from exc

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        output: dict[str, object] = {}
        for key, value in pairs:
            if key in output:
                raise HistoricalMatchAcquisitionError(
                    f"historical matches response contains duplicate JSON key {key!r}"
                )
            output[key] = value
        return output

    def reject_constant(value: str) -> None:
        raise HistoricalMatchAcquisitionError(
            f"historical matches response contains non-standard JSON constant {value}"
        )

    try:
        raw = json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise HistoricalMatchAcquisitionError(
            "historical matches response is not valid JSON"
        ) from exc
    except RecursionError as exc:
        raise HistoricalMatchAcquisitionError(
            "historical matches response exceeds supported JSON nesting"
        ) from exc
    if not isinstance(raw, list):
        raise HistoricalMatchAcquisitionError(
            "historical matches response must be a JSON array"
        )
    if any(not isinstance(row, dict) for row in raw):
        raise HistoricalMatchAcquisitionError(
            "historical matches response rows must be JSON objects"
        )
    return raw


def _validate_final_url(final_url: object, request_url: str) -> str:
    if type(final_url) is not str or not final_url or final_url != final_url.strip():
        raise HistoricalMatchAcquisitionError(
            "historical matches final URL must be a canonical absolute URL"
        )
    actual = urlsplit(final_url)
    requested = urlsplit(request_url)
    if (
        actual.scheme != "https"
        or actual.hostname != "parlay-api.com"
        or actual.port not in {None, 443}
        or actual.username is not None
        or actual.password is not None
        or actual.path != _MATCHES_PATH
        or actual.fragment
    ):
        raise HistoricalMatchAcquisitionError(
            "historical matches final URL escaped the fixed ParlayAPI origin/path"
        )
    if parse_qs(actual.query, keep_blank_values=True) != parse_qs(
        requested.query, keep_blank_values=True
    ):
        raise HistoricalMatchAcquisitionError(
            "historical matches final URL changed the requested evidence scope"
        )
    return final_url


def _default_transport(
    url: str,
    headers: Mapping[str, str],
    timeout_seconds: float,
    max_response_bytes: int,
    *,
    _open: Callable[..., object] = urlopen,
) -> RawHistoricalMatchResponse:
    request = Request(url, headers=dict(headers), method="GET")
    try:
        with _open(request, timeout=timeout_seconds) as response:
            body = response.read(max_response_bytes + 1)
            if len(body) > max_response_bytes:
                raise HistoricalMatchTransportError(
                    "historical matches response exceeded the fixed size bound"
                )
            return RawHistoricalMatchResponse(
                body=body,
                status_code=int(response.status),
                headers=dict(response.headers.items()),
                final_url=str(response.geturl()),
            )
    except HTTPError as exc:
        raise HistoricalMatchTransportError(
            f"historical matches endpoint returned HTTP {exc.code}",
            status_code=int(exc.code),
        ) from exc
    except URLError as exc:
        raise HistoricalMatchTransportError(
            "historical matches endpoint transport failed"
        ) from exc


def _system_clock(*, _now: Callable[..., datetime] = datetime.now) -> str:
    return _now(timezone.utc).isoformat()


def _build_acquirer(
    trusted_transport: HistoricalMatchTransport,
    trusted_clock: Callable[[], str],
) -> Callable[..., ParlayHistoricalMatchAcquisition]:
    def acquire_parlay_historical_matches(
        *,
        api_key: str,
        date_from: str,
        date_to: str,
        sources: tuple[str, ...] | list[str] | None = None,
        timeout_seconds: float = 10.0,
        clock: Callable[[], str] | None = None,
        transport: HistoricalMatchTransport | None = None,
    ) -> ParlayHistoricalMatchAcquisition:
        """Acquire raw table-tennis match/results bytes from the fixed API origin.

        Passing ``transport`` is an explicit test/simulation seam.  Such a call is
        useful for deterministic qualification but can never set
        ``provider_origin_verified`` true.
        """

        if type(api_key) is not str or not api_key or api_key != api_key.strip():
            raise HistoricalMatchAcquisitionError(
                "api_key must be a non-empty canonical string"
            )
        start = _canonical_date(date_from, field="date_from")
        end = _canonical_date(date_to, field="date_to")
        if date.fromisoformat(end) < date.fromisoformat(start):
            raise HistoricalMatchAcquisitionError("date_to must not precede date_from")
        requested_sources = _canonical_sources(sources)
        timeout = _finite_positive_timeout(timeout_seconds)
        selected_clock = trusted_clock if clock is None else clock
        if not callable(selected_clock):
            raise HistoricalMatchAcquisitionError("clock must be callable")

        query: dict[str, str] = {
            "dateFrom": start,
            "dateTo": end,
            "pricedOnly": "false",
            "includeRaw": "false",
            "limit": str(_REQUEST_LIMIT),
        }
        if requested_sources:
            query["sources"] = ",".join(requested_sources)
        request_url = f"{_PARLAY_ORIGIN}{_MATCHES_PATH}?{urlencode(query)}"
        headers = {
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
            "X-API-Key": api_key,
        }

        selected_transport = trusted_transport if transport is None else transport
        if not callable(selected_transport):
            raise HistoricalMatchAcquisitionError("transport must be callable")
        response = selected_transport(
            request_url,
            headers,
            timeout,
            _MAX_RESPONSE_BYTES,
        )
        if type(response) is not RawHistoricalMatchResponse:
            raise HistoricalMatchAcquisitionError(
                "historical match transport must return exact RawHistoricalMatchResponse"
            )
        if type(response.body) is not bytes:
            raise HistoricalMatchAcquisitionError(
                "historical match transport body must be exact bytes"
            )
        if len(response.body) > _MAX_RESPONSE_BYTES:
            raise HistoricalMatchAcquisitionError(
                "historical matches response exceeded the fixed size bound"
            )
        if type(response.status_code) is not int or response.status_code != 200:
            status = response.status_code if type(response.status_code) is int else None
            raise HistoricalMatchTransportError(
                "historical matches endpoint did not return HTTP 200",
                status_code=status,
            )
        final_url = _validate_final_url(response.final_url, request_url)

        window_hours_raw = _header(response.headers, "x-historical-window-hours")
        window_from_raw = _header(response.headers, "x-historical-window-from")
        if window_hours_raw is None or window_from_raw is None:
            raise HistoricalMatchAcquisitionError(
                "historical matches response is missing entitlement-window headers"
            )
        try:
            window_hours = int(window_hours_raw)
        except ValueError as exc:
            raise HistoricalMatchAcquisitionError(
                "x-historical-window-hours must be a positive integer"
            ) from exc
        if window_hours <= 0:
            raise HistoricalMatchAcquisitionError(
                "x-historical-window-hours must be a positive integer"
            )
        if date.fromisoformat(start) < _parse_historical_window_from(window_from_raw):
            raise HistoricalMatchAcquisitionError(
                "historical matches response contradicts its entitlement window"
            )

        rows = _strict_json_rows(response.body)
        observed_sources: set[str] = set()
        for index, row in enumerate(rows):
            if type(row.get("has_odds")) is not bool:
                raise HistoricalMatchAcquisitionError(
                    f"historical match row {index} must carry exact boolean has_odds"
                )
            source = _canonical_source(
                row.get("source"),
                field=f"historical match row {index}.source",
            )
            if requested_sources and source not in requested_sources:
                raise HistoricalMatchAcquisitionError(
                    f"historical match row {index} source escaped requested sources"
                )
            observed_sources.add(source)

        acquired_at = selected_clock()
        if type(acquired_at) is not str or not acquired_at or acquired_at != acquired_at.strip():
            raise HistoricalMatchAcquisitionError(
                "clock must return a non-empty canonical timestamp string"
            )
        try:
            acquired_dt = datetime.fromisoformat(acquired_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HistoricalMatchAcquisitionError(
                "clock must return an ISO-8601 timestamp"
            ) from exc
        if acquired_dt.tzinfo is None or acquired_dt.utcoffset() is None:
            raise HistoricalMatchAcquisitionError(
                "clock timestamp must include timezone"
            )

        api_version = _header(response.headers, "x-api-version")
        coverage_hint = _header(response.headers, "x-coverage-hint")
        if coverage_hint is not None and len(coverage_hint) > 4096:
            raise HistoricalMatchAcquisitionError(
                "x-coverage-hint exceeds the bounded evidence size"
            )

        return ParlayHistoricalMatchAcquisition(
            sport_key=_SPORT_KEY,
            date_from=start,
            date_to=end,
            requested_sources=requested_sources,
            requested_limit=_REQUEST_LIMIT,
            acquired_at=acquired_at,
            request_url=request_url,
            final_url=final_url,
            response_sha256=hashlib.sha256(response.body).hexdigest(),
            response_bytes=response.body,
            row_count=len(rows),
            observed_sources=tuple(sorted(observed_sources)),
            historical_window_hours=window_hours,
            historical_window_from=window_from_raw,
            api_version=api_version,
            coverage_hint=coverage_hint,
            possibly_truncated=len(rows) == _REQUEST_LIMIT,
            provider_origin_verified=transport is None,
            acquisition_time_verified=clock is None,
        )

    return acquire_parlay_historical_matches


acquire_parlay_historical_matches = _build_acquirer(_default_transport, _system_clock)
