"""AgentCoordDaemon — Multi-agent presence registry and soft file locks.

Runs persistently. Keeps track of which agents are active, what they
are working on, and which files/resources they have claimed.

Locks are ADVISORY — agents that ignore them remain autonomous.
The goal is awareness, not hard blocking.

Socket: /tmp/agent-coord.sock

Protocol (newline-delimited JSON):
  PRESENCE  {agent_id, work, files?: [...]}  → heartbeat / announce
  WHO       {}                               → list active agents + claims
  CLAIM     {agent_id, path}                → soft-lock a path
  RELEASE   {agent_id, path}                → release a lock
  CLEAR     {agent_id}                      → agent signing off
  PING      {}                              → health check

Responses always include "ok": true/false.

Presence entries expire after PRESENCE_TTL_S seconds of silence.

Usage (daemon integration):
    from agent_coord import run_coord_daemon
    await run_coord_daemon(shutdown_event)

Usage (self-test):
    python3 agent_coord.py --test-mode
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("agent-coord")

SOCKET_PATH = "/tmp/agent-coord.sock"
PRESENCE_TTL_S = 300        # 5 minutes — stale entry cleanup
MAX_MSG_BYTES  = 32_768


# ── Data Model ──────────────────────────────────────────────

@dataclass
class AgentEntry:
    agent_id: str
    work: str = ""
    files: list[str] = field(default_factory=list)
    last_seen: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.last_seen = time.monotonic()

    def is_alive(self) -> bool:
        return (time.monotonic() - self.last_seen) < PRESENCE_TTL_S

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "work": self.work,
            "files": self.files,
            "last_seen_ago_s": round(time.monotonic() - self.last_seen, 1),
        }


# ── Registry ─────────────────────────────────────────────────

class AgentRegistry:
    """In-memory registry of active agents and their claimed files."""

    def __init__(self) -> None:
        self._agents: dict[str, AgentEntry] = {}
        # path → agent_id that claimed it
        self._claims: dict[str, str] = {}

    # ── Presence ─────────────────────────────────────────────

    def announce(self, agent_id: str, work: str, files: list[str]) -> None:
        if agent_id in self._agents:
            entry = self._agents[agent_id]
            entry.work = work
            entry.files = files
            entry.touch()
        else:
            self._agents[agent_id] = AgentEntry(
                agent_id=agent_id, work=work, files=files
            )
        logger.info("[%s] presence: %s", agent_id, work[:60])

    def clear(self, agent_id: str) -> None:
        """Agent signing off — remove presence and all its claims."""
        self._agents.pop(agent_id, None)
        released = [p for p, owner in self._claims.items() if owner == agent_id]
        for path in released:
            del self._claims[path]
        if released:
            logger.info("[%s] cleared %d claim(s)", agent_id, len(released))

    def who(self) -> list[dict]:
        """Return all live agents with their claimed paths."""
        self._reap_stale()
        return [e.to_dict() for e in self._agents.values()]

    # ── Locks ────────────────────────────────────────────────

    def claim(self, agent_id: str, path: str) -> dict:
        """Soft-lock path for agent_id.

        Returns {"ok": True} if claimed, or
                {"ok": False, "claimed_by": "<other_agent_id>"}.
        """
        existing = self._claims.get(path)
        if existing and existing != agent_id:
            # Check the claiming agent is still alive
            if existing in self._agents and self._agents[existing].is_alive():
                return {"ok": False, "claimed_by": existing}
            # Stale claim — reassign
            del self._claims[path]

        self._claims[path] = agent_id
        logger.info("[%s] claimed: %s", agent_id, path)
        return {"ok": True}

    def release(self, agent_id: str, path: str) -> dict:
        """Release a claimed path."""
        owner = self._claims.get(path)
        if owner and owner != agent_id:
            return {"ok": False, "error": f"Path claimed by {owner!r}, not {agent_id!r}"}
        if path in self._claims:
            del self._claims[path]
            logger.info("[%s] released: %s", agent_id, path)
        return {"ok": True}

    def claims_snapshot(self) -> dict[str, str]:
        """Return {path: agent_id} snapshot of all active claims."""
        return dict(self._claims)

    # ── Maintenance ──────────────────────────────────────────

    def _reap_stale(self) -> int:
        stale = [aid for aid, e in self._agents.items() if not e.is_alive()]
        for aid in stale:
            self.clear(aid)
            logger.info("Reaped stale agent: %s", aid)
        return len(stale)


# ── Path Sanitization ────────────────────────────────────────

def _sanitize_path(raw: str) -> str:
    """Strip leading slashes, null bytes, and path traversal sequences.

    Coord paths are advisory lock keys, not real filesystem paths, so we
    normalize them to simple relative-style strings. Rejects empty input.
    """
    # Remove null bytes and control characters
    cleaned = raw.replace("\x00", "").strip()
    # Collapse repeated slashes
    import re
    cleaned = re.sub(r"/+", "/", cleaned)
    # Remove traversal sequences
    parts = [p for p in cleaned.split("/") if p and p != ".."]
    return "/".join(parts) if parts else ""


# ── Command Handlers ─────────────────────────────────────────

async def _handle_command(cmd_obj: dict, registry: AgentRegistry) -> dict:
    cmd = cmd_obj.get("cmd", "")

    if cmd == "PING":
        return {"ok": True, "pong": True, "agents": len(registry._agents)}

    if cmd == "PRESENCE":
        agent_id = cmd_obj.get("agent_id", "").strip()
        if not agent_id:
            return {"ok": False, "error": "agent_id required"}
        work  = str(cmd_obj.get("work", ""))[:200]
        files = [str(f)[:200] for f in cmd_obj.get("files", [])[:20]]
        registry.announce(agent_id, work, files)
        return {"ok": True}

    if cmd == "WHO":
        return {"ok": True, "agents": registry.who(), "claims": registry.claims_snapshot()}

    if cmd == "CLAIM":
        agent_id = cmd_obj.get("agent_id", "").strip()
        path     = _sanitize_path(str(cmd_obj.get("path", "")))
        if not agent_id or not path:
            return {"ok": False, "error": "agent_id and non-empty path required"}
        return registry.claim(agent_id, path)

    if cmd == "RELEASE":
        agent_id = cmd_obj.get("agent_id", "").strip()
        path     = _sanitize_path(str(cmd_obj.get("path", "")))
        if not agent_id or not path:
            return {"ok": False, "error": "agent_id and non-empty path required"}
        return registry.release(agent_id, path)

    if cmd == "CLEAR":
        agent_id = cmd_obj.get("agent_id", "").strip()
        if not agent_id:
            return {"ok": False, "error": "agent_id required"}
        registry.clear(agent_id)
        return {"ok": True}

    return {"ok": False, "error": f"Unknown command: {cmd!r}"}


# ── Connection Handler ────────────────────────────────────────

async def _handle_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    registry: AgentRegistry,
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
            response = await _handle_command(cmd_obj, registry)

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


# ── Stale Reaper Loop ─────────────────────────────────────────

async def _reaper_loop(registry: AgentRegistry, shutdown: asyncio.Event) -> None:
    """Periodically reap stale agents (every 60s)."""
    while not shutdown.is_set():
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=60.0)
            break
        except asyncio.TimeoutError:
            reaped = registry._reap_stale()
            if reaped:
                logger.info("Reaper: cleared %d stale agent(s)", reaped)


# ── Daemon Entry ─────────────────────────────────────────────

async def run_coord_daemon(
    shutdown_event: asyncio.Event | None = None,
    socket_path: str = SOCKET_PATH,
) -> None:
    """Async server loop for agent_memory_daemon integration."""
    registry = AgentRegistry()
    _shutdown = shutdown_event or asyncio.Event()

    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass

    def _cb(r, w):
        asyncio.ensure_future(_handle_connection(r, w, registry))

    server = await asyncio.start_unix_server(_cb, path=socket_path)
    os.chmod(socket_path, 0o600)  # Owner-only: no other local user can connect
    logger.info("AgentCoordDaemon listening on %s", socket_path)

    reaper = asyncio.create_task(_reaper_loop(registry, _shutdown))

    await _shutdown.wait()

    reaper.cancel()
    server.close()
    await server.wait_closed()
    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass
    logger.info("AgentCoordDaemon stopped.")


# ── Self-Test ─────────────────────────────────────────────────

async def _self_test(socket_path: str = "/tmp/agent-coord-test.sock") -> bool:
    logger.info("Running AgentCoordDaemon self-test...")
    shutdown = asyncio.Event()
    server_task = asyncio.create_task(
        run_coord_daemon(shutdown, socket_path)
    )
    await asyncio.sleep(0.1)

    async def _call(payload: dict) -> dict:
        r, w = await asyncio.open_unix_connection(socket_path)
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

        # PRESENCE
        resp = await _call({"cmd": "PRESENCE", "agent_id": "agent-a",
                            "work": "Building toolbar", "files": ["src/toolbar.js"]})
        assert resp["ok"], f"PRESENCE failed: {resp}"

        # WHO
        resp = await _call({"cmd": "WHO"})
        assert resp["ok"] and len(resp["agents"]) == 1, f"WHO failed: {resp}"
        assert resp["agents"][0]["agent_id"] == "agent-a"

        # CLAIM success
        resp = await _call({"cmd": "CLAIM", "agent_id": "agent-a", "path": "src/toolbar.js"})
        assert resp["ok"], f"CLAIM failed: {resp}"

        # CLAIM conflict
        resp = await _call({"cmd": "CLAIM", "agent_id": "agent-b", "path": "src/toolbar.js"})
        assert not resp["ok"] and resp.get("claimed_by") == "agent-a", f"Conflict check failed: {resp}"

        # RELEASE
        resp = await _call({"cmd": "RELEASE", "agent_id": "agent-a", "path": "src/toolbar.js"})
        assert resp["ok"], f"RELEASE failed: {resp}"

        # CLAIM success after release
        resp = await _call({"cmd": "CLAIM", "agent_id": "agent-b", "path": "src/toolbar.js"})
        assert resp["ok"], f"CLAIM after release failed: {resp}"

        # CLEAR
        resp = await _call({"cmd": "CLEAR", "agent_id": "agent-a"})
        assert resp["ok"], f"CLEAR failed: {resp}"

        logger.info("AgentCoordDaemon self-test PASSED")
        return True

    except Exception as e:
        logger.error("AgentCoordDaemon self-test FAILED: %s", e)
        import traceback; traceback.print_exc()
        return False
    finally:
        shutdown.set()
        await server_task


# ── CLI ──────────────────────────────────────────────────────

def main() -> None:
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="AgentCoordDaemon")
    parser.add_argument("--test-mode", action="store_true")
    parser.add_argument("--socket", default=SOCKET_PATH)
    args = parser.parse_args()

    if args.test_mode:
        success = asyncio.run(_self_test())
        raise SystemExit(0 if success else 1)

    asyncio.run(run_coord_daemon(socket_path=args.socket))


if __name__ == "__main__":
    main()
