from __future__ import annotations

"""Fail-closed exact-type admission at the canonical policy utility store.

``PolicyUtilityStore`` is the durable mutation authority for schema-v1 utility
records.  A subclass must never reach semantic-key evaluation or serialization:
its overridable properties/``to_dict`` methods could otherwise influence the
successor image and only be rejected after that image was durably published.
"""

from . import policy_utility_evidence as _utility


_ORIGINAL_APPEND = _utility.PolicyUtilityStore.append


def _append_exact_policy_utility(
    self: _utility.PolicyUtilityStore,
    evidence: _utility.PolicyUtilityEvidence,
) -> bool:
    if type(evidence) is not _utility.PolicyUtilityEvidence:
        raise _utility.PolicyUtilityError(
            "append requires exact PolicyUtilityEvidence"
        )
    return _ORIGINAL_APPEND(self, evidence)


_utility.PolicyUtilityStore.append = _append_exact_policy_utility


__all__: list[str] = []
