"""LoopDetectorDaemon — Detects and breaks agent repetition loops.

Agents record each significant tool call here. The daemon tracks
patterns per session and flags when the same tool+args_hash appears
N times consecutively — indicating the agent is stuck.

On detection:
  - Returns {"loop": true, "mayday": {...}} in the RECORD_CALL response
  - Auto-writes a lesson to hot.md via the md_writer daemon
  - Logs the loop event in its own SQLite ledger for post-session analysis

Socket: /tmp/agent-loop-detector.sock
Ledger: ~/.gemini/memory/loop_ledger.db

Protocol (newline-delimited JSON):
  RECORD_CALL  {session_id, tool, args_hash, detail?}
    → {"ok": true, "loop": false}
    → {"ok": true, "loop": true, "count": N, "mayday": {...}}

  STATUS       {session_id?}   → call counts for session (or all)
  RESET        {session_id}    → clear loop state for session
  PING         {}              → health check

Usage (daemon integration):
    from loop_detector import run_loop_detector
    await run_loop_detector(shutdown_event)

Usage (self-test):
    python3 loop_detector.py --test-mode

Agent usage (via agent_memory_api.py):
    api.record_call("build_toolbar", "abc123")
    # Returns {"loop": False} or {"loop": True, "mayday": {...}}
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("loop-detector")

SOCKET_PATH  = "/tmp/agent-loop-detector.sock"
LEDGER_DB    = os.path.expanduser("~/.gemini/memory/loop_ledger.db")
WRITER_SOCK  = "/tmp/agent-memory-writer.sock"
MAX_MSG_BYTES = 32_768

# Trigger: N consecutive identical (tool, args_hash) pairs
LOOP_THRESHOLD = 3
# Session state expires after this many seconds of silence
SESSION_TTL_S  = 1800   # 30 minutes


# ── Per-Session State ─────────────────────────────────────────

@dataclass
class SessionState:
    session_id: str
    # Ring buffer of (tool, args_hash) pairs
    recent: deque = field(default_factory=lambda: deque(maxlen=20))
    loop_flags: list[dict] = field(default_factory=list)
    last_seen: float = field(default_factory=time.monotonic)

    def record(self, tool: str, args_hash: str) -> int:
        """Record a call and return consecutive repeat count."""
        self.recent.append((tool, args_hash))
        self.last_seen = time.monotonic()
        return self._consecutive_count(tool, args_hash)

    def _consecutive_count(self, tool: str, args_hash: str) -> int:
        """Count trailing consecutive identical calls."""
        count = 0
        for t, h in reversed(list(self.recent)):
            if t == tool and h == args_hash:
                count += 1
            else:
                break
        return count

    def is_stale(self) -> bool:
        return (time.monotonic() - self.last_seen) > SESSION_TTL_S


# ── SQLite Ledger ─────────────────────────────────────────────

def _init_ledger(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS loop_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            tool       TEXT NOT NULL,
            args_hash  TEXT NOT NULL,
            count      INTEGER NOT NULL,
            ts         INTEGER NOT NULL
        )
    """)
    conn.commit()
    return conn


def _log_loop_event(
    conn: sqlite3.Connection,
    session_id: str,
    tool: str,
    args_hash: str,
    count: int,
) -> None:
    conn.execute(
        "INSERT INTO loop_events (session_id, tool, args_hash, count, ts) "
        "VALUES (?,?,?,?,?)",
        (session_id, tool, args_hash, count, int(time.time())),
    )
    conn.commit()


# ── Lesson Writer ─────────────────────────────────────────────

