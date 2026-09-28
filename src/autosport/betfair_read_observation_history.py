"""Durable historical evidence for Betfair read-completeness attempts.

This module is a dependent evidence sidecar for ``betfair_read_completeness``.
It persists exact product-issued completeness witnesses across restart without
turning persisted bytes back into live provider-origin or current-absence
authority.

The local query history is rollback-fenced by the existing
``MonotonicWorkspaceAuthority``. It does not replace ``SourceHealthStore`` and
does not perform provider I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping

from .betfair_read_completeness import (
    BetfairObservationCompleteness,
    BetfairReadCompletenessWitness,
)
from .integrity import atomic_write_json, durable_path_lock
from .monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
)
from .workspace_lock import WorkspaceEconomicLock


_SCHEMA_VERSION = 1
_AUTHORITY_FAMILY = "provider.betfair-readonly-observation-completeness"
_AUTHORITY_DOMAIN = "betfair-read-observation-history-v1"
_DIRECTORY = "provider-read-observation-history"
_HEX = frozenset("0123456789abcdef")


class BetfairReadObservationHistoryError(RuntimeError):
    """Persisted read-attempt evidence is malformed, conflicting, or stale."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise BetfairReadObservationHistoryError(
            f"{name} must be non-empty canonical text"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise BetfairReadObservationHistoryError(
            f"{name} must be valid UTF-8 text"
        ) from exc
    return value


def _sha(value: object, name: str) -> str:
    raw = _text(value, name).lower()
    if len(raw) != 64 or any(character not in _HEX for character in raw):
        raise BetfairReadObservationHistoryError(
            f"{name} must be a canonical SHA-256 hex digest"
        )
    return raw


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BetfairReadObservationHistoryError(
            f"{name} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetfairReadObservationHistoryError(
            f"{name} must include a timezone"
        )
    return parsed.astimezone(timezone.utc)


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BetfairReadObservationHistoryError(
            "read observation evidence is outside canonical JSON domain"
        ) from exc


