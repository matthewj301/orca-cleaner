"""Tests for duplicate detection classification.

Regression: the content hash must EXCLUDE the "name" field (it mirrors the
filename, so including it hid every real duplicate) and must classify
identical-content groups by whether compatible_printers also matches
(exact_content) or differs (mergeable).
"""

from pathlib import Path

from orcaslicer_cleaner.deduplicator import find_duplicates, recommend_keep
from orcaslicer_cleaner.models import DuplicateGroup, Profile, ProfileCategory, ProfileInfo


def make_profile(
    name: str, settings: dict, category=ProfileCategory.FILAMENT, updated: int = 0
) -> Profile:
    return Profile(
        name=name,
        category=category,
        directory=Path("/tmp"),
        settings={"name": name, **settings},
        info=ProfileInfo(updated_time=updated),
    )


BASE = {"nozzle_temperature": ["255"], "fan_speed": ["80"]}


def find(groups, match_type):
    return [g for g in groups if g.match_type == match_type]


class TestExactContentDetection:
    def test_identical_settings_different_names_detected(self):
        """The old hash included "name", so these were invisible."""
        a = make_profile("ABS - Generic (0.4mm)", {**BASE, "compatible_printers": ["M1"]})
        b = make_profile("ABS - Fusion Filament (0.4mm)", {**BASE, "compatible_printers": ["M1"]})
        groups = find_duplicates({ProfileCategory.FILAMENT: [a, b]})
        exact = find(groups, "exact_content")
        assert len(exact) == 1
        assert {p.name for p in exact[0].profiles} == {a.name, b.name}

    def test_different_settings_not_grouped(self):
        a = make_profile("ABS - A", {**BASE, "compatible_printers": ["M1"]})
        b = make_profile("PETG - B", {"nozzle_temperature": ["230"], "compatible_printers": ["M1"]})
        groups = find_duplicates({ProfileCategory.FILAMENT: [a, b]})
        assert not find(groups, "exact_content")
        assert not find(groups, "mergeable")


class TestMergeableDetection:
    def test_same_content_different_printers_is_mergeable(self):
        a = make_profile("PLA - X (M1)", {**BASE, "compatible_printers": ["Machine One"]})
        b = make_profile("PLA - X (M2)", {**BASE, "compatible_printers": ["Machine Two"]})
        groups = find_duplicates({ProfileCategory.FILAMENT: [a, b]})
        mergeable = find(groups, "mergeable")
        assert len(mergeable) == 1
        assert "merge" in mergeable[0].details

    def test_same_content_same_printers_is_exact_not_mergeable(self):
        a = make_profile("PLA - X", {**BASE, "compatible_printers": ["M1", "M2"]})
        b = make_profile("PLA - X copy", {**BASE, "compatible_printers": ["M2", "M1"]})
        groups = find_duplicates({ProfileCategory.FILAMENT: [a, b]})
        assert len(find(groups, "exact_content")) == 1
        assert not find(groups, "mergeable")


class TestRecommendKeep:
    def test_exact_group_prefers_standard_name_over_recency(self):
        """For identical content, the convention-following name wins even if
        the non-standard one is newer (updated_time is only sync time)."""
        nonstandard = make_profile(
            "HTPLA - Protopasta - U1 - 0.40mm",
            {**BASE, "compatible_printers": ["Snapmaker U1 - 0.40mm"]},
            updated=2000,
        )
        standard = make_profile(
            "HTPLA - Protopasta - U1 - 0.4mm",
            {**BASE, "compatible_printers": ["Snapmaker U1 - 0.40mm"]},
            updated=1000,
        )
        group = DuplicateGroup(
            profiles=[nonstandard, standard],
            similarity_score=1.0,
            match_type="exact_content",
        )
        assert recommend_keep(group) is standard

    def test_exact_group_tie_breaks_on_recency(self):
        older = make_profile("PLA - A", {**BASE, "compatible_printers": ["M1"]}, updated=1000)
        newer = make_profile("PLA - B", {**BASE, "compatible_printers": ["M1"]}, updated=2000)
        group = DuplicateGroup(
            profiles=[older, newer], similarity_score=1.0, match_type="exact_content"
        )
        assert recommend_keep(group) is newer

    def test_machines_never_form_exact_content_groups(self):
        """Regression (2026-07-10 data loss): two machines whose settings
        differ only by name are different printers, not duplicates."""
        a = make_profile(
            "Doomcube - LGX Lite Pro - TeaKettle - 0.4mm",
            {"inherits": "Base", "retraction_length": ["0.5"]},
            category=ProfileCategory.MACHINE,
        )
        b = make_profile(
            "Doomcube - WWBMG - TeaKettle - 0.4mm",
            {"inherits": "Base", "retraction_length": ["0.5"]},
            category=ProfileCategory.MACHINE,
        )
        groups = find_duplicates({ProfileCategory.MACHINE: [a, b]})
        assert not find(groups, "exact_content")
        assert not find(groups, "mergeable")

    def test_variant_group_still_prefers_recency(self):
        """Beta/test variants differ in content — latest tune wins even if
        its name is non-standard."""
        base = make_profile("PLA - X", {**BASE, "compatible_printers": ["M1"]}, updated=1000)
        beta = make_profile(
            "PLA - X - beta",
            {"nozzle_temperature": ["260"], "compatible_printers": ["M1"]},
            updated=2000,
        )
        group = DuplicateGroup(
            profiles=[base, beta], similarity_score=0.9, match_type="name_similar"
        )
        assert recommend_keep(group) is beta
