import hashlib
import io
import json
from pathlib import Path
import warnings
import zipfile

import pytest

from forward_portfolio_journal import _excluded_sha, freeze_action, freeze_proposal
from overlap_capture_scope import derive as second_scope
from stopped_overlap_scope import derive
from test_forward_portfolio_journal import HEAD, capture, clock, setup
from test_overlap_capture_scope import first
from test_quote_delay_audit import match, stamp


def bundle(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    scope_a = first(a)
    scope_b = second_scope(a, HEAD, scope_a["id_set_sha256"], before=stamp(6))
    b.mkdir()
    (b / "prior-scope.json").write_text(json.dumps(scope_b), encoding="utf-8")
    sb, jb = setup(b)
    capture(sb, 1, "odds", 10, [match(x, 10) for x in "cde"])
    capture(sb, 1, "results", 12, [])
    freeze_proposal(sb, jb, 1, set(scope_b["ids"]), HEAD, clock=clock(14, 15))
    sa, ja = a / "session", a / "journal"
    capture(sa, 2, "odds", 40, [match(x, 40) for x in "abc"])
    freeze_action(sa, ja, 1, {"old"}, HEAD, clock=clock(42, 43))
    capture(sa, 2, "results", 44, [{"fixture_id": "z"}])
    capture(sb, 2, "odds", 50, [match(x, 50) for x in "cde"])
    freeze_action(sb, jb, 1, set(scope_b["ids"]), HEAD, clock=clock(52, 53))
    files = {}
    for label, directory in (("a", a), ("b", b)):
        for p in directory.rglob("*"):
            if p.is_file():
                files["run/" + label + "/" + p.relative_to(directory).as_posix()] = p.read_bytes()
    return files, scope_a["id_set_sha256"], scope_b["id_set_sha256"]


def make_zip(files, *, unlisted=False, tamper=None, duplicate=False):
    entries = {name: {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
               for name, raw in files.items()}
    out = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for name, raw in sorted(files.items()):
                z.writestr(name, raw + b" " if name == tamper else raw)
            z.writestr("manifest.json", json.dumps({"schema": "research.tt_stopped_overlap_bundle.v1",
                                                    "entries": entries}))
            if unlisted:
                z.writestr("extra", b"x")
            if duplicate:
                z.writestr("manifest.json", "{}")
    return out.getvalue()


def inspect(raw, first_sha, second_sha, *, before=stamp(100), head=HEAD):
    return derive(raw, expected_archive_sha256=hashlib.sha256(raw).hexdigest(),
                  history_head=head, first_scope_sha256=first_sha,
                  second_scope_sha256=second_sha, before=before)


def test_all_seven_verified_captures_extend_private_exact_id_scope(tmp_path):
    files, a, b = bundle(tmp_path)
    result = inspect(make_zip(files), a, b)
    assert set(result["ids"]) == {"old", "a", "b", "c", "d", "e", "z"}
    assert result["union_count"] == 7 and result["capture_counts"] == {"odds": 4, "results": 3}
    assert result["id_set_sha256"] == _excluded_sha(set(result["ids"]))
    assert result["prior_as_of"] == stamp(53)
    assert result["second_full_action_verification"] is False
    assert result["complete_prior_universe"] is result["player_identity_verified"] is False


def test_wrong_archive_hash_or_damaged_member_is_rejected(tmp_path):
    files, a, b = bundle(tmp_path)
    raw = make_zip(files)
    with pytest.raises(ValueError, match="archive SHA-256"):
        derive(raw, expected_archive_sha256="0"*64, history_head=HEAD,
               first_scope_sha256=a, second_scope_sha256=b, before=stamp(100))
    with pytest.raises(ValueError, match="member hash"):
        inspect(make_zip(files, tamper="run/a/session/0001.odds.body.json"), a, b)


def test_unlisted_or_duplicate_archive_entries_fail_closed(tmp_path):
    files, a, b = bundle(tmp_path)
    with pytest.raises(ValueError, match="unlisted"):
        inspect(make_zip(files, unlisted=True), a, b)
    with pytest.raises(ValueError, match="duplicate or missing"):
        inspect(make_zip(files, duplicate=True), a, b)


def test_scope_hash_and_time_must_be_externally_pinned(tmp_path):
    files, a, b = bundle(tmp_path)
    raw = make_zip(files)
    with pytest.raises(ValueError, match="scope differs"):
        inspect(raw, "f"*64, b)
    with pytest.raises(ValueError, match="scope differs"):
        inspect(raw, a, b, head="f"*64)
    with pytest.raises(ValueError, match="strictly before"):
        inspect(raw, a, b, before=stamp(53))


def test_bad_second_scope_is_not_silently_reused(tmp_path):
    files, a, b = bundle(tmp_path)
    scope = json.loads(files["run/b/prior-scope.json"])
    scope["ids"].append("not-bound")
    files["run/b/prior-scope.json"] = json.dumps(scope).encode()
    with pytest.raises(ValueError, match="scope differs"):
        inspect(make_zip(files), a, b)
