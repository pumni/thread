use std::{future::Future, pin::Pin};

use crate::{
    operator_client::{WorkerDrainStatus, WorkerStatus},
    supervisor::WorkerRuntimeObservation,
    worker_host::{LegacyTaskState, ProcessLockObservation, TaskOperation, WorkerHostBinding},
};

const STATUS_POLL_LIMIT: usize = 240;
const LOCAL_EXIT_POLL_LIMIT: usize = 120;

pub(super) type CutoverFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum WorkerDrainReason {
    Quit,
    Restart,
}

impl WorkerDrainReason {
    fn code(self) -> &'static str {
        match self {
            Self::Quit => "DESKTOP_QUIT",
            Self::Restart => "DESKTOP_RESTART",
        }
    }
}

pub(super) struct DurableDrainOfflineProof {
    worker_id: String,
    marker_sha256: String,
    protected_key_sha256: String,
}

impl DurableDrainOfflineProof {
    fn after_offline_and_exit(binding: &WorkerHostBinding) -> Self {
        Self {
            worker_id: binding.worker_id.clone(),
            marker_sha256: binding.identity_marker_sha256.clone(),
            protected_key_sha256: binding.protected_key_sha256.clone(),
        }
    }

    pub(super) fn matches(&self, binding: &WorkerHostBinding) -> bool {
        self.worker_id == binding.worker_id
            && self.marker_sha256 == binding.identity_marker_sha256
            && self.protected_key_sha256 == binding.protected_key_sha256
    }
}

pub(super) trait WorkerCutoverBackend {
    fn binding(&self) -> &WorkerHostBinding;

    fn inspect_task<'a>(&'a mut self) -> CutoverFuture<'a, Result<LegacyTaskState, String>>;

    fn process_lock_observation(&mut self) -> ProcessLockObservation;

    fn task_operation<'a>(
        &'a mut self,
        operation: TaskOperation,
        expected_state: LegacyTaskState,
        offline_exit_proof: Option<DurableDrainOfflineProof>,
    ) -> CutoverFuture<'a, Result<LegacyTaskState, String>>;

    fn worker_status<'a>(&'a mut self) -> CutoverFuture<'a, Result<WorkerDrainStatus, String>>;

    fn request_drain<'a>(
        &'a mut self,
        reason_code: &'static str,
    ) -> CutoverFuture<'a, Result<WorkerDrainStatus, String>>;

    fn local_runtime<'a>(
        &'a mut self,
    ) -> CutoverFuture<'a, Result<WorkerRuntimeObservation, String>>;

    fn start_desktop_worker<'a>(&'a mut self) -> CutoverFuture<'a, Result<(), String>>;

    fn reap_desktop_after_natural_exit<'a>(&'a mut self) -> CutoverFuture<'a, Result<(), String>>;

    fn mark_desktop_online(&mut self) -> Result<(), String>;

    fn mark_legacy_active(&mut self) -> Result<(), String>;

    fn mark_legacy_ready(&mut self, diagnostic: &'static str) -> Result<(), String>;

    fn mark_intervention(&mut self, diagnostic: &'static str);

    fn pause<'a>(&'a mut self) -> CutoverFuture<'a, ()>;
}

pub(super) async fn takeover<B: WorkerCutoverBackend>(backend: &mut B) -> Result<(), String> {
    let original_state = backend.inspect_task().await?;
    if !matches!(
        original_state,
        LegacyTaskState::Running | LegacyTaskState::Ready
    ) {
        return Err(match original_state {
            LegacyTaskState::Disabled => "worker_legacy_task_invalid",
            LegacyTaskState::Invalid => "worker_legacy_task_invalid",
            LegacyTaskState::NotRegistered => "worker_legacy_task_not_registered",
            LegacyTaskState::Running | LegacyTaskState::Ready => unreachable!(),
        }
        .to_string());
    }

    let initial_runtime = backend.local_runtime().await?;
    if initial_runtime.runtime_installed || initial_runtime.startup_cleanup_pending {
        return Err("worker_cutover_rollback_required".to_string());
    }

    if original_state == LegacyTaskState::Ready {
        if backend.process_lock_observation() != ProcessLockObservation::NotHeld {
            return Err("worker_process_lock_held".to_string());
        }
        if let Err(primary_error) = backend
            .task_operation(TaskOperation::Start, LegacyTaskState::Ready, None)
            .await
        {
            return restore_ready_after_failed_start(backend, &primary_error).await;
        }
    }

    let initial_status = match wait_for_legacy_active(backend).await {
        Ok(status) => status,
        Err(primary_error) if original_state == LegacyTaskState::Ready => {
            return handle_ready_startup_failure(backend, &primary_error).await;
        }
        Err(error) => return Err(error),
    };
    backend.mark_legacy_active()?;
    ensure_active_before_drain(initial_status.status)?;

    if backend.inspect_task().await? != LegacyTaskState::Running
        || backend.process_lock_observation() != ProcessLockObservation::Held
    {
        return Err("worker_process_lock_not_acquired".to_string());
    }
    let current_status = backend.worker_status().await?;
    ensure_active_before_drain(current_status.status)?;

    let post_request = backend.request_drain("DESKTOP_LEGACY_CUTOVER").await?;
    wait_for_authoritative_offline(backend, post_request).await?;

    let proof = wait_for_legacy_exit(backend).await?;
    if !proof.matches(backend.binding()) {
        return Err("worker_identity_corrupt".to_string());
    }

    let disable_result = backend
        .task_operation(TaskOperation::Disable, LegacyTaskState::Ready, Some(proof))
        .await;
    if let Err(primary_error) = disable_result {
        return restore_original_legacy_state(backend, original_state, &primary_error).await;
    }
    match backend.inspect_task().await {
        Ok(LegacyTaskState::Disabled) => {}
        Ok(_) => {
            return restore_original_legacy_state(
                backend,
                original_state,
                "worker_legacy_task_invalid",
            )
            .await;
        }
        Err(_) => {
            return restore_original_legacy_state(
                backend,
                original_state,
                "worker_legacy_task_invalid",
            )
            .await;
        }
    }

    if let Err(primary_error) = backend.start_desktop_worker().await {
        if primary_error == "worker_startup_cleanup_failed" {
            backend.mark_intervention("worker_startup_cleanup_failed");
            return Err("worker_cutover_rollback_required".to_string());
        }
        return restore_original_legacy_state(backend, original_state, &primary_error).await;
    }

    match wait_for_desktop_online(backend).await {
        Ok(()) => commit_desktop_online(backend),
        Err(diagnostic) => {
            backend.mark_intervention(diagnostic);
            Err("worker_cutover_rollback_required".to_string())
        }
    }
}

pub(super) async fn rollback_to_legacy<B: WorkerCutoverBackend>(
    backend: &mut B,
) -> Result<(), String> {
    if backend.inspect_task().await? != LegacyTaskState::Disabled {
        return Err("worker_legacy_task_invalid".to_string());
    }
    let local = backend.local_runtime().await?;
    if !local.runtime_installed
        || local.startup_cleanup_pending
        || local.process_alive != Some(true)
        || backend.process_lock_observation() != ProcessLockObservation::Held
    {
        return Err("worker_cutover_rollback_required".to_string());
    }
    let status = backend.worker_status().await?;
    ensure_active_before_drain(status.status)?;

    let post_request = backend.request_drain("DESKTOP_ROLLBACK").await?;
    wait_for_authoritative_offline(backend, post_request).await?;
    wait_for_desktop_exit(backend).await?;
    backend.reap_desktop_after_natural_exit().await?;

    if backend.inspect_task().await? != LegacyTaskState::Disabled {
        return Err("worker_rollback_failed".to_string());
    }
    backend
        .task_operation(TaskOperation::Enable, LegacyTaskState::Disabled, None)
        .await
        .map_err(|_| "worker_rollback_failed".to_string())?;
    if backend.inspect_task().await? != LegacyTaskState::Ready {
        return Err("worker_rollback_failed".to_string());
    }
    backend
        .task_operation(TaskOperation::Start, LegacyTaskState::Ready, None)
        .await
        .map_err(|_| "worker_rollback_failed".to_string())?;

    match wait_for_legacy_active(backend).await {
        Ok(status) if matches!(status.status, WorkerStatus::Online | WorkerStatus::Degraded) => {
            backend.mark_legacy_active()
        }
        Err(_) => {
            backend.mark_intervention("worker_rollback_failed");
            Err("worker_rollback_failed".to_string())
        }
        Ok(_) => {
            backend.mark_intervention("worker_rollback_failed");
            Err("worker_rollback_failed".to_string())
        }
    }
}

pub(super) async fn quit_desktop_worker<B: WorkerCutoverBackend>(
    backend: &mut B,
) -> Result<(), String> {
    match drain_and_reap_desktop_worker(backend, WorkerDrainReason::Quit).await {
        Ok(()) => Ok(()),
        Err(error) => {
            backend.mark_intervention(lifecycle_failure_diagnostic(&error));
            Err(error)
        }
    }
}

