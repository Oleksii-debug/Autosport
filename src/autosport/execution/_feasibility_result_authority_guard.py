"""Seal the execution-feasibility positive-result convenience boundary.

The canonical issuance registry and authoritative assessment remain owned by
``execution.feasibility``.  This composition guard only prevents ordinary imports
from replacing the module-global verification callable consulted by
``ExecutionFeasibilitySnapshot.sufficient`` and thereby relabelling a caller-made
snapshot as positive.
"""

from __future__ import annotations

from . import feasibility as _feasibility


def _install() -> None:
    snapshot_type = _feasibility.ExecutionFeasibilitySnapshot
    state_type = _feasibility.FeasibilityState
    canonical_verify = _feasibility._is_execution_feasibility_result_authoritative
    original_descriptor = snapshot_type.__dict__.get("sufficient")

    if not isinstance(original_descriptor, property):
        raise RuntimeError("execution feasibility sufficient descriptor is unavailable")
    if not callable(canonical_verify):
        raise RuntimeError("canonical execution feasibility verifier is unavailable")

    positive_state = state_type.SNAPSHOT_DEPTH_SUFFICIENT_BUT_RACY

    def sufficient(self) -> bool:
        return self.state is positive_state and canonical_verify(self)

    snapshot_type.sufficient = property(
        sufficient,
        doc=original_descriptor.__doc__,
    )
    snapshot_meta = type(snapshot_type)
    seal = getattr(snapshot_meta, "seal", None)
    if not callable(seal):
        raise RuntimeError(
            "execution feasibility sufficient authority metaclass is unavailable"
        )
    seal(snapshot_type)

    # The property now closes over the exact canonical verifier.  Leaving these
    # module globals present would preserve an ordinary-import mutation/mint surface
    # even though the sealed property no longer consumes it.
    delattr(_feasibility, "_is_execution_feasibility_result_authoritative")
    if hasattr(_feasibility, "_install_execution_feasibility_result_authority"):
        delattr(_feasibility, "_install_execution_feasibility_result_authority")


_install()
del _install
del _feasibility
