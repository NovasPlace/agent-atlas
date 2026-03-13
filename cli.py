"""CLI — Click-based entry points for agent-memory-kit.

All subcommands are thin wrappers around the library modules.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time
from importlib.resources import files as pkg_files
from pathlib import Path

import click

from .config import get_config


@click.group()
@click.version_option(package_name="agent-memory-kit")
def main():
    """Agent Memory Kit — Tiered continual memory for AI coding agents."""
    pass


# ── init ──────────────────────────────────────────────────

@main.command()
@click.option("--home", default=None, help="Custom AGENT_MEMORY_HOME path")
def init(home: str | None):
    """Scaffold the memory directory structure with starter files."""
    if home:
        os.environ["AGENT_MEMORY_HOME"] = home
        from .config import reset_config
        reset_config()

    cfg = get_config()
    cfg.ensure_dirs()

    # Write starter hot.md if absent
    if not cfg.hot_file.exists():
        template = _load_template("hot.md")
        cfg.hot_file.write_text(template)
        click.echo(f"  Created {cfg.hot_file}")

    # Write starter archive.md if absent
    if not cfg.archive_file.exists():
        cfg.archive_file.write_text("# Archive — Cold Tier\n\nCompleted projects are archived here.\n")
        click.echo(f"  Created {cfg.archive_file}")

    click.echo(f"\n✓ Memory system initialized at {cfg.home}")
    click.echo(f"  Memory dir:  {cfg.memory_dir}")
    click.echo(f"  Projects:    {cfg.warm_dir}")
    click.echo(f"  Database:    {cfg.db_path}")
    click.echo(f"  Logs:        {cfg.log_dir}")


# ── status ────────────────────────────────────────────────

@main.command()
def status():
    """Show memory system health and usage statistics."""
    from .compact import report_usage, check_budget
    cfg = get_config()

    if not cfg.hot_file.exists():
        click.echo("Memory system not initialized. Run: agent-memory init")
        raise SystemExit(1)

    report_usage()
    check_budget()


# ── compact ───────────────────────────────────────────────

@main.command()
@click.option("--archive", "project_name", default=None, help="Archive a completed project")
@click.option("--dry-run", is_flag=True, help="Show what would happen")
def compact(project_name: str | None, dry_run: bool):
    """Run memory compaction and budget check."""
    from .compact import report_usage, archive_project, check_budget

    report_usage()

    if project_name:
        archive_project(project_name, dry_run=dry_run)

    check_budget()


# ── freshness ─────────────────────────────────────────────

@main.command()
def freshness():
    """Check warm file freshness against project directories."""
    from .freshness import check_freshness, print_report

    findings = check_freshness()
    has_stale = print_report(findings)

    if has_stale:
        raise SystemExit(1)


# ── briefing ──────────────────────────────────────────────

@main.command()
def briefing():
    """Generate a session briefing from CortexDB lessons."""
    from .session_briefing import write_briefing

    path = write_briefing()
    click.echo(f"Session briefing written to {path}")


# ── maintain ──────────────────────────────────────────────

@main.command()
@click.option("--dry-run", is_flag=True, help="Report without making changes")
@click.option("--trace-only", is_flag=True, help="Only prune the trace ledger")
def maintain(dry_run: bool, trace_only: bool):
    """Run cognitive maintenance (consolidation, decay, trace cleanup)."""
    from .maintain import run_full_maintenance, run_trace_cleanup, _print_result

    if trace_only:
        result = run_trace_cleanup(dry_run=dry_run)
        _print_result(result)
    else:
        run_full_maintenance(dry_run=dry_run)


# ── daemon ────────────────────────────────────────────────

@main.command()
@click.option("--dry-run", is_flag=True, help="Log actions without making changes")
def daemon(dry_run: bool):
    """Start the background memory daemon."""
    from .daemon import run_daemon

    asyncio.run(run_daemon(dry_run=dry_run))


# ── directive ─────────────────────────────────────────────

@main.command()
@click.option("--output", "-o", default=None, help="Output file path (default: stdout)")
@click.option("--operator", default=None, help="Operator name to embed")
@click.option("--review-level", default=None, help="Review level (e.g. 'staff-level engineer')")
def directive(output: str | None, operator: str | None, review_level: str | None):
    """Generate a customizable agent directive template."""
    cfg = get_config()
    template = _load_template("directive.md")

    # Apply substitutions
    if operator:
        template = template.replace("{{OPERATOR_NAME}}", operator)
    if review_level:
        template = template.replace("{{REVIEW_LEVEL}}", review_level)
    template = template.replace("{{MEMORY_DIR}}", str(cfg.memory_dir))

    if output:
        Path(output).write_text(template)
        click.echo(f"Directive written to {output}")
    else:
        click.echo(template)


# ── install-service ───────────────────────────────────────

@main.command("install-service")
@click.option("--dry-run", is_flag=True, help="Show what would be installed")
def install_service(dry_run: bool):
    """Install systemd user services for the memory daemon."""
    service_dir = Path.home() / ".config" / "systemd" / "user"

    services = {
        "agent-memory-daemon.service": _load_template("daemon.service"),
        "agent-maintain.service": _load_template("maintain.service"),
        "agent-maintain.timer": _load_template("maintain.timer"),
    }

    # Substitute the agent-memory binary path
    agent_memory_bin = shutil.which("agent-memory") or "agent-memory"

    for name, content in services.items():
        content = content.replace("{{AGENT_MEMORY_BIN}}", agent_memory_bin)
        target = service_dir / name

        if dry_run:
            click.echo(f"  [WOULD INSTALL] {target}")
            continue

        service_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        click.echo(f"  Installed {target}")

    if not dry_run:
        click.echo("\nTo enable:")
        click.echo("  systemctl --user daemon-reload")
        click.echo("  systemctl --user enable --now agent-memory-daemon")
        click.echo("  systemctl --user enable --now agent-maintain.timer")


# ── scan ──────────────────────────────────────────────────

@main.command()
@click.option("--target", default=None, help="Specific directory to scan")
@click.option("--max-age", default=24.0, help="Max file age in hours")
def scan(target: str | None, max_age: float):
    """Run the hallucination scanner on project files."""
    from .hallucination_scanner import run_scan

    summary = run_scan(target=target, max_age_hours=max_age)
    click.echo("\nHallucination Scanner Results:")
    for key, val in summary.items():
        click.echo(f"  {key}: {val}")


# ── Helpers ───────────────────────────────────────────────

def _load_template(name: str) -> str:
    """Load a template file from the package's templates/ directory."""
    template_dir = Path(__file__).parent / "templates"
    template_path = template_dir / name
    if not template_path.exists():
        click.echo(f"Error: template '{name}' not found at {template_path}", err=True)
        raise SystemExit(1)
    return template_path.read_text()
