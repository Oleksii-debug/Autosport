from __future__ import annotations

"""Bind one campaign-gated provider capture to the exact durable forward universe.

This module is composition only. It does not own a scheduler, provider session,
collector chronology, universe store, or observation ledger. Positive authority is
re-resolved from those existing product-owned authorities every time.
"""

import hashlib
import inspect
import json
from dataclasses import dataclass
from typing import Sequence

from . import provider_evaluation_universe as _provider_universe_module
from ._forward_universe_backing_guard import (
    ForwardUniverseBackingGuardError,
    load_guarded_provider_evaluation_universe,
)
from .campaign_inception import (
    CampaignInceptionSourceSpec,
    establish_campaign_inception,
)
from .campaign_provider_cycle_capture import (
    ARTIFACT_KIND,
    CampaignCompleteBoardCycleReceipt,
)
from .causal_collector import CollectorDeltaStore
from .evaluation_universe import EvaluationUniverseLedger
from .event_lifecycle import ContinuousEventLifecycle
from .forward_evaluation_universe_binding import (
    ForwardEvaluationUniverseBindingError,
    ForwardUniverseAuthorityIdentity,
    authorize_forward_source_receipts,
    resolve_forward_universe_authority_identity,
)
from .forward_evidence_completeness import (
    AuthoritativeSourceReceipt,
    ForwardEvidenceProtocolEnvelope,
    ForwardOpportunityEnvelope,
)
from .forward_universe_precommit_authority import ForwardUniversePrecommitLocator
from .provider_evaluation_universe import (
    ProviderEvaluationUniverseStore,
    _ISSUED_UNIVERSES,
    build_frozen_universe_from_complete_game_board,
)
from .provider_observation_authority import (
    CompleteGameBoardEvidenceStore,
    CompleteGameBoardSnapshot,
)


_SCHEMA_VERSION = 1
_DOMAIN = "autosport.campaign-forward-universe-cycle-authority.v1"
_HEX = frozenset("0123456789abcdef")

_ESTABLISH_CAMPAIGN = establish_campaign_inception
_RESOLVE_ARTIFACT = CollectorDeltaStore.collector_cycle_observation_artifact_evidence
_LOAD_PROVIDER_EVIDENCE = CompleteGameBoardEvidenceStore.load
_GUARDED_UNIVERSE_LOAD = load_guarded_provider_evaluation_universe
_REBUILD_PROVIDER_UNIVERSE = build_frozen_universe_from_complete_game_board
_RESOLVE_FORWARD_IDENTITY = resolve_forward_universe_authority_identity
_AUTHORIZE_FORWARD_RECEIPTS = authorize_forward_source_receipts

