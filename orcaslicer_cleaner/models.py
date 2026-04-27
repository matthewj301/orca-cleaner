"""Data models for OrcaSlicer profiles."""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ProfileCategory(enum.Enum):
    FILAMENT = "filament"
    MACHINE = "machine"
    PROCESS = "process"


class IssueSeverity(enum.Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class IssueType(enum.Enum):
    ORPHANED_FILE = "orphaned_file"
    BROKEN_REFERENCE = "broken_reference"
    MALFORMED_JSON = "malformed_json"
    STALE_PROFILE = "stale_profile"
    MISSING_FIELD = "missing_field"
    NAMING_INCONSISTENCY = "naming_inconsistency"
    BROKEN_INHERITS = "broken_inherits"
    DUPLICATE_SETTING_ID = "duplicate_setting_id"


@dataclass
class ProfileInfo:
    """Parsed contents of a .info file."""

    sync_info: str = ""
    user_id: str = ""
    setting_id: str = ""
    base_id: str = ""
    updated_time: int = 0

    @classmethod
    def from_file(cls, path: Path) -> ProfileInfo:
        info = cls()
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if key == "sync_info":
                info.sync_info = value
            elif key == "user_id":
                info.user_id = value
            elif key == "setting_id":
                info.setting_id = value
            elif key == "base_id":
                info.base_id = value
            elif key == "updated_time":
                try:
                    info.updated_time = int(value)
                except ValueError:
                    info.updated_time = 0
        return info


@dataclass
class Profile:
    """A single OrcaSlicer profile (paired .info + .json)."""

    name: str
    category: ProfileCategory
    directory: Path
    info: ProfileInfo | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    has_info_file: bool = True
    has_json_file: bool = True
    json_parse_error: str | None = None

    @property
    def info_path(self) -> Path:
        return self.directory / f"{self.name}.info"

    @property
    def json_path(self) -> Path:
        return self.directory / f"{self.name}.json"

    @property
    def compatible_printers(self) -> list[str]:
        val = self.settings.get("compatible_printers", [])
        if isinstance(val, list):
            return val
        return []

    @property
    def inherits(self) -> str | None:
        return self.settings.get("inherits")

    @property
    def profile_name_in_json(self) -> str | None:
        return self.settings.get("name")

    def settings_without_metadata(self) -> dict[str, Any]:
        """Return settings with volatile metadata stripped for comparison."""
        skip = {
            "setting_id",
            "updated_time",
            "from",
            "is_custom_defined",
            "print_settings_id",
            "filament_settings_id",
            "printer_settings_id",
        }
        return {k: v for k, v in sorted(self.settings.items()) if k not in skip}


@dataclass
class ValidationIssue:
    """A single validation issue found in a profile."""

    profile: Profile
    issue_type: IssueType
    severity: IssueSeverity
    message: str
    details: str = ""


@dataclass
class DuplicateGroup:
    """A group of profiles detected as duplicates or near-duplicates."""

    profiles: list[Profile]
    similarity_score: float
    match_type: str  # "exact_content", "name_similar", "content_similar"
    details: str = ""

    @property
    def recommended_keep(self) -> Profile:
        """Recommend keeping the most recently updated profile."""
        return max(
            self.profiles,
            key=lambda p: p.info.updated_time if p.info else 0,
        )
