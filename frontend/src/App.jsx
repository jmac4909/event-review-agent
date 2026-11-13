import { useEffect, useRef, useState } from "react";
import { api, supervisorApi } from "./api.js";
import Brand from "./Brand.jsx";
import Login from "./Login.jsx";
import Workspace from "./Workspace.jsx";

export default function App() {
  const [accounts, setAccounts] = useState([]);
  const [accessRequired, setAccessRequired] = useState(false);
  const [session, setSession] = useState(null);
  const sessionClient = useRef(null);
  // Ignore sign-in/bootstrap replies that started before the session changed.
  const sessionRevision = useRef(0);
  const [initializing, setInitializing] = useState(true);
  const [sessionError, setSessionError] = useState("");
  const [busy, setBusy] = useState(false);

  function clearSession(message = "") {
    sessionRevision.current += 1;
    sessionClient.current?.close();
    sessionClient.current = null;
    setSession(null);
    setSessionError(message);
    setBusy(false);
  }

  function openSession(user) {
    sessionClient.current?.close();
    const bound = supervisorApi(user.id, (message) => {
      if (sessionClient.current === bound) clearSession(message);
    });
    sessionClient.current = bound;
    setSession({ user, api: bound.request });
  }

  function notifyOtherTabs() {
    try {
      localStorage.setItem("event-review-session-change", crypto.randomUUID());
    } catch { /* The server precondition still protects tabs without storage. */ }
  }

  useEffect(() => {
    function changed(event) {
      if (event.key === "event-review-session-change")
        clearSession("Your session changed in another tab. Choose a region again.");
    }
    window.addEventListener("storage", changed);
    return () => {
      window.removeEventListener("storage", changed);
      sessionClient.current?.close();
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const requestRevision = sessionRevision.current;
    Promise.all([
      api("/demo-accounts", { signal: controller.signal }),
      api("/session", { signal: controller.signal }).catch((error) => {
        if (error.status === 401) return null;
        throw error;
      }),
      api("/access", { signal: controller.signal }),
    ])
      .then(([available, actor, access]) => {
        setAccounts(available);
        setAccessRequired(access.required);
        if (actor && sessionRevision.current === requestRevision) openSession(actor);
      })
      .catch((error) => {
        if (error.name !== "AbortError")
          setSessionError(
            "The workspace could not be reached. Refresh the page to retry.",
          );
      })
      .finally(() => {
        if (!controller.signal.aborted) setInitializing(false);
      });
    return () => controller.abort();
  }, []);

  async function login(accountId, accessCode) {
    const requestRevision = ++sessionRevision.current;
    setBusy(true);
    setSessionError("");
    try {
      const actor = await api("/session", {
        method: "POST",
        body: JSON.stringify({ account_id: accountId, access_code: accessCode }),
      });
      if (sessionRevision.current === requestRevision) {
        openSession(actor);
        notifyOtherTabs();
      }
    } catch (error) {
      if (sessionRevision.current === requestRevision) setSessionError(error.message || "Sign-in failed. Please retry.");
    } finally {
      if (sessionRevision.current === requestRevision) setBusy(false);
    }
  }

  async function logout() {
    const current = sessionClient.current;
    setBusy(true);
    setSessionError("");
    try {
      await current.request("/session", { method: "DELETE" });
      if (sessionClient.current === current) {
        clearSession();
        notifyOtherTabs();
      }
    } catch (error) {
      if (sessionClient.current === current) setSessionError(error.message || "Sign-out failed. Please retry.");
    } finally {
      setBusy(false);
    }
  }

  if (initializing)
    return (
      <main className="boot" role="status">
        <Brand />
        <span className="spinner" />
        Opening workspace…
      </main>
    );
  if (!session)
    return (
      <Login
        accounts={accounts}
        onLogin={login}
        busy={busy}
        error={sessionError}
        accessRequired={accessRequired}
      />
    );

  return (
    // An account change creates a fresh workspace, including its filters and drafts.
    <Workspace
      key={session.user.id}
      user={session.user}
      api={session.api}
      onLogout={logout}
      busy={busy}
      sessionError={sessionError}
    />
  );
}
