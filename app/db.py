import sqlite3
import threading
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS remediations (
    issue_number   INTEGER PRIMARY KEY,
    issue_title    TEXT NOT NULL,
    issue_url      TEXT NOT NULL,
    state          TEXT NOT NULL,           -- queued | running | pr_opened | merged | failed
    session_id     TEXT,
    session_url    TEXT,
    pr_url         TEXT,
    pr_state       TEXT,                    -- open | merged | closed
    detail         TEXT,
    created_at     REAL NOT NULL,
    dispatched_at  REAL,
    completed_at   REAL,
    acus           REAL,                    -- ACUs consumed by the session so far
    devin_mode     TEXT,
    pr_additions   INTEGER,
    pr_deletions   INTEGER,
    pr_files       INTEGER,
    pr_checks      TEXT,                    -- passing | failing | pending
    pr_comments    INTEGER,
    pr_opened_at   REAL                     -- real PR created_at from GitHub (agent wall-clock)
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
            cols = {r["name"] for r in c.execute("PRAGMA table_info(remediations)")}
            for col, typ in ("pr_state", "TEXT"), ("acus", "REAL"), ("devin_mode", "TEXT"), \
                    ("pr_additions", "INTEGER"), ("pr_deletions", "INTEGER"), \
                    ("pr_files", "INTEGER"), ("pr_checks", "TEXT"), ("pr_comments", "INTEGER"), \
                    ("pr_opened_at", "REAL"):
                if col not in cols:
                    c.execute(f"ALTER TABLE remediations ADD COLUMN {col} {typ}")

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

    def set_pr_state(self, issue_number: int, pr_state: str, detail: str | None = None):
        with self._lock, self._conn() as c:
            if detail is None:
                c.execute("UPDATE remediations SET pr_state=? WHERE issue_number=?",
                          (pr_state, issue_number))
            else:
                c.execute("UPDATE remediations SET pr_state=?, detail=? WHERE issue_number=?",
                          (pr_state, detail, issue_number))

    def update_metrics(self, issue_number: int, **fields):
        """Refresh session/PR metrics (acus, devin_mode, pr_* stats)."""
        fields = {k: v for k, v in fields.items() if v is not None}
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._lock, self._conn() as c:
            c.execute(f"UPDATE remediations SET {sets} WHERE issue_number=?",
                      (*fields.values(), issue_number))

    def active(self) -> list[dict]:
        """Work still needing attention: sessions in flight, plus open PRs awaiting merge."""
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM remediations WHERE state IN ('queued','running')"
                " OR (state='pr_opened' AND (pr_state IS NULL OR pr_state='open'))")]

    def all(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM remediations ORDER BY issue_number")]

    def recent_events(self, limit: int = 100, offset: int = 0) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM events WHERE kind != 'config' "
                "ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))]

    def event_count(self) -> int:
        with self._conn() as c:
            return c.execute(
                "SELECT COUNT(*) n FROM events WHERE kind != 'config'").fetchone()["n"]

    def counts(self) -> dict:
        with self._conn() as c:
            rows = c.execute(
                "SELECT state, COUNT(*) n FROM remediations GROUP BY state").fetchall()
            by_state = {r["state"]: r["n"] for r in rows}
            events = c.execute(
                "SELECT kind, COUNT(*) n FROM events GROUP BY kind").fetchall()
            by_kind = {r["kind"]: r["n"] for r in events}
            dur = c.execute(
                "SELECT AVG(pr_opened_at - dispatched_at) avg_s,"
                " MIN(pr_opened_at - dispatched_at) min_s,"
                " MAX(pr_opened_at - dispatched_at) max_s"
                " FROM remediations WHERE pr_opened_at IS NOT NULL AND dispatched_at IS NOT NULL"
            ).fetchone()
            totals = c.execute(
                "SELECT COALESCE(SUM(acus),0) acus, COALESCE(SUM(pr_additions),0) additions,"
                " COALESCE(SUM(pr_deletions),0) deletions, COALESCE(SUM(pr_files),0) files,"
                " SUM(CASE WHEN pr_checks='passing' THEN 1 ELSE 0 END) ci_passing,"
                " SUM(CASE WHEN pr_checks='failing' THEN 1 ELSE 0 END) ci_failing"
                " FROM remediations").fetchone()
            return {"by_state": by_state, "by_kind": by_kind,
                    "durations": dict(dur) if dur else {},
                    "totals": dict(totals)}
