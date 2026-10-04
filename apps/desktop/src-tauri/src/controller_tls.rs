use std::{
    fs::{self, OpenOptions},
    io::Write,
    io::{BufRead, BufReader},
    net::Ipv4Addr,
    net::TcpStream,
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use base64::{engine::general_purpose::STANDARD, Engine};
use rcgen::{
    BasicConstraints, CertificateParams, ExtendedKeyUsagePurpose, IsCa, Issuer, KeyPair,
    KeyUsagePurpose, PublicKeyData, SanType, PKCS_ECDSA_P256_SHA256,
};
use rustls::pki_types::CertificateDer;
use rustls::{ClientConfig, ClientConnection, RootCertStore, StreamOwned};
use serde::Serialize;
use sha2::{Digest, Sha256};
use time::OffsetDateTime;
use x509_parser::{certificate::X509Certificate, extensions::GeneralName, parse_x509_certificate};
use zeroize::Zeroizing;

const LEAF_LIFETIME_DAYS: i64 = 90;
const ROOT_LIFETIME_DAYS: i64 = 3650;
const RENEWAL_WINDOW_DAYS: i64 = 30;
const BACKDATE_SECONDS: i64 = 300;

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub(super) struct ControllerTlsSummary {
    pub configured: bool,
    pub lan_address: Option<String>,
    pub https_port: u16,
    pub public_https_origin: Option<String>,
    pub local_https_origin: String,
    pub root_fingerprint: Option<String>,
    pub leaf_expires_at: Option<String>,
}

struct Paths {
    tls_dir: PathBuf,
    root_cert: PathBuf,
    root_key: PathBuf,
    leaf_cert: PathBuf,
    leaf_key: PathBuf,
    fullchain: PathBuf,
    serving_dir: PathBuf,
    serving_key: PathBuf,
}

pub(super) struct PreparedLeafReissue {
    leaf_der: Vec<u8>,
    protected_key: Vec<u8>,
    fullchain: Vec<u8>,
    pub(super) root_fingerprint: String,
}

pub(super) struct LeafStateBackup {
    leaf_der: Vec<u8>,
    protected_key: Vec<u8>,
    fullchain: Vec<u8>,
}

impl Paths {
    fn new(data_root: &Path) -> Self {
        let tls_dir = data_root.join("tls");
        let serving_dir = tls_dir.join("serving");
        Self {
            root_cert: tls_dir.join("root-cert.der"),
            root_key: tls_dir.join("root-key.dpapi"),
            leaf_cert: tls_dir.join("leaf-cert.der"),
            leaf_key: tls_dir.join("leaf-key.dpapi"),
            fullchain: tls_dir.join("leaf-fullchain.pem"),
            serving_key: serving_dir.join("leaf-key.pem"),
            tls_dir,
            serving_dir,
        }
    }

    fn durable_files(&self) -> [&Path; 5] {
        [
            &self.root_cert,
            &self.root_key,
            &self.leaf_cert,
            &self.leaf_key,
            &self.fullchain,
        ]
    }
}

pub(super) fn provision_initial(
    data_root: &Path,
    lan_address: Ipv4Addr,
) -> Result<String, &'static str> {
    let paths = Paths::new(data_root);
    let exists = paths.durable_files().map(Path::exists);
    if exists.iter().all(|exists| !exists) {
        fs::create_dir_all(&paths.tls_dir).map_err(|_| "controller_tls_leaf_issue_failed")?;
        create_root(&paths)?;
        issue_leaf(&paths, lan_address)?;
    } else {
        validate_existing_or_renew(&paths, lan_address)?;
    }
    load_summary(&paths, lan_address)?
        .root_fingerprint
        .ok_or("controller_tls_identity_invalid")
}

pub(super) fn load_validate_or_renew(
    data_root: &Path,
    lan_address: Ipv4Addr,
) -> Result<String, &'static str> {
    let paths = Paths::new(data_root);
    validate_existing_or_renew(&paths, lan_address)?;
    load_summary(&paths, lan_address)?
        .root_fingerprint
        .ok_or("controller_tls_identity_invalid")
}

pub(super) fn validate_identity(
    data_root: &Path,
    lan_address: Ipv4Addr,
) -> Result<String, &'static str> {
    let paths = Paths::new(data_root);
    if paths.durable_files().iter().any(|path| !path.is_file()) {
        return Err("controller_tls_identity_invalid");
    }
    let root_der = fs::read(&paths.root_cert).map_err(|_| "controller_tls_identity_invalid")?;
    let protected_root_key =
        fs::read(&paths.root_key).map_err(|_| "controller_tls_identity_invalid")?;
    let root_key_bytes = Zeroizing::new(
        crate::windows_crypto::unprotect_current_user(&protected_root_key)
            .map_err(|_| "controller_tls_root_key_unprotect_failed")?,
    );
    let root_key = KeyPair::try_from(root_key_bytes.as_slice())
        .map_err(|_| "controller_tls_identity_invalid")?;
    if root_key.algorithm() != &PKCS_ECDSA_P256_SHA256 {
        return Err("controller_tls_identity_invalid");
    }
    validate_root(&root_der, root_key.subject_public_key_info().as_slice())?;
    validate_leaf_durable(&paths, &root_der, lan_address)?;
    fingerprint(&root_der)
}

pub(super) fn root_fingerprint(data_root: &Path) -> Result<String, &'static str> {
    let root_der =
        fs::read(Paths::new(data_root).root_cert).map_err(|_| "controller_tls_identity_invalid")?;
    validate_trust_anchor(&root_der).map_err(|_| "controller_tls_identity_invalid")?;
    fingerprint(&root_der)
}

