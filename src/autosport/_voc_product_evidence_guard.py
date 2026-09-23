from __future__ import annotations

"""Install a product-owned freeze boundary for paired realized-VOC evidence.

The production orchestrator already owns pre-output admission and durable backend
publication.  This guard closes the next causal seam: after both shadow outputs are
published, it derives the exact pre-outcome scoring DecisionRecord from those
canonical router authorities instead of accepting caller-authored output/cost/time
claims.  The first freeze timestamp is product-owned rather than caller supplied;
a later restart reuses the durable record without restamping it.
"""

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from .decision_ledger import DecisionRecord
from .model_compute_router import ModelComputeRouterError
from . import voc_production_orchestrator as production
from .workspace_lock import WorkspaceEconomicLock

_SCORING_ACTION = "VOC_PAIRED_SCORING_EVIDENCE"
_SCORING_KEY = "voc_scoring_evidence"
_BINDING_KEY = "voc_binding"
_TERMINAL_KEY = "voc_terminal"


def _fixed_decimal(name: str, value: object) -> str:
    if isinstance(value, bool):
        raise ModelComputeRouterError(f"{name} must be decimal text")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ModelComputeRouterError(f"{name} must be decimal text") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ModelComputeRouterError(f"{name} must be finite and non-negative")
    return format(parsed, "f")


def _quote_keys(value: object) -> tuple[str, ...]:
    if type(value) is not tuple or not value:
        raise ModelComputeRouterError("VOC quote_keys must be a non-empty tuple")
    result = tuple(production._text("quote_key", item) for item in value)
    if len(set(result)) != len(result):
        raise ModelComputeRouterError("VOC quote_keys must be unique")
    return result


def _record_sha(record: DecisionRecord) -> str:
    return production._digest(record.to_dict())


def _matching_terminals(
    records: list[DecisionRecord],
    *,
    decision_context_sha256: str,
    admission_sha256: str,
) -> list[DecisionRecord]:
    matches: list[DecisionRecord] = []
    for record in records:
        payload = record.payload
        if not isinstance(payload, Mapping):
            continue
        binding = payload.get(_BINDING_KEY)
        if (
            isinstance(binding, Mapping)
            and binding.get("decision_context_sha256") == decision_context_sha256
        ):
            matches.append(record)
            continue
        terminal = payload.get(_TERMINAL_KEY)
        if (
            isinstance(terminal, Mapping)
            and terminal.get("admission_sha256") == admission_sha256
        ):
            matches.append(record)
    return matches


