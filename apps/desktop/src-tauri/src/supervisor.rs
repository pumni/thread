use std::{
    io::{BufRead, BufReader, Read, Write},
    net::TcpListener,
    path::{Path, PathBuf},
    process::{Child, ChildStdin, Command, ExitStatus, Stdio},
    sync::{
        mpsc::{self, Receiver},
        Arc, Mutex,
    },
    thread,
    time::{Duration, Instant},
};

use crate::{
    controller_store::ControllerStore,
    controller_tls::{self, ControllerTlsSummary},
    ProvisionedRole,
};

const READINESS_TIMEOUT: Duration = Duration::from_secs(60);
const MIGRATION_TIMEOUT: Duration = Duration::from_secs(120);
const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(40);
const POSTGRES_STDERR_LIMIT: usize = 8 * 1024;

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
    HttpsSetupRequired,
    OwnerBootstrapRequired,
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
            Self::HttpsSetupRequired => "https_setup_required",
            Self::OwnerBootstrapRequired => "owner_bootstrap_required",
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

    pub fn bootstrap_owner(&mut self, username: &str, password: &str) -> Result<(), String> {
        let controller = self
            .controller
            .as_mut()
            .ok_or_else(|| "controller_runtime_unavailable".to_string())?;
        controller
            .bootstrap_owner(username, password, &mut self.lifecycle)
            .map_err(|code| {
                self.diagnostic_code = Some(code);
                code.to_string()
            })
    }

    pub fn configure_https(
        &mut self,
        lan_address: &str,
        port: u16,
    ) -> Result<ControllerTlsSummary, String> {
        let controller = self
            .controller
            .as_mut()
            .ok_or_else(|| "controller_runtime_unavailable".to_string())?;
        controller
            .configure_https(lan_address, port, &mut self.lifecycle)
            .map_err(|code| {
                self.diagnostic_code = Some(code);
                code.to_string()
            })
    }

    pub fn controller_owner_exists(&mut self) -> Result<bool, String> {
        self.controller
            .as_mut()
            .ok_or_else(|| "controller_runtime_unavailable".to_string())?
            .owner_exists()
            .map_err(str::to_string)
    }

    pub fn reconfigure_https(
        &mut self,
        lan_address: &str,
        port: u16,
        owner_authorized: bool,
    ) -> Result<ControllerTlsSummary, String> {
        self.refresh_health();
        if self.lifecycle == Lifecycle::Failed {
            return Err(self
                .diagnostic_code
                .unwrap_or("controller_runtime_unavailable")
                .to_string());
        }
        let controller = self
            .controller
            .as_mut()
            .ok_or_else(|| "controller_runtime_unavailable".to_string())?;
        match controller.reconfigure_https(lan_address, port, owner_authorized, &mut self.lifecycle)
        {
            Ok(summary) => Ok(summary),
            Err(code) => {
                if !is_non_mutating_reconfigure_error(code) {
                    self.lifecycle = Lifecycle::Failed;
                    self.diagnostic_code = Some(code);
                }
                Err(code.to_string())
            }
        }
    }

    pub fn controller_https_summary(&self) -> Result<ControllerTlsSummary, String> {
        self.controller
            .as_ref()
            .ok_or_else(|| "controller_runtime_unavailable".to_string())?
            .https_summary()
            .map_err(str::to_string)
    }

    pub fn local_controller_endpoint(&self) -> Result<String, &'static str> {
        self.controller
            .as_ref()
            .ok_or("controller_runtime_unavailable")?
            .store
            .local_endpoint()
    }

    pub fn controller_tls_root(&self) -> Result<Vec<u8>, &'static str> {
        self.controller
            .as_ref()
            .ok_or("controller_runtime_unavailable")?
            .tls_root()
    }

    pub fn restart(&mut self, role: ProvisionedRole) -> Result<(), String> {
        self.stop()?;
        self.start(role)
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
                if self
                    .controller
                    .as_ref()
                    .is_some_and(ControllerRuntime::is_serving)
                {
                    self.lifecycle = Lifecycle::Running;
                    self.diagnostic_code = None;
                }
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
    postgres_stderr: Option<Arc<Mutex<Vec<u8>>>>,
    postgres_stderr_finished: Option<Receiver<()>>,
    http: Option<Child>,
    scheduler: Option<Child>,
}

