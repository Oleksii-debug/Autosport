"""Remove the caller-injectable entropy implementation from module capability space.

The public risk-randomization issuer is already a closure-bound function that captures
both the implementation and the import-time product entropy callable.  Keeping the
implementation itself as a module attribute would nevertheless let ordinary callers
bypass that public seal by supplying ``_product_token_bytes`` directly.  Delete only
that implementation attribute after the public closure is constructed; no estimator,
registry, randomization authority or persistence format is added here.
"""
from __future__ import annotations

from . import risk_randomization_precommit as _precommit


def _install_guard() -> None:
    implementation_name = "_issue_risk_randomization_precommit"
    implementation = getattr(_precommit, implementation_name, None)
    if implementation is None:
        return
    public_issuer = _precommit.issue_risk_randomization_precommit
    closure = getattr(public_issuer, "__closure__", None)
    if not closure or not any(cell.cell_contents is implementation for cell in closure):
        raise RuntimeError(
            "risk randomization public issuer is not bound to the canonical implementation"
        )
    delattr(_precommit, implementation_name)


_install_guard()
del _install_guard
del _precommit
