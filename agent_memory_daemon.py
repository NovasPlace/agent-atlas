"""Agent Memory Daemon — Single long-running background process.

Runs all continuous memory tasks in one asyncio event loop:
- Memory sync: updates warm project files every 30 minutes
- Hallucination scanner: scans recently modified files every 15 minutes
- Session journal: updates session continuity every hour
- Subconscious watcher: auto-persists file change context to CortexDB (30s poll)
- Consolidator: archives stale sessions, prunes hot.md, budget checks (2min poll)
- MemoryReader: serves hot/warm/session MD files over Unix socket (cached)
- MemoryWriter: receives atomic validated writes from agents over Unix socket
- ContextIndexer: dual-writes all MD changes to CortexDB automatically
- ContextRecallDaemon: rebuilds live context brief from CortexDB every 90s
- AgentCoordDaemon: persistent presence registry and soft file locks for multi-agent runs
- AgentTaskQueueDaemon: SQLite-backed deferred/recurring task queue

Designed to run as a systemd user service.

Usage:
    python3 agent_memory_daemon.py
    python3 agent_memory_daemon.py --dry-run  # Log only, no writes
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time

_MEMORY_ROOT = os.path.expanduser("~/.gemini/memory")
_CORTEX_ROOT = os.environ.get("AGENT_CORTEX_ROOT", os.path.expanduser("~/.cortexdb"))
for p in [_MEMORY_ROOT, _CORTEX_ROOT]:
    if p not in sys.path:
        sys.path.insert(0, p)

# Intervals in seconds
SYNC_INTERVAL = 1800        # 30 minutes
SCAN_INTERVAL = 900         # 15 minutes
JOURNAL_INTERVAL = 3600     # 1 hour

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("agent-memory-daemon")

_shutdown = asyncio.Event()


def _handle_signal(signum, frame):
    logger.info("Received signal %d, shutting down...", signum)
    _shutdown.set()


async def run_memory_sync(dry_run: bool = False) -> None:
    """Periodic warm-file sync."""
    from memory_sync import sync_once
    while not _shutdown.is_set():
        try:
            logger.info("Running memory sync...")
            result = sync_once(dry_run=dry_run)
            logger.info("Memory sync complete: %s", result)
        except Exception as e:
            logger.error("Memory sync failed: %s", e)
        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=SYNC_INTERVAL)
            break
        except asyncio.TimeoutError:
            pass


async def run_hallucination_scanner(dry_run: bool = False) -> None:
    """Periodic hallucination scanning."""
    from hallucination_scanner import run_scan
    while not _shutdown.is_set():
        try:
            logger.info("Running hallucination scanner...")
            result = run_scan()
            logger.info(
                "Scan complete: %d files, %d unresolved imports",
                result["files_scanned"],
                result["unresolved_imports"],
            )
        except Exception as e:
            logger.error("Hallucination scanner failed: %s", e)
        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=SCAN_INTERVAL)
            break
        except asyncio.TimeoutError:
            pass


async def run_session_journal(dry_run: bool = False) -> None:
    """Periodic session journal update."""
    from session_journal import write_journal
    while not _shutdown.is_set():
        try:
            logger.info("Updating session journal...")
            path = write_journal()
            logger.info("Session journal written to %s", path)
        except Exception as e:
            logger.error("Session journal failed: %s", e)
        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=JOURNAL_INTERVAL)
            break
        except asyncio.TimeoutError:
            pass


async def main(dry_run: bool = False) -> None:
    """Run all background tasks concurrently."""
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info("Agent Memory Daemon starting...")
    logger.info(
        "Intervals: sync=%ds, scan=%ds, journal=%ds",
        SYNC_INTERVAL, SCAN_INTERVAL, JOURNAL_INTERVAL,
    )

    # Import here to avoid circular imports at module level
    from subconscious import run_subconscious
    from consolidator import run_consolidator
    from md_reader import run_reader
    from md_writer import run_writer
    from md_indexer import run_indexer
    from context_recall import run_recall_daemon
    from agent_coord import run_coord_daemon
    from agent_taskqueue import run_taskqueue_daemon
    from pg_broadcast import run_broadcast_daemon, get_pg_notifier
    from loop_detector import run_loop_detector
    from git_watcher import run_git_watcher
    from context_pressure import run_pressure_daemon
    from agent_msgqueue import run_msgqueue

    # Shared queue: md_writer puts write-event names here; indexer reads them
    write_event_queue: asyncio.Queue = asyncio.Queue()

    def _on_write(cmd: str) -> None:
        """Callback fired by MemoryWriter after each successful write."""
        # Put a non-blocking item on the queue for the ContextIndexer.
        # We don't have the full cmd_obj here so we send just the cmd name;
        # indexer uses it for logging. Full indexer integration happens inside
        # md_writer's _dispatch which calls on_write after the write.
        try:
            write_event_queue.put_nowait((cmd, {}))
        except asyncio.QueueFull:
            pass

    # Build the pg_notify callback that md_writer will call on each write.
    # The broadcaster may not be ready yet on first write, but get_pg_notifier()
    # is idempotent — it returns the singleton once run_broadcast_daemon starts it.
    def _pg_notify_cb(cmd: str, meta: dict) -> None:
        try:
            notifier = get_pg_notifier()
            notifier.notify(cmd, meta)
        except Exception as exc:
            logger.warning("pg_notify_cb error (non-fatal): %s", exc)

    if dry_run:
        logger.info("[DRY RUN] Sub-daemons (reader/writer/indexer/broadcast) skipped in dry-run mode")
        tasks = [
            asyncio.create_task(run_memory_sync(dry_run)),
            asyncio.create_task(run_hallucination_scanner(dry_run)),
            asyncio.create_task(run_session_journal(dry_run)),
            asyncio.create_task(run_subconscious(dry_run=dry_run, shutdown_event=_shutdown)),
            asyncio.create_task(run_consolidator(dry_run=dry_run, shutdown_event=_shutdown)),
        ]
    else:
        tasks = [
            asyncio.create_task(run_memory_sync(dry_run)),
            asyncio.create_task(run_hallucination_scanner(dry_run)),
            asyncio.create_task(run_session_journal(dry_run)),
            asyncio.create_task(run_subconscious(dry_run=dry_run, shutdown_event=_shutdown)),
            asyncio.create_task(run_consolidator(dry_run=dry_run, shutdown_event=_shutdown)),
            asyncio.create_task(run_reader(shutdown_event=_shutdown)),
            asyncio.create_task(run_writer(
                shutdown_event=_shutdown,
                on_write=_on_write,
                pg_notify_cb=_pg_notify_cb,
            )),
            asyncio.create_task(run_indexer(write_event_queue, shutdown_event=_shutdown)),
            asyncio.create_task(run_recall_daemon(shutdown_event=_shutdown)),
            asyncio.create_task(run_coord_daemon(shutdown_event=_shutdown)),
            asyncio.create_task(run_taskqueue_daemon(shutdown_event=_shutdown)),
            asyncio.create_task(run_broadcast_daemon(shutdown_event=_shutdown)),
            asyncio.create_task(run_loop_detector(shutdown_event=_shutdown)),
            asyncio.create_task(run_git_watcher(shutdown_event=_shutdown)),
            asyncio.create_task(run_pressure_daemon(shutdown_event=_shutdown)),
            asyncio.create_task(run_msgqueue(shutdown_event=_shutdown)),
        ]

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass

    logger.info("Agent Memory Daemon stopped (all %d tasks completed).", len(tasks))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Agent Memory Daemon")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Log actions without making changes",
    )
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))
