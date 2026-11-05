import csv
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from external_elo_causality_audit import CSV_FIELDS
from topspin_duplicate_source_candidates import COLUMNS, PARQUET_SHA256, READER_COMMIT, audit


def pred(a="1123401", b="1123402", winner="1123401", target="1", category="MT"):
    row = {key: "" for key in CSV_FIELDS}
    row.update(player_a_id=a, player_b_id=b, winner_id=winner, target=target, category=category)
    return row


def csv_bytes(rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


def raw(a="1123401", b="1123402", winner="1123401", category="MT", source_id="4567801",
        match_key="PRIVATE-KEY-A", drop_reason=None, is_doubles=False):
    return {"source_match_id": source_id, "match_key": match_key, "player_a_id": a,
            "player_b_id": b, "winner_id": winner, "category": category,
            "drop_reason": drop_reason, "is_doubles": is_doubles}


def transcode(rows):
    return json.dumps({"schema": "research.topspin_private_transcode.v1", "source_parquet_sha256": PARQUET_SHA256,
                       "reader_commit": READER_COMMIT, "columns": list(COLUMNS), "rows": rows}).encode()


def run(predictions, source_rows):
    c, t = csv_bytes(predictions), transcode(source_rows)
    return audit(c, t, expected_csv_sha256=hashlib.sha256(c).hexdigest(),
                 expected_transcode_sha256=hashlib.sha256(t).hexdigest())


@pytest.mark.parametrize("count,status", [(0, "zero"), (1, "one"), (2, "multiple")])
def test_candidate_cardinality_is_not_a_physical_event_claim(count, status):
    p = pred()
    sources = [raw(source_id=str(4567801 + i), match_key=f"PRIVATE-KEY-{i}") for i in range(count)]
    result = run([p, p.copy()], sources)
    assert result["duplicate_prediction_groups"] == 1
    assert result["groups"][0]["prediction_rows"] == 2
    assert result["groups"][0]["candidate_source_rows"] == count
    assert result["groups"][0]["candidate_multiplicity"] == status
    assert result["prediction_groups_by_candidate_multiplicity"][status] == 1
    assert all(result[key] is False for key in ("individual_source_ids_published", "prediction_row_to_source_row_verified",
        "physical_events_verified", "physical_people_verified", "source_truth_verified", "source_rights_verified",
        "result_availability_verified", "independent_holdout_verified", "real_money_execution"))
    for value in ("1123401", "1123402", "4567801", "PRIVATE-KEY"):
        assert value not in json.dumps(result)


def test_missing_repeated_and_invalid_source_identifiers_are_counted_without_merging():
    p = pred()
    rows = [raw(source_id="4567801", match_key="KEY-A"),
            raw(source_id="4567801", match_key="KEY-A"),
            raw(source_id=None, match_key=None), raw(source_id="invalid", match_key=42)]
    group = run([p, p.copy()], rows)["groups"][0]
    for key in ("source_match_id", "match_key"):
        assert group[key] == {"present_rows": 2, "missing_rows": 1, "invalid_rows": 1,
                              "distinct_values": 1, "repeated_values": 1, "rows_in_repeated_values": 2}


@pytest.mark.parametrize("change", ["reverse", "category", "winner"])
def test_join_is_directional_and_exact(change):
    value = raw()
    if change == "reverse":
        value.update(player_a_id="1123402", player_b_id="1123401")
    elif change == "category":
        value["category"] = "WT"
    else:
        value["winner_id"] = "1123402"
    p = pred()
    assert run([p, p.copy()], [value])["groups"][0]["candidate_multiplicity"] == "zero"


def test_source_eligibility_is_explicit_and_unusable_rows_are_not_joined():
    p = pred()
    source = [raw(), raw(source_id="9", is_doubles=True), raw(source_id="10", drop_reason="retired"),
              raw(source_id="11", winner="99999")]
    result = run([p, p.copy()], source)
    assert result["source_rows"] == 4
    assert result["source_rows_eligible_for_key_matching"] == 1
    assert result["source_rows_excluded"] == {"not_usable_singles": 2, "usable_rows_with_invalid_join_key": 1}
    assert result["groups"][0]["candidate_source_rows"] == 1


def test_nonduplicated_predictions_do_not_create_a_group():
    assert run([pred()], [raw()])["duplicate_prediction_groups"] == 0


def test_hash_envelope_and_boolean_checks_fail_closed():
    c = csv_bytes([pred(), pred()])
    t = transcode([raw()])
    with pytest.raises(ValueError, match="transcode SHA"):
        audit(c, t + b" ", expected_csv_sha256=hashlib.sha256(c).hexdigest(),
              expected_transcode_sha256=hashlib.sha256(t).hexdigest())
    with pytest.raises(ValueError, match="prediction CSV hash"):
        audit(c + b" ", t, expected_csv_sha256=hashlib.sha256(c).hexdigest(),
              expected_transcode_sha256=hashlib.sha256(t).hexdigest())
    wrong = json.loads(t)
    wrong["reader_commit"] = "not-the-reader"
    raw_wrong = json.dumps(wrong).encode()
    with pytest.raises(ValueError, match="envelope"):
        audit(c, raw_wrong, expected_csv_sha256=hashlib.sha256(c).hexdigest(),
              expected_transcode_sha256=hashlib.sha256(raw_wrong).hexdigest())
    with pytest.raises(ValueError, match="source row"):
        run([pred(), pred()], [raw(is_doubles=None)])


def test_pinned_parquet_decoder_when_external_inputs_are_available(tmp_path):
    root = os.environ.get("TT_TOPSPIN_PARQUET_DIR")
    if not root:
        pytest.skip("Set TT_TOPSPIN_PARQUET_DIR to the pinned reader and Parquet")
    root = Path(root)
    script = Path(__file__).with_name("decode_topspin_parquet.mjs")
    out = tmp_path / "private.json"
    run_result = subprocess.run(["node", str(script), str(root / "hyparquet"), str(root / "matches.parquet"), str(out)],
                                capture_output=True, text=True, check=True)
    summary = json.loads(run_result.stdout)
    assert summary == {"rows": 857, "columns": 8, "source_parquet_sha256": PARQUET_SHA256}
    result = json.loads(out.read_text())
    assert len(result["rows"]) == 857 and result["columns"] == list(COLUMNS)
    assert hashlib.sha256(out.read_bytes()).hexdigest() == "d0b4080bf79ab6e225e53846470fd9e5d2d9521ce81953959a10029ebb74bd61"
    assert (out.stat().st_mode & 0o077) == 0
    bad = tmp_path / "bad.parquet"
    bad.write_bytes(b"not-the-source")
    rejected = subprocess.run(["node", str(script), str(root / "hyparquet"), str(bad), str(tmp_path / "bad.json")],
                              capture_output=True, text=True)
    assert rejected.returncode != 0 and "Parquet SHA-256 mismatch" in rejected.stderr
    assert not (tmp_path / "bad.json").exists()
    copy = tmp_path / "reader"
    shutil.copytree(root / "hyparquet", copy)
    (copy / "src" / "node.js").write_text("throw new Error('must not run');\n")
    rejected = subprocess.run(["node", str(script), str(copy), str(root / "matches.parquet"), str(tmp_path / "evil.json")],
                              capture_output=True, text=True)
    assert rejected.returncode != 0 and "reader blob mismatch" in rejected.stderr
    assert not (tmp_path / "evil.json").exists()