_CAPTURED_CALLABLES = (
    ("_ESTABLISH_CAMPAIGN", _ESTABLISH_CAMPAIGN, _ESTABLISH_CAMPAIGN.__code__),
    ("_RESOLVE_ARTIFACT", _RESOLVE_ARTIFACT, _RESOLVE_ARTIFACT.__code__),
    (
        "_LOAD_PROVIDER_EVIDENCE",
        _LOAD_PROVIDER_EVIDENCE,
        _LOAD_PROVIDER_EVIDENCE.__code__,
    ),
    (
        "_GUARDED_UNIVERSE_LOAD",
        _GUARDED_UNIVERSE_LOAD,
        _GUARDED_UNIVERSE_LOAD.__code__,
    ),
    (
        "_REBUILD_PROVIDER_UNIVERSE",
        _REBUILD_PROVIDER_UNIVERSE,
        _REBUILD_PROVIDER_UNIVERSE.__code__,
    ),
    (
        "_RESOLVE_FORWARD_IDENTITY",
        _RESOLVE_FORWARD_IDENTITY,
        _RESOLVE_FORWARD_IDENTITY.__code__,
    ),
    (
        "_AUTHORIZE_FORWARD_RECEIPTS",
        _AUTHORIZE_FORWARD_RECEIPTS,
        _AUTHORIZE_FORWARD_RECEIPTS.__code__,
    ),
)
_CANONICAL_COLLECTOR_ARTIFACT_RESOLVER = inspect.getattr_static(
    CollectorDeltaStore,
    "collector_cycle_observation_artifact_evidence",
)
_CANONICAL_PROVIDER_EVIDENCE_LOADER = inspect.getattr_static(
    CompleteGameBoardEvidenceStore,
    "load",
)
_CANONICAL_ISSUED_UNIVERSES = _ISSUED_UNIVERSES
_PROVIDER_UNIVERSE_VALUES = {
    "_ISSUED_UNIVERSES": _provider_universe_module._ISSUED_UNIVERSES,
    "EvaluationRow": _provider_universe_module.EvaluationRow,
    "ObservationIntakeSnapshot": _provider_universe_module.ObservationIntakeSnapshot,
    "EvaluationUniverse": _provider_universe_module.EvaluationUniverse,
    "CompleteGameBoardSnapshot": _provider_universe_module.CompleteGameBoardSnapshot,
    "ContinuousEventLifecycle": _provider_universe_module.ContinuousEventLifecycle,
    "CompleteBoardMemberSpec": _provider_universe_module.CompleteBoardMemberSpec,
    "SlotState": _provider_universe_module.SlotState,
    "_CONSUMER_KIND": _provider_universe_module._CONSUMER_KIND,
    "_PROVIDER_ID": _provider_universe_module._PROVIDER_ID,
}
_PROVIDER_UNIVERSE_CALLABLES = tuple(
    (
        name,
        target,
        getattr(target, "__code__", None),
    )
    for name, target in (
        (
            "assert_complete_game_board_authoritative",
            _provider_universe_module.assert_complete_game_board_authoritative,
        ),
        (
            "complete_game_board_member_specs",
            _provider_universe_module.complete_game_board_member_specs,
        ),
        (
            "_resolve_event_reveal_binding",
            _provider_universe_module._resolve_event_reveal_binding,
        ),
        ("_selection_labels", _provider_universe_module._selection_labels),
        (
            "_validate_row_against_member",
            _provider_universe_module._validate_row_against_member,
        ),
        (
            "_row_semantic_sha256",
            _provider_universe_module._row_semantic_sha256,
        ),
        ("_remember_issued", _provider_universe_module._remember_issued),
        ("_text", _provider_universe_module._text),
        ("_sha", _provider_universe_module._sha),
        ("_instant", _provider_universe_module._instant),
        ("_digest", _provider_universe_module._digest),
    )
)
_INTERNAL_CALLABLES: tuple[tuple[str, object, object], ...] = ()


class CampaignForwardUniverseCycleBindingError(RuntimeError):
    """Campaign/cycle/provider/universe authority does not compose exactly."""


def _text(value: object, field: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise CampaignForwardUniverseCycleBindingError(
            f"{field} must be non-empty canonical text"
        )
    value.encode("utf-8")
    return value


def _sha(value: object, field: str) -> str:
    raw = _text(value, field)
    if (
        len(raw) != 64
        or raw != raw.lower()
        or any(character not in _HEX for character in raw)
    ):
        raise CampaignForwardUniverseCycleBindingError(
            f"{field} must be lowercase SHA-256 hex"
        )
    return raw


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True, init=False)
class CampaignForwardUniverseCycleAuthority:
    """Resolver-issued proof that one #1185 universe uses one gated provider artifact."""

    schema_version: int
    campaign_id: str
    source_id: str
    cycle_receipt_sha256: str
    campaign_receipt_sha256: str
    provider_evidence_sha256: str
    provider_frame_sha256: str
    collector_artifact_evidence_sha256: str
    precommit_authority_sha256: str
    backing_locator_sha256: str
    universe_sha256: str
    membership_sha256: str
    member_count: int
    authority_sha256: str

    def __new__(cls, *args: object, **kwargs: object):
        raise TypeError(
            "CampaignForwardUniverseCycleAuthority is resolver-issued; "
            "call resolve_campaign_forward_universe_cycle_authority"
        )

    @classmethod
    def _issue(
        cls,
        *,
        campaign_id: str,
        source_id: str,
        cycle_receipt_sha256: str,
        campaign_receipt_sha256: str,
        provider_evidence_sha256: str,
        provider_frame_sha256: str,
        collector_artifact_evidence_sha256: str,
        forward_identity: ForwardUniverseAuthorityIdentity,
    ) -> "CampaignForwardUniverseCycleAuthority":
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "campaign_id": _text(campaign_id, "campaign_id"),
            "source_id": _text(source_id, "source_id"),
            "cycle_receipt_sha256": _sha(
                cycle_receipt_sha256,
                "cycle_receipt_sha256",
            ),
            "campaign_receipt_sha256": _sha(
                campaign_receipt_sha256,
                "campaign_receipt_sha256",
            ),
            "provider_evidence_sha256": _sha(
                provider_evidence_sha256,
                "provider_evidence_sha256",
            ),
            "provider_frame_sha256": _sha(
                provider_frame_sha256,
                "provider_frame_sha256",
            ),
            "collector_artifact_evidence_sha256": _sha(
                collector_artifact_evidence_sha256,
                "collector_artifact_evidence_sha256",
            ),
            "precommit_authority_sha256": _sha(
                forward_identity.precommit_authority_sha256,
                "precommit_authority_sha256",
            ),
            "backing_locator_sha256": _sha(
                forward_identity.backing_locator_sha256,
                "backing_locator_sha256",
            ),
            "universe_sha256": _sha(
                forward_identity.universe_sha256,
                "universe_sha256",
            ),
            "membership_sha256": _sha(
                forward_identity.membership_sha256,
                "membership_sha256",
            ),
            "member_count": forward_identity.member_count,
        }
        if type(payload["member_count"]) is not int or payload["member_count"] <= 0:
            raise CampaignForwardUniverseCycleBindingError(
                "member_count must be a positive integer"
            )
        payload["authority_sha256"] = _digest(
            {"domain": _DOMAIN, "authority": payload}
        )
        instance = object.__new__(cls)
        for name in cls.__dataclass_fields__:
            object.__setattr__(instance, name, payload[name])
        return instance


