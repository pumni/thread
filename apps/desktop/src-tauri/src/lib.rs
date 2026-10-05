mod controller_store;
mod controller_tls;
mod controller_trust;
mod operator_client;
mod startup_gate;
mod supervisor;
mod windows_crypto;
mod worker_cutover;
mod worker_host;

use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
};

#[cfg(windows)]
use auto_launch::WindowsEnableMode;
use auto_launch::{AutoLaunch, AutoLaunchBuilder};
use controller_tls::ControllerTlsSummary;
use controller_trust::{ControllerTrustState, TrustProbeSummary, TrustedControllerSummary};
use serde::{Deserialize, Serialize};
use supervisor::{Supervisor, SupervisorSnapshot};
use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::TrayIconBuilder,
    AppHandle, Emitter, Manager, State,
};
use zeroize::Zeroizing;

use operator_client::{
    CreatedOperatorUser, OperatorAuthState, OperatorIdentity, OperatorUser, WorkerDrainStatus,
};
use worker_cutover::{CutoverFuture, DurableDrainOfflineProof, WorkerCutoverBackend};

const CONFIG_SCHEMA_VERSION: u32 = 1;
const QUIT_EVENT: &str = "desktop://quit-requested";
const SESSION_LOCKED_EVENT: &str = "desktop://session-locked";

fn run_native_lock_boundary(detach: impl FnOnce(), notify: impl FnOnce()) {
    detach();
    notify();
}

fn run_window_reopen(is_visible: bool, lock: impl FnOnce(), show: impl FnOnce()) {
    if !is_visible {
        lock();
    }
    show();
}

fn native_session_lock(app: &AppHandle) {
    run_native_lock_boundary(
        || app.state::<OperatorAuthState>().lock_session(),
        || {
            let _ = app.emit(SESSION_LOCKED_EVENT, ());
        },
    );
}

fn autolaunch() -> Result<AutoLaunch, String> {
    let executable =
        std::env::current_exe().map_err(|_| "autostart_executable_path_unavailable".to_string())?;
    #[cfg(windows)]
    let executable = format!("\"{}\"", executable.to_string_lossy());
    #[cfg(not(windows))]
    let executable = executable.to_string_lossy().into_owned();

    let mut builder = AutoLaunchBuilder::new();
    let no_arguments: [&str; 0] = [];
    builder
        .set_app_name("Threads Desktop")
        .set_app_path(&executable)
        .set_args(&no_arguments);
    #[cfg(windows)]
    builder.set_windows_enable_mode(WindowsEnableMode::CurrentUser);

    builder
        .build()
        .map_err(|_| "autostart_configuration_failed".to_string())
}

fn enable_autostart() -> Result<(), String> {
    let registration = autolaunch()?;
    if registration.enable().is_err() {
        disable_registration(&registration).map_err(|_| "autostart_enable_cleanup_failed")?;
        return Err("autostart_enable_failed".to_string());
    }
    Ok(())
}

fn disable_registration(registration: &AutoLaunch) -> Result<(), String> {
    match registration.disable() {
        Ok(()) => Ok(()),
        Err(auto_launch::Error::Io(error)) if error.kind() == std::io::ErrorKind::NotFound => {
            Ok(())
        }
        Err(_) => Err("autostart_disable_failed".to_string()),
    }
}

fn disable_autostart() -> Result<(), String> {
    disable_registration(&autolaunch()?)
}

#[cfg(windows)]
mod windows_session_lock {
    use crate::SESSION_LOCKED_EVENT;
    use tauri::{AppHandle, WebviewWindow};
    use windows_sys::Win32::{
        Foundation::{HWND, LPARAM, LRESULT, WPARAM},
        System::RemoteDesktop::{
            WTSRegisterSessionNotification, WTSUnRegisterSessionNotification,
            NOTIFY_FOR_THIS_SESSION,
        },
        UI::{
            Shell::{DefSubclassProc, RemoveWindowSubclass, SetWindowSubclass},
            WindowsAndMessaging::{
                WM_NCDESTROY, WM_WTSSESSION_CHANGE, WTS_SESSION_LOCK, WTS_SESSION_UNLOCK,
            },
        },
    };

    const SUBCLASS_ID: usize = 0x5448_5244;

