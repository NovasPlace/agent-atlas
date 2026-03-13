"""Subconscious Context Watcher — Auto-persist session context to CortexDB.

Monitors active project directories for file changes and stores
context snapshots as episodic memories. No LLM calls — pure AST/regex
extraction. Designed to run inside agent_memory_daemon.py.

Context extraction:
  - Python: ast.parse → function/class names
  - JS/TS: regex → function/class names
  - Other: file-level events only

Usage:
    python3 subconscious.py              # One-shot scan
    python3 subconscious.py --test-mode  # Self-test with temp dir
    python3 subconscious.py --dry-run    # Log, no CortexDB writes
"""
from __future__ import annotations

import ast
import asyncio
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

_CORTEX_ROOT = os.path.expanduser("~/Desktop/Agent_System/DB-Memory/CortexDB")
_MEMORY_ROOT = os.path.expanduser("~/.gemini/memory")
for p in [_CORTEX_ROOT, _MEMORY_ROOT]:
    if p not in sys.path:
        sys.path.insert(0, p)

from cortex.engine import Cortex

logger = logging.getLogger("subconscious")

# ── Constants ──────────────────────────────────────────────

DEFAULT_DB_PATH = os.path.expanduser("~/.cortexdb/agent_system.db")
HOT_FILE = Path(_MEMORY_ROOT) / "hot.md"

# Poll interval in seconds
POLL_INTERVAL_S = 30

# Debounce: batch rapid changes within this window
DEBOUNCE_WINDOW_S = 5

# Memory defaults
MEMORY_IMPORTANCE = 0.3     # Low — ambient observations
MEMORY_EMOTION = "neutral"
MEMORY_SOURCE = "observed"
MEMORY_TYPE = "episodic"

# Burst threshold: this many files in one batch → summarize
BURST_THRESHOLD = 5

# Max content length in a single memory
MAX_CONTENT_LEN = 500

# Directories to ignore
IGNORE_DIRS: frozenset[str] = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    "dist", "build", ".pytest_cache", ".mypy_cache",
    ".eggs", ".tox", ".cache", ".next", "coverage",
    "egg-info", ".cortexdb", ".gemini",
})

# Extensions to ignore
IGNORE_EXTENSIONS: frozenset[str] = frozenset({
    ".pyc", ".pyo", ".o", ".so", ".dylib", ".egg-info",
    ".lock", ".log", ".db", ".sqlite", ".sqlite3",
    ".whl", ".tar", ".gz", ".zip", ".jar",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot",
    ".map", ".min.js", ".min.css",
})

# Specific files to ignore
IGNORE_FILES: frozenset[str] = frozenset({
    "package-lock.json", ".DS_Store", "Thumbs.db",
    ".gitignore", ".dockerignore", ".eslintcache",
})

# Extensions that support AST/regex context extraction
PYTHON_EXTENSIONS = {".py"}
JS_EXTENSIONS = {".js", ".ts", ".jsx", ".tsx", ".mjs"}


# ── Data Model ─────────────────────────────────────────────

@dataclass
class FileSnapshot:
    """Point-in-time state of a watched file."""
    path: str
    mtime: float
    size: int


@dataclass
class FileChange:
    """A detected file change."""
    path: str
    project_name: str
    project_dir: str
    change_type: str  # "created", "modified", "deleted"
    timestamp: float = field(default_factory=time.time)


# ── Context Extraction ─────────────────────────────────────

def extract_python_symbols(filepath: str) -> list[str]:
    """Extract top-level function and class names from a Python file via AST."""
    try:
        source = Path(filepath).read_text(errors="replace")
        tree = ast.parse(source)
        symbols = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                symbols.append(f"def {node.name}()")
            elif isinstance(node, ast.ClassDef):
                methods = [
                    n.name for n in ast.iter_child_nodes(node)
                    if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
                    and not n.name.startswith("_")
                ]
                if methods:
                    symbols.append(f"class {node.name}: {', '.join(methods[:5])}")
                else:
                    symbols.append(f"class {node.name}")
        return symbols
    except (SyntaxError, ValueError, OSError):
        return []


# Regex for JS/TS: function declarations, arrow functions assigned to const/let,
# class declarations
_JS_FUNC_RE = re.compile(
    r"(?:^|\n)\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)",
)
_JS_CLASS_RE = re.compile(
    r"(?:^|\n)\s*(?:export\s+)?class\s+(\w+)",
)
_JS_CONST_FUNC_RE = re.compile(
    r"(?:^|\n)\s*(?:export\s+)?(?:const|let)\s+(\w+)\s*=\s*(?:async\s+)?\(",
)


