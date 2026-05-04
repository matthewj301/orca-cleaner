"""Naming standardizer for OrcaSlicer profiles."""

from __future__ import annotations

import datetime
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .models import Profile, ProfileCategory


@dataclass
class RenameAction:
    """A planned rename action for a single profile."""

    profile: Profile
    old_name: str
    new_name: str


# ---------------------------------------------------------------------------
# Normalization rules
# ---------------------------------------------------------------------------

# Match layer heights at the START of a name: "0.2mm", "0.3mm" (single decimal
# place) that should be "0.20mm", "0.30mm". Only matches name-leading values,
# NOT nozzle sizes which appear at the end of names or inside parentheses.
_LAYER_HEIGHT_RE = re.compile(
    r"^(\d+\.\d)mm\b"
)

# Hyphens that are word separators (with at least one space on either side)
# should be normalized to " - ". But compound words like "V-Core", "ASA-CF",
# "Metal-Filled", "PLA-CF" should be left alone.
_SPACED_HYPHEN_RE = re.compile(
    r"(?<=\S)\s+-\s*(?=\S)|(?<=\S)\s*-\s+(?=\S)"
)

# Known abbreviations to expand in hardware portions of names
_ABBREVIATIONS = {
    "TK": "TeaKettle",
}


def _normalize_name(name: str) -> str:
    """Apply all naming standardization rules to a profile name."""
    result = name

    # Rule 1: Normalize layer heights to 2 decimal places.
    # "0.2mm" -> "0.20mm", "0.3mm" -> "0.30mm"
    # "0.08mm" stays "0.08mm" (already 2+ decimals)
    def _fix_layer_height(m: re.Match) -> str:
        num = m.group(1)
        # Only pad if there's exactly one decimal digit
        return f"{num}0mm"

    result = _LAYER_HEIGHT_RE.sub(_fix_layer_height, result)

    # Rule 2: Normalize spaced hyphens to " - ".
    # Only touches hyphens that already have at least one space on one side,
    # preserving compound words like "V-Core", "ASA-CF", "Metal-Filled".
    result = _SPACED_HYPHEN_RE.sub(" - ", result)

    # Rule 3: Expand known abbreviations in hardware parenthetical.
    # "TK" -> "TeaKettle" etc.
    result = _expand_abbreviations(result)

    # Rule 4: Collapse multiple spaces
    result = re.sub(r"  +", " ", result)

    return result


def _expand_abbreviations(name: str) -> str:
    """Expand known hardware abbreviations within the name."""
    for abbrev, full in _ABBREVIATIONS.items():
        # Match abbreviation as a whole word (bounded by spaces, hyphens, or parens)
        pattern = re.compile(
            r"(?<=[\s(,-])" + re.escape(abbrev) + r"(?=[\s),-]|$)"
        )
        name = pattern.sub(full, name)
    return name


# Match a nozzle-only parenthetical at the end of a name
_NOZZLE_ONLY_PAREN_RE = re.compile(r"\((\d+\.?\d*mm)\)\s*$")


def _extract_hardware_from_machine(machine_name: str) -> str | None:
    """Extract the hardware path from a machine profile name.

    Machine names follow "PrinterModel - Extruder - Hotend - NozzleSize".
    Returns everything after the first segment, or None if there's only
    a nozzle size (e.g., "Bambu Lab X1 Carbon - 0.4mm").
    """
    parts = [p.strip() for p in machine_name.split(" - ")]
    if len(parts) < 3:
        return None  # Just "Model - 0.4mm" — no useful hardware info
    hw = " - ".join(parts[1:])
    return hw


