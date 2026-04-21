"""Validation checks for OrcaSlicer profiles."""

from __future__ import annotations

import time

from .models import (
    IssueSeverity,
    IssueType,
    Profile,
    ProfileCategory,
    ValidationIssue,
)

# Profiles older than this many days are considered stale
DEFAULT_STALE_DAYS = 365


def validate_all(
    profiles: dict[ProfileCategory, list[Profile]],
    stale_days: int = DEFAULT_STALE_DAYS,
) -> list[ValidationIssue]:
    """Run all validation checks and return a list of issues."""
    issues: list[ValidationIssue] = []

    all_profiles = [p for ps in profiles.values() for p in ps]
    machine_names = {p.name for p in profiles.get(ProfileCategory.MACHINE, [])}

    for profile in all_profiles:
        issues.extend(_check_orphaned(profile))
        issues.extend(_check_malformed_json(profile))
        issues.extend(_check_missing_fields(profile))
        issues.extend(_check_broken_references(profile, machine_names))
        issues.extend(_check_stale(profile, stale_days))

    issues.extend(_check_duplicate_setting_ids(all_profiles))

    return issues


def _check_orphaned(profile: Profile) -> list[ValidationIssue]:
    """Check for .info without .json or vice versa."""
    issues: list[ValidationIssue] = []

    if not profile.has_info_file:
        issues.append(
            ValidationIssue(
                profile=profile,
                issue_type=IssueType.ORPHANED_FILE,
                severity=IssueSeverity.WARNING,
                message=f"Missing .info file for '{profile.name}'",
                details=f"Found .json but no .info in {profile.directory}",
            )
        )
    if not profile.has_json_file:
        issues.append(
            ValidationIssue(
                profile=profile,
                issue_type=IssueType.ORPHANED_FILE,
                severity=IssueSeverity.WARNING,
                message=f"Missing .json file for '{profile.name}'",
                details=f"Found .info but no .json in {profile.directory}",
            )
        )

    return issues


def _check_malformed_json(profile: Profile) -> list[ValidationIssue]:
    """Check for JSON parse errors."""
    if profile.json_parse_error:
        return [
            ValidationIssue(
                profile=profile,
                issue_type=IssueType.MALFORMED_JSON,
                severity=IssueSeverity.ERROR,
                message=f"Malformed JSON in '{profile.name}'",
                details=profile.json_parse_error,
            )
        ]
    return []


def _check_missing_fields(profile: Profile) -> list[ValidationIssue]:
    """Check for missing or empty required fields in .info."""
    issues: list[ValidationIssue] = []

    if profile.info is None and profile.has_info_file:
        issues.append(
            ValidationIssue(
                profile=profile,
                issue_type=IssueType.MISSING_FIELD,
                severity=IssueSeverity.WARNING,
                message=f"Failed to parse .info for '{profile.name}'",
            )
        )
        return issues

    if profile.info and not profile.info.base_id:
        issues.append(
            ValidationIssue(
                profile=profile,
                issue_type=IssueType.MISSING_FIELD,
                severity=IssueSeverity.INFO,
                message=f"Empty base_id in '{profile.name}'",
            )
        )

    if profile.info and not profile.info.updated_time:
        issues.append(
            ValidationIssue(
                profile=profile,
                issue_type=IssueType.MISSING_FIELD,
                severity=IssueSeverity.INFO,
                message=f"Missing updated_time in '{profile.name}'",
            )
        )

    return issues


def _check_broken_references(
    profile: Profile, machine_names: set[str]
) -> list[ValidationIssue]:
    """Check that compatible_printers and inherits reference existing profiles."""
    issues: list[ValidationIssue] = []

    if profile.category in (ProfileCategory.FILAMENT, ProfileCategory.PROCESS):
        for printer in profile.compatible_printers:
            if printer and printer not in machine_names:
                issues.append(
                    ValidationIssue(
                        profile=profile,
                        issue_type=IssueType.BROKEN_REFERENCE,
                        severity=IssueSeverity.WARNING,
                        message=f"References non-existent printer '{printer}'",
                        details=f"Profile '{profile.name}' lists '{printer}' in compatible_printers",
                    )
                )

    return issues


def _check_stale(
    profile: Profile, stale_days: int
) -> list[ValidationIssue]:
    """Check if a profile hasn't been updated in a long time."""
    if not profile.info or not profile.info.updated_time:
        return []

    age_seconds = time.time() - profile.info.updated_time
    age_days = age_seconds / 86400

    if age_days > stale_days:
        return [
            ValidationIssue(
                profile=profile,
                issue_type=IssueType.STALE_PROFILE,
                severity=IssueSeverity.INFO,
                message=f"Profile '{profile.name}' not updated in {int(age_days)} days",
                details=f"Last updated: {profile.info.updated_time}",
            )
        ]
    return []


def _check_duplicate_setting_ids(profiles: list[Profile]) -> list[ValidationIssue]:
    """Check for profiles sharing the same setting_id."""
    issues: list[ValidationIssue] = []
    seen: dict[str, list[Profile]] = {}

    for profile in profiles:
        if not profile.info or not profile.info.setting_id:
            continue
        sid = profile.info.setting_id
        seen.setdefault(sid, []).append(profile)

    for sid, group in seen.items():
        if len(group) > 1:
            names = ", ".join(f"'{p.name}'" for p in group)
            for profile in group:
                issues.append(
                    ValidationIssue(
                        profile=profile,
                        issue_type=IssueType.DUPLICATE_SETTING_ID,
                        severity=IssueSeverity.WARNING,
                        message=f"Duplicate setting_id '{sid}'",
                        details=f"Shared by: {names}",
                    )
                )

    return issues
