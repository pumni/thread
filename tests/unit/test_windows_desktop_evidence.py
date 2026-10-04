from __future__ import annotations

import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

REPO_ROOT = Path(__file__).parents[2]
SCRIPT = REPO_ROOT / "packaging" / "windows_desktop" / "verify_runtime_evidence.py"
CONTROLLER_SMOKE = REPO_ROOT / "packaging" / "windows_desktop" / "smoke_controller_lifecycle.ps1"
CONTROLLER_HTTPS_PROBE = REPO_ROOT / "packaging" / "windows_desktop" / "controller_https_probe.ps1"
HOSTED_SMOKE = REPO_ROOT / "packaging" / "windows_desktop" / "run_hosted_smoke.ps1"


def test_controller_quit_wait_is_process_authoritative() -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell AST parser is only available on Windows test hosts")

    smoke_path = str(CONTROLLER_SMOKE).replace("'", "''")
    assertion = rf"""
$smokePath = '{smoke_path}'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $smokePath, [ref]$tokens, [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) {{ throw "Controller smoke script did not parse" }}

$waitFunctions = @($ast.FindAll({{
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Wait-ForDesktopParentExit'
}}, $true))
if ($waitFunctions.Count -ne 1) {{ throw "Expected one process-exit helper" }}

$waitBody = $waitFunctions[0].Body
$waitCommands = @($waitBody.FindAll({{
    param($node) $node -is [System.Management.Automation.Language.CommandAst]
}}, $true) | ForEach-Object {{ $_.GetCommandName() }})
foreach ($required in @('Wait-Until', 'Get-ProcessIfPresent')) {{
    if ($waitCommands -notcontains $required) {{ throw "Missing process wait command: $required" }}
}}
$forbiddenCommands = @('Get-Window', 'Find-Element', 'Find-TextContaining', 'Get-ElementName')
if (@($waitCommands | Where-Object {{ $forbiddenCommands -contains $_ }}).Count -gt 0) {{
    throw "Process-exit helper depends on UI Automation"
}}
$waitTypes = @($waitBody.FindAll({{
    param($node)
    $node -is [System.Management.Automation.Language.TypeExpressionAst] -and
        $node.TypeName.FullName -like 'System.Windows.Automation.*'
}}, $true))
$waitMembers = @($waitBody.FindAll({{
    param($node) $node -is [System.Management.Automation.Language.MemberExpressionAst]
}}, $true) | ForEach-Object {{ $_.Member.Extent.Text }})
$uiaWaitMembers = @($waitMembers | Where-Object {{
    $_ -in @('FindAll', 'FindFirst', 'FromHandle', 'Current')
}})
if ($waitTypes.Count -gt 0 -or
    $uiaWaitMembers.Count -gt 0) {{
    throw "Process-exit helper uses UI Automation"
}}

$quitFunctions = @($ast.FindAll({{
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Quit-Desktop'
}}, $true))
if ($quitFunctions.Count -ne 1) {{ throw "Expected one Quit-Desktop function" }}
$stopCalls = @($quitFunctions[0].Body.FindAll({{
    param($node)
    $node -is [System.Management.Automation.Language.CommandAst] -and
        $node.GetCommandName() -eq 'Invoke-Button' -and
        $node.Extent.Text.Contains('Stop node and quit')
}}, $true))
$waitCalls = @($quitFunctions[0].Body.FindAll({{
    param($node)
    $node -is [System.Management.Automation.Language.CommandAst] -and
        $node.GetCommandName() -eq 'Wait-ForDesktopParentExit'
}}, $true))
if ($stopCalls.Count -ne 1 -or $waitCalls.Count -ne 1 -or
    $waitCalls[0].Extent.StartOffset -le $stopCalls[0].Extent.EndOffset) {{
    throw "Quit must invoke the Stop button before waiting for parent exit"
}}
$waitTryBlocks = @($quitFunctions[0].Body.FindAll({{
    param($node)
    if ($node -isnot [System.Management.Automation.Language.TryStatementAst]) {{ return $false }}
    $commands = @($node.Body.FindAll({{
        param($child) $child -is [System.Management.Automation.Language.CommandAst]
    }}, $true) | ForEach-Object {{ $_.GetCommandName() }})
    return $commands -contains 'Wait-ForDesktopParentExit'
}}, $true))
if ($waitTryBlocks.Count -ne 1) {{ throw "Quit-Desktop must wait in one isolated process phase" }}
$normalWaitCommands = @($waitTryBlocks[0].Body.FindAll({{
    param($node) $node -is [System.Management.Automation.Language.CommandAst]
}}, $true) | ForEach-Object {{ $_.GetCommandName() }})
if ($normalWaitCommands.Count -ne 1 -or
    $normalWaitCommands[0] -ne 'Wait-ForDesktopParentExit') {{
    throw "Post-Stop success path must only wait for Desktop parent exit"
}}
"process-authoritative Controller Quit assertion PASS"
"""
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", assertion],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}"
    assert "process-authoritative Controller Quit assertion PASS" in completed.stdout


