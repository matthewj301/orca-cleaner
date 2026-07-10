"""CLI entry point for OrcaSlicer Profile Cleaner."""

from __future__ import annotations

import re
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
    CleanAction,
    DupeResolution,
    RemapAction,
    audit_links,
    execute_actions,
    execute_dupe_resolutions,
    execute_link_fixes,
    execute_printer_removal,
    execute_remap,
    filter_actions_by_printer,
    find_broken_references,
    find_printer_dependents,
    find_unassigned,
    plan_cleanup,
    preview_actions,
)
from .fileops import (
    MANIFEST_NAME,
    backup_copy,
    create_backup_dir,
    load_manifest,
    load_operation,
    load_renames,
)
from .deduplicator import find_duplicates, recommend_keep
from .models import Profile, ProfileCategory
from .safety import assess_blast_radius, coverage_lost, coverage_snapshot, new_broken_refs
from .standardizer import execute_renames, find_renames, preview_renames
from .system_profiles import load_system_profile_names
from .validators import validate_all

DEFAULT_PROFILE_DIR = Path.home() / "Library" / "Application Support" / "OrcaSlicer" / "user"
DEFAULT_SYSTEM_PROFILES = Path("/Applications/OrcaSlicer.app/Contents/Resources/profiles")

console = Console()
stderr_console = Console(stderr=True)


def _confirm_with_blast_radius(assessment, prompt_text: str) -> bool:
    """Confirm an operation, escalating to a typed 'yes' when the blast
    radius assessment carries warnings (e.g. coverage-affecting archives)."""
    if not assessment.warnings:
        return click.confirm(prompt_text)

    for warning in assessment.warnings:
        console.print(Panel(warning, title="Blast Radius Warning", border_style="red"))

    response = click.prompt("Type 'yes' to proceed", default="no")
    return response.strip().lower() == "yes"


def _post_mutation_report(profile_dir: Path, before_profiles, before_snapshot) -> None:
    """Reload profiles after a mutation and report any coverage lost or new
    broken references, so silent damage doesn't go unnoticed."""
    after_profiles = _load(profile_dir)
    if after_profiles is None:
        console.print("[yellow]Warning: could not reload profiles for post-operation check.[/yellow]")
        return

    lines = coverage_lost(before_snapshot, coverage_snapshot(after_profiles))
    lines += new_broken_refs(before_profiles, after_profiles)

    if lines:
        body = "\n".join(lines) + "\n\n[dim]Undo with: ocs undo[/dim]"
        console.print(Panel(body, title="Post-operation check", border_style="red"))
    else:
        console.print("[dim]Post-operation check: no coverage lost, no new broken references.[/dim]")


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

        if empty_links:
            from rich.table import Table as RichTable
            table = RichTable(title="Visible to ALL printers (empty compatible_printers)")
            table.add_column("Profile", max_width=60)
            table.add_column("Suggested Printer(s)", style="green")
            for link in sorted(empty_links, key=lambda l: l.profile.name):
                table.add_row(
                    link.profile.name,
                    ", ".join(link.suggested_printers) if link.suggested_printers else "?",
                )
            console.print(table)

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

    to_archive = [a.profile for a in actions]
    assessment = assess_blast_radius(profiles, to_archive)
    snapshot = coverage_snapshot(profiles)

    if not _confirm_with_blast_radius(
        assessment, f"Execute {len(actions)} action(s)? Files will be backed up to {backup_dir}"
    ):
        console.print("[yellow]Aborted.[/yellow]")
        return

    count = execute_actions(console, actions, backup_dir)
    console.print(f"\n[green]Done. {count}/{len(actions)} actions completed.[/green]")
    _post_mutation_report(profile_dir, profiles, snapshot)


# ---------------------------------------------------------------------------
# remove-printer — delete a machine and its dependent profiles
# ---------------------------------------------------------------------------


