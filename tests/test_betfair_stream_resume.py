from hashlib import sha256
import json

import pytest

from autosport.betfair_stream_resume import (
    BetfairStreamResumeError,
    BetfairStreamResumeStore,
    BetfairStreamResumeWitness,
)


HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64


def _witness(**overrides) -> BetfairStreamResumeWitness:
    values = {
        "provider_id": "betfair-exchange",
        "account_id": "acct-evidence-1",
        "environment": "PRODUCTION",
        "stream_endpoint": "stream-api-integration.betfair.com:443",
        "subscription_sha256": HASH_A,
        "pre_disconnect_initial_clk": "opaque-initial-1",
        "pre_disconnect_clk": "opaque-clk-z",
        "pre_disconnect_pt_ms": 1_797_000_000_000,
        "reconnect_id": "reconnect-1",
        "reconnect_started_at": "2026-09-21T10:00:00+00:00",
        "resent_subscription_sha256": HASH_A,
        "first_change_type": "RESUB_DELTA",
        "resumed_clk": "opaque-clk-a",
        "resumed_pt_ms": 1_797_000_000_001,
        "resub_delta_payload_sha256": HASH_B,
        "cache_before_sha256": HASH_C,
        "cache_after_sha256": HASH_D,
        "image_replacement_seen": True,
        "conflated_seen": True,
        "received_at": "2026-09-21T10:00:01+00:00",
    }
    values.update(overrides)
    return BetfairStreamResumeWitness(**values)


def test_valid_resume_witness_is_durable_and_scope_addressable(tmp_path) -> None:
    path = tmp_path / "betfair-resume.json"
    store = BetfairStreamResumeStore(path)
    witness = _witness()

    assert store.append(witness) == witness
    reopened = BetfairStreamResumeStore(path)
    assert reopened.witnesses() == (witness,)
    assert (
        reopened.latest_for_scope(
            provider_id=witness.provider_id,
            account_id=witness.account_id,
            environment=witness.environment,
            stream_endpoint=witness.stream_endpoint,
            subscription_sha256=witness.subscription_sha256,
        )
        == witness
    )
    assert witness.grants_product_live_authority is False
    assert witness.raw_tick_completeness_proven is False


def test_resume_cursor_is_opaque_not_numeric_or_lexicographic() -> None:
    witness = _witness(
        pre_disconnect_clk="opaque-z",
        resumed_clk="opaque-a",
    )
    assert witness.resumed_clk == "opaque-a"


def test_subscription_drift_fails_closed() -> None:
    with pytest.raises(
        BetfairStreamResumeError,
        match="exact durable subscription",
    ):
        _witness(resent_subscription_sha256=HASH_B)


def test_first_reconnect_acceptance_must_be_resub_delta() -> None:
    with pytest.raises(
        BetfairStreamResumeError,
        match="must be RESUB_DELTA",
    ):
        _witness(first_change_type="SUB_IMAGE")


def test_provider_publish_time_must_not_regress() -> None:
    with pytest.raises(
        BetfairStreamResumeError,
        match="publish time regresses",
    ):
        _witness(resumed_pt_ms=1_796_999_999_999)


def test_local_receipt_must_not_precede_reconnect_start() -> None:
    with pytest.raises(
        BetfairStreamResumeError,
        match="cannot precede",
    ):
        _witness(received_at="2026-09-21T09:59:59+00:00")


def test_reconnect_id_is_idempotent_only_for_exact_same_witness(tmp_path) -> None:
    store = BetfairStreamResumeStore(tmp_path / "betfair-resume.json")
    one = _witness()
    assert store.append(one) == one
    assert store.append(one) == one
    assert store.witnesses() == (one,)

    conflicting = _witness(
        resumed_pt_ms=one.resumed_pt_ms + 1,
        cache_after_sha256=HASH_A,
    )
    with pytest.raises(
        BetfairStreamResumeError,
        match="conflicting durable evidence",
    ):
        store.append(conflicting)


def test_persisted_payload_tamper_is_detected(tmp_path) -> None:
    path = tmp_path / "betfair-resume.json"
    store = BetfairStreamResumeStore(path)
    store.append(_witness())

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["records"][0]["witness"]["cache_after_sha256"] = HASH_A
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        BetfairStreamResumeError,
        match="witness_id does not match",
    ):
        BetfairStreamResumeStore(path).witnesses()


def test_broken_record_chain_is_detected(tmp_path) -> None:
    path = tmp_path / "betfair-resume.json"
    store = BetfairStreamResumeStore(path)
    store.append(_witness())
    store.append(
        _witness(
            reconnect_id="reconnect-2",
            reconnect_started_at="2026-09-21T10:01:00+00:00",
            received_at="2026-09-21T10:01:01+00:00",
            pre_disconnect_pt_ms=1_797_000_000_001,
            resumed_pt_ms=1_797_000_000_002,
            pre_disconnect_clk="opaque-clk-a",
            resumed_clk="opaque-clk-b",
            cache_before_sha256=HASH_D,
            cache_after_sha256=HASH_A,
        )
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["records"][1]["previous_record_sha256"] = HASH_A
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        BetfairStreamResumeError,
        match="record chain is broken",
    ):
        BetfairStreamResumeStore(path).witnesses()


def test_duplicate_persisted_json_keys_are_rejected(tmp_path) -> None:
    path = tmp_path / "betfair-resume.json"
    path.write_text(
        '{"schema":"autosport.betfair_stream_resume",'
        '"schema":"autosport.betfair_stream_resume",'
        '"schema_version":1,"records":[]}\n',
        encoding="utf-8",
    )

    with pytest.raises(
        BetfairStreamResumeError,
        match="strict JSON",
    ):
        BetfairStreamResumeStore(path).witnesses()


def test_noncanonical_duplicate_reconnect_record_is_rejected(tmp_path) -> None:
    path = tmp_path / "betfair-resume.json"
    store = BetfairStreamResumeStore(path)
    store.append(_witness())
    payload = json.loads(path.read_text(encoding="utf-8"))
    duplicate = dict(payload["records"][0])
    duplicate["previous_record_sha256"] = payload["records"][0]["record_sha256"]

    base = {
        "previous_record_sha256": duplicate["previous_record_sha256"],
        "witness": duplicate["witness"],
        "witness_id": duplicate["witness_id"],
    }
    duplicate["record_sha256"] = sha256(
        json.dumps(
            base,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    payload["records"].append(duplicate)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        BetfairStreamResumeError,
        match="duplicate reconnect_id",
    ):
        BetfairStreamResumeStore(path).witnesses()


def test_raw_tick_completeness_cannot_be_inferred_even_without_conflation() -> None:
    witness = _witness(
        image_replacement_seen=False,
        conflated_seen=False,
    )
    assert witness.raw_tick_completeness_proven is False
    with pytest.raises(
        BetfairStreamResumeError,
        match="does not prove preservation",
    ):
        witness.require_raw_tick_completeness()


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("provider_id", " betfair", "canonical string"),
        ("subscription_sha256", "A" * 64, "lowercase SHA-256"),
        ("pre_disconnect_pt_ms", True, "non-negative integer"),
        ("image_replacement_seen", 1, "must be boolean"),
        ("reconnect_started_at", "2026-09-21T10:00:00", "timezone"),
    ],
)
def test_witness_rejects_noncanonical_domains(field, value, match) -> None:
    with pytest.raises(BetfairStreamResumeError, match=match):
        _witness(**{field: value})