def test_controller_endpoint_inputs_have_unique_automation_ids() -> None:
    app_source = (REPO_ROOT / "apps" / "desktop" / "src" / "App.tsx").read_text(encoding="utf-8")
    input_tags = re.findall(r"<input\b[^>]*>", app_source, flags=re.DOTALL)
    expected_inputs = {
        "controller-lan-address": ("text", "Stable LAN IPv4 address"),
        "controller-https-port": ("number", "HTTPS port"),
    }

    for automation_id, (input_type, accessible_name) in expected_inputs.items():
        matching_tags = [
            tag for tag in input_tags if re.search(rf'\bid="{re.escape(automation_id)}"', tag)
        ]
        assert len(matching_tags) == 1
        assert re.search(rf'\btype="{input_type}"', matching_tags[0])
        assert f'aria-label="{accessible_name}"' in matching_tags[0]
        assert re.search(
            rf"<label>\s*{re.escape(accessible_name)}\s*<input\b[^>]*\bid=\"{re.escape(automation_id)}\"",
            app_source,
            flags=re.DOTALL,
        )

    port_tag = next(tag for tag in input_tags if 'id="controller-https-port"' in tag)
    assert re.search(r"\bmin=\{1\}", port_tag)
    assert re.search(r"\bmax=\{65535\}", port_tag)


