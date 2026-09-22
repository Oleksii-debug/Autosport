from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .domain import MarketEvent, PaperTicket, TicketStatus
from .market_mirror import MarketMirror, MirrorSnapshot
from .paper import PaperBook
from .portfolio_live_scenario import (
    FrozenLiveScenarioSpec,
    LivePortfolioScenarioError,
    LivePortfolioScenarioEvidence,
    LivePositionScenarioComponent,
    evaluate_live_portfolio_scenario,
)
from .risk import PaperRiskPolicy


SCHEMA = "autosport.paper-live-portfolio-universe.v1"
_CANONICAL_ACTIVE_VIEW_FOR_KEYS = MarketMirror.active_view_for_keys


class PaperLivePortfolioUniverseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PaperLivePortfolioUniverseEvidence:
    schema: str
    paper_risk_state_sha256: str
    product_portfolio_state_sha256: str
    generation: int
    mirror_revision: int
    position_ids: tuple[str, ...]
    structural_evidence: LivePortfolioScenarioEvidence
    evidence_sha256: str

    def __post_init__(self) -> None:
        if self.schema != SCHEMA:
            raise PaperLivePortfolioUniverseError("product-universe schema mismatch")
        if type(self.structural_evidence) is not LivePortfolioScenarioEvidence:
            raise PaperLivePortfolioUniverseError(
                "structural_evidence must be exact LivePortfolioScenarioEvidence"
            )
        if self.structural_evidence.portfolio_state_sha256 != self.product_portfolio_state_sha256:
            raise PaperLivePortfolioUniverseError("structural portfolio state mismatch")
        if self.structural_evidence.generation != self.generation:
            raise PaperLivePortfolioUniverseError("structural generation mismatch")
        if self.structural_evidence.position_count != len(self.position_ids):
            raise PaperLivePortfolioUniverseError("structural position count mismatch")
        if self.structural_evidence.source_authority_proven is not False:
            raise PaperLivePortfolioUniverseError("source authority must remain false")
        for name in (
            "paper_risk_state_sha256",
            "product_portfolio_state_sha256",
            "evidence_sha256",
        ):
            raw = getattr(self, name)
            if (
                type(raw) is not str
                or len(raw) != 64
                or raw != raw.lower()
                or any(ch not in "0123456789abcdef" for ch in raw)
            ):
                raise PaperLivePortfolioUniverseError(f"{name} must be lowercase SHA-256")
        if type(self.generation) is not int or self.generation <= 0:
            raise PaperLivePortfolioUniverseError("generation must be positive exact integer")
        if type(self.mirror_revision) is not int or self.mirror_revision < 0:
            raise PaperLivePortfolioUniverseError(
                "mirror_revision must be non-negative exact integer"
            )
        if (
            type(self.position_ids) is not tuple
            or self.position_ids != tuple(sorted(self.position_ids))
            or len(self.position_ids) != len(set(self.position_ids))
        ):
            raise PaperLivePortfolioUniverseError(
                "position_ids must be sorted unique tuple"
            )


def _digest(value: object) -> str:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise PaperLivePortfolioUniverseError(
            "product-universe evidence is not canonical JSON"
        ) from exc
    return hashlib.sha256(raw).hexdigest()


