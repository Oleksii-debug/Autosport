from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from .evaluation_intake import ObservationIntakeLedger, ObservationIntakeSnapshot
from .evaluation_universe import (
    EvaluationRow,
    EvaluationUniverse,
    EvaluationUniverseError,
    EvaluationUniverseIntegrityError,
    EvaluationUniverseLedger,
    EvaluationUniverseStore,
    SlotState,
)
from .provider_observation_authority import (
    CompleteGameBoardSnapshot,
    assert_complete_game_board_authoritative,
)


_PROVIDER_ID = "parlayapi"
_CONSUMER_KIND = "parlay-complete-game-board-evaluation-v1"
_MAX_ISSUED_UNIVERSES = 256
_ISSUED_UNIVERSES: dict[int, tuple[EvaluationUniverse, str, str]] = {}


class ProviderEvaluationUniverseError(EvaluationUniverseError):
    """Live provider completeness could not authorize the evaluation denominator."""


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ProviderEvaluationUniverseError(f"{field} must be non-empty canonical text")
    value.encode("utf-8")
    return value


def _sha(value: object, field: str) -> str:
    raw = _text(value, field).lower()
    if len(raw) != 64 or any(ch not in "0123456789abcdef" for ch in raw):
        raise ProviderEvaluationUniverseError(f"{field} must be canonical SHA-256 hex")
    return raw


