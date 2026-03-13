"""Trace — Execution observability for agent system components.

AgentTrace dataclass + @trace_execution decorator that wraps any
function, captures timing and I/O, and stores traces in CortexDB's
trace_ledger table (separate from memories to avoid polluting recall).

Usage:
    from cortex.trace import trace_execution, query_trace

    @trace_execution
    def my_handler(data):
        return process(data)

    # Later: retrieve traces for this session
    traces = query_trace(session_id)
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

DEFAULT_DB_PATH = os.path.expanduser("~/.cortexdb/agent_system.db")
DEFAULT_TRACE_DB_PATH = os.path.expanduser("~/.cortexdb/trace_ledger.db")

# Shared session ID for this process
_SESSION_ID: str = hashlib.sha256(
    f"{os.getpid()}-{time.time()}".encode()
).hexdigest()[:16]


# ── Schema ─────────────────────────────────────────────────

TRACE_SCHEMA = """
CREATE TABLE IF NOT EXISTS trace_ledger (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    timestamp REAL NOT NULL,
    target_function TEXT NOT NULL,
    input_payload TEXT DEFAULT '',
    output_payload TEXT DEFAULT '',
    execution_ms REAL NOT NULL DEFAULT 0.0,
    constraint_flag TEXT DEFAULT '',
    error TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_trace_session ON trace_ledger(session_id);
CREATE INDEX IF NOT EXISTS idx_trace_ts ON trace_ledger(timestamp);
CREATE INDEX IF NOT EXISTS idx_trace_fn ON trace_ledger(target_function);
"""


# ── AgentTrace ─────────────────────────────────────────────

@dataclass
class AgentTrace:
    """A single execution trace entry."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    session_id: str = field(default_factory=lambda: _SESSION_ID)
    timestamp: float = field(default_factory=time.time)
    target_function: str = ""
    input_payload: str = ""
    output_payload: str = ""
    execution_ms: float = 0.0
    constraint_flag: str = ""
    error: str = ""


# ── Ledger ─────────────────────────────────────────────────

class TraceLedger:
    """SQLite-backed execution ledger. Thread-safe.

    Uses a SEPARATE SQLite DB from CortexDB to guarantee that
    high-frequency telemetry writes never block cognitive or
    security writes on the main memory database.
    """

    def __init__(self, db_path: str = DEFAULT_TRACE_DB_PATH) -> None:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        # WAL + busy_timeout for concurrent access
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._lock:
            self._conn.executescript(TRACE_SCHEMA)

    def record(self, trace: AgentTrace) -> None:
        """Persist a trace entry."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO trace_ledger "
                "(id, session_id, timestamp, target_function, "
                "input_payload, output_payload, execution_ms, "
                "constraint_flag, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trace.id, trace.session_id, trace.timestamp,
                    trace.target_function, trace.input_payload,
                    trace.output_payload, trace.execution_ms,
                    trace.constraint_flag, trace.error,
                ),
            )
            self._conn.commit()

    def query(
        self,
        session_id: str | None = None,
        function_name: str | None = None,
        limit: int = 100,
    ) -> list[AgentTrace]:
        """Query traces by session and/or function name."""
        sql = "SELECT * FROM trace_ledger WHERE 1=1"
        params: list = []

        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        if function_name:
            sql += " AND target_function = ?"
            params.append(function_name)

        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()

        return [
            AgentTrace(
                id=r[0], session_id=r[1], timestamp=r[2],
                target_function=r[3], input_payload=r[4],
                output_payload=r[5], execution_ms=r[6],
                constraint_flag=r[7], error=r[8],
            )
            for r in rows
        ]

    def stats(self) -> dict[str, Any]:
        """Ledger statistics."""
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) FROM trace_ledger"
            ).fetchone()[0]
            sessions = self._conn.execute(
                "SELECT COUNT(DISTINCT session_id) FROM trace_ledger"
            ).fetchone()[0]
            avg_ms = self._conn.execute(
                "SELECT AVG(execution_ms) FROM trace_ledger"
            ).fetchone()[0] or 0.0
            errors = self._conn.execute(
                "SELECT COUNT(*) FROM trace_ledger WHERE error != ''"
            ).fetchone()[0]

        return {
            "total_traces": total,
            "unique_sessions": sessions,
            "avg_execution_ms": round(avg_ms, 2),
            "error_count": errors,
        }

    def close(self) -> None:
        """Close the database connection."""
        try:
            self._conn.close()
        except Exception:
            pass


# ── Singleton ──────────────────────────────────────────────

_ledger_instance: TraceLedger | None = None


def get_ledger(db_path: str = DEFAULT_TRACE_DB_PATH) -> TraceLedger:
    """Get or create the singleton TraceLedger."""
    global _ledger_instance
    if _ledger_instance is None:
        _ledger_instance = TraceLedger(db_path)
    return _ledger_instance


def get_session_id() -> str:
    """Current process session ID."""
    return _SESSION_ID


# ── Decorator ──────────────────────────────────────────────

def _safe_serialize(obj: Any, max_len: int = 1000) -> str:
    """Serialize an object to JSON string, truncated. Best-effort."""
    try:
        s = json.dumps(obj, default=str)
        return s[:max_len]
    except Exception:
        return str(obj)[:max_len]


def trace_execution(
    func: Callable | None = None,
    *,
    constraint_flag: str = "",
    db_path: str = DEFAULT_DB_PATH,
) -> Callable:
    """Decorator that traces function execution to the ledger.

    Usage:
        @trace_execution
        def my_handler(data):
            ...

        @trace_execution(constraint_flag="critical")
        def important_handler(data):
            ...
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            ledger = get_ledger(db_path)
            trace = AgentTrace(
                target_function=fn.__qualname__,
                input_payload=_safe_serialize(
                    {"args": args[1:], "kwargs": kwargs}  # Skip self
                    if args and hasattr(args[0], fn.__name__)
                    else {"args": args, "kwargs": kwargs}
                ),
                constraint_flag=constraint_flag,
            )

            start = time.perf_counter()
            try:
                result = fn(*args, **kwargs)
                trace.output_payload = _safe_serialize(result)
                trace.execution_ms = (time.perf_counter() - start) * 1000
                ledger.record(trace)
                return result
            except Exception as exc:
                trace.error = str(exc)[:500]
                trace.execution_ms = (time.perf_counter() - start) * 1000
                ledger.record(trace)
                raise

        return wrapper

    if func is not None:
        return decorator(func)
    return decorator


# ── Convenience ────────────────────────────────────────────

def query_trace(
    session_id: str | None = None,
    function_name: str | None = None,
    limit: int = 100,
    db_path: str = DEFAULT_DB_PATH,
) -> list[AgentTrace]:
    """Query traces from the ledger."""
    return get_ledger(db_path).query(session_id, function_name, limit)
