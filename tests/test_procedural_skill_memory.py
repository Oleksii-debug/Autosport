from __future__ import annotations

import hashlib
import json

import pytest

from autosport.procedural_skill_memory import (
    SCHEMA,
    SCHEMA_VERSION,
    ConflictingProceduralSkillVersionError,
    ProceduralSkillAuthorityError,
    ProceduralSkillMemory,
    ProceduralSkillMemoryError,
    ProceduralSkillMemoryVersion,
)


H1 = "1" * 64
H2 = "2" * 64
H3 = "3" * 64
H4 = "4" * 64
H5 = "5" * 64
H6 = "6" * 64
H7 = "7" * 64


def _version(
    *,
    version: str = "1.0.0",
    available_at: str = "2026-09-22T10:00:00Z",
    domain: tuple[str, ...] = ("market:match-odds", "sport:football"),
    predecessor_id: str | None = None,
    procedure_sha256: str = H1,
    contract_sha256: str = H2,
    owner: str = H3,
    risk: str = H4,
    economic: str = H5,
    synthetic: bool = False,
) -> ProceduralSkillMemoryVersion:
    return ProceduralSkillMemoryVersion(
        skill_key="research.calibration.inspect",
        skill_version=version,
        procedure_sha256=procedure_sha256,
        contract_sha256=contract_sha256,
        available_at=available_at,
        validity_domain=domain,
        learned_from=("experiment:exp-1", "postmortem:pm-1"),
        provenance=(("dataset", H6), ("evaluation", H7)),
        owner_authority_sha256=owner,
        risk_authority_sha256=risk,
        economic_goal_sha256=economic,
        synthetic=synthetic,
        predecessor_id=predecessor_id,
    )


