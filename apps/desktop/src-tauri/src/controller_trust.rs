use std::{
    collections::HashMap,
    io::Write,
    net::{Ipv4Addr, SocketAddr, TcpStream},
    path::Path,
    sync::{Arc, Mutex},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use reqwest::Url;
use rustls::{
    client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier},
    pki_types::{CertificateDer, ServerName, UnixTime},
    ClientConfig, ClientConnection, RootCertStore, SignatureScheme,
};
use serde::{Deserialize, Serialize};
use zeroize::Zeroizing;

use crate::{controller_tls, windows_crypto};

const TRUST_SCHEMA_VERSION: u32 = 1;
const PROBE_TTL: Duration = Duration::from_secs(5 * 60);
const PROBE_CONNECT_TIMEOUT: Duration = Duration::from_secs(5);

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub(super) struct TrustProbeSummary {
    pub probe_id: String,
    pub endpoint: String,
    pub root_fingerprint: String,
    pub expires_at: u64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub(super) struct TrustedControllerSummary {
    pub endpoint: Option<String>,
    pub root_fingerprint: Option<String>,
    pub trusted: bool,
}

#[derive(Clone, Debug)]
struct PendingProbe {
    endpoint: CanonicalEndpoint,
    root_der: Vec<u8>,
    fingerprint: String,
    created_at: u64,
    expires_at: u64,
}

#[derive(Clone, Debug)]
struct CanonicalEndpoint {
    origin: String,
    address: Ipv4Addr,
    port: u16,
}

#[derive(Debug, Default)]
struct CapturedChain {
    certificates: Mutex<Option<Vec<CertificateDer<'static>>>>,
}

impl ServerCertVerifier for CapturedChain {
    fn verify_server_cert(
        &self,
        end_entity: &CertificateDer<'_>,
        intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp_response: &[u8],
        _now: UnixTime,
    ) -> Result<ServerCertVerified, rustls::Error> {
        let mut chain = Vec::with_capacity(intermediates.len() + 1);
        chain.push(CertificateDer::from(end_entity.as_ref().to_vec()));
        chain.extend(
            intermediates
                .iter()
                .map(|certificate| CertificateDer::from(certificate.as_ref().to_vec())),
        );
        *self
            .certificates
            .lock()
            .map_err(|_| rustls::Error::General("probe capture unavailable".to_string()))? =
            Some(chain);
        Ok(ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        _message: &[u8],
        _cert: &CertificateDer<'_>,
        _dss: &rustls::DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        Ok(HandshakeSignatureValid::assertion())
    }

    fn verify_tls13_signature(
        &self,
        _message: &[u8],
        _cert: &CertificateDer<'_>,
        _dss: &rustls::DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        Ok(HandshakeSignatureValid::assertion())
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        rustls::crypto::ring::default_provider()
            .signature_verification_algorithms
            .supported_schemes()
    }
}

#[derive(Clone, Debug, Default)]
pub(super) struct ControllerTrustState {
    pending: Arc<Mutex<HashMap<String, PendingProbe>>>,
}

impl ControllerTrustState {
    pub fn probe(&self, value: &str) -> Result<TrustProbeSummary, &'static str> {
        let candidate = probe_endpoint(value)?;
        let now = unix_now();
        let expires_at = now.saturating_add(PROBE_TTL.as_secs());
        let probe_id = random_probe_id()?;
        let pending = PendingProbe {
            endpoint: candidate.endpoint.clone(),
            root_der: candidate.root_der,
            fingerprint: candidate.fingerprint.clone(),
            created_at: now,
            expires_at,
        };
        let mut guard = self
            .pending
            .lock()
            .map_err(|_| "controller_trust_probe_failed")?;
        guard.retain(|_, probe| probe.expires_at > now);
        guard.insert(probe_id.clone(), pending);
        Ok(TrustProbeSummary {
            probe_id,
            endpoint: candidate.endpoint.origin,
            root_fingerprint: candidate.fingerprint,
            expires_at,
        })
    }

    pub fn confirm(
        &self,
        probe_id: &str,
        trust_path: &Path,
    ) -> Result<TrustedControllerSummary, &'static str> {
        let pending = self
            .pending
            .lock()
            .map_err(|_| "controller_trust_probe_failed")?
            .remove(probe_id)
            .ok_or("controller_trust_probe_expired")?;
        if pending.expires_at <= unix_now()
            || pending.created_at.saturating_add(PROBE_TTL.as_secs()) != pending.expires_at
        {
            return Err("controller_trust_probe_expired");
        }
        let confirmed = match probe_endpoint(&pending.endpoint.origin) {
            Ok(candidate)
                if candidate.endpoint.origin == pending.endpoint.origin
                    && candidate.root_der == pending.root_der
                    && candidate.fingerprint == pending.fingerprint =>
            {
                candidate
            }
            _ => return Err("controller_trust_confirmation_mismatch"),
        };
        persist_trust(trust_path, &confirmed)?;
        Ok(TrustedControllerSummary {
            endpoint: Some(confirmed.endpoint.origin),
            root_fingerprint: Some(confirmed.fingerprint),
            trusted: true,
        })
    }
}