def _utc(value: datetime, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise PaperLivePortfolioUniverseError(f"{name} must be timezone-aware exact datetime")
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat(timespec="microseconds").replace("+00:00", "Z")


def _instant(value: object, name: str) -> datetime:
    if type(value) is not str or not value or value != value.strip():
        raise PaperLivePortfolioUniverseError(f"{name} must be canonical timestamp text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PaperLivePortfolioUniverseError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PaperLivePortfolioUniverseError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc)


def _ticket_payload(ticket: PaperTicket) -> dict[str, object]:
    return {
        "ticket_id": ticket.ticket_id,
        "stake": str(ticket.stake),
        "placed_at": ticket.placed_at,
        "status": ticket.status.value,
        "provider_source_ids": list(ticket.provider_source_ids),
        "provider_accounts": [
            {"source_id": source_id, "account_id": account_id}
            for source_id, account_id in ticket.provider_accounts
        ],
        "bankroll_id": ticket.bankroll_id,
        "currency": ticket.currency,
        "legs": [
            {
                "event_id": leg.event_id,
                "market_id": leg.market_id,
                "selection_id": leg.selection_id,
                "sport": leg.sport,
                "locked_odds": str(leg.locked_odds),
                "quote_key": leg.quote_key,
            }
            for leg in ticket.legs
        ],
    }


def _product_state(
    book: PaperBook,
) -> tuple[str, int, tuple[PaperTicket, ...], str]:
    try:
        PaperBook._validate_loaded_state(book)
    except (ArithmeticError, AttributeError, TypeError, ValueError) as exc:
        raise PaperLivePortfolioUniverseError(
            "PaperBook is not canonical validated portfolio state"
        ) from exc
    risk_sha = PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book)
    if risk_sha is None:
        raise PaperLivePortfolioUniverseError("PAPER risk state digest unavailable")
    if type(book._lifecycle) is not list:
        raise PaperLivePortfolioUniverseError("PAPER lifecycle generation unavailable")
    generation = len(book._lifecycle) + 1
    tickets = tuple(
        book.tickets[ticket_id]
        for ticket_id in sorted(book.tickets)
        if book.tickets[ticket_id].status is TicketStatus.OPEN
    )
    state_sha = _digest(
        {
            "schema": "autosport.paper-live-product-state.v1",
            "paper_risk_state_sha256": risk_sha,
            "generation": generation,
            "open_positions": [_ticket_payload(ticket) for ticket in tickets],
        }
    )
    return risk_sha, generation, tickets, state_sha


def _binding(ticket: PaperTicket) -> tuple[str, str, str]:
    if len(ticket.legs) != 1:
        raise PaperLivePortfolioUniverseError(
            "PAPER live universe supports only single-leg open tickets; "
            "joint multi-leg stake cannot be duplicated or arbitrarily allocated"
        )
    if len(ticket.provider_source_ids) != 1:
        raise PaperLivePortfolioUniverseError(
            "open ticket requires exactly one canonical provider source"
        )
    if len(ticket.provider_accounts) != 1:
        raise PaperLivePortfolioUniverseError(
            "open ticket requires exactly one canonical provider account"
        )
    source_id = ticket.provider_source_ids[0]
    account_source, account_id = ticket.provider_accounts[0]
    if account_source != source_id:
        raise PaperLivePortfolioUniverseError("provider account source mismatch")
    if (
        type(ticket.bankroll_id) is not str
        or not ticket.bankroll_id
        or ticket.bankroll_id != ticket.bankroll_id.strip()
        or type(ticket.currency) is not str
        or not ticket.currency
        or ticket.currency != ticket.currency.strip()
    ):
        raise PaperLivePortfolioUniverseError(
            "open ticket requires canonical bankroll_id and currency"
        )
    return source_id, account_id, ticket.legs[0].quote_key


def resolve_paper_live_portfolio_universe(
    book: PaperBook,
    mirror: MarketMirror,
    *,
    as_of: datetime,
    max_age: timedelta,
    max_observation_skew_microseconds: int,
) -> PaperLivePortfolioUniverseEvidence:
    if type(book) is not PaperBook:
        raise TypeError("book must be exact PaperBook")
    if type(mirror) is not MarketMirror:
        raise TypeError("mirror must be exact MarketMirror")
    boundary = _utc(as_of, "as_of")
    if type(max_age) is not timedelta or max_age < timedelta(0):
        raise PaperLivePortfolioUniverseError("max_age must be non-negative exact timedelta")
    if (
        type(max_observation_skew_microseconds) is not int
        or max_observation_skew_microseconds < 0
    ):
        raise PaperLivePortfolioUniverseError(
            "max_observation_skew_microseconds must be non-negative exact integer"
        )

    risk_sha, generation, tickets, state_sha = _product_state(book)
    bindings: dict[str, tuple[PaperTicket, str, str, str]] = {}
    keys: set[tuple[str, str]] = set()
    for ticket in tickets:
        source_id, account_id, quote_key = _binding(ticket)
        bindings[ticket.ticket_id] = (ticket, source_id, account_id, quote_key)
        keys.add((source_id, quote_key))

    snapshot = _CANONICAL_ACTIVE_VIEW_FOR_KEYS(
        mirror,
        keys,
        as_of=boundary,
        max_age=max_age,
    )
    if type(snapshot) is not MirrorSnapshot:
        raise PaperLivePortfolioUniverseError("canonical mirror returned invalid snapshot")
    event_by_key = {(event.source_id, event.quote_key): event for event in snapshot.events}
    if set(event_by_key) != keys:
        missing = tuple(sorted(keys - set(event_by_key)))
        extra = tuple(sorted(set(event_by_key) - keys))
        raise PaperLivePortfolioUniverseError(
            f"coherent mirror cut does not exactly cover PAPER positions: "
            f"missing={missing!r}, unexpected={extra!r}"
        )

    opened = boundary - max_age
    provisional: list[LivePositionScenarioComponent] = []
    for position_id in sorted(bindings):
        ticket, source_id, account_id, quote_key = bindings[position_id]
        event = event_by_key[(source_id, quote_key)]
        if type(event) is not MarketEvent:
            raise PaperLivePortfolioUniverseError("mirror contains non-canonical event")
        leg = ticket.legs[0]
        if (
            event.event_id != leg.event_id
            or event.market_id != leg.market_id
            or event.selection_id != leg.selection_id
            or event.sport != leg.sport
        ):
            raise PaperLivePortfolioUniverseError("mirror identity mismatches PAPER position")
        observed_text = event.source_ts or event.observed_ts
        observed = _instant(observed_text, "market observation")
        committed = _instant(event.ingest_ts, "market ingest")
        if committed < observed:
            raise PaperLivePortfolioUniverseError("market ingest precedes selected observation")
        if committed > boundary:
            raise PaperLivePortfolioUniverseError("market ingest is later than decision boundary")
        position_sha = _digest(
            {
                "schema": "autosport.paper-live-position.v1",
                "product_portfolio_state_sha256": state_sha,
                "ticket": _ticket_payload(ticket),
                "market_event": event.to_dict(),
            }
        )
        provisional.append(
            LivePositionScenarioComponent(
                position_id=position_id,
                event_id=leg.event_id,
                market_id=leg.market_id,
                provider_id=source_id,
                account_id=account_id,
                batch_id="pending",
                portfolio_state_sha256=state_sha,
                generation=generation,
                position_state_sha256=position_sha,
                observed_at=observed_text,
                committed_at=event.ingest_ts,
                capital_at_risk=ticket.stake,
                conservative_loss_upper_bound=ticket.stake,
            )
        )

    position_ids = tuple(sorted(bindings))
    age_us = (
        max_age.days * 86_400_000_000
        + max_age.seconds * 1_000_000
        + max_age.microseconds
    )
    batch_id = _digest(
        {
            "schema": "autosport.paper-live-portfolio-batch.v1",
            "product_portfolio_state_sha256": state_sha,
            "generation": generation,
            "mirror_revision": snapshot.revision,
            "position_ids": list(position_ids),
            "batch_opened_at": _utc_text(opened),
            "batch_closed_at": _utc_text(boundary),
            "max_age_microseconds": age_us,
            "max_observation_skew_microseconds": max_observation_skew_microseconds,
        }
    )
    components = tuple(
        LivePositionScenarioComponent(
            position_id=item.position_id,
            event_id=item.event_id,
            market_id=item.market_id,
            provider_id=item.provider_id,
            account_id=item.account_id,
            batch_id=batch_id,
            portfolio_state_sha256=item.portfolio_state_sha256,
            generation=item.generation,
            position_state_sha256=item.position_state_sha256,
            observed_at=item.observed_at,
            committed_at=item.committed_at,
            capital_at_risk=item.capital_at_risk,
            conservative_loss_upper_bound=item.conservative_loss_upper_bound,
        )
        for item in provisional
    )
    scenario_id = _digest(
        {
            "schema": "autosport.paper-live-portfolio-scenario-id.v1",
            "batch_id": batch_id,
            "position_state_sha256": [item.position_state_sha256 for item in components],
        }
    )
    spec = FrozenLiveScenarioSpec(
        scenario_id=scenario_id,
        batch_id=batch_id,
        portfolio_state_sha256=state_sha,
        generation=generation,
        expected_position_ids=position_ids,
        batch_opened_at=_utc_text(opened),
        batch_closed_at=_utc_text(boundary),
        max_observation_skew_microseconds=max_observation_skew_microseconds,
    )
    try:
        structural = evaluate_live_portfolio_scenario(spec, components)
    except LivePortfolioScenarioError as exc:
        raise PaperLivePortfolioUniverseError("structural scenario evaluation failed") from exc

    risk_after, generation_after, tickets_after, state_after = _product_state(book)
    if (
        risk_after != risk_sha
        or generation_after != generation
        or state_after != state_sha
        or tuple(ticket.ticket_id for ticket in tickets_after) != position_ids
    ):
        raise PaperLivePortfolioUniverseError(
            "PAPER portfolio state changed during universe projection"
        )

    payload = {
        "schema": SCHEMA,
        "paper_risk_state_sha256": risk_sha,
        "product_portfolio_state_sha256": state_sha,
        "generation": generation,
        "mirror_revision": snapshot.revision,
        "position_ids": list(position_ids),
        "structural_evidence_sha256": structural.evidence_sha256,
        "portfolio_universe_authoritative_on_reverification": True,
        "source_authority_proven": False,
        "cross_provider_atomicity_proven": False,
        "terminal_outcome_exactness_proven": False,
        "grants_sizing_authority": False,
        "grants_execution_authority": False,
        "grants_settlement_authority": False,
    }
    return PaperLivePortfolioUniverseEvidence(
        schema=SCHEMA,
        paper_risk_state_sha256=risk_sha,
        product_portfolio_state_sha256=state_sha,
        generation=generation,
        mirror_revision=snapshot.revision,
        position_ids=position_ids,
        structural_evidence=structural,
        evidence_sha256=_digest(payload),
    )


def verify_paper_live_portfolio_universe(
    evidence: PaperLivePortfolioUniverseEvidence,
    book: PaperBook,
    mirror: MarketMirror,
    *,
    as_of: datetime,
    max_age: timedelta,
    max_observation_skew_microseconds: int,
) -> bool:
    if type(evidence) is not PaperLivePortfolioUniverseEvidence:
        return False
    try:
        current = resolve_paper_live_portfolio_universe(
            book,
            mirror,
            as_of=as_of,
            max_age=max_age,
            max_observation_skew_microseconds=max_observation_skew_microseconds,
        )
    except (PaperLivePortfolioUniverseError, LivePortfolioScenarioError, TypeError, ValueError):
        return False
    return current == evidence
