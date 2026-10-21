# Verify an exact source+release ZIP manifest with a caller-pinned public X.509 cert.
# This primitive does NOT establish production signer authorization (Plan 8).
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$PackagePath,
    [Parameter(Mandatory)][string]$SourceSha,
    [Parameter(Mandatory)][string]$ExpectedPackageSha256,
    [Parameter(Mandatory)][string]$PublicCertificatePath,
    [Parameter(Mandatory)][string]$SignaturePath,
    [Parameter(Mandatory)][string]$PinnedPublicCertificateSha256
)
$ErrorActionPreference = 'Stop'
if ($SourceSha -cnotmatch '^[0-9a-f]{40}$') { throw 'Noncanonical signed update source SHA' }
if ($ExpectedPackageSha256 -cnotmatch '^[0-9a-f]{64}$') { throw 'Noncanonical signed update package SHA' }
if ($PinnedPublicCertificateSha256 -cnotmatch '^[0-9a-f]{64}$') { throw 'Noncanonical signer pin' }
$archive = (Resolve-Path -LiteralPath $PackagePath -ErrorAction Stop).ProviderPath
$certPath = (Resolve-Path -LiteralPath $PublicCertificatePath -ErrorAction Stop).ProviderPath
$signaturePath = (Resolve-Path -LiteralPath $SignaturePath -ErrorAction Stop).ProviderPath
foreach ($required in @($archive, $certPath, $signaturePath)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) { throw 'Signed update input is not a regular file' }
}
$archiveSha = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($archiveSha -cne $ExpectedPackageSha256) { throw 'Signed update package digest mismatched' }
$certBytes = [System.IO.File]::ReadAllBytes($certPath)
$signature = [System.IO.File]::ReadAllBytes($signaturePath)
if ($certBytes.Length -lt 64 -or $signature.Length -lt 64) { throw 'Certificate or signature is truncated' }
$hash = [System.Security.Cryptography.SHA256]::Create()
try {
    $certSha = [System.Convert]::ToHexString($hash.ComputeHash($certBytes)).ToLowerInvariant()
} finally { $hash.Dispose() }
if ($certSha -cne $PinnedPublicCertificateSha256) { throw 'Independent signer pin mismatch' }

$certificate = $null
$publicKey = $null
try {
    $certificate = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new($certBytes)
    if ($certificate.HasPrivateKey) { throw 'Verifier refuses a private-key certificate' }
    $now = [System.DateTimeOffset]::UtcNow
    if ($now.UtcDateTime -lt $certificate.NotBefore.ToUniversalTime() -or
        $now.UtcDateTime -gt $certificate.NotAfter.ToUniversalTime()) {
        throw 'Signer certificate is outside its validity period'
    }
    $publicKey = [System.Security.Cryptography.X509Certificates.RSACertificateExtensions]::GetRSAPublicKey($certificate)
    if ($null -eq $publicKey) { throw 'Signed update signer has no RSA public key' }
    $manifest = (@('AUTOSPORT_FIXTURE_UPDATE_SIGNATURE_V1', "source_sha=$SourceSha",
        "package_sha256=$archiveSha", '') -join [char]10)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($manifest)
    if (-not $publicKey.VerifyData($bytes, $signature,
        [System.Security.Cryptography.HashAlgorithmName]::SHA256,
        [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)) {
        throw 'Signed update manifest signature is invalid'
    }
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -cne $archiveSha) {
        throw 'Release ZIP changed during signature verification'
    }
    Write-Output "release-update-signature=PASS source_sha=$SourceSha package_sha256=$archiveSha"
}
finally {
    if ($null -ne $publicKey) { $publicKey.Dispose() }
    if ($null -ne $certificate) { $certificate.Dispose() }
}
