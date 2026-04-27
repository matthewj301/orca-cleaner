"""Load system profile names from OrcaSlicer app bundle (read-only)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SystemProfileNames:
    """Names collected from OrcaSlicer system profiles."""

    machine_names: set[str] = field(default_factory=set)
    process_names: set[str] = field(default_factory=set)
    filament_names: set[str] = field(default_factory=set)
    inherits_targets: set[str] = field(default_factory=set)


def load_system_profile_names(profiles_dir: Path) -> SystemProfileNames:
    """Scan all vendor dirs for machine, process, and filament profile names.

    Looks in <profiles_dir>/<vendor>/{machine,process,filament}/ for .json files.
    Collects:
    - machine_names: the "name" field from each machine JSON
    - process_names: the "name" field from each process JSON
    - filament_names: the "name" field from each filament JSON
    - inherits_targets: JSON filenames (without .json extension) across all vendor subdirs
    """
    result = SystemProfileNames()

    if not profiles_dir.is_dir():
        return result

    for vendor_dir in sorted(profiles_dir.iterdir()):
        if not vendor_dir.is_dir():
            continue
        _scan_vendor(vendor_dir, result)

    return result


_CATEGORY_TO_ATTR = {
    "machine": "machine_names",
    "process": "process_names",
    "filament": "filament_names",
}


def _scan_vendor(vendor_dir: Path, result: SystemProfileNames) -> None:
    """Scan a single vendor directory for machine, process, and filament profiles."""
    for category, attr in _CATEGORY_TO_ATTR.items():
        category_dir = vendor_dir / category
        if not category_dir.is_dir():
            continue

        names_set: set[str] = getattr(result, attr)
        _scan_category_dir(category_dir, names_set, result.inherits_targets)


def _scan_category_dir(
    category_dir: Path,
    names_set: set[str],
    inherits_targets: set[str],
) -> None:
    """Scan a single category directory, collecting names and inherits targets."""
    for json_file in category_dir.iterdir():
        if json_file.suffix != ".json" or json_file.name.startswith("."):
            continue

        # The filename stem is always a valid inherits target
        inherits_targets.add(json_file.stem)

        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        if isinstance(data, dict):
            name = data.get("name")
            if name and isinstance(name, str):
                names_set.add(name)
