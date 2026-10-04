function Get-OperatorLoginOutcome(
    [object]$Before,
    [object]$After,
    [bool]$UiReady,
    [bool]$InputMutationCompleted,
    [bool]$InvokeCompleted,
    [string]$UiErrorCategory
) {
    if (-not $UiReady) { return "LOGIN_UI_NOT_READY" }
    if (-not $InputMutationCompleted) { return "LOGIN_UI_INPUT_FAILED" }
    if (-not $InvokeCompleted) { return "LOGIN_UI_INVOKE_FAILED" }

    if ($Before.active_owner_session_count_before -eq 0 -and
        $After.active_owner_session_count_after -eq 1) {
        return "SUCCESS"
    }

    if ([int]$After.login_failed_attempts -gt [int]$Before.login_failed_attempts -or
        (-not [bool]$Before.login_lock_active -and [bool]$After.login_lock_active)) {
        return "LOGIN_SERVER_REJECTED"
    }

    if ([int]$After.login_success_audit_count_after -gt
        [int]$Before.login_success_audit_count_before -and
        $After.active_owner_session_count_after -ne 1) {
        return "LOGIN_SESSION_NOT_PERSISTED"
    }

    if ($UiErrorCategory -eq "OPERATOR_API_UNAVAILABLE") {
        return "LOGIN_TRANSPORT_UNAVAILABLE"
    }

    if ($Before.health_probe_outcome_before -eq "PASS" -and
        $Before.ready_probe_outcome_before -eq "PASS" -and
        ($After.health_probe_outcome -ne "PASS" -or
            $After.ready_probe_outcome -ne "PASS")) {
        return "LOGIN_RUNTIME_BECAME_UNREADY"
    }

    return "LOGIN_FAILURE_UNCLASSIFIED"
}

function Get-OperatorLoginFailureCode([string]$Outcome) {
    switch ($Outcome) {
        "LOGIN_UI_NOT_READY" { return "controller_owner_login_ui_not_ready" }
        "LOGIN_UI_INPUT_FAILED" { return "controller_owner_login_input_failed" }
        "LOGIN_UI_INVOKE_FAILED" { return "controller_owner_login_invoke_failed" }
        "LOGIN_RUNTIME_BECAME_UNREADY" { return "controller_owner_login_runtime_became_unready" }
        "LOGIN_TRANSPORT_UNAVAILABLE" { return "controller_owner_login_transport_unavailable" }
        "LOGIN_SERVER_REJECTED" { return "controller_owner_login_server_rejected" }
        "LOGIN_SESSION_NOT_PERSISTED" { return "controller_owner_login_session_not_persisted" }
        default { return "controller_owner_login_failure_unclassified" }
    }
}
