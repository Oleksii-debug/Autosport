from __future__ import annotations

import builtins
import hashlib
import hmac
import inspect
import json
import math
import os
import secrets
import weakref
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Mapping
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
    resolve_monotonic_authority_root,
)
from .workspace_lock import WorkspaceEconomicLock


SCHEMA = "autosport.provider_complete_game_board"
SCHEMA_VERSION = 1
COMPLETE_SNAPSHOT_SCOPE = "current_game_board"
COMPLETE_RESUME_MODE = "replace"
GAME_LINE_MARKETS = ("h2h", "spreads", "totals")
_MAX_SSE_BYTES = 16 * 1024 * 1024
_HEX = frozenset("0123456789abcdef")
_IDENTITY_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")
_RECEIPT_SCHEMA = "autosport.provider_acquisition_receipt"
_RECEIPT_SCHEMA_VERSION = 1
_RECEIPT_ROOT_NAME = "provider-acquisition-receipt-v1"
_RECEIPT_KEY_BYTES = 32
_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "workspace_sha256",
        "evidence_sha256",
        "state_sha256",
        "semantic_binding_sha256",
        "hmac_sha256",
    }
)


class ProviderObservationAuthorityError(RuntimeError):
    """Base error for complete-provider observation authority."""


class ProviderObservationUnsupportedError(ProviderObservationAuthorityError):
    """Provider evidence does not prove an exhaustive observation scope."""


