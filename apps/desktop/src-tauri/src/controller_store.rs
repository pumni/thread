use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    net::{Ipv4Addr, TcpListener},
    path::{Path, PathBuf},
};

use serde::{Deserialize, Serialize};
use tempfile::NamedTempFile;
use zeroize::Zeroizing;

const CONFIG_SCHEMA_VERSION: u32 = 2;
const MIN_FREE_BYTES: u64 = 256 * 1024 * 1024;
const DATABASE_USER: &str = "threads_platform";

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(super) struct ControllerConfig {
    pub schema_version: u32,
    pub controller_id: String,
    pub cluster_initialized: bool,
    pub database_port: u16,
    pub endpoint_port: u16,
    #[serde(default)]
    pub lan_address: Option<Ipv4Addr>,
    #[serde(default)]
    pub tls_identity_provisioned: bool,
}

impl ControllerConfig {
    fn save_atomic(&self, path: &Path) -> Result<(), &'static str> {
        let parent = path.parent().ok_or("controller_config_path_invalid")?;
        let mut temporary =
            NamedTempFile::new_in(parent).map_err(|_| "controller_config_write_failed")?;
        serde_json::to_writer(&mut temporary, self)
            .map_err(|_| "controller_config_write_failed")?;
        temporary
            .flush()
            .and_then(|()| temporary.as_file().sync_all())
            .map_err(|_| "controller_config_write_failed")?;
        temporary
            .persist(path)
            .map_err(|_| "controller_config_write_failed")?;
        Ok(())
    }

    fn load(path: &Path) -> Result<Self, &'static str> {
        let bytes = fs::read(path).map_err(|_| "controller_config_read_failed")?;
        let mut config: Self =
            serde_json::from_slice(&bytes).map_err(|_| "controller_config_invalid")?;
        if !matches!(config.schema_version, 1 | CONFIG_SCHEMA_VERSION)
            || config.controller_id.len() != 32
            || !config
                .controller_id
                .bytes()
                .all(|value| value.is_ascii_hexdigit())
            || config.database_port == 0
            || config.endpoint_port == 0
            || config.database_port == config.endpoint_port
        {
            return Err("controller_config_invalid");
        }
        if !config.cluster_initialized {
            return Err("controller_data_root_incomplete");
        }
        if config.schema_version == 1 {
            config.schema_version = CONFIG_SCHEMA_VERSION;
            config.save_atomic(path)?;
        }
        Ok(config)
    }
}

pub(super) struct ControllerStore {
    root: PathBuf,
    runtime_root: PathBuf,
    config_path: PathBuf,
    config: ControllerConfig,
    database_password: Zeroizing<Vec<u8>>,
    new_root: bool,
    _owner_lock: File,
}

