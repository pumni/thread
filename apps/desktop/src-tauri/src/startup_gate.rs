#[cfg(windows)]
use std::time::Duration;

#[cfg(windows)]
const STARTUP_WAIT_TIMEOUT: Duration = Duration::from_secs(30);

#[cfg(any(windows, test))]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum WaitOutcome {
    Acquired,
    Abandoned,
    TimedOut,
    Failed,
}

#[cfg(any(windows, test))]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum GateDecision {
    ContinueAsSecondary,
    TakeOverAsPrimary,
    FailClosed,
}

#[cfg(any(windows, test))]
fn decide_after_wait(outcome: WaitOutcome, ready: bool) -> GateDecision {
    match outcome {
        WaitOutcome::Abandoned => GateDecision::TakeOverAsPrimary,
        WaitOutcome::Acquired if ready => GateDecision::ContinueAsSecondary,
        WaitOutcome::Acquired | WaitOutcome::TimedOut | WaitOutcome::Failed => {
            GateDecision::FailClosed
        }
    }
}

#[cfg(windows)]
mod windows {
    use super::{decide_after_wait, GateDecision, WaitOutcome, STARTUP_WAIT_TIMEOUT};
    use std::{io, ptr::null, time::Duration};
    use windows_sys::Win32::{
        Foundation::{
            CloseHandle, GetLastError, SetLastError, ERROR_ALREADY_EXISTS, HANDLE,
            WAIT_ABANDONED_0, WAIT_FAILED, WAIT_OBJECT_0, WAIT_TIMEOUT,
        },
        System::Threading::{
            CreateEventW, CreateMutexW, ReleaseMutex, ResetEvent, SetEvent, WaitForSingleObject,
        },
    };

    const STARTUP_MUTEX_NAME: &str = r"Local\com.pumni.threads-desktop.startup.v1";
    const STARTUP_READY_EVENT_NAME: &str = r"Local\com.pumni.threads-desktop.startup-ready.v1";

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    enum StartupRole {
        Primary,
        Secondary,
    }

    struct OwnedHandle(HANDLE);

    impl Drop for OwnedHandle {
        fn drop(&mut self) {
            if !self.0.is_null() {
                unsafe { CloseHandle(self.0) };
            }
        }
    }

    pub(crate) struct StartupGate {
        mutex: OwnedHandle,
        ready_event: OwnedHandle,
        role: StartupRole,
        owns_mutex: bool,
    }

    impl StartupGate {
        pub(crate) fn enter() -> io::Result<Self> {
            let mutex_name = to_wide(STARTUP_MUTEX_NAME);
            unsafe { SetLastError(0) };
            let mutex = unsafe { CreateMutexW(null(), 1, mutex_name.as_ptr()) };
            if mutex.is_null() {
                return Err(last_os_error("create startup mutex"));
            }
            let mutex = OwnedHandle(mutex);
            let mutex_already_existed = unsafe { GetLastError() } == ERROR_ALREADY_EXISTS;

            let event_name = to_wide(STARTUP_READY_EVENT_NAME);
            let ready_event = unsafe { CreateEventW(null(), 1, 0, event_name.as_ptr()) };
            if ready_event.is_null() {
                return Err(last_os_error("create startup readiness event"));
            }
            let ready_event = OwnedHandle(ready_event);

            if !mutex_already_existed {
                reset_ready_event(ready_event.0)?;
                return Ok(Self {
                    mutex,
                    ready_event,
                    role: StartupRole::Primary,
                    owns_mutex: true,
                });
            }

            let timeout_ms = duration_to_timeout_ms(STARTUP_WAIT_TIMEOUT);
            let wait_result = unsafe { WaitForSingleObject(mutex.0, timeout_ms) };
            let (outcome, owns_mutex) = match wait_result {
                WAIT_OBJECT_0 => (WaitOutcome::Acquired, true),
                WAIT_ABANDONED_0 => (WaitOutcome::Abandoned, true),
                WAIT_TIMEOUT => (WaitOutcome::TimedOut, false),
                WAIT_FAILED => (WaitOutcome::Failed, false),
                _ => (WaitOutcome::Failed, false),
            };
            let ready = if outcome == WaitOutcome::Acquired {
                match unsafe { WaitForSingleObject(ready_event.0, 0) } {
                    WAIT_OBJECT_0 => true,
                    WAIT_TIMEOUT => false,
                    WAIT_FAILED => return Err(last_os_error("probe startup readiness event")),
                    _ => return Err(io::Error::other("unexpected startup readiness wait result")),
                }
            } else {
                false
            };

            match decide_after_wait(outcome, ready) {
                GateDecision::ContinueAsSecondary => {
                    release_mutex(mutex.0)?;
                    Ok(Self {
                        mutex,
                        ready_event,
                        role: StartupRole::Secondary,
                        owns_mutex: false,
                    })
                }
                GateDecision::TakeOverAsPrimary => {
                    reset_ready_event(ready_event.0)?;
                    Ok(Self {
                        mutex,
                        ready_event,
                        role: StartupRole::Primary,
                        owns_mutex,
                    })
                }
                GateDecision::FailClosed => {
                    let error = match outcome {
                        WaitOutcome::TimedOut => io::Error::new(
                            io::ErrorKind::TimedOut,
                            "primary startup readiness timed out",
                        ),
                        WaitOutcome::Failed => last_os_error("wait for startup mutex"),
                        WaitOutcome::Acquired => {
                            io::Error::other("startup mutex released before readiness was signaled")
                        }
                        WaitOutcome::Abandoned => {
                            io::Error::other("abandoned startup mutex could not be taken over")
                        }
                    };
                    // If this thread acquired an unready mutex, keep ownership
                    // until process exit so another launch cannot race it.
                    let _held_until_process_exit = owns_mutex.then_some(mutex);
                    Err(error)
                }
            }
        }

