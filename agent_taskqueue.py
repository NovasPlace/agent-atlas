"""AgentTaskQueueDaemon — Persistent autonomous task queue.

Agents push deferred work here. At session start, agents pull
the next pending task instead of starting cold or losing deferred items.

Supports:
  - Priority ordering (1=highest, 10=lowest)
  - Deferred execution (run_after timestamp)
  - Recurring tasks (recur_s > 0 → re-queues itself after done)

Socket:  /tmp/agent-taskqueue.sock
Storage: ~/.gemini/memory/taskqueue.db (SQLite)

Protocol (newline-delimited JSON):
  PUSH   {title, owner?, priority?, run_after?, recur_s?}
  NEXT   {owner?}          → next due task for owner (or any)
  DONE   {task_id}         → mark done; re-queue if recurring
  CANCEL {task_id}         → cancel pending task
  LIST   {owner?, status?} → list tasks
  PING   {}                → health check

Usage (daemon integration):
    from agent_taskqueue import run_taskqueue_daemon
    await run_taskqueue_daemon(shutdown_event)

Usage (self-test):
    python3 agent_taskqueue.py --test-mode
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
import uuid
from pathlib import Path

logger = logging.getLogger("agent-taskqueue")

SOCKET_PATH = "/tmp/agent-taskqueue.sock"
DB_PATH     = os.path.expanduser("~/.gemini/memory/taskqueue.db")
MAX_MSG_BYTES = 32_768

# Seed tasks on first boot
_SEED_TASKS = [
    {
        "title": "Compact memory (hot.md + CortexDB decay)",
        "owner": "any",
        "priority": 8,
        "recur_s": 86_400,   # Daily
    },
]


# ── Database Layer ────────────────────────────────────────────

def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id        TEXT PRIMARY KEY,
            title     TEXT NOT NULL,
            owner     TEXT NOT NULL DEFAULT 'any',
            priority  INTEGER NOT NULL DEFAULT 5,
            status    TEXT NOT NULL DEFAULT 'pending',
            run_after INTEGER NOT NULL DEFAULT 0,
            recur_s   INTEGER NOT NULL DEFAULT 0,
            created   INTEGER NOT NULL,
            done_at   INTEGER
        )
    """)
    conn.commit()


def _seed_defaults(conn: sqlite3.Connection) -> None:
    """Insert seed tasks only if the table is empty."""
    row = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
    if row[0] > 0:
        return
    now = int(time.time())
    for t in _SEED_TASKS:
        conn.execute(
            "INSERT INTO tasks (id, title, owner, priority, run_after, recur_s, created) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4())[:8],
                t["title"],
                t.get("owner", "any"),
                t.get("priority", 5),
                t.get("run_after", 0),
                t.get("recur_s", 0),
                now,
            ),
        )
    conn.commit()
    logger.info("Seeded %d default task(s)", len(_SEED_TASKS))


_CTRL_RE = __import__("re").compile(r"[\x00-\x1f\x7f]")

def _sanitize_str(raw: str, max_len: int = 500) -> str:
    """Strip null bytes, control characters, and truncate."""
    return _CTRL_RE.sub("", raw).strip()[:max_len]


def _push(conn: sqlite3.Connection, title: str, owner: str, priority: int,
          run_after: int, recur_s: int) -> dict:
    title = _sanitize_str(title)
    owner = _sanitize_str(owner, 100) or "any"
    if not title.strip():
        return {"ok": False, "error": "title cannot be empty"}
    priority = max(1, min(10, int(priority)))
    task_id = str(uuid.uuid4())[:8]
    conn.execute(
        "INSERT INTO tasks (id, title, owner, priority, run_after, recur_s, created) "
        "VALUES (?,?,?,?,?,?,?)",
        (task_id, title.strip()[:500], owner.strip() or "any",
         priority, max(0, int(run_after)), max(0, int(recur_s)), int(time.time())),
    )
    conn.commit()
    logger.info("Task pushed: [%s] %s (owner=%s, pri=%d)", task_id, title[:50], owner, priority)
    return {"ok": True, "task_id": task_id}


