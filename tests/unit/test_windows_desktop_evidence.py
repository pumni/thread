from __future__ import annotations

import ast
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
SCENARIO_AGGREGATOR = (
    REPO_ROOT / "packaging" / "windows_desktop" / "aggregate_controller_scenarios.py"
)
CONTROLLER_SCENARIOS = (
    "bootstrap_https_cutover_tray",
    "restart_renewal",
    "database_crash_recovery",
    "parent_crash_recovery",
    "migration_recovery_auth",
    "database_port_collision",
    "endpoint_port_collision",
    "unowned_root",
    "unwritable_root",
    "corrupt_cluster",
)
FROZEN_CONTROLLER_CHECKS = (
    "clean_profile",
    "runtime_bundle_shared",
    "controller_root_current_user_acl",
    "dpapi_current_user_round_trip",
    "atomic_non_secret_config",
    "controller_https_configuration_persisted",
    "endpoint_reconfigure_before_owner",
    "endpoint_unavailable_ip_rolls_back",
    "endpoint_collision_rolls_back",
    "endpoint_running_unavailable_ip_rolls_back",
    "endpoint_running_unavailable_ip_preserves_owner_session",
    "endpoint_running_collision_rolls_back",
    "endpoint_running_collision_preserves_owner_session",
    "endpoint_running_transition_preserves_postgres",
    "endpoint_running_transition_revokes_owner_session",
    "endpoint_running_transition_reauthenticates_one_owner_session",
    "no_lan_listener_before_local_owner_bootstrap",
    "loopback_postgres_wildcard_https_listener",
    "controller_https_root_fingerprint_matches_ui",
    "local_readiness_uses_private_root",
    "plaintext_health_rejected",
    "serving_leaf_key_cleaned_on_shutdown",
    "stale_serving_leaf_key_replaced_on_startup",
    "leaf_renewal_preserves_root_identity",
    "root_identity_persists_across_restart",
    "no_owner_or_lan_bootstrap",
    "local_first_owner_bootstrap",
    "separate_http_and_scheduler_processes",
    "x_hides_and_runtime_continues",
    "reopen_keeps_one_runtime_and_database_identity",
    "reopen_requires_operator_sign_in",
    "owner_reauthenticated_after_reopen",
    "active_session_verified_before_privileged_quit",
    "graceful_quit_stops_scheduler_http_then_postgres",
    "relaunch_preserves_database_and_endpoint",
    "database_crash_fails_closed_and_recovers_wal",
    "desktop_parent_crash_owns_process_tree_and_recovers_wal",
    "controller_root_identity_survives_crash_restart",
    "failed_migration_preserves_existing_cluster",
    "database_port_collision_does_not_rotate",
    "endpoint_port_collision_does_not_rotate",
    "unowned_root_is_preserved_and_rejected",
    "unwritable_root_is_rejected",
    "corrupt_cluster_is_preserved_and_rejected",
)


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
            'Get-InputLookupDiagnostics', 'Add-InputMutationEvidence',
            'Send-InputKeyboardValue', 'Test-ResolvedInputValue',
            'Invoke-ResolvedInputMutation')
}}, $true))
if ($functionNodes.Count -ne 13) {{ throw "Expected the focused Controller input helpers" }}
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
$mutationCalls = @($commands | Where-Object {{
    $_.GetCommandName() -eq 'Invoke-ResolvedInputMutation'
}})
if ($waitCalls.Count -ne 1 -or $mutationCalls.Count -ne 1 -or
    $waitCalls[0].Extent.StartOffset -ge $mutationCalls[0].Extent.StartOffset -or
    $waitCalls[0].Extent.Text -notmatch '\}}\s+20\s+\$inputUnavailableCode' -or
    $loginBody.Extent.Text -match 'SendWait|\.SetFocus\(') {{
    throw "Bounded exact-control lookup must complete before delegated input mutation"
}}

$mutationBody = $functions['Invoke-ResolvedInputMutation'].Body
$mutationCommands = @($mutationBody.FindAll({{
    param($node) $node -is [System.Management.Automation.Language.CommandAst]
}}, $true))
$mutationWaits = @($mutationCommands | Where-Object {{ $_.GetCommandName() -eq 'Wait-Until' }} |
    Sort-Object {{ $_.Extent.StartOffset }})
