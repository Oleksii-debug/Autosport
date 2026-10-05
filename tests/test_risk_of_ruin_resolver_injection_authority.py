from __future__ import annotations

import inspect

from autosport.risk_of_ruin_authority import verify_risk_of_ruin_authority


AVAILABLE_BY = "2026-01-01T00:00:00+00:00"


class _HostileKind(str):
    comparisons = 0

    def __hash__(self) -> int:
        type(self).comparisons += 1
        return super().__hash__()

    def __eq__(self, other: object) -> bool:
        type(self).comparisons += 1
        return super().__eq__(other)


class _HostilePathLike:
    calls = 0

    def __fspath__(self) -> str:
        type(self).calls += 1
        return "/tmp/forged-risk-registry.json"


class _HostileEvidence:
    reads = 0

    def __getattribute__(self, name: str):
        if name not in {"reads", "__class__"}:
            type(self).reads += 1
        return super().__getattribute__(name)


def test_public_risk_authority_does_not_expose_product_resolver_injection() -> None:
    """A caller must not be able to replace the product-owned positive issuer seam."""

    signature = inspect.signature(verify_risk_of_ruin_authority)

    assert "_product_evaluator_resolver" not in signature.parameters
    assert "product_evaluator_resolver" not in signature.parameters


def test_public_risk_authority_has_no_writable_resolver_closure() -> None:
    """Positive authority must not depend on caller-writable closure state."""

    assert verify_risk_of_ruin_authority.__closure__ is None
    assert verify_risk_of_ruin_authority.__code__.co_freevars == ()
    assert "_resolve_product_evaluator_result" not in verify_risk_of_ruin_authority.__code__.co_names



def test_public_verifier_rejects_hostile_kind_before_comparison() -> None:
    _HostileKind.comparisons = 0

    allowed, reason = verify_risk_of_ruin_authority(
        None,
        object(),
        kind=_HostileKind("single"),
        available_by=AVAILABLE_BY,
    )

    assert not allowed
    assert "kind is invalid" in reason
    assert _HostileKind.comparisons == 0


def test_public_verifier_rejects_pathlike_before_fspath() -> None:
    _HostilePathLike.calls = 0

    allowed, reason = verify_risk_of_ruin_authority(
        _HostilePathLike(),
        object(),
        kind="single",
        available_by=AVAILABLE_BY,
    )

    assert not allowed
    assert "registry path is invalid" in reason
    assert _HostilePathLike.calls == 0


def test_public_verifier_rejects_arbitrary_evidence_before_attribute_reads() -> None:
    _HostileEvidence.reads = 0
    evidence = _HostileEvidence()

    allowed, reason = verify_risk_of_ruin_authority(
        None,
        evidence,
        kind="single",
        available_by=AVAILABLE_BY,
    )

    assert not allowed
    assert "evidence type is invalid" in reason
    assert _HostileEvidence.reads == 0
