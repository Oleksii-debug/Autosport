from __future__ import annotations

"""Install non-bypassable realized-VOC protocol/cohort authority fences.

This module is imported by ``autosport.__init__`` before public consumers import
the VOC facade.  The guard deliberately patches the existing canonical classes
and functions *in place* rather than publishing a stricter subclass next to an
older reachable scorer.  That matters because a positive VOC route must not be
recoverable by importing an unfenced historical class or by supplying a
structurally-compatible scorer to the public canonical resolver.

Positive explicit VOC scoring additionally requires router-owned precompute
authority to bind the exact ResearchProtocol record that physically existed at
precompute time.  The binding includes the immutable protocol digest, registry
record digest, append-prefix witness, full evaluation-design digest, and the
protocol-derived cohort-eligibility digest.  A later backdated protocol can
therefore never upgrade a legacy/unbound admission into positive authority.
"""

from contextvars import ContextVar
import json
from pathlib import Path
from typing import Any, Mapping

from . import model_compute_router as router
from . import voc_outcome_scoring as scoring
from .scientific_registry import ScientificRegistry


_TARGET_IDENTITY: ContextVar[tuple[str, str] | None] = ContextVar(
    "autosport_voc_target_protocol_cohort",
    default=None,
)
_ROUTE_REGISTRY: ContextVar[ScientificRegistry | None] = ContextVar(
    "autosport_voc_route_scientific_registry",
    default=None,
)

_V2_PROTOCOL_FIELDS = frozenset(
    {
        "research_protocol_sha256",
        "research_protocol_record_sha256",
        "research_protocol_registry_index",
        "research_protocol_registry_prefix_sha256",
        "evaluation_design_sha256",
        "cohort_eligibility_sha256",
    }
)