pub(super) fn summary(
    data_root: &Path,
    lan_address: Option<Ipv4Addr>,
    https_port: u16,
) -> Result<ControllerTlsSummary, &'static str> {
    let Some(lan_address) = lan_address else {
        return Ok(ControllerTlsSummary {
            configured: false,
            lan_address: None,
            https_port,
            public_https_origin: None,
            local_https_origin: format!("https://127.0.0.1:{https_port}"),
            root_fingerprint: None,
            leaf_expires_at: None,
        });
    };
    let mut summary = load_summary(&Paths::new(data_root), lan_address)?;
    summary.https_port = https_port;
    summary.public_https_origin = Some(format!("https://{lan_address}:{https_port}"));
    summary.local_https_origin = format!("https://127.0.0.1:{https_port}");
    Ok(summary)
}

pub(super) fn root_certificate(
    data_root: &Path,
    lan_address: Ipv4Addr,
) -> Result<Vec<u8>, &'static str> {
    let paths = Paths::new(data_root);
    let root = fs::read(&paths.root_cert).map_err(|_| "controller_tls_identity_invalid")?;
    validate_trust_anchor(&root).map_err(|_| "controller_tls_identity_invalid")?;
    let leaf = fs::read(&paths.leaf_cert).map_err(|_| "controller_tls_identity_invalid")?;
    let root_certificate = parse_certificate(&root)?;
    validate_peer_leaf(&leaf, &root, lan_address)?;
    let leaf_certificate = parse_certificate(&leaf)?;
    if leaf_certificate.validity().not_after.timestamp() <= unix_now()
        || root_certificate.validity().not_after.timestamp() <= unix_now()
    {
        return Err("controller_tls_identity_invalid");
    }
    Ok(root)
}

pub(super) fn materialize_leaf_key(
    data_root: &Path,
    lan_address: Ipv4Addr,
) -> Result<(PathBuf, PathBuf), &'static str> {
    let paths = Paths::new(data_root);
    if paths.durable_files().iter().any(|path| !path.is_file()) {
        return Err("controller_tls_identity_invalid");
    }
    let root_der = fs::read(&paths.root_cert).map_err(|_| "controller_tls_identity_invalid")?;
    validate_trust_anchor(&root_der).map_err(|_| "controller_tls_identity_invalid")?;
    let (_, plaintext) = validate_leaf_durable(&paths, &root_der, lan_address)?;
    if paths.serving_key.exists() {
        fs::remove_file(&paths.serving_key)
            .map_err(|_| "controller_tls_leaf_materialize_failed")?;
    }
    fs::create_dir_all(&paths.serving_dir).map_err(|_| "controller_tls_leaf_materialize_failed")?;
    restrict_current_user(&paths.serving_dir, true)?;
    let key =
        KeyPair::try_from(plaintext.as_slice()).map_err(|_| "controller_tls_identity_invalid")?;
    let pem = Zeroizing::new(key.serialize_pem());
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&paths.serving_key)
        .map_err(|_| "controller_tls_leaf_materialize_failed")?;
    if file
        .write_all(pem.as_bytes())
        .and_then(|()| file.sync_all())
        .is_err()
    {
        drop(file);
        let _ = fs::remove_file(&paths.serving_key);
        return Err("controller_tls_leaf_materialize_failed");
    }
    drop(file);
    if let Err(error) = restrict_current_user(&paths.serving_key, false) {
        let _ = fs::remove_file(&paths.serving_key);
        return Err(error);
    }
    Ok((paths.fullchain, paths.serving_key))
}

pub(super) fn cleanup_leaf_key(data_root: &Path) -> Result<(), &'static str> {
    let serving_key = Paths::new(data_root).serving_key;
    if serving_key.exists() {
        fs::remove_file(serving_key).map_err(|_| "controller_tls_leaf_cleanup_failed")?;
    }
    Ok(())
}

pub(super) fn fingerprint(root_der: &[u8]) -> Result<String, &'static str> {
    parse_certificate(root_der).map_err(|_| "controller_tls_identity_invalid")?;
    let digest = Sha256::digest(root_der);
    Ok(format!("SHA256:{}", lower_hex(&digest)))
}

pub(super) fn validate_trust_anchor(root_der: &[u8]) -> Result<(), &'static str> {
    let root = parse_certificate(root_der)?;
    if !is_p256_key(&root) {
        return Err("controller_trust_store_invalid");
    }
    validate_root(root_der, root.tbs_certificate.subject_pki.raw)
        .map_err(|_| "controller_trust_store_invalid")
}

pub(super) fn validate_peer_leaf(
    leaf_der: &[u8],
    root_der: &[u8],
    lan_address: Ipv4Addr,
) -> Result<(), &'static str> {
    let leaf = parse_certificate(leaf_der)?;
    if !is_p256_key(&leaf) {
        return Err("controller_tls_identity_invalid");
    }
    validate_leaf(
        leaf_der,
        root_der,
        leaf.tbs_certificate.subject_pki.raw,
        lan_address,
    )
}

pub(super) fn readiness_probe(root_der: &[u8], port: u16) -> bool {
    let mut roots = RootCertStore::empty();
    if roots.add(CertificateDer::from(root_der.to_vec())).is_err() {
        return false;
    }
    let config =
        ClientConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
            .with_safe_default_protocol_versions();
    let Ok(config) = config else {
        return false;
    };
    let config = config.with_root_certificates(roots).with_no_client_auth();
    let server_name = rustls::pki_types::ServerName::IpAddress(Ipv4Addr::LOCALHOST.into());
    let Ok(connection) = ClientConnection::new(Arc::new(config), server_name) else {
        return false;
    };
    let address = std::net::SocketAddr::from((Ipv4Addr::LOCALHOST, port));
    let Ok(stream) = TcpStream::connect_timeout(&address, Duration::from_secs(1)) else {
        return false;
    };
    if stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .is_err()
        || stream
            .set_write_timeout(Some(Duration::from_secs(2)))
            .is_err()
    {
        return false;
    }
    let mut stream = StreamOwned::new(connection, stream);
    while stream.conn.is_handshaking() {
        if stream.conn.complete_io(&mut stream.sock).is_err() {
            return false;
        }
    }
    if stream
        .write_all(b"GET /ready HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        .is_err()
    {
        return false;
    }
    let mut status = String::new();
    BufReader::new(&mut stream)
        .read_line(&mut status)
        .is_ok_and(|_| status.starts_with("HTTP/1.1 200") || status.starts_with("HTTP/1.0 200"))
}

fn create_root(paths: &Paths) -> Result<(), &'static str> {
    let now = OffsetDateTime::now_utc();
    let mut params = CertificateParams::default();
    params.is_ca = IsCa::Ca(BasicConstraints::Constrained(0));
    params.key_usages = vec![KeyUsagePurpose::KeyCertSign, KeyUsagePurpose::CrlSign];
    params.not_before = now - time::Duration::seconds(BACKDATE_SECONDS);
    params.not_after = now + time::Duration::days(ROOT_LIFETIME_DAYS);
    let key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256)
        .map_err(|_| "controller_tls_identity_invalid")?;
    let certificate = params
        .self_signed(&key)
        .map_err(|_| "controller_tls_identity_invalid")?;
    let root_der = certificate.der().as_ref();
    validate_root(root_der, key.subject_public_key_info().as_slice())?;
    let private_key = Zeroizing::new(key.serialize_der());
    let protected = crate::windows_crypto::protect_current_user(&private_key)
        .map_err(|_| "controller_tls_identity_invalid")?;
    write_atomic(&paths.root_cert, root_der)?;
    write_atomic(&paths.root_key, &protected)?;
    Ok(())
}