fn drain_bounded_stderr(mut stderr: impl Read, output: Arc<Mutex<Vec<u8>>>) {
    let mut chunk = [0_u8; 1024];
    while let Ok(count) = stderr.read(&mut chunk) {
        if count == 0 {
            break;
        }

        let Ok(mut output) = output.lock() else {
            break;
        };
        let overflow = output
            .len()
            .saturating_add(count)
            .saturating_sub(POSTGRES_STDERR_LIMIT);
        if overflow > 0 {
            output.drain(..overflow);
        }
        output.extend_from_slice(&chunk[..count]);
    }
}

fn classify_postgres_start_failure(output: &[u8]) -> &'static str {
    let output = String::from_utf8_lossy(output).to_ascii_lowercase();
    if output.contains("address already in use")
        || output.contains("only one usage of each socket address")
        || output.contains("wsaeaddrinuse")
    {
        "controller_database_port_in_use"
    } else if output.contains("invalid permissions")
        || output.contains("permission denied")
        || output.contains("access is denied")
    {
        "controller_data_root_unwritable"
    } else if output.contains("could not open configuration file")
        || output.contains("syntax error in file")
    {
        "controller_database_config_invalid"
    } else if output.contains("database files are incompatible")
        || output.contains("incompatible with this version")
        || output.contains("database system identifier differs")
    {
        "controller_data_root_corrupt"
    } else if output.contains("could not load library")
        || output.contains("specified module could not be found")
    {
        "controller_database_runtime_dependency_failed"
    } else if output.contains("postmaster.pid") && output.contains("already exists") {
        "controller_database_already_running"
    } else {
        "controller_database_process_exited"
    }
}

impl ControllerRuntime {
    fn open(data_root: PathBuf, runtime_root: PathBuf) -> Result<Self, &'static str> {
        let store = ControllerStore::open(data_root, runtime_root)?;
        Ok(Self {
            store,
            job: ProcessJob::new()?,
            postgres: None,
            postgres_stderr: None,
            postgres_stderr_finished: None,
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

    fn is_serving(&self) -> bool {
        self.http.is_some()
    }

    fn https_summary(&self) -> Result<ControllerTlsSummary, &'static str> {
        controller_tls::summary(
            self.store.data_root(),
            self.store.config().lan_address,
            self.store.config().endpoint_port,
        )
    }

    fn tls_root(&self) -> Result<Vec<u8>, &'static str> {
        let lan_address = self
            .store
            .config()
            .lan_address
            .ok_or("controller_https_configuration_required")?;
        controller_tls::root_certificate(self.store.data_root(), lan_address)
    }

    fn start(&mut self, lifecycle: &mut Lifecycle) -> Result<(), &'static str> {
        controller_tls::cleanup_leaf_key(self.store.data_root())?;
        *lifecycle = Lifecycle::StartingDatabase;
        ensure_port_available(
            self.store.config().database_port,
            "controller_database_port_in_use",
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
        let Some(lan_address) = self.store.config().lan_address else {
            *lifecycle = Lifecycle::HttpsSetupRequired;
            return Ok(());
        };
        self.validate_configured_endpoint(lan_address)?;
        self.ensure_tls_identity(lan_address)?;
        if self.owner_exists()? {
            self.start_https_services(lifecycle)?;
        } else {
            *lifecycle = Lifecycle::OwnerBootstrapRequired;
        }
        Ok(())
    }

