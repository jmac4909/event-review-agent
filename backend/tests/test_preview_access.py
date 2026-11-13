import hashlib
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from event_review.app import create_app
from event_review.models import LoginRequest
from event_review.settings import Settings


CODE = "synthetic-preview-test-code"
VERIFIER = hashlib.sha256(CODE.encode()).hexdigest()
ACCOUNT = {"account_id": "supervisor-us"}
EVENT = "00000002-0000-4000-8000-000000000001"


def test_private_login_requires_code_and_keeps_secrets_out_of_responses(settings):
    private = replace(settings, preview_access_hash=VERIFIER)
    with TestClient(create_app(private)) as client:
        assert client.get("/api/access").json() == {"required": True}
        for body in (ACCOUNT, ACCOUNT | {"access_code": "incorrect"}):
            assert client.post("/api/session", json=body).status_code == 401
            assert client.get("/api/events").status_code == 401
        response = client.post("/api/session", json=ACCOUNT | {"access_code": CODE})
        assert response.status_code == 200
        assert client.get("/api/session").json()["region"] == "us"
        assert client.get("/api/events").status_code == 200
        assert client.get("/api/events/00000001-0000-4000-8000-000000000001").status_code == 404
        # SessionMiddleware cookies are signed, not encrypted. Inspect without
        # including any credential value in assertion failure output.
        import base64
        payload = base64.b64decode(client.cookies.get("event_review_session").split(".")[0]).decode()
        public = response.text + client.get("/api/health").text + client.get("/api/access").text + payload
        assert not any(secret in public for secret in (CODE, VERIFIER, private.session_secret))
        client.delete("/api/session")
        assert client.get("/api/feed").status_code == 401
    assert CODE not in repr(LoginRequest(**ACCOUNT, access_code=CODE))
    assert VERIFIER not in repr(private)


def test_enabling_or_rotating_the_code_invalidates_existing_sessions(settings):
    with TestClient(create_app(settings)) as public:
        assert public.get("/api/access").json() == {"required": False}
        public.post("/api/session", json=ACCOUNT)
        old_cookie = public.cookies.get("event_review_session")
    private = replace(settings, preview_access_hash=VERIFIER)
    with TestClient(create_app(private)) as client:
        client.cookies.set("event_review_session", old_cookie)
        assert client.get("/api/session").status_code == 401
        client.cookies.clear()
        client.post("/api/session", json=ACCOUNT | {"access_code": CODE})
        private_cookie = client.cookies.get("event_review_session")
    rotated_code = "different-synthetic-test-code"
    rotated = replace(private, preview_access_hash=hashlib.sha256(rotated_code.encode()).hexdigest())
    with TestClient(create_app(rotated)) as client:
        client.cookies.set("event_review_session", private_cookie)
        assert client.get("/api/events").status_code == 401
        assert client.post("/api/session", json=ACCOUNT | {"access_code": CODE}).status_code == 401
        client.cookies.clear()
        assert client.post("/api/session", json=ACCOUNT | {"access_code": rotated_code}).status_code == 200


def test_old_demo_session_cannot_trigger_work_through_any_api_route(settings):
    with TestClient(create_app(settings)) as public:
        public.post("/api/session", json=ACCOUNT)
        cookie = public.cookies.get("event_review_session")
    with TestClient(create_app(replace(settings, preview_access_hash=VERIFIER))) as client:
        client.cookies.set("event_review_session", cookie)
        # All authenticated routes, including future routes, must reject this
        # once-valid public session before evaluating their request body.
        for route in client.app.routes:
            if not route.path.startswith("/api/") or route.path in {
                "/api/health", "/api/access", "/api/demo-accounts", "/api/session", "/api/docs", "/api/openapi.json"
            }:
                continue
            for method in route.methods:
                path = route.path.replace("{event_id}", EVENT).replace("{guidance_id}", EVENT)
                assert client.request(method, path, json={}).status_code == 401, (method, path)


def test_preview_hash_configuration_is_validated_and_loaded(monkeypatch):
    for value in ("not-a-hash", "A" * 64, "a" * 63):
        with pytest.raises(ValueError, match="SHA-256"):
            Settings(preview_access_hash=value)
    monkeypatch.setenv("REVIEW_PREVIEW_ACCESS_HASH", VERIFIER)
    assert Settings.from_env().preview_access_hash == VERIFIER
