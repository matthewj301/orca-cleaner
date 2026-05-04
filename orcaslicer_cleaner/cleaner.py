"""Cleanup operations for OrcaSlicer profiles."""

from __future__ import annotations

import datetime
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

import re

from .models import DuplicateGroup, IssueType, Profile, ProfileCategory, ValidationIssue

# ---------------------------------------------------------------------------
# Printer matching helpers
# ---------------------------------------------------------------------------


def _profile_matches_printer(profile: Profile, printer: str) -> bool:
    """Check if a profile is associated with a printer name (case-insensitive).

    Matches against:
    - Machine profiles: the profile name itself
    - Filament/process profiles: compatible_printers list and the
      hardware parenthetical in the profile name
    """
    printer_lower = printer.lower()

    # Check the profile name itself
    if printer_lower in profile.name.lower():
        return True

    # Check compatible_printers
    for cp in profile.compatible_printers:
        if printer_lower in cp.lower():
            return True

    return False


def filter_actions_by_printer(
    actions: list["CleanAction"],
    printer: tuple[str, ...] | None = None,
    exclude_printer: tuple[str, ...] | None = None,
) -> list["CleanAction"]:
    """Filter cleanup actions by printer inclusion/exclusion."""
    if not printer and not exclude_printer:
        return actions

    result = actions

    if printer:
        result = [
            a for a in result
            if any(_profile_matches_printer(a.profile, p) for p in printer)
        ]

    if exclude_printer:
        result = [
            a for a in result
            if not any(_profile_matches_printer(a.profile, p) for p in exclude_printer)
        ]

    return result


@dataclass
class CleanAction:
    """A planned cleanup action."""

    action: str  # "delete", "archive", "rename"
    profile: Profile
    reason: str
    target_path: Path | None = None  # for rename/archive


CLEAN_TYPES = ("stale", "invalid", "dupes", "orphaned-hw", "broken-inherits")


