import hashlib
import importlib.util
import io
from pathlib import Path
import zipfile

import pytest

from test_prospective_shadow_observer import cycle

spec = importlib.util.spec_from_file_location("restore_shadow", Path(__file__).parents[1] / "scripts/restore_prospective_shadow.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def archive(root, extra=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as z:
        for path in root.rglob("*"):
            if path.is_file():
                z.write(path, path.relative_to(root))
        if extra:
            z.writestr(*extra)
    return stream.getvalue()


def test_restored_result_cannot_train_twice(tmp_path):
    root = tmp_path / "original"
    cycle(root)
    cycle(root, 4, "1", "1")
    raw = archive(root)
    destination = tmp_path / "restored"
    module.restore(raw, destination, expected_sha256=hashlib.sha256(raw).hexdigest())
    assert cycle(destination, 5, "1", "1")["results_recorded"] == 1
    assert module.ShadowStrategyMemory.load(destination / "memory.json").payload() == module.ShadowStrategyMemory.load(root / "memory.json").payload()
    with pytest.raises(ValueError, match="overwrite"):
        module.restore(raw, destination, expected_sha256=hashlib.sha256(raw).hexdigest())


def test_wrong_digest_and_archive_escape_leave_no_campaign(tmp_path):
    original = tmp_path / "original"
    cycle(original)
    raw = archive(original, ("../outside", "bad"))
    destination = tmp_path / "restored"
    with pytest.raises(ValueError, match="digest"):
        module.restore(raw, destination, expected_sha256="0" * 64)
    with pytest.raises(ValueError, match="unsafe"):
        module.restore(raw, destination, expected_sha256=hashlib.sha256(raw).hexdigest())
    assert not destination.exists()
    assert not (tmp_path / "outside").exists()


def test_missing_required_source_refuses_restore(tmp_path):
    original = tmp_path / "original"
    cycle(original)
    for path in (original / "raw").glob("*.json"):
        path.unlink()
    raw = archive(original)
    with pytest.raises(ValueError, match="source evidence missing"):
        module.restore(raw, tmp_path / "restored", expected_sha256=hashlib.sha256(raw).hexdigest())


def test_latest_failed_checkpoint_is_preserved_instead_of_old_success(tmp_path, monkeypatch):
    import sys
    original = tmp_path / "original"
    cycle(original)
    cycle(original, 4, "2", "1")
    raw = archive(original)
    def api(path, **kwargs):
        if "/workflows/" in path:
            assert "status=completed" in path
            return {"workflow_runs": [
                {"id": n, "run_number": n, "run_attempt": 1, "head_sha": "a"*40,
                 "head_branch": "work/agent-shadow-learning-20261027", "event": "push",
                 "conclusion": "failure" if n == 2 else "success"} for n in (1, 2)]}
        if "/jobs?" in path:
            return {"jobs": [{"name": "public-shadow-learning", "conclusion": "failure"}]}
        if "/runs/" in path:
            assert "/runs/2/" in path
            return {"artifacts": [{"name": "prospective-shadow-2-attempt-1", "expired": False,
                    "id": 3, "size_in_bytes": len(raw), "digest": "sha256:" + hashlib.sha256(raw).hexdigest()}]}
        return raw
    monkeypatch.setattr(module, "api", api)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setattr(sys, "argv", ["restore", "--workspace", str(tmp_path / "resumed")])
    module.main()
    assert cycle(tmp_path / "resumed", 5, "2", "1")["results_recorded"] == 1


def test_segment_selection_preserves_latest_partial_and_explicit_chain():
    def artifact(name, expired=False):
        return {"name": name, "expired": expired}
    base = "prospective-shadow-42-attempt-3"
    entries = [artifact(base+"-segment-1"), artifact(base+"-segment-2"),
               artifact(base+"-segment-3", expired=True), artifact("prospective-shadow-41-attempt-3")]
    with pytest.raises(ValueError, match="expired"):
        module.select_checkpoint(entries, run_id=42, attempt=3)
    entries.pop(2)
    assert module.select_checkpoint(entries, run_id=42, attempt=3)["name"] == base+"-segment-2"
    assert module.select_checkpoint(entries, run_id=42, attempt=3, segment=1)["name"] == base+"-segment-1"
    with pytest.raises(ValueError, match="unavailable"):
        module.select_checkpoint(entries, run_id=42, attempt=3, segment=3)
    entries.append(artifact(base))
    assert module.select_checkpoint(entries, run_id=42, attempt=3)["name"] == base
    with pytest.raises(ValueError, match="ambiguous"):
        module.select_checkpoint(entries+[artifact(base)], run_id=42, attempt=3)


def test_next_segment_restores_its_actual_predecessor(tmp_path, monkeypatch):
    import sys
    original = tmp_path / "original"
    cycle(original)
    cycle(original, 4, "2", "1")
    raw = archive(original)
    sha = hashlib.sha256(raw).hexdigest()
    def api(path, **kwargs):
        if path.endswith("/runs/42"):
            return {"id": 42, "run_attempt": 1, "head_sha": "a" * 40,
                    "head_branch": "work/agent-shadow-learning-20261027", "event": "push"}
        if path.endswith("/runs/42/artifacts?per_page=100"):
            return {"artifacts": [{"name": "prospective-shadow-42-attempt-1-segment-1",
                    "expired": False, "id": 100, "size_in_bytes": len(raw), "digest": "sha256:" + sha}]}
        assert path.endswith("/artifacts/100/zip") and kwargs["binary"]
        return raw
    monkeypatch.setattr(module, "api", api)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setattr(sys, "argv", ["restore", "--workspace", str(tmp_path / "resumed"), "--previous-segment", "1"])
    module.main()
    assert cycle(tmp_path / "resumed", 5, "2", "1")["results_recorded"] == 1


def test_failed_started_segment_cannot_silently_restore_earlier_segment():
    jobs = [{"name": "segment-1", "conclusion": "success"},
            {"name": "segment-2", "conclusion": "failure"},
            {"name": "segment-3", "conclusion": "skipped"}]
    assert module.latest_started_segment(jobs) == 2
    with pytest.raises(ValueError, match="unavailable"):
        module.select_checkpoint([{"name": "prospective-shadow-42-attempt-1-segment-1", "expired": False}],
                                 run_id=42, attempt=1, segment=module.latest_started_segment(jobs))
