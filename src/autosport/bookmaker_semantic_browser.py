"""Offline semantic-browser control proposal for sanctioned-provider integration.

This module does not navigate, authenticate, click, submit bets or prove a site's
terms. It binds accessible role/name/state and a provider-scoped quote into a
revalidatable *proposal*. Live site/permission and irreversible dispatch authority
remain external to this fixture-only boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
from typing import Callable

from .bookmaker_capability import BookmakerCapabilityProfile
from .bookmaker_integration_boundary import (
    BookmakerIntegrationEvidence,
    BookmakerIntegrationKind,
)
from .opportunity import QuoteRef


class SemanticBrowserContractError(ValueError):
    """Malformed, stale or ambiguous semantic UI fixture."""


def _text(value: object, field: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
    ):
        raise SemanticBrowserContractError(
            f"{field} must be non-empty accessible text"
        )
    try:
        value.encode("utf-8", "strict")
    except UnicodeError as exc:
        raise SemanticBrowserContractError(
            f"{field} must be UTF-8 accessible text"
        ) from exc
    return value


def _instant(value: object, field: str) -> datetime:
    raw = _text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SemanticBrowserContractError(
            f"{field} must be ISO-8601 with an offset"
        ) from exc
    if parsed.utcoffset() is None:
        raise SemanticBrowserContractError(
            f"{field} must have a timezone offset"
        )
    return parsed


def _digest(value: object, field: str) -> str:
    raw = _text(value, field)
    if len(raw) != 64 or any(ch not in "0123456789abcdef" for ch in raw):
        raise SemanticBrowserContractError(
            f"{field} must be a lowercase SHA-256 hex digest"
        )
    return raw


def _exact_odds(value: object, field: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite() or value <= 1:
        raise SemanticBrowserContractError(
            f"{field} must be exact finite Decimal odds greater than 1"
        )
    return value


@dataclass(frozen=True, slots=True)
class SemanticBrowserElement:
    """Accessible UI Automation/ARIA locator; never pixel/DOM-index based."""

    role: str
    name: str
    visible: bool
    enabled: bool
    odds: Decimal | None = None

    def __post_init__(self) -> None:
        if _text(self.role, "role") not in {"button", "radio", "link"}:
            raise SemanticBrowserContractError(
                "selection requires semantic button/radio/link role"
            )
        _text(self.name, "accessible_name")
        if type(self.visible) is not bool or type(self.enabled) is not bool:
            raise SemanticBrowserContractError(
                "visible/enabled must be explicit booleans"
            )
        if self.odds is not None:
            _exact_odds(self.odds, "element odds")


@dataclass(frozen=True, slots=True)
class BrowserFixtureSnapshot:
    """Source-scoped observation, not provider-authenticated account truth."""

    venue_id: str
    account_id: str
    profile_id: str
    event_id: str
    market_id: str
    selection_id: str
    status: str
    observed_at: str
    source_payload_sha256: str
    elements: tuple[SemanticBrowserElement, ...]
    challenge_detected: bool = False

    def __post_init__(self) -> None:
        for field in ("venue_id", "account_id", "event_id", "market_id", "selection_id"):
            _text(getattr(self, field), field)
        _digest(self.profile_id, "profile_id")
        _digest(self.source_payload_sha256, "source_payload_sha256")
        _instant(self.observed_at, "observed_at")
        if self.status not in {"OPEN", "SUSPENDED", "CLOSED", "UNKNOWN"}:
            raise SemanticBrowserContractError("unsupported market status")
        if type(self.elements) is not tuple or any(
            type(item) is not SemanticBrowserElement for item in self.elements
        ):
            raise SemanticBrowserContractError(
                "elements must be an immutable tuple of semantic elements"
            )
        for item in self.elements:
            SemanticBrowserElement.__post_init__(item)
        if type(self.challenge_detected) is not bool:
            raise SemanticBrowserContractError(
                "challenge_detected must be boolean"
            )


@dataclass(frozen=True, slots=True)
class BrowserSelectionProposal:
    """Read-only, deterministic fixture evidence. Never execution authority."""

    profile_id: str
    quote_hash: str
    page_source_sha256: str
    selection_role: str
    accessible_name: str
    observed_at: str
    evidence_sha256: str
    authority: str = "OFFLINE_PROPOSAL_ONLY"


def plan_semantic_browser_selection(
    profile: BookmakerCapabilityProfile,
    integration: BookmakerIntegrationEvidence,
    snapshot: BrowserFixtureSnapshot,
    quote: QuoteRef,
    *,
    role: str,
    accessible_name: str,
    now: str,
    max_age_seconds: int,
) -> BrowserSelectionProposal:
    """Resolve one exact visible/enabled semantic target; cause zero UI effects.

    A fresh caller-supplied fixture can support tests, but never proves that a
    site legally permits automation or that provider odds/order were accepted.
    An adapter applying a proposal MUST reobserve and independently authorize
    the real action; this function intentionally exposes no click/submit method.
    """

    if type(profile) is not BookmakerCapabilityProfile:
        raise SemanticBrowserContractError("canonical profile is required")
    if type(integration) is not BookmakerIntegrationEvidence:
        raise SemanticBrowserContractError("canonical integration evidence required")
    integration.verify_profile(profile)
    if integration.integration_kind is not BookmakerIntegrationKind.BROWSER_AUTOMATION:
        raise SemanticBrowserContractError("not a browser integration")
    if type(snapshot) is not BrowserFixtureSnapshot:
        raise SemanticBrowserContractError("canonical snapshot required")
    BrowserFixtureSnapshot.__post_init__(snapshot)
    if type(quote) is not QuoteRef:
        raise SemanticBrowserContractError("canonical QuoteRef required")
    QuoteRef.__post_init__(quote)
    if type(max_age_seconds) is not int or max_age_seconds <= 0:
        raise SemanticBrowserContractError("max_age_seconds must be positive integer")
    if (
        snapshot.venue_id != profile.venue_id
        or snapshot.account_id != profile.account_id
        or snapshot.profile_id != profile.profile_id
        or quote.source_id != profile.venue_id
        or snapshot.event_id != quote.event_id
        or snapshot.market_id != quote.market_id
        or snapshot.selection_id != quote.selection_id
    ):
        raise SemanticBrowserContractError("provider/account/quote identity drift")
    if snapshot.challenge_detected:
        raise SemanticBrowserContractError(
            "browser challenge/security control encountered; unavailable"
        )
    if snapshot.status != "OPEN":
        raise SemanticBrowserContractError("market is not unambiguously open")
    observed = _instant(snapshot.observed_at, "observed_at")
    current = _instant(now, "now")
    quote_time = _instant(quote.observed_ts, "quote.observed_ts")
    if (
        observed > current
        or quote_time > observed
        or (current - observed).total_seconds() > max_age_seconds
        or (current - quote_time).total_seconds() > max_age_seconds
    ):
        raise SemanticBrowserContractError("stale or future browser/quote observation")
    target_role = _text(role, "role")
    target_name = _text(accessible_name, "accessible_name")
    if target_role not in {"button", "radio", "link"}:
        raise SemanticBrowserContractError("unsupported semantic target role")
    matching = tuple(
        item for item in snapshot.elements
        if item.role == target_role and item.name == target_name
    )
    if len(matching) != 1:
        raise SemanticBrowserContractError(
            "missing or ambiguous role/name target"
        )
    target = matching[0]
    if not target.visible or not target.enabled:
        raise SemanticBrowserContractError("target is hidden or disabled")
    if target.odds is None or target.odds != _exact_odds(
        quote.decimal_odds, "quote odds"
    ):
        raise SemanticBrowserContractError(
            "observed semantic selection odds drift"
        )
    quote_json = json.dumps(
        QuoteRef.to_dict(quote), sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, allow_nan=False,
    ).encode("utf-8")
    quote_hash = sha256(quote_json).hexdigest()
    payload = {
        "authority": "OFFLINE_PROPOSAL_ONLY",
        "account_id": profile.account_id,
        "accessible_name": target_name,
        "observed_at": snapshot.observed_at,
        "page_source_sha256": snapshot.source_payload_sha256,
        "profile_id": profile.profile_id,
        "quote_hash": quote_hash,
        "selection_role": target_role,
    }
    evidence_sha = sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    return BrowserSelectionProposal(
        profile_id=profile.profile_id,
        quote_hash=quote_hash,
        page_source_sha256=snapshot.source_payload_sha256,
        selection_role=target_role,
        accessible_name=target_name,
        observed_at=snapshot.observed_at,
        evidence_sha256=evidence_sha,
    )