fn issue_leaf(paths: &Paths, lan_address: Ipv4Addr) -> Result<(), &'static str> {
    let candidate = generate_leaf(paths, lan_address)?;
    commit_leaf_material(paths, candidate).map(|_| ())
}

fn generate_leaf(
    paths: &Paths,
    lan_address: Ipv4Addr,
) -> Result<PreparedLeafReissue, &'static str> {
    let root_der = fs::read(&paths.root_cert).map_err(|_| "controller_tls_identity_invalid")?;
    let protected_root_key =
        fs::read(&paths.root_key).map_err(|_| "controller_tls_identity_invalid")?;
    let root_key_bytes = Zeroizing::new(
        crate::windows_crypto::unprotect_current_user(&protected_root_key)
            .map_err(|_| "controller_tls_root_key_unprotect_failed")?,
    );
    let root_key = KeyPair::try_from(root_key_bytes.as_slice())
        .map_err(|_| "controller_tls_identity_invalid")?;
    if root_key.algorithm() != &PKCS_ECDSA_P256_SHA256 {
        return Err("controller_tls_identity_invalid");
    }
    validate_root(&root_der, root_key.subject_public_key_info().as_slice())?;
    let issuer = Issuer::from_ca_cert_der(&CertificateDer::from(root_der.clone()), root_key)
        .map_err(|_| "controller_tls_identity_invalid")?;
    let now = OffsetDateTime::now_utc();
    let mut params = CertificateParams::default();
    params.is_ca = IsCa::ExplicitNoCa;
    params.subject_alt_names = vec![
        SanType::IpAddress(lan_address.into()),
        SanType::IpAddress(Ipv4Addr::LOCALHOST.into()),
    ];
    params.key_usages = vec![KeyUsagePurpose::DigitalSignature];
    params.extended_key_usages = vec![ExtendedKeyUsagePurpose::ServerAuth];
    params.not_before = now - time::Duration::seconds(BACKDATE_SECONDS);
    params.not_after = now + time::Duration::days(LEAF_LIFETIME_DAYS);
    let leaf_key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256)
        .map_err(|_| "controller_tls_leaf_issue_failed")?;
    let leaf = params
        .signed_by(&leaf_key, &issuer)
        .map_err(|_| "controller_tls_leaf_issue_failed")?;
    let leaf_der = leaf.der().as_ref();
    validate_leaf(
        leaf_der,
        &root_der,
        leaf_key.subject_public_key_info().as_slice(),
        lan_address,
    )?;
    let private_key = Zeroizing::new(leaf_key.serialize_der());
    let protected = crate::windows_crypto::protect_current_user(&private_key)
        .map_err(|_| "controller_tls_leaf_issue_failed")?;
    let fullchain = format!(
        "{}{}",
        certificate_pem("CERTIFICATE", leaf_der),
        certificate_pem("CERTIFICATE", &root_der)
    );
    Ok(PreparedLeafReissue {
        leaf_der: leaf_der.to_vec(),
        protected_key: protected,
        fullchain: fullchain.into_bytes(),
        root_fingerprint: fingerprint(&root_der)?,
    })
}

pub(super) fn prepare_leaf_reissue(
    data_root: &Path,
    current_lan_address: Ipv4Addr,
    requested_lan_address: Ipv4Addr,
) -> Result<PreparedLeafReissue, &'static str> {
    let paths = Paths::new(data_root);
    if paths.durable_files().iter().any(|path| !path.is_file()) {
        return Err("controller_tls_identity_invalid");
    }
    let root_der = fs::read(&paths.root_cert).map_err(|_| "controller_tls_identity_invalid")?;
    let root_key_protected =
        fs::read(&paths.root_key).map_err(|_| "controller_tls_identity_invalid")?;
    let root_key_bytes = Zeroizing::new(
        crate::windows_crypto::unprotect_current_user(&root_key_protected)
            .map_err(|_| "controller_tls_root_key_unprotect_failed")?,
    );
    let root_key = KeyPair::try_from(root_key_bytes.as_slice())
        .map_err(|_| "controller_tls_identity_invalid")?;
    if root_key.algorithm() != &PKCS_ECDSA_P256_SHA256 {
        return Err("controller_tls_identity_invalid");
    }
    validate_root(&root_der, root_key.subject_public_key_info().as_slice())?;
    validate_leaf_durable(&paths, &root_der, current_lan_address)?;
    generate_leaf(&paths, requested_lan_address)
}