pub(super) async fn restart_desktop_worker<B: WorkerCutoverBackend>(
    backend: &mut B,
) -> Result<(), String> {
    if let Err(error) = drain_and_reap_desktop_worker(backend, WorkerDrainReason::Restart).await {
        backend.mark_intervention(lifecycle_failure_diagnostic(&error));
        return Err(error);
    }

    if let Err(error) = backend.start_desktop_worker().await {
        backend.mark_intervention(startup_failure_diagnostic(&error));
        return Err("worker_cutover_rollback_required".to_string());
    }

    match wait_for_desktop_online(backend).await {
        Ok(()) => commit_desktop_online(backend),
        Err(diagnostic) => {
            backend.mark_intervention(diagnostic);
            Err("worker_cutover_rollback_required".to_string())
        }
    }
}

pub(super) async fn verify_legacy_owner_for_decommission<B: WorkerCutoverBackend>(
    backend: &mut B,
) -> Result<(), String> {
    let result = async {
        let task_state = backend.inspect_task().await?;
        let local = backend.local_runtime().await?;
        if local.runtime_installed
            || local.startup_cleanup_pending
            || local.job_process_count != Some(0)
        {
            return Err("worker_cutover_rollback_required".to_string());
        }
        let lock = backend.process_lock_observation();
        match (task_state, lock) {
            (LegacyTaskState::Running, ProcessLockObservation::Held) => {
                backend.mark_legacy_active()
            }
            (LegacyTaskState::Ready, ProcessLockObservation::NotHeld) => {
                backend.mark_legacy_ready("worker_legacy_task_ready")
            }
            (_, ProcessLockObservation::Unavailable) => {
                Err("worker_process_lock_unavailable".to_string())
            }
            _ => Err("worker_cutover_rollback_required".to_string()),
        }
    }
    .await;
    if let Err(error) = &result {
        backend.mark_intervention(lifecycle_failure_diagnostic(error));
    }
    result
}

async fn drain_and_reap_desktop_worker<B: WorkerCutoverBackend>(
    backend: &mut B,
    reason: WorkerDrainReason,
) -> Result<(), String> {
    if backend.inspect_task().await? != LegacyTaskState::Disabled {
        return Err("worker_legacy_task_invalid".to_string());
    }
    validate_desktop_runtime(backend).await?;
    let status = backend.worker_status().await?;
    ensure_active_before_drain(status.status)?;
    if backend.inspect_task().await? != LegacyTaskState::Disabled {
        return Err("worker_legacy_task_invalid".to_string());
    }
    validate_desktop_runtime(backend).await?;

    let post_request = backend.request_drain(reason.code()).await?;
    wait_for_authoritative_offline(backend, post_request).await?;
    wait_for_desktop_exit(backend).await?;
    if backend.inspect_task().await? != LegacyTaskState::Disabled {
        return Err("worker_legacy_task_invalid".to_string());
    }
    backend.reap_desktop_after_natural_exit().await?;
    if backend.inspect_task().await? != LegacyTaskState::Disabled {
        return Err("worker_legacy_task_invalid".to_string());
    }
    Ok(())
}

async fn validate_desktop_runtime<B: WorkerCutoverBackend>(backend: &mut B) -> Result<(), String> {
    let local = backend.local_runtime().await?;
    if local.startup_cleanup_pending {
        return Err("worker_startup_cleanup_failed".to_string());
    }
    if !local.runtime_installed
        || local.process_alive != Some(true)
        || !local.job_process_count.is_some_and(|count| count > 0)
    {
        return Err("worker_process_exited".to_string());
    }
    match backend.process_lock_observation() {
        ProcessLockObservation::Held => Ok(()),
        ProcessLockObservation::NotHeld => Err("worker_process_lock_not_acquired".to_string()),
        ProcessLockObservation::Unavailable => Err("worker_process_lock_unavailable".to_string()),
    }
}

pub(super) fn lifecycle_failure_diagnostic(error: &str) -> &'static str {
    match error {
        "worker_host_configuration_required" => "worker_host_configuration_required",
        "worker_identity_missing" => "worker_identity_missing",
        "worker_identity_not_enrolled" => "worker_identity_not_enrolled",
        "worker_device_key_unprotect_failed" => "worker_device_key_unprotect_failed",
        "worker_package_invalid" => "worker_package_invalid",
        "worker_host_config_invalid" => "worker_host_config_invalid",
        "worker_drain_unavailable" => "worker_drain_unavailable",
        "worker_drain_request_failed" => "worker_drain_request_failed",
        "worker_drain_response_invalid" => "worker_drain_response_invalid",
        "worker_drain_interrupted" => "worker_drain_interrupted",
        "worker_drain_timeout" => "worker_drain_timeout",
        "worker_process_exit_timeout" => "worker_process_exit_timeout",
        "worker_process_exited" => "worker_process_exited",
        "worker_process_lock_not_acquired" => "worker_process_lock_not_acquired",
        "worker_process_lock_held" => "worker_process_lock_held",
        "worker_process_lock_unavailable" => "worker_process_lock_unavailable",
        "worker_process_job_create_failed" => "worker_process_job_create_failed",
        "worker_process_job_assign_failed" => "worker_process_job_assign_failed",
        "worker_process_start_failed" => "worker_process_start_failed",
        "worker_legacy_task_invalid" => "worker_legacy_task_invalid",
        "worker_legacy_task_not_registered" => "worker_legacy_task_not_registered",
        "worker_legacy_task_enable_failed" => "worker_legacy_task_enable_failed",
        "worker_legacy_task_start_failed" => "worker_legacy_task_start_failed",
        "worker_legacy_task_disable_failed" => "worker_legacy_task_disable_failed",
        "worker_legacy_task_running" => "worker_legacy_task_running",
        "worker_startup_cleanup_failed" => "worker_startup_cleanup_failed",
        "worker_startup_timeout" => "worker_startup_timeout",
        "worker_identity_corrupt" => "worker_identity_corrupt",
        "worker_rollback_failed" => "worker_rollback_failed",
        "worker_forced_interruption" => "worker_forced_interruption",
        "operator_session_revoked" => "operator_session_revoked",
        "operator_forbidden" => "operator_forbidden",
        "operator_password_change_required" => "operator_password_change_required",
        "worker_protocol_incompatible" => "worker_protocol_incompatible",
        "worker_cutover_drain_already_in_progress" => "worker_cutover_drain_already_in_progress",
        _ => "worker_cutover_rollback_required",
    }
}

async fn wait_for_legacy_active<B: WorkerCutoverBackend>(
    backend: &mut B,
) -> Result<WorkerDrainStatus, String> {
    for attempt in 0..STATUS_POLL_LIMIT {
        if backend.inspect_task().await? != LegacyTaskState::Running {
            return Err("worker_legacy_task_start_failed".to_string());
        }
        let lock = backend.process_lock_observation();
        if lock == ProcessLockObservation::Unavailable {
            return Err("worker_process_lock_unavailable".to_string());
        }
        let status = backend.worker_status().await?;
        match status.status {
            WorkerStatus::Online | WorkerStatus::Degraded
                if lock == ProcessLockObservation::Held =>
            {
                return Ok(status);
            }
            WorkerStatus::Online
            | WorkerStatus::Degraded
            | WorkerStatus::Registering
            | WorkerStatus::Offline => {}
            WorkerStatus::Draining => {
                return Err("worker_cutover_drain_already_in_progress".to_string());
            }
            WorkerStatus::Disabled | WorkerStatus::UpgradeRequired => {
                return Err("worker_protocol_incompatible".to_string());
            }
        }
        if attempt + 1 < STATUS_POLL_LIMIT {
            backend.pause().await;
        }
    }
    Err("worker_startup_timeout".to_string())
}

fn ensure_active_before_drain(status: WorkerStatus) -> Result<(), String> {
    match status {
        WorkerStatus::Online | WorkerStatus::Degraded => Ok(()),
        WorkerStatus::Draining => Err("worker_cutover_drain_already_in_progress".to_string()),
        WorkerStatus::Registering
        | WorkerStatus::Offline
        | WorkerStatus::Disabled
        | WorkerStatus::UpgradeRequired => Err("worker_protocol_incompatible".to_string()),
    }
}

async fn wait_for_authoritative_offline<B: WorkerCutoverBackend>(
    backend: &mut B,
    post_request: WorkerDrainStatus,
) -> Result<(), String> {
    validate_drain_status_shape(&post_request)?;
    match post_request.status {
        WorkerStatus::Offline => return validate_offline_status(&post_request),
        WorkerStatus::Draining => {}
        WorkerStatus::Online | WorkerStatus::Degraded => {
            return Err("worker_drain_interrupted".to_string());
        }
        WorkerStatus::Registering | WorkerStatus::Disabled | WorkerStatus::UpgradeRequired => {
            return Err("worker_protocol_incompatible".to_string());
        }
    }

    for attempt in 0..STATUS_POLL_LIMIT {
        let status = backend.worker_status().await?;
        validate_drain_status_shape(&status)?;
        match status.status {
            WorkerStatus::Offline => return validate_offline_status(&status),
            WorkerStatus::Draining => {}
            WorkerStatus::Online | WorkerStatus::Degraded => {
                return Err("worker_drain_interrupted".to_string());
            }
            WorkerStatus::Registering | WorkerStatus::Disabled | WorkerStatus::UpgradeRequired => {
                return Err("worker_protocol_incompatible".to_string());
            }
        }
        if attempt + 1 < STATUS_POLL_LIMIT {
            backend.pause().await;
        }
    }
    Err("worker_drain_timeout".to_string())
}

