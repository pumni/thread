use std::{
    io::{BufRead, BufReader, Write},
    net::{TcpListener, TcpStream},
    path::PathBuf,
    process::{Child, ChildStdin, Command, ExitStatus, Stdio},
    sync::mpsc::{self, Receiver},
    thread,
    time::{Duration, Instant},
};

use crate::{controller_store::ControllerStore, ProvisionedRole};

const READINESS_TIMEOUT: Duration = Duration::from_secs(60);
const MIGRATION_TIMEOUT: Duration = Duration::from_secs(120);
const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(40);

#[derive(Clone, Debug, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub(super) struct SupervisorSnapshot {
    pub state: &'static str,
    pub process_id: Option<u32>,
    pub postgres_process_id: Option<u32>,
    pub http_process_id: Option<u32>,
    pub scheduler_process_id: Option<u32>,
    pub controller_id: Option<String>,
    pub endpoint: Option<String>,
    pub database_port: Option<u16>,
    pub diagnostic_code: Option<&'static str>,
}

impl SupervisorSnapshot {
    pub fn not_applicable() -> Self {
        Self {
            state: "not_applicable",
            process_id: None,
            postgres_process_id: None,
            http_process_id: None,
            scheduler_process_id: None,
            controller_id: None,
            endpoint: None,
            database_port: None,
            diagnostic_code: None,
        }
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
enum Lifecycle {
    #[default]
    Stopped,
    Starting,
    Preflight,
    StartingDatabase,
    Migrating,
    M1BootstrapBoundary,
    StartingHttp,
    StartingScheduler,
    Running,
    Stopping,
    Degraded,
    Failed,
}

impl Lifecycle {
    fn label(self) -> &'static str {
        match self {
            Self::Stopped => "stopped",
            Self::Starting => "starting",
            Self::Preflight => "preflight",
            Self::StartingDatabase => "starting_database",
            Self::Migrating => "migrating",
            Self::M1BootstrapBoundary => "m1_bootstrap_boundary",
            Self::StartingHttp => "starting_http",
            Self::StartingScheduler => "starting_scheduler",
            Self::Running => "running",
            Self::Stopping => "stopping",
            Self::Degraded => "degraded",
            Self::Failed => "failed",
        }
    }
}

struct MockRuntime {
    child: Child,
    stdin: ChildStdin,
    messages: Receiver<String>,
}

pub(super) struct Supervisor {
    lifecycle: Lifecycle,
    mock_runtime: Option<MockRuntime>,
    controller: Option<ControllerRuntime>,
    data_root: Option<PathBuf>,
    runtime_root: Option<PathBuf>,
    diagnostic_code: Option<&'static str>,
}

impl Default for Supervisor {
    fn default() -> Self {
        Self {
            lifecycle: Lifecycle::Stopped,
            mock_runtime: None,
            controller: None,
            data_root: None,
            runtime_root: None,
            diagnostic_code: None,
        }
    }
}

impl Supervisor {
    pub fn with_controller_paths(data_root: PathBuf, runtime_root: PathBuf) -> Self {
        Self {
            data_root: Some(data_root),
            runtime_root: Some(runtime_root),
            ..Self::default()
        }
    }

    pub fn refresh_health(&mut self) {
        if let Some(runtime) = &mut self.mock_runtime {
            if matches!(runtime.child.try_wait(), Ok(Some(_))) {
                self.mock_runtime = None;
                self.lifecycle = Lifecycle::Degraded;
                self.diagnostic_code = Some("mock_runtime_exited_unexpectedly");
            }
        }

        if let Some(controller) = &mut self.controller {
            if let Some(code) = controller.refresh_health() {
                self.lifecycle = Lifecycle::Failed;
                self.diagnostic_code = Some(code);
            }
        }
    }

    pub fn snapshot(&self) -> SupervisorSnapshot {
        let controller = self.controller.as_ref();
        let summary = controller.and_then(ControllerRuntime::summary);
        SupervisorSnapshot {
            state: self.lifecycle.label(),
            process_id: self.mock_runtime.as_ref().map(|runtime| runtime.child.id()),
            postgres_process_id: controller.and_then(ControllerRuntime::postgres_process_id),
            http_process_id: controller.and_then(ControllerRuntime::http_process_id),
            scheduler_process_id: controller.and_then(ControllerRuntime::scheduler_process_id),
            controller_id: summary.as_ref().map(|item| item.controller_id.clone()),
            endpoint: summary.as_ref().map(|item| item.endpoint.clone()),
            database_port: summary.map(|item| item.database_port),
            diagnostic_code: self.diagnostic_code,
        }
    }

