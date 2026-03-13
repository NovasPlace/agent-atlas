"""directive_indexer.py — One-time import of GEMINI.md into CortexDB.

Chunks the agent directive into typed procedural memories so the
ContextRecallDaemon can surface the most relevant sections automatically
based on what the agent is working on.

Each ## section becomes one CortexDB memory:
  type="procedural"   — long-term rules and workflows
  tags=["directive", "<section_slug>"]
  importance weighted by section criticality

Usage:
    python3 directive_indexer.py                  # index GEMINI.md
    python3 directive_indexer.py --status         # show indexed directives
    python3 directive_indexer.py --reindex        # wipe and re-import
    python3 directive_indexer.py --test-mode      # self-test

Designed to run once after kit install, and again after directive updates.
Idempotent: duplicate content is detected and skipped by default.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path

logger = logging.getLogger("directive-indexer")

# ── Paths ──────────────────────────────────────────────────

GEMINI_MD   = Path(os.path.expanduser("~/.gemini/GEMINI.md"))
CORTEX_DB   = os.path.expanduser("~/.cortexdb/agent_system.db")
DIRECTIVE_TAG = "directive"   # Primary tag on all directive memories

# ── Section Importance Map ──────────────────────────────────
# Hand-tuned importance weights per section.
# Higher = more likely to surface in context brief.

_SECTION_IMPORTANCE: dict[str, float] = {
    "0":   1.0,   # EXECUTION PROOF LAW — always critical
    "1":   0.5,   # Identity
    "2":   0.75,  # Cognition — research-first, confidence tagging
    "3":   0.9,   # Pre-ship pipeline — 7-stage gate
    "4":   0.75,  # Execution — scope, complexity scaling
    "5":   0.7,   # Code — style rules
    "6":   0.6,   # Observability — telemetry
    "7":   0.65,  # Architecture — file layout
    "8":   0.85,  # Failure memory — hard-won lessons
    "9":   0.8,   # Forbidden — never-do list
    "10":  0.5,   # Tone
    "11":  0.7,   # Continuity — memory protocol
}


# ── Parser ─────────────────────────────────────────────────

def _section_slug(header: str) -> str:
    """Turn '## 3. PRE-SHIP PIPELINE' into '3_pre_ship_pipeline'."""
    cleaned = re.sub(r"^##\s*", "", header).strip()
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", "", cleaned)
    return re.sub(r"\s+", "_", cleaned).lower()[:40]


def _section_number(header: str) -> str:
    """Extract leading digit(s): '## 0. EXECUTION...' → '0'."""
    m = re.match(r"##\s*(\d+)", header)
    return m.group(1) if m else ""


def parse_directive(md_content: str) -> list[dict]:
    """Split GEMINI.md into chunks at ## headings.

    Returns list of dicts:
      {header, slug, section_num, content, importance}
    """
    lines = md_content.splitlines()
    chunks: list[dict] = []
    current_header = ""
    current_lines: list[str] = []

    def _flush():
        if not current_header:
            return
        body = "\n".join(current_lines).strip()
        if not body:
            return
        num = _section_number(current_header)
        slug = _section_slug(current_header)
        importance = _SECTION_IMPORTANCE.get(num, 0.6)
        chunks.append({
            "header":      current_header.strip(),
            "slug":        slug,
            "section_num": num,
            "content":     f"{current_header.strip()}\n\n{body}",
            "importance":  importance,
        })

    for line in lines:
        if line.startswith("## "):
            _flush()
            current_header = line
            current_lines = []
        else:
            current_lines.append(line)

    _flush()
    return chunks


# ── CortexDB Integration ────────────────────────────────────

def _load_cortex(db_path: str):
    """Import Cortex at call time so we don't fail if CortexDB not on path."""
    _CORTEX_ROOT = os.environ.get("AGENT_CORTEX_ROOT", os.path.expanduser("~/.cortexdb"))
    _MEM_ROOT    = os.path.expanduser("~/.gemini/memory")
    for p in [_CORTEX_ROOT, _MEM_ROOT]:
        if p not in sys.path:
            sys.path.insert(0, p)
    try:
        from cortex.engine import Cortex  # type: ignore
        return Cortex(db_path)
    except ImportError as e:
        raise ImportError(
            f"CortexDB not importable: {e}\n"
            f"Expected at: {_CORTEX_ROOT}"
        ) from e


def _already_indexed(cortex, slug: str) -> bool:
    """Check if a directive section is already in CortexDB."""
    results = cortex.recall(f"directive:{slug}", limit=5)
    return any(
        DIRECTIVE_TAG in (m.tags or []) and slug in (m.tags or [])
        for m in results
    )


