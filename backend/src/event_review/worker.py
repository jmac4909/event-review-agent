"""Two simple workers per region; database claims coordinate them."""

import logging
import signal
from threading import Event as StopEvent, Thread
from time import monotonic

import psycopg

from .claims import ClaimConflict, ClaimStore
from .context import model_context
from .database import REGIONS
from .drafts import DraftStore, build_draft_context
from .embeddings import LocalEncoder
from .feed import FeedStore
from .investigation import Investigation, InvestigationLimit, MAX_CONTEXT_BYTES, MAX_LOOKUPS, check_size
from .models import GuidanceDraft, Lookup, ModelReply, ReviewResponse, Verdict, validate_verdict
from .memory import MemoryStore
from .providers import ModelFailure, make_model
from .reviewer import build_context
from .retrieval import MAX_GUIDANCE
from .settings import Settings


logger = logging.getLogger(__name__)


def remaining_seconds(deadline: float) -> float:
    """Use the same deadline before and after model calls and lookups."""
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError()
    return remaining


class Worker:
    def __init__(self, settings: Settings, region: str, worker_id: str, model=None, encoder=None):
        self.settings = settings
        self.claims = ClaimStore(settings, region)
        self.worker_id = worker_id
        self.memory = MemoryStore(settings, region)
        self.drafts = DraftStore(settings, region)
        self.feed = FeedStore(settings, region)
        self.investigation = Investigation(settings, region)
        self.model = model if model is not None else make_model(settings)
        self.other_models = {}
        self.encoder = encoder
        if settings.memory_matching == "hybrid" and self.encoder is None:
            self.encoder = LocalEncoder(settings.embedding_directory)
        if settings.memory_matching == "lexical" and self.encoder is not None:
            raise ValueError("Lexical mode must not receive an embedding model.")
        self.timeout_seconds = min(settings.model_timeout_seconds, settings.claim_seconds)

    def model_for(self, mode: str):
        if mode not in self.settings.available_models:
            raise ModelFailure("The selected model is no longer enabled on this worker.")
        if mode == self.settings.default_reviewer:
            return self.model
        if mode not in self.other_models:
            try:
                self.other_models[mode] = make_model(self.settings, mode)
            except ValueError:
                raise ModelFailure("The selected model is not configured on this worker. Check its server-side setup.") from None
        return self.other_models[mode]

    def run_once(self) -> bool:
        """Claim one event, build its evidence, review it, and save while owned."""
        self.feed.ingest_due()
        attempt = self.claims.claim_next(self.worker_id)
        if attempt is None:
            return False
        event_id = attempt["event_id"]
        attempt_id = attempt["id"]
        deadline = monotonic() + self.timeout_seconds
        try:
            event = self.claims.events.get_event(self.claims.region, event_id)
            memory = self.memory.select_for(event, self.encoder)
            site_rules = self.investigation.site_rules(event)
            context = build_context(event, memory, site_rules)
            try:
                model = self.model_for(attempt["reviewer_mode"])
                self.claims.start_review(event_id, attempt_id, self.worker_id, context, model.name)
                if memory["guidance_overflow"]:
                    raise InvestigationLimit(f"More than {MAX_GUIDANCE} approved rules share this scope. A supervisor must consolidate them before review.")
                verdict = self.investigate(event, attempt_id, context, deadline, model)
            except InvestigationLimit as error:
                verdict = validate_verdict({
                    "decision": "needs_input",
                    "severity": None,
                    "rationale": str(error),
                    "question": "Can a supervisor provide the missing evidence or resolve the applicable site rules for this event?",
                })
                self.claims.save_output(event_id, attempt_id, self.worker_id,
                                        {"source": "harness", "verdict": verdict.model_dump()})
            except TimeoutError:
                self.claims.fail(event_id, attempt_id, self.worker_id, "Model call timed out.")
                return True
            except ModelFailure as error:
                self.claims.fail(event_id, attempt_id, self.worker_id, str(error))
                return True
            except (ClaimConflict, psycopg.Error):
                # A stale claim or uncertain database acknowledgement must not
                # become a model failure that immediately releases the claim.
                raise
            except Exception as error:
                # Do not persist provider exceptions that could contain credentials.
                logger.warning("Reviewer failed (%s)", type(error).__name__)
                self.claims.fail(event_id, attempt_id, self.worker_id, "Reviewer failed before returning an output.")
                return True
            # A database error here is not a reason to repeat the model call.
            self.claims.complete(event_id, attempt_id, self.worker_id, verdict.model_dump())
        except ClaimConflict:
            logger.info("Discarded stale work for attempt %s", attempt_id)
        return True

    def draft_once(self) -> bool:
        """Suggest wording for one correction; this never approves guidance."""
        claimed = self.drafts.claim_next(self.worker_id)
        if claimed is None:
            return False
        guidance_id, attempt_id = claimed["id"], claimed["draft_attempt_id"]
        deadline = monotonic() + self.timeout_seconds
        reply, error = None, None
        try:
            try:
                source = claimed["source"]
                memory = self.memory.select_for(source["event_snapshot"], self.encoder)
                context = build_draft_context(source, memory)
                check_size(context, MAX_CONTEXT_BYTES)
                model_input = model_context(context)
                model = self.model_for(claimed["draft_history"][-1]["reviewer_mode"])
                self.drafts.start(guidance_id, attempt_id, self.worker_id, context, model.name, model_input=model_input)
                reply = model.draft(model_input, timeout_seconds=remaining_seconds(deadline))
                remaining_seconds(deadline)  # A late reply still cannot be accepted.
                try:
                    result = GuidanceDraft.model_validate(reply.output)
                    result.check_result()
                except ValueError:
                    raise ModelFailure("The model returned an invalid guidance draft. Inspect the recorded output.") from None
            except (ClaimConflict, psycopg.Error):
                raise  # Leave an uncertain acknowledgement for reconciliation/expiry.
            except TimeoutError:
                error = "Model call timed out."
            except (ModelFailure, InvestigationLimit) as failure:
                error = str(failure)
            except Exception as failure:
                logger.warning("Guidance drafting failed (%s)", type(failure).__name__)
                error = "Guidance drafting failed before returning a valid output."
            self.drafts.finish(guidance_id, attempt_id, self.worker_id,
                               output=reply.output if reply else None,
                               usage=reply.usage if reply else None, error=error)
        except ClaimConflict:
            logger.info("Discarded stale guidance draft %s", attempt_id)
        return True

    def investigate(self, event: dict, attempt_id, context: dict, deadline: float, model) -> Verdict:
        """Ask for a verdict; run a permitted lookup when the model requests one."""
        event_id = event["id"]
        used_lookups = set()
        for call_index in range(MAX_LOOKUPS + 1):
            check_size(context, MAX_CONTEXT_BYTES)
            reply = self._call_model(event_id, attempt_id, context, deadline, model)
            output = reply.output
            try:
                response = ReviewResponse.model_validate(output)
                response.check_action()
            except ValueError:
                self.claims.save_output(event_id, attempt_id, self.worker_id, output)
                raise ModelFailure("Reviewer returned an invalid verdict or lookup. Inspect the recorded output.") from None
            if response.lookup is None:
                self.claims.save_output(event_id, attempt_id, self.worker_id, output)
                return validate_verdict(response.model_dump(exclude={"lookup"}))
            request = response.lookup
            signature = (request.name, request.event_type, request.lookback_hours)
            if signature in used_lookups:
                raise InvestigationLimit("The reviewer repeated a lookup without reaching a decision.")
            if call_index == MAX_LOOKUPS:
                raise InvestigationLimit("The three-lookup limit was reached without enough evidence for a verdict.")
            used_lookups.add(signature)
            result = self._run_lookup(event, attempt_id, request, deadline)
            # Make a new context so earlier recorded inputs stay unchanged.
            context = {
                **context,
                "lookups_remaining": MAX_LOOKUPS - len(used_lookups),
                "tool_results": [
                    *context["tool_results"],
                    {"request": request.model_dump(), "result": result},
                ],
            }

    def _call_model(self, event_id, attempt_id, context: dict, deadline: float, model) -> ModelReply:
        """Record the input before calling; retain output and usage even on failure."""
        remaining_seconds(deadline)
        model_input = model_context(context)
        step_number = self.claims.start_step(
            event_id, attempt_id, self.worker_id, "model", model.name, model_input,
        )
        reply = None
        try:
            reply = model.review(model_input, timeout_seconds=remaining_seconds(deadline))
            remaining_seconds(deadline)
        except (TimeoutError, ModelFailure) as error:
            message = "Model call timed out." if isinstance(error, TimeoutError) else str(error)
            self.claims.finish_step(
                event_id, attempt_id, self.worker_id, step_number,
                output=reply.output if reply else None,
                usage=reply.usage if reply else None,
                error=message,
            )
            raise
        except Exception:
            self.claims.finish_step(event_id, attempt_id, self.worker_id, step_number, error="Model call failed.")
            raise
        self.claims.finish_step(event_id, attempt_id, self.worker_id, step_number, reply.output, usage=reply.usage)
        return reply

    def _run_lookup(self, event: dict, attempt_id, request: Lookup, deadline: float) -> dict:
        """Run one scoped lookup and record it under the same claim as the model."""
        remaining_seconds(deadline)
        event_id = event["id"]
        step_number = self.claims.start_step(
            event_id, attempt_id, self.worker_id, "tool", request.name, request.model_dump(),
        )
        try:
            result = self.investigation.lookup(event, request)
            remaining_seconds(deadline)
        except (InvestigationLimit, TimeoutError) as error:
            message = "Review deadline passed." if isinstance(error, TimeoutError) else str(error)
            self.claims.finish_step(event_id, attempt_id, self.worker_id, step_number, error=message)
            raise
        self.claims.finish_step(event_id, attempt_id, self.worker_id, step_number, result)
        return result


def run_worker(worker: Worker, stop: StopEvent) -> None:
    while not stop.is_set():
        try:
            worker.run_once()
            if not stop.is_set():
                worker.draft_once()
        except psycopg.Error as error:
            logger.warning("Worker database operation failed (%s)", type(error).__name__)
        except Exception as error:
            # A crash leaves the claim to expire; log only the error type.
            logger.error("Worker run failed (%s)", type(error).__name__)
        stop.wait(1)  # Polling interval, also limits rapid repeated failures.


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    # One shared encoder, not six copies of the same model weights.
    encoder = LocalEncoder(settings.embedding_directory) if settings.memory_matching == "hybrid" else None
    stop = StopEvent()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    threads = []
    for region in sorted(REGIONS):
        for number in (1, 2):
            worker = Worker(settings, region, f"{region}-{number}", encoder=encoder)
            thread = Thread(target=run_worker, args=(worker, stop), name=worker.worker_id)
            thread.start()
            threads.append(thread)
    logger.info("Started %d workers (two per region); enabled models: %s.", len(threads), settings.available_models)
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    main()
