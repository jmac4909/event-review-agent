from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier
import time
from uuid import UUID, uuid4

import psycopg
import pytest

from event_review.claims import ClaimConflict, ClaimStore
from event_review.database import EventNotFound, EventStore
from event_review.settings import Settings


EU_EVENT = UUID("00000001-0000-4000-8000-000000000001")
US_EVENT = UUID("00000002-0000-4000-8000-000000000001")
RESULT = {"test_result": "completed"}  # Storage fixture, not a model verdict schema.


@pytest.fixture
def claims(settings, admin):
    # This test's disposable DB has one European event, so races target one row.
    admin.execute("DELETE FROM review.events WHERE region_id = 'europe' AND id <> %s", (EU_EVENT,))
    return ClaimStore(settings, "europe")


def complete(store, attempt, result):
    return store.complete(attempt["event_id"], attempt["id"], attempt["worker_id"], result)


def fail(store, attempt):
    return store.fail(attempt["event_id"], attempt["id"], attempt["worker_id"], "Test worker failed.")


def expire(admin, attempt):
    # Move only this test DB's timestamps; never wait through a claim window.
    admin.execute("""
        UPDATE review.attempts
        SET started_at = clock_timestamp() - interval '2 hours',
            expires_at = clock_timestamp() - interval '1 hour'
        WHERE id = %s
    """, (attempt["id"],))


def state(admin):
    return admin.execute("""
        SELECT status, attempts_used, active_attempt_id FROM review.events WHERE id = %s
    """, (EU_EVENT,)).fetchone()


def test_claim_has_fixed_database_deadline_and_survives_store_recreation(settings, claims, admin):
    configured = ClaimStore(replace(settings, claim_seconds=42), "europe")
    before = admin.execute("SELECT clock_timestamp()").fetchone()[0]
    attempt = configured.claim_next("worker-a")
    after = admin.execute("SELECT clock_timestamp()").fetchone()[0]
    assert before <= attempt["started_at"] <= after
    assert attempt["expires_at"] - attempt["started_at"] == timedelta(seconds=42)
    assert attempt["region_id"] == "europe"
    assert state(admin) == ("claimed", 1, attempt["id"])
    restarted = ClaimStore(settings, "europe")
    assert restarted.claim_next("worker-b") is None
    assert complete(restarted, attempt, RESULT)["result"] == RESULT
    assert restarted.claim_next("worker-b") is None


def test_two_workers_cannot_claim_the_same_event(claims, admin):
    ready = Barrier(2)

    def claim(worker_id):
        ready.wait(timeout=5)
        return claims.claim_next(worker_id)

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(claim, ["worker-a", "worker-b"]))
    accepted = [attempt for attempt in results if attempt is not None]
    assert len(accepted) == 1
    assert admin.execute("SELECT count(*) FROM review.attempts").fetchone()[0] == 1
    assert state(admin) == ("claimed", 1, accepted[0]["id"])


def test_worker_skips_a_locked_event_and_claims_other_regional_work(settings, admin):
    store = ClaimStore(settings, "europe")
    with admin.transaction():
        locked = admin.execute("""
            SELECT id FROM review.events WHERE region_id = 'europe'
            ORDER BY occurred_at, id LIMIT 1 FOR UPDATE
        """).fetchone()[0]
        attempt = store.claim_next("worker-b")
        assert attempt is not None and attempt["event_id"] != locked
        assert attempt["region_id"] == "europe"


@pytest.mark.parametrize("replacement_worker", ["worker-a", "worker-b"])
def test_expiry_reassignment_rejects_old_attempt_even_for_same_worker(claims, admin, replacement_worker):
    old = claims.claim_next("worker-a")
    expire(admin, old)
    replacement = claims.claim_next(replacement_worker)
    assert replacement["id"] != old["id"]
    assert replacement["event_id"] == old["event_id"]
    assert state(admin) == ("claimed", 2, replacement["id"])
    assert admin.execute("SELECT status FROM review.attempts WHERE id = %s", (old["id"],)).fetchone()[0] == "expired"
    with pytest.raises(ClaimConflict):
        complete(claims, old, RESULT)
    with pytest.raises(ClaimConflict):
        fail(claims, old)
    saved = complete(claims, replacement, RESULT)
    assert saved["attempt_id"] == replacement["id"]
    assert admin.execute("SELECT count(*) FROM review.reviews").fetchone()[0] == 1


def test_expired_attempt_cannot_finish_before_reassignment(claims, admin):
    attempt = claims.claim_next("worker-a")
    expire(admin, attempt)
    with pytest.raises(ClaimConflict, match="expired"):
        complete(claims, attempt, RESULT)
    with pytest.raises(ClaimConflict, match="expired"):
        fail(claims, attempt)
    assert admin.execute("SELECT count(*) FROM review.reviews").fetchone()[0] == 0
    assert state(admin) == ("claimed", 1, attempt["id"])


