import hashlib
import logging
import secrets
from datetime import datetime
from typing import Literal
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .auth import SUPERVISORS, current_supervisor
from .claims import ClaimConflict, ClaimStore
from .database import EventNotFound, EventStore, UnsafeDatabaseRole
from .drafts import DraftStore
from .feed import FeedConflict, FeedStore, ReviewerUnavailable
from .memory import FeedbackConflict, InvalidFeedback, MemoryStore
from .models import (Correction, CorrectionRequest, Event, EventDetail, EventList, Guidance,
                     FeedRequest, FeedState, GuidanceApproval, LoginRequest, MemoryList, ReviewerRequest, SimulationRequest, Supervisor)
from .settings import Settings
from .simulation import InvalidScenario


logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = EventStore(settings)
    app = FastAPI(title="Event Review", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.store = store
    # A non-secret fingerprint invalidates old sessions when the gate is enabled
    # or rotated. Neither the access code nor its verifier enters the cookie.
    app.state.preview_access_version = (hashlib.sha256(settings.preview_access_hash.encode()).hexdigest()
                                        if settings.preview_access_hash else None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="event_review_session",
        max_age=8 * 60 * 60,
        same_site="lax",
        https_only=settings.cookie_secure,
    )

    @app.middleware("http")
    async def request_boundary(request: Request, call_next):
        # Browsers send Origin on cross-origin writes. The frontend uses the
        # same origin as the API, including through the development proxy.
        if request.url.path.startswith("/api/") and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin is not None and origin != str(request.base_url).rstrip("/"):
                return JSONResponse(status_code=403, content={"detail": "Cross-origin writes are not allowed."})
        response = await call_next(request)
        session_write_succeeded = request.url.path == "/api/session" and (
            (request.method == "POST" and response.status_code == 200)
            or (request.method == "DELETE" and response.status_code == 204)
        )
        if not session_write_succeeded:
            # Only sign-in/out may set the session cookie. A slow polling
            # response must not reinstall an old account after a tab switches.
            response.raw_headers = [(name, value) for name, value in response.raw_headers
                                    if not (name.lower() == b"set-cookie" and value.startswith(b"event_review_session="))]
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(psycopg.Error)
    @app.exception_handler(UnsafeDatabaseRole)
    async def database_failure(request: Request, error: Exception):
        logger.warning("Event store request failed (%s)", type(error).__name__)
        return JSONResponse(status_code=503, content={"detail": "The event store is temporarily unavailable. Please retry."})

    @app.get("/api/health")
    def health():
        store.check_connection()
        return {"status": "ok", "stage": "event-stream", "model_mode": settings.model_mode,
                "memory_matching": settings.memory_matching}

    @app.exception_handler(EventNotFound)
    async def missing_record(request: Request, error: EventNotFound):
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(FeedConflict)
    @app.exception_handler(FeedbackConflict)
    @app.exception_handler(ClaimConflict)
    async def conflicting_update(request: Request, error: ValueError):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(ReviewerUnavailable)
    @app.exception_handler(InvalidScenario)
    @app.exception_handler(InvalidFeedback)
    async def invalid_option(request: Request, error: ValueError):
        return JSONResponse(status_code=422, content={"detail": str(error)})

    @app.get("/api/demo-accounts", response_model=list[Supervisor])
    def demo_accounts():
        return list(SUPERVISORS.values())

    @app.get("/api/access")
    def access():
        return {"required": bool(settings.preview_access_hash)}

    @app.post("/api/session", response_model=Supervisor)
    def login(body: LoginRequest, request: Request):
        if settings.preview_access_hash and not secrets.compare_digest(
                hashlib.sha256(body.access_code.encode()).hexdigest(), settings.preview_access_hash):
            raise HTTPException(status_code=401, detail="The preview access code is incorrect.")
        account = SUPERVISORS.get(body.account_id)
        if account is None:
            raise HTTPException(status_code=400, detail="Choose one of the available demo supervisors.")
        request.session.clear()
        request.session["account_id"] = account.id
        if app.state.preview_access_version:
            request.session["access_version"] = app.state.preview_access_version
        return account

    @app.get("/api/session", response_model=Supervisor)
    def session(supervisor: Supervisor = Depends(current_supervisor)):
        return supervisor

    @app.delete("/api/session", status_code=204)
    def logout(request: Request):
        if request.headers.get("X-Expected-Supervisor") is not None:
            current_supervisor(request)
        request.session.clear()
        return Response(status_code=204)

    @app.get("/api/events", response_model=EventList)
    def events(
        supervisor: Supervisor = Depends(current_supervisor),
        status: Literal["available", "claimed", "reviewed", "needs_input", "failed"] | None = None,
        category: Literal["safety_events", "dismissed", "needs_input", "pending", "failed"] | None = None,
        severity: Literal["low", "medium", "high"] | None = None,
        event_type: str | None = Query(default=None, max_length=100),
        q: str = Query(default="", max_length=200),
        before_at: datetime | None = None,
        before_id: UUID | None = None,
    ):
        incomplete_cursor = (before_at is None) != (before_id is None)
        missing_timezone = before_at is not None and before_at.tzinfo is None
        if incomplete_cursor or missing_timezone:
            raise HTTPException(status_code=422, detail="Supply both cursor fields, with a timezone on before_at.")
        return store.list_events(
            supervisor.region, status, category=category, severity=severity,
            event_type=event_type, query=q, before_at=before_at, before_id=before_id,
        )

    @app.get("/api/feed", response_model=FeedState)
    def feed(supervisor: Supervisor = Depends(current_supervisor)):
        return FeedStore(settings, supervisor.region).get()

    @app.put("/api/feed", response_model=FeedState)
    def control_feed(body: FeedRequest, supervisor: Supervisor = Depends(current_supervisor)):
        return FeedStore(settings, supervisor.region).set_running(supervisor.id, body)

    @app.put("/api/reviewer", response_model=FeedState)
    def select_reviewer(body: ReviewerRequest, supervisor: Supervisor = Depends(current_supervisor)):
        return FeedStore(settings, supervisor.region).set_reviewer(supervisor.id, body)

    @app.get("/api/events/{event_id}", response_model=EventDetail)
    def event(event_id: UUID, supervisor: Supervisor = Depends(current_supervisor)):
        record = store.inspect_event(supervisor.region, event_id)
        if record is None:
            raise EventNotFound("Event not found.")
        return record

    @app.post("/api/events/{event_id}/retry", status_code=204)
    def retry(event_id: UUID, supervisor: Supervisor = Depends(current_supervisor)):
        ClaimStore(settings, supervisor.region).retry_failed(event_id)
        return Response(status_code=204)

    @app.post("/api/events/{event_id}/corrections", response_model=Correction)
    def correct(event_id: UUID, body: CorrectionRequest, supervisor: Supervisor = Depends(current_supervisor)):
        return MemoryStore(settings, supervisor.region).correct(event_id, supervisor.id, body)

    @app.post("/api/events/{event_id}/simulate", response_model=Event)
    def simulate(event_id: UUID, body: SimulationRequest, supervisor: Supervisor = Depends(current_supervisor)):
        return MemoryStore(settings, supervisor.region).simulate(event_id, body.request_id, body.scenario)

    @app.get("/api/memory", response_model=MemoryList)
    def memory(supervisor: Supervisor = Depends(current_supervisor)):
        return MemoryStore(settings, supervisor.region).list_memory()

    @app.post("/api/guidance/{guidance_id}/approve", response_model=Guidance)
    def approve(guidance_id: UUID, body: GuidanceApproval, supervisor: Supervisor = Depends(current_supervisor)):
        return MemoryStore(settings, supervisor.region).approve(guidance_id, supervisor.id, body)

    @app.post("/api/guidance/{guidance_id}/reject", response_model=Guidance)
    def reject(guidance_id: UUID, supervisor: Supervisor = Depends(current_supervisor)):
        return MemoryStore(settings, supervisor.region).reject_or_retire(guidance_id, supervisor.id, "reject")

    @app.post("/api/guidance/{guidance_id}/retire", response_model=Guidance)
    def retire(guidance_id: UUID, supervisor: Supervisor = Depends(current_supervisor)):
        return MemoryStore(settings, supervisor.region).reject_or_retire(guidance_id, supervisor.id, "retire")

    @app.post("/api/guidance/{guidance_id}/retry-draft", response_model=Guidance)
    def retry_draft(guidance_id: UUID, supervisor: Supervisor = Depends(current_supervisor)):
        return DraftStore(settings, supervisor.region).retry_failed(guidance_id)

    if settings.frontend_dir.is_dir():
        app.mount("/", StaticFiles(directory=settings.frontend_dir, html=True), name="frontend")
    return app

