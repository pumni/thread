use std::net::IpAddr;
use std::sync::Mutex;
use std::time::Duration;

use reqwest::{redirect::Policy, Client, StatusCode, Url};
use serde::{Deserialize, Serialize};
use zeroize::Zeroizing;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct OperatorIdentity {
    pub id: String,
    pub username: String,
    pub role: String,
    pub must_change_password: bool,
    pub expires_at: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct OperatorUser {
    pub id: String,
    pub username: String,
    pub role: String,
    pub enabled: bool,
    pub must_change_password: bool,
    pub created_at: String,
}

#[derive(Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct CreatedOperatorUser {
    pub user: OperatorUser,
    pub temporary_password: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct LoginResponse {
    access_token: String,
    operator: OperatorIdentity,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
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
#[serde(rename_all = "camelCase")]
struct ChangePasswordRequest<'a> {
    new_password: &'a str,
}

struct Session {
    base_url: String,
    bearer: Zeroizing<String>,
    operator: OperatorIdentity,
}

struct SessionSnapshot {
    base_url: String,
    bearer: Zeroizing<String>,
}

pub(crate) struct OperatorAuthState {
    session: Mutex<Option<Session>>,
    client: Client,
}

impl Default for OperatorAuthState {
    fn default() -> Self {
        Self {
            session: Mutex::new(None),
            client: Client::builder()
                .timeout(Duration::from_secs(10))
                .redirect(Policy::none())
                .no_proxy()
                .build()
                .expect("fixed Operator HTTP client options are valid"),
        }
    }
}

impl OperatorAuthState {
    pub(crate) async fn login(
        &self,
        base_url: &str,
        username: &str,
        password: &str,
    ) -> Result<OperatorIdentity, String> {
        let base_url = normalize_base_url(base_url)?;
        let url = endpoint(&base_url, "/v1/operator/login");
        let response = self
            .client
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
            bearer,
            operator: operator.clone(),
        });
        Ok(operator)
    }

    pub(crate) async fn current(&self) -> Result<Option<OperatorIdentity>, String> {
        let Some(snapshot) = self.snapshot()? else {
            return Ok(None);
        };
        let response = self
            .client
            .get(endpoint(&snapshot.base_url, "/v1/operator/me"))
            .bearer_auth(snapshot.bearer.as_str())
            .send()
            .await
            .map_err(|_| "operator_api_unavailable".to_string())?;
        if response.status() == StatusCode::UNAUTHORIZED {
            self.clear_if_matches(&snapshot);
            return Ok(None);
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
        let snapshot = self.session.lock().ok().and_then(|mut guard| {
            guard.take().map(|session| SessionSnapshot {
                base_url: session.base_url,
                bearer: session.bearer,
            })
        });
        if let Some(snapshot) = snapshot {
            let _ = self
                .client
                .post(endpoint(&snapshot.base_url, "/v1/operator/logout"))
                .bearer_auth(snapshot.bearer.as_str())
                .send()
                .await;
        }
    }

    pub(crate) async fn list_users(&self) -> Result<Vec<OperatorUser>, String> {
        let snapshot = self.require_snapshot()?;
        let response = self
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
        let response = self
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
        let response = self
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
        let response = self
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
            bearer: Zeroizing::new(session.bearer.to_string()),
        }))
    }

    fn require_snapshot(&self) -> Result<SessionSnapshot, String> {
        self.snapshot()?
            .ok_or_else(|| "operator_authentication_required".to_string())
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
    let parsed = Url::parse(value.trim()).map_err(|_| "controller_url_invalid".to_string())?;
    let loopback = parsed.host_str().is_some_and(|host| {
        host.strip_prefix('[')
            .and_then(|host| host.strip_suffix(']'))
            .unwrap_or(host)
            .parse::<IpAddr>()
            .map(|address| address.is_loopback())
            .unwrap_or(false)
    });
    if !matches!(parsed.scheme(), "http" | "https")
        || (parsed.scheme() == "http" && !loopback)
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
    fn controller_url_rejects_credentials_and_paths() {
        assert!(normalize_base_url("https://controller.test/").is_ok());
        assert!(normalize_base_url("http://127.0.0.1:8080/").is_ok());
        assert!(normalize_base_url("http://[::1]:8080/").is_ok());
        assert!(normalize_base_url("http://localhost:8080/").is_err());
        assert!(normalize_base_url("http://controller.test").is_err());
        let url_with_credentials = ["https://user", ":", "secret", "@controller.test"].concat();
        assert!(normalize_base_url(&url_with_credentials).is_err());
        assert!(normalize_base_url("https://controller.test/api").is_err());
    }
}
