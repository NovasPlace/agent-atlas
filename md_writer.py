"""MemoryWriter Daemon — Atomic validated MD writes over a Unix socket.

Agents send write commands here instead of touching MD files directly.
All writes are:
  - Validated for structure before commit
  - Atomic (write temp → os.rename)
  - Serialized via an asyncio queue (no concurrent corruption)
  - Followed by an INVALIDATE signal to md_reader.py

Socket: /tmp/agent-memory-writer.sock

Protocol (newline-delimited JSON):
  {"cmd": "UPDATE_HOT", "session_summary": "...", "open_threads": [...]}
  {"cmd": "UPDATE_SESSION", "current_work": "...", "files_touched": [...],
   "pending_actions": [...], "critical_context": [...]}
  {"cmd": "UPDATE_WARM", "slug": "locus", "status": "...", "decisions": [...]}
  {"cmd": "APPEND_LESSON", "lesson": "ffmpeg setsar=1 for Shorts"}
  {"cmd": "REGISTER_PROJECT", "name": "...", "location": "...", "status": "...",
   "warm_file": "projects/foo.md"}
  {"cmd": "PING"}

  Response: {"ok": true}
  Response: {"ok": false, "error": "..."}

Usage (daemon):
    python3 md_writer.py

Usage (self-test):
    python3 md_writer.py --test-mode
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

logger = logging.getLogger("md-writer")

# ── Paths ──────────────────────────────────────────────────

MEMORY_DIR = Path(os.path.expanduser("~/.gemini/memory"))
HOT_FILE = MEMORY_DIR / "hot.md"
SESSION_FILE = MEMORY_DIR / "session.md"
PROJECTS_DIR = MEMORY_DIR / "projects"

SOCKET_PATH = "/tmp/agent-memory-writer.sock"
READER_SOCKET_PATH = "/tmp/agent-memory-reader.sock"

# Maximum inbound message (1 MB — warm files can be large)
MAX_MSG_BYTES = 1_048_576

# ── Atomic Write ───────────────────────────────────────────

def _atomic_write(path: Path, content: str) -> None:
    """Write content atomically via temp file + rename."""
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.rename(tmp_path, path)


# ── Reader Cache Invalidation ──────────────────────────────

async def _invalidate_reader_cache(key: str) -> None:
    """Send an INVALIDATE command to md_reader if it's running."""
    try:
        r, w = await asyncio.wait_for(
            asyncio.open_unix_connection(READER_SOCKET_PATH), timeout=1.0
        )
        payload = json.dumps({"cmd": "INVALIDATE", "key": key}).encode() + b"\n"
        w.write(payload)
        await w.drain()
        await asyncio.wait_for(r.read(256), timeout=1.0)
        w.close()
        await w.wait_closed()
    except Exception:
        pass  # Reader may not be running; not a write failure


# ── Write Handlers ─────────────────────────────────────────

def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M")


def _update_hot(content: str, cmd: dict) -> str:
    """Replace SESSION SUMMARY section and update timestamp in hot.md."""
    session_summary = cmd.get("session_summary", "").strip()
    open_threads = cmd.get("open_threads")  # Optional list of strings

    if not session_summary:
        raise ValueError("session_summary is required")

    today = datetime.now().strftime("%Y-%m-%d")
    new_summary_block = f"## SESSION SUMMARY ({today})\n\n- {session_summary}\n"

    # Replace existing SESSION SUMMARY block or append one
    summary_pattern = re.compile(
        r"## SESSION SUMMARY \(\d{4}-\d{2}-\d{2}\)\n.*?(?=\n## |\n---|\Z)",
        re.DOTALL,
    )
    if summary_pattern.search(content):
        content = summary_pattern.sub(new_summary_block.rstrip(), content)
    else:
        # Append before the divider or at end
        divider_pos = content.rfind("\n---\n")
        if divider_pos != -1:
            content = content[:divider_pos] + "\n\n" + new_summary_block + content[divider_pos:]
        else:
            content = content.rstrip() + "\n\n" + new_summary_block

    # Update OPEN THREADS if provided
    if open_threads is not None and isinstance(open_threads, list):
        thread_lines = "\n".join(f"- **{t}**" for t in open_threads)
        thread_block = f"## OPEN THREADS\n\n{thread_lines}\n"
        thread_pattern = re.compile(
            r"## OPEN THREADS\n.*?(?=\n## |\n---|\Z)", re.DOTALL
        )
        if thread_pattern.search(content):
            content = thread_pattern.sub(thread_block.rstrip(), content)

    # Update last-updated timestamp
    content = re.sub(
        r"\*Last updated: .*?\*",
        f"*Last updated: {_now_str()}*",
        content,
    )
    if "*Last updated:" not in content:
        content = content.rstrip() + f"\n\n*Last updated: {_now_str()}*\n"

    return content


