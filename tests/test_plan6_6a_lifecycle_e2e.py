"""Plan 6 §6А: end-to-end PAPER release lifecycle acceptance (fixture-only).

The canonical package verifier is replaced only because these small synthetic ZIPs
are not actual signed Windows distributions. Installation, receipt/inventory,
transactional publication, rollback, crash recovery, cleanup and uninstall run
through their production implementations. No user workspace is modified.
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from autosport import offline_release_install as install
from autosport import offline_release_lifecycle as lifecycle
from autosport import offline_release_upgrade as upgrade


OLD_SHA = "a" * 40
NEW_SHA = "b" * 40


def _package(root: Path, name: str, *, source: str, version: str, schema: int):
    archive = root / name
    build = {
        "product": "Autosport", "version": version, "update_version": version,
        "update_schema": schema, "source_sha": source,
        "real_money_execution": False, "human_tested": False,
        "nvda_verified": False,
    }
    with zipfile.ZipFile(archive, "w") as writer:
        writer.writestr("Autosport-V1/Autosport.exe", source.encode("ascii"))
        writer.writestr("Autosport-V1/BUILD_INFO.json", json.dumps(build))
        writer.writestr("Autosport-V1/examples/demo.txt", "PAPER-only")
    return archive, hashlib.sha256(archive.read_bytes()).hexdigest()


@pytest.fixture
def release_case(tmp_path):
    old, old_digest = _package(
        tmp_path, "old.zip", source=OLD_SHA, version="1.0.0", schema=1,
    )
    new, new_digest = _package(
        tmp_path, "new.zip", source=NEW_SHA, version="1.1.0", schema=1,
    )
    destination = tmp_path / "Програма Autosport з пробілами"
    workspace = tmp_path / "Приватні дані користувача"
    workspace.mkdir()
    (workspace / "сесія.json").write_bytes(b'{"paper":true,"token":"never-log"}')
    preserved = (workspace / "сесія.json").read_bytes()

    # A fixture-only verifier is not a cryptographic or release qualification.
    def verify_fixture(archive, *, expected_source_sha, **_kwargs):
        return {
            "status": "PASS", "source_sha": expected_source_sha,
            "package_sha256": hashlib.sha256(Path(archive).read_bytes()).hexdigest(),
        }

    with patch.object(
        install.release_package, "verify_windows_package", side_effect=verify_fixture,
    ):
        yield {
            "destination": destination, "workspace": workspace,
            "preserved": preserved, "old": old, "old_digest": old_digest,
            "new": new, "new_digest": new_digest,
        }
    assert (workspace / "сесія.json").read_bytes() == preserved


def _old(case):
    return dict(
        archive=case["old"], expected_source_sha=OLD_SHA,
        expected_package_sha256=case["old_digest"],
    )


def _new(case):
    return dict(
        archive=case["new"], expected_source_sha=NEW_SHA,
        expected_package_sha256=case["new_digest"],
    )


def _transition(case):
    return dict(
        old_archive=case["old"], old_source_sha=OLD_SHA,
        old_package_sha256=case["old_digest"],
        new_archive=case["new"], new_source_sha=NEW_SHA,
        new_package_sha256=case["new_digest"],
    )


def test_clean_install_upgrade_rollback_finalize_uninstall_reinstall(release_case):
    case = release_case
    destination = case["destination"]
    assert install.install_verified_release(
        case["old"], destination, expected_source_sha=OLD_SHA,
        expected_package_sha256=case["old_digest"],
    )["status"] == "INSTALLED"
    assert lifecycle.inspect_installed_release(destination, **_old(case))["status"] == "VERIFIED"

    update = upgrade.upgrade_verified_paper_release(destination, **_transition(case))
    assert update["status"] == "UPDATED_PAPER_ONLY"
    assert update["schema_migrated"] is False
    assert lifecycle.inspect_installed_release(destination, **_new(case))["status"] == "VERIFIED"

    rollback = upgrade.rollback_verified_paper_release(destination, **_transition(case))
    assert rollback["status"] == "ROLLED_BACK_PAPER_ONLY"
    assert lifecycle.inspect_installed_release(destination, **_old(case))["status"] == "VERIFIED"
    assert upgrade.finalize_verified_paper_update(
        destination, **_transition(case),
    )["status"] == "PAPER_UPDATE_FINALIZED"

    assert lifecycle.uninstall_verified_release(destination, **_old(case))["status"] == "UNINSTALLED"
    assert not destination.exists()
    assert install.install_verified_release(
        case["new"], destination, expected_source_sha=NEW_SHA,
        expected_package_sha256=case["new_digest"],
    )["status"] == "INSTALLED"
    assert lifecycle.inspect_installed_release(destination, **_new(case))["status"] == "VERIFIED"
    assert (case["workspace"] / "сесія.json").read_bytes() == case["preserved"]


def test_interrupted_publication_recovers_and_disposes_candidate(release_case):
    case = release_case
    destination = case["destination"]
    install.install_verified_release(
        case["old"], destination, expected_source_sha=OLD_SHA,
        expected_package_sha256=case["old_digest"],
    )
    real_publish = upgrade._publish_directory_no_replace
    transaction = upgrade._transaction_root(destination)

    def simulate_process_abort(source, target):
        if Path(source) == transaction / "candidate" and Path(target) == destination:
            raise KeyboardInterrupt("simulated power loss before publication")
        return real_publish(source, target)

    with patch.object(upgrade, "_publish_directory_no_replace", side_effect=simulate_process_abort):
        with pytest.raises(KeyboardInterrupt, match="simulated power loss"):
            upgrade.upgrade_verified_paper_release(destination, **_transition(case))

    assert not destination.exists()
    assert (transaction / "previous").is_dir()
    assert (transaction / "candidate").is_dir()
    assert upgrade.recover_update_gap(
        destination, old_archive=case["old"], old_source_sha=OLD_SHA,
        old_package_sha256=case["old_digest"],
    )["status"] == "OLD_RELEASE_RECOVERED"
    assert lifecycle.inspect_installed_release(destination, **_old(case))["status"] == "VERIFIED"
    with pytest.raises(install.OfflineInstallError, match="previous update transaction"):
        upgrade.upgrade_verified_paper_release(destination, **_transition(case))
    assert upgrade.finalize_verified_paper_update(
        destination, **_transition(case),
    )["status"] == "PAPER_UPDATE_FINALIZED"
    assert not transaction.exists()
    assert upgrade.upgrade_verified_paper_release(
        destination, **_transition(case),
    )["status"] == "UPDATED_PAPER_ONLY"
    assert (case["workspace"] / "сесія.json").read_bytes() == case["preserved"]


def test_schema_change_refused_without_migration_or_transaction(release_case, tmp_path):
    case = release_case
    destination = case["destination"]
    install.install_verified_release(
        case["old"], destination, expected_source_sha=OLD_SHA,
        expected_package_sha256=case["old_digest"],
    )
    future, digest = _package(
        tmp_path, "future.zip", source=NEW_SHA, version="2.0.0", schema=2,
    )
    args = _transition(case)
    args.update(new_archive=future, new_package_sha256=digest)
    with pytest.raises(install.OfflineInstallError, match="unsupported schema-changing"):
        upgrade.upgrade_verified_paper_release(destination, **args)
    assert not upgrade._transaction_root(destination).exists()
    assert lifecycle.inspect_installed_release(destination, **_old(case))["status"] == "VERIFIED"
    assert (case["workspace"] / "сесія.json").read_bytes() == case["preserved"]


def test_unowned_install_file_blocks_uninstall_without_deleting_owner_data(release_case):
    case = release_case
    destination = case["destination"]
    install.install_verified_release(
        case["old"], destination, expected_source_sha=OLD_SHA,
        expected_package_sha256=case["old_digest"],
    )
    owner_file = destination / "мій файл.txt"
    owner_file.write_text("Зберегти", encoding="utf-8")
    with pytest.raises(install.OfflineInstallError):
        lifecycle.uninstall_verified_release(destination, **_old(case))
    assert owner_file.read_text(encoding="utf-8") == "Зберегти"
    assert (destination / "Autosport.exe").read_bytes() == OLD_SHA.encode("ascii")
    assert (case["workspace"] / "сесія.json").read_bytes() == case["preserved"]
