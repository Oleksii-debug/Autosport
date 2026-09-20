from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .integrity import atomic_write_json
from .json_integrity import strict_json_loads


SCHEMA = "autosport.provider_complete_game_board"
SCHEMA_VERSION = 1
COMPLETE_SNAPSHOT_SCOPE = "current_game_board"
COMPLETE_RESUME_MODE = "replace"
GAME_LINE_MARKETS = ("h2h", "spreads", "totals")
_MAX_SSE_BYTES = 16 * 1024 * 1024
_HEX = frozenset("0123456789abcdef")


class ProviderObservationAuthorityError(RuntimeError):
    """Base error for complete-provider observation authority."""


class ProviderObservationUnsupportedError(ProviderObservationAuthorityError):
    """Provider evidence does not prove an exhaustive observation scope."""


class ProviderObservationIntegrityError(ProviderObservationAuthorityError):
    """Persisted or returned provider evidence is malformed or inconsistent."""


SseSnapshotTransport = Callable[
    [str, Mapping[str, str], float, int],
    Mapping[str, object],
]
Clock = Callable[[], str]


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ProviderObservationIntegrityError(f"{name} must be non-empty canonical text")
    value.encode("utf-8")
    return value


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


def _headerless_secret(value: object) -> str:
    secret = _text(value, "api_key")
    if any(character.isspace() for character in secret):
        raise ValueError("api_key must not contain whitespace")
    return secret