def _append_lesson(content: str, lesson: str) -> str:
    """Append a lesson bullet to the RECENT LESSONS section of hot.md."""
    lesson = lesson.strip()
    if not lesson:
        raise ValueError("lesson text is required")

    # Duplicate guard
    if lesson in content:
        return content  # Already present, no-op

    lessons_pattern = re.compile(
        r"(## RECENT LESSONS\n)(.*?)(?=\n## |\n---|\Z)", re.DOTALL
    )
    match = lessons_pattern.search(content)
    if match:
        section_start = match.start()
        section_end = match.end()
        existing_body = match.group(2).rstrip()
        new_body = existing_body + f"\n- {lesson}"
        content = (
            content[:section_start]
            + f"## RECENT LESSONS\n{new_body}\n"
            + content[section_end:]
        )
    else:
        content = content.rstrip() + f"\n\n## RECENT LESSONS\n\n- {lesson}\n"

    return content


def _register_project(content: str, cmd: dict) -> str:
    """Add a new row to the ACTIVE PROJECTS table in hot.md."""
    name = cmd.get("name", "").strip()
    location = cmd.get("location", "").strip()
    status = cmd.get("status", "Active").strip()
    warm_file = cmd.get("warm_file", "—").strip()

    if not name or not location:
        raise ValueError("name and location are required")

    # Duplicate guard
    if f"| {name} |" in content:
        return content

    new_row = f"| {name} | `{location}` | {status} | `{warm_file}` |"

    # Find the last row of the table and insert after it
    table_pattern = re.compile(
        r"(\| Project \|.*?\n(?:\|[-| ]+\|\n))((?:\|.*?\n)*)",
        re.DOTALL,
    )
    match = table_pattern.search(content)
    if match:
        table_end = match.end()
        content = content[:table_end] + new_row + "\n" + content[table_end:]
    else:
        content = content.rstrip() + f"\n\n## ACTIVE PROJECTS\n\n| Project | Location | Status | Warm File |\n|---------|----------|--------|-----------|\n{new_row}\n"

    return content


def _build_session(cmd: dict) -> str:
    """Build a fresh session.md from structured fields."""
    current_work = cmd.get("current_work", "_none_").strip() or "_none_"
    files_touched = cmd.get("files_touched", [])
    pending_actions = cmd.get("pending_actions", [])
    critical_context = cmd.get("critical_context", [])

    def _bullet_list(items: list) -> str:
        if not items:
            return "_none_"
        return "\n".join(f"- {i}" for i in items)

    return (
        "# Active Session State\n\n"
        "> Written mid-conversation by the agent. Read back after truncation.\n"
        "> This file is ephemeral — overwritten each session. Not archival.\n\n"
        f"## Current Work\n{current_work}\n\n"
        f"## Files Touched\n{_bullet_list(files_touched)}\n\n"
        f"## Pending Actions\n{_bullet_list(pending_actions)}\n\n"
        f"## Context That Must Not Be Lost\n{_bullet_list(critical_context)}\n\n"
        "---\n"
        f"*Last written: {_now_str()}*\n"
    )


def _update_warm(path: Path, cmd: dict) -> str:
    """Update status and/or decisions section in a warm file."""
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    status = cmd.get("status", "").strip()
    decisions = cmd.get("decisions", [])

    if status:
        # Replace Status: line if present
        content = re.sub(r"(?m)^(\*\*Status\*\*:).*$", f"**Status**: {status}", content)
        if "**Status**:" not in content:
            content = content.rstrip() + f"\n\n**Status**: {status}\n"

    if decisions:
        decisions_block = "## Recent Decisions\n\n" + "\n".join(
            f"- {d}" for d in decisions
        ) + "\n"
        decision_pattern = re.compile(
            r"## Recent Decisions\n.*?(?=\n## |\n---|\Z)", re.DOTALL
        )
        if decision_pattern.search(content):
            content = decision_pattern.sub(decisions_block.rstrip(), content)
        else:
            content = content.rstrip() + "\n\n" + decisions_block

    # Update timestamp
    content = re.sub(
        r"\*Last updated: \d{4}-\d{2}-\d{2}\*",
        f"*Last updated: {datetime.now().strftime('%Y-%m-%d')}*",
        content,
    )

    return content


