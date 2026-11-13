import { useEffect, useRef, useState } from "react";
import Brand from "./Brand.jsx";
import Icon from "./Icon.jsx";
import EventList from "./EventList.jsx";
import EventDetails from "./EventDetails.jsx";
import MemoryPanel from "./MemoryPanel.jsx";
import FeedControls from "./FeedControls.jsx";
import { regionMarks } from "./labels.js";

export default function Workspace({ user, api, onLogout, busy, sessionError }) {
  // Feed data and cursor history belong to this signed-in workspace.
  const [eventPage, setEventPage] = useState({
    items: [], matched: 0, next_page: null, event_types: [],
    counts: {
      total: 0, available: 0, claimed: 0,
      reviewed: 0, needs_input: 0, failed: 0, safety_events: 0,
    },
  });
  const counts = eventPage.counts;
  const [pageStarts, setPageStarts] = useState([null]);
  const pageStart = pageStarts[pageStarts.length - 1];
  const [loading, setLoading] = useState(false);
  const [eventsError, setEventsError] = useState("");
  const [refresh, setRefresh] = useState(0);

  // Keep the selected event separate from the currently visible page.
  const [selectedId, setSelectedId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [query, setQuery] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [type, setType] = useState("all");
  const [category, setCategory] = useState("all");
  const [severity, setSeverity] = useState("all");

  // Actions and navigation keep their own pending state.
  const [retryBusy, setRetryBusy] = useState(false);
  const [retryError, setRetryError] = useState("");
  const [view, setView] = useState("feed");
  const [guidanceTarget, setGuidanceTarget] = useState(null);
  const [simulating, setSimulating] = useState(false);
  const [simulationError, setSimulationError] = useState("");
  const simulationRequest = useRef(null);
  const [mobileEventOpen, setMobileEventOpen] = useState(false);
  const listScroll = useRef(0);
  const detailPanel = useRef(null);

  // Wait briefly for typing to stop before searching the database.
  useEffect(() => {
    const timer = setTimeout(() => {
      setSearchQuery(query.trim());
      setPageStarts([null]);
    }, 300);
    return () => clearTimeout(timer);
  }, [query]);

  // Poll only after the preceding list/detail requests have finished.
  useEffect(() => {
    if (loading || detailLoading) return;
    const timer = setInterval(() => setRefresh((value) => value + 1), 3000);
    return () => clearInterval(timer);
  }, [user, loading, detailLoading]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    const params = new URLSearchParams();
    if (category !== "all") params.set("category", category);
    if (severity !== "all") params.set("severity", severity);
    if (type !== "all") params.set("event_type", type);
    if (searchQuery) params.set("q", searchQuery);
    if (pageStart) {
      params.set("before_at", pageStart.occurred_at);
      params.set("before_id", pageStart.id);
    }
    const suffix = params.size ? `?${params}` : "";
    async function loadEvents() {
      try {
        const page = await api(`/events${suffix}`, { signal: controller.signal });
        setEventsError("");
        setEventPage(page);
        // New arrivals or paging can move this event outside the list. Keep
        // its detail and any supervisor draft until the user selects another.
        setSelectedId((previous) => previous || page.items[0]?.id || null);
      } catch (error) {
        if (error.name === "AbortError") return;
        setEventsError(error.message || "Events could not be loaded. Please retry.");
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }
    loadEvents();
    return () => controller.abort();
  }, [user, refresh, category, severity, type, searchQuery, pageStart]);

  useEffect(() => {
    setDetail((previous) => previous?.id === selectedId ? previous : null);
    if (!selectedId) {
      setDetailLoading(false);
      return;
    }
    const controller = new AbortController();
    setDetailLoading(true);
    async function loadDetails() {
      try {
        const event = await api(`/events/${selectedId}`, { signal: controller.signal });
        setDetailError("");
        setDetail(event);
      } catch (error) {
        if (error.name !== "AbortError")
          setDetailError(error.message || "Event details could not be loaded.");
      } finally {
        if (!controller.signal.aborted) setDetailLoading(false);
      }
    }
    loadDetails();
    return () => controller.abort();
  }, [user, selectedId, refresh]);

  useEffect(() => { setRetryError(""); setSimulationError(""); }, [selectedId]);

  function refreshEvents() {
    setRefresh((value) => value + 1);
  }

  function clearFilters() {
    setPageStarts([null]);
    setQuery("");
    setSearchQuery("");
    setType("all");
    setCategory("all");
    setSeverity("all");
  }

  function changeCategory(nextCategory) {
    setPageStarts([null]);
    setCategory(nextCategory);
    // Dismissed, unanswered and unreviewed alerts have no safety severity.
    if (nextCategory !== "all" && nextCategory !== "safety_events") setSeverity("all");
  }

  function changeType(value) {
    setType(value);
    setPageStarts([null]);
  }

  function changeSeverity(value) {
    setSeverity(value);
    setPageStarts([null]);
  }

  async function retryEvent(eventId) {
    setRetryBusy(true);
    setRetryError("");
    try {
      await api(`/events/${eventId}/retry`, { method: "POST" });
      refreshEvents();
    } catch (error) {
      setRetryError(error.message || "Retry could not be requested.");
    } finally {
      setRetryBusy(false);
    }
  }

  async function simulateEvent(eventId, scenario) {
    // Reuse the request ID after a lost response, so Retry cannot create a duplicate.
    if (simulationRequest.current?.eventId !== eventId || simulationRequest.current?.scenario !== scenario)
      simulationRequest.current = { eventId, scenario, requestId: crypto.randomUUID() };
    setSimulating(true);
    setSimulationError("");
    try {
      const created = await api(`/events/${eventId}/simulate`, {
        method: "POST", body: JSON.stringify({ request_id: simulationRequest.current.requestId, scenario }),
      });
      simulationRequest.current = null;
      openEvent(created.id);
    } catch (error) {
      setSimulationError(error.message);
    } finally {
      setSimulating(false);
    }
  }

  function openEvent(id) {
    setView("feed");
    clearFilters();
    setSelectedId(id);
    setMobileEventOpen(true);
    scrollToEvent();
    refreshEvents();
  }

  function showFeed(nextCategory) {
    setView("feed");
    clearFilters();
    setCategory(nextCategory);
    setMobileEventOpen(false);
    requestAnimationFrame(() => window.scrollTo(0, 0));
  }

  function showMemory(guidance = null) {
    setGuidanceTarget(guidance ? { ...guidance } : null);
    setView("memory");
    requestAnimationFrame(() => window.scrollTo(0, 0));
  }

  function selectEvent(id) {
    listScroll.current = window.scrollY;
    setSelectedId(id);
    setMobileEventOpen(true);
    scrollToEvent();
  }

  function scrollToEvent() {
    requestAnimationFrame(() => {
      if (window.matchMedia("(max-width: 900px)").matches) {
        window.scrollTo(0, 0);
      } else {
        detailPanel.current.scrollTop = 0;
        detailPanel.current.scrollIntoView({ block: "nearest" });
      }
    });
  }

  function backToEvents() {
    setMobileEventOpen(false);
    requestAnimationFrame(() => {
      window.scrollTo(0, listScroll.current);
      document.querySelector(`[data-testid="event-${selectedId}"]`)?.focus({ preventScroll: true });
    });
  }

  const selectedIsListed = eventPage.items.some((event) => event.id === selectedId);

  return (
    <div className="workspace">
      <aside className="sidebar">
        <Brand />
        <div className="workspace-region">
          <span className="region-mark">{regionMarks[user.region]}</span>
          <span>
            <small>YOUR REGION</small>
            <strong>{user.region_label}</strong>
          </span>
        </div>
        <nav aria-label="Workspace">
          <button className={`nav-item ${view === "feed" && category !== "needs_input" ? "active" : ""}`} onClick={() => showFeed("all")}>
            <Icon name="grid" />
            Event feed<span className="nav-count">{counts.total}</span>
          </button>
          <button className={`nav-item ${view === "feed" && category === "needs_input" ? "active" : ""}`} onClick={() => showFeed("needs_input")}>
            <Icon name="clock" />
            Needs input<span className="nav-count">{counts.needs_input}</span>
          </button>
          <button className={`nav-item ${view === "memory" ? "active" : ""}`} onClick={() => showMemory()}>
            <Icon name="book" />
            Memory & guidance
          </button>
        </nav>
      </aside>
      <div className="main-column">
        <header className="topbar">
          <span>
            <span className="breadcrumb">Workspace /</span> {view === "memory" ? "Memory & guidance" : "Event feed"}
          </span>
          <div className="account-summary">
            <span className="avatar">{regionMarks[user.region]}</span>
            <span>{user.name}</span>
            <button className="text-button" onClick={onLogout} disabled={busy}>
              Sign out
            </button>
          </div>
        </header>
        <main className="main-content">
          <nav className="mobile-workspace-nav" aria-label="Mobile workspace">
            <button aria-current={view === "feed" && category !== "needs_input" ? "page" : undefined} onClick={() => showFeed("all")}>Event feed</button>
            <button aria-current={view === "feed" && category === "needs_input" ? "page" : undefined} onClick={() => showFeed("needs_input")}>Needs input <span>{counts.needs_input}</span></button>
            <button aria-current={view === "memory" ? "page" : undefined} onClick={() => showMemory()}>Guidance</button>
          </nav>
          {/* Keep both sections mounted so navigation preserves unsaved drafts. */}
          <div hidden={view !== "memory"}>
            <MemoryPanel api={api} onEvent={openEvent} active={view === "memory"} target={guidanceTarget} />
          </div>
          <div hidden={view !== "feed"} className={`feed-workspace ${mobileEventOpen ? "event-open" : ""}`}>
            <div className="feed-overview">
              <div className="page-heading workspace-heading">
                <div>
                  <span className="eyebrow">
                    {user.region_label.toUpperCase()} WORKSPACE
                  </span>
                  <h1>Event feed</h1>
                </div>
                <button
                  className="secondary-button"
                  onClick={refreshEvents}
                  disabled={loading}
                >
                  <Icon name="refresh" />
                  {loading ? "Refreshing…" : "Refresh"}
                </button>
              </div>
              {sessionError && (
                <div role="alert" className="error-banner">
                  {sessionError}
                </div>
              )}
              <FeedControls api={api} refresh={refresh} onChanged={refreshEvents} onEvent={openEvent} />
              <section className="stats" aria-label="Regional event summary">
                <button aria-pressed={category === "safety_events"} onClick={() => changeCategory("safety_events")}>
                  <span>Safety events</span>
                  <strong>
                    {counts.safety_events}
                  </strong>
                  <small>Confirmed by a review</small>
                </button>
                <button aria-pressed={category === "needs_input"} onClick={() => changeCategory("needs_input")}>
                  <span>Needs input</span>
                  <strong>
                    {counts.needs_input}
                  </strong>
                  <small>Awaiting your answer</small>
                </button>
                <button aria-pressed={category === "pending"} onClick={() => changeCategory("pending")}>
                  <span>Pending review</span>
                  <strong>{counts.available + counts.claimed}</strong>
                  <small>Queued or in progress</small>
                </button>
              </section>
            </div>
            <div className="feed-grid">
              <EventList
                events={eventPage.items}
                matched={eventPage.matched}
                page={pageStarts.length}
                hasOlder={!!eventPage.next_page}
                onOlder={() => setPageStarts((previous) => [...previous, eventPage.next_page])}
                onNewer={() => setPageStarts((previous) => previous.slice(0, -1))}
                eventTypes={eventPage.event_types}
                counts={counts}
                selectedId={selectedId}
                category={category}
                severity={severity}
                query={query}
                type={type}
                loading={loading || searchQuery !== query.trim()}
                error={eventsError}
                regionLabel={user.region_label}
                onQueryChange={setQuery}
                onTypeChange={changeType}
                onCategoryChange={changeCategory}
                onSeverityChange={changeSeverity}
                onSelect={selectEvent}
                onRefresh={refreshEvents}
                onClearFilters={clearFilters}
              />
              <div className="detail-anchor" ref={detailPanel}>
                <button className="back-to-events" onClick={backToEvents}>← Back to events</button>
                {selectedId && !selectedIsListed && !loading && !eventsError && (
                  <p className="selection-note">Selected event is outside this list.</p>
                )}
                <EventDetails
                  api={api}
                  event={detail?.id === selectedId ? detail : null}
                  loading={detailLoading && detail?.id !== selectedId}
                  error={detailError}
                  onRetry={retryEvent}
                  retryBusy={retryBusy}
                  retryError={retryError}
                  onSaved={refreshEvents}
                  onMemory={showMemory}
                  onSimulate={simulateEvent}
                  simulating={simulating}
                  simulationError={simulationError}
                />
              </div>
            </div>
          </div>
        </main>
      </div>
    </div>
  );
}
