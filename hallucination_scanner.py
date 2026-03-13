"""Hallucination Scanner — Validates Python imports in agent-generated code.

Watches for recently modified .py files under ~/projects/ and:
1. Runs ast.parse() to catch syntax errors
2. Extracts all import/from-import statements
3. Validates imports resolve via importlib.util.find_spec()
4. Logs unresolvable imports to CortexDB and a report file

Output: ~/.gemini/memory/hallucination_report.md

Usage:
    python3 hallucination_scanner.py                           # Scan all projects
    python3 hallucination_scanner.py --target ~/path/to/project  # Scan one project
"""
from __future__ import annotations

import ast
import importlib.util
import os
import sys
import time
from pathlib import Path

_CORTEX_ROOT = os.environ.get("AGENT_CORTEX_ROOT", os.path.expanduser("~/.cortexdb"))
if _CORTEX_ROOT not in sys.path:
    sys.path.insert(0, _CORTEX_ROOT)

AGENT_SYSTEM_DIR = os.environ.get("AGENT_SYSTEM_DIR", os.path.expanduser("~/projects"))
REPORT_FILE = Path(os.path.expanduser("~/.gemini/memory/hallucination_report.md"))
DEFAULT_DB_PATH = os.path.expanduser("~/.cortexdb/agent_system.db")

# Skip these directories
SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", "venv", ".venv",
    "dist", "build", ".eggs", ".tox", "site-packages",
}

# Known first-party packages that importlib won't find without install
KNOWN_FIRST_PARTY = {
    "cortex", "mnemos", "reaper", "CortexDB",
}

# How recently a file must have been modified to scan (hours)
SCAN_RECENCY_HOURS = 24


def find_python_files(
    root: str,
    max_age_hours: float = SCAN_RECENCY_HOURS,
) -> list[Path]:
    """Find recently modified .py files under root."""
    cutoff = time.time() - (max_age_hours * 3600)
    results = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            fpath = Path(dirpath) / fname
            try:
                if fpath.stat().st_mtime >= cutoff:
                    results.append(fpath)
            except OSError:
                continue

    return results