def test_controller_input_uia_resolution_is_identity_safe_and_password_is_opaque() -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell AST parser is only available on Windows test hosts")

    smoke_path = str(CONTROLLER_SMOKE).replace("'", "''")
    assertion = rf"""
$smokePath = '{smoke_path}'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $smokePath, [ref]$tokens, [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) {{ throw "Controller smoke script did not parse" }}

$functionNodes = @($ast.FindAll({{
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -in @('Set-LoginInput', 'Set-ControllerEndpointFields',
            'Find-ElementsByName', 'Find-ElementsByAutomationIdAndType',
            'Find-ElementByType', 'Test-ElementSupportsPattern',
            'Test-InteractiveInputElement', 'Resolve-InputControl',
            'Get-InputLookupDiagnostics')
}}, $true))
if ($functionNodes.Count -ne 9) {{ throw "Expected the focused Controller input helpers" }}
$functions = @{{}}
foreach ($functionNode in $functionNodes) {{ $functions[$functionNode.Name] = $functionNode }}
$login = $functions['Set-LoginInput']
$loginBody = $login.Body

function Get-Commands($body, [string]$name) {{
    @($body.FindAll({{
        param($node)
        $node -is [System.Management.Automation.Language.CommandAst] -and
            $node.GetCommandName() -eq $name
    }}, $true))
}}
function Get-PatternCalls($body) {{
    @($body.FindAll({{
        param($node)
        $node -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -and
            $node.Member.Extent.Text -eq 'GetCurrentPattern'
    }}, $true))
}}
function Get-Invocations($body, [string]$memberName) {{
    @($body.FindAll({{
        param($node)
        $node -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -and
            $node.Member.Extent.Text -eq $memberName
    }}, $true))
}}

$allowedParameter = @($loginBody.ParamBlock.Parameters | Where-Object {{
    $_.Name.VariablePath.UserPath -eq 'AllowedControlTypes'
}})
if ($allowedParameter.Count -ne 1 -or
    ($allowedParameter[0].DefaultValue.Extent.Text -replace '\s+', '') -ne
        '@([System.Windows.Automation.ControlType]::Edit)') {{
    throw "Default and text input control type must remain Edit-only"
}}

$endpointBody = $functions['Set-ControllerEndpointFields'].Body
$endpointCalls = Get-Commands $endpointBody 'Set-LoginInput'
$addressCall = @(
    $endpointCalls | Where-Object {{ $_.Extent.Text -match 'Stable LAN IPv4 address' }}
)
$portCall = @($endpointCalls | Where-Object {{ $_.Extent.Text -match 'HTTPS port' }})
if ($addressCall.Count -ne 1 -or
    $addressCall[0].Extent.Text -notmatch '-AutomationId\s+"controller-lan-address"' -or
    $addressCall[0].Extent.Text -notmatch 'ControlType\]::Edit' -or
    $portCall.Count -ne 1 -or
    $portCall[0].Extent.Text -notmatch '-AutomationId\s+"controller-https-port"') {{
    throw "Endpoint helper must target its unique automation IDs"
}}
$portTypeNames = @([regex]::Matches(
    $portCall[0].Extent.Text, 'ControlType\]::(Spinner|Edit)'
) | ForEach-Object {{ $_.Groups[1].Value }})
if (($portTypeNames -join ',') -ne 'Spinner,Edit') {{
    throw "HTTPS port must explicitly allow Spinner and Edit"
}}
$allInputCalls = Get-Commands $ast 'Set-LoginInput'
$customAutomationCalls = @($allInputCalls | Where-Object {{
    $_.Extent.Text -match '-AutomationId'
}})
if ($customAutomationCalls.Count -ne 2 -or
    @($allInputCalls | Where-Object {{ $_.Extent.Text -match 'HTTPS port' }}).Count -ne 1) {{
    throw "Only the two endpoint controls use the endpoint AutomationIds"
}}

$commands = @($loginBody.FindAll({{
    param($node) $node -is [System.Management.Automation.Language.CommandAst]
}}, $true))
$waitCalls = @($commands | Where-Object {{ $_.GetCommandName() -eq 'Wait-Until' }} |
    Sort-Object {{ $_.Extent.StartOffset }})
$sendCalls = @($loginBody.FindAll({{
    param($node)
    $node -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -and
        $node.Member.Extent.Text -eq 'SendWait'
}}, $true))
if ($waitCalls.Count -ne 2 -or $sendCalls.Count -ne 2 -or
    $waitCalls[0].Extent.StartOffset -ge $sendCalls[0].Extent.StartOffset) {{
    throw "Input must use bounded UIA lookup before keyboard mutation"
}}
$resolverCalls = Get-Commands $loginBody 'Resolve-InputControl'
$focusCalls = Get-Invocations $loginBody 'SetFocus'
if ($resolverCalls.Count -lt 2 -or $focusCalls.Count -ne 1 -or
    $resolverCalls[0].Extent.StartOffset -ge $focusCalls[0].Extent.StartOffset -or
    $focusCalls[0].Extent.StartOffset -ge $sendCalls[0].Extent.StartOffset -or
    $sendCalls[0].Extent.StartOffset -ge $sendCalls[1].Extent.StartOffset -or
    $waitCalls[0].Extent.Text -notmatch '\}}\s+20\s+\$inputUnavailableCode') {{
    throw "Resolved interactive input must be focused and keyboard-mutated after bounded lookup"
}}
$resolutionBody = $functions['Resolve-InputControl'].Body.Extent.Text
$resolverBody = $functions['Resolve-InputControl'].Body
$idLookup = Get-Commands $resolverBody 'Find-ElementsByAutomationIdAndType'
$nameLookup = Get-Commands $resolverBody 'Find-ElementsByName'
if ($idLookup.Count -ne 1 -or $nameLookup.Count -ne 1 -or
    $idLookup[0].Extent.StartOffset -ge $nameLookup[0].Extent.StartOffset -or
    $resolutionBody -notmatch '\$AllowedControlTypes\s*-contains\s*\$controlType' -or
    $resolutionBody -notmatch 'Test-InteractiveInputElement\s+\$_\s+\$FieldId') {{
    throw "AutomationId must be preferred; name fallback must filter interactive allowed controls"
}}
$idHelper = $functions['Find-ElementsByAutomationIdAndType'].Body.Extent.Text
if ($idHelper -notmatch 'AutomationIdProperty' -or
    $idHelper -notmatch 'ControlTypeProperty' -or
    $idHelper -notmatch '\[System\.Windows\.Automation\.AndCondition\]::new' -or
    $idHelper -notmatch '\$Root\.FindAll') {{
    throw "AutomationId lookup must match the ID and allowed type exactly"
}}
$interactiveBody = $functions['Test-InteractiveInputElement'].Body.Extent.Text
if ($interactiveBody -notmatch 'IsKeyboardFocusable' -or
    $interactiveBody -notmatch 'IsEnabled' -or
    $interactiveBody -notmatch 'ControlType\.Spinner' -or
    $interactiveBody -notmatch 'ControlType\.Edit' -or
    $interactiveBody -match 'ControlType\.Text' -or
    $interactiveBody -notmatch 'RangeValuePattern' -or
    $interactiveBody -notmatch 'ValuePattern') {{
    throw "Name fallback must reject labels and require focusable enabled input patterns"
}}
$interactiveChildLookup = Get-Commands $functions['Test-InteractiveInputElement'].Body `
    'Find-ElementByType'
if ($interactiveChildLookup.Count -ne 1 -or
    $interactiveChildLookup[0].Extent.Text -notmatch 'Find-ElementByType\s+\$Element' -or
    $interactiveChildLookup[0].Extent.Text -notmatch 'ControlType\]::Edit') {{
    throw "Spinner compatibility must find only an Edit child of that exact Spinner"
}}

$passwordGate = @($loginBody.FindAll({{
    param($node)
    $node -is [System.Management.Automation.Language.IfStatementAst] -and
        $node.Clauses[0].Item1.Extent.Text -match '\$fieldId\s*-ne\s*"password"'
}}, $true))
$verificationText = if ($passwordGate.Count -eq 1) {{ $passwordGate[0].Extent.Text }} else {{ '' }}
if ($passwordGate.Count -ne 1 -or
    $verificationText -notmatch '\[System\.Windows\.Automation\.RangeValuePattern\]::Pattern' -or
    $verificationText -notmatch '\[System\.Windows\.Automation\.ValuePattern\]::Pattern' -or
    $verificationText -notmatch '\[int\]::TryParse' -or
    $verificationText -notmatch 'RangeValuePattern' -or
    $verificationText -notmatch 'desktop_input_value_not_populated_\$fieldId') {{
    throw "Non-secret read-back must be type-specific, numeric-safe, and password-gated"
}}
$spinnerBranch = @($passwordGate[0].FindAll({{
    param($node)
    $node -is [System.Management.Automation.Language.IfStatementAst] -and
        $node.Clauses[0].Item1.Extent.Text -match '\$controlTypeName\s*-eq\s*"ControlType\.Spinner"'
}}, $true))
if ($spinnerBranch.Count -ne 1 -or
    $spinnerBranch[0].Extent.Text -notmatch '\[double\]\$rangePattern\.Current\.Value' -or
    $spinnerBranch[0].Extent.Text -notmatch 'Find-ElementByType\s+\$current' -or
    $spinnerBranch[0].Extent.Text -notmatch '\[System\.Windows\.Automation\.ControlType\]::Edit') {{
    throw "Spinner must verify RangeValue and keep any Edit fallback inside the named spinner"
}}

$diagnosticBody = $functions['Get-InputLookupDiagnostics'].Body.Extent.Text
$nameFinderBody = $functions['Find-ElementsByName'].Body.Extent.Text
if ($nameFinderBody -notmatch '\$Root\.FindAll' -or
    $nameFinderBody -match '\$Root\.FindFirst' -or
    $diagnosticBody -notmatch 'Find-ElementsByName' -or
    $diagnosticBody -notmatch 'foreach\s*\(\s*\$element\s+in\s+\$elements\s*' -or
    $diagnosticBody -notmatch 'automation_id' -or
    $diagnosticBody -notmatch 'control_type' -or
    $diagnosticBody -notmatch 'is_keyboard_focusable' -or
    $diagnosticBody -notmatch 'is_enabled' -or
    $diagnosticBody -notmatch 'supported_patterns' -or
    $diagnosticBody -match 'Current\.Value') {{
    throw "Failure diagnostics must enumerate structural metadata only"
}}
$namedTypeLookups = Get-Commands $loginBody 'Get-InputLookupDiagnostics'
$diagnosticCatches = @($loginBody.FindAll({{
    param($node)
    $node -is [System.Management.Automation.Language.CatchClauseAst] -and
        (Get-Commands $node.Body 'Get-InputLookupDiagnostics').Count -eq 1
}}, $true))
$diagnosticFunction = $functions['Get-InputLookupDiagnostics']
$diagPatternReads = Get-Commands $diagnosticFunction.Body 'Test-ElementSupportsPattern'
if ($namedTypeLookups.Count -ne 1 -or $diagnosticCatches.Count -ne 1 -or
    $diagnosticCatches[0].Extent.Text -notmatch 'input_lookup_diagnostics' -or
    $diagPatternReads.Count -lt 4) {{
    throw "Lookup failure must record safe pattern-presence diagnostics only"
}}
$childTypeLookups = Get-Commands $loginBody 'Find-ElementByType'
if ($childTypeLookups.Count -ne 1 -or
    $childTypeLookups[0].Extent.StartOffset -lt $spinnerBranch[0].Extent.StartOffset -or
    $childTypeLookups[0].Extent.EndOffset -gt $spinnerBranch[0].Extent.EndOffset) {{
    throw "Edit fallback must remain scoped to the exact named Spinner"
}}

$allPatternReads = Get-PatternCalls $loginBody
$gatedPatternReads = Get-PatternCalls $passwordGate[0].Clauses[0].Item2
if ($allPatternReads.Count -ne $gatedPatternReads.Count -or
    $allPatternReads.Count -lt 3) {{
    throw "Password must remain exempt from all UIA value-pattern read-back"
}}
"Typed bounded Controller input assertion PASS"
"""
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", assertion],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}"
    assert "Typed bounded Controller input assertion PASS" in completed.stdout


