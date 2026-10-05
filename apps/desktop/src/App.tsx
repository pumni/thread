import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  decommissionDevice,
  controllerHttpsConfigure,
  controllerHttpsReconfigure,
  controllerHttpsSummary,
  controllerTrustConfirm,
  controllerTrustProbe,
  controllerTrustSummary,
  getDesktopSnapshot,
  listenForTrayQuit,
  localWorkerDrainStatus,
  operatorBootstrapOwner,
  operatorChangePassword,
  operatorCreateUser,
  operatorCurrent,
  operatorListUsers,
  operatorLock,
  operatorLogin,
  operatorLogout,
  operatorUpdateUser,
  provisionRole,
  forceStopWorker,
  rollbackLocalWorkerToLegacy,
  resetUiPreferences,
  requestQuit,
  requestRestart,
  takeoverLocalWorker,
  type CreatedOperatorUser,
  type ControllerHttpsSummary,
  type DesktopSnapshot,
  type OperatorIdentity,
  type OperatorRole,
  type OperatorUser,
  type ProvisionedRole,
  type TrustProbeSummary,
  type TrustedControllerSummary,
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
const workerLifecycleRoles: OperatorRole[] = ["OWNER", "ADMIN", "OPERATOR"];
const stableWorkerErrorCodes = new Set([
  "worker_host_configuration_required",
  "worker_legacy_task_not_registered",
  "worker_legacy_task_invalid",
  "worker_legacy_task_running",
  "worker_legacy_task_disable_failed",
  "worker_legacy_task_enable_failed",
  "worker_legacy_task_start_failed",
  "worker_package_invalid",
  "worker_host_config_invalid",
  "worker_identity_missing",
  "worker_identity_not_enrolled",
  "worker_identity_corrupt",
  "worker_device_key_unprotect_failed",
  "worker_process_lock_held",
  "worker_process_lock_not_acquired",
  "worker_process_lock_unavailable",
  "worker_process_job_create_failed",
  "worker_process_job_assign_failed",
  "worker_process_exited",
  "worker_process_exit_timeout",
  "worker_process_start_failed",
  "worker_drain_unavailable",
  "worker_drain_request_failed",
  "worker_drain_response_invalid",
  "worker_drain_timeout",
  "worker_drain_interrupted",
  "worker_startup_cleanup_failed",
  "worker_startup_timeout",
  "worker_cutover_rollback_required",
  "worker_rollback_failed",
  "worker_force_confirmation_required",
  "worker_forced_interruption",
  "operator_authentication_required",
  "operator_session_revoked",
  "operator_password_change_required",
  "operator_forbidden",
  "operator_api_unavailable",
]);
const operatorAccessMessages: Record<string, string> = {
  operator_authentication_required:
    "Sign in with an Operator account authorized for this node to continue.",
  operator_session_revoked:
    "This Operator session expired or was revoked. Sign in again before stopping this node.",
  operator_password_change_required: "Change your Workspace password before stopping this node.",
  operator_forbidden: "This Operator account is not authorized to perform this node action.",
  operator_api_unavailable:
    "The Controller could not verify Operator access. Try again when it is available.",
  operator_request_failed: "The Controller could not verify Operator access. Try again.",
  operator_response_invalid: "The Controller returned an invalid Operator response. Try again.",
  controller_https_configuration_required:
    "Configure a stable LAN IPv4 address and HTTPS port before starting the Controller.",
  controller_lan_address_invalid: "Enter a valid IPv4 address assigned to this PC.",
  controller_lan_address_unavailable: "That IPv4 address is not assigned to this PC.",
  controller_endpoint_port_in_use: "That HTTPS port is already in use. Choose another port.",
  controller_https_reconfiguration_unauthorized:
    "Only a signed-in Controller Owner or Admin can change this endpoint.",
  controller_endpoint_reconfigure_session_not_revoked:
    "The endpoint changed, but session revocation could not be confirmed. Sign in again to continue.",
  controller_tls_identity_invalid:
    "The saved Controller TLS identity is invalid. The Controller did not replace it.",
  controller_trust_required: "Verify and confirm this Controller before signing in.",
  controller_trust_probe_failed: "The HTTPS trust probe failed. Check the address and try again.",
  controller_trust_probe_expired: "The trust probe expired. Probe the Controller again.",
  controller_trust_confirmation_mismatch:
    "The Controller identity changed during confirmation. Probe it again and compare fingerprints.",
  controller_trust_store_invalid: "The saved Controller trust record is invalid.",
};

function nativeErrorCode(error: unknown): string | null {
  if (typeof error === "string") return error;
  if (error instanceof Error) return error.message;
  if (typeof error === "object" && error !== null && "message" in error) {
    const message = error.message;
    return typeof message === "string" ? message : null;
  }
  return null;
}

function stableWorkerErrorCode(error: unknown): string {
  const code = nativeErrorCode(error);
  return code && stableWorkerErrorCodes.has(code) ? code : "worker_action_failed";
}

function canonicalHttpsOrigin(value: string): string | null {
  try {
    const parsed = new URL(value.trim());
    if (
      parsed.protocol !== "https:" ||
      parsed.username ||
      parsed.password ||
      parsed.pathname !== "/" ||
      parsed.search ||
      parsed.hash ||
      !/^\d{1,3}(\.\d{1,3}){3}$/.test(parsed.hostname) ||
      parsed.hostname.split(".").some((part) => Number(part) > 255)
    ) {
      return null;
    }
    return parsed.origin;
  } catch {
    return null;
  }
}

