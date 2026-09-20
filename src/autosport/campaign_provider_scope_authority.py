from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from weakref import ref

from .betfair_account_readonly import (
    ADAPTER_ID as BETFAIR_ADAPTER_ID,
    ADAPTER_VERSION as BETFAIR_ADAPTER_VERSION,
    BetfairReadOnlyClient,
)
from .bookmaker_capability import BookmakerCapabilityProfile
from .campaign_economic_authority import FinalizedCampaignAuthority
from .decision_ledger import JsonlDecisionLedger, MATERIAL_ACTION_ID_PAYLOAD_KEY
from .real_execution_ledger import ExecutionAction, ExecutionPlan, RealExecutionLedger
from .supervised_provider_evidence import (
    ProviderEvidenceError,
    VerifiedProviderEffectEvidence,
    assert_verified_provider_evidence_authoritative,
    verify_betfair_provider_state,
)


SCHEMA_VERSION = 1
_SOURCE_FAMILY = "betfair.authenticated-execution-scope.v1"
_ACCOUNT_PREFIX = "betfair-account-evidence:"


class CampaignProviderScopeError(RuntimeError):
    """Raised when exact campaign-to-provider applicability cannot be proven."""


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise CampaignProviderScopeError(
            "provider scope is not canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: object, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise CampaignProviderScopeError(
            f"{name} must be canonical non-empty text"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise CampaignProviderScopeError(f"{name} must be UTF-8") from exc
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise CampaignProviderScopeError(
            f"{name} must be canonical SHA-256"
        )
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CampaignProviderScopeError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CampaignProviderScopeError(
            f"{name} must be timezone-aware"
        )
    return parsed.astimezone(timezone.utc)


def _canonical_instant(value: object, name: str) -> str:
    return _instant(value, name).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True, weakref_slot=True)
class VerifiedBetfairProviderScopeCapture:
    """Source-issued account + execution readback evidence from one client context."""

    venue_id: str
    client_account_scope: str
    authenticated_account_id: str
    account_details_sha256: str
    adapter_id: str
    adapter_version: str
    action_id: str
    event_id: str
    market_id: str
    provider_order_ref: str | None
    provider_evidence_id: str
    provider_source_sha256: str
    request_scope_sha256: str
    readback_evidence_sha256: str
    quote_observed_at: str
    account_observed_at: str
    readback_observed_at: str
    source_interval_start: str
    source_interval_end: str
    available_at: str
    source_family: str = _SOURCE_FAMILY
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in (
            "venue_id",
            "client_account_scope",
            "authenticated_account_id",
            "adapter_id",
            "adapter_version",
            "action_id",
            "event_id",
            "market_id",
            "provider_evidence_id",
            "source_family",
        ):
            _text(getattr(self, name), name)
        for name in (
            "account_details_sha256",
            "provider_source_sha256",
            "request_scope_sha256",
            "readback_evidence_sha256",
        ):
            _sha(getattr(self, name), name)
        if self.venue_id != "betfair":
            raise CampaignProviderScopeError(
                "authenticated scope supports exact Betfair venue only"
            )
        if (
            self.adapter_id != BETFAIR_ADAPTER_ID
            or self.adapter_version != BETFAIR_ADAPTER_VERSION
        ):
            raise CampaignProviderScopeError(
                "Betfair adapter identity mismatch"
            )
        if (
            self.source_family != _SOURCE_FAMILY
            or self.schema_version != SCHEMA_VERSION
        ):
            raise CampaignProviderScopeError(
                "provider scope capture schema mismatch"
            )
        if (
            self.authenticated_account_id
            != _ACCOUNT_PREFIX + self.account_details_sha256
        ):
            raise CampaignProviderScopeError(
                "authenticated account identity is not source-derived"
            )
        if self.provider_order_ref is not None:
            _text(self.provider_order_ref, "provider_order_ref")
        start = _instant(
            self.source_interval_start, "source_interval_start"
        )
        end = _instant(self.source_interval_end, "source_interval_end")
        available = _instant(self.available_at, "available_at")
        quote = _instant(self.quote_observed_at, "quote_observed_at")
        account = _instant(
            self.account_observed_at, "account_observed_at"
        )
        readback = _instant(
            self.readback_observed_at, "readback_observed_at"
        )
        if start != min(quote, account, readback):
            raise CampaignProviderScopeError(
                "provider scope start is not mechanically derived"
            )
        if end != max(quote, account, readback):
            raise CampaignProviderScopeError(
                "provider scope end is not mechanically derived"
            )
        if available != end:
            raise CampaignProviderScopeError(
                "provider scope availability must equal latest source observation"
            )

    def payload(self) -> dict[str, object]:
        return {
            "schema": "autosport.verified_betfair_provider_scope_capture",
            "schema_version": self.schema_version,
            "source_family": self.source_family,
            "venue_id": self.venue_id,
            "client_account_scope": self.client_account_scope,
            "authenticated_account_id": self.authenticated_account_id,
            "account_details_sha256": self.account_details_sha256,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "action_id": self.action_id,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "provider_order_ref": self.provider_order_ref,
            "provider_evidence_id": self.provider_evidence_id,
            "provider_source_sha256": self.provider_source_sha256,
            "request_scope_sha256": self.request_scope_sha256,
            "readback_evidence_sha256": self.readback_evidence_sha256,
            "quote_observed_at": self.quote_observed_at,
            "account_observed_at": self.account_observed_at,
            "readback_observed_at": self.readback_observed_at,
            "source_interval_start": self.source_interval_start,
            "source_interval_end": self.source_interval_end,
            "available_at": self.available_at,
        }

    @property
    def capture_sha256(self) -> str:
        return _digest(self.payload())