def _rewrite_state(path, mutate):
    raw = json.loads(path.read_text(encoding="utf-8"))
    mutate(raw)
    without = {key: value for key, value in raw.items() if key != "state_sha256"}
    raw["state_sha256"] = hashlib.sha256(
        json.dumps(
            without,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    path.write_text(
        json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def test_initialize_publish_restart_and_causal_as_of(tmp_path):
    path = tmp_path / "procedural-memory.json"
    memory = ProceduralSkillMemory.initialize(path)
    first = _version()
    assert memory.publish(first) == first.version_id
    restarted = ProceduralSkillMemory(path)
    assert restarted.as_of(
        skill_key=first.skill_key, as_of="2026-09-22T09:59:59Z"
    ) is None
    selected = restarted.as_of(
        skill_key=first.skill_key, as_of="2026-09-22T10:00:00Z"
    )
    assert selected == first
    assert selected.execution_authorized is False
    assert restarted.state_sha256 == memory.state_sha256


def test_available_at_is_canonical_before_persistence_and_stable_after_restart(tmp_path):
    path = tmp_path / "procedural-memory.json"
    memory = ProceduralSkillMemory.initialize(path)
    version = _version(available_at="2026-09-22T12:00:00+02:00")

    assert version.available_at == "2026-09-22T10:00:00Z"
    memory.publish(version)

    restarted = ProceduralSkillMemory(path)
    assert restarted.all_versions() == (version,)
    assert restarted.as_of(
        skill_key=version.skill_key,
        as_of="2026-09-22T10:00:00Z",
    ) == version


def test_successor_is_immutable_versioned_and_latest_as_of(tmp_path):
    memory = ProceduralSkillMemory.initialize(tmp_path / "memory.json")
    first = _version()
    memory.publish(first)
    second = _version(
        version="1.1.0",
        available_at="2026-09-22T11:00:00Z",
        domain=("sport:football",),
        predecessor_id=first.version_id,
        procedure_sha256=H2,
        contract_sha256=H3,
    )
    memory.publish(second)
    assert memory.as_of(
        skill_key=first.skill_key, as_of="2026-09-22T10:30:00Z"
    ) == first
    assert memory.as_of(
        skill_key=first.skill_key, as_of="2026-09-22T11:00:00Z"
    ) == second
    assert memory.all_versions(skill_key=first.skill_key) == (first, second)


def test_validity_domain_cannot_expand_in_successor(tmp_path):
    memory = ProceduralSkillMemory.initialize(tmp_path / "memory.json")
    first = _version(domain=("sport:football",))
    memory.publish(first)
    expanded = _version(
        version="2.0.0",
        available_at="2026-09-22T11:00:00Z",
        domain=("market:match-odds", "sport:football"),
        predecessor_id=first.version_id,
        procedure_sha256=H2,
    )
    with pytest.raises(ProceduralSkillMemoryError, match="cannot expand"):
        memory.publish(expanded)


def test_successor_must_extend_latest_version(tmp_path):
    memory = ProceduralSkillMemory.initialize(tmp_path / "memory.json")
    first = _version()
    memory.publish(first)
    second = _version(
        version="1.1.0",
        available_at="2026-09-22T11:00:00Z",
        domain=("sport:football",),
        predecessor_id=first.version_id,
        procedure_sha256=H2,
    )
    memory.publish(second)
    branch = _version(
        version="1.2.0",
        available_at="2026-09-22T12:00:00Z",
        domain=("sport:football",),
        predecessor_id=first.version_id,
        procedure_sha256=H3,
    )
    with pytest.raises(ProceduralSkillMemoryError, match="latest"):
        memory.publish(branch)


def test_successor_available_at_must_strictly_advance(tmp_path):
    memory = ProceduralSkillMemory.initialize(tmp_path / "memory.json")
    first = _version()
    memory.publish(first)
    same_time = _version(
        version="1.1.0",
        available_at=first.available_at,
        domain=("sport:football",),
        predecessor_id=first.version_id,
        procedure_sha256=H2,
    )
    with pytest.raises(ProceduralSkillMemoryError, match="must advance"):
        memory.publish(same_time)


def test_same_skill_version_conflict_is_rejected(tmp_path):
    memory = ProceduralSkillMemory.initialize(tmp_path / "memory.json")
    first = _version()
    memory.publish(first)
    conflict = _version(procedure_sha256=H2)
    with pytest.raises(ConflictingProceduralSkillVersionError):
        memory.publish(conflict)


def test_exact_replay_is_idempotent(tmp_path):
    memory = ProceduralSkillMemory.initialize(tmp_path / "memory.json")
    first = _version()
    assert memory.publish(first) == first.version_id
    before = memory.state_sha256
    assert memory.publish(first) == first.version_id
    assert memory.state_sha256 == before
    assert memory.all_versions() == (first,)


def test_resolve_exact_binds_identity_version_domain_and_authorities(tmp_path):
    memory = ProceduralSkillMemory.initialize(tmp_path / "memory.json")
    first = _version()
    memory.publish(first)
    resolved = memory.resolve_exact(
        skill_key=first.skill_key,
        skill_version=first.skill_version,
        version_id=first.version_id,
        as_of=first.available_at,
        required_domain=("sport:football",),
        owner_authority_sha256=H3,
        risk_authority_sha256=H4,
        economic_goal_sha256=H5,
    )
    assert resolved == first
    assert resolved.execution_authorized is False

    with pytest.raises(ProceduralSkillAuthorityError):
        memory.resolve_exact(
            skill_key=first.skill_key,
            skill_version=first.skill_version,
            version_id=first.version_id,
            as_of=first.available_at,
            required_domain=("sport:football",),
            owner_authority_sha256=H6,
            risk_authority_sha256=H4,
            economic_goal_sha256=H5,
        )

    with pytest.raises(ProceduralSkillMemoryError, match="outside requested"):
        memory.resolve_exact(
            skill_key=first.skill_key,
            skill_version=first.skill_version,
            version_id=first.version_id,
            as_of=first.available_at,
            required_domain=("sport:tennis",),
            owner_authority_sha256=H3,
            risk_authority_sha256=H4,
            economic_goal_sha256=H5,
        )


def test_future_memory_cannot_be_resolved_at_earlier_cutoff(tmp_path):
    memory = ProceduralSkillMemory.initialize(tmp_path / "memory.json")
    first = _version(available_at="2026-09-22T12:00:00Z")
    memory.publish(first)
    with pytest.raises(ProceduralSkillMemoryError, match="not available"):
        memory.resolve_exact(
            skill_key=first.skill_key,
            skill_version=first.skill_version,
            version_id=first.version_id,
            as_of="2026-09-22T11:59:59Z",
            required_domain=("sport:football",),
            owner_authority_sha256=H3,
            risk_authority_sha256=H4,
            economic_goal_sha256=H5,
        )


def test_synthetic_marker_is_identity_bound_and_persists_restart(tmp_path):
    path = tmp_path / "memory.json"
    memory = ProceduralSkillMemory.initialize(path)
    synthetic = _version(synthetic=True)
    memory.publish(synthetic)
    restarted = ProceduralSkillMemory(path)
    loaded = restarted.all_versions()[0]
    assert loaded.synthetic is True
    assert loaded.version_id == synthetic.version_id


def test_constructor_rejects_any_execution_authority():
    with pytest.raises(ProceduralSkillAuthorityError):
        ProceduralSkillMemoryVersion(
            skill_key="research.skill",
            skill_version="1",
            procedure_sha256=H1,
            contract_sha256=H2,
            available_at="2026-09-22T10:00:00Z",
            validity_domain=("sport:football",),
            learned_from=("experiment:x",),
            provenance=(("dataset", H3),),
            owner_authority_sha256=H4,
            risk_authority_sha256=H5,
            economic_goal_sha256=H6,
            execution_authorized=True,
        )


@pytest.mark.parametrize("alias", [True, 1.0])
def test_restart_rejects_schema_version_type_aliases_even_with_valid_state_hash(
    tmp_path, alias
):
    path = tmp_path / "memory.json"
    ProceduralSkillMemory.initialize(path)
    _rewrite_state(path, lambda state: state.__setitem__("schema_version", alias))
    with pytest.raises(ProceduralSkillMemoryError, match="schema version"):
        ProceduralSkillMemory(path)


def test_restart_rejects_missing_predecessor_even_with_recomputed_hash(tmp_path):
    path = tmp_path / "memory.json"
    memory = ProceduralSkillMemory.initialize(path)
    first = _version()
    memory.publish(first)
    second = _version(
        version="1.1.0",
        available_at="2026-09-22T11:00:00Z",
        domain=("sport:football",),
        predecessor_id=first.version_id,
        procedure_sha256=H2,
    )
    memory.publish(second)

    def mutate(state):
        state["versions"] = [state["versions"][1]]

    _rewrite_state(path, mutate)
    with pytest.raises(ProceduralSkillMemoryError, match="predecessor"):
        ProceduralSkillMemory(path)


def test_restart_rejects_authority_or_domain_tamper_even_with_recomputed_hash(tmp_path):
    path = tmp_path / "memory.json"
    memory = ProceduralSkillMemory.initialize(path)
    first = _version(domain=("sport:football",))
    memory.publish(first)
    second = _version(
        version="1.1.0",
        available_at="2026-09-22T11:00:00Z",
        domain=("sport:football",),
        predecessor_id=first.version_id,
        procedure_sha256=H2,
    )
    memory.publish(second)

    def mutate(state):
        state["versions"][1]["validity_domain"] = [
            "market:match-odds",
            "sport:football",
        ]
        row = dict(state["versions"][1])
        row.pop("version_id")
        state["versions"][1]["version_id"] = hashlib.sha256(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "schema_version": SCHEMA_VERSION,
                    "kind": "ProceduralSkillMemoryVersion",
                    **row,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()

    _rewrite_state(path, mutate)
    with pytest.raises(ProceduralSkillMemoryError, match="cannot expand"):
        ProceduralSkillMemory(path)


def test_unordered_domain_learned_from_or_provenance_is_rejected():
    with pytest.raises(ProceduralSkillMemoryError, match="sorted"):
        _version(domain=("sport:football", "market:match-odds"))
    with pytest.raises(ProceduralSkillMemoryError, match="learned_from"):
        ProceduralSkillMemoryVersion(
            skill_key="research.skill",
            skill_version="1",
            procedure_sha256=H1,
            contract_sha256=H2,
            available_at="2026-09-22T10:00:00Z",
            validity_domain=("sport:football",),
            learned_from=("z", "a"),
            provenance=(("dataset", H3),),
            owner_authority_sha256=H4,
            risk_authority_sha256=H5,
            economic_goal_sha256=H6,
        )
    with pytest.raises(ProceduralSkillMemoryError, match="provenance"):
        ProceduralSkillMemoryVersion(
            skill_key="research.skill",
            skill_version="1",
            procedure_sha256=H1,
            contract_sha256=H2,
            available_at="2026-09-22T10:00:00Z",
            validity_domain=("sport:football",),
            learned_from=("x",),
            provenance=(("z", H3), ("a", H4)),
            owner_authority_sha256=H4,
            risk_authority_sha256=H5,
            economic_goal_sha256=H6,
        )