pub(super) fn commit_leaf_reissue(
    data_root: &Path,
    candidate: PreparedLeafReissue,
) -> Result<LeafStateBackup, &'static str> {
    let paths = Paths::new(data_root);
    if paths.durable_files().iter().any(|path| !path.is_file()) {
        return Err("controller_tls_identity_invalid");
    }
    let backup = LeafStateBackup {
        leaf_der: fs::read(&paths.leaf_cert).map_err(|_| "controller_tls_identity_invalid")?,
        protected_key: fs::read(&paths.leaf_key).map_err(|_| "controller_tls_identity_invalid")?,
        fullchain: fs::read(&paths.fullchain).map_err(|_| "controller_tls_identity_invalid")?,
    };
    commit_leaf_material(&paths, candidate)?;
    Ok(backup)
}

pub(super) fn restore_leaf_state(
    data_root: &Path,
    backup: LeafStateBackup,
) -> Result<(), &'static str> {
    let paths = Paths::new(data_root);
    write_atomic(&paths.leaf_cert, &backup.leaf_der)
        .and_then(|()| write_atomic(&paths.leaf_key, &backup.protected_key))
        .and_then(|()| write_atomic(&paths.fullchain, &backup.fullchain))
}

#[cfg(test)]
pub(super) fn reissue_leaf_same_root(
    data_root: &Path,
    current_lan_address: Ipv4Addr,
    requested_lan_address: Ipv4Addr,
) -> Result<String, &'static str> {
    let candidate = prepare_leaf_reissue(data_root, current_lan_address, requested_lan_address)?;
    let fingerprint = candidate.root_fingerprint.clone();
    commit_leaf_reissue(data_root, candidate)?;
    Ok(fingerprint)
}

fn commit_leaf_material(
    paths: &Paths,
    candidate: PreparedLeafReissue,
) -> Result<Option<LeafStateBackup>, &'static str> {
    let present = [
        paths.leaf_cert.is_file(),
        paths.leaf_key.is_file(),
        paths.fullchain.is_file(),
    ];
    if present.iter().any(|value| *value) && present.iter().any(|value| !*value) {
        return Err("controller_tls_identity_invalid");
    }
    let backup = if present.iter().all(|value| *value) {
        Some(LeafStateBackup {
            leaf_der: fs::read(&paths.leaf_cert).map_err(|_| "controller_tls_identity_invalid")?,
            protected_key: fs::read(&paths.leaf_key)
                .map_err(|_| "controller_tls_identity_invalid")?,
            fullchain: fs::read(&paths.fullchain).map_err(|_| "controller_tls_identity_invalid")?,
        })
    } else {
        None
    };
    let write_result = write_atomic(&paths.leaf_cert, &candidate.leaf_der)
        .and_then(|()| write_atomic(&paths.leaf_key, &candidate.protected_key))
        .and_then(|()| write_atomic(&paths.fullchain, &candidate.fullchain));
    if let Err(error) = write_result {
        let rollback = if let Some(backup) = backup.as_ref() {
            write_atomic(&paths.leaf_cert, &backup.leaf_der)
                .and_then(|()| write_atomic(&paths.leaf_key, &backup.protected_key))
                .and_then(|()| write_atomic(&paths.fullchain, &backup.fullchain))
        } else {
            [&paths.leaf_cert, &paths.leaf_key, &paths.fullchain]
                .into_iter()
                .try_for_each(|path| {
                    if path.exists() {
                        fs::remove_file(path).map_err(|_| "controller_tls_identity_invalid")?;
                    }
                    Ok(())
                })
        };
        if rollback.is_err() {
            return Err("controller_tls_identity_invalid");
        }
        return Err(error);
    }
    Ok(backup)
}

fn validate_existing_or_renew(paths: &Paths, lan_address: Ipv4Addr) -> Result<(), &'static str> {
    if paths.durable_files().iter().any(|path| !path.is_file()) {
        return Err("controller_tls_identity_invalid");
    }
    let root_der = fs::read(&paths.root_cert).map_err(|_| "controller_tls_identity_invalid")?;
    let root_key_protected =
        fs::read(&paths.root_key).map_err(|_| "controller_tls_identity_invalid")?;
    let root_key_bytes = Zeroizing::new(
        crate::windows_crypto::unprotect_current_user(&root_key_protected)
            .map_err(|_| "controller_tls_root_key_unprotect_failed")?,
    );
    let root_key = KeyPair::try_from(root_key_bytes.as_slice())
        .map_err(|_| "controller_tls_identity_invalid")?;
    if root_key.algorithm() != &PKCS_ECDSA_P256_SHA256 {
        return Err("controller_tls_identity_invalid");
    }
    validate_root(&root_der, root_key.subject_public_key_info().as_slice())?;
    let (leaf_der, _) = validate_leaf_durable(paths, &root_der, lan_address)?;
    let leaf_certificate = parse_certificate(&leaf_der)?;
    let root_certificate = parse_certificate(&root_der)?;
    let now = unix_now();
    let expires = leaf_certificate.validity().not_after.timestamp();
    if expires <= now {
        return Err("controller_tls_identity_invalid");
    }
    if expires - now <= time::Duration::days(RENEWAL_WINDOW_DAYS).whole_seconds() {
        issue_leaf(paths, lan_address)?;
    }
    if root_certificate.validity().not_after.timestamp() <= now {
        return Err("controller_tls_identity_invalid");
    }
    Ok(())
}

fn validate_leaf_durable(
    paths: &Paths,
    root_der: &[u8],
    lan_address: Ipv4Addr,
) -> Result<(Vec<u8>, Zeroizing<Vec<u8>>), &'static str> {
    let leaf_der = fs::read(&paths.leaf_cert).map_err(|_| "controller_tls_identity_invalid")?;
    let leaf_key_protected =
        fs::read(&paths.leaf_key).map_err(|_| "controller_tls_identity_invalid")?;
    let leaf_key_bytes = Zeroizing::new(
        crate::windows_crypto::unprotect_current_user(&leaf_key_protected)
            .map_err(|_| "controller_tls_leaf_issue_failed")?,
    );
    let leaf_key = KeyPair::try_from(leaf_key_bytes.as_slice())
        .map_err(|_| "controller_tls_identity_invalid")?;
    if leaf_key.algorithm() != &PKCS_ECDSA_P256_SHA256 {
        return Err("controller_tls_identity_invalid");
    }
    validate_leaf(
        &leaf_der,
        root_der,
        leaf_key.subject_public_key_info().as_slice(),
        lan_address,
    )?;
    let expected_fullchain = format!(
        "{}{}",
        certificate_pem("CERTIFICATE", &leaf_der),
        certificate_pem("CERTIFICATE", root_der)
    );
    if fs::read(&paths.fullchain).map_err(|_| "controller_tls_identity_invalid")?
        != expected_fullchain.as_bytes()
    {
        return Err("controller_tls_identity_invalid");
    }
    Ok((leaf_der, leaf_key_bytes))
}

