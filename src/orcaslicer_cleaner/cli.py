"""CLI entry point for OrcaSlicer Profile Cleaner."""

from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console

from . import loader, reporter
from .cleaner import execute_actions, plan_cleanup, preview_actions
from .deduplicator import find_duplicates
from .models import ProfileCategory
from .validators import validate_all

DEFAULT_PROFILE_DIR = Path.home() / "Library" / "Application Support" / "OrcaSlicer" / "user"

console = Console()
stderr_console = Console(stderr=True)


@click.group()
@click.option(
    "--profile-dir",
    type=click.Path(exists=True, path_type=Path),
    default=DEFAULT_PROFILE_DIR,
    help="Path to OrcaSlicer user profiles directory.",
    show_default=True,
)
@click.pass_context
def cli(ctx: click.Context, profile_dir: Path) -> None:
    """OrcaSlicer Profile Cleaner - validate, deduplicate, and clean up profiles."""
    ctx.ensure_object(dict)
    ctx.obj["profile_dir"] = profile_dir


@cli.command()
@click.option("--json-output", is_flag=True, help="Output as JSON instead of rich tables.")
@click.option(
    "--stale-days",
    type=int,
    default=365,
    help="Days after which a profile is considered stale.",
    show_default=True,
)
@click.option(
    "--name-threshold",
    type=float,
    default=85,
    help="Fuzzy name similarity threshold (0-100).",
    show_default=True,
)
@click.option(
    "--min-severity",
    type=click.Choice(["error", "warning", "info"], case_sensitive=False),
    default="info",
    help="Minimum severity to display.",
    show_default=True,
)
@click.pass_context
def scan(
    ctx: click.Context,
    json_output: bool,
    stale_days: int,
    name_threshold: float,
    min_severity: str,
) -> None:
    """Full scan: validate + deduplicate, report all findings."""
    from .models import IssueSeverity

    profile_dir: Path = ctx.obj["profile_dir"]
    profiles = _load(profile_dir)
    if profiles is None:
        return

    issues = validate_all(profiles, stale_days=stale_days)
    dupe_groups = find_duplicates(profiles, name_threshold=name_threshold)

    # Filter by severity
    severity_level = {"error": 0, "warning": 1, "info": 2}
    min_level = severity_level[min_severity.lower()]
    severity_map = {IssueSeverity.ERROR: 0, IssueSeverity.WARNING: 1, IssueSeverity.INFO: 2}
    filtered_issues = [i for i in issues if severity_map[i.severity] <= min_level]

    if json_output:
        click.echo(reporter.print_json_report(profiles, filtered_issues, dupe_groups))
    else:
        reporter.print_summary(console, profiles, issues, dupe_groups)
        console.print()
        reporter.print_issues(console, filtered_issues)
        console.print()
        reporter.print_duplicates(console, dupe_groups)

    error_count = sum(1 for i in issues if i.severity == IssueSeverity.ERROR)
    if error_count:
        sys.exit(1)


@cli.command()
@click.option(
    "--stale-days",
    type=int,
    default=365,
    help="Days after which a profile is considered stale.",
    show_default=True,
)
@click.pass_context
def validate(ctx: click.Context, stale_days: int) -> None:
    """Run validation checks only (no duplicate detection)."""
    profile_dir: Path = ctx.obj["profile_dir"]
    profiles = _load(profile_dir)
    if profiles is None:
        return

    issues = validate_all(profiles, stale_days=stale_days)

    reporter.print_summary(console, profiles, issues, [])
    console.print()
    reporter.print_issues(console, issues)

    from .models import IssueSeverity

    error_count = sum(1 for i in issues if i.severity == IssueSeverity.ERROR)
    if error_count:
        sys.exit(1)


@cli.command()
@click.option(
    "--name-threshold",
    type=float,
    default=85,
    help="Fuzzy name similarity threshold (0-100).",
    show_default=True,
)
@click.pass_context
def dedupe(ctx: click.Context, name_threshold: float) -> None:
    """Run duplicate detection only."""
    profile_dir: Path = ctx.obj["profile_dir"]
    profiles = _load(profile_dir)
    if profiles is None:
        return

    dupe_groups = find_duplicates(profiles, name_threshold=name_threshold)
    reporter.print_duplicates(console, dupe_groups)


@cli.command()
@click.option("--execute", is_flag=True, help="Actually execute cleanup actions. Without this flag, only previews.")
@click.option(
    "--backup-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Directory to archive removed profiles. Defaults to <profile-dir>/../_backup/<timestamp>.",
)
@click.pass_context
def clean(
    ctx: click.Context,
    execute: bool,
    backup_dir: Path | None,
) -> None:
    """Plan and optionally execute cleanup actions.

    By default previews actions (dry-run). Use --execute to apply changes.
    """
    profile_dir: Path = ctx.obj["profile_dir"]
    profiles = _load(profile_dir)
    if profiles is None:
        return

    issues = validate_all(profiles)
    dupe_groups = find_duplicates(profiles)
    actions = plan_cleanup(issues, dupe_groups)

    if not execute:
        preview_actions(console, actions)
        if actions:
            console.print(
                "\n[dim]Run with --execute to apply these actions. "
                "Files will be backed up before removal.[/dim]"
            )
        return

    if not actions:
        console.print("[green]Nothing to clean.[/green]")
        return

    if backup_dir is None:
        backup_dir = profile_dir.parent / "_backup"

    preview_actions(console, actions)
    console.print()

    if not click.confirm(f"Execute {len(actions)} action(s)? Files will be backed up to {backup_dir}"):
        console.print("[yellow]Aborted.[/yellow]")
        return

    count = execute_actions(console, actions, backup_dir)
    console.print(f"\n[green]Done. {count}/{len(actions)} actions completed.[/green]")


def _load(profile_dir: Path) -> dict[ProfileCategory, list] | None:
    """Load profiles from all user subdirectories, merging them."""
    try:
        roots = loader.discover_profile_dirs(profile_dir)
    except FileNotFoundError as e:
        stderr_console.print(f"[red]Error:[/red] {e}")
        return None

    if not roots:
        stderr_console.print(f"[yellow]No profile directories found in {profile_dir}[/yellow]")
        return None

    merged: dict[ProfileCategory, list] = {cat: [] for cat in ProfileCategory}
    for root in roots:
        profiles = loader.load_profiles(root)
        for cat in ProfileCategory:
            merged[cat].extend(profiles.get(cat, []))

    total = sum(len(ps) for ps in merged.values())
    stderr_console.print(f"[dim]Loaded {total} profiles from {len(roots)} directory(ies)[/dim]")
    return merged