    fn event_for_session_message(message: u32, event: usize) -> Option<&'static str> {
        (message == WM_WTSSESSION_CHANGE
            && (event == WTS_SESSION_LOCK as usize || event == WTS_SESSION_UNLOCK as usize))
            .then_some(SESSION_LOCKED_EVENT)
    }

    unsafe extern "system" fn session_window_proc(
        hwnd: HWND,
        message: u32,
        wparam: WPARAM,
        lparam: LPARAM,
        subclass_id: usize,
        reference_data: usize,
    ) -> LRESULT {
        if event_for_session_message(message, wparam).is_some() {
            let app = &*(reference_data as *const AppHandle);
            crate::native_session_lock(app);
        }

        if message == WM_NCDESTROY {
            let _ = RemoveWindowSubclass(hwnd, Some(session_window_proc), subclass_id);
            let _ = WTSUnRegisterSessionNotification(hwnd);
            drop(Box::from_raw(reference_data as *mut AppHandle));
        }
        DefSubclassProc(hwnd, message, wparam, lparam)
    }

    pub fn register(window: &WebviewWindow, app: AppHandle) -> Result<(), String> {
        let hwnd = window
            .hwnd()
            .map_err(|_| "windows_session_lock_window_handle_unavailable".to_string())?
            .0 as HWND;
        let app_pointer = Box::into_raw(Box::new(app)) as usize;
        unsafe {
            if WTSRegisterSessionNotification(hwnd, NOTIFY_FOR_THIS_SESSION) == 0 {
                drop(Box::from_raw(app_pointer as *mut AppHandle));
                return Err("windows_session_lock_registration_failed".to_string());
            }
            if SetWindowSubclass(hwnd, Some(session_window_proc), SUBCLASS_ID, app_pointer) == 0 {
                let _ = WTSUnRegisterSessionNotification(hwnd);
                drop(Box::from_raw(app_pointer as *mut AppHandle));
                return Err("windows_session_lock_message_hook_failed".to_string());
            }
        }
        Ok(())
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn windows_session_lock_and_unlock_require_reauthentication() {
            assert_eq!(
                event_for_session_message(WM_WTSSESSION_CHANGE, WTS_SESSION_LOCK as usize),
                Some(SESSION_LOCKED_EVENT)
            );
            assert_eq!(
                event_for_session_message(WM_WTSSESSION_CHANGE, WTS_SESSION_UNLOCK as usize),
                Some(SESSION_LOCKED_EVENT),
                "unlock must keep the Operator UI locked"
            );
            assert_eq!(event_for_session_message(WM_NCDESTROY, 0), None);
        }
    }
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ProvisionedRole {
    Controller,
    Worker,
    Console,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum UiTheme {
    System,
    Light,
    Dark,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct DeviceConfig {
    schema_version: u32,
    role: Option<ProvisionedRole>,
    theme: UiTheme,
    autostart_enabled: bool,
}

impl Default for DeviceConfig {
    fn default() -> Self {
        Self {
            schema_version: CONFIG_SCHEMA_VERSION,
            role: None,
            theme: UiTheme::System,
            autostart_enabled: false,
        }
    }
}

impl DeviceConfig {
    fn load(path: &Path) -> Result<Self, String> {
        if !path.exists() {
            return Ok(Self::default());
        }

        let bytes = fs::read(path).map_err(|_| "config_read_failed".to_string())?;
        let config: Self = serde_json::from_slice(&bytes)
            .map_err(|_| "config_invalid_or_incompatible".to_string())?;
        if config.schema_version != CONFIG_SCHEMA_VERSION {
            return Err("config_invalid_or_incompatible".to_string());
        }
        Ok(config)
    }

    fn save(&self, path: &Path) -> Result<(), String> {
        let parent = path
            .parent()
            .ok_or_else(|| "config_path_invalid".to_string())?;
        fs::create_dir_all(parent).map_err(|_| "config_write_failed".to_string())?;

        let mut temporary = tempfile::NamedTempFile::new_in(parent)
            .map_err(|_| "config_write_failed".to_string())?;
        serde_json::to_writer(&mut temporary, self)
            .map_err(|_| "config_write_failed".to_string())?;
        temporary
            .flush()
            .and_then(|()| temporary.as_file().sync_all())
            .map_err(|_| "config_write_failed".to_string())?;
        temporary
            .persist(path)
            .map_err(|_| "config_write_failed".to_string())?;
        Ok(())
    }
}

struct DeviceStateInner {
    config: DeviceConfig,
    supervisor: Supervisor,
}

#[derive(Clone)]
struct DeviceState {
    config_path: PathBuf,
    inner: Arc<Mutex<DeviceStateInner>>,
    worker_operation_active: Arc<AtomicBool>,
}

struct WorkerOperationGuard(Arc<AtomicBool>);

impl Drop for WorkerOperationGuard {
    fn drop(&mut self) {
        self.0.store(false, Ordering::Release);
    }
}

impl DeviceState {
    #[cfg(test)]
    fn load(
        config_path: PathBuf,
        controller_root: PathBuf,
        runtime_root: PathBuf,
    ) -> Result<Self, String> {
        Self::load_with_worker_task_helper(config_path, controller_root, runtime_root, None)
    }

    fn load_with_worker_task_helper(
        config_path: PathBuf,
        controller_root: PathBuf,
        runtime_root: PathBuf,
        worker_task_helper: Option<PathBuf>,
    ) -> Result<Self, String> {
        let config = DeviceConfig::load(&config_path)?;
        let mut supervisor = Supervisor::with_controller_paths(controller_root, runtime_root)
            .with_worker_task_helper(worker_task_helper);
        if matches!(
            config.role,
            Some(ProvisionedRole::Controller | ProvisionedRole::Worker)
        ) {
            let _ = supervisor.start(config.role.expect("role was checked"));
        }

        Ok(Self {
            config_path,
            inner: Arc::new(Mutex::new(DeviceStateInner { config, supervisor })),
            worker_operation_active: Arc::new(AtomicBool::new(false)),
        })
    }

    fn snapshot(&self) -> Result<DesktopSnapshot, String> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| "state_unavailable".to_string())?;
        inner.supervisor.refresh_health();
        Ok(DesktopSnapshot::from(&*inner))
    }

    fn begin_worker_operation(&self) -> Result<WorkerOperationGuard, String> {
        self.worker_operation_active
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .map_err(|_| "worker_cutover_operation_in_progress".to_string())?;
        Ok(WorkerOperationGuard(Arc::clone(
            &self.worker_operation_active,
        )))
    }

    fn worker_cutover_context(
        &self,
        rollback: bool,
    ) -> Result<supervisor::WorkerCutoverContext, String> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| "state_unavailable".to_string())?;
        if inner.config.role != Some(ProvisionedRole::Worker) {
            return Err("worker_host_configuration_required".to_string());
        }
        if rollback {
            inner.supervisor.worker_rollback_context()
        } else {
            inner.supervisor.worker_cutover_context()
        }
    }

    fn provision(&self, role: ProvisionedRole) -> Result<DesktopSnapshot, String> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| "state_unavailable".to_string())?;
        if inner.config.role.is_some() {
            return Err("device_already_provisioned".to_string());
        }

        let mut updated = inner.config.clone();
        updated.role = Some(role);
        updated.autostart_enabled = true;
        updated.save(&self.config_path)?;
        inner.config = updated;

        if matches!(role, ProvisionedRole::Controller | ProvisionedRole::Worker) {
            inner.supervisor.start(role)?;
        }
        Ok(DesktopSnapshot::from(&*inner))
    }

    fn reset_ui_preferences(&self) -> Result<DesktopSnapshot, String> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| "state_unavailable".to_string())?;
        let mut updated = inner.config.clone();
        updated.theme = UiTheme::System;
        updated.save(&self.config_path)?;
        inner.config = updated;
        Ok(DesktopSnapshot::from(&*inner))
    }

    fn mark_autostart_unavailable(&self) -> Result<(), String> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| "state_unavailable".to_string())?;
        let mut updated = inner.config.clone();
        updated.autostart_enabled = false;
        updated.save(&self.config_path)?;
        inner.config = updated;
        Ok(())
    }

    fn decommission(&self) -> Result<DesktopSnapshot, String> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| "state_unavailable".to_string())?;
        inner.supervisor.stop()?;

        let updated = DeviceConfig {
            theme: inner.config.theme,
            ..DeviceConfig::default()
        };
        updated.save(&self.config_path)?;
        inner.config = updated;
        Ok(DesktopSnapshot::from(&*inner))
    }

    fn stop(&self) -> Result<(), String> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| "state_unavailable".to_string())?;
        inner.supervisor.stop()
    }

    fn role(&self) -> Result<Option<ProvisionedRole>, String> {
        let inner = self
            .inner
            .lock()
            .map_err(|_| "state_unavailable".to_string())?;
        Ok(inner.config.role)
    }

    fn local_controller_url(&self) -> Result<String, String> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| "state_unavailable".to_string())?;
        if inner.config.role != Some(ProvisionedRole::Controller) {
            return Err("controller_runtime_unavailable".to_string());
        }
        inner.supervisor.refresh_health();
        inner
            .supervisor
            .local_controller_endpoint()
            .map_err(str::to_string)
    }

    fn local_worker_id(&self) -> Result<String, String> {
        let inner = self
            .inner
            .lock()
            .map_err(|_| "state_unavailable".to_string())?;
        if inner.config.role != Some(ProvisionedRole::Worker) {
            return Err("worker_host_configuration_required".to_string());
        }
        inner.supervisor.local_worker_id()
    }

    fn bootstrap_owner(&self, username: &str, password: &str) -> Result<String, String> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| "state_unavailable".to_string())?;
        if inner.config.role != Some(ProvisionedRole::Controller) {
            return Err("controller_runtime_unavailable".to_string());
        }
        inner.supervisor.bootstrap_owner(username, password)?;
        inner
            .supervisor
            .local_controller_endpoint()
            .map_err(str::to_string)
    }

    fn configure_https(
        &self,
        lan_address: &str,
        port: u16,
    ) -> Result<ControllerTlsSummary, String> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| "state_unavailable".to_string())?;
        if inner.config.role != Some(ProvisionedRole::Controller) {
            return Err("controller_runtime_unavailable".to_string());
        }
        inner.supervisor.configure_https(lan_address, port)
    }

    fn controller_owner_exists(&self) -> Result<bool, String> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| "state_unavailable".to_string())?;
        if inner.config.role != Some(ProvisionedRole::Controller) {
            return Err("controller_runtime_unavailable".to_string());
        }
        inner.supervisor.controller_owner_exists()
    }

    fn reconfigure_https(
        &self,
        lan_address: &str,
        port: u16,
        owner_authorized: bool,
    ) -> Result<ControllerTlsSummary, String> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| "state_unavailable".to_string())?;
        if inner.config.role != Some(ProvisionedRole::Controller) {
            return Err("controller_runtime_unavailable".to_string());
        }
        inner
            .supervisor
            .reconfigure_https(lan_address, port, owner_authorized)
    }

    fn controller_https_summary(&self) -> Result<ControllerTlsSummary, String> {
        let inner = self
            .inner
            .lock()
            .map_err(|_| "state_unavailable".to_string())?;
        if inner.config.role != Some(ProvisionedRole::Controller) {
            return Err("controller_runtime_unavailable".to_string());
        }
        inner.supervisor.controller_https_summary()
    }

    fn controller_tls_root(&self) -> Result<Vec<u8>, String> {
        let inner = self
            .inner
            .lock()
            .map_err(|_| "state_unavailable".to_string())?;
        if inner.config.role != Some(ProvisionedRole::Controller) {
            return Err("controller_runtime_unavailable".to_string());
        }
        inner
            .supervisor
            .controller_tls_root()
            .map_err(str::to_string)
    }

    fn trust_path(&self) -> PathBuf {
        controller_trust::trust_path(
            self.config_path
                .parent()
                .unwrap_or_else(|| std::path::Path::new(".")),
        )
    }

    fn trusted_root_for_endpoint(&self, endpoint: &str) -> Result<Vec<u8>, String> {
        controller_trust::trusted_root_for_endpoint(&self.trust_path(), endpoint)
            .map_err(str::to_string)
    }

    fn restart(&self) -> Result<(), String> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| "state_unavailable".to_string())?;
        let role = inner
            .config
            .role
            .ok_or_else(|| "device_not_provisioned".to_string())?;
        if role == ProvisionedRole::Console {
            return Err("restart_not_available_for_console".to_string());
        }
        inner.supervisor.restart(role)
    }
}