@cli.command("remove-printer")
@click.option("--backup-dir", type=click.Path(path_type=Path), default=None, help="Directory to archive removed profiles.")
@click.pass_context
def remove_printer(ctx: click.Context, backup_dir: Path | None) -> None:
    """Remove a printer and archive all filament/process profiles exclusively linked to it."""
    profile_dir: Path = ctx.obj["profile_dir"]
    profiles = _load(profile_dir)
    if profiles is None:
        return

    if backup_dir is None:
        backup_dir = profile_dir.parent / "_backup"

    machines = sorted(profiles.get(ProfileCategory.MACHINE, []), key=lambda p: p.name)
    if not machines:
        console.print("[yellow]No machine profiles found.[/yellow]")
        return

    from rich.table import Table as RichTable
    table = RichTable(title="Machine Profiles")
    table.add_column("#", style="bold cyan", width=4)
    table.add_column("Machine Name")
    for idx, m in enumerate(machines, 1):
        table.add_row(str(idx), m.name)
    console.print(table)
    console.print()

    choice = click.prompt("Select printer to remove (number, or 'q' to quit)", type=str, default="q")
    if choice.lower() == "q":
        return
    try:
        index = int(choice) - 1
        if not (0 <= index < len(machines)):
            raise ValueError
    except ValueError:
        console.print("[red]Invalid selection.[/red]")
        return

    target_machine = machines[index]
    machine_name = target_machine.name

    exclusive, shared = find_printer_dependents(profiles, machine_name)

    console.print(f"\n[bold]Removing:[/bold] {machine_name}")
    console.print(f"  [red]{len(exclusive)}[/red] exclusive profile(s) will be archived (only linked to this printer)")
    if shared:
        console.print(f"  [yellow]{len(shared)}[/yellow] shared profile(s) will have this printer removed from compatible_printers")
    console.print()

    if exclusive:
        console.print("[bold]Will archive:[/bold]")
        for p in sorted(exclusive, key=lambda x: (x.category.value, x.name)):
            console.print(f"  \\[{p.category.value}] {p.name}")
        console.print()

    to_archive = [target_machine] + exclusive
    assessment = assess_blast_radius(profiles, to_archive, to_modify=shared)
    snapshot = coverage_snapshot(profiles)

    if not _confirm_with_blast_radius(
        assessment,
        f"Proceed? Machine + {len(exclusive)} exclusive profiles archived, {len(shared)} shared profiles updated",
    ):
        console.print("[yellow]Aborted.[/yellow]")
        return

    total = execute_printer_removal(console, target_machine, exclusive, shared, backup_dir)
    console.print(f"\n[green]Done. {total} profile(s) processed.[/green]")
    _post_mutation_report(profile_dir, profiles, snapshot)


