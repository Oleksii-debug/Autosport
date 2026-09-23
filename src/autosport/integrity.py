from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterator

from .monotonic_workspace_authority import AuthorityPhase, MonotonicWorkspaceAuthority

if os.name == "nt":
    import msvcrt
else:
    import fcntl


# NamedTemporaryFile isolates serialization for concurrent writers. The final
# publication still needs a durable per-path writer fence: otherwise a canonical
# source writer and a derived-snapshot writer can each publish a whole-file image
# built from different generations and silently erase the other's update.
_ATOMIC_JSON_PUBLISH_LOCK = threading.Lock()
_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCK_LOCAL = threading.local()

_SCIENTIFIC_REGISTRY_AUTHORITY_DOMAIN = "autosport.scientific-registry.v1"
_SCIENTIFIC_REGISTRY_ENTRY_KEYS = frozenset(
    {
        "record_type",
        "record_id",
        "available_at",
        "payload",
        "record_sha256",
    }
)


def _resolved_key(path: Path) -> str:
    try:
        return str(path.resolve(strict=False))
    except OSError:
        return str(path.absolute())


def _thread_lock_for(path: Path) -> threading.RLock:
    key = _resolved_key(path)
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


def _lock_handle(handle) -> None:
    if os.name == "nt":
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_handle(handle) -> None:
    if os.name == "nt":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def durable_path_lock(path: str | Path) -> Iterator[None]:
    """Serialize canonical read/modify/write publication for one durable path.

    The sidecar lock is intentionally persistent. Removing a lock file after
    release can split future lockers across different inodes. A per-process
    re-entrant guard makes nested ``atomic_write_json`` calls safe while the OS
    lock provides the cross-process fence required by Windows product runtimes.
    """

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    key = _resolved_key(destination)
    thread_lock = _thread_lock_for(destination)

    with thread_lock:
        held = getattr(_PATH_LOCK_LOCAL, "held", None)
        if held is None:
            held = {}
            _PATH_LOCK_LOCAL.held = held
        current = held.get(key)
        if current is not None:
            current[0] += 1
            try:
                yield
            finally:
                current[0] -= 1
            return

        lock_path = destination.with_name(f".{destination.name}.lock")
        handle = lock_path.open("a+b")
        try:
            _lock_handle(handle)
            held[key] = [1, handle]
            try:
                yield
            finally:
                del held[key]
                _unlock_handle(handle)
        finally:
            handle.close()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_durable_file(path: str | Path) -> None:
    """Create an empty file when absent and fsync its current bytes without rewriting existing content."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("ab") as handle:
        handle.flush()
        os.fsync(handle.fileno())


def _looks_like_scientific_registry_state(payload: dict[str, Any]) -> bool:
    """Recognize a non-pristine ScientificRegistry whole-file image.

    The empty image is intentionally not classified here: the first real scientific
    append bootstraps authority from those already-durable empty bytes. Requiring at
    least one exact registry envelope avoids coupling unrelated generic ``records``
    stores to this scientific authority from their pristine initialization alone.
    """

    if set(payload) != {"schema_version", "records"} or payload.get("schema_version") != 1:
        return False
    records = payload.get("records")
    if type(records) is not list or not records:
        return False
    for record in records:
        if type(record) is not dict or set(record) != _SCIENTIFIC_REGISTRY_ENTRY_KEYS:
            return False
        if type(record.get("record_type")) is not str or not record["record_type"]:
            return False
        if type(record.get("record_id")) is not str or not record["record_id"]:
            return False
        if type(record.get("payload")) is not dict:
            return False
    return True


def _scientific_registry_authority(destination: Path) -> MonotonicWorkspaceAuthority:
    workspace = destination.parent.resolve(strict=False)
    return MonotonicWorkspaceAuthority(
        workspace=workspace,
        domain=_SCIENTIFIC_REGISTRY_AUTHORITY_DOMAIN,
        key=destination.name,
    )


def _authority_binding(destination: Path, observed: str | None, intended: str, *, kind: str) -> str:
    material = "\0".join(
        (
            _SCIENTIFIC_REGISTRY_AUTHORITY_DOMAIN,
            kind,
            destination.name,
            observed or "<PRISTINE>",
            intended,
        )
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _recover_or_bootstrap_scientific_registry_authority(
    authority: MonotonicWorkspaceAuthority,
    destination: Path,
    observed: str | None,
) -> None:
    history = authority.read_history()
    if not history:
        if observed is None:
            return
        tx_id = f"bootstrap-{observed}"
        binding = _authority_binding(destination, None, observed, kind="BOOTSTRAP")
        authority.prepare(
            tx_id=tx_id,
            observed_state_sha256=None,
            intended_state_sha256=observed,
            semantic_binding_sha256=binding,
        )
        authority.commit(
            tx_id=tx_id,
            observed_state_sha256=observed,
            semantic_binding_sha256=binding,
        )
        return

    pending = history[-1] if history[-1].phase is AuthorityPhase.PREPARE else None
    if pending is None:
        authority.recover(observed_state_sha256=observed)
        return
    authority.recover(
        observed_state_sha256=observed,
        tx_id=pending.tx_id,
        semantic_binding_sha256=pending.semantic_binding_sha256,
    )


def read_verified_scientific_registry_text(path: str | Path) -> str:
    """Read one stable ScientificRegistry candidate image under its path fence.

    Record/schema validation must run before monotonic authority judges the image.
    That ordering preserves precise corruption diagnostics without weakening
    rollback protection: after validation, the baseline helper reacquires this
    same path lock, exact-matches the validated bytes, and then bootstraps or
    recovers the independent authority against their digest.
    """

    destination = Path(path)
    with durable_path_lock(destination):
        try:
            return destination.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("scientific registry must be valid UTF-8 JSON") from exc

def establish_validated_scientific_registry_read_baseline(
    path: str | Path,
    validated_text: str,
) -> None:
    """Bind the first validated non-pristine registry image to machine authority.

    The verified reader cannot safely bootstrap a historyless legacy image before
    the ScientificRegistry parser has validated its complete schema and record
    digests. The caller therefore returns here only after validation. Re-read the
    exact bytes under the durable path lock before creating the trust-on-first-use
    baseline so a concurrent replacement cannot be certified from stale text.
    """

    if type(validated_text) is not str:
        raise TypeError("validated_text must be a string")

    destination = Path(path)
    with durable_path_lock(destination):
        current_bytes = destination.read_bytes()
        try:
            current_text = current_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("scientific registry must be valid UTF-8 JSON") from exc
        if current_text != validated_text:
            raise RuntimeError(
                "ScientificRegistry bytes changed after validation and before authority baseline"
            )

        try:
            payload = json.loads(current_text)
        except json.JSONDecodeError as exc:
            raise ValueError("scientific registry must be valid UTF-8 JSON") from exc
        if type(payload) is not dict or not _looks_like_scientific_registry_state(payload):
            # Preserve pristine initialization: the empty registry acquires
            # authority when its first real scientific record is published.
            return

        observed = hashlib.sha256(current_bytes).hexdigest()
        authority = _scientific_registry_authority(destination)
        _recover_or_bootstrap_scientific_registry_authority(
            authority,
            destination,
            observed,
        )


def atomic_write_json(path: str | Path, payload: dict[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        protect_scientific_registry = _looks_like_scientific_registry_state(payload)
        intended = sha256_file(temporary) if protect_scientific_registry else None

        with durable_path_lock(destination):
            with _ATOMIC_JSON_PUBLISH_LOCK:
                if not protect_scientific_registry:
                    os.replace(temporary, destination)
                    return

                assert intended is not None
                observed = sha256_file(destination) if destination.exists() else None
                authority = _scientific_registry_authority(destination)
                _recover_or_bootstrap_scientific_registry_authority(
                    authority,
                    destination,
                    observed,
                )
                binding = _authority_binding(
                    destination,
                    observed,
                    intended,
                    kind="PUBLISH",
                )
                tx_material = "\0".join(
                    (destination.name, observed or "<PRISTINE>", intended)
                ).encode("utf-8")
                tx_id = f"registry-{hashlib.sha256(tx_material).hexdigest()}"
                authority.prepare(
                    tx_id=tx_id,
                    observed_state_sha256=observed,
                    intended_state_sha256=intended,
                    semantic_binding_sha256=binding,
                )
                os.replace(temporary, destination)
                published = sha256_file(destination)
                if published != intended:
                    raise RuntimeError(
                        "published ScientificRegistry bytes do not match prepared authority digest"
                    )
                authority.commit(
                    tx_id=tx_id,
                    observed_state_sha256=published,
                    semantic_binding_sha256=binding,
                )
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
