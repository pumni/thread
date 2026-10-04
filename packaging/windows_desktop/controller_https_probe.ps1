function New-PrivateRootProbeStage {
    [pscustomobject][ordered]@{
        outcome = "NOT_RUN"
        elapsed_ms = 0
        category = $null
        exception_type = $null
    }
}

function Get-PrivateRootProbeExceptionType([System.Exception]$Exception) {
    if (-not $Exception) { return $null }
    return $Exception.GetBaseException().GetType().Name
}

function Invoke-PrivateRootHttpsProbe {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateRange(1, 65535)]
        [int]$Port,
        [Parameter(Mandatory = $true)]
        [ValidateSet("/health", "/ready")]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$RootCertificatePath,
        [Parameter()]
        [ValidateRange(100, 30000)]
        [int]$TimeoutMilliseconds = 3000
    )

    $result = [ordered]@{
        path = $Path
        target_host = "127.0.0.1"
        certificate_validation = [ordered]@{
            trust_mode = "CustomRootTrust"
            verification_flags = "NoFlag"
            revocation_mode = "NoCheck"
        }
        root_certificate_load = (New-PrivateRootProbeStage)
        tcp_connect = (New-PrivateRootProbeStage)
        tls_authentication = (New-PrivateRootProbeStage)
        http_request_write = (New-PrivateRootProbeStage)
        http_response = [ordered]@{
            outcome = "NOT_RUN"
            elapsed_ms = 0
            status_code = $null
            category = $null
            exception_type = $null
        }
        failure_stage = $null
        outcome = "FAIL"
        elapsed_ms = 0
    }
    $overallTimer = [System.Diagnostics.Stopwatch]::StartNew()
    $rootCertificate = $null
    $rootBytes = $null
    $tcpClient = $null
    $networkStream = $null
    $sslStream = $null
    $responseReader = $null

    try {
        do {
        $stageTimer = [System.Diagnostics.Stopwatch]::StartNew()
        try {
            $rootBytes = [System.IO.File]::ReadAllBytes($RootCertificatePath)
            $rootCertificate = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
                $rootBytes
            )
            $result.root_certificate_load.outcome = "PASS"
        } catch {
            $result.root_certificate_load.outcome = "FAIL"
            $result.root_certificate_load.category = "root_certificate_load_failed"
            $result.root_certificate_load.exception_type =
                Get-PrivateRootProbeExceptionType $_.Exception
            $result.failure_stage = "root_certificate_load"
            break
        } finally {
            $result.root_certificate_load.elapsed_ms = $stageTimer.ElapsedMilliseconds
            if ($rootBytes) { $null = [Array]::Clear($rootBytes, 0, $rootBytes.Length) }
        }

        $tcpClient = [System.Net.Sockets.TcpClient]::new()
        $stageTimer = [System.Diagnostics.Stopwatch]::StartNew()
        try {
            $connectTask = $tcpClient.ConnectAsync([Net.IPAddress]::Loopback, $Port)
            if (-not $connectTask.Wait($TimeoutMilliseconds)) {
                $result.tcp_connect.outcome = "TIMEOUT"
                $result.tcp_connect.category = "timeout"
                $result.failure_stage = "tcp_connect"
                break
            }
            $null = $connectTask.GetAwaiter().GetResult()
            $result.tcp_connect.outcome = "PASS"
        } catch {
            $result.tcp_connect.outcome = "FAIL"
            $result.tcp_connect.category = "connect_failed"
            $result.tcp_connect.exception_type = Get-PrivateRootProbeExceptionType $_.Exception
            $result.failure_stage = "tcp_connect"
            break
        } finally {
            $result.tcp_connect.elapsed_ms = $stageTimer.ElapsedMilliseconds
        }

        $networkStream = $tcpClient.GetStream()
        $networkStream.ReadTimeout = $TimeoutMilliseconds
        $networkStream.WriteTimeout = $TimeoutMilliseconds
        $sslStream = [System.Net.Security.SslStream]::new($networkStream, $false)
        $sslStream.ReadTimeout = $TimeoutMilliseconds
        $sslStream.WriteTimeout = $TimeoutMilliseconds

        $chainPolicy = [System.Security.Cryptography.X509Certificates.X509ChainPolicy]::new()
        $chainPolicy.TrustMode =
            [System.Security.Cryptography.X509Certificates.X509ChainTrustMode]::CustomRootTrust
        $chainPolicy.VerificationFlags =
            [System.Security.Cryptography.X509Certificates.X509VerificationFlags]::NoFlag
        $chainPolicy.RevocationMode =
            [System.Security.Cryptography.X509Certificates.X509RevocationMode]::NoCheck
        $null = $chainPolicy.CustomTrustStore.Add($rootCertificate)
        $authenticationOptions = [System.Net.Security.SslClientAuthenticationOptions]::new()
        $authenticationOptions.TargetHost = "127.0.0.1"
        $authenticationOptions.CertificateChainPolicy = $chainPolicy

        $stageTimer = [System.Diagnostics.Stopwatch]::StartNew()
        $authenticationTimeout = [System.Threading.CancellationTokenSource]::new()
        try {
            $null = $authenticationTimeout.CancelAfter($TimeoutMilliseconds)
            $authenticationTask = $sslStream.AuthenticateAsClientAsync(
                $authenticationOptions,
                $authenticationTimeout.Token
            )
            $null = $authenticationTask.GetAwaiter().GetResult()
            $result.tls_authentication.outcome = "PASS"
        } catch {
            $result.tls_authentication.outcome = "FAIL"
            $result.tls_authentication.category = if (
                $_.Exception.GetBaseException() -is [System.OperationCanceledException]
            ) { "timeout" } else { "tls_authentication_failed" }
            $result.tls_authentication.exception_type =
                Get-PrivateRootProbeExceptionType $_.Exception
            $result.failure_stage = "tls_authentication"
            break
        } finally {
            $result.tls_authentication.elapsed_ms = $stageTimer.ElapsedMilliseconds
            $null = $authenticationTimeout.Dispose()
        }

        $requestBytes = [System.Text.Encoding]::ASCII.GetBytes(
            "GET $Path HTTP/1.1`r`nHost: 127.0.0.1:$Port`r`nConnection: close`r`nAccept: */*`r`n`r`n"
        )
        $stageTimer = [System.Diagnostics.Stopwatch]::StartNew()
        try {
            $null = $sslStream.Write($requestBytes, 0, $requestBytes.Length)
            $null = $sslStream.Flush()
            $result.http_request_write.outcome = "PASS"
        } catch {
            $result.http_request_write.outcome = "FAIL"
            $result.http_request_write.category = "request_write_failed"
            $result.http_request_write.exception_type = Get-PrivateRootProbeExceptionType $_.Exception
            $result.failure_stage = "http_request_write"
            break
        } finally {
            $result.http_request_write.elapsed_ms = $stageTimer.ElapsedMilliseconds
        }

        $stageTimer = [System.Diagnostics.Stopwatch]::StartNew()
        try {
            $responseReader = [System.IO.StreamReader]::new(
                $sslStream,
                [System.Text.Encoding]::ASCII,
                $false,
                1024,
                $true
            )
            $statusLine = $responseReader.ReadLine()
            $statusMatch = [regex]::Match($statusLine, '^HTTP/\d(?:\.\d)?\s+(\d{3})(?:\s|$)')
            if (-not $statusMatch.Success) {
                $result.http_response.outcome = "INVALID_STATUS_LINE"
                $result.http_response.category = "invalid_status_line"
                $result.failure_stage = "http_response"
                break
            }
            $statusCode = [int]$statusMatch.Groups[1].Value
            $result.http_response.status_code = $statusCode
            $result.http_response.outcome = "RECEIVED"
            if ($statusCode -eq 200) {
                $result.outcome = "PASS"
            } else {
                $result.outcome = "HTTP_STATUS_NON_200"
                $result.http_response.category = "http_status_non_200"
                $result.failure_stage = "http_status"
            }
        } catch {
            $result.http_response.outcome = "FAIL"
            $result.http_response.category = "response_read_failed"
            $result.http_response.exception_type = Get-PrivateRootProbeExceptionType $_.Exception
            $result.failure_stage = "http_response"
        } finally {
            $result.http_response.elapsed_ms = $stageTimer.ElapsedMilliseconds
        }
        } while ($false)
    } catch {
        $result.outcome = "FAIL"
        if (-not $result.failure_stage) {
            $result.failure_stage = "probe_setup"
            $result.http_response.category = "probe_setup_failed"
            $result.http_response.exception_type = Get-PrivateRootProbeExceptionType $_.Exception
        }
    } finally {
        if ($responseReader) { $null = $responseReader.Dispose() }
        if ($sslStream) { $null = $sslStream.Dispose() }
        if ($networkStream) { $null = $networkStream.Dispose() }
        if ($tcpClient) { $null = $tcpClient.Dispose() }
        if ($rootCertificate) { $null = $rootCertificate.Dispose() }
        $null = $overallTimer.Stop()
        $result.elapsed_ms = $overallTimer.ElapsedMilliseconds
    }

    return [pscustomobject]$result
}