def extract_js_symbols(filepath: str) -> list[str]:
    """Extract function and class names from JS/TS files via regex."""
    try:
        source = Path(filepath).read_text(errors="replace")
        symbols = []
        for match in _JS_FUNC_RE.finditer(source):
            symbols.append(f"function {match.group(1)}()")
        for match in _JS_CLASS_RE.finditer(source):
            symbols.append(f"class {match.group(1)}")
        for match in _JS_CONST_FUNC_RE.finditer(source):
            symbols.append(f"const {match.group(1)}()")
        return symbols
    except OSError:
        return []


def extract_symbols(filepath: str) -> list[str]:
    """Extract symbols from a file based on its extension."""
    ext = Path(filepath).suffix.lower()
    if ext in PYTHON_EXTENSIONS:
        return extract_python_symbols(filepath)
    if ext in JS_EXTENSIONS:
        return extract_js_symbols(filepath)
    return []


# ── Project Loader ─────────────────────────────────────────

def load_projects_from_hot() -> dict[str, str]:
    """Parse hot.md project table. Returns {name: abs_path}."""
    if not HOT_FILE.exists():
        return {}

    projects: dict[str, str] = {}
    content = HOT_FILE.read_text()
    in_table = False

    for line in content.splitlines():
        stripped = line.strip()
        if "| Project " in stripped or "| project " in stripped:
            in_table = True
            continue
        if in_table and stripped.startswith("|---"):
            continue
        if in_table and stripped.startswith("|"):
            cols = [c.strip() for c in stripped.split("|")]
            # cols: ['', 'Name', 'Location', 'Status', 'Warm File', '']
            if len(cols) >= 4:
                name = cols[1].strip()
                location = cols[2].strip().strip("`").replace("~/", os.path.expanduser("~/"))
                if name and os.path.isdir(location):
                    projects[name] = location
        elif in_table and not stripped.startswith("|"):
            break

    return projects


# ── File Scanner ───────────────────────────────────────────

def _should_ignore(path: str) -> bool:
    """Check if a path should be ignored."""
    parts = Path(path).parts
    for part in parts:
        if part in IGNORE_DIRS:
            return True
    name = os.path.basename(path)
    if name in IGNORE_FILES:
        return True
    _, ext = os.path.splitext(name)
    if ext.lower() in IGNORE_EXTENSIONS:
        return True
    return False


def scan_directory(directory: str, max_depth: int = 4) -> dict[str, FileSnapshot]:
    """Scan a project directory for relevant files. Returns {path: snapshot}."""
    snapshots: dict[str, FileSnapshot] = {}
    base = Path(directory)

    for root, dirs, files in os.walk(directory):
        # Prune ignored directories
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]

        # Enforce max depth
        depth = len(Path(root).relative_to(base).parts)
        if depth > max_depth:
            dirs.clear()
            continue

        for fname in files:
            fpath = os.path.join(root, fname)
            if _should_ignore(fpath):
                continue
            try:
                stat = os.stat(fpath)
                snapshots[fpath] = FileSnapshot(
                    path=fpath,
                    mtime=stat.st_mtime,
                    size=stat.st_size,
                )
            except OSError:
                continue

    return snapshots


# ── Change Detector ────────────────────────────────────────

def detect_changes(
    old: dict[str, FileSnapshot],
    new: dict[str, FileSnapshot],
    project_name: str,
    project_dir: str,
) -> list[FileChange]:
    """Compare two snapshots and return detected changes."""
    changes: list[FileChange] = []
    now = time.time()

    # New or modified files
    for path, snap in new.items():
        if path not in old:
            changes.append(FileChange(
                path=path, project_name=project_name,
                project_dir=project_dir, change_type="created", timestamp=now,
            ))
        elif snap.mtime != old[path].mtime or snap.size != old[path].size:
            changes.append(FileChange(
                path=path, project_name=project_name,
                project_dir=project_dir, change_type="modified", timestamp=now,
            ))

    # Deleted files
    for path in old:
        if path not in new:
            changes.append(FileChange(
                path=path, project_name=project_name,
                project_dir=project_dir, change_type="deleted", timestamp=now,
            ))

    return changes


# ── Memory Builder ─────────────────────────────────────────

