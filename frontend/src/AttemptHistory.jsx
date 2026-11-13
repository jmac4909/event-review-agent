import { formatTime } from "./format.js";
import { StatusBadge, attemptModelLabel } from "./Assessment.jsx";
import MemorySelection from "./MemorySelection.jsx";
import ModelUsage from "./ModelUsage.jsx";

export default function AttemptHistory({ attempts }) {
  return (
    <details className="history-panel">
      <summary>Review history & context ({attempts.length} attempts)</summary>
      {!attempts.length && <p className="review-note">No attempts recorded yet.</p>}
      {attempts.map((attempt) => (
        <details className="attempt" key={attempt.id}>
          <summary><StatusBadge status={attempt.status} />
            <span>{attemptModelLabel(attempt)}</span>
          </summary>
          <dl className="attempt-facts">
            <div><dt>Claimed</dt><dd>{formatTime(attempt.started_at)} UTC</dd></div>
            <div><dt>Deadline</dt><dd>{formatTime(attempt.expires_at)} UTC</dd></div>
            {attempt.finished_at && <div><dt>Finished</dt><dd>{formatTime(attempt.finished_at)} UTC</dd></div>}
            <div><dt>Reviewer</dt><dd>{attempt.model_name || "Not started"}</dd></div>
            <div><dt>Worker</dt><dd>{attempt.worker_id}</dd></div>
            {attempt.model_finished_at && <div><dt>Review duration</dt><dd>{new Date(attempt.model_finished_at) - new Date(attempt.model_started_at)} ms</dd></div>}
          </dl>
          {attempt.error && <p className="attempt-error">{attempt.error}</p>}
          <MemorySelection context={attempt.context} />
          {attempt.context && (
            <details className="trace">
              <summary>Context & retrieval audit</summary>
              <pre>{JSON.stringify(attempt.context, null, 2)}</pre>
            </details>
          )}
          {!!attempt.steps?.length && (
            <details className="trace">
              <summary>Investigation steps ({attempt.steps.length})</summary>
              {attempt.steps.map((step, index) => (
                <details className="trace" key={index}>
                  <summary>{index + 1}. {step.kind === "tool" ? "Lookup" : "Model"}: {step.name}</summary>
                  <p>{step.finished_at ? `${new Date(step.finished_at) - new Date(step.started_at)} ms` : "Did not finish"}</p>
                  {step.error && <p className="attempt-error">{step.error}</p>}
                  <ModelUsage step={step} />
                  <b>Input</b><pre>{JSON.stringify(step.input, null, 2)}</pre>
                  <b>Output</b><pre>{JSON.stringify(step.output, null, 2)}</pre>
                </details>
              ))}
            </details>
          )}
          {attempt.model_finished_at && (
            <details className="trace">
              <summary>View model output</summary>
              <pre>{JSON.stringify(attempt.model_output, null, 2)}</pre>
            </details>
          )}
          <p className="attempt-id">Attempt {attempt.id}</p>
        </details>
      ))}
    </details>
  );
}