# ---------------------------------------------------------------------------
# fix — interactive fixes (remap, links, standardize)
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--backup-dir", type=click.Path(path_type=Path), default=None, help="Directory for file backups.")
@click.option(
    "--only", "fix_types", multiple=True,
    type=click.Choice(["remap", "links", "dupes", "names"], case_sensitive=False),
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

    def _reload() -> None:
        # Earlier phases mutate files on disk; reload so later phases
        # don't act on stale in-memory state.
        nonlocal profiles
        reloaded = _load(profile_dir)
        if reloaded is not None:
            profiles = reloaded
        else:
            console.print(
                "[yellow]Warning: could not reload profiles; later fix phases "
                "may act on stale data.[/yellow]"
            )

    # --- Phase 1: Broken reference remap ---
    if run_all or "remap" in fix_types:
        if _fix_remap(profiles, backup_dir, profile_dir):
            did_something = True
            _reload()

    # --- Phase 2: Link audit (empty/mismatched compatible_printers) ---
    if run_all or "links" in fix_types:
        if _fix_links(profiles, backup_dir, profile_dir):
            did_something = True
            _reload()

    # --- Phase 3: Duplicate resolution ---
    if run_all or "dupes" in fix_types:
        if _fix_dupes(profiles, backup_dir, profile_dir):
            did_something = True
            _reload()

    # --- Phase 4: Name standardization ---
    if run_all or "names" in fix_types:
        did_something |= _fix_names(profiles, backup_dir, profile_dir)

    if not did_something:
        console.print("\n[green]Nothing to fix — all clean![/green]")


def _fix_remap(profiles: dict[ProfileCategory, list], backup_dir: Path, profile_dir: Path) -> bool:
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
    to_modify = [p for a in actions for p in a.affected_profiles]
    assessment = assess_blast_radius(profiles, [], to_modify=to_modify)
    snapshot = coverage_snapshot(profiles)
    if _confirm_with_blast_radius(
        assessment, f"Apply {len(actions)} remap action(s) affecting {total_affected} profile(s)?"
    ):
        modified = execute_remap(console, actions, backup_dir)
        console.print(f"[green]{modified} profile(s) updated.[/green]\n")
        _post_mutation_report(profile_dir, profiles, snapshot)
        return True
    else:
        console.print("[yellow]Skipped.[/yellow]\n")
        return False


def _fix_links(profiles: dict[ProfileCategory, list], backup_dir: Path, profile_dir: Path) -> bool:
    """Fix empty/mismatched compatible_printers, then interactively assign
    printers to profiles with empty compatible_printers and no hardware hint
    in their name. Returns True if either phase did work."""
    did_something = _fix_links_known(profiles, backup_dir, profile_dir)

    # Reload so the unassigned-profile phase doesn't act on stale data if
    # the known-issue phase above just mutated files.
    if did_something:
        reloaded = _load(profile_dir)
        if reloaded is not None:
            profiles = reloaded

    if _fix_links_unassigned(profiles, backup_dir, profile_dir):
        did_something = True

    return did_something


def _fix_links_known(profiles: dict[ProfileCategory, list], backup_dir: Path, profile_dir: Path) -> bool:
    """Fix empty/mismatched compatible_printers where a hardware hint in the
    profile name lets audit_links determine the fix automatically. Returns
    True if any work was done."""
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

    to_modify = [issue.profile for issue in fixable]
    assessment = assess_blast_radius(profiles, [], to_modify=to_modify)
    snapshot = coverage_snapshot(profiles)
    if _confirm_with_blast_radius(assessment, f"Update compatible_printers for {len(fixes)} profile(s)?"):
        count = execute_link_fixes(console, fixes, backup_dir)
        console.print(f"[green]{count}/{len(fixes)} profile(s) updated.[/green]\n")
        _post_mutation_report(profile_dir, profiles, snapshot)
        return True
    else:
        console.print("[yellow]Skipped.[/yellow]\n")
        return False


# Above this count, unassigned profiles are grouped by category (process
# first) and a count is printed, so a big backlog doesn't scroll off-screen
# before the user gets any orientation.
_UNASSIGNED_GROUP_THRESHOLD = 15


def _fix_links_unassigned(profiles: dict[ProfileCategory, list], backup_dir: Path, profile_dir: Path) -> bool:
    """Interactively assign printers to profiles with empty compatible_printers
    and no hardware hint in their name (audit_links can't fix these on its
    own). Returns True if any work was done."""
    unassigned = find_unassigned(profiles)
    if not unassigned:
        console.print("[green]No unassigned profiles to review.[/green]\n")
        return False

    machines = sorted(profiles.get(ProfileCategory.MACHINE, []), key=lambda p: p.name)
    if not machines:
        console.print("[yellow]No machine profiles found to assign to.[/yellow]\n")
        return False
    machine_names = [m.name for m in machines]

    console.print(Panel(
        f"[bold]{len(unassigned)}[/bold] profile(s) have empty compatible_printers "
        "and no hardware hint in their name — these are invisible to the usual "
        "link audit and need manual assignment.",
        title="Unassigned Profiles", border_style="yellow",
    ))

    if len(unassigned) > _UNASSIGNED_GROUP_THRESHOLD:
        console.print(
            f"[dim]{len(unassigned)} profiles to review — grouped by category "
            "(process first), one at a time.[/dim]\n"
        )
        order = {ProfileCategory.PROCESS: 0, ProfileCategory.FILAMENT: 1, ProfileCategory.MACHINE: 2}
        unassigned = sorted(unassigned, key=lambda u: (order.get(u.profile.category, 9), u.profile.name))

    assignments: list[tuple[Profile, list[str]]] = []
    archives: list[Profile] = []
    stopped_early = False

    for idx, item in enumerate(unassigned, 1):
        profile = item.profile
        console.print(
            f"\n[bold]{idx}/{len(unassigned)}[/bold] "
            f"\\[{profile.category.value}] {profile.name}"
        )
        if profile.inherits:
            console.print(f"  [dim]inherits: {profile.inherits}[/dim]")

        suggested = set(item.suggested_printers)
        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column("Key", style="bold cyan", width=4)
        table.add_column("Machine Profile")
        table.add_column("")
        for midx, name in enumerate(machine_names):
            tag = "[green]suggested[/green]" if name in suggested else ""
            table.add_row(f"{midx + 1})", name, tag)
        table.add_row("a)", "[yellow]Archive (belongs to no current printer)[/yellow]", "")
        table.add_row("s)", "[dim]Skip[/dim]", "")
        table.add_row("q)", "[dim]Stop processing[/dim]", "")
        console.print(table)

        choice = click.prompt("  Choice", type=str, default="s").strip().lower()

        if choice == "q":
            stopped_early = True
            break
        if choice == "s":
            continue
        if choice == "a":
            archives.append(profile)
            console.print(f"  [yellow]Will archive[/yellow] '{profile.name}'")
            continue

        try:
            indices = [int(tok.strip()) - 1 for tok in choice.split(",") if tok.strip()]
            if not indices or any(not (0 <= i < len(machine_names)) for i in indices):
                raise ValueError
        except ValueError:
            console.print("  [red]Invalid choice, skipping.[/red]")
            continue

        chosen = [machine_names[i] for i in indices]
        assignments.append((profile, chosen))
        console.print(f"  [green]Will assign -> {', '.join(chosen)}[/green]")

    if not assignments and not archives:
        console.print()
        return False

    console.print()
    summary = (
        f"{len(assignments)} profile(s) assigned, {len(archives)} profile(s) archived"
    )
    if stopped_early:
        summary += " (stopped early — remaining profiles skipped)"

    to_modify = [p for p, _ in assignments]
    assessment = assess_blast_radius(profiles, to_archive=archives, to_modify=to_modify)
    snapshot = coverage_snapshot(profiles)
    if _confirm_with_blast_radius(assessment, f"Apply changes? {summary}"):
        updated = execute_link_fixes(console, assignments, backup_dir) if assignments else 0
        archived = 0
        if archives:
            actions = [CleanAction(action="archive", profile=p, reason="No current printer match") for p in archives]
            archived = execute_actions(console, actions, backup_dir)
        console.print(
            f"[green]{updated}/{len(assignments)} assigned, "
            f"{archived}/{len(archives)} archived.[/green]\n"
        )
        _post_mutation_report(profile_dir, profiles, snapshot)
        return True
    else:
        console.print("[yellow]Skipped.[/yellow]\n")
        return False


def _fmt_updated(profile: Profile) -> str:
    """Format a profile's updated_time as an ISO date, or '?' if unset."""
    ts = profile.info.updated_time if profile.info else 0
    if not ts:
        return "?"
    try:
        import datetime

        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return "?"


def _fmt_printers(printers: list[str], max_len: int = 60) -> str:
    joined = ", ".join(printers) if printers else "[dim](empty — all printers)[/dim]"
    if len(joined) > max_len:
        return joined[: max_len - 3] + "..."
    return joined


def _fix_dupes(profiles: dict[ProfileCategory, list], backup_dir: Path, profile_dir: Path) -> bool:
    """Interactively resolve duplicate/near-duplicate groups. Returns True if
    any work was done."""
    groups = find_duplicates(profiles)
    if not groups:
        console.print("[green]No duplicate profiles found.[/green]\n")
        return False

    machine_groups = [g for g in groups if g.profiles and g.profiles[0].category == ProfileCategory.MACHINE]
    machine_group_ids = {id(g) for g in machine_groups}
    other_groups = [g for g in groups if id(g) not in machine_group_ids]

    if machine_groups:
        console.print(
            f"[dim]{len(machine_groups)} machine duplicate group(s) found — machine "
            "profiles are referenced by name from other profiles, so they must be "
            "removed via 'ocs remove-printer' instead of 'ocs fix'.[/dim]\n"
        )

    if not other_groups:
        console.print("[green]No non-machine duplicate profiles to resolve.[/green]\n")
        return False

    console.print(Panel(
        f"[bold]{len(other_groups)}[/bold] duplicate group(s) to review",
        title="Duplicate Resolution", border_style="cyan",
    ))

    resolutions: list[DupeResolution] = []
    stopped_early = False

    for gi, group in enumerate(other_groups, 1):
        recommended = recommend_keep(group)
        console.print(
            f"\n[bold]Group {gi}/{len(other_groups)}[/bold] "
            f"([cyan]{group.match_type}[/cyan]) — {group.details}"
        )

        table = Table(show_header=True, box=None, padding=(0, 1))
        table.add_column("#", style="bold cyan", width=3)
        table.add_column("Profile", max_width=45)
        table.add_column("Category", width=10)
        table.add_column("Updated", width=10)
        table.add_column("Compatible Printers", max_width=45)
        table.add_column("", width=12)
        for idx, p in enumerate(group.profiles, 1):
            mark = "[bold green]recommended[/bold green]" if p is recommended else ""
            table.add_row(
                str(idx), p.name, p.category.value, _fmt_updated(p),
                _fmt_printers(p.compatible_printers), mark,
            )
        console.print(table)

        if group.match_type not in ("exact_content", "mergeable"):
            diffs: list[tuple[str, list[str]]] = []
            settings_list = [p.settings_without_metadata() for p in group.profiles]
            all_keys = sorted(set().union(*(s.keys() for s in settings_list)))
            for key in all_keys:
                values = [s.get(key, "<missing>") for s in settings_list]
                if any(v != values[0] for v in values):
                    diffs.append((key, [_format_value(v) if v != "<missing>" else v for v in values]))

            if diffs:
                diff_table = Table(title="Differing settings", show_header=True, box=None, padding=(0, 1))
                diff_table.add_column("Setting", style="yellow", max_width=30)
                for idx in range(len(group.profiles)):
                    diff_table.add_column(f"#{idx + 1}", max_width=30)
                for key, values in diffs[:10]:
                    diff_table.add_row(key, *values)
                console.print(diff_table)
                if len(diffs) > 10:
                    console.print(f"  [dim]... and {len(diffs) - 10} more[/dim]")

        choice = click.prompt(
            "  Keep which # (or 's' to skip, 'q' to stop)", type=str, default="s"
        ).strip().lower()

        if choice == "q":
            stopped_early = True
            break
        if choice == "s":
            continue

        try:
            index = int(choice) - 1
            if not (0 <= index < len(group.profiles)):
                raise ValueError
        except ValueError:
            console.print("  [red]Invalid choice, skipping.[/red]")
            continue

        keep = group.profiles[index]
        losers = [p for p in group.profiles if p is not keep]

        merged_printers: list[str] | None = None
        if group.match_type == "mergeable":
            union: set[str] = set()
            for p in group.profiles:
                union.update(p.compatible_printers)
            merged_printers = sorted(union)

        resolutions.append(DupeResolution(keep=keep, archive=losers, merged_printers=merged_printers))
        console.print(f"  [green]Will keep[/green] '{keep.name}', archive {len(losers)} other(s)")

    if not resolutions:
        console.print()
        return False

    console.print()
    total_archived = sum(len(r.archive) for r in resolutions)
    total_merged = sum(1 for r in resolutions if r.merged_printers)
    summary = (
        f"{len(resolutions)} group(s): {total_archived} profile(s) archived, "
        f"{total_merged} merged"
    )
    if stopped_early:
        summary += " (stopped early — remaining groups skipped)"

    to_archive = [p for r in resolutions for p in r.archive]
    to_modify = [r.keep for r in resolutions if r.merged_printers]
    assessment = assess_blast_radius(profiles, to_archive, to_modify=to_modify)
    snapshot = coverage_snapshot(profiles)
    if _confirm_with_blast_radius(assessment, f"Apply resolutions? {summary}"):
        count = execute_dupe_resolutions(console, resolutions, backup_dir)
        console.print(f"[green]{count}/{len(resolutions)} group(s) resolved.[/green]\n")
        _post_mutation_report(profile_dir, profiles, snapshot)
        return True
    else:
        console.print("[yellow]Skipped.[/yellow]\n")
        return False


def _fix_names(profiles: dict[ProfileCategory, list], backup_dir: Path, profile_dir: Path) -> bool:
    """Fix naming inconsistencies. Returns True if any work was done."""
    rename_actions = find_renames(profiles)

    if not rename_actions:
        console.print("[green]All profile names are standardized.[/green]\n")
        return False

    machine_renames = [a for a in rename_actions if a.profile.category == ProfileCategory.MACHINE]
    other_renames = [a for a in rename_actions if a.profile.category != ProfileCategory.MACHINE]
    if machine_renames and other_renames:
        console.print(
            "[yellow]Note:[/yellow] Machine profiles will be renamed first and "
            "compatible_printers references will be updated automatically.\n"
        )

    console.print(Panel(
        f"[bold]{len(rename_actions)}[/bold] profile(s) have non-standard names",
        title="Name Standardization", border_style="yellow",
    ))

    preview_renames(console, rename_actions)
    console.print()

    to_modify = [a.profile for a in rename_actions]
    assessment = assess_blast_radius(profiles, [], to_modify=to_modify)
    snapshot = coverage_snapshot(profiles)
    if _confirm_with_blast_radius(assessment, f"Rename {len(rename_actions)} profile(s)?"):
        count = execute_renames(console, rename_actions, backup_dir, all_profiles=profiles)
        console.print(f"[green]{count}/{len(rename_actions)} profile(s) renamed.[/green]\n")
        # Coverage is tracked by profile name, so an applied rename would
        # read as a "loss" in the post-op diff. Translate the before-snapshot
        # to post-rename names (only for renames that actually landed).
        for action in rename_actions:
            new_json = action.profile.directory / f"{action.new_name}.json"
            old_json = action.profile.directory / f"{action.old_name}.json"
            if not new_json.exists() or old_json.exists():
                continue
            old_key = f"{action.profile.category.value}:{action.old_name}"
            new_key = f"{action.profile.category.value}:{action.new_name}"
            for entries in snapshot.values():
                if old_key in entries:
                    entries.discard(old_key)
                    entries.add(new_key)
        _post_mutation_report(profile_dir, profiles, snapshot)
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
        table.add_column("Operation", width=18)
        table.add_column("Files", justify="right", width=8)
        table.add_column("Categories", max_width=40)
        for bdir in backup_dirs:
            files = [f for f in bdir.rglob("*.*") if f.name != MANIFEST_NAME]
            cats = sorted({f.parent.name for f in files if f.parent != bdir})
            table.add_row(
                bdir.name,
                load_operation(bdir) or "--",
                str(len(files)),
                ", ".join(cats) if cats else "--",
            )
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

    # Backups record where each file came from in a manifest; use it so files
    # go back to their original location (correct user root, original name
    # even for collision-suffixed copies). Older backups without a manifest
    # fall back to the first user root.
    manifest = load_manifest(target_backup)

    restore_files: list[tuple[Path, Path]] = []
    for category_dir in sorted(target_backup.iterdir()):
        if not category_dir.is_dir():
            continue
        dest_category = restore_root / category_dir.name
        for src_file in sorted(category_dir.iterdir()):
            if src_file.name.startswith(".") or src_file.suffix not in (".info", ".json"):
                continue
            original = manifest.get(f"{category_dir.name}/{src_file.name}")
            if original:
                dst = Path(original)
                # Guard against stale manifest paths (moved/relinked profile
                # dir): don't silently recreate a tree OrcaSlicer won't read.
                if not dst.parent.parent.is_dir():
                    stderr_console.print(
                        f"[yellow]Original location {dst.parent.parent} no longer "
                        f"exists; restoring {dst.name} to {restore_root} instead.[/yellow]"
                    )
                    dst = dest_category / dst.name
            else:
                dst = dest_category / src_file.name
            if profile_name is not None and dst.stem != profile_name:
                continue
            restore_files.append((src_file, dst))

    if not restore_files:
        stderr_console.print(f"[yellow]No files to restore from {target_backup.name}.[/yellow]")
        return

    # A backup dir can hold collision-suffixed duplicates of the same
    # destination (Name.json plus Name_1.json). The unsuffixed copy is the
    # earliest — the true pre-mutation original — so restore that one.
    def _copy_index(src: Path, dst: Path) -> int:
        if src.stem == dst.stem:
            return 0
        m = re.match(re.escape(dst.stem) + r"_(\d+)$", src.stem)
        return int(m.group(1)) if m else 0

    by_dst: dict[Path, list[Path]] = {}
    for src, dst in restore_files:
        by_dst.setdefault(dst, []).append(src)
    skipped_dupes = sum(len(srcs) - 1 for srcs in by_dst.values())
    restore_files = sorted(
        ((min(srcs, key=lambda s: _copy_index(s, dst)), dst) for dst, srcs in by_dst.items()),
        key=lambda pair: str(pair[1]),
    )
    if skipped_dupes:
        console.print(
            f"[yellow]{skipped_dupes} later duplicate copy(ies) in this backup "
            "skipped; restoring the earliest version of each file.[/yellow]"
        )

    # Files created by renames must be removed when their pre-rename
    # originals come back, or both name pairs would exist side by side.
    # Only on FULL restores: a --profile restore excludes the rename's
    # cascaded dependents, and deleting the new-name files then would leave
    # those dependents pointing at a machine that no longer exists.
    renames = load_renames(target_backup)
    dst_set = {str(dst) for _, dst in restore_files}
    removals: list[Path] = []
    if profile_name is None:
        removals = [
            Path(new) for old, new in renames.items()
            if old in dst_set and Path(new).exists()
        ]
    elif any(old in dst_set for old in renames):
        console.print(
            "[yellow]This profile was renamed in this backup. Restoring only it "
            "keeps the renamed copy in place (removing it could break profiles "
            "that reference it). Restore the full backup for a complete undo.[/yellow]"
        )

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
        console.print(f"\n[yellow]{conflicts} file(s) will be overwritten (current versions backed up first).[/yellow]")
    if removals:
        console.print(
            f"[yellow]{len(removals)} renamed file(s) will be removed "
            "(their pre-rename originals are being restored):[/yellow]"
        )
        for path in removals:
            console.print(f"  [yellow]{path.name}[/yellow]")
    if not force and not click.confirm(f"Restore {len(restore_files)} file(s)?"):
        console.print("[yellow]Aborted.[/yellow]")
        return

    # Back up anything we're about to overwrite or remove so the restore
    # is itself undoable
    if conflicts or removals:
        overwrite_backup = create_backup_dir(backup_root, "restore-overwrites")
        console.print(f"[dim]Backing up overwritten files to: {overwrite_backup}[/dim]")
        for _, dst in restore_files:
            if dst.exists():
                backup_copy(dst, overwrite_backup, dst.parent.name)
        for path in removals:
            backup_copy(path, overwrite_backup, path.parent.name)
            path.unlink()

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
# undo — restore the most recent backup
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--force", is_flag=True, help="Skip confirmation prompt.")
@click.pass_context
def undo(ctx: click.Context, force: bool) -> None:
    """Undo the last operation by restoring the most recent backup."""
    profile_dir: Path = ctx.obj["profile_dir"]
    backup_root = profile_dir.parent / "_backup"

    if not backup_root.is_dir():
        stderr_console.print(f"[yellow]No backup directory found at {backup_root}[/yellow]")
        return
    candidates = sorted(
        [d for d in backup_root.iterdir() if d.is_dir() and re.match(r"^\d{8}_\d{6}", d.name)],
        key=lambda d: d.name,
        reverse=True,
    )
    if not candidates:
        stderr_console.print("[yellow]No backups found.[/yellow]")
        return

    latest = candidates[0]
    operation = load_operation(latest) or "unknown operation"
    console.print(f"Most recent backup: [bold]{latest.name}[/bold] ({operation})")
    ctx.invoke(restore, timestamp=latest.name, profile_name=None, force=force)


# ---------------------------------------------------------------------------
# matrix — read-only material/process x printer coverage inventory
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--category",
    type=click.Choice(["filament", "process"], case_sensitive=False),
    default=None,
    help="Which matrix to print. Default: both (filament first).",
)
@click.pass_context
def matrix(ctx: click.Context, category: str | None) -> None:
    """Print a read-only coverage matrix: materials/processes x printers.

    Shows which filaments/processes exist for which printers at a glance,
    surfacing redundancy and coverage gaps. Never modifies any files.
    """
    from . import matrix as matrix_mod

    profile_dir: Path = ctx.obj["profile_dir"]
    profiles = _load(profile_dir)
    if profiles is None:
        return

    if category is None or category.lower() == "filament":
        matrix_mod.print_filament_matrix(console, profiles)
    if category is None:
        console.print()
    if category is None or category.lower() == "process":
        matrix_mod.print_process_matrix(console, profiles)