def _next(conn: sqlite3.Connection, owner: str) -> dict:
    now = int(time.time())
    if owner and owner != "any":
        row = conn.execute(
            "SELECT * FROM tasks WHERE status='pending' AND run_after<=? "
            "AND (owner=? OR owner='any') ORDER BY priority ASC, created ASC LIMIT 1",
            (now, owner),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM tasks WHERE status='pending' AND run_after<=? "
            "ORDER BY priority ASC, created ASC LIMIT 1",
            (now,),
        ).fetchone()
    if not row:
        return {"ok": True, "task": None}
    return {"ok": True, "task": dict(row)}


def _done(conn: sqlite3.Connection, task_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM tasks WHERE id=? AND status='pending'", (task_id,)
    ).fetchone()
    if not row:
        return {"ok": False, "error": f"Task {task_id!r} not found or not pending"}

    conn.execute(
        "UPDATE tasks SET status='done', done_at=? WHERE id=?",
        (int(time.time()), task_id),
    )

    # Re-queue recurring tasks
    if row["recur_s"] > 0:
        new_id = str(uuid.uuid4())[:8]
        conn.execute(
            "INSERT INTO tasks (id, title, owner, priority, run_after, recur_s, created) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                new_id, row["title"], row["owner"], row["priority"],
                int(time.time()) + row["recur_s"], row["recur_s"],
                int(time.time()),
            ),
        )
        conn.commit()
        logger.info("Task done + re-queued: [%s] → [%s] in %ds", task_id, new_id, row["recur_s"])
        return {"ok": True, "requeued_as": new_id}

    conn.commit()
    logger.info("Task done: [%s]", task_id)
    return {"ok": True}


def _cancel(conn: sqlite3.Connection, task_id: str) -> dict:
    result = conn.execute(
        "UPDATE tasks SET status='cancelled' WHERE id=? AND status='pending'",
        (task_id,),
    )
    conn.commit()
    if result.rowcount == 0:
        return {"ok": False, "error": f"Task {task_id!r} not found or not pending"}
    logger.info("Task cancelled: [%s]", task_id)
    return {"ok": True}


def _list_tasks(conn: sqlite3.Connection, owner: str, status: str) -> dict:
    clauses = []
    params: list = []
    if status:
        clauses.append("status=?")
        params.append(status)
    if owner and owner != "any":
        clauses.append("(owner=? OR owner='any')")
        params.append(owner)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM tasks {where} ORDER BY priority ASC, created ASC LIMIT 100",
        params,
    ).fetchall()
    return {"ok": True, "tasks": [dict(r) for r in rows]}


# ── Command Dispatch ──────────────────────────────────────────

async def _handle_command(cmd_obj: dict, conn: sqlite3.Connection) -> dict:
    cmd = cmd_obj.get("cmd", "")

    if cmd == "PING":
        count = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='pending'").fetchone()[0]
        return {"ok": True, "pong": True, "pending": count}

    if cmd == "PUSH":
        return _push(
            conn,
            title=str(cmd_obj.get("title", "")),
            owner=str(cmd_obj.get("owner", "any")),
            priority=int(cmd_obj.get("priority", 5)),
            run_after=int(cmd_obj.get("run_after", 0)),
            recur_s=int(cmd_obj.get("recur_s", 0)),
        )

    if cmd == "NEXT":
        return _next(conn, owner=str(cmd_obj.get("owner", "")))

    if cmd == "DONE":
        task_id = str(cmd_obj.get("task_id", "")).strip()
        if not task_id:
            return {"ok": False, "error": "task_id required"}
        return _done(conn, task_id)

    if cmd == "CANCEL":
        task_id = str(cmd_obj.get("task_id", "")).strip()
        if not task_id:
            return {"ok": False, "error": "task_id required"}
        return _cancel(conn, task_id)

    if cmd == "LIST":
        return _list_tasks(
            conn,
            owner=str(cmd_obj.get("owner", "")),
            status=str(cmd_obj.get("status", "")),
        )

    return {"ok": False, "error": f"Unknown command: {cmd!r}"}


# ── Connection Handler ────────────────────────────────────────

async def _handle_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    conn: sqlite3.Connection,
) -> None:
    try:
        raw = await asyncio.wait_for(reader.read(MAX_MSG_BYTES), timeout=5.0)
        if not raw:
            return
        try:
            cmd_obj = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as e:
            response = {"ok": False, "error": f"JSON parse error: {e}"}
        else:
            response = await _handle_command(cmd_obj, conn)

        writer.write(json.dumps(response).encode("utf-8") + b"\n")
        await writer.drain()
    except asyncio.TimeoutError:
        pass
    except Exception as e:
        logger.error("Connection error: %s", e)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


