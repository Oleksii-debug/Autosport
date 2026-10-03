from __future__ import annotations

"""Compose forward-universe reads with the canonical campaign precommit authority.

This module owns no persistence and mints no independent campaign authority.  It only
re-resolves the existing #1257 CampaignPrecommitManifest + monotonic publication
witness from an independently supplied product locator, then proves that the freshly
loaded provider evaluation universe is the universe prospectively committed there.
"""

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .campaign_precommit_manifest import (
    CampaignPrecommitManifest,
    CampaignPrecommitManifestError,
    CampaignPrecommitPublicationWitness,
    load_campaign_precommit_manifest,
    resolve_campaign_precommit_publication_witness,
)


_DOMAIN = "autosport.forward-universe-precommit-authority.v1"


class ForwardUniversePrecommitAuthorityError(RuntimeError):
    """Prospective campaign authority does not authorize the loaded universe."""


@dataclass(frozen=True, slots=True)
class ForwardUniversePrecommitLocator:
    """Routing only; positive authority is always re-resolved from #1257.

    The caller must obtain this locator independently from product composition.  It
    must not be derived from the candidate ProviderEvaluationUniverseStore being
    evaluated, because that would let the candidate backing choose its own expected
    identity.
    """

    manifest_path: Path
    workspace: Path
    workspace_instance_id: str | None = None
    authority_root: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.manifest_path, Path):
            raise TypeError("manifest_path must be pathlib.Path")
        if not isinstance(self.workspace, Path):
            raise TypeError("workspace must be pathlib.Path")
        if not self.workspace.is_absolute():
            raise ForwardUniversePrecommitAuthorityError(
                "campaign precommit workspace must be absolute"
            )
        if self.workspace_instance_id is not None and (
            type(self.workspace_instance_id) is not str
            or not self.workspace_instance_id
            or self.workspace_instance_id != self.workspace_instance_id.strip()
        ):
            raise ForwardUniversePrecommitAuthorityError(
                "workspace_instance_id must be non-empty canonical text when supplied"
            )
        if self.authority_root is not None:
            if not isinstance(self.authority_root, Path):
                raise TypeError("authority_root must be pathlib.Path when supplied")
            if not self.authority_root.is_absolute():
                raise ForwardUniversePrecommitAuthorityError(
                    "campaign precommit authority_root must be absolute"
                )

    @property
    def absolute_manifest_path(self) -> Path:
        if self.manifest_path.is_absolute():
            return Path(os.path.abspath(self.manifest_path))
        return Path(os.path.abspath(self.workspace / self.manifest_path))


