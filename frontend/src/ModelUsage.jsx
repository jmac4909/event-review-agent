const labels = {
  input_tokens: "Input tokens",
  cache_read_input_tokens: "Input read from cache",
  cache_creation_input_tokens: "Input written to cache",
  cached_input_tokens: "Cached portion of input",
  output_tokens: "Output tokens",
  reasoning_output_tokens: "Reasoning portion of output",
};

export default function ModelUsage({ step }) {
  if (step.kind !== "model") return null;
  const usage = step.usage;
  if (usage === undefined) return <p className="review-note">Token usage was not recorded for this older call.</p>;
  if (usage === null) return <p className="review-note">No token counts reported for this call.</p>;
  if (usage.provider === "mock") return <p className="review-note">Mock call · no model tokens used.</p>;
  return <section className="model-usage" aria-label="Model token usage">
    <h4>Reported token usage</h4>
    <dl className="usage-counts">
      {Object.entries(labels).filter(([key]) => usage[key] !== undefined).map(([key, label]) => (
        <div key={key}>
          <dt>{key === "input_tokens" && usage.provider === "anthropic" ? "Uncached input tokens" : label}</dt>
          <dd>{usage[key].toLocaleString()}</dd>
        </div>
      ))}
    </dl>
  </section>;
}
