"""Conservative identity snapshot quarantine for a SIMULATED TT policy.

This is an offline research audit, not a provider integration, production
policy gate, identity authority, rollback, or real training authorization.
An externally retained chain head is needed; a supplied hash cannot establish
freshness, completeness, trusted time, or actual physical identity.
"""
from dataclasses import dataclass
from datetime import timezone
import hashlib
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from autosport.learning_environment import EvidenceTruth
from autosport.participant_identity import ParticipantIdentityError, ParticipantIdentityRegistry
from causal_h2h_observer import verify_journal
from record import instant
from synthetic_learning_revision_audit import audit as audit_result_revisions

MAX_RECEIPTS = 128
MAX_SNAPSHOT_BYTES = 2_000_000
_HEX = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class SimulatedIdentityReceipt:
    """A caller-provided simulated local observation, not a provider receipt."""
    received_at: str
    identity_snapshot: bytes
    truth: EvidenceTruth = EvidenceTruth.SIMULATED


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _encoded(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _instant(value):
    if type(value) is not str:
        raise ValueError("identity receipt time must be a timezone-aware string")
    try:
        return instant(value).astimezone(timezone.utc)
    except (ValueError, AttributeError) as exc:
        raise ValueError("identity receipt time must be a timezone-aware string") from exc


def _snapshot_bytes(value):
    if type(value) is not bytes or not value or len(value) > MAX_SNAPSHOT_BYTES:
        raise ValueError("identity snapshot must be bounded nonempty bytes")
    with TemporaryDirectory(prefix="tt-identity-audit-") as tmp:
        path = Path(tmp) / "registry.json"
        path.write_bytes(value)
        try:
            ParticipantIdentityRegistry(path)
        except (ValueError, TypeError, KeyError, ParticipantIdentityError) as exc:
            raise ValueError("invalid canonical identity snapshot") from exc
    return value


def timeline_head(forecast_sha256, frozen_identity, receipts):
    """Hash validated snapshot bytes and receipt times into a chronological chain."""
    if type(forecast_sha256) is not str or not _HEX.fullmatch(forecast_sha256):
        raise ValueError("forecast SHA-256 is invalid")
    frozen = _snapshot_bytes(frozen_identity)
    if type(receipts) not in (list, tuple) or len(receipts) > MAX_RECEIPTS:
        raise ValueError("identity receipt chain must be a bounded list or tuple")
    previous = _hash(_encoded({"schema": "research.tt_identity_snapshot_anchor.v1",
                                "forecast_sha256": forecast_sha256,
                                "frozen_identity_sha256": _hash(frozen)}))
    last_time = None
    for receipt in receipts:
        if type(receipt) is not SimulatedIdentityReceipt or receipt.truth is not EvidenceTruth.SIMULATED:
            raise ValueError("only SIMULATED identity receipts are supported")
        at = _instant(receipt.received_at)
        if last_time is not None and at <= last_time:
            raise ValueError("identity receipt times must strictly increase")
        payload = _snapshot_bytes(receipt.identity_snapshot)
        previous = _hash(_encoded({"schema": "research.tt_identity_snapshot_link.v1",
                                   "previous": previous, "received_at": at.isoformat(),
                                   "identity_sha256": _hash(payload)}))
        last_time = at
    return previous


def audit(root, history, expected_result_head, *, expected_forecast_sha256,
          expected_forecast_receipt_sha256, frozen_identity, identity_receipts,
          expected_identity_head, policy, bindings, decision_at):
    """Check result revisions and a conservative as-known identity snapshot cut.

    The whole submitted chain is validated even when it includes future records.
    The comparison uses actual snapshot bytes: even irrelevant or formatting-only
    changes hold the policy. An absent difference proves neither continuity nor
    identity truth. This function does not intercept direct policy calls.
    """
    cutoff = _instant(decision_at)
    previous = audit_result_revisions(
        root, history, expected_result_head, expected_forecast_sha256=expected_forecast_sha256,
        expected_forecast_receipt_sha256=expected_forecast_receipt_sha256,
        policy=policy, bindings=bindings, decision_at=decision_at)
    journal, envelope, _ = verify_journal(Path(root))
    receipt_bytes = (Path(root) / "forecast.receipt.json").read_bytes()
    if (envelope["sha256"] != expected_forecast_sha256 or
            _hash(receipt_bytes) != expected_forecast_receipt_sha256):
        raise ValueError("forecast or receipt changed between verification reads")
    if _hash(_snapshot_bytes(frozen_identity)) != journal.get("identity_store_sha256"):
        raise ValueError("frozen identity does not match forecast-bound digest")
    head = timeline_head(expected_forecast_sha256, frozen_identity, identity_receipts)
    if type(expected_identity_head) is not str or expected_identity_head != head:
        raise ValueError("identity receipt chain head differs from external pin")
    frozen_at = _instant(envelope["frozen_at"])
    if any(_instant(receipt.received_at) <= frozen_at for receipt in identity_receipts):
        raise ValueError("identity receipt must strictly follow forecast freeze")
    baseline = _hash(frozen_identity)
    changed_times = [_instant(receipt.received_at) for receipt in identity_receipts
                     if _hash(receipt.identity_snapshot) != baseline]
    for binding in bindings:
        updated = _instant(binding.update.transition.resolved_at)
        if any(change < updated for change in changed_times):
            raise ValueError("synthetic policy update followed a known identity snapshot change")
    visible = [receipt for receipt in identity_receipts if _instant(receipt.received_at) < cutoff]
    identity_changed = any(_instant(receipt.received_at) < cutoff and
                           _hash(receipt.identity_snapshot) != baseline for receipt in identity_receipts)
    if identity_changed:
        identity_status = "identity_changed_hold"
    elif not visible:
        identity_status = "identity_snapshot_missing_hold"
    else:
        identity_status = "no_identity_change_observed"
    held = identity_status != "no_identity_change_observed" or previous["status"] == "held_for_review"
    return {"schema": "research.tt_simulated_learning_identity_audit.v1",
            "status": "held_for_review" if held else "no_observed_change_only",
            "identity_status": identity_status, "result_revision_status": previous["status"],
            "visible_identity_receipts": len(visible),
            "visible_changed_identity_receipts": sum(
                _hash(receipt.identity_snapshot) != baseline for receipt in visible),
            "verified_policy_generation": previous["policy_generation"],
            "verified_simulated_bindings": previous["verified_bindings"],
            "identity_chain_head": head, "frozen_identity_sha256": baseline,
            "bytewise_comparison_can_overquarantine": True, "policy_modified": False,
            "simulation_label_proves_source_truth": False, "identity_source_complete": False,
            "identity_continuity_verified": False,
            "identity_independently_verified": False, "pin_freshness_verified": False,
            "trusted_clock_verified": False, "frozen_opponent_snapshot_reverified": False,
            "production_enforcement": False, "model_training_authorized": False,
            "real_money_execution": False, "profitability_claim": False}
