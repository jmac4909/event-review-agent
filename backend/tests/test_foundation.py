import psycopg
import pytest

from event_review.bootstrap import bootstrap
from event_review.database import EventStore


EU_EVENT = "00000001-0000-4000-8000-000000000001"
US_EVENT = "00000002-0000-4000-8000-000000000001"


def sign_in(client, region="europe"):
    response = client.post("/api/session", json={"account_id": f"supervisor-{region}"})
    assert response.status_code == 200
    return response.json()


def test_reads_require_a_session(client):
    assert client.get("/api/events").status_code == 401
    assert client.get(f"/api/events/{EU_EVENT}").status_code == 401


def test_expected_supervisor_is_a_precondition_not_region_authority(client, admin):
    from uuid import uuid4

    sign_in(client, "us")
    header = {"X-Expected-Supervisor": "supervisor-us"}
    assert client.get("/api/events", headers=header).status_code == 200
    sign_in(client, "europe")  # Another tab replaces the shared cookie.
    before_feed = client.get("/api/feed").json()
    before_corrections = admin.execute("SELECT count(*) FROM review.corrections").fetchone()[0]
    requests = [
        ("GET", "/api/events", None),
        ("GET", f"/api/events/{US_EVENT}", None),
        ("GET", "/api/memory", None),
        ("GET", "/api/feed", None),
        ("PUT", "/api/feed", {"running": True, "reviewer": "mock", "expected_version": before_feed["version"], "run_seconds": 300}),
        ("PUT", "/api/reviewer", {"reviewer": "mock", "expected_version": before_feed["version"]}),
        ("POST", f"/api/events/{US_EVENT}/corrections", {"request_id": str(uuid4()), "expected_version": 0,
            "decision": "rejected", "severity": None, "reason": "Wrong tab must not save."}),
        ("POST", f"/api/events/{EU_EVENT}/simulate", {"request_id": str(uuid4())}),
        ("DELETE", "/api/session", None),
    ]
    for method, path, body in requests:
        response = client.request(method, path, json=body, headers=header)
        assert response.status_code == 412, (method, path, response.text)
        assert "set-cookie" not in response.headers
    assert client.get("/api/feed").json() == before_feed
    assert admin.execute("SELECT count(*) FROM review.corrections").fetchone()[0] == before_corrections
    assert client.get("/api/session").json()["region"] == "europe"
    # Omitting the precondition never bypasses the signed session / RLS.
    assert client.get(f"/api/events/{US_EVENT}").status_code == 404
    assert client.delete("/api/session", headers={"X-Expected-Supervisor": "supervisor-europe"}).status_code == 204
    assert client.get("/api/events").status_code == 401


def test_polling_cannot_reinstall_an_old_session_cookie(client):
    login = client.post("/api/session", json={"account_id": "supervisor-europe"})
    assert "event_review_session=" in login.headers["set-cookie"]
    for path in ("/api/session", "/api/events", "/api/feed", "/api/memory", f"/api/events/{EU_EVENT}"):
        response = client.get(path)
        assert response.status_code == 200
        assert "set-cookie" not in response.headers
    failed_login = client.post("/api/session", json={"account_id": "unknown"})
    assert failed_login.status_code == 400 and "set-cookie" not in failed_login.headers
    logout = client.delete("/api/session")
    assert logout.status_code == 204 and "event_review_session=" in logout.headers["set-cookie"]


@pytest.mark.parametrize("region", ["europe", "us", "asia"])
def test_login_derives_region_and_limits_events(client, region):
    assert sign_in(client, region)["region"] == region
    # A query parameter cannot override the account's region.
    response = client.get("/api/events?region=another-region")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 4
    assert {event["region_id"] for event in items} == {region}
    assert client.get("/api/session").json()["region"] == region


def test_cross_region_event_ids_are_not_disclosed(client):
    sign_in(client)
    assert client.get(f"/api/events/{EU_EVENT}").status_code == 200
    denied = client.get(f"/api/events/{US_EVENT}")
    missing = client.get("/api/events/ffffffff-ffff-4fff-8fff-ffffffffffff")
    assert denied.status_code == missing.status_code == 404
    assert denied.json() == missing.json()


def test_login_rejects_region_injection_and_unknown_accounts(client):
    assert client.post("/api/session", json={"account_id": "supervisor-europe", "region": "us"}).status_code == 422
    assert client.post("/api/session", json={"account_id": "invented"}).status_code == 400