def _write_probe_identity(directory: Path, prefix: str, *, leaf_ip: str) -> tuple[Path, Path, Path]:
    root_key = ec.generate_private_key(ec.SECP256R1())
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{prefix} synthetic root")])
    now = datetime.now(UTC)
    root_certificate = (
        x509.CertificateBuilder()
        .subject_name(root_name)
        .issuer_name(root_name)
        .public_key(root_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(root_key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_certificate = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{prefix} synthetic leaf")])
        )
        .issuer_name(root_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(leaf_ip))]),
            critical=False,
        )
        .sign(root_key, hashes.SHA256())
    )
    root_path = directory / f"{prefix}-root.der"
    certificate_path = directory / f"{prefix}-leaf.pem"
    key_path = directory / f"{prefix}-leaf-key.pem"
    root_path.write_bytes(root_certificate.public_bytes(serialization.Encoding.DER))
    certificate_path.write_bytes(leaf_certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return root_path, certificate_path, key_path


def _start_probe_http_server(
    certificate_path: Path | None = None, key_path: Path | None = None
) -> tuple[ThreadingHTTPServer, threading.Thread, int]:
    class ProbeHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            status = 200 if self.path == "/health" else 503
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    class QuietThreadingHTTPServer(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request: object, client_address: object) -> None:
            return

    server = QuietThreadingHTTPServer(("127.0.0.1", 0), ProbeHandler)
    if certificate_path is not None and key_path is not None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(certificate_path), str(key_path))
        server.socket = context.wrap_socket(server.socket, server_side=True)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, port