# ---------------------------------------------------------------------------
# prune-backups — delete old timestamped backup directories
# ---------------------------------------------------------------------------


_BACKUP_TS_RE = re.compile(r"^\d{8}_\d{6}(_\d+)?$")


@cli.command("prune-backups")
@click.option("--keep", type=int, default=20, help="Number of newest backups to keep.", show_default=True)
@click.option("--execute", is_flag=True, help="Apply pruning. Without this flag, only previews.")
@click.pass_context
def prune_backups(ctx: click.Context, keep: int, execute: bool) -> None:
    """Delete old timestamped backup directories, keeping the newest N.

    Only directories matching the timestamped backup naming pattern
    (YYYYMMDD_HHMMSS[_N]) are ever considered — manually curated or
    otherwise-named directories under _backup/ are never touched.
    """
    profile_dir: Path = ctx.obj["profile_dir"]
    backup_root = profile_dir.parent / "_backup"

    if not backup_root.is_dir():
        stderr_console.print(f"[yellow]No backup directory found at {backup_root}[/yellow]")
        return

    all_dirs = [d for d in backup_root.iterdir() if d.is_dir()]
    timestamped = sorted(
        (d for d in all_dirs if _BACKUP_TS_RE.match(d.name)),
        key=lambda d: d.name,
        reverse=True,
    )

    if len(timestamped) <= keep:
        console.print(
            f"[green]Nothing to prune.[/green] {len(timestamped)} timestamped backup(s) "
            f"found, keep threshold is {keep}."
        )
        return

    kept = timestamped[:keep]
    candidates = timestamped[keep:]

    table = Table(title="Backup Prune Candidates")
    table.add_column("Timestamp", width=20)
    table.add_column("Operation", width=18)
    table.add_column("Files", justify="right", width=8)
    for bdir in candidates:
        files = [f for f in bdir.rglob("*.*") if f.name != MANIFEST_NAME]
        table.add_row(bdir.name, load_operation(bdir) or "--", str(len(files)))
    console.print(table)

    console.print(
        f"\n[bold]{len(candidates)}[/bold] backup dir(s) would be deleted, "
        f"[bold]{len(kept)}[/bold] kept."
    )
    console.print("[dim]Non-timestamped dirs (manually curated) are never touched.[/dim]")

    if not execute:
        console.print("\n[dim]Run with --execute to apply.[/dim]")
        return

    response = click.prompt(
        "Type 'yes' to permanently delete these backup dirs", default="no"
    )
    if response.strip().lower() != "yes":
        console.print("[yellow]Aborted.[/yellow]")
        return

    deleted = 0
    for bdir in candidates:
        try:
            shutil.rmtree(bdir)
            console.print(f"  [red]Deleted[/red] {bdir.name}")
            deleted += 1
        except Exception as e:
            console.print(f"  [red]Failed[/red] {bdir.name}: {e}")
    console.print(f"\n[green]Done. {deleted}/{len(candidates)} backup dir(s) deleted.[/green]")


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