# ── Command Dispatcher ──────────────────────────────────────

async def _dispatch(cmd_obj: dict, pg_notify_cb=None) -> dict:
    """Dispatch a write command and optionally broadcast via pg_notify.

    pg_notify_cb: callable(cmd: str, meta: dict) — injected by run_writer()
    when the BroadcastDaemon is running. Called only on success.
    """
    cmd = cmd_obj.get("cmd", "")

    if cmd == "PING":
        return {"ok": True, "pong": True}

    # ── UPDATE_HOT ──
    if cmd == "UPDATE_HOT":
        try:
            content = HOT_FILE.read_text(encoding="utf-8") if HOT_FILE.exists() else ""
            new_content = _update_hot(content, cmd_obj)
            _atomic_write(HOT_FILE, new_content)
            await _invalidate_reader_cache("hot")
            logger.info("hot.md updated (session summary)")
            result = {"ok": True}
            if pg_notify_cb:
                pg_notify_cb(cmd, {})
            return result
        except Exception as e:
            logger.error("UPDATE_HOT failed: %s", e)
            return {"ok": False, "error": str(e)}

    # ── APPEND_LESSON ──
    if cmd == "APPEND_LESSON":
        try:
            content = HOT_FILE.read_text(encoding="utf-8") if HOT_FILE.exists() else ""
            new_content = _append_lesson(content, cmd_obj.get("lesson", ""))
            _atomic_write(HOT_FILE, new_content)
            await _invalidate_reader_cache("hot")
            logger.info("Lesson appended to hot.md")
            result = {"ok": True}
            if pg_notify_cb:
                # Include a truncated preview so subscribers know what changed
                preview = cmd_obj.get("lesson", "")[:120]
                pg_notify_cb(cmd, {"preview": preview})
            return result
        except Exception as e:
            logger.error("APPEND_LESSON failed: %s", e)
            return {"ok": False, "error": str(e)}

    # ── REGISTER_PROJECT ──
    if cmd == "REGISTER_PROJECT":
        try:
            content = HOT_FILE.read_text(encoding="utf-8") if HOT_FILE.exists() else ""
            new_content = _register_project(content, cmd_obj)
            _atomic_write(HOT_FILE, new_content)
            await _invalidate_reader_cache("hot")
            await _invalidate_reader_cache("all_projects")
            logger.info("Project '%s' registered in hot.md", cmd_obj.get("name"))
            result = {"ok": True}
            if pg_notify_cb:
                pg_notify_cb(cmd, {"name": cmd_obj.get("name", "")})
            return result
        except Exception as e:
            logger.error("REGISTER_PROJECT failed: %s", e)
            return {"ok": False, "error": str(e)}

    # ── UPDATE_SESSION ──
    if cmd == "UPDATE_SESSION":
        try:
            new_content = _build_session(cmd_obj)
            _atomic_write(SESSION_FILE, new_content)
            await _invalidate_reader_cache("session")
            logger.info("session.md updated")
            result = {"ok": True}
            if pg_notify_cb:
                work = cmd_obj.get("current_work", "")[:80]
                pg_notify_cb(cmd, {"current_work": work})
            return result
        except Exception as e:
            logger.error("UPDATE_SESSION failed: %s", e)
            return {"ok": False, "error": str(e)}

    # ── UPDATE_WARM ──
    if cmd == "UPDATE_WARM":
        slug = cmd_obj.get("slug", "").strip().lower()
        if not slug or not re.match(r"^[a-z0-9\-]+$", slug):
            return {"ok": False, "error": "invalid or missing slug"}
        warm_path = PROJECTS_DIR / f"{slug}.md"
        try:
            new_content = _update_warm(warm_path, cmd_obj)
            _atomic_write(warm_path, new_content)
            await _invalidate_reader_cache(f"warm:{slug}")
            logger.info("warm file '%s' updated", slug)
            result = {"ok": True}
            if pg_notify_cb:
                pg_notify_cb(cmd, {"slug": slug, "status": cmd_obj.get("status", "")})
            return result
        except Exception as e:
            logger.error("UPDATE_WARM '%s' failed: %s", slug, e)
            return {"ok": False, "error": str(e)}

    return {"ok": False, "error": f"Unknown command: {cmd!r}"}