def _install() -> None:
    orchestrator_cls = production.VOCProductionOrchestrator
    if getattr(orchestrator_cls, "_product_evidence_guard_v1", False):
        return

    original_run_pair = orchestrator_cls.run_pair

    def freeze_pair_scoring_evidence(
        self: production.VOCProductionOrchestrator,
        *,
        request_id: str,
        quote_keys: tuple[str, ...],
        evaluation_id: str | None = None,
    ) -> dict[str, str]:
        """Freeze exact product-owned paired output/cost/timing evidence once.

        The caller cannot choose the first-freeze timestamp.  That timestamp is
        sampled from the product clock only after the exact durable authorities are
        re-resolved under the workspace lock.  On restart, an exact existing record
        is returned without consulting the clock again.
        """

        request = production._text("request_id", request_id)
        keys = _quote_keys(quote_keys)
        ledger = self.decision_ledger
        if ledger is None:
            raise ModelComputeRouterError(
                "paired VOC scoring evidence requires canonical DecisionLedger authority"
            )

        admission_sha = self._ensure_pair_admission(request)
        precompute = self.router_store.get_voc_precompute_admission(request)
        if precompute is None:
            raise ModelComputeRouterError(
                "paired VOC scoring evidence requires canonical precompute admission"
            )
        context_sha = production._sha(
            "decision_context_sha256",
            precompute.get("decision_context_sha256"),
        )
        decision_input_sha = production._sha(
            "decision_input_sha256",
            precompute.get("decision_input_sha256"),
        )
        deadline = production._instant(
            "decision_deadline", precompute.get("decision_deadline")
        )
        identity = (
            production._text("evaluation_id", evaluation_id)
            if evaluation_id is not None
            else f"voc-evaluation:{production._text('admission_id', precompute.get('admission_id'))}"
        )

        shadows: dict[str, Mapping[str, Any]] = {}
        shadow_ready_at = []
        for role in ("baseline", "challenger"):
            shadow = self.router_store.get_voc_shadow_execution(request, role)
            if shadow is None:
                raise ModelComputeRouterError(
                    "paired VOC scoring evidence requires both canonical shadow executions"
                )
            wanted_identity = production._identity(
                precompute.get(f"{role}_compute_identity")
            )
            if shadow.get("candidate_identity") != wanted_identity:
                raise ModelComputeRouterError(
                    f"{role} VOC shadow identity no longer matches precompute authority"
                )
            completed_at = production._instant(
                f"{role} completed_at", shadow.get("completed_at")
            )
            available_at = production._instant(
                f"{role} available_at", shadow.get("available_at")
            )
            authority_at = production._instant(
                f"{role} authority_recorded_at",
                shadow.get("authority_recorded_at"),
            )
            if available_at > deadline:
                raise ModelComputeRouterError(
                    "positive paired VOC scoring evidence cannot freeze a deadline-late shadow"
                )
            shadow_ready_at.extend((completed_at, available_at, authority_at))
            production._sha(f"{role} output_sha256", shadow.get("output_sha256"))
            production._text(f"{role} action", shadow.get("action"))
            if type(shadow.get("abstained")) is not bool:
                raise ModelComputeRouterError(
                    f"{role} VOC shadow abstained must be bool"
                )
            production._sha(
                f"{role} authority_sha256", shadow.get("authority_sha256")
            )
            shadows[role] = shadow
        latest_shadow_ready_at = max(shadow_ready_at)

        scope = precompute.get("scope")
        expected_scope = {
            "sport_id",
            "league_id",
            "regime_id",
            "urgency_id",
            "contradiction_state",
        }
        if not isinstance(scope, Mapping) or set(scope) != expected_scope:
            raise ModelComputeRouterError("VOC precompute scope is invalid")
        canonical_scope = {
            field: production._text(field, scope.get(field))
            for field in sorted(expected_scope)
        }
        baseline_identity = production._identity(
            precompute.get("baseline_compute_identity")
        )
        challenger_identity = production._identity(
            precompute.get("challenger_compute_identity")
        )
        baseline = shadows["baseline"]
        challenger = shadows["challenger"]

        binding = {
            "decision_input_sha256": decision_input_sha,
            "decision_context_sha256": context_sha,
            "baseline_candidate_id": baseline_identity["candidate_id"],
            "baseline_backend_id": baseline_identity["backend_id"],
            "baseline_model_id": baseline_identity["model_id"],
            "baseline_config_sha256": baseline_identity["config_sha256"],
            "baseline_output_sha256": baseline["output_sha256"],
            "baseline_action": baseline["action"],
            "baseline_abstained": baseline["abstained"],
            "challenger_candidate_id": challenger_identity["candidate_id"],
            "challenger_backend_id": challenger_identity["backend_id"],
            "challenger_model_id": challenger_identity["model_id"],
            "challenger_config_sha256": challenger_identity["config_sha256"],
            "challenger_output_sha256": challenger["output_sha256"],
            "challenger_action": challenger["action"],
            "challenger_abstained": challenger["abstained"],
            **canonical_scope,
        }
        baseline_cost = _fixed_decimal(
            "baseline actual_cost", baseline.get("actual_cost")
        )
        challenger_cost = _fixed_decimal(
            "challenger actual_cost", challenger.get("actual_cost")
        )
        scoring_evidence = {
            "schema_version": 1,
            "evaluation_id": identity,
            "decision_input_sha256": decision_input_sha,
            "baseline_output_sha256": baseline["output_sha256"],
            "challenger_output_sha256": challenger["output_sha256"],
            "baseline_action": baseline["action"],
            "challenger_action": challenger["action"],
            "baseline_abstained": baseline["abstained"],
            "challenger_abstained": challenger["abstained"],
            "samples": [
                {
                    "sample_id": f"{identity}:quote:{index}",
                    "quote_key": quote_key,
                    "baseline_compute_cost": baseline_cost,
                    "challenger_compute_cost": challenger_cost,
                    "baseline_completed_at": baseline["completed_at"],
                    "challenger_completed_at": challenger["completed_at"],
                }
                for index, quote_key in enumerate(keys, start=1)
            ],
        }
        payload = {
            _BINDING_KEY: binding,
            _SCORING_KEY: scoring_evidence,
        }
        replay_run_id = f"voc-production-scoring:{request}"
        agent = "voc-production-orchestrator"
        decision_id = f"voc-scoring:{admission_sha}"

        with WorkspaceEconomicLock(self.path.parent):
            ledger.verified_snapshot()
            records = ledger.verified_records()
            context_matches = [
                record
                for record in records
                if _record_sha(record) == context_sha
            ]
            if len(context_matches) != 1:
                raise ModelComputeRouterError(
                    "paired VOC scoring evidence canonical route context is missing or ambiguous"
                )
            context = context_matches[0]
            context_payload = context.payload
            current = (
                context_payload.get("voc_current_context")
                if isinstance(context_payload, Mapping)
                else None
            )
            if (
                not isinstance(current, Mapping)
                or current.get("request_id") != request
                or current.get("decision_input_sha256") != decision_input_sha
                or current.get("task_class") != precompute.get("task_class")
                or {field: current.get(field) for field in expected_scope}
                != {field: canonical_scope[field] for field in expected_scope}
            ):
                raise ModelComputeRouterError(
                    "paired VOC scoring evidence route context no longer matches precompute authority"
                )

            same_id = [record for record in records if record.decision_id == decision_id]
            if len(same_id) > 1:
                raise ModelComputeRouterError(
                    "paired VOC scoring evidence identity is ambiguous"
                )
            if same_id:
                existing = same_id[0]
                existing_at = production._instant(
                    "durable scoring recorded_at", existing.recorded_at
                )
                if (
                    existing.replay_run_id != replay_run_id
                    or existing.agent != agent
                    or existing.observed_ts != existing.recorded_at
                    or existing.action != _SCORING_ACTION
                    or existing.to_dict()["payload"] != payload
                    or existing.context_hash != decision_input_sha
                    or existing_at < latest_shadow_ready_at
                ):
                    raise ModelComputeRouterError(
                        "paired VOC scoring evidence conflicts with durable product authority"
                    )
                return {
                    "evaluation_id": identity,
                    "decision_evidence_sha256": _record_sha(existing),
                    "admission_sha256": admission_sha,
                }

            terminals = _matching_terminals(
                records,
                decision_context_sha256=context_sha,
                admission_sha256=admission_sha,
            )
            if terminals:
                raise ModelComputeRouterError(
                    "paired VOC admission already has a different terminal record"
                )

            frozen_at_text = production._text("product freeze time", production._now())
            frozen_at = production._instant("product freeze time", frozen_at_text)
            if frozen_at < latest_shadow_ready_at:
                raise ModelComputeRouterError(
                    "product clock predates canonical shadow availability"
                )
            expected = DecisionRecord(
                replay_run_id=replay_run_id,
                agent=agent,
                observed_ts=frozen_at_text,
                action=_SCORING_ACTION,
                payload=payload,
                context_hash=decision_input_sha,
                decision_id=decision_id,
                recorded_at=frozen_at_text,
            )
            expected_sha = _record_sha(expected)
            appended_sha = ledger.append(expected)
            if appended_sha != expected_sha:
                raise ModelComputeRouterError(
                    "paired VOC scoring evidence digest is not deterministic"
                )
            ledger.verified_snapshot()

        return {
            "evaluation_id": identity,
            "decision_evidence_sha256": expected_sha,
            "admission_sha256": admission_sha,
        }

    def run_pair_and_freeze(
        self: production.VOCProductionOrchestrator,
        *,
        request_id: str,
        baseline_invoke,
        challenger_invoke,
        quote_keys: tuple[str, ...],
        evaluation_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
        """Run the exact admitted pair and freeze its canonical pre-outcome record."""

        baseline, challenger = original_run_pair(
            self,
            request_id=request_id,
            baseline_invoke=baseline_invoke,
            challenger_invoke=challenger_invoke,
        )
        frozen = freeze_pair_scoring_evidence(
            self,
            request_id=request_id,
            quote_keys=quote_keys,
            evaluation_id=evaluation_id,
        )
        return baseline, challenger, frozen

    orchestrator_cls.freeze_pair_scoring_evidence = freeze_pair_scoring_evidence
    orchestrator_cls.run_pair_and_freeze = run_pair_and_freeze
    orchestrator_cls._product_evidence_guard_v1 = True


_install()