def _inject_hardware(
    profile: Profile,
    machine_names: dict[str, str],
) -> str | None:
    """If profile has a nozzle-only parenthetical and compatible_printers
    can resolve to a hardware path, return the new name. Otherwise None.
    """
    m = _NOZZLE_ONLY_PAREN_RE.search(profile.name)
    if not m:
        return None

    printers = profile.compatible_printers
    if not printers:
        return None

    # Collect hardware paths from all compatible printers
    hw_paths: set[str] = set()
    for cp in printers:
        hw = machine_names.get(cp)
        if hw:
            hw_paths.add(hw)

    if len(hw_paths) != 1:
        return None  # Ambiguous or no hardware info

    hw_path = hw_paths.pop()
    new_name = _NOZZLE_ONLY_PAREN_RE.sub(f"({hw_path})", profile.name)
    return new_name if new_name != profile.name else None


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def find_renames(
    profiles: dict[ProfileCategory, list[Profile]],
) -> list[RenameAction]:
    """Scan all profiles and identify those needing name normalization."""
    actions: list[RenameAction] = []

    # Build machine hardware lookup for hardware injection
    machine_hw: dict[str, str] = {}
    for p in profiles.get(ProfileCategory.MACHINE, []):
        hw = _extract_hardware_from_machine(p.name)
        if hw:
            machine_hw[p.name] = hw

    for category in ProfileCategory:
        for profile in profiles.get(category, []):
            new_name = _normalize_name(profile.name)

            if category in (ProfileCategory.FILAMENT, ProfileCategory.PROCESS):
                # Hardware injection for nozzle-only parens
                injected = _inject_hardware(profile, machine_hw)
                if injected:
                    new_name = _normalize_name(injected)

                # Normalize parenthetical to match machine hardware format
                new_name = _normalize_hardware_paren(new_name, profile, machine_hw)

            if new_name != profile.name:
                actions.append(
                    RenameAction(
                        profile=profile,
                        old_name=profile.name,
                        new_name=new_name,
                    )
                )

    return actions


def _normalize_hardware_paren(
    name: str,
    profile: Profile,
    machine_hw: dict[str, str],
) -> str:
    """Normalize the hardware parenthetical in a filament/process profile name
    to match the machine's hardware format exactly.

    Fixes comma separators -> ' - ' and nozzle size format to match machine.
    """
    m = re.search(r"\(([^)]+)\)\s*$", name)
    if not m:
        return name

    paren_content = m.group(1)

    # Get the machine hardware string for this profile's printer
    printers = profile.compatible_printers
    if not printers:
        return name

    # Find the matching machine hardware to use as the canonical format
    target_hw = None
    for cp in printers:
        hw = machine_hw.get(cp)
        if hw:
            target_hw = hw
            break

    if not target_hw:
        # No machine with extractable hardware — just normalize separators
        normalized = re.sub(r"\s*,\s*", " - ", paren_content)
        normalized = re.sub(r"\s+", " ", normalized)
        if normalized != paren_content:
            return name[:m.start(1)] + normalized + name[m.end(1):]
        return name

    # The target_hw is the canonical form: "LGX Lite Pro - TeaKettle - 0.4mm"
    # If the parenthetical content matches the same hardware (same tokens),
    # replace it with the canonical form
    paren_normalized = re.sub(r"\s*[,]\s*", " - ", paren_content)
    paren_normalized = re.sub(r"(?<=\S)\s+(?=\d+\.?\d*mm)", " - ", paren_normalized)
    paren_normalized = re.sub(r"\s+", " ", paren_normalized).strip()

    # Check if paren tokens match machine hardware tokens (ignoring format)
    paren_tokens = {t.strip().lower() for t in re.split(r"[-,]", paren_normalized) if t.strip()}
    hw_tokens = {t.strip().lower() for t in target_hw.split(" - ") if t.strip()}

    # Normalize nozzle representations for comparison
    nozzle_re = re.compile(r"^\d+\.?\d*mm$")
    paren_nozzles = {t for t in paren_tokens if nozzle_re.match(t)}
    hw_nozzles = {t for t in hw_tokens if nozzle_re.match(t)}
    paren_words = paren_tokens - paren_nozzles
    hw_words = hw_tokens - hw_nozzles

    # If the non-nozzle tokens match, use the machine's canonical form
    if paren_words and paren_words == hw_words:
        new_name = name[:m.start(1)] + target_hw + name[m.end(1):]
        return new_name

    return name


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


def preview_renames(console: Console, actions: list[RenameAction]) -> None:
    """Print a Rich table showing proposed renames."""
    if not actions:
        console.print("[green]All profile names are already standardized.[/green]")
        return

    table = Table(title="Proposed Name Standardization")
    table.add_column("Category", width=10)
    table.add_column("Old Name", max_width=60)
    table.add_column("New Name", max_width=60, style="green")

    for action in sorted(actions, key=lambda a: (a.profile.category.value, a.old_name)):
        table.add_row(
            action.profile.category.value,
            action.old_name,
            action.new_name,
        )

    console.print(table)
    console.print(f"\n[dim]{len(actions)} profile(s) would be renamed.[/dim]")


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