struct DeviceWorkerCutover<'a> {
    device: DeviceState,
    operator: &'a OperatorAuthState,
    helper: PathBuf,
    binding: worker_host::WorkerHostBinding,
}

impl DeviceWorkerCutover<'_> {
    fn spawn_blocking<T: Send + 'static>(
        task: impl FnOnce() -> Result<T, String> + Send + 'static,
        failure: &'static str,
    ) -> CutoverFuture<'static, Result<T, String>> {
        Box::pin(async move {
            tauri::async_runtime::spawn_blocking(task)
                .await
                .map_err(|_| failure.to_string())?
        })
    }
}

impl WorkerCutoverBackend for DeviceWorkerCutover<'_> {
    fn binding(&self) -> &worker_host::WorkerHostBinding {
        &self.binding
    }

    fn inspect_task<'a>(
        &'a mut self,
    ) -> CutoverFuture<'a, Result<worker_host::LegacyTaskState, String>> {
        let helper = self.helper.clone();
        let binding = self.binding.clone();
        let device = self.device.clone();
        Box::pin(async move {
            let state = Self::spawn_blocking(
                move || {
                    worker_host::inspect_matching_task(&helper, &binding)
                        .map(|inspection| inspection.state)
                        .map_err(str::to_string)
                },
                "worker_legacy_task_invalid",
            )
            .await?;
            let mut inner = device
                .inner
                .lock()
                .map_err(|_| "state_unavailable".to_string())?;
            inner.supervisor.set_legacy_task_state(state);
            Ok(state)
        })
    }

    fn process_lock_observation(&mut self) -> worker_host::ProcessLockObservation {
        worker_host::observe_process_lock(&self.binding.data_root)
    }

    fn task_operation<'a>(
        &'a mut self,
        operation: worker_host::TaskOperation,
        expected_state: worker_host::LegacyTaskState,
        offline_exit_proof: Option<DurableDrainOfflineProof>,
    ) -> CutoverFuture<'a, Result<worker_host::LegacyTaskState, String>> {
        let helper = self.helper.clone();
        let binding = self.binding.clone();
        let device = self.device.clone();
        Box::pin(async move {
            let state = tauri::async_runtime::spawn_blocking(move || {
                let confirmed = if operation == worker_host::TaskOperation::Disable {
                    offline_exit_proof.is_some_and(|proof| proof.matches(&binding))
                } else {
                    offline_exit_proof.is_none()
                };
                worker_host::run_task_operation(
                    &helper,
                    &binding,
                    expected_state,
                    operation,
                    confirmed,
                )
                .map(|inspection| inspection.state)
                .map_err(str::to_string)
            })
            .await
            .map_err(|_| operation.failure_code().to_string())??;
            let mut inner = device
                .inner
                .lock()
                .map_err(|_| "state_unavailable".to_string())?;
            inner.supervisor.set_legacy_task_state(state);
            Ok(state)
        })
    }

    fn worker_status<'a>(&'a mut self) -> CutoverFuture<'a, Result<WorkerDrainStatus, String>> {
        let operator = self.operator;
        let worker_id = self.binding.worker_id.clone();
        Box::pin(async move { operator.local_worker_drain_status(&worker_id).await })
    }

    fn request_drain<'a>(
        &'a mut self,
        reason_code: &'static str,
    ) -> CutoverFuture<'a, Result<WorkerDrainStatus, String>> {
        let operator = self.operator;
        let worker_id = self.binding.worker_id.clone();
        Box::pin(async move {
            operator
                .request_local_worker_drain(&worker_id, reason_code)
                .await
        })
    }

    fn local_runtime<'a>(
        &'a mut self,
    ) -> CutoverFuture<'a, Result<supervisor::WorkerRuntimeObservation, String>> {
        let device = self.device.clone();
        Self::spawn_blocking(
            move || {
                let mut inner = device
                    .inner
                    .lock()
                    .map_err(|_| "state_unavailable".to_string())?;
                Ok(inner.supervisor.worker_runtime_observation())
            },
            "state_unavailable",
        )
    }

    fn start_desktop_worker<'a>(&'a mut self) -> CutoverFuture<'a, Result<(), String>> {
        let device = self.device.clone();
        let binding = self.binding.clone();
        Self::spawn_blocking(
            move || {
                let mut inner = device
                    .inner
                    .lock()
                    .map_err(|_| "state_unavailable".to_string())?;
                if inner.config.role != Some(ProvisionedRole::Worker) {
                    return Err("worker_host_configuration_required".to_string());
                }
                inner.supervisor.start_worker_after_legacy_cutover(&binding)
            },
            "worker_process_start_failed",
        )
    }

    fn reap_desktop_after_natural_exit<'a>(&'a mut self) -> CutoverFuture<'a, Result<(), String>> {
        let device = self.device.clone();
        Self::spawn_blocking(
            move || {
                let mut inner = device
                    .inner
                    .lock()
                    .map_err(|_| "state_unavailable".to_string())?;
                inner.supervisor.reap_worker_after_natural_exit()
            },
            "worker_process_exit_timeout",
        )
    }

    fn mark_desktop_online(&mut self) -> Result<(), String> {
        let mut inner = self
            .device
            .inner
            .lock()
            .map_err(|_| "state_unavailable".to_string())?;
        inner
            .supervisor
            .mark_worker_server_online(&self.binding.worker_id)
    }

    fn mark_legacy_active(&mut self) -> Result<(), String> {
        let mut inner = self
            .device
            .inner
            .lock()
            .map_err(|_| "state_unavailable".to_string())?;
        inner
            .supervisor
            .mark_legacy_worker_active(self.binding.clone());
        Ok(())
    }

    fn mark_legacy_ready(&mut self, diagnostic: &'static str) -> Result<(), String> {
        let mut inner = self
            .device
            .inner
            .lock()
            .map_err(|_| "state_unavailable".to_string())?;
        inner
            .supervisor
            .mark_legacy_worker_ready(self.binding.clone(), diagnostic);
        Ok(())
    }

    fn mark_intervention(&mut self, diagnostic: &'static str) {
        if let Ok(mut inner) = self.device.inner.lock() {
            inner
                .supervisor
                .mark_worker_cutover_intervention(diagnostic);
        }
    }

    fn pause<'a>(&'a mut self) -> CutoverFuture<'a, ()> {
        Box::pin(async {
            let _ = tauri::async_runtime::spawn_blocking(|| {
                std::thread::sleep(std::time::Duration::from_millis(500));
            })
            .await;
        })
    }
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct DesktopSnapshot {
    schema_version: u32,
    role: Option<ProvisionedRole>,
    theme: UiTheme,
    autostart_enabled: bool,
    supervisor: SupervisorSnapshot,
}

