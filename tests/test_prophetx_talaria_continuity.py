import json
import unittest

from autosport.prophetx_talaria_continuity import (
    ProphetXSubscription,
    ProphetXTalariaContinuity,
    TalariaContinuityError,
    TalariaProtocolError,
    build_registration_request,
    canonical_subscriptions,
    parse_registration_evidence,
    parse_talaria_frame,
)


def _frame(event, data, channel=None):
    outer = {
        "event": event,
        "data": json.dumps(data, ensure_ascii=False, separators=(",", ":")),
    }
    if channel is not None:
        outer["channel"] = channel
    return json.dumps(outer, ensure_ascii=False, separators=(",", ":"))


def _registration_payload(*, channel_limit=4):
    return {
        "data": {
            "channel_limit": channel_limit,
            "authenticated": {"auth": "signin-auth", "user_data": "{}"},
            "authorized_channel": [
                {
                    "channel_name": "market-main",
                    "auth": "main-auth",
                    "binding_events": [{"name": "market_selections"}],
                },
                {
                    "channel_name": "market-prop",
                    "auth": "prop-auth",
                    "binding_events": ["market_selections"],
                },
                {
                    "channel_name": "account-private",
                    "auth": "account-auth",
                    "binding_events": [{"name": "orders"}],
                },
            ],
        }
    }


def _continuity():
    return ProphetXTalariaContinuity(
        (
            ProphetXSubscription.event(18756),
            ProphetXSubscription.event_subtype(
                18756,
                "player_to_record_a_double_double",
            ),
        )
    )


def _established(*, rebase=False):
    authority = _continuity()
    generation = authority.open_generation(
        {"app_id": "app-1", "key": "key-1", "host": "sandbox.example"}
    )
    authority.ingest_frame(
        generation,
        _frame(
            "pusher:connection_established",
            {"socket_id": "1234.5678"},
        ),
    )
    authority.accept_registration(
        generation,
        parse_registration_evidence(_registration_payload()),
    )
    authority.ingest_frame(
        generation,
        _frame("pusher:signin_success", {}),
    )
    authority.ingest_frame(
        generation,
        _frame(
            "pusher_internal:subscription_succeeded",
            {},
            channel="market-main",
        ),
    )
    authority.ingest_frame(
        generation,
        _frame(
            "pusher_internal:subscription_succeeded",
            {},
            channel="market-prop",
        ),
    )
    if rebase:
        authority.mark_rebase_complete(generation, "a" * 64)
    return authority, generation