@dataclass(frozen=True, slots=True)
class CampaignProviderScopeProjection:
    """Immutable applicability proof consumed by campaign cost allocation."""

    campaign_id: str
    campaign_version: int
    campaign_sha256: str
    session_id: str
    run_id: str
    session_evidence_id: str
    session_evidence_sha256: str
    run_summary_sha256: str
    decision_id: str
    plan_id: str
    plan_fingerprint: str
    action_id: str
    provider_capture_sha256: str
    provider_evidence_id: str
    provider_source_sha256: str
    venue_id: str
    authenticated_account_id: str
    event_id: str
    market_id: str
    source_interval_start: str
    source_interval_end: str
    observed_at: str
    available_at: str
    schema_version: int = SCHEMA_VERSION

    def payload(self) -> dict[str, object]:
        return {
            "schema": "autosport.campaign_provider_scope_projection",
            "schema_version": self.schema_version,
            "campaign_id": self.campaign_id,
            "campaign_version": self.campaign_version,
            "campaign_sha256": self.campaign_sha256,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "session_evidence_id": self.session_evidence_id,
            "session_evidence_sha256": self.session_evidence_sha256,
            "run_summary_sha256": self.run_summary_sha256,
            "decision_id": self.decision_id,
            "plan_id": self.plan_id,
            "plan_fingerprint": self.plan_fingerprint,
            "action_id": self.action_id,
            "provider_capture_sha256": self.provider_capture_sha256,
            "provider_evidence_id": self.provider_evidence_id,
            "provider_source_sha256": self.provider_source_sha256,
            "venue_id": self.venue_id,
            "authenticated_account_id": self.authenticated_account_id,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "source_interval_start": self.source_interval_start,
            "source_interval_end": self.source_interval_end,
            "observed_at": self.observed_at,
            "available_at": self.available_at,
        }

    @property
    def applicability_digest(self) -> str:
        return _digest(self.payload())

    def assert_provider_key(
        self,
        *,
        venue_id: str,
        authenticated_account_id: str,
        market_id: str,
        event_id: str | None = None,
    ) -> None:
        """Fail closed when a downstream receipt/source key is not this scope."""

        if (
            venue_id != self.venue_id
            or authenticated_account_id != self.authenticated_account_id
            or market_id != self.market_id
            or (event_id is not None and event_id != self.event_id)
        ):
            raise CampaignProviderScopeError(
                "provider receipt/source key is not applicable to campaign"
            )


def _install_capture_authority() -> None:
    issued: dict[int, tuple[object, str]] = {}

    def remember(
        capture: VerifiedBetfairProviderScopeCapture,
    ) -> VerifiedBetfairProviderScopeCapture:
        key = id(capture)

        def forget(
            _weakref: object,
            *,
            capture_key: int = key,
        ) -> None:
            issued.pop(capture_key, None)

        issued[key] = (ref(capture, forget), capture.capture_sha256)
        return capture

    def assert_authoritative(
        capture: VerifiedBetfairProviderScopeCapture,
    ) -> None:
        if not isinstance(capture, VerifiedBetfairProviderScopeCapture):
            raise CampaignProviderScopeError(
                "provider scope capture type is not canonical"
            )
        record = issued.get(id(capture))
        if record is None or record[0]() is not capture:
            raise CampaignProviderScopeError(
                "provider scope capture was not issued by canonical resolver"
            )
        if record[1] != capture.capture_sha256:
            raise CampaignProviderScopeError(
                "provider scope capture changed after source verification"
            )

    globals()["_remember_capture"] = remember
    globals()[
        "assert_provider_scope_capture_authoritative"
    ] = assert_authoritative


