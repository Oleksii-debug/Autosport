param(
    [Parameter(Mandatory = $true)]
    [string]$PackagePath
)

# Plan 6 5V: qualify the same detached CMS primitives that a future signed
# package/update verifier can use. No signing key or production trust decision
# is created, exported, embedded in the release, or persisted by this fixture.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Security.Cryptography.Pkcs -ErrorAction Stop

function Assert-DetachedSignature {
    param(
        [Parameter(Mandatory = $true)][byte[]]$Document,
        [Parameter(Mandatory = $true)][byte[]]$Signature,
        [Parameter(Mandatory = $true)][string]$PinnedThumbprint
    )
    $info = [System.Security.Cryptography.Pkcs.ContentInfo]::new($Document)
    $verifier = [System.Security.Cryptography.Pkcs.SignedCms]::new($info, $true)
    $verifier.Decode($Signature)
    $verifier.CheckSignature($true)  # fixture: mathematical check, not chain trust
    if ($verifier.SignerInfos.Count -ne 1) {
        throw [System.Security.Cryptography.CryptographicException]::new('signature must have exactly one signer')
    }
    $signerCertificate = $verifier.SignerInfos[0].Certificate
    if ($null -eq $signerCertificate -or
        -not [string]::Equals(
            [string]$signerCertificate.Thumbprint,
            $PinnedThumbprint,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
        throw [System.Security.Cryptography.CryptographicException]::new('certificate pin mismatch')
    }
}

$package = Get-Item -LiteralPath $PackagePath -ErrorAction Stop
if ($package.PSIsContainer -or
    ($package.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
    $package.Length -lt 1 -or $package.Length -gt 268435456) {
    throw 'fixture signing requires one bounded ordinary release file'
}

$path = $package.FullName
$before = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
$bytes = [System.IO.File]::ReadAllBytes($path)
if ($bytes.Length -ne $package.Length) {
    throw 'release file changed during fixture signing read'
}
$hash = [Convert]::ToHexString(
    [System.Security.Cryptography.SHA256]::HashData($bytes)
).ToLowerInvariant()
if ($hash -ne $before) {
    throw 'release file changed before detached signing'
}

$rsa = [System.Security.Cryptography.RSA]::Create(3072)
$certificate = $null
try {
    $request = [System.Security.Cryptography.X509Certificates.CertificateRequest]::new(
        'CN=Autosport-Ephemeral-Signing-Fixture',
        $rsa,
        [System.Security.Cryptography.HashAlgorithmName]::SHA256,
        [System.Security.Cryptography.RSASignaturePadding]::Pkcs1
    )
    $certificate = $request.CreateSelfSigned(
        [System.DateTimeOffset]::UtcNow.AddMinutes(-1),
        [System.DateTimeOffset]::UtcNow.AddMinutes(15)
    )
    if (-not $certificate.HasPrivateKey) {
        throw 'fixture certificate lost its ephemeral private signing key'
    }

    $signer = [System.Security.Cryptography.Pkcs.CmsSigner]::new($certificate)
    $signer.DigestAlgorithm = [System.Security.Cryptography.Oid]::new('2.16.840.1.101.3.4.2.1')
    $signer.IncludeOption = [System.Security.Cryptography.X509Certificates.X509IncludeOption]::EndCertOnly
    $cms = [System.Security.Cryptography.Pkcs.SignedCms]::new(
        [System.Security.Cryptography.Pkcs.ContentInfo]::new($bytes),
        $true
    )
    $cms.ComputeSignature($signer)
    $signature = $cms.Encode()
    if ($null -eq $signature -or $signature.Length -lt 1) {
        throw 'fixture generated no detached signature'
    }

    Assert-DetachedSignature -Document $bytes -Signature $signature -PinnedThumbprint $certificate.Thumbprint

    $tampered = [byte[]]$bytes.Clone()
    $tampered[0] = [byte]($tampered[0] -bxor 1)
    $tamperRejected = $false
    try {
        Assert-DetachedSignature -Document $tampered -Signature $signature -PinnedThumbprint $certificate.Thumbprint
    } catch {
        $tamperRejected = $true
    }
    if (-not $tamperRejected) {
        throw 'tampered release unexpectedly passed cryptographic verification'
    }

    $wrongPinRejected = $false
    try {
        Assert-DetachedSignature -Document $bytes -Signature $signature -PinnedThumbprint ('0' * 40)
    } catch {
        $wrongPinRejected = $true
    }
    if (-not $wrongPinRejected) {
        throw 'incorrect release signer unexpectedly passed pinned verification'
    }

    $after = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($before -ne $after) {
        throw 'fixture signing mutated the original release archive'
    }

    [ordered]@{
        status = 'PASS'
        evidence_class = 'FIXTURE_DETACHED_CMS_ONLY'
        archive_sha256 = $hash
        signature_algorithm = 'CMS_SHA256_RSA3072_PKCS1'
        mathematical_signature_verified = $true
        source_archive_unchanged = $true
        tampered_content_rejected = $tamperRejected
        wrong_certificate_pin_rejected = $wrongPinRejected
        production_certificate_trusted = $false
        final_release_signed = $false
        real_money_execution = $false
        human_tested = $false
        nvda_verified = $false
    } | ConvertTo-Json -Compress -Depth 4
} finally {
    if ($null -ne $certificate) { $certificate.Dispose() }
    $rsa.Dispose()
}