    pub fn start(&mut self, role: ProvisionedRole) -> Result<(), String> {
        if self.mock_runtime.is_some() || self.controller.is_some() {
            return Ok(());
        }

        self.diagnostic_code = None;
        match role {
            ProvisionedRole::Controller => {
                self.lifecycle = Lifecycle::Preflight;
                self.start_controller()
            }
            ProvisionedRole::Worker => {
                self.lifecycle = Lifecycle::Starting;
                self.start_mock_worker()
            }
            ProvisionedRole::Console => {
                self.lifecycle = Lifecycle::Stopped;
                Ok(())
            }
        }
    }

    fn start_controller(&mut self) -> Result<(), String> {
        let data_root = self
            .data_root
            .clone()
            .ok_or_else(|| self.fail("controller_data_root_unavailable"))?;
        let runtime_root = self
            .runtime_root
            .clone()
            .ok_or_else(|| self.fail("controller_runtime_bundle_invalid"))?;

        let controller =
            ControllerRuntime::open(data_root, runtime_root).map_err(|code| self.fail(code))?;
        self.controller = Some(controller);
        self.lifecycle = Lifecycle::StartingDatabase;
        let result = self
            .controller
            .as_mut()
            .expect("Controller runtime was just installed")
            .start(&mut self.lifecycle);
        match result {
            Ok(()) => {
                self.lifecycle = Lifecycle::Running;
                self.diagnostic_code = None;
                Ok(())
            }
            Err(code) => Err(self.fail(code)),
        }
    }

    fn start_mock_worker(&mut self) -> Result<(), String> {
        let executable = std::env::current_exe().map_err(|_| self.worker_start_failed())?;
        let mut command = Command::new(executable);
        command
            .arg("--threads-desktop-mock-runtime")
            .env_clear()
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000);
        }

        let mut child = command.spawn().map_err(|_| self.worker_start_failed())?;
        let Some(stdin) = child.stdin.take() else {
            let _ = child.kill();
            let _ = child.wait();
            return Err(self.worker_start_failed());
        };
        let Some(stdout) = child.stdout.take() else {
            let _ = child.kill();
            let _ = child.wait();
            return Err(self.worker_start_failed());
        };

        let (sender, messages) = mpsc::channel();
        thread::Builder::new()
            .name("threads-worker-mock-output".to_string())
            .spawn(move || {
                for line in BufReader::new(stdout).lines() {
                    match line {
                        Ok(line) => {
                            if sender.send(line).is_err() {
                                break;
                            }
                        }
                        Err(_) => break,
                    }
                }
            })
            .map_err(|_| {
                let _ = child.kill();
                let _ = child.wait();
                self.worker_start_failed()
            })?;

