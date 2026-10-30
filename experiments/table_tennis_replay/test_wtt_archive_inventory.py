import csv
import hashlib
import io
import json
import warnings
import zipfile

import pytest

from wtt_archive_inventory import FILES, audit


COMMON = ["sport", "gender", "dataset_id", "match_id", "event_date", "player1_name",
          "player2_name", "winner", "score"]


def csv_bytes(rows, fields):
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=fields, lineterminator="\n")
    writer.writeheader(); writer.writerows(rows)
    return out.getvalue().encode()


def row(ident, day, a="1", b="2"):
    return {"sport": "table_tennis", "gender": "men", "dataset_id": "demo", "match_id": ident,
            "event_date": day, "player1_name": "A", "player2_name": "B", "winner": "1", "score": "3-1",
            "a_id_raw": a, "x_id_raw": b}


def archive(men, women, *, duplicate=False, extra=None):
    out = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(out, "w") as z:
            z.writestr(FILES[0], men); z.writestr(FILES[1], women)
            if duplicate: z.writestr(FILES[0], men)
            if extra is not None: z.writestr("unrelated.bin", extra)
    return out.getvalue()


def inspect(raw):
    return audit(raw, expected_sha256=hashlib.sha256(raw).hexdigest())


def samples():
    men = [row("x", "2025-01-01"), row("y", "2025-01-01", "1", "3"), row("z", "2025-01-02", "2", "3")]
    female = [{k: v for k, v in row("w", "2025-01-01").items() if k in COMMON}]
    return men, female


def test_source_local_date_groups_and_missing_womens_ids_are_explicit():
    men, female = samples(); raw = archive(csv_bytes(men, COMMON+["a_id_raw", "x_id_raw"]), csv_bytes(female, COMMON), extra=b"unread")
    report = inspect(raw); a, b = report["tables"]
    assert a["rows"] == 3 and a["both_raw_ids_ascii_digits"] == 3
    assert a["repeated_native_id_event_date_groups"] == 1
    assert a["rows_with_repeated_id_on_event_date"] == 2
    assert b["native_id_fields_present"] is False and b["both_raw_ids_ascii_digits"] is None
    assert b["raw_ids_with_multiple_exact_name_strings"] is None
    assert report["archive_members"] == 3 and report["other_members_decompressed"] is False
    assert report["training_authorized"] is report["cross_provider_identity_verified"] is False


def test_duplicate_match_keys_and_invalid_ids_are_counted_not_merged():
    men, female = samples(); men[1]["match_id"] = "x"; men[1]["a_id_raw"] = "1.0"
    report = inspect(archive(csv_bytes(men, COMMON+["a_id_raw", "x_id_raw"]), csv_bytes(female, COMMON)))
    a = report["tables"][0]
    assert a["duplicated_match_id_values"] == 1 and a["rows_with_duplicated_match_id"] == 2
    assert a["both_raw_ids_present"] == 3 and a["both_raw_ids_ascii_digits"] == 2


def test_cross_file_match_id_overlap_is_counted_without_merging():
    men, female = samples(); female[0]["match_id"] = "x"
    result = inspect(archive(csv_bytes(men, COMMON+["a_id_raw", "x_id_raw"]), csv_bytes(female, COMMON)))
    assert result["cross_file_exact_match_id_overlap"] == 1
    assert result["tables"][0]["duplicated_match_id_values"] == 0
    assert result["tables"][1]["duplicated_match_id_values"] == 0


def test_id_name_conflicts_are_reported_without_merging():
    men, female = samples()
    men[1]["player1_name"] = "Different spelling"
    men[2]["player1_name"] = "A"
    a = inspect(archive(csv_bytes(men, COMMON+["a_id_raw", "x_id_raw"]), csv_bytes(female, COMMON)))["tables"][0]
    assert a["raw_ids_with_multiple_exact_name_strings"] == 2
    assert a["exact_name_strings_with_multiple_raw_ids"] == 2


def test_best_of_score_integrity_and_missing_labels_are_explicit():
    men, female = samples()
    men[0].update(best_of="7", score="11-3,11-3,11-3", sport="")
    men[1].update(best_of="7", score="11-3,11-3,11-3,11-3")
    men[2].update(best_of="5", score="3-11,3-11,3-11")
    a = inspect(archive(csv_bytes(men, COMMON+["a_id_raw", "x_id_raw", "best_of"]), csv_bytes(female, COMMON)))["tables"][0]
    assert a["sport_label_present"] == 2
    assert a["best_of_values"] == {"5": 1, "7": 2}
    assert a["rank_fields_present"]["player1_rank"] == 0
    assert a["score_shape_counts"] == {"nonterminal_for_declared_best_of": 1,
                                      "terminal_and_winner_consistent": 1,
                                      "winner_conflicts_with_set_scores": 1}


def test_labeled_date_precision_is_strict_and_same_ids_are_excluded_from_groups():
    men, female = samples(); men[1]["event_date"] = "2025-01-01T10:00:00Z"; men[2]["x_id_raw"] = "2"
    a = inspect(archive(csv_bytes(men, COMMON+["a_id_raw", "x_id_raw"]), csv_bytes(female, COMMON)))["tables"][0]
    assert a["valid_date_only"] == 2 and a["invalid_or_missing_event_date"] == 1
    assert a["same_raw_id_both_sides"] == 1 and a["repeated_native_id_event_date_groups"] == 0


def test_wrong_archive_hash_missing_or_duplicate_member_fail_closed():
    men, female = samples(); a=csv_bytes(men, COMMON+["a_id_raw", "x_id_raw"]); b=csv_bytes(female, COMMON)
    raw=archive(a,b)
    with pytest.raises(ValueError, match="SHA-256"):
        audit(raw, expected_sha256="0"*64)
    with pytest.raises(ValueError, match="duplicate WTT archive member"):
        inspect(archive(a,b,duplicate=True))
    out=io.BytesIO()
    with zipfile.ZipFile(out,"w") as z:z.writestr(FILES[0],a)
    with pytest.raises(ValueError, match="missing or duplicate"):
        inspect(out.getvalue())


def test_duplicate_headers_bad_shape_and_bad_zip_fail_closed():
    men, female = samples(); b=csv_bytes(female,COMMON)
    with pytest.raises(ValueError, match="header"):
        inspect(archive(csv_bytes(men,COMMON+["a_id_raw","x_id_raw","score"]),b))
    valid=csv_bytes(men,COMMON+["a_id_raw","x_id_raw"])
    with pytest.raises(ValueError, match="row shape"):
        inspect(archive(valid+b"short,row\n",b))
    with pytest.raises(ValueError, match="corrupt"):
        inspect(b"not a zip")


def test_output_does_not_include_raw_names_or_fixture_ids():
    men, female=samples();r=inspect(archive(csv_bytes(men,COMMON+["a_id_raw","x_id_raw"]),csv_bytes(female,COMMON)))
    output=json.dumps(r)
    assert '"match_id": "x"' not in output and '"player1_name": "A"' not in output