def _require_dispatch_integrity() -> None:
    module_globals = globals()
    if (
        module_globals.get("_provider_universe_module")
        is not _provider_universe_module
        or _provider_universe_module._ISSUED_UNIVERSES
        is not _CANONICAL_ISSUED_UNIVERSES
        or module_globals.get("_ISSUED_UNIVERSES")
        is not _CANONICAL_ISSUED_UNIVERSES
    ):
        raise CampaignForwardUniverseCycleBindingError(
            "provider-universe issued-object cleanup authority changed"
        )
    for name, expected in _PROVIDER_UNIVERSE_VALUES.items():
        if getattr(_provider_universe_module, name, None) is not expected:
            raise CampaignForwardUniverseCycleBindingError(
                "provider-universe transitive authority is rebound: " + name
            )
    for name, expected, code in _PROVIDER_UNIVERSE_CALLABLES:
        if getattr(_provider_universe_module, name, None) is not expected:
            raise CampaignForwardUniverseCycleBindingError(
                "provider-universe transitive dispatch is rebound: " + name
            )
        if getattr(expected, "__code__", None) is not code:
            raise CampaignForwardUniverseCycleBindingError(
                "provider-universe transitive dispatch code changed: " + name
            )
    for name, expected, code in _INTERNAL_CALLABLES:
        if module_globals.get(name) is not expected:
            raise CampaignForwardUniverseCycleBindingError(
                "campaign forward-cycle internal dispatch is rebound: " + name
            )
        if getattr(expected, "__code__", None) is not code:
            raise CampaignForwardUniverseCycleBindingError(
                "campaign forward-cycle internal dispatch code changed: " + name
            )
    if (
        inspect.getattr_static(
            CollectorDeltaStore,
            "collector_cycle_observation_artifact_evidence",
        )
        is not _CANONICAL_COLLECTOR_ARTIFACT_RESOLVER
    ):
        raise CampaignForwardUniverseCycleBindingError(
            "collector artifact resolver authority changed"
        )
    if (
        inspect.getattr_static(CompleteGameBoardEvidenceStore, "load")
        is not _CANONICAL_PROVIDER_EVIDENCE_LOADER
    ):
        raise CampaignForwardUniverseCycleBindingError(
            "provider evidence load authority changed"
        )
    for name, expected, code in _CAPTURED_CALLABLES:
        if module_globals.get(name) is not expected:
            raise CampaignForwardUniverseCycleBindingError(
                "campaign forward-cycle dispatch authority is rebound: " + name
            )
        if getattr(expected, "__code__", None) is not code:
            raise CampaignForwardUniverseCycleBindingError(
                "campaign forward-cycle dispatch code changed: " + name
            )