def test_deadline_is_checked_after_waiting_for_the_event_lock(settings, claims, admin):
    attempt = claims.claim_next("worker-a")
    with ThreadPoolExecutor(max_workers=1) as workers:
        with admin.transaction():
            admin.execute("SELECT id FROM review.events WHERE id = %s FOR UPDATE", (EU_EVENT,))
            pending = workers.submit(complete, claims, attempt, RESULT)
            deadline = time.monotonic() + 2
            while True:
                # PostgreSQL caches monitoring snapshots inside a transaction.
                admin.execute("SELECT pg_stat_clear_snapshot()")
                waiting = admin.execute("""
                    SELECT count(*) FROM pg_stat_activity
                    WHERE datname = %s AND wait_event_type = 'Lock'
                      AND query LIKE '%%FOR UPDATE%%'
                """, (settings.pg_database,)).fetchone()[0]
                if waiting:
                    break
                assert time.monotonic() < deadline, "Worker never reached the contested row lock"
                time.sleep(0.01)
            # This is later than the worker's transaction start, but before its
            # lock is released. Using now() instead of clock_timestamp() is wrong.
            admin.execute("UPDATE review.attempts SET expires_at = clock_timestamp() WHERE id = %s", (attempt["id"],))
        with pytest.raises(ClaimConflict, match="expired"):
            pending.result(timeout=5)
    assert admin.execute("SELECT count(*) FROM review.reviews").fetchone()[0] == 0


def test_two_workers_reclaim_an_expired_event_only_once(claims, admin):
    old = claims.claim_next("worker-old")
    expire(admin, old)
    ready = Barrier(2)

    def reclaim(worker):
        ready.wait(timeout=5)
        return claims.claim_next(worker)

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(reclaim, ["worker-a", "worker-b"]))
    replacements = [attempt for attempt in results if attempt is not None]
    assert len(replacements) == 1
    assert state(admin) == ("claimed", 2, replacements[0]["id"])
    assert admin.execute("SELECT count(*) FROM review.attempts WHERE status = 'running'").fetchone()[0] == 1


def test_concurrent_duplicate_submission_saves_one_result(claims, admin):
    attempt = claims.claim_next("worker-a")
    ready = Barrier(2)

    def submit(_):
        ready.wait(timeout=5)
        return complete(claims, attempt, RESULT)

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(submit, range(2)))
    assert results[0] == results[1]
    assert admin.execute("SELECT count(*) FROM review.reviews").fetchone()[0] == 1
    assert state(admin) == ("reviewed", 1, None)
    expire(admin, attempt)
    assert complete(claims, attempt, RESULT) == results[0]


@pytest.mark.parametrize("changed", [{"test_result": "different"}, {"flag": True}])
def test_changed_replay_cannot_replace_accepted_result(claims, changed):
    attempt = claims.claim_next("worker-a")
    original = {"flag": 1}
    saved = complete(claims, attempt, original)
    with pytest.raises(ClaimConflict, match="different result"):
        complete(claims, attempt, changed)
    assert complete(claims, attempt, original) == saved


def test_bad_result_rolls_back_attempt_completion(claims, admin):
    attempt = claims.claim_next("worker-a")
    with pytest.raises(psycopg.errors.CheckViolation):
        complete(claims, attempt, [])  # The accepted-result table requires a JSON object.
    assert state(admin) == ("claimed", 1, attempt["id"])
    assert admin.execute("SELECT status, finished_at FROM review.attempts WHERE id = %s", (attempt["id"],)).fetchone() == ("running", None)
    assert admin.execute("SELECT count(*) FROM review.reviews").fetchone()[0] == 0
    assert complete(claims, attempt, RESULT)["result"] == RESULT


def test_three_failures_stop_until_manual_retry_and_preserve_history(claims, admin):
    attempts = []
    for number in range(1, 4):
        attempt = claims.claim_next("worker-a")
        attempts.append(attempt)
        failed = fail(claims, attempt)
        assert fail(claims, attempt) == failed
        expected = "failed" if number == 3 else "available"
        assert state(admin) == (expected, number, None)
    assert claims.claim_next("worker-b") is None
    claims.retry_failed(EU_EVENT)
    assert state(admin) == ("available", 0, None)
    with pytest.raises(ClaimConflict):
        claims.retry_failed(EU_EVENT)
    new = claims.claim_next("worker-a")
    with pytest.raises(ClaimConflict):
        complete(claims, attempts[0], RESULT)
    # Repeating an old failure must not release the new claim or consume budget.
    fail(claims, attempts[0])
    assert state(admin) == ("claimed", 1, new["id"])
    assert admin.execute("SELECT count(*) FROM review.attempts").fetchone()[0] == 4


