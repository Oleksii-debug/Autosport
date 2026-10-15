from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .paper import PaperBook


class RiskPathEquityReplayError(RuntimeError):
    """Raised when exact PaperBook snapshots cannot prove one bounded equity path."""


@dataclass(frozen=True, slots=True)
class EquityTransition:
    index: int
    action: str
    ticket_id: str
    balance: Decimal
    observed_at: str | None
    evidence_sha256: str


@dataclass(frozen=True, slots=True)
class RiskPathEquityReplay:
    base_snapshot_sha256: str
    final_snapshot_sha256: str
    start_balance: Decimal
    final_balance: Decimal
    minimum_equity: Decimal
    transitions: tuple[EquityTransition, ...]
    latest_observed_at: str | None
    observed_timestamps_complete: bool
    source_evidence_sha256: str


_IMMUTABLE_TICKET_FIELDS = (
    "ticket_id",
    "stake",
    "placed_at",
    "strategy_reason",
    "provider_source_ids",
    "provider_accounts",
    "bankroll_id",
    "currency",
    "legs",
)

# Keep evidence rendering bounded before fixed-point allocation. 512 matches the
# adjacent risk-evidence canonical text boundary while leaving PaperBook's own
# semantic Decimal domain unchanged.
_MAX_FIXED_POINT_MATERIALIZATION_LENGTH = 512


def _fixed_point_materialization_length(value: Decimal) -> int:
    """Return format(value, "f") size without materializing that string."""

    if type(value) is not Decimal or not value.is_finite():
        raise RiskPathEquityReplayError(
            "risk-path evidence Decimal must be a finite exact Decimal"
        )
    sign, digits, exponent = value.as_tuple()
    if type(exponent) is not int:
        raise RiskPathEquityReplayError(
            "risk-path evidence Decimal exponent must be an integer"
        )
    sign_length = 1 if sign else 0
    digit_count = len(digits)

    # format(..., "f") keeps negative zero scale but collapses nonnegative
    # zero exponents. Account for both forms without allocating the output.
    if value.is_zero():
        if exponent >= 0:
            return sign_length + 1
        return sign_length + 2 - exponent
    if exponent >= 0:
        return sign_length + digit_count + exponent
    if digit_count + exponent > 0:
        return sign_length + digit_count + 1
    return sign_length + 2 - exponent


def _decimal_evidence_text(value: Decimal, *, label: str) -> str:
    materialized_length = _fixed_point_materialization_length(value)
    if materialized_length > _MAX_FIXED_POINT_MATERIALIZATION_LENGTH:
        raise RiskPathEquityReplayError(
            f"{label} fixed-point representation exceeds supported evidence size"
        )
    return format(value, "f")


def _strict_json(payload: bytes, *, label: str) -> dict[str, Any]:
    if type(payload) is not bytes:
        raise TypeError(f"{label} must be exact bytes")

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RiskPathEquityReplayError(
                    f"{label} contains duplicate JSON key: {key}"
                )
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise RiskPathEquityReplayError(
            f"{label} contains non-finite JSON constant: {value}"
        )

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RiskPathEquityReplayError(f"{label} is not valid UTF-8") from exc
    try:
        raw = json.loads(
            text,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_constant,
        )
    except RiskPathEquityReplayError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise RiskPathEquityReplayError(f"{label} is not valid JSON") from exc
    if type(raw) is not dict:
        raise RiskPathEquityReplayError(f"{label} root must be an object")
    return raw


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _ticket_map(raw: dict[str, Any], *, label: str) -> dict[str, dict[str, Any]]:
    tickets = raw.get("tickets")
    if type(tickets) is not list:
        raise RiskPathEquityReplayError(f"{label} tickets must be a list")
    result: dict[str, dict[str, Any]] = {}
    for item in tickets:
        if type(item) is not dict:
            raise RiskPathEquityReplayError(f"{label} ticket must be an object")
        ticket_id = item.get("ticket_id")
        if type(ticket_id) is not str or not ticket_id or ticket_id.strip() != ticket_id:
            raise RiskPathEquityReplayError(f"{label} ticket_id is invalid")
        if ticket_id in result:
            raise RiskPathEquityReplayError(f"{label} contains duplicate ticket_id")
        result[ticket_id] = item
    return result


