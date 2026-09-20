"""Stable, secret-free Betfair account identity for campaign/provider applicability.

This module extends the existing campaign-provider scope authority without widening
Autosport's provider surface to writes. It obtains Betfair's authenticated
``getDeveloperAppKeys`` metadata through the canonical live transport and derives an
opaque account discriminator from non-secret provider ids only. Application keys
and session tokens are never placed into authority DTOs, digests, or logs.

It also owns restart-safe re-verification for this provider scope. Campaign decision
facts remain frozen at the session cutoff, while a later authenticated provider
re-read keeps its real later availability timestamp instead of being backdated.
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
from .campaign_economic_authority import FinalizedCampaignAuthority
from .real_execution_ledger import (
    ExecutionAction,
    ExternalReceiptIdentity,
    RealExecutionLedger,
)
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


@dataclass(frozen=True, slots=True)
class _VerifiedEffectIdentity:
    external_receipt_id: str
    selection_id: str
    status: str
    accepted_odds: str
    accepted_stake: str


_CAPTURE_EFFECT_IDENTITIES: WeakKeyDictionary = WeakKeyDictionary()


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
    capture = scope.VerifiedBetfairProviderScopeCapture(
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
    _CAPTURE_EFFECT_IDENTITIES[capture] = _VerifiedEffectIdentity(
        external_receipt_id=effect.external_receipt_id,
        selection_id=effect.selection_id,
        status=effect.status.value,
        accepted_odds=str(effect.accepted_odds),
        accepted_stake=str(effect.accepted_stake),
    )
    return capture


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
        if _CAPTURE_EFFECT_IDENTITIES.get(capture) is None:
            raise scope.CampaignProviderScopeError(
                "provider scope lost canonical provider-effect identity"
            )

    scope.capture_betfair_provider_scope = authoritative_capture
    scope.assert_provider_scope_capture_authoritative = assert_authoritative


_install_capture_authority()
del _install_capture_authority


def _assert_durable_effect_reverification(
    execution_ledger: RealExecutionLedger,
    *,
    plan_id: str,
    attempt_id: str,
    action: ExecutionAction,
    capture: scope.VerifiedBetfairProviderScopeCapture,
    campaign_as_of: datetime,
) -> None:
    """Bind a fresh provider re-read to the same durable external effect."""

    binding = execution_ledger.provider_evidence_binding(attempt_id)
    if binding is None:
        raise scope.CampaignProviderScopeError(
            "execution attempt lacks durable provider evidence binding"
        )
    binding_at = scope._instant(
        binding.get("observed_at"),
        "provider binding observed_at",
    )
    readback_at = scope._instant(
        capture.readback_observed_at,
        "readback observed_at",
    )
    if (
        binding.get("evidence_id") == capture.provider_evidence_id
        and binding_at == readback_at
    ):
        return

    # A changed evidence-instance id is admissible only for a later restart/read.
    # It must resolve the same durable external receipt for the same action. This
    # prevents a fresh provider observation from rewriting historical campaign
    # facts while allowing source authority to be reacquired after process loss.
    if readback_at <= campaign_as_of:
        raise scope.CampaignProviderScopeError(
            "durable provider evidence binding drifted before campaign cutoff"
        )
    if binding_at > readback_at:
        raise scope.CampaignProviderScopeError(
            "provider re-verification predates durable provider evidence"
        )
    effect_identity = _CAPTURE_EFFECT_IDENTITIES.get(capture)
    if effect_identity is None:
        raise scope.CampaignProviderScopeError(
            "provider re-verification lost canonical external receipt identity"
        )
    if effect_identity.selection_id != action.selection_id:
        raise scope.CampaignProviderScopeError(
            "provider re-verification selection conflicts with execution action"
        )
    saga = execution_ledger.saga(plan_id)
    receipt = ExternalReceiptIdentity(
        action.bookmaker_id,
        action.account_id,
        effect_identity.external_receipt_id,
    )
    if saga.receipts.get(receipt) != attempt_id:
        raise scope.CampaignProviderScopeError(
            "provider re-verification external receipt is not durable attempt identity"
        )


def _resolve_campaign_provider_scope_restart_safe(
    authority: FinalizedCampaignAuthority,
    *,
    session_id: str,
    execution_ledger: RealExecutionLedger,
    plan_id: str,
    attempt_id: str,
    capture: scope.VerifiedBetfairProviderScopeCapture,
    expected_applicability_digest: str | None = None,
) -> scope.CampaignProviderScopeProjection:
    """Resolve applicability while separating T0 decision facts from T1 verification."""

    if not isinstance(authority, FinalizedCampaignAuthority):
        raise scope.CampaignProviderScopeError(
            "campaign authority is not canonical"
        )
    scope.assert_provider_scope_capture_authoritative(capture)
    campaign_projection = authority.projection()
    campaign = authority._campaign
    run_registry = campaign._run_registry
    if run_registry is None:
        raise scope.CampaignProviderScopeError(
            "finalized campaign lost canonical RunRegistry binding"
        )
    sessions = [item for item in campaign.sessions if item.session_id == session_id]
    if len(sessions) != 1:
        raise scope.CampaignProviderScopeError(
            "campaign session is not uniquely resolved"
        )
    session = sessions[0]
    session_refs = [
        item
        for item in campaign_projection.session_refs
        if item.evidence_id == session.evidence_id
        and item.evidence_sha256 == session.evidence_sha256
    ]
    if len(session_refs) != 1:
        raise scope.CampaignProviderScopeError(
            "session is not exact finalized campaign membership"
        )

    summary, run_summary_sha256, records = scope._decision_prefix_records(
        run_registry, session.run_id
    )
    plan, action, plan_event = scope._execution_plan_action(
        execution_ledger,
        plan_id=plan_id,
        action_id=capture.action_id,
        attempt_id=attempt_id,
    )
    run_records = [
        record
        for record in records
        if getattr(record, "replay_run_id", None) == session.run_id
    ]
    decision, _portfolio_plan = scope._portfolio_execution_membership(
        run_records,
        plan,
    )

    if (
        action.bookmaker_id != capture.venue_id
        or action.event_id != capture.event_id
        or action.market_id != capture.market_id
        or action.action_id != capture.action_id
    ):
        raise scope.CampaignProviderScopeError(
            "provider capture conflicts with reserved execution action"
        )

    observations = tuple(
        scope._canonical_instant(value, "campaign observation timestamp")
        for value in session.observation_timestamps
    )
    quote_at = scope._canonical_instant(
        action.quote_observed_at,
        "action quote_observed_at",
    )
    if quote_at not in observations:
        raise scope.CampaignProviderScopeError(
            "execution quote is not exact campaign observation membership"
        )

    window_start = scope._instant(
        session.evaluation_window_start,
        "evaluation_window_start",
    )
    window_end = scope._instant(
        session.evaluation_window_end,
        "evaluation_window_end",
    )
    source_start = scope._instant(
        capture.source_interval_start,
        "source_interval_start",
    )
    source_end = scope._instant(
        capture.source_interval_end,
        "source_interval_end",
    )
    as_of = scope._instant(session.as_of, "session as_of")
    if source_start > window_end or source_end < window_start:
        raise scope.CampaignProviderScopeError(
            "provider source interval does not intersect campaign interval"
        )

    # Decision-causing facts are frozen at T0. Provider identity/readback may be
    # reacquired later at T1 and must retain that later availability truth.
    if scope._instant(action.quote_observed_at, "action quote_observed_at") > as_of:
        raise scope.CampaignProviderScopeError(
            "execution quote postdates campaign authority"
        )
    if scope._instant(plan.created_at, "plan created_at") > as_of:
        raise scope.CampaignProviderScopeError(
            "execution plan postdates campaign authority"
        )
    if scope._instant(plan_event["recorded_at"], "plan recorded_at") > as_of:
        raise scope.CampaignProviderScopeError(
            "execution reservation postdates campaign authority"
        )
    if scope._instant(
        getattr(decision, "observed_ts", None),
        "portfolio decision observed_ts",
    ) > as_of:
        raise scope.CampaignProviderScopeError(
            "portfolio decision postdates campaign authority"
        )
    if any(
        scope._instant(value, "campaign observation timestamp") > as_of
        for value in session.observation_timestamps
    ):
        raise scope.CampaignProviderScopeError(
            "campaign observation postdates campaign authority"
        )

    _assert_durable_effect_reverification(
        execution_ledger,
        plan_id=plan_id,
        attempt_id=attempt_id,
        action=action,
        capture=capture,
        campaign_as_of=as_of,
    )

    causal = summary.get("campaign_causal_membership")
    if (
        not isinstance(causal, dict)
        or causal.get("observation_membership_sha256")
        != session.observation_membership_sha256
    ):
        raise scope.CampaignProviderScopeError(
            "campaign causal membership drifted"
        )

    resolved = scope.CampaignProviderScopeProjection(
        campaign_id=campaign_projection.campaign_id,
        campaign_version=campaign_projection.campaign_version,
        campaign_sha256=campaign_projection.campaign_sha256,
        session_id=session.session_id,
        run_id=session.run_id,
        session_evidence_id=session.evidence_id,
        session_evidence_sha256=session.evidence_sha256,
        run_summary_sha256=run_summary_sha256,
        decision_id=decision.decision_id,
        plan_id=plan.plan_id,
        plan_fingerprint=plan.fingerprint,
        action_id=action.action_id,
        provider_capture_sha256=capture.capture_sha256,
        provider_evidence_id=capture.provider_evidence_id,
        provider_source_sha256=capture.provider_source_sha256,
        venue_id=capture.venue_id,
        authenticated_account_id=capture.authenticated_account_id,
        event_id=capture.event_id,
        market_id=capture.market_id,
        source_interval_start=capture.source_interval_start,
        source_interval_end=capture.source_interval_end,
        observed_at=capture.readback_observed_at,
        available_at=capture.available_at,
    )
    if expected_applicability_digest is not None:
        if (
            scope._sha(
                expected_applicability_digest,
                "expected_applicability_digest",
            )
            != resolved.applicability_digest
        ):
            raise scope.CampaignProviderScopeError(
                "re-resolved provider scope digest drifted"
            )
    return resolved


def _install_restart_safe_projection_authority() -> None:
    issued: dict[int, tuple[object, str]] = {}

    def authoritative_resolve(
        authority: FinalizedCampaignAuthority,
        *,
        session_id: str,
        execution_ledger: RealExecutionLedger,
        plan_id: str,
        attempt_id: str,
        capture: scope.VerifiedBetfairProviderScopeCapture,
        expected_applicability_digest: str | None = None,
    ) -> scope.CampaignProviderScopeProjection:
        projection = _resolve_campaign_provider_scope_restart_safe(
            authority,
            session_id=session_id,
            execution_ledger=execution_ledger,
            plan_id=plan_id,
            attempt_id=attempt_id,
            capture=capture,
            expected_applicability_digest=expected_applicability_digest,
        )
        key = id(projection)

        def forget(_weakref: object, *, projection_key: int = key) -> None:
            issued.pop(projection_key, None)

        issued[key] = (
            ref(projection, forget),
            projection.applicability_digest,
        )
        return projection

    def assert_authoritative(
        projection: scope.CampaignProviderScopeProjection,
    ) -> None:
        if not isinstance(projection, scope.CampaignProviderScopeProjection):
            raise scope.CampaignProviderScopeError(
                "campaign provider scope projection type is not canonical"
            )
        record = issued.get(id(projection))
        if record is None or record[0]() is not projection:
            raise scope.CampaignProviderScopeError(
                "campaign provider scope projection was not issued by canonical resolver"
            )
        if record[1] != projection.applicability_digest:
            raise scope.CampaignProviderScopeError(
                "campaign provider scope projection changed after resolution"
            )

    scope.resolve_campaign_provider_scope = authoritative_resolve
    scope.assert_campaign_provider_scope_authoritative = assert_authoritative


_install_restart_safe_projection_authority()
del _install_restart_safe_projection_authority