class ProviderObservationIntegrityError(ProviderObservationAuthorityError):
    """Persisted or returned provider evidence is malformed or inconsistent."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ProviderObservationIntegrityError(f"{name} must be non-empty canonical text")
    value.encode("utf-8")
    return value


def _provider_identity(value: object, name: str) -> str:
    raw = _text(value, name)
    if raw != raw.lower() or any(character not in _IDENTITY_CHARS for character in raw):
        raise ProviderObservationIntegrityError(
            f"{name} must be lowercase canonical provider identity"
        )
    return raw


def _sha(value: object, name: str) -> str:
    raw = _text(value, name).lower()
    if len(raw) != 64 or any(character not in _HEX for character in raw):
        raise ProviderObservationIntegrityError(f"{name} must be canonical SHA-256 hex")
    return raw


def _instant(value: object, name: str) -> str:
    raw = _text(value, name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderObservationIntegrityError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderObservationIntegrityError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ProviderObservationIntegrityError(
            "provider evidence must be canonical finite JSON"
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _default_clock() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class CompleteGameBoardRequest:
    """Exact documented scope eligible for a complete current game-line baseline."""

    sport_key: str
    bookmakers: tuple[str, ...]
    markets: tuple[str, ...] = GAME_LINE_MARKETS
    kind: str = "game"
    limit: int = 1000
    max_age_s: int = 600

    def __post_init__(self) -> None:
        object.__setattr__(self, "sport_key", _provider_identity(self.sport_key, "sport_key"))
        if not isinstance(self.bookmakers, tuple) or not self.bookmakers:
            raise ProviderObservationIntegrityError("bookmakers must be a non-empty tuple")
        books = tuple(sorted(_provider_identity(book, "bookmaker") for book in self.bookmakers))
        if len(books) != len(set(books)):
            raise ProviderObservationIntegrityError("bookmakers must be unique")
        object.__setattr__(self, "bookmakers", books)
        if not isinstance(self.markets, tuple) or self.markets != GAME_LINE_MARKETS:
            raise ProviderObservationUnsupportedError(
                "complete game-board authority requires h2h, spreads and totals together"
            )
        if self.kind != "game":
            raise ProviderObservationUnsupportedError(
                "complete provider authority is limited to kind=game"
            )
        if type(self.limit) is not int or self.limit != 1000:
            raise ProviderObservationUnsupportedError(
                "complete provider authority requires the documented limit=1000 scope"
            )
        if type(self.max_age_s) is not int or not 1 <= self.max_age_s <= 3600:
            raise ProviderObservationIntegrityError("max_age_s must be an integer in 1..3600")

    @property
    def source_id(self) -> str:
        return f"parlayapi:{self.sport_key}"

    def to_payload(self) -> dict[str, object]:
        return {
            "sport_key": self.sport_key,
            "bookmakers": list(self.bookmakers),
            "markets": list(self.markets),
            "kind": self.kind,
            "limit": self.limit,
            "max_age_s": self.max_age_s,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "CompleteGameBoardRequest":
        try:
            books = payload["bookmakers"]
            markets = payload["markets"]
            if not isinstance(books, list) or not isinstance(markets, list):
                raise ProviderObservationIntegrityError(
                    "request bookmakers/markets must be JSON arrays"
                )
            return cls(
                sport_key=payload["sport_key"],
                bookmakers=tuple(books),
                markets=tuple(markets),
                kind=payload["kind"],
                limit=payload["limit"],
                max_age_s=payload["max_age_s"],
            )
        except KeyError as exc:
            raise ProviderObservationIntegrityError(
                "complete game-board request payload is incomplete"
            ) from exc

    def sse_url(self, *, _urlencode=urlencode) -> str:
        query = _urlencode(
            {
                "bookmakers": ",".join(self.bookmakers),
                "kinds": self.kind,
                "markets": ",".join(self.markets),
                "limit": str(self.limit),
                "max_age_s": str(self.max_age_s),
            }
        )
        return f"https://parlay-api.com/v1/sse/odds/{self.sport_key}?{query}"


@dataclass(frozen=True, slots=True, weakref_slot=True)
class CompleteGameBoardSnapshot:
    """Immutable exact response evidence; construction alone grants no authority."""

    request: CompleteGameBoardRequest
    captured_at: str
    frame_json: str

    def __post_init__(self) -> None:
        if not isinstance(self.request, CompleteGameBoardRequest):
            raise ProviderObservationIntegrityError("request must be CompleteGameBoardRequest")
        object.__setattr__(self, "captured_at", _instant(self.captured_at, "captured_at"))
        if not isinstance(self.frame_json, str):
            raise ProviderObservationIntegrityError("frame_json must be text")
        try:
            frame = strict_json_loads(self.frame_json)
        except (TypeError, ValueError) as exc:
            raise ProviderObservationIntegrityError("initial_state frame is invalid JSON") from exc
        if not isinstance(frame, dict):
            raise ProviderObservationIntegrityError("initial_state frame must be an object")
        object.__setattr__(self, "frame_json", _canonical_json(frame))
        self._validate_frame(frame)

    def _validate_frame(self, frame: Mapping[str, object]) -> None:
        if frame.get("type") != "initial_state":
            raise ProviderObservationUnsupportedError(
                "provider frame is not an initial_state snapshot"
            )
        if frame.get("sport_key") != self.request.sport_key:
            raise ProviderObservationIntegrityError("provider frame sport scope mismatch")
        if frame.get("snapshot_scope") != COMPLETE_SNAPSHOT_SCOPE:
            raise ProviderObservationUnsupportedError(
                "provider snapshot scope is not current_game_board"
            )
        if frame.get("snapshot_complete") is not True:
            raise ProviderObservationUnsupportedError(
                "provider did not certify snapshot_complete=true"
            )
        if frame.get("truncated") is not False:
            raise ProviderObservationUnsupportedError(
                "truncated provider snapshot cannot prove complete membership"
            )
        if frame.get("resume_mode") != COMPLETE_RESUME_MODE:
            raise ProviderObservationUnsupportedError(
                "complete provider snapshot must use replacement semantics"
            )
        partial = frame.get("partial")
        if partial is not None and partial is not False:
            raise ProviderObservationUnsupportedError(
                "partial provider snapshot cannot prove complete membership"
            )
        for name in (
            "partial_reason",
            "missing_books",
            "truncated_books",
            "snapshot_partial_reasons",
        ):
            raw = frame.get(name)
            if raw not in (None, [], ""):
                raise ProviderObservationUnsupportedError(
                    f"provider snapshot carries {name} incompleteness evidence"
                )

        rows = frame.get("data")
        count = frame.get("count")
        if not isinstance(rows, list):
            raise ProviderObservationIntegrityError("provider snapshot data must be a list")
        if type(count) is not int or count < 0 or count != len(rows):
            raise ProviderObservationIntegrityError(
                "provider snapshot count must equal exact data length"
            )

        digests: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ProviderObservationIntegrityError(
                    "provider snapshot rows must be JSON objects"
                )
            if row.get("bookmaker") not in self.request.bookmakers:
                raise ProviderObservationIntegrityError(
                    "provider snapshot row escapes requested bookmaker scope"
                )
            if row.get("kind") != self.request.kind:
                raise ProviderObservationIntegrityError(
                    "provider snapshot row escapes requested kind scope"
                )
            if row.get("market_key") not in self.request.markets:
                raise ProviderObservationIntegrityError(
                    "provider snapshot row escapes requested market scope"
                )
            _text(row.get("event_id"), "provider event_id")
            digest = _digest(row)
            if digest in digests:
                raise ProviderObservationIntegrityError(
                    "provider snapshot contains duplicate exact rows"
                )
            digests.add(digest)

    @property
    def frame(self) -> dict[str, object]:
        loaded = strict_json_loads(self.frame_json)
        assert isinstance(loaded, dict)
        return loaded

    @property
    def frame_sha256(self) -> str:
        return hashlib.sha256(self.frame_json.encode("utf-8")).hexdigest()

    @property
    def row_sha256s(self) -> tuple[str, ...]:
        rows = self.frame["data"]
        assert isinstance(rows, list)
        return tuple(sorted(_digest(row) for row in rows))

    @property
    def evidence_sha256(self) -> str:
        return _digest(
            {
                "request": self.request.to_payload(),
                "captured_at": self.captured_at,
                "frame_sha256": self.frame_sha256,
                "row_sha256s": list(self.row_sha256s),
            }
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "request": self.request.to_payload(),
            "captured_at": self.captured_at,
            "frame_json": self.frame_json,
            "frame_sha256": self.frame_sha256,
            "row_sha256s": list(self.row_sha256s),
            "evidence_sha256": self.evidence_sha256,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "CompleteGameBoardSnapshot":
        try:
            if payload["schema"] != SCHEMA or payload["schema_version"] != SCHEMA_VERSION:
                raise ProviderObservationIntegrityError(
                    "unsupported complete game-board evidence schema"
                )
            raw_request = payload["request"]
            if not isinstance(raw_request, Mapping):
                raise ProviderObservationIntegrityError("request payload must be an object")
            snapshot = cls(
                request=CompleteGameBoardRequest.from_payload(raw_request),
                captured_at=payload["captured_at"],
                frame_json=payload["frame_json"],
            )
        except KeyError as exc:
            raise ProviderObservationIntegrityError(
                "complete game-board evidence payload is incomplete"
            ) from exc
        if payload.get("frame_sha256") != snapshot.frame_sha256:
            raise ProviderObservationIntegrityError(
                "frame_sha256 does not bind exact provider frame"
            )
        raw_rows = payload.get("row_sha256s")
        if not isinstance(raw_rows, list) or tuple(raw_rows) != snapshot.row_sha256s:
            raise ProviderObservationIntegrityError(
                "row_sha256s do not bind exact provider rows"
            )
        if payload.get("evidence_sha256") != snapshot.evidence_sha256:
            raise ProviderObservationIntegrityError(
                "evidence_sha256 does not bind complete provider evidence"
            )
        return snapshot


def _build_ephemeral_issuance_registry():
    issued: dict[int, tuple[weakref.ReferenceType, str, object]] = {}
    issuance_token = object()
    reference_factory = weakref.ref
    snapshot_type = CompleteGameBoardSnapshot

    def forget(snapshot_id: int, reference: weakref.ReferenceType) -> None:
        current = issued.get(snapshot_id)
        if current is not None and current[0] is reference:
            issued.pop(snapshot_id, None)

    def remember(snapshot: CompleteGameBoardSnapshot) -> CompleteGameBoardSnapshot:
        if type(snapshot) is not snapshot_type:
            raise ProviderObservationUnsupportedError(
                "complete provider authority requires exact CompleteGameBoardSnapshot"
            )
        snapshot_id = id(snapshot)
        reference = reference_factory(
            snapshot,
            lambda current, snapshot_id=snapshot_id: forget(snapshot_id, current),
        )
        issued[snapshot_id] = (
            reference,
            snapshot.evidence_sha256,
            issuance_token,
        )
        return snapshot

    def assert_authoritative(snapshot: CompleteGameBoardSnapshot) -> None:
        if type(snapshot) is not snapshot_type:
            raise ProviderObservationUnsupportedError(
                "complete provider authority requires exact CompleteGameBoardSnapshot"
            )
        current = issued.get(id(snapshot))
        if (
            current is None
            or current[0]() is not snapshot
            or current[1] != snapshot.evidence_sha256
            or current[2] is not issuance_token
        ):
            raise ProviderObservationUnsupportedError(
                "snapshot was not issued by canonical provider acquisition evidence"
            )

    return remember, assert_authoritative


_remember, assert_complete_game_board_authoritative = (
    _build_ephemeral_issuance_registry()
)


def _parse_sse_event(
    event_name: str | None,
    data_lines: list[str],
    *,
    _loads=strict_json_loads,
) -> Mapping[str, object] | None:
    if not data_lines:
        return None
    raw = "\n".join(data_lines)
    try:
        payload = _loads(raw)
    except (TypeError, ValueError) as exc:
        raise ProviderObservationIntegrityError("provider SSE returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ProviderObservationIntegrityError("provider SSE event data must be an object")
    payload_type = payload.get("type")
    if event_name == "initial_state" or payload_type == "initial_state":
        return payload
    if event_name == "stream_closed" or payload_type == "stream_closed":
        raise ProviderObservationUnsupportedError(
            "provider stream closed before a complete initial state was available"
        )
    return None


def _read_production_initial_state(
    request_scope: CompleteGameBoardRequest,
    *,
    api_key: str,
    timeout_seconds: float,
    _request_factory=Request,
    _open_url=urlopen,
    _parse_event=_parse_sse_event,
    _sse_url=CompleteGameBoardRequest.sse_url,
    _loads=strict_json_loads,
    _max_sse_bytes=_MAX_SSE_BYTES,
) -> Mapping[str, object]:
    """Read one bounded initial_state from the fixed production ParlayAPI SSE origin."""

    request = _request_factory(
        _sse_url(request_scope),
        headers={
            "Accept": "text/event-stream",
            "X-API-Key": api_key,
            "User-Agent": "Autosport/0.1 read-only-complete-board-observer",
        },
        method="GET",
    )
    consumed = 0
    event_name: str | None = None
    data_lines: list[str] = []
    try:
        with _open_url(request, timeout=timeout_seconds) as response:  # nosec B310 - fixed HTTPS origin
            if int(getattr(response, "status", 0)) != 200:
                raise ProviderObservationUnsupportedError("provider SSE did not return HTTP 200")
            headers = getattr(response, "headers", None)
            content_type = None if headers is None else headers.get("Content-Type")
            if not isinstance(content_type, str) or "text/event-stream" not in content_type.lower():
                raise ProviderObservationUnsupportedError(
                    "provider complete-board endpoint did not return text/event-stream"
                )
            for raw_line in response:
                consumed += len(raw_line)
                if consumed > _max_sse_bytes:
                    raise ProviderObservationUnsupportedError(
                        "provider SSE exceeded bounded initial-state evidence budget"
                    )
                try:
                    line = raw_line.decode("utf-8").rstrip("\r\n")
                except UnicodeDecodeError as exc:
                    raise ProviderObservationIntegrityError(
                        "provider SSE returned invalid UTF-8"
                    ) from exc
                if not line:
                    result = _parse_event(event_name, data_lines, _loads=_loads)
                    if result is not None:
                        return result
                    event_name = None
                    data_lines = []
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            result = _parse_event(event_name, data_lines, _loads=_loads)
            if result is not None:
                return result
    except ProviderObservationAuthorityError:
        raise
    except OSError as exc:
        raise ProviderObservationUnsupportedError(
            "provider SSE initial-state acquisition failed"
        ) from exc
    raise ProviderObservationUnsupportedError(
        "provider SSE ended before an initial_state frame was available"
    )


_TEST_ACQUISITION_CAPABILITY = object()
_TEST_ACQUISITION_ORIGIN = ContextVar(
    "autosport_provider_observation_test_origin",
    default=None,
)

_CANONICAL_MODULE_GLOBALS = globals()
_CANONICAL_INSPECT = inspect
_CANONICAL_GETATTR_STATIC = inspect.getattr_static
_CANONICAL_GETATTR_STATIC_CODE = _CANONICAL_GETATTR_STATIC.__code__
_CANONICAL_GETATTR_STATIC_GLOBALS = _CANONICAL_GETATTR_STATIC.__globals__
_CANONICAL_GETATTR_STATIC_GLOBAL_ITEMS = tuple(
    (
        name,
        _CANONICAL_GETATTR_STATIC_GLOBALS[name],
        getattr(_CANONICAL_GETATTR_STATIC_GLOBALS[name], "__code__", None),
    )
    for name in _CANONICAL_GETATTR_STATIC_CODE.co_names
    if name in _CANONICAL_GETATTR_STATIC_GLOBALS
)
_CANONICAL_MATH = math
_CANONICAL_MATH_ISFINITE = math.isfinite
_CANONICAL_HTTP_REQUEST = Request
_CANONICAL_HTTP_REQUEST_INIT = inspect.getattr_static(Request, "__init__")
_CANONICAL_HTTP_REQUEST_INIT_CODE = getattr(
    _CANONICAL_HTTP_REQUEST_INIT,
    "__code__",
    None,
)
_CANONICAL_URLOPEN = urlopen
_CANONICAL_URLOPEN_CODE = getattr(_CANONICAL_URLOPEN, "__code__", None)
_CANONICAL_STRICT_JSON_LOADS = strict_json_loads
_CANONICAL_STRICT_JSON_LOADS_CODE = getattr(
    _CANONICAL_STRICT_JSON_LOADS,
    "__code__",
    None,
)
_CANONICAL_PARSE_SSE_EVENT = _parse_sse_event
_CANONICAL_PARSE_SSE_EVENT_CODE = _CANONICAL_PARSE_SSE_EVENT.__code__
_CANONICAL_READ_PRODUCTION_INITIAL_STATE = _read_production_initial_state
_CANONICAL_READ_PRODUCTION_INITIAL_STATE_CODE = (
    _CANONICAL_READ_PRODUCTION_INITIAL_STATE.__code__
)
_CANONICAL_DEFAULT_CLOCK = _default_clock
_CANONICAL_DEFAULT_CLOCK_CODE = _CANONICAL_DEFAULT_CLOCK.__code__
_CANONICAL_SNAPSHOT_CLASS = CompleteGameBoardSnapshot
_CANONICAL_REQUEST_CLASS = CompleteGameBoardRequest
_CANONICAL_REQUEST_SSE_URL = CompleteGameBoardRequest.sse_url
_CANONICAL_REQUEST_SSE_URL_CODE = _CANONICAL_REQUEST_SSE_URL.__code__
_CANONICAL_CANONICAL_JSON = _canonical_json
_CANONICAL_CANONICAL_JSON_CODE = _CANONICAL_CANONICAL_JSON.__code__
_CANONICAL_REMEMBER = _remember
_CANONICAL_REMEMBER_CODE = _CANONICAL_REMEMBER.__code__
_CANONICAL_ASSERT_COMPLETE_GAME_BOARD_AUTHORITATIVE = (
    assert_complete_game_board_authoritative
)
_CANONICAL_ASSERT_COMPLETE_GAME_BOARD_AUTHORITATIVE_CODE = (
    _CANONICAL_ASSERT_COMPLETE_GAME_BOARD_AUTHORITATIVE.__code__
)
_CANONICAL_TEST_ACQUISITION_ORIGIN = _TEST_ACQUISITION_ORIGIN
_CANONICAL_TEST_ACQUISITION_CAPABILITY = _TEST_ACQUISITION_CAPABILITY


def _surface_code(surface: object) -> object:
    target = getattr(surface, "__func__", None)
    if target is None and isinstance(surface, property):
        target = surface.fget
    if target is None:
        target = surface
    return getattr(target, "__code__", None)


_CANONICAL_SURFACE_CODE = _surface_code
_CANONICAL_SURFACE_CODE_CODE = _surface_code.__code__
_REQUEST_ORIGIN_SURFACE_NAMES = (
    "__init__",
    "__post_init__",
    "source_id",
    "to_payload",
    "sse_url",
)
_SNAPSHOT_ORIGIN_SURFACE_NAMES = (
    "__init__",
    "__post_init__",
    "_validate_frame",
    "frame",
    "frame_sha256",
    "row_sha256s",
    "evidence_sha256",
    "to_payload",
)
_CANONICAL_REQUEST_ORIGIN_SURFACES = tuple(
    (
        name,
        inspect.getattr_static(CompleteGameBoardRequest, name),
        _surface_code(inspect.getattr_static(CompleteGameBoardRequest, name)),
    )
    for name in _REQUEST_ORIGIN_SURFACE_NAMES
)
_CANONICAL_SNAPSHOT_ORIGIN_SURFACES = tuple(
    (
        name,
        inspect.getattr_static(CompleteGameBoardSnapshot, name),
        _surface_code(inspect.getattr_static(CompleteGameBoardSnapshot, name)),
    )
    for name in _SNAPSHOT_ORIGIN_SURFACE_NAMES
)


def _require_production_capture_origin_integrity() -> None:
    module_globals = _CANONICAL_MODULE_GLOBALS
    if (
        module_globals.get("_CANONICAL_MODULE_GLOBALS") is not module_globals
        or "any" in module_globals
        or "getattr" in module_globals
        or "isinstance" in module_globals
        or "type" in module_globals
        or "str" in module_globals
        or "bool" in module_globals
        or "int" in module_globals
        or "float" in module_globals
        or "dict" in module_globals
        or "ValueError" in module_globals
        or "TypeError" in module_globals
    ):
        raise ProviderObservationIntegrityError(
            "provider production acquisition builtin dispatch shadowed"
        )
    if (
        inspect is not _CANONICAL_INSPECT
        or _CANONICAL_INSPECT.getattr_static is not _CANONICAL_GETATTR_STATIC
        or _CANONICAL_GETATTR_STATIC.__code__ is not _CANONICAL_GETATTR_STATIC_CODE
        or _CANONICAL_GETATTR_STATIC.__globals__
        is not _CANONICAL_GETATTR_STATIC_GLOBALS
        or any(
            _CANONICAL_GETATTR_STATIC_GLOBALS.get(name) is not target
            or getattr(target, "__code__", None) is not code
            for name, target, code in _CANONICAL_GETATTR_STATIC_GLOBAL_ITEMS
        )
        or module_globals.get("math") is not _CANONICAL_MATH
        or _CANONICAL_MATH.isfinite is not _CANONICAL_MATH_ISFINITE
        or Request is not _CANONICAL_HTTP_REQUEST
        or urlopen is not _CANONICAL_URLOPEN
        or strict_json_loads is not _CANONICAL_STRICT_JSON_LOADS
        or CompleteGameBoardRequest is not _CANONICAL_REQUEST_CLASS
        or CompleteGameBoardSnapshot is not _CANONICAL_SNAPSHOT_CLASS
        or _parse_sse_event is not _CANONICAL_PARSE_SSE_EVENT
        or _read_production_initial_state
        is not _CANONICAL_READ_PRODUCTION_INITIAL_STATE
        or _default_clock is not _CANONICAL_DEFAULT_CLOCK
        or _canonical_json is not _CANONICAL_CANONICAL_JSON
        or _remember is not _CANONICAL_REMEMBER
        or assert_complete_game_board_authoritative
        is not _CANONICAL_ASSERT_COMPLETE_GAME_BOARD_AUTHORITATIVE
        or _TEST_ACQUISITION_ORIGIN is not _CANONICAL_TEST_ACQUISITION_ORIGIN
        or _TEST_ACQUISITION_CAPABILITY
        is not _CANONICAL_TEST_ACQUISITION_CAPABILITY
        or _surface_code is not _CANONICAL_SURFACE_CODE
        or _CANONICAL_SURFACE_CODE.__code__ is not _CANONICAL_SURFACE_CODE_CODE
    ):
        raise ProviderObservationIntegrityError(
            "provider production acquisition origin is rebound"
        )
    if (
        _CANONICAL_PARSE_SSE_EVENT.__code__
        is not _CANONICAL_PARSE_SSE_EVENT_CODE
        or _CANONICAL_READ_PRODUCTION_INITIAL_STATE.__code__
        is not _CANONICAL_READ_PRODUCTION_INITIAL_STATE_CODE
        or _CANONICAL_DEFAULT_CLOCK.__code__ is not _CANONICAL_DEFAULT_CLOCK_CODE
        or _CANONICAL_REQUEST_SSE_URL.__code__
        is not _CANONICAL_REQUEST_SSE_URL_CODE
        or _CANONICAL_CANONICAL_JSON.__code__
        is not _CANONICAL_CANONICAL_JSON_CODE
        or _CANONICAL_REMEMBER.__code__ is not _CANONICAL_REMEMBER_CODE
        or _CANONICAL_ASSERT_COMPLETE_GAME_BOARD_AUTHORITATIVE.__code__
        is not _CANONICAL_ASSERT_COMPLETE_GAME_BOARD_AUTHORITATIVE_CODE
        or CompleteGameBoardRequest.sse_url is not _CANONICAL_REQUEST_SSE_URL
        or getattr(_CANONICAL_URLOPEN, "__code__", None)
        is not _CANONICAL_URLOPEN_CODE
        or getattr(_CANONICAL_STRICT_JSON_LOADS, "__code__", None)
        is not _CANONICAL_STRICT_JSON_LOADS_CODE
        or _CANONICAL_GETATTR_STATIC(Request, "__init__")
        is not _CANONICAL_HTTP_REQUEST_INIT
        or getattr(_CANONICAL_HTTP_REQUEST_INIT, "__code__", None)
        is not _CANONICAL_HTTP_REQUEST_INIT_CODE
        or any(
            _CANONICAL_GETATTR_STATIC(_CANONICAL_REQUEST_CLASS, name)
            is not descriptor
            or _CANONICAL_SURFACE_CODE(descriptor) is not code
            for name, descriptor, code in _CANONICAL_REQUEST_ORIGIN_SURFACES
        )
        or any(
            _CANONICAL_GETATTR_STATIC(_CANONICAL_SNAPSHOT_CLASS, name)
            is not descriptor
            or _CANONICAL_SURFACE_CODE(descriptor) is not code
            for name, descriptor, code in _CANONICAL_SNAPSHOT_ORIGIN_SURFACES
        )
    ):
        raise ProviderObservationIntegrityError(
            "provider production acquisition origin code changed"
        )


@contextmanager
def _test_acquisition_origin(*, _capability: object):
    """Enable live test transport/time only under the private test capability."""

    if _capability is not _CANONICAL_TEST_ACQUISITION_CAPABILITY:
        raise TypeError("provider test acquisition origin requires private capability")
    token = _CANONICAL_TEST_ACQUISITION_ORIGIN.set(
        _CANONICAL_TEST_ACQUISITION_CAPABILITY
    )
    try:
        yield
    finally:
        _CANONICAL_TEST_ACQUISITION_ORIGIN.reset(token)


def capture_parlay_complete_game_board(
    *,
    api_key: str,
    request: CompleteGameBoardRequest,
    timeout_seconds: float = 10.0,
) -> CompleteGameBoardSnapshot:
    """Acquire and authorize one documented complete replacement baseline."""

    if not isinstance(api_key, str) or not api_key or api_key != api_key.strip():
        raise ValueError("api_key must be non-empty trimmed text")
    if any(character.isspace() for character in api_key):
        raise ValueError("api_key must not contain whitespace")
    if type(request) is not _CANONICAL_REQUEST_CLASS:
        raise TypeError("request must be exact CompleteGameBoardRequest")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise ValueError("timeout_seconds must be a positive finite number")
    timeout = float(timeout_seconds)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout_seconds must be a positive finite number")

    test_origin = (
        _CANONICAL_TEST_ACQUISITION_ORIGIN.get()
        is _CANONICAL_TEST_ACQUISITION_CAPABILITY
    )
    if test_origin:
        frame = _CANONICAL_READ_PRODUCTION_INITIAL_STATE(
            request,
            api_key=api_key,
            timeout_seconds=timeout,
            _request_factory=Request,
            _open_url=urlopen,
            _parse_event=_parse_sse_event,
            _sse_url=CompleteGameBoardRequest.sse_url,
            _loads=strict_json_loads,
            _max_sse_bytes=_MAX_SSE_BYTES,
        )
        captured_at = _default_clock()
    else:
        _require_production_capture_origin_integrity()
        frame = _CANONICAL_READ_PRODUCTION_INITIAL_STATE(
            request,
            api_key=api_key,
            timeout_seconds=timeout,
        )
        captured_at = _CANONICAL_DEFAULT_CLOCK()
        _require_production_capture_origin_integrity()

    snapshot = _CANONICAL_SNAPSHOT_CLASS(
        request=request,
        captured_at=captured_at,
        frame_json=_CANONICAL_CANONICAL_JSON(dict(frame)),
    )
    result = _CANONICAL_REMEMBER(snapshot)
    if not test_origin:
        _require_production_capture_origin_integrity()
    return result


def _seal_provider_observation_capture_dispatch() -> None:
    module_globals = _CANONICAL_MODULE_GLOBALS
    expected_error = ProviderObservationIntegrityError
    expected_any = any
    expected_math = _CANONICAL_MATH
    expected_math_isfinite = _CANONICAL_MATH_ISFINITE
    expected_capture = capture_parlay_complete_game_board
    expected_capture_code = expected_capture.__code__
    expected_guard = _require_production_capture_origin_integrity
    expected_guard_code = expected_guard.__code__
    expected_witnesses = {
        "_CANONICAL_MODULE_GLOBALS": _CANONICAL_MODULE_GLOBALS,
        "ProviderObservationIntegrityError": expected_error,
        "_CANONICAL_INSPECT": _CANONICAL_INSPECT,
        "_CANONICAL_MATH": _CANONICAL_MATH,
        "_CANONICAL_MATH_ISFINITE": _CANONICAL_MATH_ISFINITE,
        "_CANONICAL_GETATTR_STATIC": _CANONICAL_GETATTR_STATIC,
        "_CANONICAL_GETATTR_STATIC_CODE": _CANONICAL_GETATTR_STATIC_CODE,
        "_CANONICAL_GETATTR_STATIC_GLOBALS": _CANONICAL_GETATTR_STATIC_GLOBALS,
        "_CANONICAL_GETATTR_STATIC_GLOBAL_ITEMS": _CANONICAL_GETATTR_STATIC_GLOBAL_ITEMS,
        "_CANONICAL_HTTP_REQUEST": _CANONICAL_HTTP_REQUEST,
        "_CANONICAL_HTTP_REQUEST_INIT": _CANONICAL_HTTP_REQUEST_INIT,
        "_CANONICAL_HTTP_REQUEST_INIT_CODE": _CANONICAL_HTTP_REQUEST_INIT_CODE,
        "_CANONICAL_URLOPEN": _CANONICAL_URLOPEN,
        "_CANONICAL_URLOPEN_CODE": _CANONICAL_URLOPEN_CODE,
        "_CANONICAL_STRICT_JSON_LOADS": _CANONICAL_STRICT_JSON_LOADS,
        "_CANONICAL_STRICT_JSON_LOADS_CODE": _CANONICAL_STRICT_JSON_LOADS_CODE,
        "_CANONICAL_PARSE_SSE_EVENT": _CANONICAL_PARSE_SSE_EVENT,
        "_CANONICAL_PARSE_SSE_EVENT_CODE": _CANONICAL_PARSE_SSE_EVENT_CODE,
        "_CANONICAL_READ_PRODUCTION_INITIAL_STATE": _CANONICAL_READ_PRODUCTION_INITIAL_STATE,
        "_CANONICAL_READ_PRODUCTION_INITIAL_STATE_CODE": _CANONICAL_READ_PRODUCTION_INITIAL_STATE_CODE,
        "_CANONICAL_DEFAULT_CLOCK": _CANONICAL_DEFAULT_CLOCK,
        "_CANONICAL_DEFAULT_CLOCK_CODE": _CANONICAL_DEFAULT_CLOCK_CODE,
        "_CANONICAL_SNAPSHOT_CLASS": _CANONICAL_SNAPSHOT_CLASS,
        "_CANONICAL_REQUEST_CLASS": _CANONICAL_REQUEST_CLASS,
        "_CANONICAL_REQUEST_SSE_URL": _CANONICAL_REQUEST_SSE_URL,
        "_CANONICAL_REQUEST_SSE_URL_CODE": _CANONICAL_REQUEST_SSE_URL_CODE,
        "_CANONICAL_CANONICAL_JSON": _CANONICAL_CANONICAL_JSON,
        "_CANONICAL_CANONICAL_JSON_CODE": _CANONICAL_CANONICAL_JSON_CODE,
        "_CANONICAL_REMEMBER": _CANONICAL_REMEMBER,
        "_CANONICAL_REMEMBER_CODE": _CANONICAL_REMEMBER_CODE,
        "_CANONICAL_ASSERT_COMPLETE_GAME_BOARD_AUTHORITATIVE": _CANONICAL_ASSERT_COMPLETE_GAME_BOARD_AUTHORITATIVE,
        "_CANONICAL_ASSERT_COMPLETE_GAME_BOARD_AUTHORITATIVE_CODE": _CANONICAL_ASSERT_COMPLETE_GAME_BOARD_AUTHORITATIVE_CODE,
        "_CANONICAL_TEST_ACQUISITION_ORIGIN": _CANONICAL_TEST_ACQUISITION_ORIGIN,
        "_CANONICAL_TEST_ACQUISITION_CAPABILITY": _CANONICAL_TEST_ACQUISITION_CAPABILITY,
        "_CANONICAL_SURFACE_CODE": _CANONICAL_SURFACE_CODE,
        "_CANONICAL_SURFACE_CODE_CODE": _CANONICAL_SURFACE_CODE_CODE,
        "_CANONICAL_REQUEST_ORIGIN_SURFACES": _CANONICAL_REQUEST_ORIGIN_SURFACES,
        "_CANONICAL_SNAPSHOT_ORIGIN_SURFACES": _CANONICAL_SNAPSHOT_ORIGIN_SURFACES,
    }
    expected_witness_items = tuple(expected_witnesses.items())

    def require_sealed_surface() -> None:
        if expected_any(
            name in module_globals
            for name in (
                "any",
                "getattr",
                "isinstance",
                "type",
                "str",
                "bool",
                "int",
                "float",
                "dict",
                "ValueError",
                "TypeError",
            )
        ):
            raise expected_error(
                "provider production acquisition builtin dispatch shadowed"
            )
        if (
            module_globals.get("math") is not expected_math
            or expected_math.isfinite is not expected_math_isfinite
        ):
            raise expected_error(
                "provider production acquisition numeric validation changed"
            )
        if (
            module_globals.get("_require_production_capture_origin_integrity")
            is not expected_guard
            or expected_guard.__code__ is not expected_guard_code
            or expected_capture.__code__ is not expected_capture_code
        ):
            raise expected_error(
                "provider production acquisition guard changed"
            )
        if expected_any(
            module_globals.get(name) is not expected
            for name, expected in expected_witness_items
        ):
            raise expected_error(
                "provider production acquisition witness changed"
            )

    def require_public_capture_surface() -> None:
        if (
            module_globals.get("capture_parlay_complete_game_board")
            is not sealed_capture_parlay_complete_game_board
        ):
            raise expected_error(
                "provider production acquisition public surface changed"
            )

    def sealed_capture_parlay_complete_game_board(*args, **kwargs):
        require_public_capture_surface()
        require_sealed_surface()
        result = expected_capture(*args, **kwargs)
        require_sealed_surface()
        require_public_capture_surface()
        return result

    sealed_capture_parlay_complete_game_board.__name__ = expected_capture.__name__
    sealed_capture_parlay_complete_game_board.__qualname__ = expected_capture.__qualname__
    sealed_capture_parlay_complete_game_board.__doc__ = expected_capture.__doc__
    if hasattr(sealed_capture_parlay_complete_game_board, "__wrapped__"):
        raise RuntimeError(
            "provider acquisition seal must not expose unsealed delegate"
        )

    module_globals["capture_parlay_complete_game_board"] = (
        sealed_capture_parlay_complete_game_board
    )


_seal_provider_observation_capture_dispatch()


class CompleteGameBoardEvidenceStore:
    """Immutable provider evidence anchored by acquisition receipt and machine authority."""

    DIRECTORY = "provider-complete-game-board"
    AUTHORITY_DOMAIN = "provider-complete-game-board-capture"

    def __init__(
        self,
        workspace: str | Path,
        *,
        authority_root: str | Path | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve(strict=False)
        self.root = self.workspace / self.DIRECTORY
        self.authority_root = authority_root

    def _path(self, evidence_sha256: str) -> Path:
        return self.root / f"{_sha(evidence_sha256, 'evidence_sha256')}.json"

    def _authority(self, evidence_sha256: str) -> MonotonicWorkspaceAuthority:
        return MonotonicWorkspaceAuthority(
            workspace=self.workspace,
            domain=self.AUTHORITY_DOMAIN,
            key=_sha(evidence_sha256, "evidence_sha256"),
            authority_root=self.authority_root,
        )

    def _workspace_sha256(self) -> str:
        return hashlib.sha256(str(self.workspace).encode("utf-8")).hexdigest()

    def _receipt_root(self) -> Path:
        try:
            root = resolve_monotonic_authority_root(self.workspace, self.authority_root)
        except MonotonicWorkspaceAuthorityError as exc:
            raise ProviderObservationIntegrityError(
                "provider acquisition receipt trust root is unsafe"
            ) from exc
        return root / _RECEIPT_ROOT_NAME / self._workspace_sha256()

    def _receipt_path(self, evidence_sha256: str) -> Path:
        digest = _sha(evidence_sha256, "evidence_sha256")
        return self._receipt_root() / "receipts" / f"{digest}.json"

    def _key_path(self) -> Path:
        return self._receipt_root() / "receipt.key"

    def _read_receipt_key(self, *, create: bool) -> bytes:
        path = self._key_path()
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
            key = secrets.token_bytes(_RECEIPT_KEY_BYTES)
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            except OSError as exc:
                raise ProviderObservationIntegrityError(
                    "cannot create provider acquisition receipt key"
                ) from exc
            else:
                try:
                    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                        handle.write(key.hex())
                        handle.flush()
                        os.fsync(handle.fileno())
                except OSError as exc:
                    raise ProviderObservationIntegrityError(
                        "cannot persist provider acquisition receipt key"
                    ) from exc
        try:
            raw = path.read_text(encoding="ascii").strip()
        except OSError as exc:
            raise ProviderObservationIntegrityError(
                "provider evidence is not proven by production-owned acquisition receipt"
            ) from exc
        if len(raw) != _RECEIPT_KEY_BYTES * 2 or any(character not in _HEX for character in raw):
            raise ProviderObservationIntegrityError("provider acquisition receipt key is malformed")
        key = bytes.fromhex(raw)
        if len(key) != _RECEIPT_KEY_BYTES:
            raise ProviderObservationIntegrityError("provider acquisition receipt key is malformed")
        return key

    @staticmethod
    def _state_sha256(snapshot: CompleteGameBoardSnapshot) -> str:
        return _digest(snapshot.to_payload())

    @staticmethod
    def _semantic_binding_sha256(snapshot: CompleteGameBoardSnapshot) -> str:
        return _digest(
            {
                "kind": "provider-complete-game-board-capture-v1",
                "source_id": snapshot.request.source_id,
                "request": snapshot.request.to_payload(),
                "frame_sha256": snapshot.frame_sha256,
                "evidence_sha256": snapshot.evidence_sha256,
            }
        )

    @staticmethod
    def _read_path(path: Path) -> CompleteGameBoardSnapshot:
        try:
            raw = strict_json_loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise ProviderObservationIntegrityError(
                "cannot read immutable complete provider evidence"
            ) from exc
        if not isinstance(raw, dict):
            raise ProviderObservationIntegrityError(
                "complete provider evidence must be a JSON object"
            )
        return CompleteGameBoardSnapshot.from_payload(raw)

    def _unsigned_receipt(self, snapshot: CompleteGameBoardSnapshot) -> dict[str, object]:
        return {
            "schema": _RECEIPT_SCHEMA,
            "schema_version": _RECEIPT_SCHEMA_VERSION,
            "workspace_sha256": self._workspace_sha256(),
            "evidence_sha256": snapshot.evidence_sha256,
            "state_sha256": self._state_sha256(snapshot),
            "semantic_binding_sha256": self._semantic_binding_sha256(snapshot),
        }

    @staticmethod
    def _receipt_hmac(key: bytes, payload: Mapping[str, object]) -> str:
        return hmac.new(
            key,
            _canonical_json(dict(payload)).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _write_receipt(self, snapshot: CompleteGameBoardSnapshot) -> None:
        assert_complete_game_board_authoritative(snapshot)
        unsigned = self._unsigned_receipt(snapshot)
        key = self._read_receipt_key(create=True)
        receipt = dict(unsigned)
        receipt["hmac_sha256"] = self._receipt_hmac(key, unsigned)
        path = self._receipt_path(snapshot.evidence_sha256)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, receipt)

    def _verify_receipt(self, snapshot: CompleteGameBoardSnapshot) -> None:
        path = self._receipt_path(snapshot.evidence_sha256)
        try:
            raw = strict_json_loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise ProviderObservationIntegrityError(
                "provider evidence is not proven by production-owned acquisition receipt"
            ) from exc
        if not isinstance(raw, dict) or set(raw) != _RECEIPT_KEYS:
            raise ProviderObservationIntegrityError("provider acquisition receipt is malformed")
        expected_unsigned = self._unsigned_receipt(snapshot)
        for name, expected in expected_unsigned.items():
            if raw.get(name) != expected:
                raise ProviderObservationIntegrityError(
                    "provider acquisition receipt does not bind exact persisted evidence"
                )
        supplied_hmac = _sha(raw.get("hmac_sha256"), "hmac_sha256")
        key = self._read_receipt_key(create=False)
        expected_hmac = self._receipt_hmac(key, expected_unsigned)
        if not hmac.compare_digest(supplied_hmac, expected_hmac):
            raise ProviderObservationIntegrityError(
                "provider acquisition receipt authentication failed"
            )

    def _recover_provenance(
        self,
        authority: MonotonicWorkspaceAuthority,
        snapshot: CompleteGameBoardSnapshot | None,
    ) -> None:
        observed = None if snapshot is None else self._state_sha256(snapshot)
        try:
            history = authority.read_history()
            pending = (
                history[-1]
                if history and history[-1].phase is AuthorityPhase.PREPARE
                else None
            )
            if snapshot is None:
                authority.recover(observed_state_sha256=None)
                return
            binding = self._semantic_binding_sha256(snapshot)
            if pending is not None and pending.intended_state_sha256 == observed:
                if pending.semantic_binding_sha256 != binding:
                    raise ProviderObservationIntegrityError(
                        "prepared provider-capture semantic binding mismatches published evidence"
                    )
                authority.recover(
                    observed_state_sha256=observed,
                    tx_id=pending.tx_id,
                    semantic_binding_sha256=binding,
                )
            else:
                authority.recover(observed_state_sha256=observed)
        except MonotonicWorkspaceAuthorityError as exc:
            raise ProviderObservationIntegrityError(
                "provider evidence is not proven by independent machine-state acquisition authority"
            ) from exc

    @staticmethod
    def _next_tx_id(
        authority: MonotonicWorkspaceAuthority,
        intended_state_sha256: str,
    ) -> str:
        attempt = len(authority.read_history()) + 1
        return f"provider-complete-board:{attempt}:{intended_state_sha256[:32]}"

    def save(self, snapshot: CompleteGameBoardSnapshot) -> Path:
        """Persist only a production capture and bind both independent trust roots."""

        _CANONICAL_ASSERT_COMPLETE_GAME_BOARD_AUTHORITATIVE(snapshot)
        path = self._path(snapshot.evidence_sha256)
        authority = self._authority(snapshot.evidence_sha256)
        intended = self._state_sha256(snapshot)
        binding = self._semantic_binding_sha256(snapshot)

        with WorkspaceEconomicLock(self.workspace):
            existing = self._read_path(path) if path.exists() else None
            history = authority.read_history()
            if history:
                if existing is not None:
                    self._verify_receipt(existing)
                self._recover_provenance(authority, existing)
                history = authority.read_history()
                if existing is not None:
                    if existing.to_payload() != snapshot.to_payload():
                        raise ProviderObservationIntegrityError(
                            "content-addressed provider evidence conflicts with proven bytes"
                        )
                    return path
            elif existing is not None and existing.to_payload() != snapshot.to_payload():
                raise ProviderObservationIntegrityError(
                    "unproven local provider evidence conflicts with production capture"
                )

            observed = (
                None
                if not history
                else history[-1].intended_state_sha256
                if history[-1].phase is AuthorityPhase.COMMIT
                else None
            )
            tx_id = self._next_tx_id(authority, intended)
            try:
                authority.prepare(
                    tx_id=tx_id,
                    observed_state_sha256=observed,
                    intended_state_sha256=intended,
                    semantic_binding_sha256=binding,
                )
                self._write_receipt(snapshot)
                atomic_write_json(path, snapshot.to_payload())
                published = self._read_path(path)
                if self._state_sha256(published) != intended:
                    raise ProviderObservationIntegrityError(
                        "published provider evidence does not match intended capture digest"
                    )
                self._verify_receipt(published)
                authority.commit(
                    tx_id=tx_id,
                    observed_state_sha256=intended,
                    semantic_binding_sha256=binding,
                )
            except MonotonicWorkspaceAuthorityError as exc:
                raise ProviderObservationIntegrityError(
                    "provider acquisition provenance publication failed closed"
                ) from exc
        return path

    def load(self, evidence_sha256: str) -> CompleteGameBoardSnapshot:
        """Load only evidence proven by receipt plus independent monotonic authority."""

        path = self._path(evidence_sha256)
        with WorkspaceEconomicLock(self.workspace):
            snapshot = self._read_path(path)
            if snapshot.evidence_sha256 != _sha(evidence_sha256, "evidence_sha256"):
                raise ProviderObservationIntegrityError(
                    "content-addressed provider evidence path does not match payload"
                )
            self._verify_receipt(snapshot)
            authority = self._authority(snapshot.evidence_sha256)
            self._recover_provenance(authority, snapshot)
        return _CANONICAL_REMEMBER(snapshot)



_CANONICAL_EVIDENCE_STORE_SAVE_IMPLEMENTATION_CODE = (
    CompleteGameBoardEvidenceStore.save.__code__
)
_CANONICAL_EVIDENCE_STORE_LOAD_IMPLEMENTATION_CODE = (
    CompleteGameBoardEvidenceStore.load.__code__
)


def _seal_provider_evidence_store_dispatch() -> None:
    """Seal the durable provider-evidence persistence graph against runtime retargeting."""

    module_globals = globals()
    store_type = CompleteGameBoardEvidenceStore
    expected_error = ProviderObservationIntegrityError
    expected_type = type
    expected_type_error = TypeError
    expected_getattr = getattr
    expected_any = any
    expected_unshadowed_builtins = (
        "any",
        "len",
        "str",
        "dict",
        "set",
        "isinstance",
    )
    expected_store_surface_reader = _CANONICAL_GETATTR_STATIC
    expected_store_surface_reader_code = expected_getattr(
        expected_store_surface_reader,
        "__code__",
        None,
    )
    expected_store_surface_reader_globals = expected_getattr(
        expected_store_surface_reader,
        "__globals__",
        None,
    )
    expected_store_surface_reader_global_items = tuple(
        (
            name,
            expected_store_surface_reader_globals[name],
            expected_getattr(
                expected_store_surface_reader_globals[name],
                "__code__",
                None,
            ),
        )
        for name in expected_store_surface_reader_code.co_names
        if name in expected_store_surface_reader_globals
    )
    expected_inspect = inspect
    expected_getattr_static = inspect.getattr_static
    expected_save = store_type.save
    expected_save_code = _CANONICAL_EVIDENCE_STORE_SAVE_IMPLEMENTATION_CODE
    expected_load = store_type.load
    expected_load_code = _CANONICAL_EVIDENCE_STORE_LOAD_IMPLEMENTATION_CODE
    expected_assert = _CANONICAL_ASSERT_COMPLETE_GAME_BOARD_AUTHORITATIVE
    expected_assert_code = _CANONICAL_ASSERT_COMPLETE_GAME_BOARD_AUTHORITATIVE_CODE
    expected_remember = _CANONICAL_REMEMBER
    expected_remember_code = _CANONICAL_REMEMBER_CODE

    def _surface_witness(owner, name):
        surface = expected_getattr_static(owner, name)
        function = expected_getattr(surface, "__func__", surface)
        return (
            name,
            surface,
            function,
            expected_getattr(function, "__code__", None),
        )

    expected_store_internal = tuple(
        _surface_witness(store_type, name)
        for name in (
            "_path",
            "_authority",
            "_workspace_sha256",
            "_receipt_root",
            "_receipt_path",
            "_key_path",
            "_read_receipt_key",
            "_state_sha256",
            "_semantic_binding_sha256",
            "_read_path",
            "_unsigned_receipt",
            "_receipt_hmac",
            "_write_receipt",
            "_verify_receipt",
            "_recover_provenance",
            "_next_tx_id",
        )
    )
    expected_authority_methods = tuple(
        _surface_witness(MonotonicWorkspaceAuthority, name)
        for name in (
            "__init__",
            "prepare",
            "commit",
            "abort",
            "recover",
            "read_history",
            "_validate_authority_root_selection",
            "_ensure_authority_root_bound",
            "_validate_authority_root_activation",
            "_ensure_authority_root_activated",
            "_validate_workspace_binding",
            "_ensure_workspace_bound",
            "_load_bound_history",
            "_latest_record_for_tx",
            "_require_same_transaction",
            "_validate_prepare_retry",
            "_new_record",
            "_new_terminal_record",
            "_payload",
            "_namespace_payload",
            "_ensure_namespace_marker",
            "_validate_namespace_marker",
            "_append_record",
            "_load_history",
            "_decode_record",
        )
    )
    expected_lock_methods = tuple(
        _surface_witness(WorkspaceEconomicLock, name)
        for name in (
            "__init__",
            "acquire",
            "release",
            "__enter__",
            "__exit__",
            "_open_lock_handle",
            "_open_new_lock_handle",
            "_validate_existing_lock_path",
            "_validate_open_handle_identity",
            "_require_regular_file",
            "_require_single_link",
            "_lock_handle",
            "_unlock_handle",
        )
    )
    expected_snapshot_methods = tuple(
        _surface_witness(CompleteGameBoardSnapshot, name)
        for name in ("to_payload", "from_payload")
    )
    expected_path_type = type(Path("."))
    expected_path_factory_methods = tuple(
        _surface_witness(Path, name)
        for name in ("__new__",)
    )
    expected_path_methods = tuple(
        _surface_witness(expected_path_type, name)
        for name in ("__truediv__", "__str__", "exists", "read_text", "mkdir", "parent")
    )

    expected_runtime_globals = {
        "Path": Path,
        "MonotonicWorkspaceAuthority": MonotonicWorkspaceAuthority,
        "MonotonicWorkspaceAuthorityError": MonotonicWorkspaceAuthorityError,
        "resolve_monotonic_authority_root": resolve_monotonic_authority_root,
        "WorkspaceEconomicLock": WorkspaceEconomicLock,
        "strict_json_loads": strict_json_loads,
        "atomic_write_json": atomic_write_json,
        "AuthorityPhase": AuthorityPhase,
        "CompleteGameBoardSnapshot": CompleteGameBoardSnapshot,
        "ProviderObservationIntegrityError": expected_error,
        "_CANONICAL_GETATTR_STATIC": expected_store_surface_reader,
        "_CANONICAL_EVIDENCE_STORE_SAVE_IMPLEMENTATION_CODE": expected_save_code,
        "_CANONICAL_EVIDENCE_STORE_LOAD_IMPLEMENTATION_CODE": expected_load_code,
        "hashlib": hashlib,
        "hmac": hmac,
        "os": os,
        "secrets": secrets,
        "_canonical_json": _canonical_json,
        "_digest": _digest,
        "_sha": _sha,
    }
    expected_runtime_global_items = tuple(expected_runtime_globals.items())
    expected_runtime_callables = tuple(
        (
            name,
            target,
            expected_getattr(
                expected_getattr(target, "__func__", target),
                "__code__",
                None,
            ),
        )
        for name, target in (
            ("resolve_monotonic_authority_root", resolve_monotonic_authority_root),
            ("strict_json_loads", strict_json_loads),
            ("atomic_write_json", atomic_write_json),
            ("_canonical_json", _canonical_json),
            ("_digest", _digest),
            ("_sha", _sha),
        )
    )
    expected_hashlib_sha256 = hashlib.sha256
    expected_hmac_new = hmac.new
    expected_hmac_compare_digest = hmac.compare_digest
    expected_secrets_token_bytes = secrets.token_bytes
    expected_os_open = os.open
    expected_os_fdopen = os.fdopen
    expected_os_fsync = os.fsync

    expected_dependency_functions = (
        tuple(
            ("store." + name, function, code)
            for name, _surface, function, code in expected_store_internal
            if code is not None
        )
        + tuple(
            ("authority." + name, function, code)
            for name, _surface, function, code in expected_authority_methods
            if code is not None
        )
        + tuple(
            ("lock." + name, function, code)
            for name, _surface, function, code in expected_lock_methods
            if code is not None
        )
        + tuple(
            ("snapshot." + name, function, code)
            for name, _surface, function, code in expected_snapshot_methods
            if code is not None
        )
        + tuple(
            (
                "runtime." + name,
                expected_getattr(target, "__func__", target),
                code,
            )
            for name, target, code in expected_runtime_callables
            if code is not None
        )
    )
    expected_dependency_global_witnesses = tuple(
        (
            name,
            function,
            function_globals,
            tuple(
                (
                    dependency_name,
                    function_globals[dependency_name],
                    expected_getattr(
                        function_globals[dependency_name],
                        "__code__",
                        None,
                    ),
                )
                for dependency_name in code.co_names
                if dependency_name in function_globals
            ),
        )
        for name, function, code in expected_dependency_functions
        for function_globals in (
            expected_getattr(function, "__globals__", None),
        )
        if function_globals is not None
    )
    expected_dependency_module_attr_witnesses = tuple(
        (
            name,
            dependency_name,
            module,
            attribute_name,
            expected_getattr(module, attribute_name),
            expected_getattr(
                expected_getattr(module, attribute_name),
                "__code__",
                None,
            ),
        )
        for name, function, _function_globals, global_items
        in expected_dependency_global_witnesses
        for dependency_name, module, _dependency_code in global_items
        if expected_type(module) is ModuleType
        for attribute_name in function.__code__.co_names
        if hasattr(module, attribute_name)
    )
    expected_dependency_builtin_witnesses = tuple(
        (
            name,
            function_globals,
            builtins,
            builtin_name,
            expected_getattr(builtins, builtin_name),
        )
        for name, function, function_globals, _global_items
        in expected_dependency_global_witnesses
        for builtin_name in function.__code__.co_names
        if (
            builtin_name not in function_globals
            and hasattr(builtins, builtin_name)
        )
    )

    def _require_surface_witnesses(owner, witnesses) -> bool:
        for name, surface, function, code in witnesses:
            current = expected_getattr_static(owner, name)
            if current is not surface:
                return False
            current_function = expected_getattr(current, "__func__", current)
            if current_function is not function:
                return False
            if expected_getattr(current_function, "__code__", None) is not code:
                return False
        return True

    def require_store_authority() -> None:
        if expected_any(
            name in module_globals for name in expected_unshadowed_builtins
        ):
            raise expected_error(
                "provider evidence store builtin dispatch shadowed"
            )
        if (
            module_globals.get("inspect") is not expected_inspect
            or expected_inspect.getattr_static is not expected_getattr_static
            or module_globals.get("_CANONICAL_ASSERT_COMPLETE_GAME_BOARD_AUTHORITATIVE")
            is not expected_assert
            or module_globals.get(
                "_CANONICAL_ASSERT_COMPLETE_GAME_BOARD_AUTHORITATIVE_CODE"
            )
            is not expected_assert_code
            or expected_assert.__code__ is not expected_assert_code
            or module_globals.get("_CANONICAL_REMEMBER") is not expected_remember
            or module_globals.get("_CANONICAL_REMEMBER_CODE")
            is not expected_remember_code
            or expected_remember.__code__ is not expected_remember_code
            or expected_getattr(expected_store_surface_reader, "__code__", None)
            is not expected_store_surface_reader_code
            or expected_getattr(expected_store_surface_reader, "__globals__", None)
            is not expected_store_surface_reader_globals
            or expected_any(
                expected_store_surface_reader_globals.get(name) is not target
                or expected_getattr(target, "__code__", None) is not code
                for name, target, code in expected_store_surface_reader_global_items
            )
            or expected_save.__code__ is not expected_save_code
            or expected_load.__code__ is not expected_load_code
            or expected_any(
                module_globals.get(name) is not expected
                for name, expected in expected_runtime_global_items
            )
            or expected_hashlib.sha256 is not expected_hashlib_sha256
            or expected_hmac.new is not expected_hmac_new
            or expected_hmac.compare_digest is not expected_hmac_compare_digest
            or expected_secrets.token_bytes is not expected_secrets_token_bytes
            or expected_os.open is not expected_os_open
            or expected_os.fdopen is not expected_os_fdopen
            or expected_os.fsync is not expected_os_fsync
        ):
            raise expected_error(
                "provider evidence store authority witness changed"
            )
        for name, target, code in expected_runtime_callables:
            if module_globals.get(name) is not target:
                raise expected_error(
                    "provider evidence store authority witness changed"
                )
            function = expected_getattr(target, "__func__", target)
            if expected_getattr(function, "__code__", None) is not code:
                raise expected_error(
                    "provider evidence store authority witness changed"
                )
        if expected_any(
            (
                expected_getattr(function, "__globals__", None)
                is not function_globals
                or expected_any(
                    function_globals.get(dependency_name) is not expected
                    or expected_getattr(expected, "__code__", None) is not code
                    for dependency_name, expected, code in global_items
                )
            )
            for (
                _name,
                function,
                function_globals,
                global_items,
            ) in expected_dependency_global_witnesses
        ):
            raise expected_error(
                "provider evidence store dependency globals changed"
            )
        if expected_any(
            (
                expected_getattr(module, attribute_name, None) is not expected
                or expected_getattr(expected, "__code__", None) is not code
            )
            for (
                _name,
                _dependency_name,
                module,
                attribute_name,
                expected,
                code,
            ) in expected_dependency_module_attr_witnesses
        ):
            raise expected_error(
                "provider evidence store dependency module dispatch changed"
            )
        if expected_any(
            (
                builtin_name in function_globals
                or expected_getattr(builtin_module, builtin_name, None)
                is not expected
            )
            for (
                _name,
                function_globals,
                builtin_module,
                builtin_name,
                expected,
            ) in expected_dependency_builtin_witnesses
        ):
            raise expected_error(
                "provider evidence store dependency builtin dispatch changed"
            )
        if not _require_surface_witnesses(store_type, expected_store_internal):
            raise expected_error(
                "provider evidence store internal dispatch changed"
            )
        if not _require_surface_witnesses(
            MonotonicWorkspaceAuthority,
            expected_authority_methods,
        ):
            raise expected_error(
                "provider evidence monotonic authority dispatch changed"
            )
        if not _require_surface_witnesses(
            WorkspaceEconomicLock,
            expected_lock_methods,
        ):
            raise expected_error(
                "provider evidence workspace lock dispatch changed"
            )
        if not _require_surface_witnesses(
            CompleteGameBoardSnapshot,
            expected_snapshot_methods,
        ):
            raise expected_error(
                "provider evidence snapshot dispatch changed"
            )
        if (
            not _require_surface_witnesses(Path, expected_path_factory_methods)
            or not _require_surface_witnesses(
                expected_path_type,
                expected_path_methods,
            )
        ):
            raise expected_error(
                "provider evidence filesystem path dispatch changed"
            )

    def require_store_public_surfaces() -> None:
        if expected_store_surface_reader(store_type, "save") is not sealed_save:
            raise expected_error(
                "provider evidence store save surface changed"
            )
        if expected_store_surface_reader(store_type, "load") is not sealed_load:
            raise expected_error(
                "provider evidence store load surface changed"
            )

    def sealed_save(self, snapshot):
        if expected_type(self) is not store_type:
            raise expected_type_error(
                "provider evidence save requires exact CompleteGameBoardEvidenceStore"
            )
        require_store_public_surfaces()
        require_store_authority()
        result = expected_save(self, snapshot)
        require_store_authority()
        require_store_public_surfaces()
        return result

    def sealed_load(self, evidence_sha256):
        if expected_type(self) is not store_type:
            raise expected_type_error(
                "provider evidence load requires exact CompleteGameBoardEvidenceStore"
            )
        require_store_public_surfaces()
        require_store_authority()
        result = expected_load(self, evidence_sha256)
        require_store_authority()
        require_store_public_surfaces()
        return result

    if hasattr(sealed_save, "__wrapped__") or hasattr(sealed_load, "__wrapped__"):
        raise RuntimeError("provider evidence store seal must not expose unsealed delegates")
    store_type.save = sealed_save
    store_type.load = sealed_load


_seal_provider_evidence_store_dispatch()
