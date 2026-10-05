import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

export type ProvisionedRole = "CONTROLLER" | "WORKER" | "CONSOLE";
export type UiTheme = "SYSTEM" | "LIGHT" | "DARK";
export type SupervisorState =
  | "stopped"
  | "starting"
  | "preflight"
  | "starting_database"
  | "migrating"
  | "m1_bootstrap_boundary"
  | "https_setup_required"
  | "owner_bootstrap_required"
  | "starting_http"
  | "starting_scheduler"
  | "running"
  | "stopping"
  | "degraded"
  | "failed"
  | "not_applicable";

export interface DesktopSnapshot {
  schemaVersion: number;
  role: ProvisionedRole | null;
  theme: UiTheme;
  autostartEnabled: boolean;
  supervisor: {
    state: SupervisorState;
    processId: number | null;
    postgresProcessId: number | null;
    httpProcessId: number | null;
    schedulerProcessId: number | null;
    controllerId: string | null;
    endpoint: string | null;
    databasePort: number | null;
    diagnosticCode: string | null;
    workerOwnership: "LEGACY" | "TAKEOVER_REQUIRED" | "DESKTOP" | "BLOCKED" | null;
    legacyTaskState: "RUNNING" | "READY" | "DISABLED" | "INVALID" | "NOT_REGISTERED" | null;
    workerId: string | null;
  };
}

export type WorkerStatus =
  | "REGISTERING"
  | "ONLINE"
  | "DEGRADED"
  | "DRAINING"
  | "OFFLINE"
  | "DISABLED"
  | "UPGRADE_REQUIRED";

export interface WorkerDrainStatus {
  workerId: string;
  status: WorkerStatus;
  activeBrowserSessions: number;
  runningWorkerJobs: number;
  quiescent: boolean;
}

export type OperatorRole = "OWNER" | "ADMIN" | "OPERATOR" | "VIEWER";

export interface OperatorIdentity {
  id: string;
  username: string;
  role: OperatorRole;
  mustChangePassword: boolean;
  expiresAt: string;
}

export interface OperatorUser {
  id: string;
  username: string;
  role: OperatorRole;
  enabled: boolean;
  mustChangePassword: boolean;
  createdAt: string;
}

export interface CreatedOperatorUser {
  user: OperatorUser;
  temporaryPassword: string;
}

export interface ControllerHttpsSummary {
  configured: boolean;
  lanAddress: string | null;
  httpsPort: number;
  publicHttpsOrigin: string | null;
  localHttpsOrigin: string;
  rootFingerprint: string | null;
  leafExpiresAt: string | null;
}

export interface TrustProbeSummary {
  probeId: string;
  endpoint: string;
  rootFingerprint: string;
  expiresAt: number;
}

export interface TrustedControllerSummary {
  endpoint: string | null;
  rootFingerprint: string | null;
  trusted: boolean;
}

export const DESKTOP_COMMANDS = [
  "get_desktop_snapshot",
  "provision_role",
  "reset_ui_preferences",
  "decommission_device",
  "request_quit",
  "request_restart",
  "operator_login",
  "operator_bootstrap_owner",
  "operator_current",
  "operator_logout",
  "operator_lock",
  "operator_list_users",
  "operator_create_user",
  "operator_update_user",
  "operator_change_password",
  "takeover_local_worker",
  "rollback_local_worker_to_legacy",
  "local_worker_drain_status",
  "controller_https_configure",
  "controller_https_reconfigure",
  "controller_https_summary",
  "controller_trust_probe",
  "controller_trust_confirm",
  "controller_trust_summary",
] as const;

export function getDesktopSnapshot(): Promise<DesktopSnapshot> {
  return invoke<DesktopSnapshot>("get_desktop_snapshot");
}

export function provisionRole(role: ProvisionedRole): Promise<DesktopSnapshot> {
  return invoke<DesktopSnapshot>("provision_role", { role });
}

