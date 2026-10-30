"""Check WTT raw IDs against a deliberately empty canonical identity registry.

This establishes only what an empty registry can resolve. It never uses names
as identifiers, modifies a product registry, authenticates an upstream ID or
authorizes training, odds, execution, or an historical as-of claim.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import io
import json
from pathlib import Path
import tempfile
import zipfile

from autosport.participant_identity import (
    EntityKind, IdentityView, ParticipantIdentityError, ParticipantIdentityRegistry,
)
from record import instant
from wtt_archive_inventory import FILES, _native_id, audit as audit_inventory

SOURCE = "osf:pjuw8:wtt-raw-id"


def audit(raw, *, expected_sha256, lookup_as_of):
    """Use a fresh empty existing registry, at the explicitly supplied lookup time."""
    moment = instant(lookup_as_of)
    inventory = audit_inventory(raw, expected_sha256=expected_sha256)
    men, women = inventory["tables"]
    if not men["native_id_fields_present"]:
        raise ValueError("men's native ID fields missing")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        # The inventory has already checked exact member names, sizes and CRC.
        content = archive.read(FILES[0])
    ids = set()
    for row in csv.DictReader(io.StringIO(content.decode("utf-8-sig"), newline="")):
        for field in ("a_id_raw", "x_id_raw"):
            if not _native_id(row[field]):
                raise ValueError("raw participant ID is not an ASCII digit string")
            ids.add(row[field])
    counts = Counter()
    with tempfile.TemporaryDirectory(prefix="autosport-wtt-pristine-") as directory:
        registry = ParticipantIdentityRegistry.initialize_pristine(Path(directory) / "identity.json")
        for value in sorted(ids):
            try:
                alias = registry.resolve_alias_record(SOURCE, value, as_of=moment.isoformat(),
                                                      view=IdentityView.AS_KNOWN_AT_DECISION)
                entity = registry.resolve_alias(SOURCE, value, as_of=moment.isoformat(),
                                                view=IdentityView.AS_KNOWN_AT_DECISION)
            except ParticipantIdentityError:
                counts["unresolved"] += 1
                continue
            if alias.relation != "CONFIRMED" or entity.kind is not EntityKind.PARTICIPANT:
                counts["incompatible"] += 1
            else:
                counts["resolved"] += 1
    return {"schema": "research.wtt_pristine_identity_audit.v1", "archive_sha256": inventory["archive_sha256"],
            "source_namespace": SOURCE, "lookup_as_of": moment.isoformat(),
            "registry_mode": "fresh_empty_test_only", "mens_distinct_raw_ids": len(ids),
            "mens_resolved": counts["resolved"], "mens_unresolved": counts["unresolved"],
            "mens_incompatible": counts["incompatible"],
            "womens_native_id_fields_present": women["native_id_fields_present"],
            "womens_identity_resolution": ("not_attempted_in_this_audit" if women["native_id_fields_present"]
                                           else "not_attempted_no_native_id_fields"),
            "source_truth_verified": False, "historical_decision_times_verified": False,
            "training_authorized": False, "real_money_execution": False,
            "note": "Fresh empty registry only; no conclusion about a separately populated registry."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--lookup-as-of", required=True)
    args = parser.parse_args()
    report = audit(args.archive.read_bytes(), expected_sha256=args.expected_sha256, lookup_as_of=args.lookup_as_of)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, sort_keys=True, indent=2); stream.write("\n")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
