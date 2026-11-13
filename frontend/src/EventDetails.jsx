import Icon from "./Icon.jsx";
import ReviewPanel from "./ReviewPanel.jsx";
import SimulationForm from "./SimulationForm.jsx";
import SimulationLabel from "./SimulationLabel.jsx";
import { StatusBadge } from "./Assessment.jsx";
import { formatTime } from "./format.js";
import { typeLabels } from "./labels.js";

export default function EventDetails({ api, event, loading, error, onRetry, retryBusy, retryError, onSaved, onMemory, onSimulate, simulating, simulationError }) {
  if (loading)
    return (
      <aside className="detail-panel empty-detail" aria-live="polite">
        <span className="spinner" />
        Loading event…
      </aside>
    );
  if (error && !event)
    return (
      <aside className="detail-panel empty-detail">
        <p role="alert">{error}</p>
      </aside>
    );
  if (!event)
    return (
      <aside className="detail-panel empty-detail">
        <Icon name="grid" />
        <h3>Select an event</h3>
      </aside>
    );
  return (
    <aside className="detail-panel" aria-label="Event details">
      {error && <p role="alert" className="error-banner">Draft kept. {error}</p>}
      <div className="detail-top">
        <span className="eyebrow">EVENT DETAILS</span>
        <StatusBadge status={event.status} />
      </div>
      <span className="type-label">
        {typeLabels[event.event_type] || event.event_type}
      </span>
      <h2>{event.title}</h2>
      <SimulationLabel event={event} />
      <p className="detail-location">{event.site} · {event.zone}</p>
      <p className="detail-time">{event.camera} · {formatTime(event.occurred_at)} UTC</p>
      <ReviewPanel api={api} event={event} onRetry={onRetry} retryBusy={retryBusy} retryError={retryError} onSaved={onSaved} onMemory={onMemory} />
      <details className="event-evidence">
        <summary>Event evidence</summary>
        <p className="description">{event.description}</p>
        <dl className="facts">
          <div>
            <dt>Location</dt>
            <dd>
              {event.site}
              <small>{event.zone}</small>
            </dd>
          </div>
          <div>
            <dt>Camera</dt>
            <dd>{event.camera}</dd>
          </div>
          <div>
            <dt>Occurred</dt>
            <dd>
              {formatTime(event.occurred_at)}
              <small>UTC</small>
            </dd>
          </div>
          <div>
            <dt>Source reference</dt>
            <dd className="mono">{event.source_ref}</dd>
          </div>
        </dl>
      </details>
      <SimulationForm key={event.id} event={event} onSimulate={onSimulate} busy={simulating} error={simulationError} />
      <details className="metadata">
        <summary>View source metadata</summary>
        <pre>{JSON.stringify(event.metadata, null, 2)}</pre>
      </details>
      <div className="event-id">
        Event ID <span>{event.id}</span>
      </div>
    </aside>
  );
}
