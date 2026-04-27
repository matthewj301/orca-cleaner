"""CLI entry point for OrcaSlicer Profile Cleaner."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import loader, reporter
from .cleaner import (
    RemapAction,
    audit_links,
    execute_actions,
    execute_link_fixes,
    execute_remap,
    filter_actions_by_printer,
    find_broken_references,
    plan_cleanup,
    preview_actions,
)
from .deduplicator import find_duplicates
from .models import Profile, ProfileCategory
from .standardizer import execute_renames, find_renames, preview_renames
from .system_profiles import load_system_profile_names
from .validators import validate_all

DEFAULT_PROFILE_DIR = Path.home() / "Library" / "Application Support" / "OrcaSlicer" / "user"
DEFAULT_SYSTEM_PROFILES = Path("/Applications/OrcaSlicer.app/Contents/Resources/profiles")

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
@click.option(
    "--system-profiles",
    type=click.Path(path_type=Path),
    default=DEFAULT_SYSTEM_PROFILES,
    help="Path to OrcaSlicer system profiles directory.",
    show_default=True,
)
@click.pass_context
def cli(ctx: click.Context, profile_dir: Path, system_profiles: Path) -> None:
    """OrcaSlicer Profile Cleaner - validate, deduplicate, and clean up profiles."""
    ctx.ensure_object(dict)
    ctx.obj["profile_dir"] = profile_dir

    if system_profiles.is_dir():
        sys_names = load_system_profile_names(system_profiles)
        total = (
            len(sys_names.machine_names)
            + len(sys_names.process_names)
            + len(sys_names.filament_names)
        )
        stderr_console.print(
            f"[dim]Loaded {total} system profile names "
            f"({len(sys_names.inherits_targets)} inherits targets)[/dim]"
        )
        ctx.obj["system_names"] = sys_names
    else:
        stderr_console.print(
            f"[yellow]Warning:[/yellow] System profiles not found at {system_profiles}, "
            "skipping system profile validation",
            highlight=False,
        )
        ctx.obj["system_names"] = None


# ---------------------------------------------------------------------------
# scan — unified read-only analysis
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--json-output", is_flag=True, help="Output as JSON instead of rich tables.")
@click.option(
    "--stale-days", type=int, default=365,
    help="Days after which a profile is considered stale.", show_default=True,
)
@click.option(
    "--min-severity",
    type=click.Choice(["error", "warning", "info"], case_sensitive=False),
    default="info", help="Minimum severity to display.", show_default=True,
)
@click.pass_context
def scan(ctx: click.Context, json_output: bool, stale_days: int, min_severity: str) -> None:
    """Full scan: validation, duplicates, link issues, and naming."""
    from .models import IssueSeverity

    profile_dir: Path = ctx.obj["profile_dir"]
    profiles = _load(profile_dir)
    if profiles is None:
        return

    issues = validate_all(profiles, stale_days=stale_days, system_names=ctx.obj["system_names"])
    dupe_groups = find_duplicates(profiles)

    severity_level = {"error": 0, "warning": 1, "info": 2}
    min_level = severity_level[min_severity.lower()]
    severity_map = {IssueSeverity.ERROR: 0, IssueSeverity.WARNING: 1, IssueSeverity.INFO: 2}
    filtered_issues = [i for i in issues if severity_map[i.severity] <= min_level]

    if json_output:
        click.echo(reporter.print_json_report(profiles, filtered_issues, dupe_groups))
        return

    reporter.print_summary(console, profiles, issues, dupe_groups)
    console.print()
    reporter.print_issues(console, filtered_issues)
    console.print()
    reporter.print_duplicates(console, dupe_groups)

    # Link audit
    link_issues = audit_links(profiles)
    if link_issues:
        console.print()
        empty_links = [i for i in link_issues if i.issue == "empty"]
        mismatch_links = [i for i in link_issues if i.issue == "mismatched"]
        orphaned_links = [i for i in link_issues if i.issue == "orphaned"]

        console.print(Panel(
            f"[bold]{len(link_issues)}[/bold] link issue(s): "
            f"{len(empty_links)} empty, {len(mismatch_links)} mismatched, "
            f"{len(orphaned_links)} orphaned hardware",
            title="Link Audit",
        ))

    # Naming issues
    rename_actions = find_renames(profiles)
    if rename_actions:
        console.print()
        console.print(Panel(
            f"[bold]{len(rename_actions)}[/bold] profile(s) have non-standard names",
            title="Naming",
        ))

    # Hint
    fixable = bool(link_issues or rename_actions or
                   any(i for i in issues if i.issue_type.value in ("broken_reference",)))
    if fixable:
        console.print(f"\n[dim]Run 'ocs fix' to interactively resolve fixable issues.[/dim]")

    error_count = sum(1 for i in issues if i.severity == IssueSeverity.ERROR)
    if error_count:
        sys.exit(1)


# ---------------------------------------------------------------------------
# clean — archive/delete profiles
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--execute", is_flag=True, help="Apply cleanup. Without this flag, only previews.")
@click.option("--backup-dir", type=click.Path(path_type=Path), default=None, help="Directory to archive removed profiles.")
@click.option(
    "--type", "clean_types", multiple=True,
    type=click.Choice(["stale", "invalid", "dupes", "orphaned-hw", "broken-inherits"], case_sensitive=False),
    help="Filter to specific types. Can be repeated. Default: all.",
)
@click.option(
    "--printer", multiple=True,
    help="Only clean profiles for this printer (substring match). Repeatable.",
)
@click.option(
    "--exclude-printer", multiple=True,
    help="Exclude profiles for this printer (substring match). Repeatable.",
)
@click.pass_context
def clean(
    ctx: click.Context, execute: bool, backup_dir: Path | None,
    clean_types: tuple[str, ...], printer: tuple[str, ...], exclude_printer: tuple[str, ...],
) -> None:
    """Archive profiles by type: stale, invalid, dupes, orphaned-hw, broken-inherits."""
    profile_dir: Path = ctx.obj["profile_dir"]
    profiles = _load(profile_dir)
    if profiles is None:
        return

    issues = validate_all(profiles, system_names=ctx.obj["system_names"])
    dupe_groups = find_duplicates(profiles)
    types = clean_types or None

    orphaned_link_issues = None
    if types is None or "orphaned-hw" in types:
        orphaned_link_issues = audit_links(profiles)

    actions = plan_cleanup(issues, dupe_groups, types=types, orphaned_link_issues=orphaned_link_issues)
    actions = filter_actions_by_printer(actions, printer or None, exclude_printer or None)

    if not execute:
        preview_actions(console, actions)
        if actions:
            console.print("\n[dim]Run with --execute to apply these actions.[/dim]")
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


# ---------------------------------------------------------------------------
# fix — interactive fixes (remap, links, standardize)
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--backup-dir", type=click.Path(path_type=Path), default=None, help="Directory for file backups.")
@click.option(
    "--only", "fix_types", multiple=True,
    type=click.Choice(["remap", "links", "names"], case_sensitive=False),
    help="Only run specific fix types. Default: all.",
)
@click.pass_context
def fix(ctx: click.Context, backup_dir: Path | None, fix_types: tuple[str, ...]) -> None:
    """Interactively fix issues: broken refs, mismatched links, and naming.

    Walks through each fixable issue category and lets you review and apply.
    """
    profile_dir: Path = ctx.obj["profile_dir"]
    profiles = _load(profile_dir)
    if profiles is None:
        return

    if backup_dir is None:
        backup_dir = profile_dir.parent / "_backup"

    run_all = not fix_types
    did_something = False

    # --- Phase 1: Broken reference remap ---
    if run_all or "remap" in fix_types:
        did_something |= _fix_remap(profiles, backup_dir)

    # --- Phase 2: Link audit (empty/mismatched compatible_printers) ---
    if run_all or "links" in fix_types:
        did_something |= _fix_links(profiles, backup_dir)

    # --- Phase 3: Name standardization ---
    if run_all or "names" in fix_types:
        did_something |= _fix_names(profiles, backup_dir)

    if not did_something:
        console.print("\n[green]Nothing to fix — all clean![/green]")


def _fix_remap(profiles: dict[ProfileCategory, list], backup_dir: Path) -> bool:
    """Interactive broken reference remap. Returns True if any work was done."""
    broken = find_broken_references(profiles)
    if not broken:
        console.print("[green]No broken printer references.[/green]\n")
        return False

    total_refs = sum(len(ps) for ps in broken.values())
    console.print(Panel(
        f"[bold]{total_refs}[/bold] broken reference(s) across "
        f"[bold]{len(broken)}[/bold] missing printer name(s)",
        title="Broken Printer References", border_style="red",
    ))

    machine_names = sorted(p.name for p in profiles.get(ProfileCategory.MACHINE, []))
    if not machine_names:
        console.print("[yellow]No machine profiles found to remap to.[/yellow]\n")
        return False

    actions: list[RemapAction] = []

    for broken_name, affected in sorted(broken.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        categories = set(p.category.value for p in affected)
        console.print(f"\n  [red]'{broken_name}'[/red] — {len(affected)} {'/'.join(sorted(categories))} profile(s)")

        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column("Key", style="bold cyan", width=4)
        table.add_column("Machine Profile")
        for idx, name in enumerate(machine_names):
            table.add_row(f"{idx + 1})", name)
        table.add_row("r)", "[yellow]Remove from compatible_printers[/yellow]")
        table.add_row("s)", "[dim]Skip[/dim]")
        console.print(table)

        choice = click.prompt("  Choice", type=str, default="s").strip().lower()

        if choice == "s":
            continue
        elif choice == "r":
            actions.append(RemapAction(broken_name=broken_name, affected_profiles=affected, new_name=None))
            console.print(f"  [yellow]Will remove '{broken_name}'[/yellow]")
        else:
            try:
                index = int(choice) - 1
                if not (0 <= index < len(machine_names)):
                    raise ValueError
            except ValueError:
                console.print("  [red]Invalid choice, skipping.[/red]")
                continue
            target = machine_names[index]
            actions.append(RemapAction(broken_name=broken_name, affected_profiles=affected, new_name=target))
            console.print(f"  [green]Will remap -> '{target}'[/green]")

    if not actions:
        console.print()
        return False

    console.print()
    total_affected = sum(len(a.affected_profiles) for a in actions)
    if click.confirm(f"Apply {len(actions)} remap action(s) affecting {total_affected} profile(s)?"):
        modified = execute_remap(console, actions, backup_dir)
        console.print(f"[green]{modified} profile(s) updated.[/green]\n")
        return True
    else:
        console.print("[yellow]Skipped.[/yellow]\n")
        return False


def _fix_links(profiles: dict[ProfileCategory, list], backup_dir: Path) -> bool:
    """Fix empty/mismatched compatible_printers. Returns True if any work was done."""
    link_issues = audit_links(profiles)
    fixable = [i for i in link_issues if i.issue != "orphaned"]

    if not fixable:
        console.print("[green]No link issues to fix.[/green]\n")
        return False

    empty_issues = [i for i in fixable if i.issue == "empty"]
    mismatch_issues = [i for i in fixable if i.issue == "mismatched"]

    console.print(Panel(
        f"[bold]{len(fixable)}[/bold] fixable link issue(s): "
        f"{len(empty_issues)} empty, {len(mismatch_issues)} mismatched",
        title="Link Issues", border_style="yellow",
    ))

    if empty_issues:
        table = Table(title="Empty compatible_printers (visible to all printers)")
        table.add_column("Category", width=10)
        table.add_column("Profile", max_width=55)
        table.add_column("Suggested Printers", max_width=55, style="green")
        for issue in sorted(empty_issues, key=lambda i: (i.profile.category.value, i.profile.name)):
            table.add_row(
                issue.profile.category.value,
                issue.profile.name,
                ", ".join(issue.suggested_printers),
            )
        console.print(table)
        console.print()

    if mismatch_issues:
        table = Table(title="Mismatched compatible_printers")
        table.add_column("Category", width=10)
        table.add_column("Profile", max_width=45)
        table.add_column("Issue", max_width=50)
        table.add_column("Suggested", max_width=40, style="green")
        for issue in sorted(mismatch_issues, key=lambda i: (i.profile.category.value, i.profile.name)):
            table.add_row(
                issue.profile.category.value,
                issue.profile.name,
                issue.details,
                ", ".join(issue.suggested_printers),
            )
        console.print(table)
        console.print()

    fixes = [(issue.profile, issue.suggested_printers) for issue in fixable]

    if click.confirm(f"Update compatible_printers for {len(fixes)} profile(s)?"):
        count = execute_link_fixes(console, fixes, backup_dir)
        console.print(f"[green]{count}/{len(fixes)} profile(s) updated.[/green]\n")
        return True
    else:
        console.print("[yellow]Skipped.[/yellow]\n")
        return False


def _fix_names(profiles: dict[ProfileCategory, list], backup_dir: Path) -> bool:
    """Fix naming inconsistencies. Returns True if any work was done."""
    rename_actions = find_renames(profiles)

    if not rename_actions:
        console.print("[green]All profile names are standardized.[/green]\n")
        return False

    console.print(Panel(
        f"[bold]{len(rename_actions)}[/bold] profile(s) have non-standard names",
        title="Name Standardization", border_style="yellow",
    ))

    preview_renames(console, rename_actions)
    console.print()

    if click.confirm(f"Rename {len(rename_actions)} profile(s)?"):
        count = execute_renames(console, rename_actions, backup_dir)
        console.print(f"[green]{count}/{len(rename_actions)} profile(s) renamed.[/green]\n")
        return True
    else:
        console.print("[yellow]Skipped.[/yellow]\n")
        return False


# ---------------------------------------------------------------------------
# diff — compare two profiles
# ---------------------------------------------------------------------------


@cli.command(name="diff")
@click.argument("profile_a")
@click.argument("profile_b")
@click.option("--category", type=click.Choice(["filament", "machine", "process"], case_sensitive=False), default=None, help="Profile category to search in.")
@click.option("--show-common", is_flag=True, help="Also show settings that are identical.")
@click.pass_context
def diff_cmd(ctx: click.Context, profile_a: str, profile_b: str, category: str | None, show_common: bool) -> None:
    """Show a side-by-side diff of two profiles."""
    from rapidfuzz import fuzz, process as rfprocess

    profile_dir: Path = ctx.obj["profile_dir"]
    profiles = _load(profile_dir)
    if profiles is None:
        return

    cats = [ProfileCategory(category.lower())] if category else list(ProfileCategory)
    candidates: list[Profile] = []
    for cat in cats:
        candidates.extend(profiles.get(cat, []))

    if not candidates:
        stderr_console.print("[red]No profiles found in the selected category.[/red]")
        sys.exit(1)

    def resolve_profile(name: str) -> Profile | None:
        exact = [p for p in candidates if p.name == name]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            stderr_console.print(f"[yellow]Multiple profiles named '{name}'. Use --category.[/yellow]")
            return None
        candidate_names = [p.name for p in candidates]
        matches = rfprocess.extract(name, candidate_names, scorer=fuzz.ratio, limit=3)
        if not matches or matches[0][1] < 50:
            stderr_console.print(f"[red]No close match for '{name}'.[/red]")
            return None
        best_name, best_score, _ = matches[0]
        if not click.confirm(f"'{name}' not found. Did you mean '{best_name}' ({best_score:.0f}% match)?"):
            return None
        return next((p for p in candidates if p.name == best_name), None)

    pa = resolve_profile(profile_a)
    pb = resolve_profile(profile_b) if pa else None
    if pa is None or pb is None:
        sys.exit(1)

    sa, sb = pa.settings_without_metadata(), pb.settings_without_metadata()
    all_keys = sorted(set(sa) | set(sb))

    differ, only_a, only_b, common = [], [], [], []
    for key in all_keys:
        if key in sa and key in sb:
            va, vb = _format_value(sa[key]), _format_value(sb[key])
            if sa[key] == sb[key]:
                common.append((key, va))
            else:
                differ.append((key, va, vb))
        elif key in sa:
            only_a.append((key, _format_value(sa[key])))
        else:
            only_b.append((key, _format_value(sb[key])))

    table = Table(title=f"Diff: [bold]{pa.name}[/bold] vs [bold]{pb.name}[/bold]", show_lines=True)
    table.add_column("Setting", max_width=40)
    table.add_column(pa.name, max_width=50)
    table.add_column(pb.name, max_width=50)

    for key, va, vb in differ:
        table.add_row(Text(key, style="yellow"), Text(va, style="yellow"), Text(vb, style="yellow"))
    for key, val in only_a:
        table.add_row(Text(key, style="red"), Text(val, style="red"), Text("--", style="dim"))
    for key, val in only_b:
        table.add_row(Text(key, style="green"), Text("--", style="dim"), Text(val, style="green"))
    if show_common:
        for key, val in common:
            table.add_row(Text(key, style="dim"), Text(val, style="dim"), Text(val, style="dim"))

    console.print(table)
    console.print(f"\n[bold]{len(differ)}[/bold] differ, [red]{len(only_a)}[/red] only in A, [green]{len(only_b)}[/green] only in B, [dim]{len(common)}[/dim] in common")


# ---------------------------------------------------------------------------
# restore — restore from backup
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("timestamp", required=False, default=None)
@click.option("--profile", "profile_name", default=None, help="Restore a single profile by name.")
@click.option("--force", is_flag=True, help="Skip confirmation prompt.")
@click.pass_context
def restore(ctx: click.Context, timestamp: str | None, profile_name: str | None, force: bool) -> None:
    """Restore profiles from a backup."""
    profile_dir: Path = ctx.obj["profile_dir"]
    backup_root = profile_dir.parent / "_backup"

    if not backup_root.is_dir():
        stderr_console.print(f"[yellow]No backup directory found at {backup_root}[/yellow]")
        return

    backup_dirs = sorted([d for d in backup_root.iterdir() if d.is_dir() and not d.name.startswith(".")], reverse=True)
    if not backup_dirs:
        stderr_console.print("[yellow]No backups found.[/yellow]")
        return

    if timestamp is None:
        table = Table(title="Available Backups")
        table.add_column("Timestamp", width=20)
        table.add_column("Files", justify="right", width=8)
        table.add_column("Categories", max_width=40)
        for bdir in backup_dirs:
            files = list(bdir.rglob("*.*"))
            cats = sorted({f.parent.name for f in files if f.parent != bdir})
            table.add_row(bdir.name, str(len(files)), ", ".join(cats) if cats else "--")
        console.print(table)
        console.print("\n[dim]Use 'ocs restore <timestamp>' to restore a backup.[/dim]")
        return

    target_backup = backup_root / timestamp
    if not target_backup.is_dir():
        matches = [d for d in backup_dirs if d.name.startswith(timestamp)]
        if len(matches) == 1:
            target_backup = matches[0]
        elif len(matches) > 1:
            stderr_console.print(f"[yellow]Ambiguous timestamp '{timestamp}'.[/yellow]")
            for m in matches:
                stderr_console.print(f"  {m.name}")
            return
        else:
            stderr_console.print(f"[red]No backup found for '{timestamp}'.[/red]")
            return

    try:
        user_dirs = loader.discover_profile_dirs(profile_dir)
    except FileNotFoundError:
        stderr_console.print(f"[red]Profile directory not found: {profile_dir}[/red]")
        return
    if not user_dirs:
        stderr_console.print(f"[yellow]No user directories found in {profile_dir}[/yellow]")
        return

    restore_root = user_dirs[0]

    restore_files: list[tuple[Path, Path]] = []
    for category_dir in sorted(target_backup.iterdir()):
        if not category_dir.is_dir():
            continue
        dest_category = restore_root / category_dir.name
        for src_file in sorted(category_dir.iterdir()):
            if src_file.name.startswith(".") or src_file.suffix not in (".info", ".json"):
                continue
            if profile_name is not None and src_file.stem != profile_name:
                continue
            restore_files.append((src_file, dest_category / src_file.name))

    if not restore_files:
        stderr_console.print(f"[yellow]No files to restore from {target_backup.name}.[/yellow]")
        return

    table = Table(title=f"Restore from {target_backup.name}")
    table.add_column("File", max_width=60)
    table.add_column("Category", width=10)
    table.add_column("Status", width=20)
    conflicts = 0
    for src, dst in restore_files:
        if dst.exists():
            status = "[yellow]exists (will overwrite)[/yellow]"
            conflicts += 1
        else:
            status = "[green]new[/green]"
        table.add_row(src.name, src.parent.name, status)
    console.print(table)

    if conflicts:
        console.print(f"\n[yellow]{conflicts} file(s) will be overwritten.[/yellow]")
    if not force and not click.confirm(f"Restore {len(restore_files)} file(s) to {restore_root}?"):
        console.print("[yellow]Aborted.[/yellow]")
        return

    restored = 0
    for src, dst in restore_files:
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
            console.print(f"  [green]Restored[/green] {src.name}")
            restored += 1
        except Exception as e:
            console.print(f"  [red]Failed[/red] {src.name}: {e}")
    console.print(f"\n[green]Done. {restored}/{len(restore_files)} file(s) restored.[/green]")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_value(value: object) -> str:
    """Format a settings value for display, truncating long values."""
    if isinstance(value, list):
        if len(value) > 5:
            return str(value[:5])[:-1] + f", ... ({len(value)} items)]"
        return str(value)
    if isinstance(value, str) and len(value) > 80:
        return value[:77] + "..."
    return str(value)


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