impl From<&DeviceStateInner> for DesktopSnapshot {
    fn from(inner: &DeviceStateInner) -> Self {
        let supervisor = if inner.config.role == Some(ProvisionedRole::Console) {
            SupervisorSnapshot::not_applicable()
        } else {
            inner.supervisor.snapshot()
        };

        Self {
            schema_version: inner.config.schema_version,
            role: inner.config.role,
            theme: inner.config.theme,
            autostart_enabled: inner.config.autostart_enabled,
            supervisor,
        }
    }
}

#[tauri::command]
fn get_desktop_snapshot(state: State<'_, DeviceState>) -> Result<DesktopSnapshot, String> {
    state.snapshot()
}

#[tauri::command]
fn controller_https_configure(
    state: State<'_, DeviceState>,
    lan_address: String,
    port: u16,
) -> Result<ControllerTlsSummary, String> {
    state.configure_https(&lan_address, port)
}

#[tauri::command]
async fn controller_https_reconfigure(
    state: State<'_, DeviceState>,
    operator: State<'_, OperatorAuthState>,
    lan_address: String,
    port: u16,
) -> Result<ControllerTlsSummary, String> {
    let owner_exists = state.controller_owner_exists()?;
    if owner_exists {
        authorize_node_lifecycle(&state, &operator)
            .await
            .map_err(|error| match error.as_str() {
                "operator_authentication_required"
                | "operator_forbidden"
                | "operator_password_change_required" => {
                    "controller_https_reconfiguration_unauthorized".to_string()
                }
                _ => error,
            })?;
    }
    let summary = state.reconfigure_https(&lan_address, port, owner_exists)?;
    if owner_exists {
        operator
            .revoke_session_at(&summary.local_https_origin)
            .await
            .map_err(|_| "controller_endpoint_reconfigure_session_not_revoked".to_string())?;
    }
    Ok(summary)
}

