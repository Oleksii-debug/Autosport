import hashlib
import io
import json
import zipfile

import pytest

from forward_portfolio_journal import _excluded_sha, freeze_action, freeze_proposal
from stopped_single_scope import derive
from test_forward_portfolio_journal import HEAD, capture, clock, setup
from test_quote_delay_audit import match, stamp


def bundle(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    ids = {"old"}
    scope_sha = _excluded_sha(ids)
    (root / "prior-scope.json").write_text(json.dumps({
        "schema": "research.tt_forward_prior_scope.v1", "history_head": HEAD,
        "prior_as_of": stamp(0), "ids": sorted(ids), "id_set_sha256": scope_sha,
    }), encoding="utf-8")
    session, journal = setup(root)
    capture(session, 1, "odds", 10, [match(x, 10) for x in "ab"])
    capture(session, 1, "results", 12, [{"fixture_id": "old"}])
    freeze_proposal(session, journal, 1, ids, HEAD, clock=clock(14, 15))
    capture(session, 2, "odds", 50, [match(x, 50) for x in "abc"])
    freeze_action(session, journal, 1, ids, HEAD, clock=clock(52, 53))
    capture(session, 2, "results", 54, [{"fixture_id": "z"}])
    files = {"run/" + p.relative_to(root).as_posix(): p.read_bytes()
             for p in root.rglob("*") if p.is_file()}
    return files, scope_sha


def archive(files):
    entries = {name: {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
               for name, raw in files.items()}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, raw in sorted(files.items()):
            z.writestr(name, raw)
        z.writestr("manifest.json", json.dumps({"schema": "research.tt_stopped_single_bundle.v1",
                                                "entries": entries}))
    return out.getvalue()


def inspect(raw, scope_sha, *, before=stamp(100), head=HEAD):
    return derive(raw, expected_archive_sha256=hashlib.sha256(raw).hexdigest(),
                  history_head=head, prior_scope_sha256=scope_sha, before=before)


def test_partial_run_extends_scope_using_all_four_verified_bodies(tmp_path):
    files, scope_sha = bundle(tmp_path)
    result = inspect(archive(files), scope_sha)
    assert set(result["ids"]) == {"old", "a", "b", "c", "z"}
    assert result["id_set_sha256"] == _excluded_sha(set(result["ids"]))
    assert result["capture_counts"] == {"odds": 2, "results": 2}
    assert result["prior_as_of"] == stamp(55)
    assert result["first_action_verified"] is True
    assert result["second_proposal_frozen"] is False
    assert result["complete_prior_universe"] is result["player_identity_verified"] is False


def test_archive_and_scope_must_match_external_pins(tmp_path):
    files, scope_sha = bundle(tmp_path)
    raw = archive(files)
    with pytest.raises(ValueError, match="archive SHA-256"):
        derive(raw, expected_archive_sha256="0" * 64, history_head=HEAD,
               prior_scope_sha256=scope_sha, before=stamp(100))
    with pytest.raises(ValueError, match="scope differs"):
        inspect(raw, "f" * 64)
    with pytest.raises(ValueError, match="scope differs"):
        inspect(raw, scope_sha, head="f" * 64)
    with pytest.raises(ValueError, match="strictly before"):
        inspect(raw, scope_sha, before=stamp(55))


def test_missing_result_or_extra_proposal_cannot_be_reused(tmp_path):
    files, scope_sha = bundle(tmp_path)
    incomplete = {k: v for k, v in files.items() if not k.startswith("run/session/0002.results.")}
    with pytest.raises(ValueError, match="unexpected stopped single-session captures"):
        inspect(archive(incomplete), scope_sha)
    files["run/journal/proposal-0002.body.json"] = b"{}"
    with pytest.raises(ValueError, match="unexpected stopped single-session journal entries"):
        inspect(archive(files), scope_sha)


def test_prior_scope_must_precede_first_request(tmp_path):
    files, scope_sha = bundle(tmp_path)
    old_scope = json.loads(files["run/prior-scope.json"])
    old_scope["prior_as_of"] = stamp(10)
    files["run/prior-scope.json"] = json.dumps(old_scope).encode()
    with pytest.raises(ValueError, match="prior scope does not precede"):
        inspect(archive(files), scope_sha)


def test_modified_action_or_raw_body_is_not_silently_accepted(tmp_path):
    files, scope_sha = bundle(tmp_path)
    files["run/journal/action-0001.body.json"] += b" "
    with pytest.raises(ValueError, match="journal receipt mismatch"):
        inspect(archive(files), scope_sha)
