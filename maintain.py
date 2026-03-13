#!/usr/bin/env python3
"""Agent System Maintenance — Cognitive and I/O housekeeping.

One-shot script. Run via cron or systemd timer, NOT inside
Reaper's sweep loop. Expensive operations (consolidation,
trace pruning, decay) happen here so they never block security
monitoring.

Usage:
    python3 maintain.py              # Full maintenance pass
    python3 maintain.py --dry-run    # Report only, no writes
    python3 maintain.py --trace-only # Prune trace ledger only

Recommended cadence: every 6 hours via cron.
    0 */6 * * * python3 ~/.gemini/memory/maintain.py >> ~/.cortexdb/maintain.log 2>&1
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

# ── Path setup ─────────────────────────────────────────────

_CORTEX_ROOT = os.environ.get("AGENT_CORTEX_ROOT", os.path.expanduser("~/.cortexdb"))
_MEMORY_ROOT = os.path.expanduser("~/.gemini/memory")
for p in [_CORTEX_ROOT, _MEMORY_ROOT]:
    if p not in sys.path:
        sys.path.insert(0, p)

from cortex.engine import Cortex
from lesson_engine import LessonEngine

# ── Constants ──────────────────────────────────────────────

AGENT_DB = os.path.expanduser("~/.cortexdb/agent_system.db")
TRACE_DB = os.path.expanduser("~/.cortexdb/trace_ledger.db")
TRACE_MAX_AGE_HOURS = 168       # 7 days of trace history
TRACE_MAX_ROWS = 50_000         # Hard cap on trace entries


def run_lesson_consolidation(dry_run: bool = False) -> dict:
    """Consolidate redundant lessons into generalized constraints."""
    engine = LessonEngine(AGENT_DB)
    stats_before = engine.stats()

    if dry_run:
        engine.close()
        return {
            "action": "consolidate",
            "dry_run": True,
            "lessons_total": stats_before["total"],
            "already_consolidated": stats_before.get("consolidated", 0),
        }

    created = engine.consolidate()
    stats_after = engine.stats()
    engine.close()

    return {
        "action": "consolidate",
        "new_consolidated": len(created),
        "lessons_before": stats_before["total"],
        "lessons_after": stats_after["total"],
        "consolidated_total": stats_after.get("consolidated", 0),
    }


def run_cortex_decay(dry_run: bool = False) -> dict:
    """Run Ebbinghaus decay on CortexDB memories."""
    if dry_run:
        return {"action": "decay", "dry_run": True}

    cortex = Cortex(AGENT_DB)
    pruned = cortex.decay()
    stats = cortex.stats()
    cortex.close()

    return {
        "action": "decay",
        "pruned": pruned,
        "memories_remaining": stats.get("total", 0),
    }


def run_trace_cleanup(dry_run: bool = False) -> dict:
    """Prune old trace entries beyond the retention window."""
    if not os.path.exists(TRACE_DB):
        return {"action": "trace_cleanup", "skipped": "no trace DB"}

    conn = sqlite3.connect(TRACE_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")

    total = conn.execute("SELECT COUNT(*) FROM trace_ledger").fetchone()[0]
    cutoff = time.time() - (TRACE_MAX_AGE_HOURS * 3600)
    expired = conn.execute(
        "SELECT COUNT(*) FROM trace_ledger WHERE timestamp < ?", (cutoff,)
    ).fetchone()[0]

    if dry_run:
        conn.close()
        return {
            "action": "trace_cleanup",
            "dry_run": True,
            "total": total,
            "expired": expired,
        }

    # Delete expired traces
    conn.execute("DELETE FROM trace_ledger WHERE timestamp < ?", (cutoff,))

    # Enforce hard cap (keep newest)
    remaining = conn.execute("SELECT COUNT(*) FROM trace_ledger").fetchone()[0]
    overflow = 0
    if remaining > TRACE_MAX_ROWS:
        overflow = remaining - TRACE_MAX_ROWS
        conn.execute(
            "DELETE FROM trace_ledger WHERE id IN ("
            "  SELECT id FROM trace_ledger ORDER BY timestamp ASC LIMIT ?"
            ")", (overflow,)
        )

    conn.commit()
    final = conn.execute("SELECT COUNT(*) FROM trace_ledger").fetchone()[0]
    conn.close()

    return {
        "action": "trace_cleanup",
        "expired_deleted": expired,
        "overflow_deleted": overflow,
        "remaining": final,
    }


def run_session_briefing(dry_run: bool = False) -> dict:
    """Generate session briefing for next agent session."""
    try:
        from session_briefing import write_briefing, BRIEFING_FILE
        if dry_run:
            return {"action": "session_briefing", "dry_run": True}
        path = write_briefing()
        line_count = len(BRIEFING_FILE.read_text().splitlines())
        return {
            "action": "session_briefing",
            "path": path,
            "lines": line_count,
        }
    except Exception as e:
        return {"action": "session_briefing", "error": str(e)}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Agent system cognitive and I/O maintenance"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would happen without making changes"
    )
    parser.add_argument(
        "--trace-only", action="store_true",
        help="Only prune the trace ledger"
    )
    args = parser.parse_args()

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*50}")
    print(f"  MAINTENANCE PASS — {ts}")
    print(f"{'='*50}")

    if args.trace_only:
        result = run_trace_cleanup(dry_run=args.dry_run)
        _print_result(result)
        return

    # Full maintenance pass
    for fn in [
        run_lesson_consolidation,
        run_cortex_decay,
        run_trace_cleanup,
        run_session_briefing,
    ]:
        result = fn(dry_run=args.dry_run)
        _print_result(result)

    print(f"\n{'='*50}\n")


def _print_result(result: dict) -> None:
    """Pretty-print a maintenance result."""
    action = result.pop("action", "unknown")
    print(f"\n  [{action.upper()}]")
    for key, value in result.items():
        print(f"    {key}: {value}")


if __name__ == "__main__":
    main()
