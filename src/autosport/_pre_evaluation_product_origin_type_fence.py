from __future__ import annotations

"""Exact-type fence for positive pre-evaluation product-origin authority.

``pre_evaluation_product_origin`` already re-resolves positive semantic inputs from
runtime-issued provider evidence and durable product-owned records.  Those resolvers
consume values through public Python properties and methods, however, so accepting a
subclass at the outer mint boundary would let caller-polymorphic code run before the
durable equality checks.  Positive authority is stricter than ordinary structural
compatibility: every caller-owned authority-bearing value is therefore required to be
the exact canonical concrete type before any resolver dispatch or iterable execution.
"""

from typing import Any

from . import pre_evaluation_product_origin as _origin
from .decision_ledger import JsonlDecisionLedger
from .paper import PaperBook
from .portfolio_plan import OpportunityIntent, PortfolioDependencyGraph
from .pre_evaluation_binding import BoundPreEvaluationSession
from .pre_evaluation_product_origin import (
    PreEvaluationProductOrigin,
    PreEvaluationProductOriginError,
    ProductOwnedPreEvaluationSemanticSession,
)
from .pre_evaluation_semantics import (
    PreEvaluationCostContract,
    PreEvaluationSemanticSession,
    ProviderSelectionBinding,
)
from .provider_observation_authority import CompleteGameBoardSnapshot
from .risk import PaperRiskPolicy


_ORIGINAL_PERSIST_COST_AUTHORITY = _origin.persist_pre_evaluation_cost_contract_authority
_ORIGINAL_RESOLVE_PRODUCT_ORIGIN = _origin.resolve_pre_evaluation_product_origin
_ORIGINAL_DERIVE_PRODUCT_SESSION = _origin.derive_product_owned_pre_evaluation_session
_ORIGINAL_ASSERT_PRODUCT_ORIGIN = _origin.assert_pre_evaluation_product_origin_authoritative
_ORIGINAL_PRODUCT_SESSION_POST_INIT = ProductOwnedPreEvaluationSemanticSession.__post_init__


def _require_exact_type(value: object, expected: type[Any], message: str) -> None:
    if type(value) is not expected:
        raise TypeError(message)


def _require_exact_positive_inputs(
    *,
    snapshot: CompleteGameBoardSnapshot,
    bound: BoundPreEvaluationSession,
    provider_selections: tuple[ProviderSelectionBinding, ...],
    intents: tuple[OpportunityIntent, ...],
    risk_policy: PaperRiskPolicy,
    book: PaperBook,
    dependency_graph: PortfolioDependencyGraph,
    ledger: JsonlDecisionLedger,
) -> None:
    """Fence all caller-owned values before any authority-bearing attribute read."""

    _require_exact_type(
        snapshot,
        CompleteGameBoardSnapshot,
        "snapshot must be CompleteGameBoardSnapshot",
    )
    _require_exact_type(bound, BoundPreEvaluationSession, "bound must be BoundPreEvaluationSession")
    if type(provider_selections) is not tuple:
        raise TypeError("provider_selections must be a tuple of ProviderSelectionBinding values")
    if any(type(item) is not ProviderSelectionBinding for item in provider_selections):
        raise TypeError("provider_selections must contain ProviderSelectionBinding values")
    if type(intents) is not tuple or any(type(intent) is not OpportunityIntent for intent in intents):
        raise TypeError("intents must be a tuple of OpportunityIntent values")
    _require_exact_type(risk_policy, PaperRiskPolicy, "risk_policy must be PaperRiskPolicy")
    _require_exact_type(book, PaperBook, "book must be PaperBook")
    _require_exact_type(
        dependency_graph,
        PortfolioDependencyGraph,
        "dependency_graph must be PortfolioDependencyGraph",
    )
    _require_exact_type(ledger, JsonlDecisionLedger, "ledger must be JsonlDecisionLedger")


def persist_pre_evaluation_cost_contract_authority(
    *,
    ledger: JsonlDecisionLedger,
    material_action_id: str,
    risk_policy: PaperRiskPolicy,
    contract: PreEvaluationCostContract,
):
    """Reject polymorphic cost-selection authorities before durable dispatch."""

    _require_exact_type(ledger, JsonlDecisionLedger, "ledger must be JsonlDecisionLedger")
    _require_exact_type(risk_policy, PaperRiskPolicy, "risk_policy must be PaperRiskPolicy")
    _require_exact_type(contract, PreEvaluationCostContract, "contract must be PreEvaluationCostContract")
    return _ORIGINAL_PERSIST_COST_AUTHORITY(
        ledger=ledger,
        material_action_id=material_action_id,
        risk_policy=risk_policy,
        contract=contract,
    )


