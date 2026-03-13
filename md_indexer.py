"""ContextIndexer Daemon — Auto dual-write MD changes to CortexDB.

Listens on an asyncio queue fed by md_writer.py. When a write event
arrives, it reads the changed file and stores a CortexDB memory.

No socket required — communicates via an in-process asyncio.Queue
when running inside agent_memory_daemon.py.

Supported write → memory type mapping:
  APPEND_LESSON    → procedural memory (importance=0.75, emotion=frustration)
  UPDATE_SESSION   → episodic memory (importance=0.35)
  UPDATE_HOT       → semantic project-state memory (importance=0.4)
  REGISTER_PROJECT → semantic project-state memory (importance=0.5)
  UPDATE_WARM      → semantic project-state memory (importance=0.4)

Usage (daemon integration):
    from md_indexer import ContextIndexer
    indexer = ContextIndexer(event_queue=write_event_queue)
    await indexer.run(shutdown_event)

Usage (self-test):
    python3 md_indexer.py --test-mode
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("md-indexer")

# ── Paths ──────────────────────────────────────────────────

_MEMORY_DIR = Path(os.path.expanduser("~/.gemini/memory"))
_CORTEX_ROOT = Path(os.environ.get("AGENT_CORTEX_ROOT", os.path.expanduser("~/.cortexdb")))
_DEFAULT_DB = os.path.expanduser("~/.cortexdb/agent_system.db")

for _p in [str(_CORTEX_ROOT), str(_MEMORY_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

HOT_FILE = _MEMORY_DIR / "hot.md"
SESSION_FILE = _MEMORY_DIR / "session.md"
PROJECTS_DIR = _MEMORY_DIR / "projects"

# Debounce window — rapid writes for the same key are collapsed
DEBOUNCE_S = 5.0


# ── Memory Builders ─────────────────────────────────────────

def _build_lesson_memory(cmd_obj: dict) -> dict[str, Any] | None:
    lesson = cmd_obj.get("lesson", "").strip()
    if not lesson:
        return None
    return {
        "content": lesson,
        "type": "procedural",
        "tags": ["lesson", "hot-md"],
        "importance": 0.75,
        "emotion": "frustration",
        "source": "experienced",
        "confidence": 0.9,
        "context": "agent lesson appended via md_writer",
    }


def _build_session_memory(cmd_obj: dict) -> dict[str, Any] | None:
    current_work = cmd_obj.get("current_work", "").strip()
    critical = cmd_obj.get("critical_context", [])
    if not current_work and not critical:
        return None
    parts = [f"Agent active: {current_work}"] if current_work else []
    parts.extend(critical[:3])
    content = ". ".join(parts)[:500]
    return {
        "content": content,
        "type": "episodic",
        "tags": ["session", "agent-session"],
        "importance": 0.35,
        "emotion": "neutral",
        "source": "observed",
        "context": "session state written by agent",
    }


def _build_hot_summary_memory(cmd_obj: dict) -> dict[str, Any] | None:
    summary = cmd_obj.get("session_summary", "").strip()
    if not summary:
        return None
    return {
        "content": f"Session summary: {summary}",
        "type": "semantic",
        "tags": ["session-summary", "hot-md"],
        "importance": 0.4,
        "emotion": "neutral",
        "source": "experienced",
        "context": "hot.md session summary updated by agent",
    }


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _build_project_memory(cmd_obj: dict) -> dict[str, Any] | None:
    name = cmd_obj.get("name") or cmd_obj.get("slug", "")
    status = cmd_obj.get("status", "Active").strip()
    content = f"Project '{name}' status: {status}"
    if not name:
        return None
    return {
        "content": content[:500],
        "type": "semantic",
        "tags": ["project-state", _slugify(name)],
        "importance": 0.45,
        "emotion": "neutral",
        "source": "experienced",
        "context": "project state written via md_writer",
    }


_BUILDERS = {
    "APPEND_LESSON": _build_lesson_memory,
    "UPDATE_SESSION": _build_session_memory,
    "UPDATE_HOT": _build_hot_summary_memory,
    "REGISTER_PROJECT": _build_project_memory,
    "UPDATE_WARM": _build_project_memory,
}


# ── Indexer Class ───────────────────────────────────────────

class ContextIndexer:
    """Consumes write events and dual-writes to CortexDB.

    Integration:
        queue = asyncio.Queue()
        indexer = ContextIndexer(queue)
        await indexer.run(shutdown_event)

    Feed events:
        await queue.put(("APPEND_LESSON", {"lesson": "..."}))
    """

    def __init__(
        self,
        event_queue: asyncio.Queue,
        db_path: str = _DEFAULT_DB,
    ) -> None:
        self._queue = event_queue
        self._db_path = db_path
        self._cortex = None

    def _get_cortex(self):
        if self._cortex is None:
            from cortex.engine import Cortex
            self._cortex = Cortex(self._db_path)
        return self._cortex

    def _store(self, payload: dict[str, Any]) -> bool:
        """Store a single memory in CortexDB."""
        try:
            cortex = self._get_cortex()
            cortex.remember(**payload)
            logger.debug("Indexed: %s", payload["content"][:80])
            return True
        except Exception as e:
            logger.error("CortexDB write failed: %s", e)
            return False

    async def run(self, shutdown_event: asyncio.Event | None = None) -> None:
        """Drain the event queue and index memories until shutdown."""
        _shutdown = shutdown_event or asyncio.Event()
        logger.info("ContextIndexer started")

        while not _shutdown.is_set() or not self._queue.empty():
            try:
                cmd, cmd_obj = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            try:
                builder = _BUILDERS.get(cmd)
                if builder:
                    payload = builder(cmd_obj)
                    if payload:
                        self._store(payload)
                else:
                    logger.debug("No indexer builder for cmd: %s", cmd)
            except Exception as e:
                logger.error("Indexer error for cmd %s: %s", cmd, e)
            finally:
                self._queue.task_done()

        if self._cortex is not None:
            self._cortex.close()
            self._cortex = None

        logger.info("ContextIndexer stopped")


# ── Standalone Async Runner ─────────────────────────────────

async def run_indexer(
    event_queue: asyncio.Queue,
    shutdown_event: asyncio.Event | None = None,
    db_path: str = _DEFAULT_DB,
) -> None:
    """Entry point for agent_memory_daemon integration."""
    indexer = ContextIndexer(event_queue, db_path)
    await indexer.run(shutdown_event)


# ── Self-Test ───────────────────────────────────────────────

async def _self_test() -> bool:
    """Feed events to the indexer and verify CortexDB writes."""
    import tempfile

    logger.info("Running ContextIndexer self-test...")

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        queue: asyncio.Queue = asyncio.Queue()
        shutdown = asyncio.Event()
        indexer = ContextIndexer(queue, db_path)
        task = asyncio.create_task(indexer.run(shutdown))

        # Feed events
        await queue.put(("APPEND_LESSON", {"lesson": "Always use asyncio for daemon loops"}))
        await queue.put(("UPDATE_SESSION", {
            "current_work": "Testing md_indexer",
            "critical_context": ["Indexer validated by self-test"],
        }))
        await queue.put(("UPDATE_HOT", {"session_summary": "Indexer self-test passed"}))
        await queue.put(("REGISTER_PROJECT", {"name": "TestIndexer", "status": "Active"}))

        # Wait for queue to drain
        await queue.join()
        shutdown.set()
        await task

        # Verify in CortexDB
        from cortex.engine import Cortex
        cortex = Cortex(db_path)
        memories = cortex.recall("asyncio daemon", limit=10)
        cortex.close()

        lessons = [m for m in memories if "lesson" in m.tags]
        if not lessons:
            logger.error("No lesson memory found in CortexDB")
            return False

        logger.info(
            "ContextIndexer self-test PASSED: %d memories verified",
            len(memories),
        )
        return True

    except Exception as e:
        logger.error("ContextIndexer self-test FAILED: %s", e)
        import traceback
        traceback.print_exc()
        return False
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass


# ── CLI ─────────────────────────────────────────────────────

def main() -> None:
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="ContextIndexer Daemon")
    parser.add_argument("--test-mode", action="store_true", help="Run self-test")
    args = parser.parse_args()

    if args.test_mode:
        success = asyncio.run(_self_test())
        raise SystemExit(0 if success else 1)

    # Standalone mode — create queue and run
    queue: asyncio.Queue = asyncio.Queue()
    asyncio.run(run_indexer(queue))


if __name__ == "__main__":
    main()