async def _write_loop_lesson(tool: str, count: int) -> None:
    """Push a lesson to hot.md via the writer daemon."""
    lesson = (
        f"LOOP DETECTED: '{tool}' repeated {count}x identically. "
        "Step back — re-read the file, check assumptions, change approach."
    )
    payload = json.dumps({"cmd": "APPEND_LESSON", "lesson": lesson}).encode() + b"\n"
    try:
        r, w = await asyncio.wait_for(
            asyncio.open_unix_connection(WRITER_SOCK), timeout=2.0
        )
        w.write(payload)
        await w.drain()
        await asyncio.wait_for(r.read(256), timeout=2.0)
        w.close()
        await w.wait_closed()
        logger.info("Loop lesson written for tool '%s'", tool)
    except Exception as e:
        logger.warning("Could not write loop lesson (non-fatal): %s", e)


# ── Mayday Builder ────────────────────────────────────────────

def _build_mayday(session_id: str, tool: str, args_hash: str, count: int) -> dict:
    return {
        "mayday": True,
        "stage": "EXECUTION",
        "error": f"Tool '{tool}' called {count}x consecutively with identical args ({args_hash})",
        "input_that_caused_failure": f"session={session_id} tool={tool} args_hash={args_hash}",
        "recommended_fix": (
            "Stop. Re-read the relevant source file from disk. "
            "Verify the actual function signature. "
            "Try a different approach or ask for direction."
        ),
    }


# ── Command Handlers ──────────────────────────────────────────

async def _handle_command(
    cmd_obj: dict,
    sessions: dict[str, SessionState],
    conn: sqlite3.Connection,
) -> dict:
    cmd = cmd_obj.get("cmd", "")

    if cmd == "PING":
        return {"ok": True, "pong": True, "active_sessions": len(sessions)}

    if cmd == "RECORD_CALL":
        session_id = str(cmd_obj.get("session_id", "default")).strip()[:64]
        tool       = str(cmd_obj.get("tool", "")).strip()[:100]
        args_hash  = str(cmd_obj.get("args_hash", "")).strip()[:64]

        if not tool:
            return {"ok": False, "error": "tool is required"}

        if session_id not in sessions:
            sessions[session_id] = SessionState(session_id=session_id)

        state = sessions[session_id]
        count = state.record(tool, args_hash)

        if count >= LOOP_THRESHOLD:
            _log_loop_event(conn, session_id, tool, args_hash, count)
            mayday = _build_mayday(session_id, tool, args_hash, count)
            # Fire lesson write non-blockingly
            asyncio.ensure_future(_write_loop_lesson(tool, count))
            logger.warning(
                "LOOP DETECTED [%s] tool='%s' count=%d", session_id, tool, count
            )
            return {
                "ok":    True,
                "loop":  True,
                "count": count,
                "mayday": mayday,
            }

        return {"ok": True, "loop": False, "count": count}

    if cmd == "STATUS":
        session_id = cmd_obj.get("session_id")
        if session_id:
            state = sessions.get(str(session_id))
            if not state:
                return {"ok": True, "sessions": {}}
            return {
                "ok": True,
                "sessions": {
                    session_id: {
                        "recent_calls": list(state.recent)[-10:],
                        "loop_flags":   state.loop_flags,
                        "last_seen_ago": round(time.monotonic() - state.last_seen, 1),
                    }
                },
            }
        return {
            "ok": True,
            "sessions": {
                sid: {
                    "recent_count":  len(s.recent),
                    "last_seen_ago": round(time.monotonic() - s.last_seen, 1),
                }
                for sid, s in sessions.items()
            },
        }

    if cmd == "RESET":
        session_id = str(cmd_obj.get("session_id", "")).strip()
        if session_id in sessions:
            del sessions[session_id]
            logger.info("Session reset: %s", session_id)
        return {"ok": True}

    return {"ok": False, "error": f"Unknown command: {cmd!r}"}


# ── Connection Handler ────────────────────────────────────────

async def _handle_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    sessions: dict,
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
            response = await _handle_command(cmd_obj, sessions, conn)

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


# ── Stale Session Reaper ──────────────────────────────────────

