"""Rebuild exact-ID exclusions from a pinned, stopped PAPER evidence bundle.

No new source request, player identity merge, training, or execution. This
scope covers only recorded provider IDs and does not prove independent matches.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import tempfile
import zipfile

from autosport.json_integrity import strict_json_loads
from forward_portfolio_journal import _excluded_sha, _read_record, _replay, proposal, verify_actions
from forward_prior_scope import _ids
from forward_scoped_capture import load_scope, write_json
from prospective_capture import verify_session
from record import instant

MAX_ARCHIVE = 20 * 1024 * 1024
MAX_MEMBER = 30 * 1024 * 1024
MAX_TOTAL = 120 * 1024 * 1024


def _extract_verified(raw, expected_sha256, directory, *,
                      schema="research.tt_stopped_overlap_bundle.v1"):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_ARCHIVE:
        raise ValueError("empty or oversized stopped archive")
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("stopped archive SHA-256 mismatch")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            names = [i.filename for i in infos]
            if len(names) != len(set(names)) or names.count("manifest.json") != 1:
                raise ValueError("duplicate or missing manifest")
            if sum(i.file_size for i in infos) > MAX_TOTAL:
                raise ValueError("archive expands beyond limit")
            manifest = strict_json_loads(archive.read("manifest.json").decode("utf-8"))
            if (not isinstance(manifest, dict) or manifest.get("schema") != schema
                    or not isinstance(manifest.get("entries"), dict)):
                raise ValueError("invalid stopped archive manifest")
            entries = manifest["entries"]
            if set(names) != set(entries) | {"manifest.json"}:
                raise ValueError("unlisted or missing stopped archive entry")
            for info in infos:
                path = PurePosixPath(info.filename)
                if (info.is_dir() or path.is_absolute() or ".." in path.parts or
                        info.file_size > MAX_MEMBER or info.flag_bits & 1):
                    raise ValueError("unsafe stopped archive entry")
                data = archive.read(info)
                if info.filename != "manifest.json":
                    declared = entries[info.filename]
                    if (not isinstance(declared, dict) or declared.get("bytes") != len(data) or
                            declared.get("sha256") != hashlib.sha256(data).hexdigest()):
                        raise ValueError("stopped archive member hash mismatch")
                target = Path(directory).joinpath(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(data)
            return len(entries)
    except (zipfile.BadZipFile, UnicodeError, OSError) as exc:
        raise ValueError("invalid stopped archive") from exc


def derive(raw, *, expected_archive_sha256, history_head, first_scope_sha256,
           second_scope_sha256, before):
    """Validate recorded partial sessions and union all their exact source IDs."""
    cutoff = instant(before)
    with tempfile.TemporaryDirectory(prefix="tt-stopped-scope-") as temp:
        entries = _extract_verified(raw, expected_archive_sha256, temp)
        first, second = Path(temp) / "run/a", Path(temp) / "run/b"
        scope_a, ids_a = load_scope(first, history_head, first_scope_sha256)
        scope_b, ids_b = load_scope(second, history_head, second_scope_sha256)
        va, vb = verify_session(first / "session"), verify_session(second / "session")
        if set(va) != {(1, "odds"), (1, "results"), (2, "odds"), (2, "results")}:
            raise ValueError("unexpected first stopped session")
        if set(vb) != {(1, "odds"), (1, "results"), (2, "odds")}:
            raise ValueError("unexpected second stopped session")
        first_proposal, penv = _read_record(first / "journal", "proposal", 1)
        if first_proposal != proposal(first / "session", 1, ids_a, history_head):
            raise ValueError("first stopped proposal differs")
        selected = [item["fixture_id"] for item in first_proposal["selected"]]
        if (len(selected) != len(set(selected)) or ids_b != ids_a | set(selected) or
                instant(penv["frozen_at"]) >= instant(vb[(1, "odds")]["requested_at"])):
            raise ValueError("second scope is not causally bound to first proposal")
        verify_actions(first / "session", first / "journal", ids_a, history_head, through=1)
        # B results2 was deliberately never requested: validate only the
        # independently reproducible action and its source-relative timing.
        action_b = _replay(second / "session", second / "journal", ids_b, history_head, 1)
        recorded_b, benv = _read_record(second / "journal", "action", 1)
        quote_time = instant(vb[(2, "odds")]["received_at"])
        if (action_b != recorded_b or instant(benv["computed_at"]) < quote_time or
                (instant(benv["computed_at"]) - quote_time).total_seconds() > 30):
            raise ValueError("second stopped action cannot be replayed")
        ids, times = set(ids_a) | set(ids_b), []
        counts = {"odds": 0, "results": 0}
        for root, verified in ((first, va), (second, vb)):
            for (seq, kind), receipt in verified.items():
                body = (root / "session" / f"{seq:04d}.{kind}.body.json").read_bytes()
                if hashlib.sha256(body).hexdigest() != receipt["sha256"]:
                    raise ValueError("stopped source changed after verification")
                ids.update(_ids(body, "matches" if kind == "odds" else "results",
                                "id" if kind == "odds" else "fixture_id"))
                times.append(instant(receipt["received_at"]))
                counts[kind] += 1
        times.extend((instant(penv["frozen_at"]), instant(benv["frozen_at"]),
                      instant(scope_a["prior_as_of"]), instant(scope_b["prior_as_of"])))
        prior_as_of = max(times)
        if prior_as_of >= cutoff:
            raise ValueError("stopped evidence is not strictly before new scope")
        return {"schema": "research.tt_forward_prior_scope.v1", "history_head": history_head,
                "prior_as_of": prior_as_of.isoformat(), "id_set_sha256": _excluded_sha(ids),
                "ids": sorted(ids), "union_count": len(ids), "capture_counts": counts,
                "base_scope_count": len(ids_a), "archive_sha256": expected_archive_sha256,
                "archive_manifest_entries": entries, "second_full_action_verification": False,
                "exact_provider_ids_only": True, "complete_prior_universe": False,
                "player_identity_verified": False, "independent_holdout": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("root", type=Path)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--history-head", required=True)
    parser.add_argument("--first-scope-sha256", required=True)
    parser.add_argument("--second-scope-sha256", required=True)
    args = parser.parse_args()
    scope = derive(args.archive.read_bytes(), expected_archive_sha256=args.archive_sha256,
                   history_head=args.history_head, first_scope_sha256=args.first_scope_sha256,
                   second_scope_sha256=args.second_scope_sha256,
                   before=datetime.now(timezone.utc).isoformat())
    args.root.mkdir(mode=0o700, parents=False, exist_ok=False)
    write_json(args.root / "prior-scope.json", scope)
    print(json.dumps({k: v for k, v in scope.items() if k != "ids"}, sort_keys=True))


if __name__ == "__main__":
    main()
