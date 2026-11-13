import Icon from "./Icon.jsx";
import SimulationLabel from "./SimulationLabel.jsx";
import { StatusBadge, OutcomeBadge, assessmentSource } from "./Assessment.jsx";
import { formatTime } from "./format.js";
import { categoryLabels, typeLabels } from "./labels.js";
import { useEffect, useRef } from "react";

export default function EventList({
  events, matched, eventTypes, counts, selectedId, category, severity, query, type,
  loading, error, regionLabel,
  onQueryChange, onTypeChange, onCategoryChange, onSeverityChange,
  onSelect, onRefresh, onClearFilters,
  page, hasOlder, onOlder, onNewer,
}) {
  const list = useRef(null);
  useEffect(() => {
    if (list.current) list.current.scrollTop = 0;
  }, [page, category, severity, type, query]);
  const hasFilters = category !== "all" || severity !== "all" || type !== "all" || query !== "";
  return (
    <section className="feed-panel" aria-label="Regional events">
      <div className="feed-heading">
        <h2>{category === "needs_input" ? "Needs supervisor input" : categoryLabels[category]}</h2>
        {hasFilters && <button className="text-button" onClick={onClearFilters}>Clear filters</button>}
      </div>
      <div className="filters">
        <label className="search-box">
          <Icon name="search" />
          <input
            aria-label="Search events"
            placeholder="Search events, locations, cameras"
            maxLength={200}
            value={query}
            onChange={(event) => onQueryChange(event.target.value)}
          />
        </label>
        <select
          aria-label="Filter events"
          value={category}
          onChange={(event) => onCategoryChange(event.target.value)}
        >
          {Object.entries(categoryLabels).map(([value, label]) => (
            <option key={value} value={value}>{label}</option>
          ))}
        </select>
        <select
          aria-label="Filter severity"
          value={severity}
          disabled={category !== "all" && category !== "safety_events"}
          onChange={(event) => onSeverityChange(event.target.value)}
        >
          <option value="all">Any severity</option>
          <option value="high">High</option>
          <option value="medium">Medium</option>
          <option value="low">Low</option>
        </select>
        <select
          aria-label="Filter event type"
          value={type}
          onChange={(event) => onTypeChange(event.target.value)}
        >
          <option value="all">All types</option>
          {eventTypes.map((value) => (
            <option key={value} value={value}>
              {typeLabels[value] || value}
            </option>
          ))}
        </select>
      </div>
      {!error && <div className="feed-results" role="status">
        <span>{loading ? "Updating…" : events.length < matched
          ? `${events.length} of ${matched} events · Page ${page}`
          : `${matched} ${matched === 1 ? "event" : "events"}`}</span>
        <span>Newest first</span>
      </div>}
      {error ? (
        <div className="feed-empty" role="alert">
          <h3>Events unavailable</h3>
          <p>{error}</p>
          <button
            className="secondary-button"
            onClick={onRefresh}
          >
            Retry
          </button>
        </div>
      ) : loading && !events.length ? (
        <div className="feed-empty" role="status">
          <span className="spinner" />
          Loading events…
        </div>
      ) : !events.length ? (
        <div className="feed-empty">
          <Icon name="search" />
          <h3>
            {hasFilters || counts.total ? "No matching events" : "No events yet"}
          </h3>
          <p>
            {hasFilters || counts.total
              ? "Try changing your filters."
              : "New events will appear here."}
          </p>
        </div>
      ) : (
        <div className="event-list" ref={list} aria-busy={loading}>
          {events.map((event) => (
            <button
              key={event.id}
              data-testid={`event-${event.id}`}
              className={`event-row ${selectedId === event.id ? "selected" : ""}`}
              onClick={() => onSelect(event.id)}
              aria-pressed={selectedId === event.id}
            >
              <div className="event-row-top">
                <span className="type-label">
                  {typeLabels[event.event_type] || event.event_type}
                </span>
                {!event.assessment && <StatusBadge status={event.status} />}
              </div>
              <strong>{event.title}</strong>
              <SimulationLabel event={event} />
              {event.assessment && <div className="event-assessment">
                <OutcomeBadge decision={event.assessment.decision} severity={event.assessment.severity} />
                <span className="assessment-source">{assessmentSource(event.assessment)}</span>
              </div>}
              <div className="event-location">
                <Icon name="pin" />
                {event.site} · {event.zone}
              </div>
              <div className="event-row-bottom">
                <span>{event.camera}</span>
                <time dateTime={event.occurred_at}>
                  {formatTime(event.occurred_at)} UTC
                </time>
              </div>
            </button>
          ))}
        </div>
      )}
      {(page > 1 || hasOlder) && <nav className="event-pagination" aria-label="Event pages">
        <button className="secondary-button" disabled={loading || page === 1} onClick={onNewer}>Newer events</button>
        <span>Page {page}</span>
        <button className="secondary-button" disabled={loading || !hasOlder} onClick={onOlder}>Older events</button>
      </nav>}
      <div className="feed-footer">
        <span className="live-dot" />
        {regionLabel} events only<span>Times shown in UTC</span>
      </div>
    </section>
  );
}
