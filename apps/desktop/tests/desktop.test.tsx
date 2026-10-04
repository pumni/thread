import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../src/App";
import { DESKTOP_COMMANDS, operatorLock, type DesktopSnapshot } from "../src/desktop";
import { SessionGate } from "../src/SessionGate";

const native = vi.hoisted(() => ({
  invoke: vi.fn(),
  listen: vi.fn(),
}));

vi.mock("@tauri-apps/api/core", () => ({ invoke: native.invoke }));
vi.mock("@tauri-apps/api/event", () => ({ listen: native.listen }));

function snapshot(role: DesktopSnapshot["role"] = null): DesktopSnapshot {
  return {
    schemaVersion: 1,
    role,
    theme: "SYSTEM",
    autostartEnabled: role !== null,
    supervisor: {
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
      workerId: null,
    },
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
      "operator_login",
      "operator_bootstrap_owner",
      "operator_current",
      "operator_logout",
      "operator_lock",
      "operator_list_users",
      "operator_create_user",
      "operator_update_user",
      "operator_change_password",
      "request_local_worker_drain",
      "local_worker_drain_status",
      "controller_https_configure",
      "controller_https_reconfigure",
      "controller_https_summary",
      "controller_trust_probe",
      "controller_trust_confirm",
      "controller_trust_summary",
    ]);
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
      await screen.findByText("Console mode is client-only and starts no local helper process."),
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
      if (command === "decommission_device") return snapshot();
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

  it("keeps a logged-out gate locked after tray focus without stopping the helper", () => {
    const onUnlock = vi.fn().mockResolvedValue(false);
    const { rerender } = render(
      <>
        <p>Worker helper running</p>
        <SessionGate validSession onUnlock={onUnlock}>
          <p>private account data</p>
        </SessionGate>
      </>,
    );
    expect(screen.getByText("private account data")).toBeInTheDocument();

    rerender(
      <>
        <p>Worker helper running</p>
        <SessionGate validSession={false} onUnlock={onUnlock}>
          <p>private account data</p>
        </SessionGate>
      </>,
    );
    expect(screen.queryByText("private account data")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Re-authenticate" })).not.toBeInTheDocument();

    rerender(
      <>
        <p>Worker helper running</p>
        <SessionGate validSession onUnlock={onUnlock}>
          <p>private account data</p>
        </SessionGate>
      </>,
    );
    fireEvent.focus(window);
    expect(screen.queryByText("private account data")).not.toBeInTheDocument();
    expect(screen.getByText("Session locked")).toBeInTheDocument();
    expect(screen.getByText("Worker helper running")).toBeInTheDocument();
    expect(onUnlock).not.toHaveBeenCalled();
    expect(native.invoke).not.toHaveBeenCalled();
  });
});