    fn configure_https(
        &mut self,
        lan_address: &str,
        port: u16,
        lifecycle: &mut Lifecycle,
    ) -> Result<ControllerTlsSummary, &'static str> {
        let address = parse_lan_address(lan_address)?;
        if let Some(existing) = self.store.config().lan_address {
            if existing != address || self.store.config().endpoint_port != port {
                return Err("controller_https_reconfiguration_required");
            }
            if self.store.config().tls_identity_provisioned {
                controller_tls::validate_identity(self.store.data_root(), existing)?;
            } else {
                validate_requested_endpoint(
                    address,
                    port,
                    self.store.config().database_port,
                    None,
                )?;
                self.ensure_tls_identity(address)?;
                if self.owner_exists()? {
                    self.start_https_services(lifecycle)?;
                } else {
                    *lifecycle = Lifecycle::OwnerBootstrapRequired;
                }
            }
            return self.https_summary();
        }
        validate_requested_endpoint(address, port, self.store.config().database_port, None)?;
        self.store.configure_https(lan_address, port)?;
        self.ensure_tls_identity(address)?;
        if self.owner_exists()? {
            self.start_https_services(lifecycle)?;
        } else {
            *lifecycle = Lifecycle::OwnerBootstrapRequired;
        }
        self.https_summary()
    }

    fn reconfigure_https(
        &mut self,
        lan_address: &str,
        port: u16,
        owner_authorized: bool,
        lifecycle: &mut Lifecycle,
    ) -> Result<ControllerTlsSummary, &'static str> {
        let old_address = self
            .store
            .config()
            .lan_address
            .ok_or("controller_https_configuration_required")?;
        if !self.store.config().tls_identity_provisioned {
            return Err("controller_tls_identity_invalid");
        }
        controller_tls::validate_identity(self.store.data_root(), old_address)?;

        let new_address = parse_lan_address(lan_address)?;
        if new_address == old_address && port == self.store.config().endpoint_port {
            return self.https_summary();
        }
        let old_port = self.store.config().endpoint_port;
        let owns_old_listener = self.http.is_some();
        validate_requested_endpoint(
            new_address,
            port,
            self.store.config().database_port,
            owns_old_listener.then_some(old_port),
        )?;

        let owner_exists = self.owner_exists()?;
        if owner_exists && !owner_authorized {
            return Err("operator_authentication_required");
        }

        let candidate =
            controller_tls::prepare_leaf_reissue(self.store.data_root(), old_address, new_address)?;
        let expected_fingerprint = candidate.root_fingerprint.clone();
        let was_http_running = self.http.is_some();
        let was_scheduler_running = self.scheduler.is_some();
        if was_http_running != was_scheduler_running {
            return Err("controller_runtime_state_invalid");
        }

        if was_scheduler_running {
            *lifecycle = Lifecycle::Stopping;
            stop_child(&mut self.scheduler).map_err(|_| "controller_scheduler_stop_failed")?;
        }
        if was_http_running {
            if stop_child(&mut self.http).is_err() {
                if was_scheduler_running {
                    self.scheduler = Some(self.spawn_runtime("scheduler", None)?);
                    self.wait_for_scheduler()?;
                    *lifecycle = Lifecycle::Running;
                }
                return Err("controller_http_stop_failed");
            }
            if let Err(error) = controller_tls::cleanup_leaf_key(self.store.data_root()) {
                self.resume_old_https_services(lifecycle, was_http_running)?;
                return Err(error);
            }
        }

        let backup = match controller_tls::commit_leaf_reissue(self.store.data_root(), candidate) {
            Ok(backup) => backup,
            Err(error) => {
                self.resume_old_https_services(lifecycle, was_http_running)?;
                return Err(error);
            }
        };
        let actual_fingerprint = controller_tls::root_fingerprint(self.store.data_root());
        if !matches!(actual_fingerprint, Ok(ref value) if value == &expected_fingerprint) {
            controller_tls::restore_leaf_state(self.store.data_root(), backup)?;
            self.resume_old_https_services(lifecycle, was_http_running)?;
            return Err("controller_tls_identity_invalid");
        }
        if let Err(error) = self.store.configure_https(lan_address, port) {
            controller_tls::restore_leaf_state(self.store.data_root(), backup)?;
            self.resume_old_https_services(lifecycle, was_http_running)?;
            return Err(error);
        }

        if owner_exists {
            self.start_https_services(lifecycle)?;
        } else {
            *lifecycle = Lifecycle::OwnerBootstrapRequired;
        }
        self.https_summary()
    }

    fn resume_old_https_services(
        &mut self,
        lifecycle: &mut Lifecycle,
        was_http_running: bool,
    ) -> Result<(), &'static str> {
        if was_http_running {
            self.start_https_services(lifecycle)?;
        }
        Ok(())
    }

    fn validate_configured_endpoint(
        &self,
        lan_address: std::net::Ipv4Addr,
    ) -> Result<(), &'static str> {
        validate_requested_endpoint(
            lan_address,
            self.store.config().endpoint_port,
            self.store.config().database_port,
            None,
        )
    }

    fn ensure_tls_identity(&mut self, lan_address: std::net::Ipv4Addr) -> Result<(), &'static str> {
        if self.store.config().tls_identity_provisioned {
            controller_tls::load_validate_or_renew(self.store.data_root(), lan_address)?;
        } else {
            controller_tls::provision_initial(self.store.data_root(), lan_address)?;
            self.store.mark_tls_identity_provisioned()?;
        }
        Ok(())
    }

    fn owner_exists(&mut self) -> Result<bool, &'static str> {
        let mut command = Command::new(self.store.runtime_executable());
        command.arg("owner-status");
        self.set_runtime_environment(&mut command);
        command
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000);
        }
        let mut child = command
            .spawn()
            .map_err(|_| "controller_owner_status_unavailable")?;
        if self.job.assign(&child).is_err() {
            let _ = child.kill();
            let _ = child.wait();
            return Err("controller_process_job_assign_failed");
        }
        let Some(mut stdout) = child.stdout.take() else {
            let _ = child.kill();
            let _ = child.wait();
            return Err("controller_owner_status_unavailable");
        };
        let status = wait_for_child(&mut child, Duration::from_secs(10))
            .map_err(|_| "controller_owner_status_unavailable")?;
        if !status.success() {
            return Err("controller_owner_status_unavailable");
        }
        let mut output = String::new();
        stdout
            .read_to_string(&mut output)
            .map_err(|_| "controller_owner_status_unavailable")?;
        match output.trim() {
            "OWNER_PRESENT" => Ok(true),
            "OWNER_ABSENT" => Ok(false),
            _ => Err("controller_owner_status_unavailable"),
        }
    }

    fn start_https_services(&mut self, lifecycle: &mut Lifecycle) -> Result<(), &'static str> {
        if self.http.is_some() {
            *lifecycle = Lifecycle::Running;
            return Ok(());
        }
        let lan_address = self
            .store
            .config()
            .lan_address
            .ok_or("controller_https_configuration_required")?;
        self.validate_configured_endpoint(lan_address)?;
        if !self.store.config().tls_identity_provisioned {
            return Err("controller_tls_identity_invalid");
        }
        controller_tls::load_validate_or_renew(self.store.data_root(), lan_address)?;
        let (certfile, keyfile) =
            controller_tls::materialize_leaf_key(self.store.data_root(), lan_address)?;
        *lifecycle = Lifecycle::StartingHttp;
        let http = match self.spawn_runtime("http", Some((&certfile, &keyfile))) {
            Ok(http) => http,
            Err(error) => {
                controller_tls::cleanup_leaf_key(self.store.data_root())?;
                return Err(error);
            }
        };
        self.http = Some(http);
        if let Err(error) = self.wait_for_http() {
            stop_child(&mut self.http).map_err(|_| "controller_http_stop_failed")?;
            controller_tls::cleanup_leaf_key(self.store.data_root())?;
            return Err(error);
        }

        *lifecycle = Lifecycle::StartingScheduler;
        self.scheduler = Some(match self.spawn_runtime("scheduler", None) {
            Ok(scheduler) => scheduler,
            Err(error) => {
                stop_child(&mut self.http).map_err(|_| "controller_http_stop_failed")?;
                controller_tls::cleanup_leaf_key(self.store.data_root())?;
                return Err(error);
            }
        });
        if let Err(error) = self.wait_for_scheduler() {
            stop_child(&mut self.scheduler).map_err(|_| "controller_scheduler_stop_failed")?;
            stop_child(&mut self.http).map_err(|_| "controller_http_stop_failed")?;
            controller_tls::cleanup_leaf_key(self.store.data_root())?;
            return Err(error);
        }
        *lifecycle = Lifecycle::Running;
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
            .arg(self.store.config().database_port.to_string())
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::piped());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000);
        }

        let mut child = command
            .spawn()
            .map_err(|_| "controller_database_process_spawn_failed")?;
        if let Err(()) = self.job.assign(&child) {
            let _ = child.kill();
            let _ = child.wait();
            return Err("controller_process_job_assign_failed");
        }
        let Some(stderr) = child.stderr.take() else {
            let _ = child.kill();
            let _ = child.wait();
            return Err("controller_database_diagnostics_unavailable");
        };

        let output = Arc::new(Mutex::new(Vec::with_capacity(POSTGRES_STDERR_LIMIT)));
        let reader_output = Arc::clone(&output);
        let (finished_tx, finished_rx) = mpsc::channel();
        if thread::Builder::new()
            .name("controller-postgres-stderr".to_string())
            .spawn(move || {
                drain_bounded_stderr(stderr, reader_output);
                let _ = finished_tx.send(());
            })
            .is_err()
        {
            let _ = child.kill();
            let _ = child.wait();
            return Err("controller_database_diagnostics_unavailable");
        }

        self.postgres_stderr = Some(output);
        self.postgres_stderr_finished = Some(finished_rx);
        Ok(child)
    }

    fn wait_for_database(&mut self) -> Result<(), &'static str> {
        let deadline = Instant::now() + READINESS_TIMEOUT;
        let mut delay = Duration::from_millis(150);
        while Instant::now() < deadline {
            if Self::child_exited(&mut self.postgres)? {
                return Err(self.postgres_start_failure());
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

    fn postgres_start_failure(&self) -> &'static str {
        if let Some(finished) = &self.postgres_stderr_finished {
            let _ = finished.recv_timeout(Duration::from_millis(500));
        }
        if let Some(output) = &self.postgres_stderr {
            if let Ok(output) = output.lock() {
                return classify_postgres_start_failure(&output);
            }
        }
        "controller_database_process_exited"
    }

    fn run_migration(&mut self) -> Result<(), &'static str> {
        let mut command = Command::new(self.store.runtime_executable());
        command.arg("migrate");
        self.set_runtime_environment(&mut command);
        self.run_one_shot(command, MIGRATION_TIMEOUT, "controller_migration_failed")
    }

    fn bootstrap_owner(
        &mut self,
        username: &str,
        password: &str,
        lifecycle: &mut Lifecycle,
    ) -> Result<(), &'static str> {
        let lan_address = self
            .store
            .config()
            .lan_address
            .ok_or("controller_https_configuration_required")?;
        if !self.store.config().tls_identity_provisioned {
            return Err("controller_tls_identity_invalid");
        }
        controller_tls::load_validate_or_renew(self.store.data_root(), lan_address)?;
        let mut command = Command::new(self.store.runtime_executable());
        command
            .arg("bootstrap-owner")
            .arg("--username")
            .arg(username)
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        self.set_runtime_environment(&mut command);
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000);
        }
        let mut child = command
            .spawn()
            .map_err(|_| "operator_owner_bootstrap_failed")?;
        if self.job.assign(&child).is_err() {
            let _ = child.kill();
            let _ = child.wait();
            return Err("controller_process_job_assign_failed");
        }
        let Some(mut stdin) = child.stdin.take() else {
            let _ = child.kill();
            let _ = child.wait();
            return Err("operator_owner_bootstrap_failed");
        };
        if stdin
            .write_all(password.as_bytes())
            .and_then(|()| stdin.write_all(b"\n"))
            .is_err()
        {
            let _ = child.kill();
            let _ = child.wait();
            return Err("operator_owner_bootstrap_failed");
        }
        drop(stdin);
        let status = wait_for_child(&mut child, Duration::from_secs(60))
            .map_err(|_| "operator_owner_bootstrap_failed")?;
        if status.success() {
            if !self.owner_exists()? {
                return Err("operator_owner_bootstrap_failed");
            }
            self.start_https_services(lifecycle)
        } else {
            Err("operator_owner_bootstrap_failed")
        }
    }

    fn spawn_runtime(
        &mut self,
        mode: &str,
        tls_files: Option<(&Path, &Path)>,
    ) -> Result<Child, &'static str> {
        let mut command = Command::new(self.store.runtime_executable());
        command.arg(mode);
        if mode == "http" {
            let (certfile, keyfile) = tls_files.ok_or("controller_https_configuration_required")?;
            command
                .arg("--host")
                .arg("0.0.0.0")
                .arg("--port")
                .arg(self.store.config().endpoint_port.to_string())
                .arg("--ssl-certfile")
                .arg(certfile)
                .arg("--ssl-keyfile")
                .arg(keyfile);
        }
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
        apply_runtime_environment(command, self.store.database_url().as_str());
    }

    fn wait_for_http(&mut self) -> Result<(), &'static str> {
        let endpoint = self.store.config().endpoint_port;
        let lan_address = self
            .store
            .config()
            .lan_address
            .ok_or("controller_https_configuration_required")?;
        let root = controller_tls::root_certificate(self.store.data_root(), lan_address)?;
        let deadline = Instant::now() + READINESS_TIMEOUT;
        let mut delay = Duration::from_millis(150);
        while Instant::now() < deadline {
            if Self::child_exited(&mut self.http)? {
                return Err("controller_http_start_failed");
            }
            if controller_tls::readiness_probe(&root, endpoint) {
                return Ok(());
            }
            thread::sleep(delay);
            delay = (delay * 2).min(Duration::from_secs(1));
        }
        Err("controller_tls_readiness_timeout")
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
            let scheduler_stopped = stop_child(&mut self.scheduler).is_ok();
            let http_stopped = stop_child(&mut self.http).is_ok();
            let cleanup = if http_stopped {
                controller_tls::cleanup_leaf_key(self.store.data_root())
            } else {
                Err("controller_tls_leaf_cleanup_failed")
            };
            if !database_exited {
                let _ = self.stop_database();
            }
            if !scheduler_stopped {
                return Some("controller_scheduler_stop_failed");
            }
            if !http_stopped {
                return Some("controller_http_stop_failed");
            }
            if cleanup.is_err() {
                return Some("controller_tls_leaf_cleanup_failed");
            }
            return Some(code);
        }
        None
    }

    fn stop(&mut self) -> Result<(), &'static str> {
        stop_child(&mut self.scheduler).map_err(|_| "controller_scheduler_stop_failed")?;
        stop_child(&mut self.http).map_err(|_| "controller_http_stop_failed")?;
        controller_tls::cleanup_leaf_key(self.store.data_root())?;
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
            return Err("controller_process_job_assign_failed");
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

