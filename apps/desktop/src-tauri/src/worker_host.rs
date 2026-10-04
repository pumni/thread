#[cfg(windows)]
use std::io::Read;
#[cfg(windows)]
use std::os::windows::process::CommandExt;
#[cfg(windows)]
use std::process::Stdio;
use std::{
    env, fs,
    path::{Path, PathBuf},
    process::Command,
};

use serde::Deserialize;
use sha2::{Digest, Sha256};

pub(super) const TASK_HELPER_NAME: &str = "Manage-ThreadsWorkerTask.ps1";
const KEY_FILE_MAGIC: &[u8] = b"TPW-DPAPI-ED25519-1\0";
const KEY_CONTEXT: &[u8] = b"threads-platform-worker-key-v1\0";
const MAX_HOST_CONFIG_BYTES: u64 = 16_384;
const MAX_MANIFEST_BYTES: u64 = 16_384;

const WORKER_ENVIRONMENT_OVERRIDES: &[&str] = &[
    "THREADS_WORKER_ENROLLMENT_CODE",
    "THREADS_WORKER_CONTROL_PLANE_URL",
    "THREADS_WORKER_DATA_ROOT",
    "THREADS_WORKER_DISPLAY_NAME",
    "THREADS_WORKER_AGENT_VERSION",
    "THREADS_WORKER_MAX_CONCURRENT_JOBS",
    "THREADS_WORKER_MAX_BROWSER_SESSIONS",
    "THREADS_WORKER_FEED_BROWSE_ENABLED",
    "THREADS_WORKER_THREAD_OPEN_ENABLED",
    "THREADS_WORKER_PROFILE_OPEN_ENABLED",
    "THREADS_WORKER_MEDIA_LOCAL_UPLOAD_ENABLED",
];

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub(super) enum LegacyTaskState {
    NotRegistered,
    Invalid,
    Running,
    Ready,
    Disabled,
}

impl LegacyTaskState {
    pub(super) fn label(self) -> &'static str {
        match self {
            Self::NotRegistered => "NOT_REGISTERED",
            Self::Invalid => "INVALID",
            Self::Running => "RUNNING",
            Self::Ready => "READY",
            Self::Disabled => "DISABLED",
        }
    }
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "snake_case")]
pub(super) struct TaskInspection {
    pub state: LegacyTaskState,
    same_user: bool,
    #[serde(default)]
    pub diagnostic_code: Option<String>,
    executable_path: Option<String>,
    host_config_path: Option<String>,
    working_directory: Option<String>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum WorkerOwnership {
    TakeoverRequired,
    Desktop,
    Blocked,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum ProcessLockObservation {
    Held,
    NotHeld,
    Unavailable,
}

impl WorkerOwnership {
    pub(super) fn map_label(self) -> &'static str {
        match self {
            Self::TakeoverRequired => "TAKEOVER_REQUIRED",
            Self::Desktop => "DESKTOP",
            Self::Blocked => "BLOCKED",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct OwnershipDecision {
    pub ownership: WorkerOwnership,
    pub diagnostic_code: Option<&'static str>,
}

#[derive(Clone, Debug)]
pub(super) struct WorkerHostBinding {
    pub executable: PathBuf,
    pub host_config: PathBuf,
    pub data_root: PathBuf,
    pub worker_id: String,
    pub identity_marker_sha256: String,
    pub protected_key_sha256: String,
}

pub(super) struct IdentityFileGuard {
    #[cfg(windows)]
    _marker: fs::File,
    #[cfg(windows)]
    _protected_key: fs::File,
}

pub(super) fn classify_task(state: LegacyTaskState) -> OwnershipDecision {
    match state {
        LegacyTaskState::Running => OwnershipDecision {
            ownership: WorkerOwnership::TakeoverRequired,
            diagnostic_code: Some("worker_legacy_task_running"),
        },
        LegacyTaskState::Ready => OwnershipDecision {
            ownership: WorkerOwnership::TakeoverRequired,
            diagnostic_code: Some("worker_legacy_task_ready"),
        },
        LegacyTaskState::Disabled => OwnershipDecision {
            ownership: WorkerOwnership::Desktop,
            diagnostic_code: None,
        },
        LegacyTaskState::Invalid => OwnershipDecision {
            ownership: WorkerOwnership::Blocked,
            diagnostic_code: Some("worker_legacy_task_invalid"),
        },
        LegacyTaskState::NotRegistered => OwnershipDecision {
            ownership: WorkerOwnership::Blocked,
            diagnostic_code: Some("worker_legacy_task_not_registered"),
        },
    }
}

pub(super) fn inspect_legacy_task(helper_path: &Path) -> Result<TaskInspection, &'static str> {
    if helper_path.file_name().and_then(|name| name.to_str()) != Some(TASK_HELPER_NAME)
        || !helper_path.is_file()
    {
        return Err("worker_host_configuration_required");
    }

    #[cfg(windows)]
    {
        let output = task_helper_command(helper_path)
            .output()
            .map_err(|_| "worker_legacy_task_invalid")?;
        if !output.status.success() || output.stdout.len() > 65_536 {
            return Err("worker_legacy_task_invalid");
        }
        serde_json::from_slice(&output.stdout).map_err(|_| "worker_legacy_task_invalid")
    }
    #[cfg(not(windows))]
    {
        let _ = helper_path;
        Err("worker_platform_unsupported")
    }
}

#[cfg(windows)]
fn task_helper_command(helper_path: &Path) -> Command {
    let mut command = Command::new("powershell.exe");
    command
        .arg("-NoProfile")
        .arg("-NonInteractive")
        .arg("-File")
        .arg(helper_path)
        .arg("InspectJson")
        .creation_flags(0x08000000)
        .stdin(Stdio::null())
        .stderr(Stdio::null());
    command
}

pub(super) fn validate_task_binding(
    inspection: &TaskInspection,
) -> Result<WorkerHostBinding, &'static str> {
    validate_task_binding_with(inspection, |ciphertext, entropy| {
        crate::windows_crypto::validate_worker_device_key(ciphertext, entropy)
    })
}

fn validate_task_binding_with(
    inspection: &TaskInspection,
    validate_key: impl FnOnce(&[u8], &[u8]) -> Result<(), crate::windows_crypto::WorkerKeyError>,
) -> Result<WorkerHostBinding, &'static str> {
    if matches!(
        inspection.state,
        LegacyTaskState::Invalid | LegacyTaskState::NotRegistered
    ) || !inspection.same_user
    {
        return Err("worker_legacy_task_invalid");
    }

    let executable = absolute_path(
        inspection
            .executable_path
            .as_deref()
            .ok_or("worker_legacy_task_invalid")?,
    )
    .ok_or("worker_legacy_task_invalid")?;
    let host_config = absolute_path(
        inspection
            .host_config_path
            .as_deref()
            .ok_or("worker_legacy_task_invalid")?,
    )
    .ok_or("worker_legacy_task_invalid")?;
    let working_directory = absolute_path(
        inspection
            .working_directory
            .as_deref()
            .ok_or("worker_legacy_task_invalid")?,
    )
    .ok_or("worker_legacy_task_invalid")?;
    let package_root = executable.parent().ok_or("worker_package_invalid")?;
    if executable.file_name().and_then(|name| name.to_str()) != Some("threads-worker.exe")
        || package_root != working_directory
    {
        return Err("worker_legacy_task_invalid");
    }
    validate_worker_package(package_root, &executable)?;
    let data_root = validate_host_config(&host_config)?;
    let identity = validate_identity_files(&data_root, validate_key)?;

    Ok(WorkerHostBinding {
        executable,
        host_config,
        data_root,
        worker_id: identity.worker_id,
        identity_marker_sha256: identity.marker_sha256,
        protected_key_sha256: identity.key_sha256,
    })
}

fn absolute_path(value: &str) -> Option<PathBuf> {
    let path = PathBuf::from(value);
    path.is_absolute().then_some(path)
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct BuildManifest {
    artifact_schema: String,
    project_version: String,
    git_sha: String,
    python_version: String,
    playwright_version: String,
    pyinstaller_version: String,
    target_os: String,
    target_arch: String,
    browser: String,
    created_at_utc: String,
}

pub(super) fn validate_worker_package(
    package_root: &Path,
    executable: &Path,
) -> Result<(), &'static str> {
    let manifest_path = package_root.join("BUILD-MANIFEST.json");
    let internal_path = package_root.join("_internal");
    if !executable.is_file() || !manifest_path.is_file() || !internal_path.is_dir() {
        return Err("worker_package_invalid");
    }
    if is_reparse_point(package_root)
        || is_reparse_point(executable)
        || is_reparse_point(&manifest_path)
        || is_reparse_point(&internal_path)
    {
        return Err("worker_package_invalid");
    }
    let metadata = fs::metadata(&manifest_path).map_err(|_| "worker_package_invalid")?;
    if metadata.len() > MAX_MANIFEST_BYTES {
        return Err("worker_package_invalid");
    }
    let raw = fs::read(&manifest_path).map_err(|_| "worker_package_invalid")?;
    let manifest: BuildManifest =
        serde_json::from_slice(&raw).map_err(|_| "worker_package_invalid")?;
    if manifest.artifact_schema != "threads-worker-package-v1"
        || manifest.target_os != "windows"
        || manifest.target_arch != "x64"
        || manifest.browser != "chromium"
        || !valid_project_version(&manifest.project_version)
        || !valid_tool_version(&manifest.python_version)
        || !valid_tool_version(&manifest.playwright_version)
        || !valid_tool_version(&manifest.pyinstaller_version)
        || manifest.git_sha.len() != 40
        || !manifest
            .git_sha
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        || time::OffsetDateTime::parse(
            &manifest.created_at_utc,
            &time::format_description::well_known::Rfc3339,
        )
        .is_err()
    {
        return Err("worker_package_invalid");
    }
    Ok(())
}

fn valid_project_version(value: &str) -> bool {
    let (core, suffix) = value.find(['-', '+']).map_or((value, None), |index| {
        (&value[..index], Some(&value[index + 1..]))
    });
    let mut parts = core.split('.');
    let numeric_core = (0..3).all(|_| {
        parts
            .next()
            .is_some_and(|part| !part.is_empty() && part.bytes().all(|byte| byte.is_ascii_digit()))
    }) && parts.next().is_none();
    numeric_core
        && suffix.is_none_or(|suffix| {
            !suffix.is_empty()
                && suffix
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'-' | b'+'))
        })
}

