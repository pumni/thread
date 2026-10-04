use std::sync::Mutex;
use std::time::Duration;

use reqwest::{redirect::Policy, Client, StatusCode, Url};
use serde::{Deserialize, Serialize};
use zeroize::Zeroizing;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all(serialize = "camelCase", deserialize = "snake_case"))]
pub(crate) struct OperatorIdentity {
    pub id: String,
    pub username: String,
    pub role: String,
    pub must_change_password: bool,
    pub expires_at: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all(serialize = "camelCase", deserialize = "snake_case"))]
pub(crate) struct OperatorUser {
    pub id: String,
    pub username: String,
    pub role: String,
    pub enabled: bool,
    pub must_change_password: bool,
    pub created_at: String,
}

#[derive(Deserialize, Serialize)]
#[serde(rename_all(serialize = "camelCase", deserialize = "snake_case"))]
pub(crate) struct CreatedOperatorUser {
    pub user: OperatorUser,
    pub temporary_password: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
struct LoginResponse {
    access_token: String,
    operator: OperatorIdentity,
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
struct CreateUserResponse {
    user: OperatorUser,
    temporary_password: String,
}

#[derive(Serialize)]
struct LoginRequest<'a> {
    username: &'a str,
    password: &'a str,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct CreateUserRequest<'a> {
    username: &'a str,
    role: &'a str,
}

#[derive(Serialize)]
struct UpdateUserRequest<'a> {
    #[serde(skip_serializing_if = "Option::is_none")]
    role: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    enabled: Option<bool>,
}

#[derive(Serialize)]
#[serde(rename_all = "snake_case")]
struct ChangePasswordRequest<'a> {
    new_password: &'a str,
}

#[derive(Serialize)]
struct WorkerDrainRequest<'a> {
    reason_code: &'a str,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub(crate) enum WorkerStatus {
    Registering,
    Online,
    Degraded,
    Draining,
    Offline,
    Disabled,
    UpgradeRequired,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all(serialize = "camelCase", deserialize = "snake_case"))]
pub(crate) struct WorkerDrainStatus {
    pub status: WorkerStatus,
    pub active_browser_sessions: u32,
    pub running_worker_jobs: u32,
    pub quiescent: bool,
}

struct Session {
    base_url: String,
    client: Client,
    bearer: Zeroizing<String>,
    operator: OperatorIdentity,
}

struct SessionSnapshot {
    base_url: String,
    client: Client,
    bearer: Zeroizing<String>,
}

pub(crate) struct OperatorAuthState {
    session: Mutex<Option<Session>>,
}

impl Default for OperatorAuthState {
    fn default() -> Self {
        Self {
            session: Mutex::new(None),
        }
    }
}

impl OperatorAuthState {
    pub(crate) async fn login(
        &self,
        base_url: &str,
        root_certificate: &[u8],
        username: &str,
        password: &str,
    ) -> Result<OperatorIdentity, String> {
        let base_url = normalize_base_url(base_url)?;
        if root_certificate.is_empty() {
            return Err("controller_trust_required".to_string());
        }
        let client = private_root_client(root_certificate)?;
        let url = endpoint(&base_url, "/v1/operator/login");
        let response = client
            .post(url)
            .json(&LoginRequest { username, password })
            .send()
            .await
            .map_err(|_| "operator_api_unavailable".to_string())?;
        if !response.status().is_success() {
            return Err("operator_login_failed".to_string());
        }
        let mut body: LoginResponse = response
            .json()
            .await
            .map_err(|_| "operator_login_response_invalid".to_string())?;
        let bearer = Zeroizing::new(std::mem::take(&mut body.access_token));
        let operator = body.operator;
        let mut session = self
            .session
            .lock()
            .map_err(|_| "operator_session_unavailable".to_string())?;
        *session = Some(Session {
            base_url,
            client,
            bearer,
            operator: operator.clone(),
        });
        Ok(operator)
    }

    pub(crate) async fn current(&self) -> Result<Option<OperatorIdentity>, String> {
        let Some(snapshot) = self.snapshot()? else {
            return Ok(None);
        };
        let response = snapshot
            .client
            .get(endpoint(&snapshot.base_url, "/v1/operator/me"))
            .bearer_auth(snapshot.bearer.as_str())
            .send()
            .await
            .map_err(|_| "operator_api_unavailable".to_string())?;
        if response.status() == StatusCode::UNAUTHORIZED {
            self.clear_if_matches(&snapshot);
            return Err("operator_session_revoked".to_string());
        }
        if !response.status().is_success() {
            return Err("operator_request_failed".to_string());
        }
        let operator: OperatorIdentity = response
            .json()
            .await
            .map_err(|_| "operator_response_invalid".to_string())?;
        let mut guard = self
            .session
            .lock()
            .map_err(|_| "operator_session_unavailable".to_string())?;
        if let Some(session) = guard.as_mut() {
            if session.base_url == snapshot.base_url
                && session.bearer.as_str() == snapshot.bearer.as_str()
            {
                session.operator = operator.clone();
            }
        }
        Ok(Some(operator))
    }

    pub(crate) async fn logout(&self) {
        if let Some(snapshot) = self.detach_current_session() {
            Self::revoke_snapshot(snapshot).await;
        }
    }

    pub(crate) async fn revoke_session_at(&self, base_url: &str) -> Result<(), String> {
        let base_url = normalize_base_url(base_url)?;
        let snapshot = self
            .detach_current_session()
            .ok_or_else(|| "operator_authentication_required".to_string())?;
        Self::revoke_snapshot_at(snapshot, &base_url).await
    }

    pub(crate) fn lock_session(&self) {
        let Some(snapshot) = self.detach_current_session() else {
            return;
        };
        tauri::async_runtime::spawn(async move {
            Self::revoke_snapshot(snapshot).await;
        });
    }

    pub(crate) async fn list_users(&self) -> Result<Vec<OperatorUser>, String> {
        let snapshot = self.require_snapshot()?;
        let response = snapshot
            .client
            .get(endpoint(&snapshot.base_url, "/v1/operator/users"))
            .bearer_auth(snapshot.bearer.as_str())
            .send()
            .await
            .map_err(|_| "operator_api_unavailable".to_string())?;
        if !response.status().is_success() {
            return Err("operator_request_failed".to_string());
        }
        response
            .json()
            .await
            .map_err(|_| "operator_response_invalid".to_string())
    }

