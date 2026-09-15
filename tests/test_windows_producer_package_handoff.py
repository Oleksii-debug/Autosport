from __future__ import annotations

from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW = _ROOT / ".github" / "workflows" / "windows-build.yml"


def test_build_step_retains_producer_digest_across_step_boundary() -> None:
    workflow = _WORKFLOW.read_text(encoding="utf-8")

    build_step = "- name: Build Windows candidate and retain producer-bound package digest"
    consumer_step = "- name: Materialize verified independent package extraction"
    producer_export = '"AUTOSPORT_PRODUCER_PACKAGE_SHA256=$producerPackageSha"'

    assert build_step in workflow
    assert "^SHA256=([0-9a-f]{64})$" in workflow
    assert "must emit exactly one producer-bound final package SHA-256" in workflow
    assert producer_export in workflow
    assert workflow.index(build_step) < workflow.index(producer_export) < workflow.index(consumer_step)


def test_independent_consumer_checks_producer_digest_before_mutable_json() -> None:
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    start = workflow.index("- name: Materialize verified independent package extraction")
    end = workflow.index("- name: Execute packaged NVDA evidence contract")
    consumer = workflow[start:end]

    producer_expected = "$producerPackageSha = [string]$env:AUTOSPORT_PRODUCER_PACKAGE_SHA256"
    snapshot_capture = (
        "python scripts/verify_source_checkout.py --bind-artifact $package "
        "--bound-output $consumerPackage --digest-output $consumerDigestPath"
    )
    captured_digest = "$packageSha = (Get-Content -LiteralPath $consumerDigestPath -Raw).Trim()"
    producer_check = "if ($packageSha -ne $producerPackageSha) {"
    snapshot_check = (
        "[void](Assert-ProducerPackageDigest -Path $consumerPackage "
        "-Expected $producerPackageSha -Label 'Consumer-bound release ZIP snapshot')"
    )
    mutable_json_read = "$verification = Get-Content $verificationPath -Raw | ConvertFrom-Json"

    assert producer_expected in consumer
    assert snapshot_capture in consumer
    assert captured_digest in consumer
    assert producer_check in consumer
    assert snapshot_check in consumer
    assert mutable_json_read in consumer
    assert (
        consumer.index(producer_expected)
        < consumer.index(snapshot_capture)
        < consumer.index(captured_digest)
        < consumer.index(producer_check)
        < consumer.index(snapshot_check)
        < consumer.index(mutable_json_read)
    )


def test_consumer_extracts_verified_snapshot_from_same_locked_stream() -> None:
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    start = workflow.index("- name: Materialize verified independent package extraction")
    end = workflow.index("- name: Execute packaged NVDA evidence contract")
    consumer = workflow[start:end]

    function_start = consumer.index("function Expand-ProducerBoundPackage")
    function_end = consumer.index("$package = Join-Path $PWD 'dist/Autosport-V1-windows-x64.zip'", function_start)
    locked_extract = consumer[function_start:function_end]

    lock_open = "$stream = [System.IO.File]::Open("
    read_only_share = "[System.IO.FileShare]::Read"
    digest_from_stream = "$sha256.ComputeHash($stream)"
    rewind_stream = "$stream.Position = 0"
    extract_from_stream = "[System.IO.Compression.ZipFile]::ExtractToDirectory($stream, $Destination, $true)"
    close_stream = "$stream.Dispose()"
    invocation = (
        "Expand-ProducerBoundPackage -Path $consumerPackage -Expected $producerPackageSha "
        "-Destination $postPackageExtractRoot -Label 'Consumer-bound release ZIP snapshot at extraction'"
    )

    assert lock_open in locked_extract
    assert read_only_share in locked_extract
    assert digest_from_stream in locked_extract
    assert rewind_stream in locked_extract
    assert extract_from_stream in locked_extract
    assert close_stream in locked_extract
    assert (
        locked_extract.index(lock_open)
        < locked_extract.index(digest_from_stream)
        < locked_extract.index(rewind_stream)
        < locked_extract.index(extract_from_stream)
        < locked_extract.index(close_stream)
    )
    assert invocation in consumer
    assert "Expand-Archive -LiteralPath $consumerPackage" not in consumer


def test_coordinated_zip_and_json_replacement_is_rejected_by_earlier_digest() -> None:
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    start = workflow.index("- name: Materialize verified independent package extraction")
    end = workflow.index("- name: Execute packaged NVDA evidence contract")
    consumer = workflow[start:end]

    assert "COORDINATED_REPLACEMENT_PACKAGE" in consumer
    assert "$forgedVerification = [ordered]@{" in consumer
    assert "package_sha256 = $forgedPackageSha" in consumer
    assert "Coordinated producer handoff replacement probe" in consumer
    assert "Producer-bound digest gate accepted coordinated ZIP + verification JSON replacement" in consumer
