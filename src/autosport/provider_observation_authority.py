from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import secrets
import weakref
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
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

    def sse_url(self) -> str:
        query = urlencode(
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


_ISSUED: dict[int, tuple[weakref.ReferenceType, str]] = {}


def _forget_issued(snapshot_id: int, reference: weakref.ReferenceType) -> None:
    current = _ISSUED.get(snapshot_id)
    if current is not None and current[0] is reference:
        _ISSUED.pop(snapshot_id, None)


def _remember(snapshot: CompleteGameBoardSnapshot) -> CompleteGameBoardSnapshot:
    snapshot_id = id(snapshot)
    reference = weakref.ref(
        snapshot,
        lambda current, snapshot_id=snapshot_id: _forget_issued(snapshot_id, current),
    )
    _ISSUED[snapshot_id] = (reference, snapshot.evidence_sha256)
    return snapshot


def assert_complete_game_board_authoritative(snapshot: CompleteGameBoardSnapshot) -> None:
    if not isinstance(snapshot, CompleteGameBoardSnapshot):
        raise ProviderObservationUnsupportedError(
            "complete provider authority requires CompleteGameBoardSnapshot"
        )
    issued = _ISSUED.get(id(snapshot))
    if issued is None or issued[0]() is not snapshot or issued[1] != snapshot.evidence_sha256:
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


def _read_production_initial_state(
    request_scope: CompleteGameBoardRequest,
    *,
    api_key: str,
    timeout_seconds: float,
) -> Mapping[str, object]:
    """Read one bounded initial_state from the fixed production ParlayAPI SSE origin."""

    request = Request(
        request_scope.sse_url(),
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
        with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310 - fixed HTTPS origin
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
                if consumed > _MAX_SSE_BYTES:
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
) -> CompleteGameBoardSnapshot:
    """Acquire and authorize one documented complete replacement baseline."""

    if not isinstance(api_key, str) or not api_key or api_key != api_key.strip():
        raise ValueError("api_key must be non-empty trimmed text")
    if any(character.isspace() for character in api_key):
        raise ValueError("api_key must not contain whitespace")
    if not isinstance(request, CompleteGameBoardRequest):
        raise TypeError("request must be CompleteGameBoardRequest")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise ValueError("timeout_seconds must be a positive finite number")
    timeout = float(timeout_seconds)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout_seconds must be a positive finite number")
    frame = _read_production_initial_state(
        request,
        api_key=api_key,
        timeout_seconds=timeout,
    )
    snapshot = CompleteGameBoardSnapshot(
        request=request,
        captured_at=_default_clock(),
        frame_json=_canonical_json(dict(frame)),
    )
    return _remember(snapshot)


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

        assert_complete_game_board_authoritative(snapshot)
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
        return _remember(snapshot)
