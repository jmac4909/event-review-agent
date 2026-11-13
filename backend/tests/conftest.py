import os
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql

from event_review.app import create_app
from event_review.bootstrap import bootstrap
from event_review.settings import Settings


@pytest.fixture
def settings():
    socket = os.environ.get("REVIEW_TEST_SOCKET")
    if not socket:
        pytest.fail("Run with scripts/dev.py test or set REVIEW_TEST_SOCKET to an isolated test cluster.")
    port = int(os.environ.get("REVIEW_TEST_PORT", "5432"))
    database = f"review_test_{uuid4().hex[:12]}"
    bootstrap(socket, port, database)
    # Most integration tests isolate storage/claims. Embedding-specific tests
    # opt into the packaged model, with the same no-network test policy.
    yield Settings(pg_host=socket, pg_port=port, pg_database=database, memory_matching="lexical")
    # Only this fixture's newly created database is removed.
    with psycopg.connect(host=socket, port=port, user="review_bootstrap", dbname="postgres", autocommit=True) as connection:
        connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))


@pytest.fixture
def admin(settings):
    with psycopg.connect(host=settings.pg_host, port=settings.pg_port, dbname=settings.pg_database, user="review_bootstrap", autocommit=True) as connection:
        yield connection


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client