async def _reaper_loop(sessions: dict, shutdown: asyncio.Event) -> None:
    while not shutdown.is_set():
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=120.0)
            break
        except asyncio.TimeoutError:
            stale = [sid for sid, s in sessions.items() if s.is_stale()]
            for sid in stale:
                del sessions[sid]
                logger.info("Reaped stale session: %s", sid)


# ── Daemon Entry ──────────────────────────────────────────────

async def run_loop_detector(
    shutdown_event: asyncio.Event | None = None,
    socket_path: str = SOCKET_PATH,
    ledger_path: str = LEDGER_DB,
) -> None:
    conn = _init_ledger(ledger_path)
    sessions: dict[str, SessionState] = {}
    _shutdown = shutdown_event or asyncio.Event()

    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass

    def _cb(r, w):
        asyncio.ensure_future(_handle_connection(r, w, sessions, conn))

    server = await asyncio.start_unix_server(_cb, path=socket_path)
    os.chmod(socket_path, 0o600)  # Owner-only
    logger.info("LoopDetectorDaemon listening on %s", socket_path)

    reaper = asyncio.create_task(_reaper_loop(sessions, _shutdown))

    await _shutdown.wait()

    reaper.cancel()
    server.close()
    await server.wait_closed()
    conn.close()
    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass
    logger.info("LoopDetectorDaemon stopped.")


# ── Self-Test ─────────────────────────────────────────────────

async def _self_test() -> bool:
    import tempfile
    logger.info("Running LoopDetectorDaemon self-test...")

    sock = "/tmp/agent-loop-detector-test.sock"
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    shutdown = asyncio.Event()
    server_task = asyncio.create_task(
        run_loop_detector(shutdown, sock, db_path)
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

        # No loop — 2 different calls
        resp = await _call({"cmd": "RECORD_CALL", "session_id": "s1",
                            "tool": "view_file", "args_hash": "abc"})
        assert resp["ok"] and not resp["loop"], f"False loop on call 1: {resp}"

        resp = await _call({"cmd": "RECORD_CALL", "session_id": "s1",
                            "tool": "view_file", "args_hash": "xyz"})
        assert not resp["loop"], f"False loop on different args: {resp}"

        # Build up to threshold — 3 identical consecutive
        for i in range(LOOP_THRESHOLD):
            resp = await _call({"cmd": "RECORD_CALL", "session_id": "s1",
                                "tool": "run_command", "args_hash": "deadbeef"})

        assert resp["loop"], f"Loop not detected at threshold: {resp}"
        assert resp["count"] == LOOP_THRESHOLD
        assert "mayday" in resp
        assert resp["mayday"]["mayday"] is True

        # RESET clears loop state
        resp = await _call({"cmd": "RESET", "session_id": "s1"})
        assert resp["ok"]

        resp = await _call({"cmd": "RECORD_CALL", "session_id": "s1",
                            "tool": "run_command", "args_hash": "deadbeef"})
        assert not resp["loop"], f"Loop persisted after reset: {resp}"

        # STATUS
        resp = await _call({"cmd": "STATUS"})
        assert resp["ok"] and "sessions" in resp

        logger.info("LoopDetectorDaemon self-test PASSED")
        return True

    except Exception as e:
        logger.error("LoopDetectorDaemon self-test FAILED: %s", e)
        import traceback; traceback.print_exc()
        return False
    finally:
        shutdown.set()
        await server_task
        try:
            os.unlink(db_path)
        except OSError:
            pass


# ── CLI ───────────────────────────────────────────────────────

def main() -> None:
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="LoopDetectorDaemon")
    parser.add_argument("--test-mode", action="store_true")
    parser.add_argument("--socket", default=SOCKET_PATH)
    parser.add_argument("--db",     default=LEDGER_DB)
    args = parser.parse_args()

    if args.test_mode:
        success = asyncio.run(_self_test())
        raise SystemExit(0 if success else 1)

    asyncio.run(run_loop_detector(socket_path=args.socket, ledger_path=args.db))


if __name__ == "__main__":
    main()
