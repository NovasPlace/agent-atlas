"""ContextRecallDaemon — Live context brief + auto-journal for agents.

Runs every 90 seconds. Queries CortexDB with priming, assembles a
compressed ≤50-line "context brief" from three memory tiers:
  1. Episodic  — recent file changes & session events (last 6 hours)
  2. Procedural — top lessons primed to current work
  3. Semantic   — current project states

Also AUTO-JOURNALS every tick:
  - Synthesizes a session summary from CortexDB data
  - Writes it to hot.md SESSION SUMMARY via the writer daemon
  - /update-journal is no longer needed

Writes ~/.gemini/memory/session_context.md atomically.
Registers GET_CONTEXT_BRIEF on md_reader.py via INVALIDATE.

Usage (daemon integration):
    from context_recall import run_recall_daemon
    await run_recall_daemon(shutdown_event)

Usage (standalone):
    python3 context_recall.py
    python3 context_recall.py --once      # Single build then exit
    python3 context_recall.py --test-mode # Self-test
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("context-recall")

# ── Paths ──────────────────────────────────────────────────

_MEMORY_DIR = Path(os.path.expanduser("~/.gemini/memory"))
_CORTEX_ROOT = Path(os.environ.get("AGENT_CORTEX_ROOT", os.path.expanduser("~/.cortexdb")))
_DEFAULT_DB = os.path.expanduser("~/.cortexdb/agent_system.db")

for _p in [str(_CORTEX_ROOT), str(_MEMORY_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

HOT_FILE = _MEMORY_DIR / "hot.md"
SESSION_FILE = _MEMORY_DIR / "session.md"
CONTEXT_BRIEF_FILE = _MEMORY_DIR / "session_context.md"
READER_SOCKET = "/tmp/agent-memory-reader.sock"
WRITER_SOCKET = "/tmp/agent-memory-writer.sock"

# Rebuild interval in seconds
RECALL_INTERVAL_S = 90

# Memory retrieval limits per tier
EPISODIC_LIMIT = 8
LESSON_LIMIT = 5
SEMANTIC_LIMIT = 4

# Only show episodic memories from last N hours
EPISODIC_WINDOW_HOURS = 6


# ── Hot.md Parser ──────────────────────────────────────────

def _parse_active_project(hot_content: str) -> str:
    """Extract first active project row from the ACTIVE PROJECTS table."""
    in_table = False
    for line in hot_content.splitlines():
        stripped = line.strip()
        if "| Project" in stripped:
            in_table = True
            continue
        if in_table and stripped.startswith("|---"):
            continue
        if in_table and stripped.startswith("|"):
            cols = [c.strip() for c in stripped.split("|") if c.strip()]
            if len(cols) >= 3:
                name = cols[0]
                status = cols[2]
                return f"{name} — {status}"
        elif in_table and not stripped.startswith("|"):
            break
    return "Unknown"


def _parse_session_state(session_content: str) -> tuple[str, list[str]]:
    """Extract current_work and critical_context from session.md."""
    current_work = ""
    critical: list[str] = []

    section = ""
    for line in session_content.splitlines():
        stripped = line.strip()
        if stripped.startswith("## Current Work"):
            section = "work"
            continue
        if stripped.startswith("## Context That Must Not Be Lost"):
            section = "critical"
            continue
        if stripped.startswith("## "):
            section = ""
            continue

        if section == "work" and stripped and stripped != "_none_":
            current_work = stripped
        if section == "critical" and stripped.startswith("- "):
            critical.append(stripped[2:])

    return current_work, critical[:5]


def _parse_open_threads(hot_content: str) -> list[str]:
    """Extract OPEN THREADS bullets from hot.md."""
    threads: list[str] = []
    in_threads = False
    for line in hot_content.splitlines():
        stripped = line.strip()
        if "## OPEN THREADS" in stripped:
            in_threads = True
            continue
        if in_threads and stripped.startswith("## "):
            break
        if in_threads and stripped.startswith("- **"):
            # Extract just the thread name (bold text before —)
            match = re.search(r"\*\*(.+?)\*\*", stripped)
            if match:
                threads.append(match.group(1))
    return threads[:3]


# ── CortexDB Query ─────────────────────────────────────────

def _query_cortex(current_work: str, db_path: str) -> dict:
    """Pull memories from CortexDB across three tiers."""
    try:
        from cortex.engine import Cortex
        from cortex.priming import PrimingEngine
    except ImportError:
        logger.warning("CortexDB not importable — skipping memory recall")
        return {"episodic": [], "lessons": [], "semantic": []}

    cortex = Cortex(db_path)
    priming = PrimingEngine(cortex, ttl=RECALL_INTERVAL_S + 30)

    query_term = current_work or "active project session"

    # Tier 1: Episodic — recent file change context
    cutoff = time.time() - (EPISODIC_WINDOW_HOURS * 3600)
    all_recent = cortex.list_all(limit=200)
    episodic = [
        m for m in all_recent
        if "subconscious" in m.tags or "session" in m.tags
        and m.created_at > cutoff
    ]
    episodic.sort(key=lambda m: m.created_at, reverse=True)
    episodic = episodic[:EPISODIC_LIMIT]

    # Tier 2: Procedural — lessons primed to current work
    if episodic:
        priming.prime(episodic[0].id, boost=0.2, max_hops=2)

    raw_lessons = priming.primed_recall(query_term, limit=LESSON_LIMIT * 2)
    lessons = [m for m in raw_lessons if "lesson" in m.tags][:LESSON_LIMIT]

    if not lessons:
        # Fallback: recency-ranked lessons
        all_mem = cortex.list_all(limit=300)
        lessons = sorted(
            [m for m in all_mem if "lesson" in m.tags],
            key=lambda m: m.importance,
            reverse=True,
        )[:LESSON_LIMIT]

    # Tier 3: Semantic — project state snapshots
    semantic = [
        m for m in cortex.recall(query_term, limit=SEMANTIC_LIMIT * 2)
        if "project-state" in m.tags
    ][:SEMANTIC_LIMIT]

    cortex.close()

    return {"episodic": episodic, "lessons": lessons, "semantic": semantic}


# ── Brief Builder ───────────────────────────────────────────

def _truncate(text: str, max_len: int = 90) -> str:
    return text if len(text) <= max_len else text[:max_len - 1] + "…"


def build_context_brief(db_path: str = _DEFAULT_DB) -> str:
    """Build the full context brief string."""
    now_str = datetime.now().strftime("%Y-%m-%dT%H:%M")
    lines: list[str] = [f"## LIVE CONTEXT — {now_str}", ""]

    # ── Active project (from hot.md) ──
    hot_content = HOT_FILE.read_text(encoding="utf-8") if HOT_FILE.exists() else ""
    active_project = _parse_active_project(hot_content)
    open_threads = _parse_open_threads(hot_content)

    lines.append("### Active Focus")
    lines.append(f"- {active_project}")
    lines.append("")

    # ── Session state (from session.md) ──
    session_content = SESSION_FILE.read_text(encoding="utf-8") if SESSION_FILE.exists() else ""
    current_work, critical = _parse_session_state(session_content)

    if current_work:
        lines.append("### Current Work")
        lines.append(f"- {_truncate(current_work)}")
        lines.append("")

    # ── CortexDB memories ──
    memories = _query_cortex(current_work, db_path)

    # Episodic: recent file changes
    episodic = memories["episodic"]
    if episodic:
        lines.append("### Recent Activity")
        for m in episodic[:5]:
            lines.append(f"- {_truncate(m.content)}")
        lines.append("")

    # Lessons: primed to current work
    lessons = memories["lessons"]
    if lessons:
        lines.append("### Relevant Lessons")
        for m in lessons:
            lines.append(f"- {_truncate(m.content)}")
        lines.append("")

    # Semantic: project state snapshots
    semantic = memories["semantic"]
    if semantic:
        lines.append("### Project States")
        for m in semantic:
            lines.append(f"- {_truncate(m.content)}")
        lines.append("")

    # Critical context
    if critical:
        lines.append("### Critical Context (must survive)")
        for c in critical:
            lines.append(f"- {_truncate(c)}")
        lines.append("")

    # Open threads
    if open_threads:
        lines.append("### Open Threads")
        for t in open_threads:
            lines.append(f"- {t}")
        lines.append("")

    lines.append(
        "> Refresh: `agent_memory_api.py get context` | "
        f"Next update in ~{RECALL_INTERVAL_S}s"
    )

    return "\n".join(lines)


# ── Atomic Write + Cache Invalidation ──────────────────────

def _write_brief(content: str) -> None:
    """Write session_context.md atomically."""
    tmp = CONTEXT_BRIEF_FILE.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.rename(tmp, CONTEXT_BRIEF_FILE)


async def _notify_reader(key: str = "context_brief") -> None:
    """Signal md_reader to invalidate the context brief cache entry."""
    try:
        r, w = await asyncio.wait_for(
            asyncio.open_unix_connection(READER_SOCKET), timeout=1.0
        )
        import json
        w.write(json.dumps({"cmd": "INVALIDATE", "key": key}).encode() + b"\n")
        await w.drain()
        await asyncio.wait_for(r.read(64), timeout=1.0)
        w.close()
        await w.wait_closed()
    except Exception:
        pass  # Reader may not be running


# ── Auto-Journal ────────────────────────────────────────────

def _synthesize_summary(memories: dict, current_work: str, active_project: str) -> str:
    """Synthesize a one-sentence session summary from CortexDB data.

    Priority: current_work from session.md (agent-written) → most recent
    episodic memory → active project name. Always returns a non-empty string.
    """
    if current_work and current_work != "_none_":
        base = current_work
    elif memories["episodic"]:
        base = memories["episodic"][0].content
    else:
        base = f"Active on {active_project}"

    # Trim to ≤120 chars — fits cleanly in hot.md line budget
    return base[:120]


async def _write_journal(summary: str) -> bool:
    """Send UPDATE_HOT to the writer daemon with the synthesized summary.

    Returns True if the write succeeded. Fails silently if the writer
    socket is not available (daemon restarting, etc.).
    """
    import json
    payload = {"cmd": "UPDATE_HOT", "session_summary": summary}
    try:
        r, w = await asyncio.wait_for(
            asyncio.open_unix_connection(WRITER_SOCKET), timeout=1.5
        )
        w.write(json.dumps(payload).encode() + b"\n")
        await w.drain()
        raw = await asyncio.wait_for(r.read(256), timeout=1.5)
        w.close()
        await w.wait_closed()
        resp = json.loads(raw.decode())
        return bool(resp.get("ok"))
    except Exception as e:
        logger.debug("Auto-journal write skipped: %s", e)
        return False


# ── Daemon Loop ─────────────────────────────────────────────

async def run_recall_daemon(
    shutdown_event: asyncio.Event | None = None,
    db_path: str = _DEFAULT_DB,
) -> None:
    """Async loop for agent_memory_daemon integration."""
    _shutdown = shutdown_event or asyncio.Event()
    logger.info("ContextRecallDaemon starting (rebuild every %ds)...", RECALL_INTERVAL_S)

    while not _shutdown.is_set():
        try:
            # Build context brief
            brief = build_context_brief(db_path)
            _write_brief(brief)
            await _notify_reader()
            line_count = brief.count("\n") + 1
            logger.info("Context brief rebuilt (%d lines)", line_count)

            # Auto-journal: synthesize and write session summary to hot.md
            hot_content = HOT_FILE.read_text(encoding="utf-8") if HOT_FILE.exists() else ""
            session_content = SESSION_FILE.read_text(encoding="utf-8") if SESSION_FILE.exists() else ""
            current_work, _ = _parse_session_state(session_content)
            active_project = _parse_active_project(hot_content)
            memories = _query_cortex(current_work, db_path)
            summary = _synthesize_summary(memories, current_work, active_project)
            wrote = await _write_journal(summary)
            if wrote:
                logger.info("Auto-journal: hot.md updated — %s", summary[:60])
            else:
                logger.debug("Auto-journal: writer unavailable, skipped")

        except Exception as e:
            logger.error("Context brief/journal build failed: %s", e)

        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=RECALL_INTERVAL_S)
            break
        except asyncio.TimeoutError:
            pass

    logger.info("ContextRecallDaemon stopped.")


# ── Self-Test ───────────────────────────────────────────────

async def _self_test() -> bool:
    """Build a context brief, verify structure and output."""
    import tempfile

    logger.info("Running ContextRecallDaemon self-test...")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # Temp CortexDB — fresh empty DB for the test
        db_fd, tmp_db_path = tempfile.mkstemp(suffix=".db")
        os.close(db_fd)

        # Mock hot.md
        hot = tmp / "hot.md"
        hot.write_text(
            "# HOT MEMORY\n\n## ACTIVE PROJECTS\n\n"
            "| Project | Location | Status | Warm File |\n"
            "|---------|----------|--------|-----------|\n"
            "| Locus | `~/projects/locus/` | v3.1 — Apply-back next | `projects/locus.md` |\n\n"
            "## OPEN THREADS\n\n"
            "- **CortexDB packaging** — prep for public release\n"
            "- **Agent Memory Kit** — needs icon\n\n"
            "## RECENT LESSONS\n\n"
            "- Always sanitize slugs before path operations\n"
        )

        # Mock session.md
        session = tmp / "session.md"
        session.write_text(
            "# Active Session State\n\n"
            "## Current Work\nBuilding context recall daemon\n\n"
            "## Context That Must Not Be Lost\n"
            "- Reader socket path is /tmp/agent-memory-reader.sock\n"
            "- Writer uses atomic rename\n"
        )

        # Monkeypatch globals
        global HOT_FILE, SESSION_FILE, CONTEXT_BRIEF_FILE
        _orig_hot, _orig_sess, _orig_brief = HOT_FILE, SESSION_FILE, CONTEXT_BRIEF_FILE
        HOT_FILE = hot
        SESSION_FILE = session
        CONTEXT_BRIEF_FILE = tmp / "session_context.md"

        try:
            # Build brief (no CortexDB memories in fresh DB — that's OK)
            brief = build_context_brief(db_path=tmp_db_path)

            # Structure checks
            assert "## LIVE CONTEXT" in brief, "Missing LIVE CONTEXT header"
            assert "### Active Focus" in brief, "Missing Active Focus section"
            assert "Locus" in brief, "Active project not in brief"
            assert "Building context recall daemon" in brief, "Current work not in brief"
            assert "Reader socket path" in brief, "Critical context not in brief"
            assert "CortexDB packaging" in brief, "Open threads not in brief"

            # Write and verify
            _write_brief(brief)
            assert CONTEXT_BRIEF_FILE.exists(), "session_context.md not created"

            line_count = brief.count("\n") + 1
            assert line_count <= 60, f"Brief too long: {line_count} lines"

            logger.info(
                "ContextRecallDaemon self-test PASSED (%d lines)", line_count
            )
            return True

        except Exception as e:
            logger.error("ContextRecallDaemon self-test FAILED: %s", e)
            import traceback
            traceback.print_exc()
            return False
        finally:
            HOT_FILE, SESSION_FILE, CONTEXT_BRIEF_FILE = _orig_hot, _orig_sess, _orig_brief
            try:
                os.unlink(tmp_db_path)
            except OSError:
                pass


# ── CLI ─────────────────────────────────────────────────────

def main() -> None:
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="ContextRecallDaemon")
    parser.add_argument("--test-mode", action="store_true", help="Run self-test")
    parser.add_argument("--once", action="store_true", help="Build once then exit")
    parser.add_argument("--db", default=_DEFAULT_DB, help="CortexDB path")
    args = parser.parse_args()

    if args.test_mode:
        success = asyncio.run(_self_test())
        raise SystemExit(0 if success else 1)

    if args.once:
        brief = build_context_brief(args.db)
        _write_brief(brief)
        print(brief)
        return

    asyncio.run(run_recall_daemon(db_path=args.db))


if __name__ == "__main__":
    main()
