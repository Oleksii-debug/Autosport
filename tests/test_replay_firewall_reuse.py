import threading
import unittest

from autosport.domain import MarketEvent
from autosport.replay import FutureLeakageError, ReplayEngine, ReplayLeakageFirewall


class ReplayFirewallReuseTests(unittest.TestCase):
    @staticmethod
    def _event() -> MarketEvent:
        return MarketEvent.from_dict(
            {
                "event_id": "event-1",
                "market_id": "winner",
                "selection_id": "alice",
                "decimal_odds": "2.0",
                "observed_ts": "2026-01-01T00:00:00+00:00",
                "source_id": "fixture",
                "sequence": 1,
            }
        )

    def test_replay_engine_rejects_firewall_subclasses_before_event_iteration(self) -> None:
        class Firewall(ReplayLeakageFirewall):
            pass

        class ExplosiveEvents:
            def __iter__(self):
                raise AssertionError("event iteration executed")

        with self.assertRaisesRegex(TypeError, "exact ReplayLeakageFirewall"):
            ReplayEngine(ExplosiveEvents(), Firewall())

    def test_completion_capability_rejects_bytes_subclasses(self) -> None:
        class Capability(bytes):
            pass

        firewall = ReplayLeakageFirewall({"event-1": "alice"})
        capability = firewall._claim_for_replay()

        with self.assertRaisesRegex(
            FutureLeakageError,
            "invalid replay completion capability",
        ):
            firewall._complete_replay(Capability(capability))
        with self.assertRaisesRegex(FutureLeakageError, "sealed"):
            firewall.result_for("event-1")

        firewall._complete_replay(capability)
        self.assertEqual(firewall.result_for("event-1"), "alice")

    def test_invalid_run_controls_do_not_retire_firewall(self) -> None:
        firewall = ReplayLeakageFirewall({"event-1": "alice"})
        engine = ReplayEngine([self._event()], firewall)

        invalid_calls = (
            {"speed": True},
            {"speed": -1},
            {"speed": float("nan")},
            {"speed": float("inf")},
            {"run_id": ""},
            {"run_id": " spaced "},
        )
        for kwargs in invalid_calls:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises((TypeError, ValueError)):
                    engine.run(lambda _event: None, **kwargs)
                with self.assertRaisesRegex(FutureLeakageError, "sealed"):
                    firewall.result_for("event-1")

        run = engine.run(lambda _event: None, run_id="valid")
        self.assertEqual(run.run_id, "valid")
        self.assertEqual(firewall.result_for("event-1"), "alice")

    def test_firewall_rejects_substituted_result_containers_and_values(self) -> None:
        class Results(dict):
            pass

        class Text(str):
            pass

        with self.assertRaisesRegex(TypeError, "exact dict"):
            ReplayLeakageFirewall(Results({"event-1": "alice"}))
        with self.assertRaisesRegex(TypeError, "event ids must be exact strings"):
            ReplayLeakageFirewall({Text("event-1"): "alice"})
        with self.assertRaisesRegex(TypeError, "result values must be exact strings"):
            ReplayLeakageFirewall({"event-1": Text("alice")})

    def test_result_lookup_rejects_event_id_subclass(self) -> None:
        class Text(str):
            pass

        firewall = ReplayLeakageFirewall({"event-1": "alice"})
        ReplayEngine([self._event()], firewall).run(lambda _event: None, run_id="done")

        with self.assertRaisesRegex(TypeError, "event_id must be an exact string"):
            firewall.result_for(Text("event-1"))

    def test_completed_engine_rejects_second_run_before_callback(self) -> None:
        firewall = ReplayLeakageFirewall({"event-1": "alice"})
        engine = ReplayEngine([self._event()], firewall)
        engine.run(lambda _event: None, run_id="first")
        self.assertEqual(firewall.result_for("event-1"), "alice")

        seen: list[str] = []
        with self.assertRaisesRegex(FutureLeakageError, "already unlocked"):
            engine.run(lambda event: seen.append(event.event_id), run_id="second")

        self.assertEqual(seen, [])

    def test_completed_firewall_cannot_be_reused_by_fresh_engine(self) -> None:
        firewall = ReplayLeakageFirewall({"event-1": "alice"})
        ReplayEngine([self._event()], firewall).run(lambda _event: None, run_id="first")

        seen: list[str] = []
        fresh_engine = ReplayEngine([self._event()], firewall)
        with self.assertRaisesRegex(FutureLeakageError, "use a fresh firewall"):
            fresh_engine.run(lambda event: seen.append(event.event_id), run_id="second")

        self.assertEqual(seen, [])

    def test_concurrent_shared_firewall_allows_exactly_one_replay(self) -> None:
        firewall = ReplayLeakageFirewall({"event-1": "alice"})
        first_engine = ReplayEngine([self._event()], firewall)
        second_engine = ReplayEngine([self._event()], firewall)
        first_callback_entered = threading.Event()
        release_first = threading.Event()
        first_seen: list[str] = []
        first_errors: list[BaseException] = []

        def first_callback(event: MarketEvent) -> None:
            first_seen.append(event.event_id)
            first_callback_entered.set()
            if not release_first.wait(timeout=5):
                raise RuntimeError("test timed out waiting to release first replay")

        def run_first() -> None:
            try:
                first_engine.run(first_callback, run_id="first")
            except BaseException as exc:
                first_errors.append(exc)

        first_thread = threading.Thread(target=run_first)
        first_thread.start()
        self.assertTrue(first_callback_entered.wait(timeout=5))

        second_seen: list[str] = []
        with self.assertRaisesRegex(FutureLeakageError, "already claimed"):
            second_engine.run(lambda event: second_seen.append(event.event_id), run_id="second")

        self.assertEqual(second_seen, [])
        with self.assertRaisesRegex(FutureLeakageError, "sealed"):
            firewall.result_for("event-1")

        release_first.set()
        first_thread.join(timeout=5)
        self.assertFalse(first_thread.is_alive())
        self.assertEqual(first_errors, [])
        self.assertEqual(first_seen, ["event-1"])
        self.assertEqual(firewall.result_for("event-1"), "alice")

    def test_failed_replay_retires_firewall_fail_closed(self) -> None:
        firewall = ReplayLeakageFirewall({"event-1": "alice"})
        engine = ReplayEngine([self._event()], firewall)

        def fail_callback(_event: MarketEvent) -> None:
            raise RuntimeError("callback failed")

        with self.assertRaisesRegex(RuntimeError, "callback failed"):
            engine.run(fail_callback, run_id="failed")

        with self.assertRaisesRegex(FutureLeakageError, "sealed"):
            firewall.result_for("event-1")

        seen: list[str] = []
        with self.assertRaisesRegex(FutureLeakageError, "already claimed"):
            ReplayEngine([self._event()], firewall).run(
                lambda event: seen.append(event.event_id),
                run_id="retry",
            )
        self.assertEqual(seen, [])

    def test_strategy_callback_cannot_complete_or_read_results_early(self) -> None:
        firewall = ReplayLeakageFirewall({"event-1": "alice"})
        engine = ReplayEngine([self._event()], firewall)
        seen: list[str] = []

        def adversarial_callback(event: MarketEvent) -> None:
            seen.append(event.event_id)
            self.assertFalse(hasattr(firewall, "unlock"))
            with self.assertRaisesRegex(FutureLeakageError, "sealed"):
                firewall.result_for(event.event_id)
            with self.assertRaisesRegex(FutureLeakageError, "invalid replay completion capability"):
                firewall._complete_replay(b"callback-does-not-own-capability")
            with self.assertRaisesRegex(FutureLeakageError, "sealed"):
                firewall.result_for(event.event_id)

        run = engine.run(adversarial_callback, run_id="protected")

        self.assertEqual(run.event_count, 1)
        self.assertEqual(seen, ["event-1"])
        self.assertEqual(firewall.result_for("event-1"), "alice")


if __name__ == "__main__":
    unittest.main()
