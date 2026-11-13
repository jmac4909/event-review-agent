"""Explicit settings; the API always connects as the restricted runtime role."""

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Settings:
    pg_host: str = "/var/run/postgresql"
    pg_port: int = 5432
    pg_database: str = "event_review"
    session_secret: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    preview_access_hash: str = field(default="", repr=False)
    cookie_secure: bool = False
    frontend_dir: Path = ROOT / "frontend" / "dist"
    claim_seconds: int = 15 * 60
    model_mode: str = "mock"
    enabled_models: tuple[str, ...] = ()
    anthropic_model: str = "claude-sonnet-5-5"
    codex_model: str = ""
    codex_command: str = "codex"
    model_timeout_seconds: float = 90
    model_max_tokens: int = 1200
    memory_matching: str = "hybrid"
    embedding_directory: Path = ROOT / ".runtime" / "embedding"
    feed_interval_seconds: int = 30

    def __post_init__(self):
        if self.preview_access_hash and (len(self.preview_access_hash) != 64
                or any(c not in "0123456789abcdef" for c in self.preview_access_hash)):
            raise ValueError("REVIEW_PREVIEW_ACCESS_HASH must be a lowercase SHA-256 hash of a random access code.")
        if self.claim_seconds <= 0:
            raise ValueError("Claim duration must be positive.")
        if self.model_mode not in ("mock", "anthropic", "codex", "regional"):
            raise ValueError("REVIEW_MODEL_MODE must be mock, anthropic, codex, or regional.")
        if any(mode not in ("mock", "anthropic", "codex") for mode in self.enabled_models):
            raise ValueError("REVIEW_ENABLED_MODELS accepts only mock, anthropic, and codex.")
        if self.memory_matching not in ("hybrid", "lexical"):
            raise ValueError("REVIEW_MEMORY_MATCHING must be hybrid or lexical.")
        if not 1 <= self.feed_interval_seconds <= 3600:
            raise ValueError("The event feed interval must be between 1 and 3600 seconds.")
        if not 0 < self.model_timeout_seconds <= 600:
            raise ValueError("Model timeout must be between 0 and 600 seconds.")
        if not 1 <= self.model_max_tokens <= 8192:
            raise ValueError("Model output limit must be between 1 and 8192 tokens.")
        if not self.anthropic_model.strip() or not self.codex_command.strip():
            raise ValueError("Model name and Codex command must not be blank.")

    @property
    def default_reviewer(self) -> str:
        return "mock" if self.model_mode == "regional" else self.model_mode

    def reviewer_for(self, selected: str | None) -> str:
        if self.model_mode == "regional":
            return selected or "mock"
        return self.model_mode

    @property
    def available_models(self) -> tuple[str, ...]:
        if self.model_mode != "regional":
            return (self.model_mode,)
        return tuple(mode for mode in ("mock", "anthropic", "codex")
                     if mode == "mock" or mode in self.enabled_models)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            pg_host=os.getenv("REVIEW_PGHOST", "/var/run/postgresql"),
            pg_port=int(os.getenv("REVIEW_PGPORT", "5432")),
            pg_database=os.getenv("REVIEW_PGDATABASE", "event_review"),
            session_secret=os.getenv("REVIEW_SESSION_SECRET") or secrets.token_urlsafe(32),
            preview_access_hash=os.getenv("REVIEW_PREVIEW_ACCESS_HASH", ""),
            cookie_secure=os.getenv("REVIEW_COOKIE_SECURE", "false").lower() == "true",
            claim_seconds=int(os.getenv("REVIEW_CLAIM_SECONDS", "900")),
            model_mode=os.getenv("REVIEW_MODEL_MODE", "mock"),
            enabled_models=tuple(mode.strip() for mode in os.getenv("REVIEW_ENABLED_MODELS", "").split(",") if mode.strip()),
            anthropic_model=os.getenv("REVIEW_ANTHROPIC_MODEL", "claude-sonnet-5-5"),
            codex_model=os.getenv("REVIEW_CODEX_MODEL", ""),
            codex_command=os.getenv("REVIEW_CODEX_COMMAND", "codex"),
            model_timeout_seconds=float(os.getenv("REVIEW_MODEL_TIMEOUT_SECONDS", "90")),
            model_max_tokens=int(os.getenv("REVIEW_MODEL_MAX_TOKENS", "1200")),
            memory_matching=os.getenv("REVIEW_MEMORY_MATCHING", "hybrid"),
            embedding_directory=Path(os.getenv("REVIEW_EMBEDDING_DIRECTORY", str(ROOT / ".runtime" / "embedding"))),
            feed_interval_seconds=int(os.getenv("REVIEW_FEED_INTERVAL_SECONDS", "30")),
        )