fn valid_tool_version(value: &str) -> bool {
    let (core, suffix) = value.find(['-', '+']).map_or((value, None), |index| {
        (&value[..index], Some(&value[index + 1..]))
    });
    let components = core.split('.').collect::<Vec<_>>();
    (2..=3).contains(&components.len())
        && components
            .iter()
            .all(|part| !part.is_empty() && part.bytes().all(|byte| byte.is_ascii_digit()))
        && suffix.is_none_or(|suffix| {
            !suffix.is_empty()
                && suffix
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'-' | b'+'))
        })
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct WorkerHostConfig {
    schema: String,
    #[serde(default, deserialize_with = "deserialize_non_null")]
    control_plane_url: Option<String>,
    #[serde(default, deserialize_with = "deserialize_non_null")]
    data_root: Option<String>,
    #[serde(default, deserialize_with = "deserialize_non_null")]
    display_name: Option<String>,
    #[serde(default, deserialize_with = "deserialize_non_null")]
    agent_version: Option<String>,
    #[serde(default, deserialize_with = "deserialize_non_null")]
    max_concurrent_jobs: Option<u16>,
    #[serde(default, deserialize_with = "deserialize_non_null")]
    max_browser_sessions: Option<u16>,
    #[serde(default, deserialize_with = "deserialize_non_null")]
    feed_browse_enabled: Option<bool>,
    #[serde(default, deserialize_with = "deserialize_non_null")]
    thread_open_enabled: Option<bool>,
    #[serde(default, deserialize_with = "deserialize_non_null")]
    profile_open_enabled: Option<bool>,
    #[serde(default, deserialize_with = "deserialize_non_null")]
    media_local_upload_enabled: Option<bool>,
}

fn deserialize_non_null<'de, D, T>(deserializer: D) -> Result<Option<T>, D::Error>
where
    D: serde::Deserializer<'de>,
    T: Deserialize<'de>,
{
    T::deserialize(deserializer).map(Some)
}

