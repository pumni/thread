import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../src/App";
import {
  DESKTOP_COMMANDS,
  forceStopWorker,
  operatorLock,
  type DesktopSnapshot,
} from "../src/desktop";
import { SessionGate } from "../src/SessionGate";

const native = vi.hoisted(() => ({
  invoke: vi.fn(),
  listen: vi.fn(),
}));

vi.mock("@tauri-apps/api/core", () => ({ invoke: native.invoke }));
vi.mock("@tauri-apps/api/event", () => ({ listen: native.listen }));

function snapshot(
  role: DesktopSnapshot["role"] = null,
  supervisorOverrides: Partial<DesktopSnapshot["supervisor"]> = {},
): DesktopSnapshot {
  const supervisor: DesktopSnapshot["supervisor"] = {
    state: role === "CONSOLE" ? "not_applicable" : role ? "running" : "stopped",
    processId: role && role !== "CONSOLE" ? 4242 : null,
    postgresProcessId: role === "CONTROLLER" ? 4243 : null,
    httpProcessId: role === "CONTROLLER" ? 4244 : null,
    schedulerProcessId: role === "CONTROLLER" ? 4245 : null,
    controllerId: role === "CONTROLLER" ? "0123456789abcdef0123456789abcdef" : null,
    endpoint: role === "CONTROLLER" ? "https://127.0.0.1:8443" : null,
    databasePort: role === "CONTROLLER" ? 4247 : null,
    diagnosticCode: null,
    workerOwnership: role === "WORKER" ? "BLOCKED" : null,
    legacyTaskState: null,
    workerId: role === "WORKER" ? "12345678-1234-4234-8234-123456789abc" : null,
    ...supervisorOverrides,
  };
  return {
    schemaVersion: 1,
    role,
    theme: "SYSTEM",
    autostartEnabled: role !== null,
    supervisor,
  };
}

function operatorSession(
  role: "OWNER" | "ADMIN" | "OPERATOR" | "VIEWER" = "OWNER",
  mustChangePassword = false,
) {
  return {
    id: "worker-operator-id",
    username: "worker-operator",
    role,
    mustChangePassword,
    expiresAt: "2026-10-03T18:00:00Z",
  };
}

function renderDesktop() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>,
  );
}

function fireHiddenVisibilityChange() {
  const visibilityDescriptor = Object.getOwnPropertyDescriptor(document, "visibilityState");
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    value: "hidden",
  });
  try {
    fireEvent(document, new Event("visibilitychange"));
  } finally {
    if (visibilityDescriptor) {
      Object.defineProperty(document, "visibilityState", visibilityDescriptor);
    } else {
      Reflect.deleteProperty(document, "visibilityState");
    }
  }
}