fn validate_offline_status(status: &WorkerDrainStatus) -> Result<(), String> {
    if status.status == WorkerStatus::Offline
        && status.active_browser_sessions == 0
        && status.running_worker_jobs == 0
        && !status.quiescent
    {
        Ok(())
    } else {
        Err("worker_drain_response_invalid".to_string())
    }
}

fn validate_drain_status_shape(status: &WorkerDrainStatus) -> Result<(), String> {
    match status.status {
        WorkerStatus::Draining => {
            let no_work_remains =
                status.active_browser_sessions == 0 && status.running_worker_jobs == 0;
            if status.quiescent == no_work_remains {
                Ok(())
            } else {
                Err("worker_drain_response_invalid".to_string())
            }
        }
        WorkerStatus::Offline => validate_offline_status(status),
        _ => Ok(()),
    }
}

async fn wait_for_legacy_exit<B: WorkerCutoverBackend>(
    backend: &mut B,
) -> Result<DurableDrainOfflineProof, String> {
    for attempt in 0..LOCAL_EXIT_POLL_LIMIT {
        let task_state = backend.inspect_task().await?;
        let lock = backend.process_lock_observation();
        if lock == ProcessLockObservation::Unavailable {
            return Err("worker_process_lock_unavailable".to_string());
        }
        if task_state == LegacyTaskState::Ready && lock == ProcessLockObservation::NotHeld {
            return Ok(DurableDrainOfflineProof::after_offline_and_exit(
                backend.binding(),
            ));
        }
        if task_state != LegacyTaskState::Running {
            return Err("worker_legacy_task_invalid".to_string());
        }
        if attempt + 1 < LOCAL_EXIT_POLL_LIMIT {
            backend.pause().await;
        }
    }
    Err("worker_process_exit_timeout".to_string())
}

async fn wait_for_desktop_online<B: WorkerCutoverBackend>(
    backend: &mut B,
) -> Result<(), &'static str> {
    for attempt in 0..STATUS_POLL_LIMIT {
        if backend
            .inspect_task()
            .await
            .map_err(|_| "worker_legacy_task_invalid")?
            != LegacyTaskState::Disabled
        {
            return Err("worker_legacy_task_invalid");
        }
        let local = backend
            .local_runtime()
            .await
            .map_err(|_| "worker_process_exited")?;
        if !local.runtime_installed
            || local.startup_cleanup_pending
            || local.process_alive != Some(true)
            || backend.process_lock_observation() != ProcessLockObservation::Held
        {
            return Err("worker_process_exited");
        }
        let status = backend.worker_status().await.map_err(|error| {
            if error == "operator_session_revoked" {
                "operator_session_revoked"
            } else if error == "operator_forbidden" {
                "operator_forbidden"
            } else {
                "worker_drain_unavailable"
            }
        })?;
        match status.status {
            WorkerStatus::Online => {
                if backend
                    .inspect_task()
                    .await
                    .map_err(|_| "worker_legacy_task_invalid")?
                    != LegacyTaskState::Disabled
                {
                    return Err("worker_legacy_task_invalid");
                }
                let final_local = backend
                    .local_runtime()
                    .await
                    .map_err(|_| "worker_process_exited")?;
                if !final_local.runtime_installed
                    || final_local.startup_cleanup_pending
                    || final_local.process_alive != Some(true)
                    || backend.process_lock_observation() != ProcessLockObservation::Held
                {
                    return Err("worker_process_exited");
                }
                return Ok(());
            }
            WorkerStatus::Registering | WorkerStatus::Offline | WorkerStatus::Degraded => {}
            WorkerStatus::Draining | WorkerStatus::Disabled | WorkerStatus::UpgradeRequired => {
                return Err("worker_protocol_incompatible");
            }
        }
        if attempt + 1 < STATUS_POLL_LIMIT {
            backend.pause().await;
        }
    }
    Err("worker_startup_timeout")
}

fn commit_desktop_online<B: WorkerCutoverBackend>(backend: &mut B) -> Result<(), String> {
    match backend.mark_desktop_online() {
        Ok(()) => Ok(()),
        Err(error) => {
            backend.mark_intervention(lifecycle_failure_diagnostic(&error));
            Err("worker_cutover_rollback_required".to_string())
        }
    }
}

async fn wait_for_desktop_exit<B: WorkerCutoverBackend>(backend: &mut B) -> Result<(), String> {
    for attempt in 0..LOCAL_EXIT_POLL_LIMIT {
        if backend.inspect_task().await? != LegacyTaskState::Disabled {
            return Err("worker_legacy_task_invalid".to_string());
        }
        let local = backend.local_runtime().await?;
        if local.startup_cleanup_pending {
            return Err("worker_startup_cleanup_failed".to_string());
        }
        let lock = backend.process_lock_observation();
        if lock == ProcessLockObservation::Unavailable {
            return Err("worker_process_lock_unavailable".to_string());
        }
        if !local.runtime_installed
            && local.process_exit_proven
            && local.job_process_count == Some(0)
            && lock == ProcessLockObservation::NotHeld
        {
            return Ok(());
        }
        if local.runtime_installed
            && local.process_alive == Some(false)
            && local.job_process_count == Some(0)
            && lock == ProcessLockObservation::NotHeld
        {
            return Ok(());
        }
        if attempt + 1 < LOCAL_EXIT_POLL_LIMIT {
            backend.pause().await;
        }
    }
    Err("worker_process_exit_timeout".to_string())
}

async fn restore_original_legacy_state<B: WorkerCutoverBackend>(
    backend: &mut B,
    original_state: LegacyTaskState,
    primary_error: &str,
) -> Result<(), String> {
    let local = backend.local_runtime().await?;
    if local.runtime_installed
        || local.startup_cleanup_pending
        || backend.process_lock_observation() != ProcessLockObservation::NotHeld
    {
        backend.mark_intervention("worker_cutover_rollback_required");
        return Err("worker_cutover_rollback_required".to_string());
    }
    let task_state = match backend.inspect_task().await {
        Ok(state) => state,
        Err(_) => {
            backend.mark_intervention("worker_cutover_rollback_required");
            return Err("worker_cutover_rollback_required".to_string());
        }
    };

    let mut restored_state = task_state;
    if task_state == LegacyTaskState::Disabled {
        match backend
            .task_operation(TaskOperation::Enable, LegacyTaskState::Disabled, None)
            .await
        {
            Ok(state) => restored_state = state,
            Err(_) => {
                backend.mark_intervention("worker_cutover_rollback_required");
                return Err("worker_cutover_rollback_required".to_string());
            }
        }
    }
    if restored_state == LegacyTaskState::Running {
        if original_state != LegacyTaskState::Running {
            backend.mark_intervention("worker_cutover_rollback_required");
            return Err("worker_cutover_rollback_required".to_string());
        }
        match wait_for_legacy_active(backend).await {
            Ok(status)
                if matches!(status.status, WorkerStatus::Online | WorkerStatus::Degraded) =>
            {
                if backend.mark_legacy_active().is_err() {
                    backend.mark_intervention("worker_cutover_rollback_required");
                    return Err("worker_cutover_rollback_required".to_string());
                }
                return Err(primary_error.to_string());
            }
            _ => {
                backend.mark_intervention("worker_cutover_rollback_required");
                return Err("worker_cutover_rollback_required".to_string());
            }
        }
    }
    if restored_state != LegacyTaskState::Ready {
        backend.mark_intervention("worker_cutover_rollback_required");
        return Err("worker_cutover_rollback_required".to_string());
    }
    if original_state == LegacyTaskState::Ready {
        if backend
            .mark_legacy_ready("worker_legacy_task_ready")
            .is_err()
        {
            backend.mark_intervention("worker_cutover_rollback_required");
            return Err("worker_cutover_rollback_required".to_string());
        }
        return Err(primary_error.to_string());
    }

    match backend
        .task_operation(TaskOperation::Start, LegacyTaskState::Ready, None)
        .await
    {
        Ok(LegacyTaskState::Running) => {}
        _ => {
            backend.mark_intervention("worker_cutover_rollback_required");
            return Err("worker_cutover_rollback_required".to_string());
        }
    }
    match wait_for_legacy_active(backend).await {
        Ok(status) if matches!(status.status, WorkerStatus::Online | WorkerStatus::Degraded) => {
            if backend.mark_legacy_active().is_err() {
                backend.mark_intervention("worker_cutover_rollback_required");
                return Err("worker_cutover_rollback_required".to_string());
            }
            Err(primary_error.to_string())
        }
        _ => {
            backend.mark_intervention("worker_cutover_rollback_required");
            Err("worker_cutover_rollback_required".to_string())
        }
    }
}