def test_private_root_https_probe_classifies_real_tcp_tls_and_http_stages(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("the shared Controller probe is a Windows PowerShell runtime helper")
    powershell = shutil.which("pwsh")
    if powershell is None:
        pytest.skip("the shared Controller TLS probe requires PowerShell 7")

    root_path, leaf_path, leaf_key_path = _write_probe_identity(
        tmp_path, "controller-probe-valid", leaf_ip="127.0.0.1"
    )
    wrong_root_path, _, _ = _write_probe_identity(
        tmp_path, "controller-probe-wrong-root", leaf_ip="127.0.0.1"
    )
    wrong_san_root_path, wrong_san_leaf_path, wrong_san_key_path = _write_probe_identity(
        tmp_path, "controller-probe-wrong-san", leaf_ip="192.0.2.41"
    )
    tls_server, tls_thread, tls_port = _start_probe_http_server(leaf_path, leaf_key_path)
    wrong_san_server, wrong_san_thread, wrong_san_port = _start_probe_http_server(
        wrong_san_leaf_path, wrong_san_key_path
    )
    plaintext_server, plaintext_thread, plaintext_port = _start_probe_http_server()
    with socket.socket() as unused_listener:
        unused_listener.bind(("127.0.0.1", 0))
        unused_port = int(unused_listener.getsockname()[1])

    helper_path = str(CONTROLLER_HTTPS_PROBE).replace("'", "''")
    root = str(root_path).replace("'", "''")
    wrong_root = str(wrong_root_path).replace("'", "''")
    wrong_san_root = str(wrong_san_root_path).replace("'", "''")
    assertion = f"""
. '{helper_path}'
$results = @(
    (Invoke-PrivateRootHttpsProbe `
        -Port {tls_port} -Path '/health' -RootCertificatePath '{root}'),
    (Invoke-PrivateRootHttpsProbe `
        -Port {tls_port} -Path '/ready' -RootCertificatePath '{root}'),
    (Invoke-PrivateRootHttpsProbe `
        -Port {tls_port} -Path '/health' -RootCertificatePath '{wrong_root}'),
    (Invoke-PrivateRootHttpsProbe `
        -Port {wrong_san_port} -Path '/health' -RootCertificatePath '{wrong_san_root}'),
    (Invoke-PrivateRootHttpsProbe `
        -Port {unused_port} -Path '/health' -RootCertificatePath '{root}'),
    (Invoke-PrivateRootHttpsProbe `
        -Port {plaintext_port} -Path '/health' -RootCertificatePath '{root}')
)
$results | ConvertTo-Json -Depth 8 -Compress
"""
    try:
        completed = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", assertion],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    finally:
        for server, thread in (
            (tls_server, tls_thread),
            (wrong_san_server, wrong_san_thread),
            (plaintext_server, plaintext_thread),
        ):
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}"
    probes = json.loads(completed.stdout.strip().splitlines()[-1])
    assert len(probes) == 6

    healthy, not_ready, wrong_root, wrong_san, no_listener, plaintext = probes
    assert all(isinstance(probe, dict) for probe in probes), repr(probes)
    assert healthy["outcome"] == "PASS"
    assert healthy["target_host"] == "127.0.0.1"
    assert healthy["certificate_validation"] == {
        "trust_mode": "CustomRootTrust",
        "verification_flags": "NoFlag",
        "revocation_mode": "NoCheck",
    }
    assert healthy["tcp_connect"]["outcome"] == "PASS"
    assert healthy["tls_authentication"]["outcome"] == "PASS"
    assert healthy["http_request_write"]["outcome"] == "PASS"
    assert healthy["http_response"]["status_code"] == 200

    assert not_ready["outcome"] == "HTTP_STATUS_NON_200"
    assert not_ready["tls_authentication"]["outcome"] == "PASS"
    assert not_ready["http_response"]["status_code"] == 503

    for rejected in (wrong_root, wrong_san, plaintext):
        assert rejected["outcome"] == "FAIL"
        assert rejected["tls_authentication"]["outcome"] == "FAIL"
        assert rejected["http_request_write"]["outcome"] == "NOT_RUN"
    assert no_listener["tcp_connect"]["outcome"] in {"FAIL", "TIMEOUT"}
    assert no_listener["tls_authentication"]["outcome"] == "NOT_RUN"

    helper_source = CONTROLLER_HTTPS_PROBE.read_text(encoding="utf-8")
    assert "RemoteCertificateValidationCallback" not in helper_source
    assert "DangerousAcceptAnyServerCertificateValidator" not in helper_source
    assert "X509Store" not in helper_source


def test_post_owner_probe_snapshot_and_failure_taxonomy_are_preserved() -> None:
    source = CONTROLLER_SMOKE.read_text(encoding="utf-8")
    hosted_source = HOSTED_SMOKE.read_text(encoding="utf-8")
    assert "Test-ControllerHttps" not in source
    assert "controller_tls_readiness_failed" not in source
    assert '"controller_https_probe.ps1"' in hosted_source
    assert "$stageControllerHttpsProbeScript" in hosted_source
    assert "Copy-Item -LiteralPath $controllerHttpsProbeScript" in hosted_source

    owner_call = source.index("Bootstrap-ControllerOwner $desktop.Id")
    after_owner_probe = source.index("Wait-ForControllerHttps $config 60 -AfterOwnerBootstrap")
    enabled_owner_check = source.index("$enabledOwnerCount = Invoke-Psql", owner_call)
    assert owner_call < after_owner_probe < enabled_owner_check

    snapshot_function = source[
        source.index("function Get-PostOwnerRuntimeSnapshot") : source.index(
            "function Save-PostOwnerRuntimeProbe"
        )
    ]
    for evidence_field in (
        "postgres_count",
        "postgres_pids",
        "http_count",
        "http_pids",
        "scheduler_count",
        "scheduler_pids",
        "endpoint_listener_count",
        "endpoint_listener_addresses",
        "serving_leaf_key_present",
        "active_owner_session_count",
    ):
        assert evidence_field in snapshot_function

    waiter = source[
        source.index("function Wait-ForControllerHttps") : source.index(
            "function Test-PlaintextHttpRejected"
        )
    ]
    assert 'Invoke-ControllerHttpsProbe $Config "/health"' in waiter
    assert 'Invoke-ControllerHttpsProbe $Config "/ready"' in waiter
    assert "Save-PostOwnerRuntimeProbe" in waiter
    for failure_code in (
        "controller_post_owner_http_process_missing",
        "controller_post_owner_scheduler_process_missing",
        "controller_post_owner_endpoint_listener_absent",
        "controller_tls_probe_tcp_connect_failed",
        "controller_tls_probe_validation_failed",
        "controller_https_health_status_",
        "controller_https_readiness_status_",
    ):
        assert failure_code in source
    assert "post_owner_runtime_probe = $postOwnerRuntimeProbe" in source


