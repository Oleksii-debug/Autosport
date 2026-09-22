from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .providers import ProviderUnavailableError


_SANDBOX_BASE_URL = "https://api.sandbox.prophetx.dev/partner"
_TOURNAMENTS_PATH = "/mm/get_tournaments"
_EVENTS_PATH = "/mm/get_sport_events"
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ProphetXDiscoveryPayloadError(ValueError):
    """Provider discovery evidence is malformed or semantically ambiguous."""


class ProphetXDiscoveryUnavailable(ProviderUnavailableError):
    """A read-only discovery request was unavailable before publication."""

    def __init__(self, code: str, status_code: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ProphetXDiscoveryJsonResponse:
    payload: Any
    status_code: int
    headers: Mapping[str, str]
    body_sha256: str

    def __post_init__(self) -> None:
        if type(self.status_code) is not int:
            raise TypeError("status_code must be a non-boolean int")
        if not isinstance(self.headers, Mapping):
            raise TypeError("headers must be a mapping")
        if not isinstance(self.body_sha256, str) or not _SHA256_RE.fullmatch(self.body_sha256):
            raise ValueError("body_sha256 must be lowercase SHA-256 hex")


@dataclass(frozen=True, slots=True)
class ProphetXDiscoveryAcquisition:
    request_kind: str
    tournament_id: str | None
    observed_at: str
    response_sha256: str
    data_context_id: str
    environment: str = "sandbox"
    provider: str = "prophetx"
    source_timestamp: None = None


@dataclass(frozen=True, slots=True)
class ProphetXTournament:
    tournament_id: str
    name: str | None
    canonical_sport: str | None

    @property
    def identity(self) -> str:
        return self.tournament_id


@dataclass(frozen=True, slots=True)
class ProphetXSportEvent:
    tournament_id: str
    event_id: str
    name: str | None
    canonical_sport: str | None

    @property
    def identity(self) -> tuple[str, str]:
        return (self.tournament_id, self.event_id)


@dataclass(frozen=True, slots=True)
class ProphetXDiscoveryFailure:
    tournament_id: str
    code: str
    status_code: int | None


@dataclass(frozen=True, slots=True)
class ProphetXFixtureCatalog:
    tournaments: tuple[ProphetXTournament, ...]
    events: tuple[ProphetXSportEvent, ...]
    acquisitions: tuple[ProphetXDiscoveryAcquisition, ...]
    failures: tuple[ProphetXDiscoveryFailure, ...]
    ambiguous_event_ids: tuple[str, ...]
    complete: bool
    atomic_snapshot: bool = False
    provider: str = "prophetx"
    environment: str = "sandbox"
    globally_unique_event_ids_proven: bool = False

    def __post_init__(self) -> None:
        if self.atomic_snapshot:
            raise ValueError("ProphetX fixture discovery is not an atomic provider snapshot")
        if self.complete != (len(self.failures) == 0):
            raise ValueError("complete must exactly reflect per-tournament acquisition failures")


Transport = Callable[[str, Mapping[str, str], float], ProphetXDiscoveryJsonResponse]
Clock = Callable[[], str]


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise HTTPError(req.full_url, code, "provider redirect refused", headers, fp)


def _decode_provider_json(raw: bytes) -> Any:
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise ProphetXDiscoveryPayloadError("provider response exceeds maximum size")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProphetXDiscoveryPayloadError("provider returned invalid UTF-8 JSON") from exc

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ProphetXDiscoveryPayloadError("provider returned duplicate JSON key")
            result[key] = value
        return result

    def reject_non_finite(value: str) -> None:
        raise ProphetXDiscoveryPayloadError("provider returned non-standard JSON number")

    try:
        return json.loads(
            text,
            object_pairs_hook=reject_duplicate_keys,
            parse_float=Decimal,
            parse_constant=reject_non_finite,
        )
    except json.JSONDecodeError as exc:
        raise ProphetXDiscoveryPayloadError("provider returned invalid JSON") from exc


def _default_transport(
    url: str,
    headers: Mapping[str, str],
    timeout: float,
) -> ProphetXDiscoveryJsonResponse:
    if not url.startswith(f"{_SANDBOX_BASE_URL}/"):
        raise ProphetXDiscoveryUnavailable("PROVIDER_ORIGIN_MISMATCH")
    request = Request(url, headers=dict(headers), method="GET")
    opener = build_opener(ProxyHandler({}), _NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            content_type = str(response.headers.get("Content-Type", "")).lower()
            if not content_type.startswith("application/json"):
                raise ProphetXDiscoveryPayloadError("provider response content type is not JSON")
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise ProphetXDiscoveryPayloadError("provider response exceeds maximum size")
            return ProphetXDiscoveryJsonResponse(
                payload=_decode_provider_json(raw),
                status_code=int(response.status),
                headers=dict(response.headers.items()),
                body_sha256=hashlib.sha256(raw).hexdigest(),
            )
    except HTTPError as exc:
        raise ProphetXDiscoveryUnavailable(f"HTTP_{int(exc.code)}", int(exc.code)) from exc
    except (URLError, TimeoutError) as exc:
        raise ProphetXDiscoveryUnavailable("TRANSPORT_UNAVAILABLE") from exc


def _trimmed_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty trimmed string")
    if "\r" in value or "\n" in value:
        raise ValueError(f"{field} must not contain line breaks")
    return value


def _identity(value: object, *, field: str) -> str:
    if isinstance(value, bool):
        raise ProphetXDiscoveryPayloadError(f"{field} must be a provider identity")
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProphetXDiscoveryPayloadError(f"{field} must be a non-empty trimmed provider identity")
    if "|" in value:
        raise ProphetXDiscoveryPayloadError(f"{field} contains reserved identity delimiter")
    return value


def _optional_name(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProphetXDiscoveryPayloadError(f"{field} must be a non-empty trimmed string when present")
    return value


def _canonical_sport(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("canonical sport must be a non-empty trimmed string")
    if value != value.lower() or value in {"unknown", "mixed"}:
        raise ValueError("canonical sport must be a non-reserved lowercase identity")
    if any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for ch in value):
        raise ValueError("canonical sport contains unsupported characters")
    return value


def _timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout_seconds must be a positive finite number")
    numeric = float(value)
    if not (numeric > 0 and numeric < float("inf")):
        raise ValueError("timeout_seconds must be a positive finite number")
    return numeric


def _canonical_observed_at(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProphetXDiscoveryPayloadError("product observation time must be canonical ISO-8601 text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProphetXDiscoveryPayloadError("product observation time must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProphetXDiscoveryPayloadError("product observation time must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _collection(payload: Any, *, key: str) -> list[Mapping[str, Any]]:
    if not isinstance(payload, dict) or "data" not in payload:
        raise ProphetXDiscoveryPayloadError("provider discovery response requires data")
    data = payload["data"]
    if not isinstance(data, dict) or key not in data:
        raise ProphetXDiscoveryPayloadError(f"provider discovery response requires data.{key}")
    values = data[key]
    if not isinstance(values, list) or not all(isinstance(item, dict) for item in values):
        raise ProphetXDiscoveryPayloadError(f"data.{key} must be a list of objects")
    return values


def _same_tournament(left: ProphetXTournament, right: ProphetXTournament) -> bool:
    return left == right


def _same_event(left: ProphetXSportEvent, right: ProphetXSportEvent) -> bool:
    return left == right


class ProphetXFixtureDiscovery:
    """Strictly read-only ProphetX sandbox tournament/event discovery.

    Tournament names and event names are descriptive only. Canonical sport identity can
    enter only through an explicit provider-tournament-id mapping supplied by product
    configuration; display text is never inspected to infer sport.
    """

    source_id = "prophetx:sandbox:fixture-discovery"

    def __init__(
        self,
        bearer_token: str,
        *,
        data_context_id: str,
        sport_by_tournament_id: Mapping[str, str] | None = None,
        timeout_seconds: float = 10.0,
        transport: Transport = _default_transport,
        clock: Clock = _utc_now_iso,
    ) -> None:
        self._bearer_token = _trimmed_text(bearer_token, field="bearer_token")
        self.data_context_id = _trimmed_text(data_context_id, field="data_context_id")
        self.timeout_seconds = _timeout(timeout_seconds)
        self.transport = transport
        self.clock = clock
        raw_mapping = {} if sport_by_tournament_id is None else dict(sport_by_tournament_id)
        normalized: dict[str, str] = {}
        for tournament_id, sport in raw_mapping.items():
            normalized[_identity(tournament_id, field="sport mapping tournament_id")] = _canonical_sport(sport)
        self.sport_by_tournament_id = normalized

    def discover(self) -> ProphetXFixtureCatalog:
        acquisitions: list[ProphetXDiscoveryAcquisition] = []
        tournament_response = self._get(_TOURNAMENTS_PATH)
        tournament_observed = _canonical_observed_at(self.clock())
        acquisitions.append(
            self._acquisition(
                request_kind="tournaments",
                tournament_id=None,
                observed_at=tournament_observed,
                response=tournament_response,
            )
        )
        tournaments = self._parse_tournaments(tournament_response.payload)

        events: list[ProphetXSportEvent] = []
        failures: list[ProphetXDiscoveryFailure] = []
        for tournament in tournaments:
            try:
                response = self._get(_EVENTS_PATH, {"tournament_id": tournament.tournament_id})
            except ProphetXDiscoveryUnavailable as exc:
                failures.append(
                    ProphetXDiscoveryFailure(
                        tournament_id=tournament.tournament_id,
                        code=exc.code,
                        status_code=exc.status_code,
                    )
                )
                continue
            observed_at = _canonical_observed_at(self.clock())
            acquisitions.append(
                self._acquisition(
                    request_kind="sport_events",
                    tournament_id=tournament.tournament_id,
                    observed_at=observed_at,
                    response=response,
                )
            )
            events.extend(self._parse_events(response.payload, tournament=tournament))

        event_scopes: dict[str, set[str]] = {}
        for event in events:
            event_scopes.setdefault(event.event_id, set()).add(event.tournament_id)
        ambiguous = tuple(sorted(event_id for event_id, scopes in event_scopes.items() if len(scopes) > 1))

        return ProphetXFixtureCatalog(
            tournaments=tuple(tournaments),
            events=tuple(events),
            acquisitions=tuple(acquisitions),
            failures=tuple(failures),
            ambiguous_event_ids=ambiguous,
            complete=not failures,
        )

    def _headers(self) -> Mapping[str, str]:
        return {
            "Authorization": f"Bearer {self._bearer_token}",
            "Accept": "application/json",
        }

    def _get(
        self,
        path: str,
        query: Mapping[str, str] | None = None,
    ) -> ProphetXDiscoveryJsonResponse:
        suffix = "" if not query else f"?{urlencode(query)}"
        response = self.transport(
            f"{_SANDBOX_BASE_URL}{path}{suffix}",
            self._headers(),
            self.timeout_seconds,
        )
        if response.status_code != 200:
            raise ProphetXDiscoveryUnavailable(f"HTTP_{response.status_code}", response.status_code)
        return response

    def _acquisition(
        self,
        *,
        request_kind: str,
        tournament_id: str | None,
        observed_at: str,
        response: ProphetXDiscoveryJsonResponse,
    ) -> ProphetXDiscoveryAcquisition:
        return ProphetXDiscoveryAcquisition(
            request_kind=request_kind,
            tournament_id=tournament_id,
            observed_at=observed_at,
            response_sha256=response.body_sha256,
            data_context_id=self.data_context_id,
        )

    def _parse_tournaments(self, payload: Any) -> list[ProphetXTournament]:
        result: dict[str, ProphetXTournament] = {}
        for raw in _collection(payload, key="tournaments"):
            tournament_id = _identity(raw.get("id"), field="tournament id")
            tournament = ProphetXTournament(
                tournament_id=tournament_id,
                name=_optional_name(raw.get("name"), field="tournament name"),
                canonical_sport=self.sport_by_tournament_id.get(tournament_id),
            )
            prior = result.get(tournament_id)
            if prior is not None and not _same_tournament(prior, tournament):
                raise ProphetXDiscoveryPayloadError("conflicting duplicate tournament identity")
            result[tournament_id] = tournament
        return [result[key] for key in sorted(result)]

    def _parse_events(
        self,
        payload: Any,
        *,
        tournament: ProphetXTournament,
    ) -> list[ProphetXSportEvent]:
        result: dict[str, ProphetXSportEvent] = {}
        for raw in _collection(payload, key="sport_events"):
            event_id = _identity(raw.get("event_id"), field="event_id")
            if "tournament_id" in raw:
                row_tournament_id = _identity(raw.get("tournament_id"), field="event tournament_id")
                if row_tournament_id != tournament.tournament_id:
                    raise ProphetXDiscoveryPayloadError("event tournament_id conflicts with request scope")
            event = ProphetXSportEvent(
                tournament_id=tournament.tournament_id,
                event_id=event_id,
                name=_optional_name(raw.get("name"), field="event name"),
                canonical_sport=tournament.canonical_sport,
            )
            prior = result.get(event_id)
            if prior is not None and not _same_event(prior, event):
                raise ProphetXDiscoveryPayloadError("conflicting duplicate event identity in tournament scope")
            result[event_id] = event
        return [result[key] for key in sorted(result)]
