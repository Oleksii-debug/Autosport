import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import autosport.prophetx_order_idempotency as prophetx_module
from autosport.prophetx_order_idempotency import (
    FIX_ORDER_ENTRY_CAPABILITIES,
    PROPHETX_PRODUCTION_REST_PROFILE,
    PROPHETX_SANDBOX_FIX_PROFILE,
    PROPHETX_SANDBOX_REST_PROFILE,
    REST_DIRECT_LINK_CAPABILITIES,
    ProphetXEvidenceConflict,
    ProphetXEvidenceKind,
    ProphetXFixExecutionReport,
    ProphetXOrderIdentityError,
    ProphetXProviderEvidence,
    ProphetXReconciliationDisposition,
    ProphetXRetryDisposition,
    ProphetXTransport,
    bind_before_effect,
    capabilities_for,
    load_identity,
    mark_ambiguous_delivery,
    normalize_fix_execution_reports,
    reconciliation_disposition,
    retry_disposition,
)
from autosport.real_execution_ledger import (
    AttemptState,
    ExecutionAction,
    ExecutionIdentityConflict,
    ExecutionPlan,
    RealExecutionLedger,
)

CREATED = "2026-09-22T20:00:00+00:00"
RESERVED = "2026-09-22T20:00:10+00:00"
SUBMITTED = "2026-09-22T20:00:20+00:00"
UNKNOWN = "2026-09-22T20:00:30+00:00"
OBSERVED = "2026-09-22T20:00:40+00:00"
EXPIRES = "2026-09-22T21:00:00+00:00"


def _ledger(path, profile=None, bookmaker_id="prophetx"):
    ledger = RealExecutionLedger(path)
    action = ExecutionAction(
        action_id="a1",
        bookmaker_id=bookmaker_id,
        account_id="acct-1",
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        side="BACK",
        requested_odds="2.5",
        requested_stake="10",
        quote_id="quote-1",
        quote_observed_at=CREATED,
        expires_at=EXPIRES,
    )
    plan = ExecutionPlan(
        plan_id="p1",
        bookmaker_profile_version=(
            profile or PROPHETX_SANDBOX_REST_PROFILE.bookmaker_profile_version
        ),
        decision_id="decision-1",
        approval_id="approval-1",
        created_at=CREATED,
        actions=(action,),
    )
    ledger.reserve_plan(plan)
    ledger.begin_attempt(
        plan_id="p1",
        action_id="a1",
        attempt_id="try-1",
        reserved_at=RESERVED,
    )
    return ledger


def _evidence(identity, kind, **overrides):
    values = dict(
        evidence_id="e" * 64,
        kind=kind,
        transport=identity.transport,
        observed_at=OBSERVED,
        client_order_id=identity.client_order_id,
        provider_order_id=None,
        effect_fingerprint=None,
    )
    values.update(overrides)
    return ProphetXProviderEvidence(**values)