def _expected_cycle_receipt_payload(
    *,
    campaign,
    snapshot: CompleteGameBoardSnapshot,
    collector_evidence: dict[str, object],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "campaign_id": campaign.campaign_id,
        "campaign_receipt_sha256": campaign.receipt_sha256,
        "campaign_authority_record_sha256": campaign.authority_record_sha256,
        "source_id": collector_evidence["source_id"],
        "run_id": collector_evidence["run_id"],
        "stream_epoch": collector_evidence["stream_epoch"],
        "schedule_id": collector_evidence["schedule_id"],
        "gate_binding_sha256": collector_evidence["gate_binding_sha256"],
        "cycle_seq": collector_evidence["cycle_seq"],
        "slot_ordinal": collector_evidence["slot_ordinal"],
        "artifact_id": collector_evidence["artifact_id"],
        "artifact_kind": collector_evidence["artifact_kind"],
        "provider_evidence_sha256": snapshot.evidence_sha256,
        "provider_frame_sha256": snapshot.frame_sha256,
        "provider_captured_at": snapshot.captured_at,
        "collector_artifact_evidence_sha256": collector_evidence["evidence_sha256"],
    }
    payload["receipt_sha256"] = _digest(payload)
    return payload


def _assert_cycle_receipt_exact(
    receipt: CampaignCompleteBoardCycleReceipt,
    expected: dict[str, object],
) -> None:
    if type(receipt) is not CampaignCompleteBoardCycleReceipt:
        raise TypeError(
            "cycle_receipt must be exact CampaignCompleteBoardCycleReceipt"
        )
    for name in CampaignCompleteBoardCycleReceipt.__dataclass_fields__:
        if getattr(receipt, name) != expected.get(name):
            raise CampaignForwardUniverseCycleBindingError(
                "cycle receipt does not re-resolve from campaign/provider/collector "
                f"authority: {name}"
            )


def _rebuild_exact_provider_universe(
    *,
    ledger: EvaluationUniverseLedger,
    snapshot: CompleteGameBoardSnapshot,
    event_lifecycle: ContinuousEventLifecycle | None,
):
    universe = ledger.universe
    rebuilt = None
    try:
        rebuilt = _REBUILD_PROVIDER_UNIVERSE(
            snapshot=snapshot,
            event_lifecycle=event_lifecycle,
            authority_id=universe.intake_snapshot.authority_id,
            session_id=universe.intake_snapshot.session_id,
            universe_id=universe.universe_id,
            campaign_id=universe.campaign_id,
            research_protocol_id=universe.research_protocol_id,
            protocol_sha256=universe.protocol_sha256,
            evaluation_not_before=universe.intake_snapshot.evaluation_not_before,
            frozen_at=universe.frozen_at,
            rows=universe.rows,
        )
        if (
            rebuilt.universe_sha256 != universe.universe_sha256
            or rebuilt.membership_sha256 != universe.membership_sha256
            or rebuilt.intake_snapshot.snapshot_sha256
            != universe.intake_snapshot.snapshot_sha256
        ):
            raise CampaignForwardUniverseCycleBindingError(
                "durable forward universe is not derived from the exact "
                "campaign-bound provider snapshot"
            )
        return universe
    finally:
        if rebuilt is not None:
            _CANONICAL_ISSUED_UNIVERSES.pop(id(rebuilt), None)