impl ControllerStore {
    pub fn open(root: PathBuf, runtime_root: PathBuf) -> Result<Self, &'static str> {
        Self::open_with_free_space_check(root, runtime_root, verify_free_space)
    }

    fn open_with_free_space_check(
        root: PathBuf,
        runtime_root: PathBuf,
        check_free_space: impl FnOnce(&Path) -> Result<(), &'static str>,
    ) -> Result<Self, &'static str> {
        if !root.is_absolute() || !runtime_root.is_absolute() {
            return Err("controller_path_invalid");
        }
        verify_runtime_bundle(&runtime_root)?;
        let mut volume_path = root.as_path();
        while !volume_path.exists() {
            volume_path = volume_path
                .parent()
                .ok_or("controller_disk_preflight_failed")?;
        }
        check_free_space(volume_path)?;

        let root_existed = root.exists();
        if root_existed {
            let metadata =
                fs::symlink_metadata(&root).map_err(|_| "controller_data_root_unavailable")?;
            if metadata.file_type().is_symlink() {
                return Err("controller_data_root_unowned");
            }
        } else {
            fs::create_dir_all(&root).map_err(|_| "controller_data_root_unavailable")?;
        }

        let config_path = root.join("controller.json");
        if root_existed && !config_path.is_file() {
            return Err("controller_data_root_unowned");
        }

        let owner_lock_path = root.join(".owner.lock");
        let owner_lock = open_owner_lock(&owner_lock_path)?;
        let credential_path = root.join("database-credential.dpapi");
        let (config, database_password) = if root_existed {
            let config = ControllerConfig::load(&config_path)?;
            verify_cluster_directory(&root)?;
            let protected =
                fs::read(&credential_path).map_err(|_| "controller_credential_unavailable")?;
            let password =
                Zeroizing::new(crate::windows_crypto::unprotect_current_user(&protected)?);
            if password.len() != 64 || !password.iter().all(u8::is_ascii_hexdigit) {
                return Err("controller_credential_invalid");
            }
            (config, password)
        } else {
            let mut password_random = Zeroizing::new([0u8; 32]);
            crate::windows_crypto::fill_random(&mut *password_random)?;
            let password = Zeroizing::new(hex(&*password_random).into_bytes());
            let mut identity_random = Zeroizing::new([0u8; 16]);
            crate::windows_crypto::fill_random(&mut *identity_random)?;
            let controller_id = hex(&*identity_random);

            let database_socket = TcpListener::bind(("127.0.0.1", 0))
                .map_err(|_| "controller_database_port_unavailable")?;
            let config = ControllerConfig {
                schema_version: CONFIG_SCHEMA_VERSION,
                controller_id,
                cluster_initialized: false,
                database_port: database_socket
                    .local_addr()
                    .map_err(|_| "controller_database_port_unavailable")?
                    .port(),
                endpoint_port: 8443,
                lan_address: None,
                tls_identity_provisioned: false,
            };
            drop(database_socket);

            let protected = crate::windows_crypto::protect_current_user(&password)?;
            atomic_write(
                &credential_path,
                &protected,
                "controller_credential_write_failed",
            )?;
            config.save_atomic(&config_path)?;
            (config, password)
        };

        Ok(Self {
            root,
            runtime_root,
            config_path,
            config,
            database_password,
            new_root: !root_existed,
            _owner_lock: owner_lock,
        })
    }

    pub fn config(&self) -> &ControllerConfig {
        &self.config
    }

    pub fn data_root(&self) -> &Path {
        &self.root
    }

    pub fn local_endpoint(&self) -> Result<String, &'static str> {
        if self.config.lan_address.is_none() {
            return Err("controller_https_configuration_required");
        }
        Ok(format!("https://127.0.0.1:{}", self.config.endpoint_port))
    }

    pub fn configure_https(
        &mut self,
        lan_address: &str,
        endpoint_port: u16,
    ) -> Result<(), &'static str> {
        let address = lan_address
            .parse::<Ipv4Addr>()
            .map_err(|_| "controller_lan_address_invalid")?;
        if address.is_loopback() || address.is_unspecified() || address.is_multicast() {
            return Err("controller_lan_address_invalid");
        }
        if endpoint_port == 0 {
            return Err("controller_endpoint_port_invalid");
        }
        if endpoint_port == self.config.database_port {
            return Err("controller_endpoint_port_in_use");
        }
        if self.config.lan_address == Some(address) && self.config.endpoint_port == endpoint_port {
            return Ok(());
        }
        let previous = self.config.clone();
        self.config.lan_address = Some(address);
        self.config.endpoint_port = endpoint_port;
        if let Err(error) = self.config.save_atomic(&self.config_path) {
            self.config = previous;
            return Err(error);
        }
        Ok(())
    }

    pub fn needs_initialization(&self) -> bool {
        self.new_root
    }

    #[cfg(test)]
    fn database_password(&self) -> &[u8] {
        &self.database_password
    }

    pub fn database_url(&self) -> Zeroizing<String> {
        Zeroizing::new(format!(
            "postgresql+asyncpg://{DATABASE_USER}:{}@127.0.0.1:{}/postgres",
            String::from_utf8_lossy(&self.database_password),
            self.config.database_port
        ))
    }

    pub fn mark_cluster_initialized(&mut self) -> Result<(), &'static str> {
        self.config.cluster_initialized = true;
        if let Err(error) = self.config.save_atomic(&self.config_path) {
            self.config.cluster_initialized = false;
            return Err(error);
        }
        self.new_root = false;
        Ok(())
    }

    pub fn mark_tls_identity_provisioned(&mut self) -> Result<(), &'static str> {
        self.config.tls_identity_provisioned = true;
        if let Err(error) = self.config.save_atomic(&self.config_path) {
            self.config.tls_identity_provisioned = false;
            return Err(error);
        }
        Ok(())
    }

    pub fn database_executable(&self, name: &str) -> PathBuf {
        self.runtime_root.join("postgresql").join("bin").join(name)
    }

    pub fn runtime_executable(&self) -> PathBuf {
        self.runtime_root
            .join("threads-runtime")
            .join("threads-runtime.exe")
    }

    pub fn postgres_data_dir(&self) -> PathBuf {
        self.root.join("postgresql")
    }

    pub fn postgres_ready_executable(&self) -> PathBuf {
        self.database_executable("pg_isready.exe")
    }

    pub fn pg_ctl_executable(&self) -> PathBuf {
        self.database_executable("pg_ctl.exe")
    }

    pub fn save_initdb_password_file(&self) -> Result<PathBuf, &'static str> {
        let path = self.root.join("initdb.password.tmp");
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
            .map_err(|_| "controller_initdb_secret_write_failed")?;
        let write_result = file
            .write_all(&self.database_password)
            .and_then(|()| file.write_all(b"\n"))
            .and_then(|()| file.sync_all());
        if write_result.is_err() {
            drop(file);
            fs::remove_file(&path).map_err(|_| "controller_initdb_secret_cleanup_failed")?;
            return Err("controller_initdb_secret_write_failed");
        }
        Ok(path)
    }

    pub fn remove_initdb_password_file(&self, path: &Path) -> Result<(), &'static str> {
        fs::remove_file(path).map_err(|_| "controller_initdb_secret_cleanup_failed")
    }

    pub fn config_summary(&self) -> ControllerSummary {
        ControllerSummary {
            controller_id: self.config.controller_id.clone(),
            endpoint: self.config.lan_address.map_or_else(String::new, |address| {
                format!("https://{address}:{}", self.config.endpoint_port)
            }),
            database_port: self.config.database_port,
        }
    }
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub(super) struct ControllerSummary {
    pub controller_id: String,
    pub endpoint: String,
    pub database_port: u16,
}

