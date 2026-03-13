"""Onboarding Organ — Automated codebase comprehension.

When the agent encounters an unfamiliar repo, this organ:
1. Walks the file tree (respecting .gitignore)
2. AST-parses key files for symbols and imports
3. Detects framework and architecture patterns
4. Maps the dependency graph
5. Produces a warm file for projects/
6. Stores the detected architecture in CortexDB

Usage:
    from agent_memory_kit.onboarding import onboard
    result = onboard("/path/to/project", slug="my-project")
"""
from __future__ import annotations

import ast
import json
import logging
import os
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .config import get_config

log = logging.getLogger("agent-memory.onboarding")


# ── Constants ──────────────────────────────────────────────

MAX_FILE_SIZE = 256_000  # 256KB — skip large generated files
MAX_MANIFEST_SIZE = 65_536  # 64KB — cap for config/manifest files


def _safe_read(path: Path, max_size: int = MAX_MANIFEST_SIZE) -> str:
    """Read a file with size guard. Returns empty string if too large or unreadable."""
    try:
        if path.stat().st_size > max_size:
            return ""
        return path.read_text(errors="replace")
    except Exception:
        return ""

IGNORE_DIRS = frozenset({
    ".git", ".svn", ".hg", "__pycache__", ".mypy_cache", ".pytest_cache",
    "node_modules", ".venv", "venv", ".env", "dist", "build",
    ".next", ".nuxt", "target", "vendor", ".tox", "htmlcov",
    ".eggs", "*.egg-info", ".cache", ".parcel-cache",
})

IGNORE_EXTENSIONS = frozenset({
    ".pyc", ".pyo", ".so", ".o", ".a", ".dylib", ".dll",
    ".lock", ".log", ".db", ".sqlite", ".sqlite3",
    ".whl", ".tar", ".gz", ".zip", ".jar",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot",
    ".map", ".min.js", ".min.css",
})

SOURCE_EXTENSIONS = frozenset({
    ".py", ".js", ".ts", ".jsx", ".tsx", ".mjs",
    ".go", ".rs", ".java", ".cpp", ".c", ".h",
    ".rb", ".php", ".swift", ".kt",
})

LANG_MAP = {
    ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
    ".jsx": "React JSX", ".tsx": "React TSX", ".mjs": "JavaScript",
    ".go": "Go", ".rs": "Rust", ".java": "Java",
    ".cpp": "C++", ".c": "C", ".h": "C/C++ Header",
    ".rb": "Ruby", ".php": "PHP", ".swift": "Swift", ".kt": "Kotlin",
}


# ── Data Models ────────────────────────────────────────────

@dataclass
class FileInfo:
    """Metadata about a single source file."""
    path: str
    relative: str
    language: str
    size: int
    lines: int
    symbols: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)


@dataclass
class FrameworkSignal:
    """A detected framework or tool."""
    name: str
    source: str  # what triggered the detection


@dataclass
class OnboardResult:
    """Complete onboarding analysis."""
    project_name: str
    project_path: str
    slug: str
    description: str
    primary_language: str
    languages: dict[str, int]  # lang → file count
    total_files: int
    total_lines: int
    frameworks: list[FrameworkSignal]
    architecture: str
    key_files: list[tuple[str, str]]  # (relative_path, description)
    entry_points: list[str]
    external_deps: list[str]
    internal_graph: dict[str, list[str]]  # module → imports from
    patterns: list[str]
    warm_file_path: str = ""


# ── Tree Scanner ───────────────────────────────────────────

def _should_ignore_dir(name: str) -> bool:
    """Check if a directory should be skipped."""
    return name in IGNORE_DIRS or name.startswith(".") or name.endswith(".egg-info")


