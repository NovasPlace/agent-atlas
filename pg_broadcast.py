"""pg_broadcast.py — Real-time cross-agent memory broadcast via PostgreSQL LISTEN/NOTIFY.

Every time the MemoryWriter commits a write, it calls pg_notify() here.
We do two things atomically:
  1. INSERT a row into agent_memory_events for durable replay
  2. NOTIFY on channel 'agent_memory_updates' for live subscribers

Subscribers (other agent sessions) call PGSubscriber.start() to receive events
in real-time without polling. This closes the cross-conversation context gap:
a write in Conversation A is visible to Conversation B within ~200ms.

Usage (sub-daemon, wired via agent_memory_daemon.py):
    from pg_broadcast import run_broadcast_daemon, get_pg_notifier
    notifier = get_pg_notifier(dsn)
    asyncio.create_task(run_broadcast_daemon(shutdown_event, dsn))

Usage (subscriber, in any agent session):
    from pg_broadcast import PGSubscriber
    sub = PGSubscriber(dsn)
    sub.start(callback=lambda ev: print(ev))
    # ... later ...
    sub.stop()

Usage (replay missed events):
    from pg_broadcast import get_events_since
    events = get_events_since(conn, since_id=42, limit=20)
"""
from __future__ import annotations

import asyncio
import json
import logging
import select
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

import psycopg2
import psycopg2.extensions

logger = logging.getLogger("pg-broadcast")

# ── Constants ──────────────────────────────────────────────

CHANNEL = "agent_memory_updates"
PG_DSN = "dbname=agent_ide user=frost"

# Maximum payload size for NOTIFY (Postgres limit is 8000 bytes)
MAX_NOTIFY_BYTES = 7500

# ── DB Setup ───────────────────────────────────────────────


def _get_pg_conn(dsn: str = PG_DSN) -> psycopg2.extensions.connection:
    """Return a psycopg2 connection in AUTOCOMMIT mode."""
    conn = psycopg2.connect(dsn)
    conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    return conn


def ensure_events_table(dsn: str = PG_DSN) -> None:
    """Create agent_memory_events table if it doesn't exist."""
    ddl = """
    CREATE TABLE IF NOT EXISTS agent_memory_events (
        id      SERIAL PRIMARY KEY,
        channel TEXT        NOT NULL DEFAULT 'agent_memory_updates',
        payload JSONB       NOT NULL,
        ts      TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS idx_agent_memory_events_ts
        ON agent_memory_events (ts DESC);
    """
    conn = psycopg2.connect(dsn)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(ddl)
        logger.info("agent_memory_events table ready")
    finally:
        conn.close()


# ── Notify (publisher side) ────────────────────────────────


def pg_notify(
    conn: psycopg2.extensions.connection,
    cmd: str,
    meta: dict[str, Any] | None = None,
    channel: str = CHANNEL,
) -> bool:
    """Publish a memory write event.

    Inserts a durable row into agent_memory_events, then issues NOTIFY.
    Both operations are on a single AUTOCOMMIT connection so there's no
    transaction to commit.

    Args:
        conn: AUTOCOMMIT psycopg2 connection.
        cmd:  The write command name (e.g. "APPEND_LESSON", "UPDATE_HOT").
        meta: Optional extra fields (slug, summary preview, etc.).
        channel: PG notify channel (default: agent_memory_updates).

    Returns True on success, False if the connection has gone stale.
    """
    payload: dict[str, Any] = {
        "cmd": cmd,
        "ts": datetime.now(tz=timezone.utc).isoformat(),
    }
    if meta:
        payload.update(meta)

    payload_json = json.dumps(payload)

    # Truncate if oversized (rare but possible for large warm-file updates)
    if len(payload_json.encode()) > MAX_NOTIFY_BYTES:
        payload["truncated"] = True
        payload.pop("content", None)
        payload_json = json.dumps(payload)

    try:
        with conn.cursor() as cur:
            # Durable row first
            cur.execute(
                "INSERT INTO agent_memory_events (channel, payload) VALUES (%s, %s::jsonb)",
                (channel, payload_json),
            )
            # Live notification
            cur.execute(
                "SELECT pg_notify(%s, %s)",
                (channel, payload_json),
            )
        return True
    except Exception as exc:
        logger.error("pg_notify failed (cmd=%s): %s", cmd, exc)
        return False


# ── Subscriber (listener side) ─────────────────────────────