# Settings ID fields that may contain the profile name.
_SETTINGS_ID_FIELDS = (
    "print_settings_id",
    "filament_settings_id",
    "printer_settings_id",
    "name",
)


def execute_renames(
    console: Console,
    actions: list[RenameAction],
    backup_dir: Path,
    all_profiles: dict[ProfileCategory, list[Profile]] | None = None,
) -> int:
    """Apply rename actions: back up files, rename on disk, update JSON internals.

    Machine profiles are renamed first so that compatible_printers references
    in filament/process profiles can be updated to match.

    Returns the number of profiles successfully renamed.
    """
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    timestamped_backup = backup_dir / timestamp
    timestamped_backup.mkdir(parents=True, exist_ok=True)
    console.print(f"[dim]Backing up to: {timestamped_backup}[/dim]")

    machine_actions = [a for a in actions if a.profile.category == ProfileCategory.MACHINE]
    other_actions = [a for a in actions if a.profile.category != ProfileCategory.MACHINE]

    renamed = 0
    machine_remap: dict[str, str] = {}

    for action in machine_actions:
        try:
            _execute_single_rename(action, timestamped_backup)
            console.print(
                f"  [green]Renamed[/green] {action.old_name} -> {action.new_name}"
            )
            machine_remap[action.old_name] = action.new_name
            renamed += 1
        except Exception as e:
            console.print(
                f"  [red]Failed[/red] {action.old_name}: {e}"
            )

    if machine_remap:
        _cascade_machine_renames(console, machine_remap, all_profiles)

    for action in other_actions:
        try:
            _execute_single_rename(action, timestamped_backup)
            console.print(
                f"  [green]Renamed[/green] {action.old_name} -> {action.new_name}"
            )
            renamed += 1
        except Exception as e:
            console.print(
                f"  [red]Failed[/red] {action.old_name}: {e}"
            )

    return renamed


def _cascade_machine_renames(
    console: Console,
    remap: dict[str, str],
    all_profiles: dict[ProfileCategory, list[Profile]] | None,
) -> None:
    """Update compatible_printers in filament/process profiles after machine renames."""
    if not all_profiles:
        return

    updated = 0
    for category in (ProfileCategory.FILAMENT, ProfileCategory.PROCESS):
        for profile in all_profiles.get(category, []):
            json_path = profile.directory / f"{profile.name}.json"
            if not json_path.exists():
                continue
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue

            cp = data.get("compatible_printers", [])
            if not cp:
                continue

            new_cp = [remap.get(p, p) for p in cp]
            if new_cp != cp:
                data["compatible_printers"] = new_cp
                json_path.write_text(
                    json.dumps(data, indent=4, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                updated += 1

    if updated:
        console.print(
            f"  [cyan]Updated compatible_printers in {updated} profile(s) "
            f"to match renamed machine(s).[/cyan]"
        )


def _execute_single_rename(action: RenameAction, backup_dir: Path) -> None:
    """Rename a single profile: backup, update JSON, rename files."""
    profile = action.profile
    category_backup = backup_dir / profile.category.value
    category_backup.mkdir(parents=True, exist_ok=True)

    # Back up existing files
    for suffix in (".info", ".json"):
        src = profile.directory / f"{action.old_name}{suffix}"
        if src.exists():
            dst = category_backup / f"{action.old_name}{suffix}"
            shutil.copy2(str(src), str(dst))

    # Update JSON contents if the file exists
    json_src = profile.directory / f"{action.old_name}.json"
    if json_src.exists():
        try:
            data = json.loads(json_src.read_text(encoding="utf-8"))
            modified = False

            for field in _SETTINGS_ID_FIELDS:
                if field in data and data[field] == action.old_name:
                    data[field] = action.new_name
                    modified = True

            if modified:
                json_src.write_text(
                    json.dumps(data, indent=4, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
        except (json.JSONDecodeError, OSError):
            pass  # If we can't update internals, still rename the file

    # Rename files on disk
    for suffix in (".info", ".json"):
        src = profile.directory / f"{action.old_name}{suffix}"
        dst = profile.directory / f"{action.new_name}{suffix}"
        if src.exists():
            if dst.exists():
                raise FileExistsError(
                    f"Target already exists: {dst.name}"
                )
            src.rename(dst)
