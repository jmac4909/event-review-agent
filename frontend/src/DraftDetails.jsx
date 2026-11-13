import { formatTime } from "./format.js";
import ModelUsage from "./ModelUsage.jsx";

const labels = {
  not_requested: "No draft requested.",
  queued: "Draft queued. You can write your own.",
  running: "Drafting guidance… You can edit now.",
  failed: "Draft failed. Retry or write your own.",
  cancelled: "Draft stopped.",
};

export default function DraftDetails({ item, busy, onRetry }) {
  const result = item.draft_result;
  return <div className="guidance-draft">
    {item.status === "proposed" && (result ? <details className="trace draft-explanation">
      <summary>{result.reusable ? "Suggested wording" : "No reusable wording proposed"}</summary>
      {result.reusable && <p className="guidance-text">{result.text}</p>}
      <p className="review-note">{result.rationale}</p>
    </details> : <p className="review-note">{labels[item.draft_status]}</p>)}
    {item.status === "proposed" && item.draft_status === "failed" &&
      <button className="secondary-button" disabled={busy} onClick={onRetry}>Retry guidance draft</button>}
    {!!item.draft_history?.length && <details className="trace draft-history">
      <summary>Draft history ({item.draft_history.length})</summary>
      {item.draft_history.map((attempt) => <div className="draft-attempt" key={attempt.id}>
        <p><strong>{attempt.status === "running" && item.draft_status === "cancelled" ? "Cancelled" : attempt.status}</strong> · {attempt.model_name || "Before model call"}</p>
        <p className="review-note">{formatTime(attempt.started_at)} UTC{attempt.finished_at ? ` → ${formatTime(attempt.finished_at)} UTC` : ""}</p>
        {attempt.error && <p className="error-text">{attempt.error}</p>}
        {attempt.context && <ModelUsage step={{ kind: "model", usage: attempt.usage }} />}
        <details><summary>Exact draft input</summary><pre>{JSON.stringify(attempt.model_input || attempt.context, null, 2)}</pre></details>
        {attempt.model_input && <details><summary>Context & retrieval audit</summary><pre>{JSON.stringify(attempt.context, null, 2)}</pre></details>}
        <details><summary>Exact draft output</summary><pre>{JSON.stringify(attempt.output, null, 2)}</pre></details>
      </div>)}
    </details>}
  </div>;
}