def _default_clock() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class CompleteGameBoardRequest:
    """Exact ParlayAPI stream scope eligible for a complete current game-line baseline."""

    sport_key: str
    bookmakers: tuple[str, ...]
    markets: tuple[str, ...] = GAME_LINE_MARKETS
    kind: str = "game"
    limit: int = 1000
    max_age_s: int = 600

    def __post_init__(self) -> None:
        sport_key = _text(self.sport_key, "sport_key")
        if sport_key != sport_key.lower() or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
            for character in sport_key
        ):
            raise ProviderObservationIntegrityError(
                "sport_key must be lowercase canonical provider identity"
            )
        if not isinstance(self.bookmakers, tuple) or not self.bookmakers:
            raise ProviderObservationIntegrityError("bookmakers must be a non-empty tuple")
        books = tuple(sorted(_text(book, "bookmaker") for book in self.bookmakers))
        if len(books) != len(set(books)):
            raise ProviderObservationIntegrityError("bookmakers must be unique")
        if any(book != book.lower() for book in books):
            raise ProviderObservationIntegrityError("bookmakers must be lowercase canonical identities")
        object.__setattr__(self, "bookmakers", books)

        if not isinstance(self.markets, tuple):
            raise ProviderObservationIntegrityError("markets must be a tuple")
        markets = tuple(self.markets)
        if markets != GAME_LINE_MARKETS:
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
        if (
            type(self.max_age_s) is not int
            or self.max_age_s < 1
            or self.max_age_s > 3600
        ):
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
            return cls(
                sport_key=payload["sport_key"],
                bookmakers=tuple(payload["bookmakers"]),
                markets=tuple(payload["markets"]),
                kind=payload["kind"],
                limit=payload["limit"],
                max_age_s=payload["max_age_s"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ProviderObservationAuthorityError):
                raise
            raise ProviderObservationIntegrityError(
                "complete game-board request payload is invalid"
            ) from exc

    def sse_url(self, base_url: str = "https://parlay-api.com") -> str:
        if not isinstance(base_url, str) or not base_url.startswith("https://"):
            raise ValueError("base_url must use https")
        query = urlencode(
            {
                "bookmakers": ",".join(self.bookmakers),
                "kinds": self.kind,
                "markets": ",".join(self.markets),
                "limit": str(self.limit),
                "max_age_s": str(self.max_age_s),
            }
        )
        return f"{base_url.rstrip('/')}/v1/sse/odds/{self.sport_key}?{query}"


@dataclass(frozen=True, slots=True)
class CompleteGameBoardSnapshot:
    """Immutable exact response evidence; authority is granted only by capture/store paths."""

    request: CompleteGameBoardRequest
    captured_at: str
    frame_json: str

    def __post_init__(self) -> None:
        if not isinstance(self.request, CompleteGameBoardRequest):
            raise ProviderObservationIntegrityError(
                "request must be CompleteGameBoardRequest"
            )
        object.__setattr__(self, "captured_at", _instant(self.captured_at, "captured_at"))
        if not isinstance(self.frame_json, str):
            raise ProviderObservationIntegrityError("frame_json must be text")
        try:
            frame = strict_json_loads(self.frame_json)
        except (TypeError, ValueError) as exc:
            raise ProviderObservationIntegrityError("initial_state frame is invalid JSON") from exc
        if not isinstance(frame, dict):
            raise ProviderObservationIntegrityError("initial_state frame must be an object")
        canonical = _canonical_json(frame)
        object.__setattr__(self, "frame_json", canonical)
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
        if frame.get("partial") is True:
            raise ProviderObservationUnsupportedError(
                "partial provider snapshot cannot prove complete membership"
            )
        for name in ("missing_books", "truncated_books", "snapshot_partial_reasons"):
            raw = frame.get(name)
            if raw not in (None, [], ()):
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
            bookmaker = row.get("bookmaker")
            kind = row.get("kind")
            market_key = row.get("market_key")
            event_id = row.get("event_id")
            if bookmaker not in self.request.bookmakers:
                raise ProviderObservationIntegrityError(
                    "provider snapshot row escapes requested bookmaker scope"
                )
            if kind != self.request.kind:
                raise ProviderObservationIntegrityError(
                    "provider snapshot row escapes requested kind scope"
                )
            if market_key not in self.request.markets:
                raise ProviderObservationIntegrityError(
                    "provider snapshot row escapes requested market scope"
                )
            _text(event_id, "provider event_id")
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
        except KeyError as exc:
            raise ProviderObservationIntegrityError(
                "complete game-board evidence payload is incomplete"
            ) from exc


_ISSUED: dict[int, tuple[CompleteGameBoardSnapshot, str]] = {}


def _remember(snapshot: CompleteGameBoardSnapshot) -> CompleteGameBoardSnapshot:
    _ISSUED[id(snapshot)] = (snapshot, snapshot.evidence_sha256)
    return snapshot


def assert_complete_game_board_authoritative(snapshot: CompleteGameBoardSnapshot) -> None:
    """Reject caller-constructed lookalikes that did not pass capture/store authority."""

    if not isinstance(snapshot, CompleteGameBoardSnapshot):
        raise ProviderObservationUnsupportedError(
            "complete provider authority requires CompleteGameBoardSnapshot"
        )
    issued = _ISSUED.get(id(snapshot))
    if issued is None or issued[0] is not snapshot or issued[1] != snapshot.evidence_sha256:
        raise ProviderObservationUnsupportedError(
            "snapshot was not issued by canonical provider acquisition evidence"
        )


def _parse_sse_event(event_name: str | None, data_lines: list[str]) -> Mapping[str, object] | None:
    if not data_lines:
        return None
    raw = "\n".join(data_lines)
    try:
        payload = strict_json_loads(raw)
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


def _default_sse_snapshot_transport(
    url: str,
    headers: Mapping[str, str],
    timeout_seconds: float,
    max_bytes: int,
) -> Mapping[str, object]:
    request = Request(url, headers=dict(headers), method="GET")
    consumed = 0
    event_name: str | None = None
    data_lines: list[str] = []
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310 - caller enforces HTTPS
            if int(getattr(response, "status", 0)) != 200:
                raise ProviderObservationUnsupportedError(
                    "provider SSE did not return HTTP 200"
                )
            for raw_line in response:
                consumed += len(raw_line)
                if consumed > max_bytes:
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
                    result = _parse_sse_event(event_name, data_lines)
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
            result = _parse_sse_event(event_name, data_lines)
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


def capture_parlay_complete_game_board(
    *,
    api_key: str,
    request: CompleteGameBoardRequest,
    timeout_seconds: float = 10.0,
    base_url: str = "https://parlay-api.com",
    transport: SseSnapshotTransport = _default_sse_snapshot_transport,
    clock: Clock = _default_clock,
) -> CompleteGameBoardSnapshot:
    """Acquire and authorize exactly one documented complete replacement baseline.

    No API call occurs unless this function is explicitly invoked. Credentials are sent
    only in the request header and are never persisted in evidence.
    """

    secret = _headerless_secret(api_key)
    if not isinstance(request, CompleteGameBoardRequest):
        raise TypeError("request must be CompleteGameBoardRequest")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise ValueError("timeout_seconds must be a positive finite number")
    timeout = float(timeout_seconds)
    if timeout <= 0 or timeout != timeout or timeout in (float("inf"), float("-inf")):
        raise ValueError("timeout_seconds must be a positive finite number")
    url = request.sse_url(base_url)
    frame = transport(
        url,
        {
            "Accept": "text/event-stream",
            "X-API-Key": secret,
            "User-Agent": "Autosport/0.1 read-only-complete-board-observer",
        },
        timeout,
        _MAX_SSE_BYTES,
    )
    if not isinstance(frame, Mapping):
        raise ProviderObservationIntegrityError(
            "provider snapshot transport must return an object"
        )
    snapshot = CompleteGameBoardSnapshot(
        request=request,
        captured_at=clock(),
        frame_json=_canonical_json(dict(frame)),
    )
    return _remember(snapshot)


class CompleteGameBoardEvidenceStore:
    """Content-addressed immutable persistence for authorized provider snapshots."""

    DIRECTORY = "provider-complete-game-board"

    def __init__(self, workspace: str | Path) -> None:
        self.root = Path(workspace).expanduser().resolve(strict=False) / self.DIRECTORY

    def _path(self, evidence_sha256: str) -> Path:
        return self.root / f"{_sha(evidence_sha256, 'evidence_sha256')}.json"

    def save(self, snapshot: CompleteGameBoardSnapshot) -> Path:
        assert_complete_game_board_authoritative(snapshot)
        path = self._path(snapshot.evidence_sha256)
        if path.exists():
            loaded = self.load(snapshot.evidence_sha256)
            if loaded.to_payload() != snapshot.to_payload():
                raise ProviderObservationIntegrityError(
                    "content-addressed provider evidence conflicts with existing bytes"
                )
            return path
        atomic_write_json(path, snapshot.to_payload())
        loaded = self.load(snapshot.evidence_sha256)
        if loaded.to_payload() != snapshot.to_payload():
            raise ProviderObservationIntegrityError(
                "persisted provider evidence does not match captured evidence"
            )
        return path

    def load(self, evidence_sha256: str) -> CompleteGameBoardSnapshot:
        path = self._path(evidence_sha256)
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
        snapshot = CompleteGameBoardSnapshot.from_payload(raw)
        if snapshot.evidence_sha256 != _sha(evidence_sha256, "evidence_sha256"):
            raise ProviderObservationIntegrityError(
                "content-addressed provider evidence path does not match payload"
            )
        return _remember(snapshot)
