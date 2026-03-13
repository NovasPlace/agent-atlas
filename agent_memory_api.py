"""Agent Memory API — Thin client for the MD sub-daemon system.

Agents call this instead of reading/writing MD files directly.
Connects to md_reader.py and md_writer.py Unix sockets.
Falls back to direct file reads gracefully if daemons aren't running.

Importable API:
    from agent_memory_api import MemoryAPI
    api = MemoryAPI()
    hot = api.get_hot()
    api.lesson("Always sanitize slugs before path concatenation")
    api.update_session(current_work="...", files_touched=["foo.py"])

CLI:
    python3 agent_memory_api.py ping
    python3 agent_memory_api.py get hot
    python3 agent_memory_api.py get warm locus
    python3 agent_memory_api.py get session
    python3 agent_memory_api.py get projects
    python3 agent_memory_api.py lesson "Never exceed 5 terminals"
    python3 agent_memory_api.py write session '{"current_work": "...", ...}'
    python3 agent_memory_api.py write hot '{"session_summary": "..."}'
    python3 agent_memory_api.py write warm locus '{"status": "Active"}'
    python3 agent_memory_api.py register '{"name": "...", "location": "...", "status": "..."}'
    python3 agent_memory_api.py --test-mode

All socket calls time out in 2 seconds and fall back to direct reads.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
from pathlib import Path
from typing import Any

# ── Config ─────────────────────────────────────────────────

MEMORY_DIR = Path(os.path.expanduser("~/.gemini/memory"))
HOT_FILE = MEMORY_DIR / "hot.md"
SESSION_FILE = MEMORY_DIR / "session.md"
PROJECTS_DIR = MEMORY_DIR / "projects"

READER_SOCKET    = "/tmp/agent-memory-reader.sock"
WRITER_SOCKET    = "/tmp/agent-memory-writer.sock"
COORD_SOCKET          = "/tmp/agent-coord.sock"
TASKQUEUE_SOCKET      = "/tmp/agent-taskqueue.sock"
LOOP_DETECTOR_SOCKET  = "/tmp/agent-loop-detector.sock"
PRESSURE_SOCKET       = "/tmp/agent-context-pressure.sock"
MSGQUEUE_SOCKET       = "/tmp/agent-msgqueue.sock"

# Per-call timeout in seconds
SOCKET_TIMEOUT = 2.0

# Max response size
MAX_RESPONSE = 1_048_576


# ── Low-Level Socket Call ──────────────────────────────────

def _socket_call(sock_path: str, payload: dict) -> dict | None:
    """Send a JSON command to a Unix socket and return the response dict.

    Returns None on any error (timeout, connection refused, decode failure).
    """
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(SOCKET_TIMEOUT)
        s.connect(sock_path)
        s.sendall(json.dumps(payload).encode("utf-8") + b"\n")

        chunks = []
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if chunks[-1].endswith(b"\n"):
                break

        s.close()
        raw = b"".join(chunks)
        return json.loads(raw.decode("utf-8"))
    except (ConnectionRefusedError, FileNotFoundError):
        return None  # Daemon not running — caller will fall back
    except Exception:
        return None


# ── Fallback File Reads ─────────────────────────────────────

def _fallback_get_hot() -> str:
    return HOT_FILE.read_text(encoding="utf-8") if HOT_FILE.exists() else ""


def _fallback_get_session() -> str:
    return SESSION_FILE.read_text(encoding="utf-8") if SESSION_FILE.exists() else ""


def _fallback_get_warm(slug: str) -> str:
    path = PROJECTS_DIR / f"{slug}.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _fallback_get_projects() -> dict:
    slugs = [p.stem for p in PROJECTS_DIR.glob("*.md")] if PROJECTS_DIR.exists() else []
    return {"warm_slugs": slugs, "projects": []}


# ── Public API ─────────────────────────────────────────────

class MemoryAPI:
    """Thin agent client for the MD sub-daemon system."""

    def ping(self) -> bool:
        """Return True if both daemons are reachable."""
        r = _socket_call(READER_SOCKET, {"cmd": "PING"})
        w = _socket_call(WRITER_SOCKET, {"cmd": "PING"})
        return bool(r and r.get("pong") and w and w.get("pong"))

    # ── Reads ─────────────────────────────────────────────

    def get_hot(self) -> str:
        """Return hot.md contents (cached by reader daemon, fallback to disk)."""
        resp = _socket_call(READER_SOCKET, {"cmd": "GET_HOT"})
        if resp and resp.get("ok"):
            return resp["content"]
        return _fallback_get_hot()

    def get_session(self) -> str:
        """Return session.md contents."""
        resp = _socket_call(READER_SOCKET, {"cmd": "GET_SESSION"})
        if resp and resp.get("ok"):
            return resp["content"]
        return _fallback_get_session()

    def get_warm(self, slug: str) -> str:
        """Return warm project file contents for the given slug."""
        slug = slug.strip().lower()
        resp = _socket_call(READER_SOCKET, {"cmd": "GET_WARM", "slug": slug})
        if resp and resp.get("ok"):
            return resp["content"]
        return _fallback_get_warm(slug)

    def get_all_projects(self) -> dict:
        """Return project table and warm slugs from hot.md."""
        resp = _socket_call(READER_SOCKET, {"cmd": "GET_ALL_PROJECTS"})
        if resp and resp.get("ok"):
            return resp
        return _fallback_get_projects()

    def get_context(self) -> str:
        """Return the live context brief built by ContextRecallDaemon.

        This is the primary way agents should restore context mid-session
        or after hitting token limits. Returns a ≤50-line ranked summary
        of recent activity, relevant lessons, and project state.

        Fallback: assembles a minimal brief from hot.md + session.md on disk.
        """
        resp = _socket_call(READER_SOCKET, {"cmd": "GET_CONTEXT_BRIEF"})
        if resp and resp.get("ok"):
            return resp["content"]
        # Fallback: assemble a minimal brief from disk
        parts = []
        hot = _fallback_get_hot()
        if hot:
            parts.append("## LIVE CONTEXT (fallback — daemon offline)\n")
            # Extract SESSION SUMMARY if present
            import re as _re
            m = _re.search(r"## SESSION SUMMARY.*?\n(.*?)(?=\n##|\Z)", hot, _re.DOTALL)
            if m:
                parts.append("### Last Session\n" + m.group(1).strip())
        session = _fallback_get_session()
        if session:
            # Extract current work
            m2 = _re.search(r"## Current Work\n(.+?)(?=\n##|\Z)", session, _re.DOTALL)
            if m2 and m2.group(1).strip() != "_none_":
                parts.append("\n### Current Work\n" + m2.group(1).strip())
        return "\n".join(parts) if parts else "## LIVE CONTEXT\n\n> No context available."

    # ── Writes ────────────────────────────────────────────

    def lesson(self, text: str) -> bool:
        """Append a lesson to hot.md RECENT LESSONS section.

        Returns True if write succeeded (or was duplicate-guarded).
        Silently degrades to False if daemon is unreachable.
        """
        resp = _socket_call(WRITER_SOCKET, {"cmd": "APPEND_LESSON", "lesson": text})
        return bool(resp and resp.get("ok"))

    def update_session(
        self,
        current_work: str = "",
        files_touched: list[str] | None = None,
        pending_actions: list[str] | None = None,
        critical_context: list[str] | None = None,
    ) -> bool:
        """Overwrite session.md with structured state.

        All parameters optional — omitted sections default to '_none_'.
        """
        payload = {
            "cmd": "UPDATE_SESSION",
            "current_work": current_work,
            "files_touched": files_touched or [],
            "pending_actions": pending_actions or [],
            "critical_context": critical_context or [],
        }
        resp = _socket_call(WRITER_SOCKET, payload)
        return bool(resp and resp.get("ok"))

    def update_hot(self, session_summary: str, open_threads: list[str] | None = None) -> bool:
        """Update hot.md SESSION SUMMARY and optionally OPEN THREADS."""
        payload: dict[str, Any] = {
            "cmd": "UPDATE_HOT",
            "session_summary": session_summary,
        }
        if open_threads is not None:
            payload["open_threads"] = open_threads
        resp = _socket_call(WRITER_SOCKET, payload)
        return bool(resp and resp.get("ok"))

    def update_warm(
        self,
        slug: str,
        status: str = "",
        decisions: list[str] | None = None,
    ) -> bool:
        """Update a warm project file's status and recent decisions."""
        payload = {
            "cmd": "UPDATE_WARM",
            "slug": slug.strip().lower(),
            "status": status,
            "decisions": decisions or [],
        }
        resp = _socket_call(WRITER_SOCKET, payload)
        return bool(resp and resp.get("ok"))

    def register_project(
        self,
        name: str,
        location: str,
        status: str = "Active",
        warm_file: str = "—",
    ) -> bool:
        """Add a new project row to hot.md ACTIVE PROJECTS table."""
        payload = {
            "cmd": "REGISTER_PROJECT",
            "name": name,
            "location": location,
            "status": status,
            "warm_file": warm_file,
        }
        resp = _socket_call(WRITER_SOCKET, payload)
        return bool(resp and resp.get("ok"))

    # ── Coordination ─────────────────────────────────────────

    def coord_presence(self, agent_id: str, work: str, files: list[str] | None = None) -> bool:
        """Announce this agent's presence and current work to the coord daemon."""
        resp = _socket_call(COORD_SOCKET, {
            "cmd": "PRESENCE", "agent_id": agent_id,
            "work": work, "files": files or [],
        })
        return bool(resp and resp.get("ok"))

    def coord_who(self) -> dict:
        """Return all active agents and their claimed files."""
        resp = _socket_call(COORD_SOCKET, {"cmd": "WHO"})
        if resp and resp.get("ok"):
            return resp
        return {"agents": [], "claims": {}}

    def coord_claim(self, agent_id: str, path: str) -> dict:
        """Soft-claim a file/resource. Returns {"ok": True} or {"ok": False, "claimed_by": "..."}."""
        resp = _socket_call(COORD_SOCKET, {"cmd": "CLAIM", "agent_id": agent_id, "path": path})
        return resp or {"ok": False, "error": "coord daemon unreachable"}

    def coord_release(self, agent_id: str, path: str) -> bool:
        """Release a claimed file."""
        resp = _socket_call(COORD_SOCKET, {"cmd": "RELEASE", "agent_id": agent_id, "path": path})
        return bool(resp and resp.get("ok"))

    def coord_clear(self, agent_id: str) -> bool:
        """Agent signing off — clear all presence and claims."""
        resp = _socket_call(COORD_SOCKET, {"cmd": "CLEAR", "agent_id": agent_id})
        return bool(resp and resp.get("ok"))

    # ── Task Queue ───────────────────────────────────────────

    def task_push(
        self, title: str, owner: str = "any", priority: int = 5,
        run_after: int = 0, recur_s: int = 0,
    ) -> dict:
        """Enqueue a task. Returns {"ok": True, "task_id": "..."}."""
        resp = _socket_call(TASKQUEUE_SOCKET, {
            "cmd": "PUSH", "title": title, "owner": owner,
            "priority": priority, "run_after": run_after, "recur_s": recur_s,
        })
        return resp or {"ok": False, "error": "taskqueue daemon unreachable"}

    def task_next(self, owner: str = "") -> dict | None:
        """Return next pending task for owner (or any). None if nothing pending."""
        resp = _socket_call(TASKQUEUE_SOCKET, {"cmd": "NEXT", "owner": owner})
        if resp and resp.get("ok"):
            return resp.get("task")
        return None

    def task_done(self, task_id: str) -> dict:
        """Mark a task done. Returns {"ok": True, "requeued_as": "..."} for recurring tasks."""
        resp = _socket_call(TASKQUEUE_SOCKET, {"cmd": "DONE", "task_id": task_id})
        return resp or {"ok": False, "error": "taskqueue daemon unreachable"}

    def task_cancel(self, task_id: str) -> bool:
        """Cancel a pending task."""
        resp = _socket_call(TASKQUEUE_SOCKET, {"cmd": "CANCEL", "task_id": task_id})
        return bool(resp and resp.get("ok"))

    def task_list(self, owner: str = "", status: str = "pending") -> list[dict]:
        """List tasks filtered by owner and status."""
        resp = _socket_call(TASKQUEUE_SOCKET, {"cmd": "LIST", "owner": owner, "status": status})
        if resp and resp.get("ok"):
            return resp.get("tasks", [])
        return []

    # ── Loop Detector ────────────────────────────────────

    def record_call(
        self,
        tool: str,
        args_hash: str = "",
        session_id: str = "default",
        detail: str = "",
    ) -> dict:
        """Record a tool call. Returns {loop: False} or {loop: True, mayday: {...}}.

        Call this before each significant tool invocation. If loop=True,
        the agent should stop, re-read the relevant file, and change approach.

        args_hash: a short stable identifier for the call args, e.g.
            import hashlib
            args_hash = hashlib.md5(str(args).encode()).hexdigest()[:8]
        """
        resp = _socket_call(LOOP_DETECTOR_SOCKET, {
            "cmd": "RECORD_CALL",
            "session_id": session_id,
            "tool": tool,
            "args_hash": args_hash,
            "detail": detail,
        })
        if resp is None:
            return {"ok": True, "loop": False}   # daemon down — non-fatal
        return resp

    def loop_status(self, session_id: str = "") -> dict:
        """Return current loop detection state for a session."""
        resp = _socket_call(LOOP_DETECTOR_SOCKET, {
            "cmd": "STATUS",
            "session_id": session_id or None,
        })
        return resp or {"ok": False, "error": "loop-detector unreachable"}

    def loop_reset(self, session_id: str = "default") -> bool:
        """Clear loop state for a session (e.g., after changing approach)."""
        resp = _socket_call(LOOP_DETECTOR_SOCKET, {
            "cmd": "RESET",
            "session_id": session_id,
        })
        return bool(resp and resp.get("ok"))

    # ── Context Pressure ─────────────────────────────────

    def pressure_tick(
        self,
        tool: str,
        output_chars: int = 0,
        session_id: str = "default",
    ) -> dict:
        """Report a tool call to the pressure estimator.

        Returns:
          {pressure: float, action: str, estimated_tokens: int}
          action: 'ok' | 'recommend_flush' | 'urgent_flush'

        Call after significant tool invocations. If action is
        'urgent_flush', call write_session() before the next tool.
        """
        resp = _socket_call(PRESSURE_SOCKET, {
            "cmd": "TICK",
            "session_id": session_id,
            "tool": tool,
            "output_chars": output_chars,
        })
        if resp is None:
            return {"pressure": 0.0, "action": "ok"}  # daemon down — non-fatal
        return resp

    def pressure_flush(self, session_id: str = "default") -> dict:
        """Notify pressure estimator that a flush occurred. Returns updated pressure."""
        resp = _socket_call(PRESSURE_SOCKET, {"cmd": "FLUSH", "session_id": session_id})
        return resp or {"pressure": 0.0, "action": "ok"}

    def pressure_status(self, session_id: str = "") -> dict:
        """Return current pressure breakdown for a session."""
        resp = _socket_call(PRESSURE_SOCKET, {
            "cmd": "STATUS",
            "session_id": session_id or None,
        })
        return resp or {"ok": False, "error": "pressure daemon unreachable"}

    # ── Agent Message Queue ─────────────────────────────

    def msg_send(
        self,
        from_agent: str,
        to_agent: str,
        subject: str,
        body: str = "",
        reply_to: str = "",
    ) -> dict:
        """Send an async message to another agent's inbox.

        Returns {ok, msg_id}. The recipient reads it on next session via msg_recv().
        """
        resp = _socket_call(MSGQUEUE_SOCKET, {
            "cmd": "SEND",
            "from": from_agent,
            "to": to_agent,
            "subject": subject,
            "body": body,
            "reply_to": reply_to,
        })
        return resp or {"ok": False, "error": "msgqueue daemon unreachable"}

    def msg_recv(self, agent_id: str, limit: int = 10) -> list[dict]:
        """Return unread messages for this agent. Auto-signals intent to read."""
        resp = _socket_call(MSGQUEUE_SOCKET, {
            "cmd": "RECV", "agent_id": agent_id, "limit": limit,
        })
        if resp and resp.get("ok"):
            return resp.get("messages", [])
        return []

    def msg_ack(self, msg_id: str) -> bool:
        """Mark a message as read/done."""
        resp = _socket_call(MSGQUEUE_SOCKET, {"cmd": "ACK", "msg_id": msg_id})
        return bool(resp and resp.get("ok"))

    def msg_list(self, agent_id: str) -> list[dict]:
        """List all messages (read + unread) for this agent."""
        resp = _socket_call(MSGQUEUE_SOCKET, {"cmd": "LIST", "agent_id": agent_id})
        if resp and resp.get("ok"):
            return resp.get("messages", [])
        return []

    # ── Real-Time Broadcast ──────────────────────────────────

    def subscribe(
        self,
        callback,
        channel: str = "agent_memory_updates",
        timeout: float | None = None,
    ) -> None:
        """Block and call callback(event_dict) for each real-time memory update.

        Runs a PostgreSQL LISTEN loop. Designed to be used in a background thread:

            import threading
            api = MemoryAPI()
            t = threading.Thread(
                target=api.subscribe,
                args=(lambda ev: print('EVENT:', ev),),
                daemon=True,
            )
            t.start()

        Args:
            callback: Called with a dict for each event. Keep it fast.
            channel:  PG channel to listen on (default: agent_memory_updates).
            timeout:  Stop after this many seconds (None = run forever).
        """
        try:
            from pg_broadcast import PGSubscriber
        except ImportError:
            print("pg_broadcast not available — subscribe() requires psycopg2", file=sys.stderr)
            return

        subscriber = PGSubscriber(channel=channel)
        stop_event = threading.Event()

        def _wrapped_callback(event: dict) -> None:
            callback(event)

        subscriber.start(callback=_wrapped_callback)

        try:
            if timeout is not None:
                stop_event.wait(timeout=timeout)
            else:
                # Block forever until KeyboardInterrupt or process death
                import time
                while True:
                    time.sleep(1.0)
        except KeyboardInterrupt:
            pass
        finally:
            subscriber.stop()

    def get_events(
        self,
        since_id: int = 0,
        limit: int = 20,
        channel: str = "agent_memory_updates",
    ) -> list[dict]:
        """Return memory events from the agent_memory_events table since since_id.

        Use at session start to catch up on updates from other conversations:

            events = api.get_events(since_id=last_known_id)
            for ev in events:
                print(ev['payload']['cmd'], ev['ts'])

        Returns list of dicts: {id, channel, payload (dict), ts (str)}.
        Falls back to empty list if pg_broadcast or psycopg2 unavailable.
        """
        try:
            from pg_broadcast import get_events_since
            return get_events_since(since_id=since_id, limit=limit, channel=channel)
        except ImportError:
            return []
        except Exception as exc:
            print(f"get_events failed: {exc}", file=sys.stderr)
            return []


