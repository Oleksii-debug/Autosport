"""Fail closed if durable START activation changes after its COMMIT.

The canonical ProductDecisionActivationStore already performs PREPARE -> publish ->
COMMIT under its sealed monotonic authority.  This product-composition guard narrows
the remaining caller-visible boundary: initialize_owner() does not return a positive
START binding until the canonical sealed load() has re-read the committed workspace
bytes and revalidated them against the same monotonic witness.

No second activation store or authority is introduced.  The guard only composes the
already-sealed canonical initialize/load methods, and it grants no execution or
REAL-money authority.
"""

from __future__ import annotations

from typing import Final

from .product_decision_activation import (
    ProductDecisionActivationBinding,
    ProductDecisionActivationError,
    ProductDecisionActivationStore,
)


def _install_commit_return_guard() -> None:
    store_class = ProductDecisionActivationStore
    canonical_initialize = store_class.__dict__.get("initialize_owner")
    canonical_load = store_class.__dict__.get("load")
    canonical_initialize_code = getattr(canonical_initialize, "__code__", None)
    canonical_load_code = getattr(canonical_load, "__code__", None)

    if (
        canonical_initialize is None
        or canonical_load is None
        or canonical_initialize_code is None
        or canonical_load_code is None
    ):
        raise RuntimeError(
            "canonical product decision activation return authority is unavailable"
        )

    def initialize_owner(
        self: ProductDecisionActivationStore,
        *args: object,
        **kwargs: object,
    ) -> ProductDecisionActivationBinding:
        if type(self) is not store_class:
            raise ProductDecisionActivationError(
                "product decision activation store must be the exact canonical class"
            )
        if (
            getattr(canonical_initialize, "__code__", None)
            is not canonical_initialize_code
            or store_class.__dict__.get("load") is not canonical_load
            or getattr(canonical_load, "__code__", None) is not canonical_load_code
            or store_class.__dict__.get("initialize_owner") is not initialize_owner
        ):
            raise ProductDecisionActivationError(
                "product decision activation post-COMMIT authority changed"
            )

        persisted = canonical_initialize(self, *args, **kwargs)

        # The canonical initializer has now durably COMMITted (or returned an exact
        # idempotent pre-existing binding), but the caller has not yet received
        # positive START authority.  Canonical load performs an exact local read plus
        # monotonic-authority recovery under the existing sealed dispatch graph.
        committed = canonical_load(self)
        if committed != persisted:
            raise ProductDecisionActivationError(
                "committed product decision activation changed before START return"
            )

        # Re-check guard composition after the authority-bearing read so a concurrent
        # class-level rebind cannot turn this wrapper into an unsealed return path.
        if (
            store_class.__dict__.get("initialize_owner") is not initialize_owner
            or store_class.__dict__.get("load") is not canonical_load
            or getattr(canonical_load, "__code__", None) is not canonical_load_code
        ):
            raise ProductDecisionActivationError(
                "product decision activation post-COMMIT authority changed"
            )
        return committed

    setattr(store_class, "initialize_owner", initialize_owner)


_install_commit_return_guard()
del _install_commit_return_guard