def test_three_expirations_stop_without_a_fourth_claim(claims, admin):
    for _ in range(3):
        attempt = claims.claim_next("worker-a")
        assert attempt is not None
        expire(admin, attempt)
    assert claims.claim_next("worker-b") is None
    assert state(admin) == ("failed", 3, None)
    assert admin.execute("SELECT count(*) FROM review.attempts WHERE status = 'expired'").fetchone()[0] == 3
    claims.retry_failed(EU_EVENT)
    assert claims.claim_next("worker-b") is not None


def test_manual_retry_cannot_reset_running_or_completed_work(claims, admin):
    attempt = claims.claim_next("worker-a")
    with pytest.raises(ClaimConflict):
        claims.retry_failed(EU_EVENT)
    assert state(admin) == ("claimed", 1, attempt["id"])
    complete(claims, attempt, RESULT)
    with pytest.raises(ClaimConflict):
        claims.retry_failed(EU_EVENT)
    assert claims.claim_next("worker-a") is None


def test_wrong_worker_or_attempt_id_cannot_save_or_release_a_claim(claims, admin):
    attempt = claims.claim_next("worker-a")
    for attempt_id, worker in [(attempt["id"], "worker-b"), (uuid4(), "worker-a")]:
        with pytest.raises(ClaimConflict):
            claims.complete(EU_EVENT, attempt_id, worker, RESULT)
        with pytest.raises(ClaimConflict):
            claims.fail(EU_EVENT, attempt_id, worker, "Not the owner")
    assert state(admin) == ("claimed", 1, attempt["id"])


def test_workers_and_history_are_region_scoped(settings, claims, admin):
    eu = claims.claim_next("worker-eu")
    us_store = ClaimStore(settings, "us")
    us = us_store.claim_next("worker-us")
    with pytest.raises(EventNotFound):
        complete(claims, us, RESULT)
    with pytest.raises(EventNotFound):
        claims.retry_failed(us["event_id"])
    with pytest.raises(ClaimConflict):
        claims.complete(EU_EVENT, us["id"], us["worker_id"], RESULT)
    complete(claims, eu, RESULT)
    complete(us_store, us, RESULT)
    for region in ("europe", "us"):
        with EventStore(settings).for_region(region) as connection:
            for table in ("attempts", "reviews"):
                # Fixed test-owned table names; deliberately omit WHERE to prove RLS.
                rows = connection.execute(f"SELECT region_id FROM review.{table}").fetchall()
                assert len(rows) == 1 and rows[0]["region_id"] == region
    with EventStore(settings).connection() as connection:
        assert connection.execute("SELECT count(*) AS n FROM review.attempts").fetchone()["n"] == 0
        assert connection.execute("SELECT count(*) AS n FROM review.reviews").fetchone()["n"] == 0


def test_foreign_keys_reject_cross_region_parent_links(settings, claims):
    with EventStore(settings).for_region("europe") as connection:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            connection.execute("""
                INSERT INTO review.attempts
                    (id, event_id, region_id, worker_id, started_at, expires_at, status)
                VALUES (%s, %s, 'europe', 'worker-eu', clock_timestamp(),
                        clock_timestamp() + interval '15 minutes', 'running')
            """, (uuid4(), US_EVENT))
    us_attempt = ClaimStore(settings, "us").claim_next("worker-us")
    with EventStore(settings).for_region("europe") as connection:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            connection.execute("""
                INSERT INTO review.reviews (event_id, region_id, attempt_id, result)
                VALUES (%s, 'europe', %s, '{}')
            """, (EU_EVENT, us_attempt["id"]))


def test_result_is_unique_and_runtime_cannot_edit_history_or_deadlines(settings, claims, admin):
    attempt = claims.claim_next("worker-a")
    complete(claims, attempt, RESULT)
    with pytest.raises(psycopg.errors.UniqueViolation):
        admin.execute("""
            INSERT INTO review.reviews (event_id, region_id, attempt_id, result)
            VALUES (%s, 'europe', %s, '{}')
        """, (EU_EVENT, attempt["id"]))
    for query in (
        "UPDATE review.reviews SET result = '{}'",
        "DELETE FROM review.reviews",
        "DELETE FROM review.attempts",
        "UPDATE review.attempts SET expires_at = clock_timestamp() + interval '1 day'",
        "UPDATE review.attempts SET worker_id = 'someone-else'",
    ):
        with EventStore(settings).for_region("europe") as connection:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(query)


@pytest.mark.parametrize("worker_id", ["", "  ", "x" * 101])
def test_invalid_worker_cannot_consume_an_attempt(claims, admin, worker_id):
    with pytest.raises(ValueError):
        claims.claim_next(worker_id)
    assert state(admin) == ("available", 0, None)


def test_claim_settings_are_explicit_and_validated(monkeypatch):
    assert Settings().claim_seconds == 900
    monkeypatch.setenv("REVIEW_CLAIM_SECONDS", "60")
    assert Settings.from_env().claim_seconds == 60
    with pytest.raises(ValueError):
        Settings(claim_seconds=0)
    with pytest.raises(ValueError):
        ClaimStore(Settings(), "not-a-region")
