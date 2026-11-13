"""Explicit database setup, run separately from the restricted API process."""

import argparse
import hashlib
import json
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from .settings import ROOT


def bootstrap(socket_dir: str, port: int, database: str) -> None:
    if not Path(socket_dir).is_absolute():
        raise ValueError("Setup requires an explicit local PostgreSQL socket directory.")
    connection_args = dict(host=socket_dir, port=port, user="review_bootstrap", autocommit=True)
    with psycopg.connect(dbname="postgres", **connection_args) as admin:
        for role, attributes in (
            ("review_owner", "NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS"),
            ("review_app", "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS"),
        ):
            if not admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                admin.execute(sql.SQL("CREATE ROLE {} " + attributes).format(sql.Identifier(role)))
        if not admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,)).fetchone():
            admin.execute(sql.SQL("CREATE DATABASE {} OWNER review_owner").format(sql.Identifier(database)))
        else:
            admin.execute(sql.SQL("ALTER DATABASE {} OWNER TO review_owner").format(sql.Identifier(database)))
        admin.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database)))
        admin.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO review_app").format(sql.Identifier(database)))

    with psycopg.connect(dbname=database, **connection_args) as connection:
        with connection.transaction():
            connection.execute("SET LOCAL ROLE review_owner")
            connection.execute("SELECT pg_advisory_xact_lock(73521001)")
            connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            connection.execute("CREATE SCHEMA IF NOT EXISTS review AUTHORIZATION review_owner")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS review.schema_migrations (
                    name text PRIMARY KEY,
                    checksum text NOT NULL,
                    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
                )
            """)
            for migration in sorted((ROOT / "backend" / "sql").glob("*.sql")):
                content = migration.read_text()
                checksum = hashlib.sha256(content.encode()).hexdigest()
                previous = connection.execute(
                    "SELECT checksum FROM review.schema_migrations WHERE name = %s", (migration.name,)
                ).fetchone()
                if previous:
                    if previous[0] != checksum:
                        raise RuntimeError(f"Applied migration changed: {migration.name}; add a new migration instead.")
                    continue
                connection.execute(content)
                connection.execute(
                    "INSERT INTO review.schema_migrations (name, checksum) VALUES (%s, %s)",
                    (migration.name, checksum),
                )
            events = json.loads((ROOT / "data" / "events.json").read_text())
            for event in events:
                connection.execute("SELECT set_config('app.region', %s, true)", (event["region_id"],))
                connection.execute("""
                    INSERT INTO review.events (
                        id, region_id, source_ref, event_type, title, description,
                        site, zone, camera, occurred_at, metadata
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (region_id, source_ref) DO NOTHING
                """, (
                    event["id"], event["region_id"], event["source_ref"], event["event_type"],
                    event["title"], event["description"], event["site"], event["zone"],
                    event["camera"], event["occurred_at"], Jsonb(event["metadata"]),
                ))
            for source in json.loads((ROOT / "data" / "site-context.json").read_text()):
                connection.execute("SELECT set_config('app.region', %s, true)", (source["region_id"],))
                connection.execute("""
                    INSERT INTO review.site_context (region_id, site, version, valid_from, source_ref, content)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (region_id, site, version) DO NOTHING
                """, (source["region_id"], source["site"], source["version"], source["valid_from"],
                      source["source_ref"], Jsonb(source["content"])))
    print("Database schema and synthetic seed events are ready.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket-dir", required=True)
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--database", default="event_review")
    args = parser.parse_args()
    bootstrap(args.socket_dir, args.port, args.database)


if __name__ == "__main__":
    main()