# ── Daemon Entry ─────────────────────────────────────────────

async def run_taskqueue_daemon(
    shutdown_event: asyncio.Event | None = None,
    socket_path: str = SOCKET_PATH,
    db_path: str = DB_PATH,
) -> None:
    conn = _connect(db_path)
    _init_db(conn)
    _seed_defaults(conn)

    _shutdown = shutdown_event or asyncio.Event()

    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass

    def _cb(r, w):
        asyncio.ensure_future(_handle_connection(r, w, conn))

    server = await asyncio.start_unix_server(_cb, path=socket_path)
    os.chmod(socket_path, 0o600)  # Owner-only
    logger.info("AgentTaskQueueDaemon listening on %s (db=%s)", socket_path, db_path)

    await _shutdown.wait()

    server.close()
    await server.wait_closed()
    conn.close()
    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass
    logger.info("AgentTaskQueueDaemon stopped.")


# ── Self-Test ─────────────────────────────────────────────────

async def _self_test() -> bool:
    import tempfile
    logger.info("Running AgentTaskQueueDaemon self-test...")

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    sock = "/tmp/agent-taskqueue-test.sock"

    shutdown = asyncio.Event()
    server_task = asyncio.create_task(
        run_taskqueue_daemon(shutdown, sock, db_path)
    )
    await asyncio.sleep(0.1)

    async def _call(payload: dict) -> dict:
        r, w = await asyncio.open_unix_connection(sock)
        w.write(json.dumps(payload).encode() + b"\n")
        await w.drain()
        raw = await r.read(MAX_MSG_BYTES)
        w.close()
        await w.wait_closed()
        return json.loads(raw.decode())

    try:
        # PING
        resp = await _call({"cmd": "PING"})
        assert resp["ok"] and resp["pong"], f"PING failed: {resp}"

        # PUSH
        resp = await _call({"cmd": "PUSH", "title": "Write migration tests", "priority": 3})
        assert resp["ok"], f"PUSH failed: {resp}"
        task_id = resp["task_id"]

        # NEXT
        resp = await _call({"cmd": "NEXT"})
        assert resp["ok"] and resp["task"] is not None, f"NEXT failed: {resp}"
        assert resp["task"]["id"] == task_id, "Wrong task returned"

        # DONE
        resp = await _call({"cmd": "DONE", "task_id": task_id})
        assert resp["ok"], f"DONE failed: {resp}"

        # LIST — should be done now
        resp = await _call({"cmd": "LIST", "status": "done"})
        assert resp["ok"] and any(t["id"] == task_id for t in resp["tasks"]), f"LIST failed: {resp}"

        # Recurring task
        resp = await _call({"cmd": "PUSH", "title": "Recurring test", "recur_s": 3600})
        assert resp["ok"], f"Recurring PUSH failed: {resp}"
        rid = resp["task_id"]
        resp = await _call({"cmd": "DONE", "task_id": rid})
        assert resp["ok"] and "requeued_as" in resp, f"Recurring DONE failed: {resp}"

        # CANCEL
        new_rid = resp["requeued_as"]
        resp = await _call({"cmd": "CANCEL", "task_id": new_rid})
        assert resp["ok"], f"CANCEL failed: {resp}"

        # NEXT on empty (only seed tasks left — may or may not be due)
        resp = await _call({"cmd": "NEXT", "owner": "nonexistent-agent"})
        assert resp["ok"], f"NEXT empty failed: {resp}"

        logger.info("AgentTaskQueueDaemon self-test PASSED")
        return True

    except Exception as e:
        logger.error("AgentTaskQueueDaemon self-test FAILED: %s", e)
        import traceback; traceback.print_exc()
        return False
    finally:
        shutdown.set()
        await server_task
        try:
            os.unlink(db_path)
        except OSError:
            pass


# ── CLI ──────────────────────────────────────────────────────

def main() -> None:
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="AgentTaskQueueDaemon")
    parser.add_argument("--test-mode", action="store_true")
    parser.add_argument("--socket", default=SOCKET_PATH)
    parser.add_argument("--db", default=DB_PATH)
    args = parser.parse_args()

    if args.test_mode:
        success = asyncio.run(_self_test())
        raise SystemExit(0 if success else 1)

    asyncio.run(run_taskqueue_daemon(socket_path=args.socket, db_path=args.db))


if __name__ == "__main__":
    main()
