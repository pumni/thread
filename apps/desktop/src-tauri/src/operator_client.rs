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
        let _ = snapshot
            .client
            .post(endpoint(&snapshot.base_url, "/v1/operator/logout"))
            .bearer_auth(snapshot.bearer.as_str())
            .send()
            .await;
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

    fn logout_server() -> (
        String,
        Client,
        mpsc::Receiver<String>,
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
        let client = private_root_client(root.der().as_ref()).expect("test private-root client");
        let (sender, receiver) = mpsc::channel();
        let server = thread::spawn(move || {
            let (stream, _) = listener.accept().expect("logout request");
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
            let authorization = String::from_utf8_lossy(&request)
                .lines()
                .find(|line| line.to_ascii_lowercase().starts_with("authorization:"))
                .unwrap_or_default()
                .to_string();
            sender.send(authorization).expect("authorization receiver");
            stream
                .write_all(
                    b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                )
                .expect("write logout response");
        });
        (format!("https://{address}"), client, receiver, server)
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
    fn delayed_revocation_uses_only_the_detached_bearer_after_a_new_login() {
        let (base_url, client, authorization, server) = logout_server();
        let state = OperatorAuthState::default();
        install_session_with_client(&state, &base_url, "synthetic-session-a", client.clone());
        let detached = state.detach_current_session().expect("session A detached");
        install_session_with_client(&state, &base_url, "synthetic-session-b", client);

        tauri::async_runtime::block_on(OperatorAuthState::revoke_snapshot(detached));

        assert_eq!(
            authorization.recv().expect("captured logout bearer"),
            "authorization: Bearer synthetic-session-a"
        );
        server.join().expect("logout server completed");
        assert_eq!(
            state
                .session
                .lock()
                .expect("Operator session mutex")
                .as_ref()
                .expect("session B remains current")
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
            authorization.recv().expect("captured logout bearer"),
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