export function resetUiPreferences(): Promise<DesktopSnapshot> {
  return invoke<DesktopSnapshot>("reset_ui_preferences");
}

export function decommissionDevice(confirmation: string): Promise<DesktopSnapshot> {
  return invoke<DesktopSnapshot>("decommission_device", { confirmation });
}

export function requestQuit(): Promise<void> {
  return invoke<void>("request_quit");
}

export function requestRestart(): Promise<void> {
  return invoke<void>("request_restart");
}

export function operatorCurrent(): Promise<OperatorIdentity | null> {
  return invoke<OperatorIdentity | null>("operator_current");
}

export function operatorLogin(
  apiUrl: string,
  username: string,
  password: string,
): Promise<OperatorIdentity> {
  return invoke<OperatorIdentity>("operator_login", { apiUrl, username, password });
}

export function operatorBootstrapOwner(
  username: string,
  password: string,
): Promise<OperatorIdentity> {
  return invoke<OperatorIdentity>("operator_bootstrap_owner", { username, password });
}

export function operatorLogout(): Promise<void> {
  return invoke<void>("operator_logout");
}

export function operatorLock(): Promise<void> {
  return invoke<void>("operator_lock");
}

export function operatorListUsers(): Promise<OperatorUser[]> {
  return invoke<OperatorUser[]>("operator_list_users");
}

export function operatorCreateUser(
  username: string,
  role: OperatorRole,
): Promise<CreatedOperatorUser> {
  return invoke<CreatedOperatorUser>("operator_create_user", { username, role });
}

export function operatorUpdateUser(
  userId: string,
  role: OperatorRole | null,
  enabled: boolean | null,
): Promise<OperatorUser> {
  return invoke<OperatorUser>("operator_update_user", { userId, role, enabled });
}

export function operatorChangePassword(newPassword: string): Promise<OperatorIdentity> {
  return invoke<OperatorIdentity>("operator_change_password", { newPassword });
}

export function takeoverLocalWorker(): Promise<void> {
  return invoke<void>("takeover_local_worker");
}

export function rollbackLocalWorkerToLegacy(): Promise<void> {
  return invoke<void>("rollback_local_worker_to_legacy");
}

export function localWorkerDrainStatus(): Promise<WorkerDrainStatus> {
  return invoke<WorkerDrainStatus>("local_worker_drain_status");
}

export function controllerHttpsConfigure(
  lanAddress: string,
  port: number,
): Promise<ControllerHttpsSummary> {
  return invoke<ControllerHttpsSummary>("controller_https_configure", {
    lanAddress,
    port,
  });
}

export function controllerHttpsReconfigure(
  lanAddress: string,
  port: number,
): Promise<ControllerHttpsSummary> {
  return invoke<ControllerHttpsSummary>("controller_https_reconfigure", {
    lanAddress,
    port,
  });
}

export function controllerHttpsSummary(): Promise<ControllerHttpsSummary> {
  return invoke<ControllerHttpsSummary>("controller_https_summary");
}

export function controllerTrustProbe(endpoint: string): Promise<TrustProbeSummary> {
  return invoke<TrustProbeSummary>("controller_trust_probe", { endpoint });
}

export function controllerTrustConfirm(probeId: string): Promise<TrustedControllerSummary> {
  return invoke<TrustedControllerSummary>("controller_trust_confirm", { probeId });
}

export function controllerTrustSummary(endpoint: string): Promise<TrustedControllerSummary> {
  return invoke<TrustedControllerSummary>("controller_trust_summary", { endpoint });
}

export function listenForTrayQuit(): Promise<() => void> {
  return Promise.all([
    listen("desktop://quit-requested", () => {
      window.dispatchEvent(new Event("threads-desktop:quit-requested"));
    }),
    listen("desktop://session-locked", () => {
      window.dispatchEvent(new Event("threads-desktop:session-locked"));
    }),
  ]).then((unlisten) => () => {
    unlisten.forEach((stop) => {
      stop();
    });
  });
}
