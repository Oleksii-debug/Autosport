"""Fail-closed restart publication for already-produced paired VOC work.

A router process intentionally forgets ``_live_request_ids`` on restart so old route
records cannot be used to mint outcome-aware shadow evidence. The production
orchestrator, however, can already have a durable ``SUCCEEDED`` producer receipt
when a process dies immediately before shadow-authority publication. This guard
permits only that exact pre-authorized result to finish publication after restart.
It does not make historical requests generally live and it never permits a new
backend invocation for a restarted request.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterable

from . import model_compute_router as router
from . import voc_production_orchestrator as production


@dataclass(frozen=True, slots=True)
class _RestartPublicationGrant:
    store: router.ModelComputeRouterStore
    orchestrator: production.VOCProductionOrchestrator
    request_id: str
    role: str
    route_record_sha256: str
    candidate_identity_sha256: str
    admission_sha256: str
    result_sha256: str


_RESTART_PUBLICATION_GRANT: ContextVar[_RestartPublicationGrant | None] = ContextVar(
    "autosport_voc_restart_publication_grant",
    default=None,
)


class _ScopedLiveRequestIds(set[str]):
    """Normal live IDs plus one context-local restart publication identity.

    Recovery must satisfy the original router's live-route check without making a
    historical request live to another thread. ContextVar state is intentionally
    consulted only for membership; normal route-created IDs remain ordinary set
    members and retain their existing process-wide semantics.
    """

    def __init__(
        self,
        store: router.ModelComputeRouterStore,
        values: Iterable[str] = (),
    ) -> None:
        super().__init__(values)
        self._store = store

    def process_contains(self, value: str) -> bool:
        """Return only real process-live membership, ignoring recovery grants."""

        return set.__contains__(self, value)

    def __contains__(self, value: object) -> bool:
        if set.__contains__(self, value):
            return True
        if type(value) is not str:
            return False
        grant = _RESTART_PUBLICATION_GRANT.get()
        return (
            grant is not None
            and grant.store is self._store
            and grant.request_id == value
        )


def _is_process_live(
    store: router.ModelComputeRouterStore,
    request_id: str,
) -> bool:
    live_ids = store._live_request_ids
    if isinstance(live_ids, _ScopedLiveRequestIds):
        return live_ids.process_contains(request_id)
    return request_id in live_ids


def _restart_grant(
    orchestrator: production.VOCProductionOrchestrator,
    *,
    request_id: str,
    role: str,
    admission_sha256: str | None,
) -> _RestartPublicationGrant | None:
    store = orchestrator.router_store
    if _is_process_live(store, request_id):
        return None
    if store.get_voc_shadow_execution(request_id, role) is not None:
        return None

    receipts = orchestrator._load()
    receipt = receipts.get((request_id, role))
    if receipt is None:
        raise router.ModelComputeRouterError(
            "restarted VOC request cannot begin new backend work; require a fresh route"
        )
    state = receipt.get("state")
    if state == "STARTED":
        # Preserve the owning orchestrator's existing uncertain-invocation error.
        return None
    if state == "PUBLISHED":
        raise router.ModelComputeRouterError(
            "published VOC producer receipt lacks canonical shadow authority"
        )
    if state != "SUCCEEDED":
        raise router.ModelComputeRouterError(
            "restart publication requires a durable SUCCEEDED producer receipt"
        )
    if admission_sha256 is None or orchestrator.decision_ledger is None:
        raise router.ModelComputeRouterError(
            "restart publication requires canonical paired admission authority"
        )

    canonical_admission = orchestrator._ensure_pair_admission(request_id)
    if canonical_admission != admission_sha256:
        raise router.ModelComputeRouterError(
            "restart publication admission does not match canonical paired authority"
        )
    if receipt.get("admission_sha256") != canonical_admission:
        raise router.ModelComputeRouterError(
            "restart publication receipt admission authority mismatch"
        )

    precompute, identity = orchestrator._precompute(request_id, role)
    if receipt.get("route_record_sha256") != precompute.get("route_record_sha256"):
        raise router.ModelComputeRouterError(
            "restart publication receipt route authority mismatch"
        )
    if receipt.get("candidate_identity") != identity:
        raise router.ModelComputeRouterError(
            "restart publication receipt candidate identity mismatch"
        )

    raw_result = receipt.get("result")
    if not isinstance(raw_result, dict):
        raise router.ModelComputeRouterError(
            "restart publication SUCCEEDED receipt lacks backend result"
        )
    result = production.VOCBackendResult.from_payload(raw_result)
    started_at = production._instant(
        "VOC producer started_at", receipt.get("started_at")
    )
    precompute_recorded_at = production._instant(
        "VOC precompute authority_recorded_at",
        precompute.get("authority_recorded_at"),
    )
    completed_at = production._instant(
        "VOC backend completed_at", result.completed_at
    )
    if started_at < precompute_recorded_at:
        raise router.ModelComputeRouterError(
            "restart publication producer start predates precompute authority"
        )
    if started_at > completed_at:
        raise router.ModelComputeRouterError(
            "restart publication producer start follows backend completion"
        )

    return _RestartPublicationGrant(
        store=store,
        orchestrator=orchestrator,
        request_id=request_id,
        role=role,
        route_record_sha256=precompute["route_record_sha256"],
        candidate_identity_sha256=production._digest(identity),
        admission_sha256=canonical_admission,
        result_sha256=production._digest(result.payload()),
    )


def _install() -> None:
    orchestrator_cls = production.VOCProductionOrchestrator
    store_cls = router.ModelComputeRouterStore
    if getattr(orchestrator_cls, "_restart_publication_guard_v2", False):
        return

    original_store_init = store_cls.__init__
    original_run_role = orchestrator_cls.run_role
    original_record_shadow = store_cls.record_voc_shadow_execution

    def hardened_store_init(
        self: router.ModelComputeRouterStore,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        original_store_init(self, *args, **kwargs)
        if not isinstance(self._live_request_ids, _ScopedLiveRequestIds):
            self._live_request_ids = _ScopedLiveRequestIds(
                self,
                self._live_request_ids,
            )

    def hardened_run_role(
        self: production.VOCProductionOrchestrator,
        *,
        request_id: str,
        role: str,
        invoke,
        _admission_sha256: str | None = None,
    ) -> dict[str, Any]:
        grant = _restart_grant(
            self,
            request_id=request_id,
            role=role,
            admission_sha256=_admission_sha256,
        )
        token = None
        if grant is not None:
            token = _RESTART_PUBLICATION_GRANT.set(grant)
        try:
            return original_run_role(
                self,
                request_id=request_id,
                role=role,
                invoke=invoke,
                _admission_sha256=_admission_sha256,
            )
        finally:
            if token is not None:
                _RESTART_PUBLICATION_GRANT.reset(token)

    def hardened_record_shadow(
        self: router.ModelComputeRouterStore,
        *,
        request_id: str,
        role: str,
        output_sha256: str,
        action: str,
        abstained: bool,
        completed_at: str,
        available_at: str,
        actual_cost,
        evidence_sha256: str,
    ) -> dict[str, Any]:
        if _is_process_live(self, request_id):
            return original_record_shadow(
                self,
                request_id=request_id,
                role=role,
                output_sha256=output_sha256,
                action=action,
                abstained=abstained,
                completed_at=completed_at,
                available_at=available_at,
                actual_cost=actual_cost,
                evidence_sha256=evidence_sha256,
            )

        grant = _RESTART_PUBLICATION_GRANT.get()
        if grant is None or grant.store is not self:
            return original_record_shadow(
                self,
                request_id=request_id,
                role=role,
                output_sha256=output_sha256,
                action=action,
                abstained=abstained,
                completed_at=completed_at,
                available_at=available_at,
                actual_cost=actual_cost,
                evidence_sha256=evidence_sha256,
            )
        if grant.request_id != request_id or grant.role != role:
            raise router.ModelComputeRouterError(
                "restart publication grant does not match requested shadow role"
            )

        precompute = self.get_voc_precompute_admission(request_id)
        if precompute is None:
            raise router.ModelComputeRouterError(
                "restart publication lost canonical precompute admission"
            )
        if precompute.get("route_record_sha256") != grant.route_record_sha256:
            raise router.ModelComputeRouterError(
                "restart publication canonical route authority changed"
            )
        identity = precompute.get(f"{role}_compute_identity")
        if production._digest(identity) != grant.candidate_identity_sha256:
            raise router.ModelComputeRouterError(
                "restart publication canonical candidate identity changed"
            )

        supplied_result = {
            "output_sha256": output_sha256,
            "action": action,
            "abstained": abstained,
            "completed_at": completed_at,
            "available_at": available_at,
            "actual_cost": str(actual_cost),
            "evidence_sha256": evidence_sha256,
        }
        if production._digest(supplied_result) != grant.result_sha256:
            raise router.ModelComputeRouterError(
                "restart publication result differs from durable SUCCEEDED receipt"
            )
        if not isinstance(self._live_request_ids, _ScopedLiveRequestIds):
            raise router.ModelComputeRouterError(
                "restart publication store lacks context-local liveness fence"
            )

        # The original publisher still performs every canonical validation. Its
        # liveness membership check is satisfied only in this ContextVar, for this
        # exact store/request grant. No shared request-id mutation occurs here.
        return original_record_shadow(
            self,
            request_id=request_id,
            role=role,
            output_sha256=output_sha256,
            action=action,
            abstained=abstained,
            completed_at=completed_at,
            available_at=available_at,
            actual_cost=actual_cost,
            evidence_sha256=evidence_sha256,
        )

    store_cls.__init__ = hardened_store_init
    orchestrator_cls.run_role = hardened_run_role
    store_cls.record_voc_shadow_execution = hardened_record_shadow
    orchestrator_cls._restart_publication_guard_v2 = True


_install()
