import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../src/App";
import { DESKTOP_COMMANDS, listenForTrayQuit, type DesktopSnapshot } from "../src/desktop";
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
    ]);
  });

  it("provisions one role and shows the native helper status", async () => {
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "get_desktop_snapshot") return snapshot();
      if (command === "provision_role") return snapshot("CONTROLLER");
      throw new Error("unknown command");
    });

    renderDesktop();
    await screen.findByRole("heading", { name: "Choose this PC’s role" });
    fireEvent.click(screen.getByRole("button", { name: "Provision as Controller" }));

    await screen.findByRole("heading", { name: "Your Controller" });
    expect(await screen.findByText("PID 4242")).toBeInTheDocument();
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

  it.each([
    { role: "CONTROLLER" as const, heading: "Your Controller" },
    { role: "WORKER" as const, heading: "Your Worker" },
  ])(
    "keeps the $role helper running when the native session relocks",
    async ({ role, heading }) => {
      const listeners: Record<string, (event: unknown) => void> = {};
      native.listen.mockImplementation(async (event: string, handler: (event: unknown) => void) => {
        listeners[event] = handler;
        return () => undefined;
      });
      native.invoke.mockImplementation(async (command: string) => {
        if (command === "get_desktop_snapshot") return snapshot(role);
        throw new Error(`unexpected native command: ${command}`);
      });

      renderDesktop();
      await screen.findByRole("heading", { name: heading });
      await waitFor(() => expect(listeners["desktop://session-locked"]).toBeDefined());
      act(() => listeners["desktop://session-locked"]({}));

      expect(screen.getByText("PID 4242")).toBeInTheDocument();
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

  it("routes the native session-lock event to the gate and stays locked after tray reopen", async () => {
    const listeners: Record<string, (event: unknown) => void> = {};
    native.listen.mockImplementation(async (event: string, handler: (event: unknown) => void) => {
      listeners[event] = handler;
      return () => undefined;
    });
    const { unmount } = render(
      <>
        <p>Controller helper running</p>
        <SessionGate validSession onUnlock={async () => false}>
          <p>private account data</p>
        </SessionGate>
      </>,
    );
    const unlisten = await listenForTrayQuit();

    expect(screen.getByText("private account data")).toBeInTheDocument();
    act(() => listeners["desktop://session-locked"]({}));
    expect(screen.queryByText("private account data")).not.toBeInTheDocument();
    expect(screen.getByText("Session locked")).toBeInTheDocument();

    fireEvent.focus(window);
    expect(screen.queryByText("private account data")).not.toBeInTheDocument();
    expect(screen.getByText("Controller helper running")).toBeInTheDocument();
    expect(native.invoke).not.toHaveBeenCalled();

    unlisten();
    unmount();
  });

  it("locks on the configured inactivity timeout", async () => {
    vi.useFakeTimers();
    render(
      <SessionGate validSession idleTimeoutMs={1_000} onUnlock={async () => false}>
        <p>private account data</p>
      </SessionGate>,
    );

    expect(screen.getByText("private account data")).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(screen.queryByText("private account data")).not.toBeInTheDocument();
    expect(screen.getByText("Session locked")).toBeInTheDocument();
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
