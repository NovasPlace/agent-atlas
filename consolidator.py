"""Memory Consolidation Daemon — Auto-maintain session and warm memory files.

Watches session.md for stale context, consolidates it into warm project
files, runs compaction checks, and cleans up expired session state.
Designed to run inside agent_memory_daemon.py alongside subconscious.py.

Responsibilities:
  1. Detect stale session.md (older than SESSION_STALE_HOURS) and archive it
  2. Auto-merge session file-touched entries into warm project files
  3. Run compact.py budget checks on a schedule
  4. Prune struck-through open threads from hot.md

Usage:
    python3 consolidator.py              # One-shot consolidation
    python3 consolidator.py --dry-run    # Log, no file writes
    python3 consolidator.py --test-mode  # Self-test
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("consolidator")

# ── Constants ──────────────────────────────────────────────

MEMORY_DIR = Path(os.path.expanduser("~/.gemini/memory"))
HOT_FILE = MEMORY_DIR / "hot.md"
SESSION_FILE = MEMORY_DIR / "session.md"
PROJECTS_DIR = MEMORY_DIR / "projects"

# Session state older than this is considered stale
SESSION_STALE_HOURS = 2

# How often to run consolidation (seconds)
CONSOLIDATION_INTERVAL_S = 120

# How often to run compact budget check (seconds)
COMPACT_INTERVAL_S = 600

# Maximum age before session.md gets wiped (hours)
SESSION_MAX_AGE_HOURS = 24


# ── Session Staleness ──────────────────────────────────────

def get_session_timestamp() -> datetime | None:
    """Extract the 'Last written' timestamp from session.md."""
    if not SESSION_FILE.exists():
        return None

    content = SESSION_FILE.read_text()
    match = re.search(r"\*Last written:\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})", content)
    if not match:
        return None

    try:
        return datetime.fromisoformat(match.group(1))
    except ValueError:
        return None


def is_session_stale() -> bool:
    """Check if session.md is older than SESSION_STALE_HOURS."""
    ts = get_session_timestamp()
    if ts is None:
        return True

    age_hours = (datetime.now() - ts).total_seconds() / 3600
    return age_hours > SESSION_STALE_HOURS


def is_session_empty() -> bool:
    """Check if session.md has no meaningful content in Current Work."""
    if not SESSION_FILE.exists():
        return True

    content = SESSION_FILE.read_text()

    # Extract the Current Work section value
    in_section = False
    for line in content.splitlines():
        if line.strip() == "## Current Work":
            in_section = True
            continue
        if in_section:
            stripped = line.strip()
            if stripped.startswith("## "):
                break
            if stripped and stripped != "_none_":
                return False  # Has real content
            if stripped == "_none_":
                return True

    return True


def session_age_hours() -> float:
    """Get the age of session.md in hours."""
    ts = get_session_timestamp()
    if ts is None:
        return float("inf")
    return (datetime.now() - ts).total_seconds() / 3600


# ── Session Archival ───────────────────────────────────────

def extract_session_projects(content: str) -> list[str]:
    """Extract project slugs from session.md Files Touched section."""
    projects = set()

    in_files = False
    for line in content.splitlines():
        if line.strip().startswith("## Files Touched"):
            in_files = True
            continue
        if in_files and line.strip().startswith("## "):
            break
        if in_files and line.strip().startswith("- "):
            # Extract project from path like `Locus/src/main.js`
            path_match = re.search(r"`?(\w[\w-]+)/", line)
            if path_match:
                projects.add(path_match.group(1).lower())

    return sorted(projects)


def archive_session(dry_run: bool = False) -> bool:
    """Archive stale session.md by appending key info to relevant warm files.

    Returns True if archival happened.
    """
    if not SESSION_FILE.exists():
        return False

    content = SESSION_FILE.read_text()
    if is_session_empty():
        return False

    age = session_age_hours()
    if age < SESSION_STALE_HOURS:
        return False

    projects = extract_session_projects(content)

    # Extract the "Context That Must Not Be Lost" section
    critical_lines = []
    in_critical = False
    for line in content.splitlines():
        if "Context That Must Not Be Lost" in line:
            in_critical = True
            continue
        if in_critical and line.strip().startswith("## "):
            break
        if in_critical and line.strip().startswith("---"):
            break
        if in_critical and line.strip() and line.strip() != "_none_":
            critical_lines.append(line.strip())

    ts = get_session_timestamp()
    ts_str = ts.strftime("%Y-%m-%d %H:%M") if ts else "unknown"

    # Append session summary to relevant warm files
    for slug in projects:
        warm_file = PROJECTS_DIR / f"{slug}.md"
        if not warm_file.exists():
            logger.info("No warm file for project %s, skipping", slug)
            continue

        appendix = f"\n\n## Session Archive ({ts_str})\n"
        if critical_lines:
            for line in critical_lines[:5]:
                appendix += f"- {line}\n"
        else:
            appendix += "- Session context was recorded but had no critical items.\n"

        if dry_run:
            logger.info("[DRY RUN] Would append to %s: %s", warm_file, appendix[:80])
        else:
            with open(warm_file, "a") as f:
                f.write(appendix)
            logger.info("Archived session context to %s", warm_file.name)

    # Clear session.md
    if not dry_run:
        _reset_session_file()
        logger.info("Session.md reset after archival")

    return True


def _reset_session_file() -> None:
    """Reset session.md to empty state."""
    SESSION_FILE.write_text(
        "# Active Session State\n\n"
        "> Written mid-conversation by the agent. Read back after truncation.\n"
        "> This file is ephemeral — overwritten each session. Not archival.\n\n"
        "## Current Work\n_none_\n\n"
        "## Files Touched\n_none_\n\n"
        "## Pending Actions\n_none_\n\n"
        "## Context That Must Not Be Lost\n_none_\n\n"
        "---\n"
        "*Last written: never*\n"
    )


# ── Hot.md Pruning ─────────────────────────────────────────

def prune_completed_threads(dry_run: bool = False) -> int:
    """Remove struck-through open threads from hot.md.

    Returns count of lines removed.
    """
    if not HOT_FILE.exists():
        return 0

    lines = HOT_FILE.read_text().splitlines()
    pruned = []
    removed = 0

    in_threads = False
    for line in lines:
        if "## OPEN THREADS" in line:
            in_threads = True
            pruned.append(line)
            continue

        if in_threads and line.strip().startswith("- ~~"):
            removed += 1
            continue

        if in_threads and line.strip().startswith("## "):
            in_threads = False

        pruned.append(line)

    if removed > 0:
        if dry_run:
            logger.info("[DRY RUN] Would prune %d completed threads from hot.md", removed)
        else:
            HOT_FILE.write_text("\n".join(pruned) + "\n")
            logger.info("Pruned %d completed threads from hot.md", removed)

    return removed


# ── Compaction Check ───────────────────────────────────────

def run_compact_check() -> bool:
    """Run compact.py and return True if budget is OK."""
    compact_script = MEMORY_DIR / "compact.py"
    if not compact_script.exists():
        logger.warning("compact.py not found at %s", compact_script)
        return True

    try:
        import subprocess
        result = subprocess.run(
            ["python3", str(compact_script)],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            logger.warning("compact.py reports over budget: %s", result.stdout.strip())
            return False
        logger.info("Budget check passed")
        return True
    except Exception as e:
        logger.error("compact.py failed: %s", e)
        return False


# ── Consolidation Tick ─────────────────────────────────────

def consolidation_tick(dry_run: bool = False) -> dict:
    """Run one consolidation cycle. Returns summary dict."""
    results = {
        "session_archived": False,
        "threads_pruned": 0,
        "timestamp": time.time(),
    }

    # 1. Check if session.md needs archival
    if not is_session_empty() and is_session_stale():
        results["session_archived"] = archive_session(dry_run=dry_run)

    # 2. Prune completed threads
    results["threads_pruned"] = prune_completed_threads(dry_run=dry_run)

    return results


# ── Async Daemon Loop ──────────────────────────────────────

async def run_consolidator(
    dry_run: bool = False,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Async loop for agent_memory_daemon integration."""
    logger.info(
        "Consolidator starting (consolidate every %ds, compact every %ds)...",
        CONSOLIDATION_INTERVAL_S, COMPACT_INTERVAL_S,
    )

    _shutdown = shutdown_event or asyncio.Event()
    last_compact = time.time()

    while not _shutdown.is_set():
        try:
            results = consolidation_tick(dry_run=dry_run)

            if results["session_archived"]:
                logger.info("Session context archived to warm files")
            if results["threads_pruned"] > 0:
                logger.info("Pruned %d completed threads", results["threads_pruned"])

            # Periodic compact check
            now = time.time()
            if now - last_compact >= COMPACT_INTERVAL_S:
                run_compact_check()
                last_compact = now

        except Exception as e:
            logger.error("Consolidation tick failed: %s", e)

        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=CONSOLIDATION_INTERVAL_S)
            break
        except asyncio.TimeoutError:
            pass

    logger.info("Consolidator stopped.")


