import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  decommissionDevice,
  getDesktopSnapshot,
  listenForTrayQuit,
  provisionRole,
  resetUiPreferences,
  requestQuit,
  type DesktopSnapshot,
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

function App() {
  const queryClient = useQueryClient();
  const [quitRequested, setQuitRequested] = useState(false);
  const [resetRequested, setResetRequested] = useState(false);
  const [resetPhrase, setResetPhrase] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);
  const snapshotQuery = useQuery({
    queryKey: ["desktop-snapshot"],
    queryFn: getDesktopSnapshot,
    refetchInterval: 1_000,
    retry: false,
  });

  useEffect(() => {
    let mounted = true;
    let unlisten: (() => void) | undefined;
    void listenForTrayQuit().then((stopListening) => {
      if (mounted) unlisten = stopListening;
      else stopListening();
    });
    const requestQuitFromTray = () => setQuitRequested(true);
    window.addEventListener("threads-desktop:quit-requested", requestQuitFromTray);
    return () => {
      mounted = false;
      unlisten?.();
      window.removeEventListener("threads-desktop:quit-requested", requestQuitFromTray);
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
    } catch {
      setActionError("The runtime did not confirm shutdown. Threads Desktop is still open.");
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
              M1 PRIVATE RUNTIME
            </span>
            <button type="button" className="quiet-link" onClick={() => setQuitRequested(true)}>
              Quit…
            </button>
            <button
              type="button"
              className="avatar-button"
              aria-label="Operator session is not configured"
              title="Operator sign-in is added in DX-05"
            >
              —
            </button>
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
                    unsupported. M1 has no Owner account or LAN endpoint, portable backup, or
                    production durability.
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
                    <p className="diagnostic-code" role="status">
                      Diagnostic code: {snapshot.supervisor.diagnosticCode}
                    </p>
                  )}
                </article>

                <article className="surface-card authentication-card">
                  <div className="card-heading">
                    <div>
                      <p className="eyebrow">OPERATOR ACCESS</p>
                      <h2>Sign-in is not configured</h2>
                    </div>
                    <span className="lock-symbol" aria-hidden="true">
                      ⌑
                    </span>
                  </div>
                  <SessionGate validSession={false} onUnlock={async () => false}>
                    <p>Protected account data is hidden until a valid Operator session exists.</p>
                  </SessionGate>
                  <p className="card-copy">
                    DX-02 stores no bearer or Operator credentials. Login and RBAC are delivered in
                    DX-05.
                  </p>
                </article>
              </section>

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
