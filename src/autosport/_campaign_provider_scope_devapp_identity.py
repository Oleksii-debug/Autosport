"""Stable, secret-free Betfair account identity for campaign/provider applicability.

This module extends the existing campaign-provider scope authority without widening
Autosport's provider surface to writes. It obtains Betfair's authenticated
``getDeveloperAppKeys`` metadata through the canonical live transport and derives an
opaque account discriminator from non-secret provider ids only. Application keys
and session tokens are never placed into authority DTOs, digests, or logs.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from weakref import WeakKeyDictionary, ref

from . import campaign_provider_scope_authority as scope
from .betfair_account_readonly import (
    ACCOUNT_JSON_RPC_ENDPOINT,
    BetfairReadOnlyClient,
    UrllibBetfairHttpTransport,
)
from .bookmaker_capability import BookmakerCapabilityProfile
from .real_execution_ledger import ExecutionAction
from .supervised_provider_evidence import (
    ProviderEvidenceError,
    VerifiedProviderEffectEvidence,
    assert_verified_provider_evidence_authoritative,
    verify_betfair_provider_state,
)

_GET_DEVELOPER_APP_KEYS = "AccountAPING/v1.0/getDeveloperAppKeys"
_IDENTITY_SCHEMA = "autosport.betfair_developer_app_account_identity"
_IDENTITY_SCHEMA_VERSION = 1
_MAX_IDENTITY_RESPONSE_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class _DeveloperAppIdentity:
    account_identity_sha256: str
    observed_at: str


@dataclass(frozen=True, slots=True)
class _CanonicalClientOrigin:
    transport: object
    clock: object
    credentials: object


def _install_client_origin_registry() -> WeakKeyDictionary:
    """Record only clients created with the product-owned live transport/clock."""

    origins: WeakKeyDictionary = WeakKeyDictionary()
    raw_init = BetfairReadOnlyClient.__init__
    if getattr(raw_init, "_autosport_devapp_origin_registry", False):
        existing = getattr(raw_init, "_autosport_devapp_origins", None)
        if isinstance(existing, WeakKeyDictionary):
            return existing

    def authoritative_init(
        self: BetfairReadOnlyClient,
        credentials,
        *,
        transport=None,
        timeout_seconds: float = 10.0,
        clock=None,
        venue_id: str = "betfair",
        account_id: str = "default-account",
    ) -> None:
        production_origin = transport is None and clock is None
        raw_init(
            self,
            credentials,
            transport=transport,
            timeout_seconds=timeout_seconds,
            clock=clock,
            venue_id=venue_id,
            account_id=account_id,
        )
        if production_origin:
            origins[self] = _CanonicalClientOrigin(
                self._transport,
                self._clock,
                self._credentials,
            )

    authoritative_init._autosport_devapp_origin_registry = True  # type: ignore[attr-defined]
    authoritative_init._autosport_devapp_origins = origins  # type: ignore[attr-defined]
    BetfairReadOnlyClient.__init__ = authoritative_init
    return origins


_CANONICAL_CLIENT_ORIGINS = _install_client_origin_registry()


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise scope.CampaignProviderScopeError(
            "developer-app identity is not canonical JSON"
        ) from exc


def _decode_provider_json(payload: bytes) -> object:
    if not isinstance(payload, bytes):
        raise scope.CampaignProviderScopeError(
            "Betfair developer-app transport must return bytes"
        )
    if len(payload) > _MAX_IDENTITY_RESPONSE_BYTES:
        raise scope.CampaignProviderScopeError(
            "Betfair developer-app response exceeded the identity size limit"
        )
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise scope.CampaignProviderScopeError(
            "Betfair developer-app response is not UTF-8 JSON"
        ) from exc

    def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise scope.CampaignProviderScopeError(
                    "Betfair developer-app response has duplicate JSON keys"
                )
            result[key] = value
        return result

    try:
        return json.loads(text, object_pairs_hook=no_duplicates)
    except scope.CampaignProviderScopeError:
        raise
    except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
        raise scope.CampaignProviderScopeError(
            "Betfair developer-app response is not valid JSON"
        ) from exc


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise scope.CampaignProviderScopeError(
            f"Betfair developer-app {name} is not a positive integer"
        )
    return value


def _nonempty_secret(value: object, name: str) -> None:
    if type(value) is not str or not value or value != value.strip():
        raise scope.CampaignProviderScopeError(
            f"Betfair developer-app {name} is unavailable"
        )


def _read_developer_account_identity(
    client: BetfairReadOnlyClient,
) -> _DeveloperAppIdentity:
    """Read a stable personal-account discriminator from canonical Betfair origin."""

    if type(client) is not BetfairReadOnlyClient:
        raise scope.CampaignProviderScopeError(
            "stable authenticated Betfair account identity is unavailable from non-canonical client"
        )
    origin = _CANONICAL_CLIENT_ORIGINS.get(client)
    if (
        origin is None
        or client._transport is not origin.transport
        or client._clock is not origin.clock
        or client._credentials is not origin.credentials
        or type(client._transport) is not UrllibBetfairHttpTransport
    ):
        raise scope.CampaignProviderScopeError(
            "stable authenticated Betfair account identity is unavailable from "
            "injected/non-canonical provider origin"
        )

    request_id = client._next_request_id()
    body = _canonical_json(
        {
            "jsonrpc": "2.0",
            "method": _GET_DEVELOPER_APP_KEYS,
            "params": {},
            "id": request_id,
        }
    )
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Authentication": client._credentials.session_token,
    }
    try:
        payload = client._transport.post(
            ACCOUNT_JSON_RPC_ENDPOINT,
            headers=headers,
            body=body,
            timeout_seconds=client._timeout_seconds,
        )
    except Exception:
        raise scope.CampaignProviderScopeError(
            "Betfair developer-app identity request failed"
        ) from None

    decoded = _decode_provider_json(payload)
    if type(decoded) is not dict:
        raise scope.CampaignProviderScopeError(
            "Betfair developer-app response envelope is not an object"
        )
    if decoded.get("jsonrpc") != "2.0" or decoded.get("id") != request_id:
        raise scope.CampaignProviderScopeError(
            "Betfair developer-app response does not match the request"
        )
    if "error" in decoded:
        raise scope.CampaignProviderScopeError(
            "Betfair developer-app response reported an error"
        )
    apps = decoded.get("result")
    if type(apps) is not list or len(apps) != 1:
        raise scope.CampaignProviderScopeError(
            "stable authenticated Betfair account identity requires exactly one "
            "personal developer app"
        )
    app = apps[0]
    if type(app) is not dict:
        raise scope.CampaignProviderScopeError(
            "Betfair developer-app result is not canonical"
        )
    app_id = _positive_int(app.get("appId"), "appId")
    versions = app.get("appVersions")
    if type(versions) is not list or not versions:
        raise scope.CampaignProviderScopeError(
            "Betfair developer-app versions are unavailable"
        )

    version_ids: list[int] = []
    for item in versions:
        if type(item) is not dict:
            raise scope.CampaignProviderScopeError(
                "Betfair developer-app version is not canonical"
            )
        version_id = _positive_int(item.get("versionId"), "versionId")
        if item.get("ownerManaged") is not False:
            raise scope.CampaignProviderScopeError(
                "Betfair developer-app identity is not a personal developer account"
            )
        _nonempty_secret(item.get("applicationKey"), "applicationKey")
        version = item.get("version")
        if type(version) is not str or not version or version != version.strip():
            raise scope.CampaignProviderScopeError(
                "Betfair developer-app version label is unavailable"
            )
        version_ids.append(version_id)
    if len(set(version_ids)) != len(version_ids):
        raise scope.CampaignProviderScopeError(
            "Betfair developer-app version identity is duplicated"
        )

    identity_payload = {
        "schema": _IDENTITY_SCHEMA,
        "schema_version": _IDENTITY_SCHEMA_VERSION,
        "app_id": app_id,
        "version_ids": sorted(version_ids),
    }
    account_identity_sha256 = hashlib.sha256(
        _canonical_json(identity_payload)
    ).hexdigest()
    observed_at = datetime.now(timezone.utc).isoformat()
    return _DeveloperAppIdentity(account_identity_sha256, observed_at)


def _capture_provider_scope_raw(
    client: BetfairReadOnlyClient,
    action: ExecutionAction,
    profile: BookmakerCapabilityProfile,
    *,
    expected_profile_sha256: str,
    provider_order_ref: str | None = None,
) -> scope.VerifiedBetfairProviderScopeCapture:
    if type(client) is not BetfairReadOnlyClient:
        raise scope.CampaignProviderScopeError(
            "provider scope requires canonical BetfairReadOnlyClient"
        )
    if not isinstance(action, ExecutionAction):
        raise scope.CampaignProviderScopeError(
            "provider scope requires canonical ExecutionAction"
        )
    if not isinstance(profile, BookmakerCapabilityProfile):
        raise scope.CampaignProviderScopeError(
            "provider scope requires canonical BookmakerCapabilityProfile"
        )
    scope._sha(expected_profile_sha256, "expected_profile_sha256")
    if provider_order_ref is not None:
        scope._text(provider_order_ref, "provider_order_ref")

    identity = _read_developer_account_identity(client)
    readback = client.read_execution_readback(
        action_id=action.action_id,
        market_id=action.market_id,
        provider_order_ref=provider_order_ref,
    )
    try:
        effect = verify_betfair_provider_state(
            action,
            profile,
            expected_profile_sha256=expected_profile_sha256,
            readback=readback,
            expected_provider_order_ref=provider_order_ref,
        )
        assert_verified_provider_evidence_authoritative(effect)
    except ProviderEvidenceError as exc:
        raise scope.CampaignProviderScopeError(
            "provider scope lacks canonical verified effect evidence"
        ) from exc
    if not isinstance(effect, VerifiedProviderEffectEvidence):
        raise scope.CampaignProviderScopeError(
            "provider scope requires positive authenticated provider effect"
        )

    times = (
        scope._canonical_instant(action.quote_observed_at, "quote_observed_at"),
        scope._canonical_instant(identity.observed_at, "account observed_at"),
        scope._canonical_instant(readback.observed_at, "readback observed_at"),
    )
    parsed = tuple(scope._instant(value, "provider source time") for value in times)
    start = times[parsed.index(min(parsed))]
    end = times[parsed.index(max(parsed))]
    return scope.VerifiedBetfairProviderScopeCapture(
        venue_id=effect.bookmaker_id,
        client_account_scope=effect.account_id,
        authenticated_account_id=(
            scope._ACCOUNT_PREFIX + identity.account_identity_sha256
        ),
        account_details_sha256=identity.account_identity_sha256,
        adapter_id=effect.adapter_id,
        adapter_version=effect.adapter_version,
        action_id=effect.action_id,
        event_id=effect.event_id,
        market_id=effect.market_id,
        provider_order_ref=effect.provider_order_ref,
        provider_evidence_id=effect.evidence_id,
        provider_source_sha256=effect.source_payload_sha256,
        request_scope_sha256=readback.request_scope_sha256,
        readback_evidence_sha256=readback.evidence_sha256,
        quote_observed_at=times[0],
        account_observed_at=times[1],
        readback_observed_at=times[2],
        source_interval_start=start,
        source_interval_end=end,
        available_at=end,
    )


def _install_capture_authority() -> None:
    issued: dict[int, tuple[object, str]] = {}

    def authoritative_capture(
        client: BetfairReadOnlyClient,
        action: ExecutionAction,
        profile: BookmakerCapabilityProfile,
        *,
        expected_profile_sha256: str,
        provider_order_ref: str | None = None,
    ) -> scope.VerifiedBetfairProviderScopeCapture:
        capture = _capture_provider_scope_raw(
            client,
            action,
            profile,
            expected_profile_sha256=expected_profile_sha256,
            provider_order_ref=provider_order_ref,
        )
        key = id(capture)

        def forget(_weakref: object, *, capture_key: int = key) -> None:
            issued.pop(capture_key, None)

        issued[key] = (ref(capture, forget), capture.capture_sha256)
        return capture

    def assert_authoritative(
        capture: scope.VerifiedBetfairProviderScopeCapture,
    ) -> None:
        if not isinstance(capture, scope.VerifiedBetfairProviderScopeCapture):
            raise scope.CampaignProviderScopeError(
                "provider scope capture type is not canonical"
            )
        record = issued.get(id(capture))
        if record is None or record[0]() is not capture:
            raise scope.CampaignProviderScopeError(
                "provider scope capture was not issued by canonical resolver; "
                "stable authenticated Betfair account identity is unavailable "
                "for caller-constructed capture"
            )
        if record[1] != capture.capture_sha256:
            raise scope.CampaignProviderScopeError(
                "provider scope capture changed after source verification"
            )

    scope.capture_betfair_provider_scope = authoritative_capture
    scope.assert_provider_scope_capture_authoritative = assert_authoritative


_install_capture_authority()
del _install_capture_authority