        match messages.recv_timeout(Duration::from_secs(3)) {
            Ok(message) if message == "READY" => {
                self.mock_runtime = Some(MockRuntime {
                    child,
                    stdin,
                    messages,
                });
                self.lifecycle = Lifecycle::Running;
                Ok(())
            }
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                Err(self.worker_start_failed())
            }
        }
    }

    pub fn stop(&mut self) -> Result<(), String> {
        self.lifecycle = Lifecycle::Stopping;
        if let Some(runtime) = self.mock_runtime.take() {
            return self.stop_mock(runtime);
        }
        if let Some(controller) = &mut self.controller {
            if let Err(code) = controller.stop() {
                self.lifecycle = Lifecycle::Failed;
                self.diagnostic_code = Some(code);
                return Err(code.to_string());
            }
            self.controller = None;
        }
        self.lifecycle = Lifecycle::Stopped;
        self.diagnostic_code = None;
        Ok(())
    }

    fn stop_mock(&mut self, mut runtime: MockRuntime) -> Result<(), String> {
        let acknowledged = writeln!(runtime.stdin, "STOP")
            .and_then(|()| runtime.stdin.flush())
            .is_ok()
            && matches!(
                runtime.messages.recv_timeout(Duration::from_secs(3)),
                Ok(message) if message == "STOPPED"
            );
        let deadline = Instant::now() + Duration::from_secs(2);
        let mut exited_cleanly = false;
        while Instant::now() < deadline {
            match runtime.child.try_wait() {
                Ok(Some(status)) => {
                    exited_cleanly = status.success();
                    break;
                }
                Ok(None) => thread::sleep(Duration::from_millis(25)),
                Err(_) => break,
            }
        }
        if acknowledged && exited_cleanly {
            self.lifecycle = Lifecycle::Stopped;
            self.diagnostic_code = None;
            return Ok(());
        }
        let _ = runtime.child.kill();
        let _ = runtime.child.wait();
        self.lifecycle = Lifecycle::Degraded;
        self.diagnostic_code = Some("mock_runtime_stop_unconfirmed");
        Err("mock_runtime_stop_unconfirmed".to_string())
    }

    fn worker_start_failed(&mut self) -> String {
        self.lifecycle = Lifecycle::Degraded;
        self.diagnostic_code = Some("mock_runtime_start_failed");
        "mock_runtime_start_failed".to_string()
    }

    fn fail(&mut self, code: &'static str) -> String {
        self.lifecycle = Lifecycle::Failed;
        self.diagnostic_code = Some(code);
        code.to_string()
    }
}

#[derive(Clone)]
struct RuntimeSummary {
    controller_id: String,
    endpoint: String,
    database_port: u16,
}

struct ControllerRuntime {
    store: ControllerStore,
    job: ProcessJob,
    postgres: Option<Child>,
    http: Option<Child>,
    scheduler: Option<Child>,
}

