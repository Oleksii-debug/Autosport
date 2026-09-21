"""Versioned pre-execution Observation projection for PAPER campaign admission.

The #727 reservation already commits one exact durable DecisionLedger origin before
any PAPER attempt.  This module turns that committed origin into the only supported
learning Observation through an explicit, versioned product adapter.  The adapter
uses decision-time bytes only; execution odds/stake/outcome/evidence never enter the
Observation identity.  It installs before the admission consumer guard so the
resulting resolver is captured by the existing executable closure seal.
"""

from __future__ import annotations

from typing import Mapping

from .learning_environment import Observation
from .paper_campaign_admission import (
    PaperCampaignAdmissionCoordinator,
    PaperCampaignAdmissionError,
)

_ADAPTER_SCHEMA = "autosport.paper_campaign_preexecution_observation"
_ADAPTER_SCHEMA_VERSION = 1
_LIVE_SCHEMA = "autosport.persistent_live_decision"
_LIVE_SCHEMA_VERSION = 2
_LIVE_CONTRACT = "autosport.persistent_live_decision.v2"
_LEGACY_CONTRACT = "autosport.paper_value.execution_authority.v1"


def _install() -> None:
    coordinator = PaperCampaignAdmissionCoordinator
    error = PaperCampaignAdmissionError
    observation_type = Observation
    original_resolver = coordinator._resolved_execution_decision_id
    adapter_schema = _ADAPTER_SCHEMA
    adapter_schema_version = _ADAPTER_SCHEMA_VERSION
    live_schema = _LIVE_SCHEMA
    live_schema_version = _LIVE_SCHEMA_VERSION
    live_contract = _LIVE_CONTRACT
    legacy_contract = _LEGACY_CONTRACT

    def resolved_execution_decision_id(
        self,
        *,
        run_id: str,
        reservation: Mapping[str, object],
        origin: Mapping[str, object],
    ):
        authority = original_resolver(
            self,
            run_id=run_id,
            reservation=reservation,
            origin=origin,
        )
        record = getattr(authority, "record", None)
        record_sha256 = getattr(authority, "record_sha256", None)
        if record is None or type(record_sha256) is not str:
            raise error("PAPER pre-execution Observation origin is unavailable")
        payload = getattr(record, "payload", None)
        if not isinstance(payload, Mapping):
            raise error("PAPER pre-execution Observation producer payload is invalid")

        if payload.get("schema") == live_schema:
            if payload.get("schema_version") != live_schema_version:
                raise error("unsupported live PAPER Observation producer schema")
            producer_contract = live_contract
        else:
            # The predecessor resolver has already proved this is exactly the
            # supported legacy paper-value execution-authority contract.  Give that
            # producer an explicit adapter identity instead of silently treating all
            # non-live DecisionRecords as interchangeable.
            producer_contract = legacy_contract

        decision_id = str(authority)
        context_hash = getattr(record, "context_hash", None)
        observed_ts = getattr(record, "observed_ts", None)
        if (
            type(context_hash) is not str
            or len(context_hash) != 64
            or context_hash.lower() != context_hash
            or any(ch not in "0123456789abcdef" for ch in context_hash)
        ):
            raise error("PAPER pre-execution Observation context_hash is invalid")
        if type(observed_ts) is not str or not observed_ts.strip():
            raise error("PAPER pre-execution Observation timestamp is invalid")
        if origin.get("decision_id") != decision_id or origin.get("record_sha256") != record_sha256:
            raise error("PAPER pre-execution Observation origin conflicts with reservation")

        # This is deliberately a closed decision-time projection.  The exact
        # PaperCampaignRuntime environment is separately closure-pinned by the
        # consumer guard, while every remaining identity component comes from the
        # already-committed #727 DecisionRecord origin.  No attempt/ticket/outcome
        # field is available to this projection.
        observation = observation_type(
            environment_id=self.runtime.environment.environment_id,
            observed_at=observed_ts,
            available_at=observed_ts,
            evidence=tuple(
                sorted(
                    (
                        ("adapter_schema", adapter_schema),
                        ("adapter_schema_version", str(adapter_schema_version)),
                        ("context_hash", context_hash),
                        ("decision_id", decision_id),
                        ("decision_record_sha256", record_sha256),
                        ("producer_contract", producer_contract),
                    )
                )
            ),
        )
        authority.observation = observation
        authority.decision_at = observed_ts
        authority.observation_adapter_schema = adapter_schema
        authority.observation_adapter_schema_version = adapter_schema_version
        authority.observation_producer_contract = producer_contract
        return authority

    coordinator._resolved_execution_decision_id = resolved_execution_decision_id


_install()
