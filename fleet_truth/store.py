"""SQLite store (design section 5): WAL, user_version migrations, rebuildable.

Losing the file loses history and alert de-duplication only; `fleet-truth once` recreates it.
detail and data_json are capped at 2 KB and never hold headers, tokens or response bodies.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from .checks.base import CheckResult

RETENTION_DAYS = {"results": 30, "runs": 90, "alerts": 365}
MAX_JSON = 2048

MIGRATIONS = [
    # version 1
    """
    CREATE TABLE runs (
      id INTEGER PRIMARY KEY, started_at INTEGER NOT NULL, finished_at INTEGER, ok INTEGER, error TEXT);
    CREATE TABLE results (
      run_id INTEGER NOT NULL REFERENCES runs(id), app TEXT NOT NULL, check_name TEXT NOT NULL,
      status TEXT NOT NULL CHECK (status IN ('ok','info','warn','crit','unknown')),
      verdict TEXT NOT NULL, detail TEXT NOT NULL, data_json TEXT NOT NULL DEFAULT '{}',
      observed_at INTEGER NOT NULL, PRIMARY KEY (run_id, app, check_name));
    CREATE TABLE check_state (
      app TEXT NOT NULL, check_name TEXT NOT NULL, status TEXT NOT NULL, verdict TEXT NOT NULL,
      since INTEGER NOT NULL, consecutive INTEGER NOT NULL, alerted_status TEXT,
      last_alert_at INTEGER, silenced_until INTEGER, PRIMARY KEY (app, check_name));
    CREATE TABLE alerts (
      id INTEGER PRIMARY KEY, app TEXT, check_name TEXT, status TEXT, channel TEXT NOT NULL,
      sent_at INTEGER NOT NULL, ok INTEGER NOT NULL, error TEXT);
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE INDEX results_observed ON results(observed_at);
    """,
    # version 2: episode tracking (an unhealthy episode = a run of non-ok/info results, whatever their mix)
    """
    ALTER TABLE check_state ADD COLUMN unhealthy_since INTEGER;
    ALTER TABLE check_state ADD COLUMN unhealthy_cycles INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE check_state ADD COLUMN worst_status TEXT;
    ALTER TABLE check_state ADD COLUMN worst_verdict TEXT;
    """,
]


class StoreError(Exception):
    pass


class SchemaTooNew(StoreError):
    """The database was written by a newer build (a rollback). Not corruption: never move it aside."""


@dataclass(frozen=True)
class StateRow:
    app: str
    check: str
    status: str
    verdict: str
    since: int
    consecutive: int
    alerted_status: str | None = None
    last_alert_at: int | None = None
    silenced_until: int | None = None
    unhealthy_since: int | None = None   # start of the current episode (last time the check was ok/info)
    unhealthy_cycles: int = 0            # non-healthy cycles in the episode, any mix of crit/warn/unknown
    worst_status: str | None = None      # worst of crit > warn > unknown seen in the episode
    worst_verdict: str | None = None


def _row(r) -> "StateRow":
    return StateRow(r["app"], r["check_name"], r["status"], r["verdict"], r["since"], r["consecutive"],
                    r["alerted_status"], r["last_alert_at"], r["silenced_until"], r["unhealthy_since"],
                    r["unhealthy_cycles"], r["worst_status"], r["worst_verdict"])


def _cap_json(data: dict) -> str:
    s = json.dumps(data, sort_keys=True, default=str)
    if len(s) <= MAX_JSON:
        return s
    return json.dumps({"truncated": True})


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        try:
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            self.db.row_factory = sqlite3.Row
            self._check_not_newer()   # before any pragma that would rewrite the header of a newer build's file
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self._migrate()
        except sqlite3.DatabaseError as e:
            raise StoreError(f"database unusable ({type(e).__name__}); it is a rebuildable cache: "
                             "stop Fleet Truth and delete the file") from None

    def _check_not_newer(self) -> None:
        v = self.db.execute("PRAGMA user_version").fetchone()[0]
        if v > len(MIGRATIONS):
            raise SchemaTooNew(f"database schema {v} is newer than this build ({len(MIGRATIONS)}): run the newer build, "
                               "or move the file aside yourself to start fresh")

    def _migrate(self) -> None:
        v = self.db.execute("PRAGMA user_version").fetchone()[0]
        if v > len(MIGRATIONS):
            raise SchemaTooNew(f"database schema {v} is newer than this build ({len(MIGRATIONS)}): run the newer build, "
                             "or move the file aside yourself to start fresh")
        for n in range(v, len(MIGRATIONS)):
            self.db.executescript("BEGIN;" + MIGRATIONS[n] + f"PRAGMA user_version={n + 1};COMMIT;")

    def close(self) -> None:
        with self._lock:
            self.db.close()

    # ---- runs and results
    def start_run(self, now: float) -> int:
        with self._lock:
            return self.db.execute("INSERT INTO runs(started_at) VALUES (?)", (int(now),)).lastrowid

    def finish_run(self, run_id: int, now: float, ok: bool, error: str | None = None) -> None:
        with self._lock:
            self.db.execute("UPDATE runs SET finished_at=?, ok=?, error=? WHERE id=?",
                            (int(now), int(ok), (error or None) and error[:300], run_id))

    def add_result(self, run_id: int, app: str, check: str, r: CheckResult, observed_at: float) -> None:
        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO results(run_id,app,check_name,status,verdict,detail,data_json,observed_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (run_id, app, check, r.status, r.verdict, r.detail, _cap_json(r.data), int(observed_at)))

    def last_runs(self, n: int = 5) -> list[sqlite3.Row]:
        with self._lock:
            return self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (n,)).fetchall()

    def results_for_run(self, run_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self.db.execute("SELECT * FROM results WHERE run_id=? ORDER BY app, check_name", (run_id,)).fetchall()

    # ---- check_state
    def get_state(self, app: str, check: str) -> StateRow | None:
        with self._lock:
            r = self.db.execute("SELECT * FROM check_state WHERE app=? AND check_name=?", (app, check)).fetchone()
        return None if r is None else _row(r)

    def put_state(self, s: StateRow) -> None:
        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO check_state(app,check_name,status,verdict,since,consecutive,alerted_status,"
                "last_alert_at,silenced_until,unhealthy_since,unhealthy_cycles,worst_status,worst_verdict)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (s.app, s.check, s.status, s.verdict, s.since, s.consecutive, s.alerted_status, s.last_alert_at,
                 s.silenced_until, s.unhealthy_since, s.unhealthy_cycles, s.worst_status, s.worst_verdict))

    def all_states(self) -> list[StateRow]:
        with self._lock:
            rows = self.db.execute("SELECT * FROM check_state ORDER BY app, check_name").fetchall()
        return [_row(r) for r in rows]

    # ---- alerts
    def record_alert(self, app: str | None, check: str | None, status: str | None, channel: str, sent_at: float,
                     ok: bool, error: str | None = None) -> None:
        with self._lock:
            self.db.execute("INSERT INTO alerts(app,check_name,status,channel,sent_at,ok,error) VALUES (?,?,?,?,?,?,?)",
                            (app, check, status, channel, int(sent_at), int(ok), (error or "")[:300] or None))

    def last_alert_ok_by_channel(self) -> dict[str, bool]:
        with self._lock:
            rows = self.db.execute(
                "SELECT channel, ok FROM alerts WHERE id IN (SELECT MAX(id) FROM alerts GROUP BY channel)").fetchall()
        return {r["channel"]: bool(r["ok"]) for r in rows}

    # ---- meta
    def meta_get(self, key: str) -> str | None:
        with self._lock:
            r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if r is None else r["value"]

    def meta_set(self, key: str, value: str) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)", (key, value))

    # ---- retention
    def prune(self, now: float) -> dict[str, int]:
        day = 86400
        out = {}
        with self._lock:
            c = self.db.execute("DELETE FROM results WHERE observed_at < ?", (int(now - RETENTION_DAYS["results"] * day),))
            out["results"] = c.rowcount
            c = self.db.execute("DELETE FROM runs WHERE started_at < ? AND id NOT IN (SELECT DISTINCT run_id FROM results)",
                                (int(now - RETENTION_DAYS["runs"] * day),))
            out["runs"] = c.rowcount
            c = self.db.execute("DELETE FROM alerts WHERE sent_at < ?", (int(now - RETENTION_DAYS["alerts"] * day),))
            out["alerts"] = c.rowcount
        return out
