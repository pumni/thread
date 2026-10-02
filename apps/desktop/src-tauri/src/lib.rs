mod supervisor;

use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    sync::Mutex,
};

use serde::{Deserialize, Serialize};
use supervisor::{Supervisor, SupervisorSnapshot};
use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::TrayIconBuilder,
    AppHandle, Emitter, Manager, State,
};
use tauri_plugin_autostart::ManagerExt as AutostartManagerExt;

const CONFIG_SCHEMA_VERSION: u32 = 1;
const QUIT_EVENT: &str = "desktop://quit-requested";
const SESSION_LOCKED_EVENT: &str = "desktop://session-locked";

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

struct DeviceState {
    config_path: PathBuf,
    inner: Mutex<DeviceStateInner>,
}

impl DeviceState {
    fn load(config_path: PathBuf) -> Result<Self, String> {
        let config = DeviceConfig::load(&config_path)?;
        let mut supervisor = Supervisor::default();
        if matches!(
            config.role,
            Some(ProvisionedRole::Controller | ProvisionedRole::Worker)
        ) {
            let _ = supervisor.start();
        }

        Ok(Self {
            config_path,
            inner: Mutex::new(DeviceStateInner { config, supervisor }),
        })
    }

    fn snapshot(&self) -> Result<DesktopSnapshot, String> {
        let inner = self
            .inner
            .lock()
            .map_err(|_| "state_unavailable".to_string())?;
        Ok(DesktopSnapshot::from(&*inner))
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
            inner.supervisor.start()?;
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
fn provision_role(
    app: AppHandle,
    state: State<'_, DeviceState>,
    role: ProvisionedRole,
) -> Result<DesktopSnapshot, String> {
    let snapshot = state.provision(role)?;
    if app.autolaunch().enable().is_err() {
        let _ = state.mark_autostart_unavailable();
        return Err("autostart_enable_failed".to_string());
    }
    Ok(snapshot)
}

#[tauri::command]
fn reset_ui_preferences(state: State<'_, DeviceState>) -> Result<DesktopSnapshot, String> {
    state.reset_ui_preferences()
}

#[tauri::command]
fn decommission_device(
    app: AppHandle,
    state: State<'_, DeviceState>,
    confirmation: String,
) -> Result<DesktopSnapshot, String> {
    if confirmation != "RESET THIS DEVICE" {
        return Err("decommission_confirmation_required".to_string());
    }
    app.autolaunch()
        .disable()
        .map_err(|_| "autostart_disable_failed".to_string())?;
    match state.decommission() {
        Ok(snapshot) => Ok(snapshot),
        Err(error) => {
            let _ = app.autolaunch().enable();
            Err(error)
        }
    }
}

#[tauri::command]
fn request_quit(app: AppHandle, state: State<'_, DeviceState>) -> Result<(), String> {
    state.stop()?;
    app.exit(0);
    Ok(())
}

fn show_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show_main_window(app);
        }))
        .plugin(
            tauri_plugin_autostart::Builder::new()
                .args([] as [&str; 0])
                .build(),
        )
        .invoke_handler(tauri::generate_handler![
            get_desktop_snapshot,
            provision_role,
            reset_ui_preferences,
            decommission_device,
            request_quit
        ])
        .setup(|app| {
            let path = config_path(app)?;
            let state = DeviceState::load(path).map_err(std::io::Error::other)?;
            let should_autostart = state
                .snapshot()
                .map(|snapshot| snapshot.autostart_enabled)
                .unwrap_or(false);
            app.manage(state);
            if should_autostart && app.autolaunch().enable().is_err() {
                app.state::<DeviceState>()
                    .mark_autostart_unavailable()
                    .map_err(std::io::Error::other)?;
            }
            install_tray(app)?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
                let _ = window.emit(SESSION_LOCKED_EVENT, ());
            }
        })
        .run(tauri::generate_context!())
        .expect("failed to start Threads Desktop");
}

pub fn run_mock_runtime() {
    let stdin = std::io::stdin();
    let mut stdout = std::io::stdout().lock();
    if writeln!(stdout, "READY")
        .and_then(|()| stdout.flush())
        .is_err()
    {
        return;
    }

    for line in stdin.lines() {
        match line {
            Ok(command) if command == "STOP" => {
                let _ = writeln!(stdout, "STOPPED").and_then(|()| stdout.flush());
                return;
            }
            Ok(_) => {}
            Err(_) => return,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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
        let state = DeviceState::load(path.clone()).expect("load unprovisioned device");

        let provisioned = state
            .provision(ProvisionedRole::Console)
            .expect("provision Console");
        assert_eq!(provisioned.role, Some(ProvisionedRole::Console));
        assert_eq!(provisioned.supervisor.state, "not_applicable");
        assert_eq!(
            state.provision(ProvisionedRole::Worker).unwrap_err(),
            "device_already_provisioned"
        );

        let restored = DeviceState::load(path).expect("restore provisioned device");
        let snapshot = restored.snapshot().expect("read restored role");
        assert_eq!(snapshot.role, Some(ProvisionedRole::Console));
        assert_eq!(snapshot.supervisor.state, "not_applicable");
    }
}