fn verify_runtime_bundle(root: &Path) -> Result<(), &'static str> {
    let required = [
        root.join("threads-runtime").join("threads-runtime.exe"),
        root.join("postgresql").join("bin").join("initdb.exe"),
        root.join("postgresql").join("bin").join("postgres.exe"),
        root.join("postgresql").join("bin").join("pg_ctl.exe"),
        root.join("postgresql").join("bin").join("pg_isready.exe"),
    ];
    if required.iter().all(|path| path.is_file()) {
        Ok(())
    } else {
        Err("controller_runtime_bundle_invalid")
    }
}

fn verify_cluster_directory(root: &Path) -> Result<(), &'static str> {
    let version_path = root.join("postgresql").join("PG_VERSION");
    let mut version = String::new();
    File::open(version_path)
        .and_then(|mut file| file.read_to_string(&mut version))
        .map_err(|_| "controller_database_cluster_invalid")?;
    if version.trim() == "17" {
        Ok(())
    } else {
        Err("controller_database_cluster_invalid")
    }
}

fn atomic_write(path: &Path, bytes: &[u8], error: &'static str) -> Result<(), &'static str> {
    let parent = path.parent().ok_or(error)?;
    let mut temporary = NamedTempFile::new_in(parent).map_err(|_| error)?;
    temporary.write_all(bytes).map_err(|_| error)?;
    temporary
        .flush()
        .and_then(|()| temporary.as_file().sync_all())
        .map_err(|_| error)?;
    temporary.persist(path).map_err(|_| error)?;
    Ok(())
}

fn hex(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        output.push(HEX[(byte >> 4) as usize] as char);
        output.push(HEX[(byte & 0x0f) as usize] as char);
    }
    output
}

fn verify_free_space(path: &Path) -> Result<(), &'static str> {
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::Storage::FileSystem::GetDiskFreeSpaceExW;

        let path: Vec<u16> = path.as_os_str().encode_wide().chain([0]).collect();
        let mut available = 0u64;
        let success = unsafe {
            GetDiskFreeSpaceExW(
                path.as_ptr(),
                &mut available,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
            )
        };
        if success == 0 {
            return Err("controller_disk_preflight_failed");
        }
        validate_free_space(available)
    }
    #[cfg(not(windows))]
    {
        let _ = path;
        Err("controller_platform_unsupported")
    }
}

