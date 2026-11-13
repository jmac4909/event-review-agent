import { useState } from "react";
import { formatTime } from "./format.js";
import DraftDetails from "./DraftDetails.jsx";
import { OutcomeBadge } from "./Assessment.jsx";

const guidanceLabels = {
  proposed: "Pending approval",
  approved: "In use",
  rejected: "Discarded",
  retired: "Stopped",
  superseded: "Replaced",
};

export default function GuidanceCard({ api, item, onChanged, onEvent }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const displayedDecision = item.status === "proposed" ? item.source_decision : item.decision;
  const displayedSeverity = item.status === "proposed" ? item.source_severity : item.severity;
  const differsFromSource = displayedDecision !== item.source_decision || displayedSeverity !== item.source_severity;
  const hasRule = text.trim().length >= 20 && text.trim().split(/\s+/).length >= 4;
  const sourceFacts = { ...item.metadata_condition };
  delete sourceFacts.detector_score;

  function startEditing() {
    // Take the latest wording once. Polling must never replace a typed draft.
    setText(item.draft_result?.reusable ? item.draft_result.text : "");
    setEditing(true);
  }

  async function act(action) {
    setBusy(true);
    setError("");
    try {
      const options = { method: "POST" };
      if (action === "approve") options.body = JSON.stringify({ text });
      await api(`/guidance/${item.id}/${action}`, options);
      setEditing(false);
      onChanged();
    } catch (failure) {
      setError(failure.message);
    } finally {
      setBusy(false);
    }
  }

  function approve(event) {
    event.preventDefault();
    act("approve");
  }

  return <article className="guidance-card" data-testid={`guidance-${item.id}`}>
    <div className="review-heading"><h3>{item.site} · {item.zone}</h3><span className={`badge guidance-${item.status}`}>{guidanceLabels[item.status]}</span></div>
    <p className="guidance-scope">{item.camera} · {item.event_type.replaceAll("_", " ")}</p>
    {item.approved_text ? <p className="guidance-text">{item.approved_text}</p> :
      <p className="guidance-text"><strong>Source correction:</strong> {item.proposed_text}</p>}
    <div className="guidance-effect"><strong>Source correction</strong>
      <OutcomeBadge decision={displayedDecision} severity={displayedSeverity} />
      {item.status === "proposed" && <small>From your correction.</small>}
    </div>
    {differsFromSource && <div className="guidance-conflict" role="note">
      <strong>Guidance and source correction differ</strong>
      <p>Source decision:</p>
      <OutcomeBadge decision={item.source_decision} severity={item.source_severity} />
      <p>Update the source review, then approve its replacement guidance.</p>
    </div>}
    <p className="review-note">Considered for this location and event type. The reviewer checks the rule's conditions against each new event.</p>
    <details className="trace"><summary>Source facts and wording</summary>
      <p className="review-note">Facts from the original event. Future measurements can differ.</p>
      <pre>{JSON.stringify(sourceFacts, null, 2)}</pre>
      <p className="review-note">Source correction: {item.proposed_text}</p>
    </details>
    {item.decided_by && <p className="review-note">{item.decided_by} · {formatTime(item.decided_at)} UTC</p>}
    {item.retired_at && <p className="review-note">Stopped {formatTime(item.retired_at)} UTC</p>}
    {item.status === "proposed" && <>
      {editing ? <form className="feedback-form" onSubmit={approve}>
        <label>Reusable guidance
          <textarea required rows={4} minLength={20} maxLength={2000} value={text} onChange={(e) => setText(e.target.value)} disabled={busy} />
        </label>
        <p className="review-note">Write when this applies and what future reviews should do. An answer to this one event is not a reusable rule.</p>
        <div className="form-actions">
          <button className="primary-button" type="submit" disabled={busy || !hasRule}>Approve guidance</button>
          <button className="text-button" type="button" disabled={busy} onClick={() => setEditing(false)}>Cancel</button>
        </div>
      </form> : <div className="form-actions">
        <button className="primary-button" disabled={busy} onClick={startEditing}>
          {item.draft_result && !item.draft_result.reusable ? "Write reusable guidance" : "Review & approve"}
        </button>
        <button className="secondary-button" disabled={busy} onClick={() => act("reject")}>Discard suggestion</button>
      </div>}
    </>}
    {item.status === "approved" && <button className="secondary-button" disabled={busy} onClick={() => act("retire")}>Stop using guidance</button>}
    {error && <p role="alert" className="error-text">{error}</p>}
    <button className="text-button memory-link" onClick={() => onEvent(item.source_event_id)}>Open source event</button>
    <DraftDetails item={item} busy={busy} onRetry={() => act("retry-draft")} />
  </article>;
}