def resolve_pre_evaluation_product_origin(
    *,
    snapshot: CompleteGameBoardSnapshot,
    bound: BoundPreEvaluationSession,
    provider_selections: tuple[ProviderSelectionBinding, ...],
    intents: tuple[OpportunityIntent, ...],
    risk_policy: PaperRiskPolicy,
    book: PaperBook,
    dependency_graph: PortfolioDependencyGraph,
    ledger: JsonlDecisionLedger,
    material_action_id: str,
    expected_cost_contract_sha256: str | None = None,
):
    """Exact-fence the sole public product-origin mint before re-resolution."""

    _require_exact_positive_inputs(
        snapshot=snapshot,
        bound=bound,
        provider_selections=provider_selections,
        intents=intents,
        risk_policy=risk_policy,
        book=book,
        dependency_graph=dependency_graph,
        ledger=ledger,
    )
    return _ORIGINAL_RESOLVE_PRODUCT_ORIGIN(
        snapshot=snapshot,
        bound=bound,
        provider_selections=provider_selections,
        intents=intents,
        risk_policy=risk_policy,
        book=book,
        dependency_graph=dependency_graph,
        ledger=ledger,
        material_action_id=material_action_id,
        expected_cost_contract_sha256=expected_cost_contract_sha256,
    )


def derive_product_owned_pre_evaluation_session(
    *,
    snapshot: CompleteGameBoardSnapshot,
    bound: BoundPreEvaluationSession,
    provider_selections: tuple[ProviderSelectionBinding, ...],
    intents: tuple[OpportunityIntent, ...],
    risk_policy: PaperRiskPolicy,
    book: PaperBook,
    dependency_graph: PortfolioDependencyGraph,
    ledger: JsonlDecisionLedger,
    material_action_id: str,
    expected_cost_contract_sha256: str | None = None,
):
    """Fence before the legacy derive function can materialize an iterable."""

    _require_exact_positive_inputs(
        snapshot=snapshot,
        bound=bound,
        provider_selections=provider_selections,
        intents=intents,
        risk_policy=risk_policy,
        book=book,
        dependency_graph=dependency_graph,
        ledger=ledger,
    )
    return _ORIGINAL_DERIVE_PRODUCT_SESSION(
        snapshot=snapshot,
        bound=bound,
        provider_selections=provider_selections,
        intents=intents,
        risk_policy=risk_policy,
        book=book,
        dependency_graph=dependency_graph,
        ledger=ledger,
        material_action_id=material_action_id,
        expected_cost_contract_sha256=expected_cost_contract_sha256,
    )


def assert_pre_evaluation_product_origin_authoritative(
    origin: PreEvaluationProductOrigin,
) -> None:
    if type(origin) is not PreEvaluationProductOrigin:
        raise PreEvaluationProductOriginError(
            "pre-evaluation origin requires PreEvaluationProductOrigin"
        )
    _ORIGINAL_ASSERT_PRODUCT_ORIGIN(origin)


def _product_session_post_init(self: ProductOwnedPreEvaluationSemanticSession) -> None:
    if type(self.session) is not PreEvaluationSemanticSession:
        raise TypeError("session must be PreEvaluationSemanticSession")
    if type(self.origin) is not PreEvaluationProductOrigin:
        raise PreEvaluationProductOriginError(
            "pre-evaluation origin requires PreEvaluationProductOrigin"
        )
    _ORIGINAL_PRODUCT_SESSION_POST_INIT(self)


# Install the exact external mint fence before #638 wraps canonical derivation for
# in-process issuance tracking. Internal durable/value re-resolution remains owned by
# pre_evaluation_product_origin; this module adds no second registry or authority.
_origin.persist_pre_evaluation_cost_contract_authority = persist_pre_evaluation_cost_contract_authority
_origin.resolve_pre_evaluation_product_origin = resolve_pre_evaluation_product_origin
_origin.derive_product_owned_pre_evaluation_session = derive_product_owned_pre_evaluation_session
_origin.assert_pre_evaluation_product_origin_authoritative = assert_pre_evaluation_product_origin_authoritative
ProductOwnedPreEvaluationSemanticSession.__post_init__ = _product_session_post_init


__all__: list[str] = []
