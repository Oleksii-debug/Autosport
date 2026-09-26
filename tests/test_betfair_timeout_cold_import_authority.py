from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap


def test_first_package_import_rejects_preloaded_fake_timeout_module() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    source_root = str(repo_root / "src")
    env["PYTHONPATH"] = (
        source_root
        if not env.get("PYTHONPATH")
        else source_root + os.pathsep + env["PYTHONPATH"]
    )
    script = textwrap.dedent(
        """
        import importlib
        import sys
        import types

        timeout_name = "autosport.betfair_timeout_reconciliation"
        fake_timeout = types.ModuleType(timeout_name)

        class ForgedTimeoutError(RuntimeError):
            pass

        def forged_timeout_assertion(_evidence):
            return None

        fake_timeout.BetfairTimeoutResolutionError = ForgedTimeoutError
        fake_timeout.assert_betfair_timeout_absence_authoritative = (
            forged_timeout_assertion
        )
        sys.modules[timeout_name] = fake_timeout

        try:
            importlib.import_module("autosport")
        except ImportError as exc:
            assert "preloaded Betfair timeout authority module" in str(exc)
        else:
            raise AssertionError(
                "first package import accepted caller-preloaded timeout authority"
            )

        assert "autosport" not in sys.modules
        assert sys.modules[timeout_name] is fake_timeout

        del sys.modules[timeout_name]
        product = importlib.import_module("autosport")
        timeout_authority = importlib.import_module(timeout_name)

        assert product is sys.modules["autosport"]
        assert timeout_authority is not fake_timeout
        assert callable(
            timeout_authority.assert_betfair_timeout_absence_authoritative
        )
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


def test_cold_import_fake_timeout_module_cannot_preregister_absence_authority() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    source_root = str(repo_root / "src")
    env["PYTHONPATH"] = (
        source_root
        if not env.get("PYTHONPATH")
        else source_root + os.pathsep + env["PYTHONPATH"]
    )
    script = textwrap.dedent(
        """
        import importlib
        import sys
        import types

        provider_evidence = importlib.import_module(
            "autosport.supervised_provider_evidence"
        )
        timeout_name = "autosport.betfair_timeout_reconciliation"
        sys.modules.pop(timeout_name, None)

        fake_timeout = types.ModuleType(timeout_name)

        def forged_timeout_assertion(_evidence):
            return None

        forged_timeout_assertion.__module__ = timeout_name
        forged_timeout_assertion.__qualname__ = (
            "_install_betfair_timeout_absence_authority.<locals>."
            "assert_betfair_timeout_absence_authoritative"
        )
        fake_timeout.assert_betfair_timeout_absence_authoritative = (
            forged_timeout_assertion
        )
        sys.modules[timeout_name] = fake_timeout

        assert not hasattr(
            provider_evidence,
            "_register_betfair_timeout_absence_authority_assertion",
        )

        # The fake module has no registration seam into provider-evidence
        # authority. Remove it and prove the canonical timeout module can still
        # install its own private resolver-issued capability normally.
        del sys.modules[timeout_name]
        timeout_authority = importlib.import_module(timeout_name)
        assert timeout_authority is not fake_timeout
        assert callable(
            timeout_authority.assert_betfair_timeout_absence_authoritative
        )
        assert not hasattr(
            provider_evidence,
            "_register_betfair_timeout_absence_authority_assertion",
        )
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