function App() {
  const queryClient = useQueryClient();
  const [quitRequested, setQuitRequested] = useState(false);
  const [resetRequested, setResetRequested] = useState(false);
  const [resetPhrase, setResetPhrase] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);
  const [operator, setOperator] = useState<OperatorIdentity | null>(null);
  const [operatorLoaded, setOperatorLoaded] = useState(false);
  const [sessionLocked, setSessionLocked] = useState(false);
  const [workerTransition, setWorkerTransition] = useState<"takeover" | "rollback" | null>(null);
  const [workerActionError, setWorkerActionError] = useState<string | null>(null);
  const [quitBusy, setQuitBusy] = useState(false);
  const [restartBusy, setRestartBusy] = useState(false);
  const [forceStopRequested, setForceStopRequested] = useState(false);
  const [forceStopPhrase, setForceStopPhrase] = useState("");
  const [forceStopBusy, setForceStopBusy] = useState(false);
  const [apiUrl, setApiUrl] = useState("");
  const [controllerHttps, setControllerHttps] = useState<ControllerHttpsSummary | null>(null);
  const [lanAddress, setLanAddress] = useState("");
  const [httpsPort, setHttpsPort] = useState("8443");
  const [tlsConfiguring, setTlsConfiguring] = useState(false);
  const [tlsReconfigureOpen, setTlsReconfigureOpen] = useState(false);
  const [pendingTrustProbe, setPendingTrustProbe] = useState<TrustProbeSummary | null>(null);
  const [trustedController, setTrustedController] = useState<TrustedControllerSummary | null>(null);
  const [trustBusy, setTrustBusy] = useState(false);
  const [loginUsername, setLoginUsername] = useState("");
  const [loginPassword, setLoginPassword] = useState("");
  const [firstOwnerSetup, setFirstOwnerSetup] = useState(false);
  const [restartRequested, setRestartRequested] = useState(false);
  const [operatorUsers, setOperatorUsers] = useState<OperatorUser[]>([]);
  const [newOperatorUsername, setNewOperatorUsername] = useState("");
  const [newOperatorRole, setNewOperatorRole] = useState<OperatorRole>("VIEWER");
  const [createdOperatorUser, setCreatedOperatorUser] = useState<CreatedOperatorUser | null>(null);
  const [newPassword, setNewPassword] = useState("");
  const loginGeneration = useRef(0);
  const nativeLockNotification = useRef(0);
  const snapshotQuery = useQuery({
    queryKey: ["desktop-snapshot"],
    queryFn: getDesktopSnapshot,
    refetchInterval: 1_000,
    retry: false,
  });
  const workerSessionAuthorized =
    snapshotQuery.data?.role === "WORKER" &&
    operator !== null &&
    !operator.mustChangePassword &&
    !sessionLocked &&
    workerLifecycleRoles.includes(operator.role);
  const workerDrainStatusQuery = useQuery({
    queryKey: ["local-worker-drain-status"],
    queryFn: localWorkerDrainStatus,
    enabled: workerSessionAuthorized,
    refetchInterval: (query) => {
      const code = nativeErrorCode(query.state.error);
      if (
        code === "operator_forbidden" ||
        code === "operator_session_revoked" ||
        code === "operator_authentication_required"
      ) {
        return false;
      }
      return workerSessionAuthorized ? 5_000 : false;
    },
    retry: false,
  });

  useEffect(() => {
    if (!workerSessionAuthorized) {
      queryClient.removeQueries({ queryKey: ["local-worker-drain-status"] });
    }
  }, [queryClient, workerSessionAuthorized]);

  useEffect(() => {
    if (!workerSessionAuthorized || !workerDrainStatusQuery.isError) return;
    const code = nativeErrorCode(workerDrainStatusQuery.error);
    if (code !== "operator_session_revoked" && code !== "operator_authentication_required") {
      return;
    }
    loginGeneration.current += 1;
    nativeLockNotification.current += 1;
    setOperator(null);
    setOperatorLoaded(true);
    setOperatorUsers([]);
    setCreatedOperatorUser(null);
    setNewOperatorUsername("");
    setLoginUsername("");
    setFirstOwnerSetup(false);
    setNewPassword("");
    setLoginPassword("");
    setActionError(null);
    setSessionLocked(true);
  }, [workerDrainStatusQuery.error, workerDrainStatusQuery.isError, workerSessionAuthorized]);

  const handleNativeSessionLock = useCallback(async () => {
    const notification = ++nativeLockNotification.current;
    const generation = ++loginGeneration.current;
    setSessionLocked(true);
    let current: OperatorIdentity | null = null;
    try {
      current = await operatorCurrent();
    } catch {
      // A revoked or unavailable session must stay hidden behind the sign-in form.
    }
    if (notification !== nativeLockNotification.current || generation !== loginGeneration.current) {
      return;
    }
    if (current) {
      setOperator(current);
      setOperatorLoaded(true);
      setSessionLocked(false);
      return;
    }
    setOperator(null);
    setOperatorLoaded(true);
    setOperatorUsers([]);
    setCreatedOperatorUser(null);
    setNewOperatorUsername("");
    setLoginUsername("");
    setFirstOwnerSetup(false);
    setNewPassword("");
    setLoginPassword("");
    setActionError(null);
    setSessionLocked(true);
  }, []);

  useEffect(() => {
    let mounted = true;
    const generation = loginGeneration.current;
    void operatorCurrent()
      .then((current) => {
        if (mounted && generation === loginGeneration.current) setOperator(current ?? null);
      })
      .catch(() => {
        if (mounted && generation === loginGeneration.current) setOperator(null);
      })
      .finally(() => {
        if (mounted && generation === loginGeneration.current) setOperatorLoaded(true);
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
    if (snapshotQuery.data?.role !== "CONTROLLER") {
      setControllerHttps(null);
      return;
    }
    let mounted = true;
    void controllerHttpsSummary()
      .then((summary) => {
        if (!mounted) return;
        setControllerHttps(summary);
        if (summary.lanAddress) setLanAddress(summary.lanAddress);
        setHttpsPort(String(summary.httpsPort));
      })
      .catch(() => {
        if (mounted) setControllerHttps(null);
      });
    return () => {
      mounted = false;
    };
  }, [snapshotQuery.data?.role]);

  useEffect(() => {
    if (snapshotQuery.data?.role === "CONTROLLER" || !canonicalHttpsOrigin(apiUrl)) {
      setTrustedController(null);
      return;
    }
    let mounted = true;
    setTrustedController(null);
    void controllerTrustSummary(apiUrl)
      .then((summary) => {
        if (mounted) setTrustedController(summary);
      })
      .catch(() => {
        if (mounted) setTrustedController(null);
      });
    return () => {
      mounted = false;
    };
  }, [apiUrl, snapshotQuery.data?.role]);

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
    const lockSessionFromTray = () => void handleNativeSessionLock();
    window.addEventListener("threads-desktop:quit-requested", requestQuitFromTray);
    window.addEventListener("threads-desktop:session-locked", lockSessionFromTray);
    return () => {
      mounted = false;
      unlisten?.();
      window.removeEventListener("threads-desktop:quit-requested", requestQuitFromTray);
      window.removeEventListener("threads-desktop:session-locked", lockSessionFromTray);
    };
  }, [handleNativeSessionLock]);

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
    if (snapshot?.role === "WORKER") {
      setActionError(null);
      try {
        const updated = await decommissionDevice(resetPhrase);
        queryClient.setQueryData(["desktop-snapshot"], updated);
        await handleOperatorLogout();
        setResetRequested(false);
        setResetPhrase("");
      } catch (error) {
        const code = stableWorkerErrorCode(error);
        setActionError(`Worker decommission did not complete. Diagnostic code: ${code}.`);
        await queryClient.invalidateQueries({ queryKey: ["desktop-snapshot"] });
        if (code === "operator_session_revoked" || code === "operator_authentication_required") {
          await handleOperatorLogout();
        }
      }
      return;
    }
    const completed = await updateSnapshot(() => decommissionDevice(resetPhrase));
    if (!completed) return;
    setResetRequested(false);
    setResetPhrase("");
  }

  async function handleWorkerOwnershipAction(action: "takeover" | "rollback") {
    setWorkerTransition(action);
    setWorkerActionError(null);
    try {
      if (action === "takeover") await takeoverLocalWorker();
      else await rollbackLocalWorkerToLegacy();
    } catch (error) {
      setWorkerActionError(stableWorkerErrorCode(error));
    } finally {
      setWorkerTransition(null);
      await queryClient.invalidateQueries({ queryKey: ["desktop-snapshot"] });
    }
  }

  async function handleForceStopWorker() {
    if (forceStopPhrase !== "FORCE STOP WORKER") return;
    setForceStopBusy(true);
    setWorkerActionError(null);
    try {
      const updated = await forceStopWorker(forceStopPhrase);
      queryClient.setQueryData(["desktop-snapshot"], updated);
      setForceStopRequested(false);
      setForceStopPhrase("");
      await queryClient.invalidateQueries({ queryKey: ["desktop-snapshot"] });
    } catch (error) {
      setWorkerActionError(stableWorkerErrorCode(error));
      await queryClient.invalidateQueries({ queryKey: ["desktop-snapshot"] });
    } finally {
      setForceStopBusy(false);
    }
  }

  async function handleQuit() {
    setQuitBusy(true);
    setActionError(null);
    try {
      await requestQuit();
      setQuitRequested(false);
    } catch (error) {
      const errorCode =
        typeof error === "string" ? error : error instanceof Error ? error.message : "";
      if (
        snapshot?.role !== "WORKER" ||
        errorCode === "operator_session_revoked" ||
        errorCode === "operator_authentication_required"
      ) {
        await handleOperatorLogout();
        setQuitRequested(false);
      }
      setActionError(
        snapshot?.role === "WORKER"
          ? `Worker graceful quit did not complete. Diagnostic code: ${stableWorkerErrorCode(error)}.`
          : errorCode === "operator_forbidden" && snapshot?.role === "CONTROLLER"
            ? "Only an Owner or Admin can stop this Controller."
            : (operatorAccessMessages[errorCode] ??
              "The node could not stop cleanly. Check the runtime status before retrying."),
      );
    } finally {
      setQuitBusy(false);
    }
  }

  async function handleOperatorLogin() {
    if (snapshot?.role === "CONTROLLER" && !controllerHttps?.configured && !firstOwnerSetup) {
      setActionError("Configure Controller HTTPS before signing in.");
      return;
    }
    if (
      snapshot?.role !== "CONTROLLER" &&
      (!trustedController?.trusted || trustedController.endpoint !== canonicalHttpsOrigin(apiUrl))
    ) {
      setActionError("Verify and confirm this Controller before signing in.");
      return;
    }
    setActionError(null);
    try {
      const signedIn = firstOwnerSetup
        ? await operatorBootstrapOwner(loginUsername, loginPassword)
        : await operatorLogin(
            snapshot?.role === "CONTROLLER" ? (controllerHttps?.localHttpsOrigin ?? "") : apiUrl,
            loginUsername,
            loginPassword,
          );
      loginGeneration.current += 1;
      nativeLockNotification.current += 1;
      setOperator(signedIn);
      setOperatorLoaded(true);
      setSessionLocked(false);
      setLoginPassword("");
      setFirstOwnerSetup(false);
    } catch (error) {
      const code = typeof error === "string" ? error : error instanceof Error ? error.message : "";
      setActionError(
        operatorAccessMessages[code] ??
          (firstOwnerSetup
            ? "First Owner setup failed. Check the local Controller and try again."
            : (operatorAccessMessages[code] ??
              "Sign-in failed. Check the Controller address, trust status, and credentials.")),
      );
    }
  }

  async function handleControllerHttpsConfigure() {
    const port = Number(httpsPort);
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      setActionError("Enter an HTTPS port from 1 to 65535.");
      return;
    }
    setTlsConfiguring(true);
    setActionError(null);
    try {
      const configured = tlsReconfigureOpen
        ? await controllerHttpsReconfigure(lanAddress.trim(), port)
        : await controllerHttpsConfigure(lanAddress.trim(), port);
      setControllerHttps(configured);
      setLanAddress(configured.lanAddress ?? lanAddress.trim());
      setHttpsPort(String(configured.httpsPort));
      if (tlsReconfigureOpen) {
        setOperator(null);
        setOperatorUsers([]);
      }
      setTlsReconfigureOpen(false);
      await queryClient.invalidateQueries({ queryKey: ["desktop-snapshot"] });
    } catch (error) {
      const code = typeof error === "string" ? error : error instanceof Error ? error.message : "";
      if (code === "controller_endpoint_reconfigure_session_not_revoked") {
        loginGeneration.current += 1;
        nativeLockNotification.current += 1;
        setOperator(null);
        setOperatorLoaded(true);
        setOperatorUsers([]);
        setCreatedOperatorUser(null);
        setLoginUsername("");
        setLoginPassword("");
        setSessionLocked(true);
        setTlsReconfigureOpen(false);
        try {
          const current = await controllerHttpsSummary();
          setControllerHttps(current);
          setLanAddress(current.lanAddress ?? "");
          setHttpsPort(String(current.httpsPort));
          await queryClient.invalidateQueries({ queryKey: ["desktop-snapshot"] });
        } catch {
          // Keep the reauthentication requirement even if summary refresh fails.
        }
      }
      setActionError(
        operatorAccessMessages[code] ??
          "Controller HTTPS change failed. Check the IPv4 address and port, then try again.",
      );
    } finally {
      setTlsConfiguring(false);
    }
  }

  async function handleTrustProbe() {
    const origin = canonicalHttpsOrigin(apiUrl);
    if (!origin) {
      setActionError("Enter an exact HTTPS origin using the Controller IPv4 address and port.");
      return;
    }
    setTrustBusy(true);
    setPendingTrustProbe(null);
    setTrustedController(null);
    setLoginUsername("");
    setLoginPassword("");
    setActionError(null);
    try {
      const pending = await controllerTrustProbe(apiUrl.trim());
      setPendingTrustProbe(pending);
    } catch (error) {
      const code = typeof error === "string" ? error : error instanceof Error ? error.message : "";
      setActionError(
        operatorAccessMessages[code] ?? "Controller trust probe failed. Check the HTTPS address.",
      );
    } finally {
      setTrustBusy(false);
    }
  }

  async function handleTrustConfirm() {
    if (!pendingTrustProbe) return;
    setTrustBusy(true);
    setActionError(null);
    try {
      const trusted = await controllerTrustConfirm(pendingTrustProbe.probeId);
      setTrustedController(trusted);
      setPendingTrustProbe(null);
    } catch (error) {
      const code = typeof error === "string" ? error : error instanceof Error ? error.message : "";
      setPendingTrustProbe(null);
      setTrustedController(null);
      setActionError(
        operatorAccessMessages[code] ??
          "Trust confirmation failed. Probe the Controller again and compare fingerprints.",
      );
    } finally {
      setTrustBusy(false);
    }
  }

  async function handleOperatorLogout() {
    setSessionLocked(true);
    loginGeneration.current += 1;
    nativeLockNotification.current += 1;
    await operatorLogout();
    clearProtectedOperatorState();
  }

  async function handleSessionLock() {
    setSessionLocked(true);
    loginGeneration.current += 1;
    nativeLockNotification.current += 1;
    try {
      await operatorLock();
    } catch {
      setActionError("Session lock could not be confirmed. Sign in again to continue.");
    }
    clearProtectedOperatorState();
    setActionError("Session locked. Sign in again to access protected data.");
  }

  function clearProtectedOperatorState() {
    setOperator(null);
    setOperatorLoaded(true);
    setOperatorUsers([]);
    setCreatedOperatorUser(null);
    setNewOperatorUsername("");
    setLoginUsername("");
    setFirstOwnerSetup(false);
    setNewPassword("");
    setLoginPassword("");
    setActionError(null);
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
    setRestartBusy(true);
    setActionError(null);
    try {
      await requestRestart();
      setRestartRequested(false);
      await handleOperatorLogout();
      await queryClient.invalidateQueries({ queryKey: ["desktop-snapshot"] });
    } catch (error) {
      const errorCode =
        typeof error === "string" ? error : error instanceof Error ? error.message : "";
      if (
        snapshot?.role !== "WORKER" ||
        errorCode === "operator_session_revoked" ||
        errorCode === "operator_authentication_required"
      ) {
        await handleOperatorLogout();
        setRestartRequested(false);
      }
      setActionError(
        snapshot?.role === "WORKER"
          ? `Worker restart did not complete. Diagnostic code: ${stableWorkerErrorCode(error)}.`
          : "An active Operator session with permission to restart this node is required.",
      );
    } finally {
      setRestartBusy(false);
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
  const workerStatusErrorCode = nativeErrorCode(workerDrainStatusQuery.error);
  const workerStatusForbidden = workerStatusErrorCode === "operator_forbidden";
  const canOperateWorker = workerSessionAuthorized && !workerStatusForbidden;
  const workerOwnership = snapshot.supervisor.workerOwnership;
  const legacyTaskState = snapshot.supervisor.legacyTaskState;
  const canTakeOverWorker =
    canOperateWorker &&
    workerOwnership === "TAKEOVER_REQUIRED" &&
    (legacyTaskState === "RUNNING" || legacyTaskState === "READY");
  const canRollbackWorker =
    canOperateWorker && workerOwnership === "DESKTOP" && legacyTaskState === "DISABLED";
  const workerHasIntervention =
    snapshot.supervisor.state === "degraded" ||
    snapshot.supervisor.state === "failed" ||
    snapshot.supervisor.diagnosticCode !== null;
  const canForceStopWorker =
    canOperateWorker &&
    workerOwnership === "DESKTOP" &&
    legacyTaskState === "DISABLED" &&
    workerHasIntervention;
  const workerStatusDisplay = !workerSessionAuthorized
    ? operator?.role === "VIEWER"
      ? "UNAUTHORIZED"
      : operator?.mustChangePassword
        ? "PASSWORD CHANGE REQUIRED"
        : sessionLocked
          ? "SIGN IN REQUIRED"
          : "SIGN IN REQUIRED"
    : workerDrainStatusQuery.isError
      ? workerStatusForbidden
        ? "UNAUTHORIZED"
        : "UNAVAILABLE"
      : (workerDrainStatusQuery.data?.status ?? "CHECKING");
  const workerStatusValuesAvailable =
    workerSessionAuthorized &&
    !workerDrainStatusQuery.isError &&
    workerDrainStatusQuery.data !== undefined;
  const canonicalEndpoint = canonicalHttpsOrigin(apiUrl);
  const remoteTrustReady =
    trustedController?.trusted === true && trustedController.endpoint === canonicalEndpoint;
  const ownerBootstrapRequired = snapshot.supervisor.state === "owner_bootstrap_required";
  const canReconfigureControllerEndpoint =
    ownerBootstrapRequired ||
    (operator !== null &&
      !operator.mustChangePassword &&
      (operator.role === "OWNER" || operator.role === "ADMIN"));
  const controllerLoginReady =
    snapshot.role !== "CONTROLLER" ||
    (controllerHttps?.configured === true &&
      (ownerBootstrapRequired ? firstOwnerSetup : !firstOwnerSetup));
  const loginReady = snapshot.role === "CONTROLLER" ? controllerLoginReady : remoteTrustReady;
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
            <span>{snapshot.role === "WORKER" ? "Worker Desktop" : "Desktop scaffold"}</span>
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
            {((snapshot.role === "CONTROLLER" && !operator?.mustChangePassword) ||
              (snapshot.role === "WORKER" &&
                canOperateWorker &&
                workerOwnership === "DESKTOP")) && (
              <button
                type="button"
                className="quiet-link"
                onClick={() => setRestartRequested(true)}
              >
                Restart…
              </button>
            )}
            {(snapshot.role !== "WORKER" || canOperateWorker) && (
              <button type="button" className="quiet-link" onClick={() => setQuitRequested(true)}>
                Quit…
              </button>
            )}
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
                <>
                  <section className="m1-limitations" aria-label="M1 prototype limits">
                    <strong>Internal M1 prototype — disposable test data only</strong>
                    <span>
                      The runtime is available while this Windows user is signed in. Windows logout
                      is unsupported. M1 has no portable backup or production durability.
                    </span>
                  </section>
                  <section className="surface-card" aria-label="Controller HTTPS identity">
                    <div className="card-heading">
                      <div>
                        <p className="eyebrow">CONTROLLER TRANSPORT</p>
                        <h2>Private HTTPS identity</h2>
                      </div>
                    </div>
                    {controllerHttps?.configured ? (
                      <>
                        <div className="runtime-facts">
                          <div>
                            <span>Public Controller address</span>
                            <strong>{controllerHttps.publicHttpsOrigin}</strong>
                          </div>
                          <div>
                            <span>Local HTTPS</span>
                            <strong>{controllerHttps.localHttpsOrigin} · Verified</strong>
                          </div>
                          <div>
                            <span>Controller root fingerprint</span>
                            <code>{controllerHttps.rootFingerprint}</code>
                          </div>
                          {controllerHttps.leafExpiresAt && (
                            <div>
                              <span>Leaf certificate expires</span>
                              <strong>
                                {new Date(controllerHttps.leafExpiresAt).toLocaleString()}
                              </strong>
                            </div>
                          )}
                        </div>
                        {!tlsReconfigureOpen && canReconfigureControllerEndpoint && (
                          <button
                            type="button"
                            className="button button-secondary"
                            onClick={() => {
                              setLanAddress(controllerHttps.lanAddress ?? "");
                              setHttpsPort(String(controllerHttps.httpsPort));
                              setTlsReconfigureOpen(true);
                              setActionError(null);
                            }}
                          >
                            Reconfigure HTTPS endpoint…
                          </button>
                        )}
                      </>
                    ) : null}
                    {(!controllerHttps?.configured || tlsReconfigureOpen) && (
                      <form
                        className="operator-login-form"
                        onSubmit={(event) => {
                          event.preventDefault();
                          void handleControllerHttpsConfigure();
                        }}
                      >
                        <p className="card-copy">
                          {controllerHttps?.configured
                            ? "Choose the new stable IPv4 address and HTTPS port. The Controller root identity stays the same and a new leaf certificate is issued."
                            : "Set the stable IPv4 address assigned to this PC and explicitly choose its HTTPS port."}
                        </p>
                        <label>
                          Stable LAN IPv4 address
                          <input
                            id="controller-lan-address"
                            type="text"
                            aria-label="Stable LAN IPv4 address"
                            inputMode="decimal"
                            autoComplete="off"
                            value={lanAddress}
                            onChange={(event) => setLanAddress(event.target.value)}
                            required
                          />
                        </label>
                        <label>
                          HTTPS port
                          <input
                            id="controller-https-port"
                            type="number"
                            aria-label="HTTPS port"
                            min={1}
                            max={65535}
                            value={httpsPort}
                            onChange={(event) => setHttpsPort(event.target.value)}
                            required
                          />
                        </label>
                        <button
                          type="submit"
                          className="button button-primary"
                          disabled={tlsConfiguring}
                        >
                          {tlsConfiguring
                            ? "Applying…"
                            : tlsReconfigureOpen
                              ? "Apply endpoint change"
                              : "Configure HTTPS"}
                        </button>
                        {tlsReconfigureOpen && (
                          <button
                            type="button"
                            className="button button-secondary"
                            disabled={tlsConfiguring}
                            onClick={() => {
                              setTlsReconfigureOpen(false);
                              setLanAddress(controllerHttps?.lanAddress ?? "");
                              setHttpsPort(String(controllerHttps?.httpsPort ?? 8443));
                            }}
                          >
                            Cancel
                          </button>
                        )}
                      </form>
                    )}
                  </section>
                </>
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
                      ? "Console mode is client-only and starts no local Worker or Controller runtime."
                      : snapshot.role === "CONTROLLER"
                        ? "The private PostgreSQL cluster, direct Uvicorn HTTPS/WSS listener, and scheduler run as separately supervised Windows processes."
                        : "Desktop supervises the existing enrolled Worker package under this Windows user. Server health is shown separately from the local process."}
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
                    ) : snapshot.role === "WORKER" ? (
                      <>
                        <div>
                          <span>Desktop Worker PID</span>
                          <strong>
                            {snapshot.supervisor.processId
                              ? `PID ${snapshot.supervisor.processId}`
                              : workerOwnership === "LEGACY" ||
                                  workerOwnership === "TAKEOVER_REQUIRED"
                                ? "Not owned by Desktop"
                                : workerOwnership === "BLOCKED"
                                  ? "Ownership unresolved"
                                  : "Stopped"}
                          </strong>
                        </div>
                        <div>
                          <span>Ownership</span>
                          <strong>{workerOwnership ?? "UNKNOWN"}</strong>
                        </div>
                        <div>
                          <span>Legacy task</span>
                          <strong>{legacyTaskState ?? "UNKNOWN"}</strong>
                        </div>
                        <div>
                          <span>Supervisor lifecycle</span>
                          <strong>{snapshot.supervisor.state}</strong>
                        </div>
                        <div>
                          <span>Worker UUID</span>
                          <strong>{snapshot.supervisor.workerId ?? "Not available"}</strong>
                        </div>
                      </>
                    ) : (
                      <div>
                        <span>Local runtime</span>
                        <strong>None</strong>
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
                        onUnlock={async () => {
                          try {
                            return Boolean(await operatorCurrent());
                          } catch {
                            return false;
                          }
                        }}
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
                      {operator.mustChangePassword && !sessionLocked && (
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
                            placeholder="https://192.168.1.20:8443"
                            value={apiUrl}
                            onChange={(event) => {
                              setApiUrl(event.target.value);
                              setPendingTrustProbe(null);
                              setTrustedController(null);
                              setLoginUsername("");
                              setLoginPassword("");
                            }}
                            required
                          />
                        </label>
                      )}
                      {snapshot.role !== "CONTROLLER" && (
                        <section
                          className="trust-panel"
                          aria-label="Controller first-contact trust"
                        >
                          <button
                            type="button"
                            className="button button-secondary"
                            onClick={() => void handleTrustProbe()}
                            disabled={trustBusy || !canonicalEndpoint}
                          >
                            {trustBusy ? "Checking identity…" : "Probe Controller identity"}
                          </button>
                          {pendingTrustProbe &&
                            pendingTrustProbe.endpoint === canonicalEndpoint && (
                              <div className="runtime-facts">
                                <p className="card-copy">
                                  Compare this fingerprint with the value shown on the Controller
                                  over a trusted local or out-of-band channel.
                                </p>
                                <div>
                                  <span>Candidate Controller root fingerprint</span>
                                  <code>{pendingTrustProbe.rootFingerprint}</code>
                                </div>
                                <div>
                                  <span>Probe expires</span>
                                  <strong>
                                    {new Date(
                                      pendingTrustProbe.expiresAt * 1_000,
                                    ).toLocaleTimeString()}
                                  </strong>
                                </div>
                                <button
                                  type="button"
                                  className="button button-primary"
                                  onClick={() => void handleTrustConfirm()}
                                  disabled={trustBusy}
                                >
                                  Confirm matching fingerprint
                                </button>
                              </div>
                            )}
                          {remoteTrustReady && (
                            <p className="card-copy" role="status">
                              Trusted Controller root: {trustedController.rootFingerprint}
                            </p>
                          )}
                        </section>
                      )}
                      {snapshot.role === "CONTROLLER" && !controllerHttps?.configured && (
                        <p className="card-copy" role="status">
                          Configure the Controller HTTPS identity before Owner setup or sign-in.
                        </p>
                      )}
                      {snapshot.role === "CONTROLLER" &&
                        ownerBootstrapRequired &&
                        !firstOwnerSetup && (
                          <p className="card-copy" role="status">
                            HTTPS is configured. Create the first Owner using the local native setup
                            operation to start the HTTPS listener.
                          </p>
                        )}
                      <label>
                        Username
                        <input
                          type="text"
                          autoComplete="username"
                          value={loginUsername}
                          onChange={(event) => setLoginUsername(event.target.value)}
                          disabled={!loginReady}
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
                          disabled={!loginReady}
                          required
                        />
                      </label>
                      {snapshot.role === "CONTROLLER" && firstOwnerSetup && (
                        <p className="card-copy">
                          This creates the first Owner through the local native Controller process.
                          The password is sent through stdin and never placed in command arguments.
                        </p>
                      )}
                      <button
                        type="submit"
                        className="button button-primary"
                        disabled={!loginReady}
                      >
                        {firstOwnerSetup ? "Create first Owner" : "Sign in"}
                      </button>
                      {snapshot.role === "CONTROLLER" && ownerBootstrapRequired && (
                        <button
                          type="button"
                          className="quiet-link"
                          disabled={!controllerHttps?.configured}
                          onClick={() => setFirstOwnerSetup((enabled) => !enabled)}
                        >
                          {firstOwnerSetup ? "Return to sign in" : "Set up first Owner"}
                        </button>
                      )}
                    </form>
                  )}
                </article>
              </section>

              {snapshot.role === "WORKER" && (
                <section
                  className="surface-card worker-operations-card"
                  aria-label="Worker server status and ownership controls"
                >
                  <div className="card-heading">
                    <div>
                      <p className="eyebrow">WORKER AGENT</p>
                      <h2>Server status and host ownership</h2>
                    </div>
                    <span className="status-badge status-badge-neutral">Controller status</span>
                  </div>
                  <p className="card-copy">
                    Server status comes from the trusted Controller. The local PID only describes
                    the process on this PC and does not establish server health.
                  </p>
                  <div className="runtime-facts worker-status-facts">
                    <div>
                      <span>Worker server status</span>
                      <strong>{workerStatusDisplay}</strong>
                    </div>
                    <div>
                      <span>Active browser sessions</span>
                      <strong>
                        {workerStatusValuesAvailable
                          ? workerDrainStatusQuery.data.activeBrowserSessions
                          : "Unavailable"}
                      </strong>
                    </div>
                    <div>
                      <span>Running WorkerJobs</span>
                      <strong>
                        {workerStatusValuesAvailable
                          ? workerDrainStatusQuery.data.runningWorkerJobs
                          : "Unavailable"}
                      </strong>
                    </div>
                    <div>
                      <span>Quiescent</span>
                      <strong>
                        {workerStatusValuesAvailable
                          ? String(workerDrainStatusQuery.data.quiescent)
                          : "Unavailable"}
                      </strong>
                    </div>
                  </div>
                  {!workerSessionAuthorized ? (
                    <p className="card-copy" role="status">
                      {operator?.role === "VIEWER"
                        ? "Worker status and lifecycle controls are unavailable to VIEWER accounts."
                        : operator?.mustChangePassword
                          ? "Change your password before viewing protected Worker status or using lifecycle controls."
                          : sessionLocked
                            ? "The Operator session is locked. Sign in again to view protected Worker status."
                            : "Sign in as an OWNER, ADMIN, or OPERATOR to view protected Worker status."}
                    </p>
                  ) : workerDrainStatusQuery.isError ? (
                    <p className="card-copy" role="status">
                      {workerStatusForbidden
                        ? "Unauthorized: the Controller denied this Operator access to Worker status."
                        : "Worker status is unavailable because the Controller could not verify it."}
                    </p>
                  ) : workerDrainStatusQuery.isPending ? (
                    <p className="card-copy" role="status">
                      Checking Worker status with the Controller…
                    </p>
                  ) : null}

                  {workerOwnership === "LEGACY" && (
                    <p className="card-copy" role="status">
                      The legacy Task Scheduler Worker remains the owner. Desktop will not stop or
                      force that process.
                    </p>
                  )}
                  {workerOwnership === "TAKEOVER_REQUIRED" && !canTakeOverWorker && (
                    <p className="card-copy" role="status">
                      Takeover is unavailable for this task state. Setup or intervention is
                      required; Desktop will not repair the task automatically.
                    </p>
                  )}
                  {workerOwnership === "BLOCKED" && (
                    <p className="card-copy" role="status">
                      Worker ownership is blocked. Review the diagnostic above; no automatic repair
                      action is available.
                    </p>
                  )}
                  {(legacyTaskState === "INVALID" || legacyTaskState === "NOT_REGISTERED") && (
                    <p className="card-copy" role="status">
                      The legacy task is {legacyTaskState}. Desktop will not discover or replace a
                      Worker host automatically.
                    </p>
                  )}

                  {workerActionError && (
                    <p className="diagnostic-code" role="alert">
                      Worker action failed. Diagnostic code: {workerActionError}
                    </p>
                  )}
                  <div className="worker-actions">
                    {canTakeOverWorker && (
                      <>
                        <p className="card-copy">
                          Takeover drains the existing Worker first, then starts the same enrolled
                          identity and durable browser/profile data under Desktop ownership. The
                          legacy task is disabled only after the old process exits safely.
                        </p>
                        <button
                          type="button"
                          className="button button-primary"
                          disabled={workerTransition !== null}
                          onClick={() => void handleWorkerOwnershipAction("takeover")}
                        >
                          {workerTransition === "takeover"
                            ? "Taking over Worker…"
                            : "Take over existing Worker"}
                        </button>
                      </>
                    )}
                    {canRollbackWorker && (
                      <>
                        <p className="card-copy">
                          Restore the preserved legacy Task Scheduler launch path after a graceful
                          drain. This keeps the existing Worker identity and data.
                        </p>
                        <button
                          type="button"
                          className="button button-secondary"
                          disabled={workerTransition !== null}
                          onClick={() => void handleWorkerOwnershipAction("rollback")}
                        >
                          {workerTransition === "rollback"
                            ? "Restoring legacy Worker…"
                            : "Restore legacy Worker host"}
                        </button>
                      </>
                    )}
                    {workerTransition && (
                      <p className="card-copy" role="status">
                        {workerTransition === "takeover"
                          ? "Waiting for authoritative OFFLINE, clean legacy exit, and ONLINE from the same Worker."
                          : "Waiting for Desktop drain and clean exit before restoring legacy ownership."}
                      </p>
                    )}
                    {canForceStopWorker && (
                      <div className="worker-force-area">
                        <p className="card-copy">
                          Forced interruption is available because Desktop owns this Worker and an
                          intervention is recorded. It is separate from graceful Quit and never
                          confirms server OFFLINE.
                        </p>
                        <button
                          type="button"
                          className="button button-danger-quiet"
                          onClick={() => {
                            setForceStopPhrase("");
                            setForceStopRequested(true);
                          }}
                        >
                          Force stop Worker (abnormal)…
                        </button>
                      </div>
                    )}
                  </div>
                </section>
              )}

              {operator &&
                !sessionLocked &&
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
                    {snapshot.role === "WORKER"
                      ? "Decommissioning safely drains a Desktop-owned Worker and restores the preserved legacy launch path when possible. A legacy-owned Worker remains in place; its identity, journal, and browser profiles are not deleted. The operation fails closed if safe restoration cannot be proven."
                      : "Decommissioning stops the local runtime and clears the role selection. It does not delete Controller data."}
                  </span>
                </div>
                {(snapshot.role !== "WORKER" || canOperateWorker) && (
                  <button
                    type="button"
                    className="button button-danger-quiet"
                    onClick={() => setResetRequested(true)}
                  >
                    Decommission device
                  </button>
                )}
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
                  ? workerOwnership === "LEGACY" || workerOwnership === "TAKEOVER_REQUIRED"
                    ? legacyTaskState === "RUNNING"
                      ? "The legacy Task Scheduler Worker remains the owner and continues running. Quitting Desktop leaves it untouched."
                      : legacyTaskState === "READY"
                        ? "The legacy Task Scheduler task remains enabled and ready. Quitting Desktop leaves it unchanged and does not start the Worker."
                        : "The legacy Task Scheduler remains the recorded owner. Quitting Desktop leaves its task state unchanged."
                    : "Desktop requests a graceful Worker drain and waits for authoritative OFFLINE, clean process exit, and lock release before exiting. A failure leaves Desktop open; it never forces a stop automatically."
                  : "Threads Desktop exits."}{" "}
              Closing this window only hides it to the tray.
            </p>
            {snapshot.role === "WORKER" && !canOperateWorker && (
              <p className="card-copy" role="status">
                Sign in with an unlocked OWNER, ADMIN, or OPERATOR account to quit this Worker
                Desktop.
              </p>
            )}
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
                disabled={quitBusy || (snapshot.role === "WORKER" && !canOperateWorker)}
                onClick={() => void handleQuit()}
              >
                {quitBusy
                  ? snapshot.role === "WORKER"
                    ? "Draining Worker…"
                    : "Stopping node…"
                  : snapshot.role === "WORKER" &&
                      (workerOwnership === "LEGACY" || workerOwnership === "TAKEOVER_REQUIRED")
                    ? "Quit Desktop"
                    : snapshot.role === "WORKER"
                      ? "Gracefully drain and quit"
                      : "Stop node and quit"}
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
                : "Worker Restart drains to authoritative OFFLINE, waits for clean exit, restarts the same enrolled Worker and waits for ONLINE."}{" "}
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
                disabled={restartBusy}
                onClick={() => void handleRestart()}
              >
                {restartBusy
                  ? snapshot.role === "WORKER"
                    ? "Draining and restarting Worker…"
                    : "Restarting…"
                  : "Authenticate and restart"}
              </button>
            </div>
          </section>
        </div>
      )}

      {forceStopRequested && (
        <div className="dialog-backdrop" role="presentation">
          <section
            className="confirm-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="force-stop-title"
          >
            <div className="dialog-symbol dialog-symbol-danger" aria-hidden="true">
              !
            </div>
            <h2 id="force-stop-title">Force stop Worker?</h2>
            <p>
              This is an abnormal interruption of the Desktop-owned Worker process tree. It is not
              graceful, does not prove server OFFLINE, and does not complete a drain. Use it only to
              intervene in the recorded Worker failure.
            </p>
            <label className="confirm-field">
              Type <strong className="confirm-phrase">FORCE STOP WORKER</strong> to continue
              <input
                value={forceStopPhrase}
                onChange={(event) => setForceStopPhrase(event.currentTarget.value)}
                autoComplete="off"
              />
            </label>
            <div className="dialog-actions">
              <button
                type="button"
                className="button button-secondary"
                disabled={forceStopBusy}
                onClick={() => {
                  setForceStopRequested(false);
                  setForceStopPhrase("");
                }}
              >
                Cancel
              </button>
              <button
                type="button"
                className="button button-danger"
                disabled={
                  forceStopPhrase !== "FORCE STOP WORKER" || forceStopBusy || !canForceStopWorker
                }
                onClick={() => void handleForceStopWorker()}
              >
                {forceStopBusy ? "Interrupting Worker…" : "Force stop Worker"}
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
              {snapshot.role === "WORKER"
                ? "Desktop-owned Worker is drained before the preserved legacy Task Scheduler host is restored. A Worker still owned by the legacy task remains with that task. Decommission does not delete the Worker identity, journal, or browser profiles, and fails closed if safe restoration cannot be proven."
                : "This scaffold will stop its mock node and clear only the local role selection. It does not delete Controller data."}
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
                disabled={
                  resetPhrase !== "RESET THIS DEVICE" ||
                  (snapshot.role === "WORKER" && !canOperateWorker)
                }
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
  if (snapshot.supervisor.state === "https_setup_required") return "HTTPS setup required";
  if (snapshot.supervisor.state === "owner_bootstrap_required") return "Owner setup required";
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
    if (snapshot.supervisor.state === "https_setup_required") return "Configure Controller HTTPS";
    if (snapshot.supervisor.state === "owner_bootstrap_required") return "Create the first Owner";
    return "Controller runtime is stopped";
  }
  return "Worker runtime";
}

function roleSymbol(role: ProvisionedRole): string {
  if (role === "CONTROLLER") return "⌘";
  if (role === "WORKER") return "◈";
  return "▣";
}

export default App;
