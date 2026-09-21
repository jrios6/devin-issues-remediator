import sqlite3
import threading
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS remediations (
    issue_number   INTEGER PRIMARY KEY,
    issue_title    TEXT NOT NULL,
    issue_url      TEXT NOT NULL,
    state          TEXT NOT NULL,           -- queued | running | succeeded | failed
    session_id     TEXT,
    session_url    TEXT,
    pr_url         TEXT,
    detail         TEXT,
    created_at     REAL NOT NULL,
    dispatched_at  REAL,
    completed_at   REAL
);
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    issue_number INTEGER,
    kind         TEXT NOT NULL,             -- detected | dispatched | status | pr_opened | failed
    message      TEXT
);
"""


class Store:
    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        con = sqlite3.connect(self._path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def event(self, kind: str, issue_number: int | None, message: str = ""):
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO events(ts, issue_number, kind, message) VALUES (?,?,?,?)",
                (time.time(), issue_number, kind, message),
            )

    def upsert_queued(self, issue_number: int, title: str, url: str) -> bool:
        """Insert as queued if unknown. Returns True if this issue is new."""
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT state FROM remediations WHERE issue_number=?", (issue_number,)
            ).fetchone()
            if row:
                return False
            c.execute(
                "INSERT INTO remediations(issue_number, issue_title, issue_url, state, created_at)"
                " VALUES (?,?,?,?,?)",
                (issue_number, title, url, "queued", time.time()),
            )
            return True

    def mark_dispatched(self, issue_number: int, session_id: str, session_url: str):
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE remediations SET state='running', session_id=?, session_url=?,"
                " dispatched_at=? WHERE issue_number=?",
                (session_id, session_url, time.time(), issue_number),
            )

    def mark_running(self, issue_number: int, detail: str):
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE remediations SET detail=? WHERE issue_number=? AND state='running'",
                (detail, issue_number),
            )

    def mark_completed(self, issue_number: int, state: str, pr_url: str | None, detail: str):
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE remediations SET state=?, pr_url=?, detail=?, completed_at=?"
                " WHERE issue_number=?",
                (state, pr_url, detail, time.time(), issue_number),
            )

    def active(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM remediations WHERE state IN ('queued','running')")]

    def all(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM remediations ORDER BY issue_number")]

    def recent_events(self, limit: int = 100) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]

    def counts(self) -> dict:
        with self._conn() as c:
            rows = c.execute(
                "SELECT state, COUNT(*) n FROM remediations GROUP BY state").fetchall()
            by_state = {r["state"]: r["n"] for r in rows}
            events = c.execute(
                "SELECT kind, COUNT(*) n FROM events GROUP BY kind").fetchall()
            by_kind = {r["kind"]: r["n"] for r in events}
            dur = c.execute(
                "SELECT AVG(completed_at - dispatched_at) avg_s,"
                " MIN(completed_at - dispatched_at) min_s,"
                " MAX(completed_at - dispatched_at) max_s"
                " FROM remediations WHERE completed_at IS NOT NULL AND dispatched_at IS NOT NULL"
            ).fetchone()
            return {"by_state": by_state, "by_kind": by_kind,
                    "durations": dict(dur) if dur else {}}
