"""Plan-5 Windows lab: fail-closed, *nonexecuting* controller/agent contract.

This boundary cannot dispatch arbitrary jobs, access a bookmaker or validate a
human/NVDA result. Only a separately administered low-privilege, ephemeral VM
may execute the fixed scenarios. Lab observations are untrusted until externally
attested; they are NEVER release, financial or physical-NVDA authority.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

_REPOSITORY = "Oleksii-debug/Autosport"
_SCHEMA = "autosport.windows_lab_contract"
_VERSION = 1
_PROFILE = "dedicated_ephemeral_nonadmin_vm_no_host_mounts_or_credentials"
_SCENARIOS = (
    "desktop_start_stop",
    "keyboard_semantic_navigation",
    "native_emergency_stop",
    "packaged_restart_recovery",
    "browser_semantic_readback",
)
_STATUSES = frozenset(("PASS", "FAIL", "WAIT"))
_MAX_BYTES = 16_384


class WindowsLabContractError(ValueError):
    """Safe, non-secret failure at the trust boundary."""


def _hex(value: object, length: int) -> bool:
    return (
        type(value) is str
        and len(value) == length
        and all(ch in "0123456789abcdef" for ch in value)
    )


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("ascii")).hexdigest()


def _pairs_no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise WindowsLabContractError("duplicate JSON property")
        result[key] = value
    return result


def _decode(text: object) -> dict[str, object]:
    if type(text) is not str or len(text.encode("utf-8", errors="replace")) > _MAX_BYTES:
        raise WindowsLabContractError("invalid or oversized lab document")
    failed = False
    try:
        value = json.loads(
            text, object_pairs_hook=_pairs_no_duplicates,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except Exception:
        failed = True
    if failed:
        # Never surface an exception carrying arbitrary input, secrets or paths.
        raise WindowsLabContractError("malformed lab document")
    if type(value) is not dict:
        raise WindowsLabContractError("lab document must be an object")
    return value


@dataclass(frozen=True, slots=True)
class WindowsLabTicket:
    source_sha: str
    package_sha256: str
    scenarios: tuple[str, ...]
    dispatch_event: str = "workflow_dispatch"
    dispatch_ref: str = "refs/heads/main"
    repository: str = _REPOSITORY
    runner_profile: str = _PROFILE
    execution_authority: bool = False
    human_tested: bool = False
    nvda_verified: bool = False
    target_machine_acceptance: bool = False

    def __post_init__(self) -> None:
        if not _hex(self.source_sha, 40) or not _hex(self.package_sha256, 64):
            raise WindowsLabContractError("invalid exact source or package digest")
        if (
            type(self.repository) is not str or self.repository != _REPOSITORY
            or type(self.dispatch_event) is not str or self.dispatch_event != "workflow_dispatch"
            or type(self.dispatch_ref) is not str or self.dispatch_ref != "refs/heads/main"
            or type(self.runner_profile) is not str or self.runner_profile != _PROFILE
        ):
            raise WindowsLabContractError("untrusted lab dispatch or isolation boundary")
        if (
            type(self.scenarios) is not tuple
            or not self.scenarios
            or self.scenarios != tuple(s for s in _SCENARIOS if s in self.scenarios)
        ):
            raise WindowsLabContractError("unknown, duplicate or unordered lab scenario")
        if any(type(s) is not str for s in self.scenarios):
            raise WindowsLabContractError("invalid lab scenario type")
        if any(
            getattr(self, name) is not False
            for name in (
                "execution_authority", "human_tested",
                "nvda_verified", "target_machine_acceptance",
            )
        ):
            raise WindowsLabContractError("lab ticket cannot grant authority")

    @property
    def ticket_id(self) -> str:
        return _digest(self._identity())

    def _identity(self) -> dict[str, object]:
        return {
            "repository": self.repository,
            "source_sha": self.source_sha,
            "package_sha256": self.package_sha256,
            "dispatch_event": self.dispatch_event,
            "dispatch_ref": self.dispatch_ref,
            "runner_profile": self.runner_profile,
            "scenarios": list(self.scenarios),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": _SCHEMA,
            "schema_version": _VERSION,
            "kind": "ticket",
            **self._identity(),
            "ticket_id": self.ticket_id,
            "execution_authority": False,
            "human_tested": False,
            "nvda_verified": False,
            "target_machine_acceptance": False,
        }

    def to_json(self) -> str:
        return _canonical(self.to_dict())

    @classmethod
    def from_json(cls, text: str) -> "WindowsLabTicket":
        raw = _decode(text)
        keys = set(cls("a" * 40, "b" * 64, (_SCENARIOS[0],)).to_dict())
        if set(raw) != keys or raw.get("schema") != _SCHEMA or type(raw.get("schema_version")) is not int or raw.get("schema_version") != _VERSION or raw.get("kind") != "ticket":
            raise WindowsLabContractError("unsupported lab ticket schema")
        try:
            ticket = cls(
                source_sha=raw["source_sha"],
                package_sha256=raw["package_sha256"],
                scenarios=tuple(raw["scenarios"]) if type(raw["scenarios"]) is list else raw["scenarios"],
                dispatch_event=raw["dispatch_event"],
                dispatch_ref=raw["dispatch_ref"],
                repository=raw["repository"],
                runner_profile=raw["runner_profile"],
                execution_authority=raw["execution_authority"],
                human_tested=raw["human_tested"],
                nvda_verified=raw["nvda_verified"],
                target_machine_acceptance=raw["target_machine_acceptance"],
            )
        except (TypeError, ValueError, KeyError):
            raise WindowsLabContractError("invalid lab ticket fields") from None
        if raw != ticket.to_dict():
            raise WindowsLabContractError("lab ticket identity mismatch")
        return ticket


@dataclass(frozen=True, slots=True)
class WindowsLabObservation:
    scenario: str
    status: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.scenario) is not str or self.scenario not in _SCENARIOS
            or type(self.status) is not str or self.status not in _STATUSES
            or not _hex(self.evidence_sha256, 64)
        ):
            raise WindowsLabContractError("invalid untrusted agent observation")

    def to_dict(self) -> dict[str, str]:
        return {
            "scenario": self.scenario,
            "status": self.status,
            "evidence_sha256": self.evidence_sha256,
        }


@dataclass(frozen=True, slots=True)
class WindowsLabCampaign:
    ticket: WindowsLabTicket
    observations: tuple[WindowsLabObservation, ...] = ()

    def __post_init__(self) -> None:
        if type(self.ticket) is not WindowsLabTicket or type(self.observations) is not tuple:
            raise WindowsLabContractError("invalid lab campaign")
        observed: set[str] = set()
        for item in self.observations:
            if type(item) is not WindowsLabObservation or item.scenario not in self.ticket.scenarios:
                raise WindowsLabContractError("observation outside ticket scope")
            if item.scenario in observed:
                raise WindowsLabContractError("duplicate scenario effect")
            observed.add(item.scenario)
        if tuple(s for s in self.ticket.scenarios if s in observed) != tuple(
            item.scenario for item in self.observations
        ):
            raise WindowsLabContractError("noncanonical observation chronology")

    def admit(self, observation: WindowsLabObservation) -> "WindowsLabCampaign":
        if type(observation) is not WindowsLabObservation:
            raise WindowsLabContractError("invalid agent observation")
        for old in self.observations:
            if old.scenario == observation.scenario:
                if old == observation:
                    return self  # Restart / at-least-once delivery: no duplicate.
                raise WindowsLabContractError("conflicting replay requires independent reconciliation")
        if observation.scenario not in self.ticket.scenarios:
            raise WindowsLabContractError("observation outside ticket scope")
        expected = self.ticket.scenarios[len(self.observations)]
        if observation.scenario != expected:
            raise WindowsLabContractError("scenario ordering prevents missing prerequisites")
        return WindowsLabCampaign(self.ticket, self.observations + (observation,))

    @property
    def disposition(self) -> str:
        # Even a full set of PASS observations is NOT independently attested.
        return "UNVERIFIED_AGENT_EVIDENCE"

    @property
    def source_bound_sha256(self) -> str:
        return _digest(self._payload())

    def _payload(self) -> dict[str, object]:
        return {
            "ticket": self.ticket.to_dict(),
            "observations": [v.to_dict() for v in self.observations],
        }

    def to_json(self) -> str:
        return _canonical({
            "schema": _SCHEMA,
            "schema_version": _VERSION,
            "kind": "campaign",
            **self._payload(),
            "source_bound_sha256": self.source_bound_sha256,
            "disposition": self.disposition,
            "execution_authority": False,
            "human_tested": False,
            "nvda_verified": False,
            "target_machine_acceptance": False,
        })

    @classmethod
    def from_json(cls, text: str) -> "WindowsLabCampaign":
        raw = _decode(text)
        try:
            ticket = WindowsLabTicket.from_json(_canonical(raw["ticket"]))
            rows = raw["observations"]
            if type(rows) is not list or len(rows) > len(ticket.scenarios):
                raise WindowsLabContractError("unbounded lab observations")
            values = []
            for row in rows:
                if type(row) is not dict or set(row) != {"scenario", "status", "evidence_sha256"}:
                    raise WindowsLabContractError("malformed lab observation")
                values.append(WindowsLabObservation(**row))
            campaign = cls(ticket, tuple(values))
        except (KeyError, TypeError, ValueError):
            raise WindowsLabContractError("invalid lab campaign fields") from None
        if _canonical(raw) != campaign.to_json():
            raise WindowsLabContractError("campaign source, state or authority mismatch")
        return campaign


def source_level_lab_ticket(source_sha: str, package_sha256: str) -> WindowsLabTicket:
    """Make a fixed-scope controller ticket, never a job execution instruction."""
    return WindowsLabTicket(source_sha, package_sha256, _SCENARIOS)