#[tauri::command]
fn controller_https_summary(state: State<'_, DeviceState>) -> Result<ControllerTlsSummary, String> {
    state.controller_https_summary()
}

#[tauri::command]
async fn controller_trust_probe(
    trust: State<'_, ControllerTrustState>,
    endpoint: String,
) -> Result<TrustProbeSummary, String> {
    let trust = trust.inner().clone();
    tauri::async_runtime::spawn_blocking(move || trust.probe(&endpoint))
        .await
        .map_err(|_| "controller_trust_probe_failed".to_string())?
        .map_err(str::to_string)
}

#[tauri::command]
async fn controller_trust_confirm(
    state: State<'_, DeviceState>,
    trust: State<'_, ControllerTrustState>,
    probe_id: String,
) -> Result<TrustedControllerSummary, String> {
    let trust = trust.inner().clone();
    let trust_path = state.trust_path();
    tauri::async_runtime::spawn_blocking(move || trust.confirm(&probe_id, &trust_path))
        .await
        .map_err(|_| "controller_trust_probe_failed".to_string())?
        .map_err(str::to_string)
}

#[tauri::command]
fn controller_trust_summary(
    state: State<'_, DeviceState>,
    endpoint: Option<String>,
) -> Result<TrustedControllerSummary, String> {
    controller_trust::summary(&state.trust_path(), endpoint.as_deref()).map_err(str::to_string)
}

#[tauri::command]
fn provision_role(
    state: State<'_, DeviceState>,
    role: ProvisionedRole,
) -> Result<DesktopSnapshot, String> {
    let snapshot = state.provision(role)?;
    if let Err(error) = enable_autostart() {
        state.decommission()?;
        return Err(error);
    }
    Ok(snapshot)
}

#[tauri::command]
fn reset_ui_preferences(state: State<'_, DeviceState>) -> Result<DesktopSnapshot, String> {
    state.reset_ui_preferences()
}

