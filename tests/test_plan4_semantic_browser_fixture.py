"""Plan 4 Section 3: mock-only semantic browser/quote scope falsifiers."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.bookmaker_capability import BookmakerCapabilityProfile
from autosport.bookmaker_integration_boundary import (
    BookmakerIntegrationKind,
    bind_bookmaker_integration,
)
from autosport.bookmaker_semantic_browser import (
    BrowserFixtureSnapshot,
    SemanticBrowserContractError,
    SemanticBrowserElement,
    plan_semantic_browser_selection,
)
from autosport.opportunity import QuoteRef


PROFILE_AT = "2026-10-08T12:00:00+00:00"
INTEGRATION_AT = "2026-10-08T12:00:01+00:00"
QUOTE_AT = "2026-10-08T12:00:02+00:00"
PAGE_AT = "2026-10-08T12:00:03+00:00"
NOW = "2026-10-08T12:00:04+00:00"
LABEL = "Перемога — Дім"


def _fixture():
    profile = BookmakerCapabilityProfile(
        venue_id="fixture-venue",
        account_id="acct-із-пробілами",
        adapter_id="semantic-browser-fixture",
        adapter_version="1",
        profile_version=1,
        facts=(),
        observed_at=PROFILE_AT,
        source_ref="fixture://capability",
        source_payload_sha256="a" * 64,
    )
    integration = bind_bookmaker_integration(
        profile,
        integration_kind=BookmakerIntegrationKind.BROWSER_AUTOMATION,
        observed_at=INTEGRATION_AT,
        source_ref="fixture://integration",
        source_payload_sha256="b" * 64,
    )
    quote = QuoteRef(
        event_id="event-1",
        market_id="winner",
        selection_id="home",
        source_id="fixture-venue",
        sequence=2,
        decimal_odds=Decimal("2.150"),
        observed_ts=QUOTE_AT,
        source_ts=QUOTE_AT,
        ingest_ts=QUOTE_AT,
        market_event_hash="c" * 64,
    )
    snapshot = BrowserFixtureSnapshot(
        venue_id=profile.venue_id,
        account_id=profile.account_id,
        profile_id=profile.profile_id,
        event_id=quote.event_id,
        market_id=quote.market_id,
        selection_id=quote.selection_id,
        status="OPEN",
        observed_at=PAGE_AT,
        source_payload_sha256="d" * 64,
        elements=(SemanticBrowserElement(
            role="button", name=LABEL, visible=True, enabled=True,
            odds=Decimal("2.15"),
        ),),
    )
    return profile, integration, quote, snapshot


def _plan(profile, integration, quote, snapshot, **changes):
    params = dict(role="button", accessible_name=LABEL,
                  now=NOW, max_age_seconds=60)
    params.update(changes)
    return plan_semantic_browser_selection(
        profile, integration, snapshot, quote, **params,
    )


def test_semantic_fixture_binds_exact_role_name_quote_without_ui_effect():
    profile, integration, quote, snapshot = _fixture()
    one = _plan(profile, integration, quote, snapshot)
    two = _plan(profile, integration, quote, snapshot)
    assert one == two
    assert one.authority == "OFFLINE_PROPOSAL_ONLY"
    assert one.selection_role == "button"
    assert one.accessible_name == LABEL
    assert one.profile_id == profile.profile_id
    assert one.page_source_sha256 == "d" * 64
    assert len(one.evidence_sha256) == 64
    assert not hasattr(one, "click")
    assert not hasattr(one, "submit")
    changed = replace(snapshot, source_payload_sha256="e" * 64)
    assert _plan(profile, integration, quote, changed).evidence_sha256 != one.evidence_sha256


@pytest.mark.parametrize(
    "mutation",
    [
        {"status": "SUSPENDED"},
        {"status": "CLOSED"},
        {"status": "UNKNOWN"},
        {"challenge_detected": True},
        {"venue_id": "foreign-venue"},
        {"account_id": "foreign-account"},
        {"event_id": "other-event"},
        {"market_id": "other-market"},
        {"selection_id": "away"},
        {"profile_id": "f" * 64},
        {"observed_at": "2026-10-08T12:00:05+00:00"},
        {"observed_at": "2026-10-08T11:00:00+00:00"},
        {"elements": ()},
        {"elements": (
            SemanticBrowserElement("button", LABEL, True, True, Decimal("2.15")),
            SemanticBrowserElement("button", LABEL, True, True, Decimal("2.15")),
        )},
        {"elements": (SemanticBrowserElement("button", LABEL, False, True, Decimal("2.15")),)},
        {"elements": (SemanticBrowserElement("button", LABEL, True, False, Decimal("2.15")),)},
        {"elements": (SemanticBrowserElement("button", LABEL, True, True, Decimal("2.14")),)},
        {"elements": (SemanticBrowserElement("button", "Інший вибір", True, True, Decimal("2.15")),)},
        {"elements": (SemanticBrowserElement("button", LABEL, True, True, None),)},
    ],
)
def test_semantic_browser_negative_fixture_fails_closed(mutation):
    profile, integration, quote, snapshot = _fixture()
    mutated = replace(snapshot, **mutation)
    with pytest.raises(SemanticBrowserContractError):
        _plan(profile, integration, quote, mutated)


def test_future_quote_and_cross_source_quote_never_resolve_target():
    profile, integration, quote, snapshot = _fixture()
    with pytest.raises(SemanticBrowserContractError):
        _plan(profile, integration, replace(
            quote, observed_ts="2026-10-08T12:00:05+00:00"
        ), snapshot)
    with pytest.raises(SemanticBrowserContractError):
        _plan(profile, integration, replace(
            quote, source_id="foreign-venue"
        ), snapshot)
    with pytest.raises(SemanticBrowserContractError):
        _plan(profile, integration, replace(
            quote, ingest_ts="2026-10-08T12:00:05+00:00"
        ), snapshot)
    with pytest.raises(SemanticBrowserContractError):
        _plan(profile, integration, replace(
            quote, source_ts="2026-10-08T12:00:05+00:00"
        ), snapshot)


def test_official_api_evidence_is_not_browser_permission():
    profile, integration, quote, snapshot = _fixture()
    official = replace(integration, integration_kind=BookmakerIntegrationKind.OFFICIAL_API)
    with pytest.raises(SemanticBrowserContractError, match="not a browser integration"):
        _plan(profile, official, quote, snapshot)


def test_conflicting_profile_invalidates_integration_binding():
    profile, integration, quote, snapshot = _fixture()
    changed_profile = replace(profile, adapter_version="2")
    with pytest.raises(ValueError, match="does not match"):
        _plan(changed_profile, integration, quote, snapshot)


@pytest.mark.parametrize("role,name", [
    ("link", LABEL),
    ("button", "not the accessible name"),
    ("button", ""),
    ("button", " leading-space"),
    ("img", LABEL),
])
def test_nonmatching_or_invalid_semantic_locator_rejected(role, name):
    profile, integration, quote, snapshot = _fixture()
    with pytest.raises(SemanticBrowserContractError):
        _plan(profile, integration, quote, snapshot, role=role, accessible_name=name)


def test_reject_non_decimal_odds_unbounded_age_and_non_boolean_state():
    profile, integration, quote, snapshot = _fixture()
    with pytest.raises(SemanticBrowserContractError):
        SemanticBrowserElement("button", LABEL, True, True, 2.15)
    with pytest.raises(SemanticBrowserContractError):
        SemanticBrowserElement("button", LABEL, 1, True, Decimal("2.15"))
    with pytest.raises(SemanticBrowserContractError):
        _plan(profile, integration, quote, snapshot, max_age_seconds=True)
    with pytest.raises(SemanticBrowserContractError):
        _plan(profile, integration, quote, snapshot, max_age_seconds=0)
    with pytest.raises(SemanticBrowserContractError):
        _plan(profile, integration, quote, snapshot, now="2026-10-08T13:00:00+00:00")