def scan_tree(root: str, max_depth: int = 6) -> list[FileInfo]:
    """Walk the project tree, returning FileInfo for each source file."""
    root_path = Path(root).resolve()
    files: list[FileInfo] = []

    # Check for .gitignore patterns
    gitignore_patterns: list[str] = []
    gi_path = root_path / ".gitignore"
    if gi_path.exists():
        for line in _safe_read(gi_path).splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                gitignore_patterns.append(line)

    for dirpath, dirnames, filenames in os.walk(root_path):
        # Depth check
        depth = len(Path(dirpath).relative_to(root_path).parts)
        if depth > max_depth:
            dirnames.clear()
            continue

        # Prune ignored directories (mutate in-place for os.walk)
        dirnames[:] = [
            d for d in dirnames
            if not _should_ignore_dir(d)
        ]

        for fname in filenames:
            fpath = Path(dirpath) / fname
            ext = fpath.suffix.lower()

            # Skip non-source and ignored
            if ext in IGNORE_EXTENSIONS:
                continue
            if ext not in SOURCE_EXTENSIONS and ext not in {".json", ".toml", ".yaml", ".yml", ".md", ".sh"}:
                continue

            try:
                size = fpath.stat().st_size
            except OSError:
                continue
            if size > MAX_FILE_SIZE or size == 0:
                continue

            relative = str(fpath.relative_to(root_path))
            language = LANG_MAP.get(ext, ext.lstrip("."))

            # Count lines
            try:
                content = fpath.read_text(errors="replace")
                lines = content.count("\n") + 1
            except Exception:
                lines = 0
                content = ""

            # Extract symbols for source files
            symbols: list[str] = []
            imports: list[str] = []
            if ext == ".py" and content:
                symbols, imports = _parse_python_quick(content)
            elif ext in {".ts", ".js", ".tsx", ".jsx", ".mjs"} and content:
                symbols, imports = _parse_js_quick(content)

            files.append(FileInfo(
                path=str(fpath),
                relative=relative,
                language=language,
                size=size,
                lines=lines,
                symbols=symbols,
                imports=imports,
            ))

    return files


# ── Quick Parsers ──────────────────────────────────────────

