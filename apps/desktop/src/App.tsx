import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  decommissionDevice,
  getDesktopSnapshot,
  listenForTrayQuit,
  operatorBootstrapOwner,
  operatorChangePassword,
  operatorCreateUser,
  operatorCurrent,
  operatorListUsers,
  operatorLogin,
  operatorLogout,
  operatorUpdateUser,
  provisionRole,
  resetUiPreferences,
  requestQuit,
  requestRestart,
  type CreatedOperatorUser,
  type DesktopSnapshot,
  type OperatorIdentity,
  type OperatorRole,
  type OperatorUser,
  type ProvisionedRole,
} from "./desktop";
import { SessionGate } from "./SessionGate";
import "./App.css";

const roleDetails: Record<ProvisionedRole, { label: string; description: string }> = {
  CONTROLLER: {
    label: "Controller",
    description: "Runs the disposable M1 Controller runtime on this PC.",
  },
  WORKER: {
    label: "Worker",
    description: "Runs the existing Worker Agent on this PC.",
  },
  CONSOLE: {
    label: "Console",
    description: "Connects to a Controller without a local business runtime.",
  },
};

const availableRoles: ProvisionedRole[] = ["CONTROLLER", "WORKER", "CONSOLE"];
const operatorAccessMessages: Record<string, string> = {
  operator_authentication_required: "Sign in again before stopping this node.",
  operator_password_change_required: "Change your Workspace password before stopping this node.",
  operator_forbidden: "Only an Owner or Admin can stop this Controller.",
  operator_api_unavailable:
    "The Controller could not verify Operator access. Try again when it is available.",
  operator_request_failed: "The Controller could not verify Operator access. Try again.",
  operator_response_invalid: "The Controller returned an invalid Operator response. Try again.",
};

