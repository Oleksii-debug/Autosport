import hashlib
import json
import tempfile
from pathlib import Path

import pytest

from autosport.decision_ledger import (
    DecisionLedgerIntegrityError,
    DecisionRecord,
    JsonlDecisionLedger,
)


OBSERVED = "2026-01-01T00:00:00+00:00"
RECORDED = "2026-01-01T00:00:01+00:00"


def _record(*, observed_ts: object = OBSERVED, recorded_at: object = RECORDED) -> DecisionRecord:
    return DecisionRecord(
        replay_run_id="run-chronology",
        agent="agent",
        observed_ts=observed_ts,  # type: ignore[arg-type]
        action="OBSERVE",
        payload={"x": 1},
        context_hash="ctx",
        decision_id="decision-chronology",
        recorded_at=recorded_at,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("observed_ts", "not-a-timestamp", "observed_ts must be valid ISO-8601"),
        (
            "observed_ts",
            "2026-01-01T00:00:00",
            "observed_ts must be timezone-aware ISO-8601",
        ),
        ("recorded_at", "not-a-timestamp", "recorded_at must be valid ISO-8601"),
        (
            "recorded_at",
            "2026-01-01T00:00:01",
            "recorded_at must be timezone-aware ISO-8601",
        ),
    ),
)
def test_decision_record_requires_trusted_timezone_aware_chronology(
    field: str,
    value: str,
    message: str,
) -> None:
    kwargs = {"observed_ts": OBSERVED, "recorded_at": RECORDED}
    kwargs[field] = value
    with pytest.raises(ValueError, match=message):
        _record(**kwargs)


def test_decision_record_rejects_observation_after_recording() -> None:
    with pytest.raises(ValueError, match="observed_ts cannot be after recorded_at"):
        _record(
            observed_ts="2026-01-01T00:00:02+00:00",
            recorded_at=RECORDED,
        )


def test_decision_record_accepts_exact_same_instant_with_different_offsets() -> None:
    record = _record(
        observed_ts="2026-01-01T01:00:00+01:00",
        recorded_at="2026-01-01T00:00:00Z",
    )
    assert record.observed_ts == "2026-01-01T01:00:00+01:00"
    assert record.recorded_at == "2026-01-01T00:00:00Z"


def test_restart_rejects_hash_valid_but_noncausal_decision_chronology() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "decisions.jsonl"
        ledger = JsonlDecisionLedger(path)
        ledger.append(_record())

        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["record"]["observed_ts"] = "2026-01-01T00:00:02+00:00"
        canonical = json.dumps(
            envelope["record"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        envelope["sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        path.write_text(
            json.dumps(
                envelope,
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
        )

        with pytest.raises(
            DecisionLedgerIntegrityError,
            match="observed_ts is after recorded_at",
        ):
            JsonlDecisionLedger(path).verify_integrity()


def test_restart_rejects_hash_valid_naive_decision_timestamp() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "decisions.jsonl"
        ledger = JsonlDecisionLedger(path)
        ledger.append(_record())

        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["record"]["recorded_at"] = "2026-01-01T00:00:01"
        canonical = json.dumps(
            envelope["record"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        envelope["sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        path.write_text(
            json.dumps(
                envelope,
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
        )

        with pytest.raises(
            DecisionLedgerIntegrityError,
            match="chronology is invalid",
        ):
            JsonlDecisionLedger(path).verify_integrity()

@pytest.mark.parametrize("field", ("observed_ts", "recorded_at"))
def test_decision_record_rejects_nonzero_submicrosecond_precision(field: str) -> None:
    kwargs = {"observed_ts": OBSERVED, "recorded_at": RECORDED}
    kwargs[field] = "2026-01-01T00:00:00.0000001+00:00"
    with pytest.raises(ValueError, match="precision finer than microseconds"):
        _record(**kwargs)


def test_submicrosecond_future_observation_cannot_round_back_to_recorded_at() -> None:
    with pytest.raises(ValueError, match="precision finer than microseconds"):
        _record(
            observed_ts="2026-01-01T00:00:00.0000001+00:00",
            recorded_at="2026-01-01T00:00:00.0000000+00:00",
        )

