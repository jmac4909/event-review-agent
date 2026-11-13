"""Run only this project's local database, API, or isolated integration tests.

Requires Python 3.12 with the backend dependencies and PostgreSQL binaries.
Docker Compose is the portable path for machines without local PostgreSQL.
"""

import argparse
import hashlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".local-state"
SOCKET = STATE / "socket"
PGDATA = STATE / "pgdata"
PORT = 55439


def run(arguments, **kwargs):
    return subprocess.run(arguments, cwd=ROOT, check=True, **kwargs)


def environment():
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(ROOT / "backend" / "src"),
        "REVIEW_PGHOST": str(SOCKET),
        "REVIEW_PGPORT": str(PORT),
        "REVIEW_PGDATABASE": "event_review",
        "REVIEW_TEST_SOCKET": str(SOCKET),
        "REVIEW_TEST_PORT": str(PORT),
    })
    return env


def start_database():
    for executable in ("initdb", "pg_ctl"):
        if shutil.which(executable) is None:
            raise SystemExit(f"{executable} is required for local development. Use Docker Compose instead.")
    STATE.mkdir(mode=0o700, exist_ok=True)
    SOCKET.mkdir(mode=0o700, exist_ok=True)
    if not (PGDATA / "PG_VERSION").exists():
        run(["initdb", "-D", str(PGDATA), "-U", "review_bootstrap", "--encoding=UTF8", "--locale=C", "--auth-local=trust", "--auth-host=reject"])
    status = subprocess.run(["pg_ctl", "-D", str(PGDATA), "status"], capture_output=True)
    if status.returncode != 0:
        options = shlex.join([
            "-k", str(SOCKET), "-p", str(PORT),
            "-c", "listen_addresses=", "-c", "unix_socket_permissions=0700",
            "-c", "shared_buffers=32MB", "-c", "max_connections=30",
            "-c", "max_wal_size=64MB", "-c", "min_wal_size=32MB",
        ])
        run(["pg_ctl", "-D", str(PGDATA), "-l", str(STATE / "postgres.log"), "-o", options, "-w", "start"])
    run([sys.executable, "-m", "event_review.bootstrap", "--socket-dir", str(SOCKET), "--port", str(PORT)], env=environment())


def browser_tests(pattern: str | None = None, provider_controls: bool = False, private_preview: bool = False):
    """Run write journeys against a disposable database and loopback server."""
    import psycopg
    from psycopg import sql

    sys.path.insert(0, str(ROOT / "backend/src"))
    from event_review.bootstrap import bootstrap
    from event_review.claims import ClaimStore
    from event_review.settings import Settings

    database = f"review_browser_{uuid4().hex[:12]}"
    env = environment() | {"REVIEW_PGDATABASE": database, "REVIEW_MODEL_MODE": "regional" if provider_controls else "mock",
                           "REVIEW_ENABLED_MODELS": "", "REVIEW_FEED_INTERVAL_SECONDS": "2",
                           "REVIEW_PREVIEW_ACCESS_HASH": ""}
    # Exercise real selector storage/API without allowing test workers to make
    # paid requests. Non-mock work waits until the test selects Mock again.
    api_env = env | {"REVIEW_ENABLED_MODELS": "anthropic,codex" if provider_controls else ""}
    test_code = "synthetic-browser-test-code" if private_preview else ""
    if private_preview:
        api_env["REVIEW_PREVIEW_ACCESS_HASH"] = hashlib.sha256(test_code.encode()).hexdigest()
    processes = []
    bootstrap(str(SOCKET), PORT, database)
    try:
        # Fixture 4 starts Failed so the retry form is exercised through the UI.
        claims = ClaimStore(Settings(pg_host=str(SOCKET), pg_port=PORT, pg_database=database), "europe")
        for _ in range(3):
            attempt = claims.claim_next("browser-fixture")
            assert attempt["event_id"] == UUID("00000001-0000-4000-8000-000000000004")
            claims.fail(attempt["event_id"], attempt["id"], attempt["worker_id"], "Browser retry fixture")
        run(["npm", "--prefix", "frontend", "run", "build"])
        evidence = ROOT / ".work" / database
        evidence.mkdir(parents=True)
        with socket.socket() as listener, (evidence / "server.log").open("w") as log:
            # Reserve an unused port and pass that socket to our server. A test
            # must never accidentally send writes to an existing development app.
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            url = f"http://127.0.0.1:{listener.getsockname()[1]}"
            command = [sys.executable, "-m", "uvicorn", "event_review.app:create_app", "--factory",
                       "--fd", str(listener.fileno())]
            processes.append(subprocess.Popen(command, cwd=ROOT, env=api_env, stdout=log, stderr=log,
                                               pass_fds=(listener.fileno(),)))
            processes.append(subprocess.Popen([sys.executable, "-m", "event_review.worker"],
                                               cwd=ROOT, env=env, stdout=log, stderr=log))
            for _ in range(100):
                if any(process.poll() is not None for process in processes):
                    raise RuntimeError(f"Browser fixture stopped. Inspect {evidence}/server.log")
                try:
                    with urlopen(url + "/api/health", timeout=1) as response:
                        if json.load(response)["status"] == "ok":
                            break
                except (URLError, TimeoutError):
                    time.sleep(0.1)
            else:
                raise RuntimeError("Browser fixture did not become ready.")
            browser_command = ["npm", "--prefix", "frontend", "run", "test:browser"]
            if pattern:
                browser_command.extend(["--", "--grep", pattern])
            run(browser_command, env=env | {
                "REVIEW_BASE_URL": url, "REVIEW_TEST_RETRY": "1",
                "REVIEW_TEST_FEEDBACK": "1", "REVIEW_SCREENSHOT_DIR": str(evidence),
                "REVIEW_TEST_PROVIDER_CONTROLS": "1" if provider_controls else "0",
                "REVIEW_TEST_ACCESS_CODE": test_code, "REVIEW_ACCESS_CODE_FILE": "",
            })
            print(f"Browser evidence: {evidence}")
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        with psycopg.connect(host=str(SOCKET), port=PORT, user="review_bootstrap", dbname="postgres", autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("db", "serve", "workers", "test", "browser"))
    parser.add_argument("--port", type=int, default=8116, help="API loopback port")
    parser.add_argument("--grep", help="Run only matching browser test titles")
    parser.add_argument("--provider-controls", action="store_true", help="Test provider selector storage; workers remain Mock-only")
    parser.add_argument("--private-preview", action="store_true", help="Exercise the access gate with a synthetic test code")
    args = parser.parse_args()
    start_database()
    if args.command == "serve":
        os.execvpe(sys.executable, [sys.executable, "-m", "uvicorn", "event_review.app:create_app", "--factory", "--host", "127.0.0.1", "--port", str(args.port), "--forwarded-allow-ips", "127.0.0.1"], environment())
    if args.command == "test":
        run([sys.executable, "-m", "pytest", "-c", "backend/pyproject.toml", "backend/tests"], env=environment())
    if args.command == "workers":
        os.execvpe(sys.executable, [sys.executable, "-m", "event_review.worker"], environment())
    if args.command == "browser":
        browser_tests(args.grep, args.provider_controls, args.private_preview)


if __name__ == "__main__":
    main()