def _instant(value: object, field: str) -> datetime:
    raw = _text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderEvaluationUniverseError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderEvaluationUniverseError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


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
        raise ProviderEvaluationUniverseError("provider member evidence must be finite JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CompleteBoardMemberSpec:
    """One deterministic selection member of an authoritative complete game board."""

    row_key: str
    member_sha256: str
    event_id: str | None
    market_id: str | None
    selection_id: str | None
    source_at: str

    def __post_init__(self) -> None:
        _text(self.row_key, "row_key")
        _sha(self.member_sha256, "member_sha256")
        if self.event_id is not None:
            _text(self.event_id, "event_id")
        if self.market_id is not None:
            _text(self.market_id, "market_id")
        if self.selection_id is not None:
            _text(self.selection_id, "selection_id")
        _instant(self.source_at, "source_at")



def _selection_labels(market_key: str) -> tuple[str, str]:
    if market_key in {"h2h", "spreads"}:
        return ("home", "away")
    if market_key == "totals":
        return ("over", "under")
    raise ProviderEvaluationUniverseError(
        "complete game-board consumer received unsupported market_key"
    )


def complete_game_board_member_specs(
    snapshot: CompleteGameBoardSnapshot,
) -> tuple[CompleteBoardMemberSpec, ...]:
    """Derive every selection slot from the exact live canonical provider capability."""

    assert_complete_game_board_authoritative(snapshot)
    frame = snapshot.frame
    raw_rows = frame.get("data")
    if not isinstance(raw_rows, list):
        raise ProviderEvaluationUniverseError("complete game-board data must be a list")

    members: list[CompleteBoardMemberSpec] = []
    if not raw_rows:
        member_sha256 = _digest(
            {
                "kind": _CONSUMER_KIND,
                "provider_evidence_sha256": snapshot.evidence_sha256,
                "empty_complete_board": True,
            }
        )
        members.append(
            CompleteBoardMemberSpec(
                row_key=f"parlay-board-empty:{member_sha256}",
                member_sha256=member_sha256,
                event_id=None,
                market_id=None,
                selection_id=None,
                source_at=snapshot.captured_at,
            )
        )
        return tuple(members)

    seen: set[str] = set()
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            raise ProviderEvaluationUniverseError("provider board row must be an object")
        event_id = _text(raw.get("event_id"), "provider event_id")
        bookmaker = _text(raw.get("bookmaker"), "provider bookmaker")
        market_key = _text(raw.get("market_key"), "provider market_key")
        source_at_raw = raw.get("last_update", snapshot.captured_at)
        source_at = _text(source_at_raw, "provider last_update")
        _instant(source_at, "provider last_update")
        market_id = f"{bookmaker}:{market_key}"
        provider_row_sha256 = _digest(dict(raw))
        for label in _selection_labels(market_key):
            member_sha256 = _digest(
                {
                    "kind": _CONSUMER_KIND,
                    "provider_evidence_sha256": snapshot.evidence_sha256,
                    "provider_row_sha256": provider_row_sha256,
                    "event_id": event_id,
                    "bookmaker": bookmaker,
                    "market_key": market_key,
                    "selection": label,
                }
            )
            row_key = f"parlay-board:{member_sha256}"
            if row_key in seen:
                raise ProviderEvaluationUniverseError(
                    "provider complete board produced duplicate selection membership"
                )
            seen.add(row_key)
            members.append(
                CompleteBoardMemberSpec(
                    row_key=row_key,
                    member_sha256=member_sha256,
                    event_id=event_id,
                    market_id=market_id,
                    selection_id=f"{bookmaker}:{market_key}:{label}",
                    source_at=source_at,
                )
            )
    return tuple(sorted(members, key=lambda item: item.row_key))


def _validate_row_against_member(
    *,
    row: EvaluationRow,
    member: CompleteBoardMemberSpec,
    snapshot: CompleteGameBoardSnapshot,
    evaluation_not_before: str,
    outcome_reveal_not_before: str,
) -> None:
    expected = (
        _PROVIDER_ID,
        snapshot.request.source_id,
        snapshot.request.sport_key,
        member.event_id,
        member.market_id,
        member.selection_id,
        member.row_key,
    )
    actual = (
        row.provider_id,
        row.source_id,
        row.sport,
        row.event_id,
        row.market_id,
        row.selection_id,
        row.row_key,
    )
    if actual != expected:
        raise ProviderEvaluationUniverseError(
            "evaluation row does not match exact provider selection membership"
        )
    if _instant(row.source_at, "row source_at") != _instant(member.source_at, "member source_at"):
        raise ProviderEvaluationUniverseError(
            "evaluation row source_at does not match provider member evidence"
        )
    captured = _instant(snapshot.captured_at, "provider captured_at")
    if _instant(row.received_at, "row received_at") != captured:
        raise ProviderEvaluationUniverseError(
            "evaluation row received_at must equal authoritative provider capture time"
        )
    committed = _instant(row.committed_at, "row committed_at")
    evaluation = _instant(evaluation_not_before, "evaluation_not_before")
    if committed < captured or committed > evaluation:
        raise ProviderEvaluationUniverseError(
            "evaluation row must be committed after provider capture and before evaluation"
        )
    if row.detection_at is not None and _instant(row.detection_at, "detection_at") < evaluation:
        raise ProviderEvaluationUniverseError(
            "row detection cannot precede complete provider membership authority"
        )
    if row.decision_at is not None and _instant(row.decision_at, "decision_at") < evaluation:
        raise ProviderEvaluationUniverseError(
            "row decision cannot precede complete provider membership authority"
        )
    if row.outcome_reveal_not_before is None or _instant(
        row.outcome_reveal_not_before,
        "row outcome_reveal_not_before",
    ) != _instant(outcome_reveal_not_before, "outcome_reveal_not_before"):
        raise ProviderEvaluationUniverseError(
            "row reveal boundary must equal the frozen pre-result provider consumer boundary"
        )
    if member.event_id is None:
        if row.slot_state is not SlotState.NO_EVENT:
            raise ProviderEvaluationUniverseError(
                "an authoritative empty complete board requires an explicit NO_EVENT denominator row"
            )
    elif row.slot_state in {SlotState.NO_EVENT, SlotState.SOURCE_OUTAGE}:
        raise ProviderEvaluationUniverseError(
            "provider-present selection membership cannot be labelled NO_EVENT or SOURCE_OUTAGE"
        )


def _remember_issued(
    universe: EvaluationUniverse,
    *,
    authority_id: str,
    source_id: str,
) -> None:
    if len(_ISSUED_UNIVERSES) >= _MAX_ISSUED_UNIVERSES:
        _ISSUED_UNIVERSES.pop(next(iter(_ISSUED_UNIVERSES)))
    _ISSUED_UNIVERSES[id(universe)] = (universe, authority_id, source_id)


def _assert_issued(
    universe: EvaluationUniverse,
    *,
    authority_id: str,
    source_id: str,
) -> None:
    issued = _ISSUED_UNIVERSES.get(id(universe))
    if (
        issued is None
        or issued[0] is not universe
        or issued[1] != authority_id
        or issued[2] != source_id
    ):
        raise ProviderEvaluationUniverseError(
            "first durable provider evaluation universe must come from a live canonical complete-board derivation"
        )


def build_frozen_universe_from_complete_game_board(
    *,
    snapshot: CompleteGameBoardSnapshot,
    authority_id: str,
    session_id: str,
    universe_id: str,
    campaign_id: str,
    research_protocol_id: str,
    protocol_sha256: str,
    evaluation_not_before: str,
    outcome_reveal_not_before: str,
    frozen_at: str,
    rows: Iterable[EvaluationRow],
) -> EvaluationUniverse:
    """Freeze every provider selection slot while the exact live provider capability exists."""

    assert_complete_game_board_authoritative(snapshot)
    authority_id = _text(authority_id, "authority_id")
    session_id = _text(session_id, "session_id")
    universe_id = _text(universe_id, "universe_id")
    campaign_id = _text(campaign_id, "campaign_id")
    research_protocol_id = _text(research_protocol_id, "research_protocol_id")
    protocol_sha256 = _sha(protocol_sha256, "protocol_sha256")
    captured = _instant(snapshot.captured_at, "provider captured_at")
    evaluation = _instant(evaluation_not_before, "evaluation_not_before")
    reveal = _instant(outcome_reveal_not_before, "outcome_reveal_not_before")
    frozen = _instant(frozen_at, "frozen_at")
    if evaluation < captured:
        raise ProviderEvaluationUniverseError(
            "evaluation boundary cannot precede complete provider capture"
        )
    if reveal <= evaluation:
        raise ProviderEvaluationUniverseError(
            "outcome reveal must be strictly after evaluation boundary"
        )
    if frozen < evaluation or frozen >= reveal:
        raise ProviderEvaluationUniverseError(
            "frozen_at must be after complete membership authority and before result reveal"
        )

    members = complete_game_board_member_specs(snapshot)
    materialized = tuple(rows)
    if not materialized:
        raise ProviderEvaluationUniverseError(
            "provider evaluation denominator requires explicit membership rows"
        )
    by_key = {row.row_key: row for row in materialized}
    if len(by_key) != len(materialized):
        raise ProviderEvaluationUniverseError("provider evaluation row_key values must be unique")
    expected_keys = tuple(member.row_key for member in members)
    if tuple(sorted(by_key)) != tuple(sorted(expected_keys)):
        raise ProviderEvaluationUniverseError(
            "evaluation rows must equal every canonical complete-board selection member"
        )
    for member in members:
        _validate_row_against_member(
            row=by_key[member.row_key],
            member=member,
            snapshot=snapshot,
            evaluation_not_before=evaluation_not_before,
            outcome_reveal_not_before=outcome_reveal_not_before,
        )

    root_sha256 = _digest(
        {
            "kind": _CONSUMER_KIND,
            "provider_evidence_sha256": snapshot.evidence_sha256,
            "provider_frame_sha256": snapshot.frame_sha256,
            "provider_request": snapshot.request.to_payload(),
            "provider_captured_at": snapshot.captured_at,
            "member_specs": [
                {
                    "row_key": item.row_key,
                    "member_sha256": item.member_sha256,
                    "event_id": item.event_id,
                    "market_id": item.market_id,
                    "selection_id": item.selection_id,
                    "source_at": item.source_at,
                }
                for item in members
            ],
            "evaluation_not_before": evaluation_not_before,
            "outcome_reveal_not_before": outcome_reveal_not_before,
        }
    )
    intake_snapshot = ObservationIntakeSnapshot(
        authority_id=authority_id,
        session_id=session_id,
        source_id=snapshot.request.source_id,
        campaign_id=campaign_id,
        research_protocol_id=research_protocol_id,
        protocol_sha256=protocol_sha256,
        universe_id=universe_id,
        first_cycle=1,
        last_cycle=1,
        record_count=1,
        root_sha256=root_sha256,
        expected_row_keys=tuple(sorted(expected_keys)),
        committed_at=snapshot.captured_at,
        evaluation_not_before=evaluation_not_before,
    )
    universe = EvaluationUniverse._construct(
        intake_snapshot=intake_snapshot,
        universe_id=universe_id,
        campaign_id=campaign_id,
        research_protocol_id=research_protocol_id,
        protocol_sha256=protocol_sha256,
        frozen_at=frozen_at,
        rows=materialized,
    )
    _remember_issued(
        universe,
        authority_id=authority_id,
        source_id=snapshot.request.source_id,
    )
    return universe


class _ProviderIntakeLedger(ObservationIntakeLedger):
    """Adapter for the already-derived durable consumer snapshot; not a provider issuer."""

    def __init__(self, workspace: str | Path, *, authority_id: str, source_id: str) -> None:
        self.workspace = Path(workspace)
        self.authority_id = _text(authority_id, "authority_id")
        self.source_id = _text(source_id, "source_id")

    def verify_snapshot(self, snapshot: ObservationIntakeSnapshot) -> None:
        if not isinstance(snapshot, ObservationIntakeSnapshot):
            raise EvaluationUniverseIntegrityError(
                "provider consumer snapshot must be ObservationIntakeSnapshot"
            )
        if snapshot.authority_id != self.authority_id or snapshot.source_id != self.source_id:
            raise EvaluationUniverseIntegrityError(
                "provider consumer snapshot authority/source identity mismatch"
            )
        if (snapshot.first_cycle, snapshot.last_cycle, snapshot.record_count) != (1, 1, 1):
            raise EvaluationUniverseIntegrityError(
                "provider complete-board consumer snapshot must be one immutable replacement baseline"
            )
        if not snapshot.expected_row_keys or not all(
            key.startswith("parlay-board:") or key.startswith("parlay-board-empty:")
            for key in snapshot.expected_row_keys
        ):
            raise EvaluationUniverseIntegrityError(
                "provider consumer snapshot membership is not canonical complete-board membership"
            )


class ProviderEvaluationUniverseStore:
    """Persist #638's derived consumer state without reissuing provider origin after restart."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        authority_id: str,
        source_id: str,
        paper_resolver=None,
        authority_root: str | Path | None = None,
    ) -> None:
        self.authority_id = _text(authority_id, "authority_id")
        self.source_id = _text(source_id, "source_id")
        self._intake = _ProviderIntakeLedger(
            workspace,
            authority_id=self.authority_id,
            source_id=self.source_id,
        )
        self._store = EvaluationUniverseStore(
            workspace,
            intake_ledger=self._intake,
            paper_resolver=paper_resolver,
            authority_root=authority_root,
        )

    def load(self) -> EvaluationUniverseLedger | None:
        return self._store.load()

    def save(self, ledger: EvaluationUniverseLedger) -> None:
        if not isinstance(ledger, EvaluationUniverseLedger):
            raise ProviderEvaluationUniverseError(
                "ledger must be EvaluationUniverseLedger"
            )
        if (
            ledger.universe.intake_snapshot.authority_id != self.authority_id
            or ledger.universe.intake_snapshot.source_id != self.source_id
        ):
            raise ProviderEvaluationUniverseError(
                "provider evaluation universe belongs to a different authority/source"
            )
        existing = self._store.load()
        if existing is None:
            _assert_issued(
                ledger.universe,
                authority_id=self.authority_id,
                source_id=self.source_id,
            )
        elif existing.universe.universe_sha256 != ledger.universe.universe_sha256:
            raise ProviderEvaluationUniverseError(
                "cannot replace durable provider-derived evaluation universe identity"
            )
        self._store.save(ledger)
        _ISSUED_UNIVERSES.pop(id(ledger.universe), None)
