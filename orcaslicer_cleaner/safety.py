"""Blast-radius analysis for destructive operations.

Added after a real data-loss incident (see the DATA-LOSS GUARDS note in
CLAUDE.md): this module is a read-only analysis layer that inspects a
planned mutation (archive/modify) BEFORE it executes and flags anything
that looks disproportionately risky — touching machine profiles (which
other profiles reference by name), archiving a large slice of a category,
or a bulk operation in general. It also compares before/after profile
snapshots to report printer-coverage regressions and newly-introduced
broken references, so callers can show "here's what this actually costs"
before a mutation is confirmed.

Nothing in here writes to disk.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Profile, ProfileCategory

# Percentage (as a fraction) of a category that must be archived, on top of
# the absolute floor below, before we warn about a category-wide sweep.
_CATEGORY_PERCENT_THRESHOLD = 0.15
# A category sweep must also touch at least this many profiles — otherwise
# small categories (e.g. 2 of 3) would trip the percentage alone.
_CATEGORY_ABSOLUTE_FLOOR = 3
# Total archived profiles in a single operation, regardless of category
# distribution, that's large enough to warrant a hard confirm on its own.
_BULK_THRESHOLD = 20

# Sentinel key for profiles with empty compatible_printers ("visible to ALL
# printers" in OrcaSlicer).
ALL_PRINTERS_KEY = "*ALL*"

# Cap on how many profile names to list inline before collapsing to "... and N more".
_MAX_NAMES_SHOWN = 8


@dataclass
class BlastAssessment:
    """Result of assessing a planned mutation's blast radius."""

    warnings: list[str] = field(default_factory=list)

    @property
    def requires_hard_confirm(self) -> bool:
        return bool(self.warnings)


def assess_blast_radius(
    profiles: dict[ProfileCategory, list[Profile]],
    to_archive: list[Profile],
    to_modify: list[Profile] = (),
) -> BlastAssessment:
    """Assess how risky a planned archive/modify operation is.

    Warns on: touching any machine-category profile (archived or modified),
    archiving a large fraction of a category, and archiving a large number
    of profiles overall. Returns an assessment with no warnings for a
    routine, narrowly-scoped operation.
    """
    to_modify = list(to_modify)
    warnings: list[str] = []

    # 1. Any machine-category profile touched at all.
    touched_machines = sorted(
        {
            p.name
            for p in (*to_archive, *to_modify)
            if p.category == ProfileCategory.MACHINE
        }
    )
    if touched_machines:
        names = ", ".join(touched_machines)
        warnings.append(
            f"touches machine profile(s): {names} — other profiles "
            "reference machines by name"
        )

    # 2. Per-category archive percentage.
    counts_by_category: dict[ProfileCategory, int] = {
        category: len(items) for category, items in profiles.items()
    }
    archived_by_category: dict[ProfileCategory, int] = {}
    for p in to_archive:
        archived_by_category[p.category] = archived_by_category.get(p.category, 0) + 1

    for category, archived_count in archived_by_category.items():
        total = counts_by_category.get(category, 0)
        if total <= 0:
            continue
        if archived_count < _CATEGORY_ABSOLUTE_FLOOR:
            continue
        fraction = archived_count / total
        if fraction > _CATEGORY_PERCENT_THRESHOLD:
            pct = round(fraction * 100)
            warnings.append(
                f"archives {archived_count} of {total} {category.value} "
                f"profiles ({pct}%)"
            )

    # 3. Total bulk threshold.
    if len(to_archive) >= _BULK_THRESHOLD:
        warnings.append(f"archives {len(to_archive)} profiles in one operation")

    return BlastAssessment(warnings=warnings)


def coverage_snapshot(profiles: dict[ProfileCategory, list[Profile]]) -> dict[str, set[str]]:
    """Map each machine name to the set of filament/process profiles linked to it.

    Each linked profile is represented as "category:name". Profiles with an
    empty compatible_printers list (visible to ALL printers) are collected
    under the ALL_PRINTERS_KEY sentinel instead. Machines with no linked
    profiles still get an entry (with an empty set), so their disappearance
    can be detected by `coverage_lost`.
    """
    snapshot: dict[str, set[str]] = {
        p.name: set() for p in profiles.get(ProfileCategory.MACHINE, [])
    }

    for category in (ProfileCategory.FILAMENT, ProfileCategory.PROCESS):
        for profile in profiles.get(category, []):
            key = f"{category.value}:{profile.name}"
            cp = profile.compatible_printers
            if not cp:
                snapshot.setdefault(ALL_PRINTERS_KEY, set()).add(key)
                continue
            for printer in cp:
                snapshot.setdefault(printer, set()).add(key)

    return snapshot


def _format_names(names: list[str]) -> str:
    shown = names[:_MAX_NAMES_SHOWN]
    text = ", ".join(shown)
    remaining = len(names) - len(shown)
    if remaining > 0:
        text += f", ... and {remaining} more"
    return text


def coverage_lost(before: dict[str, set[str]], after: dict[str, set[str]]) -> list[str]:
    """Report per-printer coverage regressions between two snapshots.

    Only regressions are reported (profiles present before but missing
    after); additions are ignored. The ALL_PRINTERS_KEY sentinel is always
    skipped — profiles leaving all-visibility is usually a fix, not a loss.
    A printer whose key disappeared entirely is reported as removed.
    """
    lines: list[str] = []

    for printer, before_set in before.items():
        if printer == ALL_PRINTERS_KEY:
            continue

        if printer not in after:
            if before_set:
                lines.append(f"{printer} removed (had {len(before_set)} linked profile(s))")
            continue

        after_set = after[printer]
        lost = before_set - after_set
        if not lost:
            continue

        display_names = sorted(name.split(":", 1)[1] if ":" in name else name for name in lost)
        lines.append(
            f"{printer} lost {len(lost)} profile(s): {_format_names(display_names)}"
        )

    return lines


def new_broken_refs(
    before: dict[ProfileCategory, list[Profile]],
    after: dict[ProfileCategory, list[Profile]],
) -> list[str]:
    """Report broken compatible_printers references introduced by a mutation.

    Uses cleaner.find_broken_references on both snapshots and returns lines
    only for printer names that are broken after but were NOT already
    broken before (pre-existing breakage is not this mutation's fault).
    """
    from .cleaner import find_broken_references

    before_broken = find_broken_references(before)
    after_broken = find_broken_references(after)

    lines: list[str] = []
    for printer_name in sorted(after_broken):
        if printer_name in before_broken:
            continue
        count = len(after_broken[printer_name])
        lines.append(f"new broken reference: '{printer_name}' ({count} profile(s))")

    return lines