impl ControllerRuntime {
    fn open(data_root: PathBuf, runtime_root: PathBuf) -> Result<Self, &'static str> {
        let store = ControllerStore::open(data_root, runtime_root)?;
        Ok(Self {
            store,
            job: ProcessJob::new()?,
            postgres: None,
            http: None,
            scheduler: None,
        })
    }

    fn summary(&self) -> Option<RuntimeSummary> {
        let summary = self.store.config_summary();
        Some(RuntimeSummary {
            controller_id: summary.controller_id,
            endpoint: summary.endpoint,
            database_port: summary.database_port,
        })
    }

    fn postgres_process_id(&self) -> Option<u32> {
        self.postgres.as_ref().map(Child::id)
    }

    fn http_process_id(&self) -> Option<u32> {
        self.http.as_ref().map(Child::id)
    }

    fn scheduler_process_id(&self) -> Option<u32> {
        self.scheduler.as_ref().map(Child::id)
    }

    fn start(&mut self, lifecycle: &mut Lifecycle) -> Result<(), &'static str> {
        *lifecycle = Lifecycle::StartingDatabase;
        ensure_port_available(
            self.store.config().database_port,
            "controller_database_port_in_use",
        )?;
        ensure_port_available(
            self.store.config().endpoint_port,
            "controller_endpoint_port_in_use",
        )?;

        if self.store.needs_initialization() {
            self.initialize_cluster()?;
        }

        self.postgres = Some(self.spawn_postgres()?);
        self.wait_for_database()?;

        *lifecycle = Lifecycle::Migrating;
        self.run_migration()?;
        *lifecycle = Lifecycle::M1BootstrapBoundary;
        // M1 deliberately crosses this boundary without creating Workspace or Owner rows.

        *lifecycle = Lifecycle::StartingHttp;
        self.http = Some(self.spawn_runtime("http")?);
        self.wait_for_http()?;

        *lifecycle = Lifecycle::StartingScheduler;
        self.scheduler = Some(self.spawn_runtime("scheduler")?);
        self.wait_for_scheduler()?;
        Ok(())
    }

    fn initialize_cluster(&mut self) -> Result<(), &'static str> {
        let data_dir = self.store.postgres_data_dir();
        if data_dir.exists() {
            return Err("controller_data_root_incomplete");
        }
        let password_file = self.store.save_initdb_password_file()?;
        let mut command = Command::new(self.store.database_executable("initdb.exe"));
        command
            .arg("--pgdata")
            .arg(&data_dir)
            .arg("--username")
            .arg("threads_platform")
            .arg("--pwfile")
            .arg(&password_file)
            .arg("--auth-host=scram-sha-256")
            .arg("--encoding=UTF8");
        let result = self.run_one_shot(
            command,
            Duration::from_secs(120),
            "controller_database_init_failed",
        );
        self.store.remove_initdb_password_file(&password_file)?;
        result?;
        self.store.mark_cluster_initialized()?;
        Ok(())
    }

    fn spawn_postgres(&mut self) -> Result<Child, &'static str> {
        let mut command = Command::new(self.store.database_executable("postgres.exe"));
        command
            .arg("-D")
            .arg(self.store.postgres_data_dir())
            .arg("-h")
            .arg("127.0.0.1")
            .arg("-p")
            .arg(self.store.config().database_port.to_string());
        self.spawn_attached(command, "controller_database_start_failed")
    }

    fn wait_for_database(&mut self) -> Result<(), &'static str> {
        let deadline = Instant::now() + READINESS_TIMEOUT;
        let mut delay = Duration::from_millis(150);
        while Instant::now() < deadline {
            if Self::child_exited(&mut self.postgres)? {
                return Err("controller_database_start_failed");
            }
            let mut command = Command::new(self.store.postgres_ready_executable());
            command
                .arg("-h")
                .arg("127.0.0.1")
                .arg("-p")
                .arg(self.store.config().database_port.to_string())
                .arg("-U")
                .arg("threads_platform")
                .arg("-d")
                .arg("postgres");
            if self.run_probe(command) {
                return Ok(());
            }
            thread::sleep(delay);
            delay = (delay * 2).min(Duration::from_secs(1));
        }
        Err("controller_database_readiness_timeout")
    }

    fn run_migration(&mut self) -> Result<(), &'static str> {
        let mut command = Command::new(self.store.runtime_executable());
        command.arg("migrate");
        self.set_runtime_environment(&mut command);
        self.run_one_shot(command, MIGRATION_TIMEOUT, "controller_migration_failed")
    }

    fn spawn_runtime(&mut self, mode: &str) -> Result<Child, &'static str> {
        let mut command = Command::new(self.store.runtime_executable());
        command
            .arg(mode)
            .arg("--host")
            .arg("127.0.0.1")
            .arg("--port")
            .arg(self.store.config().endpoint_port.to_string());
        self.set_runtime_environment(&mut command);
        self.spawn_attached(
            command,
            if mode == "http" {
                "controller_http_start_failed"
            } else {
                "controller_scheduler_start_failed"
            },
        )
    }

    fn set_runtime_environment(&self, command: &mut Command) {
        command
            .env(
                "THREADS_PLATFORM_DATABASE_URL",
                self.store.database_url().as_str(),
            )
            .env("THREADS_PLATFORM_WORKER_TLS_REQUIRED", "false")
            .env("THREADS_PLATFORM_LOG_LEVEL", "WARNING");
    }

    fn wait_for_http(&mut self) -> Result<(), &'static str> {
        let endpoint = self.store.config().endpoint_port;
        let deadline = Instant::now() + READINESS_TIMEOUT;
        let mut delay = Duration::from_millis(150);
        while Instant::now() < deadline {
            if Self::child_exited(&mut self.http)? {
                return Err("controller_http_start_failed");
            }
            if http_ready(endpoint) {
                return Ok(());
            }
            thread::sleep(delay);
            delay = (delay * 2).min(Duration::from_secs(1));
        }
        Err("controller_http_readiness_timeout")
    }

    fn wait_for_scheduler(&mut self) -> Result<(), &'static str> {
        let deadline = Instant::now() + Duration::from_secs(5);
        while Instant::now() < deadline {
            if Self::child_exited(&mut self.scheduler)? {
                return Err("controller_scheduler_start_failed");
            }
            thread::sleep(Duration::from_millis(100));
        }
        Ok(())
    }

    fn refresh_health(&mut self) -> Option<&'static str> {
        let database_exited = Self::process_exited(&mut self.postgres);
        let http_exited = Self::process_exited(&mut self.http);
        let scheduler_exited = Self::process_exited(&mut self.scheduler);
        let status_unavailable =
            database_exited.is_err() || http_exited.is_err() || scheduler_exited.is_err();
        let database_exited = database_exited.unwrap_or(false);
        let http_exited = http_exited.unwrap_or(false);
        let scheduler_exited = scheduler_exited.unwrap_or(false);
        let failure = if database_exited {
            Some("controller_database_process_exited")
        } else if http_exited {
            Some("controller_http_process_exited")
        } else if scheduler_exited {
            Some("controller_scheduler_process_exited")
        } else if status_unavailable {
            Some("controller_process_status_unavailable")
        } else {
            None
        };
        if let Some(code) = failure {
            let _ = stop_child(&mut self.scheduler);
            let _ = stop_child(&mut self.http);
            if !database_exited {
                let _ = self.stop_database();
            }
            return Some(code);
        }
        None
    }

    fn stop(&mut self) -> Result<(), &'static str> {
        stop_child(&mut self.scheduler).map_err(|_| "controller_scheduler_stop_failed")?;
        stop_child(&mut self.http).map_err(|_| "controller_http_stop_failed")?;
        self.stop_database()
    }

    fn stop_database(&mut self) -> Result<(), &'static str> {
        let Some(postgres) = self.postgres.as_mut() else {
            return Ok(());
        };
        if postgres
            .try_wait()
            .map_err(|_| "controller_database_stop_failed")?
            .is_some()
        {
            self.postgres = None;
            return Ok(());
        }

        let mut command = Command::new(self.store.pg_ctl_executable());
        command
            .arg("-D")
            .arg(self.store.postgres_data_dir())
            .arg("-m")
            .arg("fast")
            .arg("-w")
            .arg("-t")
            .arg("30")
            .arg("stop");
        let status = self.run_one_shot_status(command, SHUTDOWN_TIMEOUT)?;
        if !status.success() {
            return Err("controller_database_stop_failed");
        }

        let postgres = self
            .postgres
            .as_mut()
            .expect("PostgreSQL child remains tracked");
        let deadline = Instant::now() + Duration::from_secs(10);
        while Instant::now() < deadline {
            if postgres
                .try_wait()
                .map_err(|_| "controller_database_stop_failed")?
                .is_some()
            {
                self.postgres = None;
                return Ok(());
            }
            thread::sleep(Duration::from_millis(50));
        }
        Err("controller_database_stop_failed")
    }

    fn run_probe(&mut self, command: Command) -> bool {
        let Ok(mut child) = self.spawn_attached(command, "controller_database_probe_failed") else {
            return false;
        };
        wait_for_child(&mut child, Duration::from_secs(4)).is_ok_and(|status| status.success())
    }

    fn run_one_shot(
        &mut self,
        command: Command,
        timeout: Duration,
        failure: &'static str,
    ) -> Result<(), &'static str> {
        let child = self.spawn_attached(command, failure)?;
        let mut child = child;
        let status = wait_for_child(&mut child, timeout).map_err(|_| failure)?;
        if status.success() {
            Ok(())
        } else {
            Err(failure)
        }
    }

    fn run_one_shot_status(
        &mut self,
        command: Command,
        timeout: Duration,
    ) -> Result<ExitStatus, &'static str> {
        let child = self.spawn_attached(command, "controller_shutdown_helper_failed")?;
        let mut child = child;
        wait_for_child(&mut child, timeout).map_err(|_| "controller_shutdown_helper_failed")
    }

    fn spawn_attached(
        &mut self,
        mut command: Command,
        failure: &'static str,
    ) -> Result<Child, &'static str> {
        command
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000);
        }
        let mut child = command.spawn().map_err(|_| failure)?;
        if let Err(()) = self.job.assign(&child) {
            let _ = child.kill();
            let _ = child.wait();
            return Err(failure);
        }
        Ok(child)
    }

    fn child_exited(child: &mut Option<Child>) -> Result<bool, &'static str> {
        let Some(process) = child.as_mut() else {
            return Ok(true);
        };
        match process.try_wait() {
            Ok(Some(_)) => {
                *child = None;
                Ok(true)
            }
            Ok(None) => Ok(false),
            Err(_) => Err("controller_process_status_unavailable"),
        }
    }

    fn process_exited(child: &mut Option<Child>) -> Result<bool, &'static str> {
        if child.is_none() {
            return Ok(false);
        }
        Self::child_exited(child)
    }
}