function App() {
  const queryClient = useQueryClient();
  const [quitRequested, setQuitRequested] = useState(false);
  const [resetRequested, setResetRequested] = useState(false);
  const [resetPhrase, setResetPhrase] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);
  const [operator, setOperator] = useState<OperatorIdentity | null>(null);
  const [operatorLoaded, setOperatorLoaded] = useState(false);
  const [sessionLocked, setSessionLocked] = useState(false);
  const [apiUrl, setApiUrl] = useState("");
  const [loginUsername, setLoginUsername] = useState("");
  const [loginPassword, setLoginPassword] = useState("");
  const [firstOwnerSetup, setFirstOwnerSetup] = useState(false);
  const [restartRequested, setRestartRequested] = useState(false);
  const [operatorUsers, setOperatorUsers] = useState<OperatorUser[]>([]);
  const [newOperatorUsername, setNewOperatorUsername] = useState("");
  const [newOperatorRole, setNewOperatorRole] = useState<OperatorRole>("VIEWER");
  const [createdOperatorUser, setCreatedOperatorUser] = useState<CreatedOperatorUser | null>(null);
  const [newPassword, setNewPassword] = useState("");
  const snapshotQuery = useQuery({
    queryKey: ["desktop-snapshot"],
    queryFn: getDesktopSnapshot,
    refetchInterval: 1_000,
    retry: false,
  });

  useEffect(() => {
    let mounted = true;
    void operatorCurrent()
      .then((current) => {
        if (mounted) setOperator(current ?? null);
      })
      .catch(() => {
        if (mounted) setOperator(null);
      })
      .finally(() => {
        if (mounted) setOperatorLoaded(true);
      });
    return () => {
      mounted = false;
    };
  }, []);

  useEffect(() => {
    if (snapshotQuery.data?.role === "CONTROLLER" && snapshotQuery.data.supervisor.endpoint) {
      setApiUrl(snapshotQuery.data.supervisor.endpoint);
    }
  }, [snapshotQuery.data?.role, snapshotQuery.data?.supervisor.endpoint]);

  useEffect(() => {
    if (!operator || operator.mustChangePassword || !["OWNER", "ADMIN"].includes(operator.role)) {
      setOperatorUsers([]);
      return;
    }
    let mounted = true;
    void operatorListUsers()
      .then((users) => {
        if (mounted) setOperatorUsers(users);
      })
      .catch(() => {
        if (mounted) setActionError("Operator access changed. Sign in again to continue.");
      });
    return () => {
      mounted = false;
    };
  }, [operator]);

  useEffect(() => {
    let mounted = true;
    let unlisten: (() => void) | undefined;
    void listenForTrayQuit().then((stopListening) => {
      if (mounted) unlisten = stopListening;
      else stopListening();
    });
    const requestQuitFromTray = () => setQuitRequested(true);
    const lockSessionFromTray = () => setSessionLocked(true);
    window.addEventListener("threads-desktop:quit-requested", requestQuitFromTray);
    window.addEventListener("threads-desktop:session-locked", lockSessionFromTray);
    return () => {
      mounted = false;
      unlisten?.();
      window.removeEventListener("threads-desktop:quit-requested", requestQuitFromTray);
      window.removeEventListener("threads-desktop:session-locked", lockSessionFromTray);
    };
  }, []);

  async function updateSnapshot(action: () => Promise<DesktopSnapshot>): Promise<boolean> {
    setActionError(null);
    try {
      const snapshot = await action();
      queryClient.setQueryData(["desktop-snapshot"], snapshot);
      return true;
    } catch {
      setActionError(
        "The desktop action failed. The local runtime was left in its last confirmed state.",
      );
      await queryClient.invalidateQueries({ queryKey: ["desktop-snapshot"] });
      return false;
    }
  }

  async function handleRoleSelect(role: ProvisionedRole) {
    await updateSnapshot(() => provisionRole(role));
  }

  async function handleDecommission() {
    if (resetPhrase !== "RESET THIS DEVICE") return;
    const completed = await updateSnapshot(() => decommissionDevice(resetPhrase));
    if (!completed) return;
    setResetRequested(false);
    setResetPhrase("");
  }

  async function handleQuit() {
    setActionError(null);
    try {
      await requestQuit();
      setQuitRequested(false);
    } catch (error) {
      await handleOperatorLogout();
      setQuitRequested(false);
      const errorCode =
        typeof error === "string" ? error : error instanceof Error ? error.message : "";
      setActionError(
        operatorAccessMessages[errorCode] ??
          "The node could not stop cleanly. Check the runtime status before retrying.",
      );
    }
  }

  async function handleOperatorLogin() {
    setActionError(null);
    try {
      const signedIn = firstOwnerSetup
        ? await operatorBootstrapOwner(loginUsername, loginPassword)
        : await operatorLogin(apiUrl, loginUsername, loginPassword);
      setOperator(signedIn);
      setSessionLocked(false);
      setLoginPassword("");
      setFirstOwnerSetup(false);
    } catch {
      setActionError(
        firstOwnerSetup
          ? "First Owner setup failed. Check the local Controller and try again."
          : "Sign-in failed. Check the Controller address and credentials.",
      );
    }
  }

  async function handleOperatorLogout() {
    setSessionLocked(true);
    await operatorLogout();
    setOperator(null);
    setOperatorUsers([]);
    setCreatedOperatorUser(null);
    setNewPassword("");
  }

  async function handleSessionLock() {
    setSessionLocked(true);
    await handleOperatorLogout();
    setActionError("Session locked. Sign in again to access protected data.");
  }

  async function handleOperatorCreateUser() {
    if (!newOperatorUsername.trim()) return;
    setActionError(null);
    try {
      const created = await operatorCreateUser(newOperatorUsername, newOperatorRole);
      setCreatedOperatorUser(created);
      setNewOperatorUsername("");
      setOperatorUsers(await operatorListUsers());
    } catch {
      setActionError("The server denied the requested Operator user change.");
    }
  }

  async function handleOperatorUserToggle(user: OperatorUser) {
    setActionError(null);
    try {
      const changed = await operatorUpdateUser(user.id, null, !user.enabled);
      setOperatorUsers((users) => users.map((item) => (item.id === changed.id ? changed : item)));
    } catch {
      setActionError("The server denied the requested Operator user change.");
    }
  }

  async function handleOperatorRoleChange(user: OperatorUser, role: OperatorRole) {
    setActionError(null);
    try {
      const changed = await operatorUpdateUser(user.id, role, null);
      setOperatorUsers((users) => users.map((item) => (item.id === changed.id ? changed : item)));
      const current = await operatorCurrent();
      setOperator(current ?? null);
    } catch {
      setActionError("The server denied the requested Operator user change.");
    }
  }

  async function handlePasswordChange() {
    setActionError(null);
    try {
      setOperator(await operatorChangePassword(newPassword));
      setNewPassword("");
    } catch {
      setActionError("Password change failed. Use at least 12 characters.");
    }
  }

  async function handleRestart() {
    setActionError(null);
    try {
      await requestRestart();
      setRestartRequested(false);
      await handleOperatorLogout();
      await queryClient.invalidateQueries({ queryKey: ["desktop-snapshot"] });
    } catch {
      await handleOperatorLogout();
      setRestartRequested(false);
      setActionError(
        "An active Operator session with permission to restart this node is required.",
      );
    }
  }

  if (snapshotQuery.isPending) {
    return (
      <main className="boot-screen" aria-live="polite">
        Starting Threads Desktop…
      </main>
    );
  }

  if (snapshotQuery.isError || !snapshotQuery.data) {
    return (
      <main className="boot-screen" role="alert">
        <div className="error-mark">!</div>
        <h1>Configuration could not be read</h1>
        <p>
          The node has not been started. Review the local diagnostics before resetting this device.
        </p>
        <button
          type="button"
          className="button button-secondary"
          onClick={() => void snapshotQuery.refetch()}
        >
          Try again
        </button>
      </main>
    );
  }

  if (!operatorLoaded) {
    return (
      <main className="boot-screen" aria-live="polite">
        Checking Operator session…
      </main>
    );
  }

  const snapshot = snapshotQuery.data;
  return (
    <main className="desktop-shell">
      <aside className="sidebar">
        <div className="brand-lockup">
          <div className="brand-mark" aria-hidden="true">
            t
          </div>
          <div>
            <span className="brand-name">threads</span>
            <span className="brand-edition">DESKTOP</span>
          </div>
        </div>

        <div className="sidebar-section-label">DEVICE</div>
        <div className="device-card">
          <span className={`status-dot ${statusClass(snapshot)}`} aria-hidden="true" />
          <div className="device-copy">
            <strong>{snapshot.role ? roleDetails[snapshot.role].label : "Not provisioned"}</strong>
            <span>{statusLabel(snapshot)}</span>
          </div>
        </div>

        <nav className="sidebar-nav" aria-label="Device navigation">
          <button type="button" className="nav-item nav-item-active" aria-current="page">
            <span className="nav-icon" aria-hidden="true">
              ◫
            </span>
            Overview
          </button>
          <button
            type="button"
            className="nav-item"
            disabled
            title="Available in a later Desktop slice"
          >
            <span className="nav-icon" aria-hidden="true">
              ⌁
            </span>
            Accounts
            <span className="nav-soon">LATER</span>
          </button>
          <button
            type="button"
            className="nav-item"
            disabled
            title="Available in a later Desktop slice"
          >
            <span className="nav-icon" aria-hidden="true">
              ◷
            </span>
            Activity
            <span className="nav-soon">LATER</span>
          </button>
        </nav>

        <div className="sidebar-footer">
          <div className="version-line">
            <span>Desktop scaffold</span>
            <span>v0.1</span>
          </div>
          <button
            type="button"
            className="quiet-link"
            onClick={() => void updateSnapshot(resetUiPreferences)}
          >
            Reset UI preferences
          </button>
        </div>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div className="breadcrumb">
            <span>Device</span>
            <span className="crumb-divider">/</span>
            <strong>Overview</strong>
          </div>
          <div className="topbar-actions">
            <span className="environment-pill">
              <span className="environment-dot" />
              LOCAL RUNTIME
            </span>
            {snapshot.role && snapshot.role !== "CONSOLE" && !operator?.mustChangePassword && (
              <button
                type="button"
                className="quiet-link"
                onClick={() => setRestartRequested(true)}
              >
                Restart…
              </button>
            )}
            <button type="button" className="quiet-link" onClick={() => setQuitRequested(true)}>
              Quit…
            </button>
            {operator ? (
              <button
                type="button"
                className="avatar-button"
                onClick={() => void handleOperatorLogout()}
              >
                {operator.username} · {operator.role}
              </button>
            ) : (
              <span className="avatar-button">Signed out</span>
            )}
          </div>
        </header>

        <div className="page-content">
          <div className="page-heading">
            <div>
              <p className="eyebrow">DEVICE WORKSPACE</p>
              <h1>
                {snapshot.role
                  ? `Your ${roleDetails[snapshot.role].label}`
                  : "Choose this PC’s role"}
              </h1>
              <p className="page-subtitle">
                {snapshot.role
                  ? roleDetails[snapshot.role].description
                  : "Provision this device once. Changing a Controller or Worker role requires an explicit device reset."}
              </p>
            </div>
            {snapshot.role && (
              <span className="role-chip">{roleDetails[snapshot.role].label.toUpperCase()}</span>
            )}
          </div>

          {actionError && (
            <div className="inline-error" role="alert">
              {actionError}
            </div>
          )}

          {snapshot.role === null ? (
            <section className="role-grid" aria-label="Select a device role">
              {availableRoles.map((role, index) => (
                <button
                  type="button"
                  className="role-card"
                  key={role}
                  onClick={() => void handleRoleSelect(role)}
                  aria-label={`Provision as ${roleDetails[role].label}`}
                >
                  <span className={`role-symbol role-symbol-${index}`} aria-hidden="true">
                    {roleSymbol(role)}
                  </span>
                  <span className="role-card-title">{roleDetails[role].label}</span>
                  <span className="role-card-description">{roleDetails[role].description}</span>
                  <span className="role-card-action">
                    Choose role <span aria-hidden="true">↗</span>
                  </span>
                </button>
              ))}
            </section>
          ) : (
            <>
              {snapshot.role === "CONTROLLER" && (
                <section className="m1-limitations" aria-label="M1 prototype limits">
                  <strong>Internal M1 prototype — disposable test data only</strong>
                  <span>
                    The runtime is unavailable before this Windows user signs in. Windows logout is
                    unsupported. Operator login is local-only until DX-06 provisions HTTPS for LAN
                    clients. M1 has no portable backup or production durability.
                  </span>
                </section>
              )}
              <section className="overview-grid" aria-label="Runtime overview">
                <article className="surface-card runtime-card">
                  <div className="card-heading">
                    <div>
                      <p className="eyebrow">LOCAL RUNTIME</p>
                      <h2>{runtimeTitle(snapshot)}</h2>
                    </div>
                    <span className={`status-badge status-badge-${statusClass(snapshot)}`}>
                      <span className="status-dot" aria-hidden="true" />
                      {statusLabel(snapshot)}
                    </span>
                  </div>
                  <p className="card-copy">
                    {snapshot.role === "CONSOLE"
                      ? "Console mode is client-only and starts no local helper process."
                      : snapshot.role === "CONTROLLER"
                        ? "The private PostgreSQL cluster, loopback HTTP process, and scheduler run as separately supervised Windows processes."
                        : "The Worker lifecycle remains on its DX-02 mock boundary until its own implementation issue."}
                  </p>
                  <div className="runtime-facts">
                    <div>
                      <span>Role</span>
                      <strong>{roleDetails[snapshot.role].label}</strong>
                    </div>
                    {snapshot.role === "CONTROLLER" ? (
                      <>
                        <div>
                          <span>PostgreSQL</span>
                          <strong>
                            {snapshot.supervisor.postgresProcessId
                              ? `PID ${snapshot.supervisor.postgresProcessId}`
                              : "Stopped"}
                          </strong>
                        </div>
                        <div>
                          <span>HTTP</span>
                          <strong>
                            {snapshot.supervisor.httpProcessId
                              ? `PID ${snapshot.supervisor.httpProcessId}`
                              : "Stopped"}
                          </strong>
                        </div>
                        <div>
                          <span>Scheduler</span>
                          <strong>
                            {snapshot.supervisor.schedulerProcessId
                              ? `PID ${snapshot.supervisor.schedulerProcessId}`
                              : "Stopped"}
                          </strong>
                        </div>
                      </>
                    ) : (
                      <div>
                        <span>
                          {snapshot.role === "WORKER" ? "Helper process" : "Local runtime"}
                        </span>
                        <strong>
                          {snapshot.role === "WORKER"
                            ? snapshot.supervisor.processId
                              ? `PID ${snapshot.supervisor.processId}`
                              : "Stopped"
                            : "None"}
                        </strong>
                      </div>
                    )}
                    <div>
                      <span>Autostart</span>
                      <strong>
                        {snapshot.autostartEnabled ? "Enabled at user sign-in" : "Not enabled"}
                      </strong>
                    </div>
                    {snapshot.supervisor.endpoint && (
                      <div>
                        <span>Local endpoint</span>
                        <strong>{snapshot.supervisor.endpoint}</strong>
                      </div>
                    )}
                    {snapshot.supervisor.controllerId && (
                      <div>
                        <span>Controller ID</span>
                        <strong>{snapshot.supervisor.controllerId}</strong>
                      </div>
                    )}
                  </div>
                  {snapshot.supervisor.diagnosticCode && (
                    <p
                      className="diagnostic-code"
                      role="status"
                      aria-label={`Diagnostic code: ${snapshot.supervisor.diagnosticCode}`}
                    >
                      Diagnostic code: {snapshot.supervisor.diagnosticCode}
                    </p>
                  )}
                </article>

                <article className="surface-card authentication-card">
                  <div className="card-heading">
                    <div>
                      <p className="eyebrow">OPERATOR ACCESS</p>
                      <h2>
                        {operator
                          ? `Signed in as ${operator.username}`
                          : sessionLocked
                            ? "Session locked"
                            : "Sign in to this Workspace"}
                      </h2>
                    </div>
                    <span className="lock-symbol" aria-hidden="true">
                      ⌑
                    </span>
                  </div>
                  {operator ? (
                    <>
                      <SessionGate
                        validSession
                        forceLocked={sessionLocked}
                        idleTimeoutMs={5 * 60 * 1_000}
                        onLock={handleSessionLock}
                        onUnlock={async () => Boolean(await operatorCurrent())}
                      >
                        <div className="runtime-facts">
                          <div>
                            <span>Workspace role</span>
                            <strong>{operator.role}</strong>
                          </div>
                          <div>
                            <span>Session expiry</span>
                            <strong>{new Date(operator.expiresAt).toLocaleString()}</strong>
                          </div>
                        </div>
                        <button
                          type="button"
                          className="quiet-link"
                          onClick={() => void handleOperatorLogout()}
                        >
                          Sign out
                        </button>
                      </SessionGate>
                      {operator.mustChangePassword && (
                        <form
                          className="operator-login-form"
                          onSubmit={(event) => {
                            event.preventDefault();
                            void handlePasswordChange();
                          }}
                        >
                          <label>
                            Set a new password
                            <input
                              type="password"
                              autoComplete="new-password"
                              minLength={12}
                              value={newPassword}
                              onChange={(event) => setNewPassword(event.target.value)}
                              required
                            />
                          </label>
                          <button type="submit" className="button button-primary">
                            Change password
                          </button>
                        </form>
                      )}
                    </>
                  ) : (
                    <form
                      className="operator-login-form"
                      onSubmit={(event) => {
                        event.preventDefault();
                        void handleOperatorLogin();
                      }}
                    >
                      {sessionLocked && (
                        <p className="card-copy" role="status">
                          Sign in again to access protected Operator data.
                        </p>
                      )}
                      {snapshot.role !== "CONTROLLER" && (
                        <label>
                          Controller address
                          <input
                            type="url"
                            autoComplete="url"
                            placeholder="https://controller.example"
                            value={apiUrl}
                            onChange={(event) => setApiUrl(event.target.value)}
                            required
                          />
                        </label>
                      )}
                      <label>
                        Username
                        <input
                          type="text"
                          autoComplete="username"
                          value={loginUsername}
                          onChange={(event) => setLoginUsername(event.target.value)}
                          required
                        />
                      </label>
                      <label>
                        Password
                        <input
                          type="password"
                          autoComplete="current-password"
                          value={loginPassword}
                          onChange={(event) => setLoginPassword(event.target.value)}
                          required
                        />
                      </label>
                      {snapshot.role === "CONTROLLER" && firstOwnerSetup && (
                        <p className="card-copy">
                          This creates the first Owner through the local native Controller process.
                          The password is sent through stdin and never placed in command arguments.
                        </p>
                      )}
                      <button type="submit" className="button button-primary">
                        {firstOwnerSetup ? "Create first Owner" : "Sign in"}
                      </button>
                      {snapshot.role === "CONTROLLER" && (
                        <button
                          type="button"
                          className="quiet-link"
                          onClick={() => setFirstOwnerSetup((enabled) => !enabled)}
                        >
                          {firstOwnerSetup ? "Return to sign in" : "Set up first Owner"}
                        </button>
                      )}
                    </form>
                  )}
                </article>
              </section>

              {operator &&
                !operator.mustChangePassword &&
                ["OWNER", "ADMIN"].includes(operator.role) && (
                  <section className="surface-card operator-users-card" aria-label="Operator users">
                    <div className="card-heading">
                      <div>
                        <p className="eyebrow">WORKSPACE SECURITY</p>
                        <h2>Operator users</h2>
                      </div>
                      <span className="freshness">Server-enforced roles</span>
                    </div>
                    {createdOperatorUser && (
                      <div className="temporary-password" role="status">
                        <strong>One-time password for {createdOperatorUser.user.username}</strong>
                        <code>{createdOperatorUser.temporaryPassword}</code>
                        <span>Share it securely now. It is not stored by Desktop.</span>
                        <button
                          type="button"
                          className="quiet-link"
                          onClick={() => setCreatedOperatorUser(null)}
                        >
                          Dismiss
                        </button>
                      </div>
                    )}
                    <form
                      className="operator-user-create"
                      onSubmit={(event) => {
                        event.preventDefault();
                        void handleOperatorCreateUser();
                      }}
                    >
                      <label>
                        Username
                        <input
                          type="text"
                          autoComplete="off"
                          minLength={3}
                          maxLength={100}
                          value={newOperatorUsername}
                          onChange={(event) => setNewOperatorUsername(event.target.value)}
                          required
                        />
                      </label>
                      <label>
                        Role
                        <select
                          value={newOperatorRole}
                          onChange={(event) =>
                            setNewOperatorRole(event.target.value as OperatorRole)
                          }
                        >
                          {operator.role === "OWNER" && <option value="OWNER">OWNER</option>}
                          {operator.role === "OWNER" && <option value="ADMIN">ADMIN</option>}
                          <option value="OPERATOR">OPERATOR</option>
                          <option value="VIEWER">VIEWER</option>
                        </select>
                      </label>
                      <button type="submit" className="button button-primary">
                        Create user
                      </button>
                    </form>
                    <div className="operator-user-list">
                      {operatorUsers.map((user) => {
                        const mayManage =
                          operator.role === "OWNER" || !["OWNER", "ADMIN"].includes(user.role);
                        const roleOptions: OperatorRole[] =
                          operator.role === "OWNER"
                            ? ["OWNER", "ADMIN", "OPERATOR", "VIEWER"]
                            : ["OPERATOR", "VIEWER"];
                        return (
                          <div className="operator-user-row" key={user.id}>
                            <div>
                              <strong>{user.username}</strong>
                              <span>{user.enabled ? "Enabled" : "Disabled"}</span>
                            </div>
                            <span className="role-chip">{user.role}</span>
                            {mayManage && (
                              <>
                                <select
                                  aria-label={`Role for ${user.username}`}
                                  value={user.role}
                                  onChange={(event) =>
                                    void handleOperatorRoleChange(
                                      user,
                                      event.target.value as OperatorRole,
                                    )
                                  }
                                >
                                  {roleOptions.map((role) => (
                                    <option key={role} value={role}>
                                      {role}
                                    </option>
                                  ))}
                                </select>
                                <button
                                  type="button"
                                  className="quiet-link"
                                  onClick={() => void handleOperatorUserToggle(user)}
                                >
                                  {user.enabled ? "Disable" : "Enable"}
                                </button>
                              </>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  </section>
                )}

              <section className="surface-card diagnostics-card">
                <div className="card-heading">
                  <div>
                    <p className="eyebrow">HEALTH & DIAGNOSTICS</p>
                    <h2>Device status</h2>
                  </div>
                  <span className="freshness">Updated automatically</span>
                </div>
                <div className="diagnostic-list">
                  <div>
                    <span>Desktop process</span>
                    <strong>
                      <i className="mini-check" />
                      Running
                    </strong>
                  </div>
                  <div>
                    <span>Provisioned role</span>
                    <strong>{roleDetails[snapshot.role].label}</strong>
                  </div>
                  <div>
                    <span>Native supervisor</span>
                    <strong>{statusLabel(snapshot)}</strong>
                  </div>
                  <div>
                    <span>PostgreSQL / Python</span>
                    <strong className="muted-value">
                      {snapshot.role === "CONTROLLER" ? "Private M1 bundle" : "Not included"}
                    </strong>
                  </div>
                </div>
              </section>

              <section className="reset-row" aria-label="Device role management">
                <div>
                  <strong>Change this device’s role</strong>
                  <span>
                    Decommissioning stops the local runtime and clears the role selection. It does
                    not delete Controller data.
                  </span>
                </div>
                <button
                  type="button"
                  className="button button-danger-quiet"
                  onClick={() => setResetRequested(true)}
                >
                  Decommission device
                </button>
              </section>
            </>
          )}
        </div>
      </section>

      {quitRequested && (
        <div className="dialog-backdrop" role="presentation">
          <section
            className="confirm-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="quit-title"
          >
            <div className="dialog-symbol" aria-hidden="true">
              ↗
            </div>
            <h2 id="quit-title">Quit Threads Desktop?</h2>
            <p>
              {snapshot.role === "CONTROLLER"
                ? "The Controller stops its scheduler, HTTP process, and PostgreSQL database before the desktop exits."
                : snapshot.role === "WORKER"
                  ? "The Worker helper stops before the desktop exits."
                  : "Threads Desktop exits."}{" "}
              Closing this window only hides it to the tray.
            </p>
            <div className="dialog-actions">
              <button
                type="button"
                className="button button-secondary"
                onClick={() => setQuitRequested(false)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="button button-primary"
                onClick={() => void handleQuit()}
              >
                Stop node and quit
              </button>
            </div>
          </section>
        </div>
      )}

      {restartRequested && (
        <div className="dialog-backdrop" role="presentation">
          <section
            className="confirm-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="restart-title"
          >
            <div className="dialog-symbol" aria-hidden="true">
              ↻
            </div>
            <h2 id="restart-title">
              Restart {snapshot.role === "CONTROLLER" ? "Controller" : "Worker"}?
            </h2>
            <p>
              {snapshot.role === "CONTROLLER"
                ? "The Controller scheduler, HTTP process, and PostgreSQL database will stop and start again."
                : "The Worker helper will stop and start again."}{" "}
              An authorized Operator login is required.
            </p>
            <div className="dialog-actions">
              <button
                type="button"
                className="button button-secondary"
                onClick={() => setRestartRequested(false)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="button button-primary"
                onClick={() => void handleRestart()}
              >
                Authenticate and restart
              </button>
            </div>
          </section>
        </div>
      )}

      {resetRequested && (
        <div className="dialog-backdrop" role="presentation">
          <section
            className="confirm-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="reset-title"
          >
            <div className="dialog-symbol dialog-symbol-danger" aria-hidden="true">
              !
            </div>
            <h2 id="reset-title">Decommission this device?</h2>
            <p>
              This scaffold will stop its mock node and clear only the local role selection. It does
              not delete Controller data.
            </p>
            <label className="confirm-field">
              Type <strong className="confirm-phrase">RESET THIS DEVICE</strong> to continue
              <input
                value={resetPhrase}
                onChange={(event) => setResetPhrase(event.currentTarget.value)}
              />
            </label>
            <div className="dialog-actions">
              <button
                type="button"
                className="button button-secondary"
                onClick={() => {
                  setResetRequested(false);
                  setResetPhrase("");
                }}
              >
                Cancel
              </button>
              <button
                type="button"
                className="button button-danger"
                disabled={resetPhrase !== "RESET THIS DEVICE"}
                onClick={() => void handleDecommission()}
              >
                Decommission
              </button>
            </div>
          </section>
        </div>
      )}
    </main>
  );
}

function statusClass(snapshot: DesktopSnapshot): string {
  if (snapshot.role === "CONSOLE") return "neutral";
  if (snapshot.supervisor.state === "running") return "healthy";
  if (snapshot.supervisor.state === "failed" || snapshot.supervisor.state === "degraded")
    return "danger";
  return "neutral";
}

function statusLabel(snapshot: DesktopSnapshot): string {
  if (!snapshot.role) return "Setup required";
  if (snapshot.role === "CONSOLE") return "No local runtime";
  if (snapshot.supervisor.state === "running") return "Running";
  if (snapshot.supervisor.state === "degraded") return "Needs attention";
  if (snapshot.supervisor.state === "failed") return "Failed";
  if (
    snapshot.supervisor.state === "starting" ||
    snapshot.supervisor.state === "preflight" ||
    snapshot.supervisor.state === "starting_database" ||
    snapshot.supervisor.state === "migrating" ||
    snapshot.supervisor.state === "m1_bootstrap_boundary" ||
    snapshot.supervisor.state === "starting_http" ||
    snapshot.supervisor.state === "starting_scheduler"
  ) {
    return "Starting";
  }
  if (snapshot.supervisor.state === "stopping") return "Stopping";
  return "Stopped";
}

function runtimeTitle(snapshot: DesktopSnapshot): string {
  if (snapshot.role === "CONSOLE") return "Console only";
  if (snapshot.role === "CONTROLLER") {
    if (snapshot.supervisor.state === "running") return "Controller runtime is running";
    if (snapshot.supervisor.state === "failed") return "Controller runtime failed";
    return "Controller runtime is stopped";
  }
  return snapshot.supervisor.state === "running"
    ? "Worker mock is running"
    : "Worker mock is stopped";
}

function roleSymbol(role: ProvisionedRole): string {
  if (role === "CONTROLLER") return "⌘";
  if (role === "WORKER") return "◈";
  return "▣";
}

export default App;