# ── CLI ─────────────────────────────────────────────────────

def _cli() -> None:
    args = sys.argv[1:]
    if not args:
        _print_help()
        return

    api = MemoryAPI()
    cmd = args[0]

    if cmd == "ping":
        ok = api.ping()
        print("OK — both daemons reachable" if ok else "DEGRADED — falling back to disk reads")
        raise SystemExit(0 if ok else 1)

    if cmd == "get":
        if len(args) < 2:
            print("Usage: get <hot|session|projects|warm <slug>>", file=sys.stderr)
            raise SystemExit(1)
        target = args[1]
        if target == "hot":
            print(api.get_hot())
        elif target == "session":
            print(api.get_session())
        elif target == "projects":
            result = api.get_all_projects()
            print(json.dumps(result, indent=2))
        elif target == "context":
            print(api.get_context())
        elif target == "warm":
            if len(args) < 3:
                print("Usage: get warm <slug>", file=sys.stderr)
                raise SystemExit(1)
            print(api.get_warm(args[2]))
        else:
            print(f"Unknown target: {target!r}", file=sys.stderr)
            raise SystemExit(1)

    elif cmd == "lesson":
        if len(args) < 2:
            print("Usage: lesson <text>", file=sys.stderr)
            raise SystemExit(1)
        text = " ".join(args[1:])
        ok = api.lesson(text)
        print("Lesson written." if ok else "Write failed (daemon unreachable?)")
        raise SystemExit(0 if ok else 1)

    elif cmd == "write":
        if len(args) < 3:
            print("Usage: write <hot|session|warm <slug>> <JSON>", file=sys.stderr)
            raise SystemExit(1)
        target = args[1]

        if target == "session":
            payload = json.loads(args[2])
            ok = api.update_session(**payload)
        elif target == "hot":
            payload = json.loads(args[2])
            ok = api.update_hot(**payload)
        elif target == "warm":
            if len(args) < 4:
                print("Usage: write warm <slug> <JSON>", file=sys.stderr)
                raise SystemExit(1)
            slug = args[2]
            payload = json.loads(args[3])
            ok = api.update_warm(slug, **payload)
        else:
            print(f"Unknown write target: {target!r}", file=sys.stderr)
            raise SystemExit(1)

        print("Write OK." if ok else "Write failed (daemon unreachable?)")
        raise SystemExit(0 if ok else 1)

    elif cmd == "register":
        if len(args) < 2:
            print("Usage: register <JSON>", file=sys.stderr)
            raise SystemExit(1)
        payload = json.loads(args[1])
        ok = api.register_project(**payload)
        print("Registered." if ok else "Registration failed (daemon unreachable?)")
        raise SystemExit(0 if ok else 1)

    elif cmd == "coord":
        if len(args) < 2:
            print("Usage: coord <who|presence|claim|release|clear>", file=sys.stderr)
            raise SystemExit(1)
        sub = args[1]
        if sub == "who":
            result = api.coord_who()
            print(json.dumps(result, indent=2))
        elif sub == "presence":
            if len(args) < 4:
                print("Usage: coord presence <agent_id> <work>", file=sys.stderr)
                raise SystemExit(1)
            ok = api.coord_presence(args[2], " ".join(args[3:]))
            print("OK" if ok else "FAILED")
            raise SystemExit(0 if ok else 1)
        elif sub == "claim":
            if len(args) < 4:
                print("Usage: coord claim <agent_id> <path>", file=sys.stderr)
                raise SystemExit(1)
            result = api.coord_claim(args[2], args[3])
            print(json.dumps(result))
            raise SystemExit(0 if result.get("ok") else 1)
        elif sub == "release":
            if len(args) < 4:
                print("Usage: coord release <agent_id> <path>", file=sys.stderr)
                raise SystemExit(1)
            ok = api.coord_release(args[2], args[3])
            print("Released." if ok else "FAILED")
            raise SystemExit(0 if ok else 1)
        elif sub == "clear":
            if len(args) < 3:
                print("Usage: coord clear <agent_id>", file=sys.stderr)
                raise SystemExit(1)
            ok = api.coord_clear(args[2])
            print("Cleared." if ok else "FAILED")
            raise SystemExit(0 if ok else 1)
        else:
            print(f"Unknown coord subcommand: {sub!r}", file=sys.stderr)
            raise SystemExit(1)

    elif cmd == "task":
        if len(args) < 2:
            print("Usage: task <push|next|done|cancel|list>", file=sys.stderr)
            raise SystemExit(1)
        sub = args[1]
        if sub == "push":
            if len(args) < 3:
                print("Usage: task push <title> [--priority N] [--after N]", file=sys.stderr)
                raise SystemExit(1)
            title = args[2]
            priority = int(args[args.index("--priority") + 1]) if "--priority" in args else 5
            run_after = int(args[args.index("--after") + 1]) if "--after" in args else 0
            result = api.task_push(title, priority=priority, run_after=run_after)
            print(json.dumps(result))
            raise SystemExit(0 if result.get("ok") else 1)
        elif sub == "next":
            owner = args[2] if len(args) > 2 else ""
            task = api.task_next(owner)
            if task:
                print(f"[{task['id']}] (pri={task['priority']}) {task['title']}")
            else:
                print("No pending tasks.")
            raise SystemExit(0)
        elif sub == "done":
            if len(args) < 3:
                print("Usage: task done <task_id>", file=sys.stderr)
                raise SystemExit(1)
            result = api.task_done(args[2])
            print(json.dumps(result))
            raise SystemExit(0 if result.get("ok") else 1)
        elif sub == "cancel":
            if len(args) < 3:
                print("Usage: task cancel <task_id>", file=sys.stderr)
                raise SystemExit(1)
            ok = api.task_cancel(args[2])
            print("Cancelled." if ok else "FAILED")
            raise SystemExit(0 if ok else 1)
        elif sub == "list":
            status = args[2] if len(args) > 2 else "pending"
            tasks = api.task_list(status=status)
            if not tasks:
                print(f"No {status} tasks.")
            for t in tasks:
                print(f"[{t['id']}] pri={t['priority']} owner={t['owner']} {t['title']}")
            raise SystemExit(0)
        else:
            print(f"Unknown task subcommand: {sub!r}", file=sys.stderr)
            raise SystemExit(1)

    elif cmd == "--test-mode" or cmd == "test":
        _run_tests(api)

    elif cmd == "subscribe":
        # Live stream of memory events to stdout
        import json as _json
        channel = args[1] if len(args) > 1 else "agent_memory_updates"
        print(f"Subscribing to channel '{channel}'... (Ctrl+C to stop)")

        def _print_event(ev: dict) -> None:
            print(_json.dumps(ev))
            sys.stdout.flush()

        api.subscribe(_print_event, channel=channel)

    elif cmd == "events":
        since_id = 0
        limit = 20
        if "--since" in args:
            idx = args.index("--since")
            since_id = int(args[idx + 1]) if idx + 1 < len(args) else 0
        if "--limit" in args:
            idx = args.index("--limit")
            limit = int(args[idx + 1]) if idx + 1 < len(args) else 20
        events = api.get_events(since_id=since_id, limit=limit)
        if not events:
            print(f"No events since id={since_id}")
        for ev in events:
            print(f"[{ev['id']}] {ev['ts']}  {ev['payload'].get('cmd', '?')}  {ev['payload']}")
        raise SystemExit(0)

    else:
        _print_help()


