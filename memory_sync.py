#!/usr/bin/env python3
"""
Memory Sync Daemon — Auto-update warm project files from filesystem state.

Polls project directories listed in hot.md. When significant changes are
detected (new files, deleted files, renamed files), regenerates the "Key Files"
section of the corresponding warm file and updates the timestamp.

Dual-writes project state to CortexDB for searchable history with
biologically-inspired decay and priming.

No LLM required. Pure filesystem diffing.

Usage:
    python3 memory_sync.py              # Single sync pass
    python3 memory_sync.py --daemon     # Run continuously (every 60s)
    python3 memory_sync.py --dry-run    # Report changes without writing
"""
from __future__ import annotations

import argparse
import os
import re
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

MEMORY_DIR = Path(__file__).parent
HOT_FILE = MEMORY_DIR / "hot.md"
PROJECTS_DIR = MEMORY_DIR / "projects"

SYNC_INTERVAL_S = 60
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".next", "dist", "build", ".agents"}
KEY_EXTENSIONS = {".py", ".ts", ".js", ".toml", ".yaml", ".yml", ".sh", ".md", ".json", ".sql"}

# Files that always matter when present
LANDMARK_FILES = {"pyproject.toml", "package.json", "Cargo.toml", "go.mod", "Makefile", "Dockerfile"}

_running = True


def _handle_sigterm(signum, frame):
    global _running
    _running = False


# ── Hot.md Parser ──────────────────────────────────────────

def parse_hot_projects() -> dict[str, dict]:
    """Parse hot.md project table into {name: {location, warm_file}}."""
    if not HOT_FILE.exists():
        return {}

    projects = {}
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
            if len(cells) >= 4:
                name = cells[0]
                location = cells[1].strip("`").replace("~/", os.path.expanduser("~/"))
                warm_ref = cells[3].strip("`")
                warm_file = MEMORY_DIR / warm_ref if warm_ref and warm_ref != "—" else None
                projects[name] = {"location": location, "warm_file": warm_file}
        elif in_table and "|" not in stripped:
            in_table = False

    return projects


# ── Directory Scanner ──────────────────────────────────────

def scan_project_files(project_dir: str) -> list[dict]:
    """Scan a project directory and return key files with metadata.

    Returns list of {name, rel_path, size, ext, is_landmark}.
    """
    root = Path(project_dir)
    if not root.exists():
        return []

    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        rel_dir = Path(dirpath).relative_to(root)

        for fname in filenames:
            fpath = Path(dirpath) / fname
            ext = fpath.suffix.lower()

            # Skip non-key files
            if ext not in KEY_EXTENSIONS and fname not in LANDMARK_FILES:
                continue

            try:
                size = fpath.stat().st_size
            except OSError:
                continue

            rel_path = str(rel_dir / fname) if str(rel_dir) != "." else fname
            files.append({
                "name": fname,
                "rel_path": rel_path,
                "size": size,
                "ext": ext,
                "is_landmark": fname in LANDMARK_FILES,
            })

    # Sort: landmarks first, then by path
    files.sort(key=lambda f: (not f["is_landmark"], f["rel_path"]))
    return files


def files_to_table(files: list[dict], max_rows: int = 20) -> str:
    """Convert scanned files to a markdown table string."""
    if not files:
        return "| File | Purpose |\n|------|---------|"

    lines = ["| File | Size |", "|------|------|"]
    for f in files[:max_rows]:
        size_str = _human_size(f["size"])
        lines.append(f"| `{f['rel_path']}` | {size_str} |")

    if len(files) > max_rows:
        lines.append(f"| *... and {len(files) - max_rows} more* | |")

    return "\n".join(lines)


def _human_size(b: int) -> str:
    """Convert bytes to human-readable size."""
    for unit in ("B", "KB", "MB"):
        if b < 1024:
            return f"{b:.0f} {unit}" if unit == "B" else f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} GB"


# ── Warm File Updater ──────────────────────────────────────

def extract_existing_files(warm_content: str) -> str | None:
    """Extract the current Key Files table from a warm file."""
    # Look for "## Key Files" section and extract until next ##
    match = re.search(
        r"(## Key Files.*?)(?=\n## |\n---|\*Last updated|\Z)",
        warm_content, re.DOTALL
    )
    return match.group(1).strip() if match else None


def update_warm_file(warm_path: Path, new_table: str, file_count: int) -> bool:
    """Update the Key Files section in a warm file. Returns True if changed."""
    if not warm_path.exists():
        return False

    content = warm_path.read_text()
    existing = extract_existing_files(content)

    new_section = f"## Key Files\n\n{new_table}"

    if existing:
        # Check if meaningfully different (ignore whitespace)
        old_norm = re.sub(r"\s+", " ", existing)
        new_norm = re.sub(r"\s+", " ", new_section)
        if old_norm == new_norm:
            return False  # No change
        content = content.replace(existing, new_section)
    else:
        # No existing Key Files section — insert before Known Issues or at end
        insert_point = content.find("## Known Issues")
        if insert_point == -1:
            insert_point = content.find("---\n\n*Last updated")
        if insert_point == -1:
            content += f"\n\n{new_section}\n"
        else:
            content = content[:insert_point] + new_section + "\n\n" + content[insert_point:]

    # Update timestamp
    content = re.sub(
        r"\*Last updated: \d{4}-\d{2}-\d{2}\*",
        f"*Last updated: {datetime.now().strftime('%Y-%m-%d')}*",
        content,
    )

    warm_path.write_text(content)
    return True