# ── Connection Handler ──────────────────────────────────────

async def _handle_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    write_queue: asyncio.Queue,
) -> None:
    peer = writer.get_extra_info("peername", "unknown")
    try:
        raw = await asyncio.wait_for(reader.read(MAX_MSG_BYTES), timeout=10.0)
        if not raw:
            return

        try:
            cmd_obj = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as e:
            response = {"ok": False, "error": f"JSON parse error: {e}"}
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
            return

        # Non-mutating commands bypass the queue
        if cmd_obj.get("cmd") == "PING":
            response = {"ok": True, "pong": True}
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
            return

        # Mutating commands go through the serialized write queue
        result_future: asyncio.Future = asyncio.get_event_loop().create_future()
        await write_queue.put((cmd_obj, result_future))
        response = await asyncio.wait_for(result_future, timeout=15.0)

        writer.write(json.dumps(response).encode() + b"\n")
        await writer.drain()

    except asyncio.TimeoutError:
        logger.warning("Connection timed out from %s", peer)
    except Exception as e:
        logger.error("Connection error from %s: %s", peer, e)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


# ── Write Queue Worker ─────────────────────────────────────

async def _write_worker(
    write_queue: asyncio.Queue,
    shutdown_event: asyncio.Event,
    on_write: Callable[[str], None] | None = None,
    pg_notify_cb=None,
) -> None:
    """Drain the write queue serially so writes never overlap."""
    while not shutdown_event.is_set() or not write_queue.empty():
        try:
            cmd_obj, future = await asyncio.wait_for(write_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break

        try:
            result = await _dispatch(cmd_obj, pg_notify_cb=pg_notify_cb)
            future.set_result(result)
            if on_write and result.get("ok"):
                on_write(cmd_obj.get("cmd", ""))
        except Exception as e:
            logger.error("Write worker error: %s", e)
            if not future.done():
                future.set_result({"ok": False, "error": str(e)})
        finally:
            write_queue.task_done()


# ── Daemon Entry ────────────────────────────────────────────

async def run_writer(
    shutdown_event: asyncio.Event | None = None,
    socket_path: str = SOCKET_PATH,
    on_write: Callable[[str], None] | None = None,
    pg_notify_cb=None,
) -> None:
    """Async server loop for agent_memory_daemon integration.

    on_write: optional callback called after each successful write.
             Used by md_indexer to trigger CortexDB dual-write.
    pg_notify_cb: optional callable(cmd, meta) for real-time PG broadcast.
                  Injected by agent_memory_daemon when BroadcastDaemon is running.
    """
    _shutdown = shutdown_event or asyncio.Event()
    write_queue: asyncio.Queue = asyncio.Queue()

    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass

    def _connection_cb(r, w):
        asyncio.ensure_future(_handle_connection(r, w, write_queue))

    server = await asyncio.start_unix_server(_connection_cb, path=socket_path)
    os.chmod(socket_path, 0o600)  # Owner-only
    logger.info("MemoryWriter listening on %s", socket_path)

    worker_task = asyncio.create_task(
        _write_worker(write_queue, _shutdown, on_write, pg_notify_cb=pg_notify_cb)
    )

    await _shutdown.wait()

    server.close()
    await server.wait_closed()
    await worker_task

    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass
    logger.info("MemoryWriter stopped.")


# ── Self-Test ───────────────────────────────────────────────

async def _self_test(socket_path: str = "/tmp/agent-memory-writer-test.sock") -> bool:
    """Start writer server, send commands, verify file mutations."""
    import tempfile

    logger.info("Running MemoryWriter self-test...")
    shutdown = asyncio.Event()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        hot = tmp / "hot.md"
        hot.write_text(
            "# HOT MEMORY\n\n## ACTIVE PROJECTS\n\n"
            "| Project | Location | Status | Warm File |\n"
            "|---------|----------|--------|-----------|\n"
            "| Alpha | `/tmp/alpha/` | Active | `projects/alpha.md` |\n\n"
            "## RECENT LESSONS\n\n- Always test.\n\n"
            "---\n*Last updated: 2025-01-01T00:00*\n"
        )
        projects_dir = tmp / "projects"
        projects_dir.mkdir()
        session_path = tmp / "session.md"

        global HOT_FILE, SESSION_FILE, PROJECTS_DIR
        _orig_hot, _orig_sess, _orig_proj = HOT_FILE, SESSION_FILE, PROJECTS_DIR
        HOT_FILE = hot
        SESSION_FILE = session_path
        PROJECTS_DIR = projects_dir

        try:
            server_task = asyncio.create_task(run_writer(shutdown, socket_path))
            await asyncio.sleep(0.1)

            async def _call(cmd_obj: dict) -> dict:
                r, w = await asyncio.open_unix_connection(socket_path)
                w.write(json.dumps(cmd_obj).encode() + b"\n")
                await w.drain()
                raw = await r.read(65536)
                w.close()
                await w.wait_closed()
                return json.loads(raw.decode())

            # Test PING
            resp = await _call({"cmd": "PING"})
            assert resp.get("pong"), f"PING failed: {resp}"

            # Test APPEND_LESSON
            resp = await _call({"cmd": "APPEND_LESSON", "lesson": "Test lesson from self-test"})
            assert resp["ok"], f"APPEND_LESSON failed: {resp}"
            assert "Test lesson from self-test" in hot.read_text()

            # Test duplicate lesson guard
            resp = await _call({"cmd": "APPEND_LESSON", "lesson": "Test lesson from self-test"})
            assert resp["ok"], "Duplicate lesson guard should succeed silently"
            assert hot.read_text().count("Test lesson from self-test") == 1, "Duplicate should not be written"

            # Test REGISTER_PROJECT
            resp = await _call({
                "cmd": "REGISTER_PROJECT",
                "name": "NewProject",
                "location": "~/Desktop/NewProject/",
                "status": "Active",
                "warm_file": "projects/newproject.md",
            })
            assert resp["ok"], f"REGISTER_PROJECT failed: {resp}"
            assert "NewProject" in hot.read_text()

            # Test UPDATE_SESSION
            resp = await _call({
                "cmd": "UPDATE_SESSION",
                "current_work": "Testing the writer daemon",
                "files_touched": ["md_writer.py"],
                "pending_actions": ["Run full test suite"],
                "critical_context": ["Writer daemon validated by self-test"],
            })
            assert resp["ok"], f"UPDATE_SESSION failed: {resp}"
            sess = session_path.read_text()
            assert "Testing the writer daemon" in sess
            assert "md_writer.py" in sess

            # Test UPDATE_HOT
            resp = await _call({
                "cmd": "UPDATE_HOT",
                "session_summary": "Self-test completed successfully",
            })
            assert resp["ok"], f"UPDATE_HOT failed: {resp}"
            assert "Self-test completed successfully" in hot.read_text()

            # Test UPDATE_WARM (create new warm file)
            warm_path = projects_dir / "alpha.md"
            warm_path.write_text("# Alpha\n\n**Status**: Prototype\n\n*Last updated: 2025-01-01*\n")
            resp = await _call({
                "cmd": "UPDATE_WARM",
                "slug": "alpha",
                "status": "Active — post self-test",
                "decisions": ["Use async sockets", "TTL cache = 60s"],
            })
            assert resp["ok"], f"UPDATE_WARM failed: {resp}"
            warm_content = warm_path.read_text()
            assert "Active — post self-test" in warm_content
            assert "Use async sockets" in warm_content

            # Test invalid slug
            resp = await _call({"cmd": "UPDATE_WARM", "slug": "../etc/passwd", "status": "x"})
            assert not resp["ok"], "Invalid slug should fail"

            logger.info("MemoryWriter self-test PASSED")
            return True

        except Exception as e:
            logger.error("MemoryWriter self-test FAILED: %s", e)
            import traceback
            traceback.print_exc()
            return False

        finally:
            HOT_FILE, SESSION_FILE, PROJECTS_DIR = _orig_hot, _orig_sess, _orig_proj
            shutdown.set()
            await server_task


# ── CLI ─────────────────────────────────────────────────────

def main() -> None:
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="MemoryWriter Daemon")
    parser.add_argument("--test-mode", action="store_true", help="Run self-test")
    parser.add_argument("--socket", default=SOCKET_PATH, help="Socket path")
    args = parser.parse_args()

    if args.test_mode:
        success = asyncio.run(_self_test())
        raise SystemExit(0 if success else 1)

    asyncio.run(run_writer(socket_path=args.socket))


if __name__ == "__main__":
    main()