#[derive(Clone, Debug)]
struct Candidate {
    endpoint: CanonicalEndpoint,
    root_der: Vec<u8>,
    fingerprint: String,
}

#[derive(Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct TrustRecord {
    schema_version: u32,
    canonical_endpoint: String,
    root_der: Vec<u8>,
    root_fingerprint: String,
}

pub(super) fn summary(
    trust_path: &Path,
    requested_endpoint: Option<&str>,
) -> Result<TrustedControllerSummary, &'static str> {
    let Some(record) = load_trust(trust_path)? else {
        return Ok(TrustedControllerSummary {
            endpoint: None,
            root_fingerprint: None,
            trusted: false,
        });
    };
    let requested = requested_endpoint.map(parse_endpoint).transpose()?;
    let matches = requested
        .as_ref()
        .is_none_or(|endpoint| endpoint.origin == record.canonical_endpoint);
    Ok(TrustedControllerSummary {
        endpoint: matches.then_some(record.canonical_endpoint),
        root_fingerprint: matches.then_some(record.root_fingerprint),
        trusted: matches,
    })
}

pub(super) fn trusted_root_for_endpoint(
    trust_path: &Path,
    endpoint: &str,
) -> Result<Vec<u8>, &'static str> {
    let endpoint = parse_endpoint(endpoint)?;
    let record = load_trust(trust_path)?.ok_or("controller_trust_required")?;
    if record.canonical_endpoint != endpoint.origin {
        return Err("controller_trust_required");
    }
    Ok(record.root_der)
}

pub(super) fn trust_path(app_data_root: &Path) -> std::path::PathBuf {
    app_data_root.join("controller-trust.dpapi")
}

fn parse_endpoint(value: &str) -> Result<CanonicalEndpoint, &'static str> {
    if value.trim() != value {
        return Err("controller_trust_probe_failed");
    }
    let authority = value
        .split_once("://")
        .map(|(_, remainder)| remainder.split(['/', '?', '#']).next().unwrap_or_default())
        .ok_or("controller_trust_probe_failed")?;
    if authority.contains('@') {
        return Err("controller_trust_probe_failed");
    }
    let parsed = Url::parse(value).map_err(|_| "controller_trust_probe_failed")?;
    if parsed.scheme() != "https"
        || parsed.host_str().is_none()
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || parsed.query().is_some()
        || parsed.fragment().is_some()
        || !matches!(parsed.path(), "" | "/")
    {
        return Err("controller_trust_probe_failed");
    }
    let address = parsed
        .host_str()
        .and_then(|host| host.parse::<Ipv4Addr>().ok())
        .ok_or("controller_trust_probe_failed")?;
    let port = parsed
        .port_or_known_default()
        .ok_or("controller_trust_probe_failed")?;
    if port == 0 {
        return Err("controller_trust_probe_failed");
    }
    let origin = parsed.origin().ascii_serialization();
    Ok(CanonicalEndpoint {
        origin,
        address,
        port,
    })
}