pub(super) fn validate_host_config(path: &Path) -> Result<PathBuf, &'static str> {
    if !path.is_absolute() || is_reparse_point(path) {
        return Err("worker_host_config_invalid");
    }
    let metadata = fs::metadata(path).map_err(|_| "worker_host_config_invalid")?;
    if !metadata.is_file() || metadata.len() > MAX_HOST_CONFIG_BYTES {
        return Err("worker_host_config_invalid");
    }
    let raw = fs::read(path).map_err(|_| "worker_host_config_invalid")?;
    let config: WorkerHostConfig =
        serde_json::from_slice(&raw).map_err(|_| "worker_host_config_invalid")?;
    if config.schema != "threads-worker-host-v1"
        || !valid_config_string(config.control_plane_url.as_deref(), 2_048, true)
        || !valid_config_string(config.display_name.as_deref(), 255, false)
        || !valid_config_string(config.agent_version.as_deref(), 80, false)
        || !valid_capacity(config.max_concurrent_jobs)
        || !valid_capacity(config.max_browser_sessions)
    {
        return Err("worker_host_config_invalid");
    }
    let control_plane = config
        .control_plane_url
        .as_deref()
        .ok_or("worker_host_config_invalid")?;
    validate_control_plane_url(control_plane)?;
    let data_root_text = config
        .data_root
        .as_deref()
        .ok_or("worker_host_configuration_required")?;
    if !valid_config_string(Some(data_root_text), 1_024, true) {
        return Err("worker_host_config_invalid");
    }
    let data_root = PathBuf::from(data_root_text);
    if !data_root.is_absolute() {
        return Err("worker_host_config_invalid");
    }
    let data_root_metadata =
        fs::metadata(&data_root).map_err(|_| "worker_host_configuration_required")?;
    if !data_root_metadata.is_dir() || is_reparse_point(&data_root) {
        return Err("worker_host_configuration_required");
    }
    let _ = (
        config.feed_browse_enabled,
        config.thread_open_enabled,
        config.profile_open_enabled,
        config.media_local_upload_enabled,
    );
    Ok(data_root)
}

fn valid_config_string(value: Option<&str>, maximum: usize, required: bool) -> bool {
    match value {
        None => !required,
        Some(value) => {
            !value.is_empty()
                && value.len() <= maximum
                && value.trim() == value
                && !value.chars().any(char::is_control)
        }
    }
}

fn valid_capacity(value: Option<u16>) -> bool {
    value.is_none_or(|value| (1..=1_000).contains(&value))
}

fn validate_control_plane_url(value: &str) -> Result<(), &'static str> {
    let parsed = reqwest::Url::parse(value).map_err(|_| "worker_host_config_invalid")?;
    if parsed.scheme() != "https"
        || parsed.host_str().is_none()
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || parsed.query().is_some()
        || parsed.fragment().is_some()
        || !matches!(parsed.path(), "" | "/")
    {
        return Err("worker_host_config_invalid");
    }
    Ok(())
}

#[derive(Debug)]
struct ExistingIdentity {
    worker_id: String,
    marker_sha256: String,
    key_sha256: String,
}

fn validate_identity_files(
    data_root: &Path,
    validate_key: impl FnOnce(&[u8], &[u8]) -> Result<(), crate::windows_crypto::WorkerKeyError>,
) -> Result<ExistingIdentity, &'static str> {
    if !data_root.is_dir() {
        return Err("worker_host_configuration_required");
    }
    let worker_dir = data_root.join("worker");
    let marker_path = worker_dir.join("worker_id");
    if is_reparse_point(&worker_dir) || is_reparse_point(&marker_path) {
        return Err("worker_identity_corrupt");
    }
    let marker = fs::read(&marker_path).map_err(|error| {
        if error.kind() == std::io::ErrorKind::NotFound {
            "worker_identity_missing"
        } else {
            "worker_identity_corrupt"
        }
    })?;
    let (worker_id, worker_id_bytes, state) = parse_identity_marker(&marker)?;
    if state != "ENROLLED" {
        return Err("worker_identity_not_enrolled");
    }

    let key_path = worker_dir.join(format!("{worker_id}.device-key.dpapi"));
    if is_reparse_point(&key_path) {
        return Err("worker_identity_corrupt");
    }
    if fs::metadata(&key_path)
        .map_err(|error| {
            if error.kind() == std::io::ErrorKind::NotFound {
                "worker_identity_missing"
            } else {
                "worker_identity_corrupt"
            }
        })?
        .len()
        > 16_384
    {
        return Err("worker_identity_corrupt");
    }
    let protected_key = fs::read(&key_path).map_err(|error| {
        if error.kind() == std::io::ErrorKind::NotFound {
            "worker_identity_missing"
        } else {
            "worker_identity_corrupt"
        }
    })?;
    if !protected_key.starts_with(KEY_FILE_MAGIC) || protected_key.len() <= KEY_FILE_MAGIC.len() {
        return Err("worker_identity_corrupt");
    }

    let mut entropy = Vec::with_capacity(KEY_CONTEXT.len() + worker_id_bytes.len());
    entropy.extend_from_slice(KEY_CONTEXT);
    entropy.extend_from_slice(&worker_id_bytes);
    validate_key(&protected_key[KEY_FILE_MAGIC.len()..], &entropy).map_err(
        |error| match error {
            crate::windows_crypto::WorkerKeyError::UnprotectFailed => {
                "worker_device_key_unprotect_failed"
            }
            crate::windows_crypto::WorkerKeyError::InvalidPayload => "worker_identity_corrupt",
        },
    )?;

    Ok(ExistingIdentity {
        worker_id,
        marker_sha256: sha256_hex(&marker),
        key_sha256: sha256_hex(&protected_key),
    })
}