fn validate_free_space(available: u64) -> Result<(), &'static str> {
    if available < MIN_FREE_BYTES {
        Err("controller_disk_space_low")
    } else {
        Ok(())
    }
}

fn open_owner_lock(path: &Path) -> Result<File, &'static str> {
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .share_mode(0)
            .open(path)
            .map_err(|error| {
                if matches!(error.raw_os_error(), Some(32 | 33)) {
                    "controller_data_root_already_owned"
                } else {
                    "controller_data_root_access_denied"
                }
            })
    }
    #[cfg(not(windows))]
    {
        let _ = path;
        Err("controller_platform_unsupported")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn configuration_store(root: &Path, endpoint_port: u16) -> ControllerStore {
        fs::create_dir_all(root).expect("create temporary Controller root");
        let config_path = root.join("controller.json");
        let config = ControllerConfig {
            schema_version: CONFIG_SCHEMA_VERSION,
            controller_id: "0123456789abcdef0123456789abcdef".to_string(),
            cluster_initialized: true,
            database_port: 51_001,
            endpoint_port,
            lan_address: None,
            tls_identity_provisioned: false,
        };
        config
            .save_atomic(&config_path)
            .expect("persist fixture config");
        let owner_lock = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(false)
            .open(root.join(".owner.lock"))
            .expect("create fixture owner lock");
        ControllerStore {
            root: root.to_path_buf(),
            runtime_root: root.join("runtime"),
            config_path,
            config,
            database_password: Zeroizing::new(Vec::new()),
            new_root: false,
            _owner_lock: owner_lock,
        }
    }

    #[test]
    fn low_free_space_fails_before_provisioning() {
        assert_eq!(validate_free_space(0), Err("controller_disk_space_low"));
        assert_eq!(
            validate_free_space(MIN_FREE_BYTES - 1),
            Err("controller_disk_space_low")
        );
        assert_eq!(validate_free_space(MIN_FREE_BYTES), Ok(()));
    }

    #[test]
    fn disk_preflight_failure_leaves_fresh_and_existing_roots_untouched() {
        let directory = tempfile::tempdir().expect("temporary Controller parent");
        let runtime = directory.path().join("runtime");
        for relative in [
            "threads-runtime/threads-runtime.exe",
            "postgresql/bin/initdb.exe",
            "postgresql/bin/postgres.exe",
            "postgresql/bin/pg_ctl.exe",
            "postgresql/bin/pg_isready.exe",
        ] {
            let path = runtime.join(relative);
            fs::create_dir_all(path.parent().expect("runtime file parent"))
                .expect("create runtime file parent");
            fs::write(path, b"test").expect("write runtime placeholder");
        }

        let fresh_root = directory.path().join("fresh");
        let fresh_result = ControllerStore::open_with_free_space_check(
            fresh_root.clone(),
            runtime.clone(),
            |_| Err("controller_disk_space_low"),
        );
        assert_eq!(fresh_result.err(), Some("controller_disk_space_low"));
        assert!(!fresh_root.exists());

        let existing_root = directory.path().join("existing");
        fs::create_dir(&existing_root).expect("create existing root");
        let marker = existing_root.join("existing-data.marker");
        fs::write(&marker, b"preserve").expect("write existing marker");
        let existing_result =
            ControllerStore::open_with_free_space_check(existing_root.clone(), runtime, |_| {
                Err("controller_disk_space_low")
            });
        assert_eq!(existing_result.err(), Some("controller_disk_space_low"));
        assert_eq!(
            fs::read(&marker).expect("read existing marker"),
            b"preserve"
        );
        assert_eq!(fs::read_dir(existing_root).unwrap().count(), 1);
    }

    #[test]
    fn controller_config_contains_only_stable_non_secret_runtime_identity() {
        let config = ControllerConfig {
            schema_version: CONFIG_SCHEMA_VERSION,
            controller_id: "0123456789abcdef0123456789abcdef".to_string(),
            cluster_initialized: true,
            database_port: 51_001,
            endpoint_port: 51_002,
            lan_address: None,
            tls_identity_provisioned: false,
        };
        let value = serde_json::to_value(config).expect("serialize controller config");
        let object = value.as_object().expect("controller config object");
        assert_eq!(object.len(), 7);
        assert!(object.contains_key("controllerId"));
        assert!(object.contains_key("databasePort"));
        assert!(object.contains_key("endpointPort"));
        assert!(object.contains_key("lanAddress"));
        assert_eq!(object["tlsIdentityProvisioned"], false);
        assert!(!object.contains_key("password"));
        assert!(!object.contains_key("databaseUrl"));
    }

    #[test]
    fn controller_config_rejects_partial_or_invalid_clusters() {
        let directory = tempfile::tempdir().expect("temporary Controller root");
        let path = directory.path().join("controller.json");
        let mut config = ControllerConfig {
            schema_version: CONFIG_SCHEMA_VERSION,
            controller_id: "0123456789abcdef0123456789abcdef".to_string(),
            cluster_initialized: false,
            database_port: 51_001,
            endpoint_port: 51_002,
            lan_address: None,
            tls_identity_provisioned: false,
        };
        config.save_atomic(&path).expect("write incomplete config");
        assert_eq!(
            ControllerConfig::load(&path).unwrap_err(),
            "controller_data_root_incomplete"
        );
        config.cluster_initialized = true;
        config.database_port = config.endpoint_port;
        config.save_atomic(&path).expect("write invalid config");
        assert_eq!(
            ControllerConfig::load(&path).unwrap_err(),
            "controller_config_invalid"
        );
    }

    #[test]
    fn atomic_controller_config_replaces_existing_state() {
        let directory = tempfile::tempdir().expect("temporary Controller root");
        let path = directory.path().join("controller.json");
        let config = ControllerConfig {
            schema_version: CONFIG_SCHEMA_VERSION,
            controller_id: "0123456789abcdef0123456789abcdef".to_string(),
            cluster_initialized: true,
            database_port: 51_001,
            endpoint_port: 51_002,
            lan_address: None,
            tls_identity_provisioned: false,
        };
        config.save_atomic(&path).expect("first atomic write");
        config.save_atomic(&path).expect("replace atomic state");
        assert_eq!(ControllerConfig::load(&path).unwrap().database_port, 51_001);
    }

    #[test]
    fn tls_identity_provisioned_marker_persists_without_secret_material() {
        let directory = tempfile::tempdir().expect("temporary Controller state");
        let mut store = configuration_store(directory.path(), 51_002);

        store
            .mark_tls_identity_provisioned()
            .expect("persist TLS identity marker");

        let persisted = ControllerConfig::load(&directory.path().join("controller.json"))
            .expect("reload Controller config");
        assert!(persisted.tls_identity_provisioned);
        let value = serde_json::to_value(persisted).expect("serialize persisted config");
        assert!(value.get("rootKey").is_none());
        assert!(value.get("leafKey").is_none());
    }

    #[test]
    fn dx05_config_migrates_without_changing_persisted_endpoint_port() {
        let directory = tempfile::tempdir().expect("temporary Controller root");
        let path = directory.path().join("controller.json");
        fs::write(
            &path,
            br#"{"schemaVersion":1,"controllerId":"0123456789abcdef0123456789abcdef","clusterInitialized":true,"databasePort":51001,"endpointPort":51002}"#,
        )
        .expect("write DX-05 config");

        let migrated = ControllerConfig::load(&path).expect("migrate DX-05 config");

        assert_eq!(migrated.schema_version, CONFIG_SCHEMA_VERSION);
        assert_eq!(migrated.endpoint_port, 51_002);
        assert_eq!(migrated.lan_address, None);
        assert!(!migrated.tls_identity_provisioned);
        let persisted: serde_json::Value =
            serde_json::from_slice(&fs::read(path).expect("read migrated config"))
                .expect("parse migrated config");
        assert_eq!(persisted["endpointPort"], 51_002);
        assert!(persisted["lanAddress"].is_null());
        assert_eq!(persisted["tlsIdentityProvisioned"], false);
    }

    #[test]
    fn configured_endpoint_can_be_explicitly_replaced_and_persisted() {
        let directory = tempfile::tempdir().expect("temporary Controller state");
        let mut store = configuration_store(directory.path(), 51_002);

        store
            .configure_https("192.0.2.10", 54_321)
            .expect("initial endpoint");
        store
            .configure_https("198.51.100.20", 54_322)
            .expect("explicit endpoint change");
        assert_eq!(
            store.config().lan_address,
            Some(Ipv4Addr::new(198, 51, 100, 20))
        );
        assert_eq!(store.config().endpoint_port, 54_322);
        assert!(!store.config().tls_identity_provisioned);
        let persisted = ControllerConfig::load(&directory.path().join("controller.json"))
            .expect("reconfigured endpoint persisted");
        assert_eq!(persisted.lan_address, Some(Ipv4Addr::new(198, 51, 100, 20)));
        assert_eq!(persisted.endpoint_port, 54_322);
    }

    #[test]
    fn port_only_endpoint_reconfiguration_preserves_address_and_persists_port() {
        let directory = tempfile::tempdir().expect("temporary Controller state");
        let mut store = configuration_store(directory.path(), 51_002);
        let address = "192.0.2.10";
        store
            .configure_https(address, 54_321)
            .expect("initial endpoint");
        store
            .configure_https(address, 54_322)
            .expect("port-only endpoint change");

        let persisted = ControllerConfig::load(&directory.path().join("controller.json"))
            .expect("port-only change persisted");
        assert_eq!(
            store.config().lan_address,
            Some(Ipv4Addr::new(192, 0, 2, 10))
        );
        assert_eq!(store.config().endpoint_port, 54_322);
        assert_eq!(persisted.lan_address, store.config().lan_address);
        assert_eq!(persisted.endpoint_port, 54_322);
    }

    #[test]
    fn endpoint_configuration_rejects_invalid_addresses_and_database_port() {
        let directory = tempfile::tempdir().expect("temporary Controller state");
        let mut store = configuration_store(directory.path(), 51_002);
        assert_eq!(
            store.configure_https("127.0.0.1", 54_321),
            Err("controller_lan_address_invalid")
        );
        assert_eq!(
            store.configure_https("192.0.2.10", 51_001),
            Err("controller_endpoint_port_in_use")
        );
        assert_eq!(store.config().lan_address, None);
        assert_eq!(store.config().endpoint_port, 51_002);
        let persisted = ControllerConfig::load(&directory.path().join("controller.json"))
            .expect("saved config remains unchanged");
        assert_eq!(persisted.lan_address, None);
        assert_eq!(persisted.endpoint_port, 51_002);
    }

    #[cfg(windows)]
    #[test]
    fn controller_secret_is_current_user_dpapi_protected_and_root_lock_is_exclusive() {
        let directory = tempfile::tempdir().expect("temporary Controller test root");
        let runtime = directory.path().join("runtime");
        for relative in [
            "threads-runtime/threads-runtime.exe",
            "postgresql/bin/initdb.exe",
            "postgresql/bin/postgres.exe",
            "postgresql/bin/pg_ctl.exe",
            "postgresql/bin/pg_isready.exe",
        ] {
            let path = runtime.join(relative);
            fs::create_dir_all(path.parent().expect("runtime parent"))
                .expect("create runtime directory");
            fs::write(path, b"synthetic runtime file").expect("write runtime placeholder");
        }

        let root = directory.path().join("Controller");
        let mut store = ControllerStore::open(root.clone(), runtime.clone())
            .expect("create current-user encrypted Controller state");
        let protected =
            fs::read(root.join("database-credential.dpapi")).expect("read protected credential");
        let clear = Zeroizing::new(
            crate::windows_crypto::unprotect_current_user(&protected).expect("DPAPI unprotect"),
        );
        assert_eq!(&*clear, store.database_password());
        assert!(
            !String::from_utf8_lossy(&fs::read(root.join("controller.json")).unwrap())
                .contains(std::str::from_utf8(store.database_password()).unwrap())
        );
        let postgres_data = root.join("postgresql");
        fs::create_dir_all(&postgres_data).expect("create initialized cluster fixture");
        fs::write(postgres_data.join("PG_VERSION"), "17\n").expect("write cluster version");
        store
            .mark_cluster_initialized()
            .expect("persist initialized cluster state");
        let initial = store.config().clone();
        assert!(matches!(
            ControllerStore::open(root.clone(), runtime.clone()),
            Err("controller_data_root_already_owned")
        ));
        drop(store);

        let reopened = ControllerStore::open(root, runtime).expect("reopen same Controller root");
        assert_eq!(reopened.config().controller_id, initial.controller_id);
        assert_eq!(reopened.config().database_port, initial.database_port);
        assert_eq!(reopened.config().endpoint_port, initial.endpoint_port);
    }
}
