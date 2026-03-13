"""AgentMsgQueueDaemon — Async agent-to-agent message passing.

Provides a persistent, SQLite-backed inbox/outbox per agent. Enables
true async request/reply between agent sessions:

  Agent A (session 1):
    SEND {to: "agent-b", subject: "review hot.md", body: "..."}

  Agent B (session 2, hours later):
    RECV {agent_id: "agent-b"}  → gets the message, can reply

Messages are kept for 48h then auto-expired.
Replies link to their parent message via reply_to field.

Socket: /tmp/agent-msgqueue.sock
DB:     ~/.gemini/memory/agent_msgqueue.db

Protocol (newline-delimited JSON):
  SEND  {from, to, subject, body, reply_to?}  → {ok, msg_id}
  RECV  {agent_id, limit?}                    → {ok, messages: [...]}
  ACK   {msg_id}                              → mark read/done
  LIST  {agent_id}                            → all unread messages
  PING  {}
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

logger = logging.getLogger("agent-msgqueue")

SOCKET_PATH  = "/tmp/agent-msgqueue.sock"
MQ_DB        = os.path.expanduser("~/.gemini/memory/agent_msgqueue.db")
MSG_TTL_S    = 172_800   # 48 hours
MAX_MSG_BYTES = 65_536
MAX_BODY_LEN  = 4_000


# ── DB ──────────────────────────────────────────────────────

def _open_db(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id         TEXT PRIMARY KEY,
            from_agent TEXT NOT NULL,
            to_agent   TEXT NOT NULL,
            subject    TEXT NOT NULL,
            body       TEXT NOT NULL,
            reply_to   TEXT DEFAULT '',
            status     TEXT DEFAULT 'unread',
            created_at INTEGER NOT NULL,
            read_at    INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    return conn


def _expire_old(conn: sqlite3.Connection) -> int:
    cutoff = int(time.time()) - MSG_TTL_S
    cur = conn.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


def _send(conn: sqlite3.Connection, from_agent: str, to_agent: str,
          subject: str, body: str, reply_to: str = "") -> str:
    msg_id = uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO messages (id, from_agent, to_agent, subject, body, reply_to, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (msg_id, from_agent, to_agent, subject[:200], body[:MAX_BODY_LEN], reply_to, int(time.time())),
    )
    conn.commit()
    return msg_id


def _recv(conn: sqlite3.Connection, agent_id: str, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT id, from_agent, subject, body, reply_to, created_at FROM messages "
        "WHERE to_agent=? AND status='unread' ORDER BY created_at ASC LIMIT ?",
        (agent_id, limit),
    ).fetchall()
    return [
        {"id": r[0], "from": r[1], "subject": r[2],
         "body": r[3], "reply_to": r[4], "created_at": r[5]}
        for r in rows
    ]


def _ack(conn: sqlite3.Connection, msg_id: str) -> bool:
    cur = conn.execute(
        "UPDATE messages SET status='read', read_at=? WHERE id=?",
        (int(time.time()), msg_id),
    )
    conn.commit()
    return cur.rowcount > 0


def _list_all(conn: sqlite3.Connection, agent_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, from_agent, subject, status, created_at FROM messages "
        "WHERE to_agent=? ORDER BY created_at DESC LIMIT 50",
        (agent_id,),
    ).fetchall()
    return [
        {"id": r[0], "from": r[1], "subject": r[2], "status": r[3], "created_at": r[4]}
        for r in rows
    ]


# ── Input sanitization ──────────────────────────────────────

import re as _re
_CTRL = _re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

def _clean(s: str, max_len: int = 200) -> str:
    return _CTRL.sub("", str(s)).strip()[:max_len]


# ── Command Handler ─────────────────────────────────────────

async def _handle_command(cmd_obj: dict, conn: sqlite3.Connection) -> dict:
    cmd = cmd_obj.get("cmd", "")

    if cmd == "PING":
        return {"ok": True, "pong": True}

    if cmd == "SEND":
        from_agent = _clean(cmd_obj.get("from", ""))
        to_agent   = _clean(cmd_obj.get("to", ""))
        subject    = _clean(cmd_obj.get("subject", ""), 200)
        body       = _clean(cmd_obj.get("body", ""), MAX_BODY_LEN)
        reply_to   = _clean(cmd_obj.get("reply_to", ""), 12)

        if not from_agent or not to_agent:
            return {"ok": False, "error": "from and to are required"}
        if not subject:
            return {"ok": False, "error": "subject is required"}

        msg_id = _send(conn, from_agent, to_agent, subject, body, reply_to)
        logger.info("MSG [%s→%s] %s: %s", from_agent, to_agent, msg_id, subject[:40])
        return {"ok": True, "msg_id": msg_id}

    if cmd == "RECV":
        agent_id = _clean(cmd_obj.get("agent_id", ""))
        limit    = min(int(cmd_obj.get("limit", 10)), 50)
        if not agent_id:
            return {"ok": False, "error": "agent_id required"}
        messages = _recv(conn, agent_id, limit)
        return {"ok": True, "messages": messages, "count": len(messages)}

    if cmd == "ACK":
        msg_id = _clean(cmd_obj.get("msg_id", ""), 12)
        if not msg_id:
            return {"ok": False, "error": "msg_id required"}
        ok = _ack(conn, msg_id)
        return {"ok": ok, "error": "message not found" if not ok else None}

    if cmd == "LIST":
        agent_id = _clean(cmd_obj.get("agent_id", ""))
        if not agent_id:
            return {"ok": False, "error": "agent_id required"}
        return {"ok": True, "messages": _list_all(conn, agent_id)}

    return {"ok": False, "error": f"Unknown command: {cmd!r}"}


# ── Connection / Expiry Loop / Daemon ───────────────────────

async def _handle_connection(reader, writer, conn) -> None:
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
        writer.write(json.dumps(response).encode() + b"\n")
        await writer.drain()
    except asyncio.TimeoutError:
        pass
    except Exception as e:
        logger.error("Connection error: %s", e)
    finally:
        try:
            writer.close(); await writer.wait_closed()
        except Exception:
            pass


async def _expiry_loop(conn: sqlite3.Connection, shutdown: asyncio.Event) -> None:
    while not shutdown.is_set():
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=3600.0)
            break
        except asyncio.TimeoutError:
            expired = _expire_old(conn)
            if expired:
                logger.info("Expired %d old message(s)", expired)


async def run_msgqueue(
    shutdown_event: asyncio.Event | None = None,
    socket_path: str = SOCKET_PATH,
    db_path: str = MQ_DB,
) -> None:
    conn      = _open_db(db_path)
    _shutdown = shutdown_event or asyncio.Event()

    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass

    def _cb(r, w):
        asyncio.ensure_future(_handle_connection(r, w, conn))

    server = await asyncio.start_unix_server(_cb, path=socket_path)
    os.chmod(socket_path, 0o600)
    logger.info("AgentMsgQueueDaemon listening on %s", socket_path)

    expiry = asyncio.create_task(_expiry_loop(conn, _shutdown))
    await _shutdown.wait()
    expiry.cancel()
    server.close()
    await server.wait_closed()
    conn.close()
    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass
    logger.info("AgentMsgQueueDaemon stopped.")


# ── Self-Test ───────────────────────────────────────────────

async def _self_test() -> bool:
    import tempfile
    logger.info("Running AgentMsgQueueDaemon self-test...")
    sock = "/tmp/agent-msgqueue-test.sock"
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    shutdown = asyncio.Event()
    task = asyncio.create_task(run_msgqueue(shutdown, sock, db_path))
    await asyncio.sleep(0.1)

    async def _call(payload: dict) -> dict:
        r, w = await asyncio.open_unix_connection(sock)
        w.write(json.dumps(payload).encode() + b"\n")
        await w.drain()
        raw = await r.read(MAX_MSG_BYTES)
        w.close(); await w.wait_closed()
        return json.loads(raw.decode())

    try:
        assert (await _call({"cmd": "PING"}))["pong"]

        # Send a message
        resp = await _call({"cmd": "SEND", "from": "agent-a", "to": "agent-b",
                            "subject": "Hey B", "body": "Can you check hot.md?"})
        assert resp["ok"], f"SEND failed: {resp}"
        msg_id = resp["msg_id"]

        # Recv — agent-b gets it
        resp = await _call({"cmd": "RECV", "agent_id": "agent-b"})
        assert resp["ok"] and resp["count"] == 1, f"RECV failed: {resp}"
        assert resp["messages"][0]["subject"] == "Hey B"

        # ACK — mark done
        assert (await _call({"cmd": "ACK", "msg_id": msg_id}))["ok"]

        # RECV again — inbox empty
        resp = await _call({"cmd": "RECV", "agent_id": "agent-b"})
        assert resp["count"] == 0, "Inbox should be empty after ACK"

        # LIST shows read messages too
        resp = await _call({"cmd": "LIST", "agent_id": "agent-b"})
        assert any(m["id"] == msg_id for m in resp["messages"])

        # Control-char injection
        resp = await _call({"cmd": "SEND", "from": "evil\x00", "to": "agent-b",
                            "subject": "attack\x01\x02", "body": "payload"})
        assert resp["ok"]  # Accepted but sanitized
        msgs = (await _call({"cmd": "RECV", "agent_id": "agent-b"}))["messages"]
        assert "\x00" not in msgs[0]["from"], "Null byte leaked into from field"

        logger.info("AgentMsgQueueDaemon self-test PASSED")
        return True
    except Exception as e:
        logger.error("AgentMsgQueueDaemon self-test FAILED: %s", e)
        import traceback; traceback.print_exc()
        return False
    finally:
        shutdown.set(); await task
        try:
            os.unlink(db_path)
        except OSError:
            pass


# ── CLI ─────────────────────────────────────────────────────

def main() -> None:
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="AgentMsgQueueDaemon")
    parser.add_argument("--test-mode", action="store_true")
    parser.add_argument("--socket", default=SOCKET_PATH)
    parser.add_argument("--db",     default=MQ_DB)
    args = parser.parse_args()
    if args.test_mode:
        raise SystemExit(0 if asyncio.run(_self_test()) else 1)
    asyncio.run(run_msgqueue(socket_path=args.socket, db_path=args.db))


if __name__ == "__main__":
    main()