def plan_cleanup(
    issues: list[ValidationIssue],
    dupe_groups: list[DuplicateGroup],
    types: tuple[str, ...] | None = None,
    orphaned_link_issues: list["LinkIssue"] | None = None,
) -> list[CleanAction]:
    """Generate a list of proposed cleanup actions (no side effects).

    types: filter to specific cleanup categories. None means all.
           Valid values: "stale", "invalid", "dupes", "orphaned-hw", "broken-inherits"
    """
    if types is None:
        types = CLEAN_TYPES

    actions: list[CleanAction] = []
    seen: set[str] = set()  # category:name to avoid duplicates

    def _key(profile: Profile) -> str:
        return f"{profile.category.value}:{profile.name}"

    if "invalid" in types:
        # Orphaned files with no JSON
        for issue in issues:
            if issue.issue_type == IssueType.ORPHANED_FILE and not issue.profile.has_json_file:
                key = _key(issue.profile)
                if key not in seen:
                    seen.add(key)
                    actions.append(
                        CleanAction(
                            action="archive",
                            profile=issue.profile,
                            reason=f"Orphaned .info with no .json: {issue.message}",
                        )
                    )

        # Malformed JSON (can't be used)
        for issue in issues:
            if issue.issue_type == IssueType.MALFORMED_JSON:
                key = _key(issue.profile)
                if key not in seen:
                    seen.add(key)
                    actions.append(
                        CleanAction(
                            action="archive",
                            profile=issue.profile,
                            reason=f"Malformed JSON: {issue.message}",
                        )
                    )

    if "stale" in types:
        for issue in issues:
            if issue.issue_type == IssueType.STALE_PROFILE:
                key = _key(issue.profile)
                if key not in seen:
                    seen.add(key)
                    actions.append(
                        CleanAction(
                            action="archive",
                            profile=issue.profile,
                            reason=f"Stale: {issue.message}",
                        )
                    )

    if "dupes" in types:
        for group in dupe_groups:
            if group.match_type != "exact_content":
                continue
            keep = group.recommended_keep
            for profile in group.profiles:
                if profile is not keep:
                    key = _key(profile)
                    if key not in seen:
                        seen.add(key)
                        actions.append(
                            CleanAction(
                                action="archive",
                                profile=profile,
                                reason=f"Exact duplicate of '{keep.name}' ({group.details})",
                            )
                        )

    if "orphaned-hw" in types and orphaned_link_issues:
        for link_issue in orphaned_link_issues:
            if link_issue.issue != "orphaned":
                continue
            key = _key(link_issue.profile)
            if key not in seen:
                seen.add(key)
                actions.append(
                    CleanAction(
                        action="archive",
                        profile=link_issue.profile,
                        reason=f"Orphaned hardware: {link_issue.details}",
                    )
                )

    if "broken-inherits" in types:
        for issue in issues:
            if issue.issue_type == IssueType.BROKEN_INHERITS:
                key = _key(issue.profile)
                if key not in seen:
                    seen.add(key)
                    actions.append(
                        CleanAction(
                            action="archive",
                            profile=issue.profile,
                            reason=f"Broken inherits: {issue.message}",
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


# ---------------------------------------------------------------------------
# Broken-reference remap
# ---------------------------------------------------------------------------


@dataclass
class RemapAction:
    """A planned remap action for a single broken printer name."""

    broken_name: str
    affected_profiles: list[Profile]
    new_name: str | None  # None = remove from compatible_printers


def find_broken_references(
    profiles: dict[ProfileCategory, list[Profile]],
) -> dict[str, list[Profile]]:
    """Return broken printer names mapped to the profiles that reference them."""
    machine_names = {p.name for p in profiles.get(ProfileCategory.MACHINE, [])}
    broken: dict[str, list[Profile]] = {}

    for category in (ProfileCategory.FILAMENT, ProfileCategory.PROCESS):
        for profile in profiles.get(category, []):
            for printer in profile.compatible_printers:
                if printer and printer not in machine_names:
                    broken.setdefault(printer, []).append(profile)

    return broken


def _backup_json(profile: Profile, backup_dir: Path) -> None:
    """Copy a profile's .json file to the backup directory (preserving original)."""
    category_backup = backup_dir / profile.category.value
    category_backup.mkdir(parents=True, exist_ok=True)

    src = profile.json_path
    if not src.exists():
        return

    dst = category_backup / f"{profile.name}.json"
    if dst.exists():
        counter = 1
        while dst.exists():
            dst = category_backup / f"{profile.name}_{counter}.json"
            counter += 1
    shutil.copy2(str(src), str(dst))


def execute_remap(
    console: Console,
    actions: list[RemapAction],
    backup_dir: Path,
) -> int:
    """Apply remap actions: back up .json files, then rewrite compatible_printers.

    Returns the number of profiles successfully modified.
    """
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    timestamped_backup = backup_dir / timestamp
    timestamped_backup.mkdir(parents=True, exist_ok=True)
    console.print(f"[dim]Backing up to: {timestamped_backup}[/dim]")

    # Collect all modifications per profile (a profile may appear in multiple actions)
    modifications: dict[Path, list[tuple[str, str | None]]] = {}
    profile_by_path: dict[Path, Profile] = {}
    for action in actions:
        for profile in action.affected_profiles:
            path = profile.json_path
            modifications.setdefault(path, []).append(
                (action.broken_name, action.new_name)
            )
            profile_by_path[path] = profile

    # Deduplicate backup: only back up each file once
    backed_up: set[Path] = set()
    modified = 0

    for path, changes in modifications.items():
        if not path.exists():
            console.print(f"  [red]Missing[/red] {path.name}")
            continue

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            console.print(f"  [red]Failed to read[/red] {path.name}: {e}")
            continue

        printers: list[str] = data.get("compatible_printers", [])
        if not isinstance(printers, list):
            console.print(f"  [red]Unexpected type[/red] compatible_printers in {path.name}")
            continue

        original = list(printers)

        for broken_name, new_name in changes:
            if new_name is not None:
                # Replace broken name with new name
                printers = [new_name if p == broken_name else p for p in printers]
            else:
                # Remove broken name entirely
                printers = [p for p in printers if p != broken_name]

        if printers == original:
            continue

        # Back up before first modification
        if path not in backed_up:
            _backup_json(profile_by_path[path], timestamped_backup)
            backed_up.add(path)

        if not printers:
            # No remaining printers — archive the profile entirely
            profile = profile_by_path[path]
            _archive_profile(profile, timestamped_backup)
            console.print(f"  [yellow]Archived[/yellow] {path.name} (no remaining printers)")
            modified += 1
        else:
            data["compatible_printers"] = printers
            try:
                path.write_text(
                    json.dumps(data, indent=4, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                console.print(f"  [green]Updated[/green] {path.name}")
                modified += 1
            except OSError as e:
                console.print(f"  [red]Failed to write[/red] {path.name}: {e}")

    return modified


# ---------------------------------------------------------------------------
# Link auditing — detect mismatched compatible_printers
# ---------------------------------------------------------------------------

# Regex to extract hardware parenthetical from profile names
_HW_PAREN_RE = re.compile(r"\(([^)]+)\)\s*$")

# Regex to extract hardware suffix for profiles without parens
# e.g., "PolyLite PLA - Positron" -> brand/suffix is "Positron"
_NAME_SUFFIX_RE = re.compile(r"^.+\s*-\s*(.+)$")


@dataclass
class LinkIssue:
    """A mismatched compatible_printers entry."""

    profile: Profile
    issue: str  # "empty" or "mismatched"
    details: str
    suggested_printers: list[str]  # what compatible_printers should be


def _extract_hardware_hint(profile: Profile, machine_names: set[str]) -> str | None:
    """Extract the hardware identifier from a profile name.

    Returns None if the profile is generic (no hardware affinity detected).
    """
    name = profile.name

    # Check for explicit hardware in parenthetical: "Material - Brand (Hardware)"
    m = _HW_PAREN_RE.search(name)
    if m:
        return m.group(1).strip()

    # Check for parenthetical anywhere in name (not just at end)
    # Handles "PolyMaker - PolyTerra PLA (Mako) - MM"
    m = re.search(r"\(([^)]+)\)", name)
    if m:
        hw = m.group(1).strip()
        # Skip if contents look like a material descriptor, not hardware
        # e.g., "(Satin PLA)", "(Beta)", "(cMatte)"
        hw_lower = hw.lower()
        for machine in machine_names:
            if hw_lower in machine.lower():
                return hw
        # Check aliases
        for alias_from, alias_to in _HARDWARE_ALIASES.items():
            if alias_from in hw_lower:
                for machine in machine_names:
                    if alias_to in machine.lower():
                        return hw
        return hw  # Return even if no machine match — let caller decide

    # For non-parenthetical names, check if the last component matches a machine name
    # e.g., "PolyLite PLA - Positron" -> "Positron"
    # Skip if suffix is too short or just a nozzle size
    m = _NAME_SUFFIX_RE.match(name)
    if m:
        suffix = m.group(1).strip()
        if re.match(r"^\d+\.?\d*mm$", suffix) or len(suffix) < 3:
            return None
        suffix_lower = suffix.lower()
        for machine in machine_names:
            if suffix_lower in machine.lower():
                return suffix

    return None


# Known aliases: hardware terms in profile names that map to machine name terms
_HARDWARE_ALIASES = {
    "mako": "bambu",
    "tk": "teakettle",
}


def _machine_matches_hardware(machine_name: str, hardware_hint: str) -> bool:
    """Check if a machine name is compatible with a hardware hint.

    Requires ALL significant hardware tokens from the hint to appear in the
    machine name. This prevents cross-matching different hotends/extruders.
    """
    hint_lower = hardware_hint.lower()
    machine_lower = machine_name.lower()

    # Direct substring match (covers "Positron" in "Positron - Sherpa Micro - 0.4mm")
    if hint_lower in machine_lower:
        return True

    # Token-based: ALL non-nozzle hint tokens must appear in machine
    nozzle_re = re.compile(r"^\d+\.?\d*mm$")

    hint_tokens = {t.strip() for t in re.split(r"[-,]", hint_lower) if t.strip()}
    hint_tokens = {t for t in hint_tokens if not nozzle_re.match(t)}

    machine_tokens = {t.strip() for t in machine_lower.split("-") if t.strip()}

    if not hint_tokens:
        return False

    for ht in hint_tokens:
        # Check exact token match
        if ht in machine_tokens:
            continue
        # Check if hint token is a known alias for something in the machine
        alias = _HARDWARE_ALIASES.get(ht)
        if alias and any(alias in mt for mt in machine_tokens):
            continue
        # Check bidirectional substring (handles "Sherpa Mini 8t" vs "Sherpa Mini")
        if any(ht in mt or mt in ht for mt in machine_tokens):
            continue
        return False

    return True


def audit_links(
    profiles: dict[ProfileCategory, list[Profile]],
) -> list[LinkIssue]:
    """Find filament/process profiles with missing or mismatched compatible_printers."""
    machine_names = {p.name for p in profiles.get(ProfileCategory.MACHINE, [])}
    machine_list = sorted(machine_names)
    issues: list[LinkIssue] = []

    for category in (ProfileCategory.FILAMENT, ProfileCategory.PROCESS):
        for profile in profiles.get(category, []):
            hw_hint = _extract_hardware_hint(profile, machine_names)
            if hw_hint is None:
                # Generic profile — no hardware affinity, skip
                continue

            # Find which machines match this hardware
            matching_machines = [
                m for m in machine_list
                if _machine_matches_hardware(m, hw_hint)
            ]

            current = profile.compatible_printers

            if not matching_machines:
                # Hardware hint matches no current machine — orphaned hardware
                issues.append(LinkIssue(
                    profile=profile,
                    issue="orphaned",
                    details=f"Hardware '{hw_hint}' matches no current machine",
                    suggested_printers=[],
                ))
            elif not current:
                # Empty compatible_printers — shows for all printers
                issues.append(LinkIssue(
                    profile=profile,
                    issue="empty",
                    details=f"Hardware hint '{hw_hint}' but visible to all printers",
                    suggested_printers=matching_machines,
                ))
            else:
                # Check for mismatched entries
                mismatched = [
                    cp for cp in current
                    if cp in machine_names and not _machine_matches_hardware(cp, hw_hint)
                ]
                if mismatched:
                    issues.append(LinkIssue(
                        profile=profile,
                        issue="mismatched",
                        details=f"Hardware '{hw_hint}' but linked to: {', '.join(mismatched)}",
                        suggested_printers=matching_machines,
                    ))

    return issues


def execute_link_fixes(
    console: Console,
    fixes: list[tuple[Profile, list[str]]],
    backup_dir: Path,
) -> int:
    """Apply compatible_printers fixes. Each fix is (profile, new_printers_list).

    Returns number of profiles updated.
    """
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    timestamped_backup = backup_dir / timestamp
    timestamped_backup.mkdir(parents=True, exist_ok=True)
    console.print(f"[dim]Backing up to: {timestamped_backup}[/dim]")

    updated = 0
    for profile, new_printers in fixes:
        path = profile.json_path
        if not path.exists():
            console.print(f"  [red]Missing[/red] {path.name}")
            continue

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            console.print(f"  [red]Failed to read[/red] {path.name}: {e}")
            continue

        _backup_json(profile, timestamped_backup)
        data["compatible_printers"] = new_printers

        try:
            path.write_text(
                json.dumps(data, indent=4, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            console.print(f"  [green]Updated[/green] {profile.name} -> {new_printers}")
            updated += 1
        except OSError as e:
            console.print(f"  [red]Failed to write[/red] {path.name}: {e}")

    return updated