$keyboardCalls = @($mutationCommands | Where-Object {{
    $_.GetCommandName() -eq 'Send-InputKeyboardValue'
}})
$mutationFocusCalls = Get-Invocations $mutationBody 'SetFocus'
$setValueCalls = Get-Invocations $mutationBody 'SetValue'
if ($mutationWaits.Count -ne 2 -or $keyboardCalls.Count -ne 1 -or
    $mutationFocusCalls.Count -ne 1 -or $setValueCalls.Count -ne 1 -or
    $mutationFocusCalls[0].Extent.StartOffset -ge $mutationWaits[0].Extent.StartOffset -or
    $mutationWaits[0].Extent.StartOffset -ge $keyboardCalls[0].Extent.StartOffset -or
    $keyboardCalls[0].Extent.StartOffset -ge $mutationWaits[1].Extent.StartOffset -or
    $mutationBody.Extent.Text -notmatch 'desktop_input_focus_not_acquired_\$FieldId' -or
    $mutationBody.Extent.Text -notmatch 'HasKeyboardFocus') {{
    throw "Keyboard fallback must prove focus before typing and verify afterward"
}}
$programmaticGate = @($mutationBody.FindAll({{
    param($node)
    $node -is [System.Management.Automation.Language.IfStatementAst] -and
        $node.Clauses[0].Item1.Extent.Text -match '-not\s+\$isPassword\s+-and\s+\$isEdit'
}}, $true))
if ($programmaticGate.Count -ne 1 -or
    $programmaticGate[0].Extent.Text -notmatch 'ValuePattern' -or
    $programmaticGate[0].Extent.Text -notmatch 'IsReadOnly' -or
    $programmaticGate[0].Extent.Text -notmatch '\.SetValue\(\$Value\)' -or
    $programmaticGate[0].Extent.Text -notmatch 'Test-ResolvedInputValue') {{
    throw "Writable non-password Edit must try and verify ValuePattern before keyboard fallback"
}}
$keyboardBody = $functions['Send-InputKeyboardValue'].Body.Extent.Text
$sendCalls = @($functions['Send-InputKeyboardValue'].Body.FindAll({{
    param($node)
    $node -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -and
        $node.Member.Extent.Text -eq 'SendWait'
}}, $true))
if ($sendCalls.Count -ne 3 -or
    $keyboardBody -notmatch 'SendWait\("\^a"\)' -or
    $keyboardBody -notmatch 'SendWait\("\{{BACKSPACE\}}"\)' -or
    $keyboardBody -notmatch 'SendWait\(\$Value\)') {{
    throw "Keyboard fallback must select all, delete, and enter the value"
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

$verificationBody = $functions['Test-ResolvedInputValue'].Body.Extent.Text
if ($verificationBody -notmatch '\[System\.Windows\.Automation\.RangeValuePattern\]::Pattern' -or
    $verificationBody -notmatch '\[System\.Windows\.Automation\.ValuePattern\]::Pattern' -or
    $verificationBody -notmatch '\[double\]\$rangePattern\.Current\.Value' -or
    $verificationBody -notmatch '\[int\]::TryParse' -or
    $verificationBody -notmatch '\[System\.StringComparison\]::Ordinal' -or
    $verificationBody -notmatch 'Find-ElementByType\s+\$current' -or
    $verificationBody -notmatch '\[System\.Windows\.Automation\.ControlType\]::Edit') {{
    throw "Verification must preserve exact text, numeric-safe Spinner, and scoped Edit fallback"
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
$evidenceBody = $functions['Invoke-ResolvedInputMutation'].Body.Extent.Text
foreach ($safeField in @('field_id', 'control_type', 'value_pattern_supported',
        'value_pattern_read_only', 'mutation_method', 'focus_requested',
        'focus_confirmed', 'verification_performed', 'verification_succeeded',
        'failure_code')) {{
    if ($evidenceBody -notmatch "(?m)$safeField\s*=") {{
        throw "Input mutation evidence omits safe field $safeField"
    }}
}}
if ($evidenceBody -notmatch 'if\s*\(\$isPassword\).*Remove\("observed_value_length"\)' -or
    $evidenceBody -match 'password.*Current\.Value|Current\.Value.*password') {{
    throw "Password evidence must omit length and never read the secret back"
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


def test_controller_input_mutation_is_pattern_first_focus_gated_and_secret_safe() -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell mutation-path test is only available on Windows test hosts")

    smoke_path = str(CONTROLLER_SMOKE).replace("'", "''")
    assertion = rf"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    '{smoke_path}', [ref]$tokens, [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) {{ throw "Controller smoke script did not parse" }}
$helperNames = @('Add-InputMutationEvidence', 'Test-ResolvedInputValue',
    'Invoke-ResolvedInputMutation')
$helperNodes = @($ast.FindAll({{
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -in $helperNames
}}, $true))
if ($helperNodes.Count -ne $helperNames.Count) {{ throw "Input mutation helpers missing" }}
foreach ($helperNode in $helperNodes) {{ Invoke-Expression $helperNode.Extent.Text }}

function Wait-Until([scriptblock]$Condition, [int]$TimeoutSeconds, [string]$Failure) {{
    if (& $Condition) {{ return }}
    throw $Failure
}}
function Find-ElementByType($Root, $ControlType) {{ return $null }}
function Send-InputKeyboardValue([string]$Value) {{
    if (-not $script:activeControl.Current.HasKeyboardFocus) {{
        throw 'synthetic focus invariant failed'
    }}
    $script:sendCount++
    if ($script:keyboardCorrupt) {{
        $script:activeControl.ValuePattern.Current.Value = 'different'
    }} else {{
        $script:activeControl.ValuePattern.Current.Value = $Value
    }}
}}
function New-FakeControl(
    [string]$PatternMode = 'available',
    [bool]$FocusWorks = $true,
    [bool]$ReadOnly = $false
) {{
    $pattern = [pscustomobject]@{{
        Mode = $PatternMode
        SetCount = 0
        Current = [pscustomobject]@{{ IsReadOnly = $ReadOnly; Value = 'old' }}
    }}
    Add-Member -InputObject $pattern -MemberType ScriptMethod -Name SetValue -Value {{
        param([string]$NewValue)
        $this.SetCount++
        if ($this.Mode -eq 'throw_set') {{ throw 'synthetic pattern failure' }}
        if ($this.Mode -eq 'mismatch') {{
            $this.Current.Value = $NewValue.ToUpperInvariant()
        }} else {{
            $this.Current.Value = $NewValue
        }}
    }}
    $control = [pscustomobject]@{{
        PatternMode = $PatternMode
        PatternCalls = 0
        FocusWorks = $FocusWorks
        FocusCalls = 0
        ValuePattern = $pattern
        Current = [pscustomobject]@{{
            ControlType = [pscustomobject]@{{ ProgrammaticName = 'ControlType.Edit' }}
            HasKeyboardFocus = $false
        }}
    }}
    Add-Member -InputObject $control -MemberType ScriptMethod -Name GetCurrentPattern -Value {{
        param($RequestedPattern)
        $this.PatternCalls++
        if ($this.PatternMode -eq 'unavailable') {{ throw 'synthetic unsupported pattern' }}
        if ($this.PatternMode -eq 'initial_unavailable' -and $this.PatternCalls -eq 1) {{
            throw 'synthetic transient pattern unavailability'
        }}
        return $this.ValuePattern
    }}
    Add-Member -InputObject $control -MemberType ScriptMethod -Name SetFocus -Value {{
        $this.FocusCalls++
        if ($this.FocusWorks) {{ $this.Current.HasKeyboardFocus = $true }}
    }}
    return $control
}}
function Invoke-TestMutation(
    [string]$FieldId,
    [string]$Value,
    [string]$PatternMode = 'available',
    [bool]$FocusWorks = $true,
    [bool]$KeyboardCorrupt = $false
) {{
    $script:activeControl = New-FakeControl $PatternMode $FocusWorks
    $script:inputMutationEvidence = [System.Collections.Generic.List[object]]::new()
    $script:sendCount = 0
    $script:keyboardCorrupt = $KeyboardCorrupt
    $script:failure = $null
    $resolve = {{ return $script:activeControl }}
    $expectedPort = if ($FieldId -eq 'https_port') {{ [int]$Value }} else {{ 0 }}
    try {{
        Invoke-ResolvedInputMutation `
            -FieldId $FieldId -Value $Value -ResolveControl $resolve -ExpectedPort $expectedPort
    }} catch {{ $script:failure = $_.Exception.Message }}
    $evidence = if ($script:inputMutationEvidence.Count -eq 1) {{
        $script:inputMutationEvidence[0]
    }} else {{ $null }}
    return [ordered]@{{
        failure = $script:failure
        sends = $script:sendCount
        set_count = $script:activeControl.ValuePattern.SetCount
        pattern_calls = $script:activeControl.PatternCalls
        focus_calls = $script:activeControl.FocusCalls
        evidence_count = $script:inputMutationEvidence.Count
        evidence = $evidence
        evidence_json = ConvertTo-Json -InputObject $evidence -Compress -Depth 5
    }}
}}

$valuePatternSuccess = Invoke-TestMutation 'username' 'synthetic-user'
$unsupportedPatternFallback = Invoke-TestMutation 'username' 'synthetic-user' 'initial_unavailable'
$unverifiedPatternFallback = Invoke-TestMutation 'username' 'synthetic-user' 'mismatch'
$setFailureFallback = Invoke-TestMutation 'username' 'synthetic-user' 'throw_set'
$focusFailure = Invoke-TestMutation 'username' 'synthetic-user' 'unavailable' $false
$verificationFailure = Invoke-TestMutation 'username' 'synthetic-user' 'unavailable' $true $true
$secret = 'synthetic-password-never-in-evidence'
$passwordFallback = Invoke-TestMutation 'password' $secret
$result = [ordered]@{{
    value_pattern_success = $valuePatternSuccess
    unsupported_pattern_fallback = $unsupportedPatternFallback
    unverified_pattern_fallback = $unverifiedPatternFallback
    set_failure_fallback = $setFailureFallback
    focus_failure = $focusFailure
    verification_failure = $verificationFailure
    password_fallback = $passwordFallback
    password_secret_leaked = $passwordFallback.evidence_json.Contains($secret)
}}
[Console]::WriteLine(($result | ConvertTo-Json -Compress -Depth 8))
"dynamic input mutation proof PASS"
"""
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", assertion],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}"
    result = json.loads(completed.stdout.splitlines()[0])

    value_pattern = result["value_pattern_success"]
    assert value_pattern["failure"] is None
    assert value_pattern["sends"] == 0
    assert value_pattern["set_count"] == 1
    assert value_pattern["evidence_json"].find("synthetic-user") == -1
    assert value_pattern["evidence"]["mutation_method"] == "VALUE_PATTERN"
    assert value_pattern["evidence"]["verification_succeeded"] is True
    assert value_pattern["evidence"]["observed_value_length"] == len("synthetic-user")

    for key in (
        "unsupported_pattern_fallback",
        "unverified_pattern_fallback",
        "set_failure_fallback",
    ):
        fallback = result[key]
        assert fallback["failure"] is None
        assert fallback["sends"] == 1
        assert fallback["evidence"]["mutation_method"] == "KEYBOARD"
        assert fallback["evidence"]["focus_requested"] is True
        assert fallback["evidence"]["focus_confirmed"] is True
        assert fallback["evidence"]["verification_succeeded"] is True
    assert result["unverified_pattern_fallback"]["set_count"] == 1
    assert (
        result["unverified_pattern_fallback"]["evidence"]["programmatic_verification_succeeded"]
        is False
    )
    assert (
        result["unverified_pattern_fallback"]["evidence"]["programmatic_failure_code"]
        == "desktop_input_value_not_populated_username"
    )
    assert (
        result["set_failure_fallback"]["evidence"]["programmatic_failure_code"]
        == "desktop_input_value_pattern_set_failed_username"
    )

    focus_failure = result["focus_failure"]
    assert focus_failure["failure"] == "desktop_input_focus_not_acquired_username"
    assert focus_failure["sends"] == 0
    assert focus_failure["evidence_count"] == 1
    assert focus_failure["evidence"]["failure_code"] == focus_failure["failure"]
    assert focus_failure["evidence"]["focus_confirmed"] is False

    verification_failure = result["verification_failure"]
    assert verification_failure["failure"] == "desktop_input_value_not_populated_username"
    assert verification_failure["sends"] == 1
    assert verification_failure["evidence_count"] == 1
    assert verification_failure["evidence"]["verification_performed"] is True
    assert verification_failure["evidence"]["verification_succeeded"] is False

    password = result["password_fallback"]
    password_record = password["evidence"]
    assert result["password_secret_leaked"] is False
    assert password["failure"] is None
    assert password["sends"] == 1
    assert password["set_count"] == 0
    assert password["pattern_calls"] == 0
    assert password_record["verification_performed"] is False
    assert password_record["verification_succeeded"] is None
    assert "observed_value_length" not in password_record
    secret_hash = hashlib.sha256(b"synthetic-password-never-in-evidence").hexdigest()
    for forbidden in (secret_hash, "bearer", "token", "authorization"):
        assert forbidden not in password["evidence_json"].lower()
    forbidden_fields = {
        "password",
        "password_value",
        "password_hash",
        "password_length",
        "value",
        "raw_value",
        "bearer",
        "token",
        "authorization",
    }
    assert not (forbidden_fields & password_record.keys())

    assert "dynamic input mutation proof PASS" in completed.stdout


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

    fixture_helper = source[
        source.index("function Initialize-HealthyControllerFixture") : source.index(
            "\ntry {", source.index("function Initialize-HealthyControllerFixture")
        )
    ]
    owner_call = fixture_helper.index("Bootstrap-ControllerOwner $ProcessId")
    after_owner_probe = fixture_helper.index(
        "Wait-ForControllerHttps $Config 60 -AfterOwnerBootstrap"
    )
    enabled_owner_check = fixture_helper.index("$enabledOwnerCount = Invoke-Psql", owner_call)
    assert owner_call < after_owner_probe < enabled_owner_check
    assert "$fixture = Initialize-HealthyControllerFixture $desktop.Id $config" in source

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
    assert '"post_owner_runtime_probe"' in source
    assert "process_evidence = $processEvidence" in source
    assert "operator_session_timeline = @($operatorSessionTimeline)" in source


def test_post_owner_transport_classifier_ignores_owner_session_count() -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell runtime classifier test is only available on Windows test hosts")

    source = CONTROLLER_SMOKE.read_text(encoding="utf-8")
    classifier = source[
        source.index("function Get-PostOwnerRuntimeFailureCode") : source.index(
            "function Get-ControllerHttpsProbeFailureCode"
        )
    ]
    assertion = f"""
{classifier}
$runtime = @{{
    process_query_outcome = 'PASS'
    postgres_count = 1
    http_count = 1
    scheduler_count = 1
    listener_query_outcome = 'PASS'
    endpoint_listener_count = 1
    owner_session_query_outcome = 'PASS'
    active_owner_session_count = 1
}}
$withOneOwner = [pscustomobject]($runtime.Clone())
$withNoOwner = [pscustomobject]($runtime.Clone())
$withNoOwner.active_owner_session_count = 0
$oneFailure = Get-PostOwnerRuntimeFailureCode $withOneOwner
$zeroFailure = Get-PostOwnerRuntimeFailureCode $withNoOwner
if ($null -ne $oneFailure -or $null -ne $zeroFailure) {{
    throw "owner_session_count_changed_transport_classification"
}}
"runtime classifier accepts owner session counts 0 and 1"
"""
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", assertion],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}"
    assert "runtime classifier accepts owner session counts 0 and 1" in completed.stdout


def test_operator_session_waiter_records_named_transitions() -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell session timeline test is only available on Windows test hosts")

    source = CONTROLLER_SMOKE.read_text(encoding="utf-8")
    helper_functions = source[
        source.index("function Add-OperatorSessionTransition") : source.index(
            "function Assert-OneActiveOwnerSession"
        )
    ]
    assertion = f"""
$script:operatorSessionTimeline = [System.Collections.Generic.List[object]]::new()
$script:mockOwnerSessionCount = 0
function Get-ActiveOwnerSessionCount([object]$Config) {{
    return [int]$script:mockOwnerSessionCount
}}
function Get-SafeExceptionTypeName([object]$Exception) {{
    return $Exception.GetType().Name
}}
{helper_functions}
$null = Wait-ForOperatorSessionCount 'local-config' 'window_hide_lock' 0 1 'lock_failed'
$script:mockOwnerSessionCount = 1
$null = Wait-ForOperatorSessionCount 'local-config' 'reopen_after_login' 1 1 'login_failed'
[Console]::WriteLine((ConvertTo-Json `
    -InputObject @($script:operatorSessionTimeline.ToArray()) -Depth 6 -Compress))
"""
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", assertion],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}"
    timeline = json.loads(completed.stdout.strip())
    assert [entry["stage"] for entry in timeline] == ["window_hide_lock", "reopen_after_login"]
    assert [entry["expected_active_owner_session_count"] for entry in timeline] == [0, 1]
    assert [entry["observed_active_owner_session_count"] for entry in timeline] == [0, 1]
    assert [entry["outcome"] for entry in timeline] == ["PASS", "PASS"]


def test_controller_session_timeline_is_separate_and_scenario_scoped() -> None:
    source = CONTROLLER_SMOKE.read_text(encoding="utf-8")
    normalized = re.sub(r"`\s*\r?\n\s*", " ", source)
    classifier = source[
        source.index("function Get-PostOwnerRuntimeFailureCode") : source.index(
            "function Get-ControllerHttpsProbeFailureCode"
        )
    ]
    assert "active_owner_session_count" not in classifier
    assert "owner_session_query_outcome" not in classifier

    waiter = source[
        source.index("function Wait-ForControllerHttps") : source.index(
            "function Test-PlaintextHttpRejected"
        )
    ]
    assert "Wait-ForOperatorSessionCount" not in waiter
    assert "Get-ActiveOwnerSessionCount" not in waiter

    transitions = (
        ('"bootstrap" 1', "controller_first_owner_session_missing"),
        (
            '"failed_ip_reconfigure" 1',
            "controller_endpoint_reconfigure_session_lost_on_unavailable_ip",
        ),
        (
            '"failed_port_reconfigure" 1',
            "controller_endpoint_reconfigure_session_lost_on_collision",
        ),
        ('"before_privileged_quit" 1', "controller_active_owner_session_count_not_one"),
    )
    for stage_call, failure_code in transitions:
        assert stage_call in normalized
        assert failure_code in source
    for stage, failure_code in (
        ("window_hide_lock", "controller_owner_session_not_revoked_on_lock"),
        ("reopen_before_login", "controller_reopen_owner_session_not_locked"),
        ("after_quit_relaunch", "controller_owner_session_exists_after_quit_relaunch"),
        (
            "after_failed_migration_recovery",
            "controller_owner_session_exists_after_failed_migration_recovery",
        ),
    ):
        assert re.search(
            rf'Set-OperatorSessionMode[^\n]*"signed_out"\s+"{re.escape(stage)}"\s+"{re.escape(failure_code)}"',
            normalized,
        )
    session_mode = source[
        source.index("function Set-OperatorSessionMode") : source.index("function Start-Desktop")
    ]
    assert (
        "return Wait-ForOperatorSessionCount `\n        $Config $Stage 0 3 $FailureCode"
        in session_mode
    )
    assert '"cutover_revoke"' in source and '"reopen_before_login"' in source

    ensure_owner = source[
        source.index("function Ensure-ControllerOwner") : source.index("function Start-Desktop")
    ]
    normalized_ensure = re.sub(r"`\s*\r?\n\s*", " ", ensure_owner)
    assert "$config $revocationStage 0 15 $revocationFailure" in normalized_ensure
    assert "Invoke-ObservedOperatorLogin $ProcessId $config $LoginStage" in normalized_ensure
    assert "$config $LoginStage 1 3" in normalized_ensure
    assert 'Ensure-ControllerOwner $desktop.Id "cutover_relogin"' in normalized
    assert 'Ensure-ControllerOwner $desktop.Id "reopen_after_login"' in normalized
    assert "function Quit-Desktop([int]$ProcessId, [string]$LoginStage)" in source
    assert "Ensure-ControllerOwner $ProcessId $LoginStage" in source
    assert 'Wait-ForOperatorSessionCount `\n        $Config "before_privileged_quit" 1' in source

    scenario_blocks = {
        "bootstrap_https_cutover_tray": (
            '"failed_ip_reconfigure"',
            '"failed_port_reconfigure"',
            '"window_hide_lock"',
            '"reopen_before_login"',
            'Ensure-ControllerOwner $desktop.Id "reopen_after_login"',
        ),
        "restart_renewal": ('"after_quit_relaunch"', "leaf_renewal_preserves_root_identity"),
        "database_crash_recovery": (
            '"after_database_crash_recovery"',
            "Assert-DatabaseValue $config $sentinel",
        ),
        "parent_crash_recovery": (
            '"after_parent_crash_recovery"',
            "$scenarioEvidence.parent_crash_after",
        ),
        "migration_recovery_auth": (
            '"after_failed_migration_recovery"',
            'Quit-Desktop $desktop.Id "failed_migration_recovery_quit_login"',
        ),
    }
    for scenario, evidence in scenario_blocks.items():
        start = source.index(f'if ($Scenario -eq "{scenario}")')
        next_scenario = min(
            (
                position
                for other in CONTROLLER_SCENARIOS
                if other != scenario
                and (position := source.find(f'if ($Scenario -eq "{other}")', start + 1)) >= 0
            ),
            default=source.index("$finalProcesses = Get-ControllerProcesses", start),
        )
        block = source[start:next_scenario]
        for item in evidence:
            assert item in block, (scenario, item)

    assert "function Wait-ForOperatorSessionCount" in source
    assert "Add-OperatorSessionTransition" in source
    assert "operator_session_timeline = @($operatorSessionTimeline)" in source
    assert "operator_login_attempt_timeline = @($operatorLoginAttemptTimeline)" in source
    assert "input_mutation_evidence = @($inputMutationEvidence)" in source


def test_bootstrap_requires_serving_key_cleanup_after_graceful_runtime_exit() -> None:
    source = CONTROLLER_SMOKE.read_text(encoding="utf-8")
    scenario_start = source.index("$fixture = Initialize-HealthyControllerFixture")
    bootstrap_start = source.index(
        'if ($Scenario -eq "bootstrap_https_cutover_tray")', scenario_start
    )
    shutdown_start = source.index("$shutdownProcesses = @{", bootstrap_start)
    bootstrap_end = source.index('if ($Scenario -eq "restart_renewal")', shutdown_start)
    shutdown = source[shutdown_start:bootstrap_end]

    runtime_exit = shutdown.index(
        "Wait-Until { (Get-ControllerProcesses).all.Count -eq 0 } 15 "
        '"controller_quit_left_runtime_processes"'
    )
    cleanup_assertion = shutdown.index(
        "$checks.serving_leaf_key_cleaned_on_shutdown = -not "
        "(Test-Path -LiteralPath $servingKeyPath)"
    )
    check_complete = shutdown.index(
        "$checks.graceful_quit_stops_scheduler_http_then_postgres = $true"
    )
    assert runtime_exit < cleanup_assertion < check_complete
    assert 'throw "controller_serving_leaf_key_not_cleaned_on_shutdown"' in shutdown
    bootstrap_checks = source.split("bootstrap_https_cutover_tray = @(", maxsplit=1)[1].split(
        ")", maxsplit=1
    )[0]
    assert '"serving_leaf_key_cleaned_on_shutdown"' in bootstrap_checks


def test_post_crash_reauthentication_boundary_is_attempt_free_and_authoritative() -> None:
    source = CONTROLLER_SMOKE.read_text(encoding="utf-8")
    helper_start = source.index("function Assert-PostCrashOperatorReauthenticationRequired")
    helper_end = source.index("function Get-OperatorLoginUiErrorCategory", helper_start)
    helper = source[helper_start:helper_end]

    assert "Invoke-ObservedOperatorLogin" not in helper
    assert "Invoke-Button" not in helper
    assert "Set-LoginInput" not in helper
    assert ".Invoke()" not in helper
    for field in (
        "client_reauthentication_required",
        "login_ui_ready",
        "server_active_session_count_before_crash",
        "server_active_session_count_after_relaunch",
        "login_success_audit_count_before_crash",
        "login_success_audit_count_after_relaunch",
        "automatic_login_audit_delta",
        "server_session_count_delta",
        "server_session_rows_are_local_bearer_evidence",
    ):
        assert field in helper

    database_start = source.index('if ($Scenario -eq "database_crash_recovery")')
    parent_start = source.index('if ($Scenario -eq "parent_crash_recovery")', database_start)
    migration_start = source.index('if ($Scenario -eq "migration_recovery_auth")', parent_start)
    database_block = source[database_start:parent_start]
    parent_block = source[parent_start:migration_start]
    for block, stage, recovery_marker in (
        (
            database_block,
            "database_crash_reauthentication_boundary",
            "database_crash_fails_closed_and_recovers_wal",
        ),
        (
            parent_block,
            "parent_crash_reauthentication_boundary",
            "desktop_parent_crash_owns_process_tree_and_recovers_wal",
        ),
    ):
        assert "Assert-PostCrashOperatorReauthenticationRequired" in block
        assert f'"{stage}"' in block
        assert recovery_marker in block
        assert 'Set-OperatorSessionMode $desktop.Id $config "signed_out"' not in block
    assert "function Get-PostCrashOperatorAuthState" in source
    auth_state = source[source.index("function Get-PostCrashOperatorAuthState") : helper_start]
    assert "event_type LIKE 'operator.login_%'" in auth_state
    assert "operator_login_attempt_timeline_count" in auth_state


def test_post_crash_reauthentication_helper_dynamically_preserves_historical_server_rows() -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell crash-auth boundary test is only available on Windows test hosts")

    source = CONTROLLER_SMOKE.read_text(encoding="utf-8")
    helper_start = source.index("function Assert-PostCrashOperatorReauthenticationRequired")
    helper_end = source.index("function Get-OperatorLoginUiErrorCategory", helper_start)
    helper = source[helper_start:helper_end]
    assertion = f"""
$script:scenarioEvidence = [ordered]@{{}}
$script:operatorLoginAttemptTimeline = [System.Collections.Generic.List[object]]::new()
foreach ($attempt in 1..4) {{ $script:operatorLoginAttemptTimeline.Add($attempt) | Out-Null }}
$script:submissionCount = 0
$script:mockUi = [pscustomobject]@{{
    Ready = $true
    UsernamePresent = $true
    UsernameEnabled = $true
    UsernameFocusable = $true
    PasswordPresent = $true
    PasswordEnabled = $true
    PasswordFocusable = $true
    SignInPresent = $true
    SignInEnabled = $true
    SignInInvokePatternAvailable = $true
}}
$script:mockAfter = [pscustomobject]@{{
    server_active_session_count = 1
    login_success_audit_count = 9
    login_audit_event_count = 12
    operator_login_attempt_timeline_count = 4
}}
function Wait-Until([scriptblock]$Condition, [int]$TimeoutSeconds, [string]$Failure) {{
    if (-not (& $Condition)) {{ throw $Failure }}
}}
function Get-OperatorLoginUiState([int]$ProcessId) {{ return $script:mockUi }}
function Get-PostCrashOperatorAuthState([object]$Config) {{ return $script:mockAfter }}
function Invoke-ObservedOperatorLogin {{ $script:submissionCount++ }}
function Invoke-Button {{ $script:submissionCount++ }}
{helper}
$before = [pscustomobject]@{{
    server_active_session_count = 1
    login_success_audit_count = 9
    login_audit_event_count = 12
    operator_login_attempt_timeline_count = 4
}}
$persisted = Assert-PostCrashOperatorReauthenticationRequired 42 'synthetic-config' `
    'database_crash_reauthentication_boundary' $before
$script:mockUi.Ready = $false
$uiFailure = $null
try {{
    $null = Assert-PostCrashOperatorReauthenticationRequired 42 'synthetic-config' `
        'ui_failure' $before
}} catch {{ $uiFailure = $_.Exception.Message }}
$script:mockUi.Ready = $true
$script:mockAfter.login_success_audit_count = 10
$script:mockAfter.login_audit_event_count = 13
$auditFailure = $null
try {{
    $null = Assert-PostCrashOperatorReauthenticationRequired 42 'synthetic-config' `
        'audit_failure' $before
}} catch {{ $auditFailure = $_.Exception.Message }}
$script:mockAfter.login_success_audit_count = 9
$script:mockAfter.login_audit_event_count = 12
$script:mockAfter.server_active_session_count = 2
$sessionFailure = $null
try {{
    $null = Assert-PostCrashOperatorReauthenticationRequired 42 'synthetic-config' `
        'session_failure' $before
}} catch {{ $sessionFailure = $_.Exception.Message }}
[Console]::WriteLine((ConvertTo-Json -InputObject @{{
    persisted = $persisted
    ui_failure = $uiFailure
    audit_failure = $auditFailure
    session_failure = $sessionFailure
    submission_count = $script:submissionCount
    persisted_evidence = $script:scenarioEvidence.database_crash_reauthentication_boundary
}} -Depth 8 -Compress))
"""
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", assertion],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}"
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    evidence = result["persisted_evidence"]
    assert result["ui_failure"] == "controller_post_crash_login_ui_not_actionable"
    assert result["audit_failure"] == "controller_post_crash_automatic_login_detected"
    assert result["session_failure"] == "controller_post_crash_server_session_count_changed"
    assert result["submission_count"] == 0
    assert result["persisted"]["client_reauthentication_required"] is True
    assert result["persisted"]["login_ui_ready"] is True
    assert evidence["server_active_session_count_before_crash"] == 1
    assert evidence["server_active_session_count_after_relaunch"] == 1
    assert evidence["login_success_audit_count_before_crash"] == 9
    assert evidence["login_success_audit_count_after_relaunch"] == 9
    assert evidence["automatic_login_audit_delta"] == 0
    assert evidence["login_success_audit_delta"] == 0
    assert evidence["server_session_count_delta"] == 0
    assert evidence["operator_login_attempt_timeline_delta"] == 0
    assert evidence["server_session_rows_are_local_bearer_evidence"] is False


def test_controller_scenario_auth_timeline_does_not_equate_crash_rows_with_local_login() -> None:
    source = CONTROLLER_SMOKE.read_text(encoding="utf-8")
    normalized = re.sub(r"`\s*\r?\n\s*", " ", source)
    for scenario, stage in (
        ("database_crash_recovery", "database_crash_reauthentication_boundary"),
        ("parent_crash_recovery", "parent_crash_reauthentication_boundary"),
    ):
        start = source.index(f'if ($Scenario -eq "{scenario}")')
        next_scenario = (
            source.index('if ($Scenario -eq "migration_recovery_auth")', start)
            if scenario == "parent_crash_recovery"
            else source.index('if ($Scenario -eq "parent_crash_recovery")', start)
        )
        block = source[start:next_scenario]
        assert f'"{stage}"' in block
        recovery_start = block.index("$desktop = Start-ExistingController")
        recovery = block[recovery_start:]
        assert 'Set-OperatorSessionMode $desktop.Id $config "signed_out"' not in recovery
        assert "Wait-ForOperatorSessionCount" not in recovery
    assert '"window_hide_lock" "controller_owner_session_not_revoked_on_lock"' in normalized
    assert '"reopen_before_login" "controller_reopen_owner_session_not_locked"' in normalized
    assert (
        '"after_quit_relaunch" "controller_owner_session_exists_after_quit_relaunch"' in normalized
    )
    assert (
        '"after_failed_migration_recovery" '
        '"controller_owner_session_exists_after_failed_migration_recovery"' in normalized
    )


def test_controller_runtime_identity_baselines_are_phase_scoped() -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell runtime identity test is only available on Windows test hosts")

    source = CONTROLLER_SMOKE.read_text(encoding="utf-8")
    helpers = source[
        source.index("function Add-ControllerRuntimeIdentitySnapshot") : source.index(
            "function Wait-ForDesktopParentExit"
        )
    ]
    assertion = f"""
$script:controllerRuntimeIdentityTimeline = [System.Collections.Generic.List[object]]::new()
{helpers}
function New-OwnedProcesses([int]$Postgres, [int]$Http, [int]$Scheduler) {{
    return [pscustomobject]@{{
        postgres = @([pscustomobject]@{{ ProcessId = $Postgres }})
        http = @([pscustomobject]@{{ ProcessId = $Http }})
        scheduler = @([pscustomobject]@{{ ProcessId = $Scheduler }})
    }}
}}
function New-ControllerConfig([int]$Port) {{
    return [pscustomobject]@{{
        controllerId = 'controller-identity'
        lanAddress = '192.0.2.10'
        endpointPort = $Port
    }}
}}
$initial = Add-ControllerRuntimeIdentitySnapshot 'after_owner_bootstrap' `
    (New-OwnedProcesses 100 200 300) (New-ControllerConfig 52030)
$cutover = Add-ControllerRuntimeIdentitySnapshot 'after_endpoint_cutover' `
    (New-OwnedProcesses 100 201 301) (New-ControllerConfig 52105)
Assert-ControllerRuntimeIdentityCutover $initial $cutover
$hidden = Add-ControllerRuntimeIdentitySnapshot 'after_window_hide' `
    (New-OwnedProcesses 100 201 301) (New-ControllerConfig 52105)
Assert-ControllerRuntimeIdentityContinuity $cutover $hidden 'hide_identity_changed'
$reopen = Add-ControllerRuntimeIdentitySnapshot 'after_tray_reopen' `
    (New-OwnedProcesses 100 201 301) (New-ControllerConfig 52105)
Assert-ControllerRuntimeIdentityContinuity $cutover $reopen 'reopen_identity_changed'
$staleInitialRejected = $false
try {{
    Assert-ControllerRuntimeIdentityContinuity $initial $reopen 'stale_initial_baseline_accepted'
}} catch {{
    if ($_.Exception.Message -ceq 'stale_initial_baseline_accepted') {{
        $staleInitialRejected = $true
    }} else {{
        throw
    }}
}}
if (-not $staleInitialRejected -or $initial.http_pid -ne 200 -or $initial.scheduler_pid -ne 300) {{
    throw 'phase_specific_process_baseline_invalid'
}}
[Console]::WriteLine((ConvertTo-Json `
    -InputObject @($script:controllerRuntimeIdentityTimeline.ToArray()) -Depth 6 -Compress))
"""
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", assertion],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}"
    timeline = json.loads(completed.stdout.strip())
    assert [entry["stage"] for entry in timeline] == [
        "after_owner_bootstrap",
        "after_endpoint_cutover",
        "after_window_hide",
        "after_tray_reopen",
    ]
    assert [entry["postgres_pid"] for entry in timeline] == [100, 100, 100, 100]
    assert [entry["http_pid"] for entry in timeline] == [200, 201, 201, 201]
    assert [entry["scheduler_pid"] for entry in timeline] == [300, 301, 301, 301]
    assert [entry["endpoint_port"] for entry in timeline] == [52030, 52105, 52105, 52105]


def test_controller_runtime_identity_calls_use_phase_baselines() -> None:
    source = CONTROLLER_SMOKE.read_text(encoding="utf-8")
    normalized = re.sub(r"`[ \t]*\r?\n[ \t]*", " ", source)

    stages = (
        "after_owner_bootstrap",
        "after_endpoint_cutover",
        "after_window_hide",
        "after_tray_reopen",
    )
    stage_positions: list[int] = []
    for stage in stages:
        match = re.search(
            rf'Add-ControllerRuntimeIdentitySnapshot[ \t]*`[ \t]*\r?\n[ \t]*"{stage}"',
            source,
        )
        assert match is not None, stage
        stage_positions.append(match.start())
    assert stage_positions == sorted(stage_positions)
    assert (
        "Assert-ControllerRuntimeIdentityCutover $initialRuntimeIdentity "
        "$postCutoverRuntimeIdentity" in normalized
    )
    assert re.search(
        r"Assert-ControllerRuntimeIdentityContinuity \$postCutoverRuntimeIdentity "
        r"[ \t]*`[ \t]*\r?\n[ \t]*\$windowHideRuntimeIdentity "
        r'"controller_window_hide_changed_runtime_identity"',
        source,
    )
    assert re.search(
        r"Assert-ControllerRuntimeIdentityContinuity \$postCutoverRuntimeIdentity "
        r"[ \t]*`[ \t]*\r?\n[ \t]*\$trayReopenRuntimeIdentity "
        r'"controller_reopen_changed_runtime_identity"',
        source,
    )

    shutdown = source[
        source.index("$shutdownProcesses = @{") : source.index(
            "if ($shutdownProcesses.Values -contains $null)",
            source.index("$shutdownProcesses = @{"),
        )
    ]
    assert "$trayReopenRuntimeIdentity.scheduler_pid" in shutdown
    assert "$trayReopenRuntimeIdentity.http_pid" in shutdown
    assert "$trayReopenRuntimeIdentity.postgres_pid" in shutdown
    assert "$initial" not in shutdown
    assert "$httpPid" not in shutdown and "$schedulerPid" not in shutdown

    for field, variable in (
        ("initial_postgres_pid", "[int]$initialPostgresPid"),
        ("initial_http_pid", "[int]$initialHttpPid"),
        ("initial_scheduler_pid", "[int]$initialSchedulerPid"),
    ):
        assert f"$processEvidence.{field} = {variable}" in source
    assert "controller_runtime_identity_timeline = @($controllerRuntimeIdentityTimeline)" in source


def test_endpoint_collision_classifier_separates_persisted_port_and_live_processes() -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell collision classifier test is only available on Windows test hosts")

    source = CONTROLLER_SMOKE.read_text(encoding="utf-8")
    classifier = source[
        source.index("function Get-EndpointCollisionFailureCode") : source.index(
            "function Get-ControllerConfig"
        )
    ]
    assertion = f"""
{classifier}
$clean = [pscustomobject]@{{
    expected_endpoint_port = 52105
    observed_persisted_endpoint_port = 52105
    postgres_count = 0
    http_count = 0
    scheduler_count = 0
    expected_diagnostic_code = 'controller_endpoint_port_in_use'
    observed_diagnostic_code = 'controller_endpoint_port_in_use'
    diagnostic_wait_completed = $true
}}
$changedPort = [pscustomobject]($clean.PSObject.Copy())
$changedPort.observed_persisted_endpoint_port = 52106
$leftProcesses = [pscustomobject]($clean.PSObject.Copy())
$leftProcesses.postgres_count = 1
$unconfirmedDiagnostic = [pscustomobject]($clean.PSObject.Copy())
$unconfirmedDiagnostic.diagnostic_wait_completed = $false
$results = @(
    (Get-EndpointCollisionFailureCode $clean),
    (Get-EndpointCollisionFailureCode $changedPort),
    (Get-EndpointCollisionFailureCode $leftProcesses),
    (Get-EndpointCollisionFailureCode $unconfirmedDiagnostic)
)
if ($null -ne $results[0] -or
    $results[1] -cne 'controller_endpoint_collision_changed_persisted_endpoint' -or
    $results[2] -cne 'controller_endpoint_collision_left_runtime_processes' -or
    $results[3] -cne 'controller_endpoint_collision_diagnostic_not_confirmed') {{
    throw 'endpoint_collision_classification_invalid'
}}
[Console]::WriteLine(($results -join '|'))
"""
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", assertion],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}"
    assert completed.stdout.strip().endswith(
        "controller_endpoint_collision_changed_persisted_endpoint|"
        "controller_endpoint_collision_left_runtime_processes|"
        "controller_endpoint_collision_diagnostic_not_confirmed"
    )


def test_endpoint_collision_artifact_records_endpoint_diagnostic_and_process_state() -> None:
    source = CONTROLLER_SMOKE.read_text(encoding="utf-8")
    collision_start = source.index(
        "$endpointReservation = [System.Net.Sockets.TcpListener]::new(",
        source.index('"failed_migration_recovery_quit_login"'),
    )
    collision_end = source.index("$endpointReservation.Stop()", collision_start)
    collision = source[collision_start:collision_end]

    for field in (
        "expected_endpoint_port",
        "observed_persisted_endpoint_port",
        "postgres_count",
        "postgres_pids",
        "http_count",
        "http_pids",
        "scheduler_count",
        "scheduler_pids",
        "expected_diagnostic_code",
        "observed_diagnostic_code",
        "diagnostic_wait_completed",
    ):
        assert field in collision
    assert "Get-EndpointCollisionFailureCode $endpointCollisionEvidence" in collision
    assert '"controller_endpoint_port_in_use"' in collision
    assert '"controller_endpoint_collision_changed_persisted_endpoint"' in source
    assert '"controller_endpoint_collision_left_runtime_processes"' in source
    assert '"controller_endpoint_silently_rotated"' not in source
    assert "endpoint_collision_evidence = $endpointCollisionEvidence" in source


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
        '"failed_ip_reconfigure"'
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
        '"failed_port_reconfigure"'
    )
    assert "controller_endpoint_reconfigure_session_lost_on_collision" in collision_attempt
    assert "$checks.endpoint_running_collision_preserves_owner_session = $true" in collision_attempt

    owner_helper = source[
        source.index("function Ensure-ControllerOwner") : source.index("function Start-Desktop")
    ]
    transition_revoke = owner_helper.index('"controller_endpoint_reconfigure_session_not_revoked"')
    transition_login = owner_helper.index(
        "Invoke-ObservedOperatorLogin $ProcessId $config $LoginStage"
    )
    assert transition_revoke < transition_login
    login_evidence_source = (
        REPO_ROOT / "packaging" / "windows_desktop" / "operator_login_evidence.ps1"
    ).read_text(encoding="utf-8")
    assert "Invoke-ObservedOperatorLogin $ProcessId $config $LoginStage" in owner_helper
    assert "Get-OperatorLoginFailureCode" in source
    assert '"controller_owner_login_failure_unclassified"' in login_evidence_source
    assert "$checks.endpoint_running_transition_revokes_owner_session = $true" in owner_helper
    assert "$checks.endpoint_running_transition_reauthenticates_one_owner_session = $true" in (
        owner_helper
    )
    assert 'Ensure-ControllerOwner $desktop.Id "cutover_relogin"' in source
    assert "-ForceReauthentication -AfterEndpointReconfiguration" in source


def _load_verifier() -> ModuleType:
    spec = importlib.util.spec_from_file_location("windows_desktop_evidence_verifier", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_scenario_aggregator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "aggregate_controller_scenarios", SCENARIO_AGGREGATOR
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_controller_scenario_aggregator_parses_with_python_312_grammar() -> None:
    source = SCENARIO_AGGREGATOR.read_text(encoding="utf-8")
    ast.parse(source, feature_version=(3, 12))


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


def test_controller_login_classifier_uses_only_authoritative_or_allowlisted_evidence() -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell login-classifier test is only available on Windows test hosts")
    module_path = str(
        REPO_ROOT / "packaging" / "windows_desktop" / "operator_login_evidence.ps1"
    ).replace("'", "''")
    assertion = rf"""
. '{module_path}'
$before = [ordered]@{{
    active_owner_session_count_before = 0
    login_failed_attempts = 0
    login_lock_active = $false
    login_success_audit_count_before = 7
    health_probe_outcome_before = 'PASS'
    ready_probe_outcome_before = 'PASS'
}}
function New-After([int]$Sessions, [int]$Failures = 0, [bool]$Locked = $false,
    [string[]]$Events = @(), [string]$Health = 'PASS', [string]$Ready = 'PASS',
    [int]$SuccessAuditCount = 7) {{
    return [ordered]@{{
        active_owner_session_count_after = $Sessions
        login_failed_attempts = $Failures
        login_lock_active = $Locked
        login_success_audit_count_after = $SuccessAuditCount
        recent_owner_audit_event_types_after = $Events
        health_probe_outcome = $Health
        ready_probe_outcome = $Ready
    }}
}}
$historicalAuditSuccess = New-After 0 -Events @('operator.login_succeeded') -SuccessAuditCount 7
$newAuditSuccess = New-After 0 -Events @('operator.login_succeeded') -SuccessAuditCount 8
$outcomes = @(
    (Get-OperatorLoginOutcome $before (New-After 0) $false $false $false 'NONE'),
    (Get-OperatorLoginOutcome $before (New-After 0) $true $false $false 'NONE'),
    (Get-OperatorLoginOutcome $before (New-After 0) $true $true $false 'NONE'),
    (Get-OperatorLoginOutcome $before (New-After 1 -SuccessAuditCount 8) $true $true $true 'NONE'),
    (Get-OperatorLoginOutcome $before (New-After 0 -Failures 1) $true $true $true 'NONE'),
    (Get-OperatorLoginOutcome $before $historicalAuditSuccess $true $true $true 'NONE'),
    (Get-OperatorLoginOutcome $before $newAuditSuccess $true $true $true 'NONE'),
    (Get-OperatorLoginOutcome $before (New-After 0) $true $true $true 'OPERATOR_API_UNAVAILABLE'),
    (Get-OperatorLoginOutcome $before (New-After 0 -Health 'FAIL') $true $true $true 'NONE'),
    (Get-OperatorLoginOutcome $before (New-After 0) $true $true $true 'NONE')
)
[Console]::WriteLine(($outcomes | ConvertTo-Json -Compress))
"""
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", assertion],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}"
    assert json.loads(completed.stdout.strip()) == [
        "LOGIN_UI_NOT_READY",
        "LOGIN_UI_INPUT_FAILED",
        "LOGIN_UI_INVOKE_FAILED",
        "SUCCESS",
        "LOGIN_SERVER_REJECTED",
        "LOGIN_FAILURE_UNCLASSIFIED",
        "LOGIN_SESSION_NOT_PERSISTED",
        "LOGIN_TRANSPORT_UNAVAILABLE",
        "LOGIN_RUNTIME_BECAME_UNREADY",
        "LOGIN_FAILURE_UNCLASSIFIED",
    ]


def test_controller_login_readiness_is_actionable_and_submits_once_without_secret_readback() -> (
    None
):
    source = CONTROLLER_SMOKE.read_text(encoding="utf-8")
    ui_start = source.index("function Get-OperatorLoginUiState")
    ui_end = source.index("function Get-OperatorLoginDatabaseState", ui_start)
    ui_state = source[ui_start:ui_end]
    for required in (
        "UsernamePresent",
        "UsernameEnabled",
        "UsernameFocusable",
        "PasswordPresent",
        "PasswordEnabled",
        "PasswordFocusable",
        "SignInPresent",
        "SignInEnabled",
        "SignInInvokePatternAvailable",
        "IsKeyboardFocusable",
        "IsEnabled",
        "InvokePattern",
    ):
        assert required in ui_state

    observed_start = source.index("function Invoke-ObservedOperatorLogin")
    observed_end = source.index("function Add-OperatorSessionTransition", observed_start)
    observed = source[observed_start:observed_end]
    assert "$uiReady = [bool]$uiState.Ready" in observed
    assert "$inputMutationSucceeded = $false" in observed
    assert "login_ui_ready = $uiReady" in observed
    assert "input_mutation_completed = $inputMutationSucceeded" in observed
    assert "if ($uiReady -and $inputMutationSucceeded)" in observed
    assert "$uiReady = [bool]$uiState.Ready -and $inputMutationSucceeded" not in observed
    assert observed.count("$invokePattern.Invoke()") == 1
    assert observed.count('Set-LoginInput $ProcessId "Username"') == 1
    assert observed.count('Set-LoginInput $ProcessId "Password"') == 1
    assert 'while ($outcome -eq "LOGIN_FAILURE_UNCLASSIFIED"' in observed
    polling = observed[observed.index("while ($outcome") :]
    assert ".Invoke()" not in polling
    assert "Set-LoginInput" not in polling
    assert "SetValue" not in polling
    assert "Send-InputKeyboardValue" not in polling
    assert "SendWait" not in polling
    assert 'Invoke-Button $ProcessId "Sign in"' not in source

    ensure_start = source.index("function Ensure-ControllerOwner")
    ensure_end = source.index("function Start-Desktop", ensure_start)
    ensure_owner = source[ensure_start:ensure_end]
    assert ensure_owner.count("Invoke-ObservedOperatorLogin $ProcessId $config $LoginStage") == 1

    bootstrap_start = source.index("function Bootstrap-ControllerOwner")
    bootstrap_end = source.index("function Ensure-ControllerOwner", bootstrap_start)
    bootstrap = source[bootstrap_start:bootstrap_end]
    assert bootstrap.count('Invoke-Button $ProcessId "Create first Owner"') == 1
    assert bootstrap.count('Set-LoginInput $ProcessId "Username"') == 1
    assert bootstrap.count('Set-LoginInput $ProcessId "Password"') == 1
    assert bootstrap.index('Invoke-Button $ProcessId "Create first Owner"') < bootstrap.index(
        "Wait-Until {"
    )

    timeline_start = observed.index("$script:operatorLoginAttemptTimeline.Add(")
    timeline_end = observed.index(") | Out-Null", timeline_start)
    timeline_record = observed[timeline_start:timeline_end]
    assert "$script:smokeOwnerPassword" not in timeline_record
    assert "$script:smokeOwnerUsername" not in timeline_record
    assert "bearer" not in timeline_record.lower()
    assert "authorization" not in timeline_record.lower()
    assert "$_" not in timeline_record
    assert "operator_login_attempt_timeline = @($operatorLoginAttemptTimeline)" in source
    assert 'Ensure-ControllerOwner $desktop.Id "reopen_after_login"' in source
    for stage in (
        "cutover_relogin",
        "reopen_after_login",
        "graceful_quit_authorization_login",
        "restart_renewal_quit_login",
        "database_crash_failure_shutdown_login",
        "migration_recovery_fixture_quit_login",
        "failed_migration_recovery_quit_login",
        "database_port_collision_fixture_quit_login",
        "endpoint_port_collision_fixture_quit_login",
        "unowned_root_fixture_quit_login",
        "unwritable_root_fixture_quit_login",
        "corrupt_cluster_fixture_quit_login",
    ):
        assert f'"{stage}"' in source
    for safe_category in (
        "SUCCESS",
        "LOGIN_UI_NOT_READY",
        "LOGIN_UI_INPUT_FAILED",
        "LOGIN_UI_INVOKE_FAILED",
        "LOGIN_RUNTIME_BECAME_UNREADY",
        "LOGIN_TRANSPORT_UNAVAILABLE",
        "LOGIN_SERVER_REJECTED",
        "LOGIN_SESSION_NOT_PERSISTED",
        "LOGIN_FAILURE_UNCLASSIFIED",
    ):
        assert safe_category in source or safe_category in (
            REPO_ROOT / "packaging" / "windows_desktop" / "operator_login_evidence.ps1"
        ).read_text(encoding="utf-8")

    classifier = (
        REPO_ROOT / "packaging" / "windows_desktop" / "operator_login_evidence.ps1"
    ).read_text(encoding="utf-8")
    assert "login_success_audit_count_after" in classifier
    assert "login_success_audit_count_before" in classifier
    assert (
        'recent_owner_audit_event_types_after) -contains "operator.login_succeeded"'
        not in classifier
    )


def test_controller_acceptance_scenarios_cover_every_frozen_check() -> None:
    source = CONTROLLER_SMOKE.read_text(encoding="utf-8")
    check_region = source.split("$checks = [ordered]@{", maxsplit=1)[1].split("\n}", maxsplit=1)[0]
    declared_checks = set(re.findall(r"(?m)^\s*([a-z][a-z0-9_]+)\s*=\s*\$false\s*$", check_region))
    assert declared_checks == set(FROZEN_CONTROLLER_CHECKS)

    common_region = source.split("$commonScenarioChecks = @(", maxsplit=1)[1].split(
        ")\n$scenarioSpecificChecks", maxsplit=1
    )[0]
    common_checks = set(re.findall(r'"([a-z][a-z0-9_]+)"', common_region))
    specific_region = source.split("$scenarioSpecificChecks = @{", maxsplit=1)[1].split(
        "}\n$scenarioCheckNames", maxsplit=1
    )[0]
    assigned: set[str] = set(common_checks)
    scenario_names: list[str] = []
    scenario_checks: dict[str, set[str]] = {}
    for scenario, body in re.findall(
        r"(?ms)^\s{4}([a-z][a-z0-9_]+)\s*=\s*@\((.*?)\)", specific_region
    ):
        scenario_names.append(scenario)
        scenario_checks[scenario] = set(re.findall(r'"([a-z][a-z0-9_]+)"', body))
        assigned.update(scenario_checks[scenario])
    assert tuple(scenario_names) == CONTROLLER_SCENARIOS
    assert assigned == declared_checks

    pre_owner_checks = {
        "endpoint_reconfigure_before_owner",
        "endpoint_unavailable_ip_rolls_back",
        "endpoint_collision_rolls_back",
    }
    assert not (common_checks & pre_owner_checks)
    assert scenario_checks["bootstrap_https_cutover_tray"] & pre_owner_checks == pre_owner_checks
    assert all(
        not (scenario_checks[name] & pre_owner_checks)
        for name in CONTROLLER_SCENARIOS
        if name != "bootstrap_https_cutover_tray"
    )

    initial_setup_end = source.index(
        "$config = Get-ControllerConfig",
        source.index('Invoke-Button $desktop.Id "Configure HTTPS"'),
    )
    pre_owner_block_start = source.index(
        'if ($Scenario -eq "bootstrap_https_cutover_tray")', initial_setup_end
    )
    pre_owner_marker = source.index(
        "# Local setup can correct an explicitly selected endpoint before the first Owner exists.",
        pre_owner_block_start,
    )
    shared_pre_owner_start = source.index(
        "    $config = Get-ControllerConfig\n    $checks.controller_https_configuration_persisted",
        pre_owner_marker,
    )
    pre_owner_block = source[pre_owner_block_start:shared_pre_owner_start]
    for scenario_action in (
        "Get-UnassignedControllerIpv4",
        "endpointReservation.Start()",
        '"Apply endpoint change"',
        "$checks.endpoint_unavailable_ip_rolls_back = $true",
        "$checks.endpoint_collision_rolls_back = $true",
        "$checks.endpoint_reconfigure_before_owner = $true",
    ):
        assert scenario_action in pre_owner_block
    assert 'if ($Scenario -eq "bootstrap_https_cutover_tray")' in pre_owner_block
    assert 'Invoke-Button $desktop.Id "Configure HTTPS"' not in pre_owner_block

    assert "function Initialize-HealthyControllerFixture" in source
    for field in (
        "Config = Get-ControllerConfig",
        "ControllerId =",
        "DatabaseSystemIdentifier =",
        "RuntimeIdentity =",
        "RootFingerprint =",
        "EndpointPort =",
        "LanAddress =",
    ):
        assert field in source
    for scenario in CONTROLLER_SCENARIOS:
        assert f'if ($Scenario -eq "{scenario}")' in source
    assert "function Set-OperatorSessionMode" in source
    assert '[ValidateSet("signed_in", "signed_out")]' in source
    assert 'Set-OperatorSessionMode $desktop.Id $config "signed_in"' in source
    assert 'Set-OperatorSessionMode $desktop.Id $config "signed_out"' in source
    assert "[string]$Scenario" in source
    assert "operator_login_attempt_timeline = @($operatorLoginAttemptTimeline)" in source


def test_desktop_workflow_runs_independent_exact_sha_controller_scenarios() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "desktop.yml").read_text(encoding="utf-8")
    scenario_start = workflow.index("  controller-acceptance-scenario:")
    aggregate_start = workflow.index("  controller-acceptance:", scenario_start)
    scenario_job = workflow[scenario_start:aggregate_start]
    aggregate_job = workflow[aggregate_start:]

    assert "runs-on: windows-2025" in scenario_job
    assert "fail-fast: false" in scenario_job
    assert "native" in scenario_job and "windows-runtime-packaging" in scenario_job
    for scenario in CONTROLLER_SCENARIOS:
        assert f"          - {scenario}" in scenario_job
    assert "-ControllerScenario ${{ matrix.scenario }}" in scenario_job
    assert (
        "name: dx04-evidence-${{ env.DESKTOP_SOURCE_SHA }}-${{ matrix.scenario }}" in scenario_job
    )
    assert "if: always()" in scenario_job
    assert "-RuntimeLayout shared" in scenario_job
    assert "-ControllerOnly" in scenario_job

    assert "if: ${{ always() && inputs.runtime_layout == 'shared' }}" in aggregate_job
    assert "name: Shared Controller acceptance join (Windows x64)" in aggregate_job
    assert "pattern: dx04-evidence-${{ env.DESKTOP_SOURCE_SHA }}-*" in aggregate_job
    assert "aggregate_controller_scenarios.py" in aggregate_job
    assert "--source-sha ${{ env.DESKTOP_SOURCE_SHA }}" in aggregate_job
    assert "name: dx04-evidence-${{ env.DESKTOP_SOURCE_SHA }}" in aggregate_job
    assert "controller-acceptance-manifest.json" in SCENARIO_AGGREGATOR.read_text(encoding="utf-8")
    assert (REPO_ROOT / ".github" / "workflows" / "pr-acceptance.yml").read_text(
        encoding="utf-8"
    ).count("uses: ./.github/workflows/desktop.yml") == 1


def test_controller_scenario_aggregate_requires_all_exact_sha_clean_profile_evidence(
    tmp_path: Path,
) -> None:
    aggregator = _load_scenario_aggregator()
    revision = "e" * 40
    artifact_root = tmp_path / "artifacts"
    for scenario in CONTROLLER_SCENARIOS:
        evidence_dir = artifact_root / f"dx04-evidence-{revision}-{scenario}"
        evidence_dir.mkdir(parents=True)
        _write_json(
            evidence_dir / "controller-lifecycle.json",
            {
                "schema_version": 2,
                "source_revision": revision,
                "scenario": scenario,
                "runner": {
                    "github_hosted": True,
                    "windows_x64": True,
                    "non_administrator": True,
                    "clean_profile": True,
                },
                "checks": {"fixture": True},
                "failure_codes": [],
                "failure_code": None,
                "result": "PASS",
            },
        )

    output_root = tmp_path / "combined"
    manifest = aggregator.aggregate_scenarios(artifact_root, revision, output_root)
    assert manifest["result"] == "PASS"
    assert tuple(manifest["required_scenarios"]) == CONTROLLER_SCENARIOS
    assert set(manifest["observed_scenarios"]) == set(CONTROLLER_SCENARIOS)
    assert all(row["result"] == "PASS" for row in manifest["scenarios"])
    assert (output_root / "controller-acceptance-manifest.json").is_file()
    for scenario in CONTROLLER_SCENARIOS:
        assert (
            output_root / "controller-scenarios" / scenario / "controller-lifecycle.json"
        ).is_file()

    missing = artifact_root / f"dx04-evidence-{revision}-{CONTROLLER_SCENARIOS[-1]}"
    shutil.rmtree(missing)
    blocked = aggregator.aggregate_scenarios(artifact_root, revision, tmp_path / "missing")
    assert blocked["result"] == "BLOCKER"
    missing_row = next(
        row for row in blocked["scenarios"] if row["scenario"] == CONTROLLER_SCENARIOS[-1]
    )
    assert missing_row["result"] == "MISSING"


@pytest.mark.parametrize(
    "failure", ("missing", "duplicate", "wrong_sha", "wrong_runner", "blocker")
)
def test_controller_scenario_aggregate_writes_blocker_manifest_for_invalid_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    aggregator = _load_scenario_aggregator()
    revision = "c" * 40
    artifact_root = tmp_path / "artifacts"
    scenario_dirs: dict[str, Path] = {}
    for scenario in CONTROLLER_SCENARIOS:
        evidence_dir = artifact_root / f"dx04-evidence-{revision}-{scenario}"
        scenario_dirs[scenario] = evidence_dir
        evidence: dict[str, Any] = {
            "schema_version": 2,
            "source_revision": revision,
            "scenario": scenario,
            "runner": {
                "github_hosted": True,
                "windows_x64": True,
                "non_administrator": True,
                "clean_profile": True,
            },
            "checks": {"fixture": True},
            "failure_codes": [],
            "failure_code": None,
            "result": "PASS",
        }
        if failure == "wrong_sha" and scenario == CONTROLLER_SCENARIOS[0]:
            evidence["source_revision"] = "f" * 40
        elif failure == "wrong_runner" and scenario == CONTROLLER_SCENARIOS[0]:
            evidence["runner"]["clean_profile"] = False
        elif failure == "blocker" and scenario == CONTROLLER_SCENARIOS[0]:
            evidence["result"] = "BLOCKER"
            evidence["failure_code"] = "synthetic_scenario_blocker"
        _write_json(evidence_dir / "controller-lifecycle.json", evidence)

    expected_scenario = CONTROLLER_SCENARIOS[0]
    if failure == "missing":
        shutil.rmtree(scenario_dirs[CONTROLLER_SCENARIOS[-1]])
        expected_scenario = CONTROLLER_SCENARIOS[-1]
    elif failure == "duplicate":
        original_iterdir = Path.iterdir
        duplicate_path = scenario_dirs[expected_scenario]

        def duplicate_scenario_artifact(path: Path) -> Any:
            yield from original_iterdir(path)
            if path == artifact_root:
                yield duplicate_path

        monkeypatch.setattr(Path, "iterdir", duplicate_scenario_artifact)

    output_root = tmp_path / f"combined-{failure}"
    if failure == "blocker":
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "aggregate_controller_scenarios.py",
                "--artifact-root",
                str(artifact_root),
                "--source-sha",
                revision,
                "--output-root",
                str(output_root),
            ],
        )
        assert aggregator.main() == 1
    else:
        manifest = aggregator.aggregate_scenarios(artifact_root, revision, output_root)
        assert manifest["result"] == "BLOCKER"

    manifest_path = output_root / "controller-acceptance-manifest.json"
    assert manifest_path.is_file()
    written = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert written["result"] == "BLOCKER"
    row = next(item for item in written["scenarios"] if item["scenario"] == expected_scenario)
    expected_result = {
        "missing": "MISSING",
        "duplicate": "DUPLICATE",
        "wrong_sha": "BLOCKER",
        "wrong_runner": "BLOCKER",
        "blocker": "BLOCKER",
    }[failure]
    assert row["result"] == expected_result
    if failure == "wrong_sha":
        assert row["primary_failure_code"] == "controller_scenario_source_revision_mismatch"
    elif failure == "wrong_runner":
        assert row["primary_failure_code"] == "controller_scenario_runner_preflight_invalid"
    elif failure == "blocker":
        assert row["primary_failure_code"] == "synthetic_scenario_blocker"
    combined_scenarios = output_root / "controller-scenarios"
    assert combined_scenarios.is_dir()
    assert (combined_scenarios / CONTROLLER_SCENARIOS[1] / "controller-lifecycle.json").is_file()
    if failure == "blocker":
        assert (combined_scenarios / expected_scenario / "controller-lifecycle.json").is_file()
