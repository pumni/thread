param(
    [Parameter(Mandatory = $true)]
    [string]$RootCertificatePath,
    [Parameter(Mandatory = $true)]
    [string]$LeafCertificatePath
)

$ErrorActionPreference = "Stop"
$root = $null
$leaf = $null
$chain = $null
try {
    $root = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
        [System.IO.File]::ReadAllBytes($RootCertificatePath)
    )
    $leaf = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
        [System.IO.File]::ReadAllBytes($LeafCertificatePath)
    )
    $chain = [System.Security.Cryptography.X509Certificates.X509Chain]::new()
    $chain.ChainPolicy.TrustMode =
        [System.Security.Cryptography.X509Certificates.X509ChainTrustMode]::CustomRootTrust
    $chain.ChainPolicy.VerificationFlags =
        [System.Security.Cryptography.X509Certificates.X509VerificationFlags]::NoFlag
    $chain.ChainPolicy.RevocationMode =
        [System.Security.Cryptography.X509Certificates.X509RevocationMode]::NoCheck
    $null = $chain.ChainPolicy.CustomTrustStore.Add($root)

    if (-not $chain.Build($leaf)) {
        $statuses = @($chain.ChainStatus | ForEach-Object { $_.Status.ToString() })
        $statusCode = if ($statuses.Count -gt 0) { [string]::Join("_", $statuses) } else { "unknown" }
        throw "product_controller_x509_chain_build_failed_$statusCode"
    }
    if ($chain.ChainElements.Count -ne 2) {
        throw "product_controller_x509_chain_element_count_invalid"
    }
    $builtRoot = $chain.ChainElements[$chain.ChainElements.Count - 1].Certificate
    if ([Convert]::ToBase64String($builtRoot.RawData) -cne [Convert]::ToBase64String($root.RawData)) {
        throw "product_controller_x509_chain_root_mismatch"
    }

    Write-Output "product_controller_x509_chain=PASS"
} finally {
    if ($chain) { $chain.Dispose() }
    if ($leaf) { $leaf.Dispose() }
    if ($root) { $root.Dispose() }
}
