#!/usr/bin/env python3
"""
Memory freshness checker.

Run at the start of each session to detect stale warm files.
Compares the last-modified time of each warm project file against
the actual project directory's most recently modified file.

Also queries CortexDB for recent project memory access patterns
as a supplementary freshness signal.

Outputs warnings for stale entries so the agent knows to re-read
from source rather than trusting memory.

Usage:
    python3 freshness.py
"""

from __future__ import annotations

import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

MEMORY_DIR = Path(__file__).parent
HOT_FILE = MEMORY_DIR / "hot.md"
PROJECTS_DIR = MEMORY_DIR / "projects"

# How old a warm file can be relative to project changes before warning
STALE_THRESHOLD_HOURS = 24


def get_project_locations() -> dict[str, str]:
    """
    Parse hot.md to extract project name → location mappings.

    Expects a markdown table with columns: Project | Location | Status | Warm File
    """
    if not HOT_FILE.exists():
        return {}

    projects = {}
    in_table = False
    for line in HOT_FILE.read_text().splitlines():
        stripped = line.strip()

        # Detect table rows (skip header and separator)
        if "|" in stripped and "Project" in stripped:
            in_table = True
            continue
        if in_table and stripped.startswith("|---"):
            continue
        if in_table and "|" in stripped:
            cells = [c.strip() for c in stripped.split("|") if c.strip()]
            if len(cells) >= 2:
                name = cells[0]
                location = cells[1].strip("`").replace("~/", os.path.expanduser("~/"))
                projects[name] = location
        elif in_table and "|" not in stripped:
            in_table = False

    return projects


def get_latest_mtime(directory: str) -> datetime | None:
    """Get the most recent modification time of any file in the directory tree."""
    dir_path = Path(directory)
    if not dir_path.exists():
        return None

    latest = None
    skip_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv"}

    for root, dirs, files in os.walk(dir_path):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for f in files:
            filepath = Path(root) / f
            try:
                mtime = datetime.fromtimestamp(filepath.stat().st_mtime)
                if latest is None or mtime > latest:
                    latest = mtime
            except OSError:
                continue

    return latest


def check_freshness() -> list[dict]:
    """
    Check each warm file against its project directory.

    Returns a list of findings (warnings and OK statuses).
    """
    projects = get_project_locations()
    findings = []

    for name, location in projects.items():
        slug = name.lower().replace(" ", "-")
        warm_file = PROJECTS_DIR / f"{slug}.md"

        finding = {
            "project": name,
            "location": location,
            "status": "unknown",
            "detail": "",
        }

        # Check warm file exists
        if not warm_file.exists():
            finding["status"] = "missing"
            finding["detail"] = f"No warm file at {warm_file}"
            findings.append(finding)
            continue

        warm_mtime = datetime.fromtimestamp(warm_file.stat().st_mtime)

        # Check project directory exists
        if not Path(location).exists():
            finding["status"] = "warning"
            finding["detail"] = f"Project directory not found: {location}"
            findings.append(finding)
            continue

        # Compare modification times
        project_mtime = get_latest_mtime(location)
        if project_mtime is None:
            finding["status"] = "warning"
            finding["detail"] = "Could not read project directory"
            findings.append(finding)
            continue

        age_hours = (project_mtime - warm_mtime).total_seconds() / 3600

        if age_hours > STALE_THRESHOLD_HOURS:
            finding["status"] = "stale"
            finding["detail"] = (
                f"Warm file last updated {warm_mtime.strftime('%Y-%m-%d %H:%M')}. "
                f"Project modified {project_mtime.strftime('%Y-%m-%d %H:%M')} "
                f"({age_hours:.0f}h newer). "
                f"RE-READ FROM SOURCE before acting on memory."
            )
        elif age_hours > 0:
            finding["status"] = "slightly_stale"
            finding["detail"] = (
                f"Project modified {age_hours:.1f}h after warm file. "
                f"Minor drift possible."
            )
        else:
            finding["status"] = "fresh"
            finding["detail"] = "Warm file is up to date."

        # Supplement with CortexDB access data
        cortex_detail = _cortex_access_detail(name)
        if cortex_detail:
            finding["detail"] += f" {cortex_detail}"

        findings.append(finding)

    return findings


def print_report(findings: list[dict]) -> None:
    """Print the freshness report."""
    if not findings:
        print("No projects found in hot.md.")
        return

    icons = {
        "fresh": "✓",
        "slightly_stale": "~",
        "stale": "⚠",
        "missing": "✗",
        "warning": "?",
        "unknown": "?",
    }

    has_stale = False
    print("\n  Memory Freshness Check")
    print("  " + "─" * 50)

    for f in findings:
        icon = icons.get(f["status"], "?")
        print(f"  [{icon}] {f['project']}")
        print(f"      {f['detail']}")
        if f["status"] in ("stale", "missing"):
            has_stale = True

    print("  " + "─" * 50)

    if has_stale:
        print("\n  ⚠ STALE MEMORY DETECTED.")
        print("  Do NOT trust warm files marked stale.")
        print("  Re-read project files from disk before making changes.")
    else:
        print("\n  ✓ All memories are fresh.")

    print()


def main() -> None:
    findings = check_freshness()
    print_report(findings)

    # Exit with code 1 if any stale entries found (useful for scripting)
    if any(f["status"] in ("stale", "missing") for f in findings):
        sys.exit(1)


def _cortex_access_detail(project_name: str) -> str:
    """Query CortexDB for recent access data on this project. Best-effort."""
    try:
        import sys as _sys
        import os as _os
        _cortex_root = _os.path.expanduser(
            "$AGENT_CORTEX_ROOT"
        )
        if _cortex_root not in _sys.path:
            _sys.path.insert(0, _cortex_root)
        from cortex.engine import Cortex

        db_path = _os.path.expanduser("~/.cortexdb/agent_system.db")
        if not _os.path.exists(db_path):
            return ""

        cortex = Cortex(db_path)
        slug = project_name.lower().replace(" ", "-")

        # Tag-based search: look for memories tagged with project slug
        all_mems = cortex.list_all(limit=200)
        project_mems = [
            m for m in all_mems
            if slug in m.tags or "project-state" in m.tags
            and project_name.lower() in m.content.lower()
        ]
        cortex.close()

        if project_mems:
            latest = max(project_mems, key=lambda m: m.last_accessed)
            age_h = (time.time() - latest.last_accessed) / 3600
            return f"CortexDB: last accessed {age_h:.0f}h ago ({latest.access_count} accesses)."
        return "CortexDB: no memories for this project."
    except Exception:
        return ""


if __name__ == "__main__":
    main()