class ProphetXTalariaContinuityTests(unittest.TestCase):
    def test_frame_requires_double_json_data_boundary(self):
        raw = json.dumps(
            {
                "event": "pusher:connection_established",
                "data": {"socket_id": "1.2"},
            }
        )
        with self.assertRaisesRegex(TalariaProtocolError, "Talaria data must be JSON text"):
            parse_talaria_frame(raw)

    def test_frame_rejects_duplicate_outer_keys(self):
        raw = '{"event":"first","event":"second","data":"{}"}'
        with self.assertRaisesRegex(TalariaProtocolError, "duplicate JSON key"):
            parse_talaria_frame(raw)

    def test_frame_rejects_duplicate_inner_keys(self):
        raw = json.dumps(
            {"event": "control", "data": '{"value":1,"value":2}'}
        )
        with self.assertRaisesRegex(TalariaProtocolError, "duplicate JSON key"):
            parse_talaria_frame(raw)

    def test_frame_rejects_nonfinite_inner_json(self):
        raw = json.dumps({"event": "control", "data": '{"value":NaN}'})
        with self.assertRaisesRegex(TalariaProtocolError, "non-standard JSON constant"):
            parse_talaria_frame(raw)

    def test_frame_requires_object_inner_data(self):
        raw = json.dumps({"event": "control", "data": "[1,2,3]"})
        with self.assertRaisesRegex(TalariaProtocolError, "must decode to a JSON object"):
            parse_talaria_frame(raw)

    def test_event_subscription_canonicalizes_integer_identity(self):
        self.assertEqual(
            ProphetXSubscription.event(18756),
            ProphetXSubscription(kind="event", value="18756"),
        )

    def test_invalid_event_identity_is_rejected(self):
        for bad in (True, 0, -1, "0", "001", " event "):
            with self.subTest(bad=bad):
                with self.assertRaises(TalariaProtocolError):
                    ProphetXSubscription.event(bad)

    def test_event_subtype_is_explicit_and_delimiter_safe(self):
        value = ProphetXSubscription.event_subtype(18756, "player_total_hits")
        self.assertEqual(value.kind, "event_subtype")
        self.assertEqual(value.value, "18756:player_total_hits")
        with self.assertRaisesRegex(TalariaProtocolError, "delimiter"):
            ProphetXSubscription.event_subtype(18756, "bad:subtype")

    def test_direct_invalid_subscription_construction_is_rejected(self):
        with self.assertRaises(TalariaProtocolError):
            ProphetXSubscription(kind="legacy_shared", value="18756")
        with self.assertRaises(TalariaProtocolError):
            ProphetXSubscription(kind="event_subtype", value="18756")

    def test_duplicate_subscriptions_are_rejected(self):
        sub = ProphetXSubscription.event(18756)
        with self.assertRaisesRegex(TalariaProtocolError, "duplicates"):
            canonical_subscriptions((sub, sub))

    def test_registration_request_is_complete_declarative_current_model(self):
        request = build_registration_request(
            "1234.5678",
            (
                ProphetXSubscription.event_subtype(18756, "player_total_hits"),
                ProphetXSubscription.event(18756),
            ),
        )
        self.assertEqual(
            request,
            {
                "socket_id": "1234.5678",
                "service": "pusher",
                "subscriptions": [
                    {"type": "event", "ids": ["18756"]},
                    {
                        "type": "event_subtype",
                        "ids": ["18756:player_total_hits"],
                    },
                ],
            },
        )

    def test_registration_request_never_emits_legacy_subscription_type(self):
        request = build_registration_request(
            "1234.5678",
            (ProphetXSubscription.event(18756),),
        )
        encoded = json.dumps(request)
        self.assertNotIn("tournament", encoded)
        self.assertNotIn("legacy", encoded)

    def test_registration_evidence_accepts_data_wrapper(self):
        evidence = parse_registration_evidence(_registration_payload())
        self.assertEqual(evidence.channel_limit, 4)
        self.assertEqual(
            evidence.market_channels,
            ("market-main", "market-prop"),
        )
        self.assertEqual(len(evidence.channels), 3)

    def test_registration_evidence_accepts_documented_fields_top_level(self):
        evidence = parse_registration_evidence(_registration_payload()["data"])
        self.assertEqual(evidence.channel_limit, 4)

    def test_registration_evidence_rejects_mixed_envelopes(self):
        payload = _registration_payload()
        payload["channel_limit"] = 4
        with self.assertRaisesRegex(TalariaProtocolError, "must not mix"):
            parse_registration_evidence(payload)

    def test_channel_limit_is_provider_evidence_not_boolean_or_zero(self):
        for bad in (True, 0, -1, "4", None):
            with self.subTest(bad=bad):
                payload = _registration_payload()
                payload["data"]["channel_limit"] = bad
                with self.assertRaises(TalariaProtocolError):
                    parse_registration_evidence(payload)

    def test_registration_over_provider_channel_limit_fails_closed(self):
        with self.assertRaisesRegex(TalariaProtocolError, "channel_limit"):
            parse_registration_evidence(_registration_payload(channel_limit=2))

    def test_duplicate_authorized_channel_is_rejected(self):
        payload = _registration_payload()
        payload["data"]["authorized_channel"][1]["channel_name"] = "market-main"
        with self.assertRaisesRegex(TalariaProtocolError, "duplicate channel"):
            parse_registration_evidence(payload)

    def test_registration_requires_market_selections_authority(self):
        payload = _registration_payload()
        for channel in payload["data"]["authorized_channel"]:
            channel["binding_events"] = [{"name": "orders"}]
        with self.assertRaisesRegex(TalariaProtocolError, "no market_selections"):
            parse_registration_evidence(payload)

    def test_binding_events_reject_malformed_entries(self):
        payload = _registration_payload()
        payload["data"]["authorized_channel"][0]["binding_events"] = [123]
        with self.assertRaisesRegex(TalariaProtocolError, "strings or objects"):
            parse_registration_evidence(payload)

    def test_initial_state_is_fail_closed(self):
        snapshot = _continuity().snapshot()
        self.assertFalse(snapshot.decision_eligible)
        self.assertIn("PROPHETX_TALARIA_DISCONNECTED", snapshot.quality_flags)
        self.assertIn("PROPHETX_TALARIA_REBASE_REQUIRED", snapshot.quality_flags)

    def test_open_generation_does_not_claim_continuity(self):
        authority = _continuity()
        generation = authority.open_generation({"key": "k", "host": "h"})
        self.assertEqual(generation, 1)
        snapshot = authority.snapshot()
        self.assertTrue(snapshot.transport_open)
        self.assertFalse(snapshot.decision_eligible)
        self.assertTrue(snapshot.rebase_required)

    def test_connection_established_binds_socket_to_generation(self):
        authority = _continuity()
        generation = authority.open_generation({"key": "k", "host": "h"})
        authority.ingest_frame(
            generation,
            _frame(
                "pusher:connection_established",
                {"socket_id": "1.2"},
            ),
        )
        self.assertEqual(authority.snapshot().socket_id, "1.2")
        self.assertEqual(
            authority.registration_request(generation)["socket_id"],
            "1.2",
        )

    def test_registration_cannot_precede_connection_established(self):
        authority = _continuity()
        generation = authority.open_generation({"key": "k"})
        with self.assertRaisesRegex(TalariaContinuityError, "established"):
            authority.accept_registration(
                generation,
                parse_registration_evidence(_registration_payload()),
            )

    def test_signin_success_cannot_precede_registration(self):
        authority = _continuity()
        generation = authority.open_generation({"key": "k"})
        authority.ingest_frame(
            generation,
            _frame(
                "pusher:connection_established",
                {"socket_id": "1.2"},
            ),
        )
        with self.assertRaisesRegex(TalariaContinuityError, "before registration"):
            authority.ingest_frame(
                generation,
                _frame("pusher:signin_success", {}),
            )

    def test_subscription_ack_cannot_precede_signin(self):
        authority = _continuity()
        generation = authority.open_generation({"key": "k"})
        authority.ingest_frame(
            generation,
            _frame(
                "pusher:connection_established",
                {"socket_id": "1.2"},
            ),
        )
        authority.accept_registration(
            generation,
            parse_registration_evidence(_registration_payload()),
        )
        with self.assertRaisesRegex(TalariaContinuityError, "before signin"):
            authority.ingest_frame(
                generation,
                _frame(
                    "pusher_internal:subscription_succeeded",
                    {},
                    channel="market-main",
                ),
            )

    def test_subscription_ack_requires_authorized_market_channel(self):
        authority, generation = _established()
        with self.assertRaisesRegex(TalariaProtocolError, "authorized market"):
            authority.ingest_frame(
                generation,
                _frame(
                    "pusher_internal:subscription_succeeded",
                    {},
                    channel="account-private",
                ),
            )

    def test_duplicate_subscription_ack_is_rejected(self):
        authority = _continuity()
        generation = authority.open_generation({"key": "k"})
        authority.ingest_frame(
            generation,
            _frame(
                "pusher:connection_established",
                {"socket_id": "1.2"},
            ),
        )
        authority.accept_registration(
            generation,
            parse_registration_evidence(_registration_payload()),
        )
        authority.ingest_frame(generation, _frame("pusher:signin_success", {}))
        ack = _frame(
            "pusher_internal:subscription_succeeded",
            {},
            channel="market-main",
        )
        authority.ingest_frame(generation, ack)
        with self.assertRaisesRegex(TalariaProtocolError, "duplicate"):
            authority.ingest_frame(generation, ack)

    def test_all_subscription_acks_still_require_rebase(self):
        authority, _ = _established()
        snapshot = authority.snapshot()
        self.assertEqual(
            snapshot.subscribed_market_channels,
            ("market-main", "market-prop"),
        )
        self.assertFalse(snapshot.decision_eligible)
        self.assertTrue(snapshot.rebase_required)

    def test_rebase_cannot_complete_before_handshake(self):
        authority = _continuity()
        generation = authority.open_generation({"key": "k"})
        with self.assertRaisesRegex(TalariaContinuityError, "subscriptions"):
            authority.mark_rebase_complete(generation, "a" * 64)

    def test_rebase_digest_is_strict_sha256(self):
        authority, generation = _established()
        for bad in ("a", "A" * 64, "g" * 64, "a" * 63):
            with self.subTest(bad=bad):
                with self.assertRaises(TalariaProtocolError):
                    authority.mark_rebase_complete(generation, bad)

    def test_explicit_rebase_proof_enables_decision_eligibility(self):
        authority, generation = _established()
        authority.mark_rebase_complete(generation, "a" * 64)
        snapshot = authority.snapshot()
        self.assertTrue(snapshot.decision_eligible)
        self.assertEqual(snapshot.rebase_evidence_sha256, "a" * 64)
        self.assertEqual(snapshot.quality_flags, ())

    def test_market_frame_before_rebase_is_visible_but_not_eligible(self):
        authority, generation = _established()
        envelope = authority.ingest_frame(
            generation,
            _frame(
                "market_selections",
                {"scope": {"event_id": 18756}, "selections": []},
                channel="market-main",
            ),
        )
        self.assertIsNotNone(envelope)
        self.assertFalse(envelope.continuity_eligible)

    def test_market_frame_after_rebase_is_generation_bound_and_eligible(self):
        authority, generation = _established(rebase=True)
        envelope = authority.ingest_frame(
            generation,
            _frame(
                "market_selections",
                {"scope": {"event_id": 18756}, "selections": []},
                channel="market-main",
            ),
        )
        self.assertEqual(envelope.generation, generation)
        self.assertEqual(envelope.socket_id, "1234.5678")
        self.assertEqual(envelope.event_id, "18756")
        self.assertIsNone(envelope.sub_type)
        self.assertTrue(envelope.continuity_eligible)

    def test_prop_scope_uses_provider_scope_not_channel_name(self):
        authority, generation = _established(rebase=True)
        envelope = authority.ingest_frame(
            generation,
            _frame(
                "market_selections",
                {
                    "scope": {
                        "event_id": "18756",
                        "sub_type": "player_to_record_a_double_double",
                    },
                    "selections": [],
                },
                channel="market-prop",
            ),
        )
        self.assertEqual(envelope.event_id, "18756")
        self.assertEqual(
            envelope.sub_type,
            "player_to_record_a_double_double",
        )

    def test_channel_name_cannot_substitute_for_missing_provider_scope(self):
        authority, generation = _established(rebase=True)
        with self.assertRaisesRegex(TalariaProtocolError, "explicit provider scope"):
            authority.ingest_frame(
                generation,
                _frame(
                    "market_selections",
                    {"selections": []},
                    channel="market-main",
                ),
            )

    def test_market_scope_outside_declarative_subscription_is_rejected(self):
        authority, generation = _established(rebase=True)
        with self.assertRaisesRegex(TalariaProtocolError, "outside"):
            authority.ingest_frame(
                generation,
                _frame(
                    "market_selections",
                    {"scope": {"event_id": 99999}, "selections": []},
                    channel="market-main",
                ),
            )

    def test_subtype_scope_requires_exact_event_subtype_subscription(self):
        authority, generation = _established(rebase=True)
        with self.assertRaisesRegex(TalariaProtocolError, "outside"):
            authority.ingest_frame(
                generation,
                _frame(
                    "market_selections",
                    {
                        "scope": {
                            "event_id": 18756,
                            "sub_type": "different_prop",
                        }
                    },
                    channel="market-prop",
                ),
            )

    def test_market_frame_requires_authorized_market_channel(self):
        authority, generation = _established(rebase=True)
        with self.assertRaisesRegex(TalariaProtocolError, "authorized"):
            authority.ingest_frame(
                generation,
                _frame(
                    "market_selections",
                    {"scope": {"event_id": 18756}},
                    channel="account-private",
                ),
            )

    def test_new_generation_fences_old_frames(self):
        authority, old_generation = _established(rebase=True)
        new_generation = authority.open_generation({"key": "new", "host": "new"})
        self.assertEqual(new_generation, old_generation + 1)
        with self.assertRaisesRegex(TalariaContinuityError, "stale"):
            authority.ingest_frame(
                old_generation,
                _frame(
                    "market_selections",
                    {"scope": {"event_id": 18756}},
                    channel="market-main",
                ),
            )
        self.assertFalse(authority.snapshot().decision_eligible)
        self.assertTrue(authority.snapshot().rebase_required)

    def test_disconnect_revokes_eligibility_and_current_frames(self):
        authority, generation = _established(rebase=True)
        authority.disconnect(generation)
        snapshot = authority.snapshot()
        self.assertFalse(snapshot.decision_eligible)
        self.assertIn("PROPHETX_TALARIA_DISCONNECTED", snapshot.quality_flags)
        with self.assertRaisesRegex(TalariaContinuityError, "not open"):
            authority.ingest_frame(
                generation,
                _frame("pusher:pong", {}),
            )

    def test_provider_error_revokes_eligibility(self):
        authority, generation = _established(rebase=True)
        authority.ingest_frame(
            generation,
            _frame("pusher:error", {"message": "provider reset"}),
        )
        snapshot = authority.snapshot()
        self.assertFalse(snapshot.transport_open)
        self.assertFalse(snapshot.decision_eligible)
        self.assertTrue(snapshot.rebase_required)
        self.assertIn("PROPHETX_TALARIA_FAILURE_EVIDENCE", snapshot.quality_flags)

    def test_same_connection_config_does_not_force_reconnect(self):
        authority = _continuity()
        authority.open_generation({"key": "k", "app_id": "a"})
        digest = authority.snapshot().config_sha256
        changed = authority.observe_connection_config({"app_id": "a", "key": "k"})
        self.assertFalse(changed)
        self.assertTrue(authority.snapshot().transport_open)
        self.assertEqual(authority.snapshot().config_sha256, digest)

    def test_connection_config_change_revokes_current_generation(self):
        authority, _ = _established(rebase=True)
        changed = authority.observe_connection_config(
            {"app_id": "app-2", "key": "key-2", "host": "other"}
        )
        self.assertTrue(changed)
        snapshot = authority.snapshot()
        self.assertFalse(snapshot.transport_open)
        self.assertFalse(snapshot.decision_eligible)
        self.assertTrue(snapshot.rebase_required)

    def test_socket_id_change_inside_generation_fails_closed(self):
        authority = _continuity()
        generation = authority.open_generation({"key": "k"})
        authority.ingest_frame(
            generation,
            _frame(
                "pusher:connection_established",
                {"socket_id": "1.2"},
            ),
        )
        with self.assertRaisesRegex(TalariaContinuityError, "socket_id changed"):
            authority.ingest_frame(
                generation,
                _frame(
                    "pusher:connection_established",
                    {"socket_id": "3.4"},
                ),
            )
        self.assertFalse(authority.snapshot().transport_open)
        self.assertTrue(authority.snapshot().rebase_required)

    def test_unknown_control_event_is_ignored_not_published(self):
        authority, generation = _established()
        before = authority.snapshot()
        result = authority.ingest_frame(
            generation,
            _frame("pusher:pong", {}),
        )
        self.assertIsNone(result)
        self.assertEqual(authority.snapshot(), before)

    def test_generation_argument_rejects_boolean_and_unopened_zero(self):
        authority = _continuity()
        with self.assertRaises(TalariaContinuityError):
            authority.ingest_frame(True, _frame("pusher:pong", {}))
        with self.assertRaises(TalariaContinuityError):
            authority.ingest_frame(0, _frame("pusher:pong", {}))

    def test_authority_exposes_no_account_or_write_execution_surface(self):
        authority = _continuity()
        for name in (
            "login",
            "get_balance",
            "submit_order",
            "submit_multiple_orders",
            "cancel_order",
            "cancel_multiple_orders",
        ):
            self.assertFalse(hasattr(authority, name))


if __name__ == "__main__":
    unittest.main()