# ── Auto-detect New Projects ──────────────────────────────

def detect_new_projects(agent_system_dir: str) -> list[dict]:
    """Find project directories not yet in hot.md."""
    known_locations = {
        info["location"].rstrip("/")
        for info in parse_hot_projects().values()
    }

    new_projects = []
    root = Path(agent_system_dir)
    if not root.exists():
        return []

    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        child_str = str(child).rstrip("/")
        if child_str in known_locations:
            continue
        # Check for landmark files
        has_landmark = any((child / lm).exists() for lm in LANDMARK_FILES)
        has_code = any(child.glob(f"*{ext}") for ext in [".py", ".ts", ".js"])
        if has_landmark or has_code:
            new_projects.append({
                "name": child.name,
                "location": str(child),
            })

    return new_projects


# ── Sync Engine ────────────────────────────────────────────

def sync_once(dry_run: bool = False) -> dict:
    """Run a single sync pass. Returns summary of actions taken."""
    projects = parse_hot_projects()
    results = {"checked": 0, "updated": 0, "skipped": 0, "errors": []}

    for name, info in projects.items():
        location = info["location"]
        warm_file = info.get("warm_file")

        if warm_file is None:
            results["skipped"] += 1
            continue

        results["checked"] += 1

        if not Path(location).exists():
            results["errors"].append(f"{name}: directory not found at {location}")
            continue

        files = scan_project_files(location)
        table = files_to_table(files)

        if dry_run:
            existing = extract_existing_files(warm_file.read_text()) if warm_file.exists() else None
            if existing:
                old_count = existing.count("|") // 2 - 1  # rough row count
                new_count = len(files)
                if old_count != new_count:
                    print(f"  [WOULD UPDATE] {name}: {old_count} → {new_count} files")
                    results["updated"] += 1
                else:
                    results["skipped"] += 1
            continue

        if warm_file.exists():
            changed = update_warm_file(warm_file, table, len(files))
            if changed:
                print(f"  [UPDATED] {name}: {len(files)} key files")
                results["updated"] += 1
                # Dual-write to CortexDB
                _cortex_snapshot(name, len(files), location)
        else:
            results["skipped"] += 1

    return results


def daemon_loop(dry_run: bool = False):
    """Run sync continuously."""
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    print(f"[MEMORY_SYNC] Daemon started (interval={SYNC_INTERVAL_S}s)")

    while _running:
        try:
            results = sync_once(dry_run)
            if results["updated"] > 0 or results["errors"]:
                ts = datetime.now().strftime("%H:%M:%S")
                print(
                    f"[MEMORY_SYNC] {ts} — "
                    f"checked={results['checked']}, "
                    f"updated={results['updated']}, "
                    f"errors={len(results['errors'])}"
                )
                for err in results["errors"]:
                    print(f"  ⚠ {err}")
        except Exception as e:
            print(f"[MEMORY_SYNC] Sweep error: {e}", file=sys.stderr)

        # Interruptible sleep
        for _ in range(SYNC_INTERVAL_S):
            if not _running:
                break
            time.sleep(1)

    print("[MEMORY_SYNC] Daemon stopped")


def main():
    parser = argparse.ArgumentParser(description="Memory sync daemon")
    parser.add_argument("--daemon", action="store_true", help="Run continuously")
    parser.add_argument("--dry-run", action="store_true", help="Report without writing")
    parser.add_argument("--detect-new", action="store_true",
                        help="Scan for untracked projects")
    args = parser.parse_args()

    if args.detect_new:
        # Scan the Agent_System directory for new projects
        agent_dir = os.environ.get("AGENT_SYSTEM_DIR", os.path.expanduser("~/projects"))
        new = detect_new_projects(agent_dir)
        if new:
            print(f"Found {len(new)} untracked projects:")
            for p in new:
                print(f"  • {p['name']} → {p['location']}")
        else:
            print("No new projects found.")
        return

    if args.daemon:
        daemon_loop(dry_run=args.dry_run)
    else:
        print("[MEMORY_SYNC] Single pass")
        results = sync_once(dry_run=args.dry_run)
        print(
            f"  Checked: {results['checked']}, "
            f"Updated: {results['updated']}, "
            f"Skipped: {results['skipped']}"
        )
        for err in results["errors"]:
            print(f"  ⚠ {err}")


def _cortex_snapshot(project_name: str, file_count: int, location: str) -> None:
    """Store a project state snapshot in CortexDB. Best-effort."""
    try:
        from memory_bridge import get_bridge
        bridge = get_bridge()
        bridge.store_project_state(
            project_name=project_name,
            file_count=file_count,
            status="synced",
            location=location,
        )
    except Exception as e:
        print(f"  ⚠ CortexDB snapshot failed: {e}")


if __name__ == "__main__":
    main()
