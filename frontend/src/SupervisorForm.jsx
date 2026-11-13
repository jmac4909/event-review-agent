import { useEffect, useState } from "react";
import { formatTime } from "./format.js";

const guidanceStatus = {
  proposed: "Guidance awaiting approval",
  approved: "Guidance approved",
  retired: "Guidance stopped",
  rejected: "Guidance discarded",
  superseded: "Guidance replaced",
};

export default function SupervisorForm({ api, event, onSaved, onMemory }) {
  const latest = event.corrections[0];
  const latestVersion = latest?.version || 0;
  // A draft exists only while the form is open.
  const [draft, setDraft] = useState(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const [savedVersion, setSavedVersion] = useState(0);
  // After saving, polling may still show the preceding correction briefly.
  const guidanceIsCurrent = event.guidance?.source_correction_id === latest?.id
    && latest?.version >= savedVersion;
  const guidance = guidanceIsCurrent ? event.guidance : null;
  const draftIsStale = draft !== null && latestVersion !== draft.expected_version;

  let editLabel = "Correct this review";
  if (latest) editLabel = "Update supervisor review";
  else if (event.status === "needs_input") editLabel = "Answer and resolve";

  let guidanceLinkLabel = "Review suggested guidance";
  if (guidance?.status === "approved") guidanceLinkLabel = "View approved guidance";
  else if (guidance && guidance.status !== "proposed") guidanceLinkLabel = "View guidance history";

  useEffect(() => {
    // Polling can confirm our save even if its HTTP acknowledgement was lost.
    if (draft && latest?.id === draft.request_id) {
      setDraft(null);
      setSaved(true);
      setSavedVersion(latest.version);
      setError("");
      onSaved();
    }
  }, [latest?.id, draft?.request_id, onSaved]);

  function startEditing() {
    const original = event.review.result;
    setDraft({
      request_id: crypto.randomUUID(),
      expected_version: latestVersion,
      decision: latest?.decision || (original.decision === "needs_input" ? "" : original.decision),
      severity: latest?.severity || original.severity || "",
      reason: latest?.reason || "",
    });
    setError("");
    setSaved(false);
    setSavedVersion(0);
  }

  function change(field, value) {
    setDraft((previous) => ({ ...previous, [field]: value, request_id: crypto.randomUUID() }));
  }

  async function submit(eventObject) {
    eventObject.preventDefault();
    setSaving(true);
    setError("");
    const body = { ...draft, severity: draft.decision === "confirmed" ? draft.severity : null };
    try {
      const correction = await api(`/events/${event.id}/corrections`, { method: "POST", body: JSON.stringify(body) });
      setDraft(null);
      setSaved(true);
      setSavedVersion(correction.version);
      onSaved();
    } catch (failure) {
      setError(failure.message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="supervisor-section" aria-label="Supervisor review">
      <h3>Supervisor review</h3>
      {latest && <p className="review-note">Version {latest.version} · {formatTime(latest.created_at)} UTC</p>}
      {saved && <p role="status" className="saved-message">Review saved.</p>}
      {guidance && <p className="review-note" data-testid="source-guidance-status">{guidanceStatus[guidance.status]}</p>}
      {!draft ? (
        <button className="primary-button" onClick={startEditing}>
          {editLabel}
        </button>
      ) : (
        <form className="feedback-form" onSubmit={submit}>
          {event.review.result.question && <p className="form-question">{event.review.result.question}</p>}
          <p className="review-note">Applies to this event only.</p>
          <label>Supervisor decision
            <select required value={draft.decision} onChange={(e) => change("decision", e.target.value)} disabled={saving}>
              <option value="">Choose an outcome</option>
              <option value="confirmed">Safety event</option>
              <option value="rejected">Dismiss alert</option>
            </select>
          </label>
          {draft.decision === "confirmed" && <label>Severity
            <select required value={draft.severity} onChange={(e) => change("severity", e.target.value)} disabled={saving}>
              <option value="">Choose severity</option>
              <option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option>
            </select>
          </label>}
          <label>Answer or reason
            <textarea required maxLength={2000} rows={4} value={draft.reason} disabled={saving}
              placeholder="Reason for this decision"
              onChange={(e) => change("reason", e.target.value)} />
          </label>
          {error && <p role="alert" className="error-text">{error}</p>}
          {draftIsStale && (
            <p role="alert" className="error-text">Newer review available. Draft kept; load the latest before saving.</p>
          )}
          <div className="form-actions">
            <button className="primary-button" type="submit" aria-busy={saving} disabled={saving || !draft.reason.trim() || draftIsStale}>
              {saving ? "Saving…" : "Save supervisor review"}
            </button>
            <button className="text-button" type="button" disabled={saving} onClick={() => setDraft(null)}>Cancel</button>
            {draftIsStale && <button className="text-button" type="button" onClick={startEditing}>Load latest review</button>}
          </div>
        </form>
      )}
      {(latest || saved) && <button className="text-button memory-link" onClick={() => onMemory(guidance || { status: "proposed" })}>
        {guidanceLinkLabel}
      </button>}
      {event.corrections.length > 1 && <details className="correction-history">
        <summary>Supervisor correction history ({event.corrections.length})</summary>
        {event.corrections.map((item) => <div key={item.id}>
          <strong>Version {item.version} · {item.decision}{item.severity ? ` / ${item.severity}` : ""}</strong>
          <p>{item.reason}</p><small>{item.supervisor_id} · {formatTime(item.created_at)} UTC</small>
        </div>)}
      </details>}
    </section>
  );
}