fn load_summary(
    paths: &Paths,
    lan_address: Ipv4Addr,
) -> Result<ControllerTlsSummary, &'static str> {
    if paths.durable_files().iter().any(|path| !path.is_file()) {
        return Err("controller_tls_identity_invalid");
    }
    let root = fs::read(&paths.root_cert).map_err(|_| "controller_tls_identity_invalid")?;
    validate_trust_anchor(&root).map_err(|_| "controller_tls_identity_invalid")?;
    let (leaf_der, _) = validate_leaf_durable(paths, &root, lan_address)?;
    let certificate = parse_certificate(&leaf_der)?;
    let expiry = certificate.validity().not_after.to_datetime();
    let expiry = expiry
        .format(&time::format_description::well_known::Rfc3339)
        .map_err(|_| "controller_tls_identity_invalid")?;
    Ok(ControllerTlsSummary {
        configured: true,
        lan_address: Some(lan_address.to_string()),
        https_port: 0,
        public_https_origin: None,
        local_https_origin: String::new(),
        root_fingerprint: Some(fingerprint(&root)?),
        leaf_expires_at: Some(expiry),
    })
}

fn validate_root(der: &[u8], public_key: &[u8]) -> Result<(), &'static str> {
    let certificate = parse_certificate(der)?;
    if certificate.tbs_certificate.subject != certificate.tbs_certificate.issuer
        || certificate.tbs_certificate.subject_pki.raw != public_key
        || certificate.verify_signature(None).is_err()
        || !is_ecdsa_sha256(&certificate)
        || !is_p256_key(&certificate)
    {
        return Err("controller_tls_identity_invalid");
    }
    let constraints = certificate
        .basic_constraints()
        .map_err(|_| "controller_tls_identity_invalid")?
        .ok_or("controller_tls_identity_invalid")?
        .value;
    let usage = certificate
        .key_usage()
        .map_err(|_| "controller_tls_identity_invalid")?
        .ok_or("controller_tls_identity_invalid")?
        .value;
    if !constraints.ca
        || constraints.path_len_constraint != Some(0)
        || usage.flags != (1 << 5 | 1 << 6)
        || certificate.validity().not_before.timestamp() > unix_now()
        || certificate.validity().not_after.timestamp() <= unix_now()
    {
        return Err("controller_tls_identity_invalid");
    }
    Ok(())
}

fn validate_leaf(
    der: &[u8],
    root_der: &[u8],
    public_key: &[u8],
    lan_address: Ipv4Addr,
) -> Result<(), &'static str> {
    let certificate = parse_certificate(der)?;
    let root = parse_certificate(root_der)?;
    let constraints = certificate
        .basic_constraints()
        .map_err(|_| "controller_tls_identity_invalid")?
        .ok_or("controller_tls_identity_invalid")?
        .value;
    let usage = certificate
        .key_usage()
        .map_err(|_| "controller_tls_identity_invalid")?
        .ok_or("controller_tls_identity_invalid")?
        .value;
    let eku = certificate
        .extended_key_usage()
        .map_err(|_| "controller_tls_identity_invalid")?
        .ok_or("controller_tls_identity_invalid")?
        .value;
    let sans = certificate
        .subject_alternative_name()
        .map_err(|_| "controller_tls_identity_invalid")?
        .ok_or("controller_tls_identity_invalid")?
        .value
        .general_names
        .iter()
        .map(|name| match name {
            GeneralName::IPAddress(address) if address.len() == 4 => Ok(Ipv4Addr::new(
                address[0], address[1], address[2], address[3],
            )),
            _ => Err("controller_tls_identity_invalid"),
        })
        .collect::<Result<Vec<_>, _>>()?;
    let mut expected = vec![lan_address, Ipv4Addr::LOCALHOST];
    let mut actual = sans;
    expected.sort();
    actual.sort();
    if certificate.tbs_certificate.subject_pki.raw != public_key
        || certificate
            .verify_signature(Some(&root.tbs_certificate.subject_pki))
            .is_err()
        || !is_ecdsa_sha256(&certificate)
        || !is_p256_key(&certificate)
        || constraints.ca
        || constraints.path_len_constraint.is_some()
        || usage.flags != 1
        || !eku.server_auth
        || eku.any
        || eku.client_auth
        || eku.code_signing
        || eku.email_protection
        || eku.time_stamping
        || eku.ocsp_signing
        || !eku.other.is_empty()
        || actual != expected
        || certificate.validity().not_before.timestamp() > unix_now()
        || certificate.validity().not_after.timestamp() <= unix_now()
    {
        return Err("controller_tls_identity_invalid");
    }
    Ok(())
}

fn is_ecdsa_sha256(certificate: &X509Certificate<'_>) -> bool {
    certificate.signature_algorithm.algorithm.to_id_string() == "1.2.840.10045.4.3.2"
}

fn is_p256_key(certificate: &X509Certificate<'_>) -> bool {
    let algorithm = &certificate.tbs_certificate.subject_pki.algorithm;
    algorithm.algorithm.to_id_string() == "1.2.840.10045.2.1"
        && algorithm
            .parameters
            .as_ref()
            .and_then(|parameters| parameters.as_oid().ok())
            .is_some_and(|curve| curve.to_id_string() == "1.2.840.10045.3.1.7")
}

fn parse_certificate(der: &[u8]) -> Result<X509Certificate<'_>, &'static str> {
    let (remaining, certificate) =
        parse_x509_certificate(der).map_err(|_| "controller_tls_identity_invalid")?;
    if !remaining.is_empty() {
        return Err("controller_tls_identity_invalid");
    }
    Ok(certificate)
}