#[tauri::command]
async fn decommission_device(
    state: State<'_, DeviceState>,
    operator: State<'_, OperatorAuthState>,
    confirmation: String,
) -> Result<DesktopSnapshot, String> {
    if confirmation != "RESET THIS DEVICE" {
        return Err("decommission_confirmation_required".to_string());
    }
    authorize_node_lifecycle(&state, &operator).await?;
    operator.logout().await;
    disable_autostart()?;
    match state.decommission() {
        Ok(snapshot) => Ok(snapshot),
        Err(error) => {
            let _ = enable_autostart();
            Err(error)
        }
    }
}

#[tauri::command]
async fn request_quit(
    app: AppHandle,
    state: State<'_, DeviceState>,
    operator: State<'_, OperatorAuthState>,
) -> Result<(), String> {
    authorize_node_lifecycle(&state, &operator).await?;
    operator.logout().await;
    state.stop()?;
    app.exit(0);
    Ok(())
}

#[tauri::command]
async fn request_restart(
    state: State<'_, DeviceState>,
    operator: State<'_, OperatorAuthState>,
) -> Result<(), String> {
    authorize_node_lifecycle(&state, &operator).await?;
    operator.logout().await;
    state.restart()
}

async fn authorize_node_lifecycle(
    device: &DeviceState,
    operator: &OperatorAuthState,
) -> Result<(), String> {
    match device.role()? {
        Some(ProvisionedRole::Controller) => operator.authorize_node_lifecycle(true).await,
        Some(ProvisionedRole::Worker) => operator.authorize_node_lifecycle(false).await,
        Some(ProvisionedRole::Console) | None => Ok(()),
    }
}

#[tauri::command]
async fn operator_login(
    device: State<'_, DeviceState>,
    operator: State<'_, OperatorAuthState>,
    api_url: String,
    username: String,
    password: String,
) -> Result<OperatorIdentity, String> {
    let password = Zeroizing::new(password);
    let (endpoint, root_certificate) = if device.role()? == Some(ProvisionedRole::Controller) {
        (
            device.local_controller_url()?,
            device.controller_tls_root()?,
        )
    } else {
        (api_url.clone(), device.trusted_root_for_endpoint(&api_url)?)
    };
    operator
        .login(&endpoint, &root_certificate, &username, password.as_str())
        .await
}

#[tauri::command]
async fn operator_bootstrap_owner(
    device: State<'_, DeviceState>,
    operator: State<'_, OperatorAuthState>,
    username: String,
    password: String,
) -> Result<OperatorIdentity, String> {
    let password = Zeroizing::new(password);
    let _endpoint = device.bootstrap_owner(&username, password.as_str())?;
    let endpoint = device.local_controller_url()?;
    let root_certificate = device.controller_tls_root()?;
    operator
        .login(&endpoint, &root_certificate, &username, password.as_str())
        .await
}

#[tauri::command]
async fn operator_current(
    operator: State<'_, OperatorAuthState>,
) -> Result<Option<OperatorIdentity>, String> {
    operator.current().await
}

#[tauri::command]
async fn operator_logout(operator: State<'_, OperatorAuthState>) -> Result<(), String> {
    operator.logout().await;
    Ok(())
}

#[tauri::command]
fn operator_lock(operator: State<'_, OperatorAuthState>) {
    operator.lock_session();
}

#[tauri::command]
async fn operator_list_users(
    operator: State<'_, OperatorAuthState>,
) -> Result<Vec<OperatorUser>, String> {
    operator.list_users().await
}

#[tauri::command]
async fn operator_create_user(
    operator: State<'_, OperatorAuthState>,
    username: String,
    role: String,
) -> Result<CreatedOperatorUser, String> {
    operator.create_user(&username, &role).await
}

#[tauri::command]
async fn operator_update_user(
    operator: State<'_, OperatorAuthState>,
    user_id: String,
    role: Option<String>,
    enabled: Option<bool>,
) -> Result<OperatorUser, String> {
    operator
        .update_user(&user_id, role.as_deref(), enabled)
        .await
}

#[tauri::command]
async fn operator_change_password(
    operator: State<'_, OperatorAuthState>,
    new_password: String,
) -> Result<OperatorIdentity, String> {
    let new_password = Zeroizing::new(new_password);
    operator.change_password(new_password.as_str()).await
}

#[tauri::command]
async fn takeover_local_worker(
    device: State<'_, DeviceState>,
    operator: State<'_, OperatorAuthState>,
) -> Result<(), String> {
    operator.authorize_node_lifecycle(false).await?;
    let _operation = device.begin_worker_operation()?;
    let context = device.worker_cutover_context(false)?;
    let mut backend = DeviceWorkerCutover {
        device: device.inner().clone(),
        operator: operator.inner(),
        helper: context.helper,
        binding: context.binding,
    };
    worker_cutover::takeover(&mut backend).await
}

#[tauri::command]
async fn rollback_local_worker_to_legacy(
    device: State<'_, DeviceState>,
    operator: State<'_, OperatorAuthState>,
) -> Result<(), String> {
    operator.authorize_node_lifecycle(false).await?;
    let _operation = device.begin_worker_operation()?;
    let context = device.worker_cutover_context(true)?;
    let mut backend = DeviceWorkerCutover {
        device: device.inner().clone(),
        operator: operator.inner(),
        helper: context.helper,
        binding: context.binding,
    };
    worker_cutover::rollback_to_legacy(&mut backend).await
}