def test_endpoint_reconfiguration_preserves_or_revokes_owner_session_at_the_right_boundary() -> (
    None
):
    source = CONTROLLER_SMOKE.read_text(encoding="utf-8")
    unavailable_start = source.index(
        "Set-ControllerEndpointFields $desktop.Id $unavailableAddress $oldHttpsPort"
    )
    unavailable_attempt = source[
        unavailable_start : source.index("$endpointReservation =", unavailable_start)
    ]
    assert unavailable_attempt.index('"Apply endpoint change"') < unavailable_attempt.index(
        "Get-ActiveOwnerSessionCount $config"
    )
    assert "controller_endpoint_reconfigure_session_lost_on_unavailable_ip" in unavailable_attempt
    assert "$checks.endpoint_running_unavailable_ip_preserves_owner_session = $true" in (
        unavailable_attempt
    )

    post_owner_start = source.index("# Failed post-Owner attempts")
    collision_start = source.index(
        "Set-ControllerEndpointFields $desktop.Id $lanAddress $collisionPort",
        post_owner_start,
    )
    collision_attempt = source[
        collision_start : source.index("$endpointReservation.Stop()", collision_start)
    ]
    assert collision_attempt.index('"Apply endpoint change"') < collision_attempt.index(
        "Get-ActiveOwnerSessionCount $config"
    )
    assert "controller_endpoint_reconfigure_session_lost_on_collision" in collision_attempt
    assert "$checks.endpoint_running_collision_preserves_owner_session = $true" in collision_attempt

    owner_helper = source[
        source.index("function Ensure-ControllerOwner") : source.index("function Start-Desktop")
    ]
    transition_revoke = owner_helper.index('"controller_endpoint_reconfigure_session_not_revoked"')
    transition_login = owner_helper.index('Set-LoginInput $ProcessId "Username"')
    assert transition_revoke < transition_login
    assert '"controller_endpoint_reconfigure_reauthentication_failed"' in owner_helper
    assert "$checks.endpoint_running_transition_revokes_owner_session = $true" in owner_helper
    assert "$checks.endpoint_running_transition_reauthenticates_one_owner_session = $true" in (
        owner_helper
    )
    assert (
        "Ensure-ControllerOwner $desktop.Id -ForceReauthentication -AfterEndpointReconfiguration"
        in (source)
    )


