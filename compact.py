#!/usr/bin/env python3
"""
Memory compaction script.

Maintains the tiered memory system:
  1. Validates hot.md stays under the line budget
  2. Moves completed projects from hot index to cold archive
  3. Prunes stale lessons from hot memory
  4. Reports memory usage statistics

Usage:
    python3 compact.py [--dry-run] [--archive PROJECT_NAME]
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

MEMORY_DIR = Path(__file__).parent
HOT_FILE = MEMORY_DIR / "hot.md"
ARCHIVE_FILE = MEMORY_DIR / "archive.md"
PROJECTS_DIR = MEMORY_DIR / "projects"

HOT_LINE_BUDGET = 80
WARN_THRESHOLD = 40


def count_lines(filepath: Path) -> int:
    """Count non-empty lines in a file."""
    if not filepath.exists():
        return 0
    return sum(1 for line in filepath.read_text().splitlines() if line.strip())


def get_project_files() -> list[Path]:
    """List all warm-tier project files."""
    if not PROJECTS_DIR.exists():
        return []
    return sorted(PROJECTS_DIR.glob("*.md"))


def report_usage() -> None:
    """Print memory usage statistics."""
    hot_lines = count_lines(HOT_FILE)
    hot_bytes = HOT_FILE.stat().st_size if HOT_FILE.exists() else 0
    archive_lines = count_lines(ARCHIVE_FILE)
    projects = get_project_files()

    print("╔══════════════════════════════════════════╗")
    print("║       MEMORY SYSTEM — STATUS             ║")
    print("╠══════════════════════════════════════════╣")
    print(f"║  Hot (always loaded):                     ║")
    print(f"║    {hot_lines:3d}/{HOT_LINE_BUDGET} lines  "
          f"({hot_bytes:,d} bytes)              ║")

    status = "OK"
    if hot_lines > HOT_LINE_BUDGET:
        status = "⚠ OVER BUDGET"
    elif hot_lines > WARN_THRESHOLD:
        status = "⚠ NEAR LIMIT"
    print(f"║    Status: {status:<30s}║")

    print(f"║                                          ║")
    print(f"║  Warm (per-project):                      ║")
    total_warm = 0
    for p in projects:
        lines = count_lines(p)
        total_warm += lines
        print(f"║    {p.stem:<28s} {lines:3d} lines ║")
    if not projects:
        print(f"║    (none)                                ║")

    print(f"║                                          ║")
    print(f"║  Cold (archive):                          ║")
    print(f"║    {archive_lines:3d} lines                          ║")

    print(f"║                                          ║")
    total = hot_lines + total_warm + archive_lines
    print(f"║  Total memory: {total:3d} lines                 ║")
    print("╚══════════════════════════════════════════╝")


def archive_project(project_name: str, dry_run: bool = False) -> None:
    """Move a project from warm tier to cold archive."""
    slug = project_name.lower().replace(" ", "-")
    project_file = PROJECTS_DIR / f"{slug}.md"

    if not project_file.exists():
        print(f"Error: No warm file found at {project_file}")
        sys.exit(1)

    # Read project content
    content = project_file.read_text()

    # Extract first non-empty, non-heading line as summary
    summary = ""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith(">"):
            summary = stripped[:80]
            break

    archive_entry = (
        f"\n---\n\n"
        f"## {project_name}\n"
        f"- **Archived**: {datetime.now().strftime('%Y-%m-%d')}\n"
        f"- **Summary**: {summary}\n"
        f"- **Original file**: `{project_file}`\n"
    )

    if dry_run:
        print(f"[DRY RUN] Would archive '{project_name}':")
        print(f"  - Remove from hot.md project table")
        print(f"  - Append summary to archive.md")
        print(f"  - Delete {project_file}")
        return

    # Append to archive
    with open(ARCHIVE_FILE, "a") as f:
        f.write(archive_entry)

    # Remove project file
    project_file.unlink()
    print(f"Archived '{project_name}' → {ARCHIVE_FILE}")
    print(f"Deleted warm file: {project_file}")
    print("NOTE: Manually remove the row from hot.md project table.")


def check_budget() -> None:
    """Check if hot.md is within budget."""
    lines = count_lines(HOT_FILE)
    if lines > HOT_LINE_BUDGET:
        print(f"\n⚠ hot.md is {lines - HOT_LINE_BUDGET} lines over budget "
              f"({lines}/{HOT_LINE_BUDGET}).")
        print("  Consider:")
        print("  - Archiving completed projects")
        print("  - Compressing lesson entries")
        print("  - Moving open threads to relevant project files")
    elif lines > WARN_THRESHOLD:
        print(f"\n⚠ hot.md approaching limit ({lines}/{HOT_LINE_BUDGET}).")
    else:
        print(f"\n✓ hot.md within budget ({lines}/{HOT_LINE_BUDGET}).")


def report_lesson_stats() -> None:
    """Print CortexDB lesson statistics. Best-effort."""
    try:
        from lesson_engine import LessonEngine
        engine = LessonEngine()
        stats = engine.stats()
        stale = engine.stale_check()

        print("\n╔══════════════════════════════════════════╗")
        print("║       LESSON ENGINE — STATUS             ║")
        print("╠══════════════════════════════════════════╣")
        print(f"║  Total lessons: {stats['total']:<24d}  ║")
        print(f"║  Stale lessons: {stats['stale']:<24d}  ║")
        print(f"║  Avg importance: {stats['avg_importance']:<23.2f} ║")
        print(f"║  Avg accesses: {stats['avg_access_count']:<25.1f}║")

        if stats['by_emotion']:
            print(f"║                                          ║")
            print(f"║  By emotion:                             ║")
            for emotion, count in stats['by_emotion'].items():
                print(f"║    {emotion:<28s} {count:3d}    ║")

        if stale:
            print(f"║                                          ║")
            print(f"║  Stale candidates:                       ║")
            for s in stale[:5]:
                print(f"║    {s.content[:34]:<34s}    ║")

        print("╚══════════════════════════════════════════╝")
        engine.close()
    except Exception as e:
        print(f"\n⚠ CortexDB lesson stats unavailable: {e}")


def cortex_decay_for_project(project_name: str) -> None:
    """Run CortexDB decay on memories tagged with a project. Best-effort."""
    try:
        from memory_bridge import get_bridge
        bridge = get_bridge()
        pruned = bridge.cortex.decay()
        if pruned:
            print(f"  CortexDB: pruned {pruned} decayed memories")
    except Exception as e:
        print(f"  ⚠ CortexDB decay failed: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Memory compaction and maintenance"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would happen without making changes"
    )
    parser.add_argument(
        "--archive", metavar="PROJECT",
        help="Archive a completed project (move warm → cold)"
    )
    parser.add_argument(
        "--lessons", action="store_true",
        help="Show lesson engine statistics from CortexDB"
    )
    args = parser.parse_args()

    report_usage()

    if args.archive:
        archive_project(args.archive, dry_run=args.dry_run)
        cortex_decay_for_project(args.archive)

    if args.lessons:
        report_lesson_stats()

    check_budget()


if __name__ == "__main__":
    main()
