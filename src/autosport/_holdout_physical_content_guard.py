"""Bind confirmation-holdout freshness to canonical physical evaluation content.

``promotion_holdout_access_id`` is an immutable provenance/attempt identity.  It may
therefore change when a protocol, source label, licence record, or confirmation
family changes.  Those metadata changes do not make already-observed evaluation
points unseen again.

This guard composes the existing #716 holdout ledger and ScientificRegistry instead
of adding another store:

* new ledger freshness keys depend only on the canonical DatasetSnapshot manifest;
* legacy freshness ids remain readable, then are re-indexed in memory by physical
  content so an old record blocks a relabelled consume;
* factory PromotionEvidence history resolves every DatasetSnapshot through the
  canonical registry and treats the same manifest as consumed unless it is the exact
  same frozen attempt being resumed.

The durable access id remains unchanged and continues to carry provenance.  A truly
distinct frozen holdout must therefore have a distinct canonical content/subset
manifest rather than merely different authority labels.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Iterable, Mapping

from . import _point_in_time_authority_runtime_repair as _runtime
from . import _strategy_model_factory_impl as _factory
from . import point_in_time_evidence as _evidence


# Capture the already-composed #716 behavior.  The runtime repair has been installed
# before this module is imported from package __init__.
_LEGACY_FRESHNESS_ID = _evidence._holdout_freshness_id
_ORIGINAL_LEDGER_LOAD = _evidence.HoldoutConsumptionLedger._load
_ORIGINAL_FACTORY_HELPER = _factory._holdout_consumed_by_other_evidence
_ORIGINAL_FACTORY_RUN = _factory.ExperimentRunner.run_baseline_candidate
_ORIGINAL_RUNTIME_INSTALL = _runtime._install_runtime_guards
_ACTIVE_FACTORY_REGISTRY: ContextVar[object | None] = ContextVar(
    "autosport_physical_holdout_registry",
    default=None,
)


def _physical_freshness_id(
    *,
    dataset_manifest_sha256: str,
    source_identity: str,
    license_identity: str,
    confirmation_trial_family_id: str,
) -> str:
    """Return reset-resistant identity for one exact governed point population.

    The extra parameters stay in the signature for compatibility with the canonical
    ledger call surface.  They are validated as provenance by HoldoutConsumption and
    ``promotion_holdout_access_id`` but intentionally cannot mint fresh capacity.
    """

    _evidence._text(source_identity, "source_identity")
    _evidence._text(license_identity, "license_identity")
    _evidence._text(confirmation_trial_family_id, "confirmation_trial_family_id")
    return _evidence._digest(
        {
            "schema_version": _evidence._SCHEMA_VERSION,
            "kind": "autosport-holdout-physical-content-v1",
            "dataset_manifest_sha256": _evidence._sha256(
                dataset_manifest_sha256,
                "dataset_manifest_sha256",
            ),
        }
    )


def _holdout_post_init_with_legacy_read(self: _evidence.HoldoutConsumption) -> None:
    """Accept old stored freshness ids while issuing only physical-content ids."""

    _evidence._sha256(self.holdout_access_id, "holdout_access_id")
    _evidence._sha256(self.holdout_freshness_id, "holdout_freshness_id")
    for name in (
        "research_protocol_id",
        "confirmation_trial_family_id",
        "source_identity",
        "license_identity",
        "consumer_identity",
        "purpose",
    ):
        _evidence._text(getattr(self, name), name)
    manifest = _evidence._sha256(
        self.dataset_manifest_sha256,
        "dataset_manifest_sha256",
    )
    _evidence._instant(self.consumed_at_utc, "consumed_at_utc")

    expected_access_id = _evidence.promotion_holdout_access_id(
        research_protocol_id=self.research_protocol_id,
        dataset_manifest_sha256=manifest,
        source_identity=self.source_identity,
        license_identity=self.license_identity,
        confirmation_trial_family_id=self.confirmation_trial_family_id,
    )
    if self.holdout_access_id != expected_access_id:
        raise _evidence.PointInTimeEvidenceError(
            "holdout_access_id does not match canonical source identity"
        )

    physical = _physical_freshness_id(
        dataset_manifest_sha256=manifest,
        source_identity=self.source_identity,
        license_identity=self.license_identity,
        confirmation_trial_family_id=self.confirmation_trial_family_id,
    )
    legacy = _LEGACY_FRESHNESS_ID(
        dataset_manifest_sha256=manifest,
        source_identity=self.source_identity,
        license_identity=self.license_identity,
        confirmation_trial_family_id=self.confirmation_trial_family_id,
    )
    if self.holdout_freshness_id not in (physical, legacy):
        raise _evidence.PointInTimeEvidenceError(
            "holdout_freshness_id does not match canonical physical holdout identity"
        )


def _load_with_physical_reindex(self: _evidence.HoldoutConsumptionLedger) -> None:
    """Read schema-v1 history, then collapse metadata aliases to one content key."""

    _ORIGINAL_LEDGER_LOAD(self)
    normalized: dict[str, _evidence.HoldoutConsumption] = {}
    for record in self._records.values():
        physical = _physical_freshness_id(
            dataset_manifest_sha256=record.dataset_manifest_sha256,
            source_identity=record.source_identity,
            license_identity=record.license_identity,
            confirmation_trial_family_id=record.confirmation_trial_family_id,
        )
        prior = normalized.get(physical)
        if prior is not None and prior != record:
            raise _evidence.EvidenceLedgerCorruptError(
                "holdout ledger contains multiple consumptions for one physical content manifest"
            )
        normalized[physical] = record
    self._records = normalized


def _manifest_for_snapshot(registry: object, snapshot_id: object) -> str:
    snapshot_id = _factory._text(snapshot_id, "promotion evidence dataset_snapshot_id")
    getter = getattr(registry, "get", None)
    if not callable(getter):
        raise ValueError("factory physical holdout check requires canonical registry access")
    snapshot = getter("DatasetSnapshot", snapshot_id)
    if snapshot is None:
        raise ValueError(
            "promotion evidence references missing DatasetSnapshot physical authority"
        )
    payload = getattr(snapshot, "payload", None)
    if type(payload) is not dict:
        raise ValueError("DatasetSnapshot physical authority payload is invalid")
    return _factory._sha256(
        payload.get("manifest_sha256"),
        "promotion evidence dataset manifest_sha256",
    )


def _holdout_consumed_by_physical_evidence(
    prior_payloads: Iterable[Mapping[str, object]],
    *,
    same_attempt_identity: Mapping[str, object],
    registry: object,
) -> bool:
    """Return whether the exact physical holdout was disclosed by another attempt."""

    payloads = tuple(prior_payloads)
    if _ORIGINAL_FACTORY_HELPER(
        payloads,
        same_attempt_identity=same_attempt_identity,
    ):
        return True

    current_manifest = _manifest_for_snapshot(
        registry,
        same_attempt_identity.get("dataset_snapshot_id"),
    )
    for payload in payloads:
        if all(
            payload.get(key) == value
            for key, value in same_attempt_identity.items()
        ):
            continue
        prior_manifest = _manifest_for_snapshot(
            registry,
            payload.get("dataset_snapshot_id"),
        )
        if prior_manifest == current_manifest:
            return True
    return False


def _factory_holdout_consumed(
    prior_payloads: Iterable[Mapping[str, object]],
    *,
    same_attempt_identity: Mapping[str, object],
) -> bool:
    registry = _ACTIVE_FACTORY_REGISTRY.get()
    if registry is None:
        # This private helper has historically been unit-tested in isolation.  The
        # product ExperimentRunner always installs registry context below; preserving
        # the old isolated behavior avoids inventing an ambient registry authority.
        return _ORIGINAL_FACTORY_HELPER(
            prior_payloads,
            same_attempt_identity=same_attempt_identity,
        )
    return _holdout_consumed_by_physical_evidence(
        prior_payloads,
        same_attempt_identity=same_attempt_identity,
        registry=registry,
    )


def _run_with_physical_holdout_context(
    self: _factory.ExperimentRunner,
    spec: _factory.FactoryCandidateSpec,
    points,
    *,
    rule: _factory.PromotionRule,
    minimum_train_size: int | None = None,
):
    token = _ACTIVE_FACTORY_REGISTRY.set(self.registry)
    try:
        return _ORIGINAL_FACTORY_RUN(
            self,
            spec,
            points,
            rule=rule,
            minimum_train_size=minimum_train_size,
        )
    finally:
        _ACTIVE_FACTORY_REGISTRY.reset(token)


def _install_physical_guards() -> None:
    _evidence._holdout_freshness_id = _physical_freshness_id
    _evidence.HoldoutConsumption.__post_init__ = _holdout_post_init_with_legacy_read
    _evidence.HoldoutConsumptionLedger._load = _load_with_physical_reindex
    _factory._holdout_consumed_by_other_evidence = _factory_holdout_consumed
    _factory.ExperimentRunner.run_baseline_candidate = _run_with_physical_holdout_context


def _runtime_install_with_physical_guard() -> None:
    # The #716 reload guard reinstalls its own methods after point_in_time_evidence
    # reload.  Reapply our content identity last so reload cannot reopen the alias.
    _ORIGINAL_RUNTIME_INSTALL()
    _install_physical_guards()


_runtime._install_runtime_guards = _runtime_install_with_physical_guard
_install_physical_guards()

__all__: list[str] = []
