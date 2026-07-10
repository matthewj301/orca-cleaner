"""Tests for the destructive-operation blast-radius safety layer.

Added after a real data-loss incident (see the DATA-LOSS GUARDS note in
CLAUDE.md). Covers: machine-touch warnings, category percentage/absolute
thresholds, bulk-operation threshold, routine (no-warning) plans, coverage
snapshotting (including *ALL* and zero-linked machines), coverage
regression reporting, and detection of newly-introduced broken references.
"""

from pathlib import Path

from orcaslicer_cleaner.models import Profile, ProfileCategory, ProfileInfo
from orcaslicer_cleaner.safety import (
    ALL_PRINTERS_KEY,
    assess_blast_radius,
    coverage_lost,
    coverage_snapshot,
    new_broken_refs,
)


def make_profile(
    name: str,
    settings: dict,
    category=ProfileCategory.FILAMENT,
    updated: int = 0,
) -> Profile:
    return Profile(
        name=name,
        category=category,
        directory=Path("/tmp"),
        settings={"name": name, **settings},
        info=ProfileInfo(updated_time=updated),
    )


def make_machine(name: str) -> Profile:
    return make_profile(name, {}, category=ProfileCategory.MACHINE)


# ---------------------------------------------------------------------------
# assess_blast_radius
# ---------------------------------------------------------------------------


class TestMachineTouchWarning:
    def test_archiving_a_machine_warns(self):
        m1 = make_machine("Doomcube - LGX Lite Pro - TeaKettle - 0.4mm")
        profiles = {ProfileCategory.MACHINE: [m1], ProfileCategory.FILAMENT: []}
        result = assess_blast_radius(profiles, to_archive=[m1])
        assert result.requires_hard_confirm
        assert any("touches machine profile(s)" in w and m1.name in w for w in result.warnings)

    def test_modifying_a_machine_warns(self):
        m1 = make_machine("Doomcube - LGX Lite Pro - TeaKettle - 0.4mm")
        profiles = {ProfileCategory.MACHINE: [m1], ProfileCategory.FILAMENT: []}
        result = assess_blast_radius(profiles, to_archive=[], to_modify=[m1])
        assert result.requires_hard_confirm
        assert any("touches machine profile(s)" in w and m1.name in w for w in result.warnings)

    def test_multiple_machines_named_in_one_warning(self):
        m1 = make_machine("Machine A")
        m2 = make_machine("Machine B")
        profiles = {ProfileCategory.MACHINE: [m1, m2], ProfileCategory.FILAMENT: []}
        result = assess_blast_radius(profiles, to_archive=[m1, m2])
        machine_warnings = [w for w in result.warnings if "touches machine profile(s)" in w]
        assert len(machine_warnings) == 1
        assert "Machine A" in machine_warnings[0]
        assert "Machine B" in machine_warnings[0]

    def test_filament_only_archive_no_machine_warning(self):
        m1 = make_machine("Machine A")
        f1 = make_profile("PLA - Generic", {"compatible_printers": ["Machine A"]})
        profiles = {ProfileCategory.MACHINE: [m1], ProfileCategory.FILAMENT: [f1]}
        result = assess_blast_radius(profiles, to_archive=[f1])
        assert not any("touches machine profile(s)" in w for w in result.warnings)


class TestPercentageThreshold:
    def _filament_pool(self, n: int) -> list[Profile]:
        return [make_profile(f"Filament {i}", {}) for i in range(n)]

    def test_two_of_ten_no_warning(self):
        pool = self._filament_pool(10)
        profiles = {ProfileCategory.MACHINE: [], ProfileCategory.FILAMENT: pool}
        result = assess_blast_radius(profiles, to_archive=pool[:2])
        assert not result.warnings
        assert not result.requires_hard_confirm

    def test_three_of_ten_warns(self):
        pool = self._filament_pool(10)
        profiles = {ProfileCategory.MACHINE: [], ProfileCategory.FILAMENT: pool}
        result = assess_blast_radius(profiles, to_archive=pool[:3])
        assert result.requires_hard_confirm
        assert any("archives 3 of 10 filament profiles (30%)" in w for w in result.warnings)

    def test_absolute_floor_prevents_small_category_false_positive(self):
        # 2 of 3 is 67% but below the absolute floor of 3 archived profiles.
        pool = self._filament_pool(3)
        profiles = {ProfileCategory.MACHINE: [], ProfileCategory.FILAMENT: pool}
        result = assess_blast_radius(profiles, to_archive=pool[:2])
        assert not result.warnings