fn certificate_pem(label: &str, der: &[u8]) -> String {
    let encoded = STANDARD.encode(der);
    let mut output = format!("-----BEGIN {label}-----\n");
    for chunk in encoded.as_bytes().chunks(64) {
        output.push_str(std::str::from_utf8(chunk).expect("base64 is ASCII"));
        output.push('\n');
    }
    output.push_str(&format!("-----END {label}-----\n"));
    output
}

fn write_atomic(path: &Path, bytes: &[u8]) -> Result<(), &'static str> {
    let parent = path.parent().ok_or("controller_tls_identity_invalid")?;
    let mut temporary =
        tempfile::NamedTempFile::new_in(parent).map_err(|_| "controller_tls_identity_invalid")?;
    temporary
        .write_all(bytes)
        .and_then(|()| temporary.flush())
        .and_then(|()| temporary.as_file().sync_all())
        .map_err(|_| "controller_tls_identity_invalid")?;
    temporary
        .persist(path)
        .map_err(|_| "controller_tls_identity_invalid")?;
    Ok(())
}

fn lower_hex(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        output.push(HEX[(byte >> 4) as usize] as char);
        output.push(HEX[(byte & 0x0f) as usize] as char);
    }
    output
}

fn unix_now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or(Duration::ZERO)
        .as_secs() as i64
}

#[cfg(windows)]
fn restrict_current_user(path: &Path, directory: bool) -> Result<(), &'static str> {
    use std::{
        os::windows::ffi::{OsStrExt, OsStringExt},
        process::Command,
    };
    use windows_sys::Win32::{
        Foundation::{CloseHandle, LocalFree, HANDLE},
        Security::Authorization::ConvertSidToStringSidW,
        Security::{GetTokenInformation, TokenUser, TOKEN_QUERY, TOKEN_USER},
        System::Threading::{GetCurrentProcess, OpenProcessToken},
    };

    let mut token: HANDLE = std::ptr::null_mut();
    if unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) } == 0 {
        return Err("controller_tls_leaf_materialize_failed");
    }
    let mut required = 0u32;
    unsafe {
        GetTokenInformation(token, TokenUser, std::ptr::null_mut(), 0, &mut required);
    }
    let mut token_data =
        vec![0usize; required.div_ceil(std::mem::size_of::<usize>() as u32) as usize];
    let token_ok = unsafe {
        GetTokenInformation(
            token,
            TokenUser,
            token_data.as_mut_ptr().cast(),
            (token_data.len() * std::mem::size_of::<usize>()) as u32,
            &mut required,
        )
    };
    if token_ok == 0 {
        unsafe { CloseHandle(token) };
        return Err("controller_tls_leaf_materialize_failed");
    }
    let user = unsafe { &*token_data.as_ptr().cast::<TOKEN_USER>() };
    let mut sid_text = std::ptr::null_mut();
    let sid_ok = unsafe { ConvertSidToStringSidW(user.User.Sid, &mut sid_text) };
    unsafe { CloseHandle(token) };
    if sid_ok == 0 || sid_text.is_null() {
        return Err("controller_tls_leaf_materialize_failed");
    }
    let mut length = 0usize;
    unsafe {
        while *sid_text.add(length) != 0 {
            length += 1;
        }
    }
    let sid = String::from_utf16_lossy(unsafe { std::slice::from_raw_parts(sid_text, length) });
    unsafe { LocalFree(sid_text.cast()) };
    let path_wide: Vec<u16> = path.as_os_str().encode_wide().chain([0]).collect();
    let rights = if directory { "(OI)(CI)(F)" } else { "(F)" };
    let status = Command::new("icacls.exe")
        .arg(std::ffi::OsString::from_wide(
            &path_wide[..path_wide.len() - 1],
        ))
        .arg("/inheritance:r")
        .arg("/grant:r")
        .arg(format!("*{sid}:{rights}"))
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .map_err(|_| "controller_tls_leaf_materialize_failed")?;
    if status.success() {
        Ok(())
    } else {
        Err("controller_tls_leaf_materialize_failed")
    }
}

