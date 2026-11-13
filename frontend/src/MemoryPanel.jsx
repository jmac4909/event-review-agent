import { useEffect, useState } from "react";
import { formatTime } from "./format.js";
import GuidanceCard from "./GuidanceCard.jsx";

const emptyLabels = {
  proposed: "suggested",
  approved: "approved",
  history: "retired or rejected",
};

export default function MemoryPanel({ api, onEvent, active = true, target }) {
  const [data, setData] = useState({ corrections: [], guidance: [] });
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const [tab, setTab] = useState("proposed");
  useEffect(() => {
    if (target) setTab(["proposed", "approved"].includes(target.status) ? target.status : "history");
  }, [target]);

  useEffect(() => {
    if (!active || !target?.id) return;
    const card = document.querySelector(`[data-testid="guidance-${target.id}"]`);
    card?.scrollIntoView({ block: "nearest" });
  }, [active, target, tab, data.guidance.length]);

  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    setLoading(true);
    async function loadMemory() {
      try {
        const result = await api("/memory", { signal: controller.signal });
        setData(result);
        setError("");
      } catch (failure) {
        if (failure.name !== "AbortError") setError(failure.message);
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }
    loadMemory();
    return () => controller.abort();
  }, [active, refresh]);

  useEffect(() => {
    if (!active || loading) return;
    const timer = setTimeout(() => setRefresh((value) => value + 1), 3000);
    return () => clearTimeout(timer);
  }, [active, loading, refresh]);

  const visible = data.guidance.filter((item) => {
    if (tab === "history") return !["proposed", "approved"].includes(item.status);
    return item.status === tab;
  });

  function refreshMemory() {
    setRefresh((value) => value + 1);
  }

  return <section className="memory-panel" aria-label="Regional memory">
    <div className="page-heading"><div><h1>Memory & guidance</h1>
      <p>Approve guidance for future reviews.</p></div>
      <button className="secondary-button" disabled={loading} onClick={refreshMemory}>Refresh memory</button>
    </div>
    <div className="memory-tabs" aria-label="Guidance categories">
      <button aria-pressed={tab === "proposed"} onClick={() => setTab("proposed")}>Suggested ({data.guidance.filter((item) => item.status === "proposed").length})</button>
      <button aria-pressed={tab === "approved"} onClick={() => setTab("approved")}>Approved ({data.guidance.filter((item) => item.status === "approved").length})</button>
      <button aria-pressed={tab === "history"} onClick={() => setTab("history")}>History</button>
    </div>
    {error && <p role="alert" className="error-banner">{error}</p>}
    {loading && !data.guidance.length && <p role="status">Loading memory…</p>}
    {!loading && !visible.length && <p className="memory-empty">No {emptyLabels[tab]} guidance yet.</p>}
    <div className="guidance-list">{visible.map((item) => <GuidanceCard key={item.id} api={api} item={item} onChanged={refreshMemory} onEvent={onEvent} />)}</div>
    <details className="memory-corrections"><summary>Case correction history ({data.corrections.length})</summary>
      <p className="review-note">Latest 100 corrections.</p>
      {data.corrections.map((item) => <article className="case-correction" key={item.id}>
        <strong>{item.decision}{item.severity ? ` / ${item.severity}` : ""} · version {item.version}</strong>
        <p>{item.reason}</p><small>{item.supervisor_id} · {formatTime(item.created_at)} UTC</small>
        <button className="text-button" onClick={() => onEvent(item.event_id)}>Open event</button>
      </article>)}
    </details>
  </section>;
}