def resolve_campaign_forward_universe_cycle_authority(
    *,
    precommit_locator: ForwardUniversePrecommitLocator,
    collector_store: CollectorDeltaStore,
    source_spec: CampaignInceptionSourceSpec,
    cycle_receipt: CampaignCompleteBoardCycleReceipt,
    provider_evidence_store: CompleteGameBoardEvidenceStore,
    universe_store: ProviderEvaluationUniverseStore,
    protocol: ForwardEvidenceProtocolEnvelope,
    event_lifecycle: ContinuousEventLifecycle | None,
) -> CampaignForwardUniverseCycleAuthority:
    """Re-resolve exact campaign -> cycle -> provider snapshot -> #1185 universe."""

    _require_dispatch_integrity()
    if type(precommit_locator) is not ForwardUniversePrecommitLocator:
        raise TypeError("precommit_locator must be exact ForwardUniversePrecommitLocator")
    if type(collector_store) is not CollectorDeltaStore:
        raise TypeError("collector_store must be exact CollectorDeltaStore")
    if type(source_spec) is not CampaignInceptionSourceSpec:
        raise TypeError("source_spec must be exact CampaignInceptionSourceSpec")
    if type(provider_evidence_store) is not CompleteGameBoardEvidenceStore:
        raise TypeError(
            "provider_evidence_store must be exact CompleteGameBoardEvidenceStore"
        )
    if type(universe_store) is not ProviderEvaluationUniverseStore:
        raise TypeError("universe_store must be exact ProviderEvaluationUniverseStore")
    if type(protocol) is not ForwardEvidenceProtocolEnvelope:
        raise TypeError("protocol must be exact ForwardEvidenceProtocolEnvelope")
    if event_lifecycle is not None and type(event_lifecycle) is not ContinuousEventLifecycle:
        raise TypeError(
            "event_lifecycle must be exact ContinuousEventLifecycle or None"
        )
    if type(cycle_receipt) is not CampaignCompleteBoardCycleReceipt:
        raise TypeError(
            "cycle_receipt must be exact CampaignCompleteBoardCycleReceipt"
        )

    campaign = _ESTABLISH_CAMPAIGN(
        precommit_locator=precommit_locator,
        store=collector_store,
        source_spec=source_spec,
    )
    _require_dispatch_integrity()

    collector_evidence = _RESOLVE_ARTIFACT(
        collector_store,
        source_id=cycle_receipt.source_id,
        cycle_seq=cycle_receipt.cycle_seq,
        artifact_kind=ARTIFACT_KIND,
        artifact_sha256=cycle_receipt.provider_evidence_sha256,
    )
    if type(collector_evidence) is not dict:
        raise CampaignForwardUniverseCycleBindingError(
            "collector artifact resolver returned noncanonical evidence"
        )
    _require_dispatch_integrity()

    snapshot = _LOAD_PROVIDER_EVIDENCE(
        provider_evidence_store,
        cycle_receipt.provider_evidence_sha256,
    )
    if type(snapshot) is not CompleteGameBoardSnapshot:
        raise CampaignForwardUniverseCycleBindingError(
            "provider evidence resolver returned noncanonical snapshot"
        )
    _require_dispatch_integrity()

    expected_receipt = _expected_cycle_receipt_payload(
        campaign=campaign,
        snapshot=snapshot,
        collector_evidence=collector_evidence,
    )
    _assert_cycle_receipt_exact(cycle_receipt, expected_receipt)

    if (
        collector_evidence.get("authorization_sha256")
        != campaign.authority_record_sha256
        or cycle_receipt.source_id != campaign.source_id
        or cycle_receipt.source_id != source_spec.source_id
        or cycle_receipt.source_id != snapshot.request.source_id
        or cycle_receipt.source_id != universe_store.source_id
        or cycle_receipt.campaign_id != campaign.campaign_id
        or cycle_receipt.campaign_id != protocol.campaign_id
    ):
        raise CampaignForwardUniverseCycleBindingError(
            "campaign/cycle/provider/universe source or campaign identity conflicts"
        )

    try:
        ledger, _backing = _GUARDED_UNIVERSE_LOAD(universe_store)
    except ForwardUniverseBackingGuardError as exc:
        raise CampaignForwardUniverseCycleBindingError(
            "durable provider universe backing authority cannot be resolved"
        ) from exc
    if type(ledger) is not EvaluationUniverseLedger:
        raise CampaignForwardUniverseCycleBindingError(
            "durable provider evaluation universe is unavailable"
        )
    _require_dispatch_integrity()

    universe = _rebuild_exact_provider_universe(
        ledger=ledger,
        snapshot=snapshot,
        event_lifecycle=event_lifecycle,
    )
    if (
        universe.campaign_id != campaign.campaign_id
        or universe.campaign_id != protocol.campaign_id
        or universe.intake_snapshot.source_id != cycle_receipt.source_id
        or campaign.evaluation_universe_sha256 != universe.universe_sha256
    ):
        raise CampaignForwardUniverseCycleBindingError(
            "campaign precommit does not bind the exact cycle-derived provider universe"
        )

    try:
        forward_identity = _RESOLVE_FORWARD_IDENTITY(
            store=universe_store,
            protocol=protocol,
            precommit=precommit_locator,
        )
    except ForwardEvaluationUniverseBindingError as exc:
        raise CampaignForwardUniverseCycleBindingError(
            "forward universe authority cannot be re-resolved"
        ) from exc
    if type(forward_identity) is not ForwardUniverseAuthorityIdentity:
        raise CampaignForwardUniverseCycleBindingError(
            "forward universe resolver returned noncanonical authority identity"
        )
    if (
        forward_identity.universe_sha256 != universe.universe_sha256
        or forward_identity.membership_sha256 != universe.membership_sha256
        or forward_identity.member_count != len(universe.rows)
    ):
        raise CampaignForwardUniverseCycleBindingError(
            "forward universe identity conflicts with cycle-derived durable universe"
        )

    _require_dispatch_integrity()
    return CampaignForwardUniverseCycleAuthority._issue(
        campaign_id=campaign.campaign_id,
        source_id=cycle_receipt.source_id,
        cycle_receipt_sha256=cycle_receipt.receipt_sha256,
        campaign_receipt_sha256=campaign.receipt_sha256,
        provider_evidence_sha256=snapshot.evidence_sha256,
        provider_frame_sha256=snapshot.frame_sha256,
        collector_artifact_evidence_sha256=collector_evidence["evidence_sha256"],
        forward_identity=forward_identity,
    )


