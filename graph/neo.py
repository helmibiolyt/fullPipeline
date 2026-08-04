"""One way to reach the graph, from a laptop or from the graph host.

Every read-only tool in here grew its own copy of this, and each one loaded
`testPipeline/.env` directly. That file is gitignored, so it does not exist on
the graph host - which is exactly where these tools most want to run, because
that is where the lake reads are fast. `unresolved_drugs.py` died on
`KeyError: 'NEO4J_URI'` there while trying to diagnose the largest remaining
gap in the graph.

So: the environment wins if it is set, the .env fills in when it exists, and
the error says what to do rather than raising KeyError on a name nobody has
seen before.
"""
from __future__ import annotations

import os
import pathlib

_ENV = (pathlib.Path(__file__).resolve().parent.parent
        / "testPipeline" / ".env")


def _load_env_file() -> None:
    """Fill in anything the environment did not already provide.

    Deliberately does not override: on the graph host the connection comes
    from the environment, and a stale .env copied there should not win.
    """
    if not _ENV.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(_ENV, override=False)
    except ImportError:
        for line in _ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def config() -> tuple[str, str, str, str]:
    """(uri, user, password, database), or a message saying what is missing."""
    _load_env_file()
    uri = os.getenv("NEO4J_URI")
    pwd = os.getenv("NEO4J_PASSWORD")
    if not uri or not pwd:
        raise SystemExit(
            "No Neo4j connection.\n"
            "  Set NEO4J_URI and NEO4J_PASSWORD in the environment:\n"
            "      NEO4J_URI=bolt://localhost:7687 "
            "NEO4J_PASSWORD=... python graph/<tool>.py\n"
            f"  or create {_ENV} (gitignored, so it is absent on the "
            "graph host by design).")
    return (uri, os.getenv("NEO4J_USER", "neo4j"), pwd,
            os.getenv("NEO4J_DATABASE", "biolyt"))


def driver():
    """A neo4j driver built from whichever source has the connection."""
    from neo4j import GraphDatabase
    uri, user, pwd, _ = config()
    return GraphDatabase.driver(uri, auth=(user, pwd))


def session():
    """Context manager over the configured database.

        with neo.session() as s:
            s.run(...)
    """
    _, _, _, db = config()
    return driver().session(database=db)