fn probe_endpoint(value: &str) -> Result<Candidate, &'static str> {
    let endpoint = parse_endpoint(value)?;
    let capture = Arc::new(CapturedChain::default());
    let stage_a =
        ClientConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
            .with_safe_default_protocol_versions()
            .map_err(|_| "controller_trust_probe_failed")?
            .dangerous()
            .with_custom_certificate_verifier(capture.clone())
            .with_no_client_auth();
    tls_handshake(&endpoint, Arc::new(stage_a))?;
    let captured = capture
        .certificates
        .lock()
        .map_err(|_| "controller_trust_probe_failed")?
        .clone()
        .ok_or("controller_trust_probe_failed")?;
    if captured.len() != 2 {
        return Err("controller_trust_probe_failed");
    }
    let leaf_der = captured[0].as_ref().to_vec();
    let root_der = captured[1].as_ref().to_vec();
    controller_tls::validate_trust_anchor(&root_der)
        .map_err(|_| "controller_trust_probe_failed")?;
    controller_tls::validate_peer_leaf(&leaf_der, &root_der, endpoint.address)
        .map_err(|_| "controller_trust_probe_failed")?;

    let mut roots = RootCertStore::empty();
    roots
        .add(CertificateDer::from(root_der.clone()))
        .map_err(|_| "controller_trust_probe_failed")?;
    let stage_b =
        ClientConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
            .with_safe_default_protocol_versions()
            .map_err(|_| "controller_trust_probe_failed")?
            .with_root_certificates(roots)
            .with_no_client_auth();
    tls_handshake(&endpoint, Arc::new(stage_b))?;

    let fingerprint =
        controller_tls::fingerprint(&root_der).map_err(|_| "controller_trust_probe_failed")?;
    Ok(Candidate {
        endpoint,
        root_der,
        fingerprint,
    })
}

fn tls_handshake(
    endpoint: &CanonicalEndpoint,
    config: Arc<ClientConfig>,
) -> Result<(), &'static str> {
    let socket = SocketAddr::from((endpoint.address, endpoint.port));
    let mut stream = TcpStream::connect_timeout(&socket, PROBE_CONNECT_TIMEOUT)
        .map_err(|_| "controller_trust_probe_failed")?;
    let deadline = Instant::now() + PROBE_CONNECT_TIMEOUT;
    let server_name = ServerName::IpAddress(endpoint.address.into());
    let mut connection =
        ClientConnection::new(config, server_name).map_err(|_| "controller_trust_probe_failed")?;
    while connection.is_handshaking() {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero()
            || stream.set_read_timeout(Some(remaining)).is_err()
            || stream.set_write_timeout(Some(remaining)).is_err()
        {
            return Err("controller_trust_probe_failed");
        }
        if connection.complete_io(&mut stream).is_err() {
            return Err("controller_trust_probe_failed");
        }
    }
    Ok(())
}

fn random_probe_id() -> Result<String, &'static str> {
    let mut bytes = Zeroizing::new([0u8; 32]);
    windows_crypto::fill_random(&mut *bytes).map_err(|_| "controller_trust_probe_failed")?;
    Ok(lower_hex(&*bytes))
}

fn persist_trust(trust_path: &Path, candidate: &Candidate) -> Result<(), &'static str> {
    let record = TrustRecord {
        schema_version: TRUST_SCHEMA_VERSION,
        canonical_endpoint: candidate.endpoint.origin.clone(),
        root_der: candidate.root_der.clone(),
        root_fingerprint: candidate.fingerprint.clone(),
    };
    let plaintext =
        Zeroizing::new(serde_json::to_vec(&record).map_err(|_| "controller_trust_store_invalid")?);
    let protected = windows_crypto::protect_current_user(&plaintext)
        .map_err(|_| "controller_trust_store_invalid")?;
    let parent = trust_path
        .parent()
        .ok_or("controller_trust_store_invalid")?;
    std::fs::create_dir_all(parent).map_err(|_| "controller_trust_store_invalid")?;
    let mut temporary =
        tempfile::NamedTempFile::new_in(parent).map_err(|_| "controller_trust_store_invalid")?;
    temporary
        .write_all(&protected)
        .and_then(|()| temporary.flush())
        .and_then(|()| temporary.as_file().sync_all())
        .map_err(|_| "controller_trust_store_invalid")?;
    temporary
        .persist(trust_path)
        .map_err(|_| "controller_trust_store_invalid")?;
    Ok(())
}