_install_capture_authority()
del _install_capture_authority


def capture_betfair_provider_scope(
    client: BetfairReadOnlyClient,
    action: ExecutionAction,
    profile: BookmakerCapabilityProfile,
    *,
    expected_profile_sha256: str,
    provider_order_ref: str | None = None,
) -> VerifiedBetfairProviderScopeCapture:
    """Acquire non-circular account + action + external-market source evidence."""

    if type(client) is not BetfairReadOnlyClient:
        raise CampaignProviderScopeError(
            "provider scope requires canonical BetfairReadOnlyClient"
        )
    if not isinstance(action, ExecutionAction):
        raise CampaignProviderScopeError(
            "provider scope requires canonical ExecutionAction"
        )
    if not isinstance(profile, BookmakerCapabilityProfile):
        raise CampaignProviderScopeError(
            "provider scope requires canonical BookmakerCapabilityProfile"
        )

    account = client.read_account_details()
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
        raise CampaignProviderScopeError(
            "provider scope lacks canonical verified effect evidence"
        ) from exc
    if not isinstance(effect, VerifiedProviderEffectEvidence):
        raise CampaignProviderScopeError(
            "provider scope requires positive authenticated provider effect"
        )

    times = (
        _canonical_instant(
            action.quote_observed_at, "quote_observed_at"
        ),
        _canonical_instant(
            account.evidence.observed_at, "account observed_at"
        ),
        _canonical_instant(
            readback.observed_at, "readback observed_at"
        ),
    )
    parsed = tuple(
        _instant(value, "provider source time") for value in times
    )
    start = times[parsed.index(min(parsed))]
    end = times[parsed.index(max(parsed))]
    capture = VerifiedBetfairProviderScopeCapture(
        venue_id=effect.bookmaker_id,
        client_account_scope=effect.account_id,
        authenticated_account_id=(
            _ACCOUNT_PREFIX + account.evidence.source_payload_sha256
        ),
        account_details_sha256=account.evidence.source_payload_sha256,
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
    return _remember_capture(capture)


def _decision_prefix_records(run_registry, run_id: str):
    summary, summary_sha256 = (
        run_registry.verified_completed_summary_for_run(run_id)
    )
    expected_sha = summary.get("decision_ledger_sha256")
    _sha(expected_sha, "completed run decision_ledger_sha256")
    ledger = JsonlDecisionLedger(
        run_registry.path.parent / "decisions.jsonl"
    )
    snapshot = ledger.verified_snapshot()
    lines = snapshot.payload.splitlines(keepends=True)
    prefix = b""
    record_count: int | None = None
    if expected_sha == hashlib.sha256(b"").hexdigest():
        record_count = 0
    for index, line in enumerate(lines, start=1):
        prefix += line
        if hashlib.sha256(prefix).hexdigest() == expected_sha:
            record_count = index
            break
    if record_count is None:
        raise CampaignProviderScopeError(
            "completed run Decision Ledger prefix is unavailable or drifted"
        )
    records = ledger.verified_records()[:record_count]
    return summary, summary_sha256, records


def _execution_plan_action(
    ledger: RealExecutionLedger,
    *,
    plan_id: str,
    action_id: str,
    attempt_id: str,
) -> tuple[ExecutionPlan, ExecutionAction, dict[str, object]]:
    if not isinstance(ledger, RealExecutionLedger):
        raise CampaignProviderScopeError(
            "execution ledger is not canonical"
        )
    events = ledger._events()
    plan_event, _ = ledger._action_payload(
        events, plan_id, action_id
    )
    plan = ledger._plan_from_dict(plan_event["payload"]["plan"])
    if (
        plan.fingerprint
        != plan_event["payload"].get("plan_fingerprint")
    ):
        raise CampaignProviderScopeError(
            "execution plan fingerprint drifted"
        )
    actions = [
        item for item in plan.actions if item.action_id == action_id
    ]
    if len(actions) != 1:
        raise CampaignProviderScopeError(
            "execution action is not uniquely reserved"
        )
    saga = ledger.saga(plan_id)
    if saga.attempt_action_ids.get(attempt_id) != action_id:
        raise CampaignProviderScopeError(
            "provider evidence attempt does not own execution action"
        )
    return plan, actions[0], plan_event


def resolve_campaign_provider_scope(
    authority: FinalizedCampaignAuthority,
    *,
    session_id: str,
    execution_ledger: RealExecutionLedger,
    plan_id: str,
    attempt_id: str,
    capture: VerifiedBetfairProviderScopeCapture,
    expected_applicability_digest: str | None = None,
) -> CampaignProviderScopeProjection:
    """Return applicability only on exact campaign/execution/provider membership."""

    if not isinstance(authority, FinalizedCampaignAuthority):
        raise CampaignProviderScopeError(
            "campaign authority is not canonical"
        )
    assert_provider_scope_capture_authoritative(capture)
    campaign_projection = authority.projection()
    campaign = authority._campaign
    run_registry = campaign._run_registry
    if run_registry is None:
        raise CampaignProviderScopeError(
            "finalized campaign lost canonical RunRegistry binding"
        )
    sessions = [
        item
        for item in campaign.sessions
        if item.session_id == session_id
    ]
    if len(sessions) != 1:
        raise CampaignProviderScopeError(
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
        raise CampaignProviderScopeError(
            "session is not exact finalized campaign membership"
        )

    summary, run_summary_sha256, records = _decision_prefix_records(
        run_registry, session.run_id
    )
    plan, action, plan_event = _execution_plan_action(
        execution_ledger,
        plan_id=plan_id,
        action_id=capture.action_id,
        attempt_id=attempt_id,
    )
    matches = [
        record
        for record in records
        if record.replay_run_id == session.run_id
        and (
            record.decision_id == plan.decision_id
            or record.payload.get(MATERIAL_ACTION_ID_PAYLOAD_KEY)
            == plan.decision_id
        )
    ]
    if len(matches) != 1:
        raise CampaignProviderScopeError(
            "execution plan lacks exact canonical campaign run decision membership"
        )
    decision = matches[0]

    if (
        action.bookmaker_id != capture.venue_id
        or action.event_id != capture.event_id
        or action.market_id != capture.market_id
        or action.action_id != capture.action_id
    ):
        raise CampaignProviderScopeError(
            "provider capture conflicts with reserved execution action"
        )
    binding = execution_ledger.provider_evidence_binding(
        attempt_id
    )
    if binding is None:
        raise CampaignProviderScopeError(
            "execution attempt lacks durable provider evidence binding"
        )
    if (
        binding.get("evidence_id") != capture.provider_evidence_id
        or _canonical_instant(
            binding.get("observed_at"),
            "provider binding observed_at",
        )
        != _canonical_instant(
            capture.readback_observed_at,
            "readback observed_at",
        )
    ):
        raise CampaignProviderScopeError(
            "durable provider evidence binding drifted"
        )

    observations = tuple(
        _canonical_instant(
            value, "campaign observation timestamp"
        )
        for value in session.observation_timestamps
    )
    quote_at = _canonical_instant(
        action.quote_observed_at, "action quote_observed_at"
    )
    if quote_at not in observations:
        raise CampaignProviderScopeError(
            "execution quote is not exact campaign observation membership"
        )
    window_start = _instant(
        session.evaluation_window_start,
        "evaluation_window_start",
    )
    window_end = _instant(
        session.evaluation_window_end,
        "evaluation_window_end",
    )
    source_start = _instant(
        capture.source_interval_start,
        "source_interval_start",
    )
    source_end = _instant(
        capture.source_interval_end,
        "source_interval_end",
    )
    as_of = _instant(session.as_of, "session as_of")
    if source_start > window_end or source_end < window_start:
        raise CampaignProviderScopeError(
            "provider source interval does not intersect campaign interval"
        )
    if source_end > as_of:
        raise CampaignProviderScopeError(
            "future-available provider evidence cannot backdate campaign scope"
        )
    if _instant(plan.created_at, "plan created_at") > as_of:
        raise CampaignProviderScopeError(
            "execution plan postdates campaign authority"
        )
    if _instant(
        plan_event["recorded_at"],
        "plan recorded_at",
    ) > as_of:
        raise CampaignProviderScopeError(
            "execution reservation postdates campaign authority"
        )

    causal = summary.get("campaign_causal_membership")
    if (
        not isinstance(causal, dict)
        or causal.get("observation_membership_sha256")
        != session.observation_membership_sha256
    ):
        raise CampaignProviderScopeError(
            "campaign causal membership drifted"
        )

    resolved = CampaignProviderScopeProjection(
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
            _sha(
                expected_applicability_digest,
                "expected_applicability_digest",
            )
            != resolved.applicability_digest
        ):
            raise CampaignProviderScopeError(
                "re-resolved provider scope digest drifted"
            )
    return resolved
