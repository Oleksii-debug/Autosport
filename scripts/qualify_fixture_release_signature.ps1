# Plan 6 §5В: proof of certificate-backed signing on the exact release ZIP.
# Ephemeral fixture key material never leaves memory. NOT a production signing claim.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$PackagePath,
    [Parameter(Mandatory)][string]$SourceSha,
    [Parameter(Mandatory)][string]$ExpectedPackageSha256,
    [Parameter(Mandatory)][string]$EvidencePath
)
$ErrorActionPreference = 'Stop'
if ($SourceSha -cnotmatch '^[0-9a-f]{40}$') { throw 'Noncanonical source SHA' }
if ($ExpectedPackageSha256 -cnotmatch '^[0-9a-f]{64}$') { throw 'Noncanonical package digest' }
$archive = (Resolve-Path -LiteralPath $PackagePath -ErrorAction Stop).ProviderPath
if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) { throw 'Release ZIP is missing' }
$observedSha = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($observedSha -cne $ExpectedPackageSha256) { throw 'Release ZIP identity drifted' }

# Fixed UTF-8 wire format shared by signing and verifier. No mutable clock or path data.
$manifest = ((@('AUTOSPORT_FIXTURE_UPDATE_SIGNATURE_V1', "source_sha=$SourceSha",
    "package_sha256=$observedSha", '') -join [char]10))
$bytes = [System.Text.Encoding]::UTF8.GetBytes($manifest)
$rsa = $null
$certificate = $null
$publicCertificate = $null
$publicKey = $null
try {
    $rsa = [System.Security.Cryptography.RSA]::Create(3072)
    $request = [System.Security.Cryptography.X509Certificates.CertificateRequest]::new(
        'CN=Autosport fixture-only signing qualification', $rsa,
        [System.Security.Cryptography.HashAlgorithmName]::SHA256,
        [System.Security.Cryptography.RSASignaturePadding]::Pkcs1
    )
    $before = [System.DateTimeOffset]::UtcNow.AddMinutes(-5)
    $certificate = $request.CreateSelfSigned($before, $before.AddDays(2))
    # The verification side has ONLY the public DER cert, not a private-key capable object.
    $publicCertificate = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
        $certificate.Export([System.Security.Cryptography.X509Certificates.X509ContentType]::Cert)
    )
    if ($publicCertificate.HasPrivateKey) { throw 'Public verifier unexpectedly has a private key' }
    $signature = $rsa.SignData($bytes, [System.Security.Cryptography.HashAlgorithmName]::SHA256,
        [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)
    $publicKey = [System.Security.Cryptography.X509Certificates.RSACertificateExtensions]::GetRSAPublicKey(
        $publicCertificate)
    if ($null -eq $publicKey -or -not $publicKey.VerifyData(
        $bytes, $signature, [System.Security.Cryptography.HashAlgorithmName]::SHA256,
        [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)) {
        throw 'Certificate-backed release manifest verification failed'
    }

    $changedSourceFirst = if ($SourceSha[0] -eq 'a') { 'b' } else { 'a' }
    $wrongSource = $changedSourceFirst + $SourceSha.Substring(1)
    $sourceAttack = $manifest.Replace("source_sha=$SourceSha", "source_sha=$wrongSource")
    if ($publicKey.VerifyData([System.Text.Encoding]::UTF8.GetBytes($sourceAttack),
        $signature, [System.Security.Cryptography.HashAlgorithmName]::SHA256,
        [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)) {
        throw 'Substituted source SHA passed signature verification'
    }

    $changedDigestFirst = if ($observedSha[0] -eq 'a') { 'b' } else { 'a' }
    $wrongDigest = $changedDigestFirst + $observedSha.Substring(1)
    $digestAttack = $manifest.Replace("package_sha256=$observedSha", "package_sha256=$wrongDigest")
    if ($publicKey.VerifyData([System.Text.Encoding]::UTF8.GetBytes($digestAttack),
        $signature, [System.Security.Cryptography.HashAlgorithmName]::SHA256,
        [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)) {
        throw 'Substituted release ZIP digest passed signature verification'
    }

    $badSignature = [byte[]]::new($signature.Length)
    [System.Array]::Copy($signature, $badSignature, $signature.Length)
    $badSignature[0] = [byte]($badSignature[0] -bxor 1)
    if ($publicKey.VerifyData($bytes, $badSignature,
        [System.Security.Cryptography.HashAlgorithmName]::SHA256,
        [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)) {
        throw 'Corrupted signature passed verification'
    }
    $finalDigest = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($finalDigest -cne $observedSha) { throw 'Release ZIP was modified during signing' }

    $hash = [System.Security.Cryptography.SHA256]::Create()
    try {
        $publicCertSha = [System.Convert]::ToHexString(
            $hash.ComputeHash($publicCertificate.RawData)).ToLowerInvariant()
        $signatureSha = [System.Convert]::ToHexString(
            $hash.ComputeHash($signature)).ToLowerInvariant()
    } finally { $hash.Dispose() }

    $evidence = [ordered]@{
        schema = 'autosport.fixture_release_signature.v1'
        status = 'PASS'
        source_sha = $SourceSha
        package_sha256 = $observedSha
        public_certificate_sha256 = $publicCertSha
        signature_sha256 = $signatureSha
        algorithm = 'RSA-3072/SHA256/PKCS1v15'
        public_certificate_verification = $true
        wrong_source_rejected = $true
        wrong_package_digest_rejected = $true
        corrupted_signature_rejected = $true
        fixture_key_in_memory_only = $true
        production_signed = $false
        real_money_execution = $false
        human_tested = $false
        nvda_verified = $false
    }
    $output = [System.IO.Path]::GetFullPath($EvidencePath)
    [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($output)) | Out-Null
    [System.IO.File]::WriteAllText($output, (($evidence | ConvertTo-Json -Depth 5) + [char]10),
        [System.Text.UTF8Encoding]::new($false))
    Write-Output "fixture-release-signature=PASS source_sha=$SourceSha package_sha256=$observedSha"
}
finally {
    if ($null -ne $publicKey) { $publicKey.Dispose() }
    if ($null -ne $publicCertificate) { $publicCertificate.Dispose() }
    if ($null -ne $certificate) { $certificate.Dispose() }
    if ($null -ne $rsa) { $rsa.Dispose() }
}
