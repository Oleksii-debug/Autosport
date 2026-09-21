"""Fail closed when a ScientificRegistry read observes rolled-back durable bytes.

ScientificRegistry publication already advances an independent machine-local
MonotonicWorkspaceAuthority.  This runtime guard makes every registry read consume
the exact authority-verified image under the same durable path fence, so a valid
older JSON prefix cannot be replayed as current scientific truth between writes.
"""

from __future__ import annotations

import json
from typing import Any

from .integrity import read_verified_scientific_registry_text
from . import scientific_registry as _registry


def _read_authority_verified(self: _registry.ScientificRegistry) -> dict[str, Any]:
    raw = read_verified_scientific_registry_text(self.path)
    try:
        state = json.loads(
            raw,
            object_pairs_hook=_registry._reject_duplicate_keys,
            parse_constant=_registry._reject_nonfinite,
        )
    except json.JSONDecodeError as exc:
        raise ValueError("scientific registry must be valid UTF-8 JSON") from exc
    if type(state) is not dict or state.get("schema_version") != self.SCHEMA_VERSION:
        raise ValueError("scientific registry schema_version mismatch")
    records = state.get("records")
    if type(records) is not list:
        raise ValueError("scientific registry records must be a list")
    seen: set[tuple[str, str]] = set()
    fingerprints: set[str] = set()
    for raw_entry in records:
        self._validate_entry(raw_entry)
        key = (raw_entry["record_type"], raw_entry["record_id"])
        if key in seen:
            raise ValueError("scientific registry contains duplicate record identity")
        seen.add(key)
        if raw_entry["record_type"] == "Experiment":
            fingerprint = raw_entry["payload"].get("fingerprint")
            if fingerprint in fingerprints:
                # Historical explicit repeats are represented by allow_repeat and
                # therefore may share a fingerprint. Preserve existing semantics.
                pass
            fingerprints.add(fingerprint)
    return state


_registry.ScientificRegistry._read = _read_authority_verified