def authorize_campaign_forward_source_receipts(
    *,
    precommit_locator: ForwardUniversePrecommitLocator,
    collector_store: CollectorDeltaStore,
    source_spec: CampaignInceptionSourceSpec,
    cycle_receipt: CampaignCompleteBoardCycleReceipt,
    provider_evidence_store: CompleteGameBoardEvidenceStore,
    universe_store: ProviderEvaluationUniverseStore,
    protocol: ForwardEvidenceProtocolEnvelope,
    event_lifecycle: ContinuousEventLifecycle | None,
    opportunities: Sequence[ForwardOpportunityEnvelope],
) -> tuple[AuthoritativeSourceReceipt, ...]:
    """Authorize #1185 receipts only while the cycle-bound provider authority is stable."""

    before = resolve_campaign_forward_universe_cycle_authority(
        precommit_locator=precommit_locator,
        collector_store=collector_store,
        source_spec=source_spec,
        cycle_receipt=cycle_receipt,
        provider_evidence_store=provider_evidence_store,
        universe_store=universe_store,
        protocol=protocol,
        event_lifecycle=event_lifecycle,
    )
    _require_dispatch_integrity()
    try:
        receipts = _AUTHORIZE_FORWARD_RECEIPTS(
            store=universe_store,
            protocol=protocol,
            precommit=precommit_locator,
            opportunities=opportunities,
        )
    except ForwardEvaluationUniverseBindingError as exc:
        raise CampaignForwardUniverseCycleBindingError(
            "forward source receipt authorization failed"
        ) from exc
    _require_dispatch_integrity()
    after = resolve_campaign_forward_universe_cycle_authority(
        precommit_locator=precommit_locator,
        collector_store=collector_store,
        source_spec=source_spec,
        cycle_receipt=cycle_receipt,
        provider_evidence_store=provider_evidence_store,
        universe_store=universe_store,
        protocol=protocol,
        event_lifecycle=event_lifecycle,
    )
    if after != before:
        raise CampaignForwardUniverseCycleBindingError(
            "campaign/cycle/provider/universe authority changed during authorization"
        )
    if not isinstance(receipts, tuple) or not all(
        type(receipt) is AuthoritativeSourceReceipt for receipt in receipts
    ):
        raise CampaignForwardUniverseCycleBindingError(
            "forward source authorization returned noncanonical receipts"
        )
    return receipts


_INTERNAL_CALLABLES = tuple(
    (name, target, getattr(target, "__code__", None))
    for name, target in (
        ("_text", _text),
        ("_sha", _sha),
        ("_digest", _digest),
        (
            "_expected_cycle_receipt_payload",
            _expected_cycle_receipt_payload,
        ),
        ("_assert_cycle_receipt_exact", _assert_cycle_receipt_exact),
        ("_rebuild_exact_provider_universe", _rebuild_exact_provider_universe),
        (
            "resolve_campaign_forward_universe_cycle_authority",
            resolve_campaign_forward_universe_cycle_authority,
        ),
        (
            "authorize_campaign_forward_source_receipts",
            authorize_campaign_forward_source_receipts,
        ),
    )
)


__all__ = [
    "CampaignForwardUniverseCycleAuthority",
    "CampaignForwardUniverseCycleBindingError",
    "authorize_campaign_forward_source_receipts",
    "resolve_campaign_forward_universe_cycle_authority",
]