class TestBulkThreshold:
    def test_twenty_profiles_warns_regardless_of_category_split(self):
        filaments = [make_profile(f"F{i}", {}) for i in range(50)]
        processes = [
            make_profile(f"P{i}", {}, category=ProfileCategory.PROCESS) for i in range(50)
        ]
        profiles = {
            ProfileCategory.MACHINE: [],
            ProfileCategory.FILAMENT: filaments,
            ProfileCategory.PROCESS: processes,
        }
        # 10 from each category: below the 15% per-category threshold but
        # totals 20, tripping the bulk threshold.
        to_archive = filaments[:10] + processes[:10]
        result = assess_blast_radius(profiles, to_archive=to_archive)
        assert result.requires_hard_confirm
        assert any("archives 20 profiles in one operation" in w for w in result.warnings)

    def test_nineteen_profiles_no_bulk_warning(self):
        filaments = [make_profile(f"F{i}", {}) for i in range(50)]
        profiles = {ProfileCategory.MACHINE: [], ProfileCategory.FILAMENT: filaments}
        result = assess_blast_radius(profiles, to_archive=filaments[:19])
        assert not any("profiles in one operation" in w for w in result.warnings)


class TestRoutineOperation:
    def test_small_plan_no_warnings(self):
        m1 = make_machine("Machine A")
        filaments = [make_profile(f"F{i}", {"compatible_printers": ["Machine A"]}) for i in range(10)]
        profiles = {ProfileCategory.MACHINE: [m1], ProfileCategory.FILAMENT: filaments}
        result = assess_blast_radius(profiles, to_archive=filaments[:1])
        assert result.warnings == []
        assert not result.requires_hard_confirm


# ---------------------------------------------------------------------------
# coverage_snapshot
# ---------------------------------------------------------------------------


class TestCoverageSnapshot:
    def test_maps_machine_to_linked_profiles(self):
        m1 = make_machine("Machine A")
        m2 = make_machine("Machine B")
        f1 = make_profile("PLA - X", {"compatible_printers": ["Machine A"]})
        p1 = make_profile(
            "0.20mm - Standard (Machine B - 0.4mm)",
            {"compatible_printers": ["Machine B"]},
            category=ProfileCategory.PROCESS,
        )
        profiles = {
            ProfileCategory.MACHINE: [m1, m2],
            ProfileCategory.FILAMENT: [f1],
            ProfileCategory.PROCESS: [p1],
        }
        snap = coverage_snapshot(profiles)
        assert snap["Machine A"] == {"filament:PLA - X"}
        assert snap["Machine B"] == {"process:0.20mm - Standard (Machine B - 0.4mm)"}

    def test_empty_compatible_printers_goes_under_all_key(self):
        m1 = make_machine("Machine A")
        f1 = make_profile("PLA - Generic", {"compatible_printers": []})
        profiles = {ProfileCategory.MACHINE: [m1], ProfileCategory.FILAMENT: [f1]}
        snap = coverage_snapshot(profiles)
        assert snap[ALL_PRINTERS_KEY] == {"filament:PLA - Generic"}
        assert snap["Machine A"] == set()

    def test_machine_with_zero_linked_profiles_still_present(self):
        m1 = make_machine("Lonely Machine")
        profiles = {ProfileCategory.MACHINE: [m1], ProfileCategory.FILAMENT: []}
        snap = coverage_snapshot(profiles)
        assert "Lonely Machine" in snap
        assert snap["Lonely Machine"] == set()

    def test_profile_linked_to_multiple_machines_appears_under_each(self):
        m1 = make_machine("Machine A")
        m2 = make_machine("Machine B")
        f1 = make_profile("PLA - Shared", {"compatible_printers": ["Machine A", "Machine B"]})
        profiles = {
            ProfileCategory.MACHINE: [m1, m2],
            ProfileCategory.FILAMENT: [f1],
        }
        snap = coverage_snapshot(profiles)
        assert snap["Machine A"] == {"filament:PLA - Shared"}
        assert snap["Machine B"] == {"filament:PLA - Shared"}