describe("desktop provisioning", () => {
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  beforeEach(() => {
    native.invoke.mockReset();
    native.listen.mockReset();
    native.listen.mockResolvedValue(() => undefined);
  });

  it("only invokes the fixed native command set", () => {
    expect(DESKTOP_COMMANDS).toEqual([
      "get_desktop_snapshot",
      "provision_role",
      "reset_ui_preferences",
      "decommission_device",
      "request_quit",
      "request_restart",
      "force_stop_worker",
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
    ]);
    expect(DESKTOP_COMMANDS).not.toContain("request_local_worker_drain");
  });

  it("exposes only the confirmed high-level Worker force operation", async () => {
    native.invoke.mockResolvedValue(snapshot("WORKER"));

    await forceStopWorker("FORCE STOP WORKER");

    expect(native.invoke).toHaveBeenCalledWith("force_stop_worker", {
      confirmation: "FORCE STOP WORKER",
    });
  });

  it("shows explicit initial HTTPS configuration when no identity is provisioned", async () => {
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot") return snapshot("CONTROLLER");
      if (command === "operator_current") return null;
      if (command === "controller_https_summary") {
        return {
          configured: false,
          lanAddress: null,
          httpsPort: 8443,
          publicHttpsOrigin: null,
          localHttpsOrigin: "https://127.0.0.1:8443",
          rootFingerprint: null,
          leafExpiresAt: null,
        };
      }
      if (command === "controller_https_configure") {
        return {
          configured: true,
          lanAddress: "192.0.2.20",
          httpsPort: 8443,
          publicHttpsOrigin: "https://192.0.2.20:8443",
          localHttpsOrigin: "https://127.0.0.1:8443",
          rootFingerprint: "SHA256:1234567890abcdef",
          leafExpiresAt: "2027-01-01T00:00:00Z",
        };
      }
      throw new Error(`unexpected native command: ${command}`);
    });

    renderDesktop();
    await screen.findByLabelText("Stable LAN IPv4 address");
    expect(screen.getByLabelText("HTTPS port")).toHaveValue(8443);
    fireEvent.change(screen.getByLabelText("Stable LAN IPv4 address"), {
      target: { value: "192.0.2.20" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Configure HTTPS" }));

    expect(await screen.findByText("https://192.0.2.20:8443")).toBeInTheDocument();
    expect(screen.getByText("SHA256:1234567890abcdef")).toBeInTheDocument();
    expect(native.invoke).toHaveBeenCalledWith("controller_https_configure", {
      lanAddress: "192.0.2.20",
      port: 8443,
    });
  });

  it("requires an explicit reconfiguration action and reports native authorization errors", async () => {
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot") return snapshot("CONTROLLER");
      if (command === "operator_current") {
        return {
          id: "owner-id",
          username: "controller-owner",
          role: "OWNER",
          mustChangePassword: false,
          expiresAt: "2027-01-01T00:00:00Z",
        };
      }
      if (command === "operator_list_users") return [];
      if (command === "controller_https_summary") {
        return {
          configured: true,
          lanAddress: "192.0.2.20",
          httpsPort: 8443,
          publicHttpsOrigin: "https://192.0.2.20:8443",
          localHttpsOrigin: "https://127.0.0.1:8443",
          rootFingerprint: "SHA256:1234567890abcdef",
          leafExpiresAt: "2027-01-01T00:00:00Z",
        };
      }
      if (command === "controller_https_reconfigure") {
        throw new Error("controller_https_reconfiguration_unauthorized");
      }
      throw new Error(`unexpected native command: ${command}`);
    });

    renderDesktop();
    await screen.findByRole("button", { name: "Reconfigure HTTPS endpoint…" });
    expect(screen.queryByLabelText("Stable LAN IPv4 address")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Reconfigure HTTPS endpoint…" }));
    expect(screen.getByLabelText("Stable LAN IPv4 address")).toHaveValue("192.0.2.20");
    expect(screen.getByLabelText("HTTPS port")).toHaveValue(8443);
    fireEvent.change(screen.getByLabelText("Stable LAN IPv4 address"), {
      target: { value: "198.51.100.30" },
    });
    fireEvent.change(screen.getByLabelText("HTTPS port"), { target: { value: "9443" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply endpoint change" }));

    expect(
      await screen.findByText(
        "Only a signed-in Controller Owner or Admin can change this endpoint.",
      ),
    ).toBeInTheDocument();
    expect(native.invoke).toHaveBeenCalledWith("controller_https_reconfigure", {
      lanAddress: "198.51.100.30",
      port: 9443,
    });
  });

  it("requires sign-in and refreshes the endpoint when cutover session revocation is unconfirmed", async () => {
    let summaryReads = 0;
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot") return snapshot("CONTROLLER");
      if (command === "operator_current") {
        return {
          id: "owner-id",
          username: "controller-owner",
          role: "OWNER",
          mustChangePassword: false,
          expiresAt: "2027-01-01T00:00:00Z",
        };
      }
      if (command === "operator_list_users") return [];
      if (command === "controller_https_summary") {
        summaryReads += 1;
        const port = summaryReads === 1 ? 8443 : 9443;
        const address = summaryReads === 1 ? "192.0.2.20" : "198.51.100.30";
        return {
          configured: true,
          lanAddress: address,
          httpsPort: port,
          publicHttpsOrigin: `https://${address}:${port}`,
          localHttpsOrigin: `https://127.0.0.1:${port}`,
          rootFingerprint: "SHA256:1234567890abcdef",
          leafExpiresAt: "2027-01-01T00:00:00Z",
        };
      }
      if (command === "controller_https_reconfigure") {
        throw "controller_endpoint_reconfigure_session_not_revoked";
      }
      throw new Error(`unexpected native command: ${command}`);
    });

    renderDesktop();
    await screen.findByRole("button", { name: "Reconfigure HTTPS endpoint…" });
    fireEvent.click(screen.getByRole("button", { name: "Reconfigure HTTPS endpoint…" }));
    fireEvent.change(screen.getByLabelText("Stable LAN IPv4 address"), {
      target: { value: "198.51.100.30" },
    });
    fireEvent.change(screen.getByLabelText("HTTPS port"), { target: { value: "9443" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply endpoint change" }));

    expect(
      await screen.findByText(
        "The endpoint changed, but session revocation could not be confirmed. Sign in again to continue.",
      ),
    ).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByText("https://198.51.100.30:9443")).toBeInTheDocument();
    expect(screen.queryByText("Signed in as controller-owner")).not.toBeInTheDocument();
  });

  it("provisions one Controller and shows its separate runtime processes", async () => {
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot") return snapshot();
      if (command === "provision_role") return snapshot("CONTROLLER");
      throw new Error("unknown command");
    });

    renderDesktop();
    await screen.findByRole("heading", { name: "Choose this PC’s role" });
    fireEvent.click(screen.getByRole("button", { name: "Provision as Controller" }));

    await screen.findByRole("heading", { name: "Your Controller" });
    expect(await screen.findByText("PID 4243")).toBeInTheDocument();
    expect(screen.getByText("PID 4244")).toBeInTheDocument();
    expect(screen.getByText("PID 4245")).toBeInTheDocument();
    expect(screen.getByText("https://127.0.0.1:8443")).toBeInTheDocument();
    expect(screen.getByText(/disposable test data only/i)).toBeInTheDocument();
    expect(native.invoke).toHaveBeenCalledWith("provision_role", { role: "CONTROLLER" });
  });

  it("provisions Worker without exposing an ordinary role switch", async () => {
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot") return snapshot();
      if (command === "provision_role") return snapshot("WORKER");
      throw new Error("unknown command");
    });

    renderDesktop();
    await screen.findByRole("heading", { name: "Choose this PC’s role" });
    fireEvent.click(screen.getByRole("button", { name: "Provision as Worker" }));

    await screen.findByRole("heading", { name: "Your Worker" });
    expect(await screen.findByText("PID 4242")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Provision as Controller" }),
    ).not.toBeInTheDocument();
    expect(native.invoke).toHaveBeenCalledWith("provision_role", { role: "WORKER" });
  });

  it("keeps Console client-only without a local helper", async () => {
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot") return snapshot();
      if (command === "provision_role") return snapshot("CONSOLE");
      throw new Error("unknown command");
    });

    renderDesktop();
    await screen.findByRole("heading", { name: "Choose this PC’s role" });
    fireEvent.click(screen.getByRole("button", { name: "Provision as Console" }));

    expect(
      await screen.findByText(
        "Console mode is client-only and starts no local Worker or Controller runtime.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("None")).toBeInTheDocument();
  });

  it("requires an explicit native trust confirmation before remote Operator credentials", async () => {
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot") return snapshot("CONSOLE");
      if (command === "operator_current") return null;
      if (command === "controller_trust_probe") {
        return {
          probeId: "opaque-probe-id",
          endpoint: "https://192.168.1.20:8443",
          rootFingerprint: "SHA256:0123456789abcdef",
          expiresAt: 1_800_000_000,
        };
      }
      if (command === "controller_trust_confirm") {
        return {
          endpoint: "https://192.168.1.20:8443",
          rootFingerprint: "SHA256:0123456789abcdef",
          trusted: true,
        };
      }
      if (command === "operator_login") {
        return {
          id: "operator-id",
          username: "remote-operator",
          role: "OPERATOR",
          mustChangePassword: false,
          expiresAt: "2026-10-04T18:00:00Z",
        };
      }
      throw new Error(`unexpected native command: ${command}`);
    });

    renderDesktop();
    await screen.findByRole("heading", { name: "Your Console" });
    fireEvent.change(screen.getByLabelText("Controller address"), {
      target: { value: "https://192.168.1.20:8443" },
    });
    expect(screen.getByLabelText("Username")).toBeDisabled();
    expect(screen.getByLabelText("Password")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Probe Controller identity" }));
    expect(await screen.findByText("SHA256:0123456789abcdef")).toBeInTheDocument();
    expect(screen.getByLabelText("Username")).toBeDisabled();
    expect(native.invoke).not.toHaveBeenCalledWith("operator_login", expect.anything());

    fireEvent.click(screen.getByRole("button", { name: "Confirm matching fingerprint" }));
    await waitFor(() => expect(screen.getByLabelText("Username")).toBeEnabled());
    fireEvent.change(screen.getByLabelText("Username"), {
      target: { value: "remote-operator" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "synthetic password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByText("Signed in as remote-operator");
    expect(native.invoke).toHaveBeenCalledWith("controller_trust_confirm", {
      probeId: "opaque-probe-id",
    });
    expect(native.invoke).toHaveBeenCalledWith("operator_login", {
      apiUrl: "https://192.168.1.20:8443",
      username: "remote-operator",
      password: "synthetic password",
    });
  });

  it("keeps the Operator bearer inside Rust during first-Owner login", async () => {
    const bearer = "SYNTHETIC_OPERATOR_BEARER_NEVER_RENDERED";
    const controllerSnapshot = snapshot("CONTROLLER");
    controllerSnapshot.supervisor.state = "owner_bootstrap_required";
    controllerSnapshot.supervisor.httpProcessId = null;
    controllerSnapshot.supervisor.schedulerProcessId = null;
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot") return controllerSnapshot;
      if (command === "controller_https_summary") {
        return {
          configured: true,
          lanAddress: "192.168.1.20",
          httpsPort: 8443,
          publicHttpsOrigin: "https://192.168.1.20:8443",
          localHttpsOrigin: "https://127.0.0.1:8443",
          rootFingerprint: "SHA256:0123456789abcdef",
          leafExpiresAt: "2026-12-31T00:00:00Z",
        };
      }
      if (command === "operator_current") return null;
      if (command === "operator_bootstrap_owner") {
        return {
          id: "owner-id",
          username: "first-owner",
          role: "OWNER",
          mustChangePassword: false,
          expiresAt: "2026-10-03T18:00:00Z",
        };
      }
      if (command === "operator_list_users") return [];
      throw new Error(`unexpected native command: ${command}`);
    });

    renderDesktop();
    await screen.findByRole("heading", { name: "Your Controller" });
    fireEvent.click(screen.getByRole("button", { name: "Set up first Owner" }));
    fireEvent.change(screen.getByLabelText("Username"), {
      target: { value: "first-owner" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "synthetic owner passphrase" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create first Owner" }));

    expect(await screen.findByText("Signed in as first-owner")).toBeInTheDocument();
    expect(screen.queryByText(bearer)).not.toBeInTheDocument();
    expect(native.invoke).toHaveBeenCalledWith("operator_bootstrap_owner", {
      username: "first-owner",
      password: "synthetic owner passphrase",
    });
    expect(native.listen).toHaveBeenCalledTimes(2);
    expect(localStorage.length).toBe(0);
  });

  it.each([
    { role: "CONTROLLER" as const, heading: "Your Controller", process: "PID 4243" },
    { role: "WORKER" as const, heading: "Your Worker", process: "PID 4242" },
  ])(
    "keeps the $role runtime running when the native session relocks",
    async ({ role, heading, process }) => {
      const listeners: Record<string, (event: unknown) => void> = {};
      native.listen.mockImplementation(async (event: string, handler: (event: unknown) => void) => {
        listeners[event] = handler;
        return () => undefined;
      });
      native.invoke.mockImplementation(async (command: string) => {
        if (command === "get_desktop_snapshot") return snapshot(role);
        if (command === "operator_current") return null;
        throw new Error(`unexpected native command: ${command}`);
      });

      renderDesktop();
      await screen.findByRole("heading", { name: heading });
      await waitFor(() => expect(listeners["desktop://session-locked"]).toBeDefined());
      act(() => listeners["desktop://session-locked"]({}));

      expect(await screen.findByText("Session locked")).toBeInTheDocument();
      expect(screen.getByText(process)).toBeInTheDocument();
      expect(native.invoke.mock.calls.map(([command]) => command)).toEqual(
        expect.arrayContaining(["get_desktop_snapshot"]),
      );
      expect(native.invoke.mock.calls.map(([command]) => command)).not.toContain("request_quit");
      expect(native.invoke.mock.calls.map(([command]) => command)).not.toContain(
        "decommission_device",
      );
    },
  );

  it("requires the explicit reset phrase before decommissioning", async () => {
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot") return snapshot("WORKER");
      if (command === "operator_current") return operatorSession("OWNER");
      if (command === "operator_list_users") return [];
      if (command === "local_worker_drain_status") {
        return {
          workerId: "12345678-1234-4234-8234-123456789abc",
          status: "ONLINE",
          activeBrowserSessions: 0,
          runningWorkerJobs: 0,
          quiescent: false,
        };
      }
      if (command === "decommission_device") return snapshot();
      if (command === "operator_logout") return undefined;
      throw new Error("unknown command");
    });

    renderDesktop();
    await screen.findByRole("heading", { name: "Your Worker" });
    fireEvent.click(screen.getByRole("button", { name: "Decommission device" }));
    const action = screen.getByRole("button", { name: /^Decommission$/ });
    expect(action).toBeDisabled();

    fireEvent.change(screen.getByLabelText(/Type RESET THIS DEVICE to continue/), {
      target: { value: "RESET THIS DEVICE" },
    });
    fireEvent.click(action);

    await waitFor(() => {
      expect(native.invoke).toHaveBeenCalledWith("decommission_device", {
        confirmation: "RESET THIS DEVICE",
      });
    });
    expect(
      await screen.findByRole("heading", { name: "Choose this PC’s role" }),
    ).toBeInTheDocument();
  });

  it("never renders protected information without an authenticated session", () => {
    render(
      <SessionGate validSession={false} onUnlock={async () => false}>
        <p>private account data</p>
      </SessionGate>,
    );
    expect(screen.queryByText("private account data")).not.toBeInTheDocument();
    expect(screen.getByText("Session locked")).toBeInTheDocument();
  });

  it("asks for confirmation when the tray requests Quit", async () => {
    const listeners: Record<string, (event: unknown) => void> = {};
    native.listen.mockImplementation(async (event: string, handler: (event: unknown) => void) => {
      listeners[event] = handler;
      return () => undefined;
    });
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot") return snapshot();
      if (command === "request_quit") return undefined;
      throw new Error("unknown command");
    });

    renderDesktop();
    await screen.findByRole("heading", { name: "Choose this PC’s role" });
    await waitFor(() => expect(listeners["desktop://quit-requested"]).toBeDefined());
    listeners["desktop://quit-requested"]({});

    expect(
      await screen.findByRole("dialog", { name: "Quit Threads Desktop?" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Stop node and quit" }));
    await waitFor(() => expect(native.invoke).toHaveBeenCalledWith("request_quit"));
  });

  it("reports a Controller shutdown failure separately from Operator access denial", async () => {
    const listeners: Record<string, (event: unknown) => void> = {};
    native.listen.mockImplementation(async (event: string, handler: (event: unknown) => void) => {
      listeners[event] = handler;
      return () => undefined;
    });
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot") return snapshot("CONTROLLER");
      if (command === "operator_current") return null;
      if (command === "operator_logout") return undefined;
      if (command === "request_quit") throw "controller_database_stop_failed";
      throw new Error("unknown command");
    });

    renderDesktop();
    await screen.findByRole("heading", { name: "Your Controller" });
    await waitFor(() => expect(listeners["desktop://quit-requested"]).toBeDefined());
    listeners["desktop://quit-requested"]({});
    fireEvent.click(await screen.findByRole("button", { name: "Stop node and quit" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The node could not stop cleanly. Check the runtime status before retrying.",
    );
    expect(
      screen.queryByText(
        "An active Operator session with permission to stop this node is required.",
      ),
    ).not.toBeInTheDocument();
  });

  it("keeps the Worker Operator session available when graceful Quit needs intervention", async () => {
    const listeners: Record<string, (event: unknown) => void> = {};
    native.listen.mockImplementation(async (event: string, handler: (event: unknown) => void) => {
      listeners[event] = handler;
      return () => undefined;
    });
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot") return snapshot("WORKER");
      if (command === "operator_current") {
        return {
          id: "owner-id",
          username: "worker-owner",
          role: "OWNER",
          mustChangePassword: false,
          expiresAt: "2026-10-03T18:00:00Z",
        };
      }
      if (command === "operator_list_users") return [];
      if (command === "local_worker_drain_status") {
        return {
          workerId: "12345678-1234-4234-8234-123456789abc",
          status: "DRAINING",
          activeBrowserSessions: 1,
          runningWorkerJobs: 1,
          quiescent: false,
        };
      }
      if (command === "request_quit") throw "worker_drain_unavailable";
      throw new Error(`unexpected native command: ${command}`);
    });

    renderDesktop();
    expect(await screen.findByText("Signed in as worker-owner")).toBeInTheDocument();
    await waitFor(() => expect(listeners["desktop://quit-requested"]).toBeDefined());
    listeners["desktop://quit-requested"]({});
    fireEvent.click(await screen.findByRole("button", { name: "Gracefully drain and quit" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Worker graceful quit did not complete. Diagnostic code: worker_drain_unavailable.",
    );
    expect(screen.getByText("Signed in as worker-owner")).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Quit Threads Desktop?" })).toBeInTheDocument();
    expect(native.invoke.mock.calls.map(([command]) => command)).not.toContain("operator_logout");
  });

  it("waits for session revocation before exposing the next sign-in form", async () => {
    let completeLogout: (() => void) | undefined;
    const logoutPending = new Promise<void>((resolve) => {
      completeLogout = resolve;
    });
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot") return snapshot("CONTROLLER");
      if (command === "operator_current") {
        return {
          id: "owner-id",
          username: "first-owner",
          role: "OWNER",
          mustChangePassword: false,
          expiresAt: "2026-10-03T18:00:00Z",
        };
      }
      if (command === "operator_list_users") return [];
      if (command === "operator_logout") return logoutPending;
      throw new Error("unknown command");
    });

    renderDesktop();
    await screen.findByRole("button", { name: "Sign out" });
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(native.invoke).toHaveBeenCalledWith("operator_logout");
    expect(native.invoke).not.toHaveBeenCalledWith("operator_lock");

    expect(
      await screen.findByText("Authenticate again to view protected Operator data."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign in" })).not.toBeInTheDocument();

    await act(async () => completeLogout?.());
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
  });

  it("explains when the current Operator role cannot stop a Controller", async () => {
    const listeners: Record<string, (event: unknown) => void> = {};
    native.listen.mockImplementation(async (event: string, handler: (event: unknown) => void) => {
      listeners[event] = handler;
      return () => undefined;
    });
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot") return snapshot("CONTROLLER");
      if (command === "operator_current") {
        return {
          id: "operator-id",
          username: "current-operator",
          role: "OPERATOR",
          mustChangePassword: false,
          expiresAt: "2026-10-03T18:00:00Z",
        };
      }
      if (command === "operator_list_users" || command === "operator_logout") return [];
      if (command === "request_quit") throw "operator_forbidden";
      throw new Error("unknown command");
    });

    renderDesktop();
    await screen.findByText("Signed in as current-operator");
    await waitFor(() => expect(listeners["desktop://quit-requested"]).toBeDefined());
    listeners["desktop://quit-requested"]({});
    fireEvent.click(await screen.findByRole("button", { name: "Stop node and quit" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Only an Owner or Admin can stop this Controller.",
    );
  });

  it("does not lock or revoke the session on generic window blur", () => {
    const onLock = vi.fn();
    render(
      <SessionGate validSession onUnlock={async () => false} onLock={onLock}>
        <p>private account data</p>
      </SessionGate>,
    );
    expect(screen.getByText("private account data")).toBeInTheDocument();
    fireEvent.blur(window);
    expect(screen.getByText("private account data")).toBeInTheDocument();
    expect(screen.queryByText("Session locked")).not.toBeInTheDocument();
    expect(onLock).not.toHaveBeenCalled();
  });

  it("does not lock or revoke the session on generic document visibility loss", () => {
    const onLock = vi.fn();
    render(
      <SessionGate validSession onUnlock={async () => false} onLock={onLock}>
        <p>private account data</p>
      </SessionGate>,
    );
    fireHiddenVisibilityChange();

    expect(screen.getByText("private account data")).toBeInTheDocument();
    expect(screen.queryByText("Session locked")).not.toBeInTheDocument();
    expect(onLock).not.toHaveBeenCalled();
  });

  it("does not lock or sign out an Owner when Quit confirmation causes focus loss", async () => {
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot") return snapshot("CONTROLLER");
      if (command === "operator_current") {
        return {
          id: "owner-id",
          username: "first-owner",
          role: "OWNER",
          mustChangePassword: false,
          expiresAt: "2026-10-03T18:00:00Z",
        };
      }
      if (command === "operator_list_users") return [];
      if (command === "operator_lock" || command === "operator_logout") return undefined;
      throw new Error(`unexpected command: ${command}`);
    });

    renderDesktop();
    await screen.findByText("Signed in as first-owner");
    fireEvent.click(screen.getByRole("button", { name: "Quit…" }));
    expect(
      await screen.findByRole("dialog", { name: "Quit Threads Desktop?" }),
    ).toBeInTheDocument();
    fireEvent.blur(window);

    expect(screen.getByRole("dialog", { name: "Quit Threads Desktop?" })).toBeInTheDocument();
    expect(screen.getByText("Signed in as first-owner")).toBeInTheDocument();
    expect(native.invoke.mock.calls.map(([command]) => command)).not.toContain("operator_lock");
    expect(native.invoke.mock.calls.map(([command]) => command)).not.toContain("operator_logout");
  });

  it("clears Operator UI on a native lock when Rust has no current session", async () => {
    const listeners: Record<string, (event: unknown) => void> = {};
    native.listen.mockImplementation(async (event: string, handler: (event: unknown) => void) => {
      listeners[event] = handler;
      return () => undefined;
    });
    let currentCalls = 0;
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot") return snapshot("CONTROLLER");
      if (command === "operator_current") {
        currentCalls += 1;
        return currentCalls === 1
          ? {
              id: "owner-id",
              username: "first-owner",
              role: "OWNER",
              mustChangePassword: false,
              expiresAt: "2026-10-03T18:00:00Z",
            }
          : null;
      }
      if (command === "operator_list_users") return [];
      throw new Error(`unexpected native command: ${command}`);
    });

    renderDesktop();
    await screen.findByText("Signed in as first-owner");
    await waitFor(() => expect(listeners["desktop://session-locked"]).toBeDefined());
    act(() => listeners["desktop://session-locked"]({}));

    expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.queryByText("Signed in as first-owner")).not.toBeInTheDocument();
    expect(
      native.invoke.mock.calls.filter(([command]) => command === "operator_current"),
    ).toHaveLength(2);
    expect(native.invoke.mock.calls.map(([command]) => command)).not.toContain("operator_lock");
    expect(native.invoke.mock.calls.map(([command]) => command)).not.toContain("operator_logout");
  });

  it("ignores a stale native lock notification when a newer Rust session is current", async () => {
    const listeners: Record<string, (event: unknown) => void> = {};
    native.listen.mockImplementation(async (event: string, handler: (event: unknown) => void) => {
      listeners[event] = handler;
      return () => undefined;
    });
    let currentCalls = 0;
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot") return snapshot("CONTROLLER");
      if (command === "operator_current") {
        currentCalls += 1;
        return {
          id: currentCalls === 1 ? "owner-a" : "owner-b",
          username: currentCalls === 1 ? "first-owner" : "new-owner",
          role: "OWNER",
          mustChangePassword: false,
          expiresAt: "2026-10-03T18:00:00Z",
        };
      }
      if (command === "operator_list_users") return [];
      throw new Error(`unexpected native command: ${command}`);
    });

    renderDesktop();
    await screen.findByText("Signed in as first-owner");
    await waitFor(() => expect(listeners["desktop://session-locked"]).toBeDefined());
    act(() => listeners["desktop://session-locked"]({}));

    expect(await screen.findByText("Signed in as new-owner")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign in" })).not.toBeInTheDocument();
    expect(native.invoke.mock.calls.map(([command]) => command)).not.toContain("operator_lock");
    expect(native.invoke.mock.calls.map(([command]) => command)).not.toContain("operator_logout");
  });

  it("locks on the configured inactivity timeout", async () => {
    vi.useFakeTimers();
    const onLock = vi.fn(() => operatorLock());
    render(
      <SessionGate validSession idleTimeoutMs={1_000} onUnlock={async () => false} onLock={onLock}>
        <p>private account data</p>
      </SessionGate>,
    );

    expect(screen.getByText("private account data")).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(screen.queryByText("private account data")).not.toBeInTheDocument();
    expect(screen.getByText("Session locked")).toBeInTheDocument();
    expect(onLock).toHaveBeenCalledOnce();
    expect(native.invoke).toHaveBeenCalledWith("operator_lock");
    vi.useRealTimers();
  });

  it("keeps a logged-out gate locked after tray focus while the Worker Agent continues", () => {
    const onUnlock = vi.fn().mockResolvedValue(false);
    const { rerender } = render(
      <>
        <p>Worker Agent running</p>
        <SessionGate validSession onUnlock={onUnlock}>
          <p>private account data</p>
        </SessionGate>
      </>,
    );
    expect(screen.getByText("private account data")).toBeInTheDocument();

    rerender(
      <>
        <p>Worker Agent running</p>
        <SessionGate validSession={false} onUnlock={onUnlock}>
          <p>private account data</p>
        </SessionGate>
      </>,
    );
    expect(screen.queryByText("private account data")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Re-authenticate" })).not.toBeInTheDocument();

    rerender(
      <>
        <p>Worker Agent running</p>
        <SessionGate validSession onUnlock={onUnlock}>
          <p>private account data</p>
        </SessionGate>
      </>,
    );
    fireEvent.focus(window);
    expect(screen.queryByText("private account data")).not.toBeInTheDocument();
    expect(screen.getByText("Session locked")).toBeInTheDocument();
    expect(screen.getByText("Worker Agent running")).toBeInTheDocument();
    expect(onUnlock).not.toHaveBeenCalled();
    expect(native.invoke).not.toHaveBeenCalled();
  });

  it("shows real Worker ownership facts without mock, helper, secret, or exception copy", async () => {
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot") {
        return snapshot("WORKER", {
          state: "degraded",
          processId: null,
          workerOwnership: "DESKTOP",
          legacyTaskState: "DISABLED",
          workerId: "12345678-1234-4234-8234-123456789abc",
          diagnosticCode: "worker_drain_timeout",
        });
      }
      if (command === "operator_current") return null;
      throw new Error("unexpected command");
    });

    renderDesktop();
    await screen.findByRole("heading", { name: "Your Worker" });

    expect(screen.getAllByText("DESKTOP", { exact: true })).toHaveLength(2);
    expect(screen.getByText("DISABLED", { exact: true })).toBeInTheDocument();
    expect(screen.getByText("Stopped", { exact: true })).toBeInTheDocument();
    expect(screen.getByText("degraded", { exact: true })).toBeInTheDocument();
    expect(screen.getByText("12345678-1234-4234-8234-123456789abc")).toBeInTheDocument();
    expect(screen.getByText("Diagnostic code: worker_drain_timeout")).toBeInTheDocument();
    expect(screen.queryByText(/Worker mock|Helper process|DX-02 mock boundary/i)).toBeNull();
    expect(screen.queryByText(/bearer|private key|enrollment code|DPAPI ciphertext/i)).toBeNull();
    expect(native.invoke).not.toHaveBeenCalledWith("local_worker_drain_status");
  });

  it.each(["LEGACY", "TAKEOVER_REQUIRED", "DESKTOP", "BLOCKED"] as const)(
    "renders Worker ownership state %s distinctly",
    async (ownership) => {
      native.invoke.mockImplementation(async (command: string) => {
        if (command === "get_desktop_snapshot") {
          return snapshot("WORKER", {
            processId: ownership === "DESKTOP" ? 4242 : null,
            workerOwnership: ownership,
            legacyTaskState: ownership === "TAKEOVER_REQUIRED" ? "READY" : "DISABLED",
          });
        }
        if (command === "operator_current") return null;
        throw new Error("unexpected command");
      });

      renderDesktop();
      await screen.findByRole("heading", { name: "Your Worker" });
      expect(screen.getAllByText(ownership, { exact: true }).length).toBeGreaterThan(0);
      if (ownership === "LEGACY" || ownership === "TAKEOVER_REQUIRED") {
        expect(screen.getByText("Not owned by Desktop", { exact: true })).toBeInTheDocument();
      }
    },
  );

  it.each(["RUNNING", "READY", "DISABLED", "INVALID", "NOT_REGISTERED"] as const)(
    "renders legacy task state %s distinctly",
    async (taskState) => {
      native.invoke.mockImplementation(async (command: string) => {
        if (command === "get_desktop_snapshot") {
          return snapshot("WORKER", {
            workerOwnership: "BLOCKED",
            legacyTaskState: taskState,
          });
        }
        if (command === "operator_current") return null;
        throw new Error("unexpected command");
      });

      renderDesktop();
      await screen.findByRole("heading", { name: "Your Worker" });
      expect(screen.getByText(taskState, { exact: true })).toBeInTheDocument();
    },
  );

  it.each([
    "REGISTERING",
    "ONLINE",
    "DEGRADED",
    "DRAINING",
    "OFFLINE",
    "DISABLED",
    "UPGRADE_REQUIRED",
  ] as const)("renders server Worker status %s from the read-only response", async (status) => {
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot") {
        return snapshot("WORKER", { processId: null, workerOwnership: "DESKTOP" });
      }
      if (command === "operator_current") return operatorSession("OPERATOR");
      if (command === "local_worker_drain_status") {
        return {
          workerId: "12345678-1234-4234-8234-123456789abc",
          status,
          activeBrowserSessions: 2,
          runningWorkerJobs: 3,
          quiescent: false,
        };
      }
      throw new Error(`unexpected command: ${command}`);
    });

    renderDesktop();
    expect(await screen.findByText(status, { exact: true })).toBeInTheDocument();
    expect(native.invoke).toHaveBeenCalledWith("local_worker_drain_status");
    expect(
      native.invoke.mock.calls
        .filter(([command]) => command === "local_worker_drain_status")
        .every((call) => call.length === 1),
    ).toBe(true);
    expect(screen.getByText("2", { exact: true })).toBeInTheDocument();
    expect(screen.getByText("3", { exact: true })).toBeInTheDocument();
    expect(screen.getByText("false", { exact: true })).toBeInTheDocument();
    expect(screen.getByText("Stopped", { exact: true })).toBeInTheDocument();
  });

  it("shows DRAINING browser/job counts and quiescence from the server", async () => {
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot")
        return snapshot("WORKER", { workerOwnership: "DESKTOP" });
      if (command === "operator_current") return operatorSession("ADMIN");
      if (command === "operator_list_users") return [];
      if (command === "local_worker_drain_status") {
        return {
          workerId: "12345678-1234-4234-8234-123456789abc",
          status: "DRAINING",
          activeBrowserSessions: 0,
          runningWorkerJobs: 0,
          quiescent: true,
        };
      }
      throw new Error(`unexpected command: ${command}`);
    });

    renderDesktop();
    expect(await screen.findByText("DRAINING", { exact: true })).toBeInTheDocument();
    expect(screen.getByText("true", { exact: true })).toBeInTheDocument();
  });

  it.each(["operator_session_revoked", "operator_authentication_required"] as const)(
    "returns protected Worker UI to sign-in after %s status response",
    async (code) => {
      native.invoke.mockImplementation(async (command: string) => {
        if (command === "get_desktop_snapshot") return snapshot("WORKER");
        if (command === "operator_current") return operatorSession("OWNER");
        if (command === "operator_list_users") return [];
        if (command === "local_worker_drain_status") throw code;
        throw new Error(`unexpected command: ${command}`);
      });

      renderDesktop();
      expect(await screen.findByText("Session locked")).toBeInTheDocument();
      expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
      expect(screen.queryByText("ONLINE", { exact: true })).toBeNull();
      expect(native.invoke.mock.calls.map(([command]) => command)).not.toContain("request_quit");
    },
  );

  it.each([
    ["operator_forbidden", "UNAUTHORIZED"],
    ["worker_drain_unavailable", "UNAVAILABLE"],
  ] as const)("shows %s as %s without fabricating Worker server state", async (code, label) => {
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot") {
        return snapshot("WORKER", { processId: null, workerOwnership: "DESKTOP" });
      }
      if (command === "operator_current") return operatorSession("OPERATOR");
      if (command === "local_worker_drain_status") throw code;
      throw new Error(`unexpected command: ${command}`);
    });

    renderDesktop();
    expect(await screen.findByText(label, { exact: true })).toBeInTheDocument();
    expect(screen.queryByText("ONLINE", { exact: true })).toBeNull();
    expect(screen.queryByText("OFFLINE", { exact: true })).toBeNull();
    expect(screen.getAllByText("Unavailable", { exact: true }).length).toBeGreaterThan(0);
  });

  it.each(["RUNNING", "READY"] as const)(
    "uses only the high-level takeover command for a valid %s legacy task",
    async (taskState) => {
      let current = snapshot("WORKER", {
        workerOwnership: "TAKEOVER_REQUIRED",
        legacyTaskState: taskState,
      });
      native.invoke.mockImplementation(async (command: string) => {
        if (command === "get_desktop_snapshot") return current;
        if (command === "operator_current") return operatorSession("OPERATOR");
        if (command === "local_worker_drain_status") {
          return {
            workerId: "12345678-1234-4234-8234-123456789abc",
            status: "ONLINE",
            activeBrowserSessions: 0,
            runningWorkerJobs: 0,
            quiescent: false,
          };
        }
        if (command === "takeover_local_worker") {
          current = snapshot("WORKER", {
            workerOwnership: "DESKTOP",
            legacyTaskState: "DISABLED",
          });
          return undefined;
        }
        throw new Error(`unexpected command: ${command}`);
      });

      renderDesktop();
      fireEvent.click(await screen.findByRole("button", { name: "Take over existing Worker" }));
      await waitFor(() => expect(native.invoke).toHaveBeenCalledWith("takeover_local_worker"));
      await waitFor(() =>
        expect(screen.getByText("DISABLED", { exact: true })).toBeInTheDocument(),
      );
      expect(native.invoke).not.toHaveBeenCalledWith("request_local_worker_drain");
      expect(native.invoke.mock.calls.map(([command]) => command)).not.toContain(
        "force_stop_worker",
      );
    },
  );

  it("uses only the high-level rollback command for Desktop ownership", async () => {
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot") {
        return snapshot("WORKER", {
          workerOwnership: "DESKTOP",
          legacyTaskState: "DISABLED",
        });
      }
      if (command === "operator_current") return operatorSession("ADMIN");
      if (command === "operator_list_users") return [];
      if (command === "local_worker_drain_status") {
        return {
          workerId: "12345678-1234-4234-8234-123456789abc",
          status: "ONLINE",
          activeBrowserSessions: 0,
          runningWorkerJobs: 0,
          quiescent: false,
        };
      }
      if (command === "rollback_local_worker_to_legacy") return undefined;
      throw new Error(`unexpected command: ${command}`);
    });

    renderDesktop();
    fireEvent.click(await screen.findByRole("button", { name: "Restore legacy Worker host" }));
    await waitFor(() =>
      expect(native.invoke).toHaveBeenCalledWith("rollback_local_worker_to_legacy"),
    );
    expect(native.invoke).not.toHaveBeenCalledWith("request_local_worker_drain");
    expect(native.invoke.mock.calls.map(([command]) => command)).not.toContain("force_stop_worker");
  });

  it.each([
    {
      action: "takeover" as const,
      command: "takeover_local_worker",
      ownership: "TAKEOVER_REQUIRED" as const,
      task: "RUNNING" as const,
      failure: "worker_cutover_rollback_required",
    },
    {
      action: "rollback" as const,
      command: "rollback_local_worker_to_legacy",
      ownership: "DESKTOP" as const,
      task: "DISABLED" as const,
      failure: "worker_rollback_failed",
    },
  ])(
    "refreshes the Worker snapshot and preserves $action failure diagnostics",
    async (scenario) => {
      let current = snapshot("WORKER", {
        workerOwnership: scenario.ownership,
        legacyTaskState: scenario.task,
      });
      native.invoke.mockImplementation(async (command: string) => {
        if (command === "get_desktop_snapshot") return current;
        if (command === "operator_current") return operatorSession("OWNER");
        if (command === "operator_list_users") return [];
        if (command === "local_worker_drain_status") {
          return {
            workerId: "12345678-1234-4234-8234-123456789abc",
            status: "ONLINE",
            activeBrowserSessions: 0,
            runningWorkerJobs: 0,
            quiescent: false,
          };
        }
        if (command === scenario.command) {
          current = snapshot("WORKER", {
            workerOwnership: "BLOCKED",
            legacyTaskState: scenario.task,
            diagnosticCode: scenario.failure,
          });
          throw scenario.failure;
        }
        throw new Error(`unexpected command: ${command}`);
      });

      renderDesktop();
      const button =
        scenario.action === "takeover" ? "Take over existing Worker" : "Restore legacy Worker host";
      fireEvent.click(await screen.findByRole("button", { name: button }));

      expect(
        await screen.findByText(`Worker action failed. Diagnostic code: ${scenario.failure}`),
      ).toBeInTheDocument();
      await waitFor(() =>
        expect(screen.getByText(`Diagnostic code: ${scenario.failure}`)).toBeInTheDocument(),
      );
      expect(native.invoke).toHaveBeenCalledWith("get_desktop_snapshot");
      expect(native.invoke).not.toHaveBeenCalledWith("request_local_worker_drain");
    },
  );

  it.each([
    ["BLOCKED", "RUNNING"],
    ["TAKEOVER_REQUIRED", "INVALID"],
    ["TAKEOVER_REQUIRED", "NOT_REGISTERED"],
    ["LEGACY", "RUNNING"],
  ] as const)("offers no speculative action for %s / %s", async (ownership, taskState) => {
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot") {
        return snapshot("WORKER", { workerOwnership: ownership, legacyTaskState: taskState });
      }
      if (command === "operator_current") return operatorSession("OWNER");
      if (command === "operator_list_users") return [];
      if (command === "local_worker_drain_status") {
        return {
          workerId: "12345678-1234-4234-8234-123456789abc",
          status: "ONLINE",
          activeBrowserSessions: 0,
          runningWorkerJobs: 0,
          quiescent: false,
        };
      }
      throw new Error(`unexpected command: ${command}`);
    });

    renderDesktop();
    await screen.findByRole("heading", { name: "Your Worker" });
    expect(screen.queryByRole("button", { name: "Take over existing Worker" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Restore legacy Worker host" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Force stop Worker/ })).toBeNull();
  });

  it("gives OPERATOR lifecycle controls but withholds them from VIEWER and password-change sessions", async () => {
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot") {
        return snapshot("WORKER", {
          workerOwnership: "DESKTOP",
          legacyTaskState: "DISABLED",
        });
      }
      if (command === "operator_current") return operatorSession("OPERATOR");
      if (command === "local_worker_drain_status") {
        return {
          workerId: "12345678-1234-4234-8234-123456789abc",
          status: "ONLINE",
          activeBrowserSessions: 0,
          runningWorkerJobs: 0,
          quiescent: false,
        };
      }
      throw new Error(`unexpected command: ${command}`);
    });

    renderDesktop();
    expect(await screen.findByRole("button", { name: "Restart…" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Quit…" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restore legacy Worker host" })).toBeInTheDocument();
  });

  it.each([
    ["VIEWER", false],
    ["OWNER", true],
  ] as const)(
    "withholds Worker controls for role %s with mustChange=%s",
    async (role, mustChange) => {
      native.invoke.mockImplementation(async (command: string) => {
        if (command === "get_desktop_snapshot") {
          return snapshot("WORKER", {
            workerOwnership: "DESKTOP",
            legacyTaskState: "DISABLED",
            diagnosticCode: "worker_drain_timeout",
          });
        }
        if (command === "operator_current") return operatorSession(role, mustChange);
        if (command === "operator_list_users") return [];
        throw new Error(`unexpected command: ${command}`);
      });

      renderDesktop();
      await screen.findByRole("heading", { name: "Your Worker" });
      expect(screen.queryByRole("button", { name: "Restart…" })).toBeNull();
      expect(screen.queryByRole("button", { name: "Quit…" })).toBeNull();
      expect(screen.queryByRole("button", { name: "Decommission device" })).toBeNull();
      expect(screen.queryByRole("button", { name: "Restore legacy Worker host" })).toBeNull();
      expect(screen.queryByRole("button", { name: /Force stop Worker/ })).toBeNull();
      expect(native.invoke).not.toHaveBeenCalledWith("local_worker_drain_status");
    },
  );

  it("keeps graceful Quit fail-closed, preserves the Operator session, and never forces", async () => {
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot") {
        return snapshot("WORKER", { workerOwnership: "DESKTOP", legacyTaskState: "DISABLED" });
      }
      if (command === "operator_current") return operatorSession("OWNER");
      if (command === "operator_list_users") return [];
      if (command === "local_worker_drain_status") {
        return {
          workerId: "12345678-1234-4234-8234-123456789abc",
          status: "ONLINE",
          activeBrowserSessions: 0,
          runningWorkerJobs: 0,
          quiescent: false,
        };
      }
      if (command === "request_quit") throw "worker_drain_timeout";
      throw new Error(`unexpected command: ${command}`);
    });

    renderDesktop();
    fireEvent.click(await screen.findByRole("button", { name: "Quit…" }));
    expect(
      await screen.findByText(/graceful Worker drain.*authoritative OFFLINE/i),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Gracefully drain and quit" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("worker_drain_timeout");
    expect(screen.getByText("Signed in as worker-operator")).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Quit Threads Desktop?" })).toBeInTheDocument();
    expect(native.invoke.mock.calls.map(([command]) => command)).not.toContain("force_stop_worker");
    expect(native.invoke.mock.calls.map(([command]) => command)).not.toContain("operator_logout");
  });

  it("quits Desktop without starting or draining a READY legacy Worker", async () => {
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot") {
        return snapshot("WORKER", {
          workerOwnership: "TAKEOVER_REQUIRED",
          legacyTaskState: "READY",
          processId: null,
        });
      }
      if (command === "operator_current") return operatorSession("OPERATOR");
      if (command === "local_worker_drain_status") {
        return {
          workerId: "12345678-1234-4234-8234-123456789abc",
          status: "OFFLINE",
          activeBrowserSessions: 0,
          runningWorkerJobs: 0,
          quiescent: false,
        };
      }
      if (command === "request_quit") return undefined;
      throw new Error(`unexpected command: ${command}`);
    });

    renderDesktop();
    fireEvent.click(await screen.findByRole("button", { name: "Quit…" }));
    expect(
      await screen.findByText(/task remains enabled and ready.*does not start the Worker/i),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Quit Desktop" }));

    await waitFor(() => expect(native.invoke).toHaveBeenCalledWith("request_quit"));
    expect(native.invoke).not.toHaveBeenCalledWith("request_local_worker_drain");
    expect(native.invoke.mock.calls.map(([command]) => command)).not.toContain("force_stop_worker");
  });

  it("describes Worker Restart as drain-to-ONLINE and keeps the session on failure", async () => {
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot") {
        return snapshot("WORKER", { workerOwnership: "DESKTOP", legacyTaskState: "DISABLED" });
      }
      if (command === "operator_current") return operatorSession("OPERATOR");
      if (command === "local_worker_drain_status") {
        return {
          workerId: "12345678-1234-4234-8234-123456789abc",
          status: "ONLINE",
          activeBrowserSessions: 0,
          runningWorkerJobs: 0,
          quiescent: false,
        };
      }
      if (command === "request_restart") throw "worker_cutover_rollback_required";
      throw new Error(`unexpected command: ${command}`);
    });

    renderDesktop();
    fireEvent.click(await screen.findByRole("button", { name: "Restart…" }));
    expect(
      await screen.findByText(
        /drains to authoritative OFFLINE, waits for clean exit, restarts the same enrolled Worker and waits for ONLINE/i,
      ),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Authenticate and restart" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("worker_cutover_rollback_required");
    expect(screen.getByText("Signed in as worker-operator")).toBeInTheDocument();
    expect(native.invoke.mock.calls.map(([command]) => command)).not.toContain("operator_logout");
    expect(native.invoke.mock.calls.map(([command]) => command)).not.toContain("force_stop_worker");
  });

  it("requires exact force confirmation and presents interruption as abnormal", async () => {
    let current = snapshot("WORKER", {
      workerOwnership: "DESKTOP",
      legacyTaskState: "DISABLED",
      state: "degraded",
      diagnosticCode: "worker_drain_timeout",
    });
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot") return current;
      if (command === "operator_current") return operatorSession("OPERATOR");
      if (command === "local_worker_drain_status") {
        return {
          workerId: "12345678-1234-4234-8234-123456789abc",
          status: "DRAINING",
          activeBrowserSessions: 1,
          runningWorkerJobs: 1,
          quiescent: false,
        };
      }
      if (command === "force_stop_worker") {
        current = snapshot("WORKER", {
          workerOwnership: "DESKTOP",
          legacyTaskState: "DISABLED",
          state: "degraded",
          diagnosticCode: "worker_forced_interruption",
        });
        return current;
      }
      throw new Error(`unexpected command: ${command}`);
    });

    renderDesktop();
    fireEvent.click(await screen.findByRole("button", { name: "Force stop Worker (abnormal)…" }));
    expect(
      await screen.findByText(/not graceful, does not prove server OFFLINE/i),
    ).toBeInTheDocument();
    const forceDialog = screen.getByRole("dialog", { name: "Force stop Worker?" });
    const forceButton = screen.getByRole("button", { name: "Force stop Worker" });
    expect(forceButton).toBeDisabled();
    fireEvent.change(within(forceDialog).getByRole("textbox"), {
      target: { value: "force stop worker" },
    });
    expect(forceButton).toBeDisabled();
    fireEvent.change(within(forceDialog).getByRole("textbox"), {
      target: { value: "FORCE STOP WORKER" },
    });
    fireEvent.click(forceButton);

    await waitFor(() =>
      expect(native.invoke).toHaveBeenCalledWith("force_stop_worker", {
        confirmation: "FORCE STOP WORKER",
      }),
    );
    expect(screen.queryByRole("dialog", { name: "Force stop Worker?" })).toBeNull();
    expect(
      await screen.findByText("Diagnostic code: worker_forced_interruption"),
    ).toBeInTheDocument();
    expect(native.invoke).not.toHaveBeenCalledWith("request_local_worker_drain");
    const lifecycleMutations = native.invoke.mock.calls
      .map(([command]) => command)
      .filter((command) =>
        [
          "takeover_local_worker",
          "rollback_local_worker_to_legacy",
          "request_quit",
          "request_restart",
          "decommission_device",
          "force_stop_worker",
        ].includes(command as string),
      );
    expect(lifecycleMutations).toEqual(["force_stop_worker"]);
  });

  it("explains Worker decommission restoration and durable data preservation", async () => {
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot") {
        return snapshot("WORKER", {
          workerOwnership: "DESKTOP",
          legacyTaskState: "DISABLED",
        });
      }
      if (command === "operator_current") return operatorSession("ADMIN");
      if (command === "operator_list_users") return [];
      if (command === "local_worker_drain_status") {
        return {
          workerId: "12345678-1234-4234-8234-123456789abc",
          status: "ONLINE",
          activeBrowserSessions: 0,
          runningWorkerJobs: 0,
          quiescent: false,
        };
      }
      throw new Error(`unexpected command: ${command}`);
    });

    renderDesktop();
    fireEvent.click(await screen.findByRole("button", { name: "Decommission device" }));
    const dialog = await screen.findByRole("dialog", { name: "Decommission this device?" });
    expect(
      within(dialog).getByText(/preserved legacy Task Scheduler host is restored/i),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByText(
        /does not delete the Worker identity, journal, or browser profiles/i,
      ),
    ).toBeInTheDocument();
    expect(within(dialog).getByText("RESET THIS DEVICE", { exact: true })).toBeInTheDocument();
  });

  it("does not mutate Worker lifecycle when the Operator signs out", async () => {
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot") {
        return snapshot("WORKER", { workerOwnership: "DESKTOP", legacyTaskState: "DISABLED" });
      }
      if (command === "operator_current") return operatorSession("OWNER");
      if (command === "operator_list_users") return [];
      if (command === "local_worker_drain_status") {
        return {
          workerId: "12345678-1234-4234-8234-123456789abc",
          status: "ONLINE",
          activeBrowserSessions: 0,
          runningWorkerJobs: 0,
          quiescent: false,
        };
      }
      if (command === "operator_logout") return undefined;
      throw new Error(`unexpected command: ${command}`);
    });

    renderDesktop();
    fireEvent.click(await screen.findByRole("button", { name: "Sign out" }));
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();

    const mutations = native.invoke.mock.calls
      .map(([command]) => command)
      .filter((command) =>
        [
          "takeover_local_worker",
          "rollback_local_worker_to_legacy",
          "request_quit",
          "request_restart",
          "decommission_device",
          "force_stop_worker",
        ].includes(command as string),
      );
    expect(mutations).toEqual([]);
    expect(native.invoke).toHaveBeenCalledWith("operator_logout");
  });

  it("does not mutate Worker lifecycle on native lock, hide, focus loss, or reopen", async () => {
    const listeners: Record<string, (event: unknown) => void> = {};
    native.listen.mockImplementation(async (event: string, handler: (event: unknown) => void) => {
      listeners[event] = handler;
      return () => undefined;
    });
    let currentCalls = 0;
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot") {
        return snapshot("WORKER", { workerOwnership: "DESKTOP", legacyTaskState: "DISABLED" });
      }
      if (command === "operator_current") {
        currentCalls += 1;
        return currentCalls === 1 ? operatorSession("OWNER") : null;
      }
      if (command === "operator_list_users") return [];
      if (command === "local_worker_drain_status") {
        return {
          workerId: "12345678-1234-4234-8234-123456789abc",
          status: "ONLINE",
          activeBrowserSessions: 0,
          runningWorkerJobs: 0,
          quiescent: false,
        };
      }
      if (command === "operator_lock") return undefined;
      throw new Error(`unexpected command: ${command}`);
    });

    renderDesktop();
    await screen.findByText("Signed in as worker-operator");
    await waitFor(() => expect(listeners["desktop://session-locked"]).toBeDefined());
    fireEvent.blur(window);
    fireHiddenVisibilityChange();
    fireEvent.focus(window);
    expect(screen.getByText("Signed in as worker-operator")).toBeInTheDocument();
    act(() => listeners["desktop://session-locked"]({}));
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();

    const mutations = native.invoke.mock.calls
      .map(([command]) => command)
      .filter((command) =>
        [
          "takeover_local_worker",
          "rollback_local_worker_to_legacy",
          "request_quit",
          "request_restart",
          "decommission_device",
          "force_stop_worker",
        ].includes(command as string),
      );
    expect(mutations).toEqual([]);
  });
});
