import pytest

from autosport import pre_evaluation_product_origin as product_origin
from autosport.decision_ledger import JsonlDecisionLedger
from autosport.paper import PaperBook
from autosport.portfolio_plan import OpportunityIntent, PortfolioDependencyGraph
from autosport.pre_evaluation_binding import BoundPreEvaluationSession
from autosport.pre_evaluation_product_origin import (
    PreEvaluationProductOrigin,
    PreEvaluationProductOriginError,
    ProductOwnedPreEvaluationSemanticSession,
)
from autosport.pre_evaluation_semantics import (
    PreEvaluationCostContract,
    PreEvaluationSemanticSession,
    ProviderSelectionBinding,
)
from autosport.provider_observation_authority import CompleteGameBoardSnapshot
from autosport.risk import PaperRiskPolicy


def _blank(cls):
    return object.__new__(cls)


def _subclass(cls):
    return type(f"Forged{cls.__name__}", (cls,), {})


def _exact_positive_inputs():
    return {
        "snapshot": _blank(CompleteGameBoardSnapshot),
        "bound": _blank(BoundPreEvaluationSession),
        "provider_selections": (_blank(ProviderSelectionBinding),),
        "intents": (_blank(OpportunityIntent),),
        "risk_policy": _blank(PaperRiskPolicy),
        "book": _blank(PaperBook),
        "dependency_graph": _blank(PortfolioDependencyGraph),
        "ledger": _blank(JsonlDecisionLedger),
    }


@pytest.mark.parametrize(
    ("field", "canonical_type", "message"),
    (
        ("snapshot", CompleteGameBoardSnapshot, "snapshot must be CompleteGameBoardSnapshot"),
        ("bound", BoundPreEvaluationSession, "bound must be BoundPreEvaluationSession"),
        ("risk_policy", PaperRiskPolicy, "risk_policy must be PaperRiskPolicy"),
        ("book", PaperBook, "book must be PaperBook"),
        (
            "dependency_graph",
            PortfolioDependencyGraph,
            "dependency_graph must be PortfolioDependencyGraph",
        ),
        ("ledger", JsonlDecisionLedger, "ledger must be JsonlDecisionLedger"),
    ),
)
def test_product_origin_rejects_polymorphic_authority_before_resolver_dispatch(
    field,
    canonical_type,
    message,
):
    kwargs = _exact_positive_inputs()
    kwargs[field] = _blank(_subclass(canonical_type))

    with pytest.raises(TypeError, match=message):
        product_origin.resolve_pre_evaluation_product_origin(
            **kwargs,
            material_action_id="material-action",
        )


def test_product_origin_rejects_polymorphic_provider_member_before_attribute_read():
    kwargs = _exact_positive_inputs()
    kwargs["provider_selections"] = (_blank(_subclass(ProviderSelectionBinding)),)

    with pytest.raises(
        TypeError,
        match="provider_selections must contain ProviderSelectionBinding values",
    ):
        product_origin.resolve_pre_evaluation_product_origin(
            **kwargs,
            material_action_id="material-action",
        )


def test_product_origin_rejects_polymorphic_intent_before_audit_payload_read():
    kwargs = _exact_positive_inputs()
    kwargs["intents"] = (_blank(_subclass(OpportunityIntent)),)

    with pytest.raises(
        TypeError,
        match="intents must be a tuple of OpportunityIntent values",
    ):
        product_origin.resolve_pre_evaluation_product_origin(
            **kwargs,
            material_action_id="material-action",
        )


def test_product_derivation_rejects_non_tuple_provider_iterable_without_iterating():
    kwargs = _exact_positive_inputs()
    iterated = False

    def forged_provider_iterable():
        nonlocal iterated
        iterated = True
        yield _blank(ProviderSelectionBinding)

    kwargs["provider_selections"] = forged_provider_iterable()

    with pytest.raises(
        TypeError,
        match="provider_selections must be a tuple of ProviderSelectionBinding values",
    ):
        product_origin.derive_product_owned_pre_evaluation_session(
            **kwargs,
            material_action_id="material-action",
        )

    assert iterated is False


def test_cost_selection_rejects_polymorphic_contract_before_durable_dispatch():
    with pytest.raises(TypeError, match="contract must be PreEvaluationCostContract"):
        product_origin.persist_pre_evaluation_cost_contract_authority(
            ledger=_blank(JsonlDecisionLedger),
            material_action_id="material-action",
            risk_policy=_blank(PaperRiskPolicy),
            contract=_blank(_subclass(PreEvaluationCostContract)),
        )


def test_product_origin_capability_rejects_polymorphic_origin_before_digest_read():
    with pytest.raises(
        PreEvaluationProductOriginError,
        match="pre-evaluation origin requires PreEvaluationProductOrigin",
    ):
        product_origin.assert_pre_evaluation_product_origin_authoritative(
            _blank(_subclass(PreEvaluationProductOrigin))
        )


def test_product_semantic_wrapper_rejects_polymorphic_session_before_origin_read():
    with pytest.raises(TypeError, match="session must be PreEvaluationSemanticSession"):
        ProductOwnedPreEvaluationSemanticSession(
            session=_blank(_subclass(PreEvaluationSemanticSession)),
            origin=_blank(PreEvaluationProductOrigin),
        )
