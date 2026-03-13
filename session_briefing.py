"""Session Briefing — Context-aware lesson and workspace briefing.

Generates a session-start briefing by:
1. Surfacing relevant lessons from CortexDB for each active project
2. Querying Mnemos for recent workspace changes
3. Extracting open threads from hot.md

Output: ~/.gemini/memory/last_briefing.md

Usage:
    python3 session_briefing.py
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

_CORTEX_ROOT = os.environ.get("AGENT_CORTEX_ROOT", os.path.expanduser("~/.cortexdb"))
_MEMORY_ROOT = os.path.expanduser("~/.gemini/memory")
for p in [_CORTEX_ROOT, _MEMORY_ROOT]:
    if p not in sys.path:
        sys.path.insert(0, p)

from lesson_engine import LessonEngine

HOT_FILE = Path(_MEMORY_ROOT) / "hot.md"
BRIEFING_FILE = Path(_MEMORY_ROOT) / "last_briefing.md"
MNEMOS_URL = "http://localhost:7700"

# Maximum age for Mnemos events to include (hours)
EVENT_LOOKBACK_HOURS = 24


def parse_active_projects() -> list[dict[str, str]]:
    """Parse hot.md for active project names and locations."""
    if not HOT_FILE.exists():
        return []

    projects = []
    in_table = False
    for line in HOT_FILE.read_text().splitlines():
        stripped = line.strip()
        if "|" in stripped and "Project" in stripped:
            in_table = True
            continue
        if in_table and stripped.startswith("|---"):
            continue
        if in_table and "|" in stripped:
            cells = [c.strip() for c in stripped.split("|") if c.strip()]
            if len(cells) >= 3:
                projects.append({
                    "name": cells[0],
                    "location": cells[1].strip("`"),
                    "status": cells[2],
                })
        elif in_table and "|" not in stripped:
            in_table = False

    return projects


def parse_open_threads() -> list[str]:
    """Extract RECENT LESSONS section from hot.md."""
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


def query_mnemos_events() -> list[dict]:
    """Query Mnemos for recent workspace events. Best-effort."""
    try:
        import urllib.request
        import json
        from datetime import datetime, timedelta

        since = (
            datetime.now() - timedelta(hours=EVENT_LOOKBACK_HOURS)
        ).isoformat()

        url = f"{MNEMOS_URL}/projects"
        req = urllib.request.Request(url, method="GET")
        req.add_header("Accept", "application/json")

        with urllib.request.urlopen(req, timeout=3) as resp:
            projects = json.loads(resp.read())

        events = []
        for proj in projects:
            slug = proj.get("slug", "")
            evt_url = f"{MNEMOS_URL}/projects/{slug}/events?limit=20"
            req = urllib.request.Request(evt_url, method="GET")
            req.add_header("Accept", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=3) as resp:
                    data = json.loads(resp.read())
                    for evt in data.get("events", []):
                        events.append({
                            "project": slug,
                            "type": evt.get("event_type", ""),
                            "path": evt.get("path", ""),
                        })
            except Exception:
                continue

        return events
    except Exception:
        return []


def generate_briefing() -> str:
    """Generate the full session briefing."""
    lines = [
        "# Session Briefing",
        f"*Generated: {time.strftime('%Y-%m-%d %H:%M')}*",
        "",
    ]

    # Active projects + lessons
    projects = parse_active_projects()
    engine = LessonEngine()

    if projects:
        lines.append("## Active Projects")
        lines.append("")
        for proj in projects:
            lines.append(f"### {proj['name']} — {proj['status']}")
            lines.append(f"Location: `{proj['location']}`")
            lines.append("")

            # Surface relevant lessons
            lessons = engine.surface(proj["name"], limit=3)
            if lessons:
                lines.append("**Relevant lessons:**")
                for lesson in lessons:
                    content = lesson.content[:120]
                    lines.append(f"- {content}")
                lines.append("")
            else:
                lines.append("*No specific lessons for this project.*")
                lines.append("")

    engine.close()

    # Workspace changes from Mnemos
    events = query_mnemos_events()
    if events:
        lines.append("## Recent Workspace Changes")
        lines.append("")
        seen_paths = set()
        for evt in events[:20]:
            path = evt.get("path", "")
            if path not in seen_paths:
                lines.append(
                    f"- `{evt['project']}` {evt['type']}: "
                    f"`{os.path.basename(path)}`"
                )
                seen_paths.add(path)
        lines.append("")

    # Open threads
    threads = parse_open_threads()
    if threads:
        lines.append("## Active Lessons / Open Threads")
        lines.append("")
        for thread in threads:
            lines.append(f"- {thread}")
        lines.append("")

    return "\n".join(lines)


def write_briefing() -> str:
    """Generate and write the briefing file. Returns the path."""
    content = generate_briefing()
    BRIEFING_FILE.write_text(content)
    return str(BRIEFING_FILE)


def main() -> None:
    path = write_briefing()
    print(f"Session briefing written to {path}")
    line_count = len(BRIEFING_FILE.read_text().splitlines())
    print(f"  {line_count} lines")


if __name__ == "__main__":
    main()