def _parse_python_quick(content: str) -> tuple[list[str], list[str]]:
    """Extract top-level symbols and imports from Python source."""
    symbols = []
    imports = []
    try:
        tree = ast.parse(content)
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append(f"def {node.name}")
            elif isinstance(node, ast.ClassDef):
                symbols.append(f"class {node.name}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module)
    except SyntaxError:
        pass
    return symbols, imports


_JS_SYM_RE = re.compile(
    r"(?:export\s+)?(?:async\s+)?(?:function|class)\s+(\w+)", re.MULTILINE
)
_JS_CONST_RE = re.compile(
    r"(?:export\s+)?(?:const|let)\s+(\w+)\s*=\s*(?:async\s+)?\(", re.MULTILINE
)
_JS_IMPORT_RE = re.compile(
    r"import\s+(?:{[^}]+}|[^;]+)\s+from\s+['\"]([^'\"]+)['\"]", re.MULTILINE
)


def _parse_js_quick(content: str) -> tuple[list[str], list[str]]:
    """Extract symbols and imports from JS/TS source."""
    symbols = [m.group(1) for m in _JS_SYM_RE.finditer(content)]
    symbols += [m.group(1) for m in _JS_CONST_RE.finditer(content)]
    imports = [m.group(1) for m in _JS_IMPORT_RE.finditer(content)]
    return symbols, imports


# ── Framework Detector ─────────────────────────────────────

FRAMEWORK_DETECTORS: list[tuple[str, str, str]] = [
    # (framework_name, file_to_check, content_pattern)
    ("FastAPI", "pyproject.toml", r"fastapi"),
    ("FastAPI", "requirements.txt", r"fastapi"),
    ("Django", "manage.py", r"django"),
    ("Flask", "pyproject.toml", r"flask"),
    ("Flask", "requirements.txt", r"flask"),
    ("Express", "package.json", r'"express"'),
    ("Next.js", "package.json", r'"next"'),
    ("React", "package.json", r'"react"'),
    ("Vue", "package.json", r'"vue"'),
    ("Svelte", "package.json", r'"svelte"'),
    ("Electron", "package.json", r'"electron"'),
    ("Pydantic", "pyproject.toml", r"pydantic"),
    ("SQLAlchemy", "pyproject.toml", r"sqlalchemy"),
    ("Pytest", "pyproject.toml", r"pytest"),
]

FILE_PRESENCE_SIGNALS: list[tuple[str, str]] = [
    ("Dockerfile", "Docker"),
    ("docker-compose.yml", "Docker Compose"),
    ("docker-compose.yaml", "Docker Compose"),
    (".github/workflows", "GitHub Actions CI"),
    ("alembic.ini", "Alembic migrations"),
    ("Makefile", "Make build system"),
    ("tsconfig.json", "TypeScript"),
    (".service", "systemd service"),
]


def detect_frameworks(root: str, files: list[FileInfo]) -> list[FrameworkSignal]:
    """Detect frameworks and tools from file content and presence."""
    root_path = Path(root)
    signals: list[FrameworkSignal] = []
    seen: set[str] = set()

    # Content-based detection
    for fw_name, check_file, pattern in FRAMEWORK_DETECTORS:
        if fw_name in seen:
            continue
        fpath = root_path / check_file
        if fpath.exists():
            try:
                content = _safe_read(fpath)
                if re.search(pattern, content, re.IGNORECASE):
                    signals.append(FrameworkSignal(fw_name, check_file))
                    seen.add(fw_name)
            except Exception:
                pass

    # File presence detection
    for check_path, signal_name in FILE_PRESENCE_SIGNALS:
        if signal_name in seen:
            continue
        if (root_path / check_path).exists():
            signals.append(FrameworkSignal(signal_name, check_path))
            seen.add(signal_name)

    # Check for systemd services in the project
    service_files = [f for f in files if f.relative.endswith(".service")]
    if service_files and "systemd service" not in seen:
        signals.append(FrameworkSignal("systemd service", service_files[0].relative))

    return signals


# ── Architecture Classifier ───────────────────────────────

def classify_architecture(files: list[FileInfo], frameworks: list[FrameworkSignal]) -> str:
    """Infer the architecture pattern from file structure."""
    names = {f.relative for f in files}
    basenames = {Path(f.relative).name for f in files}
    dirs = {str(Path(f.relative).parent) for f in files if "/" in f.relative}
    fw_names = {s.name for s in frameworks}

    # React/Next.js SPA
    if any("components" in d for d in dirs) and any("pages" in d or "app" in d for d in dirs):
        return "React SPA" if "React" in fw_names else "Component-based SPA"

    # FastAPI/Flask layered service
    if {"models.py", "api.py"} <= basenames:
        if "store.py" in basenames or "db.py" in basenames:
            return "Layered service (models → logic → store → api)"
        return "API service (models → api)"

    # Express MVC
    if any("routes" in d for d in dirs) and any("models" in d or "middleware" in d for d in dirs):
        return "Express MVC"

    # CLI tool
    if "__main__.py" in basenames and len(files) < 10:
        return "CLI tool"

    # Daemon/service
    if "daemon.py" in basenames or "daemon.ts" in basenames:
        return "Background daemon/service"

    # Library/package
    if "__init__.py" in basenames and "setup.py" in basenames or "pyproject.toml" in basenames:
        if not any(bn in basenames for bn in {"api.py", "server.py", "app.py"}):
            return "Python library/package"

    # Monorepo
    if len(dirs) > 5 and any("src" in d for d in dirs):
        return "Monorepo/multi-module"

    # Simple script
    if len(files) <= 3:
        return "Script/utility"

    return "Standard project"


# ── Dependency Mapper ─────────────────────────────────────

def map_dependencies(root: str, files: list[FileInfo]) -> tuple[list[str], dict[str, list[str]], list[str]]:
    """Map external deps, internal dep graph, and entry points.

    Returns (external_deps, internal_graph, entry_points).
    """
    root_path = Path(root)

    # External deps from manifest
    external: list[str] = []
    pyproject = root_path / "pyproject.toml"
    if pyproject.exists():
        try:
            content = _safe_read(pyproject)
            # Simple regex extraction of dependencies
            deps_match = re.findall(r'"([a-zA-Z][a-zA-Z0-9_-]+)(?:[><=!~].*?)?"', content)
            # Filter likely dep names (not section headers)
            external = sorted(set(d.lower() for d in deps_match if len(d) > 2 and d not in {"project", "build", "system", "optional"}))
        except Exception:
            pass

    pkg_json = root_path / "package.json"
    if pkg_json.exists():
        try:
            data = json.loads(_safe_read(pkg_json))
            for section in ("dependencies", "devDependencies"):
                external.extend(data.get(section, {}).keys())
            external = sorted(set(external))
        except Exception:
            pass

    # Internal graph: which modules import which
    source_files = [f for f in files if f.language in {"Python", "TypeScript", "JavaScript", "React TSX", "React JSX"}]
    module_names = set()
    for f in source_files:
        stem = Path(f.relative).stem
        if stem != "__init__":
            module_names.add(stem)

    internal_graph: dict[str, list[str]] = defaultdict(list)
    imported_modules: set[str] = set()
    for f in source_files:
        stem = Path(f.relative).stem
        if stem == "__init__":
            continue
        for imp in f.imports:
            imp_base = imp.split(".")[-1]
            if imp_base in module_names and imp_base != stem:
                internal_graph[stem].append(imp_base)
                imported_modules.add(imp_base)

    # Entry points: source files that no other file imports
    entry_points = []
    for f in source_files:
        stem = Path(f.relative).stem
        if stem == "__init__":
            continue
        if stem not in imported_modules:
            # Extra signals for entry points
            is_entry = (
                stem in {"main", "__main__", "app", "server", "cli", "index", "daemon"}
                or stem == Path(f.relative).parent.name  # same name as dir
                or any(s.startswith("def main") for s in f.symbols)
            )
            if is_entry:
                entry_points.append(f.relative)

    return external, dict(internal_graph), entry_points


# ── Key File Identifier ───────────────────────────────────

def identify_key_files(files: list[FileInfo]) -> list[tuple[str, str]]:
    """Identify the most important files and generate descriptions."""
    key: list[tuple[str, str]] = []
    source = [f for f in files if Path(f.relative).suffix in SOURCE_EXTENSIONS]

    # Sort by symbol count (most symbols = most important)
    source.sort(key=lambda f: len(f.symbols), reverse=True)

    for f in source[:12]:
        if not f.symbols:
            continue
        classes = [s for s in f.symbols if s.startswith("class ")]
        funcs = [s for s in f.symbols if s.startswith("def ")]
        parts = []
        if classes:
            parts.append(f"{len(classes)} class{'es' if len(classes) > 1 else ''}")
        if funcs:
            parts.append(f"{len(funcs)} function{'s' if len(funcs) > 1 else ''}")
        desc = ", ".join(parts) if parts else f"{len(f.symbols)} symbols"
        key.append((f.relative, f"{f.language} — {desc} ({f.lines} lines)"))

    return key


# ── Description Extractor ─────────────────────────────────

def extract_description(root: str) -> str:
    """Try to extract a one-line project description."""
    root_path = Path(root)

    # Try pyproject.toml
    pyproject = root_path / "pyproject.toml"
    if pyproject.exists():
        for line in _safe_read(pyproject).splitlines():
            if line.strip().startswith("description"):
                match = re.search(r'"([^"]+)"', line)
                if match:
                    return match.group(1)

    # Try package.json
    pkg = root_path / "package.json"
    if pkg.exists():
        try:
            data = json.loads(_safe_read(pkg))
            if "description" in data:
                return data["description"]
        except Exception:
            pass

    # Try first line of README
    for readme in ["README.md", "README.rst", "README.txt", "README"]:
        rp = root_path / readme
        if rp.exists():
            lines = _safe_read(rp).splitlines()
            for line in lines:
                line = line.strip().lstrip("#").strip()
                if line and len(line) > 5:
                    return line[:120]

    return ""


# ── Warm File Generator ───────────────────────────────────

def generate_warm_file(result: OnboardResult) -> str:
    """Generate structured warm file markdown from onboarding results."""
    lines = [
        f"# {result.project_name}",
        "",
        f"> {result.description}" if result.description else "> (auto-onboarded project)",
        "",
        "## Location",
        f"`{result.project_path}/`",
        "",
        "## Architecture",
    ]

    # Language breakdown
    lang_parts = [f"{lang}({count})" for lang, count in sorted(result.languages.items(), key=lambda x: -x[1])[:4]]
    lines.append(f"- **Languages**: {', '.join(lang_parts)} — {result.total_files} files, {result.total_lines:,} LOC")
    lines.append(f"- **Pattern**: {result.architecture}")

    if result.frameworks:
        fw_str = ", ".join(s.name for s in result.frameworks)
        lines.append(f"- **Frameworks**: {fw_str}")

    lines.append("")

    # Key files
    if result.key_files:
        lines.append("## Key Files")
        for rel, desc in result.key_files:
            lines.append(f"- `{rel}` — {desc}")
        lines.append("")

    # Dependencies
    if result.external_deps:
        lines.append("## Dependencies")
        lines.append(f"- External: {', '.join(result.external_deps[:15])}")
        if result.internal_graph:
            chains = []
            for mod, deps in sorted(result.internal_graph.items()):
                chains.append(f"{mod} ← {', '.join(deps)}")
            lines.append(f"- Internal: {'; '.join(chains[:8])}")
        lines.append("")

    # Entry points
    if result.entry_points:
        lines.append("## Entry Points")
        for ep in result.entry_points:
            lines.append(f"- `{ep}`")
        lines.append("")

    # Patterns
    if result.patterns:
        lines.append("## Patterns Detected")
        for p in result.patterns:
            lines.append(f"- {p}")
        lines.append("")

    lines.append("## Status")
    lines.append("Auto-onboarded — needs manual review for accuracy.")
    lines.append("")
    lines.append(f"*Auto-generated by onboarding organ on {time.strftime('%Y-%m-%d')}*")

    return "\n".join(lines) + "\n"


# ── Main Entry Point ──────────────────────────────────────

def onboard(
    project_path: str,
    slug: str | None = None,
    save: bool = True,
    store_pattern: bool = True,
) -> OnboardResult:
    """Run full onboarding analysis on a project.

    Args:
        project_path: Absolute path to project root.
        slug: URL-safe project name. Auto-derived if not given.
        save: Write the warm file to projects/.
        store_pattern: Store the architecture pattern in CortexDB.

    Returns:
        OnboardResult with full analysis.
    """
    root = str(Path(project_path).resolve())
    if not Path(root).is_dir():
        raise ValueError(f"Not a directory: {root}")

    # Derive slug
    if not slug:
        slug = Path(root).name.lower().replace(" ", "-")

    project_name = slug.replace("-", " ").title()

    log.info("Onboarding: %s (%s)", project_name, root)

    # 1. Scan tree
    files = scan_tree(root)
    if not files:
        raise ValueError(f"No source files found in {root}")

    # 2. Detect frameworks
    frameworks = detect_frameworks(root, files)

    # 3. Classify architecture
    architecture = classify_architecture(files, frameworks)

    # 4. Map dependencies
    external_deps, internal_graph, entry_points = map_dependencies(root, files)

    # 5. Identify key files
    key_files = identify_key_files(files)

    # 6. Extract description
    description = extract_description(root)

    # 7. Language breakdown
    lang_counts: Counter[str] = Counter()
    total_lines = 0
    source_files = [f for f in files if Path(f.relative).suffix in SOURCE_EXTENSIONS]
    for f in source_files:
        lang_counts[f.language] += 1
        total_lines += f.lines

    primary_language = lang_counts.most_common(1)[0][0] if lang_counts else "Unknown"

    # 8. Detect high-level patterns
    patterns: list[str] = []
    fw_names = {s.name for s in frameworks}
    if "systemd service" in fw_names:
        patterns.append("systemd integration")
    if "Docker" in fw_names:
        patterns.append("containerized deployment")
    if "Pytest" in fw_names:
        patterns.append("pytest test suite")
    if any("governance" in f.relative or "verify" in f.relative for f in files):
        patterns.append("governance/verification layer")
    if any("trace" in f.relative or "ledger" in f.relative for f in files):
        patterns.append("execution tracing/observability")
    if any("config" in f.relative.lower() for f in files):
        patterns.append("externalized configuration")

    result = OnboardResult(
        project_name=project_name,
        project_path=root,
        slug=slug,
        description=description,
        primary_language=primary_language,
        languages=dict(lang_counts),
        total_files=len(source_files),
        total_lines=total_lines,
        frameworks=frameworks,
        architecture=architecture,
        key_files=key_files,
        entry_points=entry_points,
        external_deps=external_deps,
        internal_graph=internal_graph,
        patterns=patterns,
    )

    # 9. Generate and save warm file
    if save:
        cfg = get_config()
        warm_path = cfg.warm_dir / f"{slug}.md"
        warm_content = generate_warm_file(result)
        warm_path.write_text(warm_content)
        result.warm_file_path = str(warm_path)
        log.info("Warm file written: %s", warm_path)

    # 10. Store architecture pattern in CortexDB
    if store_pattern:
        try:
            from .cortex.engine import Cortex
            db_path = str(get_config().db_path)
            cortex = Cortex(db_path)
            cortex.remember(
                f"Project '{project_name}' at {root}: {architecture}. "
                f"{primary_language} ({len(source_files)} files, {total_lines} LOC). "
                f"Frameworks: {', '.join(s.name for s in frameworks) or 'none'}. "
                f"Patterns: {', '.join(patterns) or 'none'}.",
                type="procedural",
                tags=["architecture", "onboarding", slug],
                importance=0.8,
                emotion="curiosity",
                source="experienced",
                confidence=0.85,
                context=f"onboarding analysis of {slug}",
            )
            cortex.close()
            log.info("Architecture pattern stored in CortexDB")
        except Exception as e:
            log.warning("Could not store pattern in CortexDB: %s", e)

    return result