    pub(crate) async fn create_user(
        &self,
        username: &str,
        role: &str,
    ) -> Result<CreatedOperatorUser, String> {
        let snapshot = self.require_snapshot()?;
        let response = snapshot
            .client
            .post(endpoint(&snapshot.base_url, "/v1/operator/users"))
            .bearer_auth(snapshot.bearer.as_str())
            .json(&CreateUserRequest { username, role })
            .send()
            .await
            .map_err(|_| "operator_api_unavailable".to_string())?;
        if !response.status().is_success() {
            return Err("operator_request_failed".to_string());
        }
        let body: CreateUserResponse = response
            .json()
            .await
            .map_err(|_| "operator_response_invalid".to_string())?;
        Ok(CreatedOperatorUser {
            user: body.user,
            temporary_password: body.temporary_password,
        })
    }

    pub(crate) async fn update_user(
        &self,
        id: &str,
        role: Option<&str>,
        enabled: Option<bool>,
    ) -> Result<OperatorUser, String> {
        let snapshot = self.require_snapshot()?;
        let response = snapshot
            .client
            .patch(endpoint(
                &snapshot.base_url,
                &format!("/v1/operator/users/{id}"),
            ))
            .bearer_auth(snapshot.bearer.as_str())
            .json(&UpdateUserRequest { role, enabled })
            .send()
            .await
            .map_err(|_| "operator_api_unavailable".to_string())?;
        if !response.status().is_success() {
            return Err("operator_request_failed".to_string());
        }
        response
            .json()
            .await
            .map_err(|_| "operator_response_invalid".to_string())
    }

    pub(crate) async fn change_password(&self, password: &str) -> Result<OperatorIdentity, String> {
        let snapshot = self.require_snapshot()?;
        let response = snapshot
            .client
            .post(endpoint(&snapshot.base_url, "/v1/operator/me/password"))
            .bearer_auth(snapshot.bearer.as_str())
            .json(&ChangePasswordRequest {
                new_password: password,
            })
            .send()
            .await
            .map_err(|_| "operator_api_unavailable".to_string())?;
        if !response.status().is_success() {
            return Err("operator_request_failed".to_string());
        }
        let operator: OperatorIdentity = response
            .json()
            .await
            .map_err(|_| "operator_response_invalid".to_string())?;
        let mut guard = self
            .session
            .lock()
            .map_err(|_| "operator_session_unavailable".to_string())?;
        if let Some(session) = guard.as_mut() {
            if session.base_url == snapshot.base_url
                && session.bearer.as_str() == snapshot.bearer.as_str()
            {
                session.operator = operator.clone();
            }
        }
        Ok(operator)
    }

    pub(crate) async fn authorize_node_lifecycle(
        &self,
        controller_node: bool,
    ) -> Result<(), String> {
        let Some(operator) = self.current().await? else {
            return Err("operator_authentication_required".to_string());
        };
        if !lifecycle_session_allowed(
            &operator.role,
            operator.must_change_password,
            controller_node,
        ) {
            return Err(if operator.must_change_password {
                "operator_password_change_required".to_string()
            } else {
                "operator_forbidden".to_string()
            });
        }
        Ok(())
    }

    pub(crate) async fn request_local_worker_drain(
        &self,
        worker_id: &str,
        reason_code: &str,
    ) -> Result<WorkerDrainStatus, String> {
        if !valid_worker_id(worker_id) || !valid_drain_reason(reason_code) {
            return Err("worker_drain_request_failed".to_string());
        }
        let snapshot = self.require_worker_lifecycle_snapshot().await?;
        let response = snapshot
            .client
            .post(endpoint(
                &snapshot.base_url,
                &format!("/v1/workers/{worker_id}/drain"),
            ))
            .bearer_auth(snapshot.bearer.as_str())
            .json(&WorkerDrainRequest { reason_code })
            .send()
            .await
            .map_err(|_| "worker_drain_unavailable".to_string())?;
        if response.status() == StatusCode::UNAUTHORIZED {
            self.clear_if_matches(&snapshot);
            return Err("operator_session_revoked".to_string());
        }
        if response.status() == StatusCode::FORBIDDEN {
            return Err("operator_forbidden".to_string());
        }
        if !response.status().is_success() {
            return Err("worker_drain_request_failed".to_string());
        }
        response
            .json()
            .await
            .map_err(|_| "worker_drain_response_invalid".to_string())
    }

    pub(crate) async fn local_worker_drain_status(
        &self,
        worker_id: &str,
    ) -> Result<WorkerDrainStatus, String> {
        if !valid_worker_id(worker_id) {
            return Err("worker_drain_request_failed".to_string());
        }
        let snapshot = self.require_worker_lifecycle_snapshot().await?;
        let response = snapshot
            .client
            .get(endpoint(
                &snapshot.base_url,
                &format!("/v1/workers/{worker_id}/drain"),
            ))
            .bearer_auth(snapshot.bearer.as_str())
            .send()
            .await
            .map_err(|_| "worker_drain_unavailable".to_string())?;
        if response.status() == StatusCode::UNAUTHORIZED {
            self.clear_if_matches(&snapshot);
            return Err("operator_session_revoked".to_string());
        }
        if response.status() == StatusCode::FORBIDDEN {
            return Err("operator_forbidden".to_string());
        }
        if !response.status().is_success() {
            return Err("worker_drain_unavailable".to_string());
        }
        response
            .json()
            .await
            .map_err(|_| "worker_drain_response_invalid".to_string())
    }