# ── Self-Test ──────────────────────────────────────────────

def _self_test() -> bool:
    """Verify consolidation logic with temp files."""
    import tempfile

    logger.info("Running consolidator self-test...")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create mock session.md
        test_session = tmpdir / "session.md"
        test_session.write_text(
            "# Active Session State\n\n"
            "> ephemeral\n\n"
            "## Current Work\nTesting consolidation daemon\n\n"
            "## Files Touched\n- `testproject/src/main.py` — added tests\n\n"
            "## Pending Actions\n_none_\n\n"
            "## Context That Must Not Be Lost\n"
            "- Test context line one\n"
            "- Test context line two\n\n"
            "---\n"
            "*Last written: 2020-01-01T00:00*\n"
        )

        # Create mock warm file
        test_projects = tmpdir / "projects"
        test_projects.mkdir()
        warm = test_projects / "testproject.md"
        warm.write_text("# TestProject\n\nOriginal content.\n")

        # Swap module-level paths using globals()
        global SESSION_FILE, PROJECTS_DIR
        orig_session = SESSION_FILE
        orig_projects = PROJECTS_DIR
        SESSION_FILE = test_session
        PROJECTS_DIR = test_projects

        try:
            # Test project extraction
            projects = extract_session_projects(test_session.read_text())
            if "testproject" not in projects:
                logger.error("Failed to extract project slug, got: %s", projects)
                return False

            # Test staleness (2020 timestamp should be stale)
            if not is_session_stale():
                logger.error("2020 session should be stale")
                return False

            # Test archival
            archived = archive_session(dry_run=False)
            if not archived:
                logger.error("Archival returned False")
                return False

            # Verify warm file was appended
            warm_content = warm.read_text()
            if "Test context line one" not in warm_content:
                logger.error("Critical context not found in warm file")
                return False

            # Verify session was reset
            session_content = test_session.read_text()
            if "_none_" not in session_content:
                logger.error("Session not reset after archival")
                return False

            logger.info("Self-test PASSED")
            return True

        finally:
            SESSION_FILE = orig_session
            PROJECTS_DIR = orig_projects


# ── CLI ────────────────────────────────────────────────────

def main() -> None:
    """CLI entry point."""
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Memory Consolidation Daemon")
    parser.add_argument("--test-mode", action="store_true", help="Run self-test")
    parser.add_argument("--dry-run", action="store_true", help="Log only, no writes")
    parser.add_argument("--once", action="store_true", help="Single tick then exit")
    args = parser.parse_args()

    if args.test_mode:
        success = _self_test()
        raise SystemExit(0 if success else 1)

    if args.once:
        results = consolidation_tick(dry_run=args.dry_run)
        run_compact_check()
        logger.info("Results: %s", results)
        return

    # Continuous mode
    asyncio.run(run_consolidator(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
