export const decisionLabels = {
  confirmed: "Safety event",
  rejected: "Alert dismissed",
  needs_input: "Needs input",
};

export function modelLabel(name) {
  if (name?.startsWith("codex/")) return "Codex";
  if (name?.startsWith("anthropic/")) return "Claude";
  if (name?.startsWith("deterministic-mock")) return "Mock";
  return "Agent";
}

export function attemptModelLabel(attempt) {
  const hasModelStep = attempt.steps?.some((step) => step.kind === "model");
  // Older reviews predate per-call traces but still record their provider.
  const hasLegacyModelOutput = attempt.model_finished_at
    && attempt.model_output && attempt.model_output.source !== "harness";
  if (hasModelStep || hasLegacyModelOutput) return modelLabel(attempt.model_name);
  if (attempt.model_output?.source === "harness") return "Review check · no model call";
  return "No model call recorded";
}

export function assessmentSource(assessment) {
  if (assessment.source === "supervisor") return "Supervisor";
  if (assessment.source === "harness") return "Review check";
  return modelLabel(assessment.model_name);
}

export function OutcomeBadge({ decision, severity }) {
  return <span className={`outcome outcome-${decision} outcome-${severity || "none"}`}>
    {decisionLabels[decision]}{decision === "confirmed" && severity && <b> · {severity} severity</b>}
  </span>;
}

const statusLabels = {
  available: "Awaiting review",
  claimed: "In review",
  reviewed: "Review complete",
  needs_input: "Needs input",
  failed: "Review failed",
  running: "Running",
  completed: "Completed",
  expired: "Expired",
};

export function StatusBadge({ status }) {
  return <span className={`status status-${status}`}><i />{statusLabels[status] || status}</span>;
}
