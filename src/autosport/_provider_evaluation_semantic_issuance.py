from __future__ import annotations

"""Bind #638 denominator admission to the exact #662 semantic derivation result.

A live ``PreEvaluationProductOrigin`` proves the durable product inputs that were
re-resolved, but the public wrapper constructor can otherwise pair that origin with a
new exact ``PreEvaluationSemanticSession`` carrying caller-rewritten slot semantics.
The production derivation entrypoint is therefore wrapped once and records the exact
semantic authority digest issued for each live origin.  Denominator admission requires
that same origin/digest pair before the existing row-level semantic gate runs.

The registry is intentionally private implementation state, matching the other
in-process provider-origin capability fences.  It does not make persisted/reconstructed
objects authoritative across restart; a new positive freeze must re-run the canonical
product-owned derivation.
"""

import weakref

from . import _provider_evaluation_semantic_gate as semantic_gate
from . import pre_evaluation_product_origin as product_origin
from .evaluation_universe import EvaluationRow
from .pre_evaluation_binding import BoundPreEvaluationSession
from .pre_evaluation_product_origin import (
    PreEvaluationProductOrigin,
    ProductOwnedPreEvaluationSemanticSession,
)
from .pre_evaluation_semantics import PreEvaluationSemanticSession


_ISSUED_SEMANTICS: dict[
    int,
    tuple[weakref.ReferenceType[PreEvaluationProductOrigin], str],
] = {}


def _forget_semantic_origin(
    origin_id: int,
    reference: weakref.ReferenceType[PreEvaluationProductOrigin],
) -> None:
    current = _ISSUED_SEMANTICS.get(origin_id)
    if current is not None and current[0] is reference:
        _ISSUED_SEMANTICS.pop(origin_id, None)


def _remember_product_semantic_authority(
    authority: ProductOwnedPreEvaluationSemanticSession,
) -> ProductOwnedPreEvaluationSemanticSession:
    if type(authority) is not ProductOwnedPreEvaluationSemanticSession:
        raise TypeError("authority must be exact ProductOwnedPreEvaluationSemanticSession")
    if type(authority.session) is not PreEvaluationSemanticSession:
        raise TypeError("authority session must be exact PreEvaluationSemanticSession")
    origin = authority.origin
    origin_id = id(origin)
    reference = weakref.ref(
        origin,
        lambda current, origin_id=origin_id: _forget_semantic_origin(
            origin_id, current
        ),
    )
    _ISSUED_SEMANTICS[origin_id] = (
        reference,
        authority.session.authority_digest,
    )
    return authority


def _assert_product_semantic_authority_issued(
    authority: ProductOwnedPreEvaluationSemanticSession,
) -> None:
    if type(authority) is not ProductOwnedPreEvaluationSemanticSession:
        raise ValueError("product semantic authority must use the exact wrapper type")
    if type(authority.session) is not PreEvaluationSemanticSession:
        raise ValueError("product semantic authority must use the exact session type")
    origin = authority.origin
    issued = _ISSUED_SEMANTICS.get(id(origin))
    if (
        issued is None
        or issued[0]() is not origin
        or issued[1] != authority.session.authority_digest
    ):
        raise ValueError(
            "product semantic authority was not issued by canonical product derivation"
        )


def _install_derivation_issuance() -> None:
    original = product_origin.derive_product_owned_pre_evaluation_session
    if getattr(original, "_provider_denominator_semantic_issuance", False):
        return

    def derive_product_owned_pre_evaluation_session(**kwargs):
        return _remember_product_semantic_authority(original(**kwargs))

    setattr(
        derive_product_owned_pre_evaluation_session,
        "_provider_denominator_semantic_issuance",
        True,
    )
    product_origin.derive_product_owned_pre_evaluation_session = (
        derive_product_owned_pre_evaluation_session
    )


def _install_denominator_issuance_gate() -> None:
    original = semantic_gate._validate_product_semantic_authority
    if getattr(original, "_product_semantic_issuance_required", False):
        return

    def _validate_product_semantic_authority(*, pre_evaluation_authority, **kwargs):
        # Preserve the exact-type error ordering owned by the #638 gate. Rows and the
        # bound capability must be admitted before this wrapper reads nested semantic
        # authority state; otherwise a subclass or malformed capability could run code
        # before the primary fail-closed fence.
        rows = kwargs.get("rows", ())
        pre_evaluation_bound = kwargs.get("pre_evaluation_bound")
        if (
            all(type(row) is EvaluationRow for row in rows)
            and type(pre_evaluation_bound) is BoundPreEvaluationSession
            and type(pre_evaluation_authority)
            is ProductOwnedPreEvaluationSemanticSession
            and type(pre_evaluation_authority.session) is PreEvaluationSemanticSession
        ):
            try:
                _assert_product_semantic_authority_issued(pre_evaluation_authority)
            except Exception as exc:
                from . import provider_evaluation_universe as provider_consumer

                raise provider_consumer.ProviderEvaluationUniverseError(
                    "pre-evaluation semantic session was not issued by canonical product derivation"
                ) from exc
        return original(
            pre_evaluation_authority=pre_evaluation_authority,
            **kwargs,
        )

    setattr(
        _validate_product_semantic_authority,
        "_product_semantic_issuance_required",
        True,
    )
    semantic_gate._validate_product_semantic_authority = (
        _validate_product_semantic_authority
    )


_install_derivation_issuance()
_install_denominator_issuance_gate()
del _install_derivation_issuance
del _install_denominator_issuance_gate

__all__: list[str] = []
