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
      endpoint: role === "CONTROLLER" ? "http://127.0.0.1:4246" : null,
      databasePort: role === "CONTROLLER" ? 4247 : null,
      diagnosticCode: null,
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
    ]);
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
    expect(screen.getByText("http://127.0.0.1:4246")).toBeInTheDocument();
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

  it("keeps the Operator bearer inside Rust during first-Owner login", async () => {
    const bearer = "SYNTHETIC_OPERATOR_BEARER_NEVER_RENDERED";
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot") return snapshot("CONTROLLER");
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

  it("locks protected information when the native window loses focus", async () => {
    const onUnlock = vi.fn().mockResolvedValue(false);
    render(
      <SessionGate validSession onUnlock={onUnlock}>
        <p>private account data</p>
      </SessionGate>,
    );
    expect(screen.getByText("private account data")).toBeInTheDocument();
    fireEvent.blur(window);
    expect(screen.queryByText("private account data")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Re-authenticate" }));
    await waitFor(() => expect(onUnlock).toHaveBeenCalledOnce());
    expect(screen.getByText("Session locked")).toBeInTheDocument();
  });

  it.each([
    { role: "CONTROLLER" as const, process: "PID 4243" },
    { role: "WORKER" as const, process: "PID 4242" },
  ])(
    "uses operator_lock once on $role blur without stopping its helper",
    async ({ role, process }) => {
      native.invoke.mockImplementation(async (command: string) => {
        if (command === "get_desktop_snapshot") return snapshot(role);
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
        if (command === "operator_lock") return undefined;
        throw new Error(`unexpected native command: ${command}`);
      });

      renderDesktop();
      await screen.findByText("Signed in as first-owner");
      fireEvent.blur(window);

      expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
      expect(screen.queryByText("Signed in as first-owner")).not.toBeInTheDocument();
      expect(screen.getByText(process)).toBeInTheDocument();
      expect(
        native.invoke.mock.calls.filter(([command]) => command === "operator_lock"),
      ).toHaveLength(1);
      expect(native.invoke.mock.calls.map(([command]) => command)).not.toContain("operator_logout");
    },
  );

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