def extract_imports(source: str) -> list[dict]:
    """Extract import statements from Python source code.

    Returns list of {module, names, line, type} dicts.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append({
                    "module": alias.name,
                    "names": [],
                    "line": node.lineno,
                    "type": "import",
                })
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names = [a.name for a in (node.names or [])]
                imports.append({
                    "module": node.module,
                    "names": names,
                    "line": node.lineno,
                    "type": "from",
                })

    return imports


def validate_import(module_name: str, source_file: Path | None = None) -> bool:
    """Check if a module can be resolved.

    Handles:
    - Relative imports (always pass)
    - Known first-party packages
    - Sibling modules within the same package directory
    - Standard importlib resolution
    """
    if module_name.startswith("."):
        return True

    top_level = module_name.split(".")[0]
    if top_level in KNOWN_FIRST_PARTY:
        return True

    # Check if it's a sibling module in the same package
    if source_file is not None:
        parent = source_file.parent
        # The directory is a package if it has __init__.py
        init_file = parent / "__init__.py"
        if init_file.exists():
            sibling_file = parent / f"{top_level}.py"
            sibling_dir = parent / top_level
            if sibling_file.exists() or (
                sibling_dir.is_dir()
                and (sibling_dir / "__init__.py").exists()
            ):
                return True

    try:
        spec = importlib.util.find_spec(top_level)
        return spec is not None
    except (ModuleNotFoundError, ValueError):
        return False


def scan_file(filepath: Path) -> dict:
    """Scan a single Python file for hallucinated imports.

    Returns {path, syntax_ok, imports, unresolved, errors}.
    """
    result = {
        "path": str(filepath),
        "syntax_ok": True,
        "imports": [],
        "unresolved": [],
        "errors": [],
    }

    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        result["errors"].append(f"Read error: {e}")
        return result

    # AST parse check
    try:
        ast.parse(source)
    except SyntaxError as e:
        result["syntax_ok"] = False
        result["errors"].append(f"Syntax error line {e.lineno}: {e.msg}")
        return result

    # Extract and validate imports
    imports = extract_imports(source)
    result["imports"] = imports

    for imp in imports:
        if not validate_import(imp["module"], source_file=filepath):
            result["unresolved"].append(imp)

    return result


def scan_directory(
    root: str,
    max_age_hours: float = SCAN_RECENCY_HOURS,
) -> list[dict]:
    """Scan all recently modified Python files in a directory tree."""
    files = find_python_files(root, max_age_hours)
    return [scan_file(f) for f in files]


def generate_report(results: list[dict]) -> str:
    """Generate a markdown report from scan results."""
    lines = [
        "# Hallucination Scanner Report",
        f"*Scanned: {time.strftime('%Y-%m-%d %H:%M')}*",
        "",
    ]

    total_files = len(results)
    syntax_errors = sum(1 for r in results if not r["syntax_ok"])
    unresolved_total = sum(len(r["unresolved"]) for r in results)

    lines.append(f"**Files scanned:** {total_files}")
    lines.append(f"**Syntax errors:** {syntax_errors}")
    lines.append(f"**Unresolved imports:** {unresolved_total}")
    lines.append("")

    # Report issues
    issues = [r for r in results if not r["syntax_ok"] or r["unresolved"]]
    if not issues:
        lines.append("✓ No hallucinated imports detected.")
        lines.append("")
        return "\n".join(lines)

    lines.append("## Issues")
    lines.append("")

    for result in issues:
        rel_path = result["path"].replace(AGENT_SYSTEM_DIR + "/", "")
        lines.append(f"### `{rel_path}`")
        lines.append("")

        if not result["syntax_ok"]:
            for err in result["errors"]:
                lines.append(f"- ⚠ {err}")

        for imp in result["unresolved"]:
            module = imp["module"]
            line = imp["line"]
            lines.append(f"- ❌ Line {line}: `{module}` — cannot resolve")

        lines.append("")

    return "\n".join(lines)


def log_to_cortexdb(results: list[dict]) -> int:
    """Log unresolved imports to CortexDB as warnings. Returns count logged."""
    issues = [r for r in results if r["unresolved"]]
    if not issues:
        return 0

    try:
        from cortex.engine import Cortex
        cortex = Cortex(DEFAULT_DB_PATH)
        logged = 0

        for result in issues:
            for imp in result["unresolved"]:
                content = (
                    f"Hallucination warning: unresolved import '{imp['module']}' "
                    f"at line {imp['line']} in {result['path']}"
                )
                cortex.remember(
                    content,
                    type="procedural",
                    tags=["hallucination-warning", "import-error"],
                    importance=0.6,
                    emotion="surprise",
                    source="generated",
                    context="hallucination_scanner automated check",
                )
                logged += 1

        cortex.close()
        return logged
    except Exception:
        return 0


def run_scan(target: str | None = None, max_age_hours: float = SCAN_RECENCY_HOURS) -> dict:
    """Run a full scan. Returns summary dict."""
    scan_root = target or AGENT_SYSTEM_DIR
    results = scan_directory(scan_root, max_age_hours)

    report = generate_report(results)
    REPORT_FILE.write_text(report)

    logged = log_to_cortexdb(results)

    return {
        "files_scanned": len(results),
        "syntax_errors": sum(1 for r in results if not r["syntax_ok"]),
        "unresolved_imports": sum(len(r["unresolved"]) for r in results),
        "cortexdb_logged": logged,
        "report_path": str(REPORT_FILE),
    }


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Hallucination scanner")
    parser.add_argument("--target", help="Specific directory to scan")
    parser.add_argument(
        "--max-age", type=float, default=SCAN_RECENCY_HOURS,
        help="Max file age in hours to include",
    )
    args = parser.parse_args()

    summary = run_scan(target=args.target, max_age_hours=args.max_age)
    print(f"\nHallucination Scanner Results:")
    for key, val in summary.items():
        print(f"  {key}: {val}")


if __name__ == "__main__":
    main()