fn apply_runtime_environment(command: &mut Command, database_url: &str) {
    command
        .env("THREADS_PLATFORM_DATABASE_URL", database_url)
        .env("THREADS_PLATFORM_LOG_LEVEL", "WARNING");
}

fn ensure_port_available(port: u16, failure: &'static str) -> Result<(), &'static str> {
    let listener = TcpListener::bind(("127.0.0.1", port)).map_err(|_| failure)?;
    drop(listener);
    Ok(())
}

fn ensure_ipv4_wildcard_port_available(
    port: u16,
    failure: &'static str,
) -> Result<(), &'static str> {
    let listener =
        TcpListener::bind((std::net::Ipv4Addr::UNSPECIFIED, port)).map_err(|_| failure)?;
    drop(listener);
    Ok(())
}

fn parse_lan_address(value: &str) -> Result<std::net::Ipv4Addr, &'static str> {
    let address = value
        .parse::<std::net::Ipv4Addr>()
        .map_err(|_| "controller_lan_address_invalid")?;
    if address.is_loopback() || address.is_unspecified() || address.is_multicast() {
        return Err("controller_lan_address_invalid");
    }
    Ok(address)
}

fn is_non_mutating_reconfigure_error(code: &str) -> bool {
    matches!(
        code,
        "controller_lan_address_invalid"
            | "controller_lan_address_unavailable"
            | "controller_endpoint_port_invalid"
            | "controller_endpoint_port_in_use"
            | "controller_tls_leaf_issue_failed"
            | "operator_authentication_required"
    )
}

