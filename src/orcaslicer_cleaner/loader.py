"""Load OrcaSlicer profiles from disk."""

from __future__ import annotations

import json
from pathlib import Path

from .models import Profile, ProfileCategory, ProfileInfo


def discover_profile_dirs(user_dir: Path) -> list[Path]:
    """Find all user profile root directories (e.g. 1987659579/, default/)."""
    if not user_dir.is_dir():
        raise FileNotFoundError(f"Profile directory not found: {user_dir}")
    return [d for d in sorted(user_dir.iterdir()) if d.is_dir()]


def load_profiles(profile_root: Path) -> dict[ProfileCategory, list[Profile]]:
    """Load all profiles from a single profile root directory.

    Expects subdirs: filament/, machine/, process/
    """
    result: dict[ProfileCategory, list[Profile]] = {
        ProfileCategory.FILAMENT: [],
        ProfileCategory.MACHINE: [],
        ProfileCategory.PROCESS: [],
    }

    for category in ProfileCategory:
        category_dir = profile_root / category.value
        if not category_dir.is_dir():
            continue
        result[category] = _load_category(category_dir, category)

    return result


def _load_category(directory: Path, category: ProfileCategory) -> list[Profile]:
    """Load all profiles from a single category directory."""
    # Collect all profile names by looking at both .info and .json files
    names: set[str] = set()
    for f in directory.iterdir():
        if f.name.startswith("."):
            continue
        if f.suffix in (".info", ".json"):
            names.add(f.stem)

    profiles: list[Profile] = []
    for name in sorted(names):
        profile = _load_profile(name, directory, category)
        profiles.append(profile)

    return profiles


def _load_profile(name: str, directory: Path, category: ProfileCategory) -> Profile:
    """Load a single profile pair (.info + .json)."""
    info_path = directory / f"{name}.info"
    json_path = directory / f"{name}.json"

    profile = Profile(
        name=name,
        category=category,
        directory=directory,
        has_info_file=info_path.exists(),
        has_json_file=json_path.exists(),
    )

    if profile.has_info_file:
        try:
            profile.info = ProfileInfo.from_file(info_path)
        except Exception:
            profile.info = None

    if profile.has_json_file:
        try:
            profile.settings = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            profile.json_parse_error = str(e)
        except Exception:
            profile.json_parse_error = "Failed to read file"

    return profile
