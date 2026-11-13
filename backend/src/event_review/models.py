from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


Region = Literal["europe", "us", "asia"]
SimulationScenario = Literal[
    "repeat", "same_day", "two_days_later", "november_12", "november_14",
    "distance_3m", "distance_4_99m", "distance_5m"
]


@dataclass
class ModelReply:
    # Transport metadata stays separate from the model-authored JSON.
    output: Any
    usage: dict | None = None


class Supervisor(BaseModel):
    id: str
    name: str
    region: Region
    region_label: str


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: str
    access_code: str = Field(default="", max_length=128, repr=False)


class Event(BaseModel):
    id: UUID
    region_id: Region
    source_ref: str
    event_type: str
    title: str
    description: str
    site: str
    zone: str
    camera: str
    occurred_at: datetime
    status: str
    metadata: dict[str, Any]
    simulation_scenario: SimulationScenario | None = None


class EventCounts(BaseModel):
    total: int
    available: int
    claimed: int
    reviewed: int
    needs_input: int
    failed: int
    safety_events: int


class EventAssessment(BaseModel):
    decision: Literal["confirmed", "rejected", "needs_input"]
    severity: Literal["low", "medium", "high"] | None
    source: Literal["supervisor", "agent", "harness"]
    model_name: str | None


class EventListItem(Event):
    assessment: EventAssessment | None


class EventCursor(BaseModel):
    occurred_at: datetime
    id: UUID


class EventList(BaseModel):
    items: list[EventListItem]
    counts: EventCounts
    matched: int
    event_types: list[str]
    next_page: EventCursor | None = None


class FeedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    running: bool = Field(strict=True)
    expected_version: int = Field(ge=0, strict=True)
    reviewer: Literal["mock", "anthropic", "codex"]
    run_seconds: Literal[300, 600] | None = 300


class ReviewerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0, strict=True)
    reviewer: Literal["mock", "anthropic", "codex"]


class ProviderOption(BaseModel):
    id: Literal["mock", "anthropic", "codex"]
    label: str
    enabled: bool
    detail: str


class FeedState(BaseModel):
    region_id: Region
    status: Literal["paused", "running", "auto_stopped", "reviewer_changed", "reviewer_unavailable", "waiting_for_reviews", "waiting_for_worker"]
    version: int
    reviewer: Literal["mock", "anthropic", "codex"]
    model_selection: Literal["global", "regional"]
    providers: list[ProviderOption]
    interval_seconds: int
    run_seconds: int | None
    stops_at: datetime | None
    unresolved: int
    unresolved_limit: int
    generated: int
    last_event_id: UUID | None
    last_event_at: datetime | None
    updated_by: str | None
    updated_at: datetime


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    decision: Literal["confirmed", "rejected", "needs_input"]
    severity: Literal["low", "medium", "high"] | None
    rationale: str = Field(min_length=1, max_length=2000)
    question: str | None = Field(default=None, min_length=1, max_length=500)


def validate_verdict(output: dict) -> Verdict:
    verdict = Verdict.model_validate(output)
    if not verdict.rationale.strip():
        raise ValueError("A rationale is required.")
    if verdict.decision == "confirmed":
        if verdict.severity is None or verdict.question is not None:
            raise ValueError("A confirmed event needs a severity and no question.")
    elif verdict.decision == "needs_input":
        if verdict.severity is not None or not (verdict.question or "").strip():
            raise ValueError("Needs input requires a question and an unknown severity.")
    elif verdict.severity is not None or verdict.question is not None:
        raise ValueError("A rejected event has no severity or question.")
    return verdict


class Lookup(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: Literal["find_events", "get_site_context"]
    event_type: str | None = Field(default=None, min_length=1, max_length=80)
    lookback_hours: int | None = Field(default=None, ge=1, le=168)

    def check_arguments(self) -> None:
        if self.name == "get_site_context":
            if self.event_type is not None or self.lookback_hours is not None:
                raise ValueError("Site context takes no filters.")
        elif self.lookback_hours is None:
            raise ValueError("Recent events needs a lookback between 1 and 168 hours.")


class ReviewResponse(Verdict):
    # The model either requests a lookup or returns the usual verdict fields.
    decision: Literal["confirmed", "rejected", "needs_input"] | None = None
    lookup: Lookup | None = None

    def check_action(self) -> None:
        if not self.rationale.strip():
            raise ValueError("A rationale is required.")
        if self.lookup is not None:
            self.lookup.check_arguments()
            if self.decision is not None or self.severity is not None or self.question is not None:
                raise ValueError("A lookup request cannot also decide the event.")
        else:
            validate_verdict(self.model_dump(exclude={"lookup"}))


class Attempt(BaseModel):
    id: UUID
    worker_id: str
    status: str
    started_at: datetime
    expires_at: datetime
    finished_at: datetime | None
    error: str | None
    context: dict[str, Any] | None
    model_name: str | None
    reviewer_mode: Literal["mock", "anthropic", "codex"] | None = None
    model_started_at: datetime | None
    model_finished_at: datetime | None
    model_output: Any
    steps: list[dict[str, Any]] = Field(default_factory=list)


class SavedReview(BaseModel):
    attempt_id: UUID
    created_at: datetime
    result: Verdict


class CorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    expected_version: int = Field(ge=0, strict=True)
    decision: Literal["confirmed", "rejected"]
    severity: Literal["low", "medium", "high"] | None
    reason: str = Field(min_length=1, max_length=2000)


class GuidanceApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=20, max_length=2000)


class GuidanceDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reusable: bool
    text: str | None = Field(max_length=2000)
    rationale: str = Field(min_length=1, max_length=1000)

    def check_result(self) -> None:
        if not self.rationale.strip():
            raise ValueError("A rationale is required.")
        if self.reusable:
            if not self.text or not self.text.strip():
                raise ValueError("Reusable guidance needs text.")
        elif self.text is not None:
            raise ValueError("Case-specific evidence has no reusable text.")


class SimulationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    scenario: SimulationScenario = "repeat"


class Correction(BaseModel):
    id: UUID
    event_id: UUID
    version: int
    decision: Literal["confirmed", "rejected"]
    severity: str | None
    reason: str
    supervisor_id: str
    created_at: datetime


class Guidance(BaseModel):
    id: UUID
    source_event_id: UUID
    source_correction_id: UUID
    event_type: str
    site: str
    zone: str
    camera: str
    metadata_condition: dict[str, Any]
    proposed_text: str
    approved_text: str | None
    decision: Literal["confirmed", "rejected"]
    severity: str | None
    status: str
    created_at: datetime
    decided_at: datetime | None
    decided_by: str | None
    retired_at: datetime | None
    retired_by: str | None
    draft_status: str
    draft_result: GuidanceDraft | None
    draft_history: list[dict[str, Any]]


class GuidanceListItem(Guidance):
    source_decision: Literal["confirmed", "rejected"]
    source_severity: str | None


class MemoryList(BaseModel):
    corrections: list[Correction]
    guidance: list[GuidanceListItem]


class EventGuidance(BaseModel):
    id: UUID
    source_correction_id: UUID
    status: Literal["proposed", "approved", "rejected", "retired", "superseded"]


class EventDetail(Event):
    attempts: list[Attempt]
    review: SavedReview | None
    corrections: list[Correction]
    guidance: EventGuidance | None = None