#[tauri::command]
async fn local_worker_drain_status(
    device: State<'_, DeviceState>,
    operator: State<'_, OperatorAuthState>,
) -> Result<WorkerDrainStatus, String> {
    let worker_id = device.local_worker_id()?;
    operator.local_worker_drain_status(&worker_id).await
}

fn show_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        run_window_reopen(
            window.is_visible().unwrap_or(false),
            || native_session_lock(app),
            || {
                let _ = window.show();
                let _ = window.unminimize();
                let _ = window.set_focus();
            },
        );
    }
}

fn install_tray(app: &tauri::App) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, "open", "Open Threads Desktop", true, None::<&str>)?;
    let separator = PredefinedMenuItem::separator(app)?;
    let quit = MenuItem::with_id(app, "quit", "Quit…", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &separator, &quit])?;

    TrayIconBuilder::new()
        .icon(
            app.default_window_icon()
                .cloned()
                .expect("Threads Desktop must have a bundle icon"),
        )
        .menu(&menu)
        .tooltip("Threads Desktop")
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id().as_ref() {
            "open" => show_main_window(app),
            "quit" => {
                let _ = app.emit(QUIT_EVENT, ());
            }
            _ => {}
        })
        .build(app)?;
    Ok(())
}

fn config_path(app: &tauri::App) -> tauri::Result<PathBuf> {
    Ok(app.path().app_config_dir()?.join("device-config.json"))
}

fn controller_root(app: &tauri::App) -> tauri::Result<PathBuf> {
    Ok(app.path().app_local_data_dir()?.join("Controller"))
}

fn runtime_root(app: &tauri::App) -> tauri::Result<PathBuf> {
    if let Some(configured) = std::env::var_os("THREADS_DESKTOP_RUNTIME_DIR") {
        return Ok(PathBuf::from(configured));
    }
    Ok(app.path().resource_dir()?.join("runtime"))
}

fn worker_task_helper_path(app: &tauri::App) -> Option<PathBuf> {
    worker_task_helper_from_resolution(
        app.path()
            .resolve(
                worker_host::TASK_HELPER_NAME,
                tauri::path::BaseDirectory::Resource,
            )
            .ok(),
    )
}