fn load_trust(trust_path: &Path) -> Result<Option<TrustRecord>, &'static str> {
    let protected = match std::fs::read(trust_path) {
        Ok(protected) => protected,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err("controller_trust_store_invalid"),
    };
    let plaintext = Zeroizing::new(
        windows_crypto::unprotect_current_user(&protected)
            .map_err(|_| "controller_trust_store_invalid")?,
    );
    let record: TrustRecord =
        serde_json::from_slice(&plaintext).map_err(|_| "controller_trust_store_invalid")?;
    let endpoint =
        parse_endpoint(&record.canonical_endpoint).map_err(|_| "controller_trust_store_invalid")?;
    let fingerprint = controller_tls::fingerprint(&record.root_der)
        .map_err(|_| "controller_trust_store_invalid")?;
    controller_tls::validate_trust_anchor(&record.root_der)
        .map_err(|_| "controller_trust_store_invalid")?;
    if record.schema_version != TRUST_SCHEMA_VERSION
        || endpoint.origin != record.canonical_endpoint
        || fingerprint != record.root_fingerprint
    {
        return Err("controller_trust_store_invalid");
    }
    Ok(Some(record))
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

fn unix_now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or(Duration::ZERO)
        .as_secs()
}

#[cfg(test)]
mod tests {
    use super::*;
    use rcgen::{
        BasicConstraints, CertificateParams, ExtendedKeyUsagePurpose, IsCa, Issuer, KeyPair,
        KeyUsagePurpose, PKCS_ECDSA_P256_SHA256,
    };
    use rustls::{ServerConfig, ServerConnection, StreamOwned};
    use std::{io::Read, net::TcpListener, thread};
    use time::OffsetDateTime;

    struct TlsIdentity {
        server: Arc<ServerConfig>,
        root_der: Vec<u8>,
    }