def index_directive(
    gemini_md: Path = GEMINI_MD,
    db_path: str = CORTEX_DB,
    reindex: bool = False,
    dry_run: bool = False,
) -> list[str]:
    """Parse GEMINI.md and store each section in CortexDB.

    Returns list of section slugs that were written.
    """
    if not gemini_md.exists():
        raise FileNotFoundError(f"Directive not found: {gemini_md}")

    content = gemini_md.read_text(encoding="utf-8")
    chunks = parse_directive(content)

    if not chunks:
        raise ValueError("No ## sections found in directive file.")

    cortex = _load_cortex(db_path)
    written: list[str] = []

    for chunk in chunks:
        slug = chunk["slug"]
        num  = chunk["section_num"]

        if not reindex and _already_indexed(cortex, slug):
            logger.info("SKIP (already indexed): §%s %s", num, slug)
            continue

        if dry_run:
            logger.info("DRY RUN: would index §%s %s (importance=%.2f)",
                        num, slug, chunk["importance"])
            written.append(slug)
            continue

        # Delete old version if reindexing
        if reindex:
            _delete_section(cortex, slug)

        cortex.remember(
            content=chunk["content"][:2000],      # Hard cap — avoid bloat
            type="procedural",
            tags=[DIRECTIVE_TAG, slug, f"section_{num}"],
            importance=chunk["importance"],
            emotion="neutral",
            source="instructed",                   # Agent was given this
            confidence=1.0,
            context=f"GEMINI.md §{num}: {chunk['header']}",
        )
        logger.info("Indexed: §%s %s (importance=%.2f)",
                    num, slug, chunk["importance"])
        written.append(slug)

    return written


def _delete_section(cortex, slug: str) -> int:
    """Remove all directive memories for a given section slug."""
    results = cortex.recall(f"directive:{slug}", limit=20)
    deleted = 0
    for mem in results:
        if DIRECTIVE_TAG in (mem.tags or []) and slug in (mem.tags or []):
            cortex._conn.execute("DELETE FROM memories WHERE id=?", (mem.id,))
            deleted += 1
    cortex._conn.commit()
    return deleted


def status(db_path: str = CORTEX_DB) -> list[dict]:
    """Return indexed directive sections from CortexDB."""
    cortex = _load_cortex(db_path)
    results = cortex.recall("directive", limit=50)
    sections = [
        {
            "id":         m.id[:8],
            "slug":       next((t for t in m.tags if t != DIRECTIVE_TAG
                                and not t.startswith("section_")), "?"),
            "importance": round(m.importance, 2),
            "created":    m.created_at,
            "chars":      len(m.content),
        }
        for m in results
        if DIRECTIVE_TAG in (m.tags or [])
    ]
    return sections


# ── Self-Test ──────────────────────────────────────────────

def _self_test() -> bool:
    """Parse GEMINI.md and verify chunking, then dry-run index."""
    logger.info("Running directive_indexer self-test...")

    if not GEMINI_MD.exists():
        logger.error("GEMINI.md not found at %s", GEMINI_MD)
        return False

    content = GEMINI_MD.read_text(encoding="utf-8")
    chunks = parse_directive(content)

    # Should have at least 10 sections
    if len(chunks) < 10:
        logger.error("Only %d chunks parsed — expected ≥10", len(chunks))
        return False

    # Section 0 must exist and have max importance
    sec0 = next((c for c in chunks if c["section_num"] == "0"), None)
    if not sec0:
        logger.error("Section 0 (EXECUTION PROOF LAW) not found")
        return False
    assert sec0["importance"] == 1.0, f"§0 importance should be 1.0, got {sec0['importance']}"

    # Slug uniqueness
    slugs = [c["slug"] for c in chunks]
    if len(slugs) != len(set(slugs)):
        logger.error("Duplicate slugs detected: %s", slugs)
        return False

    # Dry-run index
    written = index_directive(dry_run=True)
    if len(written) < 10:
        logger.error("Dry-run wrote fewer sections than expected: %d", len(written))
        return False

    logger.info("directive_indexer self-test PASSED — %d sections parsed, %d would be indexed",
                len(chunks), len(written))
    return True


# ── CLI ────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Index GEMINI.md directive sections into CortexDB"
    )
    parser.add_argument("--status",    action="store_true", help="Show indexed directives")
    parser.add_argument("--reindex",   action="store_true", help="Wipe and re-import all sections")
    parser.add_argument("--dry-run",   action="store_true", help="Parse only, no DB writes")
    parser.add_argument("--test-mode", action="store_true", help="Run self-test")
    parser.add_argument("--db",        default=CORTEX_DB,   help="CortexDB path")
    parser.add_argument("--directive", default=str(GEMINI_MD), help="Path to directive MD file")
    args = parser.parse_args()

    if args.test_mode:
        success = _self_test()
        raise SystemExit(0 if success else 1)

    if args.status:
        sections = status(args.db)
        if not sections:
            print("No directive sections indexed yet. Run without --status to import.")
            raise SystemExit(0)
        print(f"Indexed directive sections ({len(sections)}):")
        for s in sorted(sections, key=lambda x: x["slug"]):
            print(f"  [{s['id']}] {s['slug']:40s}  imp={s['importance']}  {s['chars']}c")
        raise SystemExit(0)

    gemini_path = Path(args.directive)
    written = index_directive(
        gemini_md=gemini_path,
        db_path=args.db,
        reindex=args.reindex,
        dry_run=args.dry_run,
    )

    if args.dry_run:
        print(f"DRY RUN — {len(written)} section(s) would be indexed.")
    else:
        print(f"Done — {len(written)} section(s) indexed into CortexDB.")
        if not written:
            print("(All sections already indexed. Use --reindex to force update.)")


if __name__ == "__main__":
    main()