fn parse_identity_marker(marker: &[u8]) -> Result<(String, [u8; 16], String), &'static str> {
    let text = std::str::from_utf8(marker).map_err(|_| "worker_identity_corrupt")?;
    if text.contains('\r') && text.replace("\r\n", "").contains('\r') {
        return Err("worker_identity_corrupt");
    }
    let normalized = text.replace("\r\n", "\n");
    let body = normalized.strip_suffix('\n').unwrap_or(&normalized);
    let mut lines = body.split('\n');
    let raw_id = lines.next().ok_or("worker_identity_corrupt")?;
    let state = lines.next().ok_or("worker_identity_corrupt")?;
    if lines.next().is_some() || raw_id.len() != 36 {
        return Err("worker_identity_corrupt");
    }
    if !matches!(state, "PENDING" | "ENROLLED") {
        return Err("worker_identity_corrupt");
    }
    let worker_id_bytes = parse_uuid_bytes(raw_id).ok_or("worker_identity_corrupt")?;
    Ok((
        format_uuid(&worker_id_bytes),
        worker_id_bytes,
        state.to_string(),
    ))
}

fn parse_uuid_bytes(value: &str) -> Option<[u8; 16]> {
    if value.len() != 36
        || ![8, 13, 18, 23]
            .into_iter()
            .all(|index| value.as_bytes()[index] == b'-')
    {
        return None;
    }
    let hex: Vec<u8> = value.bytes().filter(|byte| *byte != b'-').collect();
    if hex.len() != 32 {
        return None;
    }
    let mut output = [0_u8; 16];
    let (pairs, remainder) = hex.as_slice().as_chunks::<2>();
    if !remainder.is_empty() {
        return None;
    }
    for (index, pair) in pairs.iter().enumerate() {
        output[index] = (hex_nibble(pair[0])? << 4) | hex_nibble(pair[1])?;
    }
    Some(output)
}

fn hex_nibble(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

fn format_uuid(value: &[u8; 16]) -> String {
    let raw = value
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    format!(
        "{}-{}-{}-{}-{}",
        &raw[0..8],
        &raw[8..12],
        &raw[12..16],
        &raw[16..20],
        &raw[20..32]
    )
}

fn sha256_hex(value: &[u8]) -> String {
    Sha256::digest(value)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

pub(super) fn binding_identity_unchanged(binding: &WorkerHostBinding) -> bool {
    let worker_dir = binding.data_root.join("worker");
    let marker = fs::read(worker_dir.join("worker_id"));
    let protected_key =
        fs::read(worker_dir.join(format!("{}.device-key.dpapi", binding.worker_id)));
    marker.is_ok_and(|bytes| sha256_hex(&bytes) == binding.identity_marker_sha256)
        && protected_key.is_ok_and(|bytes| sha256_hex(&bytes) == binding.protected_key_sha256)
}

pub(super) fn guard_identity_files(
    binding: &WorkerHostBinding,
) -> Result<IdentityFileGuard, &'static str> {
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        use windows_sys::Win32::Storage::FileSystem::{
            FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_READ,
        };

        let worker_dir = binding.data_root.join("worker");
        let marker_path = worker_dir.join("worker_id");
        let key_path = worker_dir.join(format!("{}.device-key.dpapi", binding.worker_id));
        let mut marker = fs::OpenOptions::new()
            .read(true)
            .share_mode(FILE_SHARE_READ)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
            .open(marker_path)
            .map_err(|_| "worker_identity_corrupt")?;
        let mut protected_key = fs::OpenOptions::new()
            .read(true)
            .share_mode(FILE_SHARE_READ)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
            .open(key_path)
            .map_err(|_| "worker_identity_corrupt")?;
        if sha256_hex(&read_file(&mut marker)?) != binding.identity_marker_sha256
            || sha256_hex(&read_file(&mut protected_key)?) != binding.protected_key_sha256
        {
            return Err("worker_identity_corrupt");
        }
        Ok(IdentityFileGuard {
            _marker: marker,
            _protected_key: protected_key,
        })
    }
    #[cfg(not(windows))]
    {
        let _ = binding;
        Err("worker_platform_unsupported")
    }
}

#[cfg(windows)]
fn read_file(file: &mut fs::File) -> Result<Vec<u8>, &'static str> {
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)
        .map_err(|_| "worker_identity_corrupt")?;
    Ok(bytes)
}

#[cfg(windows)]
fn has_reparse_point_attribute(attributes: u32) -> bool {
    use windows_sys::Win32::Storage::FileSystem::FILE_ATTRIBUTE_REPARSE_POINT;

    attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0
}

#[cfg(windows)]
fn is_reparse_point(path: &Path) -> bool {
    use std::os::windows::fs::MetadataExt;

    fs::symlink_metadata(path)
        .map(|metadata| has_reparse_point_attribute(metadata.file_attributes()))
        .unwrap_or(false)
}

#[cfg(not(windows))]
fn is_reparse_point(path: &Path) -> bool {
    fs::symlink_metadata(path)
        .map(|metadata| metadata.file_type().is_symlink())
        .unwrap_or(false)
}

fn classify_process_lock_read(result: std::io::Result<usize>) -> ProcessLockObservation {
    match result {
        Ok(_) => ProcessLockObservation::NotHeld,
        Err(error) if error.raw_os_error() == Some(33) => ProcessLockObservation::Held,
        Err(_) => ProcessLockObservation::Unavailable,
    }
}

pub(super) fn observe_process_lock(data_root: &Path) -> ProcessLockObservation {
    let path = data_root.join("worker").join("agent.lock");
    #[cfg(windows)]
    {
        use std::io::{Seek, SeekFrom};
        use std::os::windows::fs::{MetadataExt, OpenOptionsExt};
        use windows_sys::Win32::Storage::FileSystem::{
            FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE,
        };

        let mut file = match fs::OpenOptions::new()
            .read(true)
            .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
            .open(&path)
        {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                return ProcessLockObservation::NotHeld;
            }
            Err(_) => return ProcessLockObservation::Unavailable,
        };
        let metadata = match file.metadata() {
            Ok(metadata) => metadata,
            Err(_) => return ProcessLockObservation::Unavailable,
        };
        if has_reparse_point_attribute(metadata.file_attributes()) {
            return ProcessLockObservation::Unavailable;
        }
        if file.seek(SeekFrom::Start(0)).is_err() {
            return ProcessLockObservation::Unavailable;
        }
        let mut first_byte = [0_u8; 1];
        classify_process_lock_read(file.read(&mut first_byte))
    }
    #[cfg(not(windows))]
    {
        let _ = path;
        ProcessLockObservation::Unavailable
    }
}