def _digest(value: object) -> str:
    return sha256(_canonical_bytes(value)).hexdigest()


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BetfairReadObservationHistoryError(
                f"duplicate JSON key in read observation history: {key}"
            )
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class BetfairReadObservationRecord:
    """One immutable historical projection of an exact product-issued witness."""

    sequence: int
    operation: str
    completeness: BetfairObservationCompleteness
    venue_id: str
    account_id: str
    adapter_id: str
    adapter_version: str
    query_sha256: str
    attempt_id: str
    started_at: str
    finished_at: str
    pages: tuple[tuple[int, int, bool, str], ...]
    rows_observed: int
    failure_code: str | None
    source_witness_sha256: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence <= 0
        ):
            raise BetfairReadObservationHistoryError(
                "sequence must be a positive integer"
            )
        for name in (
            "operation",
            "venue_id",
            "account_id",
            "adapter_id",
            "adapter_version",
        ):
            _text(getattr(self, name), name)
        _sha(self.query_sha256, "query_sha256")
        _sha(self.attempt_id, "attempt_id")
        _sha(self.source_witness_sha256, "source_witness_sha256")
        started = _instant(self.started_at, "started_at")
        finished = _instant(self.finished_at, "finished_at")
        if finished < started:
            raise BetfairReadObservationHistoryError(
                "finished_at must not precede started_at"
            )
        if not isinstance(self.completeness, BetfairObservationCompleteness):
            raise BetfairReadObservationHistoryError(
                "completeness must be canonical BetfairObservationCompleteness"
            )
        if not isinstance(self.pages, tuple):
            raise BetfairReadObservationHistoryError("pages must be a tuple")
        for page in self.pages:
            if (
                not isinstance(page, tuple)
                or len(page) != 4
                or isinstance(page[0], bool)
                or not isinstance(page[0], int)
                or page[0] < 0
                or isinstance(page[1], bool)
                or not isinstance(page[1], int)
                or page[1] <= 0
                or not isinstance(page[2], bool)
            ):
                raise BetfairReadObservationHistoryError(
                    "page evidence is malformed"
                )
            _sha(page[3], "page response digest")
        if (
            isinstance(self.rows_observed, bool)
            or not isinstance(self.rows_observed, int)
            or self.rows_observed < 0
        ):
            raise BetfairReadObservationHistoryError(
                "rows_observed must be a non-negative integer"
            )
        if self.failure_code is not None:
            _text(self.failure_code, "failure_code")
        if (
            self.completeness
            is BetfairObservationCompleteness.COMPLETE_FOR_DECLARED_QUERY_WINDOW
            and self.failure_code is not None
        ):
            raise BetfairReadObservationHistoryError(
                "complete historical observation cannot carry failure_code"
            )
        if (
            self.completeness
            is not BetfairObservationCompleteness.COMPLETE_FOR_DECLARED_QUERY_WINDOW
            and self.failure_code is None
        ):
            raise BetfairReadObservationHistoryError(
                "incomplete historical observation requires failure_code"
            )

    @property
    def historical_only(self) -> bool:
        """Persisted history never recreates current/live provider authority."""
        return True

    @property
    def evidence_sha256(self) -> str:
        return _digest(self.to_payload())

    def same_witness(self, other: "BetfairReadObservationRecord") -> bool:
        if not isinstance(other, BetfairReadObservationRecord):
            return False
        left = self.to_payload()
        right = other.to_payload()
        left.pop("sequence")
        right.pop("sequence")
        return left == right

    def to_payload(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "operation": self.operation,
            "completeness": self.completeness.value,
            "venue_id": self.venue_id,
            "account_id": self.account_id,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "query_sha256": self.query_sha256,
            "attempt_id": self.attempt_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "pages": [list(page) for page in self.pages],
            "rows_observed": self.rows_observed,
            "failure_code": self.failure_code,
            "source_witness_sha256": self.source_witness_sha256,
        }

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, object]
    ) -> "BetfairReadObservationRecord":
        expected = {
            "sequence",
            "operation",
            "completeness",
            "venue_id",
            "account_id",
            "adapter_id",
            "adapter_version",
            "query_sha256",
            "attempt_id",
            "started_at",
            "finished_at",
            "pages",
            "rows_observed",
            "failure_code",
            "source_witness_sha256",
        }
        if set(payload) != expected:
            raise BetfairReadObservationHistoryError(
                "historical read record fields are not canonical"
            )
        raw_pages = payload["pages"]
        if not isinstance(raw_pages, list):
            raise BetfairReadObservationHistoryError(
                "historical read pages must be a JSON array"
            )
        pages: list[tuple[int, int, bool, str]] = []
        for raw_page in raw_pages:
            if not isinstance(raw_page, list) or len(raw_page) != 4:
                raise BetfairReadObservationHistoryError(
                    "historical read page evidence is malformed"
                )
            pages.append(
                (
                    raw_page[0],  # type: ignore[arg-type]
                    raw_page[1],  # type: ignore[arg-type]
                    raw_page[2],  # type: ignore[arg-type]
                    raw_page[3],  # type: ignore[arg-type]
                )
            )
        try:
            completeness = BetfairObservationCompleteness(payload["completeness"])
        except (TypeError, ValueError) as exc:
            raise BetfairReadObservationHistoryError(
                "historical completeness is not canonical"
            ) from exc
        return cls(
            sequence=payload["sequence"],  # type: ignore[arg-type]
            operation=payload["operation"],  # type: ignore[arg-type]
            completeness=completeness,
            venue_id=payload["venue_id"],  # type: ignore[arg-type]
            account_id=payload["account_id"],  # type: ignore[arg-type]
            adapter_id=payload["adapter_id"],  # type: ignore[arg-type]
            adapter_version=payload["adapter_version"],  # type: ignore[arg-type]
            query_sha256=payload["query_sha256"],  # type: ignore[arg-type]
            attempt_id=payload["attempt_id"],  # type: ignore[arg-type]
            started_at=payload["started_at"],  # type: ignore[arg-type]
            finished_at=payload["finished_at"],  # type: ignore[arg-type]
            pages=tuple(pages),
            rows_observed=payload["rows_observed"],  # type: ignore[arg-type]
            failure_code=payload["failure_code"],  # type: ignore[arg-type]
            source_witness_sha256=payload["source_witness_sha256"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_issued_witness(
        cls,
        witness: BetfairReadCompletenessWitness,
        *,
        sequence: int,
    ) -> "BetfairReadObservationRecord":
        if type(witness) is not BetfairReadCompletenessWitness:
            raise BetfairReadObservationHistoryError(
                "history accepts only exact BetfairReadCompletenessWitness"
            )
        try:
            witness.assert_issued()
            witness_sha = witness._fingerprint()
        except Exception as exc:
            raise BetfairReadObservationHistoryError(
                "history requires an exact product-issued completeness witness"
            ) from exc
        return cls(
            sequence=sequence,
            operation=witness.operation,
            completeness=witness.completeness,
            venue_id=witness.venue_id,
            account_id=witness.account_id,
            adapter_id=witness.adapter_id,
            adapter_version=witness.adapter_version,
            query_sha256=witness.query_sha256,
            attempt_id=witness.attempt_id,
            started_at=witness.started_at,
            finished_at=witness.finished_at,
            pages=witness.pages,
            rows_observed=witness.rows_observed,
            failure_code=witness.failure_code,
            source_witness_sha256=_sha(witness_sha, "source witness fingerprint"),
        )


class BetfairReadObservationHistory:
    """Query-scoped append-only historical evidence with rollback fencing."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        authority_root: str | Path | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve(strict=False)
        if not self.workspace.is_absolute():
            raise BetfairReadObservationHistoryError(
                "workspace must resolve to an absolute path"
            )
        self.root = self.workspace / _DIRECTORY
        self.authority_root = authority_root

    def _path(self, query_sha256: str) -> Path:
        return self.root / f"{_sha(query_sha256, 'query_sha256')}.json"

    def _authority(self, query_sha256: str) -> MonotonicWorkspaceAuthority:
        return MonotonicWorkspaceAuthority(
            workspace=self.workspace,
            domain=_AUTHORITY_DOMAIN,
            key=_sha(query_sha256, "query_sha256"),
            authority_root=self.authority_root,
        )

    @staticmethod
    def _empty_state(query_sha256: str) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "authority_family": _AUTHORITY_FAMILY,
            "query_sha256": _sha(query_sha256, "query_sha256"),
            "records": [],
        }

    @staticmethod
    def _state_sha256(state: Mapping[str, object]) -> str:
        return _digest(dict(state))

    @staticmethod
    def _semantic_binding_sha256(state: Mapping[str, object]) -> str:
        records = state.get("records")
        if not isinstance(records, list):
            raise BetfairReadObservationHistoryError(
                "history records must be a JSON array"
            )
        tip = None
        if records:
            last = records[-1]
            if not isinstance(last, dict):
                raise BetfairReadObservationHistoryError(
                    "history record must be a JSON object"
                )
            tip = _digest(last)
        return _digest(
            {
                "schema": _AUTHORITY_DOMAIN,
                "query_sha256": state.get("query_sha256"),
                "record_count": len(records),
                "tip_record_sha256": tip,
            }
        )

    @staticmethod
    def _read_json(path: Path) -> dict[str, object]:
        def reject_constant(value: str) -> None:
            raise BetfairReadObservationHistoryError(
                f"non-finite JSON constant in read history: {value}"
            )

        try:
            raw = json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=reject_constant,
            )
        except BetfairReadObservationHistoryError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise BetfairReadObservationHistoryError(
                "cannot read durable Betfair read observation history"
            ) from exc
        if not isinstance(raw, dict):
            raise BetfairReadObservationHistoryError(
                "Betfair read observation history must be a JSON object"
            )
        return raw

    @classmethod
    def _validate_state(
        cls, state: Mapping[str, object], *, expected_query_sha256: str
    ) -> tuple[BetfairReadObservationRecord, ...]:
        expected = {
            "schema_version",
            "authority_family",
            "query_sha256",
            "records",
        }
        if set(state) != expected:
            raise BetfairReadObservationHistoryError(
                "read observation history fields are not canonical"
            )
        if state.get("schema_version") != _SCHEMA_VERSION:
            raise BetfairReadObservationHistoryError(
                "unsupported read observation history schema"
            )
        if state.get("authority_family") != _AUTHORITY_FAMILY:
            raise BetfairReadObservationHistoryError(
                "read observation history authority family mismatch"
            )
        query_sha = _sha(state.get("query_sha256"), "persisted query_sha256")
        if query_sha != _sha(expected_query_sha256, "expected query_sha256"):
            raise BetfairReadObservationHistoryError(
                "read observation history query identity mismatch"
            )
        raw_records = state.get("records")
        if not isinstance(raw_records, list):
            raise BetfairReadObservationHistoryError(
                "read observation history records must be an array"
            )
        records: list[BetfairReadObservationRecord] = []
        attempts: set[str] = set()
        for index, raw_record in enumerate(raw_records, start=1):
            if not isinstance(raw_record, dict):
                raise BetfairReadObservationHistoryError(
                    "read observation history record must be an object"
                )
            record = BetfairReadObservationRecord.from_payload(raw_record)
            if record.sequence != index:
                raise BetfairReadObservationHistoryError(
                    "read observation history sequence is not contiguous"
                )
            if record.query_sha256 != query_sha:
                raise BetfairReadObservationHistoryError(
                    "read observation record escaped query scope"
                )
            if record.attempt_id in attempts:
                raise BetfairReadObservationHistoryError(
                    "read observation history contains duplicate attempt_id"
                )
            attempts.add(record.attempt_id)
            records.append(record)
        return tuple(records)

    def _read_state(
        self, query_sha256: str
    ) -> tuple[dict[str, object] | None, tuple[BetfairReadObservationRecord, ...]]:
        path = self._path(query_sha256)
        if not path.exists():
            return None, ()
        state = self._read_json(path)
        records = self._validate_state(
            state, expected_query_sha256=query_sha256
        )
        return state, records

    def _recover(
        self,
        *,
        query_sha256: str,
        state: Mapping[str, object] | None,
    ) -> None:
        authority = self._authority(query_sha256)
        observed = None if state is None else self._state_sha256(state)
        history = authority.read_history()
        pending = (
            history[-1]
            if history and history[-1].phase is AuthorityPhase.PREPARE
            else None
        )
        try:
            if (
                pending is not None
                and observed == pending.intended_state_sha256
                and state is not None
            ):
                binding = self._semantic_binding_sha256(state)
                if binding != pending.semantic_binding_sha256:
                    raise BetfairReadObservationHistoryError(
                        "prepared history semantic binding conflicts with local bytes"
                    )
                authority.recover(
                    observed_state_sha256=observed,
                    tx_id=pending.tx_id,
                    semantic_binding_sha256=binding,
                )
            else:
                authority.recover(observed_state_sha256=observed)
        except MonotonicWorkspaceAuthorityError as exc:
            raise BetfairReadObservationHistoryError(
                "read observation history failed monotonic rollback/recovery validation"
            ) from exc

    def append(
        self, witness: BetfairReadCompletenessWitness
    ) -> BetfairReadObservationRecord:
        if type(witness) is not BetfairReadCompletenessWitness:
            raise BetfairReadObservationHistoryError(
                "history accepts only exact BetfairReadCompletenessWitness"
            )
        query_sha = _sha(witness.query_sha256, "query_sha256")
        path = self._path(query_sha)
        with WorkspaceEconomicLock(self.workspace):
            with durable_path_lock(path):
                state, records = self._read_state(query_sha)
                self._recover(query_sha256=query_sha, state=state)

                for existing in records:
                    if existing.attempt_id != witness.attempt_id:
                        continue
                    candidate = BetfairReadObservationRecord.from_issued_witness(
                        witness, sequence=existing.sequence
                    )
                    if existing.same_witness(candidate):
                        return existing
                    raise BetfairReadObservationHistoryError(
                        "attempt_id was reused with conflicting read evidence"
                    )

                record = BetfairReadObservationRecord.from_issued_witness(
                    witness, sequence=len(records) + 1
                )
                next_state = self._empty_state(query_sha)
                next_state["records"] = [
                    existing.to_payload() for existing in records
                ] + [record.to_payload()]
                intended = self._state_sha256(next_state)
                binding = self._semantic_binding_sha256(next_state)
                observed = None if state is None else self._state_sha256(state)
                authority = self._authority(query_sha)
                tx_id = f"betfair-read-history:{record.attempt_id}:{intended[:24]}"
                try:
                    authority.prepare(
                        tx_id=tx_id,
                        observed_state_sha256=observed,
                        intended_state_sha256=intended,
                        semantic_binding_sha256=binding,
                    )
                    atomic_write_json(path, next_state)
                    published = self._read_json(path)
                    self._validate_state(
                        published, expected_query_sha256=query_sha
                    )
                    if self._state_sha256(published) != intended:
                        raise BetfairReadObservationHistoryError(
                            "published read history does not match prepared state"
                        )
                    authority.commit(
                        tx_id=tx_id,
                        observed_state_sha256=intended,
                        semantic_binding_sha256=binding,
                    )
                except BetfairReadObservationHistoryError:
                    raise
                except MonotonicWorkspaceAuthorityError as exc:
                    raise BetfairReadObservationHistoryError(
                        "read observation history publication failed closed"
                    ) from exc
                return record

    def records(
        self, query_sha256: str
    ) -> tuple[BetfairReadObservationRecord, ...]:
        query_sha = _sha(query_sha256, "query_sha256")
        path = self._path(query_sha)
        with WorkspaceEconomicLock(self.workspace):
            with durable_path_lock(path):
                state, records = self._read_state(query_sha)
                self._recover(query_sha256=query_sha, state=state)
                return records

    def records_as_of(
        self,
        query_sha256: str,
        *,
        as_of: datetime,
    ) -> tuple[BetfairReadObservationRecord, ...]:
        if not isinstance(as_of, datetime):
            raise TypeError("as_of must be a datetime")
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        cutoff = as_of.astimezone(timezone.utc)
        return tuple(
            record
            for record in self.records(query_sha256)
            if _instant(record.finished_at, "finished_at") <= cutoff
        )

    def get_attempt(
        self, query_sha256: str, attempt_id: str
    ) -> BetfairReadObservationRecord:
        expected = _sha(attempt_id, "attempt_id")
        for record in self.records(query_sha256):
            if record.attempt_id == expected:
                return record
        raise KeyError(expected)
