from __future__ import annotations

from decimal import Decimal

from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy


def test_owner_facing_evaluate_root_cannot_be_replaced() -> None:
    """Post-import class replacement must not become positive risk authority."""

    book = PaperBook("100")
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )
    original = vars(PaperRiskPolicy)["evaluate"]
    hostile_executed = False

    def hostile_evaluate(self, candidate_book, stake, *, context=None):
        nonlocal hostile_executed
        del self, candidate_book, stake, context
        hostile_executed = True
        raise AssertionError("replaced risk root must never execute")

    replacement_rejected = False
    try:
        try:
            setattr(PaperRiskPolicy, "evaluate", hostile_evaluate)
        except TypeError:
            replacement_rejected = True

        if not replacement_rejected:
            try:
                decision = policy.evaluate(book, Decimal("10"))
            except (TypeError, ValueError):
                decision = None
            assert hostile_executed is False
            if decision is not None:
                assert decision.allowed is False
    finally:
        if vars(PaperRiskPolicy).get("evaluate") is not original:
            type.__setattr__(PaperRiskPolicy, "evaluate", original)


def test_owner_facing_derive_root_cannot_be_deleted() -> None:
    """The canonical sizing entry cannot be removed after authority composition."""

    original = vars(PaperRiskPolicy)["derive_goal_stake"]
    deletion_rejected = False
    try:
        try:
            delattr(PaperRiskPolicy, "derive_goal_stake")
        except TypeError:
            deletion_rejected = True

        if not deletion_rejected:
            assert "derive_goal_stake" in vars(PaperRiskPolicy)
    finally:
        if vars(PaperRiskPolicy).get("derive_goal_stake") is not original:
            type.__setattr__(PaperRiskPolicy, "derive_goal_stake", original)


def test_root_dispatch_seal_preserves_canonical_policy_class_identity() -> None:
    """Authority sealing must not publish a facade subclass as a second policy class."""

    # PaperRiskPolicy is defined as the product's canonical dataclass, directly over
    # object. A post-composition subclass facade changes type identity for objects and
    # modules that captured the original class even when it delegates all behavior.
    assert PaperRiskPolicy.__bases__ == (object,)


def test_owner_facing_root_cannot_be_replaced_via_base_type_api() -> None:
    """A metaclass override alone must not leave type.__setattr__ as an authority bypass."""

    book = PaperBook("100")
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )
    original = vars(PaperRiskPolicy)["evaluate"]
    hostile_executed = False

    def hostile_evaluate(self, candidate_book, stake, *, context=None):
        nonlocal hostile_executed
        del self, candidate_book, stake, context
        hostile_executed = True
        raise AssertionError("base-metaclass root replacement must never execute")

    try:
        replacement_rejected = False
        try:
            type.__setattr__(PaperRiskPolicy, "evaluate", hostile_evaluate)
        except TypeError:
            replacement_rejected = True

        if not replacement_rejected:
            try:
                decision = policy.evaluate(book, Decimal("10"))
            except (TypeError, ValueError):
                decision = None
            assert hostile_executed is False
            if decision is not None:
                assert decision.allowed is False
    finally:
        if vars(PaperRiskPolicy).get("evaluate") is not original:
            type.__setattr__(PaperRiskPolicy, "evaluate", original)


def test_sealed_policy_cannot_be_subclassed_into_alternate_root_authority() -> None:
    """A subclass override must not become an alternate positive risk-policy authority."""

    subclass_rejected = False
    try:
        class HostilePaperRiskPolicy(PaperRiskPolicy):
            def evaluate(self, book, stake, *, context=None):
                del self, book, stake, context
                return None
    except TypeError:
        subclass_rejected = True

    assert subclass_rejected is True