pub(super) fn process_lock_is_available(data_root: &Path) -> Result<(), &'static str> {
    let path = data_root.join("worker").join("agent.lock");
    #[cfg(windows)]
    {
        use std::{
            fs::OpenOptions,
            os::windows::{fs::MetadataExt, fs::OpenOptionsExt, io::AsRawHandle},
        };
        use windows_sys::Win32::{
            Foundation::{GetLastError, ERROR_LOCK_VIOLATION, HANDLE},
            Storage::FileSystem::{
                LockFileEx, UnlockFileEx, FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_DELETE,
                FILE_SHARE_READ, FILE_SHARE_WRITE, LOCKFILE_EXCLUSIVE_LOCK,
                LOCKFILE_FAIL_IMMEDIATELY,
            },
            System::IO::OVERLAPPED,
        };

        let file = match OpenOptions::new()
            .read(true)
            .write(true)
            .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
            .open(&path)
        {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(_) => return Err("worker_process_lock_unavailable"),
        };
        let metadata = file
            .metadata()
            .map_err(|_| "worker_process_lock_unavailable")?;
        if has_reparse_point_attribute(metadata.file_attributes()) {
            return Err("worker_process_lock_unavailable");
        }
        let handle = file.as_raw_handle() as HANDLE;
        let mut overlapped: OVERLAPPED = unsafe { std::mem::zeroed() };
        let locked = unsafe {
            LockFileEx(
                handle,
                LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY,
                0,
                1,
                0,
                &mut overlapped,
            )
        };
        if locked == 0 {
            return Err(if unsafe { GetLastError() } == ERROR_LOCK_VIOLATION {
                "worker_process_lock_held"
            } else {
                "worker_process_lock_unavailable"
            });
        }
        let unlocked = unsafe { UnlockFileEx(handle, 0, 1, 0, &mut overlapped) };
        if unlocked == 0 {
            return Err("worker_process_lock_unavailable");
        }
        let _ = file;
        Ok(())
    }
    #[cfg(not(windows))]
    {
        let _ = path;
        Err("worker_platform_unsupported")
    }
}

pub(super) fn worker_launch_command(binding: &WorkerHostBinding) -> Command {
    let mut command = Command::new(&binding.executable);
    command.arg("--host-config").arg(&binding.host_config);
    sanitize_worker_environment(&mut command);
    command
}

