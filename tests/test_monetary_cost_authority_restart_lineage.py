from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json

import pytest

from autosport.monetary_cost_authority import (
    MonetaryAuthorityError,
    MonetaryCostAuthority,
    MonetaryEvidenceQuality,
    MonetarySourceClass,
    MonetarySourceSnapshot,
)


T0 = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=1)
T2 = T0 + timedelta(minutes=2)


class Resolver:
    def __init__(self, values):
        self.values = values

    def resolve(self, locator: str, *, as_of: datetime):
        return self.values.get(locator)


def _snapshot(*, evidence_id: str, amount: str, digest_char: str, supersedes=None, available_at=T1):
    return MonetarySourceSnapshot(
        authority_id="provider-price-cache",
        evidence_id=evidence_id,
        content_sha256=digest_char * 64,
        amount=Decimal(amount),
        currency="EUR",
        campaign_ids=("campaign-a",),
        coverage_start=T0 - timedelta(hours=1),
        coverage_end=T0,
        observed_at=T0,
        available_at=available_at,
        provenance="resolver:non-authoritative-observation",
        quality=MonetaryEvidenceQuality.ESTIMATE,
        supersedes_evidence_id=supersedes,
    )


def _rehash(record: dict[str, object]) -> None:
    payload = {
        "schema_version": record["schema_version"],
        "source_class": record["source_class"],
        "snapshot": record["snapshot"],
    }
    record["sha256"] = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def test_restart_rejects_rehashed_forged_branch_before_either_tip_can_resolve(tmp_path) -> None:
    first = _snapshot(evidence_id="price-1", amount="10", digest_char="a")
    correction = _snapshot(
        evidence_id="price-2",
        amount="7.5",
        digest_char="b",
        supersedes="price-1",
        available_at=T2,
    )
    resolver = Resolver({"first": first, "correction": correction})
    authority = MonetaryCostAuthority(
        tmp_path,
        source_resolvers={MonetarySourceClass.PROVIDER_BILLING: resolver},
    )
    authority.capture_source(
        MonetarySourceClass.PROVIDER_BILLING,
        "first",
        as_of=T1,
    )
    authority.capture_source(
        MonetarySourceClass.PROVIDER_BILLING,
        "correction",
        as_of=T2,
    )

    path = tmp_path / "monetary-cost-authority.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    correction_record = next(
        record
        for record in raw["sources"]
        if record["snapshot"]["supersedes_evidence_id"] == "price-1"
    )
    forged_tip = deepcopy(correction_record)
    forged_tip["snapshot"]["evidence_id"] = "price-3"
    forged_tip["snapshot"]["content_sha256"] = "c" * 64
    forged_tip["snapshot"]["amount"] = "6.25"
    _rehash(forged_tip)
    raw["sources"].append(forged_tip)
    path.write_text(
        json.dumps(
            raw,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(MonetaryAuthorityError, match="correction lineage cannot branch"):
        MonetaryCostAuthority(
            tmp_path,
            source_resolvers={MonetarySourceClass.PROVIDER_BILLING: resolver},
        )