async fn restore_ready_after_failed_start<B: WorkerCutoverBackend>(
    backend: &mut B,
    primary_error: &str,
) -> Result<(), String> {
    let task_state = match backend.inspect_task().await {
        Ok(state) => state,
        Err(_) => {
            backend.mark_intervention("worker_cutover_rollback_required");
            return Err("worker_cutover_rollback_required".to_string());
        }
    };
    let lock = backend.process_lock_observation();
    if task_state == LegacyTaskState::Ready && lock == ProcessLockObservation::NotHeld {
        if backend
            .mark_legacy_ready("worker_legacy_task_ready")
            .is_err()
        {
            backend.mark_intervention("worker_cutover_rollback_required");
            return Err("worker_cutover_rollback_required".to_string());
        }
        return Err(primary_error.to_string());
    }
    if task_state != LegacyTaskState::Running || lock != ProcessLockObservation::Held {
        backend.mark_intervention(if lock == ProcessLockObservation::Unavailable {
            "worker_process_lock_unavailable"
        } else {
            "worker_legacy_task_start_failed"
        });
        return Err("worker_cutover_rollback_required".to_string());
    }

    let status = match wait_for_legacy_active(backend).await {
        Ok(status) => status,
        Err(error) => {
            backend.mark_intervention(startup_failure_diagnostic(&error));
            if error == "worker_startup_timeout" {
                return Err(error);
            }
            return Err("worker_cutover_rollback_required".to_string());
        }
    };
    if !matches!(status.status, WorkerStatus::Online | WorkerStatus::Degraded) {
        backend.mark_intervention("worker_cutover_rollback_required");
        return Err("worker_cutover_rollback_required".to_string());
    }
    backend.mark_legacy_active()?;
    let post_request = backend.request_drain("DESKTOP_LEGACY_CUTOVER").await?;
    wait_for_authoritative_offline(backend, post_request).await?;
    wait_for_legacy_exit(backend).await?;
    backend
        .mark_legacy_ready("worker_legacy_task_ready")
        .map_err(|_| "worker_cutover_rollback_required".to_string())?;
    Err(primary_error.to_string())
}

async fn handle_ready_startup_failure<B: WorkerCutoverBackend>(
    backend: &mut B,
    primary_error: &str,
) -> Result<(), String> {
    let task_state = backend.inspect_task().await;
    let lock = backend.process_lock_observation();
    match (task_state, lock) {
        (Ok(LegacyTaskState::Ready), ProcessLockObservation::NotHeld) => {
            backend
                .mark_legacy_ready("worker_legacy_task_ready")
                .map_err(|_| "worker_cutover_rollback_required".to_string())?;
            Err(primary_error.to_string())
        }
        (Ok(LegacyTaskState::Running), ProcessLockObservation::Held) => {
            backend.mark_intervention(startup_failure_diagnostic(primary_error));
            Err(primary_error.to_string())
        }
        _ => {
            backend.mark_intervention("worker_cutover_rollback_required");
            Err("worker_cutover_rollback_required".to_string())
        }
    }
}