# ---------------------------------------------------------------------------
# coverage_lost
# ---------------------------------------------------------------------------


class TestCoverageLost:
    def test_partial_loss_reported(self):
        before = {"Machine A": {"filament:PLA - X", "filament:PETG - Y"}}
        after = {"Machine A": {"filament:PLA - X"}}
        lines = coverage_lost(before, after)
        assert len(lines) == 1
        assert "Machine A lost 1 profile(s): PETG - Y" in lines[0]

    def test_full_printer_removal_reported(self):
        before = {"Machine A": {"filament:PLA - X"}}
        after: dict[str, set] = {}
        lines = coverage_lost(before, after)
        assert lines == ["Machine A removed (had 1 linked profile(s))"]

    def test_printer_removed_with_zero_linked_profiles_not_reported(self):
        before = {"Machine A": set()}
        after: dict[str, set] = {}
        lines = coverage_lost(before, after)
        assert lines == []

    def test_additions_ignored(self):
        before = {"Machine A": {"filament:PLA - X"}}
        after = {"Machine A": {"filament:PLA - X", "filament:PETG - New"}}
        lines = coverage_lost(before, after)
        assert lines == []

    def test_all_key_shrinking_ignored(self):
        before = {ALL_PRINTERS_KEY: {"filament:PLA - Generic", "filament:PETG - Generic"}}
        after = {ALL_PRINTERS_KEY: {"filament:PLA - Generic"}}
        lines = coverage_lost(before, after)
        assert lines == []

    def test_all_key_fully_removed_still_ignored(self):
        before = {ALL_PRINTERS_KEY: {"filament:PLA - Generic"}}
        after: dict[str, set] = {}
        lines = coverage_lost(before, after)
        assert lines == []

    def test_no_change_no_lines(self):
        before = {"Machine A": {"filament:PLA - X"}}
        after = {"Machine A": {"filament:PLA - X"}}
        lines = coverage_lost(before, after)
        assert lines == []

    def test_name_list_capped_with_more_suffix(self):
        lost_names = {f"filament:F{i}" for i in range(12)}
        before = {"Machine A": lost_names}
        after = {"Machine A": set()}
        lines = coverage_lost(before, after)
        assert len(lines) == 1
        assert "lost 12 profile(s)" in lines[0]
        assert "... and 4 more" in lines[0]


# ---------------------------------------------------------------------------
# new_broken_refs
# ---------------------------------------------------------------------------


class TestNewBrokenRefs:
    def test_reports_only_newly_broken(self):
        m1 = make_machine("Machine A")
        # Before: filament references "Machine A" (fine) and "Ghost Printer" (already broken).
        f_before = make_profile(
            "PLA - X", {"compatible_printers": ["Machine A", "Ghost Printer"]}
        )
        before = {ProfileCategory.MACHINE: [m1], ProfileCategory.FILAMENT: [f_before]}

        # After: machine "Machine A" got renamed/removed, so its reference breaks too,
        # while "Ghost Printer" remains broken (pre-existing, should not be reported).
        f_after = make_profile(
            "PLA - X", {"compatible_printers": ["Machine A", "Ghost Printer"]}
        )
        after = {ProfileCategory.MACHINE: [], ProfileCategory.FILAMENT: [f_after]}

        lines = new_broken_refs(before, after)
        assert len(lines) == 1
        assert "new broken reference: 'Machine A' (1 profile(s))" in lines[0]
        assert not any("Ghost Printer" in line for line in lines)

    def test_no_new_breakage_no_lines(self):
        m1 = make_machine("Machine A")
        f1 = make_profile("PLA - X", {"compatible_printers": ["Machine A"]})
        before = {ProfileCategory.MACHINE: [m1], ProfileCategory.FILAMENT: [f1]}
        after = {ProfileCategory.MACHINE: [m1], ProfileCategory.FILAMENT: [f1]}
        lines = new_broken_refs(before, after)
        assert lines == []

    def test_preexisting_breakage_alone_not_reported(self):
        f1 = make_profile("PLA - X", {"compatible_printers": ["Ghost Printer"]})
        before = {ProfileCategory.MACHINE: [], ProfileCategory.FILAMENT: [f1]}
        after = {ProfileCategory.MACHINE: [], ProfileCategory.FILAMENT: [f1]}
        lines = new_broken_refs(before, after)
        assert lines == []