class PGSubscriber:
    """Thread-based LISTEN subscriber for agent_memory_updates.

    Runs a blocking select() loop in a daemon thread.  Each received
    notification calls `callback(event_dict)` on that thread — keep the
    callback fast; offload heavy work to a queue if needed.

    Usage:
        sub = PGSubscriber()
        sub.start(callback=lambda ev: print("EVENT:", ev))
        # ... when done ...
        sub.stop()
    """

    def __init__(self, dsn: str = PG_DSN, channel: str = CHANNEL) -> None:
        self._dsn = dsn
        self._channel = channel
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._conn: psycopg2.extensions.connection | None = None

    def start(self, callback: Callable[[dict], None]) -> None:
        """Start listening in a background daemon thread."""
        if self._thread and self._thread.is_alive():
            logger.warning("PGSubscriber already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._listen_loop,
            args=(callback,),
            daemon=True,
            name="pg-subscriber",
        )
        self._thread.start()
        logger.info("PGSubscriber started (channel=%s)", self._channel)

    def stop(self) -> None:
        """Signal the listener thread to stop and wait for it."""
        self._stop_event.set()
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5.0)
        logger.info("PGSubscriber stopped")

    def _listen_loop(self, callback: Callable[[dict], None]) -> None:
        """Thread body: connect, LISTEN, call callback on each notification."""
        try:
            self._conn = _get_pg_conn(self._dsn)
        except Exception as exc:
            logger.error("PGSubscriber: failed to connect: %s", exc)
            return

        try:
            with self._conn.cursor() as cur:
                cur.execute(f"LISTEN {self._channel};")
            logger.debug("LISTEN %s registered", self._channel)

            while not self._stop_event.is_set():
                # select() with 1s timeout so we can check stop_event
                readable, _, _ = select.select([self._conn], [], [], 1.0)
                if not readable:
                    continue
                # Consume all pending notifications
                self._conn.poll()
                while self._conn.notifies:
                    notify = self._conn.notifies.pop(0)
                    try:
                        event = json.loads(notify.payload)
                    except json.JSONDecodeError:
                        event = {"raw": notify.payload}
                    try:
                        callback(event)
                    except Exception as cb_exc:
                        logger.error("Subscriber callback error: %s", cb_exc)
        except Exception as exc:
            if not self._stop_event.is_set():
                logger.error("PGSubscriber listen loop error: %s", exc)
        finally:
            try:
                self._conn.close()
            except Exception:
                pass


# ── Replay (pull missed events) ────────────────────────────


def get_events_since(
    since_id: int = 0,
    limit: int = 20,
    dsn: str = PG_DSN,
    channel: str = CHANNEL,
) -> list[dict]:
    """Return events from agent_memory_events newer than since_id.

    Designed for session-start catch-up: agents call this to learn what
    other conversations wrote while they were offline.

    Returns list of dicts with keys: id, channel, payload (dict), ts (str).
    """
    try:
        conn = psycopg2.connect(dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, channel, payload, ts
                    FROM   agent_memory_events
                    WHERE  id > %s AND channel = %s
                    ORDER  BY id ASC
                    LIMIT  %s
                    """,
                    (since_id, channel, limit),
                )
                rows = cur.fetchall()
        finally:
            conn.close()

        return [
            {
                "id": row[0],
                "channel": row[1],
                "payload": row[2],  # psycopg2 returns JSONB as dict
                "ts": row[3].isoformat() if row[3] else None,
            }
            for row in rows
        ]
    except Exception as exc:
        logger.error("get_events_since failed: %s", exc)
        return []


# ── Sub-daemon (wired into agent_memory_daemon.py) ─────────


class _BroadcastDaemon:
    """Holds a persistent AUTOCOMMIT connection for the publisher.

    Reconnects automatically if the connection goes stale.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: psycopg2.extensions.connection | None = None
        self._lock = threading.Lock()

    def _ensure_conn(self) -> psycopg2.extensions.connection:
        if self._conn is None or self._conn.closed:
            self._conn = _get_pg_conn(self._dsn)
        return self._conn

    def notify(self, cmd: str, meta: dict | None = None) -> bool:
        with self._lock:
            try:
                conn = self._ensure_conn()
                return pg_notify(conn, cmd, meta)
            except Exception as exc:
                logger.error("BroadcastDaemon.notify error: %s — reconnecting", exc)
                self._conn = None
                return False

    def close(self) -> None:
        if self._conn and not self._conn.closed:
            try:
                self._conn.close()
            except Exception:
                pass


# Module-level singleton so md_writer can import get_pg_notifier()
_daemon_instance: _BroadcastDaemon | None = None


def get_pg_notifier(dsn: str = PG_DSN) -> _BroadcastDaemon:
    """Return the module-level BroadcastDaemon, creating it if needed."""
    global _daemon_instance
    if _daemon_instance is None:
        _daemon_instance = _BroadcastDaemon(dsn)
    return _daemon_instance


async def run_broadcast_daemon(
    shutdown_event: asyncio.Event,
    dsn: str = PG_DSN,
) -> None:
    """Asyncio coroutine that bootstraps the broadcast daemon.

    Called by agent_memory_daemon.py. Ensures the events table exists,
    creates the module-level notifier, and holds until shutdown.
    """
    logger.info("BroadcastDaemon starting (dsn=%s)...", dsn)
    try:
        ensure_events_table(dsn)
        get_pg_notifier(dsn)  # Warm up the singleton
        logger.info("BroadcastDaemon ready — pg_notify() available to md_writer")
    except Exception as exc:
        logger.error("BroadcastDaemon failed to start: %s", exc)
        return

    # Hold until shutdown; the actual work happens in pg_notify() callbacks
    await shutdown_event.wait()

    if _daemon_instance:
        _daemon_instance.close()
    logger.info("BroadcastDaemon stopped")
