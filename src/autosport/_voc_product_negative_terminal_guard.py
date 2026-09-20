from __future__ import annotations

"""Install product-owned negative terminal closure for admitted VOC attempts.

The scoring authority already knows how to price a failed/late/cancelled paired
attempt from immutable ModelComputeRouter execution authority.  This guard makes
that safe primitive part of the production orchestrator lifecycle, with exact-once
restart semantics and collision checks against a successful scoring terminal.
"""

from collections.abc import Mapping

from . import voc_outcome_scoring as scoring
from . import voc_production_orchestrator as production
from .decision_ledger import DecisionRecord
from .model_compute_router import ModelComputeRouterError
from .workspace_lock import WorkspaceEconomicLock

_BINDING_KEY = "voc_binding"
_TERMINAL_KEY = "voc_terminal"


def _record_sha(record: DecisionRecord) -> str:
    return production._digest(record.to_dict())


def _matching_terminals(
    records: list[DecisionRecord],
    *,
    decision_context_sha256: str,
    admission_sha256: str,
) -> list[DecisionRecord]:
    result: list[DecisionRecord] = []
    for record in records:
        payload = record.payload
        if not isinstance(payload, Mapping):
            continue
        binding = payload.get(_BINDING_KEY)
        if (
            isinstance(binding, Mapping)
            and binding.get("decision_context_sha256") == decision_context_sha256
        ):
            result.append(record)
            continue
        terminal = payload.get(_TERMINAL_KEY)
        if (
            isinstance(terminal, Mapping)
            and terminal.get("admission_sha256") == admission_sha256
        ):
            result.append(record)
    return result


def _install() -> None:
    orchestrator_cls = production.VOCProductionOrchestrator
    if getattr(orchestrator_cls, "_product_negative_terminal_guard_v1", False):
        return

    def close_pair_negative_terminal(
        self: production.VOCProductionOrchestrator,
        *,
        request_id: str,
        status: str,
        execution_id: str,
        recorded_at: str,
    ) -> dict[str, str]:
        """Close one failed paired attempt from canonical execution authority.

        ``execution_id`` is only a reference.  Cost, latency, identity and
        disposition are re-resolved by ``append_paired_voc_terminal`` from the
        router's immutable execution-authority journal; caller-authored numeric
        measurements never enter the terminal.
        """

        request = production._text("request_id", request_id)
        status_text = production._text("status", status)
        execution = production._text("execution_id", execution_id)
        terminal_at = production._text("recorded_at", recorded_at)
        production._instant("recorded_at", terminal_at)
        ledger = self.decision_ledger
        if ledger is None:
            raise ModelComputeRouterError(
                "negative paired VOC terminal requires canonical DecisionLedger authority"
            )

        admission_sha = self._ensure_pair_admission(request)
        precompute = self.router_store.get_voc_precompute_admission(request)
        if precompute is None:
            raise ModelComputeRouterError(
                "negative paired VOC terminal requires canonical precompute admission"
            )
        context_sha = production._sha(
            "decision_context_sha256",
            precompute.get("decision_context_sha256"),
        )

        with WorkspaceEconomicLock(self.path.parent):
            ledger.verified_snapshot()
            records = ledger.verified_records()
            terminals = _matching_terminals(
                records,
                decision_context_sha256=context_sha,
                admission_sha256=admission_sha,
            )
            if len(terminals) > 1:
                raise ModelComputeRouterError(
                    "paired VOC admission has ambiguous terminal records"
                )
            if terminals:
                existing = terminals[0]
                payload = existing.payload
                terminal = (
                    payload.get(_TERMINAL_KEY)
                    if isinstance(payload, Mapping)
                    else None
                )
                if not isinstance(terminal, Mapping):
                    raise ModelComputeRouterError(
                        "paired VOC admission already has a successful scoring terminal"
                    )
                if (
                    terminal.get("status") != status_text
                    or terminal.get("execution_id") != execution
                ):
                    raise ModelComputeRouterError(
                        "paired VOC negative terminal conflicts with durable product authority"
                    )
                return {
                    "admission_sha256": admission_sha,
                    "terminal_sha256": _record_sha(existing),
                    "status": status_text,
                    "execution_id": execution,
                }

            terminal_sha = scoring.append_paired_voc_terminal(
                ledger,
                compute_execution_store=self.router_store,
                admission_sha256=admission_sha,
                status=status_text,
                execution_id=execution,
                replay_run_id=f"voc-production-negative:{request}",
                agent="voc-production-orchestrator",
                recorded_at=terminal_at,
            )
            ledger.verified_snapshot()

        return {
            "admission_sha256": admission_sha,
            "terminal_sha256": terminal_sha,
            "status": status_text,
            "execution_id": execution,
        }

    orchestrator_cls.close_pair_negative_terminal = close_pair_negative_terminal
    orchestrator_cls._product_negative_terminal_guard_v1 = True


_install()
