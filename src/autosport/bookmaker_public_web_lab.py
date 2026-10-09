"""Recorded public bookmaker page-compatibility lab; no live browser or network actions.

A fixture sequence proves only that a *recorded* permitted/public navigation
path can be interpreted using accessible roles and names. It cannot establish
site permission, authentication, order acceptance, or money authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
import re
from urllib.parse import urlsplit

from .bookmaker_semantic_browser import SemanticBrowserElement


class PublicWebLabError(ValueError):
    """Malformed, untrusted or cross-origin recorded page evidence."""


def _text(value: object) -> str:
    if (type(value) is not str or not value or value != value.strip()
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
        raise PublicWebLabError("invalid public fixture text")
    try:
        value.encode("utf-8", "strict")
    except UnicodeError as exc:
        raise PublicWebLabError("invalid public fixture text") from exc
    return value


def _instant(value: object) -> datetime:
    raw = _text(value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PublicWebLabError("invalid recorded timestamp") from exc
    if parsed.utcoffset() is None:
        raise PublicWebLabError("recorded timestamp lacks timezone")
    return parsed


def _digest(value: object) -> str:
    raw = _text(value)
    if len(raw) != 64 or any(ch not in "0123456789abcdef" for ch in raw):
        raise PublicWebLabError("invalid source digest")
    return raw


def _url(value: object) -> tuple[str, str]:
    raw = _text(value)
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise PublicWebLabError("invalid public navigation URL") from exc
    if (parsed.scheme != "https" or not host or port is not None
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.netloc != host
            or not re.fullmatch(r"[a-z0-9-]+(?:\\.[a-z0-9-]+)+", host)
            or not parsed.path.startswith("/") or "//" in parsed.path
            or "\\\\" in parsed.path):
        raise PublicWebLabError("unsafe or non-public navigation URL")
    return host, raw


@dataclass(frozen=True, slots=True)
class PublicPageFixture:
    """Recorded observation; not an executable browser instruction."""

    provider_id: str
    adapter_version: str
    url: str
    page_kind: str
    market_id: str | None
    market_status: str
    observed_at: str
    source_payload_sha256: str
    elements: tuple[SemanticBrowserElement, ...]
    challenge_detected: bool = False
    authentication_required: bool = False
    geo_restricted: bool = False

    def __post_init__(self) -> None:
        if type(self) is not PublicPageFixture:
            raise PublicWebLabError("exact page fixture required")
        _text(self.provider_id)
        _text(self.adapter_version)
        _url(self.url)
        if self.page_kind not in ("LANDING", "MARKET"):
            raise PublicWebLabError("unknown page kind")
        if self.page_kind == "MARKET":
            _text(self.market_id)
        elif self.market_id is not None:
            raise PublicWebLabError("landing page cannot assert a market")
        if self.market_status not in ("OPEN", "SUSPENDED", "CLOSED", "UNKNOWN"):
            raise PublicWebLabError("unknown market status")
        _instant(self.observed_at)
        _digest(self.source_payload_sha256)
        if (type(self.elements) is not tuple or len(self.elements) > 256
                or any(type(el) is not SemanticBrowserElement for el in self.elements)):
            raise PublicWebLabError("invalid bounded semantic elements")
        for el in self.elements:
            SemanticBrowserElement.__post_init__(el)
        if any(type(getattr(self, field)) is not bool for field in (
            "challenge_detected", "authentication_required", "geo_restricted"
        )):
            raise PublicWebLabError("security flags must be explicit booleans")


@dataclass(frozen=True, slots=True)
class PublicWebLabReport:
    status: str
    reason: str
    provider_id: str
    adapter_version: str
    last_market_status: str
    navigation_steps: int
    source_chain_sha256: str
    report_sha256: str
    authority: str = "RECORDED_FIXTURE_ONLY"


def evaluate_public_web_lab(
    pages: tuple[PublicPageFixture, ...],
    *,
    expected_urls: tuple[str, ...],
    expected_provider: str,
    expected_adapter_version: str,
    market_id: str,
    target_role: str,
    target_name: str,
    expected_odds: Decimal,
    now: str,
    max_age_seconds: int,
) -> PublicWebLabReport:
    """Evaluate public recorded navigation+market+semantic compatibility.

    Caller supplied URLs/fixtures NEVER trigger navigation, authentication or
    a bet. UNKNOWN, challenges, stale observations and semantic drift fail
    closed. This report is not evidence of actual website compatibility.
    """

    if (type(pages) is not tuple or not 1 <= len(pages) <= 32
            or type(expected_urls) is not tuple or len(expected_urls) != len(pages)):
        raise PublicWebLabError("invalid bounded navigation chain")
    provider = _text(expected_provider)
    version = _text(expected_adapter_version)
    market = _text(market_id)
    role = _text(target_role)
    name = _text(target_name)
    if role not in ("button", "radio", "link"):
        raise PublicWebLabError("unsupported accessible target role")
    if (type(expected_odds) is not Decimal or not expected_odds.is_finite()
            or expected_odds <= 1):
        raise PublicWebLabError("exact positive Decimal odds required")
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 86400:
        raise PublicWebLabError("invalid bounded freshness window")

    current = _instant(now)
    origin = None
    previous_time = None
    serialized = []
    reason = "OK"
    for index, page in enumerate(pages):
        if type(page) is not PublicPageFixture:
            raise PublicWebLabError("exact page fixture required")
        PublicPageFixture.__post_init__(page)
        page_origin, url = _url(page.url)
        expected_origin, expected_url = _url(expected_urls[index])
        if (url != expected_url or page_origin != expected_origin
                or (origin is not None and origin != page_origin)):
            raise PublicWebLabError("navigation redirect or origin drift")
        origin = page_origin
        if page.provider_id != provider or page.adapter_version != version:
            raise PublicWebLabError("provider or adapter revision drift")
        observed = _instant(page.observed_at)
        if (observed > current or (previous_time is not None and observed < previous_time)
                or (current - observed).total_seconds() > max_age_seconds):
            raise PublicWebLabError("stale/future/out-of-order page observation")
        previous_time = observed
        if index < len(pages) - 1 and page.page_kind != "LANDING":
            raise PublicWebLabError("intermediate page cannot assert market")
        if index == len(pages) - 1 and (
            page.page_kind != "MARKET" or page.market_id != market
        ):
            raise PublicWebLabError("terminal market navigation mismatch")
        if page.challenge_detected:
            reason = "CHALLENGE_OR_SECURITY_CONTROL"
        elif page.authentication_required and reason == "OK":
            reason = "AUTHENTICATION_REQUIRED"
        elif page.geo_restricted and reason == "OK":
            reason = "GEO_RESTRICTED"
        serialized.append({
            "adapter_version": page.adapter_version,
            "observed_at": page.observed_at,
            "page_kind": page.page_kind,
            "market_id": page.market_id,
            "market_status": page.market_status,
            "source_payload_sha256": page.source_payload_sha256,
            "url": url,
            "elements": [
                {
                    "role": el.role, "name": el.name, "visible": el.visible,
                    "enabled": el.enabled,
                    "odds": None if el.odds is None else str(el.odds),
                } for el in page.elements
            ],
            "challenge_detected": page.challenge_detected,
            "authentication_required": page.authentication_required,
            "geo_restricted": page.geo_restricted,
        })

    final = pages[-1]
    if final.market_status != "OPEN" and reason == "OK":
        reason = "MARKET_NOT_OPEN"
    if reason == "OK":
        matches = [
            el for el in final.elements if el.role == role and el.name == name
        ]
        if len(matches) != 1:
            reason = "SEMANTIC_DRIFT"
        elif not matches[0].visible or not matches[0].enabled:
            reason = "TARGET_UNAVAILABLE"
        elif matches[0].odds != expected_odds:
            reason = "MARKET_PRICE_DRIFT"

    source_chain = sha256(json.dumps(
        serialized, sort_keys=True, ensure_ascii=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()
    status = "OFFLINE_SUPPORTED" if reason == "OK" else "UNAVAILABLE"
    report_payload = {
        "schema": "autosport.public_web_lab",
        "schema_version": 1,
        "authority": "RECORDED_FIXTURE_ONLY",
        "status": status,
        "reason": reason,
        "provider_id": provider,
        "adapter_version": version,
        "market_id": market,
        "target_role": role,
        "target_name": name,
        "expected_odds": str(expected_odds),
        "last_market_status": final.market_status,
        "navigation_steps": len(pages),
        "source_chain_sha256": source_chain,
    }
    receipt = sha256(json.dumps(
        report_payload, sort_keys=True, ensure_ascii=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()
    return PublicWebLabReport(
        status=status, reason=reason, provider_id=provider,
        adapter_version=version, last_market_status=final.market_status,
        navigation_steps=len(pages), source_chain_sha256=source_chain,
        report_sha256=receipt,
    )
