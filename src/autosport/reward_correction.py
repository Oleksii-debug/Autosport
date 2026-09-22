"""Append-only reward correction chain and exact downstream invalidation evidence.

The ledger is structural only: a referenced settlement/correction source is not proven
merely because it is recorded here.  The module does not replay policy state, rewrite
historical rewards, activate a champion, or authorize provider/real-money behavior.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final


CORRECTION_SCHEMA: Final = "autosport.reward_correction_ledger"
CORRECTION_SCHEMA_VERSION: Final = 1


class RewardCorrectionError(ValueError):
    """Raised when correction/dependency evidence is invalid or conflicts."""


def _text(name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise RewardCorrectionError(f"{name} must be a non-empty canonical string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise RewardCorrectionError(f"{name} must be valid UTF-8 text") from exc
    return value


def _sha256(name: str, value: object) -> str:
    text = _text(name, value)
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise RewardCorrectionError(f"{name} must be lowercase SHA-256 hex")
    return text


def _instant(name: str, value: object) -> datetime:
    text = _text(name, value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RewardCorrectionError(f"{name} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RewardCorrectionError(f"{name} must be timezone-aware ISO-8601")
    return parsed


def _instant_id(name: str, value: object) -> str:
    return _instant(name, value).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _decimal(name: str, value: object) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise RewardCorrectionError(f"{name} must be a finite exact Decimal")
    return value


def _json(payload: object) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise RewardCorrectionError("correction payload is not canonical JSON") from exc


def _hash(payload: object) -> str:
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True, order=True)
class EvidenceRef:
    authority_family: str
    evidence_id: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        _text("authority_family", self.authority_family)
        _text("evidence_id", self.evidence_id)
        _sha256("evidence_sha256", self.evidence_sha256)

    def to_payload(self) -> dict[str, str]:
        return {
            "authority_family": self.authority_family,
            "evidence_id": self.evidence_id,
            "evidence_sha256": self.evidence_sha256,
        }


@dataclass(frozen=True, slots=True)
class DependencyArtifact:
    artifact: EvidenceRef
    dependencies: tuple[EvidenceRef, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.artifact, EvidenceRef):
            raise RewardCorrectionError("artifact must be EvidenceRef")
        if type(self.dependencies) is not tuple or not self.dependencies:
            raise RewardCorrectionError("dependencies must be a non-empty tuple")
        if any(not isinstance(item, EvidenceRef) for item in self.dependencies):
            raise RewardCorrectionError("dependencies must contain EvidenceRef values")
        if self.dependencies != tuple(sorted(self.dependencies)):
            raise RewardCorrectionError("dependencies must be sorted")
        keys = tuple((x.authority_family, x.evidence_id) for x in self.dependencies)
        if len(keys) != len(set(keys)):
            raise RewardCorrectionError("dependency identities must be unique")
        if (self.artifact.authority_family, self.artifact.evidence_id) in keys:
            raise RewardCorrectionError("artifact cannot depend on itself")

    def to_payload(self) -> dict[str, object]:
        return {
            "artifact": self.artifact.to_payload(),
            "dependencies": [item.to_payload() for item in self.dependencies],
        }

    @property
    def record_sha256(self) -> str:
        return _hash(self.to_payload())


@dataclass(frozen=True, slots=True)
class RewardCorrectionAssertion:
    action_id: str
    transition_id: str
    superseded_reward: EvidenceRef
    corrected_reward: EvidenceRef
    corrected_reward_value: Decimal
    corrected_available_at: str
    correction_source: EvidenceRef
    generation: int
    predecessor_correction_id: str | None = None

    def __post_init__(self) -> None:
        _sha256("action_id", self.action_id)
        _sha256("transition_id", self.transition_id)
        if not isinstance(self.superseded_reward, EvidenceRef):
            raise RewardCorrectionError("superseded_reward must be EvidenceRef")
        if not isinstance(self.corrected_reward, EvidenceRef):
            raise RewardCorrectionError("corrected_reward must be EvidenceRef")
        if self.superseded_reward == self.corrected_reward:
            raise RewardCorrectionError("correction must produce a distinct reward identity")
        if self.superseded_reward.authority_family != self.corrected_reward.authority_family:
            raise RewardCorrectionError("reward correction cannot change reward authority family")
        _decimal("corrected_reward_value", self.corrected_reward_value)
        _instant("corrected_available_at", self.corrected_available_at)
        if not isinstance(self.correction_source, EvidenceRef):
            raise RewardCorrectionError("correction_source must be EvidenceRef")
        if isinstance(self.generation, bool) or not isinstance(self.generation, int):
            raise RewardCorrectionError("generation must be an integer")
        if self.generation <= 0:
            raise RewardCorrectionError("generation must be positive")
        if self.generation == 1 and self.predecessor_correction_id is not None:
            raise RewardCorrectionError("first correction cannot have predecessor")
        if self.generation > 1:
            _sha256("predecessor_correction_id", self.predecessor_correction_id)

    def to_payload(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "transition_id": self.transition_id,
            "superseded_reward": self.superseded_reward.to_payload(),
            "corrected_reward": self.corrected_reward.to_payload(),
            "corrected_reward_value": str(self.corrected_reward_value),
            "corrected_available_at": _instant_id(
                "corrected_available_at", self.corrected_available_at
            ),
            "correction_source": self.correction_source.to_payload(),
            "generation": self.generation,
            "predecessor_correction_id": self.predecessor_correction_id,
        }

    @property
    def correction_id(self) -> str:
        return _hash(self.to_payload())


@dataclass(frozen=True, slots=True)
class RewardCorrectionReceipt:
    correction_id: str
    generation: int
    invalidated_artifacts: tuple[EvidenceRef, ...]
    source_origin_proven: bool = field(default=False, init=False)
    policy_replay_authorized: bool = field(default=False, init=False)
    promotion_authorized: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        _sha256("correction_id", self.correction_id)
        if isinstance(self.generation, bool) or not isinstance(self.generation, int) or self.generation <= 0:
            raise RewardCorrectionError("generation must be a positive integer")
        if type(self.invalidated_artifacts) is not tuple:
            raise RewardCorrectionError("invalidated_artifacts must be a tuple")
        if self.invalidated_artifacts != tuple(sorted(self.invalidated_artifacts)):
            raise RewardCorrectionError("invalidated_artifacts must be sorted")


_SCHEMA = """
CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE artifacts(
  authority_family TEXT NOT NULL,
  evidence_id TEXT NOT NULL,
  evidence_sha256 TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  record_sha256 TEXT NOT NULL,
  PRIMARY KEY(authority_family,evidence_id)
) WITHOUT ROWID;
CREATE TABLE corrections(
  correction_id TEXT PRIMARY KEY,
  action_id TEXT NOT NULL,
  transition_id TEXT NOT NULL,
  generation INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  UNIQUE(action_id,transition_id,generation)
) WITHOUT ROWID;
CREATE TABLE invalidations(
  correction_id TEXT NOT NULL,
  artifact_family TEXT NOT NULL,
  artifact_id TEXT NOT NULL,
  artifact_sha256 TEXT NOT NULL,
  PRIMARY KEY(correction_id,artifact_family,artifact_id)
) WITHOUT ROWID;
CREATE TRIGGER artifacts_no_update BEFORE UPDATE ON artifacts
BEGIN SELECT RAISE(ABORT,'reward correction artifacts are immutable'); END;
CREATE TRIGGER artifacts_no_delete BEFORE DELETE ON artifacts
BEGIN SELECT RAISE(ABORT,'reward correction artifacts are immutable'); END;
CREATE TRIGGER corrections_no_update BEFORE UPDATE ON corrections
BEGIN SELECT RAISE(ABORT,'reward corrections are immutable'); END;
CREATE TRIGGER corrections_no_delete BEFORE DELETE ON corrections
BEGIN SELECT RAISE(ABORT,'reward corrections are immutable'); END;
CREATE TRIGGER invalidations_no_update BEFORE UPDATE ON invalidations
BEGIN SELECT RAISE(ABORT,'reward invalidations are immutable'); END;
CREATE TRIGGER invalidations_no_delete BEFORE DELETE ON invalidations
BEGIN SELECT RAISE(ABORT,'reward invalidations are immutable'); END;
"""
_EXPECTED_TRIGGERS = {
    "artifacts_no_update", "artifacts_no_delete", "corrections_no_update",
    "corrections_no_delete", "invalidations_no_update", "invalidations_no_delete",
}


class RewardCorrectionLedger:
    """Durable append-only correction chain plus exact dependency invalidation graph."""

    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self.path = path
        self._connection = connection

    @classmethod
    def create(cls, path: str | Path) -> "RewardCorrectionLedger":
        path = Path(path)
        if path.exists():
            raise RewardCorrectionError("reward correction ledger already exists")
        if not path.parent.is_dir():
            raise RewardCorrectionError("reward correction ledger parent must already exist")
        connection = cls._connect(path)
        try:
            connection.executescript(_SCHEMA)
            connection.executemany(
                "INSERT INTO metadata(key,value) VALUES (?,?)",
                (("schema", CORRECTION_SCHEMA), ("schema_version", str(CORRECTION_SCHEMA_VERSION))),
            )
            ledger = cls(path, connection)
            ledger.verify_integrity()
            return ledger
        except Exception:
            connection.close()
            path.unlink(missing_ok=True)
            raise

    @classmethod
    def open(cls, path: str | Path) -> "RewardCorrectionLedger":
        path = Path(path)
        if not path.is_file():
            raise RewardCorrectionError("reward correction ledger does not exist")
        connection = cls._connect(path)
        try:
            ledger = cls(path, connection)
            ledger.verify_integrity()
            return ledger
        except Exception:
            connection.close()
            raise

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(str(path), timeout=0.25, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=250")
            return connection
        except sqlite3.Error as exc:
            raise RewardCorrectionError("cannot open reward correction ledger") from exc

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "RewardCorrectionLedger":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def append_artifact(self, node: DependencyArtifact) -> EvidenceRef:
        if not isinstance(node, DependencyArtifact):
            raise TypeError("node must be DependencyArtifact")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            existing = self._connection.execute(
                "SELECT evidence_sha256,record_sha256 FROM artifacts "
                "WHERE authority_family=? AND evidence_id=?",
                (node.artifact.authority_family, node.artifact.evidence_id),
            ).fetchone()
            if existing is not None:
                if existing["evidence_sha256"] != node.artifact.evidence_sha256 or existing["record_sha256"] != node.record_sha256:
                    raise RewardCorrectionError("artifact identity is already bound differently")
                self._connection.execute("COMMIT")
                return node.artifact
            for dependency in node.dependencies:
                if self._was_superseded(dependency):
                    raise RewardCorrectionError("artifact cannot depend on a superseded reward")
                if self.is_invalidated(dependency):
                    raise RewardCorrectionError("artifact cannot depend on invalidated evidence")
                row = self._connection.execute(
                    "SELECT evidence_sha256 FROM artifacts WHERE authority_family=? AND evidence_id=?",
                    (dependency.authority_family, dependency.evidence_id),
                ).fetchone()
                if row is not None and row["evidence_sha256"] != dependency.evidence_sha256:
                    raise RewardCorrectionError("dependency digest conflicts with registered artifact")
            self._connection.execute(
                "INSERT INTO artifacts VALUES (?,?,?,?,?)",
                (node.artifact.authority_family, node.artifact.evidence_id,
                 node.artifact.evidence_sha256, _json(node.to_payload()), node.record_sha256),
            )
            self._connection.execute("COMMIT")
            return node.artifact
        except RewardCorrectionError:
            self._rollback(); raise
        except sqlite3.Error as exc:
            self._rollback()
            raise RewardCorrectionError("cannot append dependency artifact") from exc

    def append_correction(self, assertion: RewardCorrectionAssertion) -> RewardCorrectionReceipt:
        if not isinstance(assertion, RewardCorrectionAssertion):
            raise TypeError("assertion must be RewardCorrectionAssertion")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            existing = self._connection.execute(
                "SELECT payload_json FROM corrections WHERE correction_id=?",
                (assertion.correction_id,),
            ).fetchone()
            if existing is not None:
                if existing["payload_json"] != _json(assertion.to_payload()):
                    raise RewardCorrectionError("correction identity payload is corrupt")
                result = self._receipt(assertion)
                self._connection.execute("COMMIT")
                return result
            self._validate_chain(assertion)
            self._connection.execute(
                "INSERT INTO corrections VALUES (?,?,?,?,?)",
                (assertion.correction_id, assertion.action_id, assertion.transition_id,
                 assertion.generation, _json(assertion.to_payload())),
            )
            invalidated = self._derive_invalidation(assertion.superseded_reward)
            self._connection.executemany(
                "INSERT INTO invalidations VALUES (?,?,?,?)",
                ((assertion.correction_id, x.authority_family, x.evidence_id, x.evidence_sha256)
                 for x in invalidated),
            )
            self._connection.execute("COMMIT")
            return RewardCorrectionReceipt(assertion.correction_id, assertion.generation, invalidated)
        except RewardCorrectionError:
            self._rollback(); raise
        except sqlite3.IntegrityError as exc:
            self._rollback()
            raise RewardCorrectionError("correction generation conflicts with durable history") from exc
        except sqlite3.Error as exc:
            self._rollback()
            raise RewardCorrectionError("cannot append reward correction") from exc

    def latest_correction(self, *, action_id: str, transition_id: str) -> RewardCorrectionAssertion | None:
        row = self._connection.execute(
            "SELECT payload_json FROM corrections WHERE action_id=? AND transition_id=? "
            "ORDER BY generation DESC LIMIT 1",
            (_sha256("action_id", action_id), _sha256("transition_id", transition_id)),
        ).fetchone()
        return None if row is None else _correction_from_json(row["payload_json"])

    def is_invalidated(self, artifact: EvidenceRef) -> bool:
        if not isinstance(artifact, EvidenceRef):
            raise TypeError("artifact must be EvidenceRef")
        return self._connection.execute(
            "SELECT 1 FROM invalidations WHERE artifact_family=? AND artifact_id=? "
            "AND artifact_sha256=? LIMIT 1",
            (artifact.authority_family, artifact.evidence_id, artifact.evidence_sha256),
        ).fetchone() is not None

    def verify_integrity(self) -> None:
        try:
            metadata = dict(self._connection.execute("SELECT key,value FROM metadata").fetchall())
            if metadata != {"schema": CORRECTION_SCHEMA, "schema_version": str(CORRECTION_SCHEMA_VERSION)}:
                raise RewardCorrectionError("reward correction metadata mismatch")
            triggers = {row["name"] for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()}
            if triggers != _EXPECTED_TRIGGERS:
                raise RewardCorrectionError("reward correction immutability triggers mismatch")
            nodes = self._nodes()
            registered = {(n.artifact.authority_family, n.artifact.evidence_id): n.artifact for n in nodes}
            for node in nodes:
                row = self._connection.execute(
                    "SELECT record_sha256,payload_json FROM artifacts WHERE authority_family=? AND evidence_id=?",
                    (node.artifact.authority_family, node.artifact.evidence_id),
                ).fetchone()
                if row["record_sha256"] != node.record_sha256 or row["payload_json"] != _json(node.to_payload()):
                    raise RewardCorrectionError("artifact dependency record digest mismatch")
                for dependency in node.dependencies:
                    known = registered.get((dependency.authority_family, dependency.evidence_id))
                    if known is not None and known != dependency:
                        raise RewardCorrectionError("registered dependency digest mismatch")
            previous: dict[tuple[str, str], RewardCorrectionAssertion] = {}
            for row in self._connection.execute(
                "SELECT payload_json FROM corrections ORDER BY action_id,transition_id,generation"
            ).fetchall():
                current = _correction_from_json(row["payload_json"])
                key = (current.action_id, current.transition_id)
                prior = previous.get(key)
                if prior is None:
                    if current.generation != 1 or current.predecessor_correction_id is not None:
                        raise RewardCorrectionError("correction chain must start at generation 1")
                elif (current.generation != prior.generation + 1
                      or current.predecessor_correction_id != prior.correction_id
                      or current.superseded_reward != prior.corrected_reward
                      or _instant("current", current.corrected_available_at) < _instant("prior", prior.corrected_available_at)):
                    raise RewardCorrectionError("correction chain integrity mismatch")
                if self._invalidated(current.correction_id) != self._derive_invalidation(current.superseded_reward):
                    raise RewardCorrectionError("correction invalidation closure mismatch")
                previous[key] = current
        except sqlite3.Error as exc:
            raise RewardCorrectionError("reward correction ledger is unreadable") from exc

    def _validate_chain(self, current: RewardCorrectionAssertion) -> None:
        prior = self.latest_correction(action_id=current.action_id, transition_id=current.transition_id)
        if prior is None:
            if current.generation != 1 or current.predecessor_correction_id is not None:
                raise RewardCorrectionError("first durable correction must be generation 1")
            return
        if current.generation != prior.generation + 1:
            raise RewardCorrectionError("correction generation must advance exactly once")
        if current.predecessor_correction_id != prior.correction_id:
            raise RewardCorrectionError("correction predecessor does not match latest generation")
        if current.superseded_reward != prior.corrected_reward:
            raise RewardCorrectionError("next correction must supersede the prior corrected reward")
        if _instant("current", current.corrected_available_at) < _instant("prior", prior.corrected_available_at):
            raise RewardCorrectionError("correction availability cannot move backward")

    def _nodes(self) -> tuple[DependencyArtifact, ...]:
        return tuple(_artifact_from_json(row["payload_json"]) for row in self._connection.execute(
            "SELECT payload_json FROM artifacts ORDER BY authority_family,evidence_id"
        ).fetchall())

    def _derive_invalidation(self, root: EvidenceRef) -> tuple[EvidenceRef, ...]:
        reverse: dict[EvidenceRef, list[EvidenceRef]] = {}
        for node in self._nodes():
            for dependency in node.dependencies:
                reverse.setdefault(dependency, []).append(node.artifact)
        pending = [root]
        invalid: set[EvidenceRef] = set()
        while pending:
            dependency = pending.pop()
            for child in reverse.get(dependency, ()):
                if child not in invalid:
                    invalid.add(child); pending.append(child)
        return tuple(sorted(invalid))

    def _invalidated(self, correction_id: str) -> tuple[EvidenceRef, ...]:
        rows = self._connection.execute(
            "SELECT artifact_family,artifact_id,artifact_sha256 FROM invalidations "
            "WHERE correction_id=? ORDER BY artifact_family,artifact_id,artifact_sha256",
            (_sha256("correction_id", correction_id),),
        ).fetchall()
        return tuple(EvidenceRef(row[0], row[1], row[2]) for row in rows)

    def _receipt(self, assertion: RewardCorrectionAssertion) -> RewardCorrectionReceipt:
        return RewardCorrectionReceipt(
            assertion.correction_id, assertion.generation, self._invalidated(assertion.correction_id)
        )

    def _was_superseded(self, ref: EvidenceRef) -> bool:
        for row in self._connection.execute("SELECT payload_json FROM corrections").fetchall():
            if _correction_from_json(row["payload_json"]).superseded_reward == ref:
                return True
        return False

    def _rollback(self) -> None:
        try:
            self._connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass


def _ref_from(value: object) -> EvidenceRef:
    if type(value) is not dict or set(value) != {"authority_family", "evidence_id", "evidence_sha256"}:
        raise RewardCorrectionError("evidence reference payload fields mismatch")
    return EvidenceRef(value["authority_family"], value["evidence_id"], value["evidence_sha256"])


def _artifact_from_json(raw: str) -> DependencyArtifact:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RewardCorrectionError("artifact payload is invalid JSON") from exc
    if type(value) is not dict or set(value) != {"artifact", "dependencies"} or type(value["dependencies"]) is not list:
        raise RewardCorrectionError("artifact payload fields mismatch")
    node = DependencyArtifact(_ref_from(value["artifact"]), tuple(_ref_from(x) for x in value["dependencies"]))
    if _json(node.to_payload()) != raw:
        raise RewardCorrectionError("artifact payload is not canonical")
    return node


def _correction_from_json(raw: str) -> RewardCorrectionAssertion:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RewardCorrectionError("correction payload is invalid JSON") from exc
    expected = {"action_id", "transition_id", "superseded_reward", "corrected_reward",
                "corrected_reward_value", "corrected_available_at", "correction_source",
                "generation", "predecessor_correction_id"}
    if type(value) is not dict or set(value) != expected or type(value["corrected_reward_value"]) is not str:
        raise RewardCorrectionError("correction payload fields mismatch")
    try:
        reward = Decimal(value["corrected_reward_value"])
    except InvalidOperation as exc:
        raise RewardCorrectionError("corrected reward Decimal text is invalid") from exc
    if not reward.is_finite() or str(reward) != value["corrected_reward_value"]:
        raise RewardCorrectionError("corrected reward Decimal text is not canonical")
    assertion = RewardCorrectionAssertion(
        action_id=value["action_id"], transition_id=value["transition_id"],
        superseded_reward=_ref_from(value["superseded_reward"]),
        corrected_reward=_ref_from(value["corrected_reward"]), corrected_reward_value=reward,
        corrected_available_at=value["corrected_available_at"],
        correction_source=_ref_from(value["correction_source"]), generation=value["generation"],
        predecessor_correction_id=value["predecessor_correction_id"],
    )
    if _json(assertion.to_payload()) != raw:
        raise RewardCorrectionError("correction payload is not canonical")
    return assertion
