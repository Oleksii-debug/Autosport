import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from autosport.betfair_account_readonly import ADAPTER_ID, ADAPTER_VERSION
from autosport.betfair_read_completeness import (
    BetfairObservationCompleteness,
    BetfairReadCompletenessWitness,
    _issue,
)
from autosport.betfair_read_observation_history import (
    BetfairReadObservationHistory,
    BetfairReadObservationHistoryError,
)


def _sha(label: str) -> str:
    return sha256(label.encode("utf-8")).hexdigest()


def _issued(
    *,
    attempt: str,
    completeness: BetfairObservationCompleteness,
    started: str,
    finished: str,
    failure_code: str | None,
    pages=(),
    rows_observed: int = 0,
) -> BetfairReadCompletenessWitness:
    witness = BetfairReadCompletenessWitness(
        operation="listCurrentOrders",
        completeness=completeness,
        venue_id="betfair",
        account_id="account-a",
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        query_sha256=_sha("same-query"),
        attempt_id=_sha(attempt),
        started_at=started,
        finished_at=finished,
        pages=tuple(pages),
        rows_observed=rows_observed,
        failure_code=failure_code,
    )
    return _issue(witness)


class BetfairReadObservationHistoryTests(unittest.TestCase):
    def _history(self, root: Path) -> BetfairReadObservationHistory:
        workspace = root / "workspace"
        authority = root / "authority"
        workspace.mkdir(exist_ok=True)
        authority.mkdir(exist_ok=True)
        return BetfairReadObservationHistory(
            workspace,
            authority_root=authority,
        )

    def test_partial_then_recovery_survive_restart_without_rewriting_outage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = self._history(root)
            partial = _issued(
                attempt="partial-1",
                completeness=BetfairObservationCompleteness.PARTIAL,
                started="2026-09-22T07:00:00+00:00",
                finished="2026-09-22T07:00:02+00:00",
                failure_code="timeout_error",
                pages=((0, 1000, True, _sha("page-1")),),
                rows_observed=7,
            )
            first = history.append(partial)
            self.assertTrue(first.historical_only)
            self.assertEqual(first.completeness, BetfairObservationCompleteness.PARTIAL)

            complete = _issued(
                attempt="complete-2",
                completeness=(
                    BetfairObservationCompleteness.COMPLETE_FOR_DECLARED_QUERY_WINDOW
                ),
                started="2026-09-22T07:01:00+00:00",
                finished="2026-09-22T07:01:01+00:00",
                failure_code=None,
                pages=((0, 1000, False, _sha("page-2")),),
                rows_observed=0,
            )
            second = history.append(complete)
            self.assertEqual(second.sequence, 2)

            reopened = self._history(root)
            records = reopened.records(_sha("same-query"))
            self.assertEqual(
                [record.attempt_id for record in records],
                [_sha("partial-1"), _sha("complete-2")],
            )
            self.assertEqual(
                records[0].completeness,
                BetfairObservationCompleteness.PARTIAL,
            )
            self.assertEqual(records[0].failure_code, "timeout_error")
            self.assertEqual(
                records[1].completeness,
                BetfairObservationCompleteness.COMPLETE_FOR_DECLARED_QUERY_WINDOW,
            )
            self.assertTrue(all(record.historical_only for record in records))

            before_recovery = reopened.records_as_of(
                _sha("same-query"),
                as_of=datetime(2026, 9, 22, 7, 0, 30, tzinfo=timezone.utc),
            )
            self.assertEqual(len(before_recovery), 1)
            self.assertEqual(before_recovery[0].attempt_id, _sha("partial-1"))

    def test_caller_constructed_witness_cannot_be_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = self._history(root)
            forged = BetfairReadCompletenessWitness(
                operation="listCurrentOrders",
                completeness=BetfairObservationCompleteness.UNAVAILABLE_TRANSIENT,
                venue_id="betfair",
                account_id="account-a",
                adapter_id=ADAPTER_ID,
                adapter_version=ADAPTER_VERSION,
                query_sha256=_sha("same-query"),
                attempt_id=_sha("forged"),
                started_at="2026-09-22T07:00:00+00:00",
                finished_at="2026-09-22T07:00:01+00:00",
                pages=(),
                rows_observed=0,
                failure_code="timeout_error",
            )
            with self.assertRaisesRegex(
                BetfairReadObservationHistoryError, "product-issued"
            ):
                history.append(forged)
            self.assertEqual(history.records(_sha("same-query")), ())

    def test_exact_retry_is_idempotent_and_conflicting_attempt_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = self._history(root)
            original = _issued(
                attempt="attempt-1",
                completeness=BetfairObservationCompleteness.UNAVAILABLE_TRANSIENT,
                started="2026-09-22T07:00:00+00:00",
                finished="2026-09-22T07:00:01+00:00",
                failure_code="timeout_error",
            )
            first = history.append(original)
            again = history.append(original)
            self.assertEqual(first.evidence_sha256, again.evidence_sha256)
            self.assertEqual(len(history.records(_sha("same-query"))), 1)

            conflicting = _issue(
                replace(
                    original,
                    completeness=BetfairObservationCompleteness.AUTH_INVALID_OR_EXPIRED,
                    failure_code="invalid_session_information",
                )
            )
            with self.assertRaisesRegex(
                BetfairReadObservationHistoryError, "attempt_id was reused"
            ):
                history.append(conflicting)
            self.assertEqual(len(history.records(_sha("same-query"))), 1)

    def test_deleted_or_rolled_back_local_history_is_not_accepted_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = self._history(root)
            witness = _issued(
                attempt="attempt-1",
                completeness=BetfairObservationCompleteness.UNAVAILABLE_TRANSIENT,
                started="2026-09-22T07:00:00+00:00",
                finished="2026-09-22T07:00:01+00:00",
                failure_code="service_busy",
            )
            history.append(witness)
            path = history._path(_sha("same-query"))
            original = path.read_bytes()

            path.unlink()
            reopened = self._history(root)
            with self.assertRaisesRegex(
                BetfairReadObservationHistoryError, "rollback/recovery"
            ):
                reopened.records(_sha("same-query"))

            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(original)
            recovered = self._history(root).records(_sha("same-query"))
            self.assertEqual(len(recovered), 1)

    def test_tampered_persisted_record_fails_before_it_can_be_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = self._history(root)
            witness = _issued(
                attempt="attempt-1",
                completeness=BetfairObservationCompleteness.UNAVAILABLE_TRANSIENT,
                started="2026-09-22T07:00:00+00:00",
                finished="2026-09-22T07:00:01+00:00",
                failure_code="service_busy",
            )
            history.append(witness)
            path = history._path(_sha("same-query"))
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["records"][0]["failure_code"] = "timeout_error"
            path.write_text(json.dumps(payload), encoding="utf-8")

            reopened = self._history(root)
            with self.assertRaisesRegex(
                BetfairReadObservationHistoryError, "rollback/recovery"
            ):
                reopened.records(_sha("same-query"))


if __name__ == "__main__":
    unittest.main()