        pub(crate) fn signal_ready(&mut self) -> io::Result<()> {
            if self.role == StartupRole::Secondary {
                return Ok(());
            }
            if !self.owns_mutex {
                return Err(io::Error::other("primary startup mutex is not owned"));
            }
            if unsafe { SetEvent(self.ready_event.0) } == 0 {
                return Err(last_os_error("signal startup readiness event"));
            }
            release_mutex(self.mutex.0)?;
            self.owns_mutex = false;
            Ok(())
        }
    }

    fn to_wide(value: &str) -> Vec<u16> {
        value.encode_utf16().chain(std::iter::once(0)).collect()
    }

    fn duration_to_timeout_ms(duration: Duration) -> u32 {
        u32::try_from(duration.as_millis()).unwrap_or(u32::MAX - 1)
    }

    fn reset_ready_event(event: HANDLE) -> io::Result<()> {
        if unsafe { ResetEvent(event) } == 0 {
            return Err(last_os_error("reset startup readiness event"));
        }
        Ok(())
    }

    fn release_mutex(mutex: HANDLE) -> io::Result<()> {
        if unsafe { ReleaseMutex(mutex) } == 0 {
            return Err(last_os_error("release startup mutex"));
        }
        Ok(())
    }

    fn last_os_error(operation: &str) -> io::Error {
        let error = io::Error::last_os_error();
        io::Error::new(error.kind(), format!("{operation}: {error}"))
    }
}

#[cfg(windows)]
pub(crate) use windows::StartupGate;

#[cfg(test)]
mod tests {
    use super::{decide_after_wait, GateDecision, WaitOutcome};

    #[test]
    fn released_mutex_with_readiness_allows_secondary_to_enter_tauri() {
        assert_eq!(
            decide_after_wait(WaitOutcome::Acquired, true),
            GateDecision::ContinueAsSecondary
        );
    }

    #[test]
    fn abandoned_mutex_allows_safe_startup_takeover() {
        assert_eq!(
            decide_after_wait(WaitOutcome::Abandoned, false),
            GateDecision::TakeOverAsPrimary
        );
        assert_eq!(
            decide_after_wait(WaitOutcome::Abandoned, true),
            GateDecision::TakeOverAsPrimary
        );
    }

    #[test]
    fn acquired_mutex_without_readiness_fails_closed() {
        assert_eq!(
            decide_after_wait(WaitOutcome::Acquired, false),
            GateDecision::FailClosed
        );
    }

    #[test]
    fn finite_wait_timeout_fails_closed() {
        assert_eq!(
            decide_after_wait(WaitOutcome::TimedOut, false),
            GateDecision::FailClosed
        );
    }

    #[test]
    fn native_wait_error_fails_closed() {
        assert_eq!(
            decide_after_wait(WaitOutcome::Failed, false),
            GateDecision::FailClosed
        );
    }
}
