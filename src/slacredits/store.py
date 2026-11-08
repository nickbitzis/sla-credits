"""SQLite storage for the workflow state: claims, approvals, disputes, and the audit log.

Monitoring data and contracts are regenerated from the seed, so only
decisions people make are stored. The audit log is append-only (triggers).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    month TEXT NOT NULL,
    components TEXT NOT NULL,
    claimed_minutes INTEGER NOT NULL,
    submitted_on TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    measured_minutes INTEGER,
    credit INTEGER NOT NULL DEFAULT 0,
    currency TEXT NOT NULL,
    credit_usd_cents INTEGER NOT NULL DEFAULT 0,
    decided_on TEXT
);
CREATE INDEX IF NOT EXISTS claims_cm ON claims (customer_id, month);
CREATE TABLE IF NOT EXISTS approvals (
    claim_id TEXT NOT NULL, approver TEXT NOT NULL, role TEXT NOT NULL, on_date TEXT NOT NULL,
    PRIMARY KEY (claim_id, approver)
);
CREATE TABLE IF NOT EXISTS disputes (
    id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL UNIQUE,
    opened_on TEXT NOT NULL,
    status TEXT NOT NULL,
    claimed_minutes INTEGER NOT NULL,
    measured_minutes INTEGER NOT NULL,
    decision TEXT,
    goodwill_pct INTEGER,
    final_credit INTEGER,
    resolved_by TEXT,
    resolved_on TEXT,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    details TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
"""


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.executescript(SCHEMA)

    def q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def x(self, sql: str, args: tuple = ()) -> None:
        with self.lock:
            self.db.execute(sql, args)

    def tx(self):
        return _Tx(self)

    def get_meta(self, key: str) -> str | None:
        r = self.q("SELECT value FROM meta WHERE key = ?", (key,))
        return r[0]["value"] if r else None

    def set_meta(self, key: str, value: str) -> None:
        self.x("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def audit(self, actor: str, action: str, entity: str, ts: str | None = None, **details) -> None:
        ts = ts or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.x("INSERT INTO audit (ts, actor, action, entity, details) VALUES (?, ?, ?, ?, ?)",
               (ts, actor, action, entity, json.dumps(details, sort_keys=True, default=str)))

    def audit_log(self, entity: str | None = None, limit: int = 200) -> list[dict]:
        if entity:
            rows = self.q("SELECT * FROM audit WHERE entity = ? ORDER BY seq DESC LIMIT ?", (entity, limit))
        else:
            rows = self.q("SELECT * FROM audit ORDER BY seq DESC LIMIT ?", (limit,))
        for r in rows:
            r["details"] = json.loads(r["details"])
        return rows

    def next_id(self, prefix: str, table: str) -> str:
        n = self.q(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]  # table is a constant, never user input
        return f"{prefix}-{n + 1:05d}"


class _Tx:
    def __init__(self, store: Store):
        self.s = store

    def __enter__(self):
        self.s.lock.acquire()
        self.s.db.execute("BEGIN")
        return self.s

    def __exit__(self, exc_type, *_):
        try:
            self.s.db.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.s.lock.release()
        return False
