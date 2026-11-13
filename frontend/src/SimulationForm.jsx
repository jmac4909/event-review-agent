import { useState } from "react";
import { formatTime } from "./format";

export default function SimulationForm({ event, onSimulate, busy, error }) {
  const [open, setOpen] = useState(false);
  const [scenario, setScenario] = useState("repeat");
  const fieldId = `scenario-${event.id}`;
  return (
    <section className="simulation-section" aria-label="Simulate event">
      <button className="secondary-button" aria-expanded={open} onClick={() => setOpen(!open)} disabled={busy}>
        Simulate similar event
      </button>
      {open && <form onSubmit={(submit) => { submit.preventDefault(); onSimulate(event.id, scenario); }}>
        <label htmlFor={fieldId}>Test scenario</label>
        <select id={fieldId} value={scenario} disabled={busy} onChange={(change) => setScenario(change.target.value)}>
          <option value="repeat">Same evidence · current time</option>
          {event.event_type === "restricted_area_entry" && <>
            <option value="same_day">Same day · {formatTime(event.occurred_at)} UTC</option>
            <option value="two_days_later">Two days later · {formatTime(Date.parse(event.occurred_at) + 2 * 86400000)} UTC</option>
          </>}
          {event.event_type === "pedestrian_vehicle_proximity" && <>
            <option value="distance_3m">3 m separation</option>
            <option value="distance_4_99m">4.99 m separation</option>
            <option value="distance_5m">5 m separation</option>
          </>}
        </select>
        <p>The selected model reviews the new event using current approved guidance.</p>
        <button className="primary-button" disabled={busy}>{busy ? "Creating event…" : "Create event"}</button>
      </form>}
      {error && <p role="alert" className="error-text">{error}</p>}
    </section>
  );
}
