"""Canonicalize predictive-target timestamp identity.

The factory training-points manifest already normalizes timezone-aware instants to
UTC before hashing.  Predictive-target contract and population identities must use
the same semantic instant identity; otherwise equivalent ISO-8601 spellings such
as ``Z`` and ``+00:00`` can produce different hashes for the same frozen contract
or the same already-frozen training population.

This is identity hardening only.  It does not verify outcome-label semantics or
grant model-output probability, calibration, forecast, promotion, allocation,
or execution authority.
"""

from __future__ import annotations

from typing import Sequence

from . import predictive_target_semantics as _target
from ._strategy_model_factory_impl import TrainingPoint


_ORIGINAL_CONTRACT_CANONICAL_PAYLOAD = (
    _target.PredictiveTargetContract.canonical_payload
)


def _canonical_instant_text(value: object, name: str) -> str:
    return _target._instant(value, name).isoformat()


def _canonical_contract_payload(
    self: _target.PredictiveTargetContract,
) -> dict[str, object]:
    payload = _ORIGINAL_CONTRACT_CANONICAL_PAYLOAD(self)
    payload["frozen_at"] = _canonical_instant_text(self.frozen_at, "frozen_at")
    return payload


def _canonical_target_population_digest(
    *,
    contract_sha256: str,
    training_manifest_sha256: str,
    ordered_points: Sequence[TrainingPoint],
) -> str:
    return _target._canonical_digest(
        {
            "schema_version": 1,
            "kind": "autosport-binary-target-population-v1",
            "contract_sha256": contract_sha256,
            "training_points_manifest_sha256": training_manifest_sha256,
            "targets": [
                {
                    "observed_at": _canonical_instant_text(
                        point.observed_at,
                        "training point observed_at",
                    ),
                    "target": int(point.target),
                    "target_available_at": _canonical_instant_text(
                        point.target_reveal_at,
                        "training point target_available_at",
                    ),
                    "evidence_sha256s": list(point.evidence_sha256s),
                }
                for point in ordered_points
            ],
        }
    )


def install() -> None:
    if getattr(
        _target,
        "_autosport_target_timestamp_canonicalization_installed",
        False,
    ):
        return
    _target.PredictiveTargetContract.canonical_payload = _canonical_contract_payload
    _target._target_population_digest = _canonical_target_population_digest
    _target._autosport_target_timestamp_canonicalization_installed = True


install()