fn validate_requested_endpoint(
    lan_address: std::net::Ipv4Addr,
    port: u16,
    database_port: u16,
    owned_listener_port: Option<u16>,
) -> Result<(), &'static str> {
    if lan_address.is_loopback() || lan_address.is_unspecified() || lan_address.is_multicast() {
        return Err("controller_lan_address_invalid");
    }
    TcpListener::bind((lan_address, 0)).map_err(|_| "controller_lan_address_unavailable")?;
    if port == 0 {
        return Err("controller_endpoint_port_invalid");
    }
    if port == database_port {
        return Err("controller_endpoint_port_in_use");
    }
    if owned_listener_port != Some(port) {
        ensure_ipv4_wildcard_port_available(port, "controller_endpoint_port_in_use")?;
    }
    Ok(())
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

    #[cfg(windows)]
    fn runtime_with_provisioned_controller() -> (tempfile::TempDir, ControllerRuntime, u16) {
        use std::fs;

        let directory = tempfile::tempdir().expect("temporary Controller data root");
        let runtime_root = directory.path().join("runtime");
        for path in [
            runtime_root.join("threads-runtime"),
            runtime_root.join("postgresql").join("bin"),
        ] {
            fs::create_dir_all(path).expect("create runtime fixture directory");
        }
        for path in [
            runtime_root
                .join("threads-runtime")
                .join("threads-runtime.exe"),
            runtime_root
                .join("postgresql")
                .join("bin")
                .join("initdb.exe"),
            runtime_root
                .join("postgresql")
                .join("bin")
                .join("postgres.exe"),
            runtime_root
                .join("postgresql")
                .join("bin")
                .join("pg_ctl.exe"),
            runtime_root
                .join("postgresql")
                .join("bin")
                .join("pg_isready.exe"),
        ] {
            fs::write(path, []).expect("write runtime fixture executable");
        }

        let data_root = directory.path().join("controller");
        let mut store =
            ControllerStore::open(data_root, runtime_root).expect("open new Controller store");
        let old_port = loop {
            let listener = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0))
                .expect("reserve endpoint port");
            let port = listener.local_addr().expect("read endpoint port").port();
            if port != store.config().database_port {
                drop(listener);
                break port;
            }
        };
        let old_address = std::net::Ipv4Addr::new(192, 0, 2, 10);
        store
            .configure_https(&old_address.to_string(), old_port)
            .expect("persist initial endpoint");
        crate::controller_tls::provision_initial(store.data_root(), old_address)
            .expect("provision Controller TLS identity");
        store
            .mark_tls_identity_provisioned()
            .expect("persist TLS identity marker");
        let runtime = ControllerRuntime {
            store,
            job: ProcessJob::new().expect("create Controller process job"),
            postgres: None,
            postgres_stderr: None,
            postgres_stderr_finished: None,
            http: None,
            scheduler: None,
        };
        (directory, runtime, old_port)
    }

    #[cfg(windows)]
    fn reconfiguration_state(runtime: &ControllerRuntime) -> Vec<(String, Vec<u8>)> {
        [
            "controller.json",
            "tls/root-cert.der",
            "tls/root-key.dpapi",
            "tls/leaf-cert.der",
            "tls/leaf-key.dpapi",
            "tls/leaf-fullchain.pem",
        ]
        .into_iter()
        .map(|relative| {
            (
                relative.to_string(),
                std::fs::read(runtime.store.data_root().join(relative))
                    .expect("read persisted endpoint/TLS state"),
            )
        })
        .collect()
    }

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
    fn database_listener_remains_loopback_and_controller_port_uses_ipv4_wildcard() {
        let database = TcpListener::bind(("127.0.0.1", 0)).expect("bind loopback database");
        assert_eq!(database.local_addr().unwrap().ip().to_string(), "127.0.0.1");
        let endpoint = TcpListener::bind(("0.0.0.0", 0)).expect("bind wildcard endpoint");
        let port = endpoint.local_addr().unwrap().port();
        assert_eq!(
            ensure_ipv4_wildcard_port_available(port, "controller_endpoint_port_in_use"),
            Err("controller_endpoint_port_in_use")
        );
    }

    #[test]
    fn endpoint_preflight_distinguishes_unavailable_ip_and_external_port_collision() {
        assert_eq!(
            validate_requested_endpoint("192.0.2.254".parse().unwrap(), 54_321, 54_322, None,),
            Err("controller_lan_address_unavailable")
        );

        let route = std::net::UdpSocket::bind((std::net::Ipv4Addr::UNSPECIFIED, 0))
            .expect("bind route probe");
        route
            .connect((std::net::Ipv4Addr::new(192, 0, 2, 1), 443))
            .expect("select local interface");
        let address = match route.local_addr().unwrap().ip() {
            std::net::IpAddr::V4(address) => address,
            _ => panic!("route probe must select IPv4"),
        };
        let occupied = TcpListener::bind((std::net::Ipv4Addr::UNSPECIFIED, 0))
            .expect("reserve wildcard endpoint");
        let port = occupied.local_addr().unwrap().port();
        assert_eq!(
            validate_requested_endpoint(address, port, 1, None),
            Err("controller_endpoint_port_in_use")
        );
        assert!(validate_requested_endpoint(address, port, 1, Some(port)).is_ok());
    }

    #[cfg(windows)]
    #[test]
    fn occupied_endpoint_port_keeps_persisted_endpoint_and_tls_identity_unchanged() {
        let (_directory, mut runtime, old_port) = runtime_with_provisioned_controller();
        let before = reconfiguration_state(&runtime);
        let route = std::net::UdpSocket::bind((std::net::Ipv4Addr::UNSPECIFIED, 0))
            .expect("bind route probe");
        route
            .connect((std::net::Ipv4Addr::new(192, 0, 2, 1), 443))
            .expect("select local interface");
        let new_address = match route.local_addr().expect("read route address").ip() {
            std::net::IpAddr::V4(address) => address,
            _ => panic!("route probe must select IPv4"),
        };
        let occupied = loop {
            let listener = TcpListener::bind((std::net::Ipv4Addr::UNSPECIFIED, 0))
                .expect("reserve external endpoint port");
            let port = listener.local_addr().expect("read occupied port").port();
            if port != runtime.store.config().database_port && port != old_port {
                break listener;
            }
        };
        let occupied_port = occupied.local_addr().expect("read occupied port").port();
        let mut lifecycle = Lifecycle::Running;

        assert!(matches!(
            runtime.reconfigure_https(
                &new_address.to_string(),
                occupied_port,
                true,
                &mut lifecycle,
            ),
            Err("controller_endpoint_port_in_use")
        ));

        assert_eq!(lifecycle, Lifecycle::Running);
        assert_eq!(
            runtime.store.config().lan_address.unwrap().to_string(),
            "192.0.2.10"
        );
        assert_eq!(runtime.store.config().endpoint_port, old_port);
        assert_eq!(reconfiguration_state(&runtime), before);
    }

    #[cfg(windows)]
    #[test]
    fn unavailable_endpoint_ip_keeps_persisted_endpoint_and_tls_identity_unchanged() {
        let (_directory, mut runtime, old_port) = runtime_with_provisioned_controller();
        let before = reconfiguration_state(&runtime);
        let mut lifecycle = Lifecycle::Running;

        assert!(matches!(
            runtime.reconfigure_https(
                "192.0.2.254",
                old_port.wrapping_add(1).max(1),
                true,
                &mut lifecycle,
            ),
            Err("controller_lan_address_unavailable")
        ));

        assert_eq!(lifecycle, Lifecycle::Running);
        assert_eq!(
            runtime.store.config().lan_address.unwrap().to_string(),
            "192.0.2.10"
        );
        assert_eq!(runtime.store.config().endpoint_port, old_port);
        assert_eq!(reconfiguration_state(&runtime), before);
    }

    #[test]
    fn endpoint_preflight_rejects_non_lan_ipv4_addresses() {
        for value in ["127.0.0.1", "0.0.0.0", "224.0.0.1", "2001:db8::1"] {
            if let Ok(address) = value.parse() {
                assert_eq!(
                    parse_lan_address(value),
                    Err("controller_lan_address_invalid")
                );
                assert_eq!(
                    validate_requested_endpoint(address, 54_321, 54_322, None),
                    Err("controller_lan_address_invalid")
                );
            } else {
                assert_eq!(
                    parse_lan_address(value),
                    Err("controller_lan_address_invalid")
                );
            }
        }
    }

    #[test]
    fn endpoint_reconfiguration_preflight_errors_do_not_fail_the_running_lifecycle() {
        for error in [
            "controller_lan_address_invalid",
            "controller_lan_address_unavailable",
            "controller_endpoint_port_invalid",
            "controller_endpoint_port_in_use",
            "controller_tls_leaf_issue_failed",
            "operator_authentication_required",
        ] {
            assert!(is_non_mutating_reconfigure_error(error), "{error}");
        }
        assert!(is_non_mutating_reconfigure_error(
            "controller_tls_leaf_issue_failed"
        ));
        assert!(!is_non_mutating_reconfigure_error(
            "controller_tls_identity_invalid"
        ));
        assert!(!is_non_mutating_reconfigure_error(
            "controller_http_start_failed"
        ));
    }

    #[test]
    fn runtime_environment_passes_no_private_tls_key() {
        let mut command = Command::new("threads-runtime");
        apply_runtime_environment(
            &mut command,
            "postgresql+asyncpg://threads_platform@127.0.0.1:5432/threads_platform",
        );
        let variable_names = command
            .get_envs()
            .map(|(name, _)| name.to_string_lossy().into_owned())
            .collect::<std::collections::HashSet<_>>();

        assert_eq!(
            variable_names,
            std::collections::HashSet::from([
                "THREADS_PLATFORM_DATABASE_URL".to_string(),
                "THREADS_PLATFORM_LOG_LEVEL".to_string(),
            ])
        );
        assert!(!variable_names
            .iter()
            .any(|name| { name.contains("TLS") || name.contains("KEY") || name.contains("CERT") }));
    }

    #[test]
    fn postgres_startup_failure_diagnostics_are_fixed_and_redacted() {
        let output = b"FATAL: could not bind IPv4 address: Only one usage of each socket address (protocol/network address/port) is normally permitted.\nsecret-marker";
        assert_eq!(
            classify_postgres_start_failure(output),
            "controller_database_port_in_use"
        );
    }

    #[test]
    fn postgres_startup_failure_diagnostics_classify_corrupt_data() {
        let output = b"FATAL: database files are incompatible with server";
        assert_eq!(
            classify_postgres_start_failure(output),
            "controller_data_root_corrupt"
        );
    }
}
