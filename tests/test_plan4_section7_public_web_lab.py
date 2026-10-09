"""Plan 4 Section 7: offline public webpage navigation/market compatibility tests."""

from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.bookmaker_public_web_lab import (
    PublicPageFixture,
    PublicWebLabError,
    evaluate_public_web_lab,
)
from autosport.bookmaker_semantic_browser import SemanticBrowserElement

NOW = "2026-10-09T09:00:20+00:00"
BASE = "https://public.example.test"


def _pages():
    landing = PublicPageFixture(
        provider_id="synthetic-public", adapter_version="fixture-v1",
        url=BASE + "/sports", page_kind="LANDING", market_id=None,
        market_status="UNKNOWN", observed_at="2026-10-09T09:00:00+00:00",
        source_payload_sha256="a" * 64,
        elements=(SemanticBrowserElement("link", "Football", True, True),),
    )
    market = PublicPageFixture(
        provider_id="synthetic-public", adapter_version="fixture-v1",
        url=BASE + "/sports/football", page_kind="MARKET",
        market_id="MATCH-1", market_status="OPEN",
        observed_at="2026-10-09T09:00:10+00:00",
        source_payload_sha256="b" * 64,
        elements=(SemanticBrowserElement("button", "Home team", True, True, Decimal("2.50")),),
    )
    return landing, market


def _evaluate(pages=None, **changes):
    args = dict(
        pages=_pages() if pages is None else pages,
        expected_urls=(BASE + "/sports", BASE + "/sports/football"),
        expected_provider="synthetic-public",
        expected_adapter_version="fixture-v1",
        market_id="MATCH-1", target_role="button",
        target_name="Home team", expected_odds=Decimal("2.50"),
        now=NOW, max_age_seconds=120,
    )
    args.update(changes)
    return evaluate_public_web_lab(**args)


def test_recorded_public_navigation_market_read_is_deterministic_and_non_authorizing():
    a = _evaluate()
    b = _evaluate()
    assert a == b
    assert a.status == "OFFLINE_SUPPORTED"
    assert a.reason == "OK"
    assert a.navigation_steps == 2
    assert a.last_market_status == "OPEN"
    assert a.authority == "RECORDED_FIXTURE_ONLY"
    assert len(a.report_sha256) == 64
    assert not hasattr(a, "place_bet")
    assert not hasattr(a, "navigate")
    assert not hasattr(a, "click")


@pytest.mark.parametrize("status", ["SUSPENDED", "CLOSED", "UNKNOWN"])
def test_suspension_closed_unknown_do_not_report_support(status):
    landing, market = _pages()
    report = _evaluate((landing, replace(market, market_status=status)))
    assert report.status == "UNAVAILABLE"
    assert report.reason == "MARKET_NOT_OPEN"
    assert report.last_market_status == status


@pytest.mark.parametrize(("flag", "reason"), [
    ("challenge_detected", "CHALLENGE_OR_SECURITY_CONTROL"),
    ("authentication_required", "AUTHENTICATION_REQUIRED"),
    ("geo_restricted", "GEO_RESTRICTED"),
])
def test_challenge_login_geo_fail_closed_without_bypass(flag, reason):
    landing, market = _pages()
    report = _evaluate((landing, replace(market, **{flag: True})))
    assert (report.status, report.reason) == ("UNAVAILABLE", reason)


def test_semantic_locator_drift_duplicate_hidden_disabled_and_market_price():
    landing, market = _pages()
    modifications = [
        ({"elements": ()}, "SEMANTIC_DRIFT"),
        ({"elements": market.elements * 2}, "SEMANTIC_DRIFT"),
        ({"elements": (replace(market.elements[0], name="Renamed"),)}, "SEMANTIC_DRIFT"),
        ({"elements": (replace(market.elements[0], visible=False),)}, "TARGET_UNAVAILABLE"),
        ({"elements": (replace(market.elements[0], enabled=False),)}, "TARGET_UNAVAILABLE"),
        ({"elements": (replace(market.elements[0], odds=Decimal("2.51")),)}, "MARKET_PRICE_DRIFT"),
    ]
    for change, reason in modifications:
        report = _evaluate((landing, replace(market, **change)))
        assert (report.status, report.reason) == ("UNAVAILABLE", reason)


def test_evidence_revision_and_market_content_change_receipt():
    a, b = _pages()
    original = _evaluate((a, b))
    changed = _evaluate((a, replace(b, source_payload_sha256="c" * 64)))
    assert changed.status == "OFFLINE_SUPPORTED"
    assert original.source_chain_sha256 != changed.source_chain_sha256
    assert original.report_sha256 != changed.report_sha256
    both = _evaluate((replace(a, adapter_version="fixture-v2"),
                      replace(b, adapter_version="fixture-v2")),
                     expected_adapter_version="fixture-v2")
    assert both.status == "OFFLINE_SUPPORTED"
    assert both.report_sha256 != original.report_sha256


@pytest.mark.parametrize("url", [
    "http://public.example.test/sports",
    "https://user:secret@public.example.test/sports",
    "https://public.example.test/sports?token=private",
    "https://public.example.test/sports#login",
    "https://public.example.test:8443/sports",
    "https://public.example.test//sports",
    "https://public.example.test.evil.test/sports",
])
def test_unsafe_redirect_or_navigation_target_rejected_with_no_secret_in_error(url):
    landing, market = _pages()
    with pytest.raises(PublicWebLabError) as captured:
        _evaluate((replace(landing, url=url), market))
    assert "secret" not in str(captured.value)
    assert "private" not in str(captured.value)


def test_navigation_chain_must_match_exact_order_and_origin():
    landing, market = _pages()
    with pytest.raises(PublicWebLabError):
        _evaluate((landing, market), expected_urls=(BASE + "/wrong", BASE + "/sports/football"))
    with pytest.raises(PublicWebLabError):
        _evaluate((market, landing))
    with pytest.raises(PublicWebLabError):
        _evaluate((landing, replace(market, market_id="another-market")))
    with pytest.raises(PublicWebLabError):
        _evaluate((landing, replace(market, provider_id="other-provider")))
    with pytest.raises(PublicWebLabError):
        _evaluate((landing, replace(market, adapter_version="v2")))


def test_time_causality_replay_freshness_and_negative_ingress():
    landing, market = _pages()
    for altered in (
        replace(landing, observed_at="2026-10-09T09:01:00+00:00"),
        replace(landing, observed_at="2026-10-09T08:00:00+00:00"),
    ):
        with pytest.raises(PublicWebLabError):
            _evaluate((altered, market))
    with pytest.raises(PublicWebLabError):
        _evaluate((landing, replace(market, observed_at="2026-10-09T08:59:00+00:00")))
    with pytest.raises(PublicWebLabError):
        _evaluate((landing, market), expected_odds=2.5)
    with pytest.raises(PublicWebLabError):
        _evaluate((landing, market), max_age_seconds=True)
    with pytest.raises(PublicWebLabError):
        _evaluate([landing, market])
    with pytest.raises(PublicWebLabError):
        _evaluate((landing, replace(market, source_payload_sha256="not-a-hash")))
    with pytest.raises(PublicWebLabError):
        _evaluate((landing, replace(market, authentication_required="false")))


def test_bounded_fixture_and_postconstruction_mutation_fail_closed():
    landing, market = _pages()
    with pytest.raises(PublicWebLabError):
        _evaluate((landing,) * 33, expected_urls=(BASE + "/sports",) * 33)
    object.__setattr__(market, "market_status", "FAKE_OPEN")
    with pytest.raises(PublicWebLabError):
        _evaluate((landing, market))