class _ProtocolBindingError(ValueError):
    pass


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise _ProtocolBindingError(f"{field} must be a non-empty canonical string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise _ProtocolBindingError(f"{field} must be valid UTF-8") from exc
    return value


def _sha256(value: object, field: str) -> str:
    digest = _text(value, field)
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise _ProtocolBindingError(f"{field} must be lowercase SHA-256 hex")
    return digest


def _protocol_semantics(
    registry: ScientificRegistry,
    *,
    protocol_id: str,
    cohort_id: str,
) -> dict[str, Any]:
    """Resolve immutable protocol semantics plus a physical append-prefix witness."""

    if not isinstance(registry, ScientificRegistry):
        raise _ProtocolBindingError("canonical ScientificRegistry is unavailable")
    wanted_protocol = _text(protocol_id, "research_protocol_id")
    wanted_cohort = _text(cohort_id, "cohort_id")
    entry = registry.get("ResearchProtocol", wanted_protocol)
    if entry is None:
        raise _ProtocolBindingError(
            "canonical ResearchProtocol must exist before VOC precompute admission"
        )
    payload = entry.payload
    if type(payload) is not dict:
        raise _ProtocolBindingError("canonical ResearchProtocol payload is invalid")
    if payload.get("research_protocol_id") != wanted_protocol:
        raise _ProtocolBindingError("canonical ResearchProtocol identity mismatch")
    protocol_sha256 = _sha256(
        payload.get("protocol_sha256"),
        "canonical ResearchProtocol protocol_sha256",
    )
    record_sha256 = _sha256(
        entry.record_sha256,
        "canonical ResearchProtocol record_sha256",
    )
    binding = payload.get("binding")
    if type(binding) is not dict:
        raise _ProtocolBindingError("canonical ResearchProtocol binding is missing")
    design_text = binding.get("evaluation_design")
    if type(design_text) is not str or not design_text.strip():
        raise _ProtocolBindingError(
            "canonical ResearchProtocol evaluation_design is missing"
        )
    try:
        design = json.loads(design_text)
    except json.JSONDecodeError as exc:
        raise _ProtocolBindingError(
            "canonical ResearchProtocol evaluation_design is invalid JSON"
        ) from exc
    if type(design) is not dict:
        raise _ProtocolBindingError(
            "canonical ResearchProtocol evaluation_design must be an object"
        )
    protocol_cohort = _text(design.get("cohort_id"), "protocol cohort_id")
    if protocol_cohort != wanted_cohort:
        raise _ProtocolBindingError(
            "VOC precompute cohort does not match canonical ResearchProtocol"
        )
    eligibility = design.get("cohort_eligibility")
    if type(eligibility) is not dict or not eligibility:
        raise _ProtocolBindingError(
            "canonical ResearchProtocol cohort_eligibility is missing"
        )

    state = registry._read()
    records = state.get("records")
    if type(records) is not list:
        raise _ProtocolBindingError("canonical ScientificRegistry records are invalid")
    matches = [
        (index, raw)
        for index, raw in enumerate(records)
        if raw.get("record_type") == "ResearchProtocol"
        and raw.get("record_id") == wanted_protocol
    ]
    if len(matches) != 1:
        raise _ProtocolBindingError(
            "canonical ResearchProtocol physical publication witness is ambiguous"
        )
    record_index, raw = matches[0]
    if raw.get("record_sha256") != record_sha256:
        raise _ProtocolBindingError(
            "canonical ResearchProtocol registry record digest mismatch"
        )
    prefix_sha256 = router._canonical_digest(records[: record_index + 1])

    return {
        "research_protocol_sha256": protocol_sha256,
        "research_protocol_record_sha256": record_sha256,
        "research_protocol_registry_index": record_index,
        "research_protocol_registry_prefix_sha256": prefix_sha256,
        "evaluation_design_sha256": router._canonical_digest(design),
        "cohort_eligibility_sha256": router._canonical_digest(eligibility),
        "research_protocol_available_at": entry.available_at,
    }


def _registry_for_route(store: router.ModelComputeRouterStore) -> ScientificRegistry | None:
    evaluation_store = getattr(store, "_voc_evaluation_store", None)
    resolver = getattr(evaluation_store, "_canonical_authority_resolver", None)
    registry = getattr(resolver, "scientific_registry", None)
    if isinstance(registry, ScientificRegistry):
        return registry

    # The canonical workspace layout used by Autosport tests/product wiring keeps
    # the scientific registry beside durable router state.  This is a discovery
    # fallback only; if the file is absent the admission remains legacy/unbound and
    # cannot become positive scoring authority.
    candidate = Path(store.path).parent / "scientific-registry.json"
    if candidate.exists():
        return ScientificRegistry(candidate)
    return None


def _install() -> None:
    canonical_cls = scoring.CanonicalOutcomeDerivedVOCScoreAuthority
    base_cls = scoring._base.CanonicalOutcomeDerivedVOCScoreAuthority
    resolver_cls = scoring._base.CanonicalVOCAuthorityResolver
    store_cls = router.ModelComputeRouterStore

    original_base_cohort_members = base_cls._cohort_members
    original_main_eligible = canonical_cls._eligible_cohort_decisions
    original_router_precompute = canonical_cls._router_precompute_for_admission
    original_resolver_init = resolver_cls.__init__
    original_route = store_cls.route
    original_build_precompute = router._build_voc_precompute_admission
    original_validate_precompute = router._validate_persisted_voc_precompute_admission

    def hardened_build_precompute(
        *,
        request,
        candidates,
        decision,
        domain_observation,
        control,
        authority_recorded_at=None,
    ):
        registry = _ROUTE_REGISTRY.get()
        semantics: dict[str, Any] | None = None
        if registry is not None:
            if type(control) is not dict or set(control) != router._VOC_PRECOMPUTE_CONTROL_FIELDS:
                return original_build_precompute(
                    request=request,
                    candidates=candidates,
                    decision=decision,
                    domain_observation=domain_observation,
                    control=control,
                    authority_recorded_at=authority_recorded_at,
                )
            try:
                semantics = _protocol_semantics(
                    registry,
                    protocol_id=control.get("research_protocol_id"),
                    cohort_id=control.get("cohort_id"),
                )
                if router._instant(
                    "ResearchProtocol.available_at",
                    semantics["research_protocol_available_at"],
                ) > router._instant("decision.decided_at", decision.decided_at):
                    raise _ProtocolBindingError(
                        "canonical ResearchProtocol is not logically available at VOC precompute"
                    )
            except _ProtocolBindingError as exc:
                raise router.ModelComputeRouterError(str(exc)) from exc

        # This call owns the production wall-clock stamp.  When semantics are
        # present they were resolved from the durable registry *before* this stamp.
        admission = original_build_precompute(
            request=request,
            candidates=candidates,
            decision=decision,
            domain_observation=domain_observation,
            control=control,
            authority_recorded_at=authority_recorded_at,
        )
        if semantics is None:
            return admission
        return {
            **admission,
            "schema_version": 2,
            **{
                key: semantics[key]
                for key in _V2_PROTOCOL_FIELDS
            },
        }

    def hardened_validate_precompute(
        *,
        request,
        candidates,
        decision,
        domain_observation,
        raw,
    ):
        if not isinstance(raw, Mapping) or raw.get("schema_version") != 2:
            return original_validate_precompute(
                request=request,
                candidates=candidates,
                decision=decision,
                domain_observation=domain_observation,
                raw=raw,
            )
        expected_fields = set(router._VOC_PRECOMPUTE_FIELDS) | set(
            _V2_PROTOCOL_FIELDS
        )
        if set(raw) != expected_fields:
            raise router.ModelComputeRouterError(
                "persisted VOC precompute admission schema is invalid"
            )
        legacy = {
            key: raw[key]
            for key in router._VOC_PRECOMPUTE_FIELDS
        }
        legacy["schema_version"] = 1
        original_validate_precompute(
            request=request,
            candidates=candidates,
            decision=decision,
            domain_observation=domain_observation,
            raw=legacy,
        )
        for field in _V2_PROTOCOL_FIELDS - {"research_protocol_registry_index"}:
            try:
                _sha256(raw.get(field), f"persisted VOC precompute {field}")
            except _ProtocolBindingError as exc:
                raise router.ModelComputeRouterError(str(exc)) from exc
        index = raw.get("research_protocol_registry_index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise router.ModelComputeRouterError(
                "persisted VOC precompute research_protocol_registry_index is invalid"
            )
        return dict(raw)

    def hardened_route(self, *args, **kwargs):
        if kwargs.get("voc_precompute_admission") is None:
            return original_route(self, *args, **kwargs)
        registry = _registry_for_route(self)
        token = _ROUTE_REGISTRY.set(registry)
        try:
            return original_route(self, *args, **kwargs)
        finally:
            _ROUTE_REGISTRY.reset(token)

    def verify_bound_protocol(self, authority: Mapping[str, Any]) -> None:
        if authority.get("schema_version") != 2:
            raise scoring._base.VOCEvaluationError(
                "positive VOC authority lacks precompute-bound ResearchProtocol semantics"
            )
        registry = getattr(self, "scientific_registry", None)
        if not isinstance(registry, ScientificRegistry):
            raise scoring._base.VOCEvaluationError(
                "positive VOC authority lacks canonical ScientificRegistry"
            )
        try:
            semantics = _protocol_semantics(
                registry,
                protocol_id=authority.get("research_protocol_id"),
                cohort_id=authority.get("cohort_id"),
            )
        except _ProtocolBindingError as exc:
            raise scoring._base.VOCEvaluationError(str(exc)) from exc
        for field in _V2_PROTOCOL_FIELDS:
            if authority.get(field) != semantics[field]:
                raise scoring._base.VOCEvaluationError(
                    f"VOC precompute bound {field} no longer matches canonical ResearchProtocol"
                )
        if scoring._base._instant(
            authority.get("research_protocol_available_at")
            if "research_protocol_available_at" in authority
            else semantics["research_protocol_available_at"],
            field="ResearchProtocol.available_at",
        ) > scoring._base._instant(
            authority.get("admitted_at"),
            field="VOC precompute admitted_at",
        ):
            raise scoring._base.VOCEvaluationError(
                "VOC ResearchProtocol was not logically available by precompute admission"
            )

    def hardened_router_precompute(self, *args, **kwargs):
        authority = original_router_precompute(self, *args, **kwargs)
        verify_bound_protocol(self, authority)
        return authority

    def hardened_main_cohort_members(self, evaluation, *, as_of):
        cohort_id, _, _ = self._protocol_contract(evaluation)
        token = _TARGET_IDENTITY.set((evaluation.research_protocol_id, cohort_id))
        try:
            # Call the captured, pre-guard base implementation directly.  The base
            # class itself is disabled below so importing it cannot recover an
            # unfenced positive scorer.
            return original_base_cohort_members(self, evaluation, as_of=as_of)
        finally:
            _TARGET_IDENTITY.reset(token)

    def hardened_main_eligible(
        self,
        *,
        recorded_from,
        recorded_through,
        expected_task_class: str,
        expected_scope,
        expected_baseline,
        expected_challenger,
    ) -> dict[str, str]:
        target = _TARGET_IDENTITY.get()
        if target is None:
            raise scoring._base.VOCEvaluationError(
                "explicit VOC eligibility lacks canonical protocol/cohort context"
            )
        expected_protocol_id, expected_cohort_id = target
        ordered, by_sha, order = scoring._ledger_records(self.decision_ledger)
        common: dict[str, Any] = {
            "ordered": ordered,
            "by_sha": by_sha,
            "order": order,
            "recorded_from": recorded_from,
            "recorded_through": recorded_through,
            "expected_task_class": expected_task_class,
            "expected_scope": expected_scope,
            "expected_baseline": expected_baseline,
            "expected_challenger": expected_challenger,
        }
        all_explicit = self._explicit_admissions(**common)
        if all_explicit:
            canonical_explicit = self._explicit_admissions(
                **common,
                expected_protocol_id=expected_protocol_id,
                expected_cohort_id=expected_cohort_id,
            )
            if set(canonical_explicit) != set(all_explicit):
                raise scoring._base.VOCEvaluationError(
                    "explicit VOC paired admission protocol/cohort does not match canonical target"
                )
        return original_main_eligible(
            self,
            recorded_from=recorded_from,
            recorded_through=recorded_through,
            expected_task_class=expected_task_class,
            expected_scope=expected_scope,
            expected_baseline=expected_baseline,
            expected_challenger=expected_challenger,
        )

    def reject_unfenced_base(self, evaluation, *, as_of):
        raise scoring._base.VOCEvaluationError(
            "unfenced legacy VOC scorer is disabled; use canonical terminal-aware authority"
        )

    def hardened_resolver_init(
        self,
        decision_ledger,
        scientific_registry,
        outcome_authority,
        outcome_score_authority,
    ):
        if type(outcome_score_authority) is not canonical_cls:
            raise TypeError(
                "canonical VOC resolver requires the exact terminal-aware scorer authority"
            )
        return original_resolver_init(
            self,
            decision_ledger,
            scientific_registry,
            outcome_authority,
            outcome_score_authority,
        )

    # Router precompute semantics are patched before the store route wrapper can
    # invoke them.  Restart validation accepts v1 as historical measurement-only
    # evidence and v2 as promotion-grade bound authority; positive scoring rejects
    # v1 above.
    router._build_voc_precompute_admission = hardened_build_precompute
    router._validate_persisted_voc_precompute_admission = hardened_validate_precompute
    store_cls.route = hardened_route

    # Patch the canonical scorer in place.  No old scorer class is retained in a
    # module global, so ordinary imports cannot select an unfenced positive path.
    canonical_cls._router_precompute_for_admission = hardened_router_precompute
    canonical_cls._cohort_members = hardened_main_cohort_members
    canonical_cls._eligible_cohort_decisions = hardened_main_eligible

    # Direct use of the historical base scorer is fail-closed, and the public
    # resolver accepts only the exact hardened terminal-aware scorer type.
    base_cls._cohort_members = reject_unfenced_base
    resolver_cls.__init__ = hardened_resolver_init


_install()

__all__: list[str] = []