def _lifecycle(raw: dict[str, Any], *, label: str) -> list[dict[str, Any]]:
    lifecycle = raw.get("lifecycle")
    if type(lifecycle) is not list:
        raise RiskPathEquityReplayError(f"{label} lifecycle must be a list")
    for entry in lifecycle:
        if type(entry) is not dict:
            raise RiskPathEquityReplayError(f"{label} lifecycle entry must be an object")
    return lifecycle


def _immutable_ticket_projection(ticket: dict[str, Any]) -> dict[str, Any]:
    return {field: ticket.get(field) for field in _IMMUTABLE_TICKET_FIELDS}


def _reject_unsupported_exchange_sides(book: PaperBook, *, label: str) -> None:
    for ticket in book.tickets.values():
        for leg in ticket.legs:
            if leg.exchange_side == "lay":
                raise RiskPathEquityReplayError(
                    f"{label} contains LAY ticket {ticket.ticket_id}; "
                    "risk-path equity replay is BACK-only until canonical "
                    "side-aware PaperBook economics exist"
                )


def _parse_utc(value: str, *, label: str) -> datetime:
    if type(value) is not str or not value or value.strip() != value:
        raise RiskPathEquityReplayError(f"{label} must be a canonical timestamp")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise RiskPathEquityReplayError(f"{label} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise RiskPathEquityReplayError(f"{label} must include timezone")
    return parsed.astimezone(timezone.utc)


def replay_paper_book_equity_path(
    base_snapshot: bytes,
    final_snapshot: bytes,
    *,
    expected_changed_ticket_ids: frozenset[str],
    require_complete_observed_timestamps: bool = False,
) -> RiskPathEquityReplay:
    """Re-derive conservative balance/equity from one exact PaperBook suffix.

    This helper is assertion-only. It does not establish product-owned run,
    execution, settlement, membership, IID, or risk-result authority. A future
    risk-path producer must derive expected_changed_ticket_ids from canonical
    product evidence and separately verify those authorities. PaperBook timestamps
    remain observational here: this helper never promotes them into causal authority.

    PaperBook debits stake at OPEN and credits only deterministic settlement payout,
    so its cash balance is the conservative equity floor for unresolved stake.
    Replaying every lifecycle suffix transition therefore preserves transient
    drawdown that a later winning settlement would otherwise hide.
    """

    if type(expected_changed_ticket_ids) is not frozenset:
        raise TypeError("expected_changed_ticket_ids must be a frozenset")
    for ticket_id in expected_changed_ticket_ids:
        if type(ticket_id) is not str or not ticket_id or ticket_id.strip() != ticket_id:
            raise ValueError(
                "expected_changed_ticket_ids must contain canonical non-empty strings"
            )
    if type(require_complete_observed_timestamps) is not bool:
        raise TypeError("require_complete_observed_timestamps must be bool")

    try:
        base_book = PaperBook.load_bytes(base_snapshot)
        final_book = PaperBook.load_bytes(final_snapshot)
    except Exception as exc:
        raise RiskPathEquityReplayError(
            f"PaperBook semantic validation failed: {type(exc).__name__}"
        ) from exc

    _reject_unsupported_exchange_sides(
        base_book,
        label="base PaperBook snapshot",
    )
    _reject_unsupported_exchange_sides(
        final_book,
        label="final PaperBook snapshot",
    )

    base_raw = _strict_json(base_snapshot, label="base PaperBook snapshot")
    final_raw = _strict_json(final_snapshot, label="final PaperBook snapshot")
    base_schema_version = base_raw.get("schema_version")
    final_schema_version = final_raw.get("schema_version")
    if base_schema_version != final_schema_version:
        raise RiskPathEquityReplayError(
            "PaperBook schema version changed across the replay path"
        )

    if base_book.initial_bankroll != final_book.initial_bankroll:
        raise RiskPathEquityReplayError("PaperBook initial bankroll changed across path")

    base_lifecycle = _lifecycle(base_raw, label="base PaperBook snapshot")
    final_lifecycle = _lifecycle(final_raw, label="final PaperBook snapshot")
    if len(final_lifecycle) < len(base_lifecycle):
        raise RiskPathEquityReplayError("final PaperBook lifecycle rolls back BASE history")
    if final_lifecycle[: len(base_lifecycle)] != base_lifecycle:
        raise RiskPathEquityReplayError(
            "final PaperBook lifecycle is not an exact extension of BASE history"
        )

    base_tickets = _ticket_map(base_raw, label="base PaperBook snapshot")
    final_tickets = _ticket_map(final_raw, label="final PaperBook snapshot")

    for ticket_id, base_ticket in base_tickets.items():
        final_ticket = final_tickets.get(ticket_id)
        if final_ticket is None:
            raise RiskPathEquityReplayError(
                f"final PaperBook removed BASE ticket {ticket_id}"
            )
        if _immutable_ticket_projection(base_ticket) != _immutable_ticket_projection(
            final_ticket
        ):
            raise RiskPathEquityReplayError(
                f"final PaperBook mutated immutable BASE ticket {ticket_id}"
            )
        if base_ticket.get("status") != "open":
            for field in ("status", "payout", "settled_at"):
                if base_ticket.get(field) != final_ticket.get(field):
                    raise RiskPathEquityReplayError(
                        f"final PaperBook rewrote terminal BASE ticket {ticket_id}"
                    )

    suffix = final_lifecycle[len(base_lifecycle) :]
    changed_ticket_ids: set[str] = set()
    balance = base_book.balance
    minimum = balance
    transitions: list[EquityTransition] = []
    observed_times: list[tuple[datetime, str]] = []
    observed_timestamps_complete = True

    for index, entry in enumerate(suffix):
        action = entry.get("action")
        ticket_id = entry.get("ticket_id")
        if type(ticket_id) is not str or not ticket_id:
            raise RiskPathEquityReplayError("lifecycle suffix has invalid ticket_id")
        changed_ticket_ids.add(ticket_id)
        if ticket_id not in expected_changed_ticket_ids:
            raise RiskPathEquityReplayError(
                f"foreign PaperBook mutation detected for ticket {ticket_id}"
            )

        ticket = final_tickets.get(ticket_id)
        canonical_ticket = final_book.tickets.get(ticket_id)
        if ticket is None or canonical_ticket is None:
            raise RiskPathEquityReplayError(
                f"lifecycle suffix references unknown ticket {ticket_id}"
            )

        observed_at: str | None
        if action == "open":
            if ticket_id in base_tickets:
                raise RiskPathEquityReplayError(
                    f"lifecycle suffix re-opens BASE ticket {ticket_id}"
                )
            try:
                balance = PaperBook._debit_balance(balance, canonical_ticket.stake)
            except ValueError as exc:
                raise RiskPathEquityReplayError(
                    f"ticket {ticket_id} OPEN arithmetic is not canonical"
                ) from exc
            observed_at = ticket.get("placed_at")
            if type(observed_at) is not str:
                raise RiskPathEquityReplayError(
                    f"ticket {ticket_id} open lacks placed_at"
                )
        elif action == "settle":
            winning_quote_keys = entry.get("winning_quote_keys")
            void_quote_keys = entry.get("void_quote_keys")
            if type(winning_quote_keys) is not list or type(void_quote_keys) is not list:
                raise RiskPathEquityReplayError(
                    f"ticket {ticket_id} settlement evidence is not canonical"
                )
            try:
                computed_status, computed_payout, new_balance = (
                    PaperBook._settlement_result(
                        canonical_ticket,
                        balance,
                        set(winning_quote_keys),
                        set(void_quote_keys),
                    )
                )
            except ValueError as exc:
                raise RiskPathEquityReplayError(
                    f"ticket {ticket_id} SETTLE arithmetic is not canonical"
                ) from exc
            if (
                canonical_ticket.status is not computed_status
                or canonical_ticket.payout != computed_payout
            ):
                raise RiskPathEquityReplayError(
                    f"ticket {ticket_id} persisted settlement economics mismatch"
                )
            balance = new_balance
            observed_at = entry.get("settled_at")
            if observed_at is None:
                observed_timestamps_complete = False
                if require_complete_observed_timestamps:
                    raise RiskPathEquityReplayError(
                        f"ticket {ticket_id} settlement lacks observed timestamp"
                    )
            elif type(observed_at) is not str:
                raise RiskPathEquityReplayError(
                    f"ticket {ticket_id} settlement timestamp is invalid"
                )
        else:
            raise RiskPathEquityReplayError(
                f"unsupported PaperBook lifecycle action: {action!r}"
            )

        if not balance.is_finite():
            raise RiskPathEquityReplayError("replayed balance became non-finite")
        if balance < minimum:
            minimum = balance

        if observed_at is not None:
            parsed_time = _parse_utc(
                observed_at,
                label=f"transition {index} observed_at",
            )
            observed_times.append((parsed_time, observed_at))

        evidence_payload = {
            "schema": "AUTOSPORT_RISK_PATH_EQUITY_TRANSITION_V1",
            "index": index,
            "action": action,
            "ticket_id": ticket_id,
            "balance": _decimal_evidence_text(
                balance,
                label="transition balance",
            ),
            "observed_at": observed_at,
            "entry": entry,
            "ticket": _immutable_ticket_projection(ticket),
        }
        transitions.append(
            EquityTransition(
                index=index,
                action=action,
                ticket_id=ticket_id,
                balance=balance,
                observed_at=observed_at,
                evidence_sha256=_sha256(_canonical_bytes(evidence_payload)),
            )
        )

    if changed_ticket_ids != set(expected_changed_ticket_ids):
        missing = sorted(set(expected_changed_ticket_ids) - changed_ticket_ids)
        raise RiskPathEquityReplayError(
            "expected changed ticket set does not match lifecycle suffix"
            + (f": missing {missing}" if missing else "")
        )

    if balance != final_book.balance:
        raise RiskPathEquityReplayError(
            "replayed lifecycle balance does not equal final PaperBook balance"
        )

    latest_observed_at = (
        max(observed_times, key=lambda item: item[0])[1] if observed_times else None
    )
    if not suffix:
        observed_timestamps_complete = False
        if require_complete_observed_timestamps:
            raise RiskPathEquityReplayError(
                "empty lifecycle suffix has no observed transition timestamp"
            )

    base_sha = _sha256(base_snapshot)
    final_sha = _sha256(final_snapshot)
    source_payload = {
        "schema": "AUTOSPORT_RISK_PATH_EQUITY_REPLAY_V1",
        "base_snapshot_sha256": base_sha,
        "final_snapshot_sha256": final_sha,
        "paper_book_schema_version": base_schema_version,
        "expected_changed_ticket_ids": sorted(expected_changed_ticket_ids),
        "start_balance": _decimal_evidence_text(
            base_book.balance,
            label="start balance",
        ),
        "final_balance": _decimal_evidence_text(
            final_book.balance,
            label="final balance",
        ),
        "minimum_equity": _decimal_evidence_text(
            minimum,
            label="minimum equity",
        ),
        "transition_evidence_sha256": [
            transition.evidence_sha256 for transition in transitions
        ],
        "latest_observed_at": latest_observed_at,
        "observed_timestamps_complete": observed_timestamps_complete,
        "execution_authorized": False,
        "risk_result_authorized": False,
        "causal_authorized": False,
    }

    return RiskPathEquityReplay(
        base_snapshot_sha256=base_sha,
        final_snapshot_sha256=final_sha,
        start_balance=base_book.balance,
        final_balance=final_book.balance,
        minimum_equity=minimum,
        transitions=tuple(transitions),
        latest_observed_at=latest_observed_at,
        observed_timestamps_complete=observed_timestamps_complete,
        source_evidence_sha256=_sha256(_canonical_bytes(source_payload)),
    )
