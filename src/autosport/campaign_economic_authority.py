from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from ._paper_execution_anti_rollback import _authority_root, _sync_authority_directory
from .campaign_denomination import (
    CampaignDenominationBinding,
    CampaignDenominationError,
    rehydrate_campaign_denomination_binding,
)
from .campaign_evidence import (
    PaperCampaign,
    _validate_authoritative_session,
    _validate_registry_bindings,
)
from .integrity import atomic_write_json
from .run_registry import RunRegistry
from .scientific_registry import ScientificRegistry
from .workspace_lock import WorkspaceEconomicLock


class CampaignEconomicAuthorityError(ValueError):
    """Raised when finalized campaign economic authority cannot be proven."""


_DENOMINATION_STATE_KEY = "campaign_denomination_bindings"
_ISSUANCE_WITNESS_SCHEMA_VERSION = 1
_ISSUANCE_WITNESS_SUFFIX = ".campaign-denomination-issuance.monotonic-witness.jsonl"


def _sha256_payload(payload: object) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise CampaignEconomicAuthorityError(
            "campaign denomination source is not canonically serializable"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _canonical_sha256(value: object, label: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or value != value.lower()
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise CampaignEconomicAuthorityError(f"{label} is not canonical SHA-256")
    return value


def _canonical_text(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise CampaignEconomicAuthorityError(f"{label} is not canonical text")
    return value


def _utc_datetime(value: object, label: str) -> datetime:
    text = _canonical_text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CampaignEconomicAuthorityError(f"{label} is not ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CampaignEconomicAuthorityError(f"{label} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _projection_sha256(value: "CanonicalCampaignProjection") -> str:
    return _sha256_payload(
        {
            "campaign_id": value.campaign_id,
            "campaign_version": value.campaign_version,
            "campaign_sha256": value.campaign_sha256,
            "session_refs": [
                {
                    "evidence_id": item.evidence_id,
                    "evidence_sha256": item.evidence_sha256,
                }
                for item in value.session_refs
            ],
            "membership_refs": [
                {
                    "kind": item.kind,
                    "evidence_id": item.evidence_id,
                    "sha256": item.sha256,
                }
                for item in value.membership_refs
            ],
            "gross_run_pnl": str(value.gross_run_pnl),
        }
    )


def _membership_identity(
    projection: "CanonicalCampaignProjection", kind: str
) -> str:
    refs = [
        {"evidence_id": item.evidence_id, "sha256": item.sha256}
        for item in projection.membership_refs
        if item.kind == kind
    ]
    if not refs:
        raise CampaignEconomicAuthorityError(
            f"campaign denomination lacks {kind} membership authority"
        )
    return f"{kind.lower()}-membership:{_sha256_payload(refs)}"


def _economic_goal_source(summary: Mapping[str, Any]) -> dict[str, object] | None:
    runtime = summary.get("strategy_runtime")
    if not isinstance(runtime, Mapping):
        raise CampaignEconomicAuthorityError(
            "completed run lacks canonical strategy_runtime authority"
        )
    raw = runtime.get("economic_goal_provenance")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise CampaignEconomicAuthorityError(
            "completed run economic-goal provenance is malformed"
        )
    required = {
        "schema",
        "schema_version",
        "goal_id",
        "revision",
        "bankroll_id",
        "contract_sha256",
    }
    if not required.issubset(raw):
        raise CampaignEconomicAuthorityError(
            "completed run economic-goal provenance is incomplete"
        )
    if "currency" not in raw:
        return None
    schema = _canonical_text(raw["schema"], "economic-goal provenance schema")
    version = raw["schema_version"]
    if schema != "autosport.economic_goal_provenance" or version != 1:
        raise CampaignEconomicAuthorityError(
            "completed run economic-goal provenance schema is unsupported"
        )
    revision = raw["revision"]
    if type(revision) is not int or revision <= 0:
        raise CampaignEconomicAuthorityError(
            "completed run economic-goal revision is invalid"
        )
    currency = _canonical_text(raw["currency"], "economic-goal currency")
    if (
        len(currency) != 3
        or not currency.isascii()
        or not currency.isalpha()
        or currency != currency.upper()
    ):
        raise CampaignEconomicAuthorityError(
            "completed run economic-goal currency is invalid"
        )
    return {
        "goal_id": _canonical_text(raw["goal_id"], "economic-goal goal_id"),
        "revision": revision,
        "bankroll_id": _canonical_text(
            raw["bankroll_id"], "economic-goal bankroll_id"
        ),
        "currency": currency,
        "contract_sha256": _canonical_sha256(
            raw["contract_sha256"], "economic-goal contract_sha256"
        ),
    }


def _registry_identity(path: Path) -> str:
    try:
        normalized = os.path.normcase(os.path.abspath(os.fspath(path.resolve(strict=False))))
    except OSError as exc:
        raise CampaignEconomicAuthorityError(
            "cannot resolve campaign denomination registry identity"
        ) from exc
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _issuance_witness_path(registry: ScientificRegistry) -> Path:
    try:
        root = _authority_root(registry.path)
    except Exception as exc:
        raise CampaignEconomicAuthorityError(
            "cannot establish independent campaign denomination issuance authority"
        ) from exc
    return root / f"{_registry_identity(registry.path)}{_ISSUANCE_WITNESS_SUFFIX}"


def _read_issuance_witnesses(registry: ScientificRegistry) -> list[dict[str, object]]:
    path = _issuance_witness_path(registry)
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise CampaignEconomicAuthorityError(
            "cannot read campaign denomination issuance witness"
        ) from exc
    expected_keys = {
        "witness_schema_version",
        "sequence",
        "registry_identity",
        "registry_name",
        "binding_key",
        "binding_payload_sha256",
        "available_at",
        "previous_witness_sha256",
        "witness_sha256",
    }
    records: list[dict[str, object]] = []
    previous: str | None = None
    seen_keys: set[str] = set()
    for sequence, raw in enumerate(lines, start=1):
        if not raw:
            raise CampaignEconomicAuthorityError(
                "campaign denomination issuance witness contains a blank line"
            )
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise CampaignEconomicAuthorityError(
                "campaign denomination issuance witness is unreadable"
            ) from exc
        if type(record) is not dict or set(record) != expected_keys:
            raise CampaignEconomicAuthorityError(
                "campaign denomination issuance witness schema is invalid"
            )
        if record["witness_schema_version"] != _ISSUANCE_WITNESS_SCHEMA_VERSION:
            raise CampaignEconomicAuthorityError(
                "campaign denomination issuance witness version is unsupported"
            )
        if record["sequence"] != sequence:
            raise CampaignEconomicAuthorityError(
                "campaign denomination issuance witness sequence is not contiguous"
            )
        if record["registry_identity"] != _registry_identity(registry.path):
            raise CampaignEconomicAuthorityError(
                "campaign denomination issuance witness belongs to another registry"
            )
        if record["registry_name"] != registry.path.name:
            raise CampaignEconomicAuthorityError(
                "campaign denomination issuance witness names another registry"
            )
        key = _canonical_text(record["binding_key"], "issuance witness binding_key")
        if key in seen_keys:
            raise CampaignEconomicAuthorityError(
                "campaign denomination issuance witness duplicates a binding key"
            )
        seen_keys.add(key)
        _canonical_sha256(
            record["binding_payload_sha256"],
            "issuance witness binding_payload_sha256",
        )
        _utc_datetime(record["available_at"], "issuance witness available_at")
        if record["previous_witness_sha256"] != previous:
            raise CampaignEconomicAuthorityError(
                "campaign denomination issuance witness predecessor mismatch"
            )
        body = {key: record[key] for key in expected_keys if key != "witness_sha256"}
        digest = _canonical_sha256(record["witness_sha256"], "issuance witness_sha256")
        if digest != _sha256_payload(body):
            raise CampaignEconomicAuthorityError(
                "campaign denomination issuance witness digest mismatch"
            )
        previous = digest
        records.append(record)
    return records


def _append_issuance_witness(
    registry: ScientificRegistry,
    *,
    binding_key: str,
    binding_payload_sha256: str,
    available_at: datetime,
) -> dict[str, object]:
    records = _read_issuance_witnesses(registry)
    matches = [record for record in records if record["binding_key"] == binding_key]
    available_text = available_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if matches:
        record = matches[0]
        if (
            record["binding_payload_sha256"] != binding_payload_sha256
            or record["available_at"] != available_text
        ):
            raise CampaignEconomicAuthorityError(
                "campaign denomination issuance witness conflicts with candidate binding"
            )
        return record
    body: dict[str, object] = {
        "witness_schema_version": _ISSUANCE_WITNESS_SCHEMA_VERSION,
        "sequence": len(records) + 1,
        "registry_identity": _registry_identity(registry.path),
        "registry_name": registry.path.name,
        "binding_key": binding_key,
        "binding_payload_sha256": _canonical_sha256(
            binding_payload_sha256, "binding payload sha256"
        ),
        "available_at": available_text,
        "previous_witness_sha256": None if not records else records[-1]["witness_sha256"],
    }
    record = {**body, "witness_sha256": _sha256_payload(body)}
    path = _issuance_witness_path(registry)
    existed = path.exists()
    try:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        if not existed:
            _sync_authority_directory(path.parent)
    except OSError as exc:
        raise CampaignEconomicAuthorityError(
            "campaign denomination issuance witness durability barrier failed"
        ) from exc
    reread = _read_issuance_witnesses(registry)
    if not reread or reread[-1] != record:
        raise CampaignEconomicAuthorityError(
            "campaign denomination issuance witness was not durably re-resolved"
        )
    return record


@dataclass(frozen=True, order=True, slots=True)
class CanonicalSessionRef:
    evidence_id: str
    evidence_sha256: str


@dataclass(frozen=True, order=True, slots=True)
class CanonicalMembershipRef:
    kind: str
    evidence_id: str
    sha256: str


@dataclass(frozen=True, slots=True)
class CanonicalCampaignProjection:
    campaign_id: str
    campaign_version: int
    campaign_sha256: str
    session_refs: tuple[CanonicalSessionRef, ...]
    membership_refs: tuple[CanonicalMembershipRef, ...]
    gross_run_pnl: Decimal


class FinalizedCampaignAuthority:
    """Product-owned capability over an already-authoritative PaperCampaign.

    Denomination payload bytes remain co-located with ScientificRegistry state, but
    positive authority additionally requires an append-only issuance witness in the
    existing independent monotonic workspace authority root. A caller-written extra
    top-level registry payload therefore cannot manufacture denomination authority.
    """

    __slots__ = ("_campaign",)

    def __init__(self, campaign: PaperCampaign) -> None:
        if type(campaign) is not PaperCampaign:
            raise CampaignEconomicAuthorityError(
                "campaign authority requires an exact PaperCampaign"
            )
        self._campaign = campaign
        self.projection()

    def projection(self) -> CanonicalCampaignProjection:
        campaign = self._campaign
        if not campaign.finalized or campaign.campaign_sha256 is None:
            raise CampaignEconomicAuthorityError(
                "campaign authority requires a finalized PaperCampaign"
            )
        scientific_registry = campaign._scientific_registry
        run_registry = campaign._run_registry
        if type(scientific_registry) is not ScientificRegistry:
            raise CampaignEconomicAuthorityError(
                "finalized campaign lacks ScientificRegistry authority"
            )
        if type(run_registry) is not RunRegistry:
            raise CampaignEconomicAuthorityError(
                "finalized campaign lacks RunRegistry authority"
            )
        if campaign.campaign_sha256 != campaign._computed_campaign_sha256():
            raise CampaignEconomicAuthorityError(
                "finalized campaign state no longer matches campaign_sha256"
            )
        _validate_registry_bindings(campaign, scientific_registry)
        sessions: list[CanonicalSessionRef] = []
        memberships: list[CanonicalMembershipRef] = []
        for session in campaign.sessions:
            _validate_authoritative_session(
                campaign,
                session,
                scientific_registry=scientific_registry,
                run_registry=run_registry,
            )
            _, run_summary_sha256 = run_registry.verified_completed_summary_for_run(
                session.run_id
            )
            bundle = scientific_registry.get("EvaluationBundle", session.evidence_id)
            if bundle is None:
                raise CampaignEconomicAuthorityError(
                    "campaign session lacks EvaluationBundle authority"
                )
            bundle_sha256 = bundle.payload.get("bundle_sha256")
            if not isinstance(bundle_sha256, str) or len(bundle_sha256) != 64:
                raise CampaignEconomicAuthorityError(
                    "EvaluationBundle lacks canonical bundle_sha256"
                )
            sessions.append(
                CanonicalSessionRef(
                    evidence_id=session.evidence_id,
                    evidence_sha256=session.evidence_sha256,
                )
            )
            memberships.extend(
                (
                    CanonicalMembershipRef(
                        kind="SESSION",
                        evidence_id=session.evidence_id,
                        sha256=session.evidence_sha256,
                    ),
                    CanonicalMembershipRef(
                        kind="RUN",
                        evidence_id=session.run_id,
                        sha256=run_summary_sha256,
                    ),
                    CanonicalMembershipRef(
                        kind="EVALUATION",
                        evidence_id=session.evidence_id,
                        sha256=bundle_sha256.lower(),
                    ),
                )
            )
        summary = campaign.summary()
        if summary.status != "FINALIZED":
            raise CampaignEconomicAuthorityError("campaign summary is not finalized")
        if summary.campaign_sha256 != campaign.campaign_sha256:
            raise CampaignEconomicAuthorityError(
                "campaign summary digest disagrees with finalized campaign"
            )
        if not sessions:
            raise CampaignEconomicAuthorityError(
                "finalized campaign contains no authoritative session evidence"
            )
        return CanonicalCampaignProjection(
            campaign_id=campaign.campaign_id,
            campaign_version=campaign.campaign_version,
            campaign_sha256=campaign.campaign_sha256,
            session_refs=tuple(sorted(sessions)),
            membership_refs=tuple(sorted(memberships)),
            gross_run_pnl=summary.net_profit_total,
        )

    def _binding_key(self) -> str:
        projection = self.projection()
        return (
            f"{projection.campaign_id}:v{projection.campaign_version}:"
            f"{projection.campaign_sha256}"
        )

    def _registry(self) -> ScientificRegistry:
        registry = self._campaign._scientific_registry
        if type(registry) is not ScientificRegistry:
            raise CampaignEconomicAuthorityError(
                "finalized campaign lacks ScientificRegistry denomination persistence"
            )
        return registry

    def _binding_source(self) -> tuple[
        CanonicalCampaignProjection,
        dict[str, object] | None,
        list[dict[str, object]],
        list[dict[str, str]],
    ]:
        projection = self.projection()
        run_registry = self._campaign._run_registry
        if type(run_registry) is not RunRegistry:
            raise CampaignEconomicAuthorityError(
                "finalized campaign lacks RunRegistry denomination authority"
            )
        source_rows: list[dict[str, object]] = []
        goal_rows: list[dict[str, object] | None] = []
        portfolio_rows: list[dict[str, str]] = []
        for session in sorted(self._campaign.sessions, key=lambda value: value.run_id):
            summary, run_summary_sha256 = run_registry.verified_completed_summary_for_run(
                session.run_id
            )
            goal = _economic_goal_source(summary)
            goal_rows.append(goal)
            paper_book_sha256 = _canonical_sha256(
                summary.get("paper_book_sha256"), "run paper_book_sha256"
            )
            decision_ledger_sha256 = _canonical_sha256(
                summary.get("decision_ledger_sha256"), "run decision_ledger_sha256"
            )
            source_rows.append(
                {
                    "run_id": session.run_id,
                    "run_summary_sha256": run_summary_sha256,
                    "paper_book_sha256": paper_book_sha256,
                    "decision_ledger_sha256": decision_ledger_sha256,
                    "economic_goal_provenance": goal,
                }
            )
            portfolio_rows.append(
                {
                    "run_id": session.run_id,
                    "paper_book_sha256": paper_book_sha256,
                    "decision_ledger_sha256": decision_ledger_sha256,
                    "run_summary_sha256": run_summary_sha256,
                }
            )
        if all(value is None for value in goal_rows):
            return projection, None, source_rows, portfolio_rows
        if any(value is None for value in goal_rows):
            raise CampaignEconomicAuthorityError(
                "finalized campaign mixes denominated and non-denominated run authority"
            )
        assert goal_rows and goal_rows[0] is not None
        canonical_goal = goal_rows[0]
        if any(value != canonical_goal for value in goal_rows[1:]):
            raise CampaignEconomicAuthorityError(
                "finalized campaign spans conflicting EconomicGoal denomination authority"
            )
        return projection, canonical_goal, source_rows, portfolio_rows

    def _derive_binding(self, *, available_at: datetime) -> CampaignDenominationBinding | None:
        projection, canonical_goal, source_rows, portfolio_rows = self._binding_source()
        if canonical_goal is None:
            return None
        if (
            type(available_at) is not datetime
            or available_at.tzinfo is None
            or available_at.utcoffset() is None
        ):
            raise CampaignEconomicAuthorityError(
                "campaign denomination issuance clock must return timezone-aware datetime"
            )
        available_at = available_at.astimezone(timezone.utc)
        finalized_at = self._campaign.finalized_at
        if finalized_at is None:
            raise CampaignEconomicAuthorityError(
                "finalized campaign lacks finalized_at denomination boundary"
            )
        effective_at = min(
            _utc_datetime(
                session.evaluation_window_start, "session evaluation_window_start"
            )
            for session in self._campaign.sessions
        )
        observed_at = max(
            _utc_datetime(session.as_of, "session as_of")
            for session in self._campaign.sessions
        )
        finalized_boundary = _utc_datetime(finalized_at, "campaign finalized_at")
        if available_at < finalized_boundary:
            raise CampaignEconomicAuthorityError(
                "campaign denomination cannot be available before campaign finalization"
            )
        if available_at < observed_at:
            raise CampaignEconomicAuthorityError(
                "campaign denomination cannot be available before observed evidence"
            )
        return CampaignDenominationBinding(
            campaign_id=projection.campaign_id,
            campaign_version=str(projection.campaign_version),
            campaign_sha256=projection.campaign_sha256,
            session_id=_membership_identity(projection, "SESSION"),
            run_id=_membership_identity(projection, "RUN"),
            evaluation_id=_membership_identity(projection, "EVALUATION"),
            projection_sha256=_projection_sha256(projection),
            economic_goal_id=str(canonical_goal["goal_id"]),
            economic_goal_revision=int(canonical_goal["revision"]),
            economic_goal_sha256=str(canonical_goal["contract_sha256"]),
            bankroll_id=str(canonical_goal["bankroll_id"]),
            portfolio_identity="paper-portfolio:" + _sha256_payload(portfolio_rows),
            currency=str(canonical_goal["currency"]),
            effective_at=effective_at,
            observed_at=observed_at,
            available_at=available_at,
            source_evidence_sha256=_sha256_payload(source_rows),
        )

    def _persisted_payload(self) -> Mapping[str, object] | None:
        state = self._registry()._read()
        raw_bindings = state.get(_DENOMINATION_STATE_KEY)
        if raw_bindings is None:
            return None
        if type(raw_bindings) is not dict:
            raise CampaignEconomicAuthorityError(
                "persisted campaign denomination index is malformed"
            )
        raw = raw_bindings.get(self._binding_key())
        if raw is None:
            return None
        if type(raw) is not dict:
            raise CampaignEconomicAuthorityError(
                "persisted campaign denomination binding is malformed"
            )
        return raw

    def _issuance_witness(self) -> Mapping[str, object] | None:
        key = self._binding_key()
        records = _read_issuance_witnesses(self._registry())
        matches = [record for record in records if record["binding_key"] == key]
        if not matches:
            return None
        if len(matches) != 1:
            raise CampaignEconomicAuthorityError(
                "campaign denomination issuance witness is ambiguous"
            )
        return matches[0]

    def _verified_persisted_binding(
        self,
        raw: Mapping[str, object],
        witness: Mapping[str, object],
    ) -> CampaignDenominationBinding:
        try:
            persisted = rehydrate_campaign_denomination_binding(raw)
        except CampaignDenominationError as exc:
            raise CampaignEconomicAuthorityError(
                "persisted campaign denomination binding failed integrity validation"
            ) from exc
        payload_sha = _sha256_payload(dict(persisted.canonical_payload))
        if witness.get("binding_payload_sha256") != payload_sha:
            raise CampaignEconomicAuthorityError(
                "persisted campaign denomination is not backed by its issuance witness"
            )
        witnessed_at = _utc_datetime(
            witness.get("available_at"), "issuance witness available_at"
        )
        if persisted.available_at.astimezone(timezone.utc) != witnessed_at:
            raise CampaignEconomicAuthorityError(
                "persisted campaign denomination timestamp is not issuance-witnessed"
            )
        expected = self._derive_binding(available_at=witnessed_at)
        if expected is None or persisted != expected:
            raise CampaignEconomicAuthorityError(
                "persisted campaign denomination binding drifts from live campaign authority"
            )
        return persisted

    def issue_denomination_binding(self) -> CampaignDenominationBinding | None:
        """Issue once and prove ancestry through independent monotonic authority."""

        registry = self._registry()
        with WorkspaceEconomicLock(registry.path.parent):
            state = registry._read()
            raw_bindings = state.get(_DENOMINATION_STATE_KEY)
            if raw_bindings is not None and type(raw_bindings) is not dict:
                raise CampaignEconomicAuthorityError(
                    "persisted campaign denomination index is malformed"
                )
            key = self._binding_key()
            prior = None if raw_bindings is None else raw_bindings.get(key)
            witness = self._issuance_witness()

            if prior is not None:
                if type(prior) is not dict:
                    raise CampaignEconomicAuthorityError(
                        "persisted campaign denomination binding is malformed"
                    )
                if witness is None:
                    raise CampaignEconomicAuthorityError(
                        "persisted campaign denomination lacks product issuance ancestry"
                    )
                return self._verified_persisted_binding(prior, witness)

            if witness is not None:
                # Recover the ordinary crash prefix where independent issuance was
                # durable but the co-located registry cache was not yet replaced.
                witnessed_at = _utc_datetime(
                    witness.get("available_at"), "issuance witness available_at"
                )
                candidate = self._derive_binding(available_at=witnessed_at)
                if candidate is None:
                    raise CampaignEconomicAuthorityError(
                        "issuance witness exists for a campaign without denomination authority"
                    )
                candidate_payload = dict(candidate.canonical_payload)
                if witness.get("binding_payload_sha256") != _sha256_payload(candidate_payload):
                    raise CampaignEconomicAuthorityError(
                        "issuance witness does not match canonical campaign denomination"
                    )
            else:
                candidate = self._derive_binding(available_at=datetime.now(timezone.utc))
                if candidate is None:
                    return None
                candidate_payload = dict(candidate.canonical_payload)
                witness = _append_issuance_witness(
                    registry,
                    binding_key=key,
                    binding_payload_sha256=_sha256_payload(candidate_payload),
                    available_at=candidate.available_at,
                )

            if raw_bindings is None:
                raw_bindings = {}
                state[_DENOMINATION_STATE_KEY] = raw_bindings
            raw_bindings[key] = candidate_payload
            atomic_write_json(registry.path, state)

            persisted_state = registry._read()
            persisted_index = persisted_state.get(_DENOMINATION_STATE_KEY)
            if type(persisted_index) is not dict:
                raise CampaignEconomicAuthorityError(
                    "persisted campaign denomination index disappeared after publication"
                )
            persisted_raw = persisted_index.get(key)
            if type(persisted_raw) is not dict:
                raise CampaignEconomicAuthorityError(
                    "persisted campaign denomination binding disappeared after publication"
                )
            assert witness is not None
            return self._verified_persisted_binding(persisted_raw, witness)

    def denomination_binding(self) -> CampaignDenominationBinding | None:
        """Re-resolve only product-issued denomination with witnessed ancestry."""

        raw = self._persisted_payload()
        witness = self._issuance_witness()
        if raw is None:
            # A witness without the co-located cache is an incomplete crash prefix,
            # not positive authority. issue_denomination_binding() owns recovery.
            return None
        if witness is None:
            raise CampaignEconomicAuthorityError(
                "persisted campaign denomination lacks product issuance ancestry"
            )
        return self._verified_persisted_binding(raw, witness)

    def verify_denomination_binding(
        self, binding: CampaignDenominationBinding
    ) -> CampaignDenominationBinding:
        """Require an exact binding re-resolved from persisted product authority."""

        if type(binding) is not CampaignDenominationBinding:
            raise CampaignEconomicAuthorityError(
                "campaign denomination verification requires exact binding type"
            )
        expected = self.denomination_binding()
        if expected is None:
            raise CampaignEconomicAuthorityError(
                "campaign has no persisted product-issued denomination authority"
            )
        if binding != expected:
            raise CampaignEconomicAuthorityError(
                "campaign denomination binding does not match canonical run authority"
            )
        return expected
