from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence

from .campaign_cost_evidence import (
    CampaignEconomicEvidenceVersion,
    CostEvidence,
    CostEvidenceError,
    derive_campaign_economics,
)
from .campaign_economic_authority import FinalizedCampaignAuthority
from .monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicWorkspaceAuthority,
    RecoveryDisposition,
)


class CampaignEconomicStoreError(RuntimeError):
    """Raised when durable campaign economic history cannot be trusted."""


class CampaignEconomicEvidenceStore:
    """Append-only economic sidecar with workspace-external rollback fencing.

    The external MonotonicWorkspaceAuthority owns freshness. The local head is
    only a cache of that authority tip, so restoring or deleting an older local
    campaign directory cannot silently erase a later correction.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        campaign: FinalizedCampaignAuthority,
        authority_root: str | os.PathLike[str] | None = None,
    ) -> None:
        if type(campaign) is not FinalizedCampaignAuthority:
            raise CampaignEconomicStoreError(
                "store requires FinalizedCampaignAuthority"
            )
        self.root = Path(root).absolute()
        self.root.mkdir(parents=True, exist_ok=True)
        self.campaign = campaign
        self.campaign_sha256 = campaign.projection().campaign_sha256
        self._authority = MonotonicWorkspaceAuthority(
            workspace=self.root,
            domain="autosport.campaign_economics.v3",
            key=self.campaign_sha256,
            authority_root=authority_root,
        )

    def derive(
        self,
        *,
        costs: Sequence[CostEvidence],
        as_of,
        previous: CampaignEconomicEvidenceVersion | None = None,
    ) -> CampaignEconomicEvidenceVersion:
        return derive_campaign_economics(
            campaign=self.campaign,
            costs=costs,
            as_of=as_of,
            previous=previous,
        )

    def append(self, version: CampaignEconomicEvidenceVersion) -> str:
        if version.campaign_sha256 != self.campaign_sha256:
            raise CampaignEconomicStoreError(
                "economic version belongs to a different campaign"
            )
        if self._recover_exact_prepared_publish(version):
            return version.version_id

        current = self.latest()
        self._require_exact_retry_after_abort(version, current)
        if current is None:
            if version.previous_version_id is not None:
                raise CampaignEconomicStoreError(
                    "first economic version cannot name a predecessor"
                )
        else:
            if version.version_id == current.version_id:
                return version.version_id
            if (
                version.previous_version_id != current.version_id
                or version.previous_version_sha256 != current.record_sha256
            ):
                raise CampaignEconomicStoreError(
                    "economic successor does not extend current authority tip"
                )

        self._validate_derived(version, current)
        binding = self._semantic_binding(version)
        publication_tx_id = self._next_publication_tx_id(version, binding)
        prepared = self._authority.prepare(
            tx_id=publication_tx_id,
            observed_state_sha256=None if current is None else current.version_id,
            intended_state_sha256=version.version_id,
            semantic_binding_sha256=binding,
        )
        if (
            prepared.phase is not AuthorityPhase.PREPARE
            or prepared.tx_id != publication_tx_id
            or prepared.intended_state_sha256 != version.version_id
            or prepared.semantic_binding_sha256 != binding
        ):
            raise CampaignEconomicStoreError(
                "economic publication did not acquire an exact fresh authority PREPARE"
            )

        version_path = self._versions_dir() / f"{version.version_id}.json"
        encoded = _canonical_bytes(version.to_dict())
        if version_path.exists():
            if version_path.read_bytes() != encoded:
                raise CampaignEconomicStoreError(
                    "immutable economic version conflicts with exact retry"
                )
        else:
            _exclusive_create(version_path, encoded)

        _atomic_json(
            self._head_path(),
            {
                "schema_version": 1,
                "campaign_sha256": self.campaign_sha256,
                "version_id": version.version_id,
                "record_sha256": version.record_sha256,
            },
        )
        if self._read_head_id() != version.version_id:
            raise CampaignEconomicStoreError(
                "economic head failed exact durable re-read"
            )
        self._authority.commit(
            tx_id=publication_tx_id,
            observed_state_sha256=version.version_id,
            semantic_binding_sha256=binding,
        )
        return version.version_id

    def latest(self) -> CampaignEconomicEvidenceVersion | None:
        history = self._authority.read_history()
        if history and history[-1].phase is AuthorityPhase.PREPARE:
            pending = history[-1]
            prepared_path = self._versions_dir() / f"{pending.intended_state_sha256}.json"
            if prepared_path.exists():
                prepared_version = self._load_raw(pending.intended_state_sha256)
                if self._recover_exact_prepared_publish(prepared_version):
                    chain = self._chain_from(prepared_version)
                    if self._version_ids() != {
                        value.version_id for value in chain
                    }:
                        raise CampaignEconomicStoreError(
                            "economic store contains orphaned or rolled-back local history"
                        )
                    return prepared_version

        head_id = self._read_head_id()
        if head_id is None:
            if self._version_ids():
                raise CampaignEconomicStoreError(
                    "economic versions exist without a local head"
                )
            self._authority.recover(observed_state_sha256=None)
            return None

        latest = self._load_raw(head_id)
        self._authority.recover(
            observed_state_sha256=head_id,
            tx_id=head_id,
            semantic_binding_sha256=self._semantic_binding(latest),
        )
        chain = self._chain_from(latest)
        if self._version_ids() != {value.version_id for value in chain}:
            raise CampaignEconomicStoreError(
                "economic store contains orphaned or rolled-back local history"
            )
        return latest

    def load(self, version_id: str) -> CampaignEconomicEvidenceVersion:
        latest = self.latest()
        if latest is None:
            raise CampaignEconomicStoreError("economic evidence store is empty")
        by_id = {value.version_id: value for value in self._chain_from(latest)}
        try:
            return by_id[version_id]
        except KeyError as exc:
            raise CampaignEconomicStoreError(
                "economic version is not in committed history"
            ) from exc

    def verify_chain(self) -> tuple[CampaignEconomicEvidenceVersion, ...]:
        latest = self.latest()
        return () if latest is None else self._chain_from(latest)

    def _recover_exact_prepared_publish(
        self, version: CampaignEconomicEvidenceVersion
    ) -> bool:
        """Finish only the exact durable PREPARE -> local-file crash prefix.

        Normal reads remain strict about orphan files. Recovery is admitted only
        while the independent monotonic authority still has the exact requested
        version as its live PREPARE and local history contains exactly the
        committed predecessor chain plus that byte-exact direct successor.
        """
        version_path = self._versions_dir() / f"{version.version_id}.json"
        if not version_path.exists():
            return False

        history = self._authority.read_history()
        if not history:
            return False
        pending = history[-1]
        if pending.phase is not AuthorityPhase.PREPARE:
            return False

        binding = self._semantic_binding(version)
        if not self._is_publication_tx_id(version.version_id, pending.tx_id):
            if pending.intended_state_sha256 == version.version_id:
                raise CampaignEconomicStoreError(
                    "prepared economic successor uses an invalid publication transaction"
                )
            return False
        if (
            pending.intended_state_sha256 != version.version_id
            or pending.semantic_binding_sha256 != binding
        ):
            raise CampaignEconomicStoreError(
                "prepared economic successor does not match durable authority"
            )

        predecessor_id = pending.previous_committed_state_sha256
        if version.previous_version_id != predecessor_id:
            raise CampaignEconomicStoreError(
                "prepared economic successor does not extend authority predecessor"
            )
        head_id = self._read_head_id()
        if head_id not in (predecessor_id, version.version_id):
            raise CampaignEconomicStoreError(
                "prepared economic successor conflicts with local head"
            )

        current = None if predecessor_id is None else self._load_raw(predecessor_id)
        if current is None:
            if version.previous_version_sha256 is not None:
                raise CampaignEconomicStoreError(
                    "prepared first economic version names predecessor digest"
                )
            committed_ids: set[str] = set()
        else:
            if version.previous_version_sha256 != current.record_sha256:
                raise CampaignEconomicStoreError(
                    "prepared economic predecessor digest mismatch"
                )
            committed_ids = {
                value.version_id for value in self._chain_from(current)
            }

        self._validate_derived(version, current)
        encoded = _canonical_bytes(version.to_dict())
        if version_path.read_bytes() != encoded:
            raise CampaignEconomicStoreError(
                "prepared immutable economic version conflicts with exact retry"
            )
        if self._version_ids() != committed_ids | {version.version_id}:
            raise CampaignEconomicStoreError(
                "prepared recovery found unrelated economic version history"
            )

        if head_id != version.version_id:
            _atomic_json(
                self._head_path(),
                {
                    "schema_version": 1,
                    "campaign_sha256": self.campaign_sha256,
                    "version_id": version.version_id,
                    "record_sha256": version.record_sha256,
                },
            )
            if self._read_head_id() != version.version_id:
                raise CampaignEconomicStoreError(
                    "recovered economic head failed exact durable re-read"
                )
        recovered = self._authority.recover(
            observed_state_sha256=version.version_id,
            tx_id=pending.tx_id,
            semantic_binding_sha256=binding,
        )
        if (
            recovered.disposition is not RecoveryDisposition.COMMITTED_PREPARE
            or recovered.committed_state_sha256 != version.version_id
        ):
            raise CampaignEconomicStoreError(
                "prepared economic recovery did not commit exact successor"
            )
        return True

    def _require_exact_retry_after_abort(
        self,
        version: CampaignEconomicEvidenceVersion,
        current: CampaignEconomicEvidenceVersion | None,
    ) -> None:
        """Do not let a durable aborted PREPARE silently change semantic intent."""
        history = self._authority.read_history()
        if not history:
            return
        aborted = history[-1]
        if aborted.phase is not AuthorityPhase.ABORT:
            return
        current_id = None if current is None else current.version_id
        if aborted.previous_committed_state_sha256 != current_id:
            raise CampaignEconomicStoreError(
                "aborted economic publication predecessor conflicts with current state"
            )
        if not self._is_publication_tx_id(
            aborted.intended_state_sha256, aborted.tx_id
        ):
            raise CampaignEconomicStoreError(
                "aborted economic publication has invalid transaction identity"
            )
        if version.version_id != aborted.intended_state_sha256:
            raise CampaignEconomicStoreError(
                "aborted economic publication requires exact semantic retry"
            )

    def _next_publication_tx_id(
        self,
        version: CampaignEconomicEvidenceVersion,
        binding: str,
    ) -> str:
        """Allocate a never-reused authority tx for one physical publish attempt."""
        history = self._authority.read_history()
        prefix = f"{version.version_id}:publish:"
        used: set[str] = set()
        for record in history:
            belongs_to_target = record.intended_state_sha256 == version.version_id
            names_target_attempt = (
                record.tx_id == version.version_id or record.tx_id.startswith(prefix)
            )
            if belongs_to_target:
                if (
                    record.semantic_binding_sha256 != binding
                    or not self._is_publication_tx_id(version.version_id, record.tx_id)
                ):
                    raise CampaignEconomicStoreError(
                        "economic version has conflicting authority publication history"
                    )
                used.add(record.tx_id)
            elif names_target_attempt:
                raise CampaignEconomicStoreError(
                    "economic publication transaction identity was reused for another state"
                )

        attempt = 1
        while True:
            candidate = f"{prefix}{attempt}"
            if candidate not in used:
                return candidate
            attempt += 1

    @staticmethod
    def _is_publication_tx_id(version_id: str, tx_id: str) -> bool:
        if tx_id == version_id:
            return True
        prefix = f"{version_id}:publish:"
        if not tx_id.startswith(prefix):
            return False
        suffix = tx_id[len(prefix) :]
        return (
            suffix.isascii()
            and suffix.isdigit()
            and suffix != "0"
            and not suffix.startswith("0")
        )

    def _chain_from(
        self, head: CampaignEconomicEvidenceVersion
    ) -> tuple[CampaignEconomicEvidenceVersion, ...]:
        reverse: list[CampaignEconomicEvidenceVersion] = []
        seen: set[str] = set()
        current = head
        while True:
            if current.version_id in seen:
                raise CampaignEconomicStoreError(
                    "economic evidence chain contains a cycle"
                )
            seen.add(current.version_id)
            previous = None
            if current.previous_version_id is not None:
                previous = self._load_raw(current.previous_version_id)
                if current.previous_version_sha256 != previous.record_sha256:
                    raise CampaignEconomicStoreError(
                        "economic predecessor digest mismatch"
                    )
            self._validate_derived(current, previous)
            reverse.append(current)
            if previous is None:
                break
            current = previous
        reverse.reverse()
        return tuple(reverse)

    def _validate_derived(
        self,
        version: CampaignEconomicEvidenceVersion,
        previous: CampaignEconomicEvidenceVersion | None,
    ) -> None:
        try:
            expected = self.derive(
                costs=version.costs,
                as_of=version.as_of,
                previous=previous,
            )
        except CostEvidenceError as exc:
            raise CampaignEconomicStoreError(
                "economic version failed canonical re-derivation"
            ) from exc
        if expected != version:
            raise CampaignEconomicStoreError(
                "economic derived fields or campaign authority are forged"
            )

    def _load_raw(self, version_id: str) -> CampaignEconomicEvidenceVersion:
        path = self._versions_dir() / f"{version_id}.json"
        try:
            raw = _strict_json(path.read_bytes(), "economic version")
        except FileNotFoundError as exc:
            raise CampaignEconomicStoreError(
                "economic version file is missing"
            ) from exc
        try:
            version = CampaignEconomicEvidenceVersion.from_dict(raw)
        except (CostEvidenceError, TypeError, ValueError) as exc:
            raise CampaignEconomicStoreError(
                "economic version failed integrity validation"
            ) from exc
        if version.version_id != version_id:
            raise CampaignEconomicStoreError(
                "economic version path identity mismatch"
            )
        if version.campaign_sha256 != self.campaign_sha256:
            raise CampaignEconomicStoreError(
                "economic version campaign identity mismatch"
            )
        return version

    def _read_head_id(self) -> str | None:
        path = self._head_path()
        if not path.exists():
            return None
        raw = _strict_json(path.read_bytes(), "economic head")
        expected = {
            "schema_version", "campaign_sha256", "version_id", "record_sha256"
        }
        if set(raw) != expected:
            raise CampaignEconomicStoreError("economic head schema is invalid")
        if raw["schema_version"] != 1:
            raise CampaignEconomicStoreError("economic head version is unsupported")
        if raw["campaign_sha256"] != self.campaign_sha256:
            raise CampaignEconomicStoreError("economic head campaign mismatch")
        version_id = raw["version_id"]
        if not isinstance(version_id, str) or raw["record_sha256"] != version_id:
            raise CampaignEconomicStoreError("economic head digest mismatch")
        if len(version_id) != 64 or any(ch not in "0123456789abcdef" for ch in version_id):
            raise CampaignEconomicStoreError("economic head id is not SHA-256")
        return version_id

    def _semantic_binding(self, version: CampaignEconomicEvidenceVersion) -> str:
        payload = {
            "campaign_sha256": self.campaign_sha256,
            "version_id": version.version_id,
            "previous_version_id": version.previous_version_id,
        }
        return _sha256(_canonical_bytes(payload))

    def _version_ids(self) -> set[str]:
        directory = self._versions_dir()
        if not directory.exists():
            return set()
        values: set[str] = set()
        for path in directory.glob("*.json"):
            if len(path.stem) != 64 or any(
                ch not in "0123456789abcdef" for ch in path.stem
            ):
                raise CampaignEconomicStoreError(
                    "economic version filename is not SHA-256"
                )
            values.add(path.stem)
        return values

    def _campaign_dir(self) -> Path:
        return self.root / "campaign_economics" / self.campaign_sha256

    def _versions_dir(self) -> Path:
        directory = self._campaign_dir() / "versions"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _head_path(self) -> Path:
        return self._campaign_dir() / "head.json"


def _canonical_bytes(raw: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            raw,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256(raw: bytes) -> str:
    import hashlib

    return hashlib.sha256(raw).hexdigest()


def _strict_json(raw: bytes, label: str) -> Mapping[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CampaignEconomicStoreError(f"{label} is not UTF-8") from exc

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise CampaignEconomicStoreError(
                    f"{label} contains duplicate key {key!r}"
                )
            result[key] = value
        return result

    try:
        parsed = json.loads(
            text,
            object_pairs_hook=no_duplicates,
            parse_constant=lambda value: (_ for _ in ()).throw(
                CampaignEconomicStoreError(
                    f"{label} contains non-standard number {value}"
                )
            ),
        )
    except json.JSONDecodeError as exc:
        raise CampaignEconomicStoreError(f"{label} is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise CampaignEconomicStoreError(f"{label} must contain an object")
    return parsed


def _exclusive_create(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    _fsync_dir(path.parent)


def _atomic_json(path: Path, raw: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical_bytes(raw))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_dir(path.parent)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