def _slugify(name: str) -> str:
    """Convert project name to tag-safe slug."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _ext_tag(path: str) -> str:
    """Get a language tag from file extension."""
    ext = Path(path).suffix.lower()
    mapping = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".jsx": "react", ".tsx": "react", ".html": "html",
        ".css": "css", ".sh": "shell", ".md": "markdown",
        ".yaml": "yaml", ".yml": "yaml", ".json": "json",
        ".toml": "toml", ".sql": "sql", ".rs": "rust",
        ".go": "golang",
    }
    return mapping.get(ext, "")


def build_memory_content(changes: list[FileChange]) -> tuple[str, list[str]]:
    """Build memory content string and tags from a batch of changes.

    Returns (content, tags).
    """
    if not changes:
        return "", []

    project = changes[0].project_name
    project_dir = changes[0].project_dir
    slug = _slugify(project)
    tags = [f"project:{slug}", "subconscious"]

    # Burst detection
    if len(changes) >= BURST_THRESHOLD:
        tags.append("dev-session")
        by_type: dict[str, int] = {}
        exts: set[str] = set()
        for c in changes:
            by_type[c.change_type] = by_type.get(c.change_type, 0) + 1
            ext_t = _ext_tag(c.path)
            if ext_t:
                exts.add(ext_t)

        parts = []
        for ct, count in sorted(by_type.items()):
            parts.append(f"{count} {ct}")
        summary = ", ".join(parts)

        content = f"Active dev session on {project}: {len(changes)} files changed ({summary})"
        for ext_t in sorted(exts):
            tags.append(ext_t)
        return content[:MAX_CONTENT_LEN], tags

    # Individual changes
    lines: list[str] = []
    for change in changes:
        relpath = os.path.relpath(change.path, project_dir)
        tags.append(f"file-{change.change_type}")

        ext_t = _ext_tag(change.path)
        if ext_t and ext_t not in tags:
            tags.append(ext_t)

        if change.change_type == "deleted":
            lines.append(f"Deleted {relpath} from {project}")
        elif change.change_type == "created":
            symbols = extract_symbols(change.path)
            if symbols:
                sym_str = ", ".join(symbols[:6])
                lines.append(f"Created {relpath} in {project} — {sym_str}")
            else:
                lines.append(f"Created {relpath} in {project}")
        else:  # modified
            symbols = extract_symbols(change.path)
            if symbols:
                sym_str = ", ".join(symbols[:6])
                lines.append(f"Modified {relpath} in {project} — {sym_str}")
            else:
                lines.append(f"Modified {relpath} in {project}")

    content = "; ".join(lines)
    return content[:MAX_CONTENT_LEN], tags


# ── Watcher ────────────────────────────────────────────────

class SubconsciousWatcher:
    """Polls project directories for changes and persists context to CortexDB."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH, dry_run: bool = False):
        self.db_path = db_path
        self.dry_run = dry_run
        self._cortex: Cortex | None = None
        self._snapshots: dict[str, dict[str, FileSnapshot]] = {}
        self._pending: list[FileChange] = []
        self._last_flush = time.time()

    def _get_cortex(self) -> Cortex:
        if self._cortex is None:
            self._cortex = Cortex(self.db_path)
        return self._cortex

    def scan_all(self) -> list[FileChange]:
        """Scan all projects and detect changes since last scan."""
        projects = load_projects_from_hot()
        all_changes: list[FileChange] = []

        for name, directory in projects.items():
            new_snap = scan_directory(directory)
            old_snap = self._snapshots.get(name, {})

            if old_snap:  # Skip first scan — baseline only
                changes = detect_changes(old_snap, new_snap, name, directory)
                all_changes.extend(changes)

            self._snapshots[name] = new_snap

        return all_changes

    def flush(self, changes: list[FileChange]) -> int:
        """Persist a batch of changes to CortexDB. Returns count stored."""
        if not changes:
            return 0

        # Group by project
        by_project: dict[str, list[FileChange]] = {}
        for c in changes:
            by_project.setdefault(c.project_name, []).append(c)

        stored = 0
        for _project, proj_changes in by_project.items():
            content, tags = build_memory_content(proj_changes)
            if not content:
                continue

            if self.dry_run:
                logger.info("[DRY RUN] Would store: %s (tags: %s)", content, tags)
                stored += 1
                continue

            cortex = self._get_cortex()
            cortex.remember(
                content=content,
                type=MEMORY_TYPE,
                tags=tags,
                importance=MEMORY_IMPORTANCE,
                emotion=MEMORY_EMOTION,
                source=MEMORY_SOURCE,
            )
            logger.info("Stored subconscious memory: %s", content[:80])
            stored += 1

        return stored

    def tick(self) -> int:
        """Run one scan + flush cycle. Returns number of memories stored."""
        changes = self.scan_all()
        self._pending.extend(changes)

        now = time.time()
        if now - self._last_flush >= DEBOUNCE_WINDOW_S and self._pending:
            stored = self.flush(self._pending)
            self._pending.clear()
            self._last_flush = now
            return stored

        return 0

    def close(self) -> None:
        """Flush remaining changes and close CortexDB."""
        if self._pending:
            self.flush(self._pending)
            self._pending.clear()
        if self._cortex is not None:
            self._cortex.close()
            self._cortex = None