    async fn require_worker_lifecycle_snapshot(&self) -> Result<SessionSnapshot, String> {
        let snapshot = self.require_snapshot()?;
        let response = snapshot
            .client
            .get(endpoint(&snapshot.base_url, "/v1/operator/me"))
            .bearer_auth(snapshot.bearer.as_str())
            .send()
            .await
            .map_err(|_| "operator_api_unavailable".to_string())?;
        if response.status() == StatusCode::UNAUTHORIZED {
            self.clear_if_matches(&snapshot);
            return Err("operator_session_revoked".to_string());
        }
        if !response.status().is_success() {
            return Err("operator_request_failed".to_string());
        }
        let operator: OperatorIdentity = response
            .json()
            .await
            .map_err(|_| "operator_response_invalid".to_string())?;
        if !lifecycle_session_allowed(&operator.role, operator.must_change_password, false) {
            return Err(if operator.must_change_password {
                "operator_password_change_required".to_string()
            } else {
                "operator_forbidden".to_string()
            });
        }
        if let Ok(mut guard) = self.session.lock() {
            if let Some(session) = guard.as_mut() {
                if session.base_url == snapshot.base_url
                    && session.bearer.as_str() == snapshot.bearer.as_str()
                {
                    session.operator = operator;
                }
            }
        }
        Ok(snapshot)
    }

    fn snapshot(&self) -> Result<Option<SessionSnapshot>, String> {
        let guard = self
            .session
            .lock()
            .map_err(|_| "operator_session_unavailable".to_string())?;
        Ok(guard.as_ref().map(|session| SessionSnapshot {
            base_url: session.base_url.clone(),
            client: session.client.clone(),
            bearer: Zeroizing::new(session.bearer.to_string()),
        }))
    }

    fn require_snapshot(&self) -> Result<SessionSnapshot, String> {
        self.snapshot()?
            .ok_or_else(|| "operator_authentication_required".to_string())
    }

    fn detach_current_session(&self) -> Option<SessionSnapshot> {
        self.session
            .lock()
            .ok()?
            .take()
            .map(|session| SessionSnapshot {
                base_url: session.base_url,
                client: session.client,
                bearer: session.bearer,
            })
    }

    async fn revoke_snapshot(snapshot: SessionSnapshot) {
        let base_url = snapshot.base_url.clone();
        let _ = Self::revoke_snapshot_at(snapshot, &base_url).await;
    }

    async fn revoke_snapshot_at(snapshot: SessionSnapshot, base_url: &str) -> Result<(), String> {
        let response = snapshot
            .client
            .post(endpoint(base_url, "/v1/operator/logout"))
            .bearer_auth(snapshot.bearer.as_str())
            .send()
            .await
            .map_err(|_| "operator_api_unavailable".to_string())?;
        if !response.status().is_success() {
            return Err("operator_logout_failed".to_string());
        }
        Ok(())
    }

    fn clear_if_matches(&self, snapshot: &SessionSnapshot) {
        if let Ok(mut guard) = self.session.lock() {
            let matches = guard.as_ref().is_some_and(|session| {
                session.base_url == snapshot.base_url
                    && session.bearer.as_str() == snapshot.bearer.as_str()
            });
            if matches {
                guard.take();
            }
        }
    }
}

fn normalize_base_url(value: &str) -> Result<String, String> {
    if value.trim() != value {
        return Err("controller_url_invalid".to_string());
    }
    let authority = value
        .split_once("://")
        .map(|(_, remainder)| remainder.split(['/', '?', '#']).next().unwrap_or_default())
        .ok_or_else(|| "controller_url_invalid".to_string())?;
    if authority.contains('@') {
        return Err("controller_url_invalid".to_string());
    }
    let parsed = Url::parse(value).map_err(|_| "controller_url_invalid".to_string())?;
    if parsed.scheme() != "https"
        || parsed.host_str().is_none()
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || parsed.query().is_some()
        || parsed.fragment().is_some()
        || !matches!(parsed.path(), "" | "/")
    {
        return Err("controller_url_invalid".to_string());
    }
    Ok(parsed.as_str().trim_end_matches('/').to_string())
}

fn private_root_client(root_der: &[u8]) -> Result<Client, String> {
    let certificate = reqwest::Certificate::from_der(root_der)
        .map_err(|_| "controller_trust_store_invalid".to_string())?;
    Client::builder()
        .timeout(Duration::from_secs(10))
        .redirect(Policy::none())
        .no_proxy()
        .tls_certs_only([certificate])
        .build()
        .map_err(|_| "controller_trust_store_invalid".to_string())
}

fn endpoint(base_url: &str, path: &str) -> String {
    format!("{base_url}{path}")
}

fn lifecycle_role_allowed(role: &str, controller_node: bool) -> bool {
    if controller_node {
        matches!(role, "OWNER" | "ADMIN")
    } else {
        matches!(role, "OWNER" | "ADMIN" | "OPERATOR")
    }
}

fn lifecycle_session_allowed(
    role: &str,
    must_change_password: bool,
    controller_node: bool,
) -> bool {
    !must_change_password && lifecycle_role_allowed(role, controller_node)
}

fn valid_worker_id(worker_id: &str) -> bool {
    worker_id.len() == 36
        && [8, 13, 18, 23]
            .into_iter()
            .all(|index| worker_id.as_bytes()[index] == b'-')
        && worker_id.bytes().enumerate().all(|(index, byte)| {
            [8, 13, 18, 23].contains(&index)
                || byte.is_ascii_digit()
                || (b'a'..=b'f').contains(&byte)
        })
}