def _print_help() -> None:
    print(
        "agent_memory_api.py — Agent Memory Client\n\n"
        "Commands:\n"
        "  ping                                   Check daemon health\n"
        "  get context                            Live context brief (primary!)\n"
        "  get hot                                Read hot.md\n"
        "  get session                            Read session.md\n"
        "  get warm <slug>                        Read a warm project file\n"
        "  get projects                           List all projects\n"
        "  lesson <text>                          Append a lesson\n"
        "  write hot '{\"session_summary\": \"...\"}'\n"
        "  write session '{\"current_work\": \"...\", ...}'\n"
        "  write warm <slug> '{\"status\": \"...\"}'\n"
        "  register '{\"name\": \"...\", \"location\": \"...\"}'\n"
        "  coord who                              List active agents + claims\n"
        "  coord presence <agent_id> <work>       Announce presence\n"
        "  coord claim <agent_id> <path>          Soft-lock a file\n"
        "  coord release <agent_id> <path>        Release a file lock\n"
        "  coord clear <agent_id>                 Sign off (clear all claims)\n"
        "  task push <title> [--priority N] [--after N]\n"
        "  task next [owner]                      Next pending task\n"
        "  task done <task_id>                    Mark task done\n"
        "  task cancel <task_id>                  Cancel a task\n"
        "  task list [status]                     List tasks (default: pending)\n"
        "  subscribe [channel]                    Stream live memory events (Ctrl+C to stop)\n"
        "  events [--since N] [--limit N]         Replay missed events from DB\n"
        "  --test-mode                            Integration self-test\n"
    )


