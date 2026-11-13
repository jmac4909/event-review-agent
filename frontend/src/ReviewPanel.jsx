import { useEffect, useState } from "react";
import { formatTime, reviewRationale } from "./format.js";
import SupervisorForm from "./SupervisorForm.jsx";
import AttemptHistory from "./AttemptHistory.jsx";
import { decisionLabels, modelLabel } from "./Assessment.jsx";
import { reviewerLabels } from "./labels.js";

function ResultCard({ result, savedAt, modelName, historical = false }) {
  return <div className={`result-card ${historical ? "archived-verdict" : "verdict"} verdict-${result.decision} severity-${result.severity || "none"}`}>
    <strong>{decisionLabels[result.decision]}</strong>
    {result.severity && <span className="severity">{result.severity} severity</span>}
    <h4 className="decision-label">Why this decision?</h4>
    <p>{reviewRationale(result.rationale, modelName)}</p>
    {result.question && <p className="input-question"><b>{historical ? "Original question" : "Supervisor input needed"}</b>{result.question}</p>}
    <small>Saved {formatTime(savedAt)} UTC</small>
  </div>;
}

function ReviewEvidence({ context }) {
  if (!context) return null;
  const event = context.event;
  const rule = context.site_rules?.rules?.[event.event_type];
  const guidance = context.memory?.approved_guidance || [];
  const cases = context.memory?.corrections || [];
  let baseline = "No upfront rule was recorded.";
  if (rule?.duration_threshold_seconds != null) {
    baseline = `Blocking for ${rule.duration_threshold_seconds}s or more: ${rule.at_or_above_threshold.decision}, ${rule.at_or_above_threshold.severity} severity. This event: ${event.metadata.observed_duration_seconds}s.`;
  } else if (rule?.threshold_m != null) {
    baseline = `Separation below ${rule.threshold_m}m: ${rule.below_threshold.decision}, ${rule.below_threshold.severity} severity. This event: ${event.metadata.estimated_distance_m}m.`;
  } else if (rule?.unknown) {
    baseline = rule.unknown;
  } else if (rule) {
    baseline = "The supplied site rule is recorded in the full context.";
  }
  return <details className="decision-evidence">
    <summary>Rules and memory supplied</summary>
    <p><b>Baseline:</b> {baseline}</p>
    <p><b>Approved guidance:</b> {guidance.length ? "Applicability is explained in the saved rationale." : "None matched."}</p>
    {guidance.length > 0 && <ul>{guidance.map((item) => <li key={item.id}>{item.text}</li>)}</ul>}
    {cases.length > 0 && <p>{cases.length} earlier case {cases.length === 1 ? "correction was" : "corrections were"} also supplied as evidence.</p>}
  </details>;
}

function ReviewProgress({ attempt }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [attempt.id]);
  const seconds = Math.max(0, Math.floor((now - new Date(attempt.started_at)) / 1000));
  const reviewer = attempt.model_name ? modelLabel(attempt.model_name)
    : reviewerLabels[attempt.reviewer_mode] || "Agent";
  return <p className="review-waiting">
    {now >= new Date(attempt.expires_at) ? "Claim expired · waiting for retry" : `${reviewer} reviewing · ${seconds}s`}
  </p>;
}

function ReviewMetrics({ attempt }) {
  if (!attempt?.finished_at) return null;
  const seconds = Math.max(0, (new Date(attempt.finished_at) - new Date(attempt.started_at)) / 1000);
  const calls = attempt.steps?.filter((step) => step.kind === "model") || [];
  const hasCounts = calls.length > 0 && calls.every(({ usage }) => usage && usage.provider !== "mock"
    && Number.isInteger(usage.input_tokens) && Number.isInteger(usage.output_tokens));
  const input = hasCounts ? calls.reduce((total, { usage }) => total + usage.input_tokens
    + (usage.cache_read_input_tokens || 0) + (usage.cache_creation_input_tokens || 0), 0) : 0;
  const output = hasCounts ? calls.reduce((total, { usage }) => total + usage.output_tokens, 0) : 0;
  return <p className="review-note review-metrics">Agent review: {seconds.toFixed(1)}s
    {hasCounts && ` · ${input.toLocaleString()} input / ${output.toLocaleString()} output tokens`}
  </p>;
}

export default function ReviewPanel({ api, event, onRetry, retryBusy, retryError, onSaved, onMemory }) {
  const review = event.review;
  const verdict = review?.result;
  const latest = event.corrections[0];
  const accepted = event.attempts.find((attempt) => attempt.id === review?.attempt_id);
  const active = event.attempts.find((attempt) => attempt.status === "running");
  const reviewer = accepted?.model_name;
  const provider = accepted?.model_output?.source === "harness" ? "Review check" : modelLabel(reviewer);
  const current = latest ? { decision: latest.decision, severity: latest.severity, rationale: latest.reason } : verdict;
  return (
    <section className="review-section" aria-label="Review and attempts">
      <div className="review-heading">
        <h3>{latest ? "Supervisor assessment" : "Agent assessment"}</h3>
        {reviewer && <span className="badge">{latest ? "Supervisor" : provider}</span>}
      </div>
      {verdict ? (
        <ResultCard result={current} savedAt={latest?.created_at || review.created_at} modelName={latest ? null : reviewer} />
      ) : event.status === "claimed" && active ? <ReviewProgress attempt={active} /> : (
        <p className="review-waiting">
          {event.status === "failed" ? "Retry limit reached. Check the history before retrying."
            : event.status === "claimed" ? "Reviewing…"
            : "Waiting for review."}
        </p>
      )}
      {verdict && <ReviewMetrics attempt={accepted} />}
      {verdict && !latest && <ReviewEvidence context={accepted?.context} />}
      {event.status === "failed" && (
        <button className="secondary-button" onClick={() => onRetry(event.id)} disabled={retryBusy}>
          {retryBusy ? "Requesting retry…" : "Retry failed event"}
        </button>
      )}
      {retryError && <p role="alert" className="error-text">{retryError}</p>}
      {verdict && <SupervisorForm key={event.id} api={api} event={event} onSaved={onSaved} onMemory={onMemory} />}
      {latest && <details className="original-review">
        <summary>Original agent review · {provider}</summary>
        <ResultCard result={verdict} savedAt={review.created_at} historical />
        <ReviewEvidence context={accepted?.context} />
      </details>}
      <AttemptHistory attempts={event.attempts} />
    </section>
  );
}
