"""Cleanup operations for OrcaSlicer profiles."""

from __future__ import annotations

import datetime
import shutil
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from .models import DuplicateGroup, IssueType, Profile, ValidationIssue


@dataclass
class CleanAction:
    """A planned cleanup action."""

    action: str  # "delete", "archive", "rename"
    profile: Profile
    reason: str
    target_path: Path | None = None  # for rename/archive


def plan_cleanup(
    issues: list[ValidationIssue],
    dupe_groups: list[DuplicateGroup],
) -> list[CleanAction]:
    """Generate a list of proposed cleanup actions (no side effects)."""
    actions: list[CleanAction] = []

    # Orphaned files with no JSON = safe to archive
    for issue in issues:
        if issue.issue_type == IssueType.ORPHANED_FILE and not issue.profile.has_json_file:
            actions.append(
                CleanAction(
                    action="archive",
                    profile=issue.profile,
                    reason=f"Orphaned .info with no .json: {issue.message}",
                )
            )

    # Duplicate groups: archive all but the recommended keep
    for group in dupe_groups:
        if group.match_type != "exact_content":
            continue
        keep = group.recommended_keep
        for profile in group.profiles:
            if profile is not keep:
                actions.append(
                    CleanAction(
                        action="archive",
                        profile=profile,
                        reason=f"Exact duplicate of '{keep.name}' ({group.details})",
                    )
                )

    return actions


def preview_actions(console: Console, actions: list[CleanAction]) -> None:
    """Print what cleanup actions would be taken (dry-run)."""
    if not actions:
        console.print("[green]No cleanup actions to take.[/green]")
        return

    from rich.table import Table

    table = Table(title="Planned Cleanup Actions (dry-run)")
    table.add_column("Action", width=8)
    table.add_column("Profile", max_width=50)
    table.add_column("Category", width=10)
    table.add_column("Reason", max_width=60)

    for a in actions:
        table.add_row(a.action, a.profile.name, a.profile.category.value, a.reason)

    console.print(table)


def execute_actions(
    console: Console,
    actions: list[CleanAction],
    backup_dir: Path,
) -> int:
    """Execute cleanup actions, archiving files to backup_dir first.

    Adds a timestamp subdirectory to prevent overwriting previous backups.
    Returns the number of actions successfully executed.
    """
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    timestamped_backup = backup_dir / timestamp
    timestamped_backup.mkdir(parents=True, exist_ok=True)
    console.print(f"[dim]Backing up to: {timestamped_backup}[/dim]")

    executed = 0

    for action in actions:
        try:
            if action.action in ("archive", "delete"):
                _archive_profile(action.profile, timestamped_backup)
                label = "Archived" if action.action == "archive" else "Deleted"
                color = "yellow" if action.action == "archive" else "red"
                console.print(f"  [{color}]{label}[/{color}] {action.profile.name}")
                executed += 1
        except Exception as e:
            console.print(f"  [red]Failed[/red] {action.profile.name}: {e}")

    return executed


def _archive_profile(profile: Profile, backup_dir: Path) -> None:
    """Move a profile's files to the backup directory."""
    category_backup = backup_dir / profile.category.value
    category_backup.mkdir(parents=True, exist_ok=True)

    for suffix in (".info", ".json"):
        src = profile.directory / f"{profile.name}{suffix}"
        if src.exists():
            dst = category_backup / f"{profile.name}{suffix}"
            # Handle collision: append counter if destination exists
            if dst.exists():
                counter = 1
                while dst.exists():
                    dst = category_backup / f"{profile.name}_{counter}{suffix}"
                    counter += 1
            shutil.move(str(src), str(dst))
