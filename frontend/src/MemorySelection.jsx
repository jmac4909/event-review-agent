export default function MemorySelection({ context }) {
  const selection = context?.memory_selection;
  if (!selection || typeof selection !== "object" || selection.method === "none") return null;
  const cases = context.memory?.corrections || [];
  const guidance = context.memory?.approved_guidance || [];
  const matches = [...cases.map((item) => ({ ...item, kind: "Case" })),
    ...guidance.map((item) => ({ ...item, kind: "Approved guidance" }))];

  return (
    <details className="trace memory-trace">
      <summary>Memory selection</summary>
      <p>{selection.method === "hybrid" ? "Word overlap + local embeddings" : "Word overlap"}</p>
      <p>{selection.scope}</p>
      <p>{selection.case_scores.length} {selection.case_scores.length === 1 ? "case" : "cases"} considered
        {` · ${selection.guidance_scores.length} approved guidance ${selection.guidance_scores.length === 1 ? "entry" : "entries"}`}</p>
      {selection.more_cases_available && <p>Only the latest {selection.candidate_limit} matching cases were considered.</p>}
      {matches.length ? (
        <>
          <p>Scores rank similarity; they do not measure confidence or grant approval.</p>
          <ul className="memory-matches">
            {matches.map((item) => (
              <li key={`${item.kind}-${item.id}`}>
                <b>{item.kind}: {item.event_snapshot?.title || item.id}</b>
                <span>Words {item.match_scores.lexical.toFixed(3)}
                  {item.match_scores.semantic !== null && ` · Meaning ${item.match_scores.semantic.toFixed(3)}`}
                  {` · Combined ${item.match_scores.combined.toFixed(3)}`}</span>
              </li>
            ))}
          </ul>
        </>
      ) : <p>No eligible memory was available to rank.</p>}
      {selection.model && <small>{selection.model}</small>}
    </details>
  );
}