    fn tls_identity() -> TlsIdentity {
        let now = OffsetDateTime::now_utc();
        let mut root_params = CertificateParams::default();
        root_params.is_ca = IsCa::Ca(BasicConstraints::Constrained(0));
        root_params.key_usages = vec![KeyUsagePurpose::KeyCertSign, KeyUsagePurpose::CrlSign];
        root_params.not_before = now - time::Duration::minutes(5);
        root_params.not_after = now + time::Duration::days(3650);
        let root_key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).expect("P-256 root");
        let root = root_params
            .self_signed(&root_key)
            .expect("self-signed root");
        let root_der = root.der().as_ref().to_vec();
        let issuer = Issuer::from_ca_cert_der(&CertificateDer::from(root_der.clone()), root_key)
            .expect("root issuer");
        let mut leaf_params = CertificateParams::default();
        leaf_params.subject_alt_names = vec![
            rcgen::SanType::IpAddress(Ipv4Addr::LOCALHOST.into()),
            rcgen::SanType::IpAddress(Ipv4Addr::LOCALHOST.into()),
        ];
        leaf_params.is_ca = IsCa::ExplicitNoCa;
        leaf_params.key_usages = vec![KeyUsagePurpose::DigitalSignature];
        leaf_params.extended_key_usages = vec![ExtendedKeyUsagePurpose::ServerAuth];
        leaf_params.not_before = now - time::Duration::minutes(5);
        leaf_params.not_after = now + time::Duration::days(90);
        let leaf_key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).expect("P-256 leaf");
        let leaf = leaf_params
            .signed_by(&leaf_key, &issuer)
            .expect("root-signed leaf");
        let server =
            ServerConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
                .with_safe_default_protocol_versions()
                .expect("TLS versions")
                .with_no_client_auth()
                .with_single_cert(
                    vec![
                        CertificateDer::from(leaf.der().as_ref().to_vec()),
                        CertificateDer::from(root_der.clone()),
                    ],
                    rustls::pki_types::PrivatePkcs8KeyDer::from(leaf_key.serialize_der()).into(),
                )
                .expect("TLS server certificate");
        TlsIdentity {
            server: Arc::new(server),
            root_der,
        }
    }

    fn tls_peer(identities: Vec<TlsIdentity>) -> (String, thread::JoinHandle<Vec<Vec<u8>>>) {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).expect("TLS probe listener");
        let address = listener.local_addr().expect("listener address");
        let endpoint = format!("https://{address}");
        let server = thread::spawn(move || {
            let mut application_data = Vec::new();
            for identity in identities {
                let (socket, _) = listener.accept().expect("probe TLS connection");
                let connection =
                    ServerConnection::new(identity.server).expect("server TLS connection");
                let mut tls = StreamOwned::new(connection, socket);
                while tls.conn.is_handshaking() {
                    tls.conn
                        .complete_io(&mut tls.sock)
                        .expect("complete TLS handshake");
                }
                tls.sock
                    .set_read_timeout(Some(Duration::from_millis(400)))
                    .expect("bound application read");
                let mut request = [0u8; 2048];
                let data = match tls.read(&mut request) {
                    Ok(length) => request[..length].to_vec(),
                    Err(error)
                        if matches!(
                            error.kind(),
                            std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                        ) =>
                    {
                        Vec::new()
                    }
                    Err(error)
                        if matches!(
                            error.kind(),
                            std::io::ErrorKind::ConnectionAborted
                                | std::io::ErrorKind::ConnectionReset
                                | std::io::ErrorKind::UnexpectedEof
                        ) =>
                    {
                        Vec::new()
                    }
                    Err(error) => panic!("read TLS application data: {error}"),
                };
                application_data.push(data);
            }
            application_data
        });
        (endpoint, server)
    }

    fn persist_test_record(path: &Path, record: &TrustRecord) {
        let plaintext = serde_json::to_vec(record).expect("serialize test trust record");
        let protected =
            windows_crypto::protect_current_user(&plaintext).expect("protect test trust record");
        std::fs::write(path, protected).expect("write protected test trust record");
    }

    #[test]
    fn exact_https_origin_is_canonicalized_and_invalid_inputs_fail() {
        let parsed = parse_endpoint("https://192.0.2.5:443/").expect("canonical origin");
        assert_eq!(parsed.origin, "https://192.0.2.5");
        assert_eq!(parsed.port, 443);
        let with_credentials = ["https://user", ":pass@", "192.0.2.5"].concat();
        for invalid in [
            "http://192.0.2.5",
            with_credentials.as_str(),
            "https://192.0.2.5/path",
            "https://192.0.2.5?query=1",
            "https://192.0.2.5#fragment",
            "https://@192.0.2.5",
            "https://192.0.2.5:0",
            " https://192.0.2.5",
            "https://controller.example",
            "https://[::1]:8443",
        ] {
            assert_eq!(
                parse_endpoint(invalid).unwrap_err(),
                "controller_trust_probe_failed"
            );
        }
    }

    #[test]
    fn probe_ids_use_256_bits_of_system_randomness() {
        let first = random_probe_id().expect("cryptographic probe ID");
        let second = random_probe_id().expect("second cryptographic probe ID");
        assert_eq!(first.len(), 64);
        assert_ne!(first, second);
    }

    #[cfg(windows)]
    #[test]
    fn verified_probe_sends_no_http_bytes_and_does_not_persist_trust() {
        let identity = tls_identity();
        let expected_fingerprint =
            controller_tls::fingerprint(&identity.root_der).expect("root fingerprint");
        let identities = vec![
            TlsIdentity {
                server: identity.server.clone(),
                root_der: identity.root_der.clone(),
            },
            TlsIdentity {
                server: identity.server.clone(),
                root_der: identity.root_der.clone(),
            },
            TlsIdentity {
                server: identity.server.clone(),
                root_der: identity.root_der.clone(),
            },
            identity,
        ];
        let (endpoint, server) = tls_peer(identities);
        let trust_state = ControllerTrustState::default();
        let directory = tempfile::tempdir().expect("temporary private trust state");
        let trust_path = directory.path().join("controller-trust.dpapi");

        let candidate = trust_state.probe(&endpoint).expect("two-stage TLS probe");

        assert_eq!(candidate.root_fingerprint, expected_fingerprint);
        assert!(!trust_path.exists(), "probe alone must not persist trust");
        let trusted = trust_state
            .confirm(&candidate.probe_id, &trust_path)
            .expect("explicit root confirmation");
        assert!(trusted.trusted);
        assert!(trust_path.is_file());
        let application_data = server.join().expect("TLS peer completed all handshakes");
        assert_eq!(application_data, vec![Vec::<u8>::new(); 4]);
    }

    #[cfg(windows)]
    #[test]
    fn confirmation_reprobe_rejects_a_changed_root_and_discards_probe() {
        let first = tls_identity();
        let second = tls_identity();
        let (endpoint, server) = tls_peer(vec![
            TlsIdentity {
                server: first.server.clone(),
                root_der: first.root_der.clone(),
            },
            TlsIdentity {
                server: first.server.clone(),
                root_der: first.root_der.clone(),
            },
            TlsIdentity {
                server: second.server.clone(),
                root_der: second.root_der.clone(),
            },
            second,
        ]);
        let state = ControllerTrustState::default();
        let directory = tempfile::tempdir().expect("temporary private trust state");
        let trust_path = directory.path().join("controller-trust.dpapi");
        let pending = state.probe(&endpoint).expect("initial probe");

        assert_eq!(
            state.confirm(&pending.probe_id, &trust_path),
            Err("controller_trust_confirmation_mismatch")
        );
        assert!(!trust_path.exists());
        assert_eq!(server.join().unwrap().len(), 4);
    }

    #[cfg(windows)]
    #[test]
    fn expired_pending_probe_is_rejected_without_network_confirmation() {
        let identity = tls_identity();
        let (endpoint, server) = tls_peer(vec![
            TlsIdentity {
                server: identity.server.clone(),
                root_der: identity.root_der.clone(),
            },
            identity,
        ]);
        let state = ControllerTrustState::default();
        let directory = tempfile::tempdir().expect("temporary trust state");
        let trust_path = directory.path().join("controller-trust.dpapi");
        let pending = state.probe(&endpoint).expect("initial probe");
        state
            .pending
            .lock()
            .unwrap()
            .get_mut(&pending.probe_id)
            .unwrap()
            .expires_at = unix_now().saturating_sub(1);

        assert_eq!(
            state.confirm(&pending.probe_id, &trust_path),
            Err("controller_trust_probe_expired")
        );
        assert_eq!(server.join().unwrap().len(), 2);
    }

    #[cfg(windows)]
    #[test]
    fn private_root_trust_loads_without_installing_a_windows_root() {
        let identity = tls_identity();
        let endpoint = parse_endpoint("https://127.0.0.1:8443").unwrap();
        let candidate = Candidate {
            endpoint,
            fingerprint: controller_tls::fingerprint(&identity.root_der).unwrap(),
            root_der: identity.root_der.clone(),
        };
        let directory = tempfile::tempdir().expect("private trust data root");
        let path = directory.path().join("controller-trust.dpapi");
        persist_trust(&path, &candidate).expect("persist private trust");

        let trusted = load_trust(&path).expect("validate private trust").unwrap();

        assert_eq!(trusted.root_der, identity.root_der);
        assert_eq!(trusted.root_fingerprint, candidate.fingerprint);
        assert_eq!(
            trusted_root_for_endpoint(&path, "https://127.0.0.1:8443").unwrap(),
            identity.root_der
        );
        assert_eq!(
            trusted_root_for_endpoint(&path, "https://127.0.0.1:9443"),
            Err("controller_trust_required")
        );
    }

    #[cfg(windows)]
    #[test]
    fn corrupted_bundle_and_fingerprint_metadata_mismatch_fail_closed() {
        let identity = tls_identity();
        let directory = tempfile::tempdir().expect("private trust data root");
        let path = directory.path().join("controller-trust.dpapi");
        std::fs::write(&path, b"corrupt protected trust").expect("write corrupt fixture");
        assert_eq!(load_trust(&path), Err("controller_trust_store_invalid"));

        let record = TrustRecord {
            schema_version: TRUST_SCHEMA_VERSION,
            canonical_endpoint: "https://127.0.0.1:8443".to_string(),
            root_der: identity.root_der,
            root_fingerprint: "SHA256:00".repeat(32),
        };
        persist_test_record(&path, &record);
        assert_eq!(load_trust(&path), Err("controller_trust_store_invalid"));
    }
}