fn worker_task_helper_from_resolution(resolved: Option<PathBuf>) -> Option<PathBuf> {
    let helper = resolved?;
    helper.is_file().then_some(helper)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    #[cfg(windows)]
    let mut startup_gate =
        startup_gate::StartupGate::enter().expect("failed to enter Threads Desktop startup gate");

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show_main_window(app);
        }))
        .invoke_handler(tauri::generate_handler![
            get_desktop_snapshot,
            controller_https_configure,
            controller_https_reconfigure,
            controller_https_summary,
            controller_trust_probe,
            controller_trust_confirm,
            controller_trust_summary,
            provision_role,
            reset_ui_preferences,
            decommission_device,
            request_quit,
            request_restart,
            operator_login,
            operator_bootstrap_owner,
            operator_current,
            operator_logout,
            operator_lock,
            operator_list_users,
            operator_create_user,
            operator_update_user,
            operator_change_password,
            takeover_local_worker,
            rollback_local_worker_to_legacy,
            local_worker_drain_status
        ])
        .setup(|app| {
            let path = config_path(app)?;
            let data_root = controller_root(app)?;
            let runtime = runtime_root(app)?;
            let state = DeviceState::load_with_worker_task_helper(
                path,
                data_root,
                runtime,
                worker_task_helper_path(app),
            )
            .map_err(std::io::Error::other)?;
            let should_autostart = state
                .snapshot()
                .map(|snapshot| snapshot.autostart_enabled)
                .unwrap_or(false);
            app.manage(state);
            app.manage(OperatorAuthState::default());
            app.manage(ControllerTrustState::default());
            if should_autostart && enable_autostart().is_err() {
                app.state::<DeviceState>()
                    .mark_autostart_unavailable()
                    .map_err(std::io::Error::other)?;
            }
            install_tray(app)?;
            #[cfg(windows)]
            if let Some(window) = app.get_webview_window("main") {
                windows_session_lock::register(&window, app.handle().clone())
                    .map_err(std::io::Error::other)?;
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                native_session_lock(window.app_handle());
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("failed to start Threads Desktop");

    // `build` finishes plugin setup before the startup owner releases waiting
    // launches into the official single-instance plugin.
    #[cfg(windows)]
    startup_gate
        .signal_ready()
        .expect("failed to signal Threads Desktop startup readiness");

    // Keep the named-object handles alive for the lifetime of the event loop.
    app.run(|_app_handle, _event| {});
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn worker_task_helper_is_resolved_from_the_bundled_resource_directory() {
        let config: serde_json::Value =
            serde_json::from_str(include_str!("../tauri.conf.json")).expect("Tauri config");
        assert_eq!(
            config["bundle"]["resources"]
                ["../../../packaging/windows_worker/Manage-ThreadsWorkerTask.ps1"],
            worker_host::TASK_HELPER_NAME
        );

        let resource_dir = tempfile::tempdir().expect("installed resource directory");
        assert_eq!(worker_task_helper_from_resolution(None), None);
        let helper = resource_dir.path().join(worker_host::TASK_HELPER_NAME);
        fs::write(&helper, b"bundled task helper").expect("stage task helper resource");
        assert_eq!(
            worker_task_helper_from_resolution(Some(helper.clone())),
            Some(helper),
        );
    }

    #[test]
    fn worker_cutover_and_rollback_share_a_native_single_flight_guard() {
        let directory = tempfile::tempdir().expect("temporary device directory");
        let device = DeviceState::load(
            directory.path().join("device-config.json"),
            directory.path().join("Controller"),
            directory.path().join("runtime"),
        )
        .expect("load device state");

        let first = device
            .begin_worker_operation()
            .expect("first worker lifecycle operation acquires guard");
        assert!(matches!(
            device.begin_worker_operation(),
            Err(error) if error == "worker_cutover_operation_in_progress"
        ));
        drop(first);
        assert!(device.begin_worker_operation().is_ok());
    }

    #[test]
    fn config_round_trips_and_rejects_incompatible_schema() {
        let directory = tempfile::tempdir().expect("temporary config directory");
        let path = directory.path().join("device-config.json");
        let config = DeviceConfig {
            role: Some(ProvisionedRole::Controller),
            autostart_enabled: true,
            ..DeviceConfig::default()
        };
        config.save(&path).expect("save config");

        let loaded = DeviceConfig::load(&path).expect("load config");
        assert_eq!(loaded.role, Some(ProvisionedRole::Controller));
        assert!(loaded.autostart_enabled);

        let incompatible = serde_json::json!({
            "schema_version": CONFIG_SCHEMA_VERSION + 1,
            "role": "CONTROLLER",
            "theme": "SYSTEM",
            "autostart_enabled": true
        });
        fs::write(&path, incompatible.to_string()).expect("write future schema");
        assert_eq!(
            DeviceConfig::load(&path).unwrap_err(),
            "config_invalid_or_incompatible"
        );
    }

    #[test]
    fn invalid_role_and_unknown_config_fields_fail_closed() {
        let directory = tempfile::tempdir().expect("temporary config directory");
        let path = directory.path().join("device-config.json");
        fs::write(
            &path,
            r#"{"schema_version":1,"role":"ROOT","theme":"SYSTEM","autostart_enabled":false}"#,
        )
        .expect("write invalid role");
        assert!(DeviceConfig::load(&path).is_err());

        fs::write(
            &path,
            r#"{"schema_version":1,"role":null,"theme":"SYSTEM","autostart_enabled":false,"secret":"never allowed"}"#,
        )
        .expect("write unknown field");
        assert!(DeviceConfig::load(&path).is_err());
    }

    #[test]
    fn device_config_has_no_secret_or_business_state_fields() {
        let serialized = serde_json::to_value(DeviceConfig::default()).expect("serialize config");
        let object = serialized.as_object().expect("object config");
        assert_eq!(object.len(), 4);
        assert!(!object.contains_key("token"));
        assert!(!object.contains_key("database_url"));
        assert!(!object.contains_key("command"));
    }

    #[test]
    fn console_role_is_never_given_a_local_supervisor() {
        let inner = DeviceStateInner {
            config: DeviceConfig {
                role: Some(ProvisionedRole::Console),
                ..DeviceConfig::default()
            },
            supervisor: Supervisor::default(),
        };
        let snapshot = DesktopSnapshot::from(&inner);
        assert_eq!(snapshot.supervisor.state, "not_applicable");
        assert_eq!(snapshot.supervisor.process_id, None);
    }

    #[test]
    fn provisioning_persists_one_role_and_rejects_an_ordinary_role_switch() {
        let directory = tempfile::tempdir().expect("temporary config directory");
        let path = directory.path().join("device-config.json");
        let state = DeviceState::load(
            path.clone(),
            directory.path().join("Controller"),
            directory.path().join("runtime"),
        )
        .expect("load unprovisioned device");

        let provisioned = state
            .provision(ProvisionedRole::Console)
            .expect("provision Console");
        assert_eq!(provisioned.role, Some(ProvisionedRole::Console));
        assert_eq!(provisioned.supervisor.state, "not_applicable");
        assert_eq!(
            state.provision(ProvisionedRole::Worker).unwrap_err(),
            "device_already_provisioned"
        );

        let restored = DeviceState::load(
            path,
            directory.path().join("Controller"),
            directory.path().join("runtime"),
        )
        .expect("restore provisioned device");
        let snapshot = restored.snapshot().expect("read restored role");
        assert_eq!(snapshot.role, Some(ProvisionedRole::Console));
        assert_eq!(snapshot.supervisor.state, "not_applicable");
    }
}

#[cfg(test)]
mod operator_lock_boundary_tests {
    use super::*;
    use std::cell::RefCell;

    #[test]
    fn native_lock_detaches_before_emitting_the_notification() {
        let order = RefCell::new(Vec::new());

        run_native_lock_boundary(
            || order.borrow_mut().push("detach"),
            || order.borrow_mut().push("notify"),
        );

        assert_eq!(*order.borrow(), ["detach", "notify"]);
    }

    #[test]
    fn reopening_a_hidden_window_locks_before_showing_it() {
        let order = RefCell::new(Vec::new());

        run_window_reopen(
            false,
            || order.borrow_mut().push("lock"),
            || order.borrow_mut().push("show"),
        );

        assert_eq!(*order.borrow(), ["lock", "show"]);
    }

    #[test]
    fn showing_an_already_visible_window_does_not_lock_again() {
        let order = RefCell::new(Vec::new());

        run_window_reopen(
            true,
            || order.borrow_mut().push("lock"),
            || order.borrow_mut().push("show"),
        );

        assert_eq!(*order.borrow(), ["show"]);
    }
}
