"""config.py — Single source of truth for all Agent Memory Kit paths.

Every daemon and API client imports from here. Override defaults via
environment variables — useful for non-standard installs or testing.

Usage:
    from config import MEMORY_DIR, CORTEX_DB, SOCKET_DIR, CORTEX_ROOT, ...
"""
from __future__ import annotations

import os
from pathlib import Path

# ── Base Directories ──────────────────────────────────────────

# Root of all memory markdown files (hot.md, warm files, session state)
MEMORY_DIR: str = os.environ.get(
    "AGENT_MEMORY_DIR",
    str(Path.home() / ".gemini" / "memory"),
)

# Root of CortexDB SQLite store
CORTEX_DB_DIR: str = os.environ.get(
    "AGENT_CORTEX_DIR",
    str(Path.home() / ".cortexdb"),
)

# Path to CortexDB Python package (for sys.path injection)
CORTEX_ROOT: str = os.environ.get(
    "AGENT_CORTEX_ROOT",
    str(Path.home() / "Desktop" / "Agent_System" / "DB-Memory" / "CortexDB"),
)

# Directory for Unix domain sockets (must be on a local filesystem)
SOCKET_DIR: str = os.environ.get("AGENT_SOCKET_DIR", "/tmp")

# ── Derived Paths ─────────────────────────────────────────────

HOT_MD:          str = os.path.join(MEMORY_DIR, "hot.md")
ARCHIVE_MD:      str = os.path.join(MEMORY_DIR, "archive.md")
SESSION_MD:      str = os.path.join(MEMORY_DIR, "session.md")
PROJECTS_DIR:    str = os.path.join(MEMORY_DIR, "projects")

CORTEX_DB:       str = os.path.join(CORTEX_DB_DIR, "agent_system.db")
DAEMON_LOG:      str = os.path.join(CORTEX_DB_DIR, "memory-daemon.log")

# ── SQLite State Files ────────────────────────────────────────

TASKQUEUE_DB:    str = os.path.join(MEMORY_DIR, "taskqueue.db")
LOOP_LEDGER_DB:  str = os.path.join(MEMORY_DIR, "loop_ledger.db")
GIT_WATCHER_DB:  str = os.path.join(MEMORY_DIR, "git_watcher_state.db")
MSGQUEUE_DB:     str = os.path.join(MEMORY_DIR, "agent_msgqueue.db")

# ── Unix Socket Paths ─────────────────────────────────────────

READER_SOCKET:         str = os.path.join(SOCKET_DIR, "agent-memory-reader.sock")
WRITER_SOCKET:         str = os.path.join(SOCKET_DIR, "agent-memory-writer.sock")
COORD_SOCKET:          str = os.path.join(SOCKET_DIR, "agent-coord.sock")
TASKQUEUE_SOCKET:      str = os.path.join(SOCKET_DIR, "agent-taskqueue.sock")
LOOP_DETECTOR_SOCKET:  str = os.path.join(SOCKET_DIR, "agent-loop-detector.sock")
GIT_WATCHER_SOCKET:    str = os.path.join(SOCKET_DIR, "agent-git-watcher.sock")
PRESSURE_SOCKET:       str = os.path.join(SOCKET_DIR, "agent-context-pressure.sock")
MSGQUEUE_SOCKET:       str = os.path.join(SOCKET_DIR, "agent-msgqueue.sock")
PG_BROADCAST_SOCKET:   str = os.path.join(SOCKET_DIR, "agent-pg-broadcast.sock")

# ── Daemon Tuning ─────────────────────────────────────────────

# Loop detector: how many consecutive identical tool calls = a loop
LOOP_THRESHOLD: int = int(os.environ.get("AGENT_LOOP_THRESHOLD", "3"))

# Git watcher: poll interval in seconds
GIT_POLL_INTERVAL: int = int(os.environ.get("AGENT_GIT_POLL_INTERVAL", "60"))

# Context pressure: safe token limit before flush recommendation
PRESSURE_SAFE_LIMIT: int = int(os.environ.get("AGENT_PRESSURE_LIMIT", "150000"))

# Message queue TTL in seconds (default: 48h)
MSG_TTL_S: int = int(os.environ.get("AGENT_MSG_TTL", str(172_800)))


def ensure_dirs() -> None:
    """Create all required directories if they don't exist."""
    for d in [MEMORY_DIR, CORTEX_DB_DIR, PROJECTS_DIR]:
        Path(d).mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    print("Agent Memory Kit — Configuration")
    print(f"  MEMORY_DIR:    {MEMORY_DIR}")
    print(f"  CORTEX_DB_DIR: {CORTEX_DB_DIR}")
    print(f"  CORTEX_ROOT:   {CORTEX_ROOT}")
    print(f"  SOCKET_DIR:    {SOCKET_DIR}")
    print(f"  CORTEX_DB:     {CORTEX_DB}")
    print(f"  HOT_MD:        {HOT_MD}")
    ensure_dirs()
    print("  Directories: OK")