class ProphetXOrderIdempotencyTests(unittest.TestCase):
    def test_identity_is_pre_effect_durable_and_restart_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            ledger = _ledger(path)
            first = bind_before_effect(ledger, attempt_id="try-1")
            second = load_identity(RealExecutionLedger(path), attempt_id="try-1")
            self.assertEqual(first, second)
            self.assertEqual(len(first.client_order_id), 32)
            self.assertEqual(first.transport, ProphetXTransport.REST_DIRECT_LINK)

    def test_transport_is_durable_plan_fact_and_unqualified_profile_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fix = _ledger(
                Path(tmp) / "fix.jsonl",
                PROPHETX_SANDBOX_FIX_PROFILE.bookmaker_profile_version,
            )
            self.assertEqual(
                bind_before_effect(fix, attempt_id="try-1").transport,
                ProphetXTransport.FIX_ORDER_ENTRY,
            )
            unknown = _ledger(
                Path(tmp) / "unknown.jsonl",
                "prophetx-unqualified-profile",
            )
            with self.assertRaisesRegex(
                ProphetXOrderIdentityError, "qualified ProphetX transport profile"
            ):
                bind_before_effect(unknown, attempt_id="try-1")

    def test_wrong_provider_or_post_submission_binding_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            wrong = _ledger(Path(tmp) / "wrong.jsonl", bookmaker_id="betfair")
            with self.assertRaises(ExecutionIdentityConflict):
                bind_before_effect(wrong, attempt_id="try-1")
            ledger = _ledger(Path(tmp) / "late.jsonl")
            ledger.mark_submitted("try-1", submitted_at=SUBMITTED)
            with self.assertRaisesRegex(
                ProphetXOrderIdentityError, "before external effect"
            ):
                bind_before_effect(ledger, attempt_id="try-1")

    def test_rest_timeout_becomes_unknown_and_cannot_mint_fresh_external_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            ledger = _ledger(path)
            identity = bind_before_effect(ledger, attempt_id="try-1")
            ledger.mark_submitted("try-1", submitted_at=SUBMITTED)
            mark_ambiguous_delivery(
                ledger,
                identity=identity,
                reason="timeout_after_possible_send",
                observed_at=UNKNOWN,
            )
            restarted = RealExecutionLedger(path)
            self.assertEqual(restarted.attempt_state("try-1"), AttemptState.UNKNOWN)
            self.assertEqual(
                load_identity(restarted, attempt_id="try-1").client_order_id,
                identity.client_order_id,
            )
            with self.assertRaises(ProphetXOrderIdentityError):
                bind_before_effect(restarted, attempt_id="try-1")

    def test_rest_duplicate_or_wallet_evidence_never_releases_ambiguity(self):
        with tempfile.TemporaryDirectory() as tmp:
            identity = bind_before_effect(
                _ledger(Path(tmp) / "execution.jsonl"), attempt_id="try-1"
            )
            for observed in (
                _evidence(identity, ProphetXEvidenceKind.DUPLICATE_REJECTION),
                _evidence(
                    identity,
                    ProphetXEvidenceKind.WALLET_TRANSACTION,
                    client_order_id=None,
                ),
            ):
                with self.subTest(kind=observed.kind):
                    self.assertEqual(
                        reconciliation_disposition(identity, observed),
                        ProphetXReconciliationDisposition.WAIT_EXTERNAL_EVIDENCE,
                    )

    def test_rest_exact_order_correlation_is_only_a_candidate_not_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            ledger = _ledger(path)
            identity = bind_before_effect(ledger, attempt_id="try-1")
            ledger.mark_submitted("try-1", submitted_at=SUBMITTED)
            matched = _evidence(
                identity,
                ProphetXEvidenceKind.ORDER_STATE,
                provider_order_id="provider-order-7",
                effect_fingerprint=identity.effect_fingerprint,
            )
            unrelated = _evidence(
                identity,
                ProphetXEvidenceKind.ORDER_STATE,
                client_order_id="other-client-order",
                provider_order_id="provider-order-7",
            )
            self.assertEqual(
                reconciliation_disposition(identity, matched),
                ProphetXReconciliationDisposition.CONFLICT,
            )
            self.assertEqual(
                reconciliation_disposition(identity, matched, ledger=ledger),
                ProphetXReconciliationDisposition.MATCHED_PROVIDER_ORDER_CANDIDATE,
            )
            self.assertEqual(
                RealExecutionLedger(path).provider_assigned_order_id(
                    attempt_id="try-1", provider_id="prophetx"
                ),
                "provider-order-7",
            )
            self.assertEqual(
                reconciliation_disposition(identity, unrelated, ledger=ledger),
                ProphetXReconciliationDisposition.UNRELATED_PROVIDER_EVIDENCE,
            )
            conflicting = _evidence(
                identity,
                ProphetXEvidenceKind.ORDER_STATE,
                provider_order_id="provider-order-8",
                effect_fingerprint=identity.effect_fingerprint,
            )
            self.assertEqual(
                reconciliation_disposition(
                    identity,
                    conflicting,
                    ledger=RealExecutionLedger(path),
                ),
                ProphetXReconciliationDisposition.CONFLICT,
            )

    def test_provider_order_id_candidate_requires_canonical_durable_bind_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            ledger = _ledger(path)
            identity = bind_before_effect(ledger, attempt_id="try-1")
            ledger.mark_submitted("try-1", submitted_at=SUBMITTED)
            matched = _evidence(
                identity,
                ProphetXEvidenceKind.ORDER_STATE,
                provider_order_id="provider-order-shadowed",
                effect_fingerprint=identity.effect_fingerprint,
            )

            # Reproduce the predecessor false-success: a caller-held exact ledger
            # instance shadows only the top-level durable bind with an exact-looking
            # return value but performs no append.
            ledger.bind_provider_assigned_order_id = (
                lambda **_kwargs: "provider-order-shadowed"
            )

            self.assertEqual(
                reconciliation_disposition(identity, matched, ledger=ledger),
                ProphetXReconciliationDisposition.CONFLICT,
            )
            self.assertIsNone(
                RealExecutionLedger(path).provider_assigned_order_id(
                    attempt_id="try-1",
                    provider_id="prophetx",
                )
            )

    def test_provider_order_correlation_ignores_rebound_identity_resolver(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            ledger = _ledger(path)
            genuine = bind_before_effect(ledger, attempt_id="try-1")
            ledger.mark_submitted("try-1", submitted_at=SUBMITTED)
            forged = type(genuine)(
                attempt_id=genuine.attempt_id,
                environment=genuine.environment,
                transport=genuine.transport,
                client_order_id="forged-client-order",
                effect_fingerprint="f" * 64,
                production_write_qualified=genuine.production_write_qualified,
            )
            matched = _evidence(
                forged,
                ProphetXEvidenceKind.ORDER_STATE,
                provider_order_id="provider-order-forged",
                effect_fingerprint=forged.effect_fingerprint,
            )
            attacker_calls = []

            def forged_identity_resolver(*_args, **_kwargs):
                attacker_calls.append("load_identity")
                return forged

            with patch.object(
                prophetx_module,
                "load_identity",
                forged_identity_resolver,
            ):
                self.assertEqual(
                    reconciliation_disposition(forged, matched, ledger=ledger),
                    ProphetXReconciliationDisposition.CONFLICT,
                )

            self.assertEqual(attacker_calls, [])
            self.assertIsNone(
                RealExecutionLedger(path).provider_assigned_order_id(
                    attempt_id="try-1",
                    provider_id="prophetx",
                )
            )

    def test_provider_order_correlation_rejects_rebound_identity_dependency(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            ledger = _ledger(path)
            genuine = bind_before_effect(ledger, attempt_id="try-1")
            ledger.mark_submitted("try-1", submitted_at=SUBMITTED)
            forged = type(genuine)(
                attempt_id=genuine.attempt_id,
                environment=genuine.environment,
                transport=genuine.transport,
                client_order_id=genuine.client_order_id,
                effect_fingerprint="f" * 64,
                production_write_qualified=genuine.production_write_qualified,
            )
            matched = _evidence(
                forged,
                ProphetXEvidenceKind.ORDER_STATE,
                provider_order_id="provider-order-transitive",
                effect_fingerprint=forged.effect_fingerprint,
            )
            attacker_calls = []

            def forged_durable_facts(*_args, **_kwargs):
                attacker_calls.append("_durable_attempt_facts")
                return PROPHETX_SANDBOX_REST_PROFILE, forged.effect_fingerprint

            with patch.object(
                prophetx_module,
                "_durable_attempt_facts",
                forged_durable_facts,
            ):
                self.assertEqual(
                    reconciliation_disposition(forged, matched, ledger=ledger),
                    ProphetXReconciliationDisposition.CONFLICT,
                )

            self.assertEqual(attacker_calls, [])
            self.assertIsNone(
                RealExecutionLedger(path).provider_assigned_order_id(
                    attempt_id="try-1",
                    provider_id="prophetx",
                )
            )

    def test_provider_order_correlation_rejects_identity_equality_rebind(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            ledger = _ledger(path)
            genuine = bind_before_effect(ledger, attempt_id="try-1")
            ledger.mark_submitted("try-1", submitted_at=SUBMITTED)
            forged = type(genuine)(
                attempt_id=genuine.attempt_id,
                environment=genuine.environment,
                transport=genuine.transport,
                client_order_id="forged-client-order",
                effect_fingerprint="f" * 64,
                production_write_qualified=genuine.production_write_qualified,
            )
            matched = _evidence(
                forged,
                ProphetXEvidenceKind.ORDER_STATE,
                provider_order_id="provider-order-equality",
                effect_fingerprint=forged.effect_fingerprint,
            )
            attacker_calls = []

            def permissive_eq(_left, _right):
                attacker_calls.append("__eq__")
                return True

            with patch.object(type(genuine), "__eq__", permissive_eq):
                self.assertEqual(
                    reconciliation_disposition(forged, matched, ledger=ledger),
                    ProphetXReconciliationDisposition.CONFLICT,
                )

            self.assertEqual(attacker_calls, [])
            self.assertIsNone(
                RealExecutionLedger(path).provider_assigned_order_id(
                    attempt_id="try-1",
                    provider_id="prophetx",
                )
            )

    def test_provider_order_correlation_rejects_in_place_profile_map_substitution(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            ledger = _ledger(path)
            genuine = bind_before_effect(ledger, attempt_id="try-1")
            ledger.mark_submitted("try-1", submitted_at=SUBMITTED)
            forged = type(genuine)(
                attempt_id=genuine.attempt_id,
                environment=PROPHETX_SANDBOX_FIX_PROFILE.environment,
                transport=PROPHETX_SANDBOX_FIX_PROFILE.transport,
                client_order_id=genuine.client_order_id,
                effect_fingerprint=genuine.effect_fingerprint,
                production_write_qualified=(
                    PROPHETX_SANDBOX_FIX_PROFILE.production_write_qualified
                ),
            )
            matched = _evidence(
                forged,
                ProphetXEvidenceKind.FIX_EXECUTION_REPORT,
                provider_order_id="provider-order-profile-map",
                effect_fingerprint=forged.effect_fingerprint,
            )
            rest_version = PROPHETX_SANDBOX_REST_PROFILE.bookmaker_profile_version

            with patch.dict(
                prophetx_module._PROFILE_BY_VERSION,
                {rest_version: PROPHETX_SANDBOX_FIX_PROFILE},
            ):
                self.assertEqual(
                    reconciliation_disposition(forged, matched, ledger=ledger),
                    ProphetXReconciliationDisposition.CONFLICT,
                )

            self.assertIsNone(
                RealExecutionLedger(path).provider_assigned_order_id(
                    attempt_id="try-1",
                    provider_id="prophetx",
                )
            )

    def test_provider_order_correlation_rejects_in_place_profile_payload_mutation(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            ledger = _ledger(path)
            genuine = bind_before_effect(ledger, attempt_id="try-1")
            ledger.mark_submitted("try-1", submitted_at=SUBMITTED)
            forged = type(genuine)(
                attempt_id=genuine.attempt_id,
                environment=genuine.environment,
                transport=genuine.transport,
                client_order_id=genuine.client_order_id,
                effect_fingerprint=genuine.effect_fingerprint,
                production_write_qualified=True,
            )
            matched = _evidence(
                forged,
                ProphetXEvidenceKind.ORDER_STATE,
                provider_order_id="provider-order-profile-payload",
                effect_fingerprint=forged.effect_fingerprint,
            )

            original = PROPHETX_SANDBOX_REST_PROFILE.production_write_qualified
            object.__setattr__(
                PROPHETX_SANDBOX_REST_PROFILE,
                "production_write_qualified",
                True,
            )
            try:
                self.assertEqual(
                    reconciliation_disposition(forged, matched, ledger=ledger),
                    ProphetXReconciliationDisposition.CONFLICT,
                )
            finally:
                object.__setattr__(
                    PROPHETX_SANDBOX_REST_PROFILE,
                    "production_write_qualified",
                    original,
                )

            self.assertIsNone(
                RealExecutionLedger(path).provider_assigned_order_id(
                    attempt_id="try-1",
                    provider_id="prophetx",
                )
            )

    def test_provider_order_correlation_rejects_profile_slot_descriptor_rebind(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            ledger = _ledger(path)
            genuine = bind_before_effect(ledger, attempt_id="try-1")
            ledger.mark_submitted("try-1", submitted_at=SUBMITTED)
            forged = type(genuine)(
                attempt_id=genuine.attempt_id,
                environment=genuine.environment,
                transport=ProphetXTransport.FIX_ORDER_ENTRY,
                client_order_id=genuine.client_order_id,
                effect_fingerprint=genuine.effect_fingerprint,
                production_write_qualified=genuine.production_write_qualified,
            )
            matched = _evidence(
                forged,
                ProphetXEvidenceKind.FIX_EXECUTION_REPORT,
                provider_order_id="provider-order-slot-descriptor",
                effect_fingerprint=forged.effect_fingerprint,
            )

            profile_class = type(PROPHETX_SANDBOX_REST_PROFILE)
            canonical_transport_descriptor = vars(profile_class)["transport"]
            descriptor_reads = []

            class StatefulTransportDescriptor:
                def __get__(self, instance, owner=None):
                    if instance is None:
                        return self
                    descriptor_reads.append(instance)
                    canonical = canonical_transport_descriptor.__get__(
                        instance,
                        owner,
                    )
                    if instance is PROPHETX_SANDBOX_REST_PROFILE:
                        target_reads = sum(
                            item is PROPHETX_SANDBOX_REST_PROFILE
                            for item in descriptor_reads
                        )
                        if target_reads == 2:
                            return ProphetXTransport.FIX_ORDER_ENTRY
                    return canonical

                def __set__(self, instance, value):
                    raise AssertionError(
                        f"hostile descriptor write executed: {instance!r}={value!r}"
                    )

            with patch.object(
                profile_class,
                "transport",
                StatefulTransportDescriptor(),
            ):
                self.assertEqual(
                    reconciliation_disposition(forged, matched, ledger=ledger),
                    ProphetXReconciliationDisposition.CONFLICT,
                )

            self.assertEqual(descriptor_reads, [])
            self.assertIsNone(
                RealExecutionLedger(path).provider_assigned_order_id(
                    attempt_id="try-1",
                    provider_id="prophetx",
                )
            )

    def test_positive_provider_order_consumer_ignores_reexposed_binder_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            ledger = _ledger(path)
            identity = bind_before_effect(ledger, attempt_id="try-1")
            ledger.mark_submitted("try-1", submitted_at=SUBMITTED)
            matched = _evidence(
                identity,
                ProphetXEvidenceKind.ORDER_STATE,
                provider_order_id="provider-order-captured",
                effect_fingerprint=identity.effect_fingerprint,
            )
            attacker_calls = []

            def forged_binder(*_args, **_kwargs):
                attacker_calls.append("binder")
                return True

            with patch.object(
                prophetx_module,
                "_bind_provider_assigned_order_id",
                forged_binder,
                create=True,
            ):
                self.assertEqual(
                    reconciliation_disposition(identity, matched, ledger=ledger),
                    ProphetXReconciliationDisposition.MATCHED_PROVIDER_ORDER_CANDIDATE,
                )

            self.assertEqual(attacker_calls, [])
            self.assertEqual(
                RealExecutionLedger(path).provider_assigned_order_id(
                    attempt_id="try-1",
                    provider_id="prophetx",
                ),
                "provider-order-captured",
            )

    def test_economic_or_transport_conflict_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            identity = bind_before_effect(
                _ledger(Path(tmp) / "execution.jsonl"), attempt_id="try-1"
            )
            for observed in (
                _evidence(
                    identity,
                    ProphetXEvidenceKind.ORDER_STATE,
                    provider_order_id="order-1",
                    effect_fingerprint="f" * 64,
                ),
                _evidence(
                    identity,
                    ProphetXEvidenceKind.ORDER_STATE,
                    provider_order_id="order-1",
                    transport=ProphetXTransport.FIX_ORDER_ENTRY,
                ),
            ):
                self.assertEqual(
                    reconciliation_disposition(identity, observed),
                    ProphetXReconciliationDisposition.CONFLICT,
                )

    def test_rest_never_imports_fix_replay_or_readback_guarantees(self):
        with tempfile.TemporaryDirectory() as tmp:
            identity = bind_before_effect(
                _ledger(Path(tmp) / "execution.jsonl"), attempt_id="try-1"
            )
            self.assertEqual(
                retry_disposition(
                    identity,
                    candidate_effect_fingerprint=identity.effect_fingerprint,
                    exact_provider_profile_qualified=True,
                ),
                ProphetXRetryDisposition.WAIT_EXTERNAL_EVIDENCE,
            )
            self.assertTrue(REST_DIRECT_LINK_CAPABILITIES.duplicate_suppression_only)
            self.assertFalse(
                REST_DIRECT_LINK_CAPABILITIES.idempotent_redelivery_identical_economics
            )
            self.assertFalse(
                REST_DIRECT_LINK_CAPABILITIES.authoritative_readback_by_external_id
            )

    def test_fix_same_id_redelivery_requires_same_economics_and_qualified_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            identity = bind_before_effect(
                _ledger(
                    Path(tmp) / "execution.jsonl",
                    PROPHETX_SANDBOX_FIX_PROFILE.bookmaker_profile_version,
                ),
                attempt_id="try-1",
            )
            self.assertEqual(
                retry_disposition(
                    identity,
                    candidate_effect_fingerprint=identity.effect_fingerprint,
                    exact_provider_profile_qualified=True,
                ),
                ProphetXRetryDisposition.SAME_CLIENT_ID_REDELIVERY_CANDIDATE,
            )
            self.assertEqual(
                retry_disposition(
                    identity,
                    candidate_effect_fingerprint="f" * 64,
                    exact_provider_profile_qualified=True,
                ),
                ProphetXRetryDisposition.LOCAL_REJECT_DIFFERENT_ECONOMICS,
            )
            self.assertEqual(
                retry_disposition(
                    identity,
                    candidate_effect_fingerprint=identity.effect_fingerprint,
                    exact_provider_profile_qualified=False,
                ),
                ProphetXRetryDisposition.PROFILE_UNQUALIFIED,
            )

    def test_fix_rate_limit_and_status_unknown_remain_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            identity = bind_before_effect(
                _ledger(
                    Path(tmp) / "execution.jsonl",
                    PROPHETX_SANDBOX_FIX_PROFILE.bookmaker_profile_version,
                ),
                attempt_id="try-1",
            )
            self.assertEqual(
                retry_disposition(
                    identity,
                    candidate_effect_fingerprint=identity.effect_fingerprint,
                    exact_provider_profile_qualified=True,
                    preprocessing_rate_limit_reject=True,
                ),
                ProphetXRetryDisposition.SAME_CLIENT_ID_RETRY_AFTER_BACKOFF_CANDIDATE,
            )
            status = _evidence(
                identity,
                ProphetXEvidenceKind.FIX_ORDER_STATUS_UNKNOWN,
            )
            self.assertEqual(
                reconciliation_disposition(identity, status),
                ProphetXReconciliationDisposition.PROVIDER_NOT_FOUND_CANDIDATE,
            )

    def test_fix_exec_reports_dedupe_and_order_by_provider_time_not_arrival(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            ledger = _ledger(
                path,
                PROPHETX_SANDBOX_FIX_PROFILE.bookmaker_profile_version,
            )
            identity = bind_before_effect(ledger, attempt_id="try-1")
            ledger.mark_submitted("try-1", submitted_at=SUBMITTED)
            later = ProphetXFixExecutionReport(
                "exec-2",
                identity.client_order_id,
                "order-1",
                "2026-09-22T20:00:50+00:00",
                identity.effect_fingerprint,
                "PARTIAL_FILL",
                "2",
            )
            earlier = ProphetXFixExecutionReport(
                "exec-1",
                identity.client_order_id,
                "order-1",
                "2026-09-22T20:00:40+00:00",
                identity.effect_fingerprint,
                "NEW",
                "0",
            )
            with self.assertRaisesRegex(
                ProphetXOrderIdentityError,
                "require durable provider order id binding",
            ):
                normalize_fix_execution_reports(identity, (later, earlier, earlier))
            normalized = normalize_fix_execution_reports(
                identity, (later, earlier, earlier), ledger=ledger
            )
            self.assertEqual([item.exec_id for item in normalized], ["exec-1", "exec-2"])
            self.assertEqual(
                RealExecutionLedger(path).provider_assigned_order_id(
                    attempt_id="try-1", provider_id="prophetx"
                ),
                "order-1",
            )

    def test_conflicting_duplicate_fix_exec_id_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            identity = bind_before_effect(
                _ledger(
                    Path(tmp) / "execution.jsonl",
                    PROPHETX_SANDBOX_FIX_PROFILE.bookmaker_profile_version,
                ),
                attempt_id="try-1",
            )
            first = ProphetXFixExecutionReport(
                "exec-1",
                identity.client_order_id,
                "order-1",
                "2026-09-22T20:00:40+00:00",
                identity.effect_fingerprint,
                "NEW",
                "0",
            )
            conflict = ProphetXFixExecutionReport(
                "exec-1",
                identity.client_order_id,
                "order-1",
                "2026-09-22T20:00:41+00:00",
                identity.effect_fingerprint,
                "PARTIAL_FILL",
                "1",
            )
            with self.assertRaisesRegex(
                ProphetXEvidenceConflict, "conflicting provider evidence"
            ):
                normalize_fix_execution_reports(identity, (first, conflict))

    def test_fix_report_wrong_client_or_economics_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            identity = bind_before_effect(
                _ledger(
                    Path(tmp) / "execution.jsonl",
                    PROPHETX_SANDBOX_FIX_PROFILE.bookmaker_profile_version,
                ),
                attempt_id="try-1",
            )
            wrong_client = ProphetXFixExecutionReport(
                "exec-1",
                "other",
                "order-1",
                OBSERVED,
                identity.effect_fingerprint,
                "NEW",
                "0",
            )
            wrong_economics = ProphetXFixExecutionReport(
                "exec-1",
                identity.client_order_id,
                "order-1",
                OBSERVED,
                "f" * 64,
                "NEW",
                "0",
            )
            with self.assertRaisesRegex(ProphetXEvidenceConflict, "client order id"):
                normalize_fix_execution_reports(identity, (wrong_client,))
            with self.assertRaisesRegex(ProphetXEvidenceConflict, "economics"):
                normalize_fix_execution_reports(identity, (wrong_economics,))

    def test_fix_provider_order_id_conflict_is_atomic_and_restart_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            ledger = _ledger(
                path,
                PROPHETX_SANDBOX_FIX_PROFILE.bookmaker_profile_version,
            )
            identity = bind_before_effect(ledger, attempt_id="try-1")
            ledger.mark_submitted("try-1", submitted_at=SUBMITTED)
            first = ProphetXFixExecutionReport(
                "exec-1",
                identity.client_order_id,
                "order-A",
                "2026-09-22T20:00:40+00:00",
                identity.effect_fingerprint,
                "NEW",
                "0",
            )
            normalize_fix_execution_reports(identity, (first,), ledger=ledger)
            restarted = RealExecutionLedger(path)

            conflict = ProphetXFixExecutionReport(
                "exec-2",
                identity.client_order_id,
                "order-B",
                "2026-09-22T20:00:50+00:00",
                identity.effect_fingerprint,
                "PARTIAL_FILL",
                "1",
            )
            with self.assertRaisesRegex(
                ProphetXEvidenceConflict,
                "conflicts with durable attempt",
            ):
                normalize_fix_execution_reports(
                    identity, (conflict,), ledger=restarted
                )
            self.assertEqual(
                restarted.provider_assigned_order_id(
                    attempt_id="try-1", provider_id="prophetx"
                ),
                "order-A",
            )

            second_path = Path(tmp) / "second.jsonl"
            second = _ledger(
                second_path,
                PROPHETX_SANDBOX_FIX_PROFILE.bookmaker_profile_version,
            )
            second_identity = bind_before_effect(second, attempt_id="try-1")
            second.mark_submitted("try-1", submitted_at=SUBMITTED)
            batch_a = ProphetXFixExecutionReport(
                "exec-1",
                second_identity.client_order_id,
                "order-A",
                "2026-09-22T20:00:40+00:00",
                second_identity.effect_fingerprint,
                "NEW",
                "0",
            )
            batch_b = ProphetXFixExecutionReport(
                "exec-2",
                second_identity.client_order_id,
                "order-B",
                "2026-09-22T20:00:50+00:00",
                second_identity.effect_fingerprint,
                "PARTIAL_FILL",
                "1",
            )
            with self.assertRaisesRegex(
                ProphetXEvidenceConflict,
                "conflict on provider assigned order id",
            ):
                normalize_fix_execution_reports(
                    second_identity, (batch_a, batch_b), ledger=second
                )
            self.assertIsNone(
                second.provider_assigned_order_id(
                    attempt_id="try-1", provider_id="prophetx"
                )
            )


    def test_capability_matrix_is_transport_specific(self):
        self.assertEqual(
            capabilities_for(ProphetXTransport.REST_DIRECT_LINK),
            REST_DIRECT_LINK_CAPABILITIES,
        )
        self.assertEqual(
            capabilities_for(ProphetXTransport.FIX_ORDER_ENTRY),
            FIX_ORDER_ENTRY_CAPABILITIES,
        )
        self.assertTrue(
            FIX_ORDER_ENTRY_CAPABILITIES.idempotent_redelivery_identical_economics
        )
        self.assertTrue(
            FIX_ORDER_ENTRY_CAPABILITIES.order_status_by_client_or_provider_id
        )

    def test_production_profile_does_not_activate_production_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            identity = bind_before_effect(
                _ledger(
                    Path(tmp) / "execution.jsonl",
                    PROPHETX_PRODUCTION_REST_PROFILE.bookmaker_profile_version,
                ),
                attempt_id="try-1",
            )
            self.assertFalse(identity.production_write_qualified)


if __name__ == "__main__":
    unittest.main()