fn valid_drain_reason(reason_code: &str) -> bool {
    matches!(
        reason_code,
        "DESKTOP_QUIT" | "DESKTOP_RESTART" | "DESKTOP_LEGACY_CUTOVER" | "DESKTOP_ROLLBACK"
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use rcgen::{
        BasicConstraints, CertificateParams, ExtendedKeyUsagePurpose, IsCa, Issuer, KeyPair,
        KeyUsagePurpose, PKCS_ECDSA_P256_SHA256,
    };
    use rustls::{pki_types::CertificateDer, ServerConfig, ServerConnection, StreamOwned};
    use std::{
        io::{Read, Write},
        net::TcpListener,
        sync::mpsc,
        sync::Arc,
        thread,
        time::Instant,
    };
    use time::OffsetDateTime;

    fn install_session(state: &OperatorAuthState, base_url: &str, bearer: &str) {
        let root = test_root_der();
        install_session_with_client(
            state,
            base_url,
            bearer,
            private_root_client(&root).expect("test private-root client"),
        );
    }

    fn install_session_with_client(
        state: &OperatorAuthState,
        base_url: &str,
        bearer: &str,
        client: Client,
    ) {
        *state.session.lock().expect("Operator session mutex") = Some(Session {
            base_url: base_url.to_string(),
            client,
            bearer: Zeroizing::new(bearer.to_string()),
            operator: OperatorIdentity {
                id: "synthetic-owner-id".to_string(),
                username: "synthetic-owner".to_string(),
                role: "OWNER".to_string(),
                must_change_password: false,
                expires_at: "2026-10-03T18:00:00Z".to_string(),
            },
        });
    }

    fn test_root_der() -> Vec<u8> {
        let now = OffsetDateTime::now_utc();
        let mut params = CertificateParams::default();
        params.is_ca = IsCa::Ca(BasicConstraints::Constrained(0));
        params.key_usages = vec![KeyUsagePurpose::KeyCertSign, KeyUsagePurpose::CrlSign];
        params.not_before = now - time::Duration::minutes(5);
        params.not_after = now + time::Duration::days(365);
        let key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).expect("test root key");
        params
            .self_signed(&key)
            .expect("test root certificate")
            .der()
            .as_ref()
            .to_vec()
    }

    fn login_redirect_server() -> (
        String,
        Vec<u8>,
        std::sync::mpsc::Receiver<String>,
        thread::JoinHandle<()>,
    ) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("loopback listener");
        let address = listener.local_addr().expect("listener address");
        let now = OffsetDateTime::now_utc();
        let mut root_params = CertificateParams::default();
        root_params.is_ca = IsCa::Ca(BasicConstraints::Constrained(0));
        root_params.key_usages = vec![KeyUsagePurpose::KeyCertSign, KeyUsagePurpose::CrlSign];
        root_params.not_before = now - time::Duration::minutes(5);
        root_params.not_after = now + time::Duration::days(365);
        let root_key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).expect("test root key");
        let root = root_params
            .self_signed(&root_key)
            .expect("test root certificate");
        let issuer = Issuer::from_ca_cert_der(
            &CertificateDer::from(root.der().as_ref().to_vec()),
            root_key,
        )
        .expect("test root issuer");
        let mut leaf_params =
            CertificateParams::new(vec!["127.0.0.1".to_string()]).expect("test leaf parameters");
        leaf_params.is_ca = IsCa::ExplicitNoCa;
        leaf_params.key_usages = vec![KeyUsagePurpose::DigitalSignature];
        leaf_params.extended_key_usages = vec![ExtendedKeyUsagePurpose::ServerAuth];
        leaf_params.not_before = now - time::Duration::minutes(5);
        leaf_params.not_after = now + time::Duration::days(90);
        let leaf_key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).expect("test leaf key");
        let leaf = leaf_params
            .signed_by(&leaf_key, &issuer)
            .expect("test leaf certificate");
        let server_config =
            ServerConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
                .with_safe_default_protocol_versions()
                .expect("test TLS versions")
                .with_no_client_auth()
                .with_single_cert(
                    vec![
                        CertificateDer::from(leaf.der().as_ref().to_vec()),
                        CertificateDer::from(root.der().as_ref().to_vec()),
                    ],
                    rustls::pki_types::PrivatePkcs8KeyDer::from(leaf_key.serialize_der()).into(),
                )
                .expect("test TLS identity");
        let root_der = root.der().as_ref().to_vec();
        let (sender, receiver) = std::sync::mpsc::channel();
        let server = thread::spawn(move || {
            let (stream, _) = listener.accept().expect("login request");
            let connection =
                ServerConnection::new(Arc::new(server_config)).expect("test TLS server connection");
            let mut stream = StreamOwned::new(connection, stream);
            let mut request = Vec::new();
            let mut buffer = [0; 2048];
            loop {
                let length = stream.read(&mut buffer).expect("read login request");
                if length == 0 {
                    break;
                }
                request.extend_from_slice(&buffer[..length]);
                if request.windows(4).any(|window| window == b"\r\n\r\n") {
                    break;
                }
            }
            let request_line = String::from_utf8_lossy(&request)
                .lines()
                .next()
                .unwrap_or_default()
                .to_string();
            sender.send(request_line).expect("request receiver");
            stream
                .write_all(
                    b"HTTP/1.1 302 Found\r\nLocation: https://127.0.0.1:1/stolen\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                )
                .expect("write redirect response");
        });
        (format!("https://{address}"), root_der, receiver, server)
    }

    struct LogoutRequestEvidence {
        target: String,
        authorization: String,
    }

    struct WorkerDrainRequestEvidence {
        target: String,
        authorization: String,
        body: String,
    }

    #[derive(Clone, Copy)]
    enum WorkerEndpointFailure {
        Status(u16),
        Disconnect,
        UnknownStatus,
    }

    fn logout_server() -> (
        String,
        Client,
        mpsc::Receiver<LogoutRequestEvidence>,
        thread::JoinHandle<()>,
    ) {
        let (base_url, client, receiver, release_response, server) = logout_server_with_gate();
        release_response
            .send(())
            .expect("release ordinary logout response");
        (base_url, client, receiver, server)
    }

    fn logout_server_with_gate() -> (
        String,
        Client,
        mpsc::Receiver<LogoutRequestEvidence>,
        mpsc::Sender<()>,
        thread::JoinHandle<()>,
    ) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("loopback listener");
        listener
            .set_nonblocking(true)
            .expect("bounded test listener");
        let address = listener.local_addr().expect("listener address");
        let now = OffsetDateTime::now_utc();
        let mut root_params = CertificateParams::default();
        root_params.is_ca = IsCa::Ca(BasicConstraints::Constrained(0));
        root_params.key_usages = vec![KeyUsagePurpose::KeyCertSign, KeyUsagePurpose::CrlSign];
        root_params.not_before = now - time::Duration::minutes(5);
        root_params.not_after = now + time::Duration::days(365);
        let root_key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).expect("test root key");
        let root = root_params
            .self_signed(&root_key)
            .expect("test root certificate");
        let issuer = Issuer::from_ca_cert_der(
            &CertificateDer::from(root.der().as_ref().to_vec()),
            root_key,
        )
        .expect("test root issuer");
        let mut leaf_params =
            CertificateParams::new(vec!["127.0.0.1".to_string()]).expect("test leaf parameters");
        leaf_params.is_ca = IsCa::ExplicitNoCa;
        leaf_params.key_usages = vec![KeyUsagePurpose::DigitalSignature];
        leaf_params.extended_key_usages = vec![ExtendedKeyUsagePurpose::ServerAuth];
        leaf_params.not_before = now - time::Duration::minutes(5);
        leaf_params.not_after = now + time::Duration::days(90);
        let leaf_key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).expect("test leaf key");
        let leaf = leaf_params
            .signed_by(&leaf_key, &issuer)
            .expect("test leaf certificate");
        let server_config =
            ServerConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
                .with_safe_default_protocol_versions()
                .expect("test TLS versions")
                .with_no_client_auth()
                .with_single_cert(
                    vec![
                        CertificateDer::from(leaf.der().as_ref().to_vec()),
                        CertificateDer::from(root.der().as_ref().to_vec()),
                    ],
                    rustls::pki_types::PrivatePkcs8KeyDer::from(leaf_key.serialize_der()).into(),
                )
                .expect("test TLS identity");
        let client = private_root_client(root.der().as_ref()).expect("test private-root client");
        let (sender, receiver) = mpsc::channel();
        let (release_response, response_released) = mpsc::channel();
        let server = thread::spawn(move || {
            let accept_deadline = Instant::now() + Duration::from_secs(10);
            let (stream, _) = loop {
                match listener.accept() {
                    Ok(connection) => break connection,
                    Err(error)
                        if error.kind() == std::io::ErrorKind::WouldBlock
                            && Instant::now() < accept_deadline =>
                    {
                        thread::sleep(Duration::from_millis(10));
                    }
                    Err(error) => panic!("bounded logout accept failed: {error}"),
                }
            };
            stream
                .set_nonblocking(false)
                .expect("blocking accepted test stream");
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .expect("bounded logout request read");
            let connection =
                ServerConnection::new(Arc::new(server_config)).expect("test TLS server connection");
            let mut stream = StreamOwned::new(connection, stream);
            let mut request = Vec::new();
            let mut buffer = [0; 2048];
            loop {
                let length = stream.read(&mut buffer).expect("read logout request");
                if length == 0 {
                    break;
                }
                request.extend_from_slice(&buffer[..length]);
                if request.windows(4).any(|window| window == b"\r\n\r\n") {
                    break;
                }
            }
            let request_text = String::from_utf8_lossy(&request);
            let target = request_text.lines().next().unwrap_or_default().to_string();
            let authorization = request_text
                .lines()
                .find(|line| line.to_ascii_lowercase().starts_with("authorization:"))
                .unwrap_or_default()
                .to_string();
            sender
                .send(LogoutRequestEvidence {
                    target,
                    authorization,
                })
                .expect("logout request receiver");
            response_released
                .recv_timeout(Duration::from_secs(5))
                .expect("release logout response");
            stream
                .write_all(
                    b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                )
                .expect("write logout response");
        });
        (
            format!("https://{address}"),
            client,
            receiver,
            release_response,
            server,
        )
    }

    fn worker_drain_api_server(
        role: &'static str,
        endpoint_failure: Option<WorkerEndpointFailure>,
    ) -> (
        String,
        Vec<u8>,
        mpsc::Receiver<WorkerDrainRequestEvidence>,
        thread::JoinHandle<()>,
    ) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("loopback Worker API listener");
        listener
            .set_nonblocking(true)
            .expect("nonblocking test listener");
        let address = listener.local_addr().expect("Worker API listener address");
        let now = OffsetDateTime::now_utc();
        let mut root_params = CertificateParams::default();
        root_params.is_ca = IsCa::Ca(BasicConstraints::Constrained(0));
        root_params.key_usages = vec![KeyUsagePurpose::KeyCertSign, KeyUsagePurpose::CrlSign];
        root_params.not_before = now - time::Duration::minutes(5);
        root_params.not_after = now + time::Duration::days(365);
        let root_key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).expect("test root key");
        let root = root_params
            .self_signed(&root_key)
            .expect("test root certificate");
        let issuer = Issuer::from_ca_cert_der(
            &CertificateDer::from(root.der().as_ref().to_vec()),
            root_key,
        )
        .expect("test root issuer");
        let mut leaf_params =
            CertificateParams::new(vec!["127.0.0.1".to_string()]).expect("test leaf parameters");
        leaf_params.is_ca = IsCa::ExplicitNoCa;
        leaf_params.key_usages = vec![KeyUsagePurpose::DigitalSignature];
        leaf_params.extended_key_usages = vec![ExtendedKeyUsagePurpose::ServerAuth];
        leaf_params.not_before = now - time::Duration::minutes(5);
        leaf_params.not_after = now + time::Duration::days(90);
        let leaf_key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).expect("test leaf key");
        let leaf = leaf_params
            .signed_by(&leaf_key, &issuer)
            .expect("test leaf certificate");
        let server_config =
            ServerConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
                .with_safe_default_protocol_versions()
                .expect("test TLS versions")
                .with_no_client_auth()
                .with_single_cert(
                    vec![
                        CertificateDer::from(leaf.der().as_ref().to_vec()),
                        CertificateDer::from(root.der().as_ref().to_vec()),
                    ],
                    rustls::pki_types::PrivatePkcs8KeyDer::from(leaf_key.serialize_der()).into(),
                )
                .expect("test TLS identity");
        let root_der = root.der().as_ref().to_vec();
        let (sender, receiver) = mpsc::channel();
        let server = thread::spawn(move || {
            let mut drain_status_reads = 0;
            let request_limit = if endpoint_failure.is_some() { 2 } else { 6 };
            for _ in 0..request_limit {
                let accept_deadline = Instant::now() + Duration::from_secs(10);
                let (socket, _) = loop {
                    match listener.accept() {
                        Ok(connection) => break connection,
                        Err(error)
                            if error.kind() == std::io::ErrorKind::WouldBlock
                                && Instant::now() < accept_deadline =>
                        {
                            thread::sleep(Duration::from_millis(10));
                        }
                        Err(error) => panic!("bounded Worker API accept failed: {error}"),
                    }
                };
                socket
                    .set_nonblocking(false)
                    .expect("blocking accepted Worker API socket");
                socket
                    .set_read_timeout(Some(Duration::from_secs(5)))
                    .expect("bounded Worker API read");
                let connection = ServerConnection::new(Arc::new(server_config.clone()))
                    .expect("test TLS server connection");
                let mut stream = StreamOwned::new(connection, socket);
                let mut request = Vec::new();
                let mut buffer = [0_u8; 2048];
                loop {
                    let length = stream.read(&mut buffer).expect("read Worker API request");
                    if length == 0 {
                        break;
                    }
                    request.extend_from_slice(&buffer[..length]);
                    let Some(header_end) = request.windows(4).position(|part| part == b"\r\n\r\n")
                    else {
                        continue;
                    };
                    let headers = String::from_utf8_lossy(&request[..header_end]);
                    let content_length = headers
                        .lines()
                        .find_map(|line| {
                            let (name, value) = line.split_once(':')?;
                            name.eq_ignore_ascii_case("content-length")
                                .then(|| value.trim().parse::<usize>().ok())
                                .flatten()
                        })
                        .unwrap_or(0);
                    if request.len() >= header_end + 4 + content_length {
                        break;
                    }
                }
                let request_text = String::from_utf8_lossy(&request);
                let request_line = request_text.lines().next().unwrap_or_default().to_string();
                let authorization = request_text
                    .lines()
                    .find(|line| line.to_ascii_lowercase().starts_with("authorization:"))
                    .unwrap_or_default()
                    .to_string();
                let header_end = request
                    .windows(4)
                    .position(|part| part == b"\r\n\r\n")
                    .expect("HTTP request headers");
                let body = String::from_utf8_lossy(&request[header_end + 4..]).to_string();
                sender
                    .send(WorkerDrainRequestEvidence {
                        target: request_line.clone(),
                        authorization,
                        body,
                    })
                    .expect("Worker API request receiver");

                let (status, response_body) = if request_line.starts_with("GET /v1/operator/me ") {
                    (
                        "200 OK",
                        format!(
                            "{{\"id\":\"synthetic-operator\",\"username\":\"operator\",\"role\":\"{role}\",\"must_change_password\":false,\"expires_at\":\"2026-10-05T00:00:00Z\"}}"
                        ),
                    )
                } else if let Some(WorkerEndpointFailure::Disconnect) = endpoint_failure {
                    break;
                } else if let Some(WorkerEndpointFailure::Status(status)) = endpoint_failure {
                    let reason = match status {
                        401 => "Unauthorized",
                        403 => "Forbidden",
                        _ => panic!("unsupported synthetic Worker endpoint status"),
                    };
                    (
                        if status == 401 {
                            "401 Unauthorized"
                        } else {
                            "403 Forbidden"
                        },
                        reason.to_string(),
                    )
                } else if matches!(endpoint_failure, Some(WorkerEndpointFailure::UnknownStatus)) {
                    (
                        "200 OK",
                        r#"{"status":"UNKNOWN","active_browser_sessions":0,"running_worker_jobs":0,"quiescent":true}"#.to_string(),
                    )
                } else if request_line.starts_with("POST /v1/workers/") {
                    (
                        "200 OK",
                        r#"{"status":"DRAINING","active_browser_sessions":1,"running_worker_jobs":2,"quiescent":false}"#.to_string(),
                    )
                } else if request_line.starts_with("GET /v1/workers/") {
                    drain_status_reads += 1;
                    if drain_status_reads == 1 {
                        (
                            "200 OK",
                            r#"{"status":"DRAINING","active_browser_sessions":1,"running_worker_jobs":2,"quiescent":false}"#.to_string(),
                        )
                    } else {
                        (
                            "200 OK",
                            r#"{"status":"OFFLINE","active_browser_sessions":0,"running_worker_jobs":0,"quiescent":true}"#.to_string(),
                        )
                    }
                } else {
                    panic!("unexpected test Worker API request: {request_line}");
                };
                let response = format!(
                    "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    response_body.len(),
                    response_body
                );
                stream
                    .write_all(response.as_bytes())
                    .expect("write Worker API response");
            }
        });
        (format!("https://{address}"), root_der, receiver, server)
    }

    #[test]
    fn detaching_operator_session_removes_it_before_revocation() {
        let state = OperatorAuthState::default();
        install_session(&state, "https://127.0.0.1:1", "synthetic-session-a");

        let detached = state
            .detach_current_session()
            .expect("current session detached");

        assert_eq!(detached.bearer.as_str(), "synthetic-session-a");
        assert!(state
            .session
            .lock()
            .expect("Operator session mutex")
            .is_none());
    }

    #[test]
    fn endpoint_migration_revokes_detached_session_through_new_origin_after_old_is_offline() {
        let (new_origin, client, request_receiver, release_response, server) =
            logout_server_with_gate();
        let old_listener = TcpListener::bind("127.0.0.1:0").expect("old HTTPS origin");
        let old_address = old_listener.local_addr().expect("old HTTPS address");
        let old_origin = format!("https://{old_address}");
        drop(old_listener);

        let state = Arc::new(OperatorAuthState::default());
        install_session_with_client(&state, &old_origin, "synthetic-session-a", client.clone());
        let revoke_state = Arc::clone(&state);
        let revoke_origin = new_origin.clone();
        let revoke = thread::spawn(move || {
            tauri::async_runtime::block_on(revoke_state.revoke_session_at(&revoke_origin))
        });

        let request = request_receiver
            .recv_timeout(Duration::from_secs(10))
            .expect("new endpoint received logout");
        assert_eq!(request.target, "POST /v1/operator/logout HTTP/1.1");
        assert_eq!(
            request.authorization,
            "authorization: Bearer synthetic-session-a"
        );
        assert!(
            state
                .session
                .lock()
                .expect("Operator session mutex")
                .is_none(),
            "old session is detached before the response completes"
        );

        install_session_with_client(&state, &new_origin, "synthetic-session-b", client);
        release_response
            .send(())
            .expect("release delayed logout response");
        assert_eq!(revoke.join().expect("logout task completed"), Ok(()));
        server.join().expect("logout server completed");
        assert_eq!(
            state
                .session
                .lock()
                .expect("Operator session mutex")
                .as_ref()
                .expect("new session remains current")
                .bearer
                .as_str(),
            "synthetic-session-b"
        );
    }

    #[test]
    fn repeated_lock_without_a_session_is_idempotent() {
        let state = OperatorAuthState::default();

        state.lock_session();
        state.lock_session();

        assert!(state
            .session
            .lock()
            .expect("Operator session mutex")
            .is_none());
    }

    #[test]
    fn explicit_logout_revokes_the_detached_current_bearer() {
        let (base_url, client, authorization, server) = logout_server();
        let state = OperatorAuthState::default();
        install_session_with_client(&state, &base_url, "synthetic-logout-session", client);

        tauri::async_runtime::block_on(state.logout());

        assert_eq!(
            authorization
                .recv()
                .expect("captured logout request")
                .authorization,
            "authorization: Bearer synthetic-logout-session"
        );
        server.join().expect("logout server completed");
        assert!(state
            .session
            .lock()
            .expect("Operator session mutex")
            .is_none());
    }

    #[test]
    fn node_lifecycle_role_matrix_is_fixed() {
        for (role, worker_allowed, controller_allowed) in [
            ("OWNER", true, true),
            ("ADMIN", true, true),
            ("OPERATOR", true, false),
            ("VIEWER", false, false),
        ] {
            assert_eq!(
                lifecycle_session_allowed(role, false, false),
                worker_allowed
            );
            assert_eq!(
                lifecycle_session_allowed(role, false, true),
                controller_allowed
            );
            assert!(!lifecycle_session_allowed(role, true, false));
            assert!(!lifecycle_session_allowed(role, true, true));
        }
    }

    #[test]
    fn worker_drain_client_requests_once_then_polls_status_with_private_root_session() {
        let (base_url, root_der, request_receiver, server) =
            worker_drain_api_server("OPERATOR", None);
        let client = private_root_client(&root_der).expect("private-root Worker client");
        let state = OperatorAuthState::default();
        install_session_with_client(&state, &base_url, "synthetic-operator-bearer", client);
        let worker_id = "12345678-1234-4234-8234-123456789abc";

        let requested = tauri::async_runtime::block_on(
            state.request_local_worker_drain(worker_id, "DESKTOP_LEGACY_CUTOVER"),
        )
        .expect("Operator-authorized drain request");
        assert_eq!(requested.status, WorkerStatus::Draining);
        assert_eq!(requested.running_worker_jobs, 2);
        assert!(!requested.quiescent);

        let draining = tauri::async_runtime::block_on(state.local_worker_drain_status(worker_id))
            .expect("first drain status poll");
        assert_eq!(draining.status, WorkerStatus::Draining);
        let offline = tauri::async_runtime::block_on(state.local_worker_drain_status(worker_id))
            .expect("second drain status poll");
        assert_eq!(offline.status, WorkerStatus::Offline);
        assert_eq!(offline.active_browser_sessions, 0);
        assert_eq!(offline.running_worker_jobs, 0);
        assert!(offline.quiescent);

        let evidence = (0..6)
            .map(|_| {
                request_receiver
                    .recv_timeout(Duration::from_secs(5))
                    .expect("captured Worker API request")
            })
            .collect::<Vec<_>>();
        server.join().expect("Worker API test server finished");
        let drain_posts = evidence
            .iter()
            .filter(|request| request.target.starts_with("POST /v1/workers/"))
            .collect::<Vec<_>>();
        let drain_polls = evidence
            .iter()
            .filter(|request| request.target.starts_with("GET /v1/workers/"))
            .collect::<Vec<_>>();
        assert_eq!(drain_posts.len(), 1);
        assert_eq!(drain_polls.len(), 2);
        assert_eq!(
            evidence
                .iter()
                .filter(|request| request.target.starts_with("GET /v1/operator/me "))
                .count(),
            3
        );
        assert!(evidence.iter().all(|request| {
            request.authorization == "authorization: Bearer synthetic-operator-bearer"
        }));
        let post_body: serde_json::Value =
            serde_json::from_str(&drain_posts[0].body).expect("drain request body");
        assert_eq!(post_body["reason_code"], "DESKTOP_LEGACY_CUTOVER");
        let serialized = serde_json::to_string(&offline).expect("safe drain status DTO");
        assert!(!serialized.contains("synthetic-operator-bearer"));
    }

    fn assert_worker_endpoint_failure(
        method: &str,
        endpoint_failure: WorkerEndpointFailure,
        expected_code: &str,
        clear_session: bool,
    ) {
        let (base_url, root_der, request_receiver, server) =
            worker_drain_api_server("OPERATOR", Some(endpoint_failure));
        let client = private_root_client(&root_der).expect("private-root Worker client");
        let state = OperatorAuthState::default();
        install_session_with_client(&state, &base_url, "synthetic-operator-bearer", client);
        let worker_id = "12345678-1234-4234-8234-123456789abc";
        let result = match method {
            "POST" => tauri::async_runtime::block_on(
                state.request_local_worker_drain(worker_id, "DESKTOP_QUIT"),
            )
            .map(|_| ()),
            "GET" => tauri::async_runtime::block_on(state.local_worker_drain_status(worker_id))
                .map(|_| ()),
            _ => panic!("unsupported Worker endpoint method"),
        };
        assert_eq!(result.unwrap_err(), expected_code);
        assert_eq!(
            state
                .session
                .lock()
                .expect("Operator session mutex")
                .is_none(),
            clear_session
        );

        let evidence = (0..2)
            .map(|_| {
                request_receiver
                    .recv_timeout(Duration::from_secs(5))
                    .expect("captured Operator and Worker endpoint requests")
            })
            .collect::<Vec<_>>();
        server.join().expect("Worker API test server finished");
        assert!(evidence[0].target.starts_with("GET /v1/operator/me "));
        assert!(evidence[1]
            .target
            .starts_with(&format!("{method} /v1/workers/{worker_id}/drain ")));
        assert!(evidence.iter().all(|request| {
            request.authorization == "authorization: Bearer synthetic-operator-bearer"
        }));
    }

    #[test]
    fn worker_drain_endpoint_rechecks_auth_and_maps_revocation_forbidden_and_transport_errors() {
        for method in ["POST", "GET"] {
            assert_worker_endpoint_failure(
                method,
                WorkerEndpointFailure::Status(401),
                "operator_session_revoked",
                true,
            );
            assert_worker_endpoint_failure(
                method,
                WorkerEndpointFailure::Status(403),
                "operator_forbidden",
                false,
            );
            assert_worker_endpoint_failure(
                method,
                WorkerEndpointFailure::Disconnect,
                "worker_drain_unavailable",
                false,
            );
            assert_worker_endpoint_failure(
                method,
                WorkerEndpointFailure::UnknownStatus,
                "worker_drain_response_invalid",
                false,
            );
        }
    }

    #[test]
    fn worker_drain_status_uses_the_exact_server_status_enum() {
        for (wire, expected) in [
            ("REGISTERING", WorkerStatus::Registering),
            ("ONLINE", WorkerStatus::Online),
            ("DEGRADED", WorkerStatus::Degraded),
            ("DRAINING", WorkerStatus::Draining),
            ("OFFLINE", WorkerStatus::Offline),
            ("DISABLED", WorkerStatus::Disabled),
            ("UPGRADE_REQUIRED", WorkerStatus::UpgradeRequired),
        ] {
            let value: WorkerDrainStatus = serde_json::from_value(serde_json::json!({
                "status": wire,
                "active_browser_sessions": 0,
                "running_worker_jobs": 0,
                "quiescent": true
            }))
            .expect("known Worker status parses");
            assert_eq!(value.status, expected);
            assert_eq!(serde_json::to_value(value).unwrap()["status"], wire);
        }

        let unknown = serde_json::from_value::<WorkerDrainStatus>(serde_json::json!({
            "status": "UNKNOWN",
            "active_browser_sessions": 0,
            "running_worker_jobs": 0,
            "quiescent": true
        }));
        assert!(unknown.is_err());
    }

    #[test]
    fn worker_drain_client_accepts_only_fixed_reasons_and_local_uuid() {
        for reason in [
            "DESKTOP_QUIT",
            "DESKTOP_RESTART",
            "DESKTOP_LEGACY_CUTOVER",
            "DESKTOP_ROLLBACK",
        ] {
            assert!(valid_drain_reason(reason));
        }
        assert!(!valid_drain_reason("operator supplied text"));
        assert!(valid_worker_id("12345678-1234-4234-8234-123456789abc"));
        assert!(!valid_worker_id("../other-worker"));
    }

    #[test]
    fn controller_endpoint_reconfiguration_uses_owner_admin_lifecycle_authorization() {
        assert!(lifecycle_session_allowed("OWNER", false, true));
        assert!(lifecycle_session_allowed("ADMIN", false, true));
        assert!(!lifecycle_session_allowed("OPERATOR", false, true));
        assert!(!lifecycle_session_allowed("VIEWER", false, true));
        assert!(!lifecycle_session_allowed("OWNER", true, true));
        assert!(!lifecycle_session_allowed("ADMIN", true, true));
    }

    #[test]
    fn operator_http_payloads_use_snake_case_and_tauri_dtos_use_camel_case() {
        let response: LoginResponse = serde_json::from_value(serde_json::json!({
            "access_token": "synthetic-session-value",
            "token_type": "Bearer",
            "expires_at": "2026-10-03T18:00:00Z",
            "operator": {
                "id": "owner-id",
                "username": "first-owner",
                "role": "OWNER",
                "must_change_password": false,
                "expires_at": "2026-10-03T18:00:00Z"
            }
        }))
        .expect("snake_case Operator login response");
        assert_eq!(response.access_token, "synthetic-session-value");

        let identity = serde_json::to_value(response.operator).expect("Tauri identity DTO");
        assert_eq!(identity["mustChangePassword"], false);
        assert_eq!(identity["expiresAt"], "2026-10-03T18:00:00Z");
        assert!(identity.get("must_change_password").is_none());
        assert!(identity
            .to_string()
            .find("synthetic-session-value")
            .is_none());

        let request = serde_json::to_value(ChangePasswordRequest {
            new_password: "synthetic-test-passphrase",
        })
        .expect("FastAPI password request");
        assert_eq!(request["new_password"], "synthetic-test-passphrase");
        assert!(request.get("newPassword").is_none());

        let create_response: CreateUserResponse = serde_json::from_value(serde_json::json!({
            "user": {
                "id": "operator-id",
                "username": "new-operator",
                "role": "OPERATOR",
                "enabled": true,
                "must_change_password": true,
                "created_at": "2026-10-03T18:00:00Z"
            },
            "temporary_password": "synthetic-one-time-value"
        }))
        .expect("snake_case Operator user response");
        let created = CreatedOperatorUser {
            user: create_response.user,
            temporary_password: create_response.temporary_password,
        };
        let created = serde_json::to_value(created).expect("Tauri created-user DTO");
        assert_eq!(created["temporaryPassword"], "synthetic-one-time-value");
        assert_eq!(created["user"]["mustChangePassword"], true);
        assert_eq!(created["user"]["createdAt"], "2026-10-03T18:00:00Z");
    }

    #[test]
    fn controller_url_rejects_credentials_and_paths() {
        assert!(normalize_base_url("https://controller.test/").is_ok());
        assert!(normalize_base_url("http://127.0.0.1:8080/").is_err());
        assert!(normalize_base_url("http://[::1]:8080/").is_err());
        assert!(normalize_base_url("http://localhost:8080/").is_err());
        assert!(normalize_base_url("http://controller.test").is_err());
        let url_with_credentials = ["https://user", ":", "secret", "@controller.test"].concat();
        assert!(normalize_base_url(&url_with_credentials).is_err());
        assert!(normalize_base_url("https://controller.test/api").is_err());
        assert!(normalize_base_url("https://@controller.test").is_err());
        assert!(normalize_base_url(" https://controller.test ").is_err());
    }

    #[test]
    fn operator_login_uses_verified_private_root_and_does_not_follow_redirects() {
        let (base_url, root_der, request_line, server) = login_redirect_server();
        let state = OperatorAuthState::default();

        let result = tauri::async_runtime::block_on(state.login(
            &base_url,
            &root_der,
            "synthetic-owner",
            "synthetic-password",
        ));

        assert_eq!(result.unwrap_err(), "operator_login_failed");
        assert_eq!(
            request_line.recv().expect("captured login route"),
            "POST /v1/operator/login HTTP/1.1"
        );
        server.join().expect("redirect server completed");
    }
}
