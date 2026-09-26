from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .webview2_runtime_preflight import WebView2RuntimePreflight, probe_webview2_runtime


WEBVIEW2_DEPLOYMENT_MODE = "EVERGREEN_ONLINE_BOOTSTRAPPER"
WEBVIEW2_BOOTSTRAPPER_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
WEBVIEW2_BOOTSTRAPPER_FILENAME = "MicrosoftEdgeWebview2Setup.exe"
WEBVIEW2_INSTALL_ARGS = ("/silent", "/install")
WEBVIEW2_OFFLINE_INSTALL_SUPPORTED = False

_MAX_BOOTSTRAPPER_BYTES = 16 * 1024 * 1024
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_MICROSOFT_ORGANIZATION_RE = re.compile(
    r"(?:^|,\s*)O=Microsoft Corporation(?:,|$)",
    re.IGNORECASE,
)


class WebView2RuntimeDeploymentError(RuntimeError):
    """Fail-closed WebView2 prerequisite deployment error."""


def deployment_policy() -> dict[str, object]:
    """Return the exact first-run Runtime deployment policy compiled into Autosport."""

    return {
        "bootstrapper_url": WEBVIEW2_BOOTSTRAPPER_URL,
        "forced_elevation": False,
        "install_args": list(WEBVIEW2_INSTALL_ARGS),
        "kind": "webview2_runtime_deployment_policy",
        "mode": WEBVIEW2_DEPLOYMENT_MODE,
        "offline_install_supported": WEBVIEW2_OFFLINE_INSTALL_SUPPORTED,
        "schema_version": 1,
    }


def _download_bootstrapper(destination: Path) -> None:
    request = urllib.request.Request(
        WEBVIEW2_BOOTSTRAPPER_URL,
        headers={"User-Agent": "Autosport-WebView2-Prerequisite/1"},
        method="GET",
    )
    try:
        response_context = urllib.request.urlopen(request, timeout=60)
    except Exception as exc:
        raise WebView2RuntimeDeploymentError(
            "WebView2 bootstrapper download failed"
        ) from exc

    try:
        with response_context as response:
            final_url = response.geturl()
            parsed = urllib.parse.urlsplit(final_url)
            if parsed.scheme.lower() != "https" or not parsed.hostname:
                raise WebView2RuntimeDeploymentError(
                    "WebView2 bootstrapper download did not remain on HTTPS"
                )

            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_size = int(content_length, 10)
                except ValueError as exc:
                    raise WebView2RuntimeDeploymentError(
                        "WebView2 bootstrapper Content-Length is invalid"
                    ) from exc
                if declared_size <= 0 or declared_size > _MAX_BOOTSTRAPPER_BYTES:
                    raise WebView2RuntimeDeploymentError(
                        "WebView2 bootstrapper Content-Length is outside the allowed bound"
                    )

            total = 0
            with destination.open("xb") as handle:
                while True:
                    chunk = response.read(_DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MAX_BOOTSTRAPPER_BYTES:
                        raise WebView2RuntimeDeploymentError(
                            "WebView2 bootstrapper exceeded the allowed download bound"
                        )
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
    except WebView2RuntimeDeploymentError:
        destination.unlink(missing_ok=True)
        raise
    except (OSError, ValueError) as exc:
        destination.unlink(missing_ok=True)
        raise WebView2RuntimeDeploymentError(
            "WebView2 bootstrapper download could not be persisted"
        ) from exc

    if total == 0:
        destination.unlink(missing_ok=True)
        raise WebView2RuntimeDeploymentError("WebView2 bootstrapper download was empty")


def _authenticode_record(installer: Path) -> dict[str, Any]:
    script = r"""
$ErrorActionPreference = 'Stop'
$signature = Get-AuthenticodeSignature -LiteralPath $env:AUTOSPORT_WEBVIEW2_BOOTSTRAPPER
$subject = $null
if ($null -ne $signature.SignerCertificate) {
    $subject = [string]$signature.SignerCertificate.Subject
}
[ordered]@{
    status = [string]$signature.Status
    subject = $subject
} | ConvertTo-Json -Compress
""".strip()
    environment = os.environ.copy()
    environment["AUTOSPORT_WEBVIEW2_BOOTSTRAPPER"] = str(installer)
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WebView2RuntimeDeploymentError(
            "WebView2 bootstrapper Authenticode verification could not run"
        ) from exc
    if completed.returncode != 0:
        raise WebView2RuntimeDeploymentError(
            "WebView2 bootstrapper Authenticode verification failed"
        )
    try:
        record = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise WebView2RuntimeDeploymentError(
            "WebView2 bootstrapper Authenticode result is invalid"
        ) from exc
    if (
        type(record) is not dict
        or set(record) != {"status", "subject"}
        or type(record.get("status")) is not str
        or type(record.get("subject")) is not str
    ):
        raise WebView2RuntimeDeploymentError(
            "WebView2 bootstrapper Authenticode result is incomplete"
        )
    return record


def _require_microsoft_authenticode(installer: Path) -> None:
    record = _authenticode_record(installer)
    if record["status"] != "Valid":
        raise WebView2RuntimeDeploymentError(
            "WebView2 bootstrapper does not have a valid Authenticode signature"
        )
    if _MICROSOFT_ORGANIZATION_RE.search(record["subject"]) is None:
        raise WebView2RuntimeDeploymentError(
            "WebView2 bootstrapper signer is not Microsoft Corporation"
        )


def _run_bootstrapper(installer: Path) -> None:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [str(installer), *WEBVIEW2_INSTALL_ARGS],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=300,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WebView2RuntimeDeploymentError(
            "WebView2 Runtime installation did not complete"
        ) from exc
    if completed.returncode != 0:
        raise WebView2RuntimeDeploymentError(
            "WebView2 Runtime installer returned a failure status"
        )


def ensure_webview2_runtime() -> WebView2RuntimePreflight:
    """Ensure Evergreen Runtime availability before the canonical shell is imported.

    Existing Runtime availability is always resolved by the canonical preflight.
    When absent or invalid on Windows, the only automatic deployment mode is the
    Microsoft Evergreen online Bootstrapper. The installer is not elevated by
    Autosport, so Microsoft's supported per-user path remains available. Installer
    exit status is never sufficient: the canonical registry preflight must resolve
    AVAILABLE again after installation.
    """

    initial = probe_webview2_runtime()
    if initial.available is True:
        return initial
    if sys.platform != "win32":
        raise WebView2RuntimeDeploymentError(
            "automatic WebView2 Runtime deployment requires Windows"
        )

    with tempfile.TemporaryDirectory(prefix="autosport-webview2-runtime-") as directory:
        installer = Path(directory) / WEBVIEW2_BOOTSTRAPPER_FILENAME
        _download_bootstrapper(installer)
        _require_microsoft_authenticode(installer)
        _run_bootstrapper(installer)

    final = probe_webview2_runtime()
    if final.available is not True:
        raise WebView2RuntimeDeploymentError(
            "WebView2 Runtime remains unavailable after installer completion"
        )
    return final