#[cfg(not(windows))]
fn restrict_current_user(_path: &Path, _directory: bool) -> Result<(), &'static str> {
    Err("controller_platform_unsupported")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn root_material() -> (Vec<u8>, Vec<u8>) {
        let now = OffsetDateTime::now_utc();
        let mut params = CertificateParams::default();
        params.is_ca = IsCa::Ca(BasicConstraints::Constrained(0));
        params.key_usages = vec![KeyUsagePurpose::KeyCertSign, KeyUsagePurpose::CrlSign];
        params.not_before = now - time::Duration::minutes(5);
        params.not_after = now + time::Duration::days(ROOT_LIFETIME_DAYS);
        let key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).expect("P-256 root key");
        let certificate = params.self_signed(&key).expect("self-signed root");
        (certificate.der().as_ref().to_vec(), key.serialize_der())
    }

    fn leaf_material(
        root_der: &[u8],
        root_key_der: &[u8],
        san: Ipv4Addr,
        not_before: OffsetDateTime,
        not_after: OffsetDateTime,
    ) -> (Vec<u8>, Vec<u8>) {
        let key = KeyPair::try_from(root_key_der).expect("parse root key");
        let issuer = Issuer::from_ca_cert_der(&CertificateDer::from(root_der.to_vec()), key)
            .expect("parse Controller issuer");
        let mut params = CertificateParams::default();
        params.is_ca = IsCa::ExplicitNoCa;
        params.subject_alt_names = vec![
            SanType::IpAddress(san.into()),
            SanType::IpAddress(Ipv4Addr::LOCALHOST.into()),
        ];
        params.key_usages = vec![KeyUsagePurpose::DigitalSignature];
        params.extended_key_usages = vec![ExtendedKeyUsagePurpose::ServerAuth];
        params.not_before = not_before;
        params.not_after = not_after;
        let leaf_key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).expect("P-256 leaf key");
        let leaf = params
            .signed_by(&leaf_key, &issuer)
            .expect("Controller-signed leaf");
        (leaf.der().as_ref().to_vec(), leaf_key.serialize_der())
    }

    #[test]
    fn fingerprint_is_sha256_of_the_root_der() {
        let (root, _) = root_material();
        let expected = format!("SHA256:{}", lower_hex(&Sha256::digest(&root)));
        assert_eq!(fingerprint(&root).expect("root fingerprint"), expected);
    }

    #[test]
    fn leaf_key_and_exact_ip_sans_validate_under_controller_root() {
        let (root, root_key) = root_material();
        let address = Ipv4Addr::new(192, 0, 2, 10);
        let now = OffsetDateTime::now_utc();
        let (leaf, leaf_key) = leaf_material(
            &root,
            &root_key,
            address,
            now - time::Duration::minutes(5),
            now + time::Duration::days(LEAF_LIFETIME_DAYS),
        );
        let parsed_key = KeyPair::try_from(leaf_key.as_slice()).expect("leaf PKCS#8");
        validate_leaf(
            &leaf,
            &root,
            parsed_key.subject_public_key_info().as_slice(),
            address,
        )
        .expect("leaf profile valid");
        let unrelated_key =
            KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).expect("independent P-256 key");
        assert_eq!(
            validate_leaf(
                &leaf,
                &root,
                unrelated_key.subject_public_key_info().as_slice(),
                address,
            ),
            Err("controller_tls_identity_invalid")
        );
    }

    #[test]
    fn wrong_san_fails_validation() {
        let (root, root_key) = root_material();
        let configured = Ipv4Addr::new(192, 0, 2, 10);
        let actual = Ipv4Addr::new(192, 0, 2, 11);
        let now = OffsetDateTime::now_utc();
        let (leaf, leaf_key) = leaf_material(
            &root,
            &root_key,
            actual,
            now - time::Duration::minutes(5),
            now + time::Duration::days(LEAF_LIFETIME_DAYS),
        );
        let key = KeyPair::try_from(leaf_key.as_slice()).expect("leaf PKCS#8");
        assert_eq!(
            validate_leaf(
                &leaf,
                &root,
                key.subject_public_key_info().as_slice(),
                configured,
            ),
            Err("controller_tls_identity_invalid")
        );
    }

    #[test]
    fn expired_and_not_yet_valid_leafs_fail_validation() {
        let (root, root_key) = root_material();
        let now = OffsetDateTime::now_utc();
        let address = Ipv4Addr::new(192, 0, 2, 10);
        for (not_before, not_after) in [
            (
                now - time::Duration::days(2),
                now - time::Duration::seconds(1),
            ),
            (
                now + time::Duration::hours(1),
                now + time::Duration::days(LEAF_LIFETIME_DAYS),
            ),
        ] {
            let (leaf, leaf_key) = leaf_material(&root, &root_key, address, not_before, not_after);
            let key = KeyPair::try_from(leaf_key.as_slice()).expect("leaf PKCS#8");
            assert_eq!(
                validate_leaf(
                    &leaf,
                    &root,
                    key.subject_public_key_info().as_slice(),
                    address,
                ),
                Err("controller_tls_identity_invalid")
            );
        }
    }

    #[test]
    fn wrong_root_fails_leaf_chain_validation() {
        let (root, root_key) = root_material();
        let (wrong_root, _) = root_material();
        let now = OffsetDateTime::now_utc();
        let address = Ipv4Addr::new(192, 0, 2, 10);
        let (leaf, leaf_key) = leaf_material(
            &root,
            &root_key,
            address,
            now - time::Duration::minutes(5),
            now + time::Duration::days(LEAF_LIFETIME_DAYS),
        );
        let key = KeyPair::try_from(leaf_key.as_slice()).expect("leaf PKCS#8");
        assert_eq!(
            validate_leaf(
                &leaf,
                &wrong_root,
                key.subject_public_key_info().as_slice(),
                address,
            ),
            Err("controller_tls_identity_invalid")
        );
    }

    #[cfg(windows)]
    #[test]
    fn windows_dpapi_identity_persists_and_tampered_root_key_never_replaces_root() {
        let directory = tempfile::tempdir().expect("temporary TLS state");
        let address = Ipv4Addr::new(192, 0, 2, 10);
        let fingerprint_before =
            provision_initial(directory.path(), address).expect("provision root");
        let paths = Paths::new(directory.path());
        let root_before = fs::read(&paths.root_cert).expect("read root DER");
        let protected = fs::read(&paths.root_key).expect("read protected root key");
        let clear = Zeroizing::new(
            crate::windows_crypto::unprotect_current_user(&protected).expect("DPAPI unprotect"),
        );
        let reparsed = KeyPair::try_from(clear.as_slice()).expect("root PKCS#8");
        assert_eq!(
            reparsed.algorithm(),
            &PKCS_ECDSA_P256_SHA256,
            "DPAPI CurrentUser round trip retains P-256 PKCS#8"
        );
        assert_eq!(
            load_validate_or_renew(directory.path(), address).expect("load existing root"),
            fingerprint_before
        );
        assert_eq!(fs::read(&paths.root_cert).unwrap(), root_before);

        fs::write(&paths.root_key, b"tampered DPAPI blob").expect("tamper protected root key");
        assert_eq!(
            load_validate_or_renew(directory.path(), address),
            Err("controller_tls_root_key_unprotect_failed")
        );
        assert_eq!(fs::read(&paths.root_cert).unwrap(), root_before);
    }

    #[cfg(windows)]
    #[test]
    fn same_root_leaf_renewal_preserves_fingerprint_and_replaces_leaf() {
        let directory = tempfile::tempdir().expect("temporary TLS state");
        let address = Ipv4Addr::new(192, 0, 2, 10);
        let fingerprint_before =
            provision_initial(directory.path(), address).expect("initial provision");
        let paths = Paths::new(directory.path());
        let root_der = fs::read(&paths.root_cert).expect("root DER");
        let protected_root_key = fs::read(&paths.root_key).expect("DPAPI root key");
        let root_key = Zeroizing::new(
            crate::windows_crypto::unprotect_current_user(&protected_root_key)
                .expect("unprotect root key for fixture issuance"),
        );
        let now = OffsetDateTime::now_utc();
        let (expiring_leaf, expiring_leaf_key) = leaf_material(
            &root_der,
            &root_key,
            address,
            now - time::Duration::minutes(5),
            now + time::Duration::days(20),
        );
        let protected_leaf_key = crate::windows_crypto::protect_current_user(&expiring_leaf_key)
            .expect("protect expiring leaf key");
        let fullchain = format!(
            "{}{}",
            certificate_pem("CERTIFICATE", &expiring_leaf),
            certificate_pem("CERTIFICATE", &root_der)
        );
        fs::write(&paths.leaf_cert, &expiring_leaf).expect("install expiring leaf fixture");
        fs::write(&paths.leaf_key, protected_leaf_key).expect("install protected leaf fixture");
        fs::write(&paths.fullchain, fullchain).expect("install leaf chain fixture");

        let fingerprint_after =
            load_validate_or_renew(directory.path(), address).expect("renew expiring leaf");
        let renewed_leaf = fs::read(&paths.leaf_cert).expect("renewed leaf DER");
        assert_eq!(fingerprint_after, fingerprint_before);
        assert_ne!(renewed_leaf, expiring_leaf);
        let renewed = parse_certificate(&renewed_leaf).expect("parse renewed leaf");
        assert!(renewed.validity().not_after.timestamp() - unix_now() > 80 * 24 * 60 * 60);
        assert_eq!(
            fs::read(&paths.root_cert).expect("root DER after renewal"),
            root_der
        );
    }

    #[cfg(windows)]
    #[test]
    fn same_root_endpoint_reissue_changes_leaf_key_and_exact_san() {
        let directory = tempfile::tempdir().expect("temporary TLS state");
        let old_address = Ipv4Addr::new(192, 0, 2, 10);
        let new_address = Ipv4Addr::new(198, 51, 100, 20);
        let old_fingerprint = provision_initial(directory.path(), old_address).expect("provision");
        let paths = Paths::new(directory.path());
        let root_before = fs::read(&paths.root_cert).expect("root DER");
        let old_leaf = fs::read(&paths.leaf_cert).expect("old leaf DER");
        let old_key = fs::read(&paths.leaf_key).expect("old protected leaf key");

        let candidate =
            prepare_leaf_reissue(directory.path(), old_address, new_address).expect("prepare leaf");
        assert_eq!(fs::read(&paths.leaf_cert).unwrap(), old_leaf);
        assert_eq!(fs::read(&paths.leaf_key).unwrap(), old_key);
        assert_eq!(candidate.root_fingerprint, old_fingerprint);
        commit_leaf_reissue(directory.path(), candidate).expect("commit reissue");

        assert_eq!(fs::read(&paths.root_cert).unwrap(), root_before);
        assert_eq!(root_fingerprint(directory.path()).unwrap(), old_fingerprint);
        assert_ne!(fs::read(&paths.leaf_cert).unwrap(), old_leaf);
        assert_ne!(fs::read(&paths.leaf_key).unwrap(), old_key);
        assert_eq!(
            validate_identity(directory.path(), new_address).unwrap(),
            old_fingerprint
        );
        assert_eq!(
            validate_identity(directory.path(), old_address),
            Err("controller_tls_identity_invalid")
        );
    }

    #[cfg(windows)]
    #[test]
    fn same_ip_port_reissue_creates_a_new_leaf_under_the_same_root() {
        let directory = tempfile::tempdir().expect("temporary TLS state");
        let address = Ipv4Addr::new(192, 0, 2, 10);
        let fingerprint_before = provision_initial(directory.path(), address).expect("provision");
        let paths = Paths::new(directory.path());
        let root_before = fs::read(&paths.root_cert).expect("root DER");
        let old_leaf = fs::read(&paths.leaf_cert).expect("old leaf DER");
        let old_key = fs::read(&paths.leaf_key).expect("old leaf key");

        assert_eq!(
            reissue_leaf_same_root(directory.path(), address, address).expect("port-only reissue"),
            fingerprint_before
        );

        assert_eq!(fs::read(&paths.root_cert).unwrap(), root_before);
        assert_ne!(fs::read(&paths.leaf_cert).unwrap(), old_leaf);
        assert_ne!(fs::read(&paths.leaf_key).unwrap(), old_key);
        assert_eq!(
            validate_identity(directory.path(), address).unwrap(),
            fingerprint_before
        );
    }

    #[cfg(windows)]
    #[test]
    fn substituted_persisted_root_fails_closed_without_replacement() {
        let directory = tempfile::tempdir().expect("temporary TLS state");
        let address = Ipv4Addr::new(192, 0, 2, 10);
        provision_initial(directory.path(), address).expect("initial provision");
        let paths = Paths::new(directory.path());
        let (substituted_root, _) = root_material();
        fs::write(&paths.root_cert, &substituted_root).expect("substitute persisted root");

        assert_eq!(
            load_validate_or_renew(directory.path(), address),
            Err("controller_tls_identity_invalid")
        );
        assert_eq!(
            fs::read(&paths.root_cert).expect("persisted substituted root remains"),
            substituted_root
        );
    }

    #[cfg(windows)]
    #[test]
    fn all_missing_persisted_tls_state_fails_without_replacement() {
        let directory = tempfile::tempdir().expect("temporary TLS state");
        let address = Ipv4Addr::new(192, 0, 2, 10);
        provision_initial(directory.path(), address).expect("initial provision");
        let tls_directory = directory.path().join("tls");
        fs::remove_dir_all(&tls_directory).expect("remove all persisted TLS files");

        assert_eq!(
            load_validate_or_renew(directory.path(), address),
            Err("controller_tls_identity_invalid")
        );
        assert!(
            !tls_directory.exists(),
            "startup must not recreate root state"
        );
    }
}
