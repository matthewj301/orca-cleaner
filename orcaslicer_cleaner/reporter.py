"""Rich output formatting for scan results."""

from __future__ import annotations

import datetime
import json
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .models import (
    DuplicateGroup,
    IssueSeverity,
    IssueType,
    Profile,
    ProfileCategory,
    ValidationIssue,
)

SEVERITY_COLORS = {
    IssueSeverity.ERROR: "red",
    IssueSeverity.WARNING: "yellow",
    IssueSeverity.INFO: "blue",
}

SEVERITY_ICONS = {
    IssueSeverity.ERROR: "ERR",
    IssueSeverity.WARNING: "WRN",
    IssueSeverity.INFO: "INF",
}

MATCH_TYPE_COLORS = {
    "exact_content": "red",
    "content_similar": "yellow",
    "name_similar": "cyan",
}


def print_summary(
    console: Console,
    profiles: dict[ProfileCategory, list[Profile]],
    issues: list[ValidationIssue],
    dupe_groups: list[DuplicateGroup],
) -> None:
    """Print a summary panel with counts."""
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Label", style="bold")
    table.add_column("Value", justify="right")

    for cat in ProfileCategory:
        count = len(profiles.get(cat, []))
        table.add_row(f"{cat.value.title()} profiles", str(count))

    table.add_row("", "")

    error_count = sum(1 for i in issues if i.severity == IssueSeverity.ERROR)
    warn_count = sum(1 for i in issues if i.severity == IssueSeverity.WARNING)
    info_count = sum(1 for i in issues if i.severity == IssueSeverity.INFO)

    table.add_row("Errors", Text(str(error_count), style="red bold"))
    table.add_row("Warnings", Text(str(warn_count), style="yellow"))
    table.add_row("Info", Text(str(info_count), style="blue"))
    table.add_row("Duplicate groups", str(len(dupe_groups)))

    console.print(Panel(table, title="OrcaSlicer Profile Scan", border_style="green"))


def print_issues(
    console: Console,
    issues: list[ValidationIssue],
    category_filter: ProfileCategory | None = None,
    type_filter: IssueType | None = None,
) -> None:
    """Print a table of validation issues."""
    filtered = issues
    if category_filter:
        filtered = [i for i in filtered if i.profile.category == category_filter]
    if type_filter:
        filtered = [i for i in filtered if i.issue_type == type_filter]

    if not filtered:
        console.print("[green]No issues found.[/green]")
        return

    table = Table(title="Validation Issues", show_lines=True)
    table.add_column("Sev", width=3, justify="center")
    table.add_column("Category", width=10)
    table.add_column("Type", width=20)
    table.add_column("Profile", max_width=50)
    table.add_column("Message", max_width=60)

    # Sort: errors first, then warnings, then info
    severity_order = {IssueSeverity.ERROR: 0, IssueSeverity.WARNING: 1, IssueSeverity.INFO: 2}
    filtered.sort(key=lambda i: (severity_order[i.severity], i.issue_type.value))

    for issue in filtered:
        color = SEVERITY_COLORS[issue.severity]
        table.add_row(
            Text(SEVERITY_ICONS[issue.severity], style=f"bold {color}"),
            issue.profile.category.value,
            issue.issue_type.value,
            issue.profile.name,
            issue.message,
        )

    console.print(table)


def print_duplicates(
    console: Console,
    dupe_groups: list[DuplicateGroup],
) -> None:
    """Print duplicate groups with details."""
    if not dupe_groups:
        console.print("[green]No duplicates found.[/green]")
        return

    for i, group in enumerate(dupe_groups, 1):
        color = MATCH_TYPE_COLORS.get(group.match_type, "white")
        title = f"Duplicate Group {i} [{group.match_type}]"

        table = Table(show_header=True, box=None, padding=(0, 1))
        table.add_column("Profile", max_width=60)
        table.add_column("Category", width=10)
        table.add_column("Updated", width=12)
        table.add_column("Keep?", width=5, justify="center")

        recommended = group.recommended_keep
        for profile in group.profiles:
            is_keep = profile is recommended
            updated = ""
            if profile.info and profile.info.updated_time:
                dt = datetime.datetime.fromtimestamp(profile.info.updated_time)
                updated = dt.strftime("%Y-%m-%d")

            table.add_row(
                profile.name,
                profile.category.value,
                updated,
                Text("*", style="green bold") if is_keep else "",
            )

        panel_text = Text()
        panel_text.append(f"Similarity: {group.similarity_score:.0%}\n")
        panel_text.append(f"{group.details}\n")
        panel_text.append("* = recommended to keep (most recently updated)")

        console.print(Panel(table, title=title, subtitle=str(panel_text), border_style=color))
        console.print()


def print_json_report(
    profiles: dict[ProfileCategory, list[Profile]],
    issues: list[ValidationIssue],
    dupe_groups: list[DuplicateGroup],
) -> str:
    """Return a JSON string of the full report."""
    report: dict[str, Any] = {
        "summary": {
            cat.value: len(profiles.get(cat, [])) for cat in ProfileCategory
        },
        "issues": [
            {
                "profile": i.profile.name,
                "category": i.profile.category.value,
                "type": i.issue_type.value,
                "severity": i.severity.value,
                "message": i.message,
                "details": i.details,
            }
            for i in issues
        ],
        "duplicate_groups": [
            {
                "match_type": g.match_type,
                "similarity": g.similarity_score,
                "details": g.details,
                "profiles": [
                    {
                        "name": p.name,
                        "category": p.category.value,
                    }
                    for p in g.profiles
                ],
                "recommended_keep": g.recommended_keep.name,
            }
            for g in dupe_groups
        ],
    }
    return json.dumps(report, indent=2)
