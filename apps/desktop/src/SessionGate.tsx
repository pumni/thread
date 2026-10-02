import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

interface SessionGateProps {
  validSession: boolean;
  idleTimeoutMs?: number;
  onUnlock: () => Promise<boolean>;
  children: ReactNode;
}

export function SessionGate({ validSession, idleTimeoutMs, onUnlock, children }: SessionGateProps) {
  const [locked, setLocked] = useState(!validSession);
  const lockedRef = useRef(locked);
  const lastActivityAt = useRef(Date.now());

  useEffect(() => {
    lockedRef.current = locked;
  }, [locked]);

  useEffect(() => {
    if (!validSession) setLocked(true);

    const lock = () => setLocked(true);
    const recordActivity = () => {
      if (!lockedRef.current) lastActivityAt.current = Date.now();
    };
    const onVisibilityChange = () => {
      if (document.visibilityState !== "visible") lock();
    };

    window.addEventListener("blur", lock);
    window.addEventListener("pointerdown", recordActivity);
    window.addEventListener("keydown", recordActivity);
    window.addEventListener("threads-desktop:session-locked", lock);
    document.addEventListener("visibilitychange", onVisibilityChange);

    const timer =
      idleTimeoutMs === undefined
        ? undefined
        : window.setInterval(
            () => {
              if (Date.now() - lastActivityAt.current >= idleTimeoutMs) lock();
            },
            Math.max(250, Math.min(idleTimeoutMs, 1_000)),
          );

    return () => {
      window.removeEventListener("blur", lock);
      window.removeEventListener("pointerdown", recordActivity);
      window.removeEventListener("keydown", recordActivity);
      window.removeEventListener("threads-desktop:session-locked", lock);
      document.removeEventListener("visibilitychange", onVisibilityChange);
      if (timer !== undefined) window.clearInterval(timer);
    };
  }, [idleTimeoutMs, validSession]);

  async function unlock() {
    if (!validSession) return;
    const authenticated = await onUnlock();
    if (authenticated) {
      lastActivityAt.current = Date.now();
      setLocked(false);
    }
  }

  if (!validSession || locked) {
    return (
      <section className="session-lock" role="status" aria-live="polite">
        <span className="session-lock-icon" aria-hidden="true">
          ⌑
        </span>
        <div>
          <strong>Session locked</strong>
          <p>Authenticate again to view protected Operator data.</p>
        </div>
        {validSession && (
          <button
            type="button"
            className="button button-secondary button-small"
            onClick={() => void unlock()}
          >
            Re-authenticate
          </button>
        )}
      </section>
    );
  }

  return children;
}