@dataclass(frozen=True, slots=True)
class ForwardUniversePrecommitResolution:
    campaign_id: str
    source_id: str
    evaluation_universe_sha256: str
    manifest_sha256: str
    witness_authority_record_sha256: str
    witness_semantic_binding_sha256: str
    witness_workspace_instance_id: str
    witness_generation: int
    witness_observed_at: datetime
    observation_not_before: datetime
    observation_not_after: datetime

    @property
    def authority_sha256(self) -> str:
        payload = {
            "campaign_id": self.campaign_id,
            "domain": _DOMAIN,
            "evaluation_universe_sha256": self.evaluation_universe_sha256,
            "manifest_sha256": self.manifest_sha256,
            "observation_not_after": self.observation_not_after.isoformat(),
            "observation_not_before": self.observation_not_before.isoformat(),
            "source_id": self.source_id,
            "witness_authority_record_sha256": self.witness_authority_record_sha256,
            "witness_generation": self.witness_generation,
            "witness_observed_at": self.witness_observed_at.isoformat(),
            "witness_semantic_binding_sha256": self.witness_semantic_binding_sha256,
            "witness_workspace_instance_id": self.witness_workspace_instance_id,
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def _instant(value: str, name: str) -> datetime:
    if type(value) is not str:
        raise ForwardUniversePrecommitAuthorityError(f"{name} must be canonical text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ForwardUniversePrecommitAuthorityError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ForwardUniversePrecommitAuthorityError(f"{name} must include timezone")
    return parsed.astimezone(UTC)


def _build_resolver(
    *,
    locator_type: type[ForwardUniversePrecommitLocator],
    manifest_type: type[CampaignPrecommitManifest],
    witness_type: type[CampaignPrecommitPublicationWitness],
    parent_error: type[CampaignPrecommitManifestError],
    witness_resolver,
    manifest_loader,
):
    """Seal positive #1257 executable dispatch at import time."""

    witness_resolver_code = witness_resolver.__code__
    manifest_loader_code = manifest_loader.__code__

    def resolve(
        *,
        locator: ForwardUniversePrecommitLocator,
        campaign_id: str,
        source_id: str,
        evaluation_universe_sha256: str,
        earliest_source_observation: datetime,
        latest_source_observation: datetime,
    ) -> ForwardUniversePrecommitResolution:
        if type(locator) is not locator_type:
            raise TypeError("precommit locator must be exact ForwardUniversePrecommitLocator")
        if type(campaign_id) is not str or not campaign_id:
            raise ForwardUniversePrecommitAuthorityError("campaign_id must be non-empty text")
        if type(source_id) is not str or not source_id:
            raise ForwardUniversePrecommitAuthorityError("source_id must be non-empty text")
        if (
            type(evaluation_universe_sha256) is not str
            or len(evaluation_universe_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in evaluation_universe_sha256)
        ):
            raise ForwardUniversePrecommitAuthorityError(
                "evaluation_universe_sha256 must be lowercase SHA-256"
            )
        if type(earliest_source_observation) is not datetime or (
            earliest_source_observation.tzinfo is None
            or earliest_source_observation.utcoffset() is None
        ):
            raise ForwardUniversePrecommitAuthorityError(
                "earliest_source_observation must be timezone-aware datetime"
            )
        if type(latest_source_observation) is not datetime or (
            latest_source_observation.tzinfo is None
            or latest_source_observation.utcoffset() is None
        ):
            raise ForwardUniversePrecommitAuthorityError(
                "latest_source_observation must be timezone-aware datetime"
            )
        earliest = earliest_source_observation.astimezone(UTC)
        latest = latest_source_observation.astimezone(UTC)
        if earliest > latest:
            raise ForwardUniversePrecommitAuthorityError(
                "source observation interval is inverted"
            )

        if witness_resolver.__code__ is not witness_resolver_code:
            raise ForwardUniversePrecommitAuthorityError(
                "campaign precommit witness resolver executable changed"
            )
        if manifest_loader.__code__ is not manifest_loader_code:
            raise ForwardUniversePrecommitAuthorityError(
                "campaign precommit manifest loader executable changed"
            )
        target = locator.absolute_manifest_path
        try:
            witness = witness_resolver(
                target,
                workspace=locator.workspace,
                workspace_instance_id=locator.workspace_instance_id,
                authority_root=locator.authority_root,
            )
            manifest = manifest_loader(target)
        except parent_error as exc:
            raise ForwardUniversePrecommitAuthorityError(
                "campaign precommit authority cannot be resolved"
            ) from exc
        if witness_resolver.__code__ is not witness_resolver_code:
            raise ForwardUniversePrecommitAuthorityError(
                "campaign precommit witness resolver executable changed"
            )
        if manifest_loader.__code__ is not manifest_loader_code:
            raise ForwardUniversePrecommitAuthorityError(
                "campaign precommit manifest loader executable changed"
            )
        if type(witness) is not witness_type:
            raise ForwardUniversePrecommitAuthorityError(
                "campaign precommit witness has noncanonical type"
            )
        if type(manifest) is not manifest_type:
            raise ForwardUniversePrecommitAuthorityError(
                "campaign precommit manifest has noncanonical type"
            )
        if witness.manifest_sha256 != manifest.manifest_sha256:
            raise ForwardUniversePrecommitAuthorityError(
                "campaign precommit witness does not bind current manifest"
            )
        if witness.campaign_id != manifest.campaign_id:
            raise ForwardUniversePrecommitAuthorityError(
                "campaign precommit witness campaign does not match manifest"
            )
        if locator.workspace_instance_id is not None and (
            witness.workspace_instance_id != locator.workspace_instance_id
        ):
            raise ForwardUniversePrecommitAuthorityError(
                "campaign precommit witness workspace identity changed"
            )
        if manifest.campaign_id != campaign_id:
            raise ForwardUniversePrecommitAuthorityError(
                "campaign precommit campaign does not match forward campaign"
            )
        if manifest.source_id != source_id:
            raise ForwardUniversePrecommitAuthorityError(
                "campaign precommit source does not match provider universe source"
            )
        if manifest.evaluation_universe_sha256 != evaluation_universe_sha256:
            raise ForwardUniversePrecommitAuthorityError(
                "campaign precommit evaluation universe does not match durable universe"
            )

        witness_observed = _instant(
            witness.post_publish_observed_at,
            "publication witness observation",
        )
        not_before = _instant(manifest.observation_not_before, "observation_not_before")
        not_after = _instant(manifest.observation_not_after, "observation_not_after")
        if witness_observed >= earliest:
            raise ForwardUniversePrecommitAuthorityError(
                "campaign precommit publication was not witnessed before source observation"
            )
        if earliest < not_before or latest > not_after:
            raise ForwardUniversePrecommitAuthorityError(
                "durable universe observations fall outside precommitted campaign window"
            )

        return ForwardUniversePrecommitResolution(
            campaign_id=manifest.campaign_id,
            source_id=manifest.source_id,
            evaluation_universe_sha256=manifest.evaluation_universe_sha256,
            manifest_sha256=manifest.manifest_sha256,
            witness_authority_record_sha256=witness.authority_record_sha256,
            witness_semantic_binding_sha256=witness.semantic_binding_sha256,
            witness_workspace_instance_id=witness.workspace_instance_id,
            witness_generation=witness.authority_generation,
            witness_observed_at=witness_observed,
            observation_not_before=not_before,
            observation_not_after=not_after,
        )

    return resolve


resolve_forward_universe_precommit_authority = _build_resolver(
    locator_type=ForwardUniversePrecommitLocator,
    manifest_type=CampaignPrecommitManifest,
    witness_type=CampaignPrecommitPublicationWitness,
    parent_error=CampaignPrecommitManifestError,
    witness_resolver=resolve_campaign_precommit_publication_witness,
    manifest_loader=load_campaign_precommit_manifest,
)
del _build_resolver


__all__ = [
    "ForwardUniversePrecommitAuthorityError",
    "ForwardUniversePrecommitLocator",
    "ForwardUniversePrecommitResolution",
    "resolve_forward_universe_precommit_authority",
]