fn sanitize_worker_environment(command: &mut Command) {
    for name in WORKER_ENVIRONMENT_OVERRIDES {
        command.env_remove(name);
    }
    for (name, _) in env::vars_os() {
        if name.to_string_lossy().starts_with("THREADS_WORKER_") {
            command.env_remove(name);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    #[cfg(windows)]
    use std::{
        io::{BufRead, BufReader},
        process::{Command, Stdio},
        sync::mpsc,
        thread,
        time::{Duration, Instant},
    };

    fn write_manifest(package: &Path, sha: &str) {
        let manifest = serde_json::json!({
            "artifact_schema": "threads-worker-package-v1",
            "project_version": "1.2.3",
            "git_sha": sha,
            "python_version": "3.13.7",
            "playwright_version": "1.63.0",
            "pyinstaller_version": "6.15.0",
            "target_os": "windows",
            "target_arch": "x64",
            "browser": "chromium",
            "created_at_utc": "2026-10-04T12:00:00Z"
        });
        fs::write(package.join("BUILD-MANIFEST.json"), manifest.to_string())
            .expect("write Worker manifest");
    }

    fn valid_inspection(config: &Path, executable: &Path) -> TaskInspection {
        TaskInspection {
            state: LegacyTaskState::Disabled,
            same_user: true,
            diagnostic_code: None,
            executable_path: Some(executable.to_string_lossy().into_owned()),
            host_config_path: Some(config.to_string_lossy().into_owned()),
            working_directory: Some(
                executable
                    .parent()
                    .expect("package directory")
                    .to_string_lossy()
                    .into_owned(),
            ),
        }
    }

    fn make_identity_root(root: &Path, state: &str, with_key: bool) {
        let worker = root.join("worker");
        fs::create_dir_all(&worker).expect("create Worker fixture");
        fs::write(
            worker.join("worker_id"),
            format!("12345678-1234-4234-8234-123456789abc\n{state}\n"),
        )
        .expect("write marker");
        if with_key {
            fs::write(
                worker.join("12345678-1234-4234-8234-123456789abc.device-key.dpapi"),
                [KEY_FILE_MAGIC, b"synthetic-protected-key"].concat(),
            )
            .expect("write key");
        } else {
            let _ = fs::remove_file(
                worker.join("12345678-1234-4234-8234-123456789abc.device-key.dpapi"),
            );
        }
    }

    fn key_ok(
        ciphertext: &[u8],
        entropy: &[u8],
    ) -> Result<(), crate::windows_crypto::WorkerKeyError> {
        assert_eq!(ciphertext, b"synthetic-protected-key");
        assert_eq!(
            entropy,
            [
                KEY_CONTEXT,
                &[
                    0x12, 0x34, 0x56, 0x78, 0x12, 0x34, 0x42, 0x34, 0x82, 0x34, 0x12, 0x34, 0x56,
                    0x78, 0x9a, 0xbc
                ]
            ]
            .concat()
        );
        Ok(())
    }

    #[test]
    fn task_state_classifier_fails_closed_and_requires_explicit_takeover() {
        for state in [LegacyTaskState::Running, LegacyTaskState::Ready] {
            let decision = classify_task(state);
            assert_eq!(decision.ownership, WorkerOwnership::TakeoverRequired);
            assert!(decision.diagnostic_code.is_some());
        }
        assert_eq!(
            classify_task(LegacyTaskState::Disabled).ownership,
            WorkerOwnership::Desktop
        );
        assert_eq!(
            classify_task(LegacyTaskState::Invalid).ownership,
            WorkerOwnership::Blocked
        );
        assert_eq!(
            classify_task(LegacyTaskState::NotRegistered).ownership,
            WorkerOwnership::Blocked
        );
    }

    #[test]
    fn task_inspection_json_has_fixed_states_and_rejects_unknown_fields() {
        let inspection: TaskInspection = serde_json::from_str(
            r#"{"state":"RUNNING","same_user":true,"executable_path":null,"host_config_path":null,"working_directory":null}"#,
        )
        .expect("parse task state");
        assert_eq!(inspection.state, LegacyTaskState::Running);
        assert!(serde_json::from_str::<TaskInspection>(
            r#"{"state":"RUNNING","same_user":false,"executable_path":null,"host_config_path":null,"working_directory":null,"xml":"secret"}"#,
        ).is_err());
    }

    #[test]
    fn worker_manifest_contract_accepts_older_worker_sha_and_rejects_wrong_platform() {
        let dir = tempfile::tempdir().expect("package fixture");
        let package = dir.path();
        fs::create_dir(package.join("_internal")).expect("create package internal");
        let executable = package.join("threads-worker.exe");
        fs::write(&executable, b"synthetic executable").expect("write package executable");
        let older_sha = "1".repeat(40);
        write_manifest(package, &older_sha);
        assert!(validate_worker_package(package, &executable).is_ok());

        let mut manifest: serde_json::Value = serde_json::from_slice(
            &fs::read(package.join("BUILD-MANIFEST.json")).expect("read manifest"),
        )
        .expect("parse manifest");
        manifest["target_arch"] = serde_json::json!("arm64");
        fs::write(package.join("BUILD-MANIFEST.json"), manifest.to_string())
            .expect("rewrite fixture manifest");
        assert_eq!(
            validate_worker_package(package, &executable),
            Err("worker_package_invalid")
        );
    }

    #[test]
    fn host_config_requires_existing_explicit_root_and_rejects_unknown_or_null_fields() {
        let dir = tempfile::tempdir().expect("host config fixture");
        let root = dir.path().join("worker-data");
        fs::create_dir(&root).expect("create data root");
        let config = dir.path().join("host.json");
        fs::write(
            &config,
            serde_json::json!({
                "schema": "threads-worker-host-v1",
                "control_plane_url": "https://controller.example.test",
                "data_root": root.to_string_lossy(),
                "feed_browse_enabled": false
            })
            .to_string(),
        )
        .expect("write valid host config");
        assert_eq!(validate_host_config(&config).unwrap(), root);

        let missing_root = serde_json::json!({"schema":"threads-worker-host-v1", "control_plane_url":"https://controller.example.test"});
        fs::write(&config, missing_root.to_string()).expect("write missing root config");
        assert_eq!(
            validate_host_config(&config),
            Err("worker_host_configuration_required")
        );

        let null_field = serde_json::json!({"schema":"threads-worker-host-v1", "control_plane_url":"https://controller.example.test", "data_root":root, "feed_browse_enabled":null});
        fs::write(&config, null_field.to_string()).expect("write null field config");
        assert_eq!(
            validate_host_config(&config),
            Err("worker_host_config_invalid")
        );

        let unknown = serde_json::json!({"schema":"threads-worker-host-v1", "control_plane_url":"https://controller.example.test", "data_root":root, "secret":"unexpected"});
        fs::write(&config, unknown.to_string()).expect("write unknown field config");
        assert_eq!(
            validate_host_config(&config),
            Err("worker_host_config_invalid")
        );
    }

    #[test]
    fn identity_preflight_is_read_only_and_requires_enrolled_marker_and_key() {
        let dir = tempfile::tempdir().expect("identity fixture");
        let root = dir.path();
        let before = fs::read_dir(root).expect("list empty root").count();
        assert_eq!(
            validate_identity_files(root, key_ok).unwrap_err(),
            "worker_identity_missing"
        );
        assert_eq!(
            fs::read_dir(root).expect("list unchanged root").count(),
            before
        );

        make_identity_root(root, "PENDING", true);
        assert_eq!(
            validate_identity_files(root, key_ok).unwrap_err(),
            "worker_identity_not_enrolled"
        );

        make_identity_root(root, "BROKEN", true);
        assert_eq!(
            validate_identity_files(root, key_ok).unwrap_err(),
            "worker_identity_corrupt"
        );

        make_identity_root(root, "ENROLLED", false);
        assert_eq!(
            validate_identity_files(root, key_ok).unwrap_err(),
            "worker_identity_missing"
        );

        make_identity_root(root, "ENROLLED", true);
        let journal = root.join("journal");
        let profile = root.join("profiles").join("synthetic-profile");
        fs::create_dir_all(&journal).expect("create journal fixture");
        fs::create_dir_all(&profile).expect("create profile fixture");
        fs::write(journal.join("worker-state.sqlite3"), b"journal sentinel")
            .expect("write journal sentinel");
        fs::write(profile.join("profile-sentinel"), b"profile sentinel")
            .expect("write profile sentinel");
        let before = [
            root.join("worker/worker_id"),
            root.join("worker/12345678-1234-4234-8234-123456789abc.device-key.dpapi"),
            journal.join("worker-state.sqlite3"),
            profile.join("profile-sentinel"),
        ]
        .map(|path| (path.clone(), fs::read(path).expect("snapshot Worker state")));
        let identity = validate_identity_files(root, key_ok).expect("validate enrolled identity");
        for (path, bytes) in before {
            assert_eq!(fs::read(path).expect("read unchanged Worker state"), bytes);
        }
        assert_eq!(identity.worker_id, "12345678-1234-4234-8234-123456789abc");
        assert_eq!(identity.marker_sha256.len(), 64);
        assert_eq!(identity.key_sha256.len(), 64);
    }

    #[test]
    fn identity_preflight_rejects_corrupt_key_and_current_user_unprotect_failure() {
        let dir = tempfile::tempdir().expect("identity fixture");
        make_identity_root(dir.path(), "ENROLLED", true);
        let worker = dir.path().join("worker");
        let key_path = worker.join("12345678-1234-4234-8234-123456789abc.device-key.dpapi");
        fs::write(&key_path, b"invalid").expect("corrupt protected key");
        assert_eq!(
            validate_identity_files(dir.path(), key_ok).unwrap_err(),
            "worker_identity_corrupt"
        );

        make_identity_root(dir.path(), "ENROLLED", true);
        let unprotect_failure = |_ciphertext: &[u8], _entropy: &[u8]| {
            Err(crate::windows_crypto::WorkerKeyError::UnprotectFailed)
        };
        assert_eq!(
            validate_identity_files(dir.path(), unprotect_failure).unwrap_err(),
            "worker_device_key_unprotect_failed"
        );
    }

    #[test]
    fn disabled_task_requires_same_user_and_full_package_config_identity_preflight() {
        let dir = tempfile::tempdir().expect("task fixture");
        let package = dir.path().join("release");
        fs::create_dir_all(package.join("_internal")).expect("create release");
        let executable = package.join("threads-worker.exe");
        fs::File::create(&executable).expect("create executable");
        write_manifest(&package, &"a".repeat(40));
        let data_root = dir.path().join("worker-data");
        fs::create_dir(&data_root).expect("create data root");
        make_identity_root(&data_root, "ENROLLED", true);
        let config = dir.path().join("host.json");
        let mut file = fs::File::create(&config).expect("create host config");
        write!(file, "{{\"schema\":\"threads-worker-host-v1\",\"control_plane_url\":\"https://controller.example.test\",\"data_root\":{}}}", serde_json::to_string(&data_root.to_string_lossy()).unwrap()).expect("write config");

        let mut inspection = valid_inspection(&config, &executable);
        let binding = validate_task_binding_with(&inspection, key_ok)
            .expect("valid disabled task binds existing Worker identity");
        assert_eq!(binding.worker_id, "12345678-1234-4234-8234-123456789abc");
        assert_eq!(binding.executable, executable);
        assert_eq!(binding.host_config, config);
        #[cfg(windows)]
        {
            let marker = data_root.join("worker/worker_id");
            let key = data_root.join(format!("worker/{}.device-key.dpapi", binding.worker_id));
            let marker_before = fs::read(&marker).expect("read identity marker");
            let key_before = fs::read(&key).expect("read protected key");
            let _guard = guard_identity_files(&binding).expect("guard identity files");
            assert!(
                fs::read(&marker).is_ok(),
                "Worker can still read its marker"
            );
            assert!(fs::read(&key).is_ok(), "Worker can still read its key");
            assert!(fs::write(&marker, b"replacement").is_err());
            assert!(fs::remove_file(&key).is_err());
            assert_eq!(
                fs::read(&marker).expect("read unchanged marker"),
                marker_before
            );
            assert_eq!(fs::read(&key).expect("read unchanged key"), key_before);
        }
        inspection.same_user = false;
        assert_eq!(
            validate_task_binding_with(&inspection, key_ok).unwrap_err(),
            "worker_legacy_task_invalid"
        );
    }

    #[test]
    fn launch_command_uses_exact_worker_arguments_and_removes_overrides() {
        let binding = WorkerHostBinding {
            executable: PathBuf::from(r"C:\Program Files\ThreadsWorker\threads-worker.exe"),
            host_config: PathBuf::from(r"C:\Users\operator\worker-host.json"),
            data_root: PathBuf::from(r"C:\Users\operator\AppData\Local\ThreadsOperations"),
            worker_id: "12345678-1234-4234-8234-123456789abc".to_string(),
            identity_marker_sha256: "a".repeat(64),
            protected_key_sha256: "b".repeat(64),
        };
        let mut command = worker_launch_command(&binding);
        let args = command
            .get_args()
            .map(|arg| arg.to_string_lossy().into_owned())
            .collect::<Vec<_>>();
        assert_eq!(
            args,
            ["--host-config", r"C:\Users\operator\worker-host.json"]
        );
        for name in WORKER_ENVIRONMENT_OVERRIDES {
            command.env(name, "must-not-pass");
        }
        sanitize_worker_environment(&mut command);
        for name in WORKER_ENVIRONMENT_OVERRIDES {
            assert_eq!(
                command
                    .get_envs()
                    .find(|(key, _)| key.to_string_lossy() == *name)
                    .map(|(_, value)| value),
                Some(None)
            );
        }
    }

    #[test]
    fn lock_read_classifier_only_treats_error_33_as_held() {
        assert_eq!(
            classify_process_lock_read(Ok(0)),
            ProcessLockObservation::NotHeld
        );
        assert_eq!(
            classify_process_lock_read(Ok(1)),
            ProcessLockObservation::NotHeld
        );
        assert_eq!(
            classify_process_lock_read(Err(std::io::Error::from_raw_os_error(33))),
            ProcessLockObservation::Held
        );
        assert_eq!(
            classify_process_lock_read(Err(std::io::Error::from(
                std::io::ErrorKind::PermissionDenied
            ))),
            ProcessLockObservation::Unavailable
        );
    }

    #[cfg(windows)]
    #[test]
    fn reparse_point_detection_uses_the_windows_file_attribute() {
        assert!(!has_reparse_point_attribute(0));
        assert!(has_reparse_point_attribute(0x0400));
        assert!(has_reparse_point_attribute(0x0400 | 0x0020));
    }

    #[test]
    fn post_resume_observer_has_no_lock_or_mutating_file_operations() {
        let source = include_str!("worker_host.rs");
        let start = source
            .find("pub(super) fn observe_process_lock")
            .expect("post-resume observer exists");
        let end = source[start..]
            .find("pub(super) fn process_lock_is_available")
            .map(|offset| start + offset)
            .expect("pre-spawn availability probe follows observer");
        let observer = &source[start..end];
        for forbidden in [
            "LockFileEx",
            "UnlockFileEx",
            ".write(",
            ".create(",
            ".append(",
            ".truncate(",
        ] {
            assert!(
                !observer.contains(forbidden),
                "post-resume observer must not contain {forbidden}"
            );
        }
        assert!(observer.contains(".read(true)"));
        assert!(observer.contains("FILE_FLAG_OPEN_REPARSE_POINT"));
    }

    #[cfg(windows)]
    #[test]
    fn missing_and_unlocked_process_lock_observations_do_not_mutate_the_file() {
        let directory = tempfile::tempdir().expect("Worker root");
        let worker = directory.path().join("worker");
        fs::create_dir(&worker).expect("create Worker directory");
        let lock_path = worker.join("agent.lock");

        assert_eq!(
            observe_process_lock(directory.path()),
            ProcessLockObservation::NotHeld
        );
        assert!(!lock_path.exists(), "observer must not create agent.lock");

        fs::write(&lock_path, b"worker lock sentinel").expect("create existing lock file");
        let before = fs::read(&lock_path).expect("snapshot unlocked process lock");
        assert_eq!(
            observe_process_lock(directory.path()),
            ProcessLockObservation::NotHeld
        );
        assert_eq!(
            fs::read(&lock_path).expect("read unchanged process lock"),
            before
        );
    }

    #[cfg(windows)]
    #[test]
    fn read_only_observer_detects_byte_range_lock_held_by_another_handle() {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::{
            Foundation::HANDLE,
            Storage::FileSystem::{LockFileEx, UnlockFileEx, LOCKFILE_EXCLUSIVE_LOCK},
            System::IO::OVERLAPPED,
        };

        let directory = tempfile::tempdir().expect("Worker root");
        let worker = directory.path().join("worker");
        fs::create_dir(&worker).expect("create Worker directory");
        let path = worker.join("agent.lock");
        fs::write(&path, b"0").expect("create existing process lock file");
        let before = fs::read(&path).expect("snapshot process lock");
        let owner = fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(&path)
            .expect("open lock owner");
        let handle = owner.as_raw_handle() as HANDLE;
        let mut overlapped: OVERLAPPED = unsafe { std::mem::zeroed() };
        let locked =
            unsafe { LockFileEx(handle, LOCKFILE_EXCLUSIVE_LOCK, 0, 1, 0, &mut overlapped) };
        assert_ne!(locked, 0, "fixture acquires byte zero");
        assert_eq!(
            observe_process_lock(directory.path()),
            ProcessLockObservation::Held
        );
        unsafe {
            UnlockFileEx(handle, 0, 1, 0, &mut overlapped);
        }
        assert_eq!(
            observe_process_lock(directory.path()),
            ProcessLockObservation::NotHeld
        );
        assert_eq!(fs::read(&path).expect("lock remains unchanged"), before);
    }

    #[cfg(windows)]
    #[test]
    fn process_lock_junction_fails_closed_without_following_target() {
        let directory = tempfile::tempdir().expect("Worker root");
        let worker = directory.path().join("worker");
        fs::create_dir(&worker).expect("create Worker directory");
        let target = worker.join("target-lock-directory");
        let link = worker.join("agent.lock");
        fs::create_dir(&target).expect("create junction target");
        let sentinel = target.join("sentinel");
        fs::write(&sentinel, b"target bytes").expect("create target sentinel");
        let junction = Command::new("cmd.exe")
            .args(["/d", "/c", "mklink", "/J"])
            .arg(&link)
            .arg(&target)
            .output()
            .expect("create directory junction");
        assert!(
            junction.status.success(),
            "Windows creates directory junction"
        );

        assert!(is_reparse_point(&link));
        assert_eq!(
            observe_process_lock(directory.path()),
            ProcessLockObservation::Unavailable
        );
        assert_eq!(
            fs::read(&sentinel).expect("target remains untouched"),
            b"target bytes"
        );
    }

    #[cfg(windows)]
    #[test]
    fn actual_worker_process_lock_interoperates_with_read_only_observer() {
        let directory = tempfile::tempdir().expect("Worker root");
        let worker = directory.path().join("worker");
        fs::create_dir(&worker).expect("create Worker directory");
        let lock_path = worker.join("agent.lock");
        let repository_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../..")
            .canonicalize()
            .expect("resolve repository root");
        let python_source = repository_root.join("src").to_string_lossy().into_owned();
        let script = concat!(
            "import sys\n",
            "from pathlib import Path\n",
            "sys.path.insert(0, sys.argv[2])\n",
            "from threads_platform.infrastructure.worker_agent.process_lock import WorkerProcessLock\n",
            "lock = WorkerProcessLock(Path(sys.argv[1]))\n",
            "lock.acquire()\n",
            "print('WORKER_LOCK_HELD', flush=True)\n",
            "sys.stdin.readline()\n",
            "lock.release()\n",
        );

        let (program, prefix_args) = python_launcher();
        let mut child = Command::new(program)
            .args(prefix_args)
            .arg("-c")
            .arg(script)
            .arg(&lock_path)
            .arg(python_source)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .expect("start Python Worker lock fixture");
        let stdout = child.stdout.take().expect("capture Worker lock signal");
        let (signal_sender, signal_receiver) = mpsc::channel();
        thread::spawn(move || {
            let mut line = String::new();
            let result = BufReader::new(stdout).read_line(&mut line).map(|_| line);
            let _ = signal_sender.send(result);
        });
        let signal = match signal_receiver.recv_timeout(Duration::from_secs(5)) {
            Ok(Ok(signal)) => signal,
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                panic!("Worker lock fixture did not signal within the deadline");
            }
        };
        if signal.trim() != "WORKER_LOCK_HELD" {
            let _ = child.kill();
            let _ = child.wait();
            panic!("Worker lock fixture did not acquire the expected byte range");
        }

        let held = observe_process_lock(directory.path());
        let mut stdin = child.stdin.take().expect("Worker lock release channel");
        stdin
            .write_all(b"release\n")
            .expect("release actual Worker lock");
        drop(stdin);
        let deadline = Instant::now() + Duration::from_secs(5);
        let status = loop {
            if let Some(status) = child.try_wait().expect("poll Worker lock fixture") {
                break status;
            }
            if Instant::now() >= deadline {
                let _ = child.kill();
                let _ = child.wait();
                panic!("Worker lock fixture did not exit before deadline");
            }
            thread::sleep(Duration::from_millis(10));
        };
        let released = observe_process_lock(directory.path());

        assert!(status.success(), "Worker lock fixture exits cleanly");
        assert_eq!(held, ProcessLockObservation::Held);
        assert_eq!(released, ProcessLockObservation::NotHeld);
        assert_eq!(fs::read(&lock_path).expect("read lock bytes"), b"0");
    }

    #[cfg(windows)]
    fn python_launcher() -> (&'static str, Vec<&'static str>) {
        for (program, prefix_args) in [("py", vec!["-3"]), ("python", vec![])] {
            let available = Command::new(program)
                .args(prefix_args.iter().copied())
                .args(["-c", "pass"])
                .status()
                .is_ok_and(|status| status.success());
            if available {
                return (program, prefix_args);
            }
        }
        panic!("Windows test host must provide Python for Worker lock interoperability");
    }
}
