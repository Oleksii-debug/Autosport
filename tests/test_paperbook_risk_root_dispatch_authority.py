from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

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

    assert PaperRiskPolicy.__bases__ == (object,)


def test_owner_facing_root_cannot_be_replaced_via_base_type_api() -> None:
    """Metaclass data descriptors must close explicit type.__setattr__ bypass."""

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


def test_owner_facing_root_cannot_be_deleted_via_base_type_api() -> None:
    """Metaclass data descriptors must close explicit type.__delattr__ bypass."""

    original = vars(PaperRiskPolicy)["derive_goal_stake_vector"]
    deletion_rejected = False
    try:
        try:
            type.__delattr__(PaperRiskPolicy, "derive_goal_stake_vector")
        except TypeError:
            deletion_rejected = True

        if not deletion_rejected:
            assert "derive_goal_stake_vector" in vars(PaperRiskPolicy)
    finally:
        if vars(PaperRiskPolicy).get("derive_goal_stake_vector") is not original:
            type.__setattr__(PaperRiskPolicy, "derive_goal_stake_vector", original)


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


def test_root_seal_preserves_dataclass_replace_and_instance_dispatch() -> None:
    """Root sealing must not break canonical dataclass cloning used by vector allocation."""

    original = PaperRiskPolicy(
        max_ticket_fraction=Decimal("0.10"),
        max_committed_fraction=Decimal("0.50"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )
    cloned = replace(original, max_ticket_fraction=Decimal("0.20"))
    book = PaperBook("100")

    assert type(cloned) is PaperRiskPolicy
    assert cloned.max_ticket_fraction == Decimal("0.20")
    decision = cloned.evaluate(book, Decimal("1"))
    assert decision.allowed is True


def test_book_state_root_cannot_be_retargeted_or_deleted() -> None:
    """Reconstructed delegates must not bypass admission by replacing _book_state."""

    original = vars(PaperRiskPolicy)["_book_state"]
    assert type(original) is classmethod
    hostile_executed = False

    def hostile_book_state(cls, book):
        nonlocal hostile_executed
        del cls, book
        hostile_executed = True
        return (Decimal("100"), Decimal("100"), Decimal("0"), 0)

    hostile_descriptor = classmethod(hostile_book_state)

    with pytest.raises(TypeError, match="canonical PaperRiskPolicy root is sealed"):
        setattr(PaperRiskPolicy, "_book_state", hostile_descriptor)
    with pytest.raises(TypeError, match="canonical PaperRiskPolicy root is sealed"):
        type.__setattr__(PaperRiskPolicy, "_book_state", hostile_descriptor)
    with pytest.raises(TypeError, match="canonical PaperRiskPolicy root is sealed"):
        delattr(PaperRiskPolicy, "_book_state")
    with pytest.raises(TypeError, match="canonical PaperRiskPolicy root is sealed"):
        type.__delattr__(PaperRiskPolicy, "_book_state")

    assert vars(PaperRiskPolicy)["_book_state"] is original
    resolved = PaperRiskPolicy._book_state
    assert resolved.__func__ is original.__func__
    assert resolved.__self__ is PaperRiskPolicy
    assert hostile_executed is False