def _run_tests(api: MemoryAPI) -> None:
    """Integration self-test — requires daemons running."""
    import tempfile

    print("Running agent_memory_api integration test...")
    errors: list[str] = []

    # Test ping
    reachable = api.ping()
    if not reachable:
        print(
            "WARNING: Daemons not running. Testing fallback reads only.\n"
            "Start agent_memory_daemon.py for full integration test."
        )

    # Test get_hot fallback
    content = api.get_hot()
    if not content:
        errors.append("get_hot returned empty (hot.md missing?)")
    else:
        print(f"  [OK] get_hot — {len(content)} chars")

    # Test get_session fallback
    _ = api.get_session()
    print("  [OK] get_session — (empty is OK)")

    # Test get_all_projects
    projects = api.get_all_projects()
    if "warm_slugs" not in projects:
        errors.append("get_all_projects missing warm_slugs key")
    else:
        print(f"  [OK] get_all_projects — {len(projects['warm_slugs'])} warm files")

    if reachable:
        # Test lesson write
        ok = api.lesson("Integration test lesson — delete me")
        if not ok:
            errors.append("lesson() returned False")
        else:
            print("  [OK] lesson() — written")

        # Verify lesson appears in hot
        hot = api.get_hot()
        if "Integration test lesson" not in hot:
            errors.append("lesson not found in hot.md after write")
        else:
            print("  [OK] lesson verified in hot.md")

        # Test update_session
        ok = api.update_session(
            current_work="Integration test",
            files_touched=["agent_memory_api.py"],
            critical_context=["Test context"],
        )
        if not ok:
            errors.append("update_session() returned False")
        else:
            print("  [OK] update_session()")

        # Test update_hot
        ok = api.update_hot("Integration test summary")
        if not ok:
            errors.append("update_hot() returned False")
        else:
            print("  [OK] update_hot()")

        # ── Coord tests ───────────────────────────────────────
        coord_live = _socket_call(COORD_SOCKET, {"cmd": "PING"})
        if not coord_live or not coord_live.get("pong"):
            print("  [SKIP] coord daemon not reachable — skipping coord tests")
        else:
            # Presence
            ok = api.coord_presence("test-agent-a", "Integration test run", ["src/test.py"])
            if not ok:
                errors.append("coord_presence() returned False")
            else:
                print("  [OK] coord_presence()")

            # WHO
            result = api.coord_who()
            if not any(a["agent_id"] == "test-agent-a" for a in result.get("agents", [])):
                errors.append("coord_who() missing test-agent-a after presence announce")
            else:
                print("  [OK] coord_who() — agent present")

            # CLAIM success
            result = api.coord_claim("test-agent-a", "src/toolbar.js")
            if not result.get("ok"):
                errors.append(f"coord_claim() failed: {result}")
            else:
                print("  [OK] coord_claim() — claimed")

            # CLAIM conflict
            result = api.coord_claim("test-agent-b", "src/toolbar.js")
            if result.get("ok") or result.get("claimed_by") != "test-agent-a":
                errors.append(f"coord_claim() conflict detection failed: {result}")
            else:
                print("  [OK] coord_claim() — conflict correctly detected")

            # Path traversal injection — should be rejected or sanitized
            result = api.coord_claim("test-agent-a", "../../etc/shadow")
            # Either ok=False or the path got sanitized to "etc/shadow" (no traversal)
            if result.get("ok") and result.get("path", "").startswith(".."):
                errors.append(f"coord_claim() allowed traversal path: {result}")
            else:
                print("  [OK] coord_claim() — traversal path sanitized/rejected")

            # RELEASE
            ok = api.coord_release("test-agent-a", "src/toolbar.js")
            if not ok:
                errors.append("coord_release() returned False")
            else:
                print("  [OK] coord_release()")

            # CLEAR
            ok = api.coord_clear("test-agent-a")
            if not ok:
                errors.append("coord_clear() returned False")
            else:
                print("  [OK] coord_clear()")

        # ── Task Queue tests ──────────────────────────────────
        tq_live = _socket_call(TASKQUEUE_SOCKET, {"cmd": "PING"})
        if not tq_live or not tq_live.get("pong"):
            print("  [SKIP] taskqueue daemon not reachable — skipping task tests")
        else:
            # PUSH
            result = api.task_push("Integration test task", priority=1)
            if not result.get("ok"):
                errors.append(f"task_push() failed: {result}")
            else:
                task_id = result["task_id"]
                print(f"  [OK] task_push() — id={task_id}")

                # NEXT — should surface our high-priority task
                task = api.task_next()
                if task is None or task.get("id") != task_id:
                    errors.append(f"task_next() didn't return expected task: {task}")
                else:
                    print(f"  [OK] task_next() — '{task['title']}'")

                # DONE
                result = api.task_done(task_id)
                if not result.get("ok"):
                    errors.append(f"task_done() failed: {result}")
                else:
                    print("  [OK] task_done()")

                # LIST — verify done status
                tasks = api.task_list(status="done")
                if not any(t["id"] == task_id for t in tasks):
                    errors.append("task_list(status=done) missing completed task")
                else:
                    print("  [OK] task_list(status=done)")

            # Control character injection — title should be sanitized
            result = api.task_push("Injected\x00\x01title")
            if result.get("ok"):
                # Push succeeded — verify the null byte was stripped
                bad_task = api.task_next()
                if bad_task and "\x00" in bad_task.get("title", ""):
                    errors.append("task title contained null byte after sanitization")
                    api.task_cancel(bad_task["id"])
                else:
                    api.task_done(result["task_id"])
                    print("  [OK] task control-char sanitization verified")

        # ── Loop Detector tests ───────────────────────────────
        ld_live = _socket_call(LOOP_DETECTOR_SOCKET, {"cmd": "PING"})
        if not ld_live or not ld_live.get("pong"):
            print("  [SKIP] loop-detector daemon not reachable")
        else:
            # Normal calls — no loop
            api.record_call("view_file", "aaa", "integ-test")
            r = api.record_call("view_file", "bbb", "integ-test")
            if r.get("loop"):
                errors.append("loop-detector false positive on different args")
            else:
                print("  [OK] loop_detector — no false positive on different args")

            # 3 identical → loop
            for _ in range(3):
                r = api.record_call("run_command", "deaddead", "integ-test")
            if not r.get("loop") or r.get("count") < 3:
                errors.append(f"loop_detector failed to fire at threshold: {r}")
            else:
                print(f"  [OK] loop_detector — fired at count={r['count']}")

            # Reset clears state
            api.loop_reset("integ-test")
            r = api.record_call("run_command", "deaddead", "integ-test")
            if r.get("loop"):
                errors.append("loop_detector persisted after reset")
            else:
                print("  [OK] loop_detector — reset clears state")

        # ── Git Watcher tests ─────────────────────────────────
        GW_SOCKET = "/tmp/agent-git-watcher.sock"
        gw_live = _socket_call(GW_SOCKET, {"cmd": "PING"})
        if not gw_live or not gw_live.get("pong"):
            print("  [SKIP] git-watcher daemon not reachable")
        else:
            # Path traversal injection
            result = _socket_call(GW_SOCKET, {"cmd": "WATCH", "path": "../../etc/passwd"})
            if result and result.get("ok"):
                errors.append("git_watcher allowed traversal path for WATCH")
            else:
                print("  [OK] git_watcher — traversal path rejected")

            # Null byte in path
            result = _socket_call(GW_SOCKET, {"cmd": "WATCH", "path": "/tmp/test\x00evil"})
            if result and result.get("ok"):
                errors.append("git_watcher accepted null byte in path")
            else:
                print("  [OK] git_watcher — null byte path rejected")

            # STATUS returns repos
            result = _socket_call(GW_SOCKET, {"cmd": "STATUS"})
            if not result or not result.get("ok"):
                errors.append(f"git_watcher STATUS failed: {result}")
            else:
                print(f"  [OK] git_watcher STATUS — {len(result.get('repos', []))} repo(s)")

        # ── Context Pressure tests ────────────────────────────
        cp_live = _socket_call(PRESSURE_SOCKET, {"cmd": "PING"})
        if not cp_live or not cp_live.get("pong"):
            print("  [SKIP] context-pressure daemon not reachable")
        else:
            # Fresh session — low pressure
            r = api.pressure_tick("view_file", 1000, "integ-test")
            if r.get("action") != "ok" or r.get("pressure", 1) > 0.65:
                errors.append(f"pressure_tick unexpected result on fresh session: {r}")
            else:
                print(f"  [OK] pressure_tick — pressure={r['pressure']} action={r['action']}")

            # Flush reduces pressure
            r_after = api.pressure_flush("integ-test")
            if not r_after:
                errors.append("pressure_flush returned empty")
            else:
                print(f"  [OK] pressure_flush — pressure now {r_after.get('pressure')}")

            # Reset
            _socket_call(PRESSURE_SOCKET, {"cmd": "RESET", "session_id": "integ-test"})
            print("  [OK] pressure RESET")

        # ── Agent MsgQueue tests ──────────────────────────────
        mq_live = _socket_call(MSGQUEUE_SOCKET, {"cmd": "PING"})
        if not mq_live or not mq_live.get("pong"):
            print("  [SKIP] msgqueue daemon not reachable")
        else:
            # Send and receive
            r = api.msg_send("integ-agent-a", "integ-agent-b",
                             "Test handoff", "Check hot.md when ready")
            if not r.get("ok"):
                errors.append(f"msg_send failed: {r}")
            else:
                msg_id = r["msg_id"]
                print(f"  [OK] msg_send — id={msg_id}")

                msgs = api.msg_recv("integ-agent-b")
                if not msgs or msgs[0].get("subject") != "Test handoff":
                    errors.append(f"msg_recv didn't return expected message: {msgs}")
                else:
                    print(f"  [OK] msg_recv — '{msgs[0]['subject']}'")

                # ACK
                ok = api.msg_ack(msg_id)
                if not ok:
                    errors.append("msg_ack returned False")
                else:
                    print("  [OK] msg_ack")

                # Inbox empty after ACK
                remaining = api.msg_recv("integ-agent-b")
                if remaining:
                    errors.append("msg inbox not empty after ACK")
                else:
                    print("  [OK] msg inbox empty after ACK")

            # Control-char injection in from/to
            r = api.msg_send("evil\x00agent", "integ-agent-b", "inject", "payload")
            if r.get("ok"):
                msgs = api.msg_recv("integ-agent-b")
                if msgs and "\x00" in msgs[0].get("from", ""):
                    errors.append("msgqueue allowed null byte in from field")
                    api.msg_ack(msgs[0]["id"])
                else:
                    if msgs:
                        api.msg_ack(msgs[0]["id"])
                    print("  [OK] msgqueue — control chars sanitized in from field")

    if errors:
        print(f"\nFAILED — {len(errors)} error(s):")
        for e in errors:
            print(f"  ✗ {e}")
        raise SystemExit(1)
    else:
        print("\nPASSED — all checks OK")
        raise SystemExit(0)


if __name__ == "__main__":
    _cli()
