use std::{
    io::{BufRead, BufReader, Write},
    process::{Child, ChildStdin, Command, Stdio},
    sync::mpsc::{self, Receiver},
    thread,
    time::{Duration, Instant},
};

#[derive(Clone, Debug, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub(super) struct SupervisorSnapshot {
    pub state: &'static str,
    pub process_id: Option<u32>,
    pub diagnostic_code: Option<&'static str>,
}

impl SupervisorSnapshot {
    pub fn not_applicable() -> Self {
        Self {
            state: "not_applicable",
            process_id: None,
            diagnostic_code: None,
        }
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
enum Lifecycle {
    #[default]
    Stopped,
    Starting,
    Running,
    Stopping,
    Degraded,
}

impl Lifecycle {
    fn label(self) -> &'static str {
        match self {
            Self::Stopped => "stopped",
            Self::Starting => "starting",
            Self::Running => "running",
            Self::Stopping => "stopping",
            Self::Degraded => "degraded",
        }
    }

    fn begin_stop(&mut self) -> bool {
        match self {
            Self::Starting | Self::Running => {
                *self = Self::Stopping;
                true
            }
            Self::Stopping => true,
            Self::Stopped | Self::Degraded => false,
        }
    }
}

struct RuntimeProcess {
    child: Child,
    stdin: ChildStdin,
    messages: Receiver<String>,
}

#[derive(Default)]
pub(super) struct Supervisor {
    lifecycle: Lifecycle,
    runtime: Option<RuntimeProcess>,
    diagnostic_code: Option<&'static str>,
}

impl Supervisor {
    pub fn refresh_health(&mut self) {
        let child_exited = self
            .runtime
            .as_mut()
            .is_some_and(|runtime| matches!(runtime.child.try_wait(), Ok(Some(_))));
        if child_exited {
            self.runtime = None;
            self.lifecycle = Lifecycle::Degraded;
            self.diagnostic_code = Some("mock_runtime_exited_unexpectedly");
        }
    }

    pub fn snapshot(&self) -> SupervisorSnapshot {
        SupervisorSnapshot {
            state: self.lifecycle.label(),
            process_id: self.runtime.as_ref().map(|runtime| runtime.child.id()),
            diagnostic_code: self.diagnostic_code,
        }
    }

    pub fn start(&mut self) -> Result<(), String> {
        if self.runtime.is_some() {
            return Ok(());
        }

        self.lifecycle = Lifecycle::Starting;
        self.diagnostic_code = None;
        let executable = std::env::current_exe().map_err(|_| self.start_failed())?;
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

        let mut child = command.spawn().map_err(|_| self.start_failed())?;
        let Some(stdin) = child.stdin.take() else {
            let _ = child.kill();
            let _ = child.wait();
            return Err(self.start_failed());
        };
        let Some(stdout) = child.stdout.take() else {
            let _ = child.kill();
            let _ = child.wait();
            return Err(self.start_failed());
        };

        let (sender, messages) = mpsc::channel();
        thread::Builder::new()
            .name("threads-mock-runtime-output".to_string())
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
                self.start_failed()
            })?;

        match messages.recv_timeout(Duration::from_secs(3)) {
            Ok(message) if message == "READY" => {
                self.runtime = Some(RuntimeProcess {
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
                Err(self.start_failed())
            }
        }
    }

    pub fn stop(&mut self) -> Result<(), String> {
        let Some(mut runtime) = self.runtime.take() else {
            return Ok(());
        };
        self.lifecycle.begin_stop();

        let stop_acknowledged = writeln!(runtime.stdin, "STOP")
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

        if stop_acknowledged && exited_cleanly {
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

    fn start_failed(&mut self) -> String {
        self.lifecycle = Lifecycle::Degraded;
        self.diagnostic_code = Some("mock_runtime_start_failed");
        "mock_runtime_start_failed".to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn quit_during_starting_moves_to_stopping_without_waiting_for_ready() {
        let mut lifecycle = Lifecycle::Starting;
        assert!(lifecycle.begin_stop());
        assert_eq!(lifecycle, Lifecycle::Stopping);
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
}