fn startup_failure_diagnostic(error: &str) -> &'static str {
    match error {
        "worker_startup_timeout" => "worker_startup_timeout",
        "worker_startup_cleanup_failed" => "worker_startup_cleanup_failed",
        "worker_identity_missing" => "worker_identity_missing",
        "worker_identity_not_enrolled" => "worker_identity_not_enrolled",
        "worker_device_key_unprotect_failed" => "worker_device_key_unprotect_failed",
        "worker_host_configuration_required" => "worker_host_configuration_required",
        "worker_host_config_invalid" => "worker_host_config_invalid",
        "worker_process_lock_not_acquired" => "worker_process_lock_not_acquired",
        "worker_process_lock_held" => "worker_process_lock_held",
        "worker_process_lock_unavailable" => "worker_process_lock_unavailable",
        "worker_process_exited" => "worker_process_exited",
        "worker_process_start_failed" => "worker_process_start_failed",
        "worker_process_job_create_failed" => "worker_process_job_create_failed",
        "worker_process_job_assign_failed" => "worker_process_job_assign_failed",
        "worker_legacy_task_start_failed" => "worker_legacy_task_start_failed",
        "worker_legacy_task_invalid" => "worker_legacy_task_invalid",
        "worker_legacy_task_not_registered" => "worker_legacy_task_not_registered",
        "worker_cutover_drain_already_in_progress" => "worker_cutover_drain_already_in_progress",
        "worker_protocol_incompatible" => "worker_protocol_incompatible",
        "worker_drain_unavailable" => "worker_drain_unavailable",
        "worker_drain_response_invalid" => "worker_drain_response_invalid",
        "worker_drain_interrupted" => "worker_drain_interrupted",
        "worker_identity_corrupt" => "worker_identity_corrupt",
        "worker_package_invalid" => "worker_package_invalid",
        "operator_session_revoked" => "operator_session_revoked",
        "operator_forbidden" => "operator_forbidden",
        "operator_password_change_required" => "operator_password_change_required",
        _ => "worker_cutover_rollback_required",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    enum Event {
        Inspect(LegacyTaskState),
        Lock(ProcessLockObservation),
        Post(&'static str),
        PostResponse(&'static str),
        Get(&'static str),
        Task(TaskOperation),
        StartDesktop,
        ReapDesktop,
        Logout,
        MarkDesktopOnline,
        MarkLegacyActive,
        MarkLegacyReady,
        Intervention(&'static str),
    }

    struct FakeBackend {
        binding: WorkerHostBinding,
        task_state: LegacyTaskState,
        lock: ProcessLockObservation,
        statuses: VecDeque<Result<WorkerDrainStatus, String>>,
        requested: Vec<&'static str>,
        events: Vec<Event>,
        runtime: WorkerRuntimeObservation,
        desktop_start_error: Option<String>,
        task_start_error: Option<(String, bool)>,
        mark_desktop_online_error: Option<String>,
        drain_in_progress: bool,
        desktop_exits_after_offline: bool,
        desktop_job_empty_after_offline: bool,
        desktop_lock_released_after_offline: bool,
        operations: Vec<TaskOperation>,
    }

    impl FakeBackend {
        fn running(statuses: &[WorkerStatus]) -> Self {
            Self::new(LegacyTaskState::Running, statuses)
        }

        fn ready(statuses: &[WorkerStatus]) -> Self {
            Self::new(LegacyTaskState::Ready, statuses)
        }

        fn desktop(statuses: &[WorkerStatus]) -> Self {
            let mut backend = Self::new(LegacyTaskState::Disabled, statuses);
            backend.lock = ProcessLockObservation::Held;
            backend.runtime = WorkerRuntimeObservation {
                runtime_installed: true,
                startup_cleanup_pending: false,
                process_alive: Some(true),
                job_process_count: Some(1),
                process_exit_proven: false,
                server_online_verified: true,
            };
            backend
        }

        fn new(task_state: LegacyTaskState, statuses: &[WorkerStatus]) -> Self {
            let lock = if task_state == LegacyTaskState::Running {
                ProcessLockObservation::Held
            } else {
                ProcessLockObservation::NotHeld
            };
            Self {
                binding: binding(),
                task_state,
                lock,
                statuses: statuses.iter().copied().map(status).collect(),
                requested: Vec::new(),
                events: Vec::new(),
                runtime: WorkerRuntimeObservation {
                    runtime_installed: false,
                    startup_cleanup_pending: false,
                    process_alive: None,
                    job_process_count: Some(0),
                    process_exit_proven: false,
                    server_online_verified: false,
                },
                desktop_start_error: None,
                task_start_error: None,
                mark_desktop_online_error: None,
                drain_in_progress: false,
                desktop_exits_after_offline: true,
                desktop_job_empty_after_offline: true,
                desktop_lock_released_after_offline: true,
                operations: Vec::new(),
            }
        }

        fn take_status(
            &mut self,
            event: impl FnOnce(&'static str) -> Event,
        ) -> Result<WorkerDrainStatus, String> {
            let value = self
                .statuses
                .pop_front()
                .expect("planned status response")?;
            self.events.push(event(status_name(value.status)));
            self.apply_status_effects(value.status);
            Ok(value)
        }

        fn take_post_response(&mut self) -> Result<WorkerDrainStatus, String> {
            let value = self.statuses.pop_front().expect("planned POST response")?;
            self.events
                .push(Event::PostResponse(status_name(value.status)));
            self.apply_status_effects(value.status);
            Ok(value)
        }

        fn apply_status_effects(&mut self, status: WorkerStatus) {
            if status == WorkerStatus::Offline && self.drain_in_progress {
                if self.task_state == LegacyTaskState::Running {
                    self.task_state = LegacyTaskState::Ready;
                    self.lock = ProcessLockObservation::NotHeld;
                }
                if self.runtime.runtime_installed && self.desktop_exits_after_offline {
                    self.runtime.process_alive = Some(false);
                    if self.desktop_job_empty_after_offline {
                        self.runtime.job_process_count = Some(0);
                    }
                    if self.desktop_lock_released_after_offline {
                        self.lock = ProcessLockObservation::NotHeld;
                    }
                    self.runtime.process_exit_proven = true;
                }
                self.drain_in_progress = false;
            }
        }
    }

    impl WorkerCutoverBackend for FakeBackend {
        fn binding(&self) -> &WorkerHostBinding {
            &self.binding
        }

        fn inspect_task<'a>(&'a mut self) -> CutoverFuture<'a, Result<LegacyTaskState, String>> {
            Box::pin(async move {
                self.events.push(Event::Inspect(self.task_state));
                Ok(self.task_state)
            })
        }

        fn process_lock_observation(&mut self) -> ProcessLockObservation {
            self.events.push(Event::Lock(self.lock));
            self.lock
        }

        fn task_operation<'a>(
            &'a mut self,
            operation: TaskOperation,
            expected_state: LegacyTaskState,
            offline_exit_proof: Option<DurableDrainOfflineProof>,
        ) -> CutoverFuture<'a, Result<LegacyTaskState, String>> {
            Box::pin(async move {
                self.events.push(Event::Task(operation));
                if self.task_state != expected_state {
                    return Err(operation.failure_code().to_string());
                }
                if operation == TaskOperation::Start {
                    if let Some((error, started)) = self.task_start_error.take() {
                        if started {
                            self.operations.push(operation);
                            self.task_state = LegacyTaskState::Running;
                            self.lock = ProcessLockObservation::Held;
                        }
                        return Err(error);
                    }
                }
                if operation == TaskOperation::Disable
                    && (!offline_exit_proof.is_some_and(|proof| proof.matches(&self.binding))
                        || self.lock != ProcessLockObservation::NotHeld)
                {
                    return Err("worker_legacy_task_disable_failed".to_string());
                }
                self.operations.push(operation);
                self.task_state = operation.expected_state();
                match operation {
                    TaskOperation::Start => self.lock = ProcessLockObservation::Held,
                    TaskOperation::Disable | TaskOperation::Enable => {
                        self.lock = ProcessLockObservation::NotHeld
                    }
                }
                Ok(self.task_state)
            })
        }

        fn worker_status<'a>(&'a mut self) -> CutoverFuture<'a, Result<WorkerDrainStatus, String>> {
            Box::pin(async move { self.take_status(Event::Get) })
        }

        fn request_drain<'a>(
            &'a mut self,
            reason_code: &'static str,
        ) -> CutoverFuture<'a, Result<WorkerDrainStatus, String>> {
            Box::pin(async move {
                self.requested.push(reason_code);
                self.events.push(Event::Post(reason_code));
                self.drain_in_progress = true;
                self.take_post_response()
            })
        }

        fn local_runtime<'a>(
            &'a mut self,
        ) -> CutoverFuture<'a, Result<WorkerRuntimeObservation, String>> {
            Box::pin(async move { Ok(self.runtime) })
        }

        fn start_desktop_worker<'a>(&'a mut self) -> CutoverFuture<'a, Result<(), String>> {
            Box::pin(async move {
                self.events.push(Event::StartDesktop);
                if let Some(error) = self.desktop_start_error.take() {
                    if error == "worker_startup_cleanup_failed" {
                        self.runtime.startup_cleanup_pending = true;
                    }
                    return Err(error);
                }
                self.runtime = WorkerRuntimeObservation {
                    runtime_installed: true,
                    startup_cleanup_pending: false,
                    process_alive: Some(true),
                    job_process_count: Some(1),
                    process_exit_proven: false,
                    server_online_verified: false,
                };
                self.lock = ProcessLockObservation::Held;
                Ok(())
            })
        }

        fn reap_desktop_after_natural_exit<'a>(
            &'a mut self,
        ) -> CutoverFuture<'a, Result<(), String>> {
            Box::pin(async move {
                self.events.push(Event::ReapDesktop);
                if self.runtime.runtime_installed
                    && (self.runtime.process_alive != Some(false)
                        || self.runtime.job_process_count != Some(0))
                {
                    return Err("worker_process_exit_timeout".to_string());
                }
                self.runtime = WorkerRuntimeObservation {
                    runtime_installed: false,
                    startup_cleanup_pending: false,
                    process_alive: None,
                    job_process_count: Some(0),
                    process_exit_proven: true,
                    server_online_verified: false,
                };
                Ok(())
            })
        }

        fn mark_desktop_online(&mut self) -> Result<(), String> {
            self.events.push(Event::MarkDesktopOnline);
            if let Some(error) = self.mark_desktop_online_error.take() {
                return Err(error);
            }
            self.runtime.server_online_verified = true;
            Ok(())
        }

        fn mark_legacy_active(&mut self) -> Result<(), String> {
            self.events.push(Event::MarkLegacyActive);
            Ok(())
        }

        fn mark_legacy_ready(&mut self, _diagnostic: &'static str) -> Result<(), String> {
            self.events.push(Event::MarkLegacyReady);
            Ok(())
        }

        fn mark_intervention(&mut self, diagnostic: &'static str) {
            self.events.push(Event::Intervention(diagnostic));
        }

        fn pause<'a>(&'a mut self) -> CutoverFuture<'a, ()> {
            Box::pin(async {})
        }
    }

    fn binding() -> WorkerHostBinding {
        WorkerHostBinding {
            executable: "worker.exe".into(),
            host_config: "host.json".into(),
            data_root: "worker-root".into(),
            worker_id: "12345678-1234-4234-8234-123456789abc".to_string(),
            identity_marker_sha256: "a".repeat(64),
            protected_key_sha256: "b".repeat(64),
            host_config_sha256: "c".repeat(64),
        }
    }

    fn status(status: WorkerStatus) -> Result<WorkerDrainStatus, String> {
        let (active_browser_sessions, running_worker_jobs) = if status == WorkerStatus::Offline {
            (0, 0)
        } else {
            (1, 1)
        };
        status_with(status, active_browser_sessions, running_worker_jobs, false)
    }

    fn status_with(
        status: WorkerStatus,
        active_browser_sessions: u32,
        running_worker_jobs: u32,
        quiescent: bool,
    ) -> Result<WorkerDrainStatus, String> {
        Ok(WorkerDrainStatus {
            worker_id: binding().worker_id,
            status,
            active_browser_sessions,
            running_worker_jobs,
            quiescent,
        })
    }

    #[test]
    fn startup_and_lifecycle_diagnostics_preserve_stable_worker_codes() {
        for code in [
            "worker_identity_missing",
            "worker_identity_not_enrolled",
            "worker_identity_corrupt",
            "worker_device_key_unprotect_failed",
            "worker_package_invalid",
            "worker_host_config_invalid",
            "worker_process_job_create_failed",
            "worker_process_job_assign_failed",
        ] {
            assert_eq!(startup_failure_diagnostic(code), code);
            assert_eq!(lifecycle_failure_diagnostic(code), code);
        }
    }

    fn status_name(status: WorkerStatus) -> &'static str {
        match status {
            WorkerStatus::Registering => "REGISTERING",
            WorkerStatus::Online => "ONLINE",
            WorkerStatus::Degraded => "DEGRADED",
            WorkerStatus::Draining => "DRAINING",
            WorkerStatus::Offline => "OFFLINE",
            WorkerStatus::Disabled => "DISABLED",
            WorkerStatus::UpgradeRequired => "UPGRADE_REQUIRED",
        }
    }

    #[test]
    fn running_cutover_drains_to_offline_then_exits_before_disable_and_online_commit() {
        tauri::async_runtime::block_on(async {
            let mut backend = FakeBackend::running(&[
                WorkerStatus::Online,
                WorkerStatus::Online,
                WorkerStatus::Draining,
                WorkerStatus::Offline,
                WorkerStatus::Offline,
                WorkerStatus::Registering,
                WorkerStatus::Degraded,
                WorkerStatus::Online,
            ]);
            takeover(&mut backend).await.expect("cutover succeeds");

            assert_eq!(backend.requested, ["DESKTOP_LEGACY_CUTOVER"]);
            assert_eq!(backend.operations, [TaskOperation::Disable]);
            let offline = backend
                .events
                .iter()
                .position(|event| *event == Event::Get("OFFLINE"))
                .expect("authoritative OFFLINE observed");
            let disable = backend
                .events
                .iter()
                .position(|event| *event == Event::Task(TaskOperation::Disable))
                .expect("task disabled");
            let start_desktop = backend
                .events
                .iter()
                .position(|event| *event == Event::StartDesktop)
                .expect("Desktop Worker started");
            let online = backend
                .events
                .iter()
                .rposition(|event| *event == Event::Get("ONLINE"))
                .expect("authoritative ONLINE observed");
            let desktop_offline = backend
                .events
                .iter()
                .rposition(|event| *event == Event::Get("OFFLINE"))
                .expect("post-start OFFLINE is transitional");
            let desktop_degraded = backend
                .events
                .iter()
                .position(|event| *event == Event::Get("DEGRADED"))
                .expect("post-start DEGRADED is transitional");
            let mark_online = backend
                .events
                .iter()
                .position(|event| *event == Event::MarkDesktopOnline)
                .expect("health commit");
            assert!(offline < disable && disable < start_desktop && start_desktop < online);
            assert!(start_desktop < desktop_offline && desktop_offline < desktop_degraded);
            assert!(desktop_degraded < online);
            assert!(online < mark_online);
        });
    }

    #[test]
    fn ready_cutover_starts_and_observes_legacy_before_drain() {
        tauri::async_runtime::block_on(async {
            let mut backend = FakeBackend::ready(&[
                WorkerStatus::Offline,
                WorkerStatus::Registering,
                WorkerStatus::Online,
                WorkerStatus::Online,
                WorkerStatus::Draining,
                WorkerStatus::Offline,
                WorkerStatus::Offline,
                WorkerStatus::Registering,
                WorkerStatus::Degraded,
                WorkerStatus::Online,
            ]);
            takeover(&mut backend)
                .await
                .expect("READY task is drained before cutover");
            assert_eq!(backend.requested, ["DESKTOP_LEGACY_CUTOVER"]);
            assert_eq!(
                backend.operations,
                [TaskOperation::Start, TaskOperation::Disable]
            );
            let legacy_start = backend
                .events
                .iter()
                .position(|event| *event == Event::Task(TaskOperation::Start))
                .expect("legacy task started first");
            let drain = backend
                .events
                .iter()
                .position(|event| matches!(event, Event::Post(_)))
                .expect("drain requested");
            assert!(legacy_start < drain);
        });
    }

    #[test]
    fn legacy_and_desktop_startup_offline_windows_have_bounded_timeouts() {
        tauri::async_runtime::block_on(async {
            let mut legacy = FakeBackend::running(&[]);
            legacy.statuses = std::iter::repeat_with(|| status(WorkerStatus::Offline))
                .take(STATUS_POLL_LIMIT)
                .collect();
            assert_eq!(
                wait_for_legacy_active(&mut legacy).await.unwrap_err(),
                "worker_startup_timeout"
            );

            let mut desktop = FakeBackend::new(LegacyTaskState::Disabled, &[]);
            desktop.runtime = WorkerRuntimeObservation {
                runtime_installed: true,
                startup_cleanup_pending: false,
                process_alive: Some(true),
                job_process_count: Some(1),
                process_exit_proven: false,
                server_online_verified: false,
            };
            desktop.lock = ProcessLockObservation::Held;
            desktop.statuses = std::iter::repeat_with(|| status(WorkerStatus::Offline))
                .take(STATUS_POLL_LIMIT)
                .collect();
            assert_eq!(
                wait_for_desktop_online(&mut desktop).await,
                Err("worker_startup_timeout")
            );
        });
    }

    #[test]
    fn ready_startup_timeout_is_bounded_once_and_keeps_legacy_as_owner() {
        tauri::async_runtime::block_on(async {
            let mut backend = FakeBackend::ready(&[]);
            backend.statuses = std::iter::repeat_with(|| status(WorkerStatus::Offline))
                .take(STATUS_POLL_LIMIT)
                .collect();
            assert_eq!(
                takeover(&mut backend).await.unwrap_err(),
                "worker_startup_timeout"
            );
            assert_eq!(backend.task_state, LegacyTaskState::Running);
            assert_eq!(backend.lock, ProcessLockObservation::Held);
            assert_eq!(backend.operations, [TaskOperation::Start]);
            assert_eq!(backend.statuses.len(), 0);
            assert!(backend
                .events
                .contains(&Event::Intervention("worker_startup_timeout")));
        });
    }

    #[test]
    fn draining_does_not_disable_until_authoritative_offline_and_exit_proof() {
        tauri::async_runtime::block_on(async {
            let mut backend = FakeBackend::running(&[
                WorkerStatus::Online,
                WorkerStatus::Online,
                WorkerStatus::Draining,
                WorkerStatus::Draining,
                WorkerStatus::Draining,
                WorkerStatus::Offline,
                WorkerStatus::Online,
            ]);
            backend.statuses[4] = status_with(WorkerStatus::Draining, 0, 0, true);
            takeover(&mut backend)
                .await
                .expect("busy drain eventually completes");
            let get_offline = backend
                .events
                .iter()
                .position(|event| *event == Event::Get("OFFLINE"))
                .unwrap();
            let quiescent_draining = backend
                .events
                .iter()
                .rposition(|event| *event == Event::Get("DRAINING"))
                .unwrap();
            let disable = backend
                .events
                .iter()
                .position(|event| *event == Event::Task(TaskOperation::Disable))
                .unwrap();
            assert!(get_offline < disable);
            assert!(quiescent_draining < get_offline);
        });
    }

    #[test]
    fn drain_failure_never_disables_task() {
        tauri::async_runtime::block_on(async {
            let mut backend = FakeBackend::running(&[
                WorkerStatus::Online,
                WorkerStatus::Online,
                WorkerStatus::Draining,
                WorkerStatus::Online,
            ]);
            assert_eq!(
                takeover(&mut backend).await.unwrap_err(),
                "worker_drain_interrupted"
            );
            assert!(!backend.operations.contains(&TaskOperation::Disable));
        });
    }

    #[test]
    fn ready_task_drain_failure_leaves_legacy_running_and_enabled() {
        tauri::async_runtime::block_on(async {
            let mut backend = FakeBackend::ready(&[
                WorkerStatus::Online,
                WorkerStatus::Online,
                WorkerStatus::Draining,
                WorkerStatus::Online,
            ]);
            assert_eq!(
                takeover(&mut backend).await.unwrap_err(),
                "worker_drain_interrupted"
            );
            assert_eq!(backend.task_state, LegacyTaskState::Running);
            assert_eq!(backend.operations, [TaskOperation::Start]);
            assert_eq!(backend.requested, ["DESKTOP_LEGACY_CUTOVER"]);
        });
    }

    #[test]
    fn failed_ready_task_start_preserves_ready_state_without_disabling() {
        tauri::async_runtime::block_on(async {
            let mut backend = FakeBackend::ready(&[]);
            backend.task_start_error = Some(("worker_legacy_task_start_failed".to_string(), false));
            assert_eq!(
                takeover(&mut backend).await.unwrap_err(),
                "worker_legacy_task_start_failed"
            );
            assert_eq!(backend.task_state, LegacyTaskState::Ready);
            assert!(backend.operations.is_empty());
            assert!(backend.requested.is_empty());
            assert!(backend.events.contains(&Event::MarkLegacyReady));
        });
    }

    #[test]
    fn ready_start_that_ran_but_reported_failure_is_drained_back_to_ready() {
        tauri::async_runtime::block_on(async {
            let mut backend = FakeBackend::ready(&[
                WorkerStatus::Offline,
                WorkerStatus::Registering,
                WorkerStatus::Online,
                WorkerStatus::Draining,
                WorkerStatus::Offline,
            ]);
            backend.task_start_error = Some(("worker_legacy_task_start_failed".to_string(), true));
            assert_eq!(
                takeover(&mut backend).await.unwrap_err(),
                "worker_legacy_task_start_failed"
            );
            assert_eq!(backend.task_state, LegacyTaskState::Ready);
            assert_eq!(backend.requested, ["DESKTOP_LEGACY_CUTOVER"]);
            assert_eq!(backend.operations, [TaskOperation::Start]);
            assert!(!backend.operations.contains(&TaskOperation::Disable));
        });
    }

    #[test]
    fn preexisting_drain_is_not_adopted_or_disabled() {
        tauri::async_runtime::block_on(async {
            let mut backend = FakeBackend::running(&[WorkerStatus::Draining]);
            assert_eq!(
                takeover(&mut backend).await.unwrap_err(),
                "worker_cutover_drain_already_in_progress"
            );
            assert!(backend.requested.is_empty());
            assert!(backend.operations.is_empty());
        });
    }

    #[test]
    fn startup_cleanup_uncertainty_keeps_legacy_task_disabled() {
        tauri::async_runtime::block_on(async {
            let mut backend = FakeBackend::running(&[
                WorkerStatus::Online,
                WorkerStatus::Online,
                WorkerStatus::Draining,
                WorkerStatus::Offline,
            ]);
            backend.desktop_start_error = Some("worker_startup_cleanup_failed".to_string());
            assert_eq!(
                takeover(&mut backend).await.unwrap_err(),
                "worker_cutover_rollback_required"
            );
            assert_eq!(backend.task_state, LegacyTaskState::Disabled);
            assert_eq!(backend.operations, [TaskOperation::Disable]);
            assert!(backend
                .events
                .contains(&Event::Intervention("worker_startup_cleanup_failed")));
        });
    }

    #[test]
    fn desktop_online_observation_loss_does_not_reenable_legacy_task() {
        tauri::async_runtime::block_on(async {
            let mut backend = FakeBackend::running(&[
                WorkerStatus::Online,
                WorkerStatus::Online,
                WorkerStatus::Draining,
                WorkerStatus::Offline,
                WorkerStatus::Registering,
            ]);
            backend
                .statuses
                .push_back(Err("worker_drain_unavailable".to_string()));
            assert_eq!(
                takeover(&mut backend).await.unwrap_err(),
                "worker_cutover_rollback_required"
            );
            assert_eq!(backend.task_state, LegacyTaskState::Disabled);
            assert_eq!(backend.operations, [TaskOperation::Disable]);
            assert!(backend.runtime.runtime_installed);
            assert!(!backend.operations.iter().any(|operation| matches!(
                operation,
                TaskOperation::Enable | TaskOperation::Start
            )));
        });
    }

    #[test]
    fn clean_start_failure_restores_original_running_owner() {
        tauri::async_runtime::block_on(async {
            let mut backend = FakeBackend::running(&[
                WorkerStatus::Online,
                WorkerStatus::Online,
                WorkerStatus::Draining,
                WorkerStatus::Offline,
                WorkerStatus::Online,
            ]);
            backend.desktop_start_error = Some("worker_process_start_failed".to_string());
            assert_eq!(
                takeover(&mut backend).await.unwrap_err(),
                "worker_process_start_failed"
            );
            assert_eq!(backend.task_state, LegacyTaskState::Running);
            assert_eq!(
                backend.operations,
                [
                    TaskOperation::Disable,
                    TaskOperation::Enable,
                    TaskOperation::Start
                ]
            );
            assert!(backend.events.contains(&Event::MarkLegacyActive));
        });
    }

    #[test]
    fn original_ready_state_is_restored_as_ready_after_clean_start_failure() {
        tauri::async_runtime::block_on(async {
            let mut backend = FakeBackend::ready(&[
                WorkerStatus::Online,
                WorkerStatus::Online,
                WorkerStatus::Draining,
                WorkerStatus::Offline,
            ]);
            backend.desktop_start_error = Some("worker_process_start_failed".to_string());
            assert_eq!(
                takeover(&mut backend).await.unwrap_err(),
                "worker_process_start_failed"
            );
            assert_eq!(backend.task_state, LegacyTaskState::Ready);
            assert_eq!(
                backend.operations,
                [
                    TaskOperation::Start,
                    TaskOperation::Disable,
                    TaskOperation::Enable
                ]
            );
            assert_eq!(
                backend
                    .operations
                    .iter()
                    .filter(|operation| **operation == TaskOperation::Start)
                    .count(),
                1,
                "the only Start is the initial READY-task activation"
            );
        });
    }

    #[test]
    fn manual_rollback_drains_desktop_exits_naturally_then_starts_legacy() {
        tauri::async_runtime::block_on(async {
            let mut backend = FakeBackend::new(
                LegacyTaskState::Disabled,
                &[
                    WorkerStatus::Online,
                    WorkerStatus::Draining,
                    WorkerStatus::Offline,
                    WorkerStatus::Offline,
                    WorkerStatus::Registering,
                    WorkerStatus::Degraded,
                ],
            );
            backend.runtime = WorkerRuntimeObservation {
                runtime_installed: true,
                startup_cleanup_pending: false,
                process_alive: Some(true),
                job_process_count: Some(1),
                process_exit_proven: false,
                server_online_verified: false,
            };
            backend.lock = ProcessLockObservation::Held;
            rollback_to_legacy(&mut backend)
                .await
                .expect("manual rollback completes");
            assert_eq!(backend.requested, ["DESKTOP_ROLLBACK"]);
            assert_eq!(
                backend.operations,
                [TaskOperation::Enable, TaskOperation::Start]
            );
            let offline = backend
                .events
                .iter()
                .position(|event| *event == Event::Get("OFFLINE"))
                .unwrap();
            let reap = backend
                .events
                .iter()
                .position(|event| *event == Event::ReapDesktop)
                .unwrap();
            let enable = backend
                .events
                .iter()
                .position(|event| *event == Event::Task(TaskOperation::Enable))
                .unwrap();
            let legacy_offline = backend
                .events
                .iter()
                .rposition(|event| *event == Event::Get("OFFLINE"))
                .unwrap();
            let legacy_degraded = backend
                .events
                .iter()
                .position(|event| *event == Event::Get("DEGRADED"))
                .unwrap();
            let legacy_registering = backend
                .events
                .iter()
                .position(|event| *event == Event::Get("REGISTERING"))
                .unwrap();
            assert!(offline < reap && reap < enable && enable < legacy_offline);
            assert!(legacy_offline < legacy_registering && legacy_registering < legacy_degraded);
        });
    }

    #[test]
    fn manual_rollback_keeps_legacy_owner_when_status_becomes_unavailable_after_start() {
        tauri::async_runtime::block_on(async {
            let mut backend = FakeBackend::new(
                LegacyTaskState::Disabled,
                &[
                    WorkerStatus::Online,
                    WorkerStatus::Draining,
                    WorkerStatus::Offline,
                    WorkerStatus::Offline,
                ],
            );
            backend
                .statuses
                .push_back(Err("worker_drain_unavailable".to_string()));
            backend.runtime = WorkerRuntimeObservation {
                runtime_installed: true,
                startup_cleanup_pending: false,
                process_alive: Some(true),
                job_process_count: Some(1),
                process_exit_proven: false,
                server_online_verified: true,
            };
            backend.lock = ProcessLockObservation::Held;
            assert_eq!(
                rollback_to_legacy(&mut backend).await.unwrap_err(),
                "worker_rollback_failed"
            );
            assert_eq!(backend.task_state, LegacyTaskState::Running);
            assert_eq!(
                backend.operations,
                [TaskOperation::Enable, TaskOperation::Start]
            );
            assert!(backend
                .events
                .contains(&Event::Intervention("worker_rollback_failed")));
        });
    }

    #[test]
    fn manual_rollback_keeps_task_disabled_if_desktop_does_not_exit() {
        tauri::async_runtime::block_on(async {
            let mut backend = FakeBackend::new(
                LegacyTaskState::Disabled,
                &[
                    WorkerStatus::Online,
                    WorkerStatus::Draining,
                    WorkerStatus::Offline,
                ],
            );
            backend.runtime = WorkerRuntimeObservation {
                runtime_installed: true,
                startup_cleanup_pending: false,
                process_alive: Some(true),
                job_process_count: Some(1),
                process_exit_proven: false,
                server_online_verified: true,
            };
            backend.lock = ProcessLockObservation::Held;
            backend.desktop_exits_after_offline = false;
            assert_eq!(
                rollback_to_legacy(&mut backend).await.unwrap_err(),
                "worker_process_exit_timeout"
            );
            assert_eq!(backend.task_state, LegacyTaskState::Disabled);
            assert!(backend.operations.is_empty());
            assert!(backend.runtime.runtime_installed);
        });
    }

    #[test]
    fn worker_drain_wire_quiescence_matches_the_server_transition_contract() {
        let busy_draining = status_with(WorkerStatus::Draining, 1, 2, false).unwrap();
        assert!(validate_drain_status_shape(&busy_draining).is_ok());

        let quiescent_draining = status_with(WorkerStatus::Draining, 0, 0, true).unwrap();
        assert!(validate_drain_status_shape(&quiescent_draining).is_ok());

        let offline = status_with(WorkerStatus::Offline, 0, 0, false).unwrap();
        assert!(validate_drain_status_shape(&offline).is_ok());

        let invalid_offline = status_with(WorkerStatus::Offline, 0, 1, false).unwrap();
        assert_eq!(
            validate_drain_status_shape(&invalid_offline).unwrap_err(),
            "worker_drain_response_invalid"
        );

        let impossible_offline = status_with(WorkerStatus::Offline, 0, 0, true).unwrap();
        assert_eq!(
            validate_drain_status_shape(&impossible_offline).unwrap_err(),
            "worker_drain_response_invalid"
        );

        let inconsistent_draining = status_with(WorkerStatus::Draining, 0, 0, false).unwrap();
        assert_eq!(
            validate_drain_status_shape(&inconsistent_draining).unwrap_err(),
            "worker_drain_response_invalid"
        );
    }

    #[test]
    fn desktop_quit_drains_once_and_waits_offline_exit_and_reap_before_logout() {
        tauri::async_runtime::block_on(async {
            let mut backend = FakeBackend::desktop(&[
                WorkerStatus::Online,
                WorkerStatus::Draining,
                WorkerStatus::Draining,
                WorkerStatus::Offline,
            ]);
            quit_desktop_worker(&mut backend)
                .await
                .expect("Desktop Worker stops gracefully");
            backend.events.push(Event::Logout);

            assert_eq!(backend.requested, ["DESKTOP_QUIT"]);
            assert!(backend.operations.is_empty());
            assert!(!backend.runtime.runtime_installed);
            assert_eq!(backend.runtime.job_process_count, Some(0));
            assert_eq!(backend.lock, ProcessLockObservation::NotHeld);
            let post = backend
                .events
                .iter()
                .position(|event| *event == Event::Post("DESKTOP_QUIT"))
                .expect("one drain POST");
            let offline = backend
                .events
                .iter()
                .position(|event| *event == Event::Get("OFFLINE"))
                .expect("authoritative OFFLINE poll");
            let reap = backend
                .events
                .iter()
                .position(|event| *event == Event::ReapDesktop)
                .expect("natural runtime reap");
            let logout = backend
                .events
                .iter()
                .position(|event| *event == Event::Logout)
                .expect("logout after terminal proof");
            assert!(post < offline && offline < reap && reap < logout);
            assert_eq!(
                backend
                    .events
                    .iter()
                    .filter(|event| matches!(event, Event::Post(_)))
                    .count(),
                1
            );
        });
    }

    #[test]
    fn quit_drain_failure_keeps_desktop_process_and_does_not_reap() {
        tauri::async_runtime::block_on(async {
            let mut backend = FakeBackend::desktop(&[WorkerStatus::Online, WorkerStatus::Draining]);
            backend.statuses.extend(
                std::iter::repeat_with(|| status(WorkerStatus::Draining)).take(STATUS_POLL_LIMIT),
            );

            assert_eq!(
                quit_desktop_worker(&mut backend).await.unwrap_err(),
                "worker_drain_timeout"
            );
            assert_eq!(backend.requested, ["DESKTOP_QUIT"]);
            assert!(backend.runtime.runtime_installed);
            assert_eq!(backend.runtime.job_process_count, Some(1));
            assert!(!backend.events.contains(&Event::ReapDesktop));
            assert!(backend
                .events
                .contains(&Event::Intervention("worker_drain_timeout")));
        });
    }

    #[test]
    fn offline_without_natural_process_tree_exit_is_not_graceful_quit() {
        tauri::async_runtime::block_on(async {
            let mut backend = FakeBackend::desktop(&[
                WorkerStatus::Online,
                WorkerStatus::Draining,
                WorkerStatus::Offline,
            ]);
            backend.desktop_exits_after_offline = false;

            assert_eq!(
                quit_desktop_worker(&mut backend).await.unwrap_err(),
                "worker_process_exit_timeout"
            );
            assert!(backend.runtime.runtime_installed);
            assert_eq!(backend.runtime.job_process_count, Some(1));
            assert_eq!(backend.lock, ProcessLockObservation::Held);
            assert!(!backend.events.contains(&Event::ReapDesktop));
        });
    }

    #[test]
    fn offline_without_empty_job_or_released_lock_is_not_graceful_quit() {
        for (job_empty, lock_released) in [(false, true), (true, false)] {
            tauri::async_runtime::block_on(async {
                let mut backend = FakeBackend::desktop(&[
                    WorkerStatus::Online,
                    WorkerStatus::Draining,
                    WorkerStatus::Offline,
                ]);
                backend.desktop_job_empty_after_offline = job_empty;
                backend.desktop_lock_released_after_offline = lock_released;

                assert_eq!(
                    quit_desktop_worker(&mut backend).await.unwrap_err(),
                    "worker_process_exit_timeout"
                );
                assert!(backend.runtime.runtime_installed);
                assert!(!backend.events.contains(&Event::ReapDesktop));
            });
        }
    }

    #[test]
    fn restart_uses_fixed_reason_and_requires_same_binding_online_after_relaunch() {
        tauri::async_runtime::block_on(async {
            let mut backend = FakeBackend::desktop(&[
                WorkerStatus::Online,
                WorkerStatus::Draining,
                WorkerStatus::Draining,
                WorkerStatus::Offline,
                WorkerStatus::Offline,
                WorkerStatus::Registering,
                WorkerStatus::Degraded,
                WorkerStatus::Online,
            ]);
            let original_binding = backend.binding.clone();
            restart_desktop_worker(&mut backend)
                .await
                .expect("Desktop Worker restarts online");

            assert_eq!(backend.requested, ["DESKTOP_RESTART"]);
            assert_eq!(backend.binding.worker_id, original_binding.worker_id);
            assert_eq!(
                backend.binding.identity_marker_sha256,
                original_binding.identity_marker_sha256
            );
            assert_eq!(
                backend.binding.protected_key_sha256,
                original_binding.protected_key_sha256
            );
            assert_eq!(backend.task_state, LegacyTaskState::Disabled);
            assert!(backend.operations.is_empty());
            let reap = backend
                .events
                .iter()
                .position(|event| *event == Event::ReapDesktop)
                .expect("old runtime was reaped naturally");
            let start = backend
                .events
                .iter()
                .position(|event| *event == Event::StartDesktop)
                .expect("same Desktop binding restarted");
            let online = backend
                .events
                .iter()
                .rposition(|event| *event == Event::Get("ONLINE"))
                .expect("authoritative ONLINE after restart");
            let marked = backend
                .events
                .iter()
                .position(|event| *event == Event::MarkDesktopOnline)
                .expect("health marked only after ONLINE");
            assert!(reap < start && start < online && online < marked);
        });
    }

    #[test]
    fn restart_timeout_keeps_legacy_task_disabled_and_reports_intervention() {
        tauri::async_runtime::block_on(async {
            let mut backend = FakeBackend::desktop(&[
                WorkerStatus::Online,
                WorkerStatus::Draining,
                WorkerStatus::Offline,
            ]);
            backend.statuses.extend(
                std::iter::repeat_with(|| status(WorkerStatus::Offline)).take(STATUS_POLL_LIMIT),
            );

            assert_eq!(
                restart_desktop_worker(&mut backend).await.unwrap_err(),
                "worker_cutover_rollback_required"
            );
            assert_eq!(backend.task_state, LegacyTaskState::Disabled);
            assert!(backend.operations.is_empty());
            assert_eq!(backend.requested, ["DESKTOP_RESTART"]);
            assert!(backend
                .events
                .contains(&Event::Intervention("worker_startup_timeout")));
        });
    }

    #[test]
    fn restart_terminal_online_commit_failure_records_local_diagnostic_without_logout_or_fallback()
    {
        tauri::async_runtime::block_on(async {
            let mut backend = FakeBackend::desktop(&[
                WorkerStatus::Online,
                WorkerStatus::Draining,
                WorkerStatus::Draining,
                WorkerStatus::Offline,
                WorkerStatus::Offline,
                WorkerStatus::Registering,
                WorkerStatus::Online,
            ]);
            backend.mark_desktop_online_error = Some("worker_process_exited".to_string());
            let logout_called = std::cell::Cell::new(false);

            let result = crate::worker_lifecycle_then_logout(
                restart_desktop_worker(&mut backend),
                || async { logout_called.set(true) },
            )
            .await;

            assert_eq!(result, Err("worker_cutover_rollback_required".to_string()));
            assert!(backend.events.contains(&Event::Get("ONLINE")));
            assert!(backend.events.contains(&Event::MarkDesktopOnline));
            assert!(backend
                .events
                .contains(&Event::Intervention("worker_process_exited")));
            assert_eq!(backend.task_state, LegacyTaskState::Disabled);
            assert!(!backend.operations.iter().any(|operation| matches!(
                operation,
                TaskOperation::Enable | TaskOperation::Start
            )));
            assert_eq!(backend.requested, ["DESKTOP_RESTART"]);
            assert!(!logout_called.get());
        });
    }

    #[test]
    fn takeover_terminal_online_commit_failure_keeps_legacy_disabled_and_records_diagnostic() {
        tauri::async_runtime::block_on(async {
            let mut backend = FakeBackend::running(&[
                WorkerStatus::Online,
                WorkerStatus::Online,
                WorkerStatus::Draining,
                WorkerStatus::Offline,
                WorkerStatus::Offline,
                WorkerStatus::Registering,
                WorkerStatus::Degraded,
                WorkerStatus::Online,
            ]);
            backend.mark_desktop_online_error =
                Some("worker_process_lock_not_acquired".to_string());

            assert_eq!(
                takeover(&mut backend).await,
                Err("worker_cutover_rollback_required".to_string())
            );
            assert!(backend.events.contains(&Event::Get("ONLINE")));
            assert!(backend.events.contains(&Event::MarkDesktopOnline));
            assert!(backend
                .events
                .contains(&Event::Intervention("worker_process_lock_not_acquired")));
            assert_eq!(backend.task_state, LegacyTaskState::Disabled);
            assert!(!backend.operations.iter().any(|operation| matches!(
                operation,
                TaskOperation::Enable | TaskOperation::Start
            )));
        });
    }

    #[test]
    fn decommission_preserves_legacy_running_or_ready_worker_without_drain() {
        tauri::async_runtime::block_on(async {
            let mut running = FakeBackend::running(&[]);
            verify_legacy_owner_for_decommission(&mut running)
                .await
                .expect("active legacy owner remains untouched");
            assert!(running.requested.is_empty());
            assert!(running.operations.is_empty());
            assert!(running.events.contains(&Event::MarkLegacyActive));

            let mut ready = FakeBackend::ready(&[]);
            verify_legacy_owner_for_decommission(&mut ready)
                .await
                .expect("enabled ready task remains untouched");
            assert!(ready.requested.is_empty());
            assert!(ready.operations.is_empty());
            assert!(ready.events.contains(&Event::MarkLegacyReady));
        });
    }
}