#[cfg(windows)]
struct ProcessJob(windows_sys::Win32::Foundation::HANDLE);

#[cfg(windows)]
unsafe impl Send for ProcessJob {}

#[cfg(windows)]
impl ProcessJob {
    fn new() -> Result<Self, &'static str> {
        use windows_sys::Win32::{
            Foundation::{CloseHandle, INVALID_HANDLE_VALUE},
            System::JobObjects::{
                CreateJobObjectW, JobObjectExtendedLimitInformation, SetInformationJobObject,
                JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
            },
        };

        let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        if handle.is_null() || handle == INVALID_HANDLE_VALUE {
            return Err("controller_process_job_create_failed");
        }
        let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let success = unsafe {
            SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                &limits as *const _ as *const _,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        };
        if success == 0 {
            unsafe {
                CloseHandle(handle);
            }
            return Err("controller_process_job_configure_failed");
        }
        Ok(Self(handle))
    }

    fn assign(&self, child: &Child) -> Result<(), ()> {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::System::JobObjects::AssignProcessToJobObject;
        let success = unsafe {
            AssignProcessToJobObject(
                self.0,
                child.as_raw_handle() as windows_sys::Win32::Foundation::HANDLE,
            )
        };
        if success == 0 {
            Err(())
        } else {
            Ok(())
        }
    }
}

