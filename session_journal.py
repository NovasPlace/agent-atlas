"""Session Journal — Last-session continuity artifact.

Writes ~/.gemini/memory/last_session.md with:
- What project was being worked on
- Key files touched
- Open threads from hot.md
- Recent CortexDB episodic memories

Run via maintain.py timer or standalone.

Usage:
    python3 session_journal.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

_CORTEX_ROOT = os.environ.get("AGENT_CORTEX_ROOT", os.path.expanduser("~/.cortexdb"))
_MEMORY_ROOT = os.path.expanduser("~/.gemini/memory")
for p in [_CORTEX_ROOT, _MEMORY_ROOT]:
    if p not in sys.path:
        sys.path.insert(0, p)

from cortex.engine import Cortex

HOT_FILE = Path(_MEMORY_ROOT) / "hot.md"
JOURNAL_FILE = Path(_MEMORY_ROOT) / "last_session.md"
MNEMOS_URL = "http://localhost:7700"
DEFAULT_DB_PATH = os.path.expanduser("~/.cortexdb/agent_system.db")

# How far back to look for session data (hours)
SESSION_LOOKBACK_HOURS = 12


def get_recent_mnemos_sessions() -> list[dict]:
    """Query Mnemos for recent sessions. Best-effort."""
    try:
        import urllib.request
        import json

        url = f"{MNEMOS_URL}/sessions?limit=5"
        req = urllib.request.Request(url, method="GET")
        req.add_header("Accept", "application/json")

        with urllib.request.urlopen(req, timeout=3) as resp:
            return json.loads(resp.read())
    except Exception:
        return []


def get_recent_episodic_memories(db_path: str = DEFAULT_DB_PATH) -> list:
    """Get recent episodic memories from CortexDB."""
    if not os.path.exists(db_path):
        return []

    cortex = Cortex(db_path)
    cutoff = time.time() - (SESSION_LOOKBACK_HOURS * 3600)

    all_mems = cortex.list_all(limit=50)
    recent = [
        m for m in all_mems
        if m.created_at >= cutoff and m.type == "episodic"
    ]
    cortex.close()
    return recent


def parse_open_threads() -> list[str]:
    """Extract lessons/threads from hot.md."""
    if not HOT_FILE.exists():
        return []

    content = HOT_FILE.read_text()
    threads = []
    in_section = False

    for line in content.splitlines():
        stripped = line.strip()
        if "## RECENT LESSONS" in stripped:
            in_section = True
            continue
        if in_section and stripped.startswith("## "):
            break
        if in_section and stripped.startswith("- "):
            threads.append(stripped[2:])

    return threads


def generate_journal() -> str:
    """Generate the session journal."""
    lines = [
        "# Last Session Journal",
        f"*Generated: {time.strftime('%Y-%m-%d %H:%M')}*",
        "",
    ]

    # Recent Mnemos sessions
    sessions = get_recent_mnemos_sessions()
    if sessions:
        lines.append("## Recent Agent Sessions")
        lines.append("")
        for sess in sessions[:5]:
            project = sess.get("project_slug", "unknown")
            agent = sess.get("agent_id", "unknown")
            status = sess.get("status", "unknown")
            lines.append(f"- **{project}** by `{agent}` — {status}")
        lines.append("")

    # Recent episodic memories (decisions, events)
    memories = get_recent_episodic_memories()
    if memories:
        lines.append("## Recent Decisions & Events")
        lines.append("")
        for mem in memories[:10]:
            content = mem.content[:120]
            lines.append(f"- {content}")
        lines.append("")

    # Open threads
    threads = parse_open_threads()
    if threads:
        lines.append("## Open Threads")
        lines.append("")
        for thread in threads:
            lines.append(f"- {thread}")
        lines.append("")

    if len(lines) <= 3:
        lines.append("*No recent session data available.*")
        lines.append("")

    return "\n".join(lines)


def write_journal() -> str:
    """Generate and write the session journal. Returns the path."""
    content = generate_journal()
    JOURNAL_FILE.write_text(content)
    return str(JOURNAL_FILE)


def main() -> None:
    path = write_journal()
    print(f"Session journal written to {path}")


if __name__ == "__main__":
    main()