# ── Daemon Integration ─────────────────────────────────────

async def run_subconscious(
    dry_run: bool = False,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Async loop for agent_memory_daemon integration."""
    watcher = SubconsciousWatcher(dry_run=dry_run)
    logger.info("Subconscious watcher starting (poll every %ds)...", POLL_INTERVAL_S)

    # Baseline scan (no changes detected on first pass)
    watcher.scan_all()
    logger.info(
        "Baseline captured: %d projects, %d files",
        len(watcher._snapshots),
        sum(len(s) for s in watcher._snapshots.values()),
    )

    _shutdown = shutdown_event or asyncio.Event()

    while not _shutdown.is_set():
        try:
            stored = watcher.tick()
            if stored > 0:
                logger.info("Subconscious: stored %d memories", stored)
        except Exception as e:
            logger.error("Subconscious scan failed: %s", e)

        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=POLL_INTERVAL_S)
            break
        except asyncio.TimeoutError:
            pass

    watcher.close()
    logger.info("Subconscious watcher stopped.")


# ── Self-Test ──────────────────────────────────────────────

def _self_test() -> bool:
    """Create temp dir, write files, scan, verify memories stored."""
    import tempfile

    logger.info("Running subconscious self-test...")

    with tempfile.TemporaryDirectory() as tmpdir:
        db_fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(db_fd)

        try:
            watcher = SubconsciousWatcher(db_path=db_path)
            # Manually inject project instead of reading hot.md
            watcher._snapshots["TestProject"] = {}
            projects = {"TestProject": tmpdir}

            # Baseline scan
            snap1 = scan_directory(tmpdir)
            watcher._snapshots["TestProject"] = snap1

            # Create a Python file
            test_py = os.path.join(tmpdir, "hello.py")
            with open(test_py, "w") as f:
                f.write("def greet(name):\n    return f'Hello {name}'\n\nclass Greeter:\n    def say_hi(self):\n        pass\n")

            # Create a JS file
            test_js = os.path.join(tmpdir, "app.js")
            with open(test_js, "w") as f:
                f.write("function init() { console.log('starting'); }\nclass App { run() {} }\n")

            # Detect changes
            snap2 = scan_directory(tmpdir)
            changes = detect_changes(snap1, snap2, "TestProject", tmpdir)

            if len(changes) < 2:
                logger.error("Expected at least 2 changes, got %d", len(changes))
                return False

            # Store
            stored = watcher.flush(changes)
            if stored < 1:
                logger.error("Expected at least 1 memory stored, got %d", stored)
                return False

            # Verify in CortexDB
            cortex = Cortex(db_path)
            memories = cortex.recall("TestProject", limit=10)
            cortex.close()

            if not memories:
                logger.error("No memories found in CortexDB after flush")
                return False

            found_subconscious = any("subconscious" in m.tags for m in memories)
            if not found_subconscious:
                logger.error("No memory has 'subconscious' tag")
                return False

            logger.info(
                "Self-test PASSED: %d changes detected, %d memories stored",
                len(changes), stored,
            )

            watcher.close()
            return True

        finally:
            try:
                os.unlink(db_path)
            except OSError:
                pass


# ── CLI ────────────────────────────────────────────────────

def main() -> None:
    """CLI entry point."""
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Subconscious Context Watcher")
    parser.add_argument("--test-mode", action="store_true", help="Run self-test")
    parser.add_argument("--dry-run", action="store_true", help="Log only, no writes")
    parser.add_argument("--once", action="store_true", help="Single scan then exit")
    args = parser.parse_args()

    if args.test_mode:
        success = _self_test()
        raise SystemExit(0 if success else 1)

    if args.once:
        watcher = SubconsciousWatcher(dry_run=args.dry_run)
        watcher.scan_all()  # baseline
        logger.info("Baseline done. Waiting %ds for changes...", POLL_INTERVAL_S)
        time.sleep(POLL_INTERVAL_S)
        stored = watcher.tick()
        watcher.close()
        logger.info("Stored %d memories", stored)
        return

    # Continuous mode
    asyncio.run(run_subconscious(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
