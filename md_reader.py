"""MemoryReader Daemon — Serves MD memory files over a Unix socket.

Agents call this instead of reading hot.md / warm files directly.
Caches file contents with a 60s TTL to avoid redundant disk reads.
Cache is invalidated via the INVALIDATE command sent by md_writer.py.

Socket: /tmp/agent-memory-reader.sock

Protocol (newline-delimited JSON):
  Request:  {"cmd": "GET_HOT"}
  Request:  {"cmd": "GET_WARM", "slug": "locus"}
  Request:  {"cmd": "GET_SESSION"}
  Request:  {"cmd": "GET_ALL_PROJECTS"}
  Request:  {"cmd": "INVALIDATE", "key": "hot"}
  Request:  {"cmd": "PING"}

  Response: {"ok": true, "content": "...", "cached": false}
  Response: {"ok": false, "error": "file not found"}

Usage (daemon):
    python3 md_reader.py

Usage (self-test):
    python3 md_reader.py --test-mode
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

logger = logging.getLogger("md-reader")

# ── Paths ──────────────────────────────────────────────────

MEMORY_DIR = Path(os.path.expanduser("~/.gemini/memory"))
HOT_FILE = MEMORY_DIR / "hot.md"
SESSION_FILE = MEMORY_DIR / "session.md"
PROJECTS_DIR = MEMORY_DIR / "projects"

CONTEXT_BRIEF_FILE = MEMORY_DIR / "session_context.md"

SOCKET_PATH = "/tmp/agent-memory-reader.sock"

# Cache TTL in seconds
CACHE_TTL_S = 60

# Maximum message size (64 KB should be more than enough for any MD file)
MAX_MSG_BYTES = 65_536


# ── Cache ──────────────────────────────────────────────────

class _CacheEntry:
    __slots__ = ("content", "expires_at")

    def __init__(self, content: str, ttl: float = CACHE_TTL_S) -> None:
        self.content = content
        self.expires_at = time.monotonic() + ttl

    def is_valid(self) -> bool:
        return time.monotonic() < self.expires_at


class FileCache:
    """Simple TTL cache mapping cache-key → file content."""

    def __init__(self) -> None:
        self._store: dict[str, _CacheEntry] = {}

    def get(self, key: str) -> str | None:
        entry = self._store.get(key)
        if entry and entry.is_valid():
            return entry.content
        self._store.pop(key, None)
        return None

    def set(self, key: str, content: str) -> None:
        self._store[key] = _CacheEntry(content)

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)
        logger.debug("Cache invalidated: %s", key)

    def stats(self) -> dict:
        now = time.monotonic()
        valid = sum(1 for e in self._store.values() if e.expires_at > now)
        return {"total": len(self._store), "valid": valid}


# ── File Readers ───────────────────────────────────────────

def _read_file(path: Path) -> str:
    """Read a file, raising FileNotFoundError if absent."""
    if not path.exists():
        raise FileNotFoundError(f"{path} not found")
    return path.read_text(encoding="utf-8")


def _list_warm_slugs() -> list[str]:
    """Return all warm file slugs (filenames without .md)."""
    if not PROJECTS_DIR.exists():
        return []
    return [p.stem for p in PROJECTS_DIR.glob("*.md")]


def _parse_hot_projects(content: str) -> list[dict]:
    """Extract project rows from hot.md ACTIVE PROJECTS table."""
    projects = []
    in_table = False
    for line in content.splitlines():
        stripped = line.strip()
        if "| Project" in stripped:
            in_table = True
            continue
        if in_table and stripped.startswith("|---"):
            continue
        if in_table and stripped.startswith("|"):
            cols = [c.strip() for c in stripped.split("|") if c.strip()]
            if len(cols) >= 4:
                projects.append({
                    "name": cols[0],
                    "location": cols[1].strip("`"),
                    "status": cols[2],
                    "warm_file": cols[3].strip("`"),
                })
        elif in_table and not stripped.startswith("|"):
            break
    return projects


# ── Command Handlers ────────────────────────────────────────

async def _handle_command(
    cmd_obj: dict,
    cache: FileCache,
) -> dict:
    cmd = cmd_obj.get("cmd", "")

    if cmd == "PING":
        return {"ok": True, "pong": True}

    if cmd == "INVALIDATE":
        key = cmd_obj.get("key", "")
        cache.invalidate(key)
        return {"ok": True}

    if cmd == "GET_HOT":
        cached = cache.get("hot")
        if cached is not None:
            return {"ok": True, "content": cached, "cached": True}
        try:
            content = _read_file(HOT_FILE)
            cache.set("hot", content)
            return {"ok": True, "content": content, "cached": False}
        except FileNotFoundError as e:
            return {"ok": False, "error": str(e)}

    if cmd == "GET_SESSION":
        cached = cache.get("session")
        if cached is not None:
            return {"ok": True, "content": cached, "cached": True}
        try:
            content = _read_file(SESSION_FILE)
            cache.set("session", content)
            return {"ok": True, "content": content, "cached": False}
        except FileNotFoundError as e:
            return {"ok": False, "error": str(e)}

    if cmd == "GET_WARM":
        slug = cmd_obj.get("slug", "").strip().lower()
        if not slug:
            return {"ok": False, "error": "slug required"}
        # Sanitize slug — alphanumeric + hyphens only
        if not re.match(r"^[a-z0-9\-]+$", slug):
            return {"ok": False, "error": "invalid slug"}
        cache_key = f"warm:{slug}"
        cached = cache.get(cache_key)
        if cached is not None:
            return {"ok": True, "content": cached, "cached": True}
        warm_path = PROJECTS_DIR / f"{slug}.md"
        try:
            content = _read_file(warm_path)
            cache.set(cache_key, content)
            return {"ok": True, "content": content, "cached": False}
        except FileNotFoundError:
            return {"ok": False, "error": f"No warm file for slug '{slug}'"}

    if cmd == "GET_ALL_PROJECTS":
        cached = cache.get("all_projects")
        if cached is not None:
            return {"ok": True, "projects": json.loads(cached), "cached": True}
        try:
            hot_content = _read_file(HOT_FILE)
            projects = _parse_hot_projects(hot_content)
            slugs = _list_warm_slugs()
            result = {
                "projects": projects,
                "warm_slugs": slugs,
            }
            cache.set("all_projects", json.dumps(result))
            return {"ok": True, **result, "cached": False}
        except FileNotFoundError as e:
            return {"ok": False, "error": str(e)}

    if cmd == "GET_CONTEXT_BRIEF":
        cached = cache.get("context_brief")
        if cached is not None:
            return {"ok": True, "content": cached, "cached": True}
        try:
            content = _read_file(CONTEXT_BRIEF_FILE)
            cache.set("context_brief", content)
            return {"ok": True, "content": content, "cached": False}
        except FileNotFoundError:
            # Brief not built yet — return a minimal placeholder
            return {
                "ok": True,
                "content": "## LIVE CONTEXT\n\n> Context brief not yet built. Run once: python3 context_recall.py --once",
                "cached": False,
            }

    return {"ok": False, "error": f"Unknown command: {cmd!r}"}


# ── Connection Handler ──────────────────────────────────────

async def _handle_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    cache: FileCache,
) -> None:
    peer = writer.get_extra_info("peername", "unknown")
    try:
        raw = await asyncio.wait_for(reader.read(MAX_MSG_BYTES), timeout=5.0)
        if not raw:
            return

        try:
            cmd_obj = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as e:
            response = {"ok": False, "error": f"JSON parse error: {e}"}
        else:
            response = await _handle_command(cmd_obj, cache)

        encoded = json.dumps(response).encode("utf-8") + b"\n"
        writer.write(encoded)
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


# ── Daemon Entry ────────────────────────────────────────────

async def run_reader(
    shutdown_event: asyncio.Event | None = None,
    socket_path: str = SOCKET_PATH,
) -> None:
    """Async server loop for agent_memory_daemon integration."""
    cache = FileCache()
    _shutdown = shutdown_event or asyncio.Event()

    # Remove stale socket
    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass

    def _connection_cb(r, w):
        asyncio.ensure_future(_handle_connection(r, w, cache))

    server = await asyncio.start_unix_server(_connection_cb, path=socket_path)
    os.chmod(socket_path, 0o600)  # Owner-only
    logger.info("MemoryReader listening on %s", socket_path)

    # Run until shutdown
    await _shutdown.wait()

    server.close()
    await server.wait_closed()
    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass
    logger.info("MemoryReader stopped.")


# ── Self-Test ───────────────────────────────────────────────

async def _self_test(socket_path: str = "/tmp/agent-memory-reader-test.sock") -> bool:
    """Spin up a reader server, send commands, verify responses."""
    import tempfile

    logger.info("Running MemoryReader self-test...")
    shutdown = asyncio.Event()

    # Create temp hot.md
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        hot = tmp / "hot.md"
        hot.write_text(
            "# HOT MEMORY\n\n## ACTIVE PROJECTS\n\n"
            "| Project | Location | Status | Warm File |\n"
            "|---------|----------|--------|-----------|\n"
            "| TestProj | `~/Desktop/test/` | Active | `projects/testproj.md` |\n\n"
            "## RECENT LESSONS\n\n- Use async for everything.\n"
        )
        projects_dir = tmp / "projects"
        projects_dir.mkdir()
        (projects_dir / "testproj.md").write_text("# TestProj\n\nStatus: Active\n")

        # Monkeypatch globals for test
        global HOT_FILE, SESSION_FILE, PROJECTS_DIR
        _orig_hot, _orig_sess, _orig_proj = HOT_FILE, SESSION_FILE, PROJECTS_DIR
        HOT_FILE = hot
        SESSION_FILE = tmp / "session.md"
        PROJECTS_DIR = projects_dir

        try:
            # Start server
            server_task = asyncio.create_task(run_reader(shutdown, socket_path))
            await asyncio.sleep(0.1)  # Let server bind

            async def _call(cmd_obj: dict) -> dict:
                r, w = await asyncio.open_unix_connection(socket_path)
                w.write(json.dumps(cmd_obj).encode() + b"\n")
                await w.drain()
                raw = await r.read(MAX_MSG_BYTES)
                w.close()
                await w.wait_closed()
                return json.loads(raw.decode())

            # Test PING
            resp = await _call({"cmd": "PING"})
            assert resp.get("pong"), f"PING failed: {resp}"

            # Test GET_HOT
            resp = await _call({"cmd": "GET_HOT"})
            assert resp["ok"], f"GET_HOT failed: {resp}"
            assert "HOT MEMORY" in resp["content"], "hot.md content mismatch"
            assert not resp["cached"], "First read should not be cached"

            # Test cache hit
            resp2 = await _call({"cmd": "GET_HOT"})
            assert resp2["cached"], "Second read should be cached"

            # Test INVALIDATE
            await _call({"cmd": "INVALIDATE", "key": "hot"})
            resp3 = await _call({"cmd": "GET_HOT"})
            assert not resp3["cached"], "After invalidation should be uncached"

            # Test GET_WARM
            resp = await _call({"cmd": "GET_WARM", "slug": "testproj"})
            assert resp["ok"], f"GET_WARM failed: {resp}"
            assert "TestProj" in resp["content"]

            # Test GET_WARM missing slug
            resp = await _call({"cmd": "GET_WARM", "slug": "nonexistent"})
            assert not resp["ok"], "Should fail for missing warm file"

            # Test GET_ALL_PROJECTS
            resp = await _call({"cmd": "GET_ALL_PROJECTS"})
            assert resp["ok"], f"GET_ALL_PROJECTS failed: {resp}"
            assert len(resp["projects"]) >= 1

            # Test bad command
            resp = await _call({"cmd": "NONSENSE"})
            assert not resp["ok"], "Unknown command should fail"

            logger.info("MemoryReader self-test PASSED")
            return True

        except Exception as e:
            logger.error("MemoryReader self-test FAILED: %s", e)
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

    parser = argparse.ArgumentParser(description="MemoryReader Daemon")
    parser.add_argument("--test-mode", action="store_true", help="Run self-test")
    parser.add_argument("--socket", default=SOCKET_PATH, help="Socket path")
    args = parser.parse_args()

    if args.test_mode:
        success = asyncio.run(_self_test())
        raise SystemExit(0 if success else 1)

    asyncio.run(run_reader(socket_path=args.socket))


if __name__ == "__main__":
    main()