def test_sign_out_and_tampered_session(client):
    sign_in(client)
    assert client.delete("/api/session").status_code == 204
    assert client.get("/api/session").status_code == 401
    client.cookies.set("event_review_session", "tampered")
    assert client.get("/api/events").status_code == 401


def test_browser_cross_origin_writes_are_rejected(client):
    response = client.post("/api/session", json={"account_id": "supervisor-europe"}, headers={"Origin": "https://unrelated.example"})
    assert response.status_code == 403
    assert client.get("/api/session").status_code == 401


def test_database_failure_is_visible_without_exposing_internals(client, app, monkeypatch):
    sign_in(client)
    def unavailable(region, status=None, **filters):
        raise psycopg.OperationalError("internal connection detail")
    monkeypatch.setattr(app.state.store, "list_events", unavailable)
    response = client.get("/api/events")
    assert response.status_code == 503
    assert "internal connection detail" not in response.text
    assert "temporarily unavailable" in response.json()["detail"]


def test_rls_filters_even_without_an_application_where_clause(settings):
    store = EventStore(settings)
    for region in ("europe", "us", "asia"):
        with store.for_region(region) as connection:
            rows = connection.execute("SELECT region_id FROM review.events").fetchall()
            assert len(rows) == 4
            assert {row["region_id"] for row in rows} == {region}
    with store.connection() as connection:
        assert connection.execute("SELECT count(*) AS n FROM review.events").fetchone()["n"] == 0


def test_region_context_clears_after_commit_and_rollback(settings):
    with EventStore(settings).connection() as connection:
        with connection.transaction():
            connection.execute("SELECT set_config('app.region', 'europe', true)")
            assert connection.execute("SELECT count(*) AS n FROM review.events").fetchone()["n"] == 4
        assert connection.execute("SELECT count(*) AS n FROM review.events").fetchone()["n"] == 0
        with pytest.raises(RuntimeError):
            with connection.transaction():
                connection.execute("SELECT set_config('app.region', 'us', true)")
                raise RuntimeError("simulate request failure")
        assert connection.execute("SELECT count(*) AS n FROM review.events").fetchone()["n"] == 0
        with connection.transaction():
            connection.execute("SELECT set_config('app.region', 'asia', true)")
            assert {row["region_id"] for row in connection.execute("SELECT region_id FROM review.events")} == {"asia"}


def test_runtime_role_is_restricted_and_rls_is_enabled(settings, admin):
    role = admin.execute("SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname='review_app'").fetchone()
    assert role == (False, False, False, False)
    table = admin.execute("SELECT relrowsecurity, relforcerowsecurity, pg_get_userbyid(relowner) FROM pg_class WHERE oid='review.events'::regclass").fetchone()
    assert table == (True, True, "review_owner")
    with EventStore(settings).for_region("europe") as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("UPDATE review.events SET title='unauthorized'")


def test_write_policy_rejects_a_cross_region_insert(settings, admin):
    # Temporarily grant INSERT only in this disposable test database, so this
    # proves WITH CHECK independently of the foundation's read-only grant.
    admin.execute("GRANT INSERT ON review.events TO review_app")
    try:
        with EventStore(settings).for_region("europe") as connection:
            with pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level security"):
                connection.execute("""
                    INSERT INTO review.events (
                        id, region_id, source_ref, event_type, title, description,
                        site, zone, camera, occurred_at, metadata
                    ) VALUES (
                        'ffffffff-0000-4000-8000-000000000001', 'us', 'forbidden-test',
                        'test', 'test', 'test', 'test', 'test', 'test', clock_timestamp(), '{}'
                    )
                """)
    finally:
        admin.execute("REVOKE INSERT ON review.events FROM review_app")


def test_seed_is_repeatable_without_overwriting_existing_events(settings, admin):
    original = admin.execute("SELECT description FROM review.events WHERE id=%s", (EU_EVENT,)).fetchone()[0]
    admin.execute("UPDATE review.events SET description='Preserve this existing record' WHERE id=%s", (EU_EVENT,))
    try:
        bootstrap(settings.pg_host, settings.pg_port, settings.pg_database)
        assert admin.execute("SELECT count(*) FROM review.events").fetchone()[0] == 12
        assert admin.execute("SELECT description FROM review.events WHERE id=%s", (EU_EVENT,)).fetchone()[0] == "Preserve this existing record"
    finally:
        admin.execute("UPDATE review.events SET description=%s WHERE id=%s", (original, EU_EVENT))

