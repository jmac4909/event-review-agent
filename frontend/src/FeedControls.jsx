import { useEffect, useRef, useState } from "react";
import { formatTime } from "./format.js";
import { reviewerLabels } from "./labels.js";

const labels = { paused: "Paused", running: "Running", auto_stopped: "Time limit reached", reviewer_changed: "Reviewer changed",
  reviewer_unavailable: "Model unavailable",
  waiting_for_reviews: "Waiting for reviews", waiting_for_worker: "Waiting for worker" };
const isRunning = (feed) => !["paused", "auto_stopped", "reviewer_changed", "reviewer_unavailable"].includes(feed.status);

export default function FeedControls({ api, refresh, onChanged, onEvent }) {
  const [feed, setFeed] = useState(null);
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [actionError, setActionError] = useState("");
  const [modelDraft, setModelDraft] = useState(null);
  const [runDuration, setRunDuration] = useState(null);
  const pending = useRef(null);

  function received(current) {
    setFeed(current);
    const intent = pending.current;
    // A read can confirm a committed change after its response was lost.
    if (!intent) return;
    let applied;
    if (intent.kind === "reviewer") {
      applied = current.reviewer === intent.reviewer && !isRunning(current);
    } else if (intent.running) {
      applied = isRunning(current) && current.reviewer === intent.reviewer
        && current.run_seconds === intent.run_seconds;
    } else {
      applied = !isRunning(current);
    }
    if (current.version === intent.expected_version + 1 && applied) {
      pending.current = null;
      setActionError("");
      if (intent.kind === "reviewer") setModelDraft(null);
      else setRunDuration(null);
    }
  }

  useEffect(() => {
    if (busy) return;
    const controller = new AbortController();
    api("/feed", { signal: controller.signal }).then((current) => {
      received(current);
      setLoadError("");
    }).catch((error) => {
      if (error.name !== "AbortError") setLoadError(error.message);
    });
    return () => controller.abort();
  }, [refresh, busy]);

  async function change() {
    const duration = runDuration ?? (feed.run_seconds === null ? "none" : String(feed.run_seconds));
    const body = { running: !isRunning(feed), expected_version: feed.version, reviewer: feed.reviewer,
      run_seconds: duration === "none" ? null : Number(duration) };
    pending.current = { ...body, kind: "stream" };
    setBusy(true);
    setActionError("");
    try {
      received(await api("/feed", { method: "PUT", body: JSON.stringify(body) }));
      pending.current = null;
      setRunDuration(null);
    } catch (error) {
      setActionError(error.message);
    } finally {
      setBusy(false);
      onChanged();
    }
  }

  async function changeModel(event) {
    event.preventDefault();
    pending.current = { ...modelDraft, kind: "reviewer" };
    setBusy(true);
    setActionError("");
    try {
      received(await api("/reviewer", { method: "PUT", body: JSON.stringify(modelDraft) }));
      pending.current = null;
      setModelDraft(null);
    } catch (error) {
      setActionError(error.message);
    } finally {
      setBusy(false);
      onChanged();
    }
  }

  return <section className="stream-controls" aria-label="Demo event stream">
    <details className="review-settings">
    <summary><span><strong>{feed ? `New reviews: ${reviewerLabels[feed.reviewer]}` : "Loading review settings…"}</strong>
      <small>{feed ? `Stream · ${labels[feed.status]}` : ""}</small></span><span className="settings-label">Review settings</span></summary>
    <div className="settings-content">
    {feed?.model_selection === "global" && <section className="reviewer-controls" aria-label="Review model">
      <div><h2>Review model</h2>
        <p>{reviewerLabels[feed.reviewer]} for all regions.</p>
        <p>Set by the server for new reviews and suggestions.</p>
      </div>
    </section>}
    {feed?.model_selection === "regional" && <form className="reviewer-controls" aria-label="Review model" onSubmit={changeModel}>
      <div><h2>Review model</h2>
        <p>Applies to new reviews and suggestions.</p>
      </div>
      <div className="reviewer-selection">
        <label>Model
          <select value={modelDraft?.reviewer || feed.reviewer} disabled={busy}
            onChange={(event) => { setModelDraft({ reviewer: event.target.value, expected_version: feed.version }); setActionError(""); }}>
            {feed.providers.map((provider) => <option key={provider.id} value={provider.id} disabled={!provider.enabled}>
              {provider.label}{provider.enabled ? "" : " — unavailable"}
            </option>)}
          </select>
        </label>
        <button className="primary-button" disabled={busy || !modelDraft || modelDraft.reviewer === feed.reviewer}>
          Save model
        </button>
        {modelDraft && <button type="button" className="text-button" disabled={busy}
          onClick={() => { setModelDraft(null); pending.current = null; setActionError(""); }}>Cancel selection</button>}
      </div>
      <p>{feed.providers.find((provider) => provider.id === (modelDraft?.reviewer || feed.reviewer))?.detail}</p>
      <p>Changing models pauses the stream.</p>
      {modelDraft && modelDraft.expected_version !== feed.version && <p className="error-text">
        Settings changed. Cancel and select again.
      </p>}
    </form>}
    <div className="stream-heading">
      <div><h2>Demo stream</h2><p>{feed ? `${labels[feed.status]} · ${reviewerLabels[feed.reviewer]} reviews` : "Loading stream controls…"}</p></div>
      {feed && <label>Run for
        <select value={runDuration ?? (feed.run_seconds === null ? "none" : String(feed.run_seconds))}
          disabled={busy || isRunning(feed)} onChange={(event) => setRunDuration(event.target.value)}>
          <option value="300">5 minutes</option><option value="600">10 minutes</option><option value="none">Until paused</option>
        </select>
      </label>}
      <button className="secondary-button" disabled={!feed || busy || feed.status === "reviewer_unavailable"} onClick={change}>
        {busy ? "Updating…" : feed && isRunning(feed) ? "Pause stream" : "Start stream"}
      </button>
    </div>
    {feed && <>
      <p>Every {feed.interval_seconds}s, even with this page closed. Pausing stops new arrivals.</p>
      {isRunning(feed) && <p>{feed.stops_at ? `Stops at ${formatTime(feed.stops_at)} UTC.` : "Runs until paused."}</p>}
      {feed.status === "reviewer_changed" && <p>Restart to use {reviewerLabels[feed.reviewer]}.</p>}
      {feed.status === "reviewer_unavailable" && <p>Choose an available model.</p>}
      {feed.status === "waiting_for_reviews" && <p>Resolve items to resume arrivals (limit: {feed.unresolved_limit}).</p>}
      {feed.status === "waiting_for_worker" && <p>No new events. Check the workers.</p>}
      <div className="stream-facts">
        <span>{feed.generated} stream events · {feed.unresolved} unresolved</span>
        {feed.last_event_id && <button className="text-button" onClick={() => onEvent(feed.last_event_id)}>Last arrival: {formatTime(feed.last_event_at)} UTC</button>}
      </div>
    </>}
    {actionError && <p role="alert" className="error-text">{actionError}</p>}
    </div></details>
    {loadError && <p role="alert" className="error-text">{loadError}</p>}
  </section>;
}