def _load_verifier() -> ModuleType:
    spec = importlib.util.spec_from_file_location("windows_desktop_evidence_verifier", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _process_evidence(layout_root: Path, *, layout: str, revision: str) -> None:
    diagnostic_root = layout_root / "hosted-smoke-process"
    diagnostic_root.mkdir(parents=True, exist_ok=True)
    diagnostics: list[dict[str, Any]] = []
    for name in ("stdout.redacted.log", "stderr.redacted.log"):
        content = b""
        (diagnostic_root / name).write_bytes(content)
        diagnostics.append(
            {
                "name": f"hosted-smoke-process/{name}",
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    _write_json(
        layout_root / "smoke-process.json",
        {
            "schema_version": 1,
            "source_revision": revision,
            "layout": layout,
            "child_exit_code": 1,
            "spawn_failure_code": None,
            "smoke_json_copied": True,
            "smoke_status": "FAIL",
            "primary_failure_code": "packaged_http_exited_before_ready",
            "log_scrub_status": "FAIL",
            "status": "BLOCKER",
            "sanitized_diagnostics": diagnostics,
        },
    )


def test_runtime_smoke_keeps_root_failure_separate_from_log_scrub_failure(tmp_path: Path) -> None:
    verifier = _load_verifier()
    layout = "shared"
    revision = "a" * 40
    layout_root = tmp_path / layout
    _process_evidence(layout_root, layout=layout, revision=revision)
    _write_json(
        layout_root / "smoke.json",
        {
            "schema_version": 4,
            "run_kind": "github_hosted_windows_x64_isolated",
            "source_revision": revision,
            "source_tree_dirty": False,
            "status": "FAIL",
            "clean_windows_runner_status": "BLOCKER",
            "primary_failure_code": "packaged_http_exited_before_ready",
            "failure_code": "packaged_http_exited_before_ready",
            "log_scrub_status": "FAIL",
            "log_scrub_failure_code": "runtime_log_scrub_verification_failed",
            "redacted_logs": [],
            "checks": {"redacted_runtime_logs": False},
        },
    )

    result = verifier.verify_smoke(tmp_path, layout=layout, expected_revision=revision)

    assert result["status"] == "BLOCKER"
    assert result["primary_failure_code"] == "packaged_http_exited_before_ready"
    assert result["log_scrub_status"] == "FAIL"
    assert result["log_scrub_failure_code"] == "runtime_log_scrub_verification_failed"


def test_not_run_log_scrub_keeps_pre_runtime_primary_failure(tmp_path: Path) -> None:
    verifier = _load_verifier()
    layout = "shared"
    revision = "d" * 40
    layout_root = tmp_path / layout
    _process_evidence(layout_root, layout=layout, revision=revision)
    _write_json(
        layout_root / "smoke.json",
        {
            "schema_version": 4,
            "run_kind": "github_hosted_windows_x64_isolated",
            "source_revision": revision,
            "source_tree_dirty": False,
            "status": "FAIL",
            "clean_windows_runner_status": "BLOCKER",
            "primary_failure_code": "sanitized_path_leaked_forbidden_tool",
            "failure_code": "sanitized_path_leaked_forbidden_tool",
            "log_scrub_status": "NOT_RUN",
            "log_scrub_failure_code": None,
            "redacted_logs": [],
            "checks": {"redacted_runtime_logs": None},
        },
    )

    result = verifier.verify_smoke(tmp_path, layout=layout, expected_revision=revision)

    assert result["status"] == "BLOCKER"
    assert result["primary_failure_code"] == "sanitized_path_leaked_forbidden_tool"
    assert result["log_scrub_status"] == "NOT_RUN"
    assert result["log_scrub_failure_code"] is None


def test_sanitized_path_inventory_allows_ambient_docker_with_packaged_pg_ctl(
    tmp_path: Path,
) -> None:
    verifier = _load_verifier()
    bundle = r"C:\staging\shared"
    inventory: dict[str, dict[str, Any]] = {
        name: {"resolved": False, "path": None}
        for name in ("python.exe", "python3.exe", "py.exe", "uv.exe", "docker.exe")
    }
    inventory["docker.exe"] = {"resolved": True, "path": r"C:\Windows\System32\docker.exe"}
    inventory["pg_ctl.exe"] = {
        "resolved": True,
        "path": bundle + r"\postgresql\bin\pg_ctl.exe",
    }

    verifier.verify_sanitized_path_inventory(
        {"runtime_bundle_root": bundle, "sanitized_path_tool_inventory": inventory},
        tmp_path / "smoke.json",
    )


def test_sanitized_path_inventory_rejects_resolved_host_tool(tmp_path: Path) -> None:
    verifier = _load_verifier()
    bundle = r"C:\staging\split"
    inventory: dict[str, dict[str, Any]] = {
        name: {"resolved": False, "path": None}
        for name in ("python.exe", "python3.exe", "py.exe", "uv.exe", "docker.exe")
    }
    inventory["py.exe"] = {"resolved": True, "path": r"C:\Windows\py.exe"}
    inventory["pg_ctl.exe"] = {
        "resolved": True,
        "path": bundle + r"\postgresql\bin\pg_ctl.exe",
    }

    with pytest.raises(SystemExit, match="forbidden host tools"):
        verifier.verify_sanitized_path_inventory(
            {"runtime_bundle_root": bundle, "sanitized_path_tool_inventory": inventory},
            tmp_path / "smoke.json",
        )


@pytest.mark.parametrize("tool_name", ["python.exe", "uv.exe"])
def test_sanitized_path_inventory_rejects_host_python_and_uv(
    tmp_path: Path, tool_name: str
) -> None:
    verifier = _load_verifier()
    bundle = r"C:\staging\split"
    inventory: dict[str, dict[str, Any]] = {
        name: {"resolved": False, "path": None}
        for name in ("python.exe", "python3.exe", "py.exe", "uv.exe", "docker.exe")
    }
    inventory[tool_name] = {"resolved": True, "path": rf"C:\host\{tool_name}"}
    inventory["pg_ctl.exe"] = {
        "resolved": True,
        "path": bundle + r"\postgresql\bin\pg_ctl.exe",
    }

    with pytest.raises(SystemExit, match="forbidden host tools"):
        verifier.verify_sanitized_path_inventory(
            {"runtime_bundle_root": bundle, "sanitized_path_tool_inventory": inventory},
            tmp_path / "smoke.json",
        )


def test_sanitized_path_inventory_rejects_host_postgres(tmp_path: Path) -> None:
    verifier = _load_verifier()
    bundle = r"C:\staging\split"
    inventory: dict[str, dict[str, Any]] = {
        name: {"resolved": False, "path": None}
        for name in ("python.exe", "python3.exe", "py.exe", "uv.exe", "docker.exe")
    }
    inventory["pg_ctl.exe"] = {
        "resolved": True,
        "path": r"C:\Program Files\PostgreSQL\bin\pg_ctl.exe",
    }

    with pytest.raises(SystemExit, match="packaged pg_ctl"):
        verifier.verify_sanitized_path_inventory(
            {"runtime_bundle_root": bundle, "sanitized_path_tool_inventory": inventory},
            tmp_path / "smoke.json",
        )


def test_hosted_process_verifier_rejects_unscrubbed_bearer_output(tmp_path: Path) -> None:
    verifier = _load_verifier()
    layout = "split"
    revision = "c" * 40
    layout_root = tmp_path / layout
    _process_evidence(layout_root, layout=layout, revision=revision)

    stdout_path = layout_root / "hosted-smoke-process" / "stdout.redacted.log"
    stdout_path.write_text(f"Authorization: Bearer {'A' * 32}\n", encoding="utf-8")
    process_path = layout_root / "smoke-process.json"
    process = json.loads(process_path.read_text(encoding="utf-8"))
    stdout_record = next(
        item
        for item in process["sanitized_diagnostics"]
        if item["name"].endswith("stdout.redacted.log")
    )
    content = stdout_path.read_bytes()
    stdout_record["size_bytes"] = len(content)
    stdout_record["sha256"] = hashlib.sha256(content).hexdigest()
    process_path.write_text(json.dumps(process), encoding="utf-8")

    with pytest.raises(SystemExit, match="Unredacted hosted smoke process output"):
        verifier.verify_hosted_process_diagnostics(
            layout_root, layout=layout, expected_revision=revision
        )


def test_verification_artifact_records_both_layouts_after_one_smoke_fails(
    tmp_path: Path, monkeypatch: Any
) -> None:
    verifier = _load_verifier()
    revision = "b" * 40
    candidate_root = tmp_path / "candidates"
    candidate_root.mkdir()
    uv_lock = tmp_path / "uv.lock"
    uv_lock.write_text("locked", encoding="utf-8")
    postgres_archive = tmp_path / "postgres.zip"
    postgres_archive.write_bytes(b"pinned postgres archive")
    postgres_digest = hashlib.sha256(postgres_archive.read_bytes()).hexdigest()
    download_manifest = tmp_path / "download-manifest.json"
    _write_json(
        download_manifest,
        {
            "postgresql": {
                "sha256": postgres_digest,
                "content_bytes": postgres_archive.stat().st_size,
            }
        },
    )
    evidence_root = tmp_path / "hosted"
    _write_json(
        evidence_root / "hosted-runner-preflight.json",
        {
            "source_revision": revision,
            "github_actions": True,
            "runner_environment": "github-hosted",
            "runner_os": "Windows",
            "runner_arch": "X64",
            "workflow_runner_label": "windows-2025",
            "architecture_x64": True,
            "runner_image": "windows-2025",
            "runner_ambient_path_prerequisites": {
                "python": True,
                "python_launcher": True,
                "uv": True,
                "docker": True,
                "postgresql": True,
            },
            "runner_process_is_administrator": True,
        },
    )
    result_path = tmp_path / "runtime-evidence-verification.json"

    def verify_candidate(_candidate: Path, *, layout: str, **_kwargs: Any) -> dict[str, Any]:
        return {"layout": layout, "status": "PASS"}

    monkeypatch.setattr(verifier, "verify_candidate", verify_candidate)

    def verify_layout(_root: Path, *, layout: str, expected_revision: str) -> dict[str, Any]:
        if layout == "shared":
            return {
                "layout": layout,
                "status": "BLOCKER",
                "primary_failure_code": "packaged_http_exited_before_ready",
                "log_scrub_status": "FAIL",
                "child_exit_code": 1,
            }
        return {
            "layout": layout,
            "status": "PASS",
            "primary_failure_code": None,
            "log_scrub_status": "PASS",
            "child_exit_code": 0,
        }

    monkeypatch.setattr(verifier, "verify_smoke", verify_layout)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--candidate-root",
            str(candidate_root),
            "--expected-source-revision",
            revision,
            "--uv-lock",
            str(uv_lock),
            "--download-manifest",
            str(download_manifest),
            "--postgres-archive",
            str(postgres_archive),
            "--smoke-evidence-root",
            str(evidence_root),
            "--result-path",
            str(result_path),
        ],
    )

    exit_code = verifier.main()
    result = json.loads(result_path.read_text(encoding="utf-8"))

    assert exit_code == 1
    assert result["status"] == "BLOCKER"
    assert [smoke["layout"] for smoke in result["hosted_smokes"]] == ["shared", "split"]
    assert result["hosted_smokes"][0]["primary_failure_code"] == (
        "packaged_http_exited_before_ready"
    )
    assert result["hosted_smokes"][1]["status"] == "PASS"


def test_verification_accepts_only_the_selected_runtime_layout(
    tmp_path: Path, monkeypatch: Any
) -> None:
    verifier = _load_verifier()
    revision = "d" * 40
    uv_lock = tmp_path / "uv.lock"
    uv_lock.write_text("locked", encoding="utf-8")
    postgres_archive = tmp_path / "postgres.zip"
    postgres_archive.write_bytes(b"pinned postgres archive")
    download_manifest = tmp_path / "download-manifest.json"
    _write_json(
        download_manifest,
        {
            "postgresql": {
                "sha256": hashlib.sha256(postgres_archive.read_bytes()).hexdigest(),
                "content_bytes": postgres_archive.stat().st_size,
            }
        },
    )
    candidate_root = tmp_path / "candidates"
    result_path = tmp_path / "runtime-evidence-verification.json"

    def verify_selected(candidate: Path, *, layout: str, **_kwargs: Any) -> dict[str, Any]:
        assert layout == "shared"
        assert candidate == candidate_root / "shared"
        return {"layout": layout}

    monkeypatch.setattr(verifier, "verify_candidate", verify_selected)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--candidate-root",
            str(candidate_root),
            "--layouts",
            "shared",
            "--expected-source-revision",
            revision,
            "--uv-lock",
            str(uv_lock),
            "--download-manifest",
            str(download_manifest),
            "--postgres-archive",
            str(postgres_archive),
            "--result-path",
            str(result_path),
        ],
    )

    exit_code = verifier.main()
    result = json.loads(result_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert result["status"] == "PASS"
    assert [candidate["layout"] for candidate in result["candidates"]] == ["shared"]