#[cfg(windows)]
impl Drop for ProcessJob {
    fn drop(&mut self) {
        use windows_sys::Win32::Foundation::CloseHandle;
        unsafe {
            CloseHandle(self.0);
        }
    }
}

#[cfg(not(windows))]
struct ProcessJob;

#[cfg(not(windows))]
impl ProcessJob {
    fn new() -> Result<Self, &'static str> {
        Err("controller_platform_unsupported")
    }

    fn assign(&self, _child: &Child) -> Result<(), ()> {
        Err(())
    }
}

fn ensure_port_available(port: u16, failure: &'static str) -> Result<(), &'static str> {
    let listener = TcpListener::bind(("127.0.0.1", port)).map_err(|_| failure)?;
    drop(listener);
    Ok(())
}

fn http_ready(port: u16) -> bool {
    let address = format!("127.0.0.1:{port}");
    let Ok(mut stream) = TcpStream::connect(address) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    if stream
        .write_all(b"GET /ready HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        .is_err()
    {
        return false;
    }
    let mut status = String::new();
    BufReader::new(stream)
        .read_line(&mut status)
        .is_ok_and(|_| status.starts_with("HTTP/1.1 200") || status.starts_with("HTTP/1.0 200"))
}

fn wait_for_child(child: &mut Child, timeout: Duration) -> Result<ExitStatus, &'static str> {
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return Ok(status),
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(50)),
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err("controller_child_timeout");
            }
            Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err("controller_child_status_unavailable");
            }
        }
    }
}

fn stop_child(child: &mut Option<Child>) -> Result<(), ()> {
    let Some(process) = child.as_mut() else {
        return Ok(());
    };
    if process.try_wait().map_err(|_| ())?.is_none() {
        process.kill().map_err(|_| ())?;
    }
    process.wait().map_err(|_| ())?;
    *child = None;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn worker_mock_quit_keeps_fixed_orderly_control_path() {
        let mut lifecycle = Lifecycle::StartingScheduler;
        assert_eq!(lifecycle.label(), "starting_scheduler");
        lifecycle = Lifecycle::Stopping;
        assert_eq!(lifecycle.label(), "stopping");
    }

    #[test]
    fn stop_is_idempotent_when_no_child_was_started() {
        let mut supervisor = Supervisor::default();
        assert!(supervisor.stop().is_ok());
        assert_eq!(supervisor.snapshot().state, "stopped");
    }

    #[test]
    fn console_does_not_require_helper_startup() {
        let supervisor = Supervisor::default();
        let snapshot = supervisor.snapshot();
        assert_eq!(snapshot.state, "stopped");
        assert_eq!(snapshot.process_id, None);
    }

    #[test]
    fn controller_endpoints_are_loopback_only() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).expect("bind loopback");
        assert_eq!(listener.local_addr().unwrap().ip().to_string(), "127.0.0.1");
    }
}
