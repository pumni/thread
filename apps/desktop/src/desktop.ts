import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

export type ProvisionedRole = "CONTROLLER" | "WORKER" | "CONSOLE";
export type UiTheme = "SYSTEM" | "LIGHT" | "DARK";
export type SupervisorState =
  | "stopped"
  | "starting"
  | "running"
  | "stopping"
  | "degraded"
  | "not_applicable";

export interface DesktopSnapshot {
  schemaVersion: number;
  role: ProvisionedRole | null;
  theme: UiTheme;
  autostartEnabled: boolean;
  supervisor: {
    state: SupervisorState;
    processId: number | null;
    diagnosticCode: string | null;
  };
}

export const DESKTOP_COMMANDS = [
  "get_desktop_snapshot",
  "provision_role",
  "reset_ui_preferences",
  "decommission_device",
  "request_quit",
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
